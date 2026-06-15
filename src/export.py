"""export-companion (companion-spec §2-3).

Writes report/companion.json — the single JSON the live companion app fetches
and renders. Read-only over the DB; no new data sources, no model logic beyond
reading what's already stored.

Per the spec, per-match MODEL predictions (v1/v2) come from `match_log` — the
UNCALIBRATED pre-match predictions that exist for every match before kickoff —
NOT the backtest `runs`/`predictions` tables (those are calibrated, post-round,
G–L-only). This keeps the companion's numbers consistent with the scorelog.

Everything else (status, result, market price/source, captured_at, per-match
scores) comes from `matches`. The scorelog series is the cumulative model-vs-
market skill as results land, also from `match_log`.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import config
import db
import polymarket as pm
from score import bootstrap_skill_ci, skill_score

# our market_source -> the spec's compact enum
_SRC = {
    "polymarket-auto": "auto",
    "polymarket-backfill": "backfill",
    "manual-csv": "manual",
}


def _iso_z(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _ts_z(raw: str | None) -> str | None:
    """Normalize a stored timestamp ('2026-06-11T18:00:07+00:00') to '...Z'."""
    return _iso_z(pm.parse_game_time(raw)) if raw else None


def _triple(a, b, c):
    return [round(float(a), 4), round(float(b), 4), round(float(c), 4)]


def export_companion(
    conn: sqlite3.Connection | None = None,
    now: datetime | None = None,
    path=None,
) -> dict:
    own = conn is None
    conn = conn or db.connect()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    path = path if path is not None else (config.REPORT / "companion.json")
    try:
        config.REPORT.mkdir(parents=True, exist_ok=True)

        # public id = Polymarket event slug where mapped, else internal match_id
        pub = {}
        kickoff = {}
        for r in conn.execute("SELECT match_id, event_slug, kickoff FROM market_map"):
            pub[r["match_id"]] = r["event_slug"] or r["match_id"]
            kickoff[r["match_id"]] = r["kickoff"]

        # match_log indexed by (version, match_id)
        log: dict[tuple, sqlite3.Row] = {}
        for r in conn.execute("SELECT * FROM match_log"):
            log[(r["version"], r["match_id"])] = r

        rows = conn.execute("SELECT * FROM matches").fetchall()
        # order by kickoff (UTC) then group, like the live view wants
        rows = sorted(
            rows,
            key=lambda m: (pm.parse_game_time(kickoff.get(m["match_id"]))
                           or datetime.max.replace(tzinfo=timezone.utc), m["grp"]),
        )

        matches = []
        for m in rows:
            mid = m["match_id"]
            ko = pm.parse_game_time(kickoff.get(mid))
            result = m["actual_result"]
            status = "final" if result else ("live" if ko and ko <= now else "upcoming")

            v1, v2 = log.get(("v1", mid)), log.get(("v2", mid))
            preds: dict[str, list] = {}
            if v1 and v1["model_pW1"] is not None:
                preds["v1"] = _triple(v1["model_pW1"], v1["model_pD"], v1["model_pW2"])
            if v2 and v2["model_pW1"] is not None:
                preds["v2"] = _triple(v2["model_pW1"], v2["model_pD"], v2["model_pW2"])
            # market: prefer the logged pre-match snapshot (scorelog-consistent),
            # else the frozen price on matches; omit entirely if neither exists.
            if v1 and v1["market_pW1"] is not None:
                preds["market"] = _triple(v1["market_pW1"], v1["market_pD"], v1["market_pW2"])
            elif m["market_pW1"] is not None:
                preds["market"] = _triple(m["market_pW1"], m["market_pD"], m["market_pW2"])

            scored = None
            if status == "final":
                sc = {}
                for ver, lr in (("v1", v1), ("v2", v2)):
                    if lr and lr["brier_model"] is not None:
                        sc[ver] = {"brier": round(lr["brier_model"], 4),
                                   "logloss": round(lr["logloss_model"], 4)}
                mk = v1 if (v1 and v1["brier_market"] is not None) else (
                    v2 if (v2 and v2["brier_market"] is not None) else None)
                if mk is not None:
                    sc["market"] = {"brier": round(mk["brier_market"], 4),
                                    "logloss": round(mk["logloss_market"], 4)}
                scored = sc or None

            matches.append({
                "match_id": pub.get(mid, mid),
                "group": m["grp"],
                "round": m["matchday"],
                "half": m["group_half"],
                "team1": m["team1_id"],
                "team2": m["team2_id"],
                "kickoff_utc": _iso_z(ko),
                "status": status,
                "actual_result": result,
                "predictions": preds,
                "market_source": _SRC.get(m["market_source"]) if m["market_source"] else None,
                "captured_at": _ts_z(m["market_captured_at"]),
                "scored": scored,
            })

        out = {
            "generated_at": _iso_z(now),
            "matches": matches,
            "scorelog": _scorelog(conn, pub),
        }
        path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
        print(f"export-companion: wrote {path} "
              f"({len(matches)} matches, {len(out['scorelog']['points'])} scorelog points)")
        return out
    finally:
        if own:
            conn.close()


def _scorelog(conn: sqlite3.Connection, pub: dict) -> dict:
    """Cumulative model-vs-market skill (Brier + log-loss) for v1 and v2 as
    results land, ordered by match date — the live version of the backtest's
    finding. Built from scored match_log rows."""
    rows = conn.execute(
        "SELECT l.match_id, l.version, l.brier_model, l.logloss_model, "
        "l.brier_market, l.logloss_market, m.date "
        "FROM match_log l JOIN matches m ON m.match_id = l.match_id "
        "WHERE l.brier_model IS NOT NULL AND l.brier_market IS NOT NULL "
        "ORDER BY m.date, l.match_id"
    ).fetchall()

    by_match: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        if r["match_id"] not in by_match:
            order.append(r["match_id"])
        by_match.setdefault(r["match_id"], {})[r["version"]] = (
            r["brier_model"], r["logloss_model"], r["brier_market"], r["logloss_market"])

    cum = {v: {"mb": 0.0, "ml": 0.0, "kb": 0.0, "kl": 0.0, "n": 0} for v in ("v1", "v2")}
    # per-version per-match score arrays, for the pooled bootstrap CI
    arr = {v: {"mb": [], "ml": [], "kb": [], "kl": []} for v in ("v1", "v2")}
    points = []
    for mid in order:
        for ver, vals in by_match[mid].items():
            bm, lm, bk, lk = vals
            c = cum[ver]
            c["mb"] += bm; c["ml"] += lm; c["kb"] += bk; c["kl"] += lk; c["n"] += 1
            a = arr[ver]
            a["mb"].append(bm); a["ml"].append(lm); a["kb"].append(bk); a["kl"].append(lk)
        n = max(cum["v1"]["n"], cum["v2"]["n"])
        pt = {"match_id": pub.get(mid, mid), "n": n,
              "cum_skill_brier": {}, "cum_skill_logloss": {}}
        for ver in ("v1", "v2"):
            c = cum[ver]
            if c["n"] > 0 and c["kb"] > 0:
                pt["cum_skill_brier"][ver] = round(skill_score(c["mb"] / c["n"], c["kb"] / c["n"]), 4)
                pt["cum_skill_logloss"][ver] = round(skill_score(c["ml"] / c["n"], c["kl"] / c["n"]), 4)
        points.append(pt)

    # Pooled skill + bootstrap 90% CI over all scored matches (reuse score.py's
    # seeded bootstrap — deterministic, so companion.json stays diff-stable).
    pooled: dict = {"n": len(order)}
    for ver in ("v1", "v2"):
        a = arr[ver]
        if a["mb"] and sum(a["kb"]) > 0:
            cb = bootstrap_skill_ci(a["mb"], a["kb"])   # (lo, hi) on Brier skill
            cl = bootstrap_skill_ci(a["ml"], a["kl"])   # (lo, hi) on log-loss skill
            n = len(a["mb"])
            pooled[ver] = {
                "n": n,
                "skill_brier": round(skill_score(sum(a["mb"]) / n, sum(a["kb"]) / n), 4),
                "skill_brier_ci": [round(cb[0], 4), round(cb[1], 4)],
                "skill_logloss": round(skill_score(sum(a["ml"]) / n, sum(a["kl"]) / n), 4),
                "skill_logloss_ci": [round(cl[0], 4), round(cl[1], 4)],
            }
    return {"points": points, "pooled": pooled}
