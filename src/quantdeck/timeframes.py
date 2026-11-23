import re
from datetime import timedelta

_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_timeframe(tf: str) -> timedelta:
    m = re.fullmatch(r"(\d+)([mhdw])", tf.strip().lower())
    if not m:
        raise ValueError(f"bad timeframe: {tf!r} (use e.g. 1m, 15m, 1h, 4h, 1d)")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(**{_UNITS[unit]: n})


def periods_per_year(tf: timedelta, market: str) -> float:
    """Number of bars in a trading year, used to annualise returns and vol."""
    if market == "stock":
        if tf >= timedelta(days=1):
            return 252 / (tf / timedelta(days=1))
        # regular session is 6.5h
        return 252 * (timedelta(hours=6, minutes=30) / tf)
    return timedelta(days=365) / tf
