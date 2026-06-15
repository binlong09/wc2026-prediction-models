"""ISOLATED, hermetic test: backfill-market (recover a missed pre-match price).

Two levels, no network:
  1) the REAL polymarket.price_at filtering — stub only prices_history, assert it
     picks the point nearest the target AND never returns an in-play (post-kickoff)
     point;
  2) the backfill flow — stub price_at, assert de-vig to sum 1, source tag
     'polymarket-backfill', captured_at = the historical read time, insert-once
     (never overwrites a live/manual snapshot), and future fixtures skipped.

The real data/backtest.db is never touched.

Run:  PYTHONPATH=src .venv/bin/python tests/test_backfill.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="wcbt_bftest_"))
os.environ["WCBT_DB"] = str(_TMP / "bf.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import build  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import fetch_market as fm  # noqa: E402
import polymarket as pm  # noqa: E402

assert config.db_path() != config.DB_PATH and "bf.db" in str(config.db_path())

NOW = datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)


def _real_backfill_count():
    """Backfill-row count in the REAL DB, snapshotted to prove the isolated run
    doesn't touch it. Compared before/after — robust even though the real DB
    legitimately holds backfilled prices from actual recovery runs."""
    if not config.DB_PATH.exists():
        return 0
    rc = db.connect(config.DB_PATH)
    try:
        if not rc.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='matches'").fetchone()[0]:
            return 0
        return rc.execute(
            "SELECT COUNT(*) FROM matches WHERE market_source='polymarket-backfill'"
        ).fetchone()[0]
    finally:
        rc.close()


_REAL_BF_BEFORE = _real_backfill_count()  # before any synthetic work


def _ko(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S+00")


def _set_map(conn, mid, kickoff_dt, tokens):
    conn.execute(
        "INSERT OR REPLACE INTO market_map "
        "(match_id, event_slug, market_title, token_w1, token_draw, token_w2, kickoff, resolved_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (mid, "slug-" + mid, mid, tokens[0], tokens[1], tokens[2], _ko(kickoff_dt), "now"),
    )


def test_price_at_filtering():
    """REAL price_at: nearest-to-target, pre-kickoff points only."""
    ko_ts = int(NOW.timestamp())
    target = ko_ts - 3600  # T-60m
    # points: far-early, near-target (the right answer), and an in-play point
    # (after kickoff) with an obviously-wrong price that must be excluded.
    fake = [
        {"t": target - 1800, "p": 0.40},
        {"t": target + 120, "p": 0.55},   # nearest to target -> expected
        {"t": ko_ts + 600, "p": 0.99},    # in-play -> must be excluded
    ]
    pm.prices_history = lambda *a, **k: fake
    res = pm.price_at("tok", target, not_after_ts=ko_ts)
    assert res is not None
    price, used = res
    assert abs(price - 0.55) < 1e-9, f"expected nearest pre-kickoff 0.55, got {price}"
    assert used <= ko_ts, "returned an in-play timestamp!"
    # if every point is after the cutoff, returns None
    pm.prices_history = lambda *a, **k: [{"t": ko_ts + 60, "p": 0.7}]
    assert pm.price_at("tok", target, not_after_ts=ko_ts) is None
    print("Phase 1 PASS: price_at picks nearest pre-kickoff point, excludes in-play")


def main() -> int:
    test_price_at_filtering()

    pm.client = lambda: type("C", (), {"close": lambda self: None})()
    build.build_elo()

    conn = db.connect()
    ids = [r["match_id"] for r in conn.execute(
        "SELECT match_id FROM matches WHERE actual_result IS NULL LIMIT 3")]
    MISS, HAS_LIVE, FUTURE = ids

    _set_map(conn, MISS, NOW - timedelta(hours=2), ("MW", "MD", "ML"))      # past, uncaptured
    _set_map(conn, HAS_LIVE, NOW - timedelta(hours=2), ("LW", "LD", "LL"))  # past, already captured
    _set_map(conn, FUTURE, NOW + timedelta(days=2), ("FW", "FD", "FL"))     # future
    # HAS_LIVE already has a live snapshot that must NOT be overwritten
    conn.execute(
        "UPDATE matches SET market_pW1=0.5, market_pD=0.3, market_pW2=0.2, "
        "market_source='polymarket-auto', market_captured_at='2026-06-16T10:00:00+00:00' "
        "WHERE match_id=?", (HAS_LIVE,))
    conn.commit()
    conn.close()

    # stub price_at: raw prices (sum>1) keyed by token; ts = a pre-kickoff read
    read_ts = int((NOW - timedelta(hours=2) - timedelta(minutes=60)).timestamp())
    prices = {"MW": (0.60, read_ts), "MD": (0.25, read_ts), "ML": (0.20, read_ts)}
    pm.price_at = lambda token, target, not_after_ts=None, **k: prices.get(token)

    res = fm.backfill_market(now=NOW)
    assert res["recovered"] == 1, f"expected 1 recovered, got {res}"
    assert res["future"] == 1, f"expected 1 future-skip, got {res}"

    conn = db.connect()
    miss = conn.execute(
        "SELECT market_pW1, market_pD, market_pW2, market_source, market_captured_at "
        "FROM matches WHERE match_id=?", (MISS,)).fetchone()
    s = miss["market_pW1"] + miss["market_pD"] + miss["market_pW2"]
    assert abs(s - 1.0) < 1e-9, f"de-vig failed, sum={s}"
    assert abs(miss["market_pW1"] - 0.60 / 1.05) < 1e-6, miss["market_pW1"]
    assert miss["market_source"] == "polymarket-backfill", miss["market_source"]
    # captured_at is the HISTORICAL read time, not 'now'
    assert miss["market_captured_at"].startswith("2026-06-16T09:00"), miss["market_captured_at"]

    live = conn.execute(
        "SELECT market_pW1, market_source, market_captured_at FROM matches WHERE match_id=?",
        (HAS_LIVE,)).fetchone()
    assert live["market_source"] == "polymarket-auto" and live["market_pW1"] == 0.5, \
        "backfill overwrote a live snapshot!"

    fut = conn.execute("SELECT market_pW1 FROM matches WHERE match_id=?", (FUTURE,)).fetchone()
    assert fut["market_pW1"] is None, "future fixture should be left to the scheduler"
    conn.close()
    print("Phase 2 PASS: de-vig + backfill tag + historical captured_at + insert-once + future-skip")

    # real DB untouched by the isolated run (before == after; robust even though
    # the real DB legitimately holds backfilled prices from real recovery runs)
    assert _real_backfill_count() == _REAL_BF_BEFORE, (
        f"REAL DB backfill count changed: before={_REAL_BF_BEFORE} after={_real_backfill_count()}"
    )

    print("\ntest_backfill: PASS ✓")
    print(f"  artifacts in: {_TMP}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
