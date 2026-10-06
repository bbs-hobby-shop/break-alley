"""YouTube channel-roster ingestion: cheap per-channel monitoring.

Rationale: search.list costs 100 quota units/call, which caps discovery at
~16 searches/run. But once a breaker channel is known, checking its uploads
costs 1 unit (playlistItems.list) plus batched videos.list details
(1 unit per 50 ids). The search poller (app/youtube.py) discovers channels;
the roster poller watches them.

QUOTA MATH (free tier = 10,000 units/day):
  - playlistItems.list call = 1 unit  (recent uploads per channel)
  - videos.list call        = 1 unit  (up to 50 video ids per call)

  Worst case per run:  C channels x 1 playlist call
                       + ceil(C x VIDEOS_PER_CHANNEL / 50) detail calls
  Default (300 channels x 10 videos): 300 + ceil(3000/50) = 360 units/run.

  The uploads playlist id is derived deterministically ("UC.." -> "UU.."),
  so no channels.list call is needed to find it.

The channel list and both caps are configurable without code changes:
  YOUTUBE_ROSTER_MAX_CHANNELS       (default 300)
  YOUTUBE_ROSTER_VIDEOS_PER_CHANNEL  (default 10)

Pipeline per run:
  1. seed backfill: channel_ids already on youtube breaks -> roster
  2. playlistItems.list per active channel (hottest first) -> recent video ids
  3. videos.list (batched, 50 ids/call) with part=snippet,liveStreamingDetails
  4. same normalize + title/date filter as the search poller
     (normalize_youtube_video / looks_like_real_break / is_upcoming_or_live)
  5. upsert hits into breaks (no slice wipe — the search poller owns the
     youtube slice lifecycle) + stamp roster last_hit_at / last_checked_at
"""
import os
import re
import sys

import httpx

from . import config, db
from .normalizer import looks_like_real_break
from .youtube import (
    _parse_ts,
    channel_countries,
    estimate_quota_units,
    get_api_key,
    is_upcoming_or_live,
    normalize_youtube_video,
    videos_details,
)

PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"

MAX_ROSTER_CHANNELS = int(os.environ.get("YOUTUBE_ROSTER_MAX_CHANNELS", "300"))
VIDEOS_PER_CHANNEL = int(os.environ.get("YOUTUBE_ROSTER_VIDEOS_PER_CHANNEL", "10"))

# Home countries for roster channels that don't set snippet.country on
# YouTube. Only confident, evidence-backed entries — everything else stays
# NULL (= unknown region) rather than guessed.
KNOWN_COUNTRIES = {
    # Gold Coast Trading Cards: NRL/AFL rugby league niche, .net site
    "UC9_1XxNlE817786QYm7AbXg": "AU",
    # Poom Breaks: Taiwan-based (poombreaks.live)
    "UCGzeXkDhxnYkRM-8rZZvoaQ": "TW",
    # Maritime Sports Cards: maritimesportscards.com (Canada)
    "UCsHDlC4em4oZcBl49zlxJwQ": "CA",
    # CNC Breaks (CloutsnChara): Kitchener ON store
    "UCM1CnVA0viwqwoK3lAJ7clA": "CA",
    # Out Of The Box: Ottawa ON brick & mortar
    "UC3XMSBs56tO133hlF_N8VQQ": "CA",
}

# Title-substring fallback for discovered channels whose IDs aren't in
# KNOWN_COUNTRIES (e.g. search-poller discoveries). Matched case-insensitively
# against the roster channel title; same confidence bar as above.
KNOWN_COUNTRIES_BY_TITLE = {
    # ByThaCard: Taiwan group-break operation (traditional Chinese titles)
    "bythacard": "TW",
    # KwiatuCards: Polish breaker (Polish-language break titles)
    "kwiatucards": "PL",
}


def _override_country(cid: str, *titles: str | None) -> str | None:
    """Curated country for a channel: by ID, then by title substring.

    Accepts multiple candidate titles (roster title, video-details title)
    — any match wins. Only confident, evidence-backed entries live in the
    maps below; everything else stays NULL (= unknown region).
    """
    if cid in KNOWN_COUNTRIES:
        return KNOWN_COUNTRIES[cid]
    for title in titles:
        t = re.sub(r"[^a-z0-9]", "", (title or "").lower())
        for sub, country in KNOWN_COUNTRIES_BY_TITLE.items():
            if sub and sub in t:
                return country
    return None


def uploads_playlist_id(channel_id: str) -> str:
    """Deterministic uploads-playlist id for a channel.

    YouTube channel ids start with "UC"; the channel's uploads playlist is
    the same id with a "UU" prefix. Deriving it saves a channels.list call
    (1 unit) per channel per run.
    """
    if not channel_id or not channel_id.startswith("UC") or len(channel_id) <= 2:
        raise ValueError(f"unexpected channel id format: {channel_id!r}")
    return "UU" + channel_id[2:]


def _get(url: str, params: dict) -> dict:
    """GET with the API key attached. Never logs the URL (it holds the key)."""
    params = dict(params)
    params["key"] = get_api_key()
    resp = httpx.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def playlist_recent_video_ids(channel_id: str, max_results: int = 10) -> list[str]:
    """One playlistItems.list call (1 quota unit): recent uploads' video ids."""
    data = _get(PLAYLIST_ITEMS_URL, {
        "part": "contentDetails",
        "playlistId": uploads_playlist_id(channel_id),
        "maxResults": min(max_results, 50),
    })
    vids = []
    for item in data.get("items", []) or []:
        vid = ((item.get("contentDetails") or {}).get("videoId")) or ""
        if vid:
            vids.append(vid)
    return vids


