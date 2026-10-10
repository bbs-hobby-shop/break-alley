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
    "football": [r"\bfootball\b", r"\bnfl\b", r"\bcollege football\b", r"\bcfb\b",
                 "🏈"],
    "basketball": [r"\bbasketball\b", r"\bnba\b", r"\bwnba\b", "🏀"],
    "baseball": [r"\bbaseball\b", r"\bmlb\b", r"allen\s*&\s*ginter\b",
               # NOTE: "bowman" is NOT here — Bowman makes football and
               # basketball cards too (Bowman NFL/U broke this 2026-10-09).
               r"\bdiamond kings\b", r"\btopps heritage\b", "⚾"],
    "soccer": [r"\bsoccer\b", r"\bpremier league\b", r"\bepl\b", r"\bfifa\b",
               r"\bmls\b", r"\bchampions league\b", r"\bworld cup\b", r"\bla liga\b",
               "⚽"],
    "hockey": [r"\bhockey\b", r"\bnhl\b", "🏒"],
    # Brian 2026-10-09: TCG breaks (Pokemon etc.) are real box breaks too —
    # they get their own sport so buyers can filter for them.
    "tcg": [r"\bpok[eé]mon\b", r"\btcg\b", r"\bmagic\b.{0,15}\bgathering\b",
            r"\bmtg\b", r"\byu-?gi-?oh\b", r"\blorcana\b", r"\bone piece\b.{0,10}\bcard\b",
            r"\bdigimon\b", r"\bweiss schwarz\b",
            # Brian 2026-10-09: Pokemon set names (researched 2026-10-09) —
            # titles like "30th Celebration Box Break" say Pokemon without
            # saying "pokemon". Mega Evolution era + Scarlet & Violet era +
            # Sword & Shield era.
            r"\bmega evolution\b", r"\bphantasmal flames\b", r"\bascended heroes\b",
            r"\bperfect order\b", r"\bchaos rising\b", r"\bpitch black\b",
            r"\b30th celebration\b", r"\b30th anniversary\b", r"\bdelta reign\b",
            r"\bprismatic evolutions?\b", r"\bsurging sparks\b", r"\bstellar crown\b",
            r"\bshrouded fable\b", r"\btwilight masquerade\b", r"\btemporal forces\b",
            r"\bpaldean fates\b", r"\bparadox rift\b", r"\b151\b",
            r"\bobsidian flames\b", r"\bpaldea evolved\b",
            r"\bscarlet\s*&\s*violet\b", r"\bscarlet and violet\b",
            r"\bdestined rivals\b", r"\bjourney together\b",
            r"\bblack bolt\b", r"\bwhite flare\b",
            r"\bcrown zenith\b", r"\bsilver tempest\b", r"\blost origin\b",
            r"\bastral radiance\b", r"\bbrilliant stars\b", r"\bevolving skies\b",
            r"\bcelebrations\b", r"\bfusion strike\b", r"\bchilling reign\b",
            r"\bbattle styles\b", r"\bvivid voltage\b", r"\bdarkness ablaze\b",
            r"\bshining fates\b", r"\bpokemon go\b"],
    # Brian 2026-10-09: racing, wrestling/combat, golf, tennis all get their
    # own sport + thumbnail — no break settles for "other".
    "racing": [r"\bnascar\b", r"\bf1\b", r"\bformula\s?1\b", r"\bindycar\b",
               r"\bmotogp\b", r"\bracing\b", "🏎"],
    # NOTE: "boxing day" is soccer (Premier League fixtures), not the sport.
    "wrestling": [r"\bwwe\b", r"\baew\b", r"\bwrestling\b", r"\bwrestlemania\b",
                  r"\bufc\b", r"\bmma\b", r"\bboxing\b(?!\s*day)", "🤼"],
    "golf": [r"\bgolf\b", r"\bpga\b", r"\bliv golf\b", "⛳"],
    "tennis": [r"\btennis\b", r"\batp\b", r"\bwta\b", "🎾"],
}

