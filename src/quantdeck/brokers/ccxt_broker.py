from __future__ import annotations

import logging
import os

from ..models import Account, Fill, Order, OrderStatus, OrderType, Position, Side, utcnow
from ..ratelimit import is_network_error, retry
from .base import Broker, _FillTracker

log = logging.getLogger(__name__)

_STATUS = {
    "open": OrderStatus.NEW,
    "closed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "expired": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
}


def _keys(exchange: str) -> dict:
    p = exchange.upper()
    cfg = {
        "apiKey": os.environ.get(f"{p}_API_KEY"),
        "secret": os.environ.get(f"{p}_SECRET"),
    }
    if os.environ.get(f"{p}_PASSWORD"):
        cfg["password"] = os.environ[f"{p}_PASSWORD"]  # OKX passphrase etc.
    if not cfg["apiKey"] or not cfg["secret"]:
        raise RuntimeError(f"set {p}_API_KEY and {p}_SECRET")
    return cfg


def build_client(exchange: str, sandbox: bool = True, demo: bool = False, keys: bool = True):
    import ccxt

    opts = {"enableRateLimit": True, "options": {"defaultType": "spot"}}
    if keys:
        opts.update(_keys(exchange))
    client = getattr(ccxt, exchange)(opts)
    if sandbox:
        client.set_sandbox_mode(True)
    if demo:
        if not hasattr(client, "enable_demo_trading"):
            raise RuntimeError(f"{exchange} has no demo trading mode in this ccxt version")
        client.enable_demo_trading(True)
    return client


# methods that never hit the network and shouldn't use up rate limit tokens
LOCAL_METHODS = ("market", "amount_to_precision", "price_to_precision", "cost_to_precision", "milliseconds", "iso8601")


