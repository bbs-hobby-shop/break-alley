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
    "PT": "America/Los_Angeles", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "MT": "America/Denver", "MST": "America/Denver", "MDT": "America/Denver",
    "CT": "America/Chicago", "CST": "America/Chicago", "CDT": "America/Chicago",
    "ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York",
}


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Date expressions seen in real listings (2026-10-14 false-live investigation):
# item-specifics "Date: 10/16/26", description "FRIDAY, OCTOBER 16TH @ 7:50 PM".
_DATE_PATTERNS = [
    re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})"),  # 10/16/2026 or 10/16/26
    re.compile(
        r"(january|february|march|april|may|june|july|august|september|"
        r"october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|"
        r"oct|nov|dec)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{4}))?",
        re.IGNORECASE,
    ),  # October 16th / Oct 16, 2026
    re.compile(r"(\d{1,2})/(\d{1,2})(?!\d)"),  # 10/16 (no year)
]


def find_date_in_text(text: str, now=None, allow_yearless_numeric: bool = True):
    """Find an explicit calendar date in listing text. Returns a date or None.

    Yearless dates resolve to the next occurrence (a date more than a day in
    the past is next year's — sellers advertise upcoming breaks, not past
    ones). allow_yearless_numeric=False skips the bare M/D pattern: in free
    description text it false-matches case fractions like "1/4 Case Break".
    Only structured fields (the Date item specific) may use it."""
    if not text:
        return None
    from datetime import date, datetime, timedelta
    from zoneinfo import ZoneInfo

    if now is None:
        now = datetime.now(ZoneInfo("America/Chicago"))
    today = now.date()
    patterns = _DATE_PATTERNS if allow_yearless_numeric else _DATE_PATTERNS[:2]
    for pat in patterns:
        m = pat.search(text)
        if not m:
            continue
        g = m.groups()
        try:
            if len(g) == 3 and g[0].isdigit() and g[2]:
                month, day = int(g[0]), int(g[1])
                year = int(g[2])
                if year < 100:
                    year += 2000
                return date(year, month, day)
            if len(g) == 3 and not g[0].isdigit():
                month = _MONTHS[g[0].lower()[:3]]
                day = int(g[1])
                if g[2]:
                    return date(int(g[2]), month, day)
                candidate = date(today.year, month, day)
                if candidate < today - timedelta(days=1):
                    candidate = date(today.year + 1, month, day)
                return candidate
            if len(g) == 2:
                month, day = int(g[0]), int(g[1])
                candidate = date(today.year, month, day)
                if candidate < today - timedelta(days=1):
                    candidate = date(today.year + 1, month, day)
                return candidate
        except (ValueError, KeyError):
            continue
    return None


def parse_break_time(text: str) -> str | None:
    """Parse break time text into an ISO datetime string.

    If the text carries an explicit DATE (e.g. '10/16/26 9:00 PM EST' or
    'October 16th 7:50 PM CST'), that date is used as-is — NEVER rolled to
    today/tomorrow. The old today/tomorrow invention stamped future breaks
    as starting 'today at that time', which flagged ~1,100 not-live eBay
    listings as LIVE (Brian, 2026-10-14). Only a bare time-of-day (a daily
    showtime with no date anywhere) keeps the legacy today/tomorrow roll.
    Returns None if unparseable."""
    if not text:
        return None
    from datetime import datetime
    from zoneinfo import ZoneInfo

    m = re.search(
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

    tz_name = TZ_MAP.get((tz_abbr or "CT").upper(), "America/Chicago")
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    explicit_date = find_date_in_text(text, now=now)
    if explicit_date is not None:
        dt = datetime(
            explicit_date.year, explicit_date.month, explicit_date.day,
            hour, minute, tzinfo=tz,
        )
        return dt.isoformat()
    dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # Bare time-of-day (daily showtime): if it already passed today, tomorrow
    if dt <= now:
        from datetime import timedelta
        dt = dt + timedelta(days=1)
    return dt.isoformat()

def has_ebay_live_destination(video_links) -> bool:
    """True when the listing's stream destination is eBay Live itself.

    eBay "live" semantics (Brian 2026-10-14, after two fix rounds): a
    channel-page link (youtube.com/@seller) is a destination, not a live
    stream — sellers with such links sat flagged LIVE for hours while
    their channels were dark, because a scheduled time + a link is not
    evidence of a running stream. Off-platform streams are carried as
    their own YouTube/Twitch rows when they are actually live, so eBay
    rows may claim live ONLY for streams hosted on eBay Live, inside the
    grant window. Everything else is an upcoming break."""
    links = video_links
    if isinstance(links, str):
        try:
            links = json.loads(links)
        except Exception:
            links = None
    if not links:
        return False
    return any(
        isinstance(link, dict) and link.get("platform") == "eBay Live"
        for link in links
    )


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
    break_date_text = None
    for aspect in aspects:
        name = (aspect.get("name") or "").lower()
        value = (aspect.get("value") or "").strip()
        if "streaming service" in name and value:
            streaming_service = value
        elif name == "time" and value and value.lower() not in ("tbd", "see break calendar"):
            break_time_text = value
        elif name == "date" and value and value.lower() not in ("tbd", "see break calendar"):
            break_date_text = value

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

    # Attach the break's calendar DATE to the time text whenever one exists —
    # a "Date" item specific wins, else any explicit date in the description
    # ("FRIDAY, OCTOBER 16TH @ 7:50 PM"). Without it, parse_break_time must
    # invent today/tomorrow for the time-of-day, which stamped future breaks
    # as live (2026-10-14 false-live investigation).
    if break_time_text and find_date_in_text(break_time_text) is None:
        found = find_date_in_text(break_date_text) if break_date_text else None
        if found is None and desc:
            found = find_date_in_text(desc, allow_yearless_numeric=False)
        if found is not None:
            break_time_text = f"{found.month}/{found.day}/{found.year} {break_time_text}"

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
