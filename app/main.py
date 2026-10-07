"""FastAPI web UI — thin search/schedule front end (server-rendered, SEO-friendly).

Routes:
  GET /              search page with filters
  GET /break/{id}    detail page with prominent outbound link
"""
from pathlib import Path

import contextlib
import io
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, config, db, youtube
from .ingest import run_ebay, run_twitch_roster, run_youtube, run_youtube_roster
from .normalizer import date_label, display_title, extract_break_number, FORMAT_LABELS

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Box Break Finder")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def _enrich(row: dict) -> dict:
    """Add display-ready fields: cleaned title, break number, date label."""
    title = row.get("title_raw") or ""
    row["display_title"] = display_title(title)
    row["break_no"] = extract_break_number(title)
    row["date_label"] = date_label(row.get("starts_at"))
    return row


@app.get("/", response_class=HTMLResponse)
def search(
    request: Request,
    q: str | None = Query(default=None),
    format: str | None = Query(default=None),
    max_price: str | None = Query(default=None),
    source: str | None = Query(default=None),
    live: bool = Query(default=False),
    region: str | None = Query(default=None),
    suggested: str | None = Query(default=None),
    sort: str | None = Query(default=None),
):
    format = format or None
    source = source or None
    region = region if region in ("us", "intl") else None
    sort = sort if sort in ("soonest", "price_low", "price_high", "newest", "live") else None
    # max_price arrives as "" when the query string carries empty params
    # (e.g. after favoriting) — treat blank/invalid as "no cap", never 422.
    try:
        max_price_val = float(max_price) if max_price and max_price.strip() else None
    except (ValueError, TypeError):
        max_price_val = None
    format = format or None
    source = source or None
    user = auth.get_current_user(request)
    favorites: set[str] = set()
    try:
        with db.get_conn() as conn:
            results = [
                _enrich(dict(r)) for r in db.search_breaks(
                    conn, q=q or None, format=format,
                    max_price=max_price_val, source=source,
                    live_only=True if live else None, region=region,
                    sort=sort,
                )
            ]
            if user:
                favorites = db.favorite_breakers(conn, user["id"])
            updated_ago = _ago(db.last_data_update(conn))
        error = None
    except Exception as exc:  # DB not up / not migrated yet
        results, error = [], f"Database unavailable: {exc}"
        updated_ago = "—"
    return templates.TemplateResponse(request, "search.html", {
        "results": results, "error": error,
        "q": q or "", "format": format or "",
        "max_price": max_price or "", "source": source or "", "live": live,
        "region": region or "", "sort": sort or "",
        "suggested": suggested or "",
        "user": user, "favorites": favorites,
        "refresh_running": _public_refresh_running(),
        "updated_ago": updated_ago,
        "formats": ["pyt", "random", "personal", "case_break", "box_break"],
        "format_labels": FORMAT_LABELS,
    })


@app.get("/break/{break_id}", response_class=HTMLResponse)
def detail(request: Request, break_id: int):
    try:
        with db.get_conn() as conn:
            row = db.get_break(conn, break_id)
            if row:
                row = _enrich(dict(row))
        error = None if row else "Break not found."
    except Exception as exc:
        row, error = None, f"Database unavailable: {exc}"
    return templates.TemplateResponse(request, "detail.html", {
        "b": row, "error": error,
        "format_labels": FORMAT_LABELS,
    })


# ---------------------------------------------------------------------------
# Breaker suggestions: public form -> review queue -> roster (on approval)
# ---------------------------------------------------------------------------

@app.post("/suggest-breaker")
def suggest_breaker(
    request: Request,
    channel: str = Form(default=""),
    note: str = Form(default=""),
    website: str = Form(default=""),  # honeypot: bots fill it, humans don't
):
    if website.strip():
        # Bot submission: pretend it worked, store nothing.
        return RedirectResponse("/?suggested=1", status_code=303)
    channel = (channel or "").strip()
    if not channel:
        return RedirectResponse("/?suggested=0", status_code=303)
    try:
        with db.get_conn() as conn:
            db.add_breaker_suggestion(conn, channel, note)
    except Exception:
        return RedirectResponse("/?suggested=0", status_code=303)
    return RedirectResponse("/?suggested=1", status_code=303)


