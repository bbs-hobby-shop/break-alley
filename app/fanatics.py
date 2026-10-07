"""Fanatics Live poller (Brian 2026-10-07).

Fanatics Live exposes a public GraphQL API (no auth required):
    https://www.fanatics.live/graphql

We query liveStreams (Relay-style pagination, max first:30) and the shops
directory (~174 breaker shops) to build a roster, mirroring the
Twitch roster approach: poll each rostered shop's streams directly.

Stream statuses observed: COMPLETE, PREPARING (live status TBD — likely
"LIVE"; discovered on first run with active streams).
"""

import json
import os
import urllib.parse
import urllib.request

GRAPHQL_URL = "https://www.fanatics.live/graphql"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"

# Statuses that mean "not currently live"
_DONE_STATUSES = {"COMPLETE"}


def _gql(query: str) -> dict:
    """Run one GraphQL query via GET. Returns the parsed response dict."""
    # Sanitize no_proxy (VM quirk: bracketed IPv6 entries break httpx/urllib)
    os.environ["no_proxy"] = os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    url = GRAPHQL_URL + "?query=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_shops() -> list[dict]:
    """Return the full breaker directory: [{id, name}]."""
    d = _gql("{shops{id name}}")
    return d.get("data", {}).get("shops") or []


def fetch_live_streams(first: int = 30, after: str | None = None) -> dict:
    """One page of liveStreams. Returns {edges, pageInfo}."""
    after_arg = f', after: "{after}"' if after else ""
    q = (
        "{liveStreams(first:" + str(min(first, 30)) + after_arg + ")"
        "{edges{node{id name description status viewers startsAt shop{id name}} cursor} "
        "pageInfo{hasNextPage endCursor}}}"
    )
    d = _gql(q)
    return (d.get("data") or {}).get("liveStreams") or {}


def fetch_all_streams(max_pages: int = 10) -> list[dict]:
    """Paginate through liveStreams. Returns flat list of stream nodes."""
    out = []
    cursor = None
    for _ in range(max_pages):
        page = fetch_live_streams(after=cursor)
        edges = page.get("edges") or []
        out.extend(e["node"] for e in edges if e.get("node"))
        pi = page.get("pageInfo") or {}
        if not pi.get("hasNextPage"):
            break
        cursor = pi.get("endCursor")
        if not cursor:
            break
    return out


def is_live_status(status: str | None) -> bool:
    """True if the stream status means currently live (not done/preparing).

    Note: PREPARING streams with a real startsAt are 'upcoming' — handled
    separately by is_upcoming_status()."""
    if not status:
        return False
    return status.upper() not in _DONE_STATUSES and status.upper() != "PREPARING"


def is_upcoming_status(status: str | None) -> bool:
    """True if the stream is scheduled for the future (PREPARING with a
    real start time). These show as upcoming breaks, like YouTube upcoming
    streams. Brian 2026-10-07: 'Why isn't our app showing their lives or
    upcoming shows?'"""
    return (status or "").upper() == "PREPARING"


def load_fanatics_roster() -> dict[str, str]:
    """Approved Fanatics shop names -> shop IDs (lowercased name keys).

    Reads app/seed_fanatics_shops.txt: one shop name per line (case-insensitive).
    Resolves names to IDs via the live shops directory.
    """
    from pathlib import Path
    path = Path(__file__).with_name("seed_fanatics_shops.txt")
    wanted = set()
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                wanted.add(line.split("#", 1)[0].strip().lower())
    if not wanted:
        return {}
    roster = {}
    for shop in fetch_shops():
        name = (shop.get("name") or "").strip()
        if name.lower() in wanted:
            roster[name.lower()] = shop["id"]
    return roster
