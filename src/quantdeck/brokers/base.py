from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Account, Fill, Order, Position


class Broker(ABC):
    """Everything strategies touch goes through this interface.

    Backtests use SimBroker; paper and live use the Alpaca or CCXT adapters,
    or SimBroker again fed with live bars.
    """

    name = "base"

    @abstractmethod
    def account(self) -> Account: ...

    @abstractmethod
    def positions(self) -> dict[str, Position]: ...

    @abstractmethod
    def open_orders(self) -> list[Order]: ...

    @abstractmethod
    def submit(self, order: Order) -> Order: ...

    @abstractmethod
    def cancel(self, order_id: str) -> None: ...

    @abstractmethod
    def sync(self) -> list[Fill]:
        """Refresh state from the venue and return fills seen since the last call."""

    def cancel_all(self) -> None:
        for o in self.open_orders():
            if o.id:
                self.cancel(o.id)

    def close(self) -> None:
        pass


class _FillTracker:
    """Turns order snapshots into fill events for brokers that only expose order state."""

    def __init__(self) -> None:
        # order id -> (filled qty, avg price) at the last check
        self._seen: dict[str, tuple[float, float]] = {}

    def track(self, order: Order) -> None:
        if order.id and order.id not in self._seen:
            self._seen[order.id] = (order.filled_qty, order.avg_fill_price)

    def diff(self, order: Order, ts, commission: float = 0.0) -> Fill | None:
        if not order.id:
            return None
        prev_qty, prev_avg = self._seen.get(order.id, (0.0, 0.0))
        new_qty = order.filled_qty - prev_qty
        self._seen[order.id] = (order.filled_qty, order.avg_fill_price)
        if new_qty <= 1e-12:
            return None
        # price of just this slice, backed out of the running average
        price = (order.filled_qty * order.avg_fill_price - prev_qty * prev_avg) / new_qty
        return Fill(order.id, order.symbol, order.side, new_qty, price, commission, ts)

    def forget(self, order_id: str) -> None:
        self._seen.pop(order_id, None)

    def tracked(self) -> list[str]:
        return list(self._seen)
