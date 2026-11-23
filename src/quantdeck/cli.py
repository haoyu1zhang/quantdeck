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
    import uvicorn

    from .api.server import create_app

    uvicorn.run(create_app(args.runs_dir), host=args.host, port=args.port, log_level="info")
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

    st = sub.add_parser("strategies", help="list built-in strategies")
    st.set_defaults(func=cmd_strategies)

    args = p.parse_args(argv)
    if args.cmd != "run":
        _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
