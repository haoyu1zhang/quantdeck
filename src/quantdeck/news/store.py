"""SQLite store for news items, their scores and the verdicts built from them.

WAL mode, like the trade store: `news watch` writes while the dashboard and
trading runs read.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .models import NewsItem, Score, Verdict, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id           TEXT PRIMARY KEY,
    cluster_id   TEXT NOT NULL,
    source       TEXT NOT NULL,
    kind         TEXT NOT NULL,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL DEFAULT '',
    author       TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    symbols      TEXT NOT NULL DEFAULT '[]',
    engagement   TEXT NOT NULL DEFAULT '{}',
    credibility  REAL NOT NULL,
    lang         TEXT NOT NULL DEFAULT '',
    relevant     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS items_published ON items(published_at);
CREATE INDEX IF NOT EXISTS items_cluster ON items(cluster_id);
CREATE TABLE IF NOT EXISTS scores (
    item_id    TEXT PRIMARY KEY REFERENCES items(id),
    scorer     TEXT NOT NULL,
    direction  REAL NOT NULL,
    impact     REAL NOT NULL,
    confidence REAL NOT NULL,
    horizon    TEXT NOT NULL,
    scope      TEXT NOT NULL,
    markets    TEXT NOT NULL DEFAULT '[]',
    symbols    TEXT NOT NULL DEFAULT '[]',
    reason     TEXT NOT NULL DEFAULT '',
    scored_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verdicts (
    ts         TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    score      REAL NOT NULL,
    label      TEXT NOT NULL,
    confidence REAL NOT NULL,
    items      INTEGER NOT NULL,
    weight     REAL NOT NULL,
    detail     TEXT NOT NULL,
    PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS source_state (
    source     TEXT PRIMARY KEY,
    state      TEXT NOT NULL DEFAULT '{}',
    last_run   TEXT,
    last_ok    TEXT,
    last_error TEXT,
    fetched    INTEGER NOT NULL DEFAULT 0
);
"""


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class NewsStore:
    def __init__(self, path: str | Path, readonly: bool = False) -> None:
        self.path = Path(path)
        if readonly:
            self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()

    def close(self) -> None:
        self.conn.close()

    # --- items ----------------------------------------------------------------

    def existing_ids(self, ids: list[str]) -> set[str]:
        out: set[str] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = f"SELECT id FROM items WHERE id IN ({','.join('?' * len(chunk))})"
            out.update(r[0] for r in self.conn.execute(q, chunk))
        return out

    def add_items(self, items: list[NewsItem], relevant: dict[str, bool] | None = None) -> int:
        """Insert new items; returns how many were new. Existing ones just gain symbols."""
        now = _iso(utcnow())
        new = 0
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                for it in items:
                    row = self.conn.execute("SELECT symbols FROM items WHERE id=?", (it.id,)).fetchone()
                    if row:
                        merged = sorted(set(json.loads(row[0])) | set(it.symbols))
                        self.conn.execute("UPDATE items SET symbols=? WHERE id=?", (json.dumps(merged), it.id))
                        continue
                    self.conn.execute(
                        "INSERT INTO items (id, cluster_id, source, kind, title, body, url, author, published_at,"
                        " fetched_at, symbols, engagement, credibility, lang, relevant)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (it.id, it.cluster_id or it.id, it.source, it.kind, it.title, it.body, it.url, it.author,
                         _iso(it.published_at), now, json.dumps(sorted(set(it.symbols))),
                         json.dumps(it.engagement), float(it.credibility if it.credibility is not None else 0.5),
                         it.lang, int((relevant or {}).get(it.id, True))),
                    )
                    new += 1
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return new

    def recent_titles(self, hours: float = 48, limit: int = 5000) -> list[tuple[str, str, str, str]]:
        """(id, cluster_id, title, source) for dedup, newest first."""
        since = _iso(utcnow() - timedelta(hours=hours))
        q = ("SELECT id, cluster_id, title, source FROM items WHERE published_at >= ?"
             " ORDER BY published_at DESC LIMIT ?")
        return [tuple(r) for r in self.conn.execute(q, (since, limit))]

    def unscored(self, limit: int = 500) -> list[NewsItem]:
        q = ("SELECT i.* FROM items i LEFT JOIN scores s ON s.item_id = i.id"
             " WHERE s.item_id IS NULL AND i.relevant = 1 ORDER BY i.published_at DESC LIMIT ?")
        return [self._item(r) for r in self.conn.execute(q, (limit,))]

    def add_scores(self, scores: list[Score]) -> None:
        now = _iso(utcnow())
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                for s in scores:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO scores VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (s.item_id, s.scorer, s.direction, s.impact, s.confidence, s.horizon, s.scope,
                         json.dumps(s.markets), json.dumps(s.symbols), s.reason[:500], now),
                    )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def scored_window(self, hours: float) -> list[dict]:
        """Scored, relevant items from the last `hours`, with cluster size."""
        since = _iso(utcnow() - timedelta(hours=hours))
        q = """
        SELECT i.*, s.scorer, s.direction, s.impact, s.confidence, s.horizon, s.scope, s.markets,
               s.symbols AS score_symbols, s.reason,
               (SELECT COUNT(DISTINCT source) FROM items j WHERE j.cluster_id = i.cluster_id) AS corroboration,
               (SELECT MIN(published_at) FROM items j WHERE j.cluster_id = i.cluster_id) AS first_seen
        FROM items i JOIN scores s ON s.item_id = i.id
        WHERE i.published_at >= ? AND i.relevant = 1
        """
        out = []
        for r in self.conn.execute(q, (since,)):
            d = dict(r)
            for k in ("symbols", "engagement", "markets", "score_symbols"):
                d[k] = json.loads(d[k])
            out.append(d)
        return out

    def items(self, symbol: str | None = None, limit: int = 100, hours: float = 72) -> list[dict]:
        since = _iso(utcnow() - timedelta(hours=hours))
        q = """SELECT i.id, i.source, i.kind, i.title, i.url, i.author, i.published_at, i.symbols, i.engagement,
                      i.credibility, i.cluster_id, s.direction, s.impact, s.confidence, s.scope, s.markets,
                      s.symbols AS score_symbols, s.reason, s.scorer, s.horizon
               FROM items i LEFT JOIN scores s ON s.item_id = i.id
               WHERE i.published_at >= ? AND i.relevant = 1 ORDER BY i.published_at DESC LIMIT ?"""
        out = []
        for r in self.conn.execute(q, (since, max(limit * 5, 500) if symbol else limit)):
            d = dict(r)
            for k in ("symbols", "engagement", "markets", "score_symbols"):
                d[k] = json.loads(d[k]) if d[k] else ([] if k != "engagement" else {})
            if symbol and not _about(d, symbol):
                continue
            out.append(d)
            if len(out) >= limit:
                break
        return out

    # --- verdicts -------------------------------------------------------------

    def add_verdicts(self, verdicts: list[Verdict]) -> None:
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                for v in verdicts:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO verdicts VALUES (?,?,?,?,?,?,?,?)",
                        (_iso(v.ts), v.symbol, v.score, v.label, v.confidence, v.items, v.weight,
                         json.dumps(v.as_dict())),
                    )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def latest_verdicts(self) -> list[dict]:
        q = """SELECT v.detail FROM verdicts v
               JOIN (SELECT symbol, MAX(ts) AS ts FROM verdicts GROUP BY symbol) m
                 ON m.symbol = v.symbol AND m.ts = v.ts ORDER BY v.symbol"""
        return [json.loads(r[0]) for r in self.conn.execute(q)]

    def latest_verdict(self, symbol: str) -> dict | None:
        r = self.conn.execute("SELECT detail FROM verdicts WHERE symbol=? ORDER BY ts DESC LIMIT 1",
                              (symbol,)).fetchone()
        return json.loads(r[0]) if r else None

    def verdict_history(self, symbol: str, hours: float = 72) -> list[dict]:
        since = _iso(utcnow() - timedelta(hours=hours))
        q = "SELECT ts, score, label, confidence FROM verdicts WHERE symbol=? AND ts >= ? ORDER BY ts"
        return [dict(r) for r in self.conn.execute(q, (symbol, since))]

    def prune(self, keep_days: int = 30) -> None:
        cutoff = _iso(utcnow() - timedelta(days=keep_days))
        with self._lock:
            self.conn.execute("DELETE FROM scores WHERE item_id IN (SELECT id FROM items WHERE published_at < ?)",
                              (cutoff,))
            self.conn.execute("DELETE FROM items WHERE published_at < ?", (cutoff,))
            self.conn.execute("DELETE FROM verdicts WHERE ts < ?", (cutoff,))

    # --- source bookkeeping ---------------------------------------------------

    def source_state(self, name: str) -> dict:
        r = self.conn.execute("SELECT state FROM source_state WHERE source=?", (name,)).fetchone()
        return json.loads(r[0]) if r else {}

    def last_run(self, name: str) -> datetime | None:
        r = self.conn.execute("SELECT last_run FROM source_state WHERE source=?", (name,)).fetchone()
        return datetime.fromisoformat(r[0]) if r and r[0] else None

    def save_source(self, name: str, state: dict, ok: bool, error: str | None, fetched: int) -> None:
        now = _iso(utcnow())
        with self._lock:
            self.conn.execute(
                """INSERT INTO source_state (source, state, last_run, last_ok, last_error, fetched)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source) DO UPDATE SET state=excluded.state, last_run=excluded.last_run,
                     last_ok=COALESCE(excluded.last_ok, source_state.last_ok), last_error=excluded.last_error,
                     fetched=source_state.fetched + excluded.fetched""",
                (name, json.dumps(state), now, now if ok else None, error, fetched),
            )

    def sources(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT source, last_run, last_ok, last_error, fetched FROM source_state ORDER BY source")]

    # --- helpers --------------------------------------------------------------

    @staticmethod
    def _item(r) -> NewsItem:
        it = NewsItem(
            source=r["source"], kind=r["kind"], title=r["title"], body=r["body"], url=r["url"], author=r["author"],
            published_at=datetime.fromisoformat(r["published_at"]), symbols=json.loads(r["symbols"]),
            engagement=json.loads(r["engagement"]), credibility=r["credibility"], lang=r["lang"], id=r["id"],
            cluster_id=r["cluster_id"],
        )
        return it


def _about(d: dict, symbol: str) -> bool:
    if symbol.startswith("MARKET:"):
        market = symbol.split(":", 1)[1]
        return d.get("scope") in ("market", "macro") and market in (d.get("markets") or [])
    return symbol in (d.get("symbols") or []) or symbol in (d.get("score_symbols") or [])
