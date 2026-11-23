from datetime import date

from quantdeck.config import RiskConfig
from quantdeck.models import Account, Order, Position, Side
from quantdeck.risk import RiskManager

ACCT = Account(cash=10_000, equity=10_000, buying_power=10_000)


def test_order_value_limit():
    rm = RiskManager(RiskConfig(max_order_value=500))
    assert rm.check(Order("X", Side.BUY, 10), 100, ACCT, {}, {}) is not None
    assert rm.check(Order("X", Side.BUY, 4), 100, ACCT, {}, {}) is None


def test_position_and_gross_limits():
    rm = RiskManager(RiskConfig(max_position_pct=0.5, max_gross_exposure=0.8))
    assert rm.check(Order("X", Side.BUY, 60), 100, ACCT, {}, {}) is not None
    held = {"Y": Position("Y", 50, 100, 100)}
    assert rm.check(Order("X", Side.BUY, 40), 100, ACCT, held, {"Y": 100}) is not None
    assert rm.check(Order("X", Side.BUY, 30), 100, ACCT, held, {"Y": 100}) is None


def test_no_shorts_by_default():
    rm = RiskManager()
    assert rm.check(Order("X", Side.SELL, 1), 100, ACCT, {}, {}) == "shorting disabled"


def test_daily_loss_halts_new_risk_but_not_exits():
    rm = RiskManager(RiskConfig(max_daily_loss_pct=0.05))
    rm.new_day(date(2024, 1, 1), 10_000)
    rm.update_equity(9_400)
    assert rm.halted
    assert rm.check(Order("X", Side.BUY, 1), 100, ACCT, {}, {}).startswith("halted")
    held = {"X": Position("X", 5, 100, 100)}
    assert rm.check(Order("X", Side.SELL, 5), 100, ACCT, held, {}) is None


def test_orders_per_day():
    rm = RiskManager(RiskConfig(max_orders_per_day=1))
    rm.new_day(date(2024, 1, 1), 10_000)
    o = Order("X", Side.BUY, 1)
    assert rm.check(o, 100, ACCT, {}, {}) is None
    rm.record(o)
    assert rm.check(o, 100, ACCT, {}, {}) is not None
    rm.new_day(date(2024, 1, 2), 10_000)
    assert rm.check(o, 100, ACCT, {}, {}) is None
