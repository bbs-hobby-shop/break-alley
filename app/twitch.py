"""Twitch ingestion via the Helix API.

Auth: OAuth2 client-credentials flow using the app's Client ID + Client
Secret (read ONLY from env: TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET via
app/config.py — never hardcoded, never logged).

Endpoints:
  Token:  POST https://id.twitch.tv/oauth2/token
          (form: client_id, client_secret, grant_type=client_credentials)
  Search: GET https://api.twitch.tv/helix/search/channels
          (headers: Authorization: Bearer <token>, Client-Id)
  Enrich: GET https://api.twitch.tv/helix/streams?user_id=... (up to 100 ids)

RATE LIMIT (Helix app tokens: 800 requests/min — generous, not the constraint):
  Per run: Q searches + ceil(unique_channels / 100) enrich calls.
  Default: 4 queries + 1 enrich call = ~5 requests/run — trivial.

Recommended cadence: every 15-30 minutes. Live streams turn over fast, so
this is the "live discovery" source; YouTube (quota-bound) stays at 6h and
eBay (slot listings, not streams) at ~15 min.

The query list is configurable without code changes via the
TWITCH_SEARCH_QUERIES env var (comma-separated, overrides the default list).

Pipeline per run:
  1. search/channels for each query, live_only=true, first=50
     -> candidate live channels
  2. streams?user_id=... (batched, 100 ids/call) -> viewer_count, started_at
  3. normalize_twitch_channel() -> break schema rows (mirror of
     normalizer.normalize_ebay_item)
"""
import os
import sys
import time

import httpx

from . import config
from .normalizer import (
    detect_format,
    detect_sport,
    looks_like_real_break,
    normalize_product,
)

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
SEARCH_URL = "https://api.twitch.tv/helix/search/channels"
STREAMS_URL = "https://api.twitch.tv/helix/streams"

# Broad queries on purpose: search matches against channel name/title.
# The enrichment + break-signal filter keep only actual break content.
# Twitch has no search quota like YouTube (rate limit is 800 req/min),
# so a wider query list is cheap.
DEFAULT_SEARCH_QUERIES = [
    "box break",
    "card break",
    "case break",
    "group break",
    "box breaks",
    "card breaks",
    "live box break",
    "sports card break",
]

_token_cache: dict = {}


def twitch_configured() -> bool:
    return bool(config.TWITCH_CLIENT_ID and config.TWITCH_CLIENT_SECRET)


def get_app_token() -> str:
    """Fetch (and cache until expiry) an OAuth2 app access token."""
    if not twitch_configured():
        raise RuntimeError(
            "TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET are not set in the environment."
        )
    now = time.time()
    if _token_cache.get("expires_at", 0) > now + 60:
        return _token_cache["token"]

    resp = httpx.post(
        TOKEN_URL,
        data={
            "client_id": config.TWITCH_CLIENT_ID,
            "client_secret": config.TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 3600))
    return _token_cache["token"]


def search_queries() -> list[str]:
    """Query list, overridable via TWITCH_SEARCH_QUERIES env var."""
    raw = os.environ.get("TWITCH_SEARCH_QUERIES", "").strip()
    if raw:
        return [q.strip() for q in raw.split(",") if q.strip()]
    return list(DEFAULT_SEARCH_QUERIES)


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Client-Id": config.TWITCH_CLIENT_ID,
    }


def search_live_channels(query: str, token: str, first: int = 50) -> list[dict]:
    """One search/channels call, live_only=true. Returns raw channel dicts."""
    resp = httpx.get(
        SEARCH_URL,
        params={"query": query, "live_only": "true", "first": min(first, 100)},
        headers=_headers(token),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("data", []) or []


def enrich_streams(user_ids: list[str], token: str) -> dict[str, dict]:
    """Batch streams?user_id= (100 ids/call) -> {user_id: stream resource}.

    Adds viewer_count and started_at for channels that are still live.
    Channels missing from the result went offline between calls.
    """
    streams: dict[str, dict] = {}
    for i in range(0, len(user_ids), 100):
        chunk = user_ids[i:i + 100]
        resp = httpx.get(
            STREAMS_URL,
            params=[("user_id", uid) for uid in chunk],
            headers=_headers(token),
            timeout=30,
        )
        resp.raise_for_status()
        for item in resp.json().get("data", []) or []:
            uid = item.get("user_id")
            if uid:
                streams[uid] = item
    return streams


def channel_url(broadcaster_login: str) -> str:
    return f"https://www.twitch.tv/{broadcaster_login}"


def normalize_twitch_channel(channel: dict, stream: dict | None = None) -> dict | None:
    """Turn one live Twitch channel (+ optional stream enrichment) into a
    normalized break row. Returns None when the channel has no usable title.
    """
    login = channel.get("broadcaster_login") or ""
    if not login:
        return None

    # Prefer the live stream title from the enrichment call; fall back to the
    # channel search result's title.
    title = ((stream or {}).get("title")) or channel.get("title") or ""
    if not title:
        return None

    thumb = (stream or {}).get("thumbnail_url") or ""
    if thumb:
        thumb = thumb.replace("{width}", "320").replace("{height}", "180")

    url = channel_url(login)
    return {
        "source": "twitch",
        "source_url": url,
        "breaker": channel.get("display_name") or login,
        "product_raw": title,
        "product_normalized": normalize_product(title),
        "sport": detect_sport(title),
        "format": detect_format(title),
        "price": None,          # Twitch streams have no slot price
        "currency": "USD",
        "starts_at": (stream or {}).get("started_at"),  # ISO 8601 -> timestamptz
        "is_live": True,        # discovery is live_only; enrichment confirms
        "slots_total": None,
        "slots_remaining": None,
        "thumbnail_url": thumb or None,
        "title_raw": title,
        "affiliate_url": url,    # no affiliate program for Twitch; plain link
        "expires_at": None,
    }


def fetch_all_break_streams() -> list[dict]:
    """Run every configured search query, enrich live hits, and return
    normalized break rows. Prints counts only — never credentials.
    """
    token = get_app_token()  # fail fast with a clear message when unset
    queries = search_queries()

    # 1. searches -> deduped candidate channels by broadcaster id
    candidates: dict[str, dict] = {}
    n_searches = 0
    for query in queries:
        try:
            items = search_live_channels(query, token)
        except httpx.HTTPStatusError as exc:
            print(f"  search failed ({query!r}): HTTP "
                  f"{exc.response.status_code} — skipping", file=sys.stderr)
            continue
        n_searches += 1
        for ch in items:
            uid = ch.get("id")
            if uid and uid not in candidates:
                candidates[uid] = ch

    # 2. enrich via streams endpoint (batched, 100 ids/call)
    user_ids = list(candidates.keys())
    n_enrich = (len(user_ids) + 99) // 100 if user_ids else 0
    streams = enrich_streams(user_ids, token)

    # 3. normalize — only channels confirmed still live via enrichment,
    # and only streams whose title reads like a real buy-in break
    rows: list[dict] = []
    n_dropped = 0
    for uid in user_ids:
        stream = streams.get(uid)
        if not stream:
            continue  # went offline between search and enrichment
        row = normalize_twitch_channel(candidates[uid], stream)
        if row and row.get("source_url"):
            if looks_like_real_break(row["title_raw"], row.get("format")):
                rows.append(row)
            else:
                n_dropped += 1

    print(f"twitch: {n_searches} searches + {n_enrich} enrich calls; "
          f"{len(rows)} live breaks kept, {n_dropped} non-break streams dropped")
    return rows
