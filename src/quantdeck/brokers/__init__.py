from __future__ import annotations

from ..config import Config
from ..ratelimit import Throttled, TokenBucket
from .base import Broker
from .sim import SimBroker

# Alpaca allows 200 requests/minute on the trading API
ALPACA_RATE = 3.0


def make_broker(cfg: Config) -> Broker:
    b = cfg.broker
    if b.type == "sim":
        return SimBroker(cfg.cash, cfg.fees, fractional=cfg.fractional, allow_short=cfg.risk.allow_short)

    if b.type == "alpaca":
        from alpaca.trading.client import TradingClient

        from ..config import secret
        from .alpaca import AlpacaBroker

        paper = cfg.mode != "live"
        raw = TradingClient(secret("ALPACA_API_KEY"), secret("ALPACA_SECRET_KEY"), paper=paper)
        client = Throttled(raw, TokenBucket(ALPACA_RATE, capacity=10))
        return AlpacaBroker(cfg.symbols, cfg.market, paper=paper, client=client)

    if b.type == "ccxt":
        from .ccxt_broker import LOCAL_METHODS, CcxtBroker, build_client

        raw = build_client(b.exchange, sandbox=b.sandbox, demo=b.demo)
        rate = 1000.0 / max(getattr(raw, "rateLimit", 100), 1)
        client = Throttled(raw, TokenBucket(rate, capacity=max(rate, 2)), skip=LOCAL_METHODS)
        return CcxtBroker(b.exchange, cfg.symbols, client=client)

    raise ValueError(f"unknown broker type {b.type!r}")


__all__ = ["Broker", "SimBroker", "make_broker"]
