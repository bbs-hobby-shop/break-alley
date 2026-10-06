"""Ingestion runner: eBay / YouTube / Twitch -> normalize -> Postgres upsert.

Usage:
    python -m app.ingest                          # one eBay poll run (default)
    python -m app.ingest --source youtube         # one YouTube search poll run
    python -m app.ingest --source youtube-roster  # one YouTube roster poll run
    python -m app.ingest --source twitch          # one Twitch search poll run
    python -m app.ingest --source twitch-roster   # one Twitch roster poll run
    python -m app.ingest --demo                   # insert 3 sample rows (no API calls, for UI demo)

Intended to run on a schedule (cron / Celery beat): eBay every ~15 min,
YouTube search every ~6 hours (see app/youtube.py quota math),
YouTube roster every ~6 hours offset from the search poll
(see app/youtube_roster.py quota math),
Twitch roster every ~20 min (see app/twitch_roster.py).
"""
import argparse
import sys
from pathlib import Path

from . import config, db, ebay, twitch, twitch_roster, youtube, youtube_roster
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
    rows, n_searches = youtube.fetch_all_break_streams()
    print(f"fetched {len(rows)} normalized YouTube breaks")
    if n_searches == 0:
        # Total API failure (quota exhausted, rate-limited, or outage): keep
        # the existing slice instead of blanking the live site. Non-zero exit
        # marks the cron run as failed in the Render dashboard.
        print("youtube: all searches failed — keeping existing slice",
              file=sys.stderr)
        return 1
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
            # Discovery hook: every kept break teaches the roster a channel
            # to watch cheaply (1 unit/check instead of 100/search).
            if row.get("channel_id"):
                db.upsert_youtube_channel(
                    conn, row["channel_id"], title=row.get("breaker"))
    print(f"replaced youtube slice with {n} YouTube breaks")
    return 0


def run_youtube_roster() -> int:
    if not config.youtube_configured():
        print("YOUTUBE_API_KEY not set — nothing to do.", file=sys.stderr)
        return 1
    with db.get_conn() as conn:
        # Backfill the roster from channel_ids already stored on youtube
        # breaks (idempotent). The search poller's discovery hook keeps it
        # growing from here.
        n_seeded = db.seed_youtube_channels_from_breaks(conn)
        if n_seeded:
            print(f"youtube-roster: seeded {n_seeded} channels from breaks table")
        # Hand-picked channels (Brian's list) from app/seed_channels.txt
        seed_path = Path(__file__).with_name("seed_channels.txt")
        n_manual = 0
        if seed_path.exists():
            lines = seed_path.read_text().splitlines()
            n_manual = db.seed_manual_channels(conn, lines)
            if n_manual:
                print(f"youtube-roster: seeded {n_manual} manual channels")
        rows, hit_channel_ids, checked, stats = youtube_roster.fetch_roster_breaks(conn)
        if stats["n_channels"] > 0 and stats["n_api_ok"] == 0:
            # Total API failure (quota exhausted, rate-limited, or outage):
            # nothing was checked, so there is nothing to write. Unlike the
            # search poller there is no slice to protect (roster runs never
            # wipe), but a non-zero exit still marks the cron run as failed.
            print("youtube-roster: all channel checks failed — keeping existing data",
                  file=sys.stderr)
            return 1
        n = 0
        for row in rows:
            if not row.get("source_url"):
                continue
            db.upsert_break(conn, row)
            n += 1
        for cid in hit_channel_ids:
            db.upsert_youtube_channel(conn, cid)
        db.mark_channels_checked(conn, checked)
    print(f"youtube-roster: upserted {n} breaks from {len(checked)} channels "
          f"(~{stats['units']} quota units)")
    return 0


def run_twitch() -> int:
    if not twitch.twitch_configured():
        print("TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET not set — nothing to do.",
              file=sys.stderr)
        return 1
    rows, n_searches = twitch.fetch_all_break_streams()
    print(f"fetched {len(rows)} normalized Twitch breaks")
    if n_searches == 0:
        # Total API failure (rate-limited or outage): keep the existing slice
        # instead of blanking the live site. Non-zero exit marks the cron run
        # as failed in the Render dashboard.
        print("twitch: all searches failed — keeping existing slice",
              file=sys.stderr)
        return 1
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


def run_twitch_roster() -> int:
    if not twitch.twitch_configured():
        print("TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET not set — nothing to do.",
              file=sys.stderr)
        return 1
    with db.get_conn() as conn:
        # Hand-picked logins (researched 2026-10-06) from
        # app/seed_twitch_channels.txt
        seed_path = Path(__file__).with_name("seed_twitch_channels.txt")
        n_manual = 0
        if seed_path.exists():
            lines = seed_path.read_text().splitlines()
            n_manual = db.seed_manual_twitch_channels(conn, lines)
            if n_manual:
                print(f"twitch-roster: seeded {n_manual} manual channels")
        rows, hit_logins, checked, stats = twitch_roster.fetch_roster_breaks(conn)
        if stats["n_channels"] > 0 and stats["n_api_ok"] == 0:
            # Total API failure (rate-limited or outage): nothing was checked,
            # so keep the existing slice instead of blanking the live site.
            # Non-zero exit marks the cron run as failed in the dashboard.
            print("twitch-roster: streams API failed — keeping existing slice",
                  file=sys.stderr)
            return 1
        n = 0
        # Twitch data is transient (currently-live streams), so each run wipes
        # and rewrites the twitch slice in ONE transaction: ended streams vanish
        # instead of lingering as stale "live" rows.
        conn.execute("DELETE FROM breaks WHERE source = 'twitch'")
        for row in rows:
            if not row.get("source_url"):
                continue
            db.upsert_break(conn, row)
            n += 1
        for login, display_name in hit_logins.items():
            db.upsert_twitch_channel(conn, login, display_name=display_name)
        db.mark_twitch_checked(conn, checked)
    print(f"twitch-roster: upserted {n} breaks from {len(checked)} channels")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Box break ingestion runner")
    parser.add_argument("--demo", action="store_true",
                        help="insert demo rows instead of calling APIs")
    parser.add_argument("--source",
                        choices=["ebay", "youtube", "youtube-roster",
                                 "twitch", "twitch-roster"],
                        default="ebay",
                        help="which source to poll (default: ebay)")
    args = parser.parse_args()
    if args.demo:
        return run_demo()
    # Self-healing schema: the cron pollers don't run schema.sql (only the
    # web service does on deploy), so each run ensures the check constraints
    # are current before inserting. Idempotent and cheap.
    try:
        with db.get_conn() as conn:
            db.ensure_schema(conn)
    except Exception as exc:
        print(f"schema ensure failed (continuing): {exc}", file=sys.stderr)
    if args.source == "youtube":
        return run_youtube()
    if args.source == "youtube-roster":
        return run_youtube_roster()
    if args.source == "twitch":
        return run_twitch()
    if args.source == "twitch-roster":
        return run_twitch_roster()
    return run_ebay()


if __name__ == "__main__":
    raise SystemExit(main())
