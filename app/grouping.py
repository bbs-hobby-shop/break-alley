"""Break grouping (stub, V1 heuristic).

On eBay one break shows up as ~32 listings (one per team slot). This module
groups listings that likely belong to the same break so the UI can show one
break with N slots instead of N separate results.

Grouping key: (seller, break_number, product_key)
  - seller: lowercased seller username
  - break_number: "#12" style number extracted from the title (may be None)
  - product_key: canonical product name when the normalizer matched one,
    else the sport, else "unknown"

This is intentionally simple for V1. It will over/under-group on messy
titles; the product alias table is the main lever for improving it.
"""
import re
from collections import defaultdict

BREAK_NUMBER_RE = re.compile(r"#(\d{1,4})\b")


def extract_break_number(title: str) -> str | None:
    m = BREAK_NUMBER_RE.search(title or "")
    return m.group(1) if m else None


def group_key(row: dict) -> tuple:
    seller = (row.get("breaker") or "").strip().lower()
    number = extract_break_number(row.get("title_raw") or "")
    product = row.get("product_normalized") or row.get("sport") or "unknown"
    return (seller, number, product)


def group_listings(rows: list[dict]) -> dict[tuple, list[dict]]:
    """Group normalized eBay rows into likely breaks.

    Returns {group_key: [rows]}. Single-listing groups are breaks with one
    slot seen (or personal breaks); callers decide how to display them.
    """
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        groups[group_key(row)].append(row)
    return dict(groups)


def summarize_group(key: tuple, rows: list[dict]) -> dict:
    """Roll a group up into one displayable break summary."""
    seller, number, product = key
    prices = [r["price"] for r in rows if r.get("price") is not None]
    return {
        "breaker": rows[0].get("breaker"),
        "break_number": number,
        "product": product,
        "sport": rows[0].get("sport"),
        "format": rows[0].get("format"),
        "slot_count": len(rows),
        "min_price": min(prices) if prices else None,
        "max_price": max(prices) if prices else None,
        "thumbnail_url": rows[0].get("thumbnail_url"),
        "listings": rows,
    }
