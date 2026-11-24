"""X (Twitter) through the official API v2 recent-search endpoint.

Needs X_BEARER_TOKEN. Reading posts is a paid tier on X's API, so this
source is off unless the token is set. Searches are built from the
watchlist's cashtags plus any `accounts:` and `queries:` you add.

If you'd rather collect posts some other way, push them in through the
`jsonl` source or POST /api/news/ingest; they get scored the same way.

Per-author credibility starts from the source's base value and goes up with
follower count and verification. Accounts listed under `trusted:` get a
fixed value (e.g. official accounts of regulators, exchanges, companies).
"""

from __future__ import annotations

import math
import os

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

    def fetch(self, state):
        token = self.opts.get("bearer_token") or os.environ.get("X_BEARER_TOKEN")
        if not token:
            raise RuntimeError("x source needs X_BEARER_TOKEN (X API with read access)")
        since = state.setdefault("since_id", {})
        min_followers = int(self.opts.get("min_followers", 0))
        items = []
        for q in self.queries():
            params = {
                "query": q, "max_results": int(self.opts.get("max_results", 50)),
                "tweet.fields": "created_at,public_metrics,lang,author_id",
                "expansions": "author_id",
                "user.fields": "username,name,verified,verified_type,public_metrics",
                "since_id": since.get(q),
            }
            data = get_json(self.URL, params=params, headers={"Authorization": f"Bearer {token}"})
            users = {u["id"]: u for u in (data.get("includes") or {}).get("users", [])}
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
