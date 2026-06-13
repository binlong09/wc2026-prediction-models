"""SQLite schema (spec §5) and connection helper.

The active DB path comes from config.db_path(), which honors the WCBT_DB env
var. That indirection is what keeps the isolated synthetic smoke test (a temp
DB) from ever touching the real data/backtest.db.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    team_id       TEXT PRIMARY KEY,   -- canonical name, must match across sources
    name          TEXT NOT NULL,
    elo_pre       REAL,               -- Elo snapshot immediately pre-tournament
    confederation TEXT
);

CREATE TABLE IF NOT EXISTS matches (
    match_id     TEXT PRIMARY KEY,
    matchday     INTEGER NOT NULL,    -- 1,2,3 (group-stage ROUNDS, derived)
    grp          TEXT NOT NULL,       -- 'A'..'L'
    group_half   INTEGER NOT NULL,    -- 1 if grp in A..F else 2 (the split key)
    date         TEXT,
    team1_id     TEXT NOT NULL REFERENCES teams(team_id),
    team2_id     TEXT NOT NULL REFERENCES teams(team_id),
    neutral      INTEGER NOT NULL,    -- 0 only for host nations at home
    elo_diff     REAL,                -- (elo1 + home_adj) - elo2

    actual_result TEXT,               -- 'team1_win'|'draw'|'team2_win'|NULL
    actual_goals1 INTEGER,
    actual_goals2 INTEGER,

    model_pW1    REAL, model_pD REAL, model_pW2 REAL,
    market_pW1   REAL, market_pD REAL, market_pW2 REAL
);

-- per-version predictions live here so all three versions share one DB.
CREATE TABLE IF NOT EXISTS predictions (
    version      TEXT NOT NULL,       -- 'v1'|'v2'|'v3'
    match_id     TEXT NOT NULL REFERENCES matches(match_id),
    matchday     INTEGER NOT NULL,
    split        TEXT NOT NULL,       -- 'train'|'test'
    pW1 REAL, pD REAL, pW2 REAL,      -- calibrated, out-of-sample for test
    temperature  REAL,
    PRIMARY KEY (version, match_id)
);

-- Fixture -> Polymarket token map (addendum §2). Resolved once by
-- verify-market-map and read by fetch-market; the scheduler reads it too.
CREATE TABLE IF NOT EXISTS market_map (
    match_id     TEXT PRIMARY KEY REFERENCES matches(match_id),
    event_slug   TEXT,
    market_title TEXT,                -- human-readable, for the eyeball pass
    token_w1     TEXT,                -- "Yes" token of the team1-win market
    token_draw   TEXT,                -- "Yes" token of the draw market
    token_w2     TEXT,                -- "Yes" token of the team2-win market
    kickoff      TEXT,                -- ISO kickoff from Polymarket (scheduler)
    resolved_at  TEXT
);

-- Scorekeeper (NOT a learner). One immutable row per (version, match): the
-- pre-match model + vig-stripped market probabilities captured BEFORE kickoff.
-- The *_at-fault score columns + actual_result are filled in retrospectively
-- after results land. Probabilities and captured_at are never overwritten.
-- Kept deliberately separate from the `runs` backtest table.
CREATE TABLE IF NOT EXISTS match_log (
    match_id      TEXT NOT NULL REFERENCES matches(match_id),
    version       TEXT NOT NULL,          -- 'v1'|'v2'|'v3'
    model_pW1     REAL, model_pD REAL, model_pW2 REAL,    -- immutable
    market_pW1    REAL, market_pD REAL, market_pW2 REAL,  -- immutable (NULL if not entered yet)
    captured_at   TEXT NOT NULL,          -- pre-match timestamp (immutable)

    actual_result TEXT,                   -- filled after the fact
    brier_model   REAL, logloss_model REAL,
    brier_market  REAL, logloss_market REAL,
    PRIMARY KEY (version, match_id)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id    TEXT, version TEXT,     -- 'v1'|'v2'|'v3'
    matchday  INTEGER,                -- 1|2|3, or NULL for pooled
    split     TEXT,                   -- 'test' (and 'train' if logged)
    brier_model REAL, logloss_model REAL,
    brier_market REAL, logloss_market REAL,
    skill_brier REAL, skill_logloss REAL,
    skill_brier_lo REAL, skill_brier_hi REAL,     -- bootstrap 90% CI (pooled)
    skill_logloss_lo REAL, skill_logloss_hi REAL,
    n INTEGER, created_at TEXT
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path is not None else config.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after the original matches DDL; applied to pre-existing DBs.
_MATCHES_MIGRATIONS = {
    "market_source": "TEXT",        # 'manual-csv' | 'polymarket-auto'
    "market_captured_at": "TEXT",   # when the market price was captured
}


def _migrate(conn: sqlite3.Connection) -> None:
    have = {r[1] for r in conn.execute("PRAGMA table_info(matches)")}
    for col, decl in _MATCHES_MIGRATIONS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE matches ADD COLUMN {col} {decl}")


def init_db(path: Path | None = None) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()
