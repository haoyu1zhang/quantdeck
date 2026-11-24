"""Runs pipeline passes on demand from the dashboard, one at a time, off the request thread."""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from .config import NewsConfig
from .models import utcnow

log = logging.getLogger(__name__)


class Refresher:
    def __init__(self, cfg: NewsConfig, pipeline_factory=None) -> None:
        self.cfg = cfg
        self._factory = pipeline_factory or self._default_factory
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._queued: set[str] | None = None   # sources to run again after the current pass; empty set = all
        self.status: dict = {"state": "idle"}

    def _default_factory(self):
        from .pipeline import NewsPipeline

        return NewsPipeline(self.cfg)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, only: list[str] | None = None) -> dict:
        """Start a pass over all sources, or just `only`. If one is running, queue another."""
        with self._lock:
            if self.running:
                if only is None:
                    self._queued = set()
                elif self._queued is None:
                    self._queued = set(only)
                elif self._queued:
                    self._queued |= set(only)
                return dict(self.status)
            self._launch(only)
            return dict(self.status)

    def _launch(self, only):
        self.status = {"state": "running", "started": utcnow().isoformat(), "scope": only or "all"}
        self._thread = threading.Thread(target=self._run, args=(only,), name="news-refresh", daemon=True)
        self._thread.start()

    def _run(self, only):
        started = datetime.fromisoformat(self.status["started"])
        try:
            pipe = self._factory()
            try:
                rep = pipe.run_once(force=only is None, only=only)
            finally:
                pipe.store.close()
            result = {
                "state": "done", "fetched": rep.fetched, "errors": rep.errors, "new_items": rep.new_items,
                "scored": rep.scored, "seconds": round(rep.seconds, 1),
            }
        except Exception as e:
            log.exception("news refresh failed")
            result = {"state": "failed", "error": f"{type(e).__name__}: {e}"[:300]}
        result.update(started=started.isoformat(), finished=utcnow().isoformat(), scope=only or "all")
        with self._lock:
            self.status = result
            queued, self._queued = self._queued, None
            if queued is not None:
                self._launch(sorted(queued) or None)

    def wait(self, timeout: float = 30) -> dict:
        """For tests and scripts: block until no pass is running."""
        end = threading.Event()
        for _ in range(int(timeout * 20)):
            t = self._thread
            if t is None or not t.is_alive():
                with self._lock:
                    if not self.running:
                        return dict(self.status)
            end.wait(0.05)
        return dict(self.status)
