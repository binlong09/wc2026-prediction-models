"""ISOLATED test: text-state export/import round-trip.

The durable state (market_map, market snapshots, match_log) must survive a full
DB teardown: export to CSV, drop the DB, rebuild the schema + fixtures, import
the CSVs, and confirm the durable rows come back identically. Also confirms the
export is deterministic (re-export is byte-identical -> no spurious commits).

Never touches the real DB or the real data/state/.

Run:  PYTHONPATH=src .venv/bin/python tests/test_state.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="wcbt_statetest_"))
os.environ["WCBT_DB"] = str(_TMP / "st.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config  # noqa: E402

# redirect data dir so state CSVs write under the temp tree, not the real repo
config.DATA = _TMP / "data"
import state  # noqa: E402
state.STATE_DIR = config.DATA / "state"
state.MARKET_MAP_CSV = state.STATE_DIR / "market_map.csv"
state.SNAPSHOTS_CSV = state.STATE_DIR / "market_snapshots.csv"
state.MATCH_LOG_CSV = state.STATE_DIR / "match_log.csv"

import build  # noqa: E402
import db  # noqa: E402

assert config.db_path() != config.DB_PATH and "st.db" in str(config.db_path())


def _snapshot(conn):
    """Durable state as comparable tuples."""
    mm = conn.execute(
        "SELECT match_id, event_slug, token_w1, token_draw, token_w2, kickoff FROM market_map ORDER BY match_id"
    ).fetchall()
    sn = conn.execute(
        "SELECT match_id, round(market_pW1,6), round(market_pD,6), round(market_pW2,6), "
        "market_source, market_captured_at FROM matches WHERE market_pW1 IS NOT NULL ORDER BY match_id"
    ).fetchall()
    ml = conn.execute(
        "SELECT version, match_id, round(model_pW1,6), round(market_pW1,6), captured_at "
        "FROM match_log ORDER BY match_id, version"
    ).fetchall()
    return [tuple(r) for r in mm], [tuple(r) for r in sn], [tuple(r) for r in ml]


def main() -> int:
    build.build_elo()
    conn = db.connect()
    ids = [r["match_id"] for r in conn.execute(
        "SELECT match_id FROM matches WHERE actual_result IS NULL LIMIT 2")]
    A, B = ids

    # seed durable state: a map row, a captured price, and two match_log rows
    conn.execute(
        "INSERT INTO market_map (match_id,event_slug,market_title,token_w1,token_draw,token_w2,kickoff,resolved_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (A, "fifwc-a", "A", "tA1", "tAd", "tA2", "2026-06-15 12:00:00+00", "2026-06-13T00:00:00+00:00"))
    conn.execute(
        "UPDATE matches SET market_pW1=0.512345, market_pD=0.298765, market_pW2=0.18889, "
        "market_source='polymarket-backfill', market_captured_at='2026-06-15T11:00:00+00:00' WHERE match_id=?", (A,))
    for ver in ("v1", "v2"):
        conn.execute(
            "INSERT INTO match_log (version,match_id,model_pW1,model_pD,model_pW2,"
            "market_pW1,market_pD,market_pW2,captured_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (ver, A, 0.55, 0.25, 0.2, 0.512345, 0.298765, 0.18889, "2026-06-15T11:00:00+00:00"))
    conn.commit()
    before = _snapshot(conn)
    conn.close()

    # export -> CSVs
    state.export_state()
    assert state.MARKET_MAP_CSV.exists() and state.SNAPSHOTS_CSV.exists() and state.MATCH_LOG_CSV.exists()
    snap_text = state.SNAPSHOTS_CSV.read_text()
    log_text = state.MATCH_LOG_CSV.read_text()

    # determinism: re-export is byte-identical
    state.export_state()
    assert state.SNAPSHOTS_CSV.read_text() == snap_text, "snapshots export not deterministic"
    assert state.MATCH_LOG_CSV.read_text() == log_text, "match_log export not deterministic"
    print("export determinism: re-export byte-identical ✓")

    # full teardown: drop the DB entirely
    Path(config.db_path()).unlink()

    # rebuild schema + fixtures, then import the committed CSVs
    build.build_elo()
    state.import_state()

    conn = db.connect()
    after = _snapshot(conn)
    # the captured price restored exactly
    px = conn.execute(
        "SELECT round(market_pW1,6) a, market_source, market_captured_at FROM matches WHERE match_id=?", (A,)
    ).fetchone()
    n_log = conn.execute("SELECT COUNT(*) FROM match_log WHERE match_id=?", (A,)).fetchone()[0]
    # a match with no state stays clean
    clean = conn.execute("SELECT market_pW1 FROM matches WHERE match_id=?", (B,)).fetchone()["market_pW1"]
    conn.close()

    assert after == before, "durable state did not round-trip identically"
    assert px["a"] == 0.512345 and px["market_source"] == "polymarket-backfill", px
    assert px["market_captured_at"] == "2026-06-15T11:00:00+00:00", "captured_at not preserved"
    assert n_log == 2, f"expected 2 match_log rows restored, got {n_log}"
    assert clean is None, "a match with no persisted state should stay clean"

    # real repo's data/state/ untouched
    assert str(state.STATE_DIR).startswith(str(_TMP)), "state dir not redirected!"

    print("round-trip: export -> drop DB -> rebuild -> import restored durable state identically ✓")
    print("\ntest_state: PASS ✓")
    print(f"  artifacts in: {_TMP}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
