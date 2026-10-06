"""Extract live video location and break time from eBay listing descriptions.

Patterns (verified 2026-10-06 via manual audit of 3 listings):
1. Item specifics: "Streaming Service" field (e.g., YouTube), "Time"/"Date" fields
2. Description: "WHEN AND WHERE IS MY BREAK?" section with
   "Breaks are on our {platform} Livestream:" + linked channel name
3. eBay Live badge: "See events" link to ebay.com/ebaylive/sellers/{hash}
4. Phrases: "break night at 7 pm CT", "PREFILL RIPS ONCE FULL {date}",
   "randomized by random.org on {Name} YouTube channel"
"""

import os
import re
from urllib.parse import urlparse, parse_qs

# Sanitize proxy env (VM quirk: bracketed IPv6 in no_proxy breaks httpx)
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "localhost,127.0.0.1"

import httpx

# Platform detection patterns
PLATFORM_PATTERNS = [
    (r"youtube\.com|youtu\.be", "YouTube"),
    (r"facebook\.com", "Facebook"),
    (r"twitch\.tv", "Twitch"),
    (r"whatnot\.com", "Whatnot"),
    (r"ebay\.com/ebaylive", "eBay Live"),
    (r"instagram\.com", "Instagram"),
    (r"tiktok\.com", "TikTok"),
]

# Time patterns: "7 pm CT", "8:30pm EST", "Saturday 7pm", etc.
TIME_PATTERNS = [
    r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*(?:CT|ET|PT|MT|EST|EDT|CST|CDT|PST|PDT|MST|MDT))",
    r"((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s*(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*(?:CT|ET|PT|MT)?)?)",
    r"break\s*night\s*(?:at\s*)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*(?:CT|ET|PT|MT)?)",
]

def extract_video_info(html: str, item_url: str) -> dict:
    """Extract video platform, URL, and break time from eBay listing HTML.
    
    Returns dict with: video_url, video_platform, break_time_text (all may be None)
    """
    result = {"video_url": None, "video_platform": None, "break_time_text": None}
    
    # 1. Look for eBay Live "See events" link
    ebay_live_match = re.search(
        r'href="(https://www\.ebay\.com/ebaylive/sellers/[^"]+)"', html, re.IGNORECASE
    )
    if ebay_live_match:
        result["video_url"] = ebay_live_match.group(1)
        result["video_platform"] = "eBay Live"
    
    # 2. Look for "Streaming Service" in item specifics
    streaming_match = re.search(
        r"Streaming Service</[^>]+>[^<]*<[^>]+>([^<]+)", html, re.IGNORECASE
    )
    if streaming_match:
        platform = streaming_match.group(1).strip()
        if not result["video_platform"]:
            result["video_platform"] = platform
    
    # 3. Look for YouTube channel links in description
    # Pattern: "Breaks are on our YouTube Livestream:" + link
    yt_match = re.search(
        r'(?:youtube\.com/@([^/"\s]+)|youtube\.com/channel/([^/"\s]+))', html, re.IGNORECASE
    )
    if yt_match:
        channel = yt_match.group(1) or yt_match.group(2)
        result["video_url"] = f"https://www.youtube.com/@{channel}"
        result["video_platform"] = "YouTube"
    
    # 4. Look for break time phrases
    for pattern in TIME_PATTERNS:
        time_match = re.search(pattern, html, re.IGNORECASE)
        if time_match:
            result["break_time_text"] = time_match.group(1).strip()
            break
    
    # 5. Look for "WHEN AND WHERE IS MY BREAK?" section
    break_section = re.search(
        r"WHEN AND WHERE IS MY BREAK\?.*?(<ul>.*?</ul>|<p>.*?</p>)",
        html, re.IGNORECASE | re.DOTALL
    )
    if break_section:
        section_html = break_section.group(1)
        # Extract time from the section
        if not result["break_time_text"]:
            for pattern in TIME_PATTERNS:
                m = re.search(pattern, section_html, re.IGNORECASE)
                if m:
                    result["break_time_text"] = m.group(1).strip()
                    break
    
    return result


def fetch_listing_html(item_url: str) -> str | None:
    """Fetch eBay listing page HTML. Returns None on failure."""
    try:
        # Extract item ID from URL
        # Format: https://www.ebay.com/itm/128103773037?...
        match = re.search(r"/itm/(\d+)", item_url)
        if not match:
            return None
        item_id = match.group(1)
        
        # Use the mobile/description URL for cleaner HTML
        url = f"https://www.ebay.com/itm/{item_id}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        with httpx.Client(timeout=30, headers=headers) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text
    except Exception as e:
        print(f"Failed to fetch {item_url}: {e}")
        return None


def extract_for_listing(item_url: str) -> dict:
    """Fetch a listing and extract video info. Returns dict with video fields."""
    html = fetch_listing_html(item_url)
    if not html:
        return {"video_url": None, "video_platform": None, "break_time_text": None}
    return extract_video_info(html, item_url)
