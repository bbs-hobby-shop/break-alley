"""Normalize raw source data (titles, prices) into the break schema.

V1 approach: keyword/regex matching. Good enough to start; the product alias
table (products.py / products DB table) is curated by hand and grows over time.
"""
import re
from decimal import Decimal, InvalidOperation

from .products import PRODUCTS

SPORT_KEYWORDS = {
    # Only unambiguous sport words here. Brand words (panini, topps, bowman,
    # upper deck) span multiple sports and are handled via the product table.
    "football": [r"\bfootball\b", r"\bnfl\b"],
    "basketball": [r"\bbasketball\b", r"\bnba\b"],
    "baseball": [r"\bbaseball\b", r"\bmlb\b"],
    "soccer": [r"\bsoccer\b", r"\bpremier league\b", r"\bepl\b", r"\bfifa\b"],
    "hockey": [r"\bhockey\b", r"\bnhl\b"],
}

FORMAT_PATTERNS = [
    ("pyt", [r"\bpyt\b", r"pick your team"]),
    ("random", [r"\brandom\b", r"\brandom team\b"]),
    ("division", [r"\bdivision\b"]),
    ("hit_draft", [r"hit\s*draft"]),
    ("personal", [r"\bpersonal\b"]),
    ("case_break", [r"\bcase break\b", r"\bcase\b.*\bbreak\b"]),
]


def _first_match(text: str, patterns: dict | list) -> str | None:
    items = patterns.items() if isinstance(patterns, dict) else patterns
    for key, pats in items:
        for pat in pats:
            if re.search(pat, text, re.IGNORECASE):
                return key
    return None


def detect_sport(title: str) -> str:
    # Prefer the curated product table (explicit sport) over bare keywords,
    # since brand words like "topps" span multiple sports.
    canonical = normalize_product(title)
    if canonical:
        return PRODUCTS[canonical]["sport"]
    return _first_match(title, SPORT_KEYWORDS) or "other"


def detect_format(title: str) -> str:
    return _first_match(title, FORMAT_PATTERNS) or "unknown"


def normalize_product(title: str) -> str | None:
    """Return the canonical product name if any alias matches the title."""
    lowered = title.lower()
    for canonical, info in PRODUCTS.items():
        for alias in info["aliases"]:
            if alias in lowered:
                return canonical
    return None


def parse_price(value) -> Decimal | None:
    """Parse eBay-style price values ('19.99', 19.99) -> Decimal."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def normalize_ebay_item(item: dict, affiliate_url: str | None = None) -> dict:
    """Turn one eBay Browse API itemSummary into a normalized break row."""
    title = item.get("title", "") or ""
    price_info = item.get("price", {}) or {}
    avail = (item.get("estimatedAvailabilities") or [{}])[0]
    return {
        "source": "ebay",
        "source_url": item.get("itemWebUrl"),
        "breaker": (item.get("seller") or {}).get("username"),
        "product_raw": title,
        "product_normalized": normalize_product(title),
        "sport": detect_sport(title),
        "format": detect_format(title),
        "price": parse_price(price_info.get("value")),
        "currency": price_info.get("currency") or "USD",
        "starts_at": None,          # eBay slot listings have no scheduled start
        "is_live": False,           # eBay listings are buyable now, not "live streams"
        "slots_total": None,
        "slots_remaining": avail.get("estimatedAvailableQuantity"),
        "thumbnail_url": (item.get("image") or {}).get("imageUrl"),
        "title_raw": title,
        "affiliate_url": affiliate_url,
        "expires_at": None,
    }
