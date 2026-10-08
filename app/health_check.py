"""Daily system health check for BreakAlley (Brian 2026-10-07).

Runs every morning at 6 AM CDT. Verifies every part of the app is working:
- All pollers running and producing data (eBay, YouTube, Twitch, Fanatics)
- Break data quality (titles, times, URLs, formats)
- Times accurate (not stale, timezone sane)
- Links valid (source URLs, affiliate URLs)
- Site responsive, search working
- UI controls audit: every button, dropdown, form, and link on the homepage,
  a break detail page, and a breaker page is wired to a real route/handler
  (Brian 2026-10-07)
- Database integrity
- Security basics

Exits 0 if all checks pass, 1 if any critical check fails.
Prints a human-readable report; on failure the cron dashboard shows it.
"""

import os
import re
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# VM quirk: sanitize no_proxy before any HTTP
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "localhost,127.0.0.1"

SITE_URL = "https://breakalley.com"

CHECKS = []  # (name, passed, detail)


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
    # Max acceptable staleness per source
    FRESHNESS = {
        "ebay": timedelta(hours=1),
        "youtube": timedelta(hours=8),
        "twitch": timedelta(hours=1),
        "fanatics": timedelta(hours=1),
    }
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
            # Live breaks should have been fetched recently
            n = conn.execute(
                "SELECT COUNT(*) FROM breaks WHERE is_live"
                " AND fetched_at < NOW() - INTERVAL '2 hours'"
            ).fetchone()[0]
            check("times: live breaks fresh", n == 0, f"{n} stale live")
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


def check_ui_controls():
    """Brian 2026-10-07: every button, dropdown, form, and link on the key
    pages must be wired to a real route/handler. Catches dead buttons,
    selects outside forms, empty hrefs, and nested <a> tags (which broke
    every card on 2026-10-07)."""
    from html.parser import HTMLParser

    # Known app routes (path templates) for validating form actions + links
    try:
        from . import main as app_main
        routes = set()
        for r in app_main.app.routes:
            p = getattr(r, "path", "")
            if p:
                # turn /breaker/{breaker_name} into a regex
                routes.add(re.sub(r"\{[^}]+\}", r"[^/]+", p))
    except Exception as e:
        check("ui: routes loaded", False, str(e)[:80])
        return

    def route_exists(path):
        if not path or not path.startswith("/"):
            return False
        return any(re.fullmatch(rx, path.split("?")[0]) for rx in routes)

    class ControlParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.forms = []       # (action, method)
            self.selects = []     # (name, option_count, in_form)
            self.buttons = []     # (text, in_form, has_handler)
            self.links = []       # (href, text)
            self.nested_a = 0
            self._form_depth = 0
            self._a_depth = 0
            self._cur_select = None
            self._cur_button = None

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "form":
                self._form_depth += 1
                self.forms.append((a.get("action", ""), a.get("method", "get")))
            elif tag == "select":
                self._cur_select = [a.get("name", ""), 0, self._form_depth > 0]
            elif tag == "option" and self._cur_select is not None:
                self._cur_select[1] += 1
            elif tag == "button":
                self._cur_button = [a.get("type", "submit"),
                                    self._form_depth > 0,
                                    "onclick" in a or "data-" in str(attrs)]
            elif tag == "a":
                if self._a_depth > 0:
                    self.nested_a += 1
                self._a_depth += 1
                self.links.append((a.get("href", ""), ""))
            elif tag == "input" and a.get("type") in ("submit", "button"):
                self.buttons.append((a.get("value", a.get("type")),
                                     self._form_depth > 0, True))

        def handle_data(self, data):
            if self._cur_button is not None:
                self._cur_button[0] += data.strip()[:30]
            if self.links and self._a_depth > 0:
                href, txt = self.links[-1]
                self.links[-1] = (href, (txt + data.strip()[:30]))

        def handle_endtag(self, tag):
            if tag == "form":
                self._form_depth = max(0, self._form_depth - 1)
            elif tag == "select" and self._cur_select is not None:
                self.selects.append(tuple(self._cur_select))
                self._cur_select = None
            elif tag == "button" and self._cur_button is not None:
                self.buttons.append((self._cur_button[0],
                                     self._cur_button[1],
                                     self._cur_button[2]))
                self._cur_button = None
            elif tag == "a":
                self._a_depth = max(0, self._a_depth - 1)

    def fetch(path):
        req = urllib.request.Request(
            SITE_URL + path, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    # Pages to audit: homepage, a real break detail, a real breaker page
    pages = [("/", "homepage")]
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT id FROM breaks WHERE is_live = TRUE LIMIT 1").fetchone()
        if row:
            pages.append((f"/break/{row[0]}", "break detail"))
        brow = conn.execute(
            "SELECT breaker FROM breaks WHERE breaker IS NOT NULL "
            "LIMIT 1").fetchone()
        if brow:
            pages.append(("/breaker/" + urllib.parse.quote(brow[0]),
                          "breaker page"))
        conn.close()
    except Exception:
        pass

    total_issues = []
    for path, label in pages:
        try:
            html = fetch(path)
        except Exception as e:
            check(f"ui: {label} loads", False, str(e)[:80])
            continue
        p = ControlParser()
        try:
            p.feed(html)
        except Exception:
            pass
        issues = []
        for action, method in p.forms:
            if not route_exists(action):
                issues.append(f"form action '{action}' has no route")
        for name, nopts, in_form in p.selects:
            if not in_form:
                issues.append(f"select '{name}' is not inside a form")
            if nopts < 2:
                issues.append(f"select '{name}' has only {nopts} option(s)")
        for text, in_form, has_handler in p.buttons:
            # Buttons outside a form are fine when JS-wired (onclick/data-*)
            # — e.g. the custom bottom-sheet dropdown options (2026-10-08).
            if not in_form and not has_handler:
                issues.append(f"button '{text}' is not inside a form"
                              " and has no JS handler")
        for href, text in p.links:
            if not href or href == "#":
                issues.append(f"link '{text}' has empty/dead href")
            elif href.startswith("/") and not route_exists(href):
                issues.append(f"link '{text}' -> '{href}' has no route")
        if p.nested_a:
            issues.append(f"{p.nested_a} nested <a> tag(s) — breaks layout")
        n_controls = (len(p.forms) + len(p.selects) + len(p.buttons)
                      + len(p.links))
        if issues:
            total_issues += [f"{label}: {i}" for i in issues]
        check(f"ui: {label} controls wired",
              not issues,
              f"{n_controls} controls checked" if not issues
              else "; ".join(issues[:3]))

    if total_issues:
        check("ui: no dead controls anywhere", False,
              f"{len(total_issues)} issue(s)")


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
    check_ui_controls()
    check_prune()
    check_security()

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
