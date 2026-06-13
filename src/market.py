"""load-market (spec §4C, §10).

Reads a hand-entered market CSV for one round and writes NORMALIZED implied
probabilities into matches.market_*. Raw market prices sum to >1 (the
overround/vig); normalizing the three to sum to 1 is critical, otherwise the
market looks artificially overconfident and the comparison is unfair.

CSV columns: group,team1,team2,market_pW1,market_pD,market_pW2
"""
from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import build  # for canon()
import config
import db


def _normalize(p1: float, pd_: float, p2: float) -> tuple[float, float, float]:
    s = p1 + pd_ + p2
    if s <= 0:
        raise ValueError("market probabilities sum to <= 0")
    return p1 / s, pd_ / s, p2 / s


def load_market(matchday: int, conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or db.connect()
    path = config.MARKET / f"matchday_{matchday}.csv"
    if not Path(path).exists():
        print(f"load-market: {path} not found — nothing to load")
        if own:
            conn.close()
        return
    try:
        loaded = 0
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                grp = row["group"].replace("Group", "").strip()
                t1, t2 = build.canon(row["team1"]), build.canon(row["team2"])
                p1, pd_, p2 = _normalize(
                    float(row["market_pW1"]), float(row["market_pD"]), float(row["market_pW2"])
                )
                # match in stored orientation; if the CSV lists teams reversed,
                # swap the win probabilities to match team1/team2 in the DB.
                m = conn.execute(
                    "SELECT match_id, team1_id, team2_id FROM matches "
                    "WHERE grp=? AND ((team1_id=? AND team2_id=?) OR (team1_id=? AND team2_id=?))",
                    (grp, t1, t2, t2, t1),
                ).fetchone()
                if m is None:
                    print(f"  warn: no match for {grp} {t1} vs {t2}; skipped")
                    continue
                if m["team1_id"] == t1:
                    mp1, mpd, mp2 = p1, pd_, p2
                else:  # reversed
                    mp1, mpd, mp2 = p2, pd_, p1
                # Manual load is the human override path: it MAY overwrite an
                # existing (incl. auto) price. fetch-market never does.
                conn.execute(
                    "UPDATE matches SET market_pW1=?, market_pD=?, market_pW2=?, "
                    "market_source='manual-csv', market_captured_at=? WHERE match_id=?",
                    (mp1, mpd, mp2, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     m["match_id"]),
                )
                loaded += 1
        conn.commit()
        print(f"load-market: loaded + normalized {loaded} rows from matchday_{matchday}.csv")
    finally:
        if own:
            conn.close()