def _admin_key_ok(key: str | None) -> bool:
    return bool(config.ADMIN_KEY) and key == config.ADMIN_KEY


@app.get("/admin/suggestions", response_class=HTMLResponse)
def admin_suggestions(
    request: Request,
    key: str | None = Query(default=None),
    refresh: str | None = Query(default=None),
    wait: int | None = Query(default=None),
):
    if not _admin_key_ok(key):
        return templates.TemplateResponse(request, "admin_suggestions.html", {
            "denied": True, "pending": [], "reviewed": [], "key": key or "",
        })
    try:
        with db.get_conn() as conn:
            pending = db.list_breaker_suggestions(conn, status="pending")
            reviewed = db.list_breaker_suggestions(conn)[:50]
            reviewed = [r for r in reviewed if r["status"] != "pending"]
            stats = db.user_stats(conn)
    except Exception as exc:
        return templates.TemplateResponse(request, "admin_suggestions.html", {
            "denied": False, "error": f"Database unavailable: {exc}",
            "pending": [], "reviewed": [], "key": key or "",
        })
    return templates.TemplateResponse(request, "admin_suggestions.html", {
        "denied": False, "pending": pending, "reviewed": reviewed,
        "stats": stats, "key": key or "",
        "refresh_msg": refresh, "refresh_wait": wait or 0,
        "refresh_state": _refresh_status(),
    })


@app.post("/admin/suggestions/{suggestion_id}/approve")
def admin_approve(
    request: Request, suggestion_id: int,
    key: str = Form(default=""),
    reviewer_note: str = Form(default=""),
):
    if not _admin_key_ok(key):
        return RedirectResponse("/admin/suggestions", status_code=303)
    try:
        with db.get_conn() as conn:
            rows = db.list_breaker_suggestions(conn)
            row = next((r for r in rows if r["id"] == suggestion_id), None)
            if not row or row["status"] != "pending":
                raise ValueError("Suggestion not found or already reviewed.")
            # Resolve to a real channel first (1 quota unit) — a bad handle
            # fails here, before anything touches the roster.
            channel_id, title = youtube.resolve_channel(row["input_text"])
            db.upsert_youtube_channel(conn, channel_id, title=title, source="manual")
            db.review_breaker_suggestion(
                conn, suggestion_id, True, channel_id=channel_id,
                reviewer_note=(reviewer_note or "").strip()[:500] or None,
            )
    except Exception as exc:
        # Stay on the page with the error; the suggestion stays pending so
        # it can be fixed up or rejected.
        return templates.TemplateResponse(request, "admin_suggestions.html", {
            "denied": False, "resolve_error": str(exc),
            "failed_id": suggestion_id, "key": key,
            "pending": _admin_lists(key)[0], "reviewed": _admin_lists(key)[1],
        })
    return RedirectResponse(f"/admin/suggestions?key={key}", status_code=303)


def _admin_lists(key: str) -> tuple[list, list]:
    try:
        with db.get_conn() as conn:
            pending = db.list_breaker_suggestions(conn, status="pending")
            reviewed = [r for r in db.list_breaker_suggestions(conn)[:50]
                        if r["status"] != "pending"]
        return pending, reviewed
    except Exception:
        return [], []


