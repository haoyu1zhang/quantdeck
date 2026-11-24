import pytest

from quantdeck.config import config_from_dict, load_config


def base(**kw):
    return {"name": "t", "symbols": ["BTC/USDT"], **kw}


def test_defaults_and_sections():
    cfg = config_from_dict(base(broker={"type": "ccxt", "exchange": "binance"}))
    assert cfg.data.exchange == "binance"
    assert cfg.broker.sandbox is True


def test_rejects_unknown_keys():
    with pytest.raises(ValueError, match="unknown keys"):
        config_from_dict(base(risk={"max_loss": 1}))


def test_live_mode_guards():
    with pytest.raises(ValueError, match="real broker"):
        config_from_dict(base(mode="live"))
    with pytest.raises(ValueError, match="sandbox"):
        config_from_dict(base(mode="live", broker={"type": "ccxt", "exchange": "binance"}))


def test_example_configs_load():
    from pathlib import Path

    from quantdeck.news import load_news_config
    from quantdeck.news.sources import make_source
    from quantdeck.news.symbols import Watchlist

    for p in Path(__file__).parent.parent.joinpath("examples").glob("*.yaml"):
        if p.name == "news.yaml":
            cfg = load_news_config(p)
            wl = Watchlist(cfg.stock, cfg.crypto)
            assert all(make_source(s, wl) for s in cfg.sources)
        else:
            load_config(p)