# Brian 2026-10-09: buyer-facing sport names + thumbnails. Every break gets a
# real sport — "other" is only the last-resort fallback, never the default.
SPORT_LABELS = {
    "football": "Football",
    "baseball": "Baseball",
    "basketball": "Basketball",
    "hockey": "Hockey",
    "soccer": "Soccer",
    "tcg": "TCG",
    "racing": "Racing",
    "wrestling": "Wrestling",
    "golf": "Golf",
    "tennis": "Tennis",
    "other": "Other",
}
SPORT_ICONS = {
    "football": "🏈",
    "baseball": "⚾",
    "basketball": "🏀",
    "hockey": "🏒",
    "soccer": "⚽",
    "tcg": "🃏",
    "racing": "🏎️",
    "wrestling": "🤼",
    "golf": "⛳",
    "tennis": "🎾",
    "other": "🏟️",
}
# Cool custom thumbnail per sport (Brian 2026-10-09) — generated art, not
# generic emoji. "other" keeps the emoji fallback.
SPORT_IMAGES = {
    "football": "/static/sport-football.webp",
    "baseball": "/static/sport-baseball.webp",
    "basketball": "/static/sport-basketball.webp",
    "hockey": "/static/sport-hockey.webp",
    "soccer": "/static/sport-soccer.webp",
    "tcg": "/static/sport-tcg.webp",
    "racing": "/static/sport-racing.webp",
    "wrestling": "/static/sport-wrestling.webp",
    "golf": "/static/sport-golf.webp",
    "tennis": "/static/sport-tennis.webp",
}
# Filter dropdown order.
SPORTS = ["football", "baseball", "basketball", "hockey", "soccer", "tcg",
          "racing", "wrestling", "golf", "tennis", "other"]

# Five buyer-facing categories. Specific formats are checked first; anything
# else a real break is still a box break, so the generic bucket catches it.
FORMAT_PATTERNS = [
    ("pyt", [r"\bpyt\b", r"pick your team", r"\bteam break\b", r"\bplayer break\b"]),
    ("random", [r"\brandom\b", r"\brandom team\b", r"\bdivisions?\b.{0,20}\bbreak\b"]),
    ("personal", [r"\bpersonals?\b"]),
    ("case_break", [r"\bcase break\b", r"\bcase\b.*\bbreak\b"]),
    ("box_break", [r"\bbox break\b", r"\bgroup\s*breaks?\b", r"#groupbreaks?\b"]),
]

# Display names for the filter dropdown and card tags, including legacy
# values that may still sit in older rows until pollers re-ingest them.
FORMAT_LABELS = {
    "pyt": "Pick Your Team",
    "random": "Random",
    "personal": "Personal",
    "case_break": "Case Break",
    "box_break": "Box Break",
    "division": "Division",
    "hit_draft": "Hit Draft",
    "group_break": "Group Break",
    "team_break": "Team Break",
    "player_break": "Player Break",
}

