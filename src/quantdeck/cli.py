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
    app = create_app(args.runs_dir, token=token, news_db=args.news_db,
                     news_config=args.news_config or os.environ.get("QUANTDECK_NEWS_CONFIG"))
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
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


def _news_label(label: str, lang: str) -> str:
    from .news import LABELS_ZH

    return LABELS_ZH[label] if lang == "zh" else label.replace("_", " ")


def print_verdicts(verdicts: list[dict], lang: str = "en", drivers: int = 1) -> None:
    if not verdicts:
        print("no verdicts yet")
        return
    print(f"{'symbol':<16}{'verdict':<16}{'score':>7}{'conf':>7}{'items':>7}")
    for v in sorted(verdicts, key=lambda v: (not v["symbol"].startswith("MARKET:"), -abs(v["score"]))):
        print(f"{v['symbol']:<16}{_news_label(v['label'], lang):<16}{v['score']:>+7.2f}"
              f"{v['confidence']:>7.0%}{v['items']:>7}")
        for d in v["drivers"][:drivers]:
            arrow = "+" if d["direction"] > 0 else "-" if d["direction"] < 0 else "="
            print(f"{'':<4}{arrow} [{d['source']}] {d['title'][:100]}")


def _news_notifier(cfg):
    from .notify import Notifier, channels_from_env

    channels = channels_from_env()
    return Notifier(channels, events=["news"], prefix="[news] ", cooldown=cfg.alerts.cooldown) if channels else None


def cmd_news_fetch(args) -> int:
    from .news import NewsPipeline, load_news_config

    cfg = load_news_config(args.config)
    pipe = NewsPipeline(cfg, notifier=_news_notifier(cfg) if args.alerts else None)
    print(f"scorer: {pipe.scorer.name}")
    rep = pipe.run_once(force=True)
    for name in sorted(set(rep.fetched) | set(rep.errors)):
        print(f"  {name:<20}{rep.errors[name] if name in rep.errors else str(rep.fetched[name]) + ' items'}")
    print(f"{rep.new_items} new, {rep.scored} scored in {rep.seconds:.1f}s\n")
    print_verdicts([v.as_dict() for v in rep.verdicts], args.lang or cfg.alerts.lang, drivers=args.drivers)
    if pipe.notifier:
        pipe.notifier.flush(10)
    return 0


def cmd_news_watch(args) -> int:
    import signal

    from .news import NewsPipeline, load_news_config

    cfg = load_news_config(args.config)
    if args.interval:
        cfg.interval = args.interval
    _setup_logging(args.verbose, cfg.db_path.parent / "news.log")
    log = logging.getLogger("quantdeck.news")
    pipe = NewsPipeline(cfg, notifier=_news_notifier(cfg))
    signal.signal(signal.SIGINT, pipe.stop)
    signal.signal(signal.SIGTERM, pipe.stop)
    log.info("watching %d sources, scorer %s, every %ds", len(pipe.sources), pipe.scorer.name, cfg.interval)

    def report(rep):
        moved = ", ".join(f"{v.symbol} {v.label} {v.score:+.2f}" for v in rep.verdicts if v.label != "neutral")
        log.info("cycle: %d fetched, %d new, %d scored, %d errors, %.1fs%s",
                 sum(rep.fetched.values()), rep.new_items, rep.scored, len(rep.errors), rep.seconds,
                 f" | {moved}" if moved else "")
        for a in rep.alerts:
            log.info("alert: %s", a)

    pipe.watch(max_cycles=args.cycles, on_cycle=report)
    if pipe.notifier:
        pipe.notifier.flush(5)
    return 0


def cmd_news_report(args) -> int:
    from .news import NewsStore, load_news_config

    cfg = load_news_config(args.config)
    if not cfg.db_path.exists():
        print(f"{cfg.db_path} doesn't exist yet; run `quantdeck news fetch {args.config}` first")
        return 1
    st = NewsStore(cfg.db_path, readonly=True)
    try:
        if args.symbol:
            v = st.latest_verdict(args.symbol.upper())
            if v is None:
                print(f"no verdict for {args.symbol}")
                return 1
            print_verdicts([v], args.lang or cfg.alerts.lang, drivers=0)
            print()
            for it in st.items(args.symbol.upper(), limit=args.limit):
                d = it.get("direction")
                arrow = "  " if d is None else "+" if d > 0.05 else "-" if d < -0.05 else "="
                print(f"{arrow} {it['published_at'][:16]} [{it['source']}] {it['title'][:110]}")
                if it.get("reason"):
                    print(f"     {it['reason'][:140]}")
        else:
            print_verdicts(st.latest_verdicts(), args.lang or cfg.alerts.lang, drivers=args.drivers)
    finally:
        st.close()
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
    s.add_argument("--news-db", default="runs/news/news.db", help="news database for the News tab")
    s.add_argument("--news-config", help="news config; enables the refresh button and manual adds in the News tab")
    s.set_defaults(func=cmd_serve)

    w = sub.add_parser("watchdog", help="alert when a paper/live run stops sending heartbeats")
    w.add_argument("--runs-dir", default="runs")
    w.add_argument("--stale", type=int, default=600, help="seconds without a heartbeat before alerting")
    w.add_argument("--interval", type=int, default=60)
    w.set_defaults(func=cmd_watchdog)

    nt = sub.add_parser("notify-test", help="send a test notification")
    nt.set_defaults(func=cmd_notify_test)

    nw = sub.add_parser("news", help="collect news and score it bullish / bearish")
    nsub = nw.add_subparsers(dest="news_cmd", required=True)
    nf = nsub.add_parser("fetch", help="fetch every source once, score, print verdicts")
    nf.add_argument("config")
    nf.add_argument("--lang", choices=["en", "zh"])
    nf.add_argument("--drivers", type=int, default=2, help="top headlines to show per symbol")
    nf.add_argument("--alerts", action="store_true", help="also send alerts for strong verdicts")
    nf.set_defaults(func=cmd_news_fetch)
    nwt = nsub.add_parser("watch", help="keep collecting and scoring, send alerts")
    nwt.add_argument("config")
    nwt.add_argument("--interval", type=int, help="seconds between cycles (overrides the config)")
    nwt.add_argument("--cycles", type=int, help="stop after N cycles")
    nwt.set_defaults(func=cmd_news_watch)
    nr = nsub.add_parser("report", help="print the latest verdicts, or the news behind one symbol")
    nr.add_argument("config")
    nr.add_argument("--symbol", help="e.g. NVDA, BTC, MARKET:crypto")
    nr.add_argument("--limit", type=int, default=20)
    nr.add_argument("--lang", choices=["en", "zh"])
    nr.add_argument("--drivers", type=int, default=2)
    nr.set_defaults(func=cmd_news_report)

    st = sub.add_parser("strategies", help="list built-in strategies")
    st.set_defaults(func=cmd_strategies)

    args = p.parse_args(argv)
    if args.cmd != "run" and getattr(args, "news_cmd", None) != "watch":
        _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
