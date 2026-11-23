from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import Executor, Future, ProcessPoolExecutor

log = logging.getLogger(__name__)


def run_backtest_job(raw_cfg: dict) -> dict:
    """Runs in a worker process. Takes and returns plain dicts so it pickles cheaply."""
    from ..backtest import result_to_dict, run_backtest
    from ..config import config_from_dict
    from ..data import load_history
    from ..strategies import load_strategy

    cfg = config_from_dict({**raw_cfg, "mode": "backtest"})
    d = cfg.data
    data = load_history(d.source, cfg.market, cfg.symbols, d.timeframe, d.start, d.end,
                        exchange=d.exchange, path=d.path)
    strategy = load_strategy(cfg.strategy.name, cfg.strategy.params)
    res = run_backtest(strategy, data, cfg.cash, cfg.fees, cfg.risk, cfg.market, d.timeframe, cfg.fractional)
    return result_to_dict(res)


class QueueFull(RuntimeError):
    pass


class JobManager:
    """Backtests run in a process pool so a long one never blocks the API.

    Identical requests share one job (and its cached result), and the number of
    queued jobs is capped so a burst of clicks can't pile up unbounded work.
    """

    def __init__(
        self,
        executor: Executor | None = None,
        max_pending: int = 16,
        cache_size: int = 64,
        ttl: float = 3600,
        func=run_backtest_job,
    ) -> None:
        workers = max(1, min(4, (os.cpu_count() or 2) - 1))
        self.executor = executor or ProcessPoolExecutor(max_workers=workers)
        self.max_pending = max_pending
        self.ttl = ttl
        self.func = func
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._by_key: OrderedDict[str, str] = OrderedDict()
        self._cache_size = cache_size

    @staticmethod
    def key_for(raw_cfg: dict) -> str:
        blob = json.dumps(raw_cfg, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:24]

    def submit(self, raw_cfg: dict) -> dict:
        key = self.key_for(raw_cfg)
        with self._lock:
            self._gc()
            existing = self._by_key.get(key)
            if existing and existing in self._jobs and self._jobs[existing]["status"] != "failed":
                self._by_key.move_to_end(key)
                return self._public(existing)

            pending = sum(1 for j in self._jobs.values() if j["status"] in ("queued", "running"))
            if pending >= self.max_pending:
                raise QueueFull(f"{pending} backtests already queued, try again shortly")

            job_id = uuid.uuid4().hex[:12]
            job = {"id": job_id, "key": key, "status": "queued", "created": time.time(),
                   "result": None, "error": None}
            self._jobs[job_id] = job
            self._by_key[key] = job_id
            while len(self._by_key) > self._cache_size:
                old_key, old_id = self._by_key.popitem(last=False)
                self._jobs.pop(old_id, None)

        fut = self.executor.submit(self.func, raw_cfg)
        with self._lock:
            job["status"] = "running" if not fut.done() else job["status"]
        fut.add_done_callback(lambda f, jid=job_id: self._done(jid, f))
        return self._public(job_id)

    def _done(self, job_id: str, fut: Future) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            try:
                job["result"] = fut.result()
                job["status"] = "done"
            except Exception as e:
                log.warning("backtest %s failed: %s", job_id, e)
                job["error"] = str(e)
                job["status"] = "failed"
            job["finished"] = time.time()

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            if job_id not in self._jobs:
                return None
            return self._public(job_id, with_result=True)

    def _public(self, job_id: str, with_result: bool = False) -> dict:
        j = self._jobs[job_id]
        out = {"id": j["id"], "status": j["status"], "error": j["error"]}
        if with_result and j["status"] == "done":
            out["result"] = j["result"]
        return out

    def _gc(self) -> None:
        cutoff = time.time() - self.ttl
        for jid in [k for k, j in self._jobs.items() if j.get("finished", time.time()) < cutoff]:
            key = self._jobs.pop(jid)["key"]
            if self._by_key.get(key) == jid:
                del self._by_key[key]

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)
