from __future__ import annotations

import logging

import pandas as pd

from .sources import AlpacaSource, CcxtSource, CsvSource, DataSource, make_source
from .store import cache_path, load_cached, normalize, read_csv, save_cached, slice_range, to_utc

log = logging.getLogger(__name__)


def load_history(
    source: str,
    market: str,
    symbols: list[str],
    timeframe: str,
    start=None,
    end=None,
    exchange: str | None = None,
    path: str | None = None,
    refresh: bool = False,
) -> dict[str, pd.DataFrame]:
    """Bars for a backtest. Remote sources are cached as parquet so reruns are offline."""
    src = None
    out = {}
    for sym in symbols:
        if source == "csv":
            src = src or make_source(source, market, exchange, path)
            out[sym] = src.fetch(sym, timeframe, start, end)
            continue

        cp = cache_path(source, sym, timeframe, exchange)
        cached = None if refresh else load_cached(cp)
        need_fetch = cached is None or cached.empty
        if not need_fetch:
            # fetch only if the cache doesn't cover the requested range
            if start is not None and cached.index[0] > to_utc(start) + pd.Timedelta(days=3):
                need_fetch = True
            if end is not None and cached.index[-1] < to_utc(end) - pd.Timedelta(days=3):
                need_fetch = True
        if need_fetch:
            src = src or make_source(source, market, exchange, path)
            log.info("downloading %s %s from %s", sym, timeframe, source)
            df = src.fetch(sym, timeframe, start, end)
            if not df.empty:
                save_cached(cp, df)
            cached = load_cached(cp)
        out[sym] = slice_range(cached if cached is not None else pd.DataFrame(), start, end)
        if out[sym].empty:
            raise ValueError(f"no data for {sym} {timeframe} in the requested range")
    return out


__all__ = [
    "DataSource", "CsvSource", "CcxtSource", "AlpacaSource", "make_source", "load_history",
    "normalize", "read_csv", "cache_path",
]
