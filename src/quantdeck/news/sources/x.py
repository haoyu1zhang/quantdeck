"""X (Twitter) posts, two ways:

  x              the official API v2 recent search. Needs X_BEARER_TOKEN.
                 Pay per use, about $5 per 1,000 posts read.
  twitterapi_io  TwitterAPI.io, a third-party search API over public posts.
                 Needs TWITTERAPI_IO_KEY. Around $0.15 per 1,000 posts.

Both build the same searches from the watchlist's cashtags plus any
`accounts:` and `queries:` you add, and both take `daily_limit:`, a cap on
posts read per UTC day, so a busy cashtag can't run up the bill.

If you'd rather collect posts some other way, push them in through the
`jsonl` source or POST /api/news/ingest; they get scored the same way.

Per-author credibility starts from the source's base value and goes up with
follower count and verification. Accounts listed under `trusted:` get a
fixed value (e.g. official accounts of regulators, exchanges, companies).
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone

from ..http import get_json
from ..models import utcnow
from .base import Source, parse_time

MAX_QUERY = 512


def _chunks(terms: list[str], suffix: str, limit: int = MAX_QUERY) -> list[str]:
    out, cur = [], []
    for t in terms:
        trial = "(" + " OR ".join(cur + [t]) + ")" + suffix
        if cur and len(trial) > limit:
            out.append("(" + " OR ".join(cur) + ")" + suffix)
            cur = [t]
        else:
            cur.append(t)
    if cur:
        out.append("(" + " OR ".join(cur) + ")" + suffix)
    return out


class XSearch(Source):
    type = "x"
    default_kind = "social"
    URL = "https://api.x.com/2/tweets/search/recent"

    def queries(self) -> list[str]:
        lang = self.opts.get("lang", "en")
        suffix = " -is:retweet -is:reply" + (f" lang:{lang}" if lang else "")
        out = []
        if self.opts.get("cashtags", True):
            tags = [f"${s}" for s in self.watchlist.symbols()]
            out += _chunks(tags, suffix)
        accounts = self.opts.get("accounts") or []
        if accounts:
            out += _chunks([f"from:{a.lstrip('@')}" for a in accounts], " -is:retweet")
        out += list(self.opts.get("queries") or [])
        return out

    def author_credibility(self, user: dict) -> float:
        handle = (user.get("username") or "").lower()
        trusted = {k.lstrip("@").lower(): v for k, v in (self.opts.get("trusted") or {}).items()}
        if handle in trusted:
            return float(trusted[handle])
        followers = (user.get("public_metrics") or {}).get("followers_count", 0)
        c = self.credibility + 0.08 * math.log10(1 + followers / 1000)  # +0.08 per 10x past 1k
        if user.get("verified_type") in ("business", "government"):
            c += 0.2
        elif user.get("verified"):
            c += 0.05
        return max(0.05, min(c, float(self.opts.get("max_credibility", 0.75))))

    def budget(self, state) -> int | None:
        """Posts still allowed today, or None for no cap."""
        limit = self.opts.get("daily_limit")
        if limit is None:
            return None
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if state.get("day") != today:
            state["day"], state["read_today"] = today, 0
        return max(int(limit) - state.get("read_today", 0), 0)

    def spend(self, state, n: int) -> None:
        state["read_today"] = state.get("read_today", 0) + n

    def fetch(self, state):
        token = self.opts.get("bearer_token") or os.environ.get("X_BEARER_TOKEN")
        if not token:
            raise RuntimeError("x source needs X_BEARER_TOKEN (X API with read access)")
        since = state.setdefault("since_id", {})
        min_followers = int(self.opts.get("min_followers", 0))
        items = []
        left = self.budget(state)
        for q in self.queries():
            if left is not None and left < 10:
                break  # the API's minimum page is 10
            per = int(self.opts.get("max_results", 50))
            if left is not None:
                per = min(per, left)
            params = {
                "query": q, "max_results": max(10, min(per, 100)),
                "tweet.fields": "created_at,public_metrics,lang,author_id",
                "expansions": "author_id",
                "user.fields": "username,name,verified,verified_type,public_metrics",
                "since_id": since.get(q),
            }
            data = get_json(self.URL, params=params, headers={"Authorization": f"Bearer {token}"})
            users = {u["id"]: u for u in (data.get("includes") or {}).get("users", [])}
            got = len(data.get("data") or [])
            self.spend(state, got)
            if left is not None:
                left -= got
            newest = since.get(q)
            for t in data.get("data", []) or []:
                user = users.get(t.get("author_id"), {})
                if (user.get("public_metrics") or {}).get("followers_count", 0) < min_followers:
                    continue
                m = t.get("public_metrics") or {}
                handle = user.get("username", "")
                text = t.get("text", "")
                items.append(self.item(
                    title=text[:280], body=text[280:], url=f"https://x.com/{handle}/status/{t['id']}",
                    author=f"@{handle}", published_at=parse_time(t.get("created_at")) or utcnow(),
                    native_id=t["id"], lang=t.get("lang", ""),
                    engagement={"likes": m.get("like_count", 0), "reposts": m.get("retweet_count", 0)
                                + m.get("quote_count", 0), "replies": m.get("reply_count", 0),
                                "followers": (user.get("public_metrics") or {}).get("followers_count", 0)},
                    credibility=self.author_credibility(user),
                ))
                if newest is None or int(t["id"]) > int(newest):
                    newest = t["id"]
            if newest:
                since[q] = newest
        return items


class TwitterApiIo(XSearch):
    """X posts through TwitterAPI.io's advanced search (pay per post returned)."""

    type = "twitterapi_io"
    URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"

    @staticmethod
    def _user(a: dict) -> dict:
        # reshape to the fields author_credibility() reads
        return {"username": a.get("userName", ""), "verified": bool(a.get("isBlueVerified")),
                "verified_type": (a.get("verifiedType") or "").lower() or None,
                "public_metrics": {"followers_count": a.get("followers", 0) or 0}}

    def fetch(self, state):
        key = self.opts.get("api_key") or os.environ.get("TWITTERAPI_IO_KEY")
        if not key:
            raise RuntimeError("twitterapi_io source needs TWITTERAPI_IO_KEY")
        since = state.setdefault("since_id", {})
        min_followers = int(self.opts.get("min_followers", 0))
        max_pages = int(self.opts.get("max_pages", 2))
        items = []
        left = self.budget(state)
        for q in self.queries():
            query = q + (f" since_id:{since[q]}" if since.get(q) else "")
            newest, cursor = since.get(q), ""
            for _ in range(max_pages):
                if left is not None and left <= 0:
                    break
                data = get_json(self.URL, params={"query": query, "queryType": "Latest", "cursor": cursor},
                                headers={"X-API-Key": key})
                tweets = data.get("tweets") or []
                self.spend(state, len(tweets))
                if left is not None:
                    left -= len(tweets)
                for t in tweets:
                    if newest is None or int(t["id"]) > int(newest):
                        newest = t["id"]
                    if t.get("isRetweet") or t.get("isReply"):
                        continue
                    a = t.get("author") or {}
                    if (a.get("followers") or 0) < min_followers:
                        continue
                    handle = a.get("userName", "")
                    text = t.get("text", "")
                    items.append(self.item(
                        title=text[:280], body=text[280:],
                        url=t.get("url") or f"https://x.com/{handle}/status/{t['id']}",
                        author=f"@{handle}", published_at=parse_time(t.get("createdAt")) or utcnow(),
                        native_id=str(t["id"]), lang=t.get("lang") or "",
                        engagement={"likes": t.get("likeCount", 0) or 0,
                                    "reposts": (t.get("retweetCount", 0) or 0) + (t.get("quoteCount", 0) or 0),
                                    "replies": t.get("replyCount", 0) or 0, "followers": a.get("followers", 0) or 0},
                        credibility=self.author_credibility(self._user(a)),
                    ))
                cursor = data.get("next_cursor") or ""
                # first run: one page per query is enough; later runs page until caught up
                if not data.get("has_next_page") or not cursor or not since.get(q):
                    break
            if newest:
                since[q] = str(newest)
        return items
