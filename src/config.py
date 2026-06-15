"""Central configuration: paths and tunables.

Everything that the spec says should be "config-driven" (Elo K, home advantage,
fallback-prior constants) lives here so the rest of the code reads clean.
"""
from __future__ import annotations

import os
from pathlib import Path

# ---- Paths -----------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
MARKET = DATA / "market"
REPORT = ROOT / "report"

DB_PATH = DATA / "backtest.db"          # the real, canonical DB

RESULTS_CSV = RAW / "international_results.csv"
WORLDCUP_JSON = RAW / "worldcup_2026.json"

# ---- Data source URLs (verified at build time, 2026-06-12) -----------------
# NOTE: the spec's hinted martj42 path uses a hyphen ("international-results")
# which 404s. The working mirror is the UNDERSCORE repo. See README.
RESULTS_URL = (
    "https://raw.githubusercontent.com/martj42/international_results/"
    "master/results.csv"
)
WORLDCUP_URL = (
    "https://raw.githubusercontent.com/openfootball/worldcup.json/"
    "master/2026/worldcup.json"
)

# ---- Elo tunables (spec §6) ------------------------------------------------
ELO_INIT = 1500.0
ELO_HOME_ADV = 65.0           # H, Elo points for a true home team; 0 if neutral
ELO_K_DEFAULT = 30.0          # ordinary qualifiers / minor tournaments
ELO_K_FRIENDLY = 20.0         # friendlies move ratings less
ELO_K_MAJOR = 40.0            # World Cups / continental finals
ELO_GD_SCALING = True         # goal-difference-scaled K multiplier

# v2 (later): modest K for in-tournament nudging. Defined now, unused in v1.
ELO_K_INTOURNAMENT = 20.0

# ---- Fallback-prior constants (spec §6, weaker option) ---------------------
FALLBACK_D_MAX = 0.28
FALLBACK_C = 120.0

# ---- Pre-tournament cutoff -------------------------------------------------
# Elo is snapshotted from all matches strictly BEFORE the first WC2026 game.
WC2026_START = "2026-06-11"

# ---- Host nations: play some group games at home (neutral = 0) -------------
HOST_NATIONS = {"United States", "Canada", "Mexico"}

# ---- Bootstrap ----
BOOTSTRAP_DRAWS = 10_000
BOOTSTRAP_SEED = 20260611

# ---- Market snapshot scheduler (addendum Stage 2) --------------------------
# ALL timing keys off the stored Polymarket gameStartTime (UTC), never the slug
# date — many fixtures kick off the UTC day after their local/slug date.
SNAPSHOT_WINDOW_MIN = 90     # snapshot a fixture once kickoff is within this many minutes
SNAPSHOT_ALERT_MIN = 30      # uncaptured this close to (or past) kickoff -> exit non-zero
SNAPSHOT_MISS_GRACE_MIN = 180  # keep noting a missed (past-kickoff) capture this long, then stop

# ---- Backfill (recover a missed pre-match price from CLOB prices-history) ---
# Polymarket keeps a per-token price time-series (even for resolved markets), so
# a missed snapshot is recoverable. Backfill reads the price this many minutes
# before kickoff, using only pre-kickoff points (never an in-play price).
BACKFILL_TARGET_MIN = 60     # target read ~60 min pre-kickoff (a settled pre-match read)
BACKFILL_FIDELITY_MIN = 10   # prices-history bucket size (minutes)


def db_path() -> Path:
    """Active DB path. Override with WCBT_DB env var (used by the ISOLATED
    synthetic smoke test so it can never touch the real backtest.db)."""
    override = os.environ.get("WCBT_DB")
    return Path(override) if override else DB_PATH
