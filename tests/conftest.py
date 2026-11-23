import numpy as np
import pandas as pd
import pytest


def make_bars(n=500, start=100.0, seed=0, freq="D", drift=0.0003, vol=0.02, end=None):
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    open_ = np.r_[start, close[:-1]]
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.01, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.01, n))
    if end is not None:
        idx = pd.date_range(end=end, periods=n, freq=freq, tz="UTC")
    else:
        idx = pd.date_range("2021-01-01", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": 1000.0}, index=idx)


def frame(rows, freq="D"):
    """Bars from (open, high, low, close) tuples."""
    idx = pd.date_range("2024-01-01", periods=len(rows), freq=freq, tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1.0)


@pytest.fixture
def bars():
    return make_bars()
