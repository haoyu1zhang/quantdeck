import pandas as pd
import pytest

from conftest import frame
from quantdeck.backtest import run_backtest
from quantdeck.brokers.alpaca import AlpacaBroker
from quantdeck.config import FeeConfig, RiskConfig
from quantdeck.models import Order, OrderType, Side
from quantdeck.strategies import Strategy
from quantdeck.timeframes import trading_day

NOFEE = FeeConfig(slippage=0)


class BuyOnce(Strategy):
    params = {}

    def on_bar(self, ctx, bar):
        if not ctx.state.get("done") and ctx.order_target_percent(bar.symbol, 1.0):
            ctx.state["done"] = True


def test_stop_loss_rests_at_broker_and_fires():
    df = frame([(100, 100, 100, 100), (100, 101, 99, 100), (100, 100, 100, 100), (95, 96, 80, 85), (85, 85, 85, 85)])
    res = run_backtest(BuyOnce(), {"X": df}, cash=1000, fees=NOFEE, risk=RiskConfig(stop_loss_pct=0.1))
    sells = [f for f in res.fills if f.side == Side.SELL]
    assert len(sells) == 1
    assert sells[0].price == pytest.approx(90)  # stop at 100 * 0.9, not the 80 low
    assert res.equity.iloc[-1] == pytest.approx(900, rel=1e-3)


def test_take_profit_leg():
    df = frame([(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 100, 100), (104, 125, 103, 110), (110, 110, 110, 110)])
    res = run_backtest(BuyOnce(), {"X": df}, cash=1000, fees=NOFEE,
                       risk=RiskConfig(stop_loss_pct=0.1, take_profit_pct=0.2))
    sells = [f for f in res.fills if f.side == Side.SELL]
    assert sells and sells[0].price == pytest.approx(120)


def test_trailing_stop_ratchets_up():
    rows = [(100, 100, 100, 100), (100, 100, 100, 100)]
    rows += [(p, p, p, p) for p in (110, 120, 130)]
    rows += [(130, 130, 100, 105), (105, 105, 105, 105)]
    res = run_backtest(BuyOnce(), {"X": frame(rows)}, cash=1000, fees=NOFEE,
                       risk=RiskConfig(stop_loss_pct=0.1, trailing_stop=True))
    sells = [f for f in res.fills if f.side == Side.SELL]
    assert sells[0].price == pytest.approx(117)  # 130 peak * 0.9


def test_strategy_exit_cancels_protection(bars):
    from quantdeck.strategies import load_strategy

    res = run_backtest(load_strategy("sma_cross"), {"X": bars}, fees=NOFEE, risk=RiskConfig(stop_loss_pct=0.2))
    # never more than one position's worth sold per round trip
    assert (res.trades.qty > 0).all()
    assert res.metrics["rejected_orders"] == 0


class Rotate(Strategy):
    params = {}

    def on_bar(self, ctx, bar):
        if bar.symbol != "B":
            return
        n = ctx.bars("A")
        if n == 2:
            ctx.order_target_percent("A", 0.99)
        if n == 4:
            ctx.close_position("A")
            ctx.order_target_percent("B", 0.99)


def test_rotation_reuses_sale_proceeds_same_bar():
    flat = frame([(10, 10, 10, 10)] * 7)
    res = run_backtest(Rotate(), {"A": flat, "B": flat}, cash=1000, fees=NOFEE)
    b_buys = [f for f in res.fills if f.symbol == "B"]
    assert b_buys and b_buys[0].qty * 10 == pytest.approx(990, rel=0.01)


def test_volume_cap_fills_over_several_bars():
    df = frame([(10, 10, 10, 10)] * 6).assign(volume=20.0)
    res = run_backtest(BuyOnce(), {"X": df}, cash=1000, fees=FeeConfig(slippage=0, max_volume_pct=0.25))
    assert [f.qty for f in res.fills][:3] == [5, 5, 5]


def test_warmup_window_excluded(bars):
    start = bars.index[300]
    res = run_backtest(BuyOnce(), {"X": bars}, fees=NOFEE, start_at=start)
    assert res.equity.index[0] == start
    assert res.fills[0].ts > start


def test_stock_trading_day_is_new_york_date():
    late = pd.Timestamp("2024-03-06 02:00", tz="UTC")  # 21:00 on the 5th in New York
    assert str(trading_day(late, "stock")) == "2024-03-05"
    assert str(trading_day(late, "crypto")) == "2024-03-06"


def test_alpaca_exit_requests():
    stock = AlpacaBroker(["AAPL"], "stock", client=object())
    req = stock._request(Order("AAPL", Side.SELL, 10, OrderType.STOP, stop_price=90, take_profit=120))
    assert req.order_class.value == "oco" and req.stop_loss.stop_price == 90
    frac = Order("AAPL", Side.SELL, 1.5, OrderType.STOP, stop_price=90, take_profit=120)
    req = stock._request(frac)
    assert req.time_in_force.value == "day" and frac.take_profit is None

    crypto = AlpacaBroker(["BTC/USD"], "crypto", client=object())
    req = crypto._request(Order("BTC/USD", Side.SELL, 0.1, OrderType.STOP, stop_price=50000))
    assert req.type.value == "stop_limit" and req.limit_price < 50000
