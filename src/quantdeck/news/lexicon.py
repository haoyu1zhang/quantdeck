"""Rule-based scorer: finance-specific phrases with a direction and an impact.

Used on its own when no LLM is configured, and next to the LLM as a sanity
check. It reads headlines literally, so it misses sarcasm, context and
"less bad than feared". Treat its output as a rough prior.
"""

from __future__ import annotations

import re

from .models import NewsItem, Score
from .symbols import market_scope

# (pattern, direction, impact 0..1)
RULES: list[tuple[str, float, float]] = [
    # earnings and guidance
    (r"beats? (?:\w+ )?(?:estimates|expectations|forecasts|consensus)", 1, 0.6),
    (r"tops? (?:\w+ )?(?:estimates|expectations|forecasts)", 1, 0.6),
    (r"miss(?:es|ed)? (?:\w+ )?(?:estimates|expectations|forecasts|consensus)", -1, 0.6),
    (r"(?:raises?|lifts?|boosts?|hikes?) (?:\w+ )?(?:guidance|outlook|forecast)", 1, 0.7),
    (r"(?:cuts?|lowers?|slashes?) (?:\w+ )?(?:guidance|outlook|forecast)", -1, 0.7),
    (r"record (?:revenue|profit|sales|earnings|high)", 1, 0.5),
    (r"profit warning|warns? (?:on|of) (?:profit|revenue|sales)", -1, 0.7),
    (r"(?:revenue|sales|profit) (?:falls?|drops?|declines?|plunges?)", -1, 0.5),
    (r"(?:revenue|sales|profit) (?:rises?|jumps?|surges?|soars?|grows?)", 1, 0.5),
    # analyst actions
    (r"upgrade[sd]?\b|raised to (?:buy|outperform|overweight)", 1, 0.5),
    (r"downgrade[sd]?\b|cut to (?:sell|underperform|underweight|neutral)", -1, 0.5),
    (r"(?:raises?|lifts?) (?:price )?target", 1, 0.35),
    (r"(?:cuts?|lowers?) (?:price )?target", -1, 0.35),
    # corporate actions
    (r"(?:share|stock) buyback|repurchase program", 1, 0.45),
    (r"(?:raises?|increases?|hikes?) (?:its )?dividend", 1, 0.35),
    (r"(?:cuts?|suspends?|slashes?) (?:its )?dividend", -1, 0.6),
    (r"to acquire|agrees? to buy|takeover bid|buyout offer", 1, 0.5),
    (r"stock split", 1, 0.3),
    (r"(?:secondary|stock) offering|share sale|dilut", -1, 0.45),
    (r"layoffs?|job cuts|cut(?:s|ting)? \d[\d,]* jobs", -1, 0.3),
    (r"bankrupt|chapter 11|insolven|receivership", -1, 0.95),
    (r"delist", -1, 0.8),
    (r"restat(?:e|ement)|accounting (?:irregularit|error)|non-reliance", -1, 0.8),
    (r"ceo (?:resigns?|steps? down|ousted|fired)|cfo (?:resigns?|steps? down)", -1, 0.5),
    (r"recalls?\b", -1, 0.4),
    (r"(?:wins?|awarded|secures?|lands?) [^.,;]{0,40}?(?:contract|deal|order)", 1, 0.45),
    (r"partnership with|strategic partnership", 1, 0.3),
    (r"fda (?:approv|clear)", 1, 0.75),
    (r"fda (?:reject|declin)|complete response letter|clinical hold", -1, 0.75),
    (r"(?:trial|study) (?:succeeds?|meets? (?:primary )?endpoint)", 1, 0.7),
    (r"(?:trial|study) (?:fails?|misses? (?:primary )?endpoint)", -1, 0.75),
    # legal and regulatory
    (r"(?:sec|doj|ftc|cftc) (?:sues?|charges?|probe|investigat)", -1, 0.7),
    (r"\b(?:lawsuit|sued|class action|antitrust)\b", -1, 0.4),
    (r"\b(?:fined?|penalty|settles? charges)\b", -1, 0.4),
    (r"(?:investigation|probe) (?:dropped|closed|ends?)|charges dropped|wins? (?:lawsuit|case|appeal)", 1, 0.5),
    (r"sanction(?:s|ed)", -1, 0.5),
    (r"export (?:ban|restriction|curbs?)", -1, 0.5),
    # crypto
    (r"(?:etf|etfs) (?:approv|launch|gets? (?:the )?green light)|approves? (?:\w+ )*etfs?", 1, 0.8),
    (r"(?:etf|etfs) (?:reject|denied|delay)", -1, 0.55),
    (r"\b(?:hack(?:ed)?|exploit(?:ed)?|drained|stolen|heist)\b", -1, 0.75),
    (r"rug ?pull|ponzi|fraud", -1, 0.7),
    (r"depeg|de-peg|loses? (?:its )?peg", -1, 0.85),
    (r"(?:etf )?inflows?", 1, 0.35),
    (r"(?:etf )?outflows?", -1, 0.35),
    (r"liquidat(?:ed|ions?)", -1, 0.35),
    (r"(?:halts?|suspends?|pauses?) (?:withdrawals|deposits|trading)", -1, 0.8),
    (r"(?:lists?|listing) on (?:binance|coinbase|robinhood|upbit)|adds? support for", 1, 0.45),
    (r"(?:strategic )?(?:bitcoin|crypto) reserve", 1, 0.55),
    (r"(?:treasury|corporate) (?:buys?|adds?|purchases?) .{0,20}(?:btc|bitcoin|eth|ether)", 1, 0.45),
    (r"whales? (?:buy|accumulat)", 1, 0.25),
    (r"whales? (?:sell|dump)|moves? .{0,20} to (?:an )?exchange", -1, 0.25),
    (r"crackdown|\bban(?:s|ned)?\b", -1, 0.55),
    (r"(?:clear|regulatory) clarity|bill (?:passes|signed)", 1, 0.5),
    (r"unlock(?:s|ing)? .{0,30}tokens|token unlock", -1, 0.3),
    # macro (sign is from the point of view of risk assets)
    (r"rate cut|cuts? (?:interest )?rates|dovish", 1, 0.7),
    (r"rate hike|raises? (?:interest )?rates|hawkish", -1, 0.7),
    (r"(?:inflation|cpi|pce) (?:cools?|eases?|slows?|falls?|below)", 1, 0.6),
    (r"(?:inflation|cpi|pce) (?:heats?|accelerates?|jumps?|rises?|hotter|above)", -1, 0.6),
    (r"recession fears?|slowdown fears|hard landing", -1, 0.5),
    (r"soft landing", 1, 0.4),
    (r"tariffs? (?:imposed|hike|raised|threat)|new tariffs|trade war", -1, 0.55),
    (r"tariffs? (?:paused|lifted|cut|removed|exempt)|trade deal", 1, 0.5),
    (r"government shutdown", -1, 0.35),
    # price action words, weak on their own
    (r"\b(?:soars?|surges?|skyrockets?|rall(?:y|ies)|jumps?|spikes?)\b", 1, 0.25),
    (r"\b(?:plunges?|plummets?|crash(?:es)?|tumbles?|sinks?|slumps?|tanks?)\b", -1, 0.25),
    (r"all[- ]time high|\bath\b|record high", 1, 0.25),
    (r"\b(?:bullish|moon(?:ing)?|breakout)\b", 1, 0.15),
    (r"\b(?:bearish|dump(?:ing)?|capitulat)", -1, 0.15),
    # Chinese headlines
    (r"利好|大涨|暴涨|飙升|创新高|超预期|上调评级|回购|增持|获批", 1, 0.45),
    (r"利空|大跌|暴跌|跳水|崩盘|不及预期|下调评级|减持|被盗|黑客|爆雷|立案|退市|破产", -1, 0.5),
]

