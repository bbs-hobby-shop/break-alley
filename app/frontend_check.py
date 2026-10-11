"""Daily front-end audit for BreakAlley (Brian 2026-10-11).

"Front end audit" (renamed from "content audit"): the interface check. Runs at
10 AM CDT as part of the front-end audit cron, BEFORE the platform-coverage
analysis. HTTP-only (no database) so it runs from anywhere.

Checks:
- Every public page loads (HTTP 200), no 500s
- Login-gated pages redirect to sign-in instead of crashing
- Footer present on every page (site-maturity requirement, Brian 2026-10-10)
- Feedback is a link to /feedback, not the old inline form (Brian 2026-10-11)
- UI controls: every button, dropdown, form, and link on the key pages is
  wired to a real route/handler (moved here from the backend audit 2026-10-11;
  originally Brian 2026-10-07). Catches dead buttons, selects outside forms,
  empty hrefs, and nested <a> tags (which broke every card on 2026-10-07).

Routes are validated against the app's live /openapi.json, so the check
never drifts when routes change. Exits 0 if all pass, 1 on any failure.
"""

import os
import re
import sys
import json
import urllib.request
import urllib.parse
from html.parser import HTMLParser

# VM quirk: sanitize no_proxy before any HTTP
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "localhost,127.0.0.1"

SITE_URL = "https://breakalley.com"

CHECKS = []  # (name, passed, detail)


def check(name, passed, detail=""):
    CHECKS.append((name, bool(passed), str(detail)))
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))


def fetch(path):
    req = urllib.request.Request(
        SITE_URL + path, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.status, resp.read().decode("utf-8", errors="ignore")


def load_routes():
    """Live route templates from /openapi.json, as regexes."""
    try:
        status, body = fetch("/openapi.json")
        data = json.loads(body)
        routes = set()
        for p in data.get("paths", {}):
            routes.add(re.sub(r"\{[^}]+\}", r"[^/]+", p))
        return routes
    except Exception as e:
        check("frontend: openapi.json reachable", False, str(e)[:80])
        return set()


def route_exists(routes, path):
    if not path or not path.startswith("/"):
        return False
    return any(re.fullmatch(rx, path.split("?")[0]) for rx in routes)


# Pages every visitor can open — must all return 200.
PUBLIC_PAGES = [
    ("/", "homepage"),
    ("/results", "results"),
    ("/guide", "guide"),
    ("/pro", "pro page"),
    ("/pro/success", "pro success"),
    ("/feedback", "feedback page"),
    ("/about", "about"),
    ("/privacy", "privacy"),
    ("/disclosure", "affiliate disclosure"),
    ("/submit-show", "submit show"),
    ("/login", "login"),
    ("/signup", "signup"),
]

# Login-gated pages — must redirect to sign-in (302/303) or render login (200),
# never 500.
GATED_PAGES = [
    ("/account", "account"),
    ("/my-breaks", "my breaks"),
]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def check_pages():
    """Every public page loads; gated pages redirect cleanly."""
    for path, label in PUBLIC_PAGES:
        try:
            status, _ = fetch(path)
            check(f"page:{label} loads", status == 200, f"HTTP {status}")
        except urllib.error.HTTPError as e:
            check(f"page:{label} loads", False, f"HTTP {e.code}")
        except Exception as e:
            check(f"page:{label} loads", False, str(e)[:80])
    for path, label in GATED_PAGES:
        try:
            # Don't follow redirects: we want to see the redirect itself.
            req = urllib.request.Request(
                SITE_URL + path, headers={"User-Agent": "Mozilla/5.0"})
            opener = urllib.request.build_opener(NoRedirect())
            try:
                resp = opener.open(req, timeout=20)
                status = resp.status
            except urllib.error.HTTPError as e:
                status = e.code
            ok = status in (200, 301, 302, 303, 307)
            check(f"page:{label} gated cleanly", ok, f"HTTP {status}")
        except Exception as e:
            check(f"page:{label} gated cleanly", False, str(e)[:80])


def check_chrome(html, label):
    """Footer + feedback-link presence on a page (Brian 2026-10-10/11)."""
    has_footer = 'class="site-footer"' in html
    check(f"chrome:{label} footer present", has_footer)
    # The standalone /feedback page has the real form, and the legal/info
    # pages (about/privacy/disclosure) never had a feedback section — every
    # other page should link to /feedback, not embed the old inline form.
    if label in ("feedback page", "about", "privacy",
                 "affiliate disclosure"):
        return
    has_link = "/feedback?next=" in html
    has_old_form = 'id="feedback" class="fb-wrap"' in html
    check(f"chrome:{label} feedback link (not inline form)",
          has_link and not has_old_form,
          "link ok" if (has_link and not has_old_form)
          else ("missing link" if not has_link else "old inline form still present"))


class ControlParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.selects = []
        self.buttons = []
        self.links = []
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


def check_ui_controls(routes):
    """Every control on the key pages is wired to a real route/handler."""
    if not routes:
        return  # already flagged by load_routes
    pages = [("/", "homepage")]
    # Pull a real break-detail and breaker URL from the results page.
    try:
        _, html = fetch("/results")
        m = re.search(r'href="(/break/\d+)"', html)
        if m:
            pages.append((m.group(1), "break detail"))
        m = re.search(r'href="(/breaker/[^"]+)"', html)
        if m:
            pages.append((urllib.parse.unquote(m.group(1)), "breaker page"))
    except Exception:
        pass
    # Release detail pages (/release/{id}) are linked from the homepage's
    # upcoming-releases section — no /release index exists.
    try:
        _, home_html = fetch("/")
        m = re.search(r'href="(/release/\d+)"', home_html)
        if m:
            pages.append((m.group(1), "release detail"))
    except Exception:
        pass

    total_issues = []
    for path, label in pages:
        try:
            _, html = fetch(path)
        except Exception as e:
            check(f"ui:{label} controls checked", False, str(e)[:80])
            continue
        p = ControlParser()
        try:
            p.feed(html)
        except Exception:
            pass
        issues = []
        for action, _ in p.forms:
            if not route_exists(routes, action):
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
            elif href.startswith("/") and not route_exists(routes, href):
                issues.append(f"link '{text}' -> '{href}' has no route")
        if p.nested_a:
            issues.append(f"{p.nested_a} nested <a> tag(s) — breaks layout")
        n_controls = (len(p.forms) + len(p.selects) + len(p.buttons)
                      + len(p.links))
        if issues:
            total_issues += [f"{label}: {i}" for i in issues]
        check(f"ui:{label} controls wired", not issues,
              f"{n_controls} controls checked" if not issues
              else "; ".join(issues[:3]))
    if total_issues:
        check("ui:no dead controls anywhere", False,
              f"{len(total_issues)} issue(s)")


def main():
    print("BreakAlley front-end audit — interface checks")
    print("=" * 60)
    # BRIAN'S RULE (2026-10-07): sequential, never parallel — don't hammer
    # the site with concurrent checks.
    routes = load_routes()
    check_pages()
    # Chrome checks need page HTML — re-fetch the public pages.
    for path, label in PUBLIC_PAGES:
        try:
            _, html = fetch(path)
            check_chrome(html, label)
        except Exception as e:
            check(f"chrome:{label} fetched", False, str(e)[:80])
    check_ui_controls(routes)
    print("=" * 60)
    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("\nFAILED:")
        for name, _, detail in failed:
            print(f"  x {name}" + (f" — {detail}" if detail else ""))
        return 1
    print("\nFront end clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
