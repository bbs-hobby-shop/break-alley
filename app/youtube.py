"""YouTube ingestion via the YouTube Data API v3.

Auth: a single API key read ONLY from env (YOUTUBE_API_KEY via app/config.py)
— never hardcoded, never logged. The key is sent as the `key` query param on
each request; URLs containing it are never printed or logged.

QUOTA MATH (free tier = 10,000 units/day — documented, recompute before
raising cadence or query count):
  - search.list call  = 100 units (cost is per call, NOT per result, so we
    always request maxResults=50 to get full value out of each call)
  - videos.list call  =   1 unit  (up to 50 video ids per call)

  Per run:  Q queries x E event types searches + ceil(unique_videos / 50)
            detail calls
  Default:  8 queries x 2 event types (live, upcoming) = 16 searches
            = 1600 units
            ~300 unique videos -> 6 detail calls = 6 units
            ~= 1606 units/run

  Cadence:  every 6 hours (4 runs/day) -> ~6,425 units/day (~64% of quota).
  Do NOT run this every 15 minutes like eBay: hourly runs at the default
  query count would already burn ~38,500 units/day — way over quota.

The query list is configurable without code changes via the
YOUTUBE_SEARCH_QUERIES env var (comma-separated, overrides the default list).

Pipeline per run:
  1. search.list for each (query, eventType) -> candidate video ids
  2. videos.list (batched, 50 ids/call) with part=snippet,liveStreamingDetails
     -> scheduledStartTime, live state, channel, thumbnails
  3. normalize_youtube_video() -> break schema rows (mirror of
     normalizer.normalize_ebay_item)
"""
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import httpx

from . import config
from .normalizer import (
    detect_format,
    detect_sport,
    looks_like_real_break,
    normalize_product,
)

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"

EVENT_TYPES = ["live", "upcoming"]

# Break-format-specific queries plus one broad net ("box break") so plainly
# titled breaks ("2024 Topps Chrome 2 Box Break") are still discovered.
# The title filter (not the query) is what guarantees quality, so the broad
# query is safe. 8 queries x 2 event types = 16 searches = ~1,600 units/run;
# 4 runs/day ~= 6,400 units/day (~64% of the 10k quota).
DEFAULT_SEARCH_QUERIES = [
    "box break",
    "pyt box break",
    "random team box break",
    "player break",
    "case break",
    "group break",
    "division break",
    "box mixer break",
]


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def is_upcoming_or_live(row: dict) -> bool:
    """True only for streams happening now or with a confirmed future date.

    Old/past streams and videos with no parseable scheduled time are dropped —
    there's no point listing a break you can no longer join. Live streams must
    have started within the last 24h: YouTube sometimes reports ancient zombie
    "live" broadcasts that never properly ended.
    """
    now = datetime.now(timezone.utc)
    if row.get("is_live"):
        started = _parse_ts(row.get("starts_at"))
        return started is not None and started > now - timedelta(days=1)
    starts_at = _parse_ts(row.get("starts_at"))
    return starts_at is not None and starts_at > now


def get_api_key() -> str:
    if not config.YOUTUBE_API_KEY:
        raise RuntimeError("YOUTUBE_API_KEY is not set in the environment.")
    return config.YOUTUBE_API_KEY


def search_queries() -> list[str]:
    """Query list, overridable via YOUTUBE_SEARCH_QUERIES env var."""
    raw = os.environ.get("YOUTUBE_SEARCH_QUERIES", "").strip()
    if raw:
        return [q.strip() for q in raw.split(",") if q.strip()]
    return list(DEFAULT_SEARCH_QUERIES)


def estimate_quota_units(n_searches: int, n_detail_calls: int) -> int:
    """Quota cost of a run: 100 units/search.list + 1 unit/videos.list."""
    return n_searches * 100 + n_detail_calls * 1


def _get(url: str, params: dict) -> dict:
    """GET with the API key attached. Never logs the URL (it holds the key)."""
    params = dict(params)
    params["key"] = get_api_key()
    resp = httpx.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


_CHANNEL_ID_RE = re.compile(r"^UC[\w-]{20,}$")


def channel_countries(channel_ids: list[str]) -> dict[str, str | None]:
    """Map channel ids to their 2-letter home country (snippet.country).

    Batched 50 ids per channels.list call (1 quota unit each). Channels that
    never set a country come back as None — genuinely unknown, not US.
    """
    out: dict[str, str | None] = {}
    ids = [c for c in dict.fromkeys(channel_ids) if c]
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        try:
            data = _get(CHANNELS_URL, {"part": "snippet", "id": ",".join(batch)})
        except Exception as exc:
            print(f"  channel country lookup failed: {exc}", file=sys.stderr)
            continue
        for ch in data.get("items", []):
            out[ch["id"]] = (ch.get("snippet", {}).get("country") or None)
    return out


def resolve_channel(user_input: str) -> tuple[str, str]:
    """Turn a visitor's breaker suggestion into a (channel_id, title).

    Accepts a UC channel id, an @handle, a youtube.com/@handle or
    /channel/UC... URL, or a bare handle-ish name. Costs 1 quota unit.
    Raises ValueError with a human-readable message when unresolvable.
    """
    text = (user_input or "").strip()
    if not text:
        raise ValueError("Enter a YouTube channel name, @handle, or link.")

    # Direct channel id (also from /channel/UC... URLs)
    m = re.search(r"UC[\w-]{22}", text)
    if m and _CHANNEL_ID_RE.match(m.group(0)):
        return _channel_title(m.group(0))

    # @handle from input or URL
    m = re.search(r"@([\w.-]{3,30})", text)
    handle = m.group(1) if m else re.sub(r"\s+", "", text)
    if not handle:
        raise ValueError("Couldn't make sense of that — try a @handle or channel link.")
    data = _get(CHANNELS_URL, {"part": "id,snippet", "forHandle": handle})
    items = data.get("items", [])
    if not items:
        raise ValueError(f'No YouTube channel found for "@{handle}". Check the spelling.')
    ch = items[0]
    return ch["id"], ch["snippet"]["title"]


