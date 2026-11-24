"""collect -> tag -> dedupe -> store -> score -> verdicts -> alerts"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import timedelta

from .aggregate import build_verdicts
from .config import NewsConfig
from .dedup import Deduper
from .llm import LLMScorer
from .models import LABELS_ZH, NewsItem, Verdict, utcnow
from .config import SourceConfig
from .sources import JsonlInbox, Source, make_source
from .store import NewsStore
from .symbols import Watchlist, market_scope

log = logging.getLogger(__name__)

MAX_ITEM_AGE = timedelta(days=7)
SCORE_PER_CYCLE = 400


@dataclass
class CycleReport:
    fetched: dict = field(default_factory=dict)   # source -> items returned
    errors: dict = field(default_factory=dict)    # source -> message
    new_items: int = 0
    scored: int = 0
    verdicts: list = field(default_factory=list)
    alerts: list = field(default_factory=list)
    seconds: float = 0.0


class NewsPipeline:
    def __init__(self, cfg: NewsConfig, store: NewsStore | None = None, notifier=None,
                 sources: list[Source] | None = None, scorer: LLMScorer | None = None) -> None:
        self.cfg = cfg
        self.watchlist = Watchlist(cfg.stock, cfg.crypto)
        self.store = store or NewsStore(cfg.db_path)
        self.sources = sources if sources is not None else [
            make_source(s, self.watchlist) for s in cfg.sources if s.enabled]
        if sources is None and not any(s.name == "ingest" for s in self.sources):
            # items pushed through POST /api/news/ingest land here
            self.sources.append(JsonlInbox(SourceConfig(name="ingest", type="jsonl", every=0,
                                                        options={"path": str(inbox_dir(cfg))}), self.watchlist))
        self.scorer = scorer or LLMScorer(cfg.llm, self.watchlist, lang=cfg.alerts.lang)
        self.notifier = notifier
        self._last_labels: dict[str, str] = {
            v["symbol"]: v["label"] for v in self.store.latest_verdicts()}
        self._stop = threading.Event()

    # --- one cycle --------------------------------------------------------------

    def due(self, force: bool = False) -> list[Source]:
        if force:
            return list(self.sources)
        now = utcnow()
        out = []
        for s in self.sources:
            last = self.store.last_run(s.name)
            if last is None or now - last >= timedelta(minutes=s.cfg.every) - timedelta(seconds=5):
                out.append(s)
        return out

    def collect(self, sources: list[Source], report: CycleReport) -> list[NewsItem]:
        if not sources:
            return []
        items: list[NewsItem] = []
        workers = max(1, min(self.cfg.fetch_workers, len(sources)))
        states = {s.name: self.store.source_state(s.name) for s in sources}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="news-fetch") as pool:
            futs = {pool.submit(s.fetch, states[s.name]): s for s in sources}
            for fut in as_completed(futs):
                s = futs[fut]
                try:
                    got = fut.result()
                except Exception as e:
                    msg = f"{type(e).__name__}: {e}"[:300]
                    report.errors[s.name] = msg
                    self.store.save_source(s.name, states[s.name], False, msg, 0)
                    log.warning("news source %s failed: %s", s.name, msg)
                    continue
                for it in got:
                    if it.credibility is None:
                        it.credibility = s.credibility
                report.fetched[s.name] = len(got)
                self.store.save_source(s.name, states[s.name], True, None, len(got))
                items.extend(got)
        return items

    def prepare(self, items: list[NewsItem]) -> tuple[list[NewsItem], dict[str, bool]]:
        """Merge duplicates by id, tag symbols, assign clusters, decide relevance."""
        merged: dict[str, NewsItem] = {}
        for it in items:
            if it.id in merged:
                merged[it.id].symbols = sorted(set(merged[it.id].symbols) | set(it.symbols))
            else:
                merged[it.id] = it
        existing = self.store.existing_ids(list(merged))
        now = utcnow()
        dedup = Deduper()
        dedup.seed(self.store.recent_titles())
        out, relevant = [], {}
        for it in sorted(merged.values(), key=lambda x: x.published_at):
            if it.id not in existing:
                if now - it.published_at > MAX_ITEM_AGE:
                    continue
                if it.published_at > now + timedelta(minutes=10):
                    it.published_at = now  # bad clocks and timezone bugs on the source side
                it.symbols = sorted(set(self.watchlist.normalize(it.symbols)) | set(self.watchlist.find(it.text)))
                it.cluster_id = dedup.assign(it.id, it.title)
                scope, _ = market_scope(it.text)
                # untagged, non-market items are kept for the record but not scored
                relevant[it.id] = bool(it.symbols) or scope != "symbol" or it.kind == "official"
            out.append(it)
        return out, relevant

    def score_pending(self) -> int:
        pending = self.store.unscored(SCORE_PER_CYCLE)
        if not pending:
            return 0
        scores = self.scorer.score(pending)
        self.store.add_scores(scores)
        return len(scores)

    def refresh_verdicts(self) -> list[Verdict]:
        rows = self.store.scored_window(self.cfg.weights.max_age_hours)
        verdicts = build_verdicts(rows, self.watchlist, self.cfg.weights)
        self.store.add_verdicts(verdicts)
        return verdicts

    def run_once(self, force: bool = False) -> CycleReport:
        t0 = time.monotonic()
        report = CycleReport()
        raw = self.collect(self.due(force), report)
        items, relevant = self.prepare(raw)
        report.new_items = self.store.add_items(items, relevant)
        report.scored = self.score_pending()
        report.verdicts = self.refresh_verdicts()
        report.alerts = self.alert(report.verdicts)
        report.seconds = time.monotonic() - t0
        return report

    # --- alerts -----------------------------------------------------------------

    def alert(self, verdicts: list[Verdict]) -> list[str]:
        a = self.cfg.alerts
        sent = []
        for v in verdicts:
            prev = self._last_labels.get(v.symbol)
            self._last_labels[v.symbol] = v.label
            if not a.enabled or v.label == prev or v.label not in a.labels or v.confidence < a.min_confidence:
                continue
            title, text = format_alert(v, a.lang)
            sent.append(title)
            if self.notifier is not None:
                self.notifier.notify("news", title, text, key=f"news:{v.symbol}:{v.label}")
        return sent

    # --- loop -------------------------------------------------------------------

    def stop(self, *_):
        self._stop.set()

    def watch(self, max_cycles: int | None = None, on_cycle=None) -> None:
        n = 0
        last_prune = 0.0
        while not self._stop.is_set():
            try:
                rep = self.run_once()
                if on_cycle:
                    on_cycle(rep)
            except Exception:
                log.exception("news cycle failed")
            if time.monotonic() - last_prune > 6 * 3600:
                self.store.prune()
                last_prune = time.monotonic()
            n += 1
            if max_cycles is not None and n >= max_cycles:
                break
            self._stop.wait(self.cfg.interval)


def inbox_dir(cfg: NewsConfig):
    return cfg.db_path.parent / "inbox"


def format_alert(v: Verdict, lang: str = "en") -> tuple[str, str]:
    name = v.symbol.replace("MARKET:stock", "US stocks").replace("MARKET:crypto", "crypto market")
    if lang == "zh":
        name = name.replace("US stocks", "美股大盘").replace("crypto market", "加密市场")
        title = f"{name} {LABELS_ZH[v.label]} (得分 {v.score:+.2f}, 置信度 {v.confidence:.0%})"
    else:
        title = f"{name} {v.label.replace('_', ' ')} (score {v.score:+.2f}, confidence {v.confidence:.0%})"
    lines = [f"{'+' if d.direction > 0 else '-'} [{d.source}] {d.title[:140]}" for d in v.drivers[:3]]
    return title, "\n".join(lines)
