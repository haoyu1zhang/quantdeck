import json

import pandas as pd
import pytest

from conftest import make_bars
from quantdeck.brokers.ccxt_broker import CcxtBroker
from quantdeck.brokers.sim import SimBroker
from quantdeck.config import config_from_dict
from quantdeck.data.sources import DataSource
from quantdeck.live.clock import AlwaysOpen, UsEquityClock
from quantdeck.live.runner import LiveRunner
from quantdeck.models import Order, OrderStatus, Side
from quantdeck.strategies import Strategy, load_strategy


class GrowingSource(DataSource):
    """Hands out history and reveals one more bar each time it's polled."""

    def __init__(self, df, visible):
        self.df, self.visible = df, visible

    def fetch(self, symbol, timeframe, start=None, end=None):
        return self.df

    def latest(self, symbol, timeframe, n):
        out = self.df.iloc[: self.visible].tail(n)
        self.visible += 1
        return out


class AlwaysBuy(Strategy):
    params = {}

    def on_bar(self, ctx, bar):
        if ctx.position(bar.symbol) == 0 and not ctx.open_orders(bar.symbol):
            ctx.order_target_percent(bar.symbol, 0.5)


def make_cfg(tmp_path, **kw):
    return config_from_dict({
        "name": "t", "mode": "paper", "symbols": ["BTC/USDT"], "cash": 1000, "poll_seconds": 0,
        "runs_dir": str(tmp_path), "data": {"source": "ccxt", "exchange": "x", "timeframe": "1h", "warmup": 50},
        **kw,
    })


def test_local_paper_run_trades_and_writes_status(tmp_path):
    end = pd.Timestamp.now(tz="UTC").floor("h") - pd.Timedelta(hours=1)
    df = make_bars(120, freq="h", end=end)
    cfg = make_cfg(tmp_path)
    broker = SimBroker(cfg.cash, cfg.fees)
    runner = LiveRunner(cfg, AlwaysBuy(), broker, GrowingSource(df, 100), clock=AlwaysOpen())
    runner.run(max_loops=5)

    status = json.loads((tmp_path / "t" / "status.json").read_text())
    assert status["state"] == "stopped"
    assert status["recent_orders"], "strategy should have placed an order"
    assert broker.positions()["BTC/USDT"].qty > 0
    assert (tmp_path / "t" / "sim_state.json").exists()
    assert (tmp_path / "t" / "journal.jsonl").read_text().count('"kind": "fill"') >= 1
    assert not (tmp_path / "t" / "run.lock").exists()

    # restart picks the account back up
    broker2 = SimBroker(cfg.cash, cfg.fees)
    r2 = LiveRunner(cfg, AlwaysBuy(), broker2, GrowingSource(df, 110), clock=AlwaysOpen())
    r2.run(max_loops=1)
    assert broker2.positions()["BTC/USDT"].qty == pytest.approx(broker.positions()["BTC/USDT"].qty)


def test_stop_file_stops_runner(tmp_path):
    df = make_bars(100, freq="h", end=pd.Timestamp.now(tz="UTC").floor("h") - pd.Timedelta(hours=1))
    cfg = make_cfg(tmp_path)
    runner = LiveRunner(cfg, load_strategy("sma_cross"), SimBroker(1000), GrowingSource(df, 90), clock=AlwaysOpen())

    original = runner._start

    def start_then_request_stop():
        original()
        (tmp_path / "t" / "STOP").touch()

    runner._start = start_then_request_stop
    runner.run(max_loops=100)
    assert json.loads((tmp_path / "t" / "status.json").read_text())["state"] == "stopped"


def test_forming_bar_is_ignored(tmp_path):
    now = pd.Timestamp.now(tz="UTC")
    df = make_bars(5, freq="h", end=now.floor("h"))  # last bar still open
    runner = LiveRunner(make_cfg(tmp_path), AlwaysBuy(), SimBroker(1000), GrowingSource(df, 5), clock=AlwaysOpen())
    bars = runner._closed_bars("BTC/USDT", df)
    assert len(bars) == 4


