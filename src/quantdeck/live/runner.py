from __future__ import annotations

import json
import logging
import os
import signal
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from ..brokers.base import Broker
from ..brokers.sim import SimBroker
from ..config import Config
from ..context import Context
from ..data.sources import DataSource
from ..models import Bar, Fill, Order, OrderStatus, utcnow
from ..risk import RiskManager
from ..strategies.base import Strategy
from ..timeframes import parse_timeframe
from .clock import MarketClock, clock_for
from .runlock import RunLock

log = logging.getLogger(__name__)

MAX_FETCH_WORKERS = 8


def _write_json(path: Path, data) -> None:
    # write then rename, so readers (the dashboard) never see half a file
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, default=str, indent=1), encoding="utf-8")
    os.replace(tmp, path)


class Journal:
    """Append-only JSONL log of orders and fills. Safe to call from several threads."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def write(self, kind: str, payload: dict) -> None:
        line = json.dumps({"kind": kind, "at": utcnow().isoformat(), **payload}, default=str)
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


class LiveRunner:
    """Drives a strategy against a real-time feed.

    Network calls (one per symbol per poll) run in a thread pool so a slow
    endpoint doesn't hold up the others. Strategy callbacks and order placement
    stay on the main thread, in timestamp order, so there are no races between
    a fill coming in and the strategy deciding what to do next.
    """

    def __init__(
        self,
        cfg: Config,
        strategy: Strategy,
        broker: Broker,
        source: DataSource,
        clock: MarketClock | None = None,
    ) -> None:
        self.cfg = cfg
        self.strategy = strategy
        self.broker = broker
        self.source = source
        self.clock = clock or clock_for(cfg.market, broker)
        self.tf = parse_timeframe(cfg.data.timeframe)
        self.dir = cfg.run_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = RunLock(self.dir / "run.lock")
        self.journal = Journal(self.dir / "journal.jsonl")
        self.risk = RiskManager(cfg.risk)
        self.ctx = Context(broker, self.risk, cfg.symbols, fractional=cfg.fractional, on_order=self._on_order)
        self._last_ts: dict[str, pd.Timestamp] = {}
        self._recent: deque = deque(maxlen=50)
        self._errors = 0
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=min(MAX_FETCH_WORKERS, max(len(cfg.symbols), 1)))
        self.started_at = utcnow()

    # --- lifecycle ----------------------------------------------------------

    def stop(self, *_):
        self._stop.set()

    def run(self, max_loops: int | None = None) -> None:
        with self.lock:
            self._install_signals()
            (self.dir / "STOP").unlink(missing_ok=True)
            try:
                self._start()
                loops = 0
                while not self._stop.is_set():
                    self._tick()
                    loops += 1
                    if max_loops is not None and loops >= max_loops:
                        break
                    self._stop.wait(self.cfg.poll_seconds)
            finally:
                self._shutdown()

    def _install_signals(self) -> None:
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self.stop)
            signal.signal(signal.SIGTERM, self.stop)

    def _start(self) -> None:
        log.info("starting %s in %s mode (%s, broker=%s)", self.cfg.name, self.cfg.mode,
                 self.cfg.market, self.broker.name)
        if isinstance(self.broker, SimBroker):
            state_file = self.dir / "sim_state.json"
            if state_file.exists():
                self.broker.load_state(json.loads(state_file.read_text()))
                log.info("restored local paper account from %s", state_file)

        # warm up indicators with history, without trading
        self.ctx.trading = False
        frames = self._fetch_all(self.cfg.data.warmup)
        for sym, df in frames.items():
            for bar in self._closed_bars(sym, df):
                self.ctx.now = bar.ts
                self.ctx.push_bar(bar)
                if isinstance(self.broker, SimBroker):
                    self.broker.mark(sym, bar.close)
        self.ctx.trading = True

        # reconcile with whatever the venue says we hold
        self.broker.sync()
        self.ctx.refresh()
        acct = self.ctx.account
        self.risk.new_day(utcnow().date(), acct.equity)
        log.info("account equity %.2f cash %.2f", acct.equity, acct.cash)
        for p in self.ctx.positions().values():
            log.info("existing position %s %s @ %.4f", p.symbol, p.qty, p.avg_price)
        for o in self.ctx.open_orders():
            log.info("existing open order %s %s %s %s", o.id, o.side.value, o.qty, o.symbol)
        self.strategy.on_start(self.ctx)
        self._write_status("running")

    def _shutdown(self) -> None:
        log.info("stopping %s", self.cfg.name)
        try:
            self.strategy.on_stop(self.ctx)
        except Exception:
            log.exception("on_stop failed")
        if self.cfg.mode != "backtest":
            try:
                self.broker.cancel_all()
            except Exception as e:
                log.warning("cancel on shutdown failed: %s", e)
        self._save_sim()
        self._write_status("stopped")
        self._pool.shutdown(wait=False, cancel_futures=True)
        self.broker.close()

    # --- one poll -----------------------------------------------------------

    def _tick(self) -> None:
        if (self.dir / "STOP").exists():
            log.info("STOP file found")
            self._stop.set()
            return
        try:
            self._handle_fills(self.broker.sync())

            if not self.clock.is_open():
                self._write_status("market closed")
                return

            frames = self._fetch_all(10)
            new_bars = []
            for sym, df in frames.items():
                new_bars.extend(self._closed_bars(sym, df))
            new_bars.sort(key=lambda b: (b.ts, b.symbol))

            for bar in new_bars:
                self._on_bar(bar)

            self._handle_fills(self.broker.sync())
            self._save_sim()
            self._errors = 0
            self._write_status("halted" if self.risk.halted else "running")
        except Exception as e:
            self._errors += 1
            log.exception("loop error (%d in a row)", self._errors)
            self._write_status(f"error: {e}")
            if self._errors >= 10:
                self.risk.halt("too many consecutive errors")
            # back off a bit more each time, up to five minutes
            self._stop.wait(min(300, 5 * self._errors))

    def _on_bar(self, bar: Bar) -> None:
        if isinstance(self.broker, SimBroker):
            self.broker.process_bar(bar)
            self._handle_fills(self.broker.sync())
        self.ctx.now = bar.ts
        self.ctx.push_bar(bar)
        self.ctx.refresh()
        self.risk.new_day(bar.ts.date(), self.ctx.equity)
        self.risk.update_equity(self.ctx.equity)
        self.strategy.on_bar(self.ctx, bar)
        self._append_equity(bar.ts)

    def _handle_fills(self, fills: list[Fill]) -> None:
        if not fills:
            return
        self.ctx.refresh()
        for f in fills:
            log.info("fill %s %s %s @ %.6f", f.side.value, f.qty, f.symbol, f.price)
            if not isinstance(self.broker, SimBroker):
                self._mark_filled(f)  # SimBroker updates the order objects itself
            self.journal.write("fill", f.to_dict())
            self.strategy.on_fill(self.ctx, f)

    def _mark_filled(self, f: Fill) -> None:
        for o in self._recent:
            if o.id == f.order_id:
                total = o.filled_qty + f.qty
                o.avg_fill_price = (o.avg_fill_price * o.filled_qty + f.price * f.qty) / total
                o.filled_qty = total
                o.status = OrderStatus.FILLED if o.remaining <= 1e-9 else OrderStatus.PARTIAL
                return

    def _on_order(self, order: Order) -> None:
        self._recent.appendleft(order)
        self.journal.write("order", order.to_dict())

    # --- data ---------------------------------------------------------------

    def _fetch_all(self, n: int) -> dict[str, pd.DataFrame]:
        futures = {
            sym: self._pool.submit(self.source.latest, sym, self.cfg.data.timeframe, n)
            for sym in self.cfg.symbols
        }
        out = {}
        for sym, fut in futures.items():
            try:
                out[sym] = fut.result(timeout=60)
            except Exception as e:
                log.warning("fetch failed for %s: %s", sym, e)
        return out

    def _closed_bars(self, sym: str, df: pd.DataFrame) -> list[Bar]:
        """Bars that have fully closed and that we haven't seen yet."""
        if df is None or df.empty:
            return []
        now = pd.Timestamp.now(tz="UTC")
        last = self._last_ts.get(sym)
        out = []
        for ts, r in df.iterrows():
            if ts + self.tf > now:
                continue  # still forming
            if last is not None and ts <= last:
                continue
            out.append(Bar(sym, ts, float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume)))
            last = ts
        if last is not None:
            self._last_ts[sym] = last
        return out

    # --- persistence --------------------------------------------------------

    def _save_sim(self) -> None:
        if isinstance(self.broker, SimBroker):
            _write_json(self.dir / "sim_state.json", self.broker.to_state())

    def _append_equity(self, ts) -> None:
        path = self.dir / "equity.csv"
        new = not path.exists()
        with open(path, "a", encoding="utf-8") as f:
            if new:
                f.write("ts,equity\n")
            f.write(f"{pd.Timestamp(ts).isoformat()},{self.ctx.equity:.6f}\n")

    def _write_status(self, state: str) -> None:
        try:
            acct = self.ctx.account
            account = acct.to_dict()
        except Exception:
            account = None
        status = {
            "name": self.cfg.name,
            "mode": self.cfg.mode,
            "market": self.cfg.market,
            "broker": self.broker.name,
            "strategy": self.cfg.strategy.name,
            "params": self.cfg.strategy.params,
            "symbols": self.cfg.symbols,
            "timeframe": self.cfg.data.timeframe,
            "state": state,
            "pid": os.getpid(),
            "started_at": self.started_at.isoformat(),
            "heartbeat": utcnow().isoformat(),
            "halted": self.risk.halted,
            "halt_reason": self.risk.halt_reason,
            "account": account,
            "positions": [p.to_dict() for p in self.ctx.positions().values()],
            "open_orders": [o.to_dict() for o in self.ctx.open_orders()],
            "recent_orders": [o.to_dict() for o in self._recent],
            "last_bars": {s: t.isoformat() for s, t in self._last_ts.items()},
        }
        _write_json(self.dir / "status.json", status)
