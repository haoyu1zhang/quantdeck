from datetime import datetime, timezone
from types import SimpleNamespace

from quantdeck.brokers.alpaca import AlpacaBroker
from quantdeck.models import Order, Side


class ConnectionError_(Exception):
    pass


ConnectionError_.__name__ = "ConnectionError"


def raw_order(**kw):
    base = dict(id="abc", symbol="BTCUSD", side="buy", qty="1", order_type="market", limit_price=None,
                stop_price=None, time_in_force="gtc", client_order_id="c1", status="new", filled_qty="0",
                filled_avg_price=None, created_at=datetime.now(timezone.utc))
    base.update(kw)
    return SimpleNamespace(**base)


class FakeTrading:
    def __init__(self, fail=False):
        self.fail = fail
        self.submitted = []
        self.state = {}

    def submit_order(self, req):
        o = raw_order(client_order_id=req.client_order_id, symbol=req.symbol.replace("/", ""))
        self.submitted.append(o)
        self.state["abc"] = o
        if self.fail:
            self.fail = False
            raise ConnectionError_("reset by peer")
        return o

    def get_order_by_client_id(self, cid):
        for o in self.submitted:
            if o.client_order_id == cid:
                return o
        raise KeyError(cid)

    def get_order_by_id(self, oid):
        return self.state[oid]

    def get_all_positions(self):
        return [SimpleNamespace(symbol="BTCUSD", qty="0.5", avg_entry_price="100", current_price="110")]


def test_crypto_symbols_map_back_and_retry_is_idempotent():
    client = FakeTrading(fail=True)
    b = AlpacaBroker(["BTC/USD"], "crypto", client=client)
    o = b.submit(Order("BTC/USD", Side.BUY, 1))
    assert o.id == "abc" and len(client.submitted) == 1
    assert "BTC/USD" in b.positions()

    client.state["abc"] = raw_order(status="filled", filled_qty="1", filled_avg_price="105")
    fills = b.sync()
    assert len(fills) == 1 and fills[0].symbol == "BTC/USD" and fills[0].price == 105
    assert b.sync() == []
