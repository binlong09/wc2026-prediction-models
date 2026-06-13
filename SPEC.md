# World Cup 2026 Prediction Backtest — Build Spec

A handoff spec for Claude Code. Build this independently from the description below; no
prior conversation context is required.

---

## 1. Goal & epistemic framing

Build a backtesting harness that answers one question honestly: **does a prior-based
model, lightly calibrated on in-tournament results, predict held-out World Cup group
matches any better than the betting-market price?**

The expected answer is "no, or not measurably so." That is a _successful_ outcome, not a
failure. The deliverable is a rigorous comparison with proper scoring and honest
uncertainty, **not** a money-making signal. Do not add any betting/execution logic.

Key design facts:

- 2026 group stage = 12 groups (A–L) × 4 teams. Each matchday, every group plays 2
  matches → **24 matches per matchday**, **72 group matches total**.
- Train/test split is **per matchday, by group half**: groups A–F (12 matches) =
  calibration set; groups G–L (12 matches) = held-out test set. No team appears in both
  halves, so there is no leakage.
- Inputs are pre-tournament only for v1: Elo prior (which already bakes in friendlies).
- Test set is tiny (12 per matchday, 36 pooled). Confidence intervals will be huge. The
  harness must surface that, not hide it.

---

## 2. Tech stack

- Python 3.12, dependency management via **uv** (`pyproject.toml`).
- **SQLite** for persistence (`data/backtest.db`).
- `numpy`, `pandas`, `scipy` (for the 1-D temperature optimization), `httpx` (fetch).
- No web framework, no notebooks. Plain CLI entry points.

---

## 3. Project structure

```
worldcup-backtest/
  pyproject.toml
  README.md
  data/
    raw/            # fetched: international results CSV, worldcup.json
    market/         # hand-entered market probabilities, one CSV per matchday
    backtest.db
  src/
    fetch.py        # pull sources, build elo priors, populate teams + matches
    model.py        # prior model: match -> (pW1, pD, pW2); pluggable behind a Protocol
    calibrate.py    # temperature scaling fit on the train half
    score.py        # multiclass brier, log-loss, skill score, bootstrap CI
    backtest.py     # the per-matchday train/test loop (the core)
    cli.py          # commands: fetch, build-elo, predict, calibrate, backtest, report
```

---

## 4. Data sources (all free, no API key)

**A. International match history (for Elo + friendlies).** Use the standard open dataset
of all international results from 1872 to present (martj42 "international-football-results"
— available as CSV on GitHub mirrors). Columns include date, home_team, away_team,
home_score, away_score, tournament, neutral. This contains every friendly, so friendlies
are incorporated automatically once Elo is computed over it. Prefer a `raw.githubusercontent.com`
mirror so no Kaggle auth is needed; confirm the exact raw URL at build time.

**B. World Cup 2026 fixtures + results (the test labels).** Use **openfootball**:
`github.com/openfootball/worldcup.json`, file `2026/worldcup.json`. Public domain, JSON,
no key, served from `raw.githubusercontent.com`. Re-fetch after each matchday to pick up
new results. Confirm the exact raw file path at build time (likely
`.../openfootball/worldcup.json/master/2026/worldcup.json`).

**C. Market baseline (scored against, entered by hand).** No reliable free historical odds
API — so the user records Kalshi/Polymarket implied probabilities manually before each
matchday into `data/market/matchday_{1,2,3}.csv`. Format:

```
group,team1,team2,market_pW1,market_pD,market_pW2
G,USA,Paraguay,0.48,0.29,0.23
...
```

**Critical:** raw market prices sum to >1 (the overround/vig). On load, **normalize the
three numbers to sum to 1** before scoring. Otherwise the market looks artificially
overconfident.

---

## 5. Database schema (SQLite DDL)

