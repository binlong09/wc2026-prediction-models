"""CLI entry points (spec §10).

Commands: fetch · build-elo · load-market · refresh-results · backtest · report
Run with:  PYTHONPATH=src python src/cli.py <command> [args]
       or:  uv run python src/cli.py <command> [args]
"""
from __future__ import annotations

import argparse
import sys

import build
import db
import fetch
import fetch_market as fm
import market
import report
import scheduler
import scorelog
from backtest import run_backtest


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wcbt", description="World Cup 2026 prediction backtest")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("fetch", help="pull history CSV + worldcup.json into data/raw/")
    sub.add_parser("build-elo", help="compute Elo, write elo_pre, populate teams + matches")

    lm = sub.add_parser("load-market", help="load + normalize a matchday market CSV (manual fallback)")
    lm.add_argument("--matchday", "-m", type=int, required=True, choices=(1, 2, 3))

    sub.add_parser("verify-market-map",
                   help="resolve all 72 fixtures to Polymarket markets + print for eyeball check")
    fmp = sub.add_parser("fetch-market",
                         help="auto-snapshot vig-stripped Polymarket prices for a matchday (insert-once)")
    fmp.add_argument("--matchday", "-m", type=int, required=True, choices=(1, 2, 3))

    bf = sub.add_parser("backfill-market",
                        help="recover MISSED pre-match prices from CLOB prices-history (insert-once)")
    bf.add_argument("--target-min", type=int, default=None,
                    help="minutes before kickoff to read (default from config)")

    sd = sub.add_parser("snapshot-due",
                        help="scheduler pass: snapshot fixtures kicking off within the window (cron job)")
    sd.add_argument("--window-min", type=int, default=None, help="override snapshot window (minutes)")
    sd.add_argument("--alert-min", type=int, default=None, help="override pre-kickoff alert horizon (minutes)")
    sd.add_argument("--now", type=str, default=None,
                    help="override 'now' (ISO UTC) for dry-run timing checks")

    sub.add_parser("refresh-results", help="re-fetch + update actual_* (then auto score-log)")
    sub.add_parser("backtest", help="run the train/test loop over rounds with results")
    sub.add_parser("report", help="regenerate static report grid + reliability PNGs")

    sub.add_parser("log-predictions",
                   help="capture immutable pre-match model + market probs for upcoming matches")
    sub.add_parser("score-log", help="retrospectively score logged matches that now have results")
    sub.add_parser("scorelog", help="show the running model-vs-market scorekeeper report")

    args = p.parse_args(argv)

    if args.cmd == "fetch":
        fetch.fetch_all()
    elif args.cmd == "build-elo":
        db.init_db()
        build.build_elo()
    elif args.cmd == "load-market":
        market.load_market(args.matchday)
    elif args.cmd == "verify-market-map":
        fm.verify_market_map()
    elif args.cmd == "fetch-market":
        fm.fetch_market(args.matchday)
    elif args.cmd == "backfill-market":
        fm.backfill_market(target_min=args.target_min)
    elif args.cmd == "snapshot-due":
        now = scheduler.parse_kickoff(args.now) if args.now else None
        if args.now and now is None:
            p.error(f"--now '{args.now}' is not a parseable ISO UTC timestamp")
        return scheduler.snapshot_due(now=now, window_min=args.window_min, alert_min=args.alert_min)
    elif args.cmd == "refresh-results":
        fetch.fetch_all()
        build.refresh_results()
        scorelog.score_log()  # score any logged matches that just finished
    elif args.cmd == "backtest":
        run_backtest()
    elif args.cmd == "report":
        report.make_report()
    elif args.cmd == "log-predictions":
        scorelog.log_predictions()
    elif args.cmd == "score-log":
        scorelog.score_log()
    elif args.cmd == "scorelog":
        scorelog.scorelog_report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
