"""ISOLATED test: the model-vs-market scorekeeper (Task 2).

Exercises the full scorekeeper path on a throwaway DB:
  log pre-match -> simulate results -> score retrospectively -> report,
and asserts the hard boundaries:
  * a logged prediction is IMMUTABLE (probs + captured_at never change, even
    after the result is known and scored);
  * re-running the logger never overwrites an existing row (INSERT OR IGNORE);
  * scoring fills model AND market scores where a market price was captured;
  * the real data/backtest.db is never touched.

Run:  PYTHONPATH=src .venv/bin/python tests/test_scorelog.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="wcbt_sltest_"))
os.environ["WCBT_DB"] = str(_TMP / "sl.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config  # noqa: E402

# isolate market input dir + report output dir
_SRC_EXAMPLE = config.MARKET / "matchday_1.example.csv"
config.MARKET = _TMP / "market"
config.MARKET.mkdir(parents=True, exist_ok=True)
config.REPORT = _TMP / "report"
shutil.copy(_SRC_EXAMPLE, config.MARKET / "matchday_1.csv")

import build  # noqa: E402
import db  # noqa: E402
import market  # noqa: E402
import scorelog  # noqa: E402

assert config.db_path() != config.DB_PATH and "sl.db" in str(config.db_path())

_GOALS = {"team1_win": (2, 0), "draw": (1, 1), "team2_win": (0, 2)}


def main() -> int:
    build.build_elo()
    market.load_market(1)  # vig-stripped market for the G–L round-1 fixtures

    # 1) Capture pre-match predictions for upcoming matches.
    n_logged = scorelog.log_predictions()
    assert n_logged > 0, "nothing logged"

    conn = db.connect()
    # rows that carry a market price (the example covers G–L round 1)
    mkt_rows = conn.execute(
        "SELECT version, match_id, model_pW1, model_pD, model_pW2, captured_at "
        "FROM match_log WHERE market_pW1 IS NOT NULL ORDER BY match_id"
    ).fetchall()
    assert mkt_rows, "no logged rows carry a market price"
    both_versions = {r["version"] for r in mkt_rows}
    assert both_versions == {"v1", "v2"}, f"expected v1+v2 logged, got {both_versions}"

    # snapshot probs + captured_at to prove immutability later
    snap = {
        (r["version"], r["match_id"]): (
            r["model_pW1"], r["model_pD"], r["model_pW2"], r["captured_at"]
        )
        for r in mkt_rows
    }

    # 2) Simulate results for the market-bearing matches (directly on `matches`).
    sim_ids = sorted({r["match_id"] for r in mkt_rows})
    for i, mid in enumerate(sim_ids):
        res = ["team1_win", "draw", "team2_win"][i % 3]
        g1, g2 = _GOALS[res]
        conn.execute(
            "UPDATE matches SET actual_result=?, actual_goals1=?, actual_goals2=? WHERE match_id=?",
            (res, g1, g2, mid),
        )
    conn.commit()
    conn.close()

    # 3) Score retrospectively.
    n_scored = scorelog.score_log()
    assert n_scored >= len(sim_ids) * 2, f"expected ≥{len(sim_ids)*2} scored, got {n_scored}"

    conn = db.connect()
    # 4a) IMMUTABILITY: probs + captured_at unchanged after scoring.
    for (ver, mid), (p1, pdd, p2, cap) in snap.items():
        r = conn.execute(
            "SELECT model_pW1, model_pD, model_pW2, captured_at, "
            "brier_model, brier_market FROM match_log WHERE version=? AND match_id=?",
            (ver, mid),
        ).fetchone()
        assert (r["model_pW1"], r["model_pD"], r["model_pW2"]) == (p1, pdd, p2), \
            f"probs mutated for {ver}/{mid}!"
        assert r["captured_at"] == cap, "captured_at mutated!"
        assert r["brier_model"] is not None, "model not scored"
        assert r["brier_market"] is not None, "market not scored despite market price present"
    conn.close()

    # 4b) Re-running the logger must NOT overwrite (now-played rows skipped,
    #     still-unplayed already-logged rows ignored).
    n_relog = scorelog.log_predictions()
    conn = db.connect()
    for (ver, mid), (p1, pdd, p2, cap) in snap.items():
        r = conn.execute(
            "SELECT model_pW1, captured_at FROM match_log WHERE version=? AND match_id=?",
            (ver, mid),
        ).fetchone()
        assert r["model_pW1"] == p1 and r["captured_at"] == cap, \
            "re-log overwrote an immutable row!"
    conn.close()
    print(f"  re-log captured {n_relog} new rows (must not touch the {len(snap)} scored rows)")

    # 5) Report renders.
    scorelog.scorelog_report()
    assert (config.REPORT / "scorelog.html").exists(), "scorelog.html missing"
    assert (config.REPORT / "scorelog.png").exists(), "scorelog.png missing"

    # 6) Real DB untouched.
    if config.DB_PATH.exists():
        rc = db.connect(config.DB_PATH)
        has_log = rc.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='match_log'"
        ).fetchone()[0]
        real_logged = (
            rc.execute("SELECT COUNT(*) FROM match_log").fetchone()[0] if has_log else 0
        )
        rc.close()
        assert real_logged == 0, "REAL DB match_log was contaminated!"

    print("\ntest_scorelog: PASS ✓")
    print(f"  logged={n_logged}, scored={n_scored}, market-bearing immutable rows={len(snap)}")
    print(f"  artifacts in: {_TMP}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
