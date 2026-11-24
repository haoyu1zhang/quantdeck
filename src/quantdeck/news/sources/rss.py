"""RSS 2.0 and Atom feeds.

`url` may be a template with {symbol} or {query}; the feed is then fetched
once per watchlist symbol (restricted to `market:` if set), and every item
is tagged with that symbol.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

from ..http import get_text
from ..models import utcnow
from .base import Source, parse_time, strip_html, expand

log = logging.getLogger(__name__)

MAX_BYTES = 5_000_000


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el, name: str):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _text(el, *names: str) -> str:
    for n in names:
        c = _child(el, n)
        if c is not None and (c.text or "").strip():
            return c.text.strip()
    return ""


def parse_feed(xml: str) -> list[dict]:
    """Entries as plain dicts: title, url, body, published, id, author."""
    if len(xml) > MAX_BYTES:
        xml = xml[:MAX_BYTES]
    root = ET.fromstring(xml)
    out = []
    for el in root.iter():
        tag = _local(el.tag)
        if tag == "item":                      # RSS 2.0 / RDF
            link = _text(el, "link")
            author = _text(el, "source", "creator", "author")
            out.append({
                "title": strip_html(_text(el, "title"), 500),
                "url": link,
                "body": strip_html(_text(el, "description", "encoded")),
                "published": _text(el, "pubDate", "date", "published", "updated"),
                "id": _text(el, "guid") or link,
                "author": author,
            })
        elif tag == "entry":                   # Atom
            link = ""
            for c in el:
                if _local(c.tag) == "link" and c.get("rel", "alternate") == "alternate":
                    link = c.get("href", "")
                    break
            author_el = _child(el, "author")
            out.append({
                "title": strip_html(_text(el, "title"), 500),
                "url": link,
                "body": strip_html(_text(el, "summary", "content")),
                "published": _text(el, "published", "updated"),
                "id": _text(el, "id") or link,
                "author": _text(author_el, "name") if author_el is not None else "",
            })
    return out


class RssSource(Source):
    type = "rss"

    def fetch(self, state: dict) -> list:
        url = self.opts.get("url")
        if not url:
            raise ValueError(f"{self.name}: rss source needs url")
        limit = int(self.opts.get("limit", 50))
        if "{symbol}" in url or "{query}" in url:
            jobs = [(expand(url, symbol=s, query=self.query_for(s)), [s]) for s in self.targets()]
        else:
            jobs = [(url, [])]

        items, errors = [], []
        for u, tags in jobs:
            try:
                entries = parse_feed(get_text(u, timeout=float(self.opts.get("timeout", 20))))
            except Exception as e:
                errors.append(e)
                log.debug("%s: %s failed: %s", self.name, u, e)
                continue
            for e in entries[:limit]:
                if not e["title"]:
                    continue
                items.append(self.item(
                    title=e["title"], url=e["url"], body=e["body"], author=e["author"],
                    published_at=parse_time(e["published"]) or utcnow(),
                    native_id=e["id"], symbols=list(tags),
                ))
        if errors and not items:
            raise errors[0]
        return items
