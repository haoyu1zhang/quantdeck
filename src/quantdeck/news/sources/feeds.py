"""JSON API sources: Alpaca news (Benzinga), SEC EDGAR filings, Reddit, CryptoPanic."""

from __future__ import annotations

import base64
import logging
import os
import threading
import time
from datetime import timedelta

from ..http import get_json, get_text, request
from ..models import utcnow
from .base import Source, from_epoch, parse_time, strip_html
from .rss import parse_feed

log = logging.getLogger(__name__)


class AlpacaNews(Source):
    """Alpaca's news endpoint, which carries the Benzinga wire. Free with any Alpaca account."""

    type = "alpaca"
    default_kind = "newswire"
    URL = "https://data.alpaca.markets/v1beta1/news"

    def fetch(self, state):
        key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError("alpaca news needs ALPACA_API_KEY / ALPACA_SECRET_KEY")
        syms = []
        for s in self.targets():
            syms.append(f"{s}USD" if self.watchlist.market_of(s) == "crypto" else s)
        start = state.get("since") or (utcnow() - timedelta(hours=int(self.opts.get("lookback_hours", 24)))).isoformat()
        data = get_json(self.URL, params={
            "symbols": ",".join(syms), "limit": 50, "sort": "desc", "start": start,
            "include_content": "false",
        }, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
        items = []
        for n in data.get("news", []):
            ts = parse_time(n.get("created_at")) or utcnow()
            items.append(self.item(
                title=n.get("headline", ""), body=strip_html(n.get("summary")), url=n.get("url", ""),
                author=n.get("author") or n.get("source", ""), published_at=ts, native_id=str(n.get("id")),
                symbols=self.watchlist.normalize(n.get("symbols") or []),
            ))
        if items:
            state["since"] = max(i.published_at for i in items).isoformat()
        return items


# what the 8-K item numbers mean, so a scorer reading the title has something to go on
EIGHT_K_ITEMS = {
    "1.01": "entry into a material definitive agreement",
    "1.02": "termination of a material definitive agreement",
    "1.03": "bankruptcy or receivership",
    "1.05": "material cybersecurity incident",
    "2.01": "completion of acquisition or disposition of assets",
    "2.02": "results of operations and financial condition (earnings)",
    "2.03": "creation of a direct financial obligation",
    "2.04": "triggering events that accelerate a financial obligation",
    "2.05": "costs associated with exit or disposal activities (restructuring)",
    "2.06": "material impairments",
    "3.01": "notice of delisting or failure to satisfy a listing rule",
    "3.02": "unregistered sales of equity securities (dilution)",
    "4.01": "change in the company's certifying accountant",
    "4.02": "non-reliance on previously issued financial statements (restatement)",
    "5.02": "departure or appointment of directors or officers",
    "5.07": "submission of matters to a vote of security holders",
    "7.01": "regulation FD disclosure",
    "8.01": "other events",
}


class SecFilings(Source):
    """Recent 8-K filings for each stock in the watchlist, from EDGAR's Atom feeds.

    The SEC asks for a User-Agent with contact details: set SEC_USER_AGENT
    (e.g. "yourname you@example.com") or `user_agent:` on the source.
    """

    type = "sec"
    default_kind = "filing"
    URL = "https://www.sec.gov/cgi-bin/browse-edgar"
    _lock = threading.Lock()

    def targets(self):
        return self.watchlist.symbols("stock")

    def fetch(self, state):
        ua = self.opts.get("user_agent") or os.environ.get("SEC_USER_AGENT")
        if not ua:
            raise RuntimeError("sec source needs SEC_USER_AGENT (name and email) per SEC fair-access rules")
        forms = self.opts.get("forms", ["8-K"])
        items = []
        for sym in self.targets():
            for form in forms:
                with self._lock:  # SEC allows 10 requests a second; stay well under
                    time.sleep(0.15)
                    xml = get_text(self.URL, params={
                        "action": "getcompany", "CIK": sym, "type": form, "dateb": "", "owner": "include",
                        "count": 10, "output": "atom"}, headers={"User-Agent": ua})
                for e in parse_feed(xml):
                    body = e["body"]
                    meanings = [f"Item {k}: {v}" for k, v in EIGHT_K_ITEMS.items() if k in body]
                    items.append(self.item(
                        title=f"{sym} {e['title']}", url=e["url"],
                        body=" | ".join(filter(None, [body[:300], "; ".join(meanings)])), published_at=parse_time(e["published"]) or utcnow(),
                        native_id=e["id"], symbols=[sym], author="SEC EDGAR",
                    ))
        return items


class Reddit(Source):
    """New posts from subreddits. Uses app-only OAuth when REDDIT_CLIENT_ID /
    REDDIT_CLIENT_SECRET are set; Reddit throttles anonymous requests hard."""

    type = "reddit"
    default_kind = "social"

    def _token(self, state) -> str | None:
        cid, csecret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
        if not cid or not csecret:
            return None
        if state.get("token") and state.get("token_exp", 0) > time.time() + 60:
            return state["token"]
        auth = base64.b64encode(f"{cid}:{csecret}".encode()).decode()
        import json

        raw = request("https://www.reddit.com/api/v1/access_token", data=b"grant_type=client_credentials",
                      headers={"Authorization": f"Basic {auth}",
                               "Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        tok = json.loads(raw)
        state["token"], state["token_exp"] = tok["access_token"], time.time() + int(tok.get("expires_in", 3600))
        return state["token"]

    def fetch(self, state):
        subs = self.opts.get("subreddits") or []
        if not subs:
            raise ValueError(f"{self.name}: reddit source needs subreddits")
        listing = self.opts.get("listing", "new")
        token = self._token(state)
        base = "https://oauth.reddit.com" if token else "https://www.reddit.com"
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        min_score = int(self.opts.get("min_score", 0))
        items = []
        for sub in subs:
            data = get_json(f"{base}/r/{sub}/{listing}.json", params={"limit": 50, "raw_json": 1}, headers=headers)
            for child in data.get("data", {}).get("children", []):
                p = child.get("data", {})
                if p.get("stickied") or p.get("score", 0) < min_score:
                    continue
                items.append(self.item(
                    title=p.get("title", ""), body=(p.get("selftext") or "")[:2000],
                    url="https://www.reddit.com" + p.get("permalink", ""), author=f"u/{p.get('author')} r/{sub}",
                    published_at=from_epoch(p.get("created_utc", time.time())), native_id=p.get("name", ""),
                    engagement={"score": p.get("score", 0), "replies": p.get("num_comments", 0),
                                "upvote_ratio": p.get("upvote_ratio")},
                ))
        return items


class CryptoPanic(Source):
    """CryptoPanic aggregates crypto news and lets readers vote on it. Needs CRYPTOPANIC_TOKEN."""

    type = "cryptopanic"
    default_kind = "media"
    URL = "https://cryptopanic.com/api/developer/v2/posts/"

    def targets(self):
        return self.watchlist.symbols("crypto")

    def fetch(self, state):
        token = self.opts.get("token") or os.environ.get("CRYPTOPANIC_TOKEN")
        if not token:
            raise RuntimeError("cryptopanic source needs CRYPTOPANIC_TOKEN")
        data = get_json(self.opts.get("url", self.URL), params={
            "auth_token": token, "public": "true", "currencies": ",".join(self.targets()),
            "filter": self.opts.get("filter"), "kind": "news"})
        items = []
        for p in data.get("results", []):
            votes = p.get("votes") or {}
            src = p.get("source") or {}
            items.append(self.item(
                title=p.get("title", ""), body=strip_html(p.get("description")),
                url=p.get("original_url") or p.get("url", ""),
                author=src.get("title") or src.get("domain", ""),
                published_at=parse_time(p.get("published_at")) or utcnow(), native_id=str(p.get("id", "")),
                symbols=self.watchlist.normalize([c.get("code", "") for c in p.get("currencies") or []]),
                engagement={"likes": votes.get("liked", 0) + votes.get("positive", 0),
                            "dislikes": votes.get("disliked", 0) + votes.get("negative", 0),
                            "important": votes.get("important", 0)},
            ))
        return items
