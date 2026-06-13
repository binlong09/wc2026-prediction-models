"""ISOLATED end-to-end smoke test.

Today only a handful of real group games are played, so a real backtest scores
nothing. This test proves the full pipeline (build → simulate → backtest →
report) runs end to end by SIMULATING a complete tournament.

Isolation guarantees (must never contaminate real data):
  * the DB is a throwaway temp file (via WCBT_DB) — never data/backtest.db;
  * the report dir is redirected to the temp dir — never the real report/;
  * real raw files are only READ (never written);
  * the script asserts it is not pointing at the real DB before doing anything.

Run:  PYTHONPATH=src .venv/bin/python tests/smoke_test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import numpy as np

# Redirect the DB to a throwaway temp file BEFORE importing anything that
# resolves config.db_path().
_TMP = Path(tempfile.mkdtemp(prefix="wcbt_smoke_"))
os.environ["WCBT_DB"] = str(_TMP / "smoke.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config  # noqa: E402

# Redirect the report output dir too (so we never overwrite the real report/).
config.REPORT = _TMP / "report"

import backtest  # noqa: E402
import build  # noqa: E402
import db  # noqa: E402
import elo  # noqa: E402
import report  # noqa: E402
from model import Prior  # noqa: E402

# --- hard isolation guards --------------------------------------------------
REAL_DB = config.DB_PATH
assert config.db_path() != REAL_DB, "smoke test must not use the real DB!"
assert "smoke" in str(config.db_path()), "DB path not the temp smoke DB!"
print(f"smoke: isolated DB     = {config.db_path()}")
print(f"smoke: isolated report = {config.REPORT}")

_OUTCOMES = ["team1_win", "draw", "team2_win"]
_GOALS = {"team1_win": (2, 0), "draw": (1, 1), "team2_win": (0, 2)}


def main() -> int:
    # 1) Build fixtures + Elo into the temp DB (reads real raw data, read-only).
    build.build_elo()

    # 2) Fit the prior and SIMULATE a full tournament into the temp DB.
    features, _ = elo.run_history(config.RESULTS_CSV, config.WC2026_START)
    prior = Prior.fit(features)
    rng = np.random.default_rng(12345)

    conn = db.connect()
    rows = conn.execute(
        "SELECT match_id, neutral, elo_diff, group_half FROM matches"
    ).fetchall()
    for r in rows:
        p = np.array(prior.prob(r["elo_diff"], r["neutral"]))
        p = p / p.sum()
        res = _OUTCOMES[int(rng.choice(3, p=p))]
        g1, g2 = _GOALS[res]
        conn.execute(
            "UPDATE matches SET actual_result=?, actual_goals1=?, actual_goals2=? WHERE match_id=?",
            (res, g1, g2, r["match_id"]),
        )
        # Give the held-out half (G–L) a synthetic, already-normalized market:
        # the true probs nudged by noise — a realistic competitor to the model.
        if r["group_half"] == 2:
            mp = np.clip(p + rng.normal(0, 0.05, 3), 0.02, None)
            mp = mp / mp.sum()
            conn.execute(
                "UPDATE matches SET market_pW1=?, market_pD=?, market_pW2=? WHERE match_id=?",
                (float(mp[0]), float(mp[1]), float(mp[2]), r["match_id"]),
            )
    conn.commit()
    conn.close()

    # 3) Run the real backtest loop + report against the temp DB.
    backtest.run_backtest()
    report.make_report()

    # 4) Verify the pipeline produced a scored, pooled grid for BOTH versions.
    conn = db.connect()
    n_runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    pooled = conn.execute(
        "SELECT version, n, skill_brier, skill_brier_lo, skill_brier_hi "
        "FROM runs WHERE matchday IS NULL ORDER BY version"
    ).fetchall()
    n_preds = conn.execute(
        "SELECT COUNT(*) FROM predictions WHERE split='test'"
    ).fetchone()[0]

    versions = {r["version"] for r in pooled}
    assert versions == {"v1", "v2"}, f"expected pooled v1+v2, got {versions}"
    for r in pooled:
        assert r["n"] == 36, f"pooled {r['version']} test n should be 36, got {r['n']}"
    assert n_preds == 72, f"expected 72 test predictions (2 versions x 36), got {n_preds}"
    assert (config.REPORT / "report.html").exists(), "report.html not written"
    for v in ("v1", "v2"):
        assert (config.REPORT / f"calibration_{v}.png").exists(), f"calibration_{v}.png missing"

    # round-1 invariant via the stored (calibrated) predictions: identical base
    # preds + identical train => identical temperature => identical calibrated
    # round-1 predictions for v1 and v2.
    r1 = {}
    for row in conn.execute(
        "SELECT version, match_id, pW1, pD, pW2 FROM predictions WHERE matchday=1"
    ):
        r1.setdefault(row["match_id"], {})[row["version"]] = (row["pW1"], row["pD"], row["pW2"])
    mism = [mid for mid, vs in r1.items() if "v1" in vs and "v2" in vs and vs["v1"] != vs["v2"]]
    assert not mism, f"v1≠v2 on round-1 stored predictions: {mism[:3]}"
    conn.close()

    # 5) Confirm the REAL data was untouched.
    real_played = "n/a"
    if REAL_DB.exists():
        rc = db.connect(REAL_DB)
        real_played = rc.execute(
            "SELECT COUNT(*) FROM matches WHERE actual_result IS NOT NULL"
        ).fetchone()[0]
        real_runs_scored = rc.execute(
            "SELECT COUNT(*) FROM runs WHERE n > 0"
        ).fetchone()[0]
        rc.close()
        assert real_played <= 3, f"REAL DB was contaminated! played={real_played}"
        assert real_runs_scored == 0, "REAL DB has scored runs — contamination!"

    print("\nsmoke: PASS ✓")
    print(f"  runs rows: {n_runs}, test predictions: {n_preds}")
    for r in pooled:
        print(
            f"  pooled {r['version']}: n={r['n']} skill_brier={r['skill_brier']:+.3f} "
            f"CI=[{r['skill_brier_lo']:+.3f}, {r['skill_brier_hi']:+.3f}]"
        )
    print(f"  real DB matches-with-results still: {real_played} (uncontaminated)")
    print(f"  artifacts in: {_TMP}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
