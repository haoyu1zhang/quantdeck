import numpy as np

from ..indicators import atr
from .base import Strategy


class DonchianBreakout(Strategy):
    """Turtle-style channel breakout.

    Enters when the close breaks the highest high of the last `entry` bars and
    exits on a break of the lowest low of the last `exit` bars. Position size
    targets `risk` of equity per 2 ATR move, capped by allocation.
    """

    name = "donchian"
    params = {"entry": 20, "exit": 10, "atr": 20, "risk": 0.01, "allocation": None}

    def warmup(self) -> int:
        return max(self.p.entry, self.p.exit, self.p.atr) + 1

    def on_bar(self, ctx, bar):
        sym = bar.symbol
        if ctx.bars(sym) < self.warmup():
            return
        n = max(self.p.entry, self.p.atr) + 2
        highs, lows, closes = ctx.highs(sym, n), ctx.lows(sym, n), ctx.closes(sym, n)
        # channels from the bars *before* this one
        upper = highs[-self.p.entry - 1:-1].max()
        lower = lows[-self.p.exit - 1:-1].min()
        holding = ctx.position(sym) + ctx.pending_qty(sym) > 0

        if not holding and bar.close > upper:
            a = atr(highs, lows, closes, self.p.atr)[-1]
            if np.isnan(a) or a <= 0:
                return
            cap = (self.p.allocation or 0.98 / len(ctx.symbols)) * ctx.equity / bar.close
            qty = min(ctx.equity * self.p.risk / (2 * a), cap)
            ctx.buy(sym, qty)
        elif holding and bar.close < lower:
            ctx.close_position(sym)
