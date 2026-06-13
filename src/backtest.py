"""The per-matchday train/test loop (spec §9) — the core.

The loop is IDENTICAL for every version; it never branches on which version it
is. The version's `update` (a no-op for v1) is the only thing that makes runs
differ — that's the precondition for a fair comparison.

Invariants held every round, every version:
  * calibrate on A–F (group_half 1), test only on G–L (group_half 2);
  * the test set is always 12, never 24;
  * `update` fires AFTER the round's test scoring, so the model never sees a
    test result before predicting it.

Only rounds whose BOTH halves are fully played are scored; others are skipped.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

import config
import db
import elo
from calibrate import apply_temperature, fit_temperature
from model import Match, Prior, V1Model, V2Model
from score import (
    bootstrap_skill_ci,
    brier,
    logloss,
    one_hot,
    skill_score,
)


# --------------------------------------------------------------------------- #
# Loading                                                                       #
# --------------------------------------------------------------------------- #
def _load_matches(conn: sqlite3.Connection) -> dict[int, list[Match]]:
    rounds: dict[int, list[Match]] = {1: [], 2: [], 3: []}
    for r in conn.execute("SELECT * FROM matches ORDER BY grp, matchday"):
        rounds[r["matchday"]].append(
            Match(
                match_id=r["match_id"], matchday=r["matchday"], grp=r["grp"],
                group_half=r["group_half"], team1_id=r["team1_id"],
                team2_id=r["team2_id"], neutral=r["neutral"], elo_diff=r["elo_diff"],
                actual_result=r["actual_result"],
                actual_goals1=r["actual_goals1"], actual_goals2=r["actual_goals2"],
            )
        )
    return rounds


def _load_elo_pre(conn: sqlite3.Connection) -> dict[str, float]:
    return {r["team_id"]: r["elo_pre"] for r in conn.execute("SELECT team_id, elo_pre FROM teams")}


def _market(conn_row) -> tuple[float, float, float] | None:
    if conn_row["market_pW1"] is None:
        return None
    return (conn_row["market_pW1"], conn_row["market_pD"], conn_row["market_pW2"])


def _market_for(conn: sqlite3.Connection, match_id: str):
    row = conn.execute(
        "SELECT market_pW1, market_pD, market_pW2 FROM matches WHERE match_id=?",
        (match_id,),
    ).fetchone()
    return _market(row)


def _make_models(prior: Prior, elo_pre: dict[str, float]):
    """v1 + v2. v3 appends here later; the loop below is unchanged regardless."""
    return [V1Model(prior, elo_pre), V2Model(prior, elo_pre)]


# --------------------------------------------------------------------------- #
# The loop                                                                      #
# --------------------------------------------------------------------------- #
def run_backtest(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or db.connect()
    try:
        db.init_db()
        # Fit the shared prior over full history (recomputes Elo features).
        features, _ = elo.run_history(config.RESULTS_CSV, config.WC2026_START)
        prior = Prior.fit(features)

        elo_pre = _load_elo_pre(conn)
        rounds = _load_matches(conn)

        complete = [
            r for r in (1, 2, 3)
            if rounds[r] and all(m.actual_result for m in rounds[r])
        ]
        print(f"backtest: complete rounds (both halves played): {complete or 'none'}")

        # Full rebuild: clear prior predictions/runs so re-runs are idempotent.
        conn.execute("DELETE FROM predictions")
        conn.execute("DELETE FROM runs")
        conn.execute(
            "UPDATE matches SET model_pW1=NULL, model_pD=NULL, model_pW2=NULL"
        )
        conn.commit()

        run_id = uuid.uuid4().hex[:12]
        created = datetime.now(timezone.utc).isoformat(timespec="seconds")

        for mdl in _make_models(prior, elo_pre):
            version = mdl.version
            mdl.reset()
            pooled = []  # (cal_prob, market_prob_or_None, y) across processed rounds

            for r in (1, 2, 3):
                if r not in complete:
                    continue  # can't score; leave state untouched
                rmatches = rounds[r]
                base = {m.match_id: mdl.predict(m) for m in rmatches}

                train = [m for m in rmatches if m.group_half == 1]
                test = [m for m in rmatches if m.group_half == 2]

                T = fit_temperature(
                    [base[m.match_id] for m in train],
                    [one_hot(m.actual_result) for m in train],
                )

                # store train predictions (in-sample) for completeness
                for m in train:
                    _store_pred(conn, version, m, "train", apply_temperature(base[m.match_id], T), T)

                mb, ml, kb, kl = [], [], [], []  # model/market brier & logloss
                for m in test:
                    cal = apply_temperature(base[m.match_id], T)
                    _store_pred(conn, version, m, "test", cal, T)
                    if version == "v1":  # surface baseline calibrated output on matches
                        conn.execute(
                            "UPDATE matches SET model_pW1=?, model_pD=?, model_pW2=? WHERE match_id=?",
                            (cal[0], cal[1], cal[2], m.match_id),
                        )
                    y = one_hot(m.actual_result)
                    mb.append(brier(cal, y))
                    ml.append(logloss(cal, y))
                    mk = _market_for(conn, m.match_id)
                    if mk is not None:
                        kb.append(brier(mk, y))
                        kl.append(logloss(mk, y))
                    pooled.append((cal, mk, y))

                _store_run(conn, run_id, version, r, "test", mb, ml, kb, kl, created)

                mdl.update(rmatches)  # v1 no-op; AFTER scoring

            # ---- pooled across this version's processed test halves ----
            have_mkt = [(cal, mk, y) for (cal, mk, y) in pooled if mk is not None]
            if have_mkt:
                mb = [brier(c, y) for c, _, y in have_mkt]
                ml = [logloss(c, y) for c, _, y in have_mkt]
                kb = [brier(mk, y) for _, mk, y in have_mkt]
                kl = [logloss(mk, y) for _, mk, y in have_mkt]
                _store_run(conn, run_id, version, None, "test", mb, ml, kb, kl, created, bootstrap=True)
            elif pooled:  # model-only pooled (no market entered yet)
                mb = [brier(c, y) for c, _, y in pooled]
                ml = [logloss(c, y) for c, _, y in pooled]
                _store_run(conn, run_id, version, None, "test", mb, ml, [], [], created)

        conn.commit()
        print(f"backtest: done (run_id={run_id})")
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------- #
# Persistence helpers                                                           #
# --------------------------------------------------------------------------- #
def _store_pred(conn, version, m: Match, split, cal, T) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO predictions "
        "(version, match_id, matchday, split, pW1, pD, pW2, temperature) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (version, m.match_id, m.matchday, split, cal[0], cal[1], cal[2], T),
    )


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _store_run(conn, run_id, version, matchday, split, mb, ml, kb, kl, created, bootstrap=False) -> None:
    brier_model, logloss_model = _mean(mb), _mean(ml)
    brier_market, logloss_market = _mean(kb), _mean(kl)
    skill_b = skill_score(brier_model, brier_market) if kb else None
    skill_l = skill_score(logloss_model, logloss_market) if kl else None

    sb_lo = sb_hi = sl_lo = sl_hi = None
    if bootstrap and kb:
        import numpy as np
        sb_lo, sb_hi = bootstrap_skill_ci(np.array(mb), np.array(kb))
        sl_lo, sl_hi = bootstrap_skill_ci(np.array(ml), np.array(kl))

    conn.execute(
        "INSERT INTO runs (run_id, version, matchday, split, "
        "brier_model, logloss_model, brier_market, logloss_market, "
        "skill_brier, skill_logloss, skill_brier_lo, skill_brier_hi, "
        "skill_logloss_lo, skill_logloss_hi, n, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, version, matchday, split,
         brier_model, logloss_model, brier_market, logloss_market,
         skill_b, skill_l, sb_lo, sb_hi, sl_lo, sl_hi, len(mb), created),
    )
