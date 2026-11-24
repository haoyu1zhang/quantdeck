from __future__ import annotations

import math
from collections import defaultdict

import numpy as np
import pandas as pd

from ..models import Fill, Side

TRADE_COLUMNS = ["symbol", "entry_ts", "exit_ts", "qty", "entry_price", "exit_price", "pnl", "return_pct", "bars"]


def round_trips(fills: list[Fill], index: pd.DatetimeIndex | None = None) -> pd.DataFrame:
    """Group fills into flat-to-flat trades per symbol."""
    rows = []
    state: dict[str, dict] = defaultdict(lambda: {"qty": 0.0})

    for f in sorted(fills, key=lambda f: f.ts):
        s = state[f.symbol]
        signed = f.qty if f.side == Side.BUY else -f.qty
        remaining = signed
        fee_per_unit = f.commission / f.qty if f.qty else 0.0

        while abs(remaining) > 1e-12:
            if abs(s["qty"]) < 1e-12:
                s.update(qty=0.0, entry_ts=f.ts, cash=0.0, fees=0.0, max_qty=0.0, entry_cost=0.0)
            same_dir = s["qty"] == 0 or (s["qty"] > 0) == (remaining > 0)
            step = remaining if same_dir else math.copysign(min(abs(remaining), abs(s["qty"])), remaining)
            s["qty"] += step
            s["cash"] -= step * f.price
            s["fees"] += abs(step) * fee_per_unit
            if same_dir:
                s["entry_cost"] += abs(step) * f.price
                s["max_qty"] = max(s["max_qty"], abs(s["qty"]))
            remaining -= step
            if abs(s["qty"]) < 1e-12:
                pnl = s["cash"] - s["fees"]
                entry_px = s["entry_cost"] / s["max_qty"] if s["max_qty"] else 0.0
                bars = 0
                if index is not None:
                    bars = int(index.searchsorted(f.ts) - index.searchsorted(s["entry_ts"]))
                rows.append(
                    {
                        "symbol": f.symbol,
                        "entry_ts": s["entry_ts"],
                        "exit_ts": f.ts,
                        "qty": s["max_qty"],
                        "entry_price": entry_px,
                        "exit_price": f.price,
                        "pnl": pnl,
                        "return_pct": pnl / s["entry_cost"] if s["entry_cost"] else 0.0,
                        "bars": bars,
                    }
                )
                s["qty"] = 0.0
    return pd.DataFrame(rows, columns=TRADE_COLUMNS)


def drawdown(equity: pd.Series) -> pd.Series:
    peak = equity.cummax()
    return equity / peak - 1


def compute_metrics(
    equity: pd.Series,
    trades: pd.DataFrame,
    periods_per_year: float,
    exposure: pd.Series | None = None,
) -> dict:
    out: dict = {}
    if equity.empty:
        return out
    start, end = float(equity.iloc[0]), float(equity.iloc[-1])
    rets = equity.pct_change().dropna()
    n = len(rets)

    out["start_equity"] = start
    out["end_equity"] = end
    out["total_return"] = end / start - 1 if start else 0.0
    years = n / periods_per_year if periods_per_year else 0
    out["cagr"] = (end / start) ** (1 / years) - 1 if years > 0 and start > 0 and end > 0 else 0.0

    std = rets.std(ddof=1) if n > 1 else 0.0
    out["volatility"] = float(std * math.sqrt(periods_per_year)) if std else 0.0
    out["sharpe"] = float(rets.mean() / std * math.sqrt(periods_per_year)) if std else 0.0
    downside = rets[rets < 0]
    dstd = math.sqrt((downside**2).sum() / n) if n else 0.0
    out["sortino"] = float(rets.mean() / dstd * math.sqrt(periods_per_year)) if dstd else 0.0

    dd = drawdown(equity)
    out["max_drawdown"] = float(dd.min())
    # longest stretch below a previous peak, in bars
    underwater = (dd < 0).astype(int).to_numpy()
    longest = cur = 0
    for u in underwater:
        cur = cur + 1 if u else 0
        longest = max(longest, cur)
    out["max_drawdown_bars"] = int(longest)
    out["calmar"] = out["cagr"] / abs(out["max_drawdown"]) if out["max_drawdown"] < 0 else 0.0

    out["trades"] = int(len(trades))
    if len(trades):
        wins = trades[trades.pnl > 0]
        losses = trades[trades.pnl <= 0]
        out["win_rate"] = len(wins) / len(trades)
        gross_loss = -losses.pnl.sum()
        out["profit_factor"] = float(wins.pnl.sum() / gross_loss) if gross_loss > 0 else float("inf")
        out["avg_trade_return"] = float(trades.return_pct.mean())
        out["avg_bars_held"] = float(trades.bars.mean())
    else:
        out.update(win_rate=0.0, profit_factor=0.0, avg_trade_return=0.0, avg_bars_held=0.0)

    if exposure is not None and len(exposure):
        out["exposure"] = float((exposure > 1e-9).mean())
    return out


def clean_for_json(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, (float, np.floating)):
            v = float(v)
            if math.isinf(v) or math.isnan(v):
                v = None
        elif isinstance(v, np.integer):
            v = int(v)
        out[k] = v
    return out