def estimate_roster_quota_units(n_channels: int, n_videos: int) -> int:
    """Quota cost of a roster run: 1 unit per playlistItems call + 1 per
    videos.list batch. n_channels playlist calls, ceil(n_videos/50) batches."""
    n_detail_calls = (n_videos + 49) // 50 if n_videos else 0
    return estimate_quota_units(0, n_channels + n_detail_calls)


def fetch_roster_breaks(conn):
    """Poll active roster channels and return kept break rows.

    Returns (rows, hit_channel_ids, checked_channel_ids, stats). Writes
    nothing — the caller upserts rows and stamps the roster, so a total API
    failure leaves existing data untouched (no slice wipe here at all).
    """
    get_api_key()  # fail fast with a clear message when the key is missing

    channels = db.get_roster_channels(conn, MAX_ROSTER_CHANNELS)
    if not channels:
        print("youtube-roster: roster is empty — discovery hook will fill it "
              "as the search poller keeps breaks.")
        return [], [], [], {
            "n_channels": 0, "n_api_ok": 0, "units": 0,
            "n_kept": 0, "n_non_break": 0, "n_past": 0, "n_no_date": 0,
        }

    # 1. recent video ids per channel (1 quota unit each), deduped
    video_ids: list[str] = []
    channel_of_video: dict[str, str] = {}
    checked: list[str] = []
    n_playlist_calls = 0
    for ch in channels:
        cid = ch["channel_id"]
        try:
            vids = playlist_recent_video_ids(cid, VIDEOS_PER_CHANNEL)
        except httpx.HTTPStatusError as exc:
            print(f"  roster playlist failed ({cid}): "
                  f"HTTP {exc.response.status_code} — skipping",
                  file=sys.stderr)
            continue
        n_playlist_calls += 1
        checked.append(cid)
        for vid in vids:
            if vid and vid not in channel_of_video:
                channel_of_video[vid] = cid
                video_ids.append(vid)

    # 2. details in 50-id batches (1 quota unit per batch)
    details = videos_details(video_ids) if video_ids else {}

    # 3. home countries for channels still unknown: YouTube's own
    #    snippet.country first (1 unit per 50 channels), then the curated
    #    overrides. Titles come from the video details — roster titles are
    #    NULL for discovered channels, so the roster table alone can't
    #    drive the title fallback.
    country_of = {ch["channel_id"]: ch.get("country") for ch in channels}
    roster_title_of = {ch["channel_id"]: ch.get("title") for ch in channels}
    detail_title_of: dict[str, str] = {}
    for vid, det in details.items():
        snip = (det or {}).get("snippet", {})
        cid = snip.get("channelId") or channel_of_video.get(vid)
        if cid and snip.get("channelTitle"):
            detail_title_of.setdefault(cid, snip["channelTitle"])
    missing = [cid for cid, c in country_of.items() if not c]
    n_country_calls = 0
    if missing:
        n_country_calls = (len(missing) + 49) // 50
        try:
            api_map = channel_countries(missing)
        except Exception as exc:
            print(f"  roster country lookup failed: {exc}", file=sys.stderr)
            api_map = {}
        fresh = {cid: c for cid, c in api_map.items() if c}
        for cid in missing:
            if cid not in fresh:
                override = _override_country(
                    cid, roster_title_of.get(cid), detail_title_of.get(cid))
                if override:
                    fresh[cid] = override
        n_filled = db.set_channel_countries(conn, fresh)
        country_of.update(fresh)
        if n_filled:
            print(f"youtube-roster: backfilled country for {n_filled} channels")

    # 4. normalize + keep only real, joinable breaks (same filter as search)
    rows: list[dict] = []
    hit_channels: set[str] = set()
    n_non_break = n_past = n_no_date = 0
    for vid in video_ids:
        row = normalize_youtube_video(vid, None, details.get(vid))
        if not (row and row.get("source_url")):
            continue
        if not looks_like_real_break(row["title_raw"], row.get("format")):
            n_non_break += 1
        elif is_upcoming_or_live(row):
            row["country"] = country_of.get(channel_of_video[vid])
            rows.append(row)
            hit_channels.add(channel_of_video[vid])
        elif _parse_ts(row.get("starts_at")) is None:
            n_no_date += 1
        else:
            n_past += 1

    units = estimate_roster_quota_units(n_playlist_calls, len(video_ids)) + n_country_calls
    print(f"youtube-roster: {n_playlist_calls} playlist + "
          f"{(len(video_ids) + 49) // 50 if video_ids else 0} detail + "
          f"{n_country_calls} country calls "
          f"= ~{units} quota units across {len(checked)} channels; "
          f"{len(rows)} upcoming breaks kept, "
          f"{n_non_break} non-break / {n_past} past / {n_no_date} no-date dropped")
    return rows, sorted(hit_channels), checked, {
        "n_channels": len(channels),
        "n_api_ok": n_playlist_calls,
        "units": units,
        "n_kept": len(rows),
        "n_non_break": n_non_break,
        "n_past": n_past,
        "n_no_date": n_no_date,
    }
