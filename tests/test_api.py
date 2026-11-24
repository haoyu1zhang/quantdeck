import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from quantdeck.api.jobs import JobManager
from quantdeck.api.server import create_app


@pytest.fixture
def client(tmp_path):
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "status.json").write_text(json.dumps(
        {"name": "demo", "mode": "paper", "state": "running", "positions": [], "recent_orders": []}))
    from quantdeck.storage import TradeStore

    st = TradeStore(tmp_path / "demo" / "trades.db")
    st.record_equity("2024-01-01T00:00:00+00:00", 100)
    st.close()
    jobs = JobManager(executor=ThreadPoolExecutor(1), func=lambda cfg: {"ok": True})
    with TestClient(create_app(tmp_path, jobs)) as c:
        yield c, tmp_path


def test_strategies_and_index(client):
    c, _ = client
    names = [s["name"] for s in c.get("/api/strategies").json()]
    assert "sma_cross" in names
    assert c.get("/").status_code == 200


def test_runs(client):
    c, root = client
    assert c.get("/api/runs").json()[0]["name"] == "demo"
    assert c.get("/api/runs/demo/equity").json()["equity"] == [100.0]
    assert c.get("/api/runs/demo/stats").json()["trades"] == 0
    assert c.post("/api/runs/demo/stop").status_code == 200
    assert (root / "demo" / "STOP").exists()
    assert c.get("/api/runs/..%2f..%2fetc").status_code in (400, 404)
    assert c.get("/api/runs/missing").status_code == 404


def test_backtest_validation(client):
    c, _ = client
    bad = {"symbols": ["X"], "strategy": {"name": "os:system"}}
    assert c.post("/api/backtests", json=bad).status_code == 400
    csv = {"symbols": ["X"], "strategy": {"name": "sma_cross"}, "data": {"source": "csv", "path": "/etc"}}
    assert c.post("/api/backtests", json=csv).status_code == 400
    ok = c.post("/api/backtests", json={"symbols": ["X"], "strategy": {"name": "sma_cross"}})
    assert ok.status_code == 202


def test_token_auth(tmp_path):
    jobs = JobManager(executor=ThreadPoolExecutor(1), func=lambda cfg: {"ok": True})
    with TestClient(create_app(tmp_path, jobs, token="s3cret-token-value")) as c:
        assert c.get("/").status_code == 200  # page loads, then asks for the token
        assert c.get("/api/strategies").status_code == 401
        ok = c.get("/api/strategies", headers={"Authorization": "Bearer s3cret-token-value"})
        assert ok.status_code == 200
        for _ in range(12):
            c.get("/api/runs", headers={"Authorization": "Bearer wrong"})
        locked = c.get("/api/strategies", headers={"Authorization": "Bearer s3cret-token-value"})
        assert locked.status_code == 429
