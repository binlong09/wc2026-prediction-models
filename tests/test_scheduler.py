"""ISOLATED, hermetic test: the snapshot scheduler (Stage 2).

Stubs the Polymarket price API (no network). Verifies, against a controlled set
of fixtures with crafted kickoff times:
  * window selection — only fixtures kicking off within the window are captured;
  * HARD past-kickoff guard — a fixture already kicked off is never snapshotted;
  * TZ-ARTIFACT correctness — timing keys off the stored kickoff, NOT the slug/
    local date: a fixture whose date is the day before its kickoff snapshots on
    the kickoff day, and a far-future fixture dated "today" is NOT snapshotted;
  * insert-once — a second pass captures nothing and never overwrites;
  * ALERT — an uncaptured fixture within the alert horizon (or just past kickoff)
    makes the run exit non-zero;
  * a clean pass with nothing imminent exits zero;
  * the real data/backtest.db is never touched.

Run:  PYTHONPATH=src .venv/bin/python tests/test_scheduler.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="wcbt_schedtest_"))
os.environ["WCBT_DB"] = str(_TMP / "sched.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import build  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import polymarket as pm  # noqa: E402
import scheduler  # noqa: E402

assert config.db_path() != config.DB_PATH and "sched.db" in str(config.db_path())

NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def _ko(dt: datetime) -> str:
    # Polymarket's exact gameStartTime format: 'YYYY-MM-DD HH:MM:SS+00'
    return dt.strftime("%Y-%m-%d %H:%M:%S+00")


# midpoints keyed by token; F_FAIL tokens return None to force a capture failure
_MIDS = {"W": 0.50, "D": 0.30, "L": 0.25}
def _midpoint(token, cli=None):
    if token.startswith("FAIL"):
        return None
    return _MIDS[token[-1]]


def _set_map(conn, mid, kickoff_dt, tokens=("W", "D", "L"), date=None):
    conn.execute(
        "INSERT OR REPLACE INTO market_map "
        "(match_id, event_slug, market_title, token_w1, token_draw, token_w2, kickoff, resolved_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (mid, "slug-" + mid, mid, tokens[0], tokens[1], tokens[2], _ko(kickoff_dt), "now"),
    )
    if date is not None:
        conn.execute("UPDATE matches SET date=? WHERE match_id=?", (date, mid))


def _captured(conn, mid) -> bool:
    r = conn.execute(
        "SELECT market_source FROM matches WHERE match_id=?", (mid,)
    ).fetchone()
    return r["market_source"] == "polymarket-auto"


def main() -> int:
    pm.client = lambda: type("C", (), {"close": lambda self: None})()
    pm.midpoint = _midpoint

    build.build_elo()
    conn = db.connect()
    ids = [r["match_id"] for r in conn.execute(
        "SELECT match_id FROM matches WHERE actual_result IS NULL LIMIT 6")]
    DUE, NOTYET, PAST, FAIL, TZ, FAR = ids

    # parse_kickoff sanity on the real format
    assert scheduler.parse_kickoff("2026-06-15 19:00:00+00") == datetime(
        2026, 6, 15, 19, 0, tzinfo=timezone.utc), "kickoff parse wrong"

    # --- Phase 1: crafted fixtures (expects alert -> exit 1) ---
    _set_map(conn, DUE,    NOW + timedelta(minutes=60))                    # in window -> capture
    _set_map(conn, NOTYET, NOW + timedelta(minutes=180))                  # outside window
    _set_map(conn, PAST,   NOW - timedelta(minutes=30))                   # kicked off -> guard + alert
    _set_map(conn, FAIL,   NOW + timedelta(minutes=20),                   # imminent + capture fails
             tokens=("FAILW", "FAILD", "FAILL"))
    # TZ artifact: dated the day BEFORE kickoff -> must still capture (keys off ko)
    _set_map(conn, TZ,     NOW + timedelta(minutes=30), date="2026-06-14")
    # far future but dated "today" -> must NOT capture (date must be ignored)
    _set_map(conn, FAR,    NOW + timedelta(days=5), date="2026-06-15")
    conn.commit()

    rc = scheduler.snapshot_due(now=NOW)
    assert rc == 1, f"expected alert exit 1, got {rc}"

    assert _captured(conn, DUE), "DUE fixture (T-60m) should be captured"
    assert _captured(conn, TZ), "TZ fixture should capture on kickoff day despite earlier date"
    assert not _captured(conn, NOTYET), "NOTYET (T-180m) must be outside window"
    assert not _captured(conn, PAST), "PAST fixture must hit the hard guard"
    assert not _captured(conn, FAIL), "FAIL fixture has no price -> not captured"
    assert not _captured(conn, FAR), "FAR-future fixture dated today must NOT be captured"
    print("Phase 1 PASS: window + past-kickoff guard + TZ-artifact correct; alert -> exit 1")

    # --- insert-once: second pass captures nothing new, doesn't overwrite ---
    ts_before = conn.execute(
        "SELECT market_captured_at FROM matches WHERE match_id=?", (DUE,)
    ).fetchone()["market_captured_at"]
    p_before = conn.execute(
        "SELECT market_pW1 FROM matches WHERE match_id=?", (DUE,)
    ).fetchone()["market_pW1"]
    scheduler.snapshot_due(now=NOW)
    row = conn.execute(
        "SELECT market_captured_at, market_pW1 FROM matches WHERE match_id=?", (DUE,)
    ).fetchone()
    assert row["market_captured_at"] == ts_before and row["market_pW1"] == p_before, \
        "insert-once violated: existing snapshot changed on re-run"
    print("Phase 2 PASS: insert-once — re-run never overwrites an existing snapshot")

    # --- Phase 3: clean pass with nothing imminent -> exit 0 ---
    conn.execute("DELETE FROM market_map")
    conn.execute("UPDATE matches SET market_pW1=NULL, market_pD=NULL, market_pW2=NULL, "
                 "market_source=NULL, market_captured_at=NULL")
    _set_map(conn, DUE, NOW + timedelta(minutes=60))     # only a clean, in-window fixture
    _set_map(conn, FAR, NOW + timedelta(days=5))         # far future, no alert
    conn.commit()
    rc_clean = scheduler.snapshot_due(now=NOW)
    assert rc_clean == 0, f"clean pass should exit 0, got {rc_clean}"
    assert _captured(conn, DUE) and not _captured(conn, FAR)
    print("Phase 3 PASS: clean pass exits 0, captures only the in-window fixture")
    conn.close()

    # real DB untouched
    if config.DB_PATH.exists():
        rc2 = db.connect(config.DB_PATH)
        has = rc2.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='matches'").fetchone()[0]
        n_auto = rc2.execute(
            "SELECT COUNT(*) FROM matches WHERE market_source='polymarket-auto'"
        ).fetchone()[0] if has else 0
        rc2.close()
        assert n_auto == 0, "real DB got auto snapshots — contamination!"

    print("\ntest_scheduler: PASS ✓")
    print(f"  artifacts in: {_TMP}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
