"""Daily backend audit for BreakAlley (Brian 2026-10-07; renamed 2026-10-11).

Runs every morning at 6 AM CDT. Verifies every part of the app's plumbing:
- All pollers running and producing data (eBay, YouTube, Twitch, Fanatics)
- Break data quality (titles, times, URLs, formats)
- Times accurate (not stale, timezone sane)
- Links valid (source URLs, affiliate URLs)
- Site responsive
- Environment: required secrets configured
- Stripe billing: keys authenticate, Pro price live
- Push notification pipeline tables
- Database size sane, ended listings pruned
- Security basics

The interface checks (UI controls, page loads, footer, feedback link) moved to
the front-end audit (app/frontend_check.py) on 2026-10-11.

Exits 0 if all checks pass, 1 if any critical check fails.
Prints a human-readable report; on failure the cron dashboard shows it.
"""

import os
import re
import sys
import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# VM quirk: sanitize no_proxy before any HTTP
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "localhost,127.0.0.1"

SITE_URL = "https://breakalley.com"

CHECKS = []  # (name, passed, detail)

# Max acceptable slice staleness per source — shared by check_pollers and
# check_times so the two checks can never drift. (2026-10-11: the blanket
# 2h "live breaks fresh" bar false-alarmed every morning on YouTube, whose
# roster poll runs on a longer cadence than the other platforms.)
POLLER_FRESHNESS = {
    # ebay freshness 1h at the normal 30-min cadence. (Was TEMPORARILY relaxed during the 2026-10-09→13 eBay 429 throttle: 2h at hourly, 4h at 3-hourly; reverted 2026-10-13 when the throttle cleared.)
    "ebay": timedelta(hours=1),
    "youtube": timedelta(hours=8),
    "twitch": timedelta(hours=1),
    "fanatics": timedelta(hours=1),
}


def check(name, passed, detail=""):
    CHECKS.append((name, bool(passed), str(detail)))
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))


def get_db():
    # NOTE: db.get_conn() returns DICT rows, but this module was written
    # with tuple indexing (row[0]). Use a plain tuple-row connection here.
    # (2026-10-08: dict-row KeyError(0) made every DB check fail with "0".)
    import psycopg
    from . import config
    return psycopg.connect(config.DATABASE_URL)


def check_pollers():
    """Each poller slice should have fresh data (updated in last N hours)."""
    # Max acceptable staleness per source (shared module constant)
    FRESHNESS = POLLER_FRESHNESS
    try:
        with get_db() as conn:
            for source, max_age in FRESHNESS.items():
                row = conn.execute(
                    "SELECT COUNT(*), MAX(fetched_at) FROM breaks WHERE source = %s",
                    (source,),
                ).fetchone()
                count, last = row[0], row[1]
                if count == 0:
                    check(f"poller:{source} has data", False, "0 breaks in slice")
                    continue
                age = datetime.now(timezone.utc) - last
                fresh = age < max_age
                check(f"poller:{source} fresh", fresh,
                      f"{count} breaks, last fetch {age.total_seconds()/60:.0f} min ago")
    except Exception as e:
        check("pollers: database reachable", False, str(e)[:100])


def check_roster_tables():
    """Roster tables exist and have active entries."""
    try:
        with get_db() as conn:
            for table, label in [
                ("youtube_channels", "YouTube"),
                ("twitch_channels", "Twitch"),
                ("fanatics_shops", "Fanatics"),
            ]:
                try:
                    n = conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE active"
                    ).fetchone()[0]
                    check(f"roster:{label} populated", n > 0, f"{n} active")
                except Exception:
                    check(f"roster:{label} table exists", False, "table missing")
            # eBay roster is a seed file, not a table
            from pathlib import Path
            seed = Path(__file__).with_name("seed_ebay_sellers.txt")
            n = sum(1 for l in seed.read_text().splitlines()
                    if l.strip() and not l.strip().startswith("#"))
            check("roster:eBay seed file", n > 0, f"{n} sellers")
    except Exception as e:
        check("rosters: database reachable", False, str(e)[:100])


