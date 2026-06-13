"""Proper scoring + bootstrap CI (spec §8).

Outcomes are one-hot (y1, yD, y2). Skill score > 0 means the model beats the
market on that metric. The bootstrap CI is the headline honesty device: on ~36
test games it will be wide and straddle 0, and the report shows it prominently.
"""
from __future__ import annotations

import math

import numpy as np

import config
from model import DRAW, TEAM1_WIN, TEAM2_WIN

Prob = tuple[float, float, float]
OneHot = tuple[int, int, int]

_EPS = 1e-15

_ONEHOT = {
    TEAM1_WIN: (1, 0, 0),
    DRAW: (0, 1, 0),
    TEAM2_WIN: (0, 0, 1),
}


def one_hot(result: str) -> OneHot:
    return _ONEHOT[result]


def brier(p: Prob, y: OneHot) -> float:
    """Multiclass Brier, 3 classes => range [0, 2]."""
    return sum((pi - yi) ** 2 for pi, yi in zip(p, y))


def logloss(p: Prob, y: OneHot) -> float:
    """Multiclass log loss; probabilities clipped to [1e-15, 1] before logs."""
    return -sum(yi * math.log(min(max(pi, _EPS), 1.0)) for pi, yi in zip(p, y))


def skill_score(model_score: float, market_score: float) -> float:
    """>0 model beats market; 0 tie; <0 market better. Lower scores are better
    (Brier/log-loss are losses), so skill = 1 - model/market."""
    if market_score == 0:
        return 0.0
    return 1.0 - (model_score / market_score)


def bootstrap_skill_ci(
    model_scores: np.ndarray,
    market_scores: np.ndarray,
    draws: int = config.BOOTSTRAP_DRAWS,
    seed: int = config.BOOTSTRAP_SEED,
    lo_pct: float = 5.0,
    hi_pct: float = 95.0,
) -> tuple[float, float]:
    """Resample the test matches with replacement `draws` times; recompute the
    pooled skill score each time; return the (5th, 95th) percentiles.

    Pooled skill is 1 - mean(model)/mean(market) over the resampled set — the
    same statistic the headline reports, so the CI matches the point estimate.
    """
    model_scores = np.asarray(model_scores, dtype=float)
    market_scores = np.asarray(market_scores, dtype=float)
    n = len(model_scores)
    if n == 0:
        return (float("nan"), float("nan"))

    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(draws, n))
    model_means = model_scores[idx].mean(axis=1)
    market_means = market_scores[idx].mean(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        skills = np.where(market_means != 0, 1.0 - model_means / market_means, 0.0)
    return (float(np.percentile(skills, lo_pct)), float(np.percentile(skills, hi_pct)))
