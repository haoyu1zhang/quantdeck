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
from ..timeframes import parse_timeframe, periods_per_year
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
) -> BacktestResult:
    if not data:
        raise ValueError("no data")
    data = {s: _check_frame(s, df) for s, df in data.items()}
    symbols = list(data)
    risk_cfg = risk or RiskConfig()

    broker = SimBroker(cash, fees, fractional=fractional, allow_short=risk_cfg.allow_short)
    rm = RiskManager(risk_cfg)
    ctx = Context(broker, rm, symbols, fractional=fractional)

    # long format, one row per (ts, symbol), so multiple symbols interleave correctly
    frames = []
    for s, df in data.items():
        f = df[["open", "high", "low", "close", "volume"]].copy()
        f["symbol"] = s
        frames.append(f)
    allbars = pd.concat(frames).sort_index(kind="stable")
    timestamps = allbars.index.unique()

    eq_vals, expo_vals = [], []
    strategy.on_start(ctx)

    grouped = allbars.groupby(level=0, sort=True)
    for ts, rows in grouped:
        bars = [
            Bar(r.symbol, ts, r.open, r.high, r.low, r.close, r.volume)
            for r in rows.itertuples(index=False)
        ]

        # 1. fill whatever was queued last bar, at this bar's prices
        for b in bars:
            broker.process_bar(b)
        new_fills = broker.sync()

        ctx.now = ts
        ctx.refresh()
        rm.new_day(ts.date(), ctx.equity)
        for f in new_fills:
            strategy.on_fill(ctx, f)

        # 2. strategy sees the completed bar
        for b in bars:
            ctx.push_bar(b)
        ctx.refresh()
        rm.update_equity(ctx.equity)
        for b in bars:
            strategy.on_bar(ctx, b)

        acct = broker.account()
        eq_vals.append(acct.equity)
        gross = sum(abs(p.market_value) for p in broker.positions().values())
        expo_vals.append(gross / acct.equity if acct.equity > 0 else 0.0)

    strategy.on_stop(ctx)

    equity = pd.Series(eq_vals, index=timestamps, name="equity")
    exposure = pd.Series(expo_vals, index=timestamps, name="exposure")

    # equal-weight buy and hold of the same symbols, for comparison
    closes = pd.DataFrame({s: df["close"] for s, df in data.items()}).reindex(timestamps).ffill()
    first = closes.bfill().iloc[0]
    benchmark = (closes / first).mean(axis=1) * cash
    benchmark.name = "benchmark"

    tf = parse_timeframe(timeframe)
    ppy = periods_per_year(tf, market)
    trades = round_trips(broker.fills, timestamps)
    metrics = compute_metrics(equity, trades, ppy, exposure)
    metrics["benchmark_return"] = float(benchmark.iloc[-1] / cash - 1)
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
