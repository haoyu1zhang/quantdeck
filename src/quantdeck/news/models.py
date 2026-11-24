from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

MARKET_STOCK = "MARKET:stock"
MARKET_CRYPTO = "MARKET:crypto"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def item_id(source: str, key: str) -> str:
    return hashlib.sha1(f"{source}\x00{key}".encode()).hexdigest()[:20]


@dataclass
class NewsItem:
    """One headline, post or filing, as it came from a source."""

    source: str                      # name from the config, e.g. "coindesk"
    kind: str                        # official | newswire | media | social | filing
    title: str
    published_at: datetime
    url: str = ""
    body: str = ""
    author: str = ""
    native_id: str = ""              # id on the source, when it has one
    symbols: list = field(default_factory=list)   # canonical assets the source tagged
    engagement: dict = field(default_factory=dict)  # likes, reposts, replies, score, followers ...
    credibility: Optional[float] = None  # overrides the source default (e.g. per X account)
    lang: str = ""
    id: str = ""
    cluster_id: str = ""

    def __post_init__(self) -> None:
        if self.published_at.tzinfo is None:
            self.published_at = self.published_at.replace(tzinfo=timezone.utc)
        if not self.id:
            self.id = item_id(self.source, self.native_id or self.url or self.title)

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.body}".strip()


@dataclass
class Score:
    """What a scorer thinks one item means."""

    item_id: str
    direction: float                 # -1 bearish .. +1 bullish
    impact: float                    # 0..1, how much it could move price
    confidence: float                # 0..1
    scorer: str = "rules"            # rules | llm:<model> | hybrid:<model>
    horizon: str = "days"            # intraday | days | weeks
    scope: str = "symbol"            # symbol | market | macro
    markets: list = field(default_factory=list)   # stock / crypto, for market and macro scope
    symbols: list = field(default_factory=list)   # assets the scorer says it's about
    reason: str = ""


@dataclass
class Driver:
    item_id: str
    title: str
    source: str
    url: str
    published_at: str
    direction: float
    weight: float
    contribution: float
    reason: str


@dataclass
class Verdict:
    symbol: str                      # canonical asset (AAPL, BTC) or MARKET:stock / MARKET:crypto
    score: float                     # -1..1, weighted and shrunk toward 0 when evidence is thin
    label: str                       # strong_bearish | bearish | neutral | bullish | strong_bullish
    confidence: float
    items: int
    weight: float
    bullish_weight: float
    bearish_weight: float
    ts: datetime = field(default_factory=utcnow)
    drivers: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "score": round(self.score, 4),
            "label": self.label,
            "confidence": round(self.confidence, 4),
            "items": self.items,
            "weight": round(self.weight, 4),
            "bullish_weight": round(self.bullish_weight, 4),
            "bearish_weight": round(self.bearish_weight, 4),
            "ts": self.ts.isoformat(),
            "drivers": [d.__dict__ for d in self.drivers],
        }


LABELS = ("strong_bearish", "bearish", "neutral", "bullish", "strong_bullish")

LABELS_ZH = {
    "strong_bearish": "强利空",
    "bearish": "利空",
    "neutral": "中性",
    "bullish": "利多",
    "strong_bullish": "强利多",
}
