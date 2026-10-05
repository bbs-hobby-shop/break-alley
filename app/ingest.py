"""Ingestion runner: eBay / YouTube / Twitch -> normalize -> Postgres upsert.

Usage:
    python -m app.ingest                  # one eBay poll run (default)
    python -m app.ingest --source youtube  # one YouTube poll run
    python -m app.ingest --source twitch   # one Twitch poll run
    python -m app.ingest --demo            # insert 3 sample rows (no API calls, for UI demo)

Intended to run on a schedule (cron / Celery beat): eBay every ~15 min,
YouTube every ~6 hours (see app/youtube.py quota math),
Twitch every ~15-30 min (see app/twitch.py rate-limit notes).
"""
import argparse
import sys

from . import config, db, ebay, twitch, youtube
from .normalizer import normalize_ebay_item

DEMO_ROWS = [
    {
        "source": "ebay",
        "source_url": "https://www.ebay.com/itm/demo1",
        "breaker": "demo_breaks",
        "product_raw": "2024 Panini Prizm Football Hobby Box PYT #12",
        "product_normalized": "2024 Panini Prizm Football Hobby",
        "sport": "football",
        "format": "pyt",
        "price": "29.99",
        "currency": "USD",
        "starts_at": None,
        "is_live": False,
        "slots_total": None,
        "slots_remaining": 7,
        "thumbnail_url": None,
        "title_raw": "2024 Panini Prizm Football Hobby Box Break PYT #12 - Dallas Cowboys",
        "affiliate_url": "https://www.ebay.com/itm/demo1",
        "expires_at": None,
    },
    {
        "source": "ebay",
        "source_url": "https://www.ebay.com/itm/demo2",
        "breaker": "demo_breaks",
        "product_raw": "2024 Bowman Chrome Baseball Hobby",
        "product_normalized": "2024 Bowman Chrome Baseball Hobby",
        "sport": "baseball",
        "format": "random",
        "price": "14.99",
        "currency": "USD",
        "starts_at": None,
        "is_live": False,
        "slots_total": None,
        "slots_remaining": 12,
        "thumbnail_url": None,
        "title_raw": "2024 Bowman Chrome Baseball Hobby Box Break Random Team",
        "affiliate_url": "https://www.ebay.com/itm/demo2",
        "expires_at": None,
    },
    {
        "source": "youtube",
        "source_url": "https://www.youtube.com/watch?v=demo3",
        "breaker": "Demo Card Channel",
        "product_raw": "5-Box 2024 Mosaic Basketball Mixer",
        "product_normalized": "2024 Panini Mosaic Basketball Hobby",
        "sport": "basketball",
        "format": "division",
        "price": None,
        "currency": "USD",
        "starts_at": "2026-10-05T01:00:00+00:00",
        "is_live": True,
        "slots_total": None,
        "slots_remaining": None,
        "thumbnail_url": None,
        "title_raw": "LIVE 5-Box 2024 Mosaic Basketball Mixer Division Break",
        "affiliate_url": "https://www.youtube.com/watch?v=demo3",
        "expires_at": None,
    },
]


def run_demo() -> int:
    with db.get_conn() as conn:
        for row in DEMO_ROWS:
            db.upsert_break(conn, row)
    print(f"inserted {len(DEMO_ROWS)} demo rows")
    return 0


def run_ebay() -> int:
    if not config.ebay_configured():
        print("EBAY_APP_ID / EBAY_CERT_ID not set — nothing to do.", file=sys.stderr)
        return 1
    items = ebay.fetch_all_break_listings()
    print(f"fetched {len(items)} raw eBay listings")
    n = 0
    with db.get_conn() as conn:
        for item in items:
            row = normalize_ebay_item(
                item, affiliate_url=ebay.build_affiliate_url(item.get("itemWebUrl"))
            )
            if not row.get("source_url"):
                continue
            db.upsert_break(conn, row)
            n += 1
    print(f"upserted {n} normalized breaks")
    return 0


def run_youtube() -> int:
    if not config.youtube_configured():
        print("YOUTUBE_API_KEY not set — nothing to do.", file=sys.stderr)
        return 1
    rows = youtube.fetch_all_break_streams()
    print(f"fetched {len(rows)} normalized YouTube breaks")
    n = 0
    # YouTube data is transient (live/upcoming streams), so each run wipes
    # and rewrites the youtube slice in ONE transaction: stale streams vanish
    # instead of lingering forever, and a failed run rolls back cleanly.
    with db.get_conn() as conn:
        conn.execute("DELETE FROM breaks WHERE source = 'youtube'")
        for row in rows:
            if not row.get("source_url"):
                continue
            db.upsert_break(conn, row)
            n += 1
    print(f"replaced youtube slice with {n} YouTube breaks")
    return 0


def run_twitch() -> int:
    if not twitch.twitch_configured():
        print("TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET not set — nothing to do.",
              file=sys.stderr)
        return 1
    rows = twitch.fetch_all_break_streams()
    print(f"fetched {len(rows)} normalized Twitch breaks")
    n = 0
    # Twitch data is transient (currently-live streams), so each run wipes
    # and rewrites the twitch slice in ONE transaction: ended streams vanish
    # instead of lingering as stale "live" rows.
    with db.get_conn() as conn:
        conn.execute("DELETE FROM breaks WHERE source = 'twitch'")
        for row in rows:
            if not row.get("source_url"):
                continue
            db.upsert_break(conn, row)
            n += 1
    print(f"replaced twitch slice with {n} Twitch breaks")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Box break ingestion runner")
    parser.add_argument("--demo", action="store_true",
                        help="insert demo rows instead of calling APIs")
    parser.add_argument("--source", choices=["ebay", "youtube", "twitch"],
                        default="ebay",
                        help="which source to poll (default: ebay)")
    args = parser.parse_args()
    if args.demo:
        return run_demo()
    if args.source == "youtube":
        return run_youtube()
    if args.source == "twitch":
        return run_twitch()
    return run_ebay()


if __name__ == "__main__":
    raise SystemExit(main())
