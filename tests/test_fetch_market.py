"""ISOLATED, hermetic test: market_map + fetch-market de-vig + insert-once.

Stubs the Polymarket client (no network) so this is deterministic. Verifies:
  * verify-market-map stores the resolved tokens in market_map;
  * fetch-market de-vigs the three midpoints to sum 1 and tags source/captured_at;
  * fetch-market is INSERT-ONCE — a second run never overwrites;
  * load-market (manual fallback) can still override an auto snapshot;
  * the real data/backtest.db is never touched.

Run:  PYTHONPATH=src .venv/bin/python tests/test_fetch_market.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="wcbt_fmtest_"))
os.environ["WCBT_DB"] = str(_TMP / "fm.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import build  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import fetch_market as fm  # noqa: E402
import polymarket as pm  # noqa: E402

assert config.db_path() != config.DB_PATH and "fm.db" in str(config.db_path())

# Two fixtures with hand-set midpoints (raw, sum > 1 to exercise de-vig).
# token ids are arbitrary strings; the stub maps them to fixed midpoints.
_MIDS = {
    "BEL_W": 0.60, "BEL_D": 0.25, "BEL_W2": 0.17,   # sum 1.02
    "SPA_W": 0.90, "SPA_D": 0.07, "SPA_W2": 0.05,   # sum 1.02
}


def _fake_index(_cli, valid_ids=None):
    idx = {
        frozenset({"Belgium", "Egypt"}): [{
            "slug": "fifwc-bel-egy-2026-06-15", "title": "Belgium vs. Egypt",
            "date": "2026-06-15", "kickoff": "2026-06-15 19:00:00+00",
            "team_tokens": {"Belgium": "BEL_W", "Egypt": "BEL_W2"},
            "draw_token": "BEL_D", "raw_names": ["Belgium", "Egypt"],
        }],
        frozenset({"Spain", "Cape Verde"}): [{
            "slug": "fifwc-esp-cvi-2026-06-15", "title": "Spain vs. Cabo Verde",
            "date": "2026-06-15", "kickoff": "2026-06-15 16:00:00+00",
            "team_tokens": {"Spain": "SPA_W", "Cape Verde": "SPA_W2"},
            "draw_token": "SPA_D", "raw_names": ["Spain", "Cabo Verde"],
        }],
    }
    return idx, []


def main() -> int:
    # stub the network surface
    class _DummyClient:
        def close(self):
            pass

    pm.build_event_index = _fake_index
    pm.client = lambda: _DummyClient()
    pm.midpoint = lambda token, cli=None: _MIDS.get(token)

    build.build_elo()
    fm.verify_market_map()

    conn = db.connect()
    n_map = conn.execute("SELECT COUNT(*) FROM market_map").fetchone()[0]
    assert n_map == 2, f"expected 2 mapped fixtures, got {n_map}"

    bel = conn.execute(
        "SELECT match_id FROM matches WHERE team1_id='Belgium' AND team2_id='Egypt'"
    ).fetchone()["match_id"]
    bel_md = next(r["matchday"] for r in conn.execute(
        "SELECT matchday FROM matches WHERE match_id=?", (bel,)))
    conn.close()

    # fetch the matchday that contains Belgium vs Egypt
    res = fm.fetch_market(bel_md)
    assert res["captured"] >= 1, "nothing captured"

    conn = db.connect()
    row = conn.execute(
        "SELECT market_pW1, market_pD, market_pW2, market_source, market_captured_at "
        "FROM matches WHERE match_id=?", (bel,)
    ).fetchone()
    s = row["market_pW1"] + row["market_pD"] + row["market_pW2"]
    assert abs(s - 1.0) < 1e-9, f"de-vig failed, sum={s}"
    # de-vigged Belgium win = 0.60 / 1.02
    assert abs(row["market_pW1"] - 0.60 / 1.02) < 1e-6, row["market_pW1"]
    assert row["market_source"] == "polymarket-auto", row["market_source"]
    assert row["market_captured_at"] is not None
    snap = (row["market_pW1"], row["market_pD"], row["market_pW2"], row["market_captured_at"])
    conn.close()

    # INSERT-ONCE: second run captures nothing, values unchanged
    res2 = fm.fetch_market(bel_md)
    assert res2["captured"] == 0, f"insert-once violated: captured={res2['captured']}"
    conn = db.connect()
    row2 = conn.execute(
        "SELECT market_pW1, market_pD, market_pW2, market_captured_at "
        "FROM matches WHERE match_id=?", (bel,)
    ).fetchone()
    assert (row2["market_pW1"], row2["market_pD"], row2["market_pW2"],
            row2["market_captured_at"]) == snap, "auto snapshot was overwritten!"

    # MANUAL OVERRIDE still works: load-market may overwrite the auto snapshot.
    import csv
    config.MARKET.mkdir(parents=True, exist_ok=True)
    with open(config.MARKET / f"matchday_{bel_md}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["group", "team1", "team2", "market_pW1", "market_pD", "market_pW2"])
        w.writerow(["G", "Belgium", "Egypt", "0.50", "0.30", "0.30"])  # raw, sums 1.10
    import market
    market.load_market(bel_md)
    row3 = conn.execute(
        "SELECT market_pW1, market_source FROM matches WHERE match_id=?", (bel,)
    ).fetchone()
    assert row3["market_source"] == "manual-csv", "manual override did not take"
    assert abs(row3["market_pW1"] - 0.50 / 1.10) < 1e-6, row3["market_pW1"]
    conn.close()

    # real DB untouched
    if config.DB_PATH.exists():
        rc = db.connect(config.DB_PATH)
        has_mm = rc.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='market_map'"
        ).fetchone()[0]
        real_auto = rc.execute(
            "SELECT COUNT(*) FROM matches WHERE market_source='polymarket-auto'"
        ).fetchone()[0] if rc.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='matches'"
        ).fetchone()[0] else 0
        rc.close()
        assert real_auto == 0, "real DB got auto snapshots — contamination!"

    print("\ntest_fetch_market: PASS ✓")
    print(f"  mapped=2, de-vig ok (sum=1), insert-once ok, manual override ok")
    print(f"  artifacts in: {_TMP}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
