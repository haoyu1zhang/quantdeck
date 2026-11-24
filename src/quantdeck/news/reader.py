"""Read-only access to the latest verdicts, for strategies and the risk gate."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .models import utcnow
from .store import NewsStore
from .symbols import canonical

log = logging.getLogger(__name__)


class NewsReader:
    """Looks up the current verdict for a trading symbol.

    Verdicts older than `max_age_minutes` are treated as missing, so a
    stopped `news watch` can't keep blocking or allowing trades on stale
    information. Results are cached for `cache_seconds`.
    """

    def __init__(self, db: str | Path, market: str, max_age_minutes: float = 60, cache_seconds: float = 30) -> None:
        self.db = Path(db)
        self.market = market
        self.max_age = timedelta(minutes=max_age_minutes)
        self.cache_seconds = cache_seconds
        self._store: NewsStore | None = None
        self._cache: dict[str, tuple[float, dict | None]] = {}
        self._lock = threading.Lock()
        self._warned = False

    def _open(self) -> NewsStore | None:
        if self._store is None:
            if not self.db.exists():
                if not self._warned:
                    log.warning("news database %s not found; is `quantdeck news watch` running?", self.db)
                    self._warned = True
                return None
            self._store = NewsStore(self.db, readonly=True)
        return self._store

    def verdict(self, symbol: str) -> dict | None:
        key = symbol if symbol.startswith("MARKET:") else canonical(symbol, self.market)
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self.cache_seconds:
                return hit[1]
            v = None
            try:
                st = self._open()
                v = st.latest_verdict(key) if st else None
            except Exception as e:
                log.warning("news lookup for %s failed: %s", key, e)
            if v is not None and utcnow() - datetime.fromisoformat(v["ts"]) > self.max_age:
                v = None
            self._cache[key] = (now, v)
            return v

    def score(self, symbol: str) -> float | None:
        v = self.verdict(symbol)
        return None if v is None else float(v["score"])

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None
