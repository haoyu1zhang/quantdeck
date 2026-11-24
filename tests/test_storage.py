import threading
from datetime import datetime, timedelta, timezone

import pytest

from quantdeck.models import Fill, Order, Side
from quantdeck.storage import TradeStore

T = datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_orders_fills_and_stats(tmp_path):
    st = TradeStore(tmp_path / "t.db")
    o = Order("BTC/USDT", Side.BUY, 2, id="1")
    st.record_order(o)
    st.record_fill(Fill("1", "BTC/USDT", Side.BUY, 1, 100, 0.1, T))
    st.record_fill(Fill("1", "BTC/USDT", Side.BUY, 1, 110, 0.1, T + timedelta(hours=1)))
    row = st.orders()[0]
    assert row["status"] == "filled" and row["avg_price"] == pytest.approx(105)

    st.record_order(Order("BTC/USDT", Side.SELL, 2, id="2"))
    st.record_fill(Fill("2", "BTC/USDT", Side.SELL, 2, 120, 0.2, T + timedelta(hours=2)))
    s = st.stats()
    assert s["trades"] == 1
    assert s["realized_pnl"] == pytest.approx(240 - 210 - 0.4)
    assert s["commissions"] == pytest.approx(0.4)

    st.record_equity(T, 1000)
    st.record_equity(T, 1001)  # same bar overwrites
    assert list(st.equity()) == [1001]


def test_reader_alongside_writer(tmp_path):
    w = TradeStore(tmp_path / "t.db")
    stop = threading.Event()

    def write():
        i = 0
        while not stop.is_set():
            w.record_equity(T + timedelta(minutes=i), 1000 + i)
            i += 1

    th = threading.Thread(target=write)
    th.start()
    try:
        for _ in range(50):
            r = TradeStore(tmp_path / "t.db", readonly=True)
            r.equity()
            r.close()
    finally:
        stop.set()
        th.join()
    assert len(w.equity(100000)) > 0
