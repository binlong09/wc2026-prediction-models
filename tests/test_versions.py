"""ISOLATED test: v1 vs v2 invariants.

The headline guardrail for v2: because `update` has not yet fired when round-1
predictions are made, v1 and v2 MUST produce identical round-1 predictions. If
they differ at round 1, something is wrong. This test asserts that, and also
confirms v2 actually *does* diverge from round 2 once it has updated on round-1
results (otherwise v2 would be a silent no-op).

Isolation: throwaway temp DB via WCBT_DB; never touches data/backtest.db.

Run:  PYTHONPATH=src .venv/bin/python tests/test_versions.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="wcbt_vtest_"))
os.environ["WCBT_DB"] = str(_TMP / "vtest.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import build  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import elo  # noqa: E402
from backtest import _load_elo_pre, _load_matches  # noqa: E402
from model import Prior, V1Model, V2Model  # noqa: E402

assert config.db_path() != config.DB_PATH and "vtest" in str(config.db_path())

_GOALS = {"team1_win": (2, 0), "draw": (1, 1), "team2_win": (0, 2)}


def main() -> int:
    build.build_elo()
    features, _ = elo.run_history(config.RESULTS_CSV, config.WC2026_START)
    prior = Prior.fit(features)

    conn = db.connect()
    elo_pre = _load_elo_pre(conn)
    rounds = _load_matches(conn)
    conn.close()

    v1 = V1Model(prior, elo_pre)
    v2 = V2Model(prior, elo_pre)
    v1.reset()
    v2.reset()

    # --- INVARIANT 1: identical round-1 predictions (no prior results yet) ---
    round1 = rounds[1]
    assert len(round1) == 24, f"expected 24 round-1 matches, got {len(round1)}"
    for m in round1:
        p1, p2 = v1.predict(m), v2.predict(m)
        assert p1 == p2, f"round-1 mismatch on {m.match_id}: {p1} != {p2}"
    print(f"INVARIANT 1 PASS: v1 ≡ v2 on all {len(round1)} round-1 predictions")

    # Snapshot v1's round-2 predictions BEFORE either model updates.
    round2 = rounds[2]
    v1_r2_before = {m.match_id: v1.predict(m) for m in round2}

    # --- Feed round-1 results to BOTH models' update() ---
    # (v1.update is a no-op; v2.update nudges ratings.) Use synthetic results so
    # the test doesn't depend on real fixtures being played yet.
    for m in round1:
        m.actual_result = "team1_win"
        m.actual_goals1, m.actual_goals2 = _GOALS["team1_win"]
    v1.update(round1)
    v2.update(round1)

    # --- INVARIANT 2: v1 unchanged at round 2; v2 has moved for ≥1 match ---
    v1_unchanged = all(v1.predict(m) == v1_r2_before[m.match_id] for m in round2)
    v2_moved = any(v2.predict(m) != v1_r2_before[m.match_id] for m in round2)
    assert v1_unchanged, "v1 changed after update() — it must be a no-op!"
    assert v2_moved, "v2 did not change after update() — it's a silent no-op!"
    print("INVARIANT 2 PASS: after round-1 results, v1 frozen, v2 diverged at round 2")

    # Sanity: the nudge is MODEST (modest K). Average |Δ elo_diff-implied prob|
    # over round-2 should be small — a round shouldn't swing predictions wildly.
    deltas = [
        max(abs(a - b) for a, b in zip(v2.predict(m), v1_r2_before[m.match_id]))
        for m in round2
    ]
    avg_shift = sum(deltas) / len(deltas)
    print(f"  modest-K check: mean max prob shift at round 2 = {avg_shift:.4f}")
    assert avg_shift < 0.20, f"round-2 shift {avg_shift:.3f} too large — K likely too high"

    print("\ntest_versions: PASS ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
