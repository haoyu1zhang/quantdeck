from __future__ import annotations

import importlib
import os
import sys

from .base import Strategy
from .donchian import DonchianBreakout
from .rsi_reversion import RsiReversion
from .sma_cross import SmaCross

REGISTRY: dict[str, type[Strategy]] = {
    cls.name: cls for cls in (SmaCross, RsiReversion, DonchianBreakout)
}


def load_strategy(name: str, params: dict | None = None) -> Strategy:
    """Look up a built-in strategy by name, or import one as 'package.module:ClassName'."""
    if ":" in name:
        mod_name, cls_name = name.split(":", 1)
        # the console script doesn't put the working directory on sys.path
        if os.getcwd() not in sys.path:
            sys.path.insert(0, os.getcwd())
        cls = getattr(importlib.import_module(mod_name), cls_name)
    else:
        try:
            cls = REGISTRY[name]
        except KeyError:
            raise ValueError(f"unknown strategy {name!r}; built-ins: {', '.join(REGISTRY)}") from None
    return cls(**(params or {}))


__all__ = ["Strategy", "REGISTRY", "load_strategy", "SmaCross", "RsiReversion", "DonchianBreakout"]
