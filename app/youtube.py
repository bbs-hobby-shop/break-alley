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
  Default:  3 queries x 2 event types (live, upcoming) = 6 searches
            = 600 units
            ~150 unique videos -> 3 detail calls = 3 units
            ~= 603 units/run

  Cadence:  every 6 hours (4 runs/day) -> ~2,412 units/day (~24% of quota).
  That leaves ~7,500 units/day of headroom for query growth or extra runs.
  Do NOT run this every 15 minutes like eBay: hourly runs at the default
  query count would already burn ~14,500 units/day — over quota.

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
from datetime import datetime, timezone

import httpx

from . import config
from .normalizer import detect_format, detect_sport, normalize_product

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

EVENT_TYPES = ["live", "upcoming"]

# Broad queries on purpose: each search costs 100 units, so a few wide nets
# beat many narrow ones. Queries are matched against stream titles.
DEFAULT_SEARCH_QUERIES = [
    "box break",
    "card break",
    "box break pyt",
]


# Strong title signals of a REAL buy-in break (not a recap, vlog, or casual
# opening). A video is kept when the normalizer detected a break format
# (pyt/random/division/hit_draft/personal/case_break) OR any of these match.
STRONG_BREAK_SIGNALS = [
    r"break\s*#\s*\d+",          # "Break #12"
    r"#\d+\s*(pyt|break)",       # "#12 PYT Break"
    r"\bslots?\b",               # "slots left", "8 slots"
    r"\bspots?\b.{0,20}\b(left|available|open|for sale)\b",
    r"live\s*fills?",            # "live fills"
    r"pick\s*your",              # "pick your team/division"
    r"\bgroup\s*break\b",
]


def looks_like_real_break(title: str, fmt: str) -> bool:
    """True only for videos that read like actual buy-in break listings."""
    if fmt and fmt != "unknown":
        return True
    return any(re.search(p, title, re.IGNORECASE) for p in STRONG_BREAK_SIGNALS)


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
    there's no point listing a break you can no longer join.
    """
    if row.get("is_live"):
        return True
    starts_at = _parse_ts(row.get("starts_at"))
    return starts_at is not None and starts_at > datetime.now(timezone.utc)


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


def fetch_all_break_streams() -> list[dict]:
    """Run every configured (query, event type) search, fetch details for the
    deduped video ids, and return normalized break rows.

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
    n_dropped = 0
    for vid in video_ids:
        row = normalize_youtube_video(vid, candidates[vid], details.get(vid))
        if row and row.get("source_url"):
            if (looks_like_real_break(row["title_raw"], row.get("format"))
                    and is_upcoming_or_live(row)):
                rows.append(row)
            else:
                n_dropped += 1

    units = estimate_quota_units(n_searches, n_detail_calls)
    print(f"youtube: {n_searches} searches + {n_detail_calls} detail calls "
          f"= ~{units} quota units; {len(rows)} upcoming breaks kept, "
          f"{n_dropped} dropped (non-break, past, or no confirmed date)")
    return rows
