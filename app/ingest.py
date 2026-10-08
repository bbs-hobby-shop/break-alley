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
import time
from pathlib import Path

import psycopg.errors

from . import config, db, ebay, fanatics_roster, twitch, twitch_roster, youtube, youtube_roster
from .normalizer import normalize_ebay_item


def _transact_with_retry(work, attempts=3):
    """Run work(conn) in a fresh transaction, retrying on Postgres deadlocks.

    The slice pollers (twitch/fanatics/youtube) wipe-and-rewrite the breaks
    table while the eBay poller is upserting/pruning it — two transactions
    occasionally lock rows in opposite order and Postgres kills one with
    DeadlockDetected (2026-10-08: twitch poller failed 3x in 7h). Deadlocks
    are transient by nature; a fresh attempt almost always succeeds.
    """
    last = None
    for i in range(attempts):
        try:
            with db.get_conn() as conn:
                return work(conn)
        except psycopg.errors.DeadlockDetected as exc:
            last = exc
            print(f"deadlock detected (attempt {i + 1}/{attempts}) — retrying",
                  file=sys.stderr)
            time.sleep(2 * (i + 1))
    raise last

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
    # One-time: parse existing break_time_text into starts_at for standard format
    # (Brian 2026-10-06: eBay times must match other platforms)
    try:
        from .ebay_video import parse_break_time
        from datetime import datetime, timezone, timedelta
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT id, break_time_text FROM breaks WHERE source='ebay' "
                "AND break_time_text IS NOT NULL AND starts_at IS NULL"
            ).fetchall()
            n_parsed = 0
            for r in rows:
                parsed = parse_break_time(r["break_time_text"])
                if parsed:
                    # Mark as live if started within last 4h (Brian 2026-10-06)
                    is_live = False
                    try:
                        dt = datetime.fromisoformat(parsed.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        now = datetime.now(timezone.utc)
                        if timedelta(hours=-4) < (dt - now) < timedelta(minutes=15):
                            is_live = True
                    except Exception:
                        pass
                    conn.execute(
                        "UPDATE breaks SET starts_at=%s, is_live=%s WHERE id=%s",
                        (parsed, is_live, r["id"]),
                    )
                    n_parsed += 1
            if n_parsed:
                print(f"ebay: parsed {n_parsed} break times into starts_at")
            # Update is_live for existing listings with starts_at (Brian 2026-10-06)
            rows2 = conn.execute(
                "SELECT id, starts_at FROM breaks WHERE source='ebay' "
                "AND starts_at IS NOT NULL AND NOT COALESCE(is_live, FALSE)"
            ).fetchall()
            n_live = 0
            n_checked = len(rows2)
            for r in rows2:
                try:
                    dt = r["starts_at"]
                    if isinstance(dt, str):
                        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    if timedelta(hours=-4) < (dt - now) < timedelta(minutes=15):
                        conn.execute(
                            "UPDATE breaks SET is_live=TRUE WHERE id=%s",
                            (r["id"],),
                        )
                        n_live += 1
                except Exception:
                    pass
            print(f"ebay: checked {n_checked} breaks with start times, marked {n_live} as live")
    except Exception as e:
        print(f"ebay time backfill failed: {e}", file=sys.stderr)
    items, fetch_stats = ebay.fetch_all_break_listings()
    print(f"fetched {len(items)} raw eBay listings")
    if not items and fetch_stats["throttled"] > 0:
        # eBay throttled us all run: no fresh data. Still prune ended/sold
        # (SQL-only, no API) so dead auctions don't pile up — but skip the
        # vanished rule or a long throttle would nuke the whole catalog.
        # Then fail visibly so Brian hears about it (Brian 2026-10-08).
        try:
            with db.get_conn() as prune_conn:
                pruned = db.prune_ended_ebay(prune_conn, skip_vanished=True)
            total = pruned["ended_auctions"] + pruned["sold_out"]
            if total:
                print(f"ebay: throttled-run prune removed {total} ended/sold")
        except Exception as e:
            print(f"ebay PRUNE FAILED: {e}", file=sys.stderr)
        print("ebay: throttled (429) — no fresh listings this run",
              file=sys.stderr)
        return 1
    n = 0
    n_video = 0
    n_err = 0
    with db.get_conn() as conn:
        for item in items:
            try:
                row = normalize_ebay_item(
                    item, affiliate_url=ebay.build_affiliate_url(item.get("itemWebUrl"))
                )
            except Exception as e:
                # One poison listing must not kill the whole 15-min run.
                n_err += 1
                print(f"ebay: skipping unparseable listing: {e}")
                continue
            # Auction-filtered search pass tags items _known_auction: mark them
            # even when buyingOptions is absent from the API response.
            if item.get("_known_auction"):
                row["is_auction"] = True
            if not row.get("source_url"):
                continue
            # Extract video info for new listings before they enter the app
            # (Brian 2026-10-06: all details must be there before listing goes live).
            # Check-once: listings are marked checked after the attempt so
            # video-less listings are never re-fetched (Browse API quota).
            checked_video = False
            if db.needs_video_info(conn, row["source_url"]):
                checked_video = True
                try:
                    from .ebay_video import extract_for_listing
                    video = extract_for_listing(row["source_url"])
                    row["video_url"] = video["video_url"]
                    row["video_platform"] = video["video_platform"]
                    row["break_time_text"] = video["break_time_text"]
                    row["video_links"] = video["video_links"]
                    # Standardize eBay break times to match other platforms (Brian 2026-10-06)
                    if video.get("break_starts_at") and not row.get("starts_at"):
                        row["starts_at"] = video["break_starts_at"]
                    # If break started recently (within 4h), mark as live (Brian 2026-10-06)
                    if row.get("starts_at"):
                        try:
                            from datetime import datetime, timezone, timedelta
                            starts_at = row["starts_at"]
                            if isinstance(starts_at, str):
                                starts_at = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
                            if starts_at.tzinfo is None:
                                starts_at = starts_at.replace(tzinfo=timezone.utc)
                            now = datetime.now(timezone.utc)
                            # Started within last 4 hours and not more than 15 min in future
                            if timedelta(hours=-4) < (starts_at - now) < timedelta(minutes=15):
                                row["is_live"] = True
                        except Exception:
                            pass
                    if video["video_links"]:
                        n_video += 1
                except Exception as e:
                    print(f"video extract failed for {row['source_url']}: {e}")
            try:
                db.upsert_break(conn, row)
            except Exception as e:
                n_err += 1
                print(f"ebay: upsert failed for {row.get('source_url')}: {e}")
                continue
            if checked_video:
                db.mark_video_checked(conn, row["source_url"])
            n += 1
    print(f"upserted {n} normalized breaks ({n_video} new with video info)"
          + (f", {n_err} errors" if n_err else ""))
    # Remove ended auctions, sold-out BIN, and vanished listings (Brian 2026-10-07).
    # Fresh connection: the upsert block's conn is closed by now, and the
    # long poll run can also kill idle connections (2026-10-08: prune failed
    # every run with "the connection is closed", leaving ~2000 ended auctions).
    prune_ok = True
    try:
        with db.get_conn() as prune_conn:
            pruned = db.prune_ended_ebay(prune_conn)
        total = sum(pruned.values())
        if total:
            print(f"ebay: pruned ended/sold listings {pruned}")
    except Exception as e:
        prune_ok = False
        print(f"ebay PRUNE FAILED: {e}", file=sys.stderr)
    if n == 0 and n_err > 0:
        # Every listing failed: something systemic is wrong -- mark the run
        # failed in the Render dashboard instead of exiting 0 on a stale site.
        return 1
    if not prune_ok:
        # Brian 2026-10-08: a dead prune must NEVER fail silently again.
        # Mark the run failed so the dashboard shows it red.
        return 1
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
    # Deadlock retry: the eBay poller may be writing breaks concurrently.
    def _write_youtube(conn):
        n = 0
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
        return n
    n = _transact_with_retry(_write_youtube)
    print(f"replaced youtube slice with {n} YouTube breaks")
    return 0


def run_youtube_roster() -> int:
    if not config.youtube_configured():
        print("YOUTUBE_API_KEY not set — nothing to do.", file=sys.stderr)
        return 1
    with db.get_conn() as conn:
        # Backfill the roster from channel_ids already stored on youtube
        # breaks (idempotent). New channels come from seed_channels.txt
        # (Brian's approved list) and the suggest-a-breaker flow.
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
        n_backfilled = db.backfill_break_countries(conn)
        if n_backfilled:
            print(f"youtube-roster: stamped country on {n_backfilled} older breaks")
        # Prune stale YouTube breaks (the broad search poller used to wipe the
        # slice; the roster is now the sole YouTube source, so it prunes here).
        # Keep live breaks and anything starting in the future; drop the rest.
        n_pruned = conn.execute(
            """DELETE FROM breaks
               WHERE source = 'youtube'
                 AND NOT COALESCE(is_live, FALSE)
                 AND (starts_at IS NULL OR starts_at < NOW())"""
        ).rowcount
        if n_pruned:
            print(f"youtube-roster: pruned {n_pruned} stale breaks")
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
    # instead of lingering as stale "live" rows. Deadlock retry: the eBay
    # poller may be writing breaks concurrently.
    def _write_twitch(conn):
        n = 0
        conn.execute("DELETE FROM breaks WHERE source = 'twitch'")
        for row in rows:
            if not row.get("source_url"):
                continue
            db.upsert_break(conn, row)
            n += 1
        return n
    n = _transact_with_retry(_write_twitch)
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
    # Twitch data is transient (currently-live streams), so each run wipes
    # and rewrites the twitch slice in ONE transaction: ended streams vanish
    # instead of lingering as stale "live" rows. Deadlock retry: the eBay
    # poller may be writing breaks concurrently (2026-10-08: 3 failures/7h).
    def _write_twitch_roster(conn):
        n = 0
        conn.execute("DELETE FROM breaks WHERE source = 'twitch'")
        for row in rows:
            if not row.get("source_url"):
                continue
            db.upsert_break(conn, row)
            n += 1
        for login, display_name in hit_logins.items():
            db.upsert_twitch_channel(conn, login, display_name=display_name)
        db.mark_twitch_checked(conn, checked)
        return n
    n = _transact_with_retry(_write_twitch_roster)
    print(f"twitch-roster: upserted {n} breaks from {len(checked)} channels")
    return 0


def run_fanatics() -> int:
    """One Fanatics Live roster poll run (Brian 2026-10-07)."""
    with db.get_conn() as conn:
        rows, hit_shop_ids, checked, stats = fanatics_roster.fetch_roster_breaks(conn)
        if stats["n_shops"] > 0 and stats["n_api_ok"] == 0:
            # Total API failure: nothing was checked, so keep the existing
            # slice instead of blanking the live site.
            print("fanatics-roster: streams API failed — keeping existing slice",
                  file=sys.stderr)
            return 1
    # Fanatics data is transient (currently-live streams), so each run
    # wipes and rewrites the fanatics slice in ONE transaction: ended
    # streams vanish instead of lingering as stale "live" rows.
    # Deadlock retry: the eBay poller may be writing breaks concurrently.
    def _write_fanatics(conn):
        n = 0
        conn.execute("DELETE FROM breaks WHERE source = 'fanatics'")
        for row in rows:
            if not row.get("source_url"):
                continue
            db.upsert_break(conn, row)
            n += 1
        db.mark_fanatics_checked(conn, checked)
        return n
    n = _transact_with_retry(_write_fanatics)
    print(f"fanatics-roster: upserted {n} breaks from {len(checked)} shops")
    return 0


# Card release evenings with extra fetch passes (Idea 2026-10-06, calendar 2026-10-07).
# Dates now live in the release_calendar table (editable via /admin) —
# the pass reads from there, no hardcoded dates.


def run_release_night() -> int:
    """Extra evening roster pass on card release nights.

    The cron fires daily at 01:00 UTC (8 PM CDT); this no-ops on
    non-release nights. On release nights it runs the standard YouTube
    roster poll — same real-buy-in and live-or-future filters, ~107
    YouTube API units, well within the daily quota.
    """
    from zoneinfo import ZoneInfo
    from datetime import datetime
    today = datetime.now(ZoneInfo("America/Chicago")).date()
    try:
        with db.get_conn() as conn:
            products = db.get_release_products(conn, today)
    except Exception as exc:
        print(f"release-night: database unavailable ({exc}) — skipping")
        return 0
    if not products:
        print(f"release-night: {today} is not a release night — skipping")
        return 0
    print(f"release-night: {today} — {', '.join(products)}")
    return run_youtube_roster()


def run_ebay_video_backfill() -> int:
    """One-time backfill: extract video info for eBay listings missing it.

    Fetches each eBay listing page and extracts the live video URL,
    platform, and break time from the description. Run manually via:
        python -m app.ingest --source ebay-video-backfill
    """
    from .ebay_video import extract_for_listing
    import time
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT source_url FROM breaks WHERE source = 'ebay' "
            "AND video_url IS NULL"
        ).fetchall()
    print(f"ebay-video-backfill: {len(rows)} listings need video info")
    n_ok = 0
    for i, r in enumerate(rows):
        url = r["source_url"]
        try:
            video = extract_for_listing(url)
            with db.get_conn() as conn:
                db.update_video_info(
                    conn, url, video["video_url"],
                    video["video_platform"], video["break_time_text"]
                )
            if video["video_url"]:
                n_ok += 1
                print(f"  [{i+1}/{len(rows)}] OK: {video['video_platform']} - {url[:60]}")
            else:
                print(f"  [{i+1}/{len(rows)}] no video found: {url[:60]}")
        except Exception as e:
            print(f"  [{i+1}/{len(rows)}] FAILED {url[:60]}: {e}")
        # Be polite to eBay
        time.sleep(1)
    print(f"ebay-video-backfill: done, {n_ok}/{len(rows)} with video info")
    return 0

