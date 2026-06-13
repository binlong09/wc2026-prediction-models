"""build-elo + refresh-results (spec §10).

Computes Elo over the full history, snapshots `elo_pre`, and populates `teams`
and `matches` from worldcup.json: derives the group ROUND (1/2/3) by
date-ordering each group's six round-robin games, sets group_half / neutral /
elo_diff. Idempotent: re-runs refresh fixtures + actual results without
clobbering any loaded market_* or stored model_* columns.
"""
from __future__ import annotations

import json
import sqlite3

import config
import db
import elo

# worldcup.json team name -> martj42 canonical name (verified 2026-06-12).
NAME_MAP = {
    "USA": "United States",
    "Bosnia & Herzegovina": "Bosnia and Herzegovina",
}


def canon(name: str) -> str:
    return NAME_MAP.get(name, name)


def _group_letter(group_field: str) -> str:
    # "Group A" -> "A"
    return group_field.replace("Group", "").strip()


def _result(score_ft) -> tuple[str | None, int | None, int | None]:
    if not score_ft:
        return None, None, None
    g1, g2 = int(score_ft[0]), int(score_ft[1])
    if g1 > g2:
        res = "team1_win"
    elif g1 < g2:
        res = "team2_win"
    else:
        res = "draw"
    return res, g1, g2


def _load_group_matches(worldcup_json) -> list[dict]:
    """Return group-stage matches with a derived round (1/2/3) per group."""
    raw = json.load(open(worldcup_json))
    gm = [m for m in raw["matches"] if str(m.get("round", "")).startswith("Matchday")]

    # bucket by group, order by (date, time), assign round = index // 2 + 1
    by_group: dict[str, list[dict]] = {}
    for m in gm:
        by_group.setdefault(_group_letter(m["group"]), []).append(m)

    out: list[dict] = []
    for grp, ms in by_group.items():
        ms.sort(key=lambda m: (m.get("date", ""), m.get("time", "")))
        if len(ms) != 6:
            raise ValueError(f"group {grp} has {len(ms)} matches, expected 6")
        for i, m in enumerate(ms):
            rnd = i // 2 + 1            # 0,1->R1  2,3->R2  4,5->R3
            res, g1, g2 = _result(m.get("score", {}).get("ft"))
            out.append({
                "grp": grp,
                "matchday": rnd,
                "date": m.get("date"),
                "team1": canon(m["team1"]),
                "team2": canon(m["team2"]),
                "actual_result": res,
                "actual_goals1": g1,
                "actual_goals2": g2,
            })
    return out


def _home_bonuses(team1: str, team2: str, neutral: int) -> tuple[float, float]:
    if neutral:
        return 0.0, 0.0
    if team1 in config.HOST_NATIONS:
        return config.ELO_HOME_ADV, 0.0
    if team2 in config.HOST_NATIONS:
        return 0.0, config.ELO_HOME_ADV
    return 0.0, 0.0


def build_elo(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or db.connect()
    try:
        db.init_db()  # ensure schema on the active DB
        _, ratings = elo.run_history(config.RESULTS_CSV, config.WC2026_START)
        matches = _load_group_matches(config.WORLDCUP_JSON)

        # ---- teams ----
        team_ids = {m["team1"] for m in matches} | {m["team2"] for m in matches}
        for tid in sorted(team_ids):
            conn.execute(
                "INSERT INTO teams (team_id, name, elo_pre, confederation) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(team_id) DO UPDATE SET elo_pre=excluded.elo_pre",
                (tid, tid, ratings.get(tid, config.ELO_INIT), None),
            )

        # ---- matches ----
        for m in matches:
            grp = m["grp"]
            group_half = 1 if grp in set("ABCDEF") else 2
            t1, t2 = m["team1"], m["team2"]
            neutral = 0 if (t1 in config.HOST_NATIONS or t2 in config.HOST_NATIONS) else 1
            h1, h2 = _home_bonuses(t1, t2, neutral)
            elo_diff = (ratings.get(t1, config.ELO_INIT) + h1) - (ratings.get(t2, config.ELO_INIT) + h2)
            match_id = f"{grp}-md{m['matchday']}-{t1}-vs-{t2}".replace(" ", "_")

            # Upsert fixture + actual columns ONLY; preserve market_*/model_*.
            conn.execute(
                """
                INSERT INTO matches (
                    match_id, matchday, grp, group_half, date,
                    team1_id, team2_id, neutral, elo_diff,
                    actual_result, actual_goals1, actual_goals2
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(match_id) DO UPDATE SET
                    matchday=excluded.matchday, grp=excluded.grp,
                    group_half=excluded.group_half, date=excluded.date,
                    team1_id=excluded.team1_id, team2_id=excluded.team2_id,
                    neutral=excluded.neutral, elo_diff=excluded.elo_diff,
                    actual_result=excluded.actual_result,
                    actual_goals1=excluded.actual_goals1,
                    actual_goals2=excluded.actual_goals2
                """,
                (
                    match_id, m["matchday"], grp, group_half, m["date"],
                    t1, t2, neutral, elo_diff,
                    m["actual_result"], m["actual_goals1"], m["actual_goals2"],
                ),
            )
        conn.commit()
        n_played = sum(1 for m in matches if m["actual_result"])
        print(
            f"build-elo: {len(team_ids)} teams, {len(matches)} group matches "
            f"({n_played} with results so far)"
        )
    finally:
        if own:
            conn.close()


def refresh_results(conn: sqlite3.Connection | None = None) -> None:
    """Re-read worldcup.json and update actual_* for finished matches."""
    own = conn is None
    conn = conn or db.connect()
    try:
        matches = _load_group_matches(config.WORLDCUP_JSON)
        updated = 0
        for m in matches:
            if not m["actual_result"]:
                continue
            match_id = f"{m['grp']}-md{m['matchday']}-{m['team1']}-vs-{m['team2']}".replace(" ", "_")
            cur = conn.execute(
                "UPDATE matches SET actual_result=?, actual_goals1=?, actual_goals2=? "
                "WHERE match_id=?",
                (m["actual_result"], m["actual_goals1"], m["actual_goals2"], match_id),
            )
            updated += cur.rowcount
        conn.commit()
        print(f"refresh-results: updated {updated} matches with results")
    finally:
        if own:
            conn.close()
