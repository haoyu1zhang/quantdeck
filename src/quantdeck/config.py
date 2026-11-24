from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

import yaml

MODES = ("backtest", "paper", "live")
MARKETS = ("stock", "crypto")


@dataclass
class BrokerConfig:
    type: str = "sim"            # sim | alpaca | ccxt
    exchange: Optional[str] = None
    sandbox: bool = True         # exchange testnet
    demo: bool = False           # exchange demo trading (e.g. Bybit), uses live keys


@dataclass
class DataConfig:
    source: str = "csv"          # csv | alpaca | ccxt
    timeframe: str = "1d"
    start: Optional[str] = None
    end: Optional[str] = None
    exchange: Optional[str] = None
    path: Optional[str] = None   # csv file or directory
    warmup: int = 200            # bars loaded before live trading starts


@dataclass
class FeeConfig:
    rate: float = 0.0            # fraction of notional
    per_share: float = 0.0
    minimum: float = 0.0
    slippage: float = 0.0005     # fraction of price, applied against us
    max_volume_pct: Optional[float] = None  # backtest: cap each fill at this share of bar volume


@dataclass
class RiskConfig:
    max_order_value: Optional[float] = None
    max_position_pct: float = 1.0
    max_gross_exposure: float = 1.0
    max_daily_loss_pct: Optional[float] = None
    max_orders_per_day: Optional[int] = None
    allow_short: bool = False
    # exits placed at the broker as soon as a position opens, so they still work if we crash
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    trailing_stop: bool = False


@dataclass
class NotifyConfig:
    # channels: None = on if its environment variables are set, false = off, true = required.
    # webhook may also name the format: slack, discord, wecom, feishu, dingtalk
    telegram: Optional[bool] = None
    webhook: Any = None
    email: Optional[bool] = None
    events: list = field(default_factory=lambda: ["start", "stop", "fill", "stop_hit", "halt", "error"])
    cooldown: int = 300


@dataclass
class NewsLinkConfig:
    # verdicts written by `quantdeck news watch`; paper and live runs only
    db: Optional[str] = None
    max_age_minutes: float = 60        # older verdicts count as no verdict
    block_longs_below: Optional[float] = None   # e.g. -0.45: no new longs on strong bearish news
    block_shorts_above: Optional[float] = None  # e.g. 0.45: no new shorts on strong bullish news


@dataclass
class StrategyConfig:
    name: str = "sma_cross"
    params: dict = field(default_factory=dict)


@dataclass
class Config:
    name: str = "default"
    mode: str = "backtest"
    market: str = "crypto"
    symbols: list = field(default_factory=list)
    cash: float = 10_000.0
    fractional: bool = True
    poll_seconds: int = 30
    runs_dir: str = "runs"
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    data: DataConfig = field(default_factory=DataConfig)
    fees: FeeConfig = field(default_factory=FeeConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    news: NewsLinkConfig = field(default_factory=NewsLinkConfig)

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if self.market not in MARKETS:
            raise ValueError(f"market must be one of {MARKETS}")
        if not self.symbols:
            raise ValueError("no symbols configured")
        if self.mode == "live" and self.broker.type == "sim":
            raise ValueError("live mode needs a real broker (alpaca or ccxt)")
        if self.mode == "live" and self.broker.type == "ccxt" and (self.broker.sandbox or self.broker.demo):
            raise ValueError("live mode with sandbox/demo enabled; set both to false or use paper mode")
        if self.broker.type == "ccxt" and not self.broker.exchange:
            raise ValueError("broker.exchange is required for ccxt")

    @property
    def run_dir(self) -> Path:
        return Path(self.runs_dir) / self.name


_SECTIONS = {
    "broker": BrokerConfig,
    "data": DataConfig,
    "fees": FeeConfig,
    "risk": RiskConfig,
    "strategy": StrategyConfig,
    "notify": NotifyConfig,
    "news": NewsLinkConfig,
}


def _build(cls, raw: dict):
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"unknown keys for {cls.__name__}: {sorted(unknown)}")
    return cls(**raw)


def config_from_dict(raw: dict[str, Any]) -> Config:
    raw = dict(raw)
    kwargs = {}
    for key, cls in _SECTIONS.items():
        if key in raw:
            kwargs[key] = _build(cls, raw.pop(key) or {})
    cfg = _build(Config, {**raw, **kwargs})
    if cfg.data.exchange is None and cfg.broker.exchange:
        cfg.data.exchange = cfg.broker.exchange
    cfg.validate()
    return cfg


def load_config(path: str | Path) -> Config:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return config_from_dict(raw)


def load_dotenv(path: str | Path = ".env") -> None:
    """Tiny .env reader so we don't need python-dotenv. Existing env wins."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def secret(name: str, required: bool = True) -> Optional[str]:
    value = os.environ.get(name)
    if required and not value:
        raise RuntimeError(f"missing environment variable {name}")
    return value
