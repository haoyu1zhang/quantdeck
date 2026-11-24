from __future__ import annotations

import importlib
import json
import logging
import os
import sys
from pathlib import Path

from ..config import SourceConfig
from ..models import utcnow
from ..symbols import Watchlist
from .base import Source, parse_time
from .feeds import AlpacaNews, CryptoPanic, Reddit, SecFilings
from .rss import RssSource
from .x import XSearch

log = logging.getLogger(__name__)


class JsonlInbox(Source):
    """Reads *.jsonl files dropped into a folder, one item per line, then moves
    them to processed/. The simplest way to plug in a scraper of your own.

    Each line: {"title": ..., "published_at": ISO time, "url": ..., "body": ...,
    "author": ..., "symbols": [...], "engagement": {...}, "credibility": 0..1}
    """

    type = "jsonl"
    default_kind = "social"

    def fetch(self, state):
        folder = Path(self.opts.get("path", "news_inbox"))
        if not folder.exists():
            return []
        done = folder / "processed"
        items = []
        for f in sorted(folder.glob("*.jsonl")):
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    items.append(item_from_dict(json.loads(line), self))
                except (ValueError, KeyError, TypeError) as e:
                    log.warning("%s: %s line %d skipped: %s", self.name, f.name, n, e)
            done.mkdir(exist_ok=True)
            f.replace(done / f.name)
        return items


def item_from_dict(d: dict, src: Source):
    if not str(d.get("title", "")).strip():
        raise ValueError("title is required")
    ts = parse_time(str(d["published_at"])) if d.get("published_at") else utcnow()
    cred = d.get("credibility")
    if cred is not None:
        cred = max(0.0, min(float(cred), 1.0))
    return src.item(
        title=str(d["title"])[:500], body=str(d.get("body", ""))[:4000], url=str(d.get("url", "")),
        author=str(d.get("author", "")), published_at=ts or utcnow(), native_id=str(d.get("id", "")),
        symbols=src.watchlist.normalize(d.get("symbols") or []),
        engagement={k: v for k, v in (d.get("engagement") or {}).items() if isinstance(v, (int, float))},
        credibility=cred, kind=d.get("kind") or src.kind, lang=str(d.get("lang", "")),
    )


REGISTRY: dict[str, type[Source]] = {
    cls.type: cls for cls in (RssSource, AlpacaNews, SecFilings, Reddit, CryptoPanic, XSearch, JsonlInbox)
}


def make_source(cfg: SourceConfig, watchlist: Watchlist) -> Source:
    if ":" in cfg.type:
        mod_name, cls_name = cfg.type.split(":", 1)
        if os.getcwd() not in sys.path:
            sys.path.insert(0, os.getcwd())
        cls = getattr(importlib.import_module(mod_name), cls_name)
    else:
        try:
            cls = REGISTRY[cfg.type]
        except KeyError:
            raise ValueError(f"unknown news source type {cfg.type!r}; built-ins: {', '.join(REGISTRY)}") from None
    return cls(cfg, watchlist)


__all__ = ["Source", "REGISTRY", "make_source", "item_from_dict", "JsonlInbox"]
