# Addendum — Automated Market Snapshot (`fetch-market` + scheduler)

Extends SPEC.md. Replaces manual `load-market` entry with an automated pre-match snapshot.
Read-only research data collection — no auth, no trading endpoints, ever.

---

## Why this exists

Polymarket's public API returns only _live_ market state — no historical price endpoint.
Once a match resolves, the pre-match price is unrecoverable. So the price must be captured
_before kickoff_ and stored by us. The snapshot is the one irreversible step in the whole
pipeline: results can always be re-fetched later; a missed pre-match price cannot.

## Output contract

The fetcher writes the **same fields `load-market` already populates**
(`market_pW1/pD/pW2`, vig-stripped, summing to 1) into the DB, tagged
`source='polymarket-auto'` with a `captured_at` timestamp. Nothing downstream changes.

---

## 1. Endpoints (free, no key)

- **Gamma API** — event/market discovery and metadata (resolve a fixture → its market →
  the three outcome token IDs).
- **CLOB API** — per-token price; use the **midpoint** (between bid/ask) as the probability
  proxy.
- Public market reads need no authentication. **Do not** add order/trade endpoints — this
  component reads prices only.

## 2. Fixture → token mapping (the hard part — build + verify once)

The API work is easy; matching our 72 fixtures to Polymarket's markets is the real effort.

- Build a `market_map` table: `match_id → {token_w1, token_draw, token_w2}`.
- Resolve via Gamma search on team names + match date. Reuse the existing USA /
  Bosnia & Herzegovina name mapping; extend for any other mismatches.
- Add a one-time `verify-market-map` command that prints each fixture next to the resolved
  Polymarket market title, for a human eyeball pass **before** the scheduler is trusted. A
  silently mis-mapped fixture poisons a data point with no error.
- Store the map in the DB so the scheduler just reads it; don't re-resolve every run.

## 3. Scheduler design

- **Not** a single daily cron — kickoffs are staggered across the day. A once-a-day fetch
  catches some matches in-play and misses others.
- Run **every 30–60 min** (GitHub Actions cron, matching the stock-vetter pattern).
- Each run: for every fixture with kickoff in `[now, now + WINDOW]` (default WINDOW ≈ 90 min)
  that has **no snapshot yet** → fetch midpoints, de-vig, store.
- **Idempotent / insert-once:** once a match has a snapshot, never overwrite it. Same
  immutability rule as the companion log — the pre-match price is frozen the moment it's
  captured.
- Self-healing: because it polls a window and skips already-captured matches, a missed or
  delayed run is recovered by the next one, as long as kickoff hasn't passed.

### Timing

- Target snapshot ≈ 60–90 min pre-kickoff: late enough that lineups are often public and the
  price is a settled pre-match read, early enough to be safely before kick. Config-driven.
- **Hard guard:** never snapshot a match whose kickoff is already in the past — that would
  capture an in-play or settling price and contaminate the data point.

### GitHub Actions caveat

Scheduled workflows can be delayed (sometimes 5–15+ min) or occasionally skipped under load.
The frequent-poll + window + idempotency design absorbs this — don't assume on-time
execution. Build for slack, not for precision timing.

## 4. De-vig

Same as `load-market`: take the three outcome midpoints, normalize to sum to 1. Do **not**
skip this because the API numbers "look clean" — midpoints still embed the spread.

## 5. Failure handling (critical — a miss is permanent)

- If a fixture is still unsnapshotted within ~30 min of its kickoff, **send an alert**
  (reuse the existing Resend setup) so a manual fallback entry is possible before kick.
- Log each run's outcome (matches checked, captured, skipped, failed).
- Keep `load-market` working as the manual fallback path — the alert exists so you can use it.

## 6. Optional second source — Kalshi

Kalshi has its own public API. Adding it restores the two-book cross-check, but it's a
separate mapping layer (fixture → Kalshi market). Add only if you want it; not required.

---

## Boundary

Read-only public price data for research. No authentication, no order placement, no trading
endpoints. The scheduler observes the market; it never participates in it.
