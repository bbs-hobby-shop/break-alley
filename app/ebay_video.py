"""Extract live video locations and break time from eBay listings via the
Browse API getItem endpoint (no scraping — eBay 403s datacenter IPs).

Patterns (verified 2026-10-06 via manual audit):
1. Item specifics: "Streaming Service" field (e.g., YouTube), "Time"/"Date"
2. Description: "WHEN AND WHERE IS MY BREAK?" section with
   "Breaks are on our {platform} Livestream:" + linked channel name
3. eBay Live: item specifics or description mentions
4. Phrases: "break night at 7 pm CT", "PREFILL RIPS ONCE FULL {date}"

Collects ALL platforms found (YouTube, Facebook, Twitch, eBay Live, etc.),
not just the first one. (Brian 2026-10-06)
"""

import os
import re
import json

# Sanitize proxy env (VM quirk: bracketed IPv6 in no_proxy breaks httpx)
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "localhost,127.0.0.1"

# Time patterns: "7 pm CT", "8:30pm EST", "Saturday 7pm", etc.
TIME_PATTERNS = [
    r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*(?:CT|ET|PT|MT|EST|EDT|CST|CDT|PST|PDT|MST|MDT))",
    r"break\s*night\s*(?:at\s*)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*(?:CT|ET|PT|MT)?)",
]

# Timezone abbreviations to IANA names for break time parsing
TZ_MAP = {
    "PT": "US/Pacific", "PST": "US/Pacific", "PDT": "US/Pacific",
    "MT": "US/Mountain", "MST": "US/Mountain", "MDT": "US/Mountain",
    "CT": "US/Central", "CST": "US/Central", "CDT": "US/Central",
    "ET": "US/Eastern", "EST": "US/Eastern", "EDT": "US/Eastern",
}


def parse_break_time(text: str) -> str | None:
    """Parse '1:30PM PST' style text into an ISO datetime string (assumes today,
    or tomorrow if the time already passed). Returns None if unparseable."""
    if not text:
        return None
    from datetime import datetime
    from zoneinfo import ZoneInfo

    m = re.match(
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*([A-Z]{2,4})?",
        text.strip(),
        re.IGNORECASE,
    )
    if not m:
        return None
    hour, minute, ampm, tz_abbr = m.groups()
    hour, minute = int(hour), int(minute or 0)
    if ampm.lower() == "pm" and hour != 12:
        hour += 12
    if ampm.lower() == "am" and hour == 12:
        hour = 0

    tz_name = TZ_MAP.get((tz_abbr or "CT").upper(), "US/Central")
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # If the time already passed today, assume tomorrow
    if dt <= now:
        from datetime import timedelta
        dt = dt + timedelta(days=1)
    return dt.isoformat()

# Platform URL patterns: (regex, platform name)
PLATFORM_URL_PATTERNS = [
    (r'youtube\.com/@([^/"\s<]+)', "YouTube"),
    (r'youtu\.be/([^/"\s<]+)', "YouTube"),
    (r'facebook\.com/([^/"\s<]+)', "Facebook"),
    (r'twitch\.tv/([^/"\s<]+)', "Twitch"),
    (r'whatnot\.com/([^/"\s<]+)', "Whatnot"),
    (r'tiktok\.com/@([^/"\s<]+)', "TikTok"),
    (r'instagram\.com/([^/"\s<]+)', "Instagram"),
]


def extract_video_info(item: dict) -> dict:
    """Extract ALL video platforms, URLs, and break time from a getItem response.

    Returns dict with:
      - video_links: list of {"url": ..., "platform": ...} (may be empty)
      - video_url: first URL (backwards compat)
      - video_platform: first platform (backwards compat)
      - break_time_text: str or None
    """
    links = []  # list of (url, platform)
    seen_urls = set()

    def add_link(url, platform):
        if url and url not in seen_urls:
            seen_urls.add(url)
            links.append({"url": url, "platform": platform})

    # Item specifics (localizedAspects)
    aspects = item.get("localizedAspects") or []
    streaming_service = None
    break_time_text = None
    for aspect in aspects:
        name = (aspect.get("name") or "").lower()
        value = (aspect.get("value") or "").strip()
        if "streaming service" in name and value:
            streaming_service = value
        elif name == "time" and value and value.lower() not in ("tbd", "see break calendar"):
            break_time_text = value

    # Description HTML
    desc = item.get("description") or ""

    # eBay Live detection (from description or streaming service)
    if streaming_service and "ebay" in streaming_service.lower() and "live" in streaming_service.lower():
        add_link(None, "eBay Live")
    elif re.search(r"ebay\s*live", desc, re.IGNORECASE):
        add_link(None, "eBay Live")
    
    # If streaming service specified but no URL found, add it as platform-only
    if streaming_service and not any(l["platform"].lower() == streaming_service.lower() for l in links):
        # Don't add if we already have a link for this platform
        pass

    # Find ALL platform URLs in description
    for pattern, platform in PLATFORM_URL_PATTERNS:
        for match in re.finditer(pattern, desc, re.IGNORECASE):
            identifier = match.group(1)
            # Reconstruct URL based on platform
            if platform == "YouTube":
                url = f"https://www.youtube.com/@{identifier}"
            elif platform == "Facebook":
                url = f"https://www.facebook.com/{identifier}"
            elif platform == "Twitch":
                url = f"https://www.twitch.tv/{identifier}"
            elif platform == "Whatnot":
                url = f"https://www.whatnot.com/{identifier}"
            elif platform == "TikTok":
                url = f"https://www.tiktok.com/@{identifier}"
            elif platform == "Instagram":
                url = f"https://www.instagram.com/{identifier}"
            else:
                url = match.group(0)
            add_link(url, platform)

    # Break time phrases (if not already from item specifics)
    if not break_time_text:
        for pattern in TIME_PATTERNS:
            m = re.search(pattern, desc, re.IGNORECASE)
            if m:
                break_time_text = m.group(1).strip()
                break

    # Backwards compat: first link as video_url/video_platform
    video_url = links[0]["url"] if links else None
    video_platform = links[0]["platform"] if links else None

    # Parse break time into a datetime for standard formatting (Brian 2026-10-06)
    break_starts_at = parse_break_time(break_time_text) if break_time_text else None

    return {
        "video_links": links,
        "video_url": video_url,
        "video_platform": video_platform,
        "break_time_text": break_time_text,
        "break_starts_at": break_starts_at,
    }


def extract_for_listing(item_url: str) -> dict:
    """Fetch item via Browse API getItem and extract video info.

    item_url: eBay listing URL (https://www.ebay.com/itm/{item_id}...)
    Returns dict with video_links, video_url, video_platform, break_time_text.
    """
    from .ebay import get_app_token, get_item

    match = re.search(r"/itm/(\d+)", item_url)
    if not match:
        return {"video_links": [], "video_url": None, "video_platform": None, "break_time_text": None}

    numeric_id = match.group(1)
    # eBay Browse API expects itemId in format v1|{numeric_id}|0, not just the
    # numeric ID from the URL. (Brian 2026-10-06: getItem 404s with numeric ID)
    api_item_id = f"v1|{numeric_id}|0"

    try:
        token = get_app_token()
    except Exception as e:
        print(f"eBay token failed: {e}")
        return {"video_links": [], "video_url": None, "video_platform": None, "break_time_text": None}

    item = get_item(api_item_id, token)
    if not item:
        return {"video_links": [], "video_url": None, "video_platform": None, "break_time_text": None}

    return extract_video_info(item)
