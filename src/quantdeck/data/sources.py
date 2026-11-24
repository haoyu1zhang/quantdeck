from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path

import pandas as pd

from ..config import secret
from ..timeframes import parse_timeframe
from .store import normalize, read_csv, slice_range, to_utc

log = logging.getLogger(__name__)


class DataSource(ABC):
    name = "base"

    @abstractmethod
    def fetch(self, symbol: str, timeframe: str, start=None, end=None) -> pd.DataFrame:
        """Historical bars, indexed by bar open time (UTC)."""

    def latest(self, symbol: str, timeframe: str, n: int) -> pd.DataFrame:
        tf = parse_timeframe(timeframe)
        start = pd.Timestamp.now(tz="UTC") - tf * (n + 5)
        return self.fetch(symbol, timeframe, start=start).tail(n)


class CsvSource(DataSource):
    """One CSV per symbol. `path` is a file (single symbol) or a directory
    containing files named like BTC-USDT.csv or AAPL.csv."""

    name = "csv"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _file_for(self, symbol: str) -> Path:
        if self.path.is_file():
            return self.path
        for cand in (symbol, symbol.replace("/", "-"), symbol.replace("/", "_"), symbol.replace("/", "")):
            p = self.path / f"{cand}.csv"
            if p.exists():
                return p
        raise FileNotFoundError(f"no csv for {symbol} in {self.path}")

    def fetch(self, symbol, timeframe, start=None, end=None):
        return slice_range(read_csv(self._file_for(symbol)), start, end)


class CcxtSource(DataSource):
    name = "ccxt"

    def __init__(self, exchange: str, client=None) -> None:
        if client is None:
            import ccxt

            from ..ratelimit import Throttled, TokenBucket

            raw = getattr(ccxt, exchange)({"enableRateLimit": True})
            rate = 1000.0 / max(getattr(raw, "rateLimit", 100), 1)
            client = Throttled(raw, TokenBucket(rate, capacity=max(rate, 2)))
        self.exchange = exchange
        self.client = client

    def fetch(self, symbol, timeframe, start=None, end=None):
        tf_ms = int(parse_timeframe(timeframe).total_seconds() * 1000)
        since = int(to_utc(start).timestamp() * 1000) if start is not None else None
        end_ms = int(to_utc(end).timestamp() * 1000) if end is not None else None
        rows: list = []
        while True:
            batch = self.client.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
            if not batch:
                break
            rows.extend(batch)
            last = batch[-1][0]
            if end_ms is not None and last >= end_ms:
                break
            if len(batch) < 2 or (since is not None and last < since):
                break
            if since is None:
                break  # no start given: one page of recent bars is enough
            since = last + tf_ms
            if since > time.time() * 1000:
                break
        if not rows:
            return normalize(pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"]))
        df = normalize(pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]))
        return slice_range(df, start, end)


class AlpacaSource(DataSource):
    name = "alpaca"

    def __init__(self, market: str, feed: str = "iex") -> None:
        from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient

        self.market = market
        self.feed = feed
        if market == "stock":
            self.client = StockHistoricalDataClient(secret("ALPACA_API_KEY"), secret("ALPACA_SECRET_KEY"))
        else:
            # crypto bars don't need keys
            self.client = CryptoHistoricalDataClient(
                secret("ALPACA_API_KEY", required=False), secret("ALPACA_SECRET_KEY", required=False)
            )

    @staticmethod
    def _timeframe(tf: str):
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        td = parse_timeframe(tf)
        minutes = int(td.total_seconds() // 60)
        if minutes % (60 * 24 * 7) == 0:
            return TimeFrame(minutes // (60 * 24 * 7), TimeFrameUnit.Week)
        if minutes % (60 * 24) == 0:
            return TimeFrame(minutes // (60 * 24), TimeFrameUnit.Day)
        if minutes % 60 == 0:
            return TimeFrame(minutes // 60, TimeFrameUnit.Hour)
        return TimeFrame(minutes, TimeFrameUnit.Minute)

    def fetch(self, symbol, timeframe, start=None, end=None):
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest

        start = to_utc(start).to_pydatetime() if start is not None else None
        end = to_utc(end).to_pydatetime() if end is not None else None
        tf = self._timeframe(timeframe)
        if self.market == "stock":
            req = StockBarsRequest(
                symbol_or_symbols=symbol, timeframe=tf, start=start, end=end,
                adjustment=Adjustment.ALL, feed=DataFeed(self.feed),
            )
            bars = self.client.get_stock_bars(req)
        else:
            req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=start, end=end)
            bars = self.client.get_crypto_bars(req)
        df = bars.df
        if df.empty:
            return normalize(pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"]))
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        return normalize(df)


def make_source(source: str, market: str, exchange: str | None = None, path: str | None = None) -> DataSource:
    if source == "csv":
        if not path:
            raise ValueError("data.path is required for csv")
        return CsvSource(path)
    if source == "ccxt":
        if not exchange:
            raise ValueError("data.exchange is required for ccxt")
        return CcxtSource(exchange)
    if source == "alpaca":
        return AlpacaSource(market)
    raise ValueError(f"unknown data source {source!r}")
