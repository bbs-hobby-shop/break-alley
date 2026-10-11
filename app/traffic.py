"""First-party site traffic tracking (Brian 2026-10-11).

Anonymous pageview logging — no third-party trackers, no IPs, no PII.
visitor_id is a random UUID stored in a first-party cookie; referrer is
host-only. Everything here is best-effort: tracking must NEVER break or
slow the user-facing request.
"""

import time
import uuid
from urllib.parse import urlparse

from . import db

VISITOR_COOKIE = "ba_vid"
SESSION_COOKIE = "ba_sid"
SESSION_TIMEOUT = 1800  # 30 min of inactivity starts a new session

# Paths that are never tracked (admin, assets, API, outbound redirects).
_SKIP_PREFIXES = ("/static", "/admin", "/api", "/go", "/.well-known")

# Bot / crawler / monitoring user-agents that must not count as visitors.
# Brian 2026-10-11: site has no marketing, so real human traffic is ~zero;
# be aggressive — cookie-less bots were inflating "visitors" 100x.
_BOT_HINTS = (
    "bot", "crawler", "spider", "crawl", "slurp", "mediapartners",
    "baidu", "yandex", "sogou", "exabot", "facebot", "ia_archiver",
    "ahrefs", "semrush", "mj12", "dotbot", "petal", "bytespider",
    "python-requests", "httpx", "curl", "wget", "go-http-client",
    "uptimerobot", "pingdom", "datadog", "newrelic",
    "headless", "phantom", "selenium", "puppeteer", "playwright",
    "scrapy", "okhttp", "httpclient", "java/", "libwww-perl",
    "shodan", "censys", "zgrab", "masscan", "nmap",
)


def is_bot(user_agent: str) -> bool:
    ua = (user_agent or "").lower()
    # Empty user-agent: no real browser does this — it's a script/scanner.
    if not ua.strip():
        return True
    return any(h in ua for h in _BOT_HINTS)


def device_type(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if "ipad" in ua or "tablet" in ua or ("android" in ua and "mobile" not in ua and "tablet" in ua):
        return "tablet"
    if "iphone" in ua or "ipod" in ua or "android" in ua or "mobile" in ua:
        return "mobile"
    return "desktop"


def should_track(method: str, path: str, user_agent: str) -> bool:
    """True for real human page views only."""
    if method != "GET":
        return False
    if path.startswith(_SKIP_PREFIXES):
        return False
    if is_bot(user_agent):
        return False
    return True


def external_referrer(request) -> str | None:
    """Host-only external referrer, or None for direct/internal traffic."""
    ref = request.headers.get("referer") or request.headers.get("referrer")
    if not ref:
        return None
    try:
        host = (urlparse(ref).hostname or "").lower()
    except Exception:
        return None
    if not host or host == (request.url.hostname or "").lower():
        return None
    # Strip common www. prefix so google.com and www.google.com group together.
    return host[4:] if host.startswith("www.") else host


def resolve_ids(request) -> tuple[str, str, bool, bool]:
    """Return (visitor_id, session_id, new_visitor, new_session).

    Visitor id persists ~1 year. Session rotates after 30 min of inactivity;
    the session cookie carries its own last-seen timestamp so no DB lookup
    is needed to decide.
    """
    now = int(time.time())
    new_visitor = False
    new_session = False

    visitor_id = request.cookies.get(VISITOR_COOKIE)
    if not visitor_id:
        visitor_id = uuid.uuid4().hex
        new_visitor = True

    raw_sid = request.cookies.get(SESSION_COOKIE)
    session_id, last_seen = None, 0
    if raw_sid and ":" in raw_sid:
        try:
            session_id, last_seen = raw_sid.rsplit(":", 1)
            last_seen = int(last_seen)
        except (ValueError, TypeError):
            session_id, last_seen = None, 0
    if not session_id or now - last_seen > SESSION_TIMEOUT:
        session_id = uuid.uuid4().hex
        new_session = True

    return visitor_id, session_id, new_visitor, new_session


def log_pageview(request, response, user_id: int | None = None) -> None:
    """Log one pageview and set visitor/session cookies. Never raises."""
    try:
        path = request.url.path or "/"
        ua = request.headers.get("user-agent", "")
        if not should_track(request.method, path, ua):
            return
        visitor_id, session_id, new_visitor, new_session = resolve_ids(request)
        referrer = external_referrer(request)
        device = device_type(ua)
        with db.get_conn() as conn:
            conn.execute(
                """INSERT INTO page_views
                   (visitor_id, session_id, user_id, path, referrer, device)
                   VALUES (%(v)s, %(s)s, %(u)s, %(p)s, %(r)s, %(d)s)""",
                {"v": visitor_id, "s": session_id, "u": user_id,
                 "p": path[:200], "r": (referrer or "")[:120] or None,
                 "d": device},
            )
        # Refresh cookies on the way out (session cookie carries last-seen).
        if new_visitor:
            response.set_cookie(VISITOR_COOKIE, visitor_id, max_age=365 * 86400,
                                httponly=True, samesite="lax")
        response.set_cookie(SESSION_COOKIE, f"{session_id}:{int(time.time())}",
                            httponly=True, samesite="lax")
    except Exception:
        pass