@app.post("/admin/suggestions/{suggestion_id}/reject")
def admin_reject(
    request: Request, suggestion_id: int,
    key: str = Form(default=""),
    reviewer_note: str = Form(default=""),
):
    if not _admin_key_ok(key):
        return RedirectResponse("/admin/suggestions", status_code=303)
    try:
        with db.get_conn() as conn:
            db.review_breaker_suggestion(
                conn, suggestion_id, False,
                reviewer_note=(reviewer_note or "").strip()[:500] or None,
            )
    except Exception:
        pass
    return RedirectResponse(f"/admin/suggestions?key={key}", status_code=303)


@app.post("/admin/breaks/{break_id}/delete")
def admin_delete_break(
    request: Request, break_id: int,
    key: str = Form(default=""),
):
    """Remove a junk listing (Brian 2026-10-06). Also blocks the seller's
    'At the Break' single-card pattern via the title filter."""
    if not _admin_key_ok(key):
        return RedirectResponse("/admin/suggestions", status_code=303)
    try:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM breaks WHERE id = %s", (break_id,))
    except Exception:
        pass
    return RedirectResponse(f"/admin/suggestions?key={key}", status_code=303)


# ---------------------------------------------------------------------------
# Public data refresh (customer-facing; cheap sources only)
# ---------------------------------------------------------------------------
# The full YouTube search (~1,600 quota units) stays on its schedule and the
# admin button. The public button runs only the cheap sources (roster ~52
# units, Twitch + eBay ~0), so even aggressive clicking can't burn the quota:
# a global cooldown makes extra clicks no-ops, and refresh results are
# shared — one user's refresh updates the data for everyone.

PUBLIC_REFRESH_COOLDOWN_SECS = 300  # 5 min, global across all visitors
PUBLIC_REFRESH_SOURCES = (
    ("youtube-roster", run_youtube_roster),
    ("twitch-roster", run_twitch_roster),
    ("ebay", run_ebay),
)

_public_refresh_lock = threading.Lock()
_public_refresh_state = {"running": False, "started_at": None, "finished_at": None}


def _run_public_refresh_job() -> None:
    try:
        for name, fn in PUBLIC_REFRESH_SOURCES:
            try:
                fn()
            except Exception as exc:  # one source failing must not kill the rest
                print(f"public refresh: {name} failed: {exc}")
    finally:
        with _public_refresh_lock:
            _public_refresh_state.update(running=False, finished_at=time.time())


def _public_refresh_running() -> bool:
    with _public_refresh_lock:
        return bool(_public_refresh_state["running"])


def _ago(dt) -> str:
    if not dt:
        return "never yet"
    s = (datetime.now(timezone.utc) - dt).total_seconds()
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} hr ago"
    return f"{int(s // 86400)}d ago"


@app.post("/refresh")
def public_refresh(
    q: str = Form(default=""),
    format: str = Form(default=""),
    source: str = Form(default=""),
    max_price: str = Form(default=""),
    live: str = Form(default=""),
):
    # Preserve the visitor's filters across the redirect.
    params = {}
    if q:
        params["q"] = q
    if format:
        params["format"] = format
    if source:
        params["source"] = source
    if max_price:
        params["max_price"] = max_price
    if live:
        params["live"] = live
    dest = "/" + ("?" + urlencode(params) if params else "")
    with _public_refresh_lock:
        now = time.time()
        if _public_refresh_state["running"]:
            return RedirectResponse(dest, status_code=303)
        last = _public_refresh_state["started_at"]
        if last and now - last < PUBLIC_REFRESH_COOLDOWN_SECS:
            return RedirectResponse(dest, status_code=303)
        _public_refresh_state.update(running=True, started_at=now, finished_at=None)
    threading.Thread(target=_run_public_refresh_job, daemon=True).start()
    return RedirectResponse(dest, status_code=303)


# ---------------------------------------------------------------------------
# Admin data refresh (manual poll trigger; ADMIN_KEY-gated)
# ---------------------------------------------------------------------------

REFRESH_COOLDOWN_SECS = 1800  # 30 min between manual refreshes
REFRESH_SOURCES = (
    ("youtube-roster", run_youtube_roster),
    ("twitch-roster", run_twitch_roster),
    ("youtube", run_youtube),
    ("ebay", run_ebay),
)

