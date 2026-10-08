"""Fanatics Live shop-roster ingestion: cheap per-shop live monitoring.

Rationale: Fanatics Live exposes a public GraphQL API (no auth) at
https://www.fanatics.live/graphql. The liveStreams connection returns all
streams (paginated, max first:30); we filter to rostered shops and keep
live streams plus upcoming/scheduled streams (PREPARING with a real
startsAt) whose title reads like a real buy-in break.

COST: ~10 GraphQL GETs per run for the full stream list. Effectively
free, so the roster can grow large.

Pipeline per run:
  1. seed backfill: shop names from app/seed_fanatics_shops.txt
     (resolved to shop IDs via the live shops directory)
  2. fetch_all_streams() -> all streams on the platform
  3. keep only streams from rostered shops + normalize + title filter
     (normalize_fanatics_stream / looks_like_real_break)
  4. wipe + rewrite the fanatics slice in ONE transaction (Fanatics data is
     transient — ended streams vanish instead of lingering as stale
     rows) + stamp roster last_hit_at / last_checked_at
"""

import sys
from pathlib import Path

from . import db, fanatics
from .normalizer import (
    detect_format,
    detect_sport,
    looks_like_real_break,
    normalize_product,
)


def normalize_fanatics_stream(stream: dict) -> dict | None:
    """Turn one Fanatics Live stream into a normalized break row.

    Returns None when the stream has no usable title.
    Handles both live streams (is_live=True) and upcoming/scheduled
    streams (PREPARING with startsAt -> is_live=False, starts_at set).
    """
    from . import fanatics as fanatics_mod
    shop = stream.get("shop") or {}
    shop_name = (shop.get("name") or "").strip()
    title = (stream.get("name") or "").strip()
    if not title:
        return None
    stream_id = stream.get("id") or ""
    url = f"https://www.fanatics.live/shows/{stream_id}" if stream_id else None
    if not url:
        return None
    status = stream.get("status")
    is_live = fanatics_mod.is_live_status(status)
    # Upcoming: PREPARING with a real start time (not the 3000-01-01 placeholder)
    starts_at = stream.get("startsAt")
    if starts_at and str(starts_at).startswith("3000-"):
        starts_at = None
    return {
        "source": "fanatics",
        "source_url": url,
        "breaker": shop_name or "Fanatics Live",
        "product_raw": title,
        "product_normalized": normalize_product(title),
        "sport": detect_sport(title),
        "format": detect_format(title),
        "price": None,          # Fanatics streams have no slot price in the API
        "currency": "USD",
        "starts_at": starts_at,  # ISO 8601 -> timestamptz; None for live
        "is_live": is_live,
        "slots_total": None,
        "slots_remaining": None,
        "thumbnail_url": None,  # API exposes no thumbnail on LiveStream
        "title_raw": title,
        "affiliate_url": url,   # no affiliate program for Fanatics; plain link
        "expires_at": None,
    }


def fetch_roster_breaks(conn):
    """Poll rostered shops' live streams and return kept break rows.

    Returns (rows, hit_shop_ids, checked_shop_ids, stats) where hit_shop_ids
    maps shop_id -> shop name. Writes nothing — the caller upserts rows,
    wipes + rewrites the fanatics slice, and stamps the roster, so a total
    API failure leaves existing data untouched.
    """
    # 1. seed backfill: resolve seed-file names to shop IDs
    seed_path = Path(__file__).with_name("seed_fanatics_shops.txt")
    n_manual = 0
    if seed_path.exists():
        id_by_name = fanatics.load_fanatics_roster()  # name -> id
        n_manual = db.seed_manual_fanatics_shops(
            conn, seed_path.read_text().splitlines(), id_by_name)
        if n_manual:
            print(f"fanatics-roster: seeded {n_manual} manual shops")

    # 2. roster from DB
    shops = db.get_fanatics_roster(conn, 500)
    if not shops:
        print("fanatics-roster: roster is empty — add shop names to "
              "app/seed_fanatics_shops.txt.")
        return [], {}, [], {
            "n_shops": 0, "n_api_ok": 0,
            "n_kept": 0, "n_non_break": 0,
        }
    roster_ids = set()
    id_to_name = {}
    for s in shops:
        sid = (s["shop_id"] or "").strip()
        if sid:
            roster_ids.add(sid)
            id_to_name[sid] = s["name"] or sid

    # 3. fetch all platform streams, keep rostered + live ones
    try:
        streams = fanatics.fetch_all_streams()
        n_api_ok = 1
    except Exception as exc:  # noqa: BLE001 — total API failure -> keep slice
        print(f"fanatics-roster: streams API failed ({exc}) — keeping slice",
              file=sys.stderr)
        return [], {}, [], {
            "n_shops": len(roster_ids), "n_api_ok": 0,
            "n_kept": 0, "n_non_break": 0,
        }

    rows: list[dict] = []
    hit: dict[str, str] = {}
    n_dropped = 0
    n_off_roster = 0
    n_upcoming = 0
    n_live = 0
    for stream in streams:
        shop = stream.get("shop") or {}
        shop_id = shop.get("id") or ""
        if shop_id not in roster_ids:
            n_off_roster += 1
            continue
        status = stream.get("status")
        is_live = fanatics.is_live_status(status)
        is_upcoming = fanatics.is_upcoming_status(status)
        if not (is_live or is_upcoming):
            continue  # COMPLETE or unknown done status
        row = normalize_fanatics_stream(stream)
        if not (row and row.get("source_url")):
            continue
        # Upcoming streams without a real start time are placeholders — skip
        if is_upcoming and not row.get("starts_at"):
            continue
        if looks_like_real_break(row["title_raw"], row.get("format"),
                                 row.get("breaker")):
            rows.append(row)
            hit[shop_id] = shop.get("name") or shop_id
            # stamp last_hit_at for productive shops
            db.upsert_fanatics_shop(conn, shop_id,
                                   name=shop.get("name"))
            if is_live:
                n_live += 1
            else:
                n_upcoming += 1
        else:
            n_dropped += 1

    print(f"fanatics-roster: {len(streams)} platform streams scanned, "
          f"{n_live} live + {n_upcoming} upcoming breaks kept from rostered shops, "
          f"{n_dropped} non-break streams dropped, "
          f"{n_off_roster} off-roster streams skipped")
    return rows, hit, list(roster_ids), {
        "n_shops": len(roster_ids),
        "n_api_ok": n_api_ok,
        "n_kept": len(rows),
        "n_non_break": n_dropped,
    }
