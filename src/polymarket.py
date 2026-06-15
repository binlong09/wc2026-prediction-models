"""Read-only Polymarket client + fixture resolver (addendum §1–2).

Boundary (hard): this module reads PUBLIC PRICE DATA ONLY. Gamma for
event/market discovery, CLOB for per-token midpoints. No authentication, no
order/trade endpoints, ever. The scheduler observes the market; it never
participates in it.

Match structure on Polymarket: each World Cup group fixture is a *moneyline
event* with slug `fifwc-{code}-{code}-{YYYY-MM-DD}` containing exactly THREE
binary Yes/No markets — team1-win, draw, team2-win. P(outcome) is the "Yes"
token's midpoint; the three Yes midpoints sum to >1 (the vig), de-vigged at fetch
time.

Resolution is deterministic, not fuzzy: we enumerate every event under the
`fifa-world-cup` tag, keep the 72 whose slug ends in a date (the clean moneyline
events — prop/parlay events have suffixed slugs), and match each of our fixtures
by canonicalized team set. Per-fixture fuzzy search proved unreliable; tag
enumeration returns all 72 in one sweep.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import httpx

import build  # canon() — worldcup.json/martj42 name mapping

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
WC_TAG_SLUG = "fifa-world-cup"

# A clean moneyline event slug ends with the kickoff date; prop/parlay events for
# the same fixture carry extra suffix segments after the date.
_DATE_SLUG = re.compile(r"^fifwc-.+-\d{4}-\d{2}-\d{2}$")

# Polymarket display name -> our canonical team_id (martj42). Extends the
# existing build.NAME_MAP for spellings Polymarket uses differently. Verified by
# canonicalizing every team name across all 72 moneyline events (see README).
PM_NAME_MAP = {
    "Cabo Verde": "Cape Verde",
    "Korea Republic": "South Korea",
    "Czechia": "Czech Republic",
    "Bosnia-Herzegovina": "Bosnia and Herzegovina",
    "IR Iran": "Iran",
    "Côte d'Ivoire": "Ivory Coast",
    "Türkiye": "Turkey",
    "USA": "United States",
    "Bosnia & Herzegovina": "Bosnia and Herzegovina",
}


def pm_canon(name: str) -> str:
    """Canonicalize a Polymarket team string to our team_id."""
    n = (name or "").strip()
    if n in PM_NAME_MAP:
        return PM_NAME_MAP[n]
    return build.canon(n)  # falls back to worldcup.json->martj42 map


class PolymarketError(RuntimeError):
    pass


def client() -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        timeout=30.0,
        headers={"User-Agent": "wc2026-backtest/research (read-only price collection)"},
    )


_OFFSET_NO_MIN = re.compile(r"([+-]\d{2})$")  # '+00' -> needs ':00' for fromisoformat


def parse_game_time(raw: str | None) -> datetime | None:
    """Parse Polymarket's gameStartTime ('2026-06-15 19:00:00+00') to an aware
    UTC datetime. Returns None if absent/unparseable. The canonical kickoff
    parser — scheduler and backfill both key off this, never the slug date."""
    if not raw:
        return None
    s = raw.strip().replace(" ", "T")
    s = _OFFSET_NO_MIN.sub(r"\1:00", s)  # '+00' -> '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# Gamma — discovery (read-only)                                                 #
# --------------------------------------------------------------------------- #
def _yes_token(market: dict) -> str | None:
    raw = market.get("clobTokenIds")
    if not raw:
        return None
    ids = json.loads(raw) if isinstance(raw, str) else raw
    return ids[0] if ids else None  # outcomes ["Yes","No"] -> [0] is "Yes"


def _is_draw_market(market: dict) -> bool:
    slug = (market.get("slug") or "").lower()
    git = (market.get("groupItemTitle") or "").lower()
    q = (market.get("question") or "").lower()
    return slug.endswith("-draw") or git.startswith("draw") or "end in a draw" in q


def _kickoff(event: dict) -> str | None:
    """Kickoff timestamp (e.g. '2026-06-15 16:00:00+00') for the scheduler."""
    for m in event.get("markets", []) or []:
        if m.get("gameStartTime"):
            return m["gameStartTime"]
    return event.get("gameStartTime") or event.get("startTime") or None


def iter_world_cup_events(cli: httpx.Client) -> list[dict]:
    """Every clean moneyline event under the World Cup tag (date-suffixed slug)."""
    out: dict[str, dict] = {}
    for off in range(0, 2000, 100):
        r = cli.get(
            f"{GAMMA}/events",
            params={"tag_slug": WC_TAG_SLUG, "limit": 100, "offset": off},
        )
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        for e in page:
            s = e.get("slug", "") or ""
            if _DATE_SLUG.match(s):
                out[s] = e
        if len(page) < 100:
            break
    return list(out.values())


def parse_match_event(ev: dict) -> dict | None:
    """Parse a moneyline event into team/draw Yes-token ids. None if it isn't a
    clean two-team + draw event."""
    draw_token = None
    team_tokens: dict[str, str] = {}
    raw_names: list[str] = []
    for m in ev.get("markets", []) or []:
        tok = _yes_token(m)
        if tok is None:
            continue
        if _is_draw_market(m):
            draw_token = tok
        else:
            name = (m.get("groupItemTitle") or "").strip()
            raw_names.append(name)
            team_tokens[pm_canon(name)] = tok
    if not draw_token or len(team_tokens) != 2:
        return None
    slug = ev.get("slug", "")
    return {
        "slug": slug,
        "title": ev.get("title"),
        "date": slug[-10:],
        "kickoff": _kickoff(ev),
        "team_tokens": team_tokens,
        "draw_token": draw_token,
        "raw_names": raw_names,
    }


def build_event_index(
    cli: httpx.Client, valid_ids: set[str] | None = None
) -> tuple[dict, list[str]]:
    """Return (index, unmapped_names).

    index: frozenset({team_id, team_id}) -> list of parsed match events.
    unmapped_names: Polymarket team strings that did NOT canonicalize to a known
    team_id (surfaced by verify-market-map; a silent mis-map poisons a data
    point with no error).
    """
    index: dict[frozenset, list[dict]] = {}
    unmapped: set[str] = set()
    for ev in iter_world_cup_events(cli):
        parsed = parse_match_event(ev)
        if parsed is None:
            continue
        index.setdefault(frozenset(parsed["team_tokens"].keys()), []).append(parsed)
        if valid_ids is not None:
            for raw in parsed["raw_names"]:
                if pm_canon(raw) not in valid_ids:
                    unmapped.add(raw)
    return index, sorted(unmapped)


def resolve_fixture(team1_id: str, team2_id: str, date: str, index: dict) -> dict:
    """Match one fixture against a prebuilt index. Returns a dict with
    matched=True/False; on success carries the three Yes-token ids in team1/team2
    orientation plus the kickoff."""
    cands = index.get(frozenset({team1_id, team2_id}), [])
    if not cands:
        return {"matched": False, "reason": "no moneyline event with this team set"}
    chosen = next((c for c in cands if c["date"] == date), cands[0])
    return {
        "matched": True,
        "event_slug": chosen["slug"],
        "title": chosen["title"],
        "token_w1": chosen["team_tokens"][team1_id],
        "token_draw": chosen["draw_token"],
        "token_w2": chosen["team_tokens"][team2_id],
        "kickoff": chosen["kickoff"],
        "date_match": chosen["date"] == date,
    }


# --------------------------------------------------------------------------- #
# CLOB — prices (read-only)                                                     #
# --------------------------------------------------------------------------- #
def midpoint(token_id: str, cli: httpx.Client | None = None) -> float | None:
    """Midpoint (between best bid/ask) for a token. None if unavailable."""
    own = cli is None
    cli = cli or client()
    try:
        r = cli.get(f"{CLOB}/midpoint", params={"token_id": token_id})
        if r.status_code != 200:
            return None
        mid = r.json().get("mid")
        return float(mid) if mid is not None else None
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None
    finally:
        if own:
            cli.close()


def prices_history(
    token_id: str, start_ts: int, end_ts: int, fidelity: int = 10,
    cli: httpx.Client | None = None,
) -> list[dict]:
    """Read-only price time-series for a token over [start_ts, end_ts] (unix
    seconds), bucketed at `fidelity` minutes. Works for resolved markets too —
    Polymarket retains the history, which is what makes a missed snapshot
    recoverable. Returns [] on any error. Each point is {"t": unix, "p": price}."""
    own = cli is None
    cli = cli or client()
    try:
        r = cli.get(
            f"{CLOB}/prices-history",
            params={"market": token_id, "startTs": int(start_ts),
                    "endTs": int(end_ts), "fidelity": fidelity},
        )
        if r.status_code != 200:
            return []
        return r.json().get("history", []) or []
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return []
    finally:
        if own:
            cli.close()


def price_at(
    token_id: str, target_ts: int, not_after_ts: int | None = None,
    search_window_s: int = 10800, fidelity: int = 10, cli: httpx.Client | None = None,
) -> tuple[float, int] | None:
    """The token's price nearest to `target_ts`, restricted to points at or
    before `not_after_ts` (use the kickoff time so an in-play price is never
    returned). Returns (price, used_ts) or None if no qualifying point exists."""
    hist = prices_history(
        token_id, target_ts - search_window_s,
        (not_after_ts if not_after_ts is not None else target_ts) + 600,
        fidelity=fidelity, cli=cli,
    )
    pts = [p for p in hist if not_after_ts is None or p.get("t", 0) <= not_after_ts]
    if not pts:
        return None
    nearest = min(pts, key=lambda p: abs(p["t"] - target_ts))
    return float(nearest["p"]), int(nearest["t"])