def test_equity_clock_fallback():
    c = UsEquityClock()
    assert c.is_open(pd.Timestamp("2024-03-05 15:00", tz="UTC").to_pydatetime())      # Tue 10:00 ET
    assert not c.is_open(pd.Timestamp("2024-03-05 22:00", tz="UTC").to_pydatetime())  # after close
    assert not c.is_open(pd.Timestamp("2024-03-09 15:00", tz="UTC").to_pydatetime())  # Saturday


# --- ccxt adapter against a fake exchange ---------------------------------------


class NetworkError(Exception):
    pass


class FakeExchange:
    has = {"createOrder": True, "fetchClosedOrders": True}

    def __init__(self, fail_first_create=False):
        self.orders = {}
        self.fail_first_create = fail_first_create
        self.create_calls = 0
        self.balance = {"USDT": 1000.0, "BTC": 0.0}

    def load_markets(self):
        pass

    def market(self, s):
        return {"base": s.split("/")[0], "quote": s.split("/")[1],
                "limits": {"amount": {"min": 0.001}, "cost": {"min": 5}}}

    def amount_to_precision(self, s, x):
        return f"{int(x * 1000) / 1000:.3f}"

    def price_to_precision(self, s, x):
        return f"{x:.2f}"

    def fetch_tickers(self, symbols):
        return {s: {"last": 100.0} for s in symbols}

    def fetch_balance(self):
        return {"total": dict(self.balance), "free": dict(self.balance)}

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        self.create_calls += 1
        oid = str(len(self.orders) + 1)
        o = {"id": oid, "symbol": symbol, "type": type, "side": side, "amount": amount, "filled": 0,
             "status": "open", "clientOrderId": (params or {}).get("clientOrderId"), "price": price}
        self.orders[oid] = o
        if self.fail_first_create and self.create_calls == 1:
            raise NetworkError("timed out")  # order landed, response lost
        return o

    def fetch_open_orders(self, symbol):
        return [o for o in self.orders.values() if o["status"] == "open" and o["symbol"] == symbol]

    def fetch_closed_orders(self, symbol, limit=50):
        return [o for o in self.orders.values() if o["status"] != "open"]

    def fetch_order(self, oid, symbol):
        return self.orders[oid]

    def cancel_order(self, oid, symbol):
        self.orders[oid]["status"] = "canceled"


def test_ccxt_rejects_below_minimum():
    b = CcxtBroker("fake", ["BTC/USDT"], client=FakeExchange())
    b.account()
    o = b.submit(Order("BTC/USDT", Side.BUY, 0.0004))
    assert o.status == OrderStatus.REJECTED
    o = b.submit(Order("BTC/USDT", Side.BUY, 0.01))  # 1 USDT notional < 5
    assert o.status == OrderStatus.REJECTED


def test_ccxt_timeout_does_not_double_order():
    ex = FakeExchange(fail_first_create=True)
    b = CcxtBroker("fake", ["BTC/USDT"], client=ex)
    b.account()
    o = b.submit(Order("BTC/USDT", Side.BUY, 0.5))
    assert o.id == "1"
    assert len(ex.orders) == 1, "retry must find the order that already landed"


def test_ccxt_sync_reports_fills():
    ex = FakeExchange()
    b = CcxtBroker("fake", ["BTC/USDT"], client=ex)
    b.account()
    o = b.submit(Order("BTC/USDT", Side.BUY, 0.5))
    ex.orders[o.id].update(filled=0.2, average=100.0)
    f1 = b.sync()
    ex.orders[o.id].update(filled=0.5, average=101.2, status="closed")
    ex.balance["BTC"] = 0.5
    f2 = b.sync()
    assert [round(f.qty, 6) for f in f1 + f2] == [0.2, 0.3]
    assert f2[0].price == pytest.approx((0.5 * 101.2 - 0.2 * 100) / 0.3)
    assert b.sync() == []
