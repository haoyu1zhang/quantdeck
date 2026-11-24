"""Parameter search and walk-forward testing.

Grid search on its own mostly finds the parameters that happened to fit the
past. Walk-forward keeps it honest: pick parameters on one window, then score
them on the following window the search never saw, and repeat.
"""

from __future__ import annotations

import itertools
import logging
import math
import multiprocessing
import os
from concurrent.futures import Executor, ProcessPoolExecutor
from dataclasses import dataclass, field

import pandas as pd

from ..config import FeeConfig, RiskConfig
from ..timeframes import parse_timeframe, periods_per_year
from .engine import run_backtest
from .metrics import compute_metrics

log = logging.getLogger(__name__)


def _number(x: str):
    try:
        return int(x)
    except ValueError:
        try:
            return float(x)
        except ValueError:
            return x


def parse_grid(specs: list[str]) -> dict[str, list]:
    """'fast=10:50:10' (inclusive range) or 'slow=50,100,200' or 'risk=0.01'."""
    grid: dict[str, list] = {}
    for spec in specs:
        name, _, values = spec.partition("=")
        if not name or not values:
            raise ValueError(f"bad grid spec {spec!r}, expected name=values")
        if ":" in values:
            parts = [_number(p) for p in values.split(":")]
            if len(parts) != 3:
                raise ValueError(f"range must be start:stop:step, got {values!r}")
            start, stop, step = parts
            if step <= 0:
                raise ValueError("range step must be positive")
            n = int(math.floor((stop - start) / step + 1e-9)) + 1
            vals = [start + i * step for i in range(n)]
            if all(isinstance(p, int) for p in parts):
                vals = [int(v) for v in vals]
            else:
                vals = [round(v, 10) for v in vals]
        else:
            vals = [_number(v) for v in values.split(",") if v != ""]
        grid[name.strip()] = vals
    return grid


def expand(grid: dict[str, list]) -> list[dict]:
    if not grid:
        return [{}]
    keys = list(grid)
    return [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*(grid[k] for k in keys))]