def check_data_quality():
    """Breaks have valid titles, URLs, formats, sports."""
    try:
        with get_db() as conn:
            # No empty titles
            n = conn.execute(
                "SELECT COUNT(*) FROM breaks WHERE title_raw IS NULL OR title_raw = ''"
            ).fetchone()[0]
            check("data: titles present", n == 0, f"{n} empty titles")

            # Valid URLs
            n = conn.execute(
                "SELECT COUNT(*) FROM breaks WHERE source_url IS NULL OR source_url = ''"
                "OR source_url NOT LIKE 'http%'"
            ).fetchone()[0]
            check("data: source URLs valid", n == 0, f"{n} bad URLs")

            # Format distribution sane (not everything unknown)
            total = conn.execute("SELECT COUNT(*) FROM breaks").fetchone()[0]
            unknown = conn.execute(
                "SELECT COUNT(*) FROM breaks WHERE format = 'unknown' OR format IS NULL"
            ).fetchone()[0]
            pct = (unknown / total * 100) if total else 0
            check("data: formats detected", pct < 50,
                  f"{pct:.0f}% unknown of {total}")

            # No far-future starts_at (placeholder dates like 3000-01-01)
            n = conn.execute(
                "SELECT COUNT(*) FROM breaks WHERE starts_at > NOW() + INTERVAL '1 year'"
            ).fetchone()[0]
            check("data: no placeholder dates", n == 0, f"{n} far-future")

            # Upcoming streams shouldn't be stale (started >2h ago but still marked upcoming)
            n = conn.execute(
                "SELECT COUNT(*) FROM breaks WHERE NOT is_live AND starts_at IS NOT NULL"
                " AND starts_at < NOW() - INTERVAL '2 hours'"
            ).fetchone()[0]
            check("data: no stale upcoming", n == 0, f"{n} stale upcoming")
    except Exception as e:
        check("data quality: database reachable", False, str(e)[:100])


def check_times():
    """Spot-check that break times are sane and timezone-aware."""
    try:
        with get_db() as conn:
            # Live breaks should have been fetched recently — per source,
            # using the same staleness bars as check_pollers. A blanket bar
            # false-alarms on platforms with longer poll cadences
            # (e.g. YouTube roster polls every 3h).
            stale = 0
            detail_parts = []
            for source, max_age in POLLER_FRESHNESS.items():
                n = conn.execute(
                    "SELECT COUNT(*) FROM breaks WHERE source = %s AND is_live"
                    " AND fetched_at < NOW() - %s::interval",
                    (source, max_age),
                ).fetchone()[0]
                if n:
                    stale += n
                    detail_parts.append(f"{source}:{n}")
            check("times: live breaks fresh", stale == 0,
                  f"{stale} stale live ({', '.join(detail_parts)})" if stale
                  else "0 stale live")
    except Exception as e:
        check("times: database reachable", False, str(e)[:100])


