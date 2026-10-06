"""Extract live video location and break time from eBay listings via the
Browse API getItem endpoint (no scraping — eBay 403s datacenter IPs).

Patterns (verified 2026-10-06 via manual audit):
1. Item specifics: "Streaming Service" field (e.g., YouTube), "Time"/"Date"
2. Description: "WHEN AND WHERE IS MY BREAK?" section with
   "Breaks are on our {platform} Livestream:" + linked channel name
3. eBay Live: item specifics or description mentions
4. Phrases: "break night at 7 pm CT", "PREFILL RIPS ONCE FULL {date}"
"""

import os
import re

# Sanitize proxy env (VM quirk: bracketed IPv6 in no_proxy breaks httpx)
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "localhost,127.0.0.1"

# Time patterns: "7 pm CT", "8:30pm EST", "Saturday 7pm", etc.
TIME_PATTERNS = [
    r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*(?:CT|ET|PT|MT|EST|EDT|CST|CDT|PST|PDT|MST|MDT))",
    r"break\s*night\s*(?:at\s*)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*(?:CT|ET|PT|MT)?)",
]


def extract_video_info(item: dict) -> dict:
    """Extract video platform, URL, and break time from a getItem response.

    Returns dict with: video_url, video_platform, break_time_text (may be None)
    """
    result = {"video_url": None, "video_platform": None, "break_time_text": None}

    # Item specifics (localizedAspects)
    aspects = item.get("localizedAspects") or []
    for aspect in aspects:
        name = (aspect.get("name") or "").lower()
        value = (aspect.get("value") or "").strip()
        if "streaming service" in name and value:
            result["video_platform"] = value
        elif name == "time" and value and value.lower() not in ("tbd", "see break calendar"):
            result["break_time_text"] = value

    # Description HTML
    desc = item.get("description") or ""

    # eBay Live detection
    if re.search(r"ebay\s*live", desc, re.IGNORECASE):
        if not result["video_platform"]:
            result["video_platform"] = "eBay Live"

    # YouTube channel links
    yt_match = re.search(
        r'youtube\.com/@([^/"\s<]+)', desc, re.IGNORECASE
    )
    if yt_match:
        result["video_url"] = f"https://www.youtube.com/@{yt_match.group(1)}"
        result["video_platform"] = "YouTube"

    # Break time phrases (if not already from item specifics)
    if not result["break_time_text"]:
        for pattern in TIME_PATTERNS:
            m = re.search(pattern, desc, re.IGNORECASE)
            if m:
                result["break_time_text"] = m.group(1).strip()
                break

    return result


def extract_for_listing(item_url: str) -> dict:
    """Fetch item via Browse API getItem and extract video info.

    item_url: eBay listing URL (https://www.ebay.com/itm/{item_id}...)
    Returns dict with video_url, video_platform, break_time_text.
    """
    from .ebay import get_app_token, get_item

    match = re.search(r"/itm/(\d+)", item_url)
    if not match:
        return {"video_url": None, "video_platform": None, "break_time_text": None}

    try:
        token = get_app_token()
    except Exception as e:
        print(f"eBay token failed: {e}")
        return {"video_url": None, "video_platform": None, "break_time_text": None}

    item = get_item(match.group(1), token)
    if not item:
        return {"video_url": None, "video_platform": None, "break_time_text": None}

    return extract_video_info(item)
