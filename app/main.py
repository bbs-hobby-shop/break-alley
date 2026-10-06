"""FastAPI web UI — thin search/schedule front end (server-rendered, SEO-friendly).

Routes:
  GET /              search page with filters
  GET /break/{id}    detail page with prominent outbound link
"""
from pathlib import Path

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, config, db, youtube
from .normalizer import date_label, display_title, extract_break_number

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
    max_price: float | None = Query(default=None),
    source: str | None = Query(default=None),
    live: bool = Query(default=False),
    suggested: str | None = Query(default=None),
):
    format = format or None
    source = source or None
    user = auth.get_current_user(request)
    favorites: set[str] = set()
    try:
        with db.get_conn() as conn:
            results = [
                _enrich(dict(r)) for r in db.search_breaks(
                    conn, q=q or None, format=format,
                    max_price=max_price, source=source,
                    live_only=True if live else None,
                )
            ]
            if user:
                favorites = db.favorite_breakers(conn, user["id"])
        error = None
    except Exception as exc:  # DB not up / not migrated yet
        results, error = [], f"Database unavailable: {exc}"
    return templates.TemplateResponse(request, "search.html", {
        "results": results, "error": error,
        "q": q or "", "format": format or "",
        "max_price": max_price or "", "source": source or "", "live": live,
        "suggested": suggested or "",
        "user": user, "favorites": favorites,
        "formats": ["pyt", "random", "division", "hit_draft", "personal", "case_break", "group_break"],
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
def admin_suggestions(request: Request, key: str | None = Query(default=None)):
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


# ---------------------------------------------------------------------------
# User accounts (optional perks; browsing stays free)
# ---------------------------------------------------------------------------

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
    resp = RedirectResponse("/account", status_code=303)
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
    dest = next if next.startswith("/") and not next.startswith("//") else "/account"
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
def account(request: Request):
    try:
        user = _require_user(request)
    except _LoginRequired:
        return RedirectResponse("/login?next=/account", status_code=303)
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
    })


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
                           source.strip() or None, mp)
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