```sql
CREATE TABLE teams (
    team_id      TEXT PRIMARY KEY,     -- canonical name or code, must match across sources
    name         TEXT NOT NULL,
    elo_pre      REAL,                 -- Elo snapshot immediately pre-tournament
    confederation TEXT
);

CREATE TABLE matches (
    match_id     TEXT PRIMARY KEY,
    matchday     INTEGER NOT NULL,     -- 1, 2, 3 (group stage rounds only)
    grp          TEXT NOT NULL,        -- 'A'..'L'
    group_half   INTEGER NOT NULL,     -- 1 if grp in A..F else 2  (the split key)
    date         TEXT,
    team1_id     TEXT NOT NULL REFERENCES teams(team_id),
    team2_id     TEXT NOT NULL REFERENCES teams(team_id),
    neutral      INTEGER NOT NULL,     -- 0 only for host nations at home (USA/CAN/MEX)
    elo_diff     REAL,                 -- (elo1 + home_adj) - elo2

    actual_result TEXT,                -- 'team1_win' | 'draw' | 'team2_win' | NULL until played
    actual_goals1 INTEGER,
    actual_goals2 INTEGER,

    model_pW1    REAL, model_pD REAL, model_pW2 REAL,   -- calibrated model output
    market_pW1   REAL, market_pD REAL, market_pW2 REAL  -- normalized, from CSV
);

-- log each version's per-matchday + pooled metrics for cross-version comparison
CREATE TABLE runs (
    run_id    TEXT, version TEXT,                   -- 'v1' | 'v2' | 'v3'
    matchday  INTEGER,                              -- 1|2|3, or NULL for pooled
    split     TEXT,                                 -- 'test' (and 'train' if you log it)
    brier_model REAL, logloss_model REAL,
    brier_market REAL, logloss_market REAL,
    skill_brier REAL, skill_logloss REAL,
    n INTEGER, created_at TEXT
);
```

Host-nation note: USA, Canada, Mexico play some group games at home → `neutral = 0` and a
home Elo bonus applies. Everything else is neutral.

---

## 6. Model versions (`model.py`)

**Two independent axes — do not conflate them.** "Version" (v1/v2/v3) is _how the model
works internally_. "Round" (matchday 1/2/3) is _which games you're predicting_. A version
is **not** assigned to a round. Every version re-runs the full backtest across all three
rounds, and you compare versions by their pooled test scores. You never hand round 1 to v1
and round 2 to v2 — that would test each version on only 12 games and make them
incomparable. The result is a grid: {v1, v2, v3} × {round 1, 2, 3, pooled}.

Consequence to bake in: at round 1 there are zero prior tournament results, so v2's update
has nothing to feed on — **v1 and v2 produce identical round-1 predictions** and only
diverge from round 2 onward. That's expected, not a bug.

All three versions implement one interface. The only thing that differs between them is
`update` (and a suspension tweak in v3), which keeps the backtest loop identical for all —
the precondition for a fair comparison.

```python
from typing import Protocol

class Model(Protocol):
    def predict(self, match) -> tuple[float, float, float]:
        """(pW1, pD, pW2), each >0, summing to 1. Uses current internal Elo state."""
    def update(self, played_matches) -> None:
        """Fold a round's actual results into the model's state. v1: no-op."""
    def reset(self) -> None:
        """Restore the pre-tournament prior. Called once before each version's run."""
```

- **v1 — frozen prior.** `update` is a no-op; ratings never move. The static baseline.
- **v2 — sequential Elo updating.** `update` runs the round's 24 results through the Elo
  engine, nudging ratings before the next round. Friendlies/priors are not discarded —
  they're where Elo _started_; round results only nudge it. **Keep `K` modest**: 24 games
  should move a prior built on hundreds of historical matches only a little. If round 1
  swings round-2 predictions hard, `K` is too high and you're overfitting noise.
- **v3 — v2 plus suspensions.** At `predict` time, apply a small rating penalty for
  known-suspended key players (yellow-card accumulation + red cards are deterministic and
  live in the match-event data). Do **not** attempt injuries — no clean free availability
  feed, and quantifying the effect needs a player-value model that's a project of its own.

