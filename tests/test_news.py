import json
from datetime import timedelta

import pytest

from quantdeck.news import NewsPipeline, NewsReader, NewsStore, news_config_from_dict
from quantdeck.news.aggregate import build_verdicts, corroboration_factor, engagement_factor, label_for
from quantdeck.news.config import LLMConfig, SourceConfig, WeightConfig
from quantdeck.news.dedup import Deduper
from quantdeck.news.lexicon import score_rules
from quantdeck.news.llm import LLMScorer, _extract_json
from quantdeck.news.models import MARKET_CRYPTO, NewsItem, utcnow
from quantdeck.news.sources import Source, item_from_dict
from quantdeck.news.sources.rss import parse_feed
from quantdeck.news.sources.x import XSearch, _chunks
from quantdeck.news.symbols import Watchlist, canonical, market_scope

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Nvidia beats estimates, raises guidance</title><link>https://ex.com/a</link>
<description>&lt;p&gt;Record &lt;b&gt;revenue&lt;/b&gt;&lt;/p&gt;</description>
<pubDate>Mon, 23 Nov 2026 14:30:00 GMT</pubDate><guid>a1</guid><source url="x">Reuters</source></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
<entry><title>8-K - Current report</title><link rel="alternate" href="https://sec.gov/x"/>
<summary>Filed: 2026-11-20 Item 2.02 and 9.01</summary><updated>2026-11-20T21:05:00-05:00</updated>
<id>urn:1</id></entry></feed>"""


def wl():
    return Watchlist(stock=[{"NVDA": ["Nvidia"]}, "AAPL", {"ON": ["onsemi"]}], crypto=["BTC/USDT", "ETH"])


def cfg_dict(tmp_path, **kw):
    d = {"db": str(tmp_path / "news.db"), "stock": [{"NVDA": ["Nvidia"]}, "AAPL"], "crypto": ["BTC"],
         "llm": {"provider": "none"}, "sources": []}
    d.update(kw)
    return d


class FakeSource(Source):
    type = "fake"

    def __init__(self, name, items, watchlist, kind="media", credibility=None, fail=False):
        super().__init__(SourceConfig(name=name, type="fake", kind=kind, credibility=credibility), watchlist)
        self._items, self._fail, self.calls = items, fail, 0

    def fetch(self, state):
        self.calls += 1
        state["calls"] = state.get("calls", 0) + 1
        if self._fail:
            raise ConnectionError("down")
        return [self.item(**i) for i in self._items]


def mk(title, minutes_ago=10, **kw):
    return {"title": title, "published_at": utcnow() - timedelta(minutes=minutes_ago), **kw}


# --- parsing ------------------------------------------------------------------

def test_parse_rss_and_atom():
    r = parse_feed(RSS)[0]
    assert r["title"].startswith("Nvidia beats") and r["url"] == "https://ex.com/a"
    assert r["body"] == "Record revenue" and r["id"] == "a1" and r["author"] == "Reuters"
    a = parse_feed(ATOM)[0]
    assert a["url"] == "https://sec.gov/x" and "2.02" in a["body"] and a["published"].startswith("2026-11-20")


# --- symbols ------------------------------------------------------------------

def test_watchlist_matching():
    w = wl()
    assert canonical("BTC/USDT", "crypto") == "BTC" and canonical("brk/b", "stock") == "BRK.B"
    assert set(w.find("Nvidia and Bitcoin rally; $AAPL flat")) == {"NVDA", "BTC", "AAPL"}
    assert w.find("Ether ETF inflows") == ["ETH"]
    assert "ON" not in w.find("shares moved on the news")      # ambiguous ticker needs a cashtag
    assert "ON" in w.find("$ON jumps") and "ON" in w.find("onsemi guides lower")
    assert w.find("nvda in a url") == []                        # bare ticker is case sensitive
    assert w.normalize(["BTCUSD", "nvda", "TSLA", "ETH/USD"]) == ["BTC", "NVDA", "ETH"]


def test_market_scope():
    assert market_scope("Fed signals rate cut in December")[0] == "macro"
    assert market_scope("Stablecoin bill passes Senate") == ("market", ["crypto"])
    assert market_scope("Apple unveils new iPad")[0] == "symbol"


# --- dedup --------------------------------------------------------------------

def test_dedup_groups_syndicated_copies():
    d = Deduper()
    a = d.assign("1", "Nvidia beats estimates as data center revenue soars - Reuters")
    b = d.assign("2", "Nvidia beats estimates as data center revenue soars | CNBC")
    c = d.assign("3", "Nvidia beats estimates as data center revenue soars, shares jump after hours")
    e = d.assign("4", "Tesla recalls 200,000 vehicles over camera issue")
    assert a == b == c == "1" and e == "4"


# --- scoring ------------------------------------------------------------------

def _item(title, **kw):
    return NewsItem(source="t", kind=kw.pop("kind", "media"), title=title, published_at=utcnow(), **kw)


def test_rules_direction():
    assert score_rules(_item("Nvidia beats estimates and raises guidance")).direction > 0.5
    assert score_rules(_item("Exchange hacked, $200M drained from hot wallet")).direction < -0.5
    assert score_rules(_item("Company files for Chapter 11 bankruptcy")).impact >= 0.9
    neutral = score_rules(_item("Apple to hold event next week"))
    assert neutral.direction == 0 and neutral.confidence <= 0.2
    assert score_rules(_item("Fed cuts rates by 25bp")).scope == "macro"
    # negation flips and weakens
    assert score_rules(_item("CEO denies company was hacked")).direction > 0


def test_llm_scorer_parses_and_clamps(monkeypatch):
    w = wl()
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    s = LLMScorer(LLMConfig(provider="openai", model="m"), w)
    items = [_item("Nvidia beats estimates"), _item("Fed hikes rates unexpectedly"), _item("Bitcoin hacked?")]
    reply = {"items": [
        {"i": 0, "direction": 0.8, "impact": 4, "confidence": 0.9, "horizon": "days", "scope": "symbol",
         "symbols": ["nvda", "XYZ"], "reason": "beat"},
        {"i": 1, "direction": -3, "impact": 9, "confidence": 2, "horizon": "soon", "scope": "macro", "markets": []},
    ]}
    monkeypatch.setattr(s, "_call", lambda system, prompt: "```json\n" + json.dumps(reply) + "\n```")
    out = s.score(items)
    assert out[0].direction == 0.8 and out[0].impact == 0.8 and out[0].symbols == ["NVDA"]
    assert out[1].direction == -1 and out[1].impact == 1 and out[1].confidence == 1
    assert out[1].horizon == "days" and set(out[1].markets) == {"stock", "crypto"}
    assert out[2].scorer == "rules"                # missing from reply -> rules
    assert _extract_json('noise {"items": []} tail') == {"items": []}


def test_llm_failure_falls_back_to_rules(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    s = LLMScorer(LLMConfig(provider="auto"), wl())
    assert s.provider == "anthropic"

    def boom(*a):
        raise TimeoutError("slow")

    monkeypatch.setattr(s, "_call", boom)
    assert [x.scorer for x in s.score([_item("Nvidia beats estimates")])] == ["rules"]


def test_llm_auto_without_keys_uses_rules(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "NEWS_LLM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert LLMScorer(LLMConfig(), wl()).name == "rules"
    with pytest.raises(RuntimeError):
        LLMScorer(LLMConfig(provider="anthropic"), wl())


# --- aggregation --------------------------------------------------------------

def row(id, direction, symbols=(), cluster=None, minutes_ago=30, cred=0.9, impact=0.8, conf=0.9, kind="media",
        scope="symbol", markets=(), corroboration=1, engagement=None):
    return {"id": id, "cluster_id": cluster or id, "title": id, "source": "s", "url": "", "kind": kind,
            "published_at": (utcnow() - timedelta(minutes=minutes_ago)).isoformat(), "credibility": cred,
            "impact": impact, "confidence": conf, "direction": direction, "horizon": "days", "scope": scope,
            "markets": list(markets), "symbols": list(symbols), "score_symbols": [], "reason": "",
            "corroboration": corroboration, "engagement": engagement or {}}


def by_symbol(vs):
    return {v.symbol: v for v in vs}


def test_verdict_direction_prior_and_labels():
    w, cfg = wl(), WeightConfig()
    v = by_symbol(build_verdicts([row("a", 1, ["NVDA"]), row("b", 0.8, ["NVDA"]), row("c", -0.9, ["AAPL"])], w, cfg))
    assert v["NVDA"].score > 0.45 and v["NVDA"].label == "strong_bullish"
    assert v["AAPL"].score < 0 and abs(v["AAPL"].score) < 0.9   # one headline is shrunk by the prior
    assert v["ETH"].label == "neutral" and v["ETH"].items == 0
    assert v["NVDA"].drivers[0].item_id in ("a", "b")
    assert label_for(-0.2, cfg) == "bearish" and label_for(0.0, cfg) == "neutral"


def test_cluster_counts_once_and_old_news_fades():
    w, cfg = wl(), WeightConfig()
    copies = [row(f"c{i}", -1, ["BTC"], cluster="story", corroboration=5) for i in range(10)]
    one = by_symbol(build_verdicts(copies, w, cfg))["BTC"]
    assert one.items == 1
    fresh = by_symbol(build_verdicts([row("f", 1, ["BTC"], minutes_ago=10)], w, cfg))["BTC"].weight
    stale = by_symbol(build_verdicts([row("s", 1, ["BTC"], minutes_ago=60 * 48)], w, cfg))["BTC"].weight
    assert stale < fresh * 0.3
    assert corroboration_factor(1) == 1 and corroboration_factor(4) == 1.5
    assert engagement_factor({}) == 1 and 1 < engagement_factor({"likes": 5000, "reposts": 800}) <= 1.6


def test_macro_news_reaches_every_symbol_at_reduced_weight():
    w, cfg = wl(), WeightConfig()
    v = by_symbol(build_verdicts([row("m", -1, scope="macro", markets=["stock", "crypto"])], w, cfg))
    assert v["MARKET:stock"].score < 0 and v[MARKET_CRYPTO].score < 0
    assert v["NVDA"].score < 0 and v["NVDA"].weight == pytest.approx(v["MARKET:stock"].weight * cfg.macro_beta)


# --- sources ------------------------------------------------------------------

def test_x_queries_and_author_credibility():
    w = Watchlist(stock=[f"T{i:03d}" for i in range(120)], crypto=["BTC"])
    x = XSearch(SourceConfig(name="x", type="x", options={"accounts": ["@SECGov"], "trusted": {"secgov": 1.0}}), w)
    qs = x.queries()
    assert all(len(q) <= 512 for q in qs) and len(qs) >= 3 and any("from:SECGov" in q for q in qs)
    assert x.author_credibility({"username": "SECGov"}) == 1.0
    small = x.author_credibility({"username": "a", "public_metrics": {"followers_count": 200}})
    big = x.author_credibility({"username": "b", "verified_type": "business",
                                "public_metrics": {"followers_count": 2_000_000}})
    assert small < 0.35 < big <= 0.75
    assert _chunks(["a", "b"], " x") == ["(a OR b) x"]


def test_item_from_dict_validates():
    src = FakeSource("in", [], wl())
    it = item_from_dict({"title": "hi $NVDA", "symbols": ["nvda"], "credibility": 7, "engagement": {"likes": 3,
                         "bad": "x"}}, src)
    assert it.symbols == ["NVDA"] and it.credibility == 1.0 and it.engagement == {"likes": 3}
    with pytest.raises(ValueError):
        item_from_dict({"title": " "}, src)


def test_config_validation(tmp_path):
    with pytest.raises(ValueError):
        news_config_from_dict({"db": "x"})                       # no watchlist
    with pytest.raises(ValueError):
        news_config_from_dict(cfg_dict(tmp_path, sources=[{"name": "a", "type": "rss"}, {"name": "a", "type": "rss"}]))
    c = news_config_from_dict(cfg_dict(tmp_path, sources=[{"name": "r", "type": "reddit", "subreddits": ["x"]}]))
    assert c.sources[0].options == {"subreddits": ["x"]}


# --- pipeline -----------------------------------------------------------------

def test_pipeline_end_to_end(tmp_path):
    cfg = news_config_from_dict(cfg_dict(tmp_path))
    w = Watchlist(cfg.stock, cfg.crypto)
    good = FakeSource("wire", [
        mk("Nvidia beats estimates, raises guidance - Reuters", url="u1", native_id="1"),
        mk("Nvidia beats estimates, raises guidance | CNBC", url="u2", native_id="2"),
        mk("Bitcoin exchange hacked, $300M drained", native_id="3"),
        mk("Local bakery wins award", native_id="4"),
        mk("Ancient news about Apple", minutes_ago=60 * 24 * 30, native_id="5"),
    ], w, kind="newswire")
    bad = FakeSource("flaky", [], w, fail=True)
    pipe = NewsPipeline(cfg, sources=[good, bad])
    rep = pipe.run_once(force=True)
    assert rep.errors == {"flaky": "ConnectionError: down"} and rep.fetched == {"wire": 5}
    assert rep.new_items == 4 and rep.scored == 3              # 30-day-old item dropped; bakery stored, not scored
    v = by_symbol(rep.verdicts)
    assert v["NVDA"].score > 0.15 and v["NVDA"].items == 1     # the two copies are one story
    assert v["BTC"].score < -0.15
    st = pipe.store
    nvda = st.items("NVDA")
    assert len(nvda) == 2 and len({i["cluster_id"] for i in nvda}) == 1
    assert st.source_state("wire") == {"calls": 1}
    assert {s["source"]: bool(s["last_error"]) for s in st.sources()} == {"wire": False, "flaky": True}

    # a second cycle with nothing new scores nothing and keeps the verdict
    rep2 = pipe.run_once(force=True)
    assert rep2.new_items == 0 and rep2.scored == 0
    assert by_symbol(rep2.verdicts)["NVDA"].label == v["NVDA"].label

    # sources aren't fetched again before `every` minutes have passed
    assert pipe.due() == []


def test_alerts_fire_on_label_change(tmp_path):
    cfg = news_config_from_dict(cfg_dict(tmp_path, alerts={"min_confidence": 0.0, "lang": "zh"}))
    w = Watchlist(cfg.stock, cfg.crypto)
    sent = []

    class N:
        def notify(self, event, title, text="", key=None):
            sent.append((event, title))

    titles = ["Apple supplier files for Chapter 11 bankruptcy", "DOJ sues Apple over App Store antitrust claims",
              "Apple cuts guidance as iPhone sales plunge"]
    src = FakeSource("off", [mk(t, native_id=str(i), symbols=["AAPL"]) for i, t in enumerate(titles)], w,
                     kind="official")
    pipe = NewsPipeline(cfg, sources=[src], notifier=N())
    pipe.run_once(force=True)
    assert any(e == "news" and "AAPL" in t and "强利空" in t for e, t in sent)
    n = len(sent)
    pipe.run_once(force=True)
    assert len(sent) == n                                      # same label, no repeat


def test_ingest_endpoint_feeds_pipeline(tmp_path):
    from fastapi.testclient import TestClient

    from quantdeck.api.server import create_app

    cfg = news_config_from_dict(cfg_dict(tmp_path))
    db = cfg.db_path
    with TestClient(create_app(tmp_path / "runs", news_db=db)) as c:
        assert c.get("/api/news/verdicts").json()["available"] is False
        assert c.post("/api/news/ingest", json={"items": [{"title": ""}]}).status_code == 400
        r = c.post("/api/news/ingest", json={"items": [
            {"title": "$NVDA wins $2 billion contract from Pentagon", "kind": "social", "credibility": 0.5,
             "engagement": {"likes": 900}}]})
        assert r.status_code == 202 and r.json() == {"queued": 1}

        pipe = NewsPipeline(cfg)                               # real constructor adds the ingest inbox
        rep = pipe.run_once()
        assert rep.fetched.get("ingest") == 1 and rep.scored == 1
        body = c.get("/api/news/verdicts").json()
        nv = {v["symbol"]: v for v in body["verdicts"]}["NVDA"]
        assert nv["score"] > 0 and nv["drivers"][0]["title"].startswith("$NVDA")
        items = c.get("/api/news/items", params={"symbol": "NVDA"}).json()
        assert items[0]["source"] == "ingest" and items[0]["credibility"] == 0.5
        assert c.get("/api/news/history", params={"symbol": "NVDA"}).json()


# --- trading link -------------------------------------------------------------

def test_reader_and_entry_gate(tmp_path):
    from quantdeck.brokers.sim import SimBroker
    from quantdeck.config import FeeConfig, RiskConfig
    from quantdeck.context import Context
    from quantdeck.models import Bar
    from quantdeck.news.models import Verdict
    from quantdeck.risk import RiskManager

    db = tmp_path / "news.db"
    st = NewsStore(db)
    st.add_verdicts([Verdict("BTC", -0.6, "strong_bearish", 0.7, 3, 2.0, 0.1, 1.9)])
    reader = NewsReader(db, "crypto", max_age_minutes=60, cache_seconds=0)
    assert reader.score("BTC/USDT") == -0.6 and reader.verdict("ETH/USDT") is None

    broker = SimBroker(10_000, FeeConfig(), fractional=True)
    ctx = Context(broker, RiskManager(RiskConfig()), ["BTC/USDT"])
    ctx.push_bar(Bar("BTC/USDT", utcnow(), 100, 100, 100, 100, 1))
    assert ctx.news("BTC/USDT") is None                        # no reader: backtest behaviour
    ctx.news_reader, ctx.news_gate = reader, (-0.45, None)
    assert ctx.news_score("BTC/USDT") == -0.6
    o = ctx.buy("BTC/USDT", 1)
    assert o.status.value == "rejected" and "strong_bearish" in o.reason

    st.add_verdicts([Verdict("BTC", 0.1, "neutral", 0.2, 1, 0.5, 0.3, 0.2, ts=utcnow() + timedelta(seconds=1))])
    assert ctx.buy("BTC/USDT", 1).status.value != "rejected"

    old = Verdict("BTC", -0.9, "strong_bearish", 0.9, 5, 3, 0, 3, ts=utcnow() - timedelta(hours=3))
    st2 = NewsStore(tmp_path / "old.db")
    st2.add_verdicts([old])
    assert NewsReader(tmp_path / "old.db", "crypto", max_age_minutes=60).verdict("BTC") is None