def check_site():
    """Homepage loads, search works, platform filters present."""
    try:
        req = urllib.request.Request(
            SITE_URL + "/", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            ok = resp.status == 200
            check("site: homepage loads", ok, f"HTTP {resp.status}")
            for platform in ["ebay", "youtube", "twitch", "fanatics"]:
                found = f'value="{platform}"' in html or f"value='{platform}'" in html
                check(f"site: {platform} filter present", found)
            # Results count present
            check("site: results render",
                  "results found" in html.lower() or "result" in html.lower())
    except urllib.error.HTTPError as e:
        # Brian 2026-10-07: distinguish deploy-502s (transient, from a push)
        # from real outages. A 502 lasting >2 min likely means a stuck deploy
        # blocking the queue — check the Render deploys page, don't push again.
        if e.code == 502:
            detail = ("HTTP 502 — possible stuck Render deploy; check deploys "
                      "page for a blocked deploy before pushing again")
        else:
            detail = f"HTTP {e.code}"
        check("site: homepage loads", False, detail)
    except Exception as e:
        check("site: homepage loads", False, str(e)[:100])

    # Search API smoke test
    try:
        url = SITE_URL + "/?q=break&source=fanatics"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            check("site: fanatics search works", resp.status == 200)
    except Exception as e:
        check("site: fanatics search works", False, str(e)[:100])




def check_env():
    """Required secrets are configured (values never printed)."""
    required = ["DATABASE_URL", "SECRET_KEY", "ADMIN_KEY",
                "STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY",
                "STRIPE_WEBHOOK_SECRET", "STRIPE_PRO_PRICE_ID",
                "YOUTUBE_API_KEY"]
    missing = [k for k in required if not os.environ.get(k, "").strip()]
    check("env: required vars set", not missing,
          f"missing: {', '.join(missing)}" if missing else f"{len(required)} set")


def check_stripe():
    """Stripe live keys authenticate and the Pro price is retrievable.

    Brian 2026-10-11: an invalid STRIPE_SECRET_KEY broke billing silently —
    nothing failed until the portal was actually used. This catches it at
    6 AM instead of at a customer's tap."""
    import base64
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    price_id = os.environ.get("STRIPE_PRO_PRICE_ID", "").strip()
    if not key:
        check("stripe: secret key configured", False, "STRIPE_SECRET_KEY empty")
        return
    ok_prefix = key.startswith(("sk_live_", "rk_live_"))
    check("stripe: live secret key type", ok_prefix,
          "rk_live_/sk_live_" if ok_prefix else f"unexpected prefix {key[:8]}")
    if not ok_prefix or not price_id:
        return
    try:
        cred = base64.b64encode(f"{key}:".encode()).decode()
        req = urllib.request.Request(
            f"https://api.stripe.com/v1/prices/{urllib.parse.quote(price_id)}",
            headers={"Authorization": f"Basic {cred}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            active = data.get("active", False)
            check("stripe: Pro price live", resp.status == 200 and active,
                  f"{price_id} active={active}")
    except urllib.error.HTTPError as e:
        check("stripe: API authenticates", False, f"HTTP {e.code}")
    except Exception as e:
        check("stripe: API reachable", False, str(e)[:80])


def check_push_pipeline():
    """Push notification plumbing tables exist (Brian 2026-10-09: Pro alerts)."""
    try:
        with get_db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0]
            check("push: subscriptions table", True, f"{n} subscriptions")
    except Exception as e:
        check("push: subscriptions table", False, str(e)[:100])


def check_db_growth():
    """Database size sane — catch runaway growth before it hurts."""
    try:
        with get_db() as conn:
            size = conn.execute(
                "SELECT pg_database_size(current_database())").fetchone()[0]
            mb = size / 1024 / 1024
            breaks = conn.execute("SELECT COUNT(*) FROM breaks").fetchone()[0]
            check("db: size sane", mb < 900, f"{mb:.0f} MB, {breaks} breaks")
    except Exception as e:
        check("db: size check", False, str(e)[:100])


def check_prune():
    """Ended/sold listings are being removed (not accumulating)."""
    try:
        with get_db() as conn:
            # eBay: no auctions past end time should linger
            n = conn.execute(
                "SELECT COUNT(*) FROM breaks WHERE source = 'ebay'"
                " AND is_auction AND auction_ends_at < NOW() - INTERVAL '1 hour'"
            ).fetchone()[0]
            check("prune: ended auctions removed", n == 0, f"{n} lingering")
    except Exception as e:
        check("prune: database reachable", False, str(e)[:100])


def check_security():
    """Basic security hygiene."""
    # No secrets in repo
    from pathlib import Path
    repo = Path(__file__).parent.parent
    bad = []
    for f in ["app/db.py", "app/config.py", "app/main.py"]:
        p = repo / f
        if p.exists():
            text = p.read_text()
            for pattern in [r"sk-[a-zA-Z0-9]{20,}", r"AKIA[0-9A-Z]{16}"]:
                if re.search(pattern, text):
                    bad.append(f)
                    break
    check("security: no hardcoded secrets", not bad, ", ".join(bad) or "clean")

    # Site uses HTTPS
    check("security: https enforced", SITE_URL.startswith("https://"))


def check_audit_patterns():
    """Brian 2026-10-07: audit lessons applied to all platforms, forever.

    Every platform poller must:
    1. Capture BOTH live and upcoming/scheduled (not live-only)
    2. Paginate fully (no silent truncation)
    3. Search roster-direct, not keyword-only
    4. Filter without over-blocking legit titles
    5. ROSTER-ONLY: never query outside approved rosters. Growth comes from
       adding breakers to the roster, not from loose searches that pull in
       random listings misinterpreted as box breaks.
    """
    try:
        with get_db() as conn:
            # 1. Each platform should have BOTH live and upcoming breaks
            # (catches live-only regressions like the Twitch schedule gap)
            for source in ["youtube", "twitch", "fanatics", "ebay"]:
                live = conn.execute(
                    "SELECT COUNT(*) FROM breaks WHERE source = %s AND is_live",
                    (source,)).fetchone()[0]
                upcoming = conn.execute(
                    "SELECT COUNT(*) FROM breaks WHERE source = %s AND NOT is_live",
                    (source,)).fetchone()[0]
                # eBay listings are rarely "live" in the stream sense; skip the ratio check there
                if source == "ebay":
                    check(f"audit:{source} has listings", live + upcoming > 0,
                          f"{live + upcoming} total")
                else:
                    # The regression this guards is live-ONLY polling (the old
                    # Twitch schedule gap): the poller must capture scheduled/
                    # upcoming content. Live count is time-of-day dependent
                    # (0 live at 6 AM is normal), so only upcoming is required.
                    # (2026-10-08: requiring live>0 false-alarmed on quiet mornings.)
                    check(f"audit:{source} live+upcoming coverage", upcoming > 0,
                          f"{live} live, {upcoming} upcoming")

            # 2. No platform should be empty while its roster is populated
            # (catches silent truncation / total poller failure)
            for table, source, label in [
                ("youtube_channels", "youtube", "YouTube"),
                ("twitch_channels", "twitch", "Twitch"),
                ("fanatics_shops", "fanatics", "Fanatics"),
            ]:
                try:
                    roster_n = conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE active").fetchone()[0]
                    breaks_n = conn.execute(
                        "SELECT COUNT(*) FROM breaks WHERE source = %s",
                        (source,)).fetchone()[0]
                    # Roster populated but zero breaks = poller silently failing
                    ok = roster_n == 0 or breaks_n > 0
                    check(f"audit:{label} roster->breaks flowing", ok,
                          f"{roster_n} rostered, {breaks_n} breaks")
                except Exception:
                    pass  # table check already covered in check_roster_tables
    except Exception as e:
        check("audit: database reachable", False, str(e)[:100])


def check_regions():
    """Brian 2026-10-11: every seller on every platform gets an explicit region.

    No NULL countries — a breaker without a country is a gap to research,
    not something to silently default. Flags distinct breakers (by source)
    that have breaks with NULL country.
    """
    try:
        with get_db() as conn:
            # Non-YouTube: breaker names with NULL country
            for source in ["twitch", "ebay", "fanatics"]:
                rows = conn.execute(
                    "SELECT DISTINCT breaker FROM breaks "
                    "WHERE source = %s AND country IS NULL AND breaker IS NOT NULL",
                    (source,)).fetchall()
                names = sorted(r[0] for r in rows)
                check(f"regions:{source} all breakers tagged", not names,
                      f"{len(names)} untagged: {', '.join(names[:5])}" + ("..." if len(names) > 5 else "") if names else "all tagged")
            # YouTube: channels with NULL country
            rows = conn.execute(
                "SELECT COUNT(*) FROM youtube_channels WHERE country IS NULL").fetchone()
            n = rows[0]
            check("regions:youtube all channels tagged", n == 0,
                  f"{n} channels without country" if n else "all tagged")
    except Exception as e:
        check("regions: database reachable", False, str(e)[:100])


def main():
    print(f"BreakAlley daily health check — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")
    print("=" * 60)

    # BRIAN'S RULE (2026-10-07): run every check ONE AT A TIME, sequentially.
    # Never parallelize/thread these — hammering the DB and site with
    # concurrent checks risks overloading and crashing things.
    check_pollers()
    check_roster_tables()
    check_audit_patterns()
    check_data_quality()
    check_times()
    check_site()
    check_env()
    check_stripe()
    check_push_pipeline()
    check_db_growth()
    check_prune()
    check_security()
    check_regions()

    print("=" * 60)
    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("\nFAILED:")
        for name, _, detail in failed:
            print(f"  ✗ {name}" + (f" — {detail}" if detail else ""))
        return 1
    print("\nAll systems operational.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
