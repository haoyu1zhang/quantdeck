"""Small numpy indicator set. Every function returns an array the same length as
its input, padded with NaN where there isn't enough data yet."""

from __future__ import annotations

import numpy as np


def sma(x: np.ndarray, n: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def ema(x: np.ndarray, n: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    alpha = 2 / (n + 1)
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = alpha * x[i] + (1 - alpha) * out[i - 1]
    return out


def rsi(x: np.ndarray, n: int = 14) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    if len(x) <= n:
        return out
    d = np.diff(x)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    avg_up = up[:n].mean()
    avg_dn = dn[:n].mean()
    out[n] = 100.0 if avg_dn == 0 else 100 - 100 / (1 + avg_up / avg_dn)
    for i in range(n + 1, len(x)):
        avg_up = (avg_up * (n - 1) + up[i - 1]) / n
        avg_dn = (avg_dn * (n - 1) + dn[i - 1]) / n
        out[i] = 100.0 if avg_dn == 0 else 100 - 100 / (1 + avg_up / avg_dn)
    return out


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> np.ndarray:
    high, low, close = (np.asarray(a, dtype=float) for a in (high, low, close))
    out = np.full(len(close), np.nan)
    if len(close) <= n:
        return out
    prev = np.roll(close, 1)
    tr = np.maximum(high - low, np.maximum(abs(high - prev), abs(low - prev)))
    tr[0] = high[0] - low[0]
    out[n] = tr[1:n + 1].mean()
    for i in range(n + 1, len(close)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def rolling_max(x: np.ndarray, n: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        w = np.lib.stride_tricks.sliding_window_view(x, n)
        out[n - 1:] = w.max(axis=1)
    return out


def rolling_min(x: np.ndarray, n: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        w = np.lib.stride_tricks.sliding_window_view(x, n)
        out[n - 1:] = w.min(axis=1)
    return out
