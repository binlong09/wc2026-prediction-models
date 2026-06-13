"""Prediction models (spec §6).

One Protocol, three versions slot behind it. Only v1 is implemented here; the
seam (predict/update/reset over an internal Elo state) is shaped so v2/v3 add
behaviour to `update` without the backtest loop ever changing.

Design note that makes that seam real: a model carries its OWN mutable Elo
ratings dict and recomputes `elo_diff` from it at predict time. For v1 the
ratings never move, so this equals the stored snapshot. For v2 (later),
`update` nudges the ratings and `predict` reflects it automatically — the loop
and `predict` stay byte-for-byte identical across versions.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

import config
import elo

# Outcome label space (sklearn sorts these alphabetically into classes_).
TEAM1_WIN, DRAW, TEAM2_WIN = "team1_win", "draw", "team2_win"


@dataclass
class Match:
    """A WC2026 group match as carried through predict/backtest."""
    match_id: str
    matchday: int            # group ROUND 1/2/3 (derived)
    grp: str                 # 'A'..'L'
    group_half: int          # 1 = A–F (train), 2 = G–L (test)
    team1_id: str
    team2_id: str
    neutral: int             # 0 for host home games
    elo_diff: float          # stored snapshot (reference; predict recomputes)
    actual_result: str | None = None
    actual_goals1: int | None = None
    actual_goals2: int | None = None


@runtime_checkable
class Model(Protocol):
    def predict(self, match: Match) -> tuple[float, float, float]:
        """(pW1, pD, pW2), each > 0, summing to 1. Uses current Elo state."""

    def update(self, played_matches: Sequence[Match]) -> None:
        """Fold a round's actual results into state. v1: no-op."""

    def reset(self) -> None:
        """Restore the pre-tournament prior. Called once before each run."""


# --------------------------------------------------------------------------- #
# The shared prior: multinomial logistic on [elo_diff, neutral] over history.   #
# --------------------------------------------------------------------------- #
class Prior:
    """Multinomial-logit prior, fit once over the full international history.

    This *is* the v1 prior; v2/v3 inherit the identical fitted object and only
    differ in how their Elo state evolves.
    """

    def __init__(self, clf: LogisticRegression):
        self.clf = clf
        # column index of each outcome in clf.classes_
        self._idx = {c: i for i, c in enumerate(clf.classes_)}

    @classmethod
    def fit(cls, features: pd.DataFrame) -> "Prior":
        X = features[["elo_diff", "neutral"]].to_numpy(dtype=float)
        y = features["outcome"].to_numpy()
        # near-unregularized: with ~49k rows and 2 features, L2 is irrelevant;
        # large C keeps the prior honestly calibrated to the data.
        clf = LogisticRegression(C=1e6, solver="lbfgs", max_iter=2000)
        clf.fit(X, y)
        return cls(clf)

    def prob(self, elo_diff: float, neutral: int) -> tuple[float, float, float]:
        p = self.clf.predict_proba(np.array([[elo_diff, neutral]], dtype=float))[0]
        return (
            float(p[self._idx[TEAM1_WIN]]),
            float(p[self._idx[DRAW]]),
            float(p[self._idx[TEAM2_WIN]]),
        )