# Strong title signals of a REAL buy-in break (not a recap, vlog, or casual
# opening). A stream/video is kept when a break format was detected above
# OR any of these match. Shared by the YouTube and Twitch ingesters.
STRONG_BREAK_SIGNALS = [
    r"break\s*#\s*\d+",          # "Break #12"
    r"#\d+\s*(pyt|break)",       # "#12 PYT Break"
    r"\bslots?\b.{0,20}\b(left|available|open|remaining|for sale)\b",
    # "8 slots left" — narrowed from bare "slots" which caught casino streams
    r"\bspots?\b.{0,20}\b(left|available|open|for sale)\b",
    r"live\s*fills?",            # "live fills"
    r"pick\s*your",              # "pick your team/division"
    r"\bgroup\s*break\b",
    r"\bmixer\b",                # "10 Box Mixer"
    r"\d+\s*box.{0,25}\bbreak\b",  # "32 Box PLAYER Break"
    r"\bteams?\b.{0,25}\b(available|left|open|for sale)\b",
    r"\$\s*\d+(?:\.\d{1,2})?\s*(?:/|per)\s*(?:slots?|spots?|teams?|entr(?:y|ies)|packs?)",
    # "$25/slot", "$40 per team" — a priced slot is the clearest buy-in proof
    r"\bbuy[\s-]*in\b(?!\s+bulk\b)",  # "buy in", "buy-in" (not "buy in bulk")
    r"\bentry\s*fee\b",               # "entry fee"
    r"\bclaim\b.{0,25}\b(spots?|slots?|teams?|packs?)\b",  # "claim your spot"
    r"\b(spots?|slots?|teams?)\b.{0,20}\bclaim\b",          # "spots open for claim"
    r"\bserial\b.{0,20}\bbreak\b",    # "serial # break", "serial number break"
    r"\$1\s*(auctions?|starts?)\b",  # "$1 auctions", "$1 starts" (Fanatics format, Brian 2026-10-07)
    r"\bauctions?\b.{0,25}\ball\s*day\b",  # "auctions all day"
]


# Gaming channels are never box-break channels — checked against the
# channel/breaker name as well as the title (Brian 2026-10-08: a Roblox
# "Ragdoll Break Live" stream slipped through on title alone).
GAMING_WORDS = [
    r"\broblox\b",
    r"\bfortnite\b",
    r"\bminecraft\b",
    r"\bgta\b",
    r"\bgameplay\b",
    r"\bgaming\b",
]


# Title words that mark a video as NOT a buy-in break even when it
# is a scheduled stream: recaps/vlogs/etc. TCG and non-sports breaks
# (Pokemon, Yu-Gi-Oh, Magic, Lorcana...) are welcome as long as they are
# real box breaks.
NON_BREAK_TITLE_WORDS = [
    r"\brecap\b",
    r"\bhighlights?\b",
    r"\bvlog\b",
    r"\bcollection\b",
    r"\bmail\s*days?\b",
    r"\bunboxing\b",
    # "At the Break" — single-card seller name, not a box break (Brian 2026-10-06)
    r"\bat\s+the\s+break\b",
    # Casino / gambling — "slots" here means slot machines, not break slots
    r"\bcasino\b",
    r"\bslot\s*machines?\b",
    r"\bgambling\b",
    r"\bbetting\b",
    r"\bplayer\s*props?\b",
    r"\bparlay\b",
    r"\bsportsbook\b",
    r"\bodds\b",
    r"\bspins?\b",
    r"\bjackpot\b",
    r"\bpacanele\b",  # Romanian for slot machines
    r"\bwatch\s*party\b",
    r"\bpreview\b",
    # Electrical circuit breakers — "breaker" keyword collision, not box breaks
    r"\bcircuit\s*breakers?\b",
    r"\bsquare\s*d\b",
    r"\b\d+\s*(amp|pole|volt)\b",
    r"\btiki\b",  # Tiki glasses/mugs, not breaks
    # Giveaways are not buy-in breaks
    r"\bgiveaway\b",
    r"\bfree\b.{0,30}\bbreak\b",
    # "Set Break" = selling singles from a set, NOT a box break (Brian 2026-10-06)
    r"\bset\s+break\b",
    # Card product names containing "break" — not box breaks
    r"\bfast\s+break\b",  # Panini Fast Break set
    r"\bbreak\s+out\b",  # Break Out subset
    r"\blimit\s+break\b",  # MTG card name
    # Single-card sales patterns
    r"\bpick\s+a\s+card\b",
    r"\bcomplete\s+your\s+set\b",
    r"\brookie\s+card\s+#\d+",  # "Rookie Card #384" = single
    # Test listings
    r"\btest\.live\.us-seller\b",
    # Non-card "breaks" (geodes, etc.)
    r"\bgeode",
    r"\bcrack\s+open\b",
    # Card lots/repacks — not box breaks
    r"\b\d+\s+card\s+lot\b",
    r"\bguaranteed\b.{0,20}\b(holo|rare|vmax|vstar)\b",
    # Gaming streams — "break" in game titles/context, not box breaks (Brian 2026-10-08)
    *GAMING_WORDS,
    # Gossip/celebrity "breaks silence" — not a box break (Brian 2026-10-08)
    r"\bbreak\s+(his|her|their|the)\s+silence\b",
]