_refresh_lock = threading.Lock()
_refresh_state = {
    "running": False,
    "started_at": None,    # epoch seconds
    "finished_at": None,   # epoch seconds
    "summary": None,       # last few log lines of the finished run
    "error": None,
}


def _run_refresh_job() -> None:
    """Run all pollers sequentially in a background thread; never raises."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            for name, fn in REFRESH_SOURCES:
                print(f"--- refresh: {name} ---")
                try:
                    fn()
                except Exception as exc:  # one source failing must not kill the rest
                    print(f"refresh: {name} failed: {exc}")
    finally:
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        with _refresh_lock:
            _refresh_state.update(
                running=False, finished_at=time.time(),
                summary="\n".join(lines[-12:]) or "(no output)",
                error=None,
            )


def _refresh_status() -> dict:
    with _refresh_lock:
        st = dict(_refresh_state)
    now = time.time()
    st["cooldown_remaining"] = 0
    if st["started_at"] and now - st["started_at"] < REFRESH_COOLDOWN_SECS:
        st["cooldown_remaining"] = int(REFRESH_COOLDOWN_SECS - (now - st["started_at"]))
    return st


@app.post("/admin/refresh")
def admin_refresh(key: str = Form(default="")):
    if not _admin_key_ok(key):
        return RedirectResponse("/admin/suggestions", status_code=303)
    with _refresh_lock:
        if _refresh_state["running"]:
            return RedirectResponse(
                f"/admin/suggestions?key={key}&refresh=running", status_code=303)
        now = time.time()
        last = _refresh_state["started_at"]
        if last and now - last < REFRESH_COOLDOWN_SECS:
            wait = int(REFRESH_COOLDOWN_SECS - (now - last))
            return RedirectResponse(
                f"/admin/suggestions?key={key}&refresh=cooldown&wait={wait}",
                status_code=303)
        _refresh_state.update(running=True, started_at=now,
                              finished_at=None, summary=None, error=None)
    threading.Thread(target=_run_refresh_job, daemon=True).start()
    return RedirectResponse(
        f"/admin/suggestions?key={key}&refresh=started", status_code=303)

def _set_session_cookie(response: RedirectResponse, request: Request, user_id: int) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE, auth.create_session(user_id),
        max_age=auth.SESSION_MAX_AGE, httponly=True, samesite="lax",
        secure=request.url.scheme == "https", path="/",
    )


def _client_ip(request: Request) -> str:
    # Render sits behind a proxy; X-Forwarded-For carries the real IP.
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "?")


@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    if auth.get_current_user(request):
        return RedirectResponse("/account", status_code=303)
    return templates.TemplateResponse(request, "signup.html", {"error": None})


@app.post("/signup")
def signup(
    request: Request,
    email: str = Form(default=""),
    password: str = Form(default=""),
    website: str = Form(default=""),  # honeypot
):
    if website.strip():
        return RedirectResponse("/login", status_code=303)  # bot: fake success
    ip = _client_ip(request)
    if not auth.throttle_check(ip):
        return templates.TemplateResponse(request, "signup.html", {
            "error": f"Too many attempts — try again in {auth.throttle_wait_seconds(ip)}s.",
        })
    email = (email or "").strip()
    if not auth.valid_email(email):
        return templates.TemplateResponse(request, "signup.html",
                                          {"error": "Enter a valid email address."})
    if len(password or "") < 8:
        return templates.TemplateResponse(request, "signup.html", {
            "error": "Password needs to be at least 8 characters."})
    try:
        with db.get_conn() as conn:
            if db.get_user_by_email(conn, email):
                return templates.TemplateResponse(request, "signup.html", {
                    "error": "That email already has an account — try signing in."})
            user_id = db.create_user(conn, email, auth.hash_password(password))
    except Exception:
        return templates.TemplateResponse(request, "signup.html", {
            "error": "Something went wrong creating your account. Try again."})
    resp = RedirectResponse("/", status_code=303)
    _set_session_cookie(resp, request, user_id)
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str | None = Query(default=None)):
    if auth.get_current_user(request):
        return RedirectResponse(next or "/account", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None, "next": next or ""})


@app.post("/login")
def login(
    request: Request,
    email: str = Form(default=""),
    password: str = Form(default=""),
    next: str = Form(default=""),
    website: str = Form(default=""),  # honeypot
):
    if website.strip():
        return RedirectResponse("/login", status_code=303)
    ip = _client_ip(request)
    if not auth.throttle_check(ip):
        return templates.TemplateResponse(request, "login.html", {
            "error": f"Too many attempts — try again in {auth.throttle_wait_seconds(ip)}s.",
            "next": next})
    user = None
    try:
        with db.get_conn() as conn:
            user = db.get_user_by_email(conn, email)
    except Exception:
        pass
    if not user or not auth.check_password(password or "", user["password_hash"]):
        # Same message either way: don't reveal which half was wrong.
        return templates.TemplateResponse(request, "login.html", {
            "error": "Email or password didn't match.", "next": next})
    dest = next if next.startswith("/") and not next.startswith("//") else "/"
    resp = RedirectResponse(dest, status_code=303)
    _set_session_cookie(resp, request, user["id"])
    return resp


@app.post("/logout")
def logout(request: Request):
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


def _require_user(request: Request):
    user = auth.get_current_user(request)
    if not user:
        raise _LoginRequired()
    return user


class _LoginRequired(Exception):
    pass


@app.get("/account", response_class=HTMLResponse)
def account(request: Request, notice: str | None = Query(default=None)):
    try:
        user = _require_user(request)
    except _LoginRequired:
        return RedirectResponse("/login?next=/account", status_code=303)
    notices = {
        "email_updated": "Email address updated.",
        "password_updated": "Password updated.",
    }
    try:
        with db.get_conn() as conn:
            favs = db.list_favorites(conn, user["id"])
            live = [_enrich(r) for r in
                    db.breaks_for_breakers(conn, [f["breaker"] for f in favs])]
            searches = db.list_saved_searches(conn, user["id"])
        error = None
    except Exception as exc:
        favs, live, searches, error = [], [], [], f"Database unavailable: {exc}"
    return templates.TemplateResponse(request, "account.html", {
        "user": user, "favorites": favs, "live": live,
        "searches": searches, "error": error,
        "notice": notices.get(notice or ""),
    })


def _account_error(request: Request, user: dict, msg: str):
    """Re-render the account page with an error (keeps favorites/searches)."""
    try:
        with db.get_conn() as conn:
            favs = db.list_favorites(conn, user["id"])
            live = [_enrich(r) for r in
                    db.breaks_for_breakers(conn, [f["breaker"] for f in favs])]
            searches = db.list_saved_searches(conn, user["id"])
    except Exception:
        favs, live, searches = [], [], []
    return templates.TemplateResponse(request, "account.html", {
        "user": user, "favorites": favs, "live": live,
        "searches": searches, "error": msg, "notice": "",
    })


@app.post("/account/email")
def account_change_email(
    request: Request,
    new_email: str = Form(default=""),
    password: str = Form(default=""),
):
    try:
        user = _require_user(request)
    except _LoginRequired:
        return RedirectResponse("/login?next=/account", status_code=303)
    new_email = (new_email or "").strip().lower()
    if not auth.valid_email(new_email):
        return _account_error(request, user, "That doesn't look like a valid email address.")
    if not auth.check_password(password or "", user["password_hash"]):
        return _account_error(request, user, "Current password is incorrect.")
    if new_email == user["email"]:
        return RedirectResponse("/account", status_code=303)
    try:
        with db.get_conn() as conn:
            if db.get_user_by_email(conn, new_email):
                return _account_error(request, user, "That email is already in use.")
            db.update_user_email(conn, user["id"], new_email)
    except Exception:
        return _account_error(request, user, "Couldn't update your email. Try again.")
    return RedirectResponse("/account?notice=email_updated", status_code=303)


@app.post("/account/password")
def account_change_password(
    request: Request,
    current_password: str = Form(default=""),
    new_password: str = Form(default=""),
    confirm_password: str = Form(default=""),
):
    try:
        user = _require_user(request)
    except _LoginRequired:
        return RedirectResponse("/login?next=/account", status_code=303)
    if not auth.check_password(current_password or "", user["password_hash"]):
        return _account_error(request, user, "Current password is incorrect.")
    if len(new_password or "") < 8:
        return _account_error(request, user, "New password must be at least 8 characters.")
    if new_password != confirm_password:
        return _account_error(request, user, "New passwords don't match.")
    try:
        with db.get_conn() as conn:
            db.update_password_hash(conn, user["id"], auth.hash_password(new_password))
    except Exception:
        return _account_error(request, user, "Couldn't update your password. Try again.")
    return RedirectResponse("/account?notice=password_updated", status_code=303)


@app.post("/account/delete")
def account_delete(
    request: Request,
    password: str = Form(default=""),
    confirm: str = Form(default=""),
):
    try:
        user = _require_user(request)
    except _LoginRequired:
        return RedirectResponse("/login?next=/account", status_code=303)
    if (confirm or "").strip().upper() != "DELETE":
        return _account_error(request, user, 'Type DELETE to confirm account deletion.')
    if not auth.check_password(password or "", user["password_hash"]):
        return _account_error(request, user, "Password is incorrect.")
    try:
        with db.get_conn() as conn:
            db.delete_user(conn, user["id"])
    except Exception:
        return _account_error(request, user, "Couldn't delete your account. Try again.")
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@app.post("/favorites/toggle")
def favorite_toggle(
    request: Request,
    breaker: str = Form(default=""),
    next: str = Form(default="/"),
):
    try:
        user = _require_user(request)
    except _LoginRequired:
        return RedirectResponse("/signup", status_code=303)
    breaker = (breaker or "").strip()
    dest = next if next.startswith("/") and not next.startswith("//") else "/"
    if breaker:
        try:
            with db.get_conn() as conn:
                if breaker in db.favorite_breakers(conn, user["id"]):
                    db.remove_favorite(conn, user["id"], breaker)
                else:
                    db.add_favorite(conn, user["id"], breaker)
        except Exception:
            pass
    return RedirectResponse(dest, status_code=303)


@app.post("/searches/save")
def save_search_route(
    request: Request,
    name: str = Form(default=""),
    q: str = Form(default=""),
    format: str = Form(default=""),
    source: str = Form(default=""),
    max_price: str = Form(default=""),
    region: str = Form(default=""),
):
    try:
        user = _require_user(request)
    except _LoginRequired:
        return RedirectResponse("/signup", status_code=303)
    name = (name or "").strip() or "Untitled search"
    try:
        mp = float(max_price) if max_price.strip() else None
    except ValueError:
        mp = None
    try:
        with db.get_conn() as conn:
            db.save_search(conn, user["id"], name,
                           q.strip() or None, format.strip() or None,
                           source.strip() or None, mp,
                           region.strip() or None)
    except Exception:
        pass
    return RedirectResponse("/account", status_code=303)


@app.post("/searches/{search_id}/delete")
def delete_search_route(request: Request, search_id: int):
    try:
        user = _require_user(request)
    except _LoginRequired:
        return RedirectResponse("/login", status_code=303)
    try:
        with db.get_conn() as conn:
            db.delete_saved_search(conn, user["id"], search_id)
    except Exception:
        pass
    return RedirectResponse("/account", status_code=303)
