from __future__ import annotations

import itertools
import math

from ..config import FeeConfig
from ..models import Account, Bar, Fill, Order, OrderStatus, OrderType, Position, Side
from .base import Broker

EPS = 1e-9


class SimBroker(Broker):
    """Local matching against bars.

    Orders submitted while bar N is being processed are matched against bar N+1,
    so a strategy can never trade at a price it used to make the decision.
    """

    name = "sim"

    def __init__(
        self,
        cash: float,
        fees: FeeConfig | None = None,
        fractional: bool = True,
        allow_short: bool = False,
    ) -> None:
        self.cash = float(cash)
        self.fees = fees or FeeConfig()
        self.fractional = fractional
        self.allow_short = allow_short
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, Order] = {}
        self._working: dict[str, Order] = {}  # open orders only, so matching stays O(open)
        self._last: dict[str, float] = {}
        self._new_fills: list[Fill] = []
        self.fills: list[Fill] = []
        self._ids = itertools.count(1)

    # --- Broker API ---------------------------------------------------------

    def account(self) -> Account:
        equity = self.cash + sum(p.qty * self._last.get(s, p.avg_price) for s, p in self._positions.items())
        return Account(cash=self.cash, equity=equity, buying_power=max(self.cash, 0.0))

    def positions(self) -> dict[str, Position]:
        return {s: p for s, p in self._positions.items() if abs(p.qty) > EPS}

    def open_orders(self) -> list[Order]:
        return [o for o in self._working.values() if o.status.is_open]

    def submit(self, order: Order) -> Order:
        order.id = f"sim-{next(self._ids)}"
        if not self.fractional:
            order.qty = math.floor(order.qty + EPS)
        if order.qty <= EPS:
            order.status = OrderStatus.REJECTED
            order.reason = "quantity rounds to zero"
        elif order.type == OrderType.LIMIT and order.limit_price is None:
            order.status = OrderStatus.REJECTED
            order.reason = "limit order without limit_price"
        elif order.type == OrderType.STOP and order.stop_price is None:
            order.status = OrderStatus.REJECTED
            order.reason = "stop order without stop_price"
        self._orders[order.id] = order
        if order.status.is_open:
            self._working[order.id] = order
        return order

    def cancel(self, order_id: str) -> None:
        o = self._orders.get(order_id)
        if o and o.status.is_open:
            o.status = OrderStatus.CANCELED
        self._working.pop(order_id, None)

    def sync(self) -> list[Fill]:
        out, self._new_fills = self._new_fills, []
        return out

    # --- persistence (local paper trading survives restarts) ----------------

    def to_state(self) -> dict:
        return {
            "cash": self.cash,
            "positions": {s: [p.qty, p.avg_price] for s, p in self.positions().items()},
            "last": dict(self._last),
        }

    def load_state(self, state: dict) -> None:
        self.cash = float(state["cash"])
        self._last = {k: float(v) for k, v in state.get("last", {}).items()}
        self._positions = {
            s: Position(s, qty=q, avg_price=a, last_price=self._last.get(s, a))
            for s, (q, a) in state.get("positions", {}).items()
        }

    # --- simulation ---------------------------------------------------------

    def mark(self, symbol: str, price: float) -> None:
        self._last[symbol] = price
        if symbol in self._positions:
            self._positions[symbol].last_price = price

    def process_bars(self, bars: list[Bar]) -> list[Fill]:
        """Match one timestamp's bars. Sells go first so their proceeds can fund buys."""
        fills = []
        for side in (Side.SELL, Side.BUY):
            for bar in bars:
                fills += self._process(bar, side)
        for bar in bars:
            self.mark(bar.symbol, bar.close)
        for oid in [k for k, o in self._working.items() if not o.status.is_open]:
            del self._working[oid]
        return fills

    def process_bar(self, bar: Bar) -> list[Fill]:
        return self.process_bars([bar])

    def _process(self, bar: Bar, side: Side) -> list[Fill]:
        fills = []
        for o in list(self._working.values()):
            if o.symbol != bar.symbol or o.side != side or not o.status.is_open:
                continue
            if o.created_at >= bar.ts:
                # submitted during or after this bar; wait for the next one
                continue
            price = self._match(o, bar)
            if price is not None:
                f = self._execute(o, price, bar)
                if f:
                    fills.append(f)
        return fills

    def _match(self, o: Order, bar: Bar) -> float | None:
        slip = self.fees.slippage
        buy = o.side == Side.BUY
        if o.type == OrderType.MARKET:
            return bar.open * (1 + slip if buy else 1 - slip)
        if o.type == OrderType.LIMIT:
            lp = o.limit_price
            if buy and bar.low <= lp:
                return min(bar.open, lp)
            if not buy and bar.high >= lp:
                return max(bar.open, lp)
            return None
        if o.type == OrderType.STOP:
            sp, tp = o.stop_price, o.take_profit
            if buy and bar.high >= sp:
                return max(bar.open, sp) * (1 + slip)
            if not buy:
                # if a bar spans both legs we can't know which came first; assume the stop
                if bar.low <= sp:
                    return min(bar.open, sp) * (1 - slip)
                if tp is not None and bar.high >= tp:
                    return max(bar.open, tp)
        return None

    def _commission(self, qty: float, price: float) -> float:
        c = qty * price * self.fees.rate + qty * self.fees.per_share
        return max(c, self.fees.minimum)

    def _execute(self, o: Order, price: float, bar: Bar) -> Fill | None:
        qty = o.remaining
        pos = self._positions.get(o.symbol) or Position(o.symbol)
        cash_limited = False

        cap = self.fees.max_volume_pct
        vol_limited = False
        if cap and bar.volume > 0 and qty > bar.volume * cap:
            qty = bar.volume * cap
            if not self.fractional:
                qty = math.floor(qty + EPS)
            vol_limited = True

        if o.side == Side.BUY:
            cost = qty * price + self._commission(qty, price)
            if cost > self.cash + EPS:
                # shrink to what we can afford instead of rejecting outright
                per_unit = price * (1 + self.fees.rate) + self.fees.per_share
                qty = max(self.cash - self.fees.minimum, 0.0) / per_unit
                if not self.fractional:
                    qty = math.floor(qty + EPS)
                cash_limited = True
        elif not self.allow_short and qty > max(pos.qty, 0.0) + EPS:
            qty = max(pos.qty, 0.0)
            cash_limited = True

        if qty <= EPS:
            if vol_limited and not cash_limited:
                return None  # volume cap rounded to zero this bar; try again next bar
            o.status = OrderStatus.REJECTED
            o.reason = "insufficient cash" if o.side == Side.BUY else "nothing to sell"
            return None

        commission = self._commission(qty, price)
        signed = qty if o.side == Side.BUY else -qty
        self.cash -= signed * price + commission
        self._apply(pos, signed, price)
        self._positions[o.symbol] = pos

        new_filled = o.filled_qty + qty
        o.avg_fill_price = (o.avg_fill_price * o.filled_qty + price * qty) / new_filled
        o.filled_qty = new_filled
        if o.remaining <= EPS:
            o.status = OrderStatus.FILLED
        elif cash_limited:
            # the rest can't be funded (or there's nothing left to sell); don't leave it hanging
            o.status = OrderStatus.CANCELED
            o.reason = "partially filled, rest canceled"
        else:
            o.status = OrderStatus.PARTIAL  # volume-capped, keeps working next bar

        fill = Fill(o.id, o.symbol, o.side, qty, price, commission, bar.ts)
        self.fills.append(fill)
        self._new_fills.append(fill)
        return fill

    @staticmethod
    def _apply(pos: Position, signed_qty: float, price: float) -> None:
        new_qty = pos.qty + signed_qty
        if abs(pos.qty) < EPS or (pos.qty > 0) == (signed_qty > 0):
            # opening or adding
            pos.avg_price = (pos.avg_price * pos.qty + price * signed_qty) / new_qty
        elif abs(new_qty) > EPS and (new_qty > 0) != (pos.qty > 0):
            # flipped through zero, the remainder opens at this price
            pos.avg_price = price
        if abs(new_qty) < EPS:
            new_qty, pos.avg_price = 0.0, 0.0
        pos.qty = new_qty
        pos.last_price = price
