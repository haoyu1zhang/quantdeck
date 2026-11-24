from __future__ import annotations

import hmac
import json
import re
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..storage import TradeStore
from ..strategies import REGISTRY
from .jobs import JobManager, QueueFull

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def clean_for_json_deep(x):
    import math

    if isinstance(x, dict):
        return {k: clean_for_json_deep(v) for k, v in x.items()}
    if isinstance(x, list):
        return [clean_for_json_deep(v) for v in x]
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return x


class _FailureLimiter:
    """Locks an IP out for a while after too many bad tokens."""

    def __init__(self, max_failures: int = 10, window: float = 300) -> None:
        self.max_failures, self.window = max_failures, window
        self._fails: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def blocked(self, ip: str) -> bool:
        with self._lock:
            q = self._fails[ip]
            while q and q[0] < time.monotonic() - self.window:
                q.popleft()
            return len(q) >= self.max_failures

    def fail(self, ip: str) -> None:
        with self._lock:
            self._fails[ip].append(time.monotonic())


def create_app(runs_dir: str | Path = "runs", jobs: JobManager | None = None, token: str | None = None,
               news_db: str | Path | None = None, news_config: str | Path | None = None) -> FastAPI:
    runs = Path(runs_dir)
    news_path = Path(news_db) if news_db else runs / "news" / "news.db"
    refresher = None
    if news_config:
        from ..news import load_news_config
        from ..news.refresher import Refresher

        ncfg = load_news_config(news_config)
        news_path = ncfg.db_path
        refresher = Refresher(ncfg)
    state = {"jobs": jobs}
    limiter = _FailureLimiter()

    @asynccontextmanager
    async def lifespan(app):
        if state["jobs"] is None:
            state["jobs"] = JobManager()
        yield
        state["jobs"].shutdown()

    app = FastAPI(title="quantdeck", lifespan=lifespan)

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        if not token or not request.url.path.startswith("/api/"):
            return await call_next(request)
        ip = request.client.host if request.client else "?"
        if limiter.blocked(ip):
            return JSONResponse({"detail": "too many failed attempts, try later"}, status_code=429)
        header = request.headers.get("authorization", "")
        given = header[7:] if header.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(given.encode(), token.encode()):
            limiter.fail(ip)
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    def run_dir(name: str) -> Path:
        if not _NAME.match(name):
            raise HTTPException(400, "bad run name")
        d = runs / name
        if not (d / "status.json").exists():
            raise HTTPException(404, "no such run")
        return d

    @app.get("/api/strategies")
    def strategies():
        return [
            {"name": name, "params": cls.params, "doc": (cls.__doc__ or "").strip()}
            for name, cls in REGISTRY.items()
        ]

    @app.post("/api/backtests", status_code=202)
    def submit_backtest(cfg: dict = Body(...)):
        # only built-in strategies over HTTP; "module:Class" would let a request import code
        name = (cfg.get("strategy") or {}).get("name", "")
        if name not in REGISTRY:
            raise HTTPException(400, f"unknown strategy {name!r}")
        data = cfg.get("data") or {}
        if data.get("source") == "csv":
            raise HTTPException(400, "csv backtests are CLI only")
        try:
            return state["jobs"].submit(cfg)
        except QueueFull as e:
            return JSONResponse({"detail": str(e)}, status_code=429)

    @app.get("/api/backtests/{job_id}")
    def get_backtest(job_id: str):
        job = state["jobs"].get(job_id)
        if job is None:
            raise HTTPException(404, "unknown job")
        return job

    @app.get("/api/runs")
    def list_runs():
        out = []
        if runs.exists():
            for d in sorted(runs.iterdir()):
                f = d / "status.json"
                if f.exists():
                    try:
                        s = json.loads(f.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    out.append({k: s.get(k) for k in
                                ("name", "mode", "market", "strategy", "state", "heartbeat", "halted", "account")})
        return out

    @app.get("/api/runs/{name}")
    def get_run(name: str):
        return json.loads((run_dir(name) / "status.json").read_text(encoding="utf-8"))

    def store(name: str):
        db = run_dir(name) / "trades.db"
        return TradeStore(db, readonly=True) if db.exists() else None

    @app.get("/api/runs/{name}/equity")
    def get_run_equity(name: str, limit: int = 2000):
        st = store(name)
        if st is None:
            return {"ts": [], "equity": []}
        try:
            eq = st.equity(min(max(limit, 1), 20000))
        finally:
            st.close()
        return {"ts": [t.isoformat() for t in eq.index], "equity": [round(float(v), 6) for v in eq.values]}

    @app.get("/api/runs/{name}/stats")
    def get_run_stats(name: str):
        st = store(name)
        if st is None:
            return {"trades": 0, "realized_pnl": 0.0, "recent_trades": []}
        try:
            return clean_for_json_deep(st.stats())
        finally:
            st.close()

    @app.post("/api/runs/{name}/stop")
    def stop_run(name: str):
        # the runner checks for this file every poll and shuts down cleanly
        (run_dir(name) / "STOP").touch()
        return {"ok": True}

    # --- news -----------------------------------------------------------------

    def news_store():
        from ..news.store import NewsStore

        if not news_path.exists():
            return None
        return NewsStore(news_path, readonly=True)

    @app.get("/api/news/verdicts")
    def news_verdicts():
        st = news_store()
        if st is None:
            return {"available": False, "verdicts": [], "sources": []}
        try:
            return {"available": True, "verdicts": st.latest_verdicts(), "sources": st.sources()}
        finally:
            st.close()

    @app.get("/api/news/items")
    def news_items(symbol: str | None = None, limit: int = 100, hours: float = 72):
        st = news_store()
        if st is None:
            return []
        try:
            return st.items(symbol, limit=min(max(limit, 1), 500), hours=min(max(hours, 1), 24 * 30))
        finally:
            st.close()

    @app.get("/api/news/history")
    def news_history(symbol: str, hours: float = 72):
        st = news_store()
        if st is None:
            return []
        try:
            return st.verdict_history(symbol, hours=min(max(hours, 1), 24 * 30))
        finally:
            st.close()

    @app.post("/api/news/ingest", status_code=202)
    def news_ingest(payload: dict = Body(...)):
        """Queue items from an outside collector; `news watch` tags and scores them next cycle."""
        rows = payload.get("items") if isinstance(payload.get("items"), list) else [payload]
        if not rows or len(rows) > 500:
            raise HTTPException(400, "send 1-500 items")
        process = bool(payload.get("process")) and isinstance(payload.get("items"), list)
        clean = []
        for r in rows:
            if not isinstance(r, dict) or not str(r.get("title", "")).strip():
                raise HTTPException(400, "every item needs a title")
            keep = {k: r[k] for k in ("title", "body", "url", "author", "published_at", "symbols", "engagement",
                                      "credibility", "kind", "lang", "id") if k in r}
            if keep.get("kind") not in (None, "official", "newswire", "media", "social", "filing"):
                raise HTTPException(400, f"bad kind {keep['kind']!r}")
            clean.append(keep)
        inbox = news_path.parent / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        name = f"api-{time.time_ns()}.jsonl"
        tmp = inbox / (name + ".tmp")
        tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in clean) + "\n", encoding="utf-8")
        tmp.replace(inbox / name)
        # process=true: score it now instead of waiting for the next `news watch` cycle
        processing = bool(process and refresher)
        if processing:
            refresher.start(only=["ingest"])
        return {"queued": len(clean), "processing": processing}

    @app.get("/api/news/refresh")
    def news_refresh_status():
        if refresher is None:
            return {"enabled": False, "state": "idle"}
        return {"enabled": True, **refresher.status}

    @app.post("/api/news/refresh", status_code=202)
    def news_refresh():
        if refresher is None:
            raise HTTPException(400, "start the dashboard with --news-config news.yaml to refresh from here")
        return {"enabled": True, **refresher.start()}

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/")
        def index():
            return FileResponse(WEB_DIR / "index.html")

    return app
