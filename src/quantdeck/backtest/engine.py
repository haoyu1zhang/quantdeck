from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from ..brokers.sim import SimBroker
from ..config import FeeConfig, RiskConfig
from ..context import Context
from ..models import Bar, Fill, Order
from ..risk import RiskManager
from ..strategies.base import Strategy
from ..data.store import to_utc
from ..timeframes import parse_timeframe, periods_per_year, trading_day
from .metrics import compute_metrics, round_trips

log = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    equity: pd.Series
    benchmark: pd.Series
    exposure: pd.Series
    trades: pd.DataFrame
    fills: list[Fill]
    orders: list[Order]
    metrics: dict
    meta: dict = field(default_factory=dict)

    def summary(self) -> str:
        m = self.metrics
        lines = [
            f"{'period':<18}{self.equity.index[0]:%Y-%m-%d} -> {self.equity.index[-1]:%Y-%m-%d}",
            f"{'total return':<18}{m['total_return']:>10.2%}",
            f"{'buy & hold':<18}{m.get('benchmark_return', 0):>10.2%}",
            f"{'CAGR':<18}{m['cagr']:>10.2%}",
            f"{'sharpe':<18}{m['sharpe']:>10.2f}",
            f"{'sortino':<18}{m['sortino']:>10.2f}",
            f"{'max drawdown':<18}{m['max_drawdown']:>10.2%}",
            f"{'trades':<18}{m['trades']:>10d}",
            f"{'win rate':<18}{m['win_rate']:>10.2%}",
            f"{'profit factor':<18}{m['profit_factor']:>10.2f}",
            f"{'exposure':<18}{m.get('exposure', 0):>10.2%}",
        ]
        return "\n".join(lines)


def _check_frame(sym: str, df: pd.DataFrame) -> pd.DataFrame:
    missing = {"open", "high", "low", "close"} - set(df.columns)
    if missing:
        raise ValueError(f"{sym}: missing columns {sorted(missing)}")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError(f"{sym}: index must be a DatetimeIndex")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if "volume" not in df:
        df = df.assign(volume=0.0)
    return df.dropna(subset=["open", "high", "low", "close"])


def run_backtest(
    strategy: Strategy,
    data: dict[str, pd.DataFrame],
    cash: float = 10_000,
    fees: FeeConfig | None = None,
    risk: RiskConfig | None = None,
    market: str = "crypto",
    timeframe: str = "1d",
    fractional: bool = True,
    start_at=None,
) -> BacktestResult:
    """Run `strategy` over `data`.

    Bars before `start_at` only warm up the strategy: it sees them but can't
    trade, and they aren't part of the equity curve or metrics.
    """
    if not data:
        raise ValueError("no data")
    data = {s: _check_frame(s, df) for s, df in data.items()}
    symbols = list(data)
    risk_cfg = risk or RiskConfig()

    broker = SimBroker(cash, fees, fractional=fractional, allow_short=risk_cfg.allow_short)
    rm = RiskManager(risk_cfg)
    ctx = Context(broker, rm, symbols, fractional=fractional)
    ctx.periods_per_year = periods_per_year(parse_timeframe(timeframe), market)

    # plain numpy per symbol; iterating DataFrames row by row is far too slow
    timestamps = data[symbols[0]].index
    for df in list(data.values())[1:]:
        timestamps = timestamps.union(df.index)
    cols = {}
    for s, df in data.items():
        aligned = df.reindex(timestamps)
        cols[s] = (
            aligned[["open", "high", "low", "close", "volume"]].to_numpy(dtype=float),
            ~aligned["close"].isna().to_numpy(),
        )
    ts_list = list(timestamps)
    start_ts = to_utc(start_at) if start_at is not None else None

    eq_ts, eq_vals, expo_vals = [], [], []
    ctx.trading = start_ts is None
    strategy.on_start(ctx)

    for i, ts in enumerate(ts_list):
        if not ctx.trading and ts >= start_ts:
            ctx.trading = True
        bars = []
        for s in symbols:
            arr, present = cols[s]
            if present[i]:
                o, h, lo, c, v = arr[i]
                bars.append(Bar(s, ts, o, h, lo, c, v))

        # 1. fill whatever was queued last bar, at this bar's prices
        broker.process_bars(bars)
        new_fills = broker.sync()

        ctx.now = ts
        ctx.refresh()
        rm.new_day(trading_day(ts, market), ctx.equity)
        for f in new_fills:
            strategy.on_fill(ctx, f)

        # 2. strategy sees the completed bar
        for b in bars:
            ctx.push_bar(b)
        if new_fills:
            ctx.refresh()
        else:
            ctx.mark_prices()
        rm.update_equity(ctx.equity)
        for b in bars:
            strategy.on_bar(ctx, b)
        ctx.sync_protection()

        if ctx.trading:
            acct = broker.account()
            eq_ts.append(ts)
            eq_vals.append(acct.equity)
            gross = sum(abs(p.market_value) for p in broker.positions().values())
            expo_vals.append(gross / acct.equity if acct.equity > 0 else 0.0)

    strategy.on_stop(ctx)
    if not eq_ts:
        raise ValueError("start_at is after the last bar")

    index = pd.DatetimeIndex(eq_ts)
    equity = pd.Series(eq_vals, index=index, name="equity")
    exposure = pd.Series(expo_vals, index=index, name="exposure")
    start_equity = float(equity.iloc[0]) if start_ts is not None else cash

    # equal-weight buy and hold of the same symbols, for comparison
    closes = pd.DataFrame({s: df["close"] for s, df in data.items()}).reindex(index).ffill()
    first = closes.bfill().iloc[0]
    benchmark = (closes / first).mean(axis=1) * start_equity
    benchmark.name = "benchmark"

    tf = parse_timeframe(timeframe)
    ppy = periods_per_year(tf, market)
    trades = round_trips(broker.fills, index)
    metrics = compute_metrics(equity, trades, ppy, exposure)
    metrics["benchmark_return"] = float(benchmark.iloc[-1] / start_equity - 1)
    metrics["rejected_orders"] = len(ctx.rejected)

    return BacktestResult(
        equity=equity,
        benchmark=benchmark,
        exposure=exposure,
        trades=trades,
        fills=list(broker.fills),
        orders=list(broker._orders.values()) + ctx.rejected,
        metrics=metrics,
        meta={
            "strategy": strategy.name or type(strategy).__name__,
            "params": vars(strategy.p),
            "symbols": symbols,
            "timeframe": timeframe,
            "market": market,
            "cash": cash,
        },
    )


def result_to_dict(res: BacktestResult, max_points: int = 1500) -> dict:
    """JSON-friendly version of a result, with curves thinned out for charts."""
    from .metrics import clean_for_json, drawdown

    step = max(1, len(res.equity) // max_points)
    eq = res.equity.iloc[::step]
    if eq.index[-1] != res.equity.index[-1]:
        eq = pd.concat([eq, res.equity.iloc[-1:]])
    bench = res.benchmark.reindex(eq.index)
    dd = drawdown(res.equity).reindex(eq.index)
    trades = res.trades.copy()
    for col in ("entry_ts", "exit_ts"):
        trades[col] = trades[col].astype(str)
    return {
        "meta": res.meta,
        "metrics": clean_for_json(res.metrics),
        "curve": {
            "ts": [t.isoformat() for t in eq.index],
            "equity": [round(float(v), 4) for v in eq.values],
            "benchmark": [round(float(v), 4) for v in bench.values],
            "drawdown": [round(float(v), 6) for v in dd.values],
        },
        "trades": trades.round(6).to_dict(orient="records"),
    }
