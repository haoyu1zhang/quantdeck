from .base import Strategy


class SmaCross(Strategy):
    """Long while the fast SMA is above the slow one, flat otherwise.

    With vol_target set (e.g. 0.4 for 40% a year) the position is scaled so
    that calmer and wilder markets carry roughly the same risk.
    """

    name = "sma_cross"
    params = {"fast": 20, "slow": 50, "allocation": None, "vol_target": None}

    def __init__(self, **kw):
        super().__init__(**kw)
        if self.p.fast >= self.p.slow:
            raise ValueError("fast must be shorter than slow")

    def warmup(self) -> int:
        return self.p.slow

    def on_bar(self, ctx, bar):
        sym = bar.symbol
        if ctx.bars(sym) < self.p.slow:
            return
        closes = ctx.closes(sym, self.p.slow)
        fast = closes[-self.p.fast:].mean()
        slow = closes.mean()
        holding = ctx.position(sym) + ctx.pending_qty(sym) > 0

        if fast > slow and not holding:
            weight = self.p.allocation or 0.98 / len(ctx.symbols)
            if self.p.vol_target:
                weight = min(weight, ctx.vol_target_weight(sym, self.p.vol_target, cap=weight))
            if weight > 0:
                ctx.order_target_percent(sym, weight)
        elif fast < slow and holding:
            ctx.close_position(sym)

