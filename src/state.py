"""Text-state persistence — the durable, irreplaceable state as readable CSVs.

`data/backtest.db` is a *rebuildable working file* (not committed). The state
that CANNOT be regenerated lives as human-readable, git-diffable CSVs under
`data/state/` and is committed instead:

  * market_map.csv       — fixture -> Polymarket token mapping (+ kickoff)
  * market_snapshots.csv — the pre-match prices captured before kickoff
  * match_log.csv        — the immutable pre-match model + market log (scorelog)

Everything else is regenerated each run from external sources + computation and
so is NOT persisted here: teams.elo_pre + matches fixtures (martj42 history +
worldcup.json), matches.actual_result (re-fetched via refresh-results), the
backtest runs/predictions, match_log scores (recomputed by score-log), and
report/companion.json.

CSV rows are sorted by primary key and floats rounded, so a single changed value
produces a single readable line in `git diff` — and a no-op run rewrites the
files byte-identically (no spurious commits).
"""
from __future__ import annotations

import csv
import sqlite3

import config
import db

STATE_DIR = config.DATA / "state"
MARKET_MAP_CSV = STATE_DIR / "market_map.csv"
SNAPSHOTS_CSV = STATE_DIR / "market_snapshots.csv"
MATCH_LOG_CSV = STATE_DIR / "match_log.csv"

_ROUND = 6  # decimals for probabilities — plenty for scoring, clean to read


def _p(x) -> str:
    """Probability cell: rounded, or '' for NULL."""
    return "" if x is None else f"{round(float(x), _ROUND)}"


def _write(path, header: list[str], rows) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _read(path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _f(v):
    return float(v) if v not in (None, "") else None


def _s(v):
    return v if v not in (None, "") else None


# --------------------------------------------------------------------------- #
# Export                                                                        #
# --------------------------------------------------------------------------- #
def export_state(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or db.connect()
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)

        mm = conn.execute(
            "SELECT match_id, event_slug, market_title, token_w1, token_draw, "
            "token_w2, kickoff, resolved_at FROM market_map ORDER BY match_id"
        ).fetchall()
        _write(
            MARKET_MAP_CSV,
            ["match_id", "event_slug", "market_title", "token_w1", "token_draw",
             "token_w2", "kickoff", "resolved_at"],
            [[r[k] if r[k] is not None else "" for k in r.keys()] for r in mm],
        )

        sn = conn.execute(
            "SELECT match_id, market_pW1, market_pD, market_pW2, market_source, "
            "market_captured_at FROM matches WHERE market_pW1 IS NOT NULL ORDER BY match_id"
        ).fetchall()
        _write(
            SNAPSHOTS_CSV,
            ["match_id", "market_pW1", "market_pD", "market_pW2", "market_source", "market_captured_at"],
            [[r["match_id"], _p(r["market_pW1"]), _p(r["market_pD"]), _p(r["market_pW2"]),
              r["market_source"] or "", r["market_captured_at"] or ""] for r in sn],
        )

        ml = conn.execute(
            "SELECT version, match_id, model_pW1, model_pD, model_pW2, market_pW1, "
            "market_pD, market_pW2, captured_at FROM match_log ORDER BY match_id, version"
        ).fetchall()
        _write(
            MATCH_LOG_CSV,
            ["version", "match_id", "model_pW1", "model_pD", "model_pW2",
             "market_pW1", "market_pD", "market_pW2", "captured_at"],
            [[r["version"], r["match_id"], _p(r["model_pW1"]), _p(r["model_pD"]), _p(r["model_pW2"]),
              _p(r["market_pW1"]), _p(r["market_pD"]), _p(r["market_pW2"]), r["captured_at"] or ""]
             for r in ml],
        )
        print(f"export-state: {len(mm)} map, {len(sn)} snapshots, {len(ml)} log rows -> data/state/")
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------- #
# Import (apply persisted state onto a freshly-built DB)                        #
# --------------------------------------------------------------------------- #
def import_state(conn: sqlite3.Connection | None = None) -> None:
    """Apply the committed CSV state onto the DB. Run AFTER build-elo (so the
    teams/matches rows the snapshots attach to already exist)."""
    own = conn is None
    conn = conn or db.connect()
    try:
        db.init_db()

        for r in _read(MARKET_MAP_CSV):
            conn.execute(
                "INSERT INTO market_map (match_id, event_slug, market_title, token_w1, "
                "token_draw, token_w2, kickoff, resolved_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(match_id) DO UPDATE SET event_slug=excluded.event_slug, "
                "market_title=excluded.market_title, token_w1=excluded.token_w1, "
                "token_draw=excluded.token_draw, token_w2=excluded.token_w2, "
                "kickoff=excluded.kickoff, resolved_at=excluded.resolved_at",
                (r["match_id"], _s(r["event_slug"]), _s(r["market_title"]), _s(r["token_w1"]),
                 _s(r["token_draw"]), _s(r["token_w2"]), _s(r["kickoff"]), _s(r["resolved_at"])),
            )

        for r in _read(SNAPSHOTS_CSV):
            conn.execute(
                "UPDATE matches SET market_pW1=?, market_pD=?, market_pW2=?, "
                "market_source=?, market_captured_at=? WHERE match_id=?",
                (_f(r["market_pW1"]), _f(r["market_pD"]), _f(r["market_pW2"]),
                 _s(r["market_source"]), _s(r["market_captured_at"]), r["match_id"]),
            )

        for r in _read(MATCH_LOG_CSV):
            # immutable capture; scores are recomputed by score-log
            conn.execute(
                "INSERT OR IGNORE INTO match_log (version, match_id, model_pW1, model_pD, "
                "model_pW2, market_pW1, market_pD, market_pW2, captured_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (r["version"], r["match_id"], _f(r["model_pW1"]), _f(r["model_pD"]),
                 _f(r["model_pW2"]), _f(r["market_pW1"]), _f(r["market_pD"]),
                 _f(r["market_pW2"]), _s(r["captured_at"])),
            )
        conn.commit()
        print(
            f"import-state: applied {len(_read(MARKET_MAP_CSV))} map, "
            f"{len(_read(SNAPSHOTS_CSV))} snapshots, {len(_read(MATCH_LOG_CSV))} log rows"
        )
    finally:
        if own:
            conn.close()
