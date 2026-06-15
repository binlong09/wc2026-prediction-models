# World Cup 2026 Prediction Backtest

A backtesting harness that answers one question honestly: **does a prior-based
model, lightly calibrated on in-tournament results, predict held-out World Cup
group matches any better than the betting-market price?**

The expected answer is *"no, or not measurably so"* — and that is a **successful**
outcome, not a failure. The deliverable is a rigorous comparison with proper
scoring and honest uncertainty, **not** a money-making signal. There is no
betting or execution logic here, by design.

> **Likely true finding:** skill scores ≈ 0 across versions, bootstrap CIs
> straddle 0, the model cannot be distinguished from the market — and (later)
> v2/v3 won't measurably beat v1 — on this little data. The value of the project
> is the calibration discipline and the proper-scoring comparison, not a verdict.

---

## Status: v1 + v2 complete

This build implements **v1 and v2**, end to end: fetch → Elo engine →
multinomial-logit prior → temperature calibration → proper scoring → the
per-matchday backtest loop → static report. It also adds a **model-vs-market
scorekeeper** (`match_log` + the `scorelog` report) and an **automated read-only
market snapshot** from Polymarket — both the manual `fetch-market` (Stage 1) and
the polling **scheduler** `snapshot-due` + GitHub Actions cron (Stage 2). v3 (v2 +
suspensions) is **intentionally not built yet** — the `Model` Protocol and the
version-agnostic loop are shaped so it slots in later without reworking the
pipeline. See [Roadmap](#roadmap--out-of-scope).

- **v1 — frozen prior.** `update` is a no-op; ratings never move. The baseline.
- **v2 — sequential Elo updating.** `update` folds a completed round's 24 results
  into the Elo ratings before the next round, with a modest, config-driven K.
  Adding v2 required **zero changes to the backtest loop** — only one extra entry
  in `_make_models`. Because `update` hasn't fired at round 1, **v1 and v2 produce
  identical round-1 predictions** (asserted in `tests/test_versions.py`) and only
  diverge from round 2.

> **Expected finding (not a failure):** v2 does **not** measurably beat v1 — their
> pooled skill scores sit inside each other's CIs. Nothing is tuned toward making
> v2 "win"; the honest comparison is the point.

---

## How the split works (the design, in one breath)

- 2026 group stage = **12 groups (A–L) × 4 teams**. Each group plays a 6-game
  round-robin over **3 rounds** (each team's 1st/2nd/3rd game) → **72 group
  matches**, 24 per round.
- **Train/test split is per round, by group half:** groups **A–F** (12 games) =
  calibration set; groups **G–L** (12 games) = held-out test set. No team appears
  in both halves, so there is **no leakage**.
- "Version" (v1/v2/v3 = *how the model works*) and "round" (1/2/3 = *which games
  you predict*) are independent axes. Every version re-runs the full backtest
  across all three rounds; you compare versions by their **pooled** test scores.
  The output is a grid: {v1, v2, v3} × {round 1, 2, 3, pooled}.
- The test set is tiny (12 per round, 36 pooled). Confidence intervals are huge.
  The harness **surfaces** that with a bootstrap CI rather than hiding it.

---

## Quickstart

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv venv --python 3.12
uv pip install numpy pandas scipy httpx scikit-learn matplotlib

# backtest pipeline (PYTHONPATH=src puts the modules on the path; no install needed)
PYTHONPATH=src python src/cli.py fetch            # pull raw sources -> data/raw/
PYTHONPATH=src python src/cli.py build-elo        # Elo over history; populate teams+matches
PYTHONPATH=src python src/cli.py load-market -m 1 # load + NORMALIZE market_1 (after entering it)
PYTHONPATH=src python src/cli.py refresh-results  # re-fetch results once a round is played
PYTHONPATH=src python src/cli.py backtest         # the train/test loop over completed rounds (v1+v2)
PYTHONPATH=src python src/cli.py report           # grid table + reliability PNGs -> report/

# live scorekeeper (Task 2) — capture pre-match, score as results land
PYTHONPATH=src python src/cli.py load-market -m 1 # enter real odds FIRST so the snapshot has a market
PYTHONPATH=src python src/cli.py log-predictions  # immutable pre-match capture of model + market probs
PYTHONPATH=src python src/cli.py refresh-results  # updates results AND auto-scores the log
PYTHONPATH=src python src/cli.py scorelog         # running model-vs-market comparison -> report/
```

**Right now (mid-tournament, only a few games played) the backtest correctly
reports that no complete round is scorable yet.** To see the full pipeline run
end to end on a *simulated* complete tournament — in a throwaway DB that never
touches real data — run the isolated smoke test:

```bash
PYTHONPATH=src python tests/smoke_test.py
```

---

## Data sources (free, no API key)

| | Source | Notes |
|---|---|---|
| **A. International history** | `martj42/international_results` `results.csv` | Every international 1872→present incl. friendlies. Elo (and hence friendlies) is computed from this. |
| **B. WC2026 fixtures + results** | `openfootball/worldcup.json` `2026/worldcup.json` | Public domain. The test labels. Re-fetch after each round. |
| **C. Market baseline (manual)** | hand-entered CSV | `data/market/matchday_{1,2,3}.csv`, groups G–L only. See [`data/market/README.md`](data/market/README.md). The manual fallback. |
| **C′. Market baseline (auto)** | Polymarket Gamma + CLOB | `fetch-market` snapshots vig-stripped prices into the same fields. Read-only, no key. See [Automated market snapshot](#automated-market-snapshot-fetch-market). |

### ⚠️ Two data corrections found at build time (2026-06-12)

1. **martj42 URL — the spec's hint was wrong.** The hinted path
   `martj42/international-results` (hyphen) **404s**. The working mirror is the
   **underscore** repo `martj42/international_results`. Columns match the spec
   (`date,home_team,away_team,home_score,away_score,tournament,city,country,neutral`).
   Configured in `src/config.py` (`RESULTS_URL`).
2. **`worldcup.json`'s `round` field is calendar matchdays, not group rounds.**
   It labels games "Matchday 1"…"Matchday 17" (plus knockout rounds), *not* the
   group round 1/2/3 we split on. We **derive** the group round by date-ordering
   each group's six games (first 2 → round 1, next 2 → round 2, last 2 → round 3).
   See `src/build.py::_load_group_matches`.

Only two team names differ between the sources, mapped in `build.NAME_MAP`:
`USA → United States`, `Bosnia & Herzegovina → Bosnia and Herzegovina`.

---

## Project structure

```
worldcup-backtest/
  pyproject.toml        # py3.12 / uv
  README.md
  data/
    raw/                # fetched: international_results.csv, worldcup_2026.json
    market/             # hand-entered market probs (one CSV per round) + template
    backtest.db         # SQLite (created by build-elo)
  src/
    config.py           # paths + tunables (Elo K/H, bootstrap, host nations)
    db.py               # SQLite schema (§5) + connection helper
    fetch.py            # pull the two raw sources
    elo.py              # chronological Elo engine -> elo_pre + history features
    model.py            # Model Protocol + v1 multinomial-logit prior (frozen)
    calibrate.py        # temperature scaling (one parameter, scipy bounded)
    score.py            # multiclass Brier, log-loss, skill score, bootstrap CI
    build.py            # build-elo + refresh-results (populate teams + matches)
    market.py           # load-market (+ vig normalization)
    backtest.py         # the per-round train/test loop (the core)
    report.py           # static grid (terminal + HTML) + reliability PNGs
    scorelog.py         # model-vs-market scorekeeper: log + score + report (Task 2)
    polymarket.py       # read-only Gamma+CLOB client + fixture->token resolver (addendum)
    fetch_market.py     # verify-market-map + fetch-market (auto market snapshot, addendum §1-2)
    scheduler.py        # snapshot-due: kickoff-window polling + guards + alert (addendum §3-5)
    cli.py              # ...+ verify-market-map/fetch-market/snapshot-due/
                        #   log-predictions/score-log/scorelog
  .github/workflows/
    snapshot-market.yml # cron poll -> snapshot-due, commits captures back (Stage 2)
  tests/
    smoke_test.py       # ISOLATED end-to-end backtest on a simulated tournament (v1+v2)
    test_versions.py    # ISOLATED v1≡v2 round-1 invariant + v2-diverges-at-round-2
    test_scorelog.py    # ISOLATED scorekeeper: log -> score -> immutability boundaries
    test_fetch_market.py# ISOLATED (stubbed API) market_map + de-vig + insert-once
    test_scheduler.py   # ISOLATED window + past-kickoff guard + TZ-artifact + alert exit
```

All three tests are self-isolating (throwaway DB via `WCBT_DB`, redirected
report dir) and never touch `data/backtest.db`. Run them with plain `python`:

```bash
PYTHONPATH=src python tests/test_versions.py      # the round-1 v1≡v2 guardrail
PYTHONPATH=src python tests/test_scorelog.py      # scorekeeper boundaries
PYTHONPATH=src python tests/test_fetch_market.py  # market_map + de-vig + insert-once (stubbed API)
PYTHONPATH=src python tests/test_scheduler.py     # window + past-kickoff guard + TZ-artifact + alert
PYTHONPATH=src python tests/smoke_test.py         # full simulated tournament
```

> Two small deviations from the spec's §3 layout, both for clarity: the Elo
> engine lives in its own `elo.py` (the spec folds it into `fetch.py`), and
> `build.py`/`market.py` hold the build-elo/load-market logic. The reused Elo
> engine and the clean `Model` seam are what let v2/v3 drop in later untouched.

---

## How a backtest round works (`backtest.py`)

The loop is **identical for every version** — it never branches on which version
it is. The version's `update` (a no-op for v1) is the only difference, which is
the precondition for a fair comparison.

```
for version in (V1, [V2, V3 later]):
    version.reset()                       # restore pre-tournament prior
    for round in (1, 2, 3):               # skip rounds not fully played
        base = predict(every match in round)              # uncalibrated
        T    = fit_temperature(base on A–F, results on A–F)   # train half
        for m in G–L:                     # test half — OUT OF SAMPLE
            store(apply_temperature(base[m], T)); score vs market   # n = 12
        version.update(all 24 results)    # v1 no-op; AFTER test scoring
    pool the three test halves (36) -> skill score + bootstrap CI
```

Invariants held every round, every version: calibrate on A–F, **test only on
G–L** (never score the half you calibrated on); the test set is **always 12**;
`update` fires **after** the round's test scoring, so the model never sees a test
result before predicting it.

---

## The model (`model.py`)

**v1 — frozen prior.** A **multinomial logistic regression** of outcome
(team1_win / draw / team2_win) on features `[elo_diff, neutral]`, fit over the
full international history with `elo_diff` computed from our own Elo ratings as of
each historical match date. It is calibrated by construction and models draws
natively (which a raw Elo expected-score can't). `update` is a no-op; ratings
never move. This is the static baseline.

**v2 — sequential Elo updating.** Identical to v1 except `update` folds a
completed round's 24 results into the Elo ratings before the next round. It
subclasses the same base as v1 and overrides only `update`; `predict`/`reset`/the
home-advantage handling are inherited, so the loop is unchanged.

> **K choice (flagged deviation).** The historical replay rates World Cup games at
> K≈40 (`ELO_K_MAJOR`). v2's in-tournament `update` deliberately uses the smaller,
> config-driven `ELO_K_INTOURNAMENT` (default **20**). 24 games should only lightly
> nudge a prior built on ~49k matches; K=40 here would let one round of noise swing
> the next round's predictions (overfitting). `tests/test_versions.py` confirms the
> nudge is modest (mean round-2 prob shift ≈ 0.01). It's one config number to tune.

Each model carries its **own** mutable Elo state and recomputes `elo_diff` at
predict time. For v1 the ratings never move, so this equals the stored snapshot;
for v2 the same seam lets `update` nudge ratings and have `predict` reflect it
automatically, with the loop unchanged. This is also why **v1 and v2 are
identical at round 1** — `update` simply hasn't been called yet.

**Elo engine (`elo.py`).** Standard Elo: init 1500, expected score
`We = 1/(1+10^(-(R1+H−R2)/400))`, `H ≈ 65` home points (0 when neutral), update
`R' = R + K·(S−We)`. `K` is config-driven and tournament-weighted (≈40 majors,
≈20 friendlies, ≈30 otherwise) with an optional goal-difference multiplier. Host
nations (USA/Canada/Mexico) play their group games at home → `neutral = 0` and the
home bonus is applied to whichever side is the host.

---

## The scorekeeper (`scorelog.py`, Task 2)

A **live, accumulating** version of the backtest finding — and a *scorekeeper, not
a learner*. It records, **before kickoff**, each version's predicted `(pW1,pD,pW2)`
alongside the vig-stripped market price, then scores both retrospectively as
results land and accumulates the model-vs-market comparison match by match. It
lives in its own `match_log` table, separate from the backtest `runs`.

Hard boundaries (all asserted in `tests/test_scorelog.py`):

- **Never feeds back into any model.** No online tuning, no nudging predictions
  toward the market or toward being right. It only records and, later, scores.
- **Logged predictions are immutable.** Probabilities + `captured_at` are written
  once (`INSERT OR IGNORE`) and never overwritten — not even after the result is
  known. Only the score columns + `actual_result` are filled in afterward.
- **Logged once, before the match.** Already-decided games are never logged
  (you can't claim an honest pre-match prediction for a finished game).

Commands: `log-predictions` (capture), `score-log` (retrospective scoring; also
runs automatically at the end of `refresh-results`), `scorelog` (the report view —
a table + a `report/scorelog.png` of cumulative skill as results land).

---

## Automated market snapshot (`fetch-market`)

The addendum replaces manual `load-market` entry with an automated **read-only**
pre-match snapshot from Polymarket. Polymarket exposes only *live* market state —
once a match resolves, the pre-match price is unrecoverable — so the snapshot is
the one irreversible step in the pipeline and must happen before kickoff.

**Status: Stage 1 (manual command) + Stage 2 (scheduler) complete.**

It writes the **same `market_pW1/pD/pW2` fields** `load-market` populates (vig-
stripped, summing to 1), tagged `market_source='polymarket-auto'` with a
`market_captured_at`. Nothing downstream changes.

How a fixture maps to prices: each World Cup group match is a Polymarket
*moneyline event* (`fifwc-{code}-{code}-{date}`) with three binary Yes/No markets
— team1-win, draw, team2-win. P(outcome) is each market's **"Yes" token
midpoint** (CLOB `/midpoint`). The three Yes midpoints sum to >1 (the vig) and are
de-vigged by normalizing — *not skipped because the API numbers look clean; the
midpoint still embeds the spread.*

**Resolution is deterministic, not fuzzy.** We enumerate every event under the
`fifa-world-cup` tag and keep the 72 whose slug ends in a date (the clean
moneyline events), then match each fixture by canonicalized team set. Per-fixture
fuzzy search proved unreliable; the tag sweep returns all 72 at once.

Name mapping extends the existing USA/Bosnia map (`polymarket.PM_NAME_MAP`).
Discovered by canonicalizing every team name across all 72 events:
`Cabo Verde→Cape Verde`, `Korea Republic→South Korea`, `Czechia→Czech Republic`,
`Bosnia-Herzegovina→Bosnia and Herzegovina`, `IR Iran→Iran`,
`Côte d'Ivoire→Ivory Coast`, `Türkiye→Turkey`.

Commands:

```bash
PYTHONPATH=src python src/cli.py verify-market-map   # resolve all 72 -> market_map; print each
                                                     # fixture next to its Polymarket title (eyeball)
PYTHONPATH=src python src/cli.py fetch-market -m 1    # snapshot vig-stripped prices for a matchday
```

- **`verify-market-map`** is the one-time human check before anything trusts the
  mapping — it prints `fixture → resolved title` with `✓` (team+date match), `~`
  (team match, slug date off by a timezone day — benign), `✗` (unresolved), and
  stores the `match_id → {token_w1, token_draw, token_w2, kickoff}` map.
- **`fetch-market`** is **insert-once**: it never overwrites an existing snapshot
  (any source), and in Stage 1 it also skips already-played matches. `load-market`
  remains the manual fallback and is the *only* path allowed to overwrite (the
  human override).

> **Boundary (hard):** read-only public price data for research. No
> authentication, no order placement, no trade endpoints. The fetcher observes
> the market; it never participates in it.

### The scheduler (Stage 2)

`snapshot-due` is a thin polling wrapper around `fetch-market`'s capture, run on a
frequent GitHub Actions cron. Each pass snapshots any fixture kicking off within
the window that isn't already captured.

```bash
PYTHONPATH=src python src/cli.py snapshot-due            # one polling pass (the cron job)
PYTHONPATH=src python src/cli.py snapshot-due --now 2026-06-14T03:00:00+00:00   # dry-run timing
```

- **All timing keys off the stored Polymarket `gameStartTime` (UTC), never the
  slug or local date.** This is load-bearing, not cosmetic: many fixtures kick off
  the UTC day *after* their slug date (e.g. `fifwc-arg-alg-2026-06-16` →
  `2026-06-17 01:00 UTC`), and the three Stage-1 timezone-artifact matches only
  snapshot on the right day when keyed off kickoff. `scheduler.parse_kickoff()`
  parses the `'YYYY-MM-DD HH:MM:SS+00'` format to an aware UTC datetime.
- **Window:** a fixture is captured once `now ≤ gameStartTime ≤ now + WINDOW`
  (`SNAPSHOT_WINDOW_MIN`, default 90).
- **Hard guard:** a fixture whose `gameStartTime` is already past is never
  snapshotted (it would capture an in-play / settling price).
- **Insert-once / self-healing:** captured prices are immutable; because each run
  polls a window and skips already-captured fixtures, a delayed or skipped cron
  run is recovered by the next one — as long as kickoff hasn't passed. Built for
  GitHub Actions' delayed/skipped runs, not on-time execution.
- **Alert (no Resend):** if a fixture is still uncaptured within
  `SNAPSHOT_ALERT_MIN` (default 30) **before** kickoff, the run logs loudly and
  **exits non-zero** — the workflow fails and GitHub's failed-run email *is* the
  alert, giving you time to hand-enter via `load-market` before kick. Only this
  *actionable* pre-kickoff case fails the run. A fixture whose kickoff has already
  passed uncaptured (within `SNAPSHOT_MISS_GRACE_MIN`) is a permanent miss — it's
  logged as a loud `NOTE`, but does **not** fail the run, since a pre-match price
  can't be recovered after kickoff and failing on it would only be non-actionable
  alert noise.

The workflow is [`.github/workflows/snapshot-market.yml`](.github/workflows/snapshot-market.yml):
cron `*/30 * * * *`, installs deps with uv, runs `snapshot-due`, then commits the
updated `data/backtest.db` back so captures persist across the ephemeral runners
(insert-once is meaningless if the DB is discarded each run), and finally
propagates the exit code so an alert fails the run. It needs the committed DB to
already have `build-elo` + `verify-market-map` done (a one-time local step you
commit). Read-only price collection only — no auth, no trading.

> The scorelog logs the model's **uncalibrated** pre-match prior prediction, by
> design: temperature calibration needs that round's already-played A–F results,
> which generally don't exist before the G–L games kick off. The backtest `runs`
> table is the calibrated, in-sample-trained view; the two are intentionally
> distinct. **Enter real odds and run `load-market` *before* `log-predictions`** so
> the immutable snapshot captures a market to score against.

---

## Roadmap & out-of-scope

### Coming later (the seam is already in place)
- **v3 — v2 + suspensions.** A small rating penalty at predict time for
  known-suspended key players (yellow-card accumulation + reds are deterministic,
  in the match-event data). It will subclass v2, reuse its `update`, and add only
  the predict-time penalty — the loop stays unchanged. **No injuries** — there's no
  clean free availability feed and quantifying it needs a player-value model of its
  own.

Two data sources, two distinct jobs — never merged into one "training pile": a
round's 24 results feed `update` (they move *team strength*); the next round's 12
A–F games feed temperature scaling (they correct *over/under-confidence*).

### Round 3: the model is blind by design — and that blindness is the finding

The model is intentionally blind to round-3 disciplinary suspensions and squad
rotation (rested starters in dead rubbers, already-qualified teams playing loose,
must-win teams playing open). No version accounts for these — they aren't in the
free data feeds, and v3 (suspensions only) was scoped out for that reason: the
feeds carry no card data, so it could only run on manual entry and would merely
re-confirm the v1≡v2 result.

This is **by design, not a defect**. The market price *does* incorporate this
information (lineups, motivation, who's rested), so the model-vs-market skill gap
in round 3 is the *measurement* of how much that information matters — not a bug
to be fixed. Sequential updating (v2) compounds it: it will confidently carry
stale round-1/2 form into a meaningless game.

Expect **both v1 and v2 to score worst in round 3**. That dip is a finding, not a
failure: it shows round-3 group games are where home-modelable signal (team
strength) matters least and market-only signal (lineups, motivation) matters
most. Inspect the round-3 residuals in that light.

### Deliberately out of scope
- **xG as a feature** — only worth adding after confirming the simpler model is
  already at market level (it probably is).
- **Recent-form delta** — a v2-style explicit feature (Elo change over the last N
  pre-tournament friendlies), rather than relying only on baked-in `elo_pre`.
- **No interactive frontend.** The whole output is a ~12-row grid plus a few PNGs;
  a web UI would be effort on the least interesting part. Static table + plots only.

### Separate later project (NOT part of this build)
A live matchday companion — a mobile-first tool to punch in a round's market odds
and see the frozen model's three probabilities beside the market's, tracking
divergence as the tournament unfolds. That's an operations dashboard, a different
product (genuinely suited to Next.js + Turso + Vercel), to be built only after
v1–v3 are done.
