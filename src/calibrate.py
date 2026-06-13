"""Temperature scaling (spec §7).

One principled parameter, fittable (barely) on 12 games. We deliberately do NOT
fit a per-class calibration curve — 12 games would only let us fit noise.
"""
from __future__ import annotations

import math
from typing import Sequence

from scipy.optimize import minimize_scalar

Prob = tuple[float, float, float]

_EPS = 1e-15


def apply_temperature(p: Prob, T: float) -> Prob:
    """T>1 softens (less confident), T<1 sharpens. Power-then-renormalize."""
    q = [max(pi, _EPS) ** (1.0 / T) for pi in p]
    s = sum(q)
    return (q[0] / s, q[1] / s, q[2] / s)


def _mean_logloss(T: float, preds: Sequence[Prob], outcomes: Sequence[Prob]) -> float:
    total = 0.0
    for p, y in zip(preds, outcomes):
        q = apply_temperature(p, T)
        total += -sum(yi * math.log(min(max(qi, _EPS), 1.0)) for qi, yi in zip(q, y))
    return total / max(len(preds), 1)


def fit_temperature(
    train_preds: Sequence[Prob], train_outcomes: Sequence[Prob]
) -> float:
    """Minimize mean log-loss over the train half wrt T in (0.2, 5.0).

    Returns 1.0 (a no-op) when there is nothing to fit, so the loop degrades
    gracefully on empty/degenerate train sets.
    """
    if not train_preds:
        return 1.0
    res = minimize_scalar(
        _mean_logloss,
        bounds=(0.2, 5.0),
        method="bounded",
        args=(train_preds, train_outcomes),
    )
    return float(res.x)