def main() -> int:
    parser = argparse.ArgumentParser(description="Box break ingestion runner")
    parser.add_argument("--demo", action="store_true",
                        help="insert demo rows instead of calling APIs")
    parser.add_argument("--source",
                        choices=["ebay", "youtube", "youtube-roster",
                                 "twitch", "twitch-roster", "fanatics",
                                 "release-night",
                                 "ebay-video-backfill"],
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
        result = run_youtube()
    elif args.source == "youtube-roster":
        result = run_youtube_roster()
    elif args.source == "twitch":
        result = run_twitch()
    elif args.source == "twitch-roster":
        result = run_twitch_roster()
    elif args.source == "fanatics":
        result = run_fanatics()
    elif args.source == "release-night":
        result = run_release_night()
    elif args.source == "ebay-video-backfill":
        result = run_ebay_video_backfill()
    else:
        try:
            result = run_ebay()
        except Exception as exc:
            print(f"ebay poller failed (continuing to purge): {exc}", file=sys.stderr)
            result = 1
    # Purge junk that slipped through (casino, betting, giveaways, etc.)
    # Runs even if the eBay API is rate-limited — purge only needs the DB
    try:
        with db.get_conn() as conn:
            n_purged = db.purge_junk_breaks(conn)
            print(f"purged {n_purged} junk breaks")
    except Exception as exc:
        print(f"junk purge failed (continuing): {exc}", file=sys.stderr)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
