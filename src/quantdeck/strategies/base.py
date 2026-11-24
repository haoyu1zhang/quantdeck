from __future__ import annotations

from types import SimpleNamespace

from ..models import Bar, Fill


class Strategy:
    """Subclass this and implement on_bar.

    Declare tunable parameters in `params`; values from the config override them
    and are available as self.p.<name>.
    """

    name = ""
    params: dict = {}

    def __init__(self, **overrides) -> None:
        unknown = set(overrides) - set(self.params)
        if unknown:
            raise ValueError(f"{type(self).__name__} has no parameter(s) {sorted(unknown)}")
        merged = {**self.params, **overrides}
        self.p = SimpleNamespace(**merged)

    def warmup(self) -> int:
        """Bars needed before signals mean anything."""
        return 0

    def on_start(self, ctx) -> None:
        pass

    def on_bar(self, ctx, bar: Bar) -> None:
        raise NotImplementedError

    def on_fill(self, ctx, fill: Fill) -> None:
        pass

    def on_stop(self, ctx) -> None:
        pass
