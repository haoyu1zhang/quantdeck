import pandas as pd
import pytest

from quantdeck.brokers.sim import SimBroker
from quantdeck.config import FeeConfig
from quantdeck.models import Bar, Order, OrderStatus, OrderType, Side

T0 = pd.Timestamp("2024-01-01", tz="UTC")
T1 = pd.Timestamp("2024-01-02", tz="UTC")


def bar(ts, o, h, l, c, sym="X"):
    return Bar(sym, ts, o, h, l, c, 1.0)


def test_market_order_fills_next_bar_open_with_slippage():
    b = SimBroker(1000, FeeConfig(slippage=0.01))
    o = b.submit(Order("X", Side.BUY, 1, created_at=T0))
    assert b.process_bar(bar(T0, 10, 11, 9, 10)) == []  # same bar: no fill
    fills = b.process_bar(bar(T1, 20, 21, 19, 20))
    assert len(fills) == 1
    assert fills[0].price == pytest.approx(20 * 1.01)
    assert o.status == OrderStatus.FILLED
    assert b.cash == pytest.approx(1000 - 20.2)


def test_limit_and_stop_orders():
    b = SimBroker(1000, FeeConfig(slippage=0))
    lim = b.submit(Order("X", Side.BUY, 1, OrderType.LIMIT, limit_price=9.5, created_at=T0))
    stp = b.submit(Order("X", Side.BUY, 1, OrderType.STOP, stop_price=10.8, created_at=T0))
    b.process_bar(bar(T1, 10, 11, 9, 10.5))
    assert lim.avg_fill_price == pytest.approx(9.5)
    assert stp.avg_fill_price == pytest.approx(10.8)


def test_limit_gap_fills_at_open():
    b = SimBroker(1000, FeeConfig(slippage=0))
    lim = b.submit(Order("X", Side.BUY, 1, OrderType.LIMIT, limit_price=9.5, created_at=T0))
    b.process_bar(bar(T1, 9.0, 9.2, 8.8, 9.1))
    assert lim.avg_fill_price == pytest.approx(9.0)


def test_buy_shrinks_to_available_cash():
    b = SimBroker(100, FeeConfig(slippage=0, rate=0.01))
    o = b.submit(Order("X", Side.BUY, 50, created_at=T0))
    b.process_bar(bar(T1, 10, 10, 10, 10))
    assert o.filled_qty == pytest.approx(100 / 10.1)
    assert b.cash == pytest.approx(0, abs=1e-6)


def test_cannot_sell_what_we_dont_have():
    b = SimBroker(100)
    o = b.submit(Order("X", Side.SELL, 1, created_at=T0))
    b.process_bar(bar(T1, 10, 10, 10, 10))
    assert o.status == OrderStatus.REJECTED


def test_whole_shares_only():
    b = SimBroker(1000, fractional=False)
    o = b.submit(Order("X", Side.BUY, 0.7, created_at=T0))
    assert o.status == OrderStatus.REJECTED


def test_round_trip_pnl_and_state():
    b = SimBroker(1000, FeeConfig(slippage=0))
    b.submit(Order("X", Side.BUY, 10, created_at=T0))
    b.process_bar(bar(T1, 10, 10, 10, 10))
    assert b.positions()["X"].avg_price == pytest.approx(10)
    b.submit(Order("X", Side.SELL, 10, created_at=T1))
    b.process_bar(bar(T1 + pd.Timedelta(days=1), 12, 12, 12, 12))
    assert b.positions() == {}
    assert b.cash == pytest.approx(1020)

    again = SimBroker(0)
    again.load_state(b.to_state())
    assert again.cash == pytest.approx(1020)