@dataclass
class Settings:
    """Everything a single backtest needs besides the strategy parameters."""

    strategy: str
    cash: float = 10_000
    fees: FeeConfig = field(default_factory=FeeConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    market: str = "crypto"
    timeframe: str = "1d"
    fractional: bool = True
    base_params: dict = field(default_factory=dict)


# set once per worker process so the data isn't pickled for every combination
_DATA: dict[str, pd.DataFrame] = {}


def _init_worker(data):
    global _DATA
    _DATA = data


def _slice(data, lo, hi):
    return {s: df[(df.index >= lo) & (df.index < hi)] for s, df in data.items()}


def _evaluate(task) -> dict:
    from ..strategies import load_strategy

    settings, params, lo, start, hi = task
    row = dict(params)
    try:
        strat = load_strategy(settings.strategy, {**settings.base_params, **params})
    except ValueError as e:
        row["error"] = str(e)
        return row
    data = _slice(_DATA, lo, hi)
    try:
        res = run_backtest(strat, data, settings.cash, settings.fees, settings.risk, settings.market,
                           settings.timeframe, settings.fractional, start_at=start)
    except ValueError as e:
        row["error"] = str(e)
        return row
    m = res.metrics
    row.update({k: m.get(k) for k in ("total_return", "cagr", "sharpe", "sortino", "max_drawdown",
                                      "calmar", "trades", "win_rate", "profit_factor", "exposure")})
    row["_equity"] = res.equity
    return row


def _score(row: dict, metric: str, min_trades: int) -> float:
    if row.get("error") or (row.get("trades") or 0) < min_trades:
        return -math.inf
    v = row.get(metric)
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return -math.inf
    return float(v)


def _warmup_bars(settings: Settings, combos: list[dict]) -> int:
    from ..strategies import load_strategy

    need = 0
    for p in combos:
        try:
            need = max(need, load_strategy(settings.strategy, {**settings.base_params, **p}).warmup())
        except ValueError:
            pass
    return need


class _Runner:
    def __init__(self, data, workers: int | None, executor: Executor | None):
        self.data = data
        self.own = executor is None
        if executor is None:
            workers = workers or max(1, (os.cpu_count() or 2) - 1)
            if workers == 1:
                _init_worker(data)
                self.executor = None
            else:
                # spawn, not fork: forking a process that has threads running can deadlock
                ctx = multiprocessing.get_context("spawn")
                self.executor = ProcessPoolExecutor(workers, mp_context=ctx, initializer=_init_worker,
                                                    initargs=(data,))
        else:
            _init_worker(data)  # thread or in-process executors share our globals
            self.executor = executor

    def map(self, tasks):
        if self.executor is None:
            return [_evaluate(t) for t in tasks]
        return list(self.executor.map(_evaluate, tasks, chunksize=max(1, len(tasks) // 32)))

    def close(self):
        if self.own and self.executor is not None:
            self.executor.shutdown()


def _index(data) -> pd.DatetimeIndex:
    idx = None
    for df in data.values():
        idx = df.index if idx is None else idx.union(df.index)
    return idx


def grid_search(
    settings: Settings,
    grid: dict[str, list],
    data: dict[str, pd.DataFrame],
    metric: str = "sharpe",
    min_trades: int = 1,
    workers: int | None = None,
    executor: Executor | None = None,
    start=None,
    end=None,
) -> pd.DataFrame:
    """Every combination over [start, end). Best first."""
    combos = expand(grid)
    idx = _index(data)
    lo_i = 0 if start is None else idx.searchsorted(pd.Timestamp(start))
    hi = idx[-1] + pd.Timedelta(1, "ns") if end is None else pd.Timestamp(end)
    warm = _warmup_bars(settings, combos)
    lo = idx[max(lo_i - warm, 0)]
    start_at = idx[lo_i] if lo_i > 0 else None

    runner = _Runner(data, workers, executor)
    try:
        rows = runner.map([(settings, p, lo, start_at, hi) for p in combos])
    finally:
        runner.close()
    for r in rows:
        r.pop("_equity", None)
        r["score"] = _score(r, metric, min_trades)
    return pd.DataFrame(rows).sort_values("score", ascending=False, kind="stable").reset_index(drop=True)


@dataclass
class WalkForwardResult:
    folds: pd.DataFrame
    equity: pd.Series
    metrics: dict

    def summary(self) -> str:
        m = self.metrics
        lines = [f"{'fold':<6}{'train':<25}{'test':<25}{'params':<32}{'IS':>8}{'OOS ret':>10}{'OOS sharpe':>12}"]
        for _, f in self.folds.iterrows():
            lines.append(
                f"{f.fold:<6}{f.train_start:%Y-%m-%d}..{f.train_end:%Y-%m-%d}  "
                f"{f.test_start:%Y-%m-%d}..{f.test_end:%Y-%m-%d}  {str(f.params):<32}"
                f"{f.train_score:>8.2f}{f.test_return:>10.2%}{f.test_sharpe:>12.2f}"
            )
        lines += [
            "",
            f"out-of-sample return   {m['total_return']:.2%}",
            f"out-of-sample CAGR     {m['cagr']:.2%}",
            f"out-of-sample sharpe   {m['sharpe']:.2f}",
            f"out-of-sample max DD   {m['max_drawdown']:.2%}",
            f"walk-forward efficiency {m['efficiency']:.2f}  (OOS sharpe / in-sample sharpe; ~0.5+ is decent)",
        ]
        return "\n".join(lines)


def walk_forward(
    settings: Settings,
    grid: dict[str, list],
    data: dict[str, pd.DataFrame],
    folds: int = 4,
    train_frac: float = 0.7,
    metric: str = "sharpe",
    min_trades: int = 1,
    workers: int | None = None,
    executor: Executor | None = None,
) -> WalkForwardResult:
    """Rolling walk-forward.

    The last (1 - train_frac) of the data is cut into `folds` test windows. Each
    is preceded by a training window of train_frac of the data, where the grid
    is searched; the winner is then run, untouched, on the test window.
    """
    if folds < 1 or not 0 < train_frac < 1:
        raise ValueError("need folds >= 1 and 0 < train_frac < 1")
    idx = _index(data)
    n = len(idx)
    test_len = int(n * (1 - train_frac) / folds)
    train_len = int(n * train_frac)
    if test_len < 2 or train_len < 2:
        raise ValueError("not enough bars for that many folds")

    combos = expand(grid)
    warm = _warmup_bars(settings, combos)
    end_of = lambda i: idx[i] if i < n else idx[-1] + pd.Timedelta(1, "ns")  # noqa: E731

    runner = _Runner(data, workers, executor)
    fold_rows, pieces = [], []
    try:
        for k in range(folds):
            test_lo = n - (folds - k) * test_len
            test_hi = test_lo + test_len if k < folds - 1 else n
            train_lo = test_lo - train_len

            tasks = [(settings, p, idx[max(train_lo - warm, 0)], idx[train_lo] if train_lo > 0 else None,
                      idx[test_lo]) for p in combos]
            rows = runner.map(tasks)
            scored = sorted(rows, key=lambda r: _score(r, metric, min_trades), reverse=True)
            best = scored[0]
            if _score(best, metric, min_trades) == -math.inf:
                log.warning("fold %d: no combination met the criteria", k + 1)
            params = {key: best[key] for key in grid}

            test = runner.map([(settings, params, idx[max(test_lo - warm, 0)], idx[test_lo], end_of(test_hi))])[0]
            eq = test.get("_equity")
            if eq is None or eq.empty:
                raise ValueError(f"fold {k + 1}: test run failed ({test.get('error')})")
            pieces.append(eq / eq.iloc[0])
            fold_rows.append({
                "fold": k + 1,
                "train_start": idx[train_lo], "train_end": idx[test_lo - 1],
                "test_start": idx[test_lo], "test_end": idx[test_hi - 1],
                "params": params,
                "train_score": _score(best, metric, min_trades),
                "train_sharpe": best.get("sharpe") or 0.0,
                "test_return": test.get("total_return") or 0.0,
                "test_sharpe": test.get("sharpe") or 0.0,
                "test_max_drawdown": test.get("max_drawdown") or 0.0,
                "test_trades": test.get("trades") or 0,
            })
    finally:
        runner.close()

    # chain the test windows into one out-of-sample curve
    curve, level = [], settings.cash
    for piece in pieces:
        curve.append(piece * level)
        level = float(curve[-1].iloc[-1])
    equity = pd.concat(curve)
    equity = equity[~equity.index.duplicated(keep="last")]

    folds_df = pd.DataFrame(fold_rows)
    ppy = periods_per_year(parse_timeframe(settings.timeframe), settings.market)
    metrics = compute_metrics(equity, pd.DataFrame(columns=["pnl", "return_pct", "bars"]), ppy)
    in_sample = folds_df.train_sharpe.mean()
    metrics["efficiency"] = float(metrics["sharpe"] / in_sample) if in_sample and in_sample > 0 else 0.0
    metrics["trades"] = int(folds_df.test_trades.sum())
    return WalkForwardResult(folds_df, equity, metrics)


__all__ = ["Settings", "parse_grid", "expand", "grid_search", "walk_forward", "WalkForwardResult"]
