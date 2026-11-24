from __future__ import annotations

import functools
import logging
import random
import threading
import time
from typing import Callable, Iterable

log = logging.getLogger(__name__)


class TokenBucket:
    """Thread-safe token bucket. `rate` tokens per second, bursts up to `capacity`."""

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.capacity = capacity or max(rate, 1.0)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> float:
        """Block until tokens are available. Returns seconds spent waiting."""
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                wait = (tokens - self._tokens) / self.rate
            time.sleep(wait)
            waited += wait


class Throttled:
    """Wraps an API client so every method call takes a token first.

    Several threads can share one wrapped client; the bucket keeps the combined
    request rate under the venue's limit.
    """

    def __init__(self, client, bucket: TokenBucket, skip: Iterable[str] = ()) -> None:
        self._client = client
        self._bucket = bucket
        self._skip = set(skip)

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if not callable(attr) or name.startswith("_") or name in self._skip:
            return attr

        @functools.wraps(attr)
        def call(*a, **kw):
            self._bucket.acquire()
            return attr(*a, **kw)

        return call


def retry(
    fn: Callable,
    is_transient: Callable[[Exception], bool],
    attempts: int = 3,
    base_delay: float = 0.5,
):
    """Call fn, retrying transient errors with jittered exponential backoff."""
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            if i == attempts - 1 or not is_transient(e):
                raise
            delay = base_delay * (2**i) * (1 + random.random() * 0.25)
            log.warning("transient error (%s), retry %d/%d in %.1fs", e, i + 1, attempts - 1, delay)
            time.sleep(delay)


def is_network_error(e: Exception) -> bool:
    name = type(e).__name__
    if name in {"NetworkError", "RequestTimeout", "ExchangeNotAvailable", "DDoSProtection",
                "RateLimitExceeded", "ConnectionError", "Timeout", "ReadTimeout", "ConnectTimeout"}:
        return True
    for base in type(e).__mro__:
        if base.__name__ in {"NetworkError", "ConnectionError", "TimeoutError"}:
            return True
    status = getattr(getattr(e, "response", None), "status_code", None) or getattr(e, "status_code", None)
    return status in (429, 500, 502, 503, 504)
