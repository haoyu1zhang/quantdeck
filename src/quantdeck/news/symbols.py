"""Which assets a piece of text is about.

Assets are keyed by a canonical name: the ticker for stocks (AAPL) and the
base asset for crypto (BTC), so BTC/USDT on Binance and BTC/USD on Alpaca
share the same news.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CRYPTO_NAMES = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "ether", "eth"],
    "SOL": ["solana"],
    "XRP": ["xrp", "ripple"],
    "BNB": ["bnb", "binance coin"],
    "DOGE": ["dogecoin", "doge"],
    "ADA": ["cardano"],
    "AVAX": ["avalanche"],
    "LINK": ["chainlink"],
    "DOT": ["polkadot"],
    "TRX": ["tron"],
    "TON": ["toncoin"],
    "LTC": ["litecoin"],
    "SUI": ["sui network"],
    "MATIC": ["polygon"],
    "SHIB": ["shiba inu"],
    "UNI": ["uniswap"],
    "ARB": ["arbitrum"],
    "OP": ["optimism"],
}

# tickers that are also ordinary words; only matched as $CASHTAG
AMBIGUOUS = {"A", "ALL", "ARE", "BE", "CAN", "CAR", "FOR", "GO", "HAS", "IT", "KEY", "NOW", "ON", "ONE",
             "OPEN", "OUT", "REAL", "SEE", "SO", "TECH", "TRUE", "TWO", "WELL", "AI", "AN", "AM", "BIG",
             "CEO", "EV", "FUN", "LOVE", "MAN", "NEW", "PAY", "PLAY", "RUN", "SAFE", "SAVE", "UP", "USA",
             "OP", "ARB", "LINK", "UNI", "DOT", "SUI", "TON"}

MACRO_TERMS = [
    r"\bfed\b", r"federal reserve", r"\bfomc\b", r"powell", r"rate (?:cut|hike|decision)s?", r"interest rates?",
    r"\bcpi\b", r"\bppi\b", r"\bpce\b", r"inflation", r"jobs report", r"nonfarm payrolls?", r"unemployment rate",
    r"\bgdp\b", r"recession", r"treasury yields?", r"tariffs?", r"trade war", r"debt ceiling",
    r"government shutdown", r"stimulus", r"quantitative (?:easing|tightening)", r"美联储", r"降息", r"加息", r"通胀",
    r"非农", r"关税",
]
CRYPTO_MARKET_TERMS = [
    r"\bcrypto(?:currency|currencies)?\b", r"stablecoins?", r"spot (?:bitcoin |ether |crypto )?etfs?",
    r"\bdefi\b", r"crypto exchange", r"digital assets?", r"加密货币", r"稳定币",
]
STOCK_MARKET_TERMS = [r"s&p 500", r"\bnasdaq\b", r"dow jones", r"wall street", r"stock market", r"\bvix\b", r"美股"]

_MACRO = re.compile("|".join(MACRO_TERMS), re.I)
_CRYPTO_MKT = re.compile("|".join(CRYPTO_MARKET_TERMS), re.I)
_STOCK_MKT = re.compile("|".join(STOCK_MARKET_TERMS), re.I)


def canonical(symbol: str, market: str) -> str:
    s = symbol.strip().lstrip("$").upper()
    if market == "crypto":
        return re.split(r"[/:\-]", s)[0]
    return s.replace("/", ".")


@dataclass
class Asset:
    symbol: str                      # canonical
    market: str                      # stock | crypto
    aliases: list = field(default_factory=list)


class Watchlist:
    def __init__(self, stock: list | None = None, crypto: list | None = None) -> None:
        self.assets: dict[str, Asset] = {}
        for market, entries in (("stock", stock or []), ("crypto", crypto or [])):
            for entry in entries:
                if isinstance(entry, dict):
                    for sym, aliases in entry.items():
                        self._add(sym, market, aliases or [])
                else:
                    self._add(str(entry), market, [])
        self._patterns = {s: self._pattern(a) for s, a in self.assets.items()}

    def _add(self, symbol: str, market: str, aliases) -> None:
        if isinstance(aliases, str):
            aliases = [aliases]
        sym = canonical(symbol, market)
        a = self.assets.setdefault(sym, Asset(sym, market))
        if market == "crypto":
            a.aliases.extend(n for n in CRYPTO_NAMES.get(sym, []) if n not in a.aliases)
        a.aliases.extend(x for x in aliases if x not in a.aliases)

    @staticmethod
    def _pattern(a: Asset) -> re.Pattern:
        parts = [r"\$" + re.escape(a.symbol) + r"\b"]
        if a.symbol not in AMBIGUOUS and len(a.symbol) >= 2:
            # bare ticker, case-sensitive, so "NVDA" matches and "nvda" in a URL slug doesn't need to
            parts.append(r"(?-i:\b" + re.escape(a.symbol) + r"\b)")
        for alias in a.aliases:
            if alias.upper() == a.symbol and a.symbol in AMBIGUOUS:
                continue
            if re.fullmatch(r"\w[\w .&'-]*", alias):
                parts.append(r"\b" + re.escape(alias) + r"\b")
            else:
                parts.append(re.escape(alias))
        return re.compile("|".join(parts), re.I)

    def __contains__(self, symbol: str) -> bool:
        return symbol in self.assets

    def market_of(self, symbol: str) -> str | None:
        a = self.assets.get(symbol)
        return a.market if a else None

    def symbols(self, market: str | None = None) -> list[str]:
        return [s for s, a in self.assets.items() if market is None or a.market == market]

    def find(self, text: str) -> list[str]:
        return [s for s, p in self._patterns.items() if p.search(text)]

    def normalize(self, symbols: list[str]) -> list[str]:
        """Map symbols a source attached (AAPL, BTCUSD, BTC/USD) onto the watchlist."""
        out = []
        for raw in symbols:
            s = str(raw).lstrip("$").upper()
            for cand in (s, canonical(s, "crypto"), re.sub(r"(USDT|USDC|USD)$", "", s)):
                if cand in self.assets and cand not in out:
                    out.append(cand)
                    break
        return out


def market_scope(text: str) -> tuple[str, list[str]]:
    """Rough guess at whether text is market-wide. Returns (scope, markets)."""
    if _MACRO.search(text):
        return "macro", ["stock", "crypto"]
    markets = []
    if _CRYPTO_MKT.search(text):
        markets.append("crypto")
    if _STOCK_MKT.search(text):
        markets.append("stock")
    return ("market", markets) if markets else ("symbol", [])
