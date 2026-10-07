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


def fetch_scheduled_segments(logins: list[str], token: str) -> tuple[list[dict], int]:
    """Fetch upcoming scheduled streams via Twitch Schedule API.

    Brian 2026-10-07: the roster poller was live-only, missing scheduled
    upcoming shows. Returns (segments, n_ok) where each segment has
    login, title, start_time attached.
    """
    from .twitch import USERS_URL, SCHEDULE_URL

    # 1. Resolve logins -> broadcaster_ids (100 per call)
    login_to_id: dict[str, str] = {}
    for i in range(0, len(logins), 100):
        chunk = logins[i:i + 100]
        try:
            resp = httpx.get(
                USERS_URL,
                params=[("login", login) for login in chunk],
                headers=_headers(token),
                timeout=30,
            )
            resp.raise_for_status()
            for u in resp.json().get("data", []) or []:
                login = (u.get("login") or "").lower()
                if login and u.get("id"):
                    login_to_id[login] = u["id"]
        except httpx.HTTPStatusError as exc:
            print(f"  roster users failed: HTTP {exc.response.status_code} — skipping",
                  file=sys.stderr)
            continue

    # 2. Fetch schedule per channel
    segments: list[dict] = []
    n_ok = 0
    for login, broadcaster_id in login_to_id.items():
        try:
            resp = httpx.get(
                SCHEDULE_URL,
                params={"broadcaster_id": broadcaster_id, "first": 25},
                headers=_headers(token),
                timeout=30,
            )
            resp.raise_for_status()
            n_ok += 1
            data = resp.json().get("data") or {}
            # Skip channels on vacation
            if (data.get("vacation") or {}).get("start_time"):
                continue
            for seg in data.get("segments") or []:
                # Skip canceled segments
                canceled_until = seg.get("canceled_until")
                start = seg.get("start_time")
                if canceled_until and start and canceled_until >= start:
                    continue
                seg = dict(seg)
                seg["_login"] = login
                segments.append(seg)
        except httpx.HTTPStatusError as exc:
            # 404 = no schedule set up; not an error
            if exc.response.status_code != 404:
                print(f"  roster schedule failed for {login}: "
                      f"HTTP {exc.response.status_code} — skipping",
                      file=sys.stderr)
            continue
    return segments, n_ok


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
    n_live = 0
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
            n_live += 1
        else:
            n_dropped += 1

    # Scheduled upcoming streams (Brian 2026-10-07 audit: was live-only)
    n_upcoming = 0
    try:
        segments, n_sched_ok = fetch_scheduled_segments(logins, token) if logins else ([], 0)
    except Exception as exc:
        print(f"twitch-roster: schedule fetch failed ({exc}) — continuing with live only",
              file=sys.stderr)
        segments, n_sched_ok = [], 0
    for seg in segments:
        login = seg.get("_login") or ""
        title = (seg.get("title") or "").strip()
        start_time = seg.get("start_time")
        if not title or not start_time:
            continue
        # Build a row like normalize_twitch_channel but for upcoming
        from .normalizer import detect_format, detect_sport, normalize_product
        fmt = detect_format(title)
        if not looks_like_real_break(title, fmt):
            n_dropped += 1
            continue
        url = f"https://www.twitch.tv/{login}"
        # Find display name from channels
        display = login
        for ch in channels:
            if (ch["login"] or "").lower() == login:
                display = ch.get("display_name") or login
                break
        rows.append({
            "source": "twitch",
            "source_url": url,
            "breaker": display,
            "product_raw": title,
            "product_normalized": normalize_product(title),
            "sport": detect_sport(title),
            "format": fmt,
            "price": None,
            "currency": "USD",
            "starts_at": start_time,  # ISO 8601 -> timestamptz
            "is_live": False,
            "slots_total": None,
            "slots_remaining": None,
            "thumbnail_url": None,
            "title_raw": title,
            "affiliate_url": url,
            "expires_at": None,
        })
        hit[login] = display
        n_upcoming += 1

    print(f"twitch-roster: {n_api_ok} streams call(s) + {n_sched_ok} schedule call(s) "
          f"across {len(logins)} channels; {n_live} live + {n_upcoming} upcoming breaks kept, "
          f"{n_dropped} non-break dropped")
    return rows, hit, logins, {
        "n_channels": len(logins),
        "n_api_ok": n_api_ok,
        "n_kept": len(rows),
        "n_non_break": n_dropped,
    }
