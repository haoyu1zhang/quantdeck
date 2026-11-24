import numpy as np

from ..indicators import rsi
from .base import Strategy


class RsiReversion(Strategy):
    """Buy oversold dips, sell when RSI recovers.

    With trend > 0 it only buys while price is above that SMA, which avoids
    catching falling knives in a downtrend.
    """

    name = "rsi_reversion"
    params = {"period": 14, "entry": 30, "exit": 55, "trend": 200, "allocation": None}

    def warmup(self) -> int:
        return max(self.p.period + 1, self.p.trend)

    def on_bar(self, ctx, bar):
        sym = bar.symbol
        need = max(self.p.period * 5, self.p.trend)
        if ctx.bars(sym) < self.warmup():
            return
        closes = ctx.closes(sym, need)
        r = rsi(closes, self.p.period)[-1]
        if np.isnan(r):
            return
        holding = ctx.position(sym) + ctx.pending_qty(sym) > 0

        if not holding and r < self.p.entry:
            if self.p.trend and bar.close < closes[-self.p.trend:].mean():
                return
            weight = self.p.allocation or 0.98 / len(ctx.symbols)
            ctx.order_target_percent(sym, weight)
        elif holding and r > self.p.exit:
            ctx.close_position(sym)
