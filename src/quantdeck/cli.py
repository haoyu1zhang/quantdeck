from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


def _setup_logging(verbose: bool, logfile: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if logfile:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def cmd_backtest(args) -> int:
    from .backtest import run_backtest
    from .config import load_config
    from .data import load_history
    from .strategies import load_strategy

    cfg = load_config(args.config)
    d = cfg.data
    data = load_history(d.source, cfg.market, cfg.symbols, d.timeframe, d.start, d.end,
                        exchange=d.exchange, path=d.path, refresh=args.refresh)
    strategy = load_strategy(cfg.strategy.name, cfg.strategy.params)
    res = run_backtest(strategy, data, cfg.cash, cfg.fees, cfg.risk, cfg.market, d.timeframe, cfg.fractional)
    print(res.summary())

    out = Path(args.out) if args.out else cfg.run_dir / "backtest"
    out.mkdir(parents=True, exist_ok=True)
    res.equity.to_frame().join(res.benchmark).to_csv(out / "equity.csv")
    res.trades.to_csv(out / "trades.csv", index=False)
    from .backtest.metrics import clean_for_json
    (out / "metrics.json").write_text(json.dumps(clean_for_json(res.metrics), indent=2))
    print(f"\nresults written to {out}")
    return 0


def cmd_optimize(args) -> int:
    from .backtest.optimize import Settings, grid_search, parse_grid, walk_forward
    from .config import load_config
    from .data import load_history

    cfg = load_config(args.config)
    d = cfg.data
    grid = parse_grid(args.grid)
    if not grid:
        print("give at least one --grid name=values")
        return 2
    data = load_history(d.source, cfg.market, cfg.symbols, d.timeframe, d.start, d.end,
                        exchange=d.exchange, path=d.path)
    settings = Settings(cfg.strategy.name, cfg.cash, cfg.fees, cfg.risk, cfg.market, d.timeframe,
                        cfg.fractional, base_params=cfg.strategy.params)
    out = Path(args.out) if args.out else cfg.run_dir / "optimize"
    out.mkdir(parents=True, exist_ok=True)
    n = 1
    for v in grid.values():
        n *= len(v)
    print(f"{n} combinations of {', '.join(grid)}")

    if args.walk_forward:
        res = walk_forward(settings, grid, data, folds=args.walk_forward, train_frac=args.train_frac,
                           metric=args.metric, min_trades=args.min_trades, workers=args.workers)
        print(res.summary())
        res.folds.to_csv(out / "walk_forward.csv", index=False)
        res.equity.to_frame("equity").to_csv(out / "walk_forward_equity.csv")
    else:
        table = grid_search(settings, grid, data, metric=args.metric, min_trades=args.min_trades,
                            workers=args.workers)
        cols = list(grid) + ["total_return", "cagr", "sharpe", "max_drawdown", "trades", "win_rate"]
        with pd_options():
            print(table[cols].head(args.top).to_string(index=False))
        table.to_csv(out / "grid.csv", index=False)
        print("\nin-sample only: the best row is the one most likely to be overfit. "
              "use --walk-forward to check it on unseen data.")
    print(f"\nresults written to {out}")
    return 0


def pd_options():
    import pandas as pd

    return pd.option_context("display.width", 160, "display.float_format", "{:,.4f}".format)


def cmd_run(args) -> int:
    from .brokers import make_broker
    from .config import load_config
    from .data import make_source
    from .live.runlock import AlreadyRunning
    from .live.runner import LiveRunner
    from .strategies import load_strategy

    cfg = load_config(args.config)
    if cfg.mode == "backtest":
        print("config mode is 'backtest'; use `quantdeck backtest` or set mode to paper/live")
        return 2
    if cfg.mode == "live" and not args.confirm_live:
        print("this config trades real money. re-run with --confirm-live if that's what you want.")
        return 2

    _setup_logging(args.verbose, cfg.run_dir / "run.log")
    d = cfg.data
    broker = make_broker(cfg)
    source = make_source(d.source, cfg.market, d.exchange, d.path)
    strategy = load_strategy(cfg.strategy.name, cfg.strategy.params)
    runner = LiveRunner(cfg, strategy, broker, source)
    try:
        runner.run(max_loops=args.loops)
    except AlreadyRunning as e:
        print(e)
        return 1
    return 0


def cmd_fetch(args) -> int:
    from .data import load_history

    data = load_history(args.source, args.market, args.symbols, args.timeframe, args.start, args.end,
                        exchange=args.exchange, refresh=True)
    for sym, df in data.items():
        print(f"{sym}: {len(df)} bars, {df.index[0]} -> {df.index[-1]}")
    return 0


def cmd_serve(args) -> int:
    import os

    import uvicorn

    from .api.server import create_app

    token = os.environ.get("QUANTDECK_TOKEN") or None
    if args.host not in ("127.0.0.1", "localhost", "::1") and not token:
        print("refusing to listen on a public address without QUANTDECK_TOKEN set")
        return 2
    if token and len(token) < 16:
        print("QUANTDECK_TOKEN should be at least 16 characters")
        return 2
    uvicorn.run(create_app(args.runs_dir, token=token), host=args.host, port=args.port, log_level="info")
    return 0


def cmd_watchdog(args) -> int:
    from .live.watchdog import watch
    from .notify import Notifier, channels_from_env

    channels = channels_from_env()
    if not channels:
        print("no notification channel configured (set TELEGRAM_*, NOTIFY_WEBHOOK_URL or SMTP_* in .env)")
        return 2
    watch(args.runs_dir, Notifier(channels, cooldown=0), stale_after=args.stale, interval=args.interval)
    return 0


def cmd_notify_test(args) -> int:
    from .notify import Notifier, channels_from_env

    channels = channels_from_env()
    if not channels:
        print("no notification channel configured")
        return 2
    n = Notifier(channels, cooldown=0)
    n.notify("start", "quantdeck test message", "if you can read this, notifications work")
    n.flush(15)
    print("sent via " + ", ".join(c.name for c in channels))
    return 0


def cmd_strategies(args) -> int:
    from .strategies import REGISTRY

    for name, cls in REGISTRY.items():
        doc = (cls.__doc__ or "").strip().splitlines()[0] if cls.__doc__ else ""
        print(f"{name:<16}{doc}")
        print(f"{'':<16}params: {cls.params}")
    return 0


def main(argv: list[str] | None = None) -> int:
    from .config import load_dotenv

    load_dotenv()
    p = argparse.ArgumentParser(prog="quantdeck")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backtest", help="run a backtest from a config file")
    b.add_argument("config")
    b.add_argument("--out", help="where to write results (default runs/<name>/backtest)")
    b.add_argument("--refresh", action="store_true", help="ignore cached data and download again")
    b.set_defaults(func=cmd_backtest)

    o = sub.add_parser("optimize", help="grid search, optionally with walk-forward testing")
    o.add_argument("config")
    o.add_argument("--grid", action="append", default=[], metavar="NAME=VALUES",
                   help="e.g. fast=10:50:10 or slow=50,100,200 (repeatable)")
    o.add_argument("--metric", default="sharpe",
                   choices=["sharpe", "sortino", "cagr", "total_return", "calmar", "profit_factor"])
    o.add_argument("--min-trades", type=int, default=5, help="ignore combinations that barely trade")
    o.add_argument("--walk-forward", type=int, metavar="FOLDS", help="number of out-of-sample windows")
    o.add_argument("--train-frac", type=float, default=0.7)
    o.add_argument("--workers", type=int, help="processes (default: cores - 1)")
    o.add_argument("--top", type=int, default=15)
    o.add_argument("--out")
    o.set_defaults(func=cmd_optimize)

    r = sub.add_parser("run", help="paper or live trading")
    r.add_argument("config")
    r.add_argument("--confirm-live", action="store_true", help="required when mode is live")
    r.add_argument("--loops", type=int, help="stop after N polls (handy for testing)")
    r.set_defaults(func=cmd_run)

    f = sub.add_parser("fetch", help="download and cache bars")
    f.add_argument("--source", choices=["ccxt", "alpaca"], required=True)
    f.add_argument("--market", choices=["stock", "crypto"], default="crypto")
    f.add_argument("--exchange", help="ccxt exchange id, e.g. binance")
    f.add_argument("--timeframe", default="1d")
    f.add_argument("--start")
    f.add_argument("--end")
    f.add_argument("symbols", nargs="+")
    f.set_defaults(func=cmd_fetch)

    s = sub.add_parser("serve", help="start the web dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--runs-dir", default="runs")
    s.set_defaults(func=cmd_serve)

    w = sub.add_parser("watchdog", help="alert when a paper/live run stops sending heartbeats")
    w.add_argument("--runs-dir", default="runs")
    w.add_argument("--stale", type=int, default=600, help="seconds without a heartbeat before alerting")
    w.add_argument("--interval", type=int, default=60)
    w.set_defaults(func=cmd_watchdog)

    nt = sub.add_parser("notify-test", help="send a test notification")
    nt.set_defaults(func=cmd_notify_test)

    st = sub.add_parser("strategies", help="list built-in strategies")
    st.set_defaults(func=cmd_strategies)

    args = p.parse_args(argv)
    if args.cmd != "run":
        _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
