from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class OrderStatus(str, Enum):
    NEW = "new"
    PARTIAL = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"

    @property
    def is_open(self) -> bool:
        return self in (OrderStatus.NEW, OrderStatus.PARTIAL)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Bar:
    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Order:
    symbol: str
    side: Side
    qty: float
    type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    tif: str = "gtc"
    client_id: str = field(default_factory=lambda: "qd" + uuid.uuid4().hex[:20])
    id: Optional[str] = None
    status: OrderStatus = OrderStatus.NEW
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    created_at: datetime = field(default_factory=utcnow)
    reason: str = ""
    take_profit: Optional[float] = None  # stop orders only: one-cancels-other exit
    protective: bool = False

    @property
    def remaining(self) -> float:
        return max(self.qty - self.filled_qty, 0.0)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "client_id": self.client_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "qty": self.qty,
            "type": self.type.value,
            "limit_price": self.limit_price,
            "stop_price": self.stop_price,
            "status": self.status.value,
            "filled_qty": self.filled_qty,
            "avg_fill_price": self.avg_fill_price,
            "created_at": self.created_at.isoformat(),
            "reason": self.reason,
            "take_profit": self.take_profit,
            "protective": self.protective,
        }


@dataclass
class Fill:
    order_id: str
    symbol: str
    side: Side
    qty: float
    price: float
    commission: float
    ts: datetime

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "qty": self.qty,
            "price": self.price,
            "commission": self.commission,
            "ts": self.ts.isoformat(),
        }


@dataclass
class Position:
    symbol: str
    qty: float = 0.0
    avg_price: float = 0.0
    last_price: float = 0.0

    @property
    def market_value(self) -> float:
        return self.qty * self.last_price

    @property
    def unrealized_pnl(self) -> float:
        return (self.last_price - self.avg_price) * self.qty

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "qty": self.qty,
            "avg_price": self.avg_price,
            "last_price": self.last_price,
            "market_value": self.market_value,
            "unrealized_pnl": self.unrealized_pnl,
        }


@dataclass
class Account:
    cash: float
    equity: float
    buying_power: float

    def to_dict(self) -> dict:
        return {"cash": self.cash, "equity": self.equity, "buying_power": self.buying_power}
