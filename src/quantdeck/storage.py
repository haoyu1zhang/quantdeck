"""SQLite record of a paper/live run: orders, fills and the equity curve.

WAL mode lets the dashboard read while the runner writes, without either
blocking the other.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import pandas as pd

from .models import Fill, Order, Side

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    client_id   TEXT PRIMARY KEY,
    id          TEXT,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,
    qty         REAL NOT NULL,
    type        TEXT NOT NULL,
    limit_price REAL,
    stop_price  REAL,
    take_profit REAL,
    status      TEXT NOT NULL,
    filled_qty  REAL NOT NULL DEFAULT 0,
    avg_price   REAL NOT NULL DEFAULT 0,
    protective  INTEGER NOT NULL DEFAULT 0,
    reason      TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_id ON orders(id);
CREATE TABLE IF NOT EXISTS fills (
    rowid       INTEGER PRIMARY KEY,
    order_id    TEXT,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,
    qty         REAL NOT NULL,
    price       REAL NOT NULL,
    commission  REAL NOT NULL DEFAULT 0,
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fills_ts ON fills(ts);
CREATE TABLE IF NOT EXISTS equity (
    ts      TEXT PRIMARY KEY,
    equity  REAL NOT NULL
);
"""


def _now() -> str:
    return datetime.now().astimezone().isoformat()


class TradeStore:
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
        self._lock = threading.Lock()

    def close(self) -> None:
        self.conn.close()

    # --- writes -------------------------------------------------------------

    def record_order(self, o: Order) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO orders (client_id, id, symbol, side, qty, type, limit_price, stop_price, take_profit,
                                       status, filled_qty, avg_price, protective, reason, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(client_id) DO UPDATE SET
                       id=excluded.id, status=excluded.status, filled_qty=excluded.filled_qty,
                       avg_price=excluded.avg_price, reason=excluded.reason, updated_at=excluded.updated_at""",
                (o.client_id, o.id, o.symbol, o.side.value, o.qty, o.type.value, o.limit_price, o.stop_price,
                 o.take_profit, o.status.value, o.filled_qty, o.avg_fill_price, int(o.protective), o.reason,
                 o.created_at.isoformat(), _now()),
            )

    def record_fill(self, f: Fill) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO fills (order_id, symbol, side, qty, price, commission, ts) VALUES (?,?,?,?,?,?,?)",
                (f.order_id, f.symbol, f.side.value, f.qty, f.price, f.commission, pd.Timestamp(f.ts).isoformat()),
            )
            self.conn.execute(
                """UPDATE orders SET filled_qty = filled_qty + ?,
                       avg_price = CASE WHEN filled_qty + ? > 0
                                   THEN (avg_price * filled_qty + ? * ?) / (filled_qty + ?) ELSE 0 END,
                       status = CASE WHEN filled_qty + ? >= qty - 1e-9 THEN 'filled' ELSE 'partially_filled' END,
                       updated_at = ?
                   WHERE id = ? AND status NOT IN ('filled')""",
                (f.qty, f.qty, f.price, f.qty, f.qty, f.qty, _now(), f.order_id),
            )

    def record_equity(self, ts, equity: float) -> None:
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO equity (ts, equity) VALUES (?, ?)",
                              (pd.Timestamp(ts).isoformat(), float(equity)))

    # --- reads --------------------------------------------------------------

    def equity(self, limit: int = 5000) -> pd.Series:
        rows = self.conn.execute(
            "SELECT ts, equity FROM (SELECT * FROM equity ORDER BY ts DESC LIMIT ?) ORDER BY ts", (limit,)
        ).fetchall()
        if not rows:
            return pd.Series(dtype=float, name="equity")
        ts, eq = zip(*rows, strict=True)
        return pd.Series(eq, index=pd.to_datetime(list(ts), utc=True), name="equity")

    def fills(self, limit: int | None = None) -> list[Fill]:
        q = "SELECT order_id, symbol, side, qty, price, commission, ts FROM fills ORDER BY ts, rowid"
        rows = self.conn.execute(q).fetchall()
        if limit:
            rows = rows[-limit:]
        return [Fill(r[0], r[1], Side(r[2]), r[3], r[4], r[5], pd.Timestamp(r[6]).to_pydatetime()) for r in rows]

    def orders(self, limit: int = 100) -> list[dict]:
        cur = self.conn.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    def stats(self) -> dict:
        """Realised results from closed round trips."""
        from .backtest.metrics import round_trips

        trades = round_trips(self.fills())
        out = {"trades": int(len(trades)), "realized_pnl": float(trades.pnl.sum()) if len(trades) else 0.0}
        if len(trades):
            wins = trades[trades.pnl > 0]
            losses = trades[trades.pnl <= 0]
            out["win_rate"] = len(wins) / len(trades)
            gross_loss = -losses.pnl.sum()
            out["profit_factor"] = float(wins.pnl.sum() / gross_loss) if gross_loss > 0 else None
            out["by_symbol"] = {s: float(g.pnl.sum()) for s, g in trades.groupby("symbol")}
        commissions = self.conn.execute("SELECT COALESCE(SUM(commission), 0) FROM fills").fetchone()[0]
        out["commissions"] = float(commissions)
        trades = trades.copy()
        for col in ("entry_ts", "exit_ts"):
            trades[col] = trades[col].astype(str)
        out["recent_trades"] = trades.tail(50).round(6).to_dict(orient="records")[::-1]
        return out