_COMPILED = [(re.compile(p, re.I), d, m) for p, d, m in RULES]
_NEGATION = re.compile(r"\b(?:no|not|denies|denied|false|rumou?rs? of|won't|unlikely to|fails to)\b[^.]{0,25}$", re.I)


def score_rules(item: NewsItem) -> Score:
    text = item.text
    hits: list[tuple[float, float]] = []
    for pat, d, m in _COMPILED:
        mt = pat.search(text)
        if not mt:
            continue
        if _NEGATION.search(text[max(0, mt.start() - 40):mt.start()]):
            d = -d * 0.5
        hits.append((d, m))
    scope, markets = market_scope(text)
    if not hits:
        return Score(item.id, 0.0, 0.1, 0.2, scorer="rules", scope=scope, markets=markets, reason="no rule matched")
    # strongest signal dominates; agreeing signals add a little, conflicting ones cut confidence
    hits.sort(key=lambda h: -h[1])
    top_d, top_m = hits[0]
    net = sum(d * m for d, m in hits)
    gross = sum(abs(d) * m for d, m in hits)
    agreement = abs(net) / gross if gross else 0.0
    # a single strong rule (bankruptcy, hack) points almost fully one way; weak ones less so
    direction = max(-1.0, min(1.0, net / max(top_m, 1e-9))) * (0.6 + 0.4 * top_m) if gross else 0.0
    impact = min(1.0, top_m + 0.1 * (len(hits) - 1))
    confidence = round(0.25 + 0.35 * agreement, 3)
    return Score(item.id, round(direction, 3), round(impact, 3), confidence, scorer="rules", scope=scope,
                 markets=markets, reason=f"{len(hits)} rule hit(s)")