**Two data sources, two distinct jobs — never merge into one "training pile":** a round's
24 results feed `update` (they move _team strength_). The 12 calibration games of the next
round feed temperature scaling (they correct _over/underconfidence_). Different mechanisms.

### Prior model — multinomial logistic on history (recommended)

Fit a multinomial logistic regression of outcome (team1_win / draw / team2_win) on features
`[elo_diff, neutral]` using the full international history dataset (section 4A), computing
`elo_diff` from your own Elo ratings as of each historical match date. Calibrated by
construction and models draws natively — which a raw Elo expected-score cannot.

**v1 prior detail.** This historical regression _is_ the v1 prior; v2/v3 inherit it and
add state. The Elo computation below produces both `elo_pre` and the historical features.

**Elo computation (build it; it produces both `elo_pre` and the historical features):**

- Standard Elo. Expected score `We = 1 / (1 + 10**(-(R1 + H - R2)/400))`.
- `H` = home advantage in Elo points, ~65 for a true home team, 0 when `neutral`.
- Update `R' = R + K * (S - We)`, `S ∈ {1, 0.5, 0}`. Use `K ≈ 40` for World Cups /
  continental finals, lower (~20) for friendlies, optionally goal-difference scaled. Keep
  K config-driven.
- Initialize all teams at 1500, run chronologically over the whole dataset through the
  pre-tournament cutoff, snapshot each WC team's rating into `teams.elo_pre`.

**Fallback prior (if assembling the historical regression is awkward):** closed-form Elo
with an explicit draw bump — `pD = d_max * exp(-(elo_diff / c)**2)` (peaks when teams are
even), split the remaining `1 - pD` between win/loss via the Elo expected score. Tune
`d_max ≈ 0.28`, `c ≈ 120` as starting values. Mark this clearly as the weaker fallback.

Friendlies are **not** a separate feature in v1 — they're already inside `elo_pre`. (v2:
add a recent-form delta = Elo change over the last N pre-tournament friendlies.)

---

## 7. Calibration — temperature scaling (`calibrate.py`)

One principled parameter, fittable (barely) on 12 games.

```python
def apply_temperature(p: tuple[float,float,float], T: float) -> tuple[float,float,float]:
    q = [pi ** (1.0 / T) for pi in p]
    s = sum(q)
    return tuple(qi / s for qi in q)   # T>1 softens (less confident), T<1 sharpens

def fit_temperature(train_preds, train_outcomes) -> float:
    # minimize mean log-loss over the train half wrt T, T in (0.2, 5.0)
    # scipy.optimize.minimize_scalar(..., bounds=(0.2, 5.0), method='bounded')
    ...
```

Do **not** fit a flexible per-class calibration curve — 12 games cannot support it; you'd
fit noise. One temperature only.

---

## 8. Scoring (`score.py`)

Outcomes one-hot encoded as `y = (y1, yD, y2)`. Clip probabilities to `[1e-15, 1]` before
logs.

```python
def brier(p, y):       # multiclass Brier, 3 classes => range [0, 2]
    return sum((pi - yi) ** 2 for pi, yi in zip(p, y))

def logloss(p, y):     # multiclass log loss
    eps = 1e-15
    return -sum(yi * math.log(min(max(pi, eps), 1.0)) for pi, yi in zip(p, y))

def skill_score(model_score, market_score) -> float:
    # >0  => model beats market ; 0 => tie ; <0 => market better
    return 1.0 - (model_score / market_score)
```

Also implement a **bootstrap CI** for the skill score: resample the test matches with
replacement (e.g. 10k draws), recompute the pooled skill score each time, report the 5th
and 95th percentiles. Expect a wide interval straddling 0 — report it prominently.

---

## 9. The backtest loop (`backtest.py`) — the core

The loop is **identical for every version** — it never branches on which version it is.
The version's `update` (no-op for v1) is the only thing that makes the runs differ. Add a
`version` column to stored predictions and to the `runs` table so all three live in one DB.

