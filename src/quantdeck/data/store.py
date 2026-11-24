from __future__ import annotations

import os
import re
from pathlib import Path

import pandas as pd

COLUMNS = ["open", "high", "low", "close", "volume"]

_ALIASES = {
    "timestamp": "ts", "time": "ts", "date": "ts", "datetime": "ts", "t": "ts",
    "index": "ts", "unnamed: 0": "ts", "open_time": "ts",
    "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "vol": "volume",
    "adj close": "adj_close", "adj_close": "adj_close",
}


def cache_dir() -> Path:
    return Path(os.environ.get("QUANTDECK_DATA", Path.home() / ".quantdeck" / "data"))


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Return a frame indexed by UTC timestamps with open/high/low/close/volume."""
    df = df.copy()
    df.columns = [_ALIASES.get(str(c).strip().lower(), str(c).strip().lower()) for c in df.columns]
    if "ts" in df.columns:
        ts = df.pop("ts")
        if pd.api.types.is_numeric_dtype(ts):
            # epoch seconds or milliseconds
            unit = "ms" if ts.max() > 1e11 else "s"
            idx = pd.to_datetime(ts, unit=unit, utc=True)
        else:
            idx = pd.to_datetime(ts, utc=True)
        df.index = pd.DatetimeIndex(idx)
    elif not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("can't find a timestamp column")
    idx = df.index
    df.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    df.index.name = "ts"
    if "volume" not in df.columns:
        df["volume"] = 0.0
    missing = set(COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"missing columns: {sorted(missing)}")
    df = df[COLUMNS].astype(float)
    return df[~df.index.duplicated(keep="last")].sort_index()


def read_csv(path: str | Path) -> pd.DataFrame:
    return normalize(pd.read_csv(path))


def _safe(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", symbol)


def cache_path(source: str, symbol: str, timeframe: str, exchange: str | None = None) -> Path:
    parts = [source] + ([exchange] if exchange else [])
    return cache_dir().joinpath(*parts) / f"{_safe(symbol)}_{timeframe}.parquet"


def load_cached(path: Path) -> pd.DataFrame | None:
    if path.exists():
        return pd.read_parquet(path)
    return None


def save_cached(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    old = load_cached(path)
    if old is not None and len(old):
        df = pd.concat([old, df])
        df = df[~df.index.duplicated(keep="last")].sort_index()
    df.to_parquet(path)


def to_utc(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def slice_range(df: pd.DataFrame, start=None, end=None) -> pd.DataFrame:
    if start is not None:
        df = df[df.index >= to_utc(start)]
    if end is not None:
        df = df[df.index < to_utc(end)]
    return df
