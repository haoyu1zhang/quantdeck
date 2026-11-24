from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import make_bars
from quantdeck.backtest.optimize import Settings, expand, grid_search, parse_grid, walk_forward
from quantdeck.config import FeeConfig


def test_parse_grid():
    g = parse_grid(["fast=10:30:10", "slow=50,100", "risk=0.01:0.03:0.01", "x=7"])
    assert g == {"fast": [10, 20, 30], "slow": [50, 100], "risk": [0.01, 0.02, 0.03], "x": [7]}
    assert len(expand(g)) == 18
    with pytest.raises(ValueError):
        parse_grid(["fast"])


@pytest.fixture
def data():
    return {"A": make_bars(800, seed=5), "B": make_bars(800, seed=6, start=40)}


def test_grid_search_ranks_and_skips_invalid(data):
    settings = Settings("sma_cross", fees=FeeConfig(rate=0.001))
    grid = {"fast": [10, 20, 60], "slow": [50, 100]}
    table = grid_search(settings, grid, data, workers=1, min_trades=1)
    assert len(table) == 6
    bad = table[(table.fast == 60) & (table.slow == 50)]
    assert bad.error.notna().all() and (bad.score == float("-inf")).all()
    good = table[table.score > float("-inf")]
    assert list(good.score) == sorted(good.score, reverse=True)


def test_walk_forward_is_out_of_sample(data):
    settings = Settings("sma_cross", fees=FeeConfig(rate=0.001))
    res = walk_forward(settings, {"fast": [10, 20], "slow": [50, 100]}, data, folds=3, train_frac=0.6,
                       executor=ThreadPoolExecutor(2))
    f = res.folds
    assert len(f) == 3
    assert (f.train_end < f.test_start).all()
    assert (f.test_start.iloc[1:].values > f.test_end.iloc[:-1].values).all()
    assert res.equity.index[0] >= f.test_start.iloc[0]
    assert res.equity.iloc[0] == pytest.approx(settings.cash)
    assert "efficiency" in res.metrics
    assert "out-of-sample" in res.summary()


def test_process_pool_path(data):
    settings = Settings("sma_cross")
    table = grid_search(settings, {"fast": [10, 20], "slow": [50]}, data, workers=2)
    assert len(table) == 2 and table.score.notna().all()
