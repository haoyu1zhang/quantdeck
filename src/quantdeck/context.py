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
        self._hist = {s: History(lookback) for s in symbols}
        self._prices: dict[str, float] = {}
        self._account: Account | None = None
        self._positions: dict[str, Position] = {}
        self._open: list[Order] = []
        self._on_order = on_order
        self.rejected: list[Order] = []

    # --- engine side --------------------------------------------------------

    def push_bar(self, bar: Bar) -> None:
        self._hist[bar.symbol].append(bar)
        self._prices[bar.symbol] = bar.close

    def refresh(self) -> None:
        self._account = self.broker.account()
        self._positions = self.broker.positions()
        self._open = self.broker.open_orders()
        for sym, p in self._positions.items():
            if sym in self._prices:
                p.last_price = self._prices[sym]

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

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return [o for o in self._open if symbol is None or o.symbol == symbol]

    def pending_qty(self, symbol: str) -> float:
        q = 0.0
        for o in self.open_orders(symbol):
            q += o.remaining if o.side == Side.BUY else -o.remaining
        return q

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
            # no margin: don't ask for more than the cash can pay for
            px = limit or self.price(symbol)
            if px > 0:
                delta = min(delta, max(self.account.buying_power, 0.0) * 0.999 / px)
                if not self.fractional:
                    delta = math.floor(delta)
                if delta <= 1e-9:
                    return None
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
        for o in self.open_orders(symbol):
            if o.id:
                self.broker.cancel(o.id)
        self._open = [o for o in self._open if symbol is not None and o.symbol != symbol]

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
        reason = self.risk.check(order, ref_price, self.account, self._positions, self._prices)
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
