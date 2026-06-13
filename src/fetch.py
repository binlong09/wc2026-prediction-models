"""Pull the two free data sources into data/raw/ (spec §4).

A. martj42 international results CSV  (Elo + friendlies baked in)
B. openfootball 2026/worldcup.json    (fixtures + results = test labels)

URLs verified 2026-06-12. The martj42 path uses an UNDERSCORE repo
(international_results); the hyphen variant in the spec 404s.
"""
from __future__ import annotations

import httpx

import config


def _download(url: str, dest, label: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  fetching {label} …")
    with httpx.Client(follow_redirects=True, timeout=60.0) as client:
        resp = client.get(url)
        resp.raise_for_status()
    dest.write_bytes(resp.content)
    print(f"    -> {dest}  ({len(resp.content):,} bytes)")


def fetch_all() -> None:
    print("fetch: pulling raw sources")
    _download(config.RESULTS_URL, config.RESULTS_CSV, "international results CSV")
    _download(config.WORLDCUP_URL, config.WORLDCUP_JSON, "worldcup 2026 JSON")
    print("fetch: done")


if __name__ == "__main__":
    fetch_all()
