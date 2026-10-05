"""Normalize raw source data (titles, prices) into the break schema.

V1 approach: keyword/regex matching. Good enough to start; the product alias
table (products.py / products DB table) is curated by hand and grows over time.
"""
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .products import PRODUCTS

CENTRAL = ZoneInfo("America/Chicago")

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

# Strong title signals of a REAL buy-in break (not a recap, vlog, or casual
# opening). A stream/video is kept when a break format was detected above
# OR any of these match. Shared by the YouTube and Twitch ingesters.
STRONG_BREAK_SIGNALS = [
    r"break\s*#\s*\d+",          # "Break #12"
    r"#\d+\s*(pyt|break)",       # "#12 PYT Break"
    r"\bslots?\b",               # "slots left", "8 slots"
    r"\bspots?\b.{0,20}\b(left|available|open|for sale)\b",
    r"live\s*fills?",            # "live fills"
    r"pick\s*your",              # "pick your team/division"
    r"\bgroup\s*break\b",
    r"\bmixer\b",                # "10 Box Mixer"
    r"\d+\s*box.{0,25}\bbreak\b",  # "32 Box PLAYER Break"
    r"\bteams?\b.{0,25}\b(available|left|open|for sale)\b",
]


# Title words that mark a video as NOT a sports-card buy-in break even when it
# is a scheduled stream: recaps/vlogs/etc, and non-sports card games (Pokemon,
# Yu-Gi-Oh, Magic, Lorcana...). Brian wants sports card box breaks only.
NON_BREAK_TITLE_WORDS = [
    r"\brecap\b",
    r"\bhighlights?\b",
    r"\bvlog\b",
    r"\bcollection\b",
    r"\bmail\s*days?\b",
    r"\bunboxing\b",
    # trading card games that are not sports cards
    r"\bpokemon\b",
    r"\byu[\s-]?gi[\s-]?oh\b",
    r"\byugioh\b",
    r"\bmagic\b.{0,15}\bgathering\b",
    r"\bmtg\b",
    r"\blorcana\b",
    r"\bone\s*piece\b",
    r"\bdigimon\b",
    r"\bdragon\s*ball\b",
    r"\bflesh\s+and\s+blood\b",
]


def looks_like_real_break(title: str, fmt: str | None) -> bool:
    """True for titles that read like actual buy-in break listings.

    Candidates are already live/upcoming streams matching break queries, so
    the bar is: a detected break format, the word "break" in the title, or a
    strong break signal — minus explicit non-break content (recaps, vlogs).
    """
    if any(re.search(p, title, re.IGNORECASE) for p in NON_BREAK_TITLE_WORDS):
        return False
    if fmt and fmt != "unknown":
        return True
    if "break" in title.lower():
        return True
    return any(re.search(p, title, re.IGNORECASE) for p in STRONG_BREAK_SIGNALS)


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


# ---------------------------------------------------------------------------
# Display formatting: turn raw stream/listing titles into clean card titles.
# ---------------------------------------------------------------------------

# Acronyms and codes that stay uppercase when calming an ALL-CAPS title.
_TITLE_ACRONYMS = {
    "PYT", "PYC", "PYS", "PYD", "NFL", "NBA", "MLB", "NHL", "UFC", "PGA",
    "FIFA", "EPL", "UD", "RT", "GB", "CT", "DT", "HD", "TV", "FB", "BB",
}

# Words with non-standard casing worth preserving.
_TITLE_SPECIAL_CASE = {
    "ebay": "eBay",
    "topps": "Topps",
    "panini": "Panini",
    "upper": "Upper",
    "prizm": "Prizm",
}


def _cap_word(word: str) -> str:
    """Capitalize the first letter, lowercase the rest (punctuation kept)."""
    out, first = [], True
    for ch in word:
        if ch.isalpha() and first:
            out.append(ch.upper())
            first = False
        elif ch.isalpha():
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out)


def display_title(title: str) -> str:
    """Clean a raw title for display.

    Collapses whitespace; if the title is mostly ALL CAPS (typical of
    streamer titles), converts to title case while keeping acronyms
    (PYT, NFL, ...) and special casings (eBay) intact.
    """
    t = re.sub(r"\s+", " ", (title or "")).strip()
    if not t:
        return t
    letters = [c for c in t if c.isalpha()]
    shouty = bool(letters) and (
        sum(1 for c in letters if c.isupper()) / len(letters) >= 0.6
    )
    if not shouty:
        return t
    words = []
    for word in t.split(" "):
        core = word.strip("()#,.:!?\"'").upper()
        if core in _TITLE_ACRONYMS:
            words.append(word)  # keep acronym as-is
        elif core.lower() in _TITLE_SPECIAL_CASE:
            # preserve e.g. eBay inside longer tokens like "eBayLive"
            words.append(re.sub(
                core.lower(), _TITLE_SPECIAL_CASE[core.lower()],
                word, flags=re.IGNORECASE,
            ))
        else:
            words.append(_cap_word(word))
    return " ".join(words)


def extract_break_number(title: str) -> str | None:
    """'Break #12' from a title, else None."""
    m = re.search(r"break\s*#\s*(\d+)", title or "", re.IGNORECASE)
    return f"Break #{m.group(1)}" if m else None


def date_label(value) -> str | None:
    """Friendly Central-time label: 'Today · 7:30 PM', 'Tomorrow · 8:00 PM',
    or 'Sat Oct 11 · 7:30 PM'. Accepts datetimes or ISO strings."""
    if not value:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(CENTRAL)
    today = datetime.now(CENTRAL).date()
    try:
        t = local.strftime("%-I:%M %p")
    except ValueError:  # non-Linux strftime
        t = local.strftime("%I:%M %p").lstrip("0")
    day = local.date()
    if day == today:
        return f"Today · {t}"
    if day == today + timedelta(days=1):
        return f"Tomorrow · {t}"
    return f"{local.strftime('%a %b %d')} · {t}"
