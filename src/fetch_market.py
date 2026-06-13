"""Automated market snapshot — Stage 1 manual commands (addendum).

  verify-market-map : resolve all 72 fixtures -> market_map, print each fixture
                      next to its resolved Polymarket title for a human eyeball
                      pass before anything trusts the mapping.
  fetch-market -m N : read the map, fetch the three Yes-token midpoints per
                      fixture, de-vig (same normalization as load-market), and
                      write market_pW1/pD/pW2 tagged source='polymarket-auto'
                      with captured_at. INSERT-ONCE: never overwrites an existing
                      snapshot (the pre-match price is frozen once captured).

load-market stays the manual fallback. The scheduler (Stage 2) is NOT built yet.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import config
import db
import polymarket as pm


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _grp_letter(grp: str) -> str:
    return grp


def _fixtures(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT match_id, matchday, grp, team1_id, team2_id, date, "
        "actual_result, market_pW1, market_source FROM matches ORDER BY date, grp"
    ).fetchall()


# --------------------------------------------------------------------------- #
# verify-market-map — resolve + store + print for the human eyeball pass        #
# --------------------------------------------------------------------------- #
def verify_market_map(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or db.connect()
    try:
        db.init_db()
        valid = {r["team_id"] for r in conn.execute("SELECT team_id FROM teams")}
        if not valid:
            print("verify-market-map: no teams in DB — run build-elo first.")
            return

        print("verify-market-map: enumerating World Cup events from Polymarket …")
        cli = pm.client()
        try:
            index, unmapped = pm.build_event_index(cli, valid_ids=valid)
        finally:
            cli.close()
        print(f"  resolved {len(index)} distinct team-set events from the tag.")
        if unmapped:
            print(f"  ⚠ UNMAPPED Polymarket team names (add to PM_NAME_MAP): {unmapped}")

        fixtures = _fixtures(conn)
        resolved_at = _now()
        ok = miss = 0
        print(
            f"\n{'grp':>3} {'md':>2} {'fixture':<42} {'date':<11} "
            f"{'':1} {'resolved Polymarket title':<34} {'date'}"
        )
        print("-" * 104)
        for f in fixtures:
            res = pm.resolve_fixture(f["team1_id"], f["team2_id"], f["date"], index)
            fixture = f"{f['team1_id']} vs {f['team2_id']}"
            if res.get("matched"):
                ok += 1
                conn.execute(
                    "INSERT INTO market_map "
                    "(match_id, event_slug, market_title, token_w1, token_draw, token_w2, "
                    " kickoff, resolved_at) VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(match_id) DO UPDATE SET "
                    "event_slug=excluded.event_slug, market_title=excluded.market_title, "
                    "token_w1=excluded.token_w1, token_draw=excluded.token_draw, "
                    "token_w2=excluded.token_w2, kickoff=excluded.kickoff, "
                    "resolved_at=excluded.resolved_at",
                    (f["match_id"], res["event_slug"], res["title"], res["token_w1"],
                     res["token_draw"], res["token_w2"], res["kickoff"], resolved_at),
                )
                flag = "✓" if res["date_match"] else "~"  # ~ = team-set match, date off (TZ)
                print(
                    f"{f['grp']:>3} {f['matchday']:>2} {fixture:<42} {f['date']:<11} "
                    f"{flag:1} {(res['title'] or ''):<34} {res['event_slug'][-10:]}"
                )
            else:
                miss += 1
                print(
                    f"{f['grp']:>3} {f['matchday']:>2} {fixture:<42} {f['date']:<11} "
                    f"{'✗':1} NO MATCH — {res.get('reason')}"
                )
        conn.commit()
        print("-" * 104)
        print(f"verify-market-map: {ok}/{len(fixtures)} fixtures mapped, {miss} unresolved.")
        print("  ✓ = team-set + date match   ~ = team-set match, slug date off by TZ (benign)")
        if miss or unmapped:
            print("  Fix unresolved rows before trusting the scheduler.")
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------- #
# fetch-market — capture midpoints, de-vig, store (insert-once)                  #
# --------------------------------------------------------------------------- #
def _normalize(p1: float, pd_: float, p2: float) -> tuple[float, float, float]:
    s = p1 + pd_ + p2
    if s <= 0:
        raise ValueError("midpoints sum to <= 0")
    return p1 / s, pd_ / s, p2 / s


def capture_one(conn, match_id, token_w1, token_draw, token_w2, cli) -> dict:
    """Fetch + de-vig + insert-once write for ONE fixture.

    Additive helper shared by the Stage-2 scheduler (scheduler.py). It does NOT
    decide whether a fixture is *due* — the caller owns the kickoff-window logic;
    this only performs the capture, mirroring fetch_market()'s per-match body.
    insert-once is enforced in SQL (`WHERE market_pW1 IS NULL`) so a repeat or
    concurrent run can never overwrite, and load-market's manual override is
    never on this path. Returns {status: captured|failed|skipped, ...}.
    """
    m1 = pm.midpoint(token_w1, cli=cli)
    md = pm.midpoint(token_draw, cli=cli)
    m2 = pm.midpoint(token_w2, cli=cli)
    if None in (m1, md, m2):
        return {"status": "failed", "reason": f"missing midpoint w1={m1} d={md} w2={m2}"}
    p1, pd_, p2 = _normalize(m1, md, m2)
    cur = conn.execute(
        "UPDATE matches SET market_pW1=?, market_pD=?, market_pW2=?, "
        "market_source='polymarket-auto', market_captured_at=? "
        "WHERE match_id=? AND market_pW1 IS NULL",
        (p1, pd_, p2, _now(), match_id),
    )
    if cur.rowcount:
        return {"status": "captured", "raw": (m1, md, m2), "probs": (p1, pd_, p2)}
    return {"status": "skipped", "reason": "already captured (race)"}


def fetch_market(matchday: int, conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    conn = conn or db.connect()
    try:
        db.init_db()
        rows = conn.execute(
            "SELECT m.match_id, m.grp, m.team1_id, m.team2_id, m.date, "
            "m.actual_result, m.market_pW1, m.market_source, "
            "mm.token_w1, mm.token_draw, mm.token_w2, mm.event_slug "
            "FROM matches m JOIN market_map mm ON mm.match_id = m.match_id "
            "WHERE m.matchday = ? ORDER BY m.date, m.grp",
            (matchday,),
        ).fetchall()
        if not rows:
            print(f"fetch-market: no mapped fixtures for matchday {matchday}. "
                  "Run verify-market-map first.")
            return {"checked": 0, "captured": 0, "skipped": 0, "failed": 0}

        captured = skipped = failed = 0
        cli = pm.client()
        try:
            for r in rows:
                fixture = f"{r['grp']} {r['team1_id']} vs {r['team2_id']}"
                # insert-once: never overwrite an existing snapshot (any source)
                if r["market_pW1"] is not None:
                    print(f"  skip  {fixture}: already captured (source={r['market_source']})")
                    skipped += 1
                    continue
                # Stage-1 safety: don't snapshot a finished match (would capture a
                # settled price). The precise past-kickoff window guard is Stage 2.
                if r["actual_result"] is not None:
                    print(f"  skip  {fixture}: already played (no pre-match price possible)")
                    skipped += 1
                    continue

                m1 = pm.midpoint(r["token_w1"], cli=cli)
                md = pm.midpoint(r["token_draw"], cli=cli)
                m2 = pm.midpoint(r["token_w2"], cli=cli)
                if None in (m1, md, m2):
                    print(f"  FAIL  {fixture}: missing midpoint(s) "
                          f"(w1={m1}, draw={md}, w2={m2})")
                    failed += 1
                    continue

                p1, pd_, p2 = _normalize(m1, md, m2)
                # insert-once guard in SQL too (race-safe): only write if still NULL
                cur = conn.execute(
                    "UPDATE matches SET market_pW1=?, market_pD=?, market_pW2=?, "
                    "market_source='polymarket-auto', market_captured_at=? "
                    "WHERE match_id=? AND market_pW1 IS NULL",
                    (p1, pd_, p2, _now(), r["match_id"]),
                )
                if cur.rowcount:
                    captured += 1
                    print(
                        f"  CAP   {fixture}  [{r['event_slug']}]\n"
                        f"          raw midpoints  w1={m1:.3f} draw={md:.3f} w2={m2:.3f} "
                        f"(sum {m1+md+m2:.3f})\n"
                        f"          de-vigged      pW1={p1:.3f} pD={pd_:.3f} pW2={p2:.3f}"
                    )
                else:
                    skipped += 1
            conn.commit()
        finally:
            cli.close()

        result = {"checked": len(rows), "captured": captured, "skipped": skipped, "failed": failed}
        print(f"fetch-market md{matchday}: checked={result['checked']} "
              f"captured={captured} skipped={skipped} failed={failed}")
        return result
    finally:
        if own:
            conn.close()