# Words that put "break" in a card-break context. A title containing the
# standalone word "break(s)" is only kept when one of these is also present —
# this kills programming tutorials ("break & continue"), sermons
# ("break every bondage"), and fantasy shows ("break down").
BREAK_CONTEXT_WORDS = [
    r"\bbox(es)?\b", r"\bcase\b", r"\bpacks?\b", r"\bcards?\b",
    r"\bteams?\b", r"\bplayers?\b", r"\bdivisions?\b",
    r"\bmixer\b", r"\bslots?\b", r"\bspots?\b", r"\bdrafts?\b",
    r"\bgroups?\b", r"\bpersonals?\b", r"\bpyt\b",
    r"\bchrome\b", r"\bhobby\b", r"\bjumbo\b", r"\bmega\b", r"\bblaster\b",
    r"#?sportscards?\b", r"#?groupbreaks?\b",
]


def looks_like_real_break(title: str, fmt: str | None,
                          breaker: str | None = None) -> bool:
    """True for titles that read like actual buy-in break listings.

    Candidates are already live/upcoming streams matching break queries, so
    the bar is: a detected break format, a strong break signal, or the
    standalone word "break(s)" alongside card-context words —
    minus explicit non-break content (recaps, vlogs, mail days). TCG and
    non-sports breaks count as long as they are real buy-in breaks.

    A detected break format wins over non-break words: product names like
    "Topps Museum Collection" contain the blocklisted word "collection"
    (Brian 2026-10-07 — was dropping 155 real Fanatics breaks).

    Gaming channels are never box-break channels: when the breaker/channel
    name is passed, gaming words in it also reject the row (Brian
    2026-10-08 — a Roblox "Ragdoll Break Live" stream passed on title alone).
    """
    if fmt and fmt != "unknown":
        return True
    if any(re.search(p, title, re.IGNORECASE) for p in NON_BREAK_TITLE_WORDS):
        return False
    if breaker and any(re.search(p, breaker, re.IGNORECASE) for p in GAMING_WORDS):
        return False
    if any(re.search(p, title, re.IGNORECASE) for p in STRONG_BREAK_SIGNALS):
        return True
    return bool(
        re.search(r"(?:\bbreaks?\b|#groupbreaks?\b|#breaks?\b)", title, re.IGNORECASE)
        and any(re.search(p, title, re.IGNORECASE) for p in BREAK_CONTEXT_WORDS)
    )


def _first_match(text: str, patterns: dict | list) -> str | None:
    items = patterns.items() if isinstance(patterns, dict) else patterns
    for key, pats in items:
        for pat in pats:
            if re.search(pat, text, re.IGNORECASE):
                return key
    return None


def _keyword_sport_hits(title: str) -> list[str]:
    """Sport keys whose keywords match the title (dict order)."""
    return [key for key, pats in SPORT_KEYWORDS.items()
            if any(re.search(p, title, re.IGNORECASE) for p in pats)]


def detect_sports(title: str) -> list[str]:
    """All sports matching the title, in SPORTS display order.

    The curated product table counts as a match (its explicit sport comes
    first), plus any additional keyword sports. The full list drives the
    per-sport tags on cards (Brian 2026-10-09).
    """
    hits: list[str] = []
    canonical = normalize_product(title or "")
    if canonical:
        hits.append(PRODUCTS[canonical]["sport"])
    for s in _keyword_sport_hits(title or ""):
        if s not in hits:
            hits.append(s)
    order = {s: i for i, s in enumerate(SPORTS)}
    return sorted(hits, key=lambda s: order.get(s, 99))


