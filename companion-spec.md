# World Cup 2026 — Live Matchday Companion (Build Spec)

A handoff spec for Claude Code. Separate repo from the backtest. A **read-only viewer**
over the backtest project's published output — it displays the model's probabilities next
to the market's, per match and cumulatively. It is not a backtest, not a model, and not a
betting tool.

---

## 0. The one hard rule — displays, does not advise

This companion exists to _watch_ the model-vs-market comparison, not to act on it. The
backtest's finding is that the model tracks the market and has no demonstrated edge; this
tool visualizes that, it does not try to beat it. Enforce throughout:

- **No betting language.** No "bet", "pick", "value", "edge", "opportunity", "back this",
  no implied EV, no bet-sizing, no Kelly, no profit/PnL/stake tracking, no links to place
  wagers.
- The model-vs-market gap is shown **neutrally** — e.g. "model 0.31 · market 0.29 · Δ+0.02"
  — never framed as an opportunity or a side to take.
- A short standing line is always visible: _"Research comparison tool. The model has no
  demonstrated edge over the market — this displays the comparison, it is not betting
  advice."_
- The scorelog view reinforces the point on purpose: it shows model ≈ market over time.

If a feature would only make sense for someone deciding what to wager, it does not belong
here.

---

## 1. Stack

- **Next.js (App Router) on Vercel.** Mobile-first — this is used on a phone during matches.
- **No database. No Turso.** The companion holds no state of its own; it fetches one JSON.
- Plain fetch + React. Charting: lightweight (e.g. a small SVG/recharts line for the
  scorelog). No auth — it's a personal read-only viewer (optionally gate with a Vercel
  password if you don't want it public).

---

## 2. Data source — the backtest repo's published JSON

Source of truth: the public backtest repo
`github.com/binlong09/wc2026-prediction-models`. The companion **fetches a single
committed JSON** and renders it. No SQLite parsing, no Polymarket calls, no model logic
duplicated here.

```
https://raw.githubusercontent.com/binlong09/wc2026-prediction-models/main/report/companion.json
```

Fetch with ISR / revalidate every ~5 min (and a manual refresh control). During a live
match the user can pull-to-refresh; the underlying data only changes when the backtest
repo's cron commits, so polling faster than that buys nothing.

### Prerequisite on the backtest side (small addition — call this out to the user)

The companion depends on the backtest repo emitting `report/companion.json` and committing
it. That's a small addition to the backtest repo's `report` step:

- Add an `export-companion` step to `report.py` that writes `report/companion.json` from
  the DB (`match_log` + `matches`), and have the results-automation workflow commit it
  alongside the report (commit-on-change only, same discipline as the rest).
- **Per-match predictions come from `match_log` (the scorelog), not the backtest `runs`
  table** — i.e. the _uncalibrated pre-match_ predictions, which exist for every match
  before kickoff. The calibrated backtest predictions only exist post-round and only for
  the G–L test half, so they are not suitable for a live per-match view. This keeps the
  companion's numbers consistent with the scorelog by design.

## 3. Data contract (`companion.json`)

```jsonc
{
    "generated_at": "2026-06-15T18:00:00Z",
    "matches": [
        {
            "match_id": "fifwc-bel-egy-2026-06-15",
            "group": "G",
            "round": 1,
            "half": 2, // half 2 = G–L (test half)
            "team1": "Belgium",
            "team2": "Egypt",
            "kickoff_utc": "2026-06-15T19:00:00Z",
            "status": "upcoming", // upcoming | live | final
            "actual_result": null, // "team1_win" | "draw" | "team2_win" | null
            "predictions": {
                // uncalibrated pre-match (from match_log)
                "v1": [0.596, 0.241, 0.163], // [pW1, pD, pW2], sum 1
                "v2": [0.596, 0.241, 0.163],
                "market": [0.585, 0.226, 0.189], // vig-stripped
            },
            "market_source": "polymarket-auto", // auto | backfill | manual | null
            "captured_at": "2026-06-15T17:30:00Z",
            "scored": null, // when final: per-match brier/logloss per source
        },
    ],
    "scorelog": {
        // cumulative, ordered by match as results land
        "points": [
            {
                "match_id": "...",
                "n": 1,
                "cum_skill_brier": { "v1": -0.01, "v2": -0.01 }, // model-vs-market skill, >0 = model better
                "cum_skill_logloss": { "v1": 0.0, "v2": 0.0 },
            },
        ],
    },
}
```

If a field is absent (e.g. no market captured yet for an upcoming match), render the row
with the model numbers and a clear "market: not yet captured" — never fabricate a price.

---

## 4. Views

### A. Per-match screen (the primary one)

A list of matches grouped/sorted by kickoff, with upcoming and live matches first. Each
match card shows, for the three outcomes (team1 win / draw / team2 win), **v1, v2, and
market side by side**, plus the model-vs-market gap. Suggested layout per card:

```
Belgium  vs  Egypt          G · R1 · kickoff 19:00 UTC · upcoming
                 W1      D      W2
   v1           0.596  0.241  0.163
   v2           0.596  0.241  0.163
   market       0.585  0.226  0.189
   Δ (v1−mkt)  +0.011 +0.015 −0.026
```

- v1 and v2 will be **identical in round 1** (by design — v2 hasn't updated yet); show both
  anyway, and they diverge from round 2. Don't special-case round 1.
- When `status: final`, show the actual result and, if `scored` is present, each source's
  per-match Brier/log-loss — so you can see who called it better _after the fact_, framed
  as scoring, never as a missed bet.
- "market: not yet captured" state for upcoming matches without a price yet.
- Neutral gap display only. No coloring that implies "back this side" — if you color the Δ,
  color by magnitude (how far apart model and market are), not by which side is "favorable".

### B. Scorelog screen (the running comparison)

A single line chart of cumulative model-vs-market skill (Brier-based) for **v1 and v2**
across matches as results land, with a flat zero reference line (zero = "indistinguishable
from market"). Below it, the current pooled numbers + bootstrap CI if present.

The honest framing is the feature: the line hugging zero _is_ the finding. Caption it
plainly — e.g. _"At/near zero means the model is indistinguishable from the market. That's
the expected result."_ This screen is the live, accumulating version of the backtest's
conclusion.

---

## 5. What this companion must NOT do

- No Polymarket/Kalshi calls of its own — it only reads `companion.json`.
- No writing anywhere, no database, no user accounts, no stored predictions — the backtest
  repo is the single source of truth.
- No model logic — no calibration, no Elo, no re-scoring. If a number isn't in the JSON,
  it isn't shown.
- No betting affordances of any kind (see §0).
- No self-updating "learning" — it is a window, not a participant.

---

## 6. Build notes

- Mobile-first: single column, large tap targets, readable probability tables on a phone.
- Handle the three states cleanly: upcoming (model + market or "not captured"), live (same,
  result pending), final (result + per-match scores).
- Times: store/compare in UTC (matches the backtest's kickoff handling), but display in the
  user's local time with the UTC also shown, so the timezone-artifact fixtures aren't
  confusing.
- Empty state: before any results are in, the per-match screen still works (shows
  predictions vs market); the scorelog screen shows "no completed matches yet."
- Keep it small. This is a viewer over a JSON — resist scope creep back toward anything that
  looks like a model or a betting tool.

---

## 7. Sequencing

1. First, on the **backtest repo**: add `export-companion` to the report step and have the
   automation workflow commit `report/companion.json` (commit-on-change). Confirm the JSON
   matches §3 against real DB contents.
2. Then build this companion as a separate repo/deploy that fetches and renders it.

Do step 1 first — the companion has nothing to read until the JSON is published.
