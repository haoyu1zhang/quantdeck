from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Callable, Optional

import numpy as np
import pandas as pd

from .brokers.base import Broker
from .models import Account, Bar, Order, OrderStatus, OrderType, Position, Side, utcnow
from .risk import RiskManager

log = logging.getLogger("quantdeck.strategy")


class History:
    __slots__ = ("maxlen", "ts", "open", "high", "low", "close", "volume")

    def __init__(self, maxlen: int) -> None:
        self.maxlen = maxlen
        self.ts: list = []
        self.open: list = []
        self.high: list = []
        self.low: list = []
        self.close: list = []
        self.volume: list = []

    def append(self, bar: Bar) -> None:
        if self.ts and bar.ts <= self.ts[-1]:
            return  # duplicate or out of order
        self.ts.append(bar.ts)
        self.open.append(bar.open)
        self.high.append(bar.high)
        self.low.append(bar.low)
        self.close.append(bar.close)
        self.volume.append(bar.volume)
        if len(self.ts) > self.maxlen * 2:
            for name in ("ts", "open", "high", "low", "close", "volume"):
                setattr(self, name, getattr(self, name)[-self.maxlen:])

    def __len__(self) -> int:
        return len(self.ts)


class Context:
    """The handle a strategy gets. Same object in backtest, paper and live."""

    def __init__(
        self,
        broker: Broker,
        risk: RiskManager,
        symbols: list[str],
        fractional: bool = True,
        lookback: int = 5000,
        on_order: Optional[Callable[[Order], None]] = None,
    ) -> None:
        self.broker = broker
        self.risk = risk
        self.symbols = list(symbols)
        self.fractional = fractional
        self.now: datetime = utcnow()
        self.trading = True
        self.state: dict = {}  # free space for strategies
        self.periods_per_year: float = 365.0  # set by the engine/runner from the timeframe
        self._hist = {s: History(lookback) for s in symbols}
        self._prices: dict[str, float] = {}
        self._account: Account | None = None
        self._positions: dict[str, Position] = {}
        self._open: list[Order] = []
        self._on_order = on_order
        self._peak: dict[str, float] = {}
        self._protective: dict[str, float | None] = {}  # order id -> take-profit price
        self.rejected: list[Order] = []

    # --- engine side --------------------------------------------------------

    def push_bar(self, bar: Bar) -> None:
        self._hist[bar.symbol].append(bar)
        self._prices[bar.symbol] = bar.close

    def refresh(self) -> None:
        self._account = self.broker.account()
        self._positions = self.broker.positions()
        self._open = self.broker.open_orders()
        # live brokers hand back fresh objects; re-attach what only we know
        for o in self._open:
            if o.id in self._protective:
                o.protective = True
                o.take_profit = o.take_profit or self._protective[o.id]
        for sym, p in self._positions.items():
            if sym in self._prices:
                p.last_price = self._prices[sym]

    def mark_prices(self) -> None:
        """Cheap refresh when nothing traded: just reprice what we hold."""
        if self._account is None:
            self.refresh()
            return
        value = 0.0
        for sym, p in self._positions.items():
            p.last_price = self._prices.get(sym, p.last_price)
            value += p.qty * p.last_price
        a = self._account
        self._account = type(a)(cash=a.cash, equity=a.cash + value, buying_power=a.buying_power)

    # --- data ---------------------------------------------------------------

    def bars(self, symbol: str) -> int:
        return len(self._hist[symbol])

    def closes(self, symbol: str, n: int | None = None) -> np.ndarray:
        c = self._hist[symbol].close
        return np.asarray(c[-n:] if n else c, dtype=float)

    def highs(self, symbol: str, n: int | None = None) -> np.ndarray:
        h = self._hist[symbol].high
        return np.asarray(h[-n:] if n else h, dtype=float)

    def lows(self, symbol: str, n: int | None = None) -> np.ndarray:
        lo = self._hist[symbol].low
        return np.asarray(lo[-n:] if n else lo, dtype=float)

    def history(self, symbol: str, n: int | None = None) -> pd.DataFrame:
        h = self._hist[symbol]
        sl = slice(-n, None) if n else slice(None)
        return pd.DataFrame(
            {
                "open": h.open[sl],
                "high": h.high[sl],
                "low": h.low[sl],
                "close": h.close[sl],
                "volume": h.volume[sl],
            },
            index=pd.DatetimeIndex(h.ts[sl], name="ts"),
        )

    def price(self, symbol: str) -> float:
        return self._prices.get(symbol, 0.0)

    # --- portfolio ----------------------------------------------------------

    @property
    def account(self) -> Account:
        if self._account is None:
            self.refresh()
        return self._account

    @property
    def equity(self) -> float:
        return self.account.equity

    @property
    def cash(self) -> float:
        return self.account.cash

    def position(self, symbol: str) -> float:
        p = self._positions.get(symbol)
        return p.qty if p else 0.0

    def positions(self) -> dict[str, Position]:
        return dict(self._positions)

    def open_orders(self, symbol: str | None = None, protective: bool = False) -> list[Order]:
        """Working orders. Protective exits are left out unless asked for."""
        return [
            o for o in self._open
            if (symbol is None or o.symbol == symbol) and (protective or not o.protective)
        ]

    def pending_qty(self, symbol: str) -> float:
        q = 0.0
        for o in self.open_orders(symbol):
            q += o.remaining if o.side == Side.BUY else -o.remaining
        return q

    def _projected(self) -> dict[str, Position]:
        """Positions as they'll be once the non-protective open orders fill."""
        out = {s: Position(s, p.qty, p.avg_price, p.last_price) for s, p in self._positions.items()}
        for o in self.open_orders():
            p = out.setdefault(o.symbol, Position(o.symbol, last_price=self.price(o.symbol)))
            p.qty += o.remaining if o.side == Side.BUY else -o.remaining
        return out

    def available_cash(self) -> float:
        """Buying power after the open orders settle: sells add, buys take away."""
        cash = max(self.account.buying_power, 0.0)
        for o in self.open_orders():
            px = o.limit_price or o.stop_price or self.price(o.symbol)
            cash += (-1 if o.side == Side.BUY else 1) * o.remaining * px
        return cash

    # --- orders -------------------------------------------------------------

    def buy(self, symbol: str, qty: float, limit: float | None = None, stop: float | None = None) -> Order | None:
        return self._order(symbol, Side.BUY, qty, limit, stop)

    def sell(self, symbol: str, qty: float, limit: float | None = None, stop: float | None = None) -> Order | None:
        return self._order(symbol, Side.SELL, qty, limit, stop)

    def order_target_qty(self, symbol: str, target: float, limit: float | None = None) -> Order | None:
        delta = target - self.position(symbol) - self.pending_qty(symbol)
        if not self.fractional:
            delta = math.trunc(delta)
        if abs(delta) < 1e-9:
            return None
        if delta > 0 and not self.risk.cfg.allow_short:
            # no margin: don't ask for more than the cash can pay for, counting
            # what the sells already queued this bar will bring in
            px = limit or self.price(symbol)
            if px > 0:
                delta = min(delta, max(self.available_cash(), 0.0) * 0.999 / px)
                if not self.fractional:
                    delta = math.floor(delta)
                if delta <= 1e-9:
                    return None
        if delta < 0:
            # exiting: the protective orders would otherwise hold the same shares
            self._cancel_protection(symbol)
        side = Side.BUY if delta > 0 else Side.SELL
        return self._order(symbol, side, abs(delta), limit, None)

    def order_target_percent(self, symbol: str, pct: float, limit: float | None = None) -> Order | None:
        px = limit or self.price(symbol)
        if px <= 0:
            return None
        return self.order_target_qty(symbol, self.equity * pct / px, limit)

    def close_position(self, symbol: str) -> Order | None:
        return self.order_target_qty(symbol, 0.0)

    def cancel_all(self, symbol: str | None = None) -> None:
        for o in self.open_orders(symbol, protective=True):
            if o.id:
                self.broker.cancel(o.id)
        self._open = [o for o in self._open if symbol is not None and o.symbol != symbol]

    def vol_target_weight(self, symbol: str, target_vol: float, lookback: int = 20,
                          cap: float = 1.0) -> float:
        """Portfolio weight that would give `symbol` roughly `target_vol` annualised volatility."""
        closes = self.closes(symbol, lookback + 1)
        if len(closes) < lookback + 1:
            return 0.0
        rets = np.diff(np.log(closes))
        vol = rets.std(ddof=1) * math.sqrt(self.periods_per_year)
        if not np.isfinite(vol) or vol <= 0:
            return 0.0
        return float(min(target_vol / vol, cap))

    # --- protective exits ---------------------------------------------------

    def is_protective(self, order_id: str | None) -> bool:
        return order_id in self._protective

    def adopt_protection(self) -> None:
        """After a restart, treat resting stop orders on held symbols as ours."""
        for o in self._open:
            if o.side == Side.SELL and o.type == OrderType.STOP and self.position(o.symbol) > 0:
                o.protective = True
                self._protective[o.id] = o.take_profit

    def sync_protection(self) -> None:
        """Keep one stop (with take-profit where the broker allows) resting for every long."""
        cfg = self.risk.cfg
        if not self.trading or (cfg.stop_loss_pct is None and cfg.take_profit_pct is None):
            return
        for sym in self.symbols:
            qty = self.position(sym)
            stops = [o for o in self.open_orders(sym, protective=True) if o.protective]
            if qty <= 1e-9:
                if stops:
                    self._cancel_protection(sym)
                self._peak.pop(sym, None)
                continue
            if self.pending_qty(sym) < 0:
                continue  # the strategy is already selling

            pos = self._positions[sym]
            price = self.price(sym)
            self._peak[sym] = max(self._peak.get(sym, pos.avg_price), price)
            anchor = self._peak[sym] if cfg.trailing_stop else pos.avg_price
            stop_px = anchor * (1 - cfg.stop_loss_pct) if cfg.stop_loss_pct else None
            tp_px = pos.avg_price * (1 + cfg.take_profit_pct) if cfg.take_profit_pct else None

            # a take-profit the broker couldn't attach (or no stop at all): watch it ourselves
            soft_tp = tp_px is not None and not any(o.take_profit for o in stops)
            if soft_tp and price >= tp_px:
                self.log("take-profit %s at %.4f", sym, price)
                self.close_position(sym)
                continue
            if stop_px is None:
                continue

            current = stops[0] if len(stops) == 1 else None
            stale = (
                current is None
                or abs(current.remaining - qty) > max(qty * 1e-6, 1e-9)
                or (cfg.trailing_stop and stop_px > (current.stop_price or 0) * 1.005)
            )
            if not stale:
                continue
            self._cancel_protection(sym)
            if price and stop_px >= price:
                # already through the stop; get out now rather than rest an order
                self._order(sym, Side.SELL, qty, None, None)
                continue
            order = Order(sym, Side.SELL, qty, OrderType.STOP, stop_price=stop_px,
                          take_profit=tp_px, created_at=self.now, protective=True)
            try:
                order = self.broker.submit(order)
            except Exception as e:
                log.error("could not place protective stop for %s: %s", sym, e)
                continue
            if order.status != OrderStatus.REJECTED:
                self._open.append(order)
                self._protective[order.id] = order.take_profit
            if self._on_order:
                self._on_order(order)

    def _cancel_protection(self, symbol: str) -> None:
        keep = []
        for o in self._open:
            if o.protective and o.symbol == symbol:
                self._protective.pop(o.id, None)
                try:
                    if o.id:
                        self.broker.cancel(o.id)
                except Exception as e:
                    log.warning("cancel of protective order %s failed: %s", o.id, e)
            else:
                keep.append(o)
        self._open = keep

    def log(self, msg: str, *args) -> None:
        log.info("[%s] " + msg, self.now.isoformat(), *args)

    def _order(self, symbol, side, qty, limit, stop) -> Order | None:
        if not self.trading:
            return None
        if symbol not in self._hist:
            raise KeyError(f"{symbol} is not in the configured symbols")
        if not self.fractional:
            qty = math.floor(qty + 1e-9)
        if qty <= 0:
            return None
        otype = OrderType.LIMIT if limit else OrderType.STOP if stop else OrderType.MARKET
        order = Order(symbol, side, float(qty), otype, limit_price=limit, stop_price=stop, created_at=self.now)

        ref_price = limit or stop or self.price(symbol)
        reason = self.risk.check(order, ref_price, self.account, self._projected(), self._prices)
        if reason:
            order.status = OrderStatus.REJECTED
            order.reason = reason
            self.rejected.append(order)
            log.info("order blocked by risk: %s %s %s (%s)", side.value, qty, symbol, reason)
        else:
            try:
                order = self.broker.submit(order)
            except Exception as e:  # venue errors shouldn't kill the loop
                order.status = OrderStatus.REJECTED
                order.reason = f"broker error: {e}"
                log.error("submit failed for %s: %s", symbol, e)
            if order.status != OrderStatus.REJECTED:
                self.risk.record(order)
                self._open.append(order)
        if self._on_order:
            self._on_order(order)
        return order
