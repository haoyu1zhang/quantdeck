"""Round trips against real paper/testnet venues.

Skipped unless credentials are in the environment:
    BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_SECRET   (testnet.binance.vision)
    ALPACA_API_KEY / ALPACA_SECRET_KEY                 (paper account)

Run with:  pytest -m integration
Orders are limit orders far from the market and are canceled right away.
"""

import os
import time

import pytest

from quantdeck.models import Order, OrderType, Side

pytestmark = pytest.mark.integration


def _need(*names):
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        pytest.skip(f"set {', '.join(missing)}")


def _wait_until(fn, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(1)
    return False


def test_ccxt_public_data():
    from quantdeck.data.sources import CcxtSource

    # coinbase rather than binance: CI runners sit in the US, where binance.com is blocked
    df = CcxtSource("coinbase").latest("BTC/USD", "1h", 50)
    assert len(df) >= 45 and (df.close > 0).all()


def test_binance_testnet_order_round_trip(monkeypatch):
    _need("BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_SECRET")
    monkeypatch.setenv("BINANCE_API_KEY", os.environ["BINANCE_TESTNET_API_KEY"])
    monkeypatch.setenv("BINANCE_SECRET", os.environ["BINANCE_TESTNET_SECRET"])
    from quantdeck.brokers.ccxt_broker import CcxtBroker

    b = CcxtBroker("binance", ["BTC/USDT"], sandbox=True)
    acct = b.account()
    assert acct.cash > 0
    price = b._prices["BTC/USDT"]
    o = b.submit(Order("BTC/USDT", Side.BUY, 0.001, OrderType.LIMIT, limit_price=round(price * 0.7, 2)))
    assert o.id, o.reason
    assert _wait_until(lambda: any(x.id == o.id for x in b.open_orders()))
    b.cancel(o.id)
    assert _wait_until(lambda: not any(x.id == o.id for x in b.open_orders()))


def test_alpaca_paper_order_round_trip():
    _need("ALPACA_API_KEY", "ALPACA_SECRET_KEY")
    from quantdeck.brokers.alpaca import AlpacaBroker

    b = AlpacaBroker(["BTC/USD"], "crypto", paper=True)
    assert b.account().equity > 0
    b.market_open()
    o = b.submit(Order("BTC/USD", Side.BUY, 0.001, OrderType.LIMIT, limit_price=1000))
    assert o.id
    assert _wait_until(lambda: any(x.id == o.id for x in b.open_orders()))
    b.cancel(o.id)
    assert _wait_until(lambda: not any(x.id == o.id for x in b.open_orders()))
