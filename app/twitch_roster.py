"""Twitch channel-roster ingestion: cheap per-channel live monitoring.

Rationale: Twitch's Search Channels endpoint matches the query against
channel NAMES, not live stream titles — searching "box break" can never
find a live break on a channel named e.g. "LaytonSportsCards" (verified
2026-10-06: 30 consecutive search-poll runs, zero streams, no errors).
But once a breaker login is known, GET /helix/streams?user_login=
(100 logins per call) reports exactly who is live right now, including
the stream title.

COST: 1 API call per 100 roster channels per run. Twitch's rate limit is
800 req/min — effectively free, so the roster can grow large.

The channel list is configurable without code changes:
  TWITCH_ROSTER_MAX_CHANNELS  (default 300)

Pipeline per run:
  1. seed backfill: manual logins from app/seed_twitch_channels.txt
  2. streams?user_login= (batched, 100 logins/call) -> live streams
  3. same normalize + title filter as the twitch search poller
     (normalize_twitch_channel / looks_like_real_break)
  4. wipe + rewrite the twitch slice in ONE transaction (Twitch data is
     transient — ended streams vanish instead of lingering as stale
     "live" rows) + stamp roster last_hit_at / last_checked_at
"""
import os
import sys

import httpx

from . import db
from .normalizer import looks_like_real_break
from .twitch import (
    STREAMS_URL,
    _headers,
    get_app_token,
    normalize_twitch_channel,
)

MAX_ROSTER_CHANNELS = int(os.environ.get("TWITCH_ROSTER_MAX_CHANNELS", "300"))


def streams_by_logins(logins: list[str], token: str) -> tuple[dict, int]:
    """GET streams?user_login= (100 logins/call) -> ({login: stream}, n_ok).

    n_ok counts successful API calls; 0 means total failure (rate-limited
    or outage), which callers must treat as a failed poll, NOT an empty
    result. Never logs the token.
    """
    streams: dict[str, dict] = {}
    n_ok = 0
    for i in range(0, len(logins), 100):
        chunk = logins[i:i + 100]
        try:
            resp = httpx.get(
                STREAMS_URL,
                params=[("user_login", login) for login in chunk],
                headers=_headers(token),
                timeout=30,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            print(f"  roster streams failed (chunk of {len(chunk)}): "
                  f"HTTP {exc.response.status_code} — skipping",
                  file=sys.stderr)
            continue
        n_ok += 1
        for item in resp.json().get("data", []) or []:
            login = (item.get("user_login") or "").lower()
            if login:
                streams[login] = item
    return streams, n_ok


def fetch_roster_breaks(conn):
    """Poll active roster channels and return kept break rows.

    Returns (rows, hit_logins, checked_logins, stats) where hit_logins maps
    login -> display name. Writes nothing — the caller upserts rows, wipes
    + rewrites the twitch slice, and stamps the roster, so a total API
    failure leaves existing data untouched.
    """
    token = get_app_token()  # fail fast with a clear message when creds missing

    channels = db.get_twitch_roster(conn, MAX_ROSTER_CHANNELS)
    if not channels:
        print("twitch-roster: roster is empty — add logins to "
              "app/seed_twitch_channels.txt.")
        return [], {}, [], {
            "n_channels": 0, "n_api_ok": 0,
            "n_kept": 0, "n_non_break": 0,
        }

    logins = []
    for ch in channels:
        login = (ch["login"] or "").strip().lower()
        if login and login not in logins:
            logins.append(login)

    streams, n_api_ok = streams_by_logins(logins, token) if logins else ({}, 0)

    # Normalize — only streams whose title reads like a real buy-in break.
    # (All streams returned here are currently live by definition.)
    rows: list[dict] = []
    hit: dict[str, str] = {}
    n_dropped = 0
    for login in logins:
        s = streams.get(login)
        if not s:
            continue  # offline right now
        ch = {
            "broadcaster_login": s.get("user_login"),
            "display_name": s.get("user_name"),
            "title": s.get("title"),
        }
        row = normalize_twitch_channel(ch, s)
        if not (row and row.get("source_url")):
            continue
        if looks_like_real_break(row["title_raw"], row.get("format")):
            rows.append(row)
            hit[login] = s.get("user_name") or login
        else:
            n_dropped += 1

    print(f"twitch-roster: {n_api_ok} streams call(s) across {len(logins)} "
          f"channels; {len(rows)} live breaks kept, "
          f"{n_dropped} non-break streams dropped")
    return rows, hit, logins, {
        "n_channels": len(logins),
        "n_api_ok": n_api_ok,
        "n_kept": len(rows),
        "n_non_break": n_dropped,
    }
