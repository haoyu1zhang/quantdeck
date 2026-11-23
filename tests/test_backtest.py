import numpy as np
import pandas as pd
import pytest

from conftest import frame, make_bars
from quantdeck.backtest import result_to_dict, run_backtest
from quantdeck.backtest.metrics import compute_metrics, drawdown, round_trips
from quantdeck.config import FeeConfig
from quantdeck.models import Fill, Side
from quantdeck.strategies import REGISTRY, Strategy, load_strategy


class BuyFirstBar(Strategy):
    params = {}

    def on_bar(self, ctx, bar):
        if ctx.position(bar.symbol) == 0 and not ctx.open_orders(bar.symbol):
            ctx.order_target_percent(bar.symbol, 1.0)


class Peek(Strategy):
    """Records what it sees so we can check nothing from the future leaks in."""

    params = {}

    def on_start(self, ctx):
        self.seen = []

    def on_bar(self, ctx, bar):
        self.seen.append((bar.ts, ctx.history(bar.symbol).index[-1]))


def test_fill_happens_at_next_open():
    df = frame([(10, 10, 10, 10), (12, 12, 12, 12), (13, 13, 13, 13)])
    res = run_backtest(BuyFirstBar(), {"X": df}, cash=1000, fees=FeeConfig(slippage=0))
    assert res.fills[0].price == 12
    assert res.fills[0].ts == df.index[1]


def test_history_never_ahead_of_current_bar(bars):
    s = Peek()
    run_backtest(s, {"X": bars})
    assert all(ts == last for ts, last in s.seen)


def test_buy_and_hold_tracks_benchmark(bars):
    res = run_backtest(BuyFirstBar(), {"X": bars}, cash=10_000, fees=FeeConfig(slippage=0))
    # one bar of lag and 0% fees: should be very close to the benchmark
    assert res.metrics["total_return"] == pytest.approx(res.metrics["benchmark_return"], rel=0.05, abs=0.02)


@pytest.mark.parametrize("name", list(REGISTRY))
def test_builtin_strategies_run_multi_symbol(name):
    data = {"A": make_bars(seed=1), "B": make_bars(seed=2, start=50)}
    res = run_backtest(load_strategy(name), data, fees=FeeConfig(rate=0.001))
    assert len(res.equity) == 500
    assert np.isfinite(res.equity).all()
    assert (res.equity > 0).all()
    d = result_to_dict(res)
    assert len(d["curve"]["ts"]) == len(d["curve"]["equity"])


def test_whole_shares_for_stocks(bars):
    res = run_backtest(BuyFirstBar(), {"X": bars}, cash=1000, market="stock", fractional=False)
    assert all(f.qty == int(f.qty) for f in res.fills)


def test_round_trips_and_drawdown():
    t = pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC")
    fills = [
        Fill("1", "X", Side.BUY, 2, 10, 0.1, t[0]),
        Fill("2", "X", Side.BUY, 2, 12, 0.1, t[1]),
        Fill("3", "X", Side.SELL, 4, 15, 0.2, t[2]),
    ]
    tr = round_trips(fills, t)
    assert len(tr) == 1
    assert tr.pnl[0] == pytest.approx(4 * 15 - 20 - 24 - 0.4)
    assert tr.entry_price[0] == pytest.approx(11)
    assert tr.bars[0] == 2

    eq = pd.Series([100, 120, 90, 130], index=t)
    assert drawdown(eq).min() == pytest.approx(-0.25)
    m = compute_metrics(eq, tr, 365)
    assert m["max_drawdown"] == pytest.approx(-0.25)
    assert m["trades"] == 1 and m["win_rate"] == 1.0


def test_strategy_param_validation():
    with pytest.raises(ValueError):
        load_strategy("sma_cross", {"nope": 1})
    with pytest.raises(ValueError):
        load_strategy("sma_cross", {"fast": 50, "slow": 20})
    s = load_strategy("examples.my_strategy:MomentumRank", {"top": 1})
    assert s.p.top == 1
