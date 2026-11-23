"""A custom strategy. Point a config at it with

    strategy:
      name: examples.my_strategy:MomentumRank

and run quantdeck from the repo root so `examples` is importable.
"""

from quantdeck.strategies import Strategy


class MomentumRank(Strategy):
    """Hold the top N symbols by trailing return, rebalanced every `every` bars."""

    params = {"lookback": 60, "top": 2, "every": 5}

    def warmup(self):
        return self.p.lookback + 1

    def on_start(self, ctx):
        ctx.state["bar"] = 0

    def on_bar(self, ctx, bar):
        # on_bar fires once per symbol; act only on the last one of each timestamp
        if bar.symbol != ctx.symbols[-1]:
            return
        ctx.state["bar"] += 1
        if ctx.state["bar"] % self.p.every:
            return

        scores = {}
        for sym in ctx.symbols:
            closes = ctx.closes(sym, self.p.lookback + 1)
            if len(closes) > self.p.lookback:
                scores[sym] = closes[-1] / closes[0] - 1
        if not scores:
            return

        winners = sorted(scores, key=scores.get, reverse=True)[: self.p.top]
        weight = 0.98 / self.p.top
        # sell first so the buys have cash
        for sym in ctx.symbols:
            if sym not in winners:
                ctx.close_position(sym)
        for sym in winners:
            if scores[sym] > 0:
                ctx.order_target_percent(sym, weight)
            else:
                ctx.close_position(sym)
