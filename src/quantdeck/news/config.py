from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class SourceConfig:
    name: str
    type: str                        # rss | alpaca | sec | reddit | cryptopanic | x | jsonl | module:Class
    every: int = 5                   # minutes between fetches
    credibility: Optional[float] = None  # 0..1, default depends on kind
    kind: Optional[str] = None       # official | newswire | media | social | filing
    market: Optional[str] = None     # stock | crypto: limit a per-symbol template to one market
    enabled: bool = True
    options: dict = field(default_factory=dict)  # everything else goes to the source


@dataclass
class LLMConfig:
    provider: str = "auto"           # auto | anthropic | openai | none
    model: Optional[str] = None
    base_url: Optional[str] = None   # openai-compatible endpoint (DeepSeek, Qwen, local vLLM ...)
    batch_size: int = 15
    workers: int = 2
    timeout: float = 60


@dataclass
class WeightConfig:
    half_life_hours: dict = field(default_factory=lambda: {"intraday": 4, "days": 24, "weeks": 96})
    max_age_hours: float = 72        # ignore anything older when building verdicts
    macro_beta: float = 0.5          # how much market-wide news counts toward each symbol
    prior: float = 0.6               # pseudo-weight of "no news"; pulls thin verdicts toward neutral
    thresholds: dict = field(default_factory=lambda: {"strong": 0.45, "weak": 0.15})


@dataclass
class AlertConfig:
    enabled: bool = True
    labels: list = field(default_factory=lambda: ["strong_bearish", "strong_bullish"])
    min_confidence: float = 0.35
    cooldown: int = 3600
    lang: str = "en"                 # en | zh


@dataclass
class NewsConfig:
    db: str = "runs/news/news.db"
    interval: int = 60               # seconds between pipeline cycles in `news watch`
    stock: list = field(default_factory=list)     # watchlist entries: "AAPL" or {"AAPL": ["Apple"]}
    crypto: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    llm: LLMConfig = field(default_factory=LLMConfig)
    weights: WeightConfig = field(default_factory=WeightConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    fetch_workers: int = 8

    @property
    def db_path(self) -> Path:
        return Path(self.db)


_SECTIONS = {"llm": LLMConfig, "weights": WeightConfig, "alerts": AlertConfig}
_SOURCE_KEYS = {f.name for f in fields(SourceConfig)} - {"options"}


def _build(cls, raw: dict):
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"unknown keys for {cls.__name__}: {sorted(unknown)}")
    return cls(**raw)


def _source(raw: dict) -> SourceConfig:
    if "name" not in raw or "type" not in raw:
        raise ValueError(f"news source needs name and type: {raw}")
    base = {k: v for k, v in raw.items() if k in _SOURCE_KEYS}
    opts = {k: v for k, v in raw.items() if k not in _SOURCE_KEYS}
    opts.update(raw.get("options") or {})
    opts.pop("options", None)
    return SourceConfig(**base, options=opts)


def news_config_from_dict(raw: dict[str, Any]) -> NewsConfig:
    raw = dict(raw or {})
    raw = raw.get("news", raw)
    raw = dict(raw)
    kwargs = {k: _build(cls, raw.pop(k) or {}) for k, cls in _SECTIONS.items() if k in raw}
    sources = [_source(s) for s in raw.pop("sources", []) or []]
    names = [s.name for s in sources]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"duplicate news source names: {sorted(dupes)}")
    cfg = _build(NewsConfig, {**raw, **kwargs, "sources": sources})
    if not cfg.stock and not cfg.crypto:
        raise ValueError("news config needs a watchlist: `stock:` and/or `crypto:`")
    return cfg


def load_news_config(path: str | Path) -> NewsConfig:
    with open(path, encoding="utf-8") as f:
        return news_config_from_dict(yaml.safe_load(f) or {})
