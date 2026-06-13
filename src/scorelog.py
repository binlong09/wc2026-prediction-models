"""Model-vs-market scorekeeper (Task 2).

A SCOREKEEPER, not a learner. It records, before kickoff, each version's
predicted (pW1,pD,pW2) alongside the vig-stripped market probabilities; then,
after results land, it scores both retrospectively and accumulates the
comparison over the tournament.

Hard boundaries enforced here:
  * It NEVER feeds back into any model — no online tuning, no nudging
    predictions toward the market or toward being right. It only records, and
    later scores.
  * A logged pre-match prediction is IMMUTABLE: probabilities + captured_at are
    written once (INSERT OR IGNORE) and never overwritten, even after the result
    is known. Only the score columns + actual_result are filled in afterward.
  * It lives in its own table (`match_log`), separate from the backtest `runs`.

What is logged as the model prediction: the model's pre-match `predict()` output
(the prior-based probability). It is deliberately NOT temperature-calibrated —
calibration needs that round's already-played A–F results, which generally don't
exist before the G–L games kick off. So the scorelog reflects the honest,
uncalibrated pre-match prior. (The backtest's `runs` table is the calibrated,
in-sample-trained view; the two are intentionally distinct.)
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import config
import db
import elo
from backtest import _load_elo_pre, _load_matches, _make_models
from model import Prior
from score import (
    bootstrap_skill_ci,
    brier,
    logloss,
    one_hot,
    skill_score,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _market_map(conn: sqlite3.Connection) -> dict[str, tuple]:
    out = {}
    for r in conn.execute(
        "SELECT match_id, market_pW1, market_pD, market_pW2 FROM matches "
        "WHERE market_pW1 IS NOT NULL"
    ):
        out[r["match_id"]] = (r["market_pW1"], r["market_pD"], r["market_pW2"])
    return out


# --------------------------------------------------------------------------- #
# Capture (immutable, pre-match)                                                #
# --------------------------------------------------------------------------- #
def log_predictions(conn: sqlite3.Connection | None = None) -> int:
    """Capture pre-match predictions for every not-yet-played, not-yet-logged
    match, for each version, at the model's CURRENT state.

    State handling mirrors the backtest exactly: v2 only advances its Elo state
    after a round is FULLY played, so upcoming matches are predicted with state
    that reflects all completed earlier rounds and nothing more. We stop at the
    first not-fully-played round — we cannot honestly advance v2's state past a
    round whose results aren't all in, so later rounds are logged on a future run
    once that round completes.
    """
    own = conn is None
    conn = conn or db.connect()
    try:
        db.init_db()
        features, _ = elo.run_history(config.RESULTS_CSV, config.WC2026_START)
        prior = Prior.fit(features)
        elo_pre = _load_elo_pre(conn)
        rounds = _load_matches(conn)
        market = _market_map(conn)

        models = _make_models(prior, elo_pre)
        for mdl in models:
            mdl.reset()

        captured = _now()
        logged = 0
        for r in (1, 2, 3):
            rmatches = rounds[r]
            if not rmatches:
                continue
            # log upcoming (unplayed) matches at the current state, immutably
            for m in rmatches:
                if m.actual_result is not None:
                    continue  # already decided — cannot log an honest pre-match pred
                mkt = market.get(m.match_id, (None, None, None))
                for mdl in models:
                    p = mdl.predict(m)
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO match_log "
                        "(match_id, version, model_pW1, model_pD, model_pW2, "
                        " market_pW1, market_pD, market_pW2, captured_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (m.match_id, mdl.version, p[0], p[1], p[2],
                         mkt[0], mkt[1], mkt[2], captured),
                    )
                    logged += cur.rowcount

            # advance state only if this round is fully played; else stop here
            if all(m.actual_result for m in rmatches):
                for mdl in models:
                    mdl.update(rmatches)
            else:
                break

        conn.commit()
        print(f"log-predictions: captured {logged} new pre-match rows (existing rows untouched)")
        return logged
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------- #
# Retrospective scoring (never touches probabilities)                           #
# --------------------------------------------------------------------------- #
def score_log(conn: sqlite3.Connection | None = None) -> int:
    """Score logged matches whose result is now known and not yet scored. Fills
    actual_result + brier/logloss for model and (if present) market. Never
    modifies the logged probabilities or captured_at."""
    own = conn is None
    conn = conn or db.connect()
    try:
        results = {
            r["match_id"]: r["actual_result"]
            for r in conn.execute(
                "SELECT match_id, actual_result FROM matches WHERE actual_result IS NOT NULL"
            )
        }
        rows = conn.execute(
            "SELECT version, match_id, model_pW1, model_pD, model_pW2, "
            "market_pW1, market_pD, market_pW2 FROM match_log WHERE brier_model IS NULL"
        ).fetchall()

        scored = 0
        for row in rows:
            res = results.get(row["match_id"])
            if res is None:
                continue  # still unplayed
            y = one_hot(res)
            mp = (row["model_pW1"], row["model_pD"], row["model_pW2"])
            bm, lm = brier(mp, y), logloss(mp, y)
            if row["market_pW1"] is not None:
                kp = (row["market_pW1"], row["market_pD"], row["market_pW2"])
                bk, lk = brier(kp, y), logloss(kp, y)
            else:
                bk = lk = None
            conn.execute(
                "UPDATE match_log SET actual_result=?, brier_model=?, logloss_model=?, "
                "brier_market=?, logloss_market=? WHERE version=? AND match_id=?",
                (res, bm, lm, bk, lk, row["version"], row["match_id"]),
            )
            scored += 1
        conn.commit()
        print(f"score-log: scored {scored} newly-finished logged matches")
        return scored
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------- #
# Report view: the running model-vs-market comparison as it accumulates         #
# --------------------------------------------------------------------------- #
def _scored_rows(conn: sqlite3.Connection, version: str):
    # ordered by the match date (the order results actually land), then id
    return conn.execute(
        "SELECT l.*, m.date AS mdate FROM match_log l "
        "JOIN matches m ON m.match_id = l.match_id "
        "WHERE l.version=? AND l.brier_model IS NOT NULL "
        "ORDER BY m.date, l.match_id",
        (version,),
    ).fetchall()


def _summary(conn: sqlite3.Connection, version: str) -> dict | None:
    rows = _scored_rows(conn, version)
    if not rows:
        return None
    mb = [r["brier_model"] for r in rows]
    ml = [r["logloss_model"] for r in rows]
    have_mkt = [r for r in rows if r["brier_market"] is not None]
    kb = [r["brier_market"] for r in have_mkt]
    kl = [r["logloss_market"] for r in have_mkt]
    out = {
        "version": version,
        "n": len(rows),
        "n_market": len(have_mkt),
        "brier_model": sum(mb) / len(mb),
        "logloss_model": sum(ml) / len(ml),
        "brier_market": (sum(kb) / len(kb)) if kb else None,
        "logloss_market": (sum(kl) / len(kl)) if kl else None,
        "skill_brier": None,
        "skill_logloss": None,
        "ci": (None, None),
    }
    if kb:
        # market scores aligned to the SAME matches that have market
        mb_m = [r["brier_model"] for r in have_mkt]
        ml_m = [r["logloss_model"] for r in have_mkt]
        out["skill_brier"] = skill_score(sum(mb_m) / len(mb_m), sum(kb) / len(kb))
        out["skill_logloss"] = skill_score(sum(ml_m) / len(ml_m), sum(kl) / len(kl))
        import numpy as np
        out["ci"] = bootstrap_skill_ci(np.array(mb_m), np.array(kb))
    return out


def _cumulative(conn: sqlite3.Connection, version: str):
    """Running pooled Brier skill score after each match with a market price."""
    rows = [r for r in _scored_rows(conn, version) if r["brier_market"] is not None]
    xs, ys = [], []
    cum_m, cum_k = 0.0, 0.0
    for i, r in enumerate(rows, 1):
        cum_m += r["brier_model"]
        cum_k += r["brier_market"]
        xs.append(i)
        ys.append(skill_score(cum_m / i, cum_k / i))
    return xs, ys


def scorelog_report(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or db.connect()
    try:
        config.REPORT.mkdir(parents=True, exist_ok=True)
        versions = [r["version"] for r in conn.execute(
            "SELECT DISTINCT version FROM match_log ORDER BY version"
        )]
        summaries = [s for v in versions if (s := _summary(conn, v))]

        print("\n=== Scorelog — running model-vs-market comparison ===")
        n_logged = conn.execute("SELECT COUNT(*) FROM match_log").fetchone()[0]
        n_scored = conn.execute(
            "SELECT COUNT(*) FROM match_log WHERE brier_model IS NOT NULL"
        ).fetchone()[0]
        print(f"logged rows: {n_logged}  |  scored so far: {n_scored}")

        if not summaries:
            print("No scored logged matches yet. Run `log-predictions` before "
                  "kickoff, then `refresh-results` (auto-scores) once games finish.")
            _write_scorelog_html(None, summaries, "No scored matches yet.",
                                 config.REPORT / "scorelog.html")
            return

        header = (
            f"{'ver':>3} {'n':>3} {'n_mkt':>5} "
            f"{'brier_m':>8} {'brier_mkt':>9} {'sk_brier':>9} "
            f"{'ll_m':>7} {'ll_mkt':>7} {'sk_ll':>7} {'skill_brier 90% CI':>20}"
        )
        print(header)
        print("-" * len(header))
        for s in summaries:
            lo, hi = s["ci"]
            ci = "—" if lo is None else f"[{lo:+.2f}, {hi:+.2f}]"
            f = lambda x, nd=4: "—" if x is None else f"{x:.{nd}f}"
            print(
                f"{s['version']:>3} {s['n']:>3} {s['n_market']:>5} "
                f"{f(s['brier_model']):>8} {f(s['brier_market']):>9} {f(s['skill_brier'],3):>9} "
                f"{f(s['logloss_model'],3):>7} {f(s['logloss_market'],3):>7} {f(s['skill_logloss'],3):>7} "
                f"{ci:>20}"
            )

        png_ok = _cumulative_png(conn, versions, config.REPORT / "scorelog.png")
        readout = _scorelog_readout(summaries)
        print("\n--- readout ---\n" + readout)
        _write_scorelog_html(
            png_ok, summaries, readout, config.REPORT / "scorelog.html"
        )
        print(f"\nwrote {config.REPORT / 'scorelog.html'}")
        if png_ok:
            print(f"wrote {config.REPORT / 'scorelog.png'}")
    finally:
        if own:
            conn.close()


def _scorelog_readout(summaries) -> str:
    parts = []
    for s in summaries:
        if s["skill_brier"] is None:
            parts.append(
                f"{s['version']}: {s['n']} scored, no market entered yet so no skill score."
            )
            continue
        lo, hi = s["ci"]
        straddles = lo is not None and lo < 0 < hi
        verdict = ("CI straddles 0 — model and market indistinguishable so far"
                   if straddles else "CI excludes 0")
        parts.append(
            f"{s['version']}: {s['n_market']} scored vs market, "
            f"skill(Brier)={s['skill_brier']:+.3f} ({verdict})."
        )
    return (
        " ".join(parts)
        + " This is the live, accumulating version of the backtest finding; on "
        "this little data expect skill near 0 with a wide CI that tightens only "
        "slowly as more matches land. The scorekeeper records and scores — it "
        "never feeds back into any model."
    )


def _cumulative_png(conn, versions, path) -> bool:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    series = {v: _cumulative(conn, v) for v in versions}
    series = {v: (xs, ys) for v, (xs, ys) in series.items() if xs}
    if not series:
        return False
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.axhline(0, color="k", lw=1, ls="--", label="tie with market")
    for v, (xs, ys) in series.items():
        ax.plot(xs, ys, marker="o", ms=3, label=v)
    ax.set_xlabel("matches scored (in date order)")
    ax.set_ylabel("cumulative skill score (Brier)")
    ax.set_title("Running model-vs-market skill as results land")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return True


def _write_scorelog_html(png_ok, summaries, readout, path) -> None:
    def f(x, nd=4):
        return "—" if x is None else f"{x:.{nd}f}"

    trs = []
    for s in summaries:
        lo, hi = s["ci"]
        ci = "—" if lo is None else f"[{lo:+.2f}, {hi:+.2f}]"
        trs.append(
            f"<tr><td>{s['version']}</td><td>{s['n']}</td><td>{s['n_market']}</td>"
            f"<td>{f(s['brier_model'])}</td><td>{f(s['brier_market'])}</td>"
            f"<td>{f(s['skill_brier'],3)}</td><td>{f(s['logloss_model'],3)}</td>"
            f"<td>{f(s['logloss_market'],3)}</td><td>{f(s['skill_logloss'],3)}</td>"
            f"<td>{ci}</td></tr>"
        )
    img = '<img src="scorelog.png" alt="cumulative skill" style="max-width:100%">' if png_ok else ""
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>WC2026 Scorelog</title>
<style>
 body{{font-family:system-ui,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem;color:#222}}
 table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}
 th,td{{border:1px solid #ddd;padding:.35rem .5rem;text-align:right}}
 th:first-child,td:first-child{{text-align:left}}
 .readout{{background:#eef7ee;border:1px solid #9ccc9c;padding:1rem;border-radius:6px;margin:1.5rem 0}}
</style></head><body>
<h1>Scorelog — running model-vs-market comparison</h1>
<p>Immutable pre-match predictions vs the vig-stripped market, scored as results
land. A scorekeeper, not a learner — it never feeds back into any model.</p>
<table><thead><tr>
<th>ver</th><th>n</th><th>n vs market</th>
<th>Brier model</th><th>Brier market</th><th>skill(Brier)</th>
<th>logloss model</th><th>logloss market</th><th>skill(logloss)</th>
<th>skill(Brier) 90% CI</th></tr></thead>
<tbody>{''.join(trs)}</tbody></table>
<div class="readout"><strong>Readout.</strong> {readout}</div>
{img}
</body></html>"""
    path.write_text(html)
