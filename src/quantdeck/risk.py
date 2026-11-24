from __future__ import annotations

import logging
from datetime import date

from .config import RiskConfig
from .models import Account, Order, Position, Side

log = logging.getLogger(__name__)


class RiskManager:
    """Pre-trade checks. Sits between the strategy and the broker in every mode."""

    def __init__(self, cfg: RiskConfig | None = None) -> None:
        self.cfg = cfg or RiskConfig()
        self.halted = False
        self.halt_reason = ""
        self._day: date | None = None
        self._day_start_equity = 0.0
        self._orders_today = 0

    def new_day(self, day: date, equity: float) -> None:
        if day != self._day:
            self._day = day
            self._day_start_equity = equity
            self._orders_today = 0

    def update_equity(self, equity: float) -> None:
        limit = self.cfg.max_daily_loss_pct
        if limit is None or self.halted or self._day_start_equity <= 0:
            return
        loss = 1 - equity / self._day_start_equity
        if loss >= limit:
            self.halt(f"daily loss {loss:.2%} hit limit {limit:.2%}")

    def halt(self, reason: str) -> None:
        if not self.halted:
            log.warning("trading halted: %s", reason)
        self.halted = True
        self.halt_reason = reason

    def check(
        self,
        order: Order,
        price: float,
        account: Account,
        positions: dict[str, Position],
        prices: dict[str, float],
    ) -> str | None:
        """Return None if the order may go out, otherwise the reason it can't."""
        cfg = self.cfg
        pos = positions.get(order.symbol)
        cur_qty = pos.qty if pos else 0.0
        signed = order.qty if order.side == Side.BUY else -order.qty
        new_qty = cur_qty + signed
        reduces = abs(new_qty) < abs(cur_qty) and (new_qty == 0 or (new_qty > 0) == (cur_qty > 0))

        # always let exits through, even when halted
        if reduces:
            return None
        if self.halted:
            return f"halted: {self.halt_reason}"
        if price <= 0:
            return "no price available"
        if new_qty < 0 and not cfg.allow_short:
            return "shorting disabled"

        value = order.qty * price
        if cfg.max_order_value is not None and value > cfg.max_order_value:
            return f"order value {value:.2f} > max_order_value {cfg.max_order_value:.2f}"

        if cfg.max_orders_per_day is not None and self._orders_today >= cfg.max_orders_per_day:
            return "max_orders_per_day reached"

        equity = account.equity
        if equity <= 0:
            return "no equity"
        if abs(new_qty) * price > cfg.max_position_pct * equity * (1 + 1e-6):
            return f"position would exceed {cfg.max_position_pct:.0%} of equity"

        gross = 0.0
        for sym, p in positions.items():
            if sym != order.symbol:
                gross += abs(p.qty) * prices.get(sym, p.last_price or p.avg_price)
        gross += abs(new_qty) * price
        if gross > cfg.max_gross_exposure * equity * (1 + 1e-6):
            return f"gross exposure would exceed {cfg.max_gross_exposure:.0%} of equity"
        return None

    def record(self, order: Order) -> None:
        self._orders_today += 1