```
for version in (V1(), V2(), V3()):
    version.reset()                         # restore pre-tournament prior

    for md in (1, 2, 3):
        base = { m: version.predict(m) for m in matches[md] }   # uncalibrated

        train = matches[md] where group_half == 1   # groups A–F, 12 played games
        test  = matches[md] where group_half == 2   # groups G–L, 12 held-out games

        T = fit_temperature(base on train, actual results on train)

        for m in test:
            store(version, md, apply_temperature(base[m], T))    # OUT OF SAMPLE
            score model vs market on the SAME game  -> runs table (n=12)

        version.update(all 24 results of md)        # v1 no-ops; v2/v3 nudge Elo

    pool this version's three test halves (36 games); skill score + bootstrap CI
```

Two invariants to hold in _every_ round, _every_ version: calibrate on A–F, test only on
G–L (never score the half you calibrated on); the test set is always 12, never 24. The
`update` step fires _after_ the round's test scoring, so the model never sees a test
result before predicting it.

Important: only run a matchday once both halves' results are populated in `matches`. Skip
matchdays whose results aren't in yet.

---

## 10. CLI commands (`cli.py`)

- `fetch` — pull history CSV + worldcup.json into `data/raw/`.
- `build-elo` — compute Elo over history, write `teams.elo_pre`, populate `matches`
  (fixtures, `group_half`, `neutral`, `elo_diff`).
- `load-market --matchday N` — load + **normalize** the hand-entered market CSV.
- `refresh-results` — re-fetch worldcup.json, update `actual_*` for finished matches.
- `backtest` — run the section 9 loop for all three versions over matchdays with results.
- `report` — regenerate static outputs into `report/` (no server, no interactive UI):
    - **a table** of the {v1,v2,v3} × {round 1,2,3,pooled} grid: model Brier/log-loss,
      market Brier/log-loss, skill score, bootstrap 90% CI. Terminal print + a one-page
      `report/report.html` (or `.md`).
    - **reliability diagrams** as matplotlib PNGs, one per version (`report/calibration_v*.png`):
      predicted probability vs observed frequency, binned. This is where you can _see_
      whether v2's updating tightened calibration or just added noise — a flat table number
      hides it. Regenerated on each run.

---

## 11. What "done" looks like

A `report` that shows, per matchday and pooled: model Brier/log-loss, market Brier/log-loss,
skill score, and a wide bootstrap CI — plus a one-paragraph honest readout. The likely true
finding is "skill scores ≈ 0 across versions, CIs straddle 0, cannot distinguish model from
market — and v2/v3 don't measurably beat v1 — on this little data." Write the README to say
exactly that. The value of the project is the calibration discipline and the proper-scoring
comparison, not a verdict.

**No interactive frontend for the backtest.** The entire output is a ~12-row grid plus a
few PNGs — a Next.js/Turso UI would be effort on the least interesting part. Static table +
calibration plots only.

---

## 12. Future / out-of-scope notes (README)

- **Round-3 confound (applies to v2/v3 especially).** Qualified teams rest starters and
  dead rubbers go loose, so round 3 is drawn from a different distribution. Sequential
  updating will confidently carry stale round-1/2 form into a meaningless game. Inspect
  round-3 residuals; don't be surprised if v2/v3 do _worse_ there than v1.
- **xG as a feature** — only after confirming the simpler model is already at market level
  (it probably is).
- **Recent-form delta** — v2-style: Elo change over the last N pre-tournament friendlies as
  an explicit feature, rather than relying only on the baked-in `elo_pre`.

### Separate later project — live matchday companion (NOT part of this build)

A mobile-first tool, distinct from the backtest: punch in a match's market odds before a
round, see the frozen model's three probabilities next to the market's, track divergence as
the tournament unfolds. This is an operations dashboard, not a backtest — different product,
genuinely suited to a Next.js + Turso + Vercel stack. Build only after the backtest above is
complete. Reminder placed for the user to revisit this after v1–v3.
