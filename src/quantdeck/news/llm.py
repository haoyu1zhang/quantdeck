"""LLM scorer. Reads a batch of headlines and returns, for each one, which way
it points, how much it matters and how sure it is.

Providers:
  anthropic  ANTHROPIC_API_KEY
  openai     OPENAI_API_KEY, or NEWS_LLM_API_KEY with NEWS_LLM_BASE_URL for any
             OpenAI-compatible endpoint (DeepSeek, Qwen, Moonshot, a local vLLM ...)

`auto` picks the first one with a key and falls back to the rules when
there is none.
"""

from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor

from ..ratelimit import TokenBucket
from .config import LLMConfig
from .http import post_json
from .lexicon import score_rules
from .models import NewsItem, Score
from .symbols import Watchlist

log = logging.getLogger(__name__)

DEFAULT_MODELS = {"anthropic": "claude-haiku-5-5", "openai": "gpt-4o-mini"}

SYSTEM = """You are a markets analyst. You rate news items for their likely effect on the prices of \
specific US stocks and crypto assets over the next hours to weeks.

For every item return:
- direction: -1.0 (clearly bearish) to 1.0 (clearly bullish). 0 if neutral, already priced in, or unclear.
- impact: 1-5. 1 = noise or opinion, 2 = minor, 3 = notable, 4 = major company or sector event, \
5 = rare, market-moving (bankruptcy, hack of a top exchange, surprise Fed move, ETF approval).
- confidence: 0.0-1.0, how sure you are about direction.
- horizon: "intraday", "days" or "weeks".
- scope: "symbol" if about particular assets, "market" if about a whole market (all crypto, all US stocks), \
"macro" for economy-wide news (rates, inflation, jobs, tariffs, geopolitics).
- markets: for market/macro scope, which of ["stock", "crypto"] it affects.
- symbols: tickers from the watchlist it is about (use the watchlist spelling). Empty if none.
- reason: one short sentence{lang_hint}.

Rules:
- Judge the news, not the tone. Rumours, opinions, price predictions and memes are low impact and low confidence.
- A headline that only describes a price move ("BTC jumps 3%") is low impact: it reports what already happened.
- Be careful with "less bad than feared", denials, and news that is negative for one asset but positive for a rival.
- Social posts from unknown accounts deserve lower confidence than official sources and newswires.
Reply with JSON only: {{"items": [{{"i": <index>, "direction": .., "impact": .., "confidence": .., \
"horizon": .., "scope": .., "markets": [..], "symbols": [..], "reason": ".."}}]}}"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start:end + 1])


def _clamp(x, lo, hi, default):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


class LLMScorer:
    def __init__(self, cfg: LLMConfig, watchlist: Watchlist, lang: str = "en") -> None:
        self.cfg = cfg
        self.watchlist = watchlist
        self.lang = lang
        self.provider, self.key, self.base_url = self._resolve()
        self.model = cfg.model or os.environ.get("NEWS_LLM_MODEL") or DEFAULT_MODELS.get(self.provider, "")
        self._bucket = TokenBucket(rate=2.0, capacity=2)

    def _resolve(self):
        p = self.cfg.provider
        env = os.environ.get
        if p in ("auto", "anthropic") and env("ANTHROPIC_API_KEY"):
            return "anthropic", env("ANTHROPIC_API_KEY"), self.cfg.base_url or "https://api.anthropic.com"
        if p in ("auto", "openai"):
            key = env("NEWS_LLM_API_KEY") or env("OPENAI_API_KEY")
            if key:
                base = self.cfg.base_url or env("NEWS_LLM_BASE_URL") or env("OPENAI_BASE_URL") or "https://api.openai.com/v1"
                return "openai", key, base.rstrip("/")
        if p not in ("auto", "none"):
            raise RuntimeError(f"llm provider {p!r} selected but no API key found in the environment")
        return "none", None, None

    @property
    def enabled(self) -> bool:
        return self.provider != "none"

    @property
    def name(self) -> str:
        return f"llm:{self.model}" if self.enabled else "rules"

    # --- calls ----------------------------------------------------------------

    def _system(self) -> str:
        hint = " in Simplified Chinese" if self.lang == "zh" else ""
        return SYSTEM.format(lang_hint=hint)

    def _prompt(self, items: list[NewsItem]) -> str:
        wl = {m: self.watchlist.symbols(m) for m in ("stock", "crypto")}
        rows = []
        for i, it in enumerate(items):
            rows.append({
                "i": i, "source": it.source, "kind": it.kind, "author": it.author[:60],
                "time": it.published_at.strftime("%Y-%m-%d %H:%M UTC"), "title": it.title[:300],
                "body": it.body[:500], "tagged": it.symbols,
            })
        return (f"Watchlist: stocks {wl['stock']}, crypto {wl['crypto']}\n\nItems:\n"
                + "\n".join(json.dumps(r, ensure_ascii=False) for r in rows))

    def _call(self, system: str, prompt: str) -> str:
        self._bucket.acquire()
        if self.provider == "anthropic":
            data = post_json(f"{self.base_url}/v1/messages", {
                "model": self.model, "max_tokens": 4096, "temperature": 0, "system": system,
                "messages": [{"role": "user", "content": prompt}],
            }, headers={"x-api-key": self.key, "anthropic-version": "2023-06-01"}, timeout=self.cfg.timeout)
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        data = post_json(f"{self.base_url}/chat/completions", {
            "model": self.model, "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }, headers={"Authorization": f"Bearer {self.key}"}, timeout=self.cfg.timeout)
        return data["choices"][0]["message"]["content"]

    def score_batch(self, items: list[NewsItem]) -> list[Score]:
        reply = self._call(self._system(), self._prompt(items))
        parsed = {int(r.get("i", -1)): r for r in _extract_json(reply).get("items", []) if isinstance(r, dict)}
        out = []
        for i, it in enumerate(items):
            r = parsed.get(i)
            rules = score_rules(it)
            if r is None:
                out.append(rules)
                continue
            scope = r.get("scope") if r.get("scope") in ("symbol", "market", "macro") else rules.scope
            markets = [m for m in (r.get("markets") or []) if m in ("stock", "crypto")]
            if scope != "symbol" and not markets:
                markets = rules.markets or ["stock", "crypto"]
            syms = self.watchlist.normalize([str(s) for s in (r.get("symbols") or [])])
            s = Score(
                item_id=it.id,
                direction=round(_clamp(r.get("direction"), -1, 1, 0.0), 3),
                impact=round(_clamp(r.get("impact"), 1, 5, 2) / 5, 3),
                confidence=round(_clamp(r.get("confidence"), 0, 1, 0.5), 3),
                scorer=f"llm:{self.model}",
                horizon=r.get("horizon") if r.get("horizon") in ("intraday", "days", "weeks") else "days",
                scope=scope, markets=markets, symbols=syms, reason=str(r.get("reason", ""))[:300],
            )
            # second opinion: a strong rule hit pointing the other way lowers confidence
            if rules.impact >= 0.5 and abs(rules.direction) >= 0.5 and abs(s.direction) >= 0.3 \
                    and (rules.direction > 0) != (s.direction > 0):
                s.confidence = round(s.confidence * 0.75, 3)
                s.scorer = f"hybrid:{self.model}"
            out.append(s)
        return out

    def score(self, items: list[NewsItem]) -> list[Score]:
        if not items:
            return []
        if not self.enabled:
            return [score_rules(it) for it in items]
        n = max(1, self.cfg.batch_size)
        batches = [items[i:i + n] for i in range(0, len(items), n)]

        def run(batch):
            try:
                return self.score_batch(batch)
            except Exception as e:
                log.warning("llm scoring failed for %d items, using rules: %s", len(batch), e)
                return [score_rules(it) for it in batch]

        with ThreadPoolExecutor(max_workers=max(1, self.cfg.workers)) as pool:
            return [s for res in pool.map(run, batches) for s in res]
