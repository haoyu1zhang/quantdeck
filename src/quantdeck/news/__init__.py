"""News collection and bullish/bearish scoring for the watchlist.

    sources -> tag symbols -> dedupe -> score (LLM + rules) -> weight -> verdict per asset

See examples/news.yaml and the "News" section of the README.
"""

from .config import NewsConfig, load_news_config, news_config_from_dict
from .models import LABELS, LABELS_ZH, NewsItem, Score, Verdict
from .pipeline import NewsPipeline
from .reader import NewsReader
from .store import NewsStore

__all__ = [
    "NewsConfig", "load_news_config", "news_config_from_dict", "NewsItem", "Score", "Verdict", "LABELS",
    "LABELS_ZH", "NewsPipeline", "NewsReader", "NewsStore",
]
