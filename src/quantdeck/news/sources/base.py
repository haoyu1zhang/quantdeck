from __future__ import annotations

import html
import re
import urllib.parse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from ..config import SourceConfig
from ..models import NewsItem
from ..symbols import Watchlist

DEFAULT_CREDIBILITY = {
    "official": 1.0,    # central banks, regulators, statistics offices, exchanges' own notices
    "filing": 0.9,      # SEC filings
    "newswire": 0.85,   # Reuters, Bloomberg, Benzinga wire, Dow Jones
    "media": 0.65,      # general financial and crypto press
    "social": 0.3,      # Reddit, X, Telegram (adjusted per author where possible)
}


class Source:
    """Base for a news source. Subclasses implement fetch().

    `state` is a small dict that survives between runs (stored in the news
    database), for cursors like since_id or the newest timestamp seen.
    """

    type = ""
    default_kind = "media"

    def __init__(self, cfg: SourceConfig, watchlist: Watchlist) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.watchlist = watchlist
        self.kind = cfg.kind or self.default_kind
        self.opts = cfg.options

    @property
    def credibility(self) -> float:
        if self.cfg.credibility is not None:
            return float(self.cfg.credibility)
        return DEFAULT_CREDIBILITY.get(self.kind, 0.5)

    def fetch(self, state: dict) -> list[NewsItem]:
        raise NotImplementedError

    def item(self, **kw) -> NewsItem:
        kw.setdefault("kind", self.kind)
        return NewsItem(source=self.name, **kw)

    def targets(self) -> list[str]:
        """Watchlist symbols this source covers when its URL is a per-symbol template."""
        return self.watchlist.symbols(self.cfg.market)

    def query_for(self, symbol: str) -> str:
        a = self.watchlist.assets[symbol]
        if a.market == "crypto":
            name = a.aliases[0] if a.aliases else symbol
            return f"{name} crypto"
        name = next((x for x in a.aliases if " " in x or len(x) > 4), None)
        return f"{symbol} {name} stock" if name else f"{symbol} stock"


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def strip_html(s: str | None, limit: int = 2000) -> str:
    if not s:
        return ""
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    return _WS.sub(" ", s).strip()[:limit]


def parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def from_epoch(x) -> datetime:
    return datetime.fromtimestamp(float(x), tz=timezone.utc)


def expand(url: str, **values) -> str:
    return url.format(**{k: urllib.parse.quote(str(v)) for k, v in values.items()})
