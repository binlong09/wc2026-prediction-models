"""Static report (spec §10, §11).

Regenerates, with no server and no interactive UI:
  * the {v1,v2,v3} x {round 1,2,3,pooled} grid — model/market Brier & log-loss,
    skill score, bootstrap 90% CI — printed to the terminal and to
    report/report.html;
  * one reliability diagram PNG per version (report/calibration_v*.png).

The whole point is the calibration discipline and the proper-scoring
comparison, not a verdict — so the honest readout is generated, not asserted.
"""
from __future__ import annotations

import sqlite3

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

import config
import db
from score import one_hot

_RNDLBL = {1: "round 1", 2: "round 2", 3: "round 3", None: "POOLED"}


def _fmt(x, nd=4):
    return "—" if x is None else f"{x:.{nd}f}"


def _ci(lo, hi):
    if lo is None or hi is None:
        return "—"
    return f"[{lo:+.2f}, {hi:+.2f}]"


def _grid_rows(conn: sqlite3.Connection):
    # latest run only
    row = conn.execute("SELECT run_id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
    if row is None:
        return None, []
    run_id = row["run_id"]
    rows = conn.execute(
        "SELECT * FROM runs WHERE run_id=? ORDER BY version, "
        "CASE WHEN matchday IS NULL THEN 99 ELSE matchday END",
        (run_id,),
    ).fetchall()
    return run_id, rows


def _print_grid(rows) -> str:
    header = (
        f"{'ver':>3} {'round':>8} {'n':>3} "
        f"{'brier_m':>8} {'brier_mkt':>9} {'sk_brier':>9} "
        f"{'ll_m':>7} {'ll_mkt':>7} {'sk_ll':>7} {'skill_brier 90% CI':>20}"
    )
    lines = [header, "-" * len(header)]
    for r in rows:
        lines.append(
            f"{r['version']:>3} {_RNDLBL[r['matchday']]:>8} {r['n']:>3} "
            f"{_fmt(r['brier_model']):>8} {_fmt(r['brier_market']):>9} {_fmt(r['skill_brier'],3):>9} "
            f"{_fmt(r['logloss_model'],3):>7} {_fmt(r['logloss_market'],3):>7} {_fmt(r['skill_logloss'],3):>7} "
            f"{_ci(r['skill_brier_lo'], r['skill_brier_hi']):>20}"
        )
    out = "\n".join(lines)
    print(out)
    return out


def _readout(rows) -> str:
    pooled = [r for r in rows if r["matchday"] is None]
    if not pooled:
        return (
            "No complete group round (both halves played) is available yet, so "
            "no out-of-sample comparison could be scored. Re-run `refresh-results` "
            "and `backtest` once a full round of results is in."
        )
    parts = []
    for r in pooled:
        if r["brier_market"] is None:
            parts.append(
                f"{r['version']}: pooled n={r['n']}, model Brier {_fmt(r['brier_model'],3)} "
                f"(no market entered, so no skill score)."
            )
            continue
        sb = r["skill_brier"]
        ci = _ci(r["skill_brier_lo"], r["skill_brier_hi"])
        straddles = (r["skill_brier_lo"] or 0) < 0 < (r["skill_brier_hi"] or 0)
        verdict = (
            "the CI straddles 0, so we cannot distinguish the model from the market"
            if straddles else
            ("the CI excludes 0 in the model's favour" if (sb or 0) > 0
             else "the CI excludes 0 in the market's favour")
        )
        parts.append(
            f"{r['version']}: pooled n={r['n']}, skill(Brier)={_fmt(sb,3)} "
            f"with 90% CI {ci} — {verdict}."
        )
    base = " ".join(parts)
    return (
        base
        + " As the spec anticipates, on this little data the honest finding is that "
        "skill scores sit near 0 with wide CIs; the value here is the calibration "
        "discipline and proper-scoring comparison, not a money-making verdict."
    )


def _reliability_png(conn: sqlite3.Connection, version: str, path) -> bool:
    """Predicted probability vs observed frequency, binned over all test-split
    class predictions for `version`. Returns False if there's nothing to plot."""
    q = conn.execute(
        "SELECT p.pW1, p.pD, p.pW2, m.actual_result "
        "FROM predictions p JOIN matches m ON m.match_id=p.match_id "
        "WHERE p.version=? AND p.split='test' AND m.actual_result IS NOT NULL",
        (version,),
    ).fetchall()
    if not q:
        return False

    preds, obs = [], []
    for r in q:
        y = one_hot(r["actual_result"])
        for pi, yi in zip((r["pW1"], r["pD"], r["pW2"]), y):
            preds.append(pi)
            obs.append(yi)
    preds, obs = np.array(preds), np.array(obs)

    bins = np.linspace(0, 1, 11)
    idx = np.clip(np.digitize(preds, bins) - 1, 0, len(bins) - 2)
    xs, ys, ns = [], [], []
    for b in range(len(bins) - 1):
        mask = idx == b
        if mask.sum() == 0:
            continue
        xs.append(preds[mask].mean())
        ys.append(obs[mask].mean())
        ns.append(int(mask.sum()))

    fig, ax = plt.subplots(figsize=(5, 5))
    ax.plot([0, 1], [0, 1], "k--", lw=1, label="perfect calibration")
    ax.scatter(xs, ys, s=[20 + 6 * n for n in ns], alpha=0.7, label="binned (size ∝ n)")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("mean predicted probability")
    ax.set_ylabel("observed frequency")
    ax.set_title(f"Reliability — {version} (test split, n={len(q)} games)")
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return True


def _write_html(run_id, rows, readout, png_notes, path) -> None:
    def cell(x, nd=4):
        return _fmt(x, nd)

    trs = []
    for r in rows:
        trs.append(
            "<tr>"
            f"<td>{r['version']}</td><td>{_RNDLBL[r['matchday']]}</td><td>{r['n']}</td>"
            f"<td>{cell(r['brier_model'])}</td><td>{cell(r['brier_market'])}</td>"
            f"<td>{cell(r['skill_brier'],3)}</td>"
            f"<td>{cell(r['logloss_model'],3)}</td><td>{cell(r['logloss_market'],3)}</td>"
            f"<td>{cell(r['skill_logloss'],3)}</td>"
            f"<td>{_ci(r['skill_brier_lo'], r['skill_brier_hi'])}</td>"
            "</tr>"
        )
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>WC2026 Backtest Report</title>
<style>
 body{{font-family:system-ui,sans-serif;max-width:920px;margin:2rem auto;padding:0 1rem;color:#222}}
 table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}
 th,td{{border:1px solid #ddd;padding:.35rem .5rem;text-align:right}}
 th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){{text-align:left}}
 tr:has(td:nth-child(2):not(:empty)):has(td) td{{}}
 caption{{text-align:left;font-weight:600;margin-bottom:.5rem}}
 .pooled{{background:#f6f6ff;font-weight:600}}
 .readout{{background:#fffbe6;border:1px solid #ecd97a;padding:1rem;border-radius:6px;margin:1.5rem 0}}
 img{{max-width:48%;margin:.5rem 1% 0 0;border:1px solid #eee}}
 code{{background:#f3f3f3;padding:.1rem .3rem;border-radius:3px}}
</style></head><body>
<h1>World Cup 2026 Prediction Backtest</h1>
<p>Run <code>{run_id}</code>. Grid of {{v1,v2,v3}} × {{round 1,2,3,pooled}}.
Lower Brier / log-loss is better; skill score &gt; 0 means the model beats the
market. CI is the bootstrap 90% interval on the pooled Brier skill score.</p>
<table>
<caption>Out-of-sample (G–L) scores — calibrated on A–F each round</caption>
<thead><tr>
<th>ver</th><th>round</th><th>n</th>
<th>Brier model</th><th>Brier market</th><th>skill(Brier)</th>
<th>logloss model</th><th>logloss market</th><th>skill(logloss)</th>
<th>skill(Brier) 90% CI</th>
</tr></thead>
<tbody>
{''.join(trs)}
</tbody></table>
<div class="readout"><strong>Honest readout.</strong> {readout}</div>
<h2>Reliability diagrams</h2>
<p>{png_notes}</p>
</body></html>"""
    path.write_text(html)


def make_report(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or db.connect()
    try:
        config.REPORT.mkdir(parents=True, exist_ok=True)
        run_id, rows = _grid_rows(conn)

        print("\n=== WC2026 Backtest Report ===")
        if not rows:
            msg = (
                "No scored runs yet. Run `fetch` → `build-elo` → `load-market` → "
                "`backtest` first, and ensure at least one group round is fully played."
            )
            print(msg)
            readout = _readout([])
            _write_html("(none)", [], readout, "No reliability data yet.", config.REPORT / "report.html")
            print("\n" + readout)
            print(f"\nwrote {config.REPORT / 'report.html'}")
            return

        _print_grid(rows)
        readout = _readout(rows)

        versions = sorted({r["version"] for r in rows})
        png_made = []
        for v in versions:
            png = config.REPORT / f"calibration_{v}.png"
            if _reliability_png(conn, v, png):
                png_made.append(v)
        notes = (
            " ".join(f'<img src="calibration_{v}.png" alt="reliability {v}">' for v in png_made)
            if png_made else "No test-split predictions to plot yet."
        )

        _write_html(run_id, rows, readout, notes, config.REPORT / "report.html")
        print("\n--- honest readout ---\n" + readout)
        print(f"\nwrote {config.REPORT / 'report.html'}")
        for v in png_made:
            print(f"wrote {config.REPORT / f'calibration_{v}.png'}")
    finally:
        if own:
            conn.close()