class CcxtBroker(Broker):
    """Spot trading on any exchange CCXT supports.

    Spot exchanges don't track an average entry price, so we keep our own
    from the fills we see. After a restart it falls back to the current price.
    """

    name = "ccxt"

    def __init__(
        self,
        exchange: str,
        symbols: list[str],
        sandbox: bool = True,
        demo: bool = False,
        client=None,
    ) -> None:
        if client is None:
            client = build_client(exchange, sandbox, demo)
        self.client = client
        self.exchange = exchange
        self.symbols = list(symbols)
        self.client.load_markets()
        quotes = {self.client.market(s)["quote"] for s in self.symbols}
        if len(quotes) != 1:
            raise ValueError(f"all symbols must share one quote currency, got {sorted(quotes)}")
        self.quote = quotes.pop()
        self._tracker = _FillTracker()
        self._order_symbol: dict[str, str] = {}
        self._avg: dict[str, float] = {}
        self._prices: dict[str, float] = {}
        log.info("ccxt %s ready (sandbox=%s demo=%s quote=%s)", exchange, sandbox, demo, self.quote)

    # --- helpers ------------------------------------------------------------

    def _refresh_prices(self) -> None:
        try:
            tickers = self.client.fetch_tickers(self.symbols)
            for s, t in tickers.items():
                if t.get("last"):
                    self._prices[s] = float(t["last"])
        except Exception:
            for s in self.symbols:
                t = self.client.fetch_ticker(s)
                if t.get("last"):
                    self._prices[s] = float(t["last"])

    def _base(self, symbol: str) -> str:
        return self.client.market(symbol)["base"]

    def _convert(self, o: dict) -> Order:
        status = _STATUS.get(o.get("status") or "open", OrderStatus.NEW)
        filled = float(o.get("filled") or 0)
        amount = float(o.get("amount") or 0)
        if status == OrderStatus.NEW and filled > 0:
            status = OrderStatus.PARTIAL
        otype = o.get("type") or "market"
        return Order(
            symbol=o["symbol"],
            side=Side(o["side"]),
            qty=amount,
            type=OrderType(otype) if otype in ("market", "limit") else OrderType.STOP,
            limit_price=o.get("price") if otype == "limit" else None,
            stop_price=o.get("triggerPrice") or o.get("stopPrice"),
            client_id=o.get("clientOrderId") or "",
            id=str(o["id"]),
            status=status,
            filled_qty=filled,
            avg_fill_price=float(o.get("average") or o.get("price") or 0),
        )

    # --- Broker API ---------------------------------------------------------

    def account(self) -> Account:
        bal = self.client.fetch_balance()
        self._refresh_prices()
        total = bal.get("total", {})
        free = bal.get("free", {})
        cash = float(total.get(self.quote) or 0)
        equity = cash
        for s in self.symbols:
            equity += float(total.get(self._base(s)) or 0) * self._prices.get(s, 0.0)
        return Account(cash=cash, equity=equity, buying_power=float(free.get(self.quote) or 0))

    def positions(self) -> dict[str, Position]:
        bal = self.client.fetch_balance()
        total = bal.get("total", {})
        out = {}
        for s in self.symbols:
            qty = float(total.get(self._base(s)) or 0)
            min_amt = (self.client.market(s).get("limits", {}).get("amount", {}) or {}).get("min") or 0
            if qty <= max(min_amt, 1e-12):
                continue  # dust
            px = self._prices.get(s, 0.0)
            out[s] = Position(s, qty=qty, avg_price=self._avg.get(s, px), last_price=px)
        return out

    def open_orders(self) -> list[Order]:
        out = []
        for s in self.symbols:
            for o in self.client.fetch_open_orders(s):
                out.append(self._convert(o))
        return out

    def submit(self, order: Order) -> Order:
        market = self.client.market(order.symbol)
        amount = float(self.client.amount_to_precision(order.symbol, order.qty))
        limits = market.get("limits", {})
        min_amt = (limits.get("amount") or {}).get("min")
        if amount <= 0 or (min_amt and amount < min_amt):
            order.status = OrderStatus.REJECTED
            order.reason = f"amount {order.qty} below exchange minimum {min_amt}"
            return order
        ref = order.limit_price or order.stop_price or self._prices.get(order.symbol)
        min_cost = (limits.get("cost") or {}).get("min")
        if min_cost and ref and amount * ref < min_cost:
            order.status = OrderStatus.REJECTED
            order.reason = f"notional {amount * ref:.2f} below exchange minimum {min_cost}"
            return order

        params = {"clientOrderId": order.client_id}
        side = order.side.value
        price = None
        otype = "market"
        if order.type == OrderType.LIMIT:
            otype = "limit"
            price = float(self.client.price_to_precision(order.symbol, order.limit_price))
        elif order.type == OrderType.STOP:
            order.take_profit = None  # spot has no portable OCO; the context watches it
            params["triggerPrice"] = float(self.client.price_to_precision(order.symbol, order.stop_price))

        def attempt():
            try:
                return self.client.create_order(order.symbol, otype, side, amount, price, params)
            except Exception as e:
                if is_network_error(e):
                    existing = self._by_client_id(order.symbol, order.client_id)
                    if existing is not None:
                        return existing
                raise

        resp = retry(attempt, is_network_error)
        order.id = str(resp["id"])
        order.qty = amount
        self._order_symbol[order.id] = order.symbol
        self._tracker.track(Order(order.symbol, order.side, amount, id=order.id))
        log.info("ccxt %s order %s %s %s -> %s", self.exchange, side, amount, order.symbol, order.id)
        return order

    def _by_client_id(self, symbol: str, client_id: str) -> dict | None:
        lookups = [self.client.fetch_open_orders]
        if self.client.has.get("fetchClosedOrders"):
            lookups.append(lambda s: self.client.fetch_closed_orders(s, limit=50))
        for fetch in lookups:
            try:
                for o in fetch(symbol):
                    if o.get("clientOrderId") == client_id:
                        return o
            except Exception:
                pass
        return None

    def cancel(self, order_id: str) -> None:
        sym = self._order_symbol.get(order_id)
        self.client.cancel_order(order_id, sym)

    def sync(self) -> list[Fill]:
        fills = []
        for oid in self._tracker.tracked():
            sym = self._order_symbol.get(oid)
            try:
                raw = self.client.fetch_order(oid, sym)
            except Exception as e:
                log.warning("could not refresh order %s: %s", oid, e)
                continue
            o = self._convert(raw)
            fee = raw.get("fee") or {}
            commission = float(fee.get("cost") or 0) if fee.get("currency") == self.quote else 0.0
            f = self._tracker.diff(o, utcnow(), commission)
            if f:
                self._update_avg(f)
                fills.append(f)
            if not o.status.is_open:
                self._tracker.forget(oid)
                self._order_symbol.pop(oid, None)
        return fills

    def to_state(self) -> dict:
        return {"avg": dict(self._avg)}

    def load_state(self, state: dict) -> None:
        self._avg.update({k: float(v) for k, v in state.get("avg", {}).items()})

    def _update_avg(self, f: Fill) -> None:
        if f.side != Side.BUY:
            return
        bal_qty = 0.0
        try:
            bal_qty = float(self.client.fetch_balance().get("total", {}).get(self._base(f.symbol)) or 0)
        except Exception:
            pass
        prev_qty = max(bal_qty - f.qty, 0.0)
        prev_avg = self._avg.get(f.symbol, f.price)
        total = prev_qty + f.qty
        self._avg[f.symbol] = (prev_avg * prev_qty + f.price * f.qty) / total if total else f.price
