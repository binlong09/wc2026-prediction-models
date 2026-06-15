"""Market-snapshot scheduler (addendum §3, Stage 2).

A thin polling wrapper around fetch-market's capture, meant to run on a frequent
GitHub Actions cron (every 30–60 min). Each run snapshots any fixture whose
kickoff is within the window and that isn't already captured; idempotent and
insert-once, so a delayed or skipped run self-heals on the next one.

TIMING — read this before changing anything:
  Every timing decision keys off the stored Polymarket **gameStartTime** (UTC),
  NEVER the slug date or our local fixture date. This is not cosmetic: many
  fixtures kick off the UTC day *after* their slug/local date (e.g.
  `fifwc-arg-alg-2026-06-16` kicks off 2026-06-17 01:00 UTC), and the three
  Stage-1 "timezone-artifact" matches (slug date off by a day) likewise only
  snapshot on the correct day when keyed off gameStartTime. Keying off the slug
  date would capture them on the wrong day. See parse_kickoff() + _classify().

GUARDS:
  * Hard past-kickoff guard: never snapshot a fixture whose gameStartTime is
    already in the past (would capture an in-play / settling price).
  * insert-once: never overwrite an existing snapshot (any source); the manual
    load-market override path is never touched.

ALERT (no Resend — the failed-workflow email is the alert):
  If a fixture is still uncaptured and unplayed within SNAPSHOT_ALERT_MIN of
  kickoff (or up to SNAPSHOT_MISS_GRACE_MIN past it), the run logs loudly and
  returns a non-zero exit code so GitHub Actions emails you in time to hand-enter
  via load-market.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import config
import db
import fetch_market as fm
import polymarket as pm

_OFFSET_NO_MIN = re.compile(r"([+-]\d{2})$")  # '+00' -> needs ':00'


def parse_kickoff(raw: str | None) -> datetime | None:
    """Parse Polymarket's gameStartTime ('2026-06-15 19:00:00+00') to an
    aware UTC datetime. Returns None if absent/unparseable."""
    if not raw:
        return None
    s = raw.strip().replace(" ", "T")
    s = _OFFSET_NO_MIN.sub(r"\1:00", s)  # '+00' -> '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class Fixture:
    match_id: str
    label: str
    event_slug: str
    kickoff: datetime | None
    captured: bool
    played: bool
    token_w1: str
    token_draw: str
    token_w2: str


def _load(conn: sqlite3.Connection) -> list[Fixture]:
    rows = conn.execute(
        "SELECT m.match_id, m.grp, m.team1_id, m.team2_id, m.actual_result, "
        "m.market_pW1, mm.event_slug, mm.kickoff, mm.token_w1, mm.token_draw, mm.token_w2 "
        "FROM matches m JOIN market_map mm ON mm.match_id = m.match_id"
    ).fetchall()
    out = []
    for r in rows:
        out.append(Fixture(
            match_id=r["match_id"],
            label=f"{r['grp']} {r['team1_id']} vs {r['team2_id']}",
            event_slug=r["event_slug"],
            kickoff=parse_kickoff(r["kickoff"]),
            captured=r["market_pW1"] is not None,
            played=r["actual_result"] is not None,
            token_w1=r["token_w1"], token_draw=r["token_draw"], token_w2=r["token_w2"],
        ))
    return out


def snapshot_due(
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
    window_min: int | None = None,
    alert_min: int | None = None,
    miss_grace_min: int | None = None,
) -> int:
    """Run one polling pass. Returns a process exit code (0 ok, 1 = alert)."""
    own = conn is None
    conn = conn or db.connect()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    window = timedelta(minutes=window_min if window_min is not None else config.SNAPSHOT_WINDOW_MIN)
    alert = timedelta(minutes=alert_min if alert_min is not None else config.SNAPSHOT_ALERT_MIN)
    grace = timedelta(minutes=miss_grace_min if miss_grace_min is not None else config.SNAPSHOT_MISS_GRACE_MIN)

    try:
        db.init_db()
        fixtures = _load(conn)
        if not fixtures:
            print("snapshot-due: market_map is empty — run verify-market-map first.")
            return 0

        print(f"snapshot-due @ {now.isoformat(timespec='seconds')}  "
              f"(window={int(window.total_seconds()//60)}m, alert={int(alert.total_seconds()//60)}m)")

        captured = already = played = not_yet = no_kickoff = failed = 0
        # alerts: ACTIONABLE pre-kickoff misses -> non-zero exit (the email).
        #   Per the spec, the alert is "uncaptured ~30 min BEFORE kickoff" so you
        #   can still hand-enter via load-market in time. Only these fail the run.
        # misses: past-kickoff / played / unschedulable -> loud WARN, exit 0.
        #   A pre-match price after kickoff is unrecoverable; failing the run on it
        #   isn't actionable and would just spam non-actionable failure emails.
        alerts: list[str] = []
        misses: list[str] = []
        cli = pm.client()
        try:
            for f in fixtures:
                ko = f.kickoff
                if f.captured:
                    already += 1
                    continue
                if f.played:
                    # uncaptured but already played: pre-match price is gone.
                    played += 1
                    if ko is not None and now - ko <= grace:
                        misses.append(f"MISSED (played, uncaptured): {f.label} koff {ko.isoformat()}")
                    continue
                if ko is None:
                    no_kickoff += 1
                    misses.append(f"NO KICKOFF in market_map: {f.label} — cannot schedule")
                    continue

                # HARD GUARD: never snapshot a fixture already kicked off.
                if ko < now:
                    # permanent miss; loudly note recent ones, quietly skip old ones
                    if now - ko <= grace:
                        misses.append(
                            f"MISSED (kickoff passed, uncaptured): {f.label} "
                            f"koff {ko.isoformat()} ({_mins(now - ko)}m ago)")
                    else:
                        print(f"  past   {f.label}: kickoff {ko.isoformat()} long past — skip")
                    continue

                mins_to_ko = ko - now
                if mins_to_ko <= window:
                    # DUE — within the snapshot window and not yet kicked off
                    res = fm.capture_one(conn, f.match_id, f.token_w1, f.token_draw,
                                         f.token_w2, cli)
                    if res["status"] == "captured":
                        captured += 1
                        p = res["probs"]
                        print(f"  CAP    {f.label}  koff {ko.isoformat()} "
                              f"(T-{_mins(mins_to_ko)}m)  pW1={p[0]:.3f} pD={p[1]:.3f} pW2={p[2]:.3f}")
                    elif res["status"] == "failed":
                        failed += 1
                        print(f"  FAIL   {f.label}: {res['reason']}")
                        # only actionable if still BEFORE kickoff within the horizon
                        if mins_to_ko <= alert:
                            alerts.append(f"IMMINENT, capture FAILED: {f.label} "
                                          f"kicks off in {_mins(mins_to_ko)}m")
                    else:
                        already += 1
                else:
                    not_yet += 1
                    # not in the window yet, but if it's already inside the alert
                    # horizon and still uncaptured something is wrong
                    if mins_to_ko <= alert:
                        alerts.append(f"IMMINENT, uncaptured: {f.label} "
                                      f"kicks off in {_mins(mins_to_ko)}m")
            conn.commit()
        finally:
            cli.close()

        print(f"snapshot-due: captured={captured} already={already} not-yet={not_yet} "
              f"played={played} failed={failed} no-kickoff={no_kickoff}")

        # Recent non-actionable misses: log loudly for visibility, but do NOT
        # fail the run (kickoff has passed — nothing to hand-enter in time).
        if misses:
            print("\n" + "-" * 70)
            print(f"NOTE: {len(misses)} uncaptured fixture(s) past kickoff or unschedulable "
                  "(not actionable — logged, run not failed):")
            for m in misses:
                print(f"  ~~ {m}")
            print("-" * 70)

        # Actionable pre-kickoff alerts: fail the run so the email fires in time.
        if alerts:
            print("\n" + "=" * 70)
            print(f"ALERT: {len(alerts)} fixture(s) uncaptured close to kickoff "
                  f"(hand-enter via `load-market` BEFORE kickoff):")
            for a in alerts:
                print(f"  !! {a}")
            print("=" * 70)
            print("Exiting non-zero so the failed-workflow email fires.")
            return 1
        return 0
    finally:
        if own:
            conn.close()


def _mins(td: timedelta) -> int:
    return int(round(td.total_seconds() / 60))
