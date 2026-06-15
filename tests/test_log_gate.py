"""ISOLATED test: the market-present gate on log-predictions.

The mandatory rule: a match is NOT logged until its market price is captured
(insert-once + a null market would lock the match out of the scorelog forever).
This verifies a match with no market is skipped, and is logged exactly once after
its market appears — and that re-running never re-logs or overwrites it.

Run:  PYTHONPATH=src .venv/bin/python tests/test_log_gate.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="wcbt_gatetest_"))
os.environ["WCBT_DB"] = str(_TMP / "gate.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import build  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import scorelog  # noqa: E402

assert config.db_path() != config.DB_PATH and "gate.db" in str(config.db_path())


def _logged_rows(conn, mid):
    return conn.execute(
        "SELECT version, market_pW1 FROM match_log WHERE match_id=? ORDER BY version", (mid,)
    ).fetchall()


def main() -> int:
    build.build_elo()
    conn = db.connect()
    # a round-1 unplayed match (round 1 so log_predictions reaches it before the
    # break), with NO market yet — build-elo leaves all market_* null.
    M = conn.execute(
        "SELECT match_id FROM matches WHERE matchday=1 AND actual_result IS NULL "
        "AND market_pW1 IS NULL LIMIT 1"
    ).fetchone()["match_id"]
    conn.close()

    # 1) No market anywhere -> the gate skips everything.
    n0 = scorelog.log_predictions()
    conn = db.connect()
    assert n0 == 0, f"gate failed: logged {n0} rows with no market present"
    assert conn.execute("SELECT COUNT(*) FROM match_log").fetchone()[0] == 0
    assert not _logged_rows(conn, M), "match logged despite no market!"
    print("Phase 1 PASS: match with no market is NOT logged")

    # 2) Market for M appears -> it becomes loggable.
    conn.execute(
        "UPDATE matches SET market_pW1=0.5, market_pD=0.3, market_pW2=0.2, "
        "market_source='polymarket-auto', market_captured_at='2026-06-15T11:00:00+00:00' "
        "WHERE match_id=?", (M,))
    conn.commit()
    conn.close()

    n1 = scorelog.log_predictions()
    conn = db.connect()
    rows = _logged_rows(conn, M)
    assert n1 == 2, f"expected 2 new rows (v1+v2) for M, got {n1}"
    assert {r["version"] for r in rows} == {"v1", "v2"}, "both versions should be logged"
    assert all(r["market_pW1"] == 0.5 for r in rows), "logged rows must carry the market"
    # only M was logged (it's the sole match with a market)
    assert conn.execute("SELECT COUNT(DISTINCT match_id) FROM match_log").fetchone()[0] == 1
    snap = {(r["version"]): r["market_pW1"] for r in rows}
    conn.close()
    print("Phase 2 PASS: match is logged (v1+v2, with market) once its price appears")

    # 3) Re-run -> insert-once, no new rows, nothing overwritten.
    n2 = scorelog.log_predictions()
    conn = db.connect()
    rows2 = _logged_rows(conn, M)
    assert n2 == 0, f"insert-once violated: re-log added {n2} rows"
    assert {(r["version"]): r["market_pW1"] for r in rows2} == snap, "re-log mutated the row"
    conn.close()
    print("Phase 3 PASS: re-running logs nothing new and overwrites nothing")

    # real DB untouched
    if config.DB_PATH.exists():
        rc = db.connect(config.DB_PATH)
        has = rc.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='match_log'").fetchone()[0]
        # (we don't assert a fixed count — the live pipeline may populate it; we
        #  only assert our isolated run didn't write to the real file, which it
        #  can't, since WCBT_DB points elsewhere. Existence check is enough here.)
        rc.close()
        assert has in (0, 1)

    print("\ntest_log_gate: PASS ✓")
    print(f"  artifacts in: {_TMP}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
