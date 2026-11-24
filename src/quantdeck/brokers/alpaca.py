from __future__ import annotations

import logging

from ..config import secret
from ..ratelimit import is_network_error, retry
from ..models import Account, Fill, Order, OrderStatus, OrderType, Position, Side, utcnow
from .base import Broker, _FillTracker

log = logging.getLogger(__name__)

_STATUS = {
    "new": OrderStatus.NEW,
    "accepted": OrderStatus.NEW,
    "pending_new": OrderStatus.NEW,
    "accepted_for_bidding": OrderStatus.NEW,
    "held": OrderStatus.NEW,
    "partially_filled": OrderStatus.PARTIAL,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.CANCELED,
    "done_for_day": OrderStatus.CANCELED,
    "replaced": OrderStatus.CANCELED,
    "pending_cancel": OrderStatus.NEW,
    "rejected": OrderStatus.REJECTED,
    "suspended": OrderStatus.REJECTED,
}


def _val(x):
    return getattr(x, "value", x)


class AlpacaBroker(Broker):
    """Alpaca Trading API. paper=True talks to paper-api.alpaca.markets."""

    name = "alpaca"

    def __init__(self, symbols: list[str], market: str, paper: bool = True, client=None) -> None:
        if client is None:
            from alpaca.trading.client import TradingClient

            client = TradingClient(secret("ALPACA_API_KEY"), secret("ALPACA_SECRET_KEY"), paper=paper)
        self.client = client
        self.paper = paper
        self.market = market
        # Alpaca reports crypto positions as BTCUSD but takes orders as BTC/USD
        self._alias = {s.replace("/", ""): s for s in symbols}
        self._tracker = _FillTracker()

    def _sym(self, raw: str) -> str:
        return self._alias.get(raw, raw)

    def _convert(self, o) -> Order:
        otype = _val(o.order_type)
        stop = float(o.stop_price) if o.stop_price else None
        limit = float(o.limit_price) if o.limit_price else None
        take_profit = None
        if _val(getattr(o, "order_class", None)) == "oco":
            # OCO comes back as the take-profit limit with the stop as a leg
            for leg in getattr(o, "legs", None) or []:
                if leg.stop_price:
                    stop = float(leg.stop_price)
            take_profit, limit, otype = limit, None, "stop"
        if otype == "stop_limit":
            otype, limit = "stop", None
        return Order(
            symbol=self._sym(o.symbol),
            side=Side(_val(o.side)),
            qty=float(o.qty or 0),
            type=OrderType(otype) if otype in ("market", "limit", "stop") else OrderType.MARKET,
            limit_price=limit,
            stop_price=stop,
            tif=_val(o.time_in_force),
            client_id=o.client_order_id,
            id=str(o.id),
            status=_STATUS.get(_val(o.status), OrderStatus.NEW),
            filled_qty=float(o.filled_qty or 0),
            avg_fill_price=float(o.filled_avg_price or 0),
            created_at=o.created_at or utcnow(),
            take_profit=take_profit,
        )

    def account(self) -> Account:
        a = self.client.get_account()
        return Account(cash=float(a.cash), equity=float(a.equity), buying_power=float(a.buying_power))

    def positions(self) -> dict[str, Position]:
        out = {}
        for p in self.client.get_all_positions():
            sym = self._sym(p.symbol)
            out[sym] = Position(
                sym,
                qty=float(p.qty),
                avg_price=float(p.avg_entry_price),
                last_price=float(p.current_price or 0),
            )
        return out

    def open_orders(self) -> list[Order]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
        return [self._convert(o) for o in orders]

    def submit(self, order: Order) -> Order:
        req = self._request(order)

        def attempt():
            try:
                return self.client.submit_order(req)
            except Exception as e:
                # a timeout doesn't mean the order didn't land; the client id lets us check
                if is_network_error(e):
                    existing = self._by_client_id(order.client_id)
                    if existing is not None:
                        return existing
                raise

        resp = retry(attempt, is_network_error)
        placed = self._convert(resp)
        order.id = placed.id
        order.status = placed.status
        order.filled_qty = placed.filled_qty
        order.avg_fill_price = placed.avg_fill_price
        self._tracker.track(Order(order.symbol, order.side, order.qty, id=order.id))
        log.info("alpaca %s order %s %s %s -> %s", "paper" if self.paper else "LIVE",
                 order.side.value, order.qty, order.symbol, order.id)
        return order

    def _request(self, order: Order):
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import (
            LimitOrderRequest,
            MarketOrderRequest,
            StopLimitOrderRequest,
            StopLossRequest,
            StopOrderRequest,
            TakeProfitRequest,
        )

        whole = abs(order.qty - round(order.qty)) < 1e-9
        if self.market == "crypto":
            tif = TimeInForce.GTC
        elif order.type != OrderType.MARKET and whole:
            tif = TimeInForce.GTC  # resting exits should survive the close
        else:
            tif = TimeInForce.DAY  # fractional stock orders have to be DAY
        common = dict(
            symbol=order.symbol,
            qty=round(order.qty, 9),
            side=OrderSide.BUY if order.side == Side.BUY else OrderSide.SELL,
            time_in_force=tif,
            client_order_id=order.client_id,
        )

        if order.type == OrderType.LIMIT:
            return LimitOrderRequest(limit_price=self._px(order.limit_price), **common)
        if order.type == OrderType.STOP:
            stop = self._px(order.stop_price)
            if self.market == "crypto":
                # crypto has no plain stop orders; a wide stop-limit behaves close to one
                order.take_profit = None
                limit = self._px(stop * (0.99 if order.side == Side.SELL else 1.01))
                return StopLimitOrderRequest(stop_price=stop, limit_price=limit, **common)
            if order.take_profit and whole and order.side == Side.SELL:
                # one-cancels-other exit: the limit leg is the take-profit
                return LimitOrderRequest(
                    limit_price=self._px(order.take_profit),
                    order_class=OrderClass.OCO,
                    take_profit=TakeProfitRequest(limit_price=self._px(order.take_profit)),
                    stop_loss=StopLossRequest(stop_price=stop),
                    **common,
                )
            order.take_profit = None  # can't attach it; the context watches it instead
            return StopOrderRequest(stop_price=stop, **common)
        return MarketOrderRequest(**common)

    def _by_client_id(self, client_id: str):
        try:
            return self.client.get_order_by_client_id(client_id)
        except Exception:
            return None

    def _px(self, p: float) -> float:
        if self.market == "stock":
            return round(p, 2) if p >= 1 else round(p, 4)
        return p

    def cancel(self, order_id: str) -> None:
        self.client.cancel_order_by_id(order_id)

    def sync(self) -> list[Fill]:
        fills = []
        for oid in self._tracker.tracked():
            try:
                raw = self.client.get_order_by_id(oid)
            except Exception as e:
                log.warning("could not refresh order %s: %s", oid, e)
                continue
            # an OCO exit can fill through its stop leg, which has its own id
            for leg in getattr(raw, "legs", None) or []:
                self._tracker.track(Order(self._sym(leg.symbol), Side(_val(leg.side)), float(leg.qty or 0),
                                          id=str(leg.id)))
            o = self._convert(raw)
            f = self._tracker.diff(o, utcnow())
            if f:
                fills.append(f)
            legs_open = any(
                _STATUS.get(_val(leg.status), OrderStatus.NEW).is_open for leg in (getattr(raw, "legs", None) or [])
            )
            if not o.status.is_open and not legs_open:
                self._tracker.forget(oid)
        return fills

    def market_open(self) -> bool:
        return bool(self.client.get_clock().is_open)
