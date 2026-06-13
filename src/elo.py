"""Chronological Elo engine (spec §6).

Standard Elo with a configurable, tournament-weighted K and an optional
goal-difference multiplier (World Football Elo style). Running it over the full
martj42 history produces BOTH:

  1. `elo_pre` — each team's rating snapshot immediately before WC2026, and
  2. the historical training features `(elo_diff, neutral, outcome)` that the
     multinomial-logit prior in model.py is fit on.

Crucially, the feature row for a match is computed from the ratings *as they
stand before that match* — i.e. genuinely out-of-sample in time.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

import config

# Continental finals + the World Cup itself → K_MAJOR. Matched as substrings
# (case-insensitive) against the martj42 `tournament` column.
_MAJOR_KEYS = (
    "fifa world cup",        # but NOT "qualification" — handled below
    "uefa euro",
    "copa américa",
    "copa america",
    "african cup of nations",
    "afc asian cup",
    "gold cup",
    "confederations cup",
    "oceania nations cup",
)


def classify_k(tournament: str) -> float:
    """Pick the base K for a tournament name (spec §6: 40 majors / 20 friendly /
    ~30 otherwise)."""
    t = (tournament or "").strip().lower()
    if t == "friendly":
        return config.ELO_K_FRIENDLY
    if "qualification" in t or "qualifier" in t:
        return config.ELO_K_DEFAULT
    for key in _MAJOR_KEYS:
        if key in t:
            return config.ELO_K_MAJOR
    return config.ELO_K_DEFAULT


def gd_multiplier(goal_diff: int) -> float:
    """World Football Elo goal-difference weighting. 1.0 for a 0/1-goal margin,
    growing slowly for blowouts so a 6-0 doesn't swing ratings absurdly."""
    g = abs(goal_diff)
    if g <= 1:
        return 1.0
    if g == 2:
        return 1.5
    if g == 3:
        return 1.75
    return 1.75 + (g - 3) / 8.0


def expected_score(r1: float, r2: float, home_adj: float) -> float:
    """We for team1: 1 / (1 + 10**(-(R1 + H - R2)/400))."""
    return 1.0 / (1.0 + 10.0 ** (-((r1 + home_adj) - r2) / 400.0))


@dataclass
class EloEngine:
    ratings: dict[str, float] = field(default_factory=dict)
    home_adv: float = config.ELO_HOME_ADV
    gd_scaling: bool = config.ELO_GD_SCALING

    def rating(self, team: str) -> float:
        return self.ratings.get(team, config.ELO_INIT)

    def update_one(
        self,
        team1: str,
        team2: str,
        score1: int,
        score2: int,
        neutral: bool,
        tournament: str,
        k_override: float | None = None,
    ) -> dict:
        """Apply one match. Returns the pre-update feature row + the resulting
        ratings. team1 is the home side (martj42 home_team)."""
        r1, r2 = self.rating(team1), self.rating(team2)
        home_adj = 0.0 if neutral else self.home_adv

        we1 = expected_score(r1, r2, home_adj)
        elo_diff = (r1 + home_adj) - r2

        if score1 > score2:
            s1, outcome = 1.0, "team1_win"
        elif score1 < score2:
            s1, outcome = 0.0, "team2_win"
        else:
            s1, outcome = 0.5, "draw"

        base_k = k_override if k_override is not None else classify_k(tournament)
        k = base_k * (gd_multiplier(score1 - score2) if self.gd_scaling else 1.0)

        delta = k * (s1 - we1)
        self.ratings[team1] = r1 + delta
        self.ratings[team2] = r2 - delta

        return {
            "elo_diff": elo_diff,
            "neutral": int(bool(neutral)),
            "outcome": outcome,
            "we1": we1,
        }


def _coerce_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"true", "1", "yes"}


def run_history(
    results_csv, cutoff_date: str
) -> tuple[pd.DataFrame, dict[str, float]]:
    """Replay all internationals strictly before `cutoff_date`.

    Returns (features_df, ratings_at_cutoff). features_df has one row per
    processed match with columns [date, elo_diff, neutral, outcome].
    """
    df = pd.read_csv(results_csv)
    df = df.dropna(subset=["home_score", "away_score"])
    df = df[df["date"] < cutoff_date].sort_values("date", kind="stable")

    engine = EloEngine()
    rows = []
    for r in df.itertuples(index=False):
        feat = engine.update_one(
            team1=r.home_team,
            team2=r.away_team,
            score1=int(r.home_score),
            score2=int(r.away_score),
            neutral=_coerce_bool(r.neutral),
            tournament=r.tournament,
        )
        feat["date"] = r.date
        rows.append(feat)

    features = pd.DataFrame(rows, columns=["date", "elo_diff", "neutral", "outcome", "we1"])
    print(
        f"elo: replayed {len(features):,} matches up to {cutoff_date}; "
        f"{len(engine.ratings):,} teams rated"
    )
    return features, dict(engine.ratings)
