"""Turn scored items into a weight each, and weights into a verdict per asset.

weight = credibility x impact x confidence x recency x engagement x corroboration x relevance

  credibility    source kind default, per-source override, per-author for X
  impact         from the scorer, 0..1
  confidence     from the scorer, 0..1
  recency        0.5 ** (age / half-life); the half-life depends on the item's horizon
  engagement     1 .. 1.6, log of likes / reposts / replies / votes (social items only)
  corroboration  1 .. 1.75, more distinct sources carrying the same story
  relevance      1 for items about the asset, macro_beta for market-wide items

Only the strongest copy of each story (cluster) counts, so a headline
syndicated to twenty sites is one piece of evidence with a corroboration
bonus, not twenty.

score = sum(weight x direction) / (sum(weight) + prior)

The prior acts like a pseudo-observation of "nothing happened": with little
news the score stays near zero instead of swinging on a single headline.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime

from .config import WeightConfig
from .models import MARKET_CRYPTO, MARKET_STOCK, Driver, Verdict, utcnow
from .symbols import Watchlist


def engagement_factor(e: dict) -> float:
    if not e:
        return 1.0
    raw = (e.get("likes", 0) + 2 * e.get("reposts", 0) + e.get("replies", 0) + max(e.get("score", 0), 0)
           + 3 * e.get("important", 0) - e.get("dislikes", 0))
    return 1.0 + min(0.6, 0.15 * math.log10(1 + max(raw, 0)))


def corroboration_factor(n_sources: int) -> float:
    return 1.0 + min(0.75, 0.25 * math.log2(max(n_sources, 1)))


def recency_factor(published: datetime, horizon: str, cfg: WeightConfig, now: datetime) -> float:
    age_h = max((now - published).total_seconds() / 3600, 0.0)
    hl = float(cfg.half_life_hours.get(horizon, cfg.half_life_hours.get("days", 24)))
    return 0.5 ** (age_h / max(hl, 0.1))


def item_weight(row: dict, cfg: WeightConfig, now: datetime) -> float:
    published = datetime.fromisoformat(row["published_at"])
    w = (float(row["credibility"]) * float(row["impact"]) * float(row["confidence"])
         * recency_factor(published, row.get("horizon") or "days", cfg, now)
         * corroboration_factor(int(row.get("corroboration") or 1)))
    if row.get("kind") == "social":
        w *= engagement_factor(row.get("engagement") or {})
    return w


def label_for(score: float, cfg: WeightConfig) -> str:
    strong, weak = float(cfg.thresholds.get("strong", 0.45)), float(cfg.thresholds.get("weak", 0.15))
    if score >= strong:
        return "strong_bullish"
    if score >= weak:
        return "bullish"
    if score <= -strong:
        return "strong_bearish"
    if score <= -weak:
        return "bearish"
    return "neutral"


def targets_of(row: dict, watchlist: Watchlist) -> dict[str, float]:
    """Which verdicts an item feeds, and with what relevance."""
    out: dict[str, float] = {}
    for s in set(row.get("symbols") or []) | set(row.get("score_symbols") or []):
        if s in watchlist:
            out[s] = 1.0
    scope = row.get("scope") or "symbol"
    if scope in ("market", "macro"):
        for m in row.get("markets") or []:
            out[MARKET_STOCK if m == "stock" else MARKET_CRYPTO] = 1.0
    return out


def build_verdicts(rows: list[dict], watchlist: Watchlist, cfg: WeightConfig, now: datetime | None = None,
                   top: int = 5) -> list[Verdict]:
    now = now or utcnow()
    # one representative per (cluster, target): the copy with the largest weight
    best: dict[tuple[str, str], tuple[float, float, dict]] = {}
    for row in rows:
        if abs(float(row["direction"])) < 1e-9 and float(row["impact"]) <= 0.2:
            continue
        w = item_weight(row, cfg, now)
        if w <= 1e-6:
            continue
        for target, rel in targets_of(row, watchlist).items():
            key = (row["cluster_id"], target)
            ww = w * rel
            if key not in best or ww > best[key][0]:
                best[key] = (ww, float(row["direction"]), row)

    per_target: dict[str, list] = defaultdict(list)
    for (_, target), (w, d, row) in best.items():
        per_target[target].append((w, d, row))

    # market-wide evidence also counts toward each asset in that market, scaled down
    market_lists = {"stock": per_target.get(MARKET_STOCK, []), "crypto": per_target.get(MARKET_CRYPTO, [])}

    verdicts = []
    targets = watchlist.symbols() + [t for t in (MARKET_STOCK, MARKET_CRYPTO)
                                     if watchlist.symbols(t.split(":")[1])]
    for target in targets:
        own = list(per_target.get(target, []))
        if not target.startswith("MARKET:"):
            market = watchlist.market_of(target)
            seen = {r["cluster_id"] for _, _, r in own}
            own += [(w * cfg.macro_beta, d, r) for w, d, r in market_lists.get(market, [])
                    if r["cluster_id"] not in seen]
        verdicts.append(_verdict(target, own, cfg, now, top))
    return verdicts


def _verdict(target: str, ev: list, cfg: WeightConfig, now: datetime, top: int) -> Verdict:
    total = sum(w for w, _, _ in ev)
    net = sum(w * d for w, d, _ in ev)
    gross = sum(w * abs(d) for w, d, _ in ev)
    bull = sum(w * d for w, d, _ in ev if d > 0)
    bear = -sum(w * d for w, d, _ in ev if d < 0)
    score = net / (total + cfg.prior) if ev else 0.0
    agreement = abs(net) / gross if gross > 0 else 0.0
    coverage = 1 - math.exp(-total / 1.5)
    confidence = agreement * coverage
    ev.sort(key=lambda x: -abs(x[0] * x[1]))
    drivers = [Driver(item_id=r["id"], title=r["title"], source=r["source"], url=r["url"],
                      published_at=r["published_at"], direction=round(d, 3), weight=round(w, 4),
                      contribution=round(w * d, 4), reason=r.get("reason") or "")
               for w, d, r in ev[:top]]
    return Verdict(symbol=target, score=max(-1.0, min(1.0, score)), label=label_for(score, cfg),
                   confidence=confidence, items=len(ev), weight=total, bullish_weight=bull,
                   bearish_weight=bear, ts=now, drivers=drivers)
