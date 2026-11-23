from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..strategies import REGISTRY
from .jobs import JobManager, QueueFull

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def create_app(runs_dir: str | Path = "runs", jobs: JobManager | None = None) -> FastAPI:
    runs = Path(runs_dir)
    state = {"jobs": jobs}

    @asynccontextmanager
    async def lifespan(app):
        if state["jobs"] is None:
            state["jobs"] = JobManager()
        yield
        state["jobs"].shutdown()

    app = FastAPI(title="quantdeck", lifespan=lifespan)

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

    @app.get("/api/runs/{name}/equity")
    def get_run_equity(name: str, limit: int = 2000):
        f = run_dir(name) / "equity.csv"
        if not f.exists():
            return {"ts": [], "equity": []}
        lines = f.read_text(encoding="utf-8").splitlines()[1:][-limit:]
        ts, eq = [], []
        for line in lines:
            t, _, v = line.partition(",")
            ts.append(t)
            eq.append(float(v))
        return {"ts": ts, "equity": eq}

    @app.post("/api/runs/{name}/stop")
    def stop_run(name: str):
        # the runner checks for this file every poll and shuts down cleanly
        (run_dir(name) / "STOP").touch()
        return {"ok": True}

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/")
        def index():
            return FileResponse(WEB_DIR / "index.html")

    return app
