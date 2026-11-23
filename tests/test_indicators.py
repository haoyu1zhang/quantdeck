import numpy as np
import pandas as pd
import pytest

from quantdeck.indicators import atr, ema, rolling_max, rolling_min, rsi, sma


def test_sma_matches_pandas():
    x = np.random.default_rng(0).normal(size=100).cumsum()
    np.testing.assert_allclose(sma(x, 10)[9:], pd.Series(x).rolling(10).mean()[9:])
    assert np.isnan(sma(x, 10)[:9]).all()


def test_ema_and_rolling():
    x = np.arange(1, 21, dtype=float)
    assert ema(x, 5)[4] == pytest.approx(3.0)
    assert rolling_max(x, 3)[-1] == 20 and rolling_min(x, 3)[-1] == 18


def test_rsi_extremes_and_range():
    up = np.arange(1, 40, dtype=float)
    assert rsi(up, 14)[-1] == 100
    x = np.random.default_rng(1).normal(size=300).cumsum() + 100
    r = rsi(x, 14)
    assert np.nanmin(r) >= 0 and np.nanmax(r) <= 100


def test_atr_positive():
    rng = np.random.default_rng(2)
    c = 100 + rng.normal(size=100).cumsum()
    a = atr(c + 1, c - 1, c, 14)
    assert np.isnan(a[:14]).all() and (a[14:] > 0).all()
