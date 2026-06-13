# Hand-entered market probabilities

There is no reliable free historical odds API, so the market baseline is entered
by hand before each round. Record Kalshi/Polymarket (or sportsbook-implied)
probabilities for the **held-out test half only — groups G–L** of each round,
into `matchday_{1,2,3}.csv`.

## Format

```
group,team1,team2,market_pW1,market_pD,market_pW2
G,Belgium,Egypt,0.62,0.24,0.20
...
```

- `team1`/`team2` must be the two teams in that group's match (order doesn't
  matter — the loader matches either orientation and swaps the win
  probabilities to the stored team1/team2 order).
- `market_pW1` = P(team1 win), `market_pD` = P(draw), `market_pW2` = P(team2 win).

## Critical: the vig

Raw market prices sum to **more than 1** (the overround / bookmaker margin). The
loader **normalizes the three numbers to sum to 1** on load — otherwise the
market looks artificially overconfident and the comparison is unfair. Enter the
raw implied probabilities as-is; don't pre-normalize.

`matchday_1.example.csv` is a ready-made template using the real group G–L
round-1 fixtures (numbers are illustrative). Copy it to `matchday_1.csv`, replace
with real observed prices, then run `load-market --matchday 1`.