# --------------------------------------------------------------------------- #
# Base class holding the shared Elo-state mechanics; versions subclass it.      #
# --------------------------------------------------------------------------- #
class _EloStateModel:
    """Common machinery: holds a mutable ratings dict + the frozen snapshot it
    resets to, and computes elo_diff (with host home advantage) for predict."""

    def __init__(
        self,
        prior: Prior,
        elo_pre: dict[str, float],
        home_adv: float = config.ELO_HOME_ADV,
        host_nations: set[str] | None = None,
    ):
        self.prior = prior
        self._snapshot = dict(elo_pre)          # frozen pre-tournament prior
        self.ratings = dict(elo_pre)            # mutable current state
        self.home_adv = home_adv
        self.host_nations = host_nations if host_nations is not None else config.HOST_NATIONS

    def reset(self) -> None:
        self.ratings = dict(self._snapshot)

    def _home_bonuses(self, match: Match) -> tuple[float, float]:
        """(h1, h2): Elo points added to each side. Host plays at home in the
        group stage → that side gets +H; otherwise neutral."""
        if match.neutral:
            return 0.0, 0.0
        if match.team1_id in self.host_nations:
            return self.home_adv, 0.0
        if match.team2_id in self.host_nations:
            return 0.0, self.home_adv
        return 0.0, 0.0

    def _elo_diff(self, match: Match) -> float:
        h1, h2 = self._home_bonuses(match)
        r1 = self.ratings.get(match.team1_id, config.ELO_INIT)
        r2 = self.ratings.get(match.team2_id, config.ELO_INIT)
        return (r1 + h1) - (r2 + h2)

    def predict(self, match: Match) -> tuple[float, float, float]:
        return self.prior.prob(self._elo_diff(match), int(match.neutral))


class V1Model(_EloStateModel):
    """v1 — frozen prior. Ratings never move; the static baseline."""

    version = "v1"

    def update(self, played_matches: Sequence[Match]) -> None:
        # no-op by design: the prior is frozen.
        return None


class V2Model(_EloStateModel):
    """v2 — sequential Elo updating.

    Identical to v1 in every respect EXCEPT `update`, which folds a completed
    round's results into the ratings before the next round. `predict`, `reset`,
    and the home-advantage handling are all inherited unchanged, so the backtest
    loop runs byte-for-byte identically for v1 and v2 — the precondition for a
    fair comparison.

    Two consequences fall out of this for free:
      * Friendlies/priors are not discarded — they are where Elo *started*; a
        round's results only nudge it.
      * At round 1 there are no prior tournament results, so `update` has not yet
        fired when round-1 predictions are made. v1 and v2 therefore produce
        IDENTICAL round-1 predictions and only diverge from round 2 onward.
        (Asserted in tests/test_versions.py.)

    K CHOICE — flagged deviation. The historical Elo replay rates World Cup games
    at K≈40 (`ELO_K_MAJOR`). v2's in-tournament `update` deliberately uses the
    smaller, config-driven `ELO_K_INTOURNAMENT` (default 20) instead. Rationale:
    24 games should only lightly nudge a prior built on ~49k historical matches;
    K=40 here would let a single round of noise swing the next round's
    predictions hard (overfitting). It is one config number, so it stays tunable.
    """

    version = "v2"

    def __init__(self, *args, k_intournament: float | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.k_intournament = (
            k_intournament if k_intournament is not None else config.ELO_K_INTOURNAMENT
        )

    def update(self, played_matches: Sequence[Match]) -> None:
        for m in played_matches:
            if m.actual_result is None:
                continue  # never update on an unplayed match
            h1, h2 = self._home_bonuses(m)
            r1 = self.ratings.get(m.team1_id, config.ELO_INIT)
            r2 = self.ratings.get(m.team2_id, config.ELO_INIT)
            # expected score for team1 with home bonuses applied to both sides
            we1 = elo.expected_score(r1 + h1, r2 + h2, 0.0)
            if m.actual_result == TEAM1_WIN:
                s1 = 1.0
            elif m.actual_result == DRAW:
                s1 = 0.5
            else:
                s1 = 0.0
            k = self.k_intournament
            if (
                config.ELO_GD_SCALING
                and m.actual_goals1 is not None
                and m.actual_goals2 is not None
            ):
                k *= elo.gd_multiplier(m.actual_goals1 - m.actual_goals2)
            delta = k * (s1 - we1)
            self.ratings[m.team1_id] = r1 + delta
            self.ratings[m.team2_id] = r2 - delta


# v3 (v2 + suspensions) intentionally NOT implemented yet. It will subclass
# _EloStateModel (or V2Model), reuse this update(), and add a small predict-time
# rating penalty for known-suspended key players. The loop and predict stay
# unchanged. Do not implement here until v3 is requested.