def _channel_title(channel_id: str) -> tuple[str, str]:
    data = _get(CHANNELS_URL, {"part": "id,snippet", "id": channel_id})
    items = data.get("items", [])
    if not items:
        raise ValueError("That channel id doesn't exist on YouTube.")
    return items[0]["id"], items[0]["snippet"]["title"]


def search_streams(query: str, event_type: str, max_results: int = 50) -> list[dict]:
    """One search.list call for live or upcoming break streams."""
    data = _get(SEARCH_URL, {
        "part": "snippet",
        "type": "video",
        "eventType": event_type,
        "q": query,
        "maxResults": min(max_results, 50),
    })
    return data.get("items", []) or []


def videos_details(video_ids: list[str]) -> dict[str, dict]:
    """Batch videos.list (50 ids/call, 1 quota unit/call) -> {videoId: resource}."""
    details: dict[str, dict] = {}
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        data = _get(VIDEOS_URL, {
            "part": "snippet,liveStreamingDetails",
            "id": ",".join(chunk),
        })
        for item in data.get("items", []) or []:
            vid = item.get("id")
            if vid:
                details[vid] = item
    return details


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def normalize_youtube_video(
    video_id: str, search_item: dict, details: dict | None
) -> dict | None:
    """Turn one YouTube video into a normalized break row. Returns None when
    the video has no usable details (deleted/private between calls)."""
    if not details:
        return None
    snippet = details.get("snippet") or {}
    live = details.get("liveStreamingDetails") or {}

    title = snippet.get("title", "") or ""
    thumbs = snippet.get("thumbnails") or {}
    thumb_url = (
        (thumbs.get("high") or {}).get("url")
        or (thumbs.get("medium") or {}).get("url")
        or (thumbs.get("default") or {}).get("url")
    )

    broadcast = snippet.get("liveBroadcastContent")
    is_live = broadcast == "live" or bool(
        live.get("actualStartTime") and not live.get("actualEndTime")
    )
    starts_at = live.get("scheduledStartTime") or live.get("actualStartTime")

    url = watch_url(video_id)
    return {
        "source": "youtube",
        "source_url": url,
        "breaker": snippet.get("channelTitle"),
        "channel_id": snippet.get("channelId"),  # roster discovery + backfill
        "product_raw": title,
        "product_normalized": normalize_product(title),
        "sport": detect_sport(title),
        "format": detect_format(title),
        "price": None,          # YouTube streams have no slot price
        "currency": "USD",
        "starts_at": starts_at,  # ISO 8601 -> timestamptz
        "is_live": is_live,
        "slots_total": None,
        "slots_remaining": None,
        "thumbnail_url": thumb_url,
        "title_raw": title,
        "affiliate_url": url,    # no affiliate program for YouTube; plain link
        "expires_at": None,
    }


def fetch_all_break_streams() -> tuple[list[dict], int]:
    """Run every configured (query, event type) search, fetch details for the
    deduped video ids, and return normalized break rows.

    Returns (rows, n_successful_searches). When n_successful_searches is 0 the
    API never answered (quota exhausted, rate-limited, or outage) — that is a
    failed poll, NOT an empty result, and callers must keep the existing slice
    instead of wiping it.

    Prints quota usage (counts only — never the key).
    """
    queries = search_queries()
    get_api_key()  # fail fast with a clear message when the key is missing

    # 1. searches -> deduped candidate video ids (keep a search item as fallback)
    candidates: dict[str, dict] = {}
    n_searches = 0
    for query in queries:
        for event_type in EVENT_TYPES:
            try:
                items = search_streams(query, event_type)
            except httpx.HTTPStatusError as exc:
                print(f"  search failed ({query!r}, {event_type}): "
                      f"HTTP {exc.response.status_code} — skipping",
                      file=sys.stderr)
                continue
            n_searches += 1
            for item in items:
                vid = ((item.get("id") or {}).get("videoId")) or ""
                if vid and vid not in candidates:
                    candidates[vid] = item

    # 2. details in 50-id batches (1 quota unit per batch)
    video_ids = list(candidates.keys())
    n_detail_calls = (len(video_ids) + 49) // 50 if video_ids else 0
    details = videos_details(video_ids)

    # 3. normalize + keep only real, joinable breaks
    rows: list[dict] = []
    n_non_break = n_past = n_no_date = 0
    for vid in video_ids:
        row = normalize_youtube_video(vid, candidates[vid], details.get(vid))
        if not (row and row.get("source_url")):
            continue
        if not looks_like_real_break(row["title_raw"], row.get("format"),
                                       row.get("breaker")):
            n_non_break += 1
        elif is_upcoming_or_live(row):
            rows.append(row)
        elif _parse_ts(row.get("starts_at")) is None:
            n_no_date += 1
        else:
            n_past += 1

    units = estimate_quota_units(n_searches, n_detail_calls)
    print(f"youtube: {n_searches} searches + {n_detail_calls} detail calls "
          f"= ~{units} quota units; {len(rows)} upcoming breaks kept, "
          f"{n_non_break} non-break / {n_past} past / {n_no_date} no-date dropped")
    return rows, n_searches