def detect_sport(title: str) -> str:
    """Primary sport for a title — the first match in SPORTS order.

    Brian 2026-10-09: no more "multi" category. A title matching 2+ sports
    stores the primary sport here; the full list lives in detect_sports()
    (sports array column) and the card shows one tag per sport.
    """
    hits = detect_sports(title)
    return hits[0] if hits else "other"


def detect_formats(title: str) -> list[str]:
    """All formats matching the title, in FORMAT_PATTERNS order.

    Brian 2026-10-09: "PYT + Personals" gets both tags. box_break is the
    generic catch-all — it's dropped when a more specific format matched
    ("PYT Box Break" is just PYT, not two tags).
    """
    hits = [key for key, pats in FORMAT_PATTERNS
            if any(re.search(p, title or "", re.IGNORECASE) for p in pats)]
    if len(hits) > 1 and "box_break" in hits:
        hits.remove("box_break")
    return hits


def detect_format(title: str) -> str:
    """Specific format detected in the title, or 'unknown'.

    'unknown' is also the keep/drop signal in looks_like_real_break: a
    *detected* format means a real break. At write time db.upsert_break
    maps 'unknown' -> 'box_break' so every kept break lands in a category.
    """
    return _first_match(title, FORMAT_PATTERNS) or "unknown"


def format_label(fmt: str | None) -> str:
    """Buyer-facing label for a stored format value."""
    return FORMAT_LABELS.get(fmt or "", fmt or "")


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


def extract_break_group_key(title: str, breaker: str | None) -> str | None:
    """Group key for eBay listings that are team-by-team slices of one break.

    Professional breakers list each team separately ("Break #2428 - Arizona
    Cardinals", "Break #2428 - Atlanta Falcons", ...). The group key collapses
    these to one break: breaker + break number when present, else breaker +
    normalized title prefix (team suffix stripped).

    Returns None when no groupable pattern is found (single listing).
    """
    if not title or not breaker:
        return None
    t = title.strip()
    # Break number: "#2428", "Break #2428", "Break#2428"
    m = re.search(r"#\s*(\d{2,6})\b", t)
    if m:
        return f"ebay|{breaker.lower()}|break#{m.group(1)}"
    # Fallback: strip a trailing " - {team}" suffix and normalize.
    # Heuristic: if the title ends with " - X" where X is short (likely a team
    # name), use the prefix as the group key.
    m2 = re.match(r"^(.*?)\s+[-–|]\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})$", t)
    if m2:
        prefix = re.sub(r"[^a-z0-9]+", " ", m2.group(1).lower()).strip()
        if len(prefix) > 10:  # avoid over-grouping tiny titles
            return f"ebay|{breaker.lower()}|{prefix}"
    return None


def normalize_ebay_item(item: dict, affiliate_url: str | None = None) -> dict:
    """Turn one eBay Browse API itemSummary into a normalized break row."""
    title = item.get("title", "") or ""
    price_info = item.get("price", {}) or {}
    avail = (item.get("estimatedAvailabilities") or [{}])[0]
    breaker = (item.get("seller") or {}).get("username")
    # Auction detection (Brian 2026-10-07): eBay auctions get a countdown timer.
    # buyingOptions is e.g. ["AUCTION"], ["FIXED_PRICE"], or ["AUCTION", "FIXED_PRICE"].
    buying_options = [str(o).upper() for o in (item.get("buyingOptions") or [])]
    is_auction = "AUCTION" in buying_options
    auction_ends_at = item.get("itemEndDate")  # ISO 8601; TIMESTAMPTZ accepts it
    current_bid = None
    if is_auction:
        bid_info = item.get("currentBidPrice") or {}
        current_bid = parse_price(bid_info.get("value"))
    return {
        "source": "ebay",
        "source_url": item.get("itemWebUrl"),
        "breaker": breaker,
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
        "group_key": extract_break_group_key(title, breaker),
        "is_auction": is_auction,
        "auction_ends_at": auction_ends_at,
        "current_bid": current_bid,
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
