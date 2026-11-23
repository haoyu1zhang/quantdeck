from __future__ import annotations

import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


class MarketClock:
    def is_open(self, now: datetime | None = None) -> bool:
        raise NotImplementedError


class AlwaysOpen(MarketClock):
    """Crypto trades around the clock."""

    def is_open(self, now=None) -> bool:
        return True


class UsEquityClock(MarketClock):
    """Regular NYSE session. Asks the broker when it can (that knows about
    holidays and half days), otherwise falls back to 9:30-16:00 ET on weekdays."""

    def __init__(self, broker=None, cache_seconds: int = 60) -> None:
        self.broker = broker
        self.cache_seconds = cache_seconds
        self._cached: tuple[float, bool] | None = None

    def is_open(self, now: datetime | None = None) -> bool:
        if now is None and self.broker is not None and hasattr(self.broker, "market_open"):
            t = time.monotonic()
            if self._cached and t - self._cached[0] < self.cache_seconds:
                return self._cached[1]
            try:
                is_open = bool(self.broker.market_open())
                self._cached = (t, is_open)
                return is_open
            except Exception:
                pass
        ny = (now or datetime.now(tz=NY)).astimezone(NY)
        if ny.weekday() >= 5:
            return False
        return dtime(9, 30) <= ny.time() < dtime(16, 0)


def clock_for(market: str, broker=None) -> MarketClock:
    return UsEquityClock(broker) if market == "stock" else AlwaysOpen()
