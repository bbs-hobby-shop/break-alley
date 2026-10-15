"""DB-only eBay cleanup (Brian 2026-10-09).

Removes ended auctions and sold-out Buy It Now listings using only our own
database — zero eBay API calls. Runs as its own Render cron
(break-alley-prune-ebay), so cleanup keeps working during 429 throttles and
never depends on the eBay poller running at all.

The "vanished" rule (not seen in feed for 24h) is only applied when the
eBay feed data is fresh — during a throttle, fetched_at goes stale for
everything and the rule would nuke the whole catalog.
"""

import sys
from datetime import datetime, timezone

from . import db


def feed_is_fresh(conn, max_age_hours: int = 2) -> bool:
    """Has the eBay poller successfully refreshed listings recently?

    Default 2h (a few poll intervals): during a 429 throttle, MAX(fetched_at)
    can look "fresh" for up to 24h after the last success while the feed is
    actually dead — and the vanished rule would then nuke live listings.
    A tight window keeps the vanished rule honest.
    """
    row = conn.execute(
        "SELECT MAX(fetched_at) AS m FROM breaks WHERE source = 'ebay'"
    ).fetchone()
    if not row or not row["m"]:
        return False
    latest = row["m"]
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - latest).total_seconds() < max_age_hours * 3600


def main() -> int:
    try:
        with db.get_conn() as conn:
            db.ensure_schema(conn)
    except Exception as exc:
        print(f"schema ensure failed (continuing): {exc}", file=sys.stderr)
    try:
        with db.get_conn() as conn:
            skip_vanished = not feed_is_fresh(conn)
            pruned = db.prune_ended_ebay(conn, skip_vanished=skip_vanished)
            # Demote stale LIVE flags (Brian 2026-10-14: "live only" search
            # showed 1,158 eBay listings that were not streaming — the flag
            # was granted by a clock heuristic and never revoked, and 429
            # throttling meant rows were never re-polled to clear it).
            # eBay: live is only credible inside the same window the
            # granters use — started within the last 2h and not more than
            # 15 min in the future — AND the listing must carry a stream
            # destination (video platform or extracted video link). A
            # clock-only live claim is not live: that is what kept 279
            # future-break auctions flagged after the first demote —
            # their time-of-day stamps happened to fall in tonight's
            # window even though the breaks are days away.
            n_ebay_demoted = conn.execute(
                """UPDATE breaks SET is_live = FALSE
                   WHERE source = 'ebay' AND COALESCE(is_live, FALSE)
                     AND (starts_at IS NULL
                          OR starts_at < NOW() - INTERVAL '2 hours'
                          OR starts_at > NOW() + INTERVAL '15 minutes'
                          OR (video_platform IS NULL
                              AND (video_links IS NULL
                                   OR video_links::text IN ('[]', 'null'))))"""
            ).rowcount
            # YouTube backstop: the roster poller demotes ended streams on
            # every run; if polls have been down for 4h+, no live flag is
            # trustworthy anymore (fetched_at = last time a poll saw it).
            n_yt_demoted = conn.execute(
                """UPDATE breaks SET is_live = FALSE
                   WHERE source = 'youtube' AND COALESCE(is_live, FALSE)
                     AND fetched_at < NOW() - INTERVAL '4 hours'"""
            ).rowcount
    except Exception as exc:
        # Brian 2026-10-08: a dead prune must NEVER fail silently.
        print(f"PRUNE FAILED: {exc}", file=sys.stderr)
        return 1
    total = sum(pruned.values())
    print(f"prune: removed {total} dead eBay listings "
          f"(ended={pruned['ended_auctions']}, sold={pruned['sold_out']}, "
          f"vanished={pruned['vanished']}, skip_vanished={skip_vanished}); "
          f"demoted stale-live: ebay={n_ebay_demoted}, youtube={n_yt_demoted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
