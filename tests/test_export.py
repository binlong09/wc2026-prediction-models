"""ISOLATED test: export-companion (companion-spec §3).

Builds a temp DB with a populated match_log (simulating log-predictions +
score-log) and verifies companion.json matches the contract: per-match
predictions come from match_log, status/result/market_source from matches,
scored when final, and a cumulative scorelog series. Never touches the real DB or
the real report/.

Run:  PYTHONPATH=src .venv/bin/python tests/test_export.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="wcbt_exptest_"))
os.environ["WCBT_DB"] = str(_TMP / "exp.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config  # noqa: E402
config.REPORT = _TMP / "report"  # never write the real report/

import build  # noqa: E402
import db  # noqa: E402
import export  # noqa: E402

assert config.db_path() != config.DB_PATH and "exp.db" in str(config.db_path())

NOW = datetime(2026, 6, 15, 14, 0, 0, tzinfo=timezone.utc)


def main() -> int:
    build.build_elo()
    conn = db.connect()
    # a played match to make FINAL, and an unplayed one left UPCOMING
    final_id = conn.execute(
        "SELECT match_id FROM matches WHERE actual_result IS NULL LIMIT 1").fetchone()["match_id"]
    upcoming_id = conn.execute(
        "SELECT match_id FROM matches WHERE actual_result IS NULL AND match_id<>? LIMIT 1",
        (final_id,)).fetchone()["match_id"]

    # map + kickoff (12:00, before NOW) + final result + market on the match
    conn.execute(
        "INSERT INTO market_map (match_id,event_slug,market_title,token_w1,token_draw,token_w2,kickoff,resolved_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (final_id, "fifwc-test-2026-06-15", "Test", "a", "b", "c", "2026-06-15 12:00:00+00", "now"))
    conn.execute(
        "UPDATE matches SET actual_result='team1_win', actual_goals1=2, actual_goals2=0, "
        "market_pW1=0.5, market_pD=0.3, market_pW2=0.2, market_source='polymarket-auto', "
        "market_captured_at='2026-06-15T11:00:00+00:00' WHERE match_id=?", (final_id,))
    # match_log: v1 + v2 model preds + market snapshot + per-match scores
    for ver, p1 in (("v1", 0.55), ("v2", 0.58)):
        conn.execute(
            "INSERT INTO match_log (version,match_id,model_pW1,model_pD,model_pW2,"
            "market_pW1,market_pD,market_pW2,captured_at,actual_result,"
            "brier_model,logloss_model,brier_market,logloss_market) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ver, final_id, p1, 0.25, 1 - p1 - 0.25, 0.5, 0.3, 0.2, "2026-06-15T11:00:00+00:00",
             "team1_win", 0.3, 0.6, 0.35, 0.7))
    conn.commit()
    conn.close()

    out = export.export_companion(now=NOW)

    by_id = {m["match_id"]: m for m in out["matches"]}
    assert len(out["matches"]) == 72

    fm = by_id["fifwc-test-2026-06-15"]                 # public id = event slug
    assert fm["status"] == "final" and fm["actual_result"] == "team1_win"
    assert fm["kickoff_utc"] == "2026-06-15T12:00:00Z"
    assert fm["market_source"] == "auto"               # mapped from polymarket-auto
    assert fm["captured_at"] == "2026-06-15T11:00:00Z"
    assert fm["predictions"]["v1"] == [0.55, 0.25, 0.2]
    assert fm["predictions"]["v2"] == [0.58, 0.25, 0.17]
    assert fm["predictions"]["market"] == [0.5, 0.3, 0.2]
    assert fm["scored"]["v1"] == {"brier": 0.3, "logloss": 0.6}
    assert fm["scored"]["market"] == {"brier": 0.35, "logloss": 0.7}
    print("FINAL match: slug id, status, kickoff_utc Z, mapped source, preds, scored — OK")

    um = by_id[upcoming_id]                             # no market_map -> internal id
    assert um["status"] == "upcoming" and um["predictions"] == {} and um["market_source"] is None
    print("UPCOMING match: internal id, empty preds, null market — OK")

    pts = out["scorelog"]["points"]
    assert len(pts) == 1 and pts[0]["n"] == 1
    # model brier 0.3 vs market 0.35 -> skill 1 - 0.3/0.35 > 0
    assert pts[0]["cum_skill_brier"]["v1"] == round(1 - 0.3 / 0.35, 4)
    assert "v2" in pts[0]["cum_skill_brier"]
    print(f"scorelog: 1 point, cum_skill_brier={pts[0]['cum_skill_brier']} — OK")

    # JSON is valid + written to the redirected (temp) report dir, not the real one
    assert (config.REPORT / "companion.json").exists()
    json.loads((config.REPORT / "companion.json").read_text())

    print("\ntest_export: PASS ✓")
    print(f"  artifacts in: {_TMP}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
