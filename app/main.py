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
from .ingest import run_ebay, run_fanatics, run_twitch_roster, run_youtube, run_youtube_roster
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
    # Thumbnail: real image where available, format icon fallback (Brian 2026-10-06)
    thumb = row.get("thumbnail_url")
    if not thumb and row.get("source") == "youtube":
        # YouTube video thumbnails via video ID in source_url
        import re
        url = row.get("source_url") or ""
        m = re.search(r"(?:v=|youtu\.be/|/live/|/shorts/)([A-Za-z0-9_-]{11})", url)
        if m:
            thumb = f"https://i.ytimg.com/vi/{m.group(1)}/hqdefault.jpg"
    row["thumb_url"] = thumb
    # Format icon: instant visual format recognition
    fmt = (row.get("format") or "").lower()
    icons = {
        "pyt": "🎯", "pick_your_team": "🎯",
        "random": "🎲",
        "personal": "👤",
        "case_break": "📦", "case": "📦",
        "box_break": "🃏",
    }
    row["format_icon"] = icons.get(fmt, "🃏")
    return row


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    suggested: str | None = Query(default=None),
):
    """Brian 2026-10-08: clean landing page — search form, releases, and
    suggestion boxes, but NO break cards. Results live on /results."""
    user = auth.get_current_user(request)
    try:
        with db.get_conn() as conn:
            try:
                upcoming_releases = db.list_upcoming_releases(conn)
            except Exception:
                upcoming_releases = []
        error = None
    except Exception as exc:
        error = f"Database unavailable: {exc}"
        upcoming_releases = []
    return templates.TemplateResponse(request, "home.html", {
        "error": error,
        "suggested": suggested or "",
        "user": user,
        "refresh_running": _public_refresh_running(),
        "upcoming_releases": upcoming_releases,
        "q": "", "format": "", "max_price": "", "source": "",
        "live": False, "region": "", "sort": "", "auctions": False,
        "formats": ["pyt", "random", "personal", "case_break", "box_break"],
        "format_labels": FORMAT_LABELS,
    })


@app.get("/results", response_class=HTMLResponse)
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
    auctions: bool = Query(default=False),
):
    format = format or None
    source = source or None
    region = region if region in ("us", "intl") else None
    sort = sort if sort in ("soonest", "price_low", "price_high", "newest", "live", "ending") else None
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
    saved_ids: set[int] = set()
    try:
        with db.get_conn() as conn:
            # Brian 2026-10-08: real result total (was capped at 2000 by the
            # old SQL LIMIT). Cards still render 60 at a time for phone speed.
            total_results = db.count_search_breaks(
                conn, q=q or None, format=format,
                max_price=max_price_val, source=source,
                live_only=True if live else None, region=region,
                auction_only=True if auctions else None,
            )
            all_results = [
                _enrich(dict(r)) for r in db.search_breaks(
                    conn, q=q or None, format=format,
                    max_price=max_price_val, source=source,
                    live_only=True if live else None, region=region,
                    sort=sort, auction_only=True if auctions else None,
                    limit=PAGE_SIZE,
                )
            ]
            # Pagination (Brian 2026-10-07): render 60 at a time so the page
            # stays fast on phones (1,500+ cards was choking the iOS keyboard).
            results = all_results[:PAGE_SIZE]
            has_more = total_results > PAGE_SIZE
            if user:
                favorites = db.favorite_breakers(conn, user["id"])
                saved_ids = db.saved_listing_ids(conn, user["id"])
            updated_ago = _ago(db.last_data_update(conn))
            # Release calendar (Idea 2026-10-07): upcoming card drops for the
            # homepage banner. Never breaks the page if the table is missing.
            try:
                upcoming_releases = db.list_upcoming_releases(conn)
            except Exception:
                upcoming_releases = []
            # Brian 2026-10-07: track real searches (not plain homepage loads)
            # for demand analytics — what buyers are looking for.
            if q or format or source or max_price_val or live or auctions or sort or region:
                db.log_event(
                    conn, "search",
                    user_id=user["id"] if user else None,
                    meta={"q": q or None, "format": format, "source": source,
                          "max_price": max_price_val, "live": live,
                          "auctions": auctions, "sort": sort, "region": region,
                          "result_count": len(results)},
                )
        error = None
    except Exception as exc:  # DB not up / not migrated yet
        results, error = [], f"Database unavailable: {exc}"
        updated_ago = "—"
        upcoming_releases = []
    return templates.TemplateResponse(request, "results.html", {
        "results": results, "error": error,
        "total_results": total_results, "has_more": has_more,
        "page_size": PAGE_SIZE,
        "q": q or "", "format": format or "",
        "max_price": max_price or "", "source": source or "", "live": live,
        "region": region or "", "sort": sort or "", "auctions": auctions,
        "suggested": suggested or "",
        "user": user, "favorites": favorites, "saved_ids": saved_ids,
        "refresh_running": _public_refresh_running(),
        "updated_ago": updated_ago,
        "upcoming_releases": upcoming_releases,
        "formats": ["pyt", "random", "personal", "case_break", "box_break"],
        "format_labels": FORMAT_LABELS,
    })


@app.get("/guide", response_class=HTMLResponse)
def guide_page(request: Request):
    """Brian 2026-10-08: newcomer guide — what each break format means
    and how each platform works. Static content, no DB needed."""
    user = auth.get_current_user(request)
    return templates.TemplateResponse(request, "guide.html", {
        "request": request, "user": user,
    })


@app.get("/breaker/{breaker_name}", response_class=HTMLResponse)
def breaker_page(request: Request, breaker_name: str):
    """Brian 2026-10-07: tapping a breaker's name shows all their current
    and upcoming breaks. Reuses the search template + card UI."""
    import urllib.parse
    breaker = urllib.parse.unquote(breaker_name)
    user = auth.get_current_user(request)
    favorites: set[str] = set()
    saved_ids: set[int] = set()
    try:
        with db.get_conn() as conn:
            results = [_enrich(dict(r)) for r in
                       db.breaks_for_breakers(conn, [breaker], limit=200)]
            if user:
                favorites = db.favorite_breakers(conn, user["id"])
                saved_ids = db.saved_listing_ids(conn, user["id"])
        error = None
    except Exception as exc:
        results, error = [], f"Database unavailable: {exc}"
    return templates.TemplateResponse(request, "search.html", {
        "results": results, "error": error,
        "q": "", "format": "", "max_price": "", "source": "", "live": False,
        "region": "", "sort": "", "auctions": False, "suggested": "",
        "user": user, "favorites": favorites, "saved_ids": saved_ids,
        "refresh_running": False, "updated_ago": "",
        "upcoming_releases": [],
        "breaker_page": breaker,
        "formats": ["pyt", "random", "personal", "case_break", "box_break"],
        "format_labels": FORMAT_LABELS,
    })


PAGE_SIZE = 60


@app.get("/more", response_class=HTMLResponse)
def load_more(
    request: Request,
    q: str | None = Query(default=None),
    format: str | None = Query(default=None),
    max_price: str | None = Query(default=None),
    source: str | None = Query(default=None),
    live: bool = Query(default=False),
    region: str | None = Query(default=None),
    sort: str | None = Query(default=None),
    auctions: bool = Query(default=False),
    offset: int = Query(default=0),
):
    """Brian 2026-10-07: AJAX pagination — returns the next PAGE_SIZE cards
    as HTML fragments for the Load more button. Same filters as the search."""
    format = format or None
    source = source or None
    region = region if region in ("us", "intl") else None
    sort = sort if sort in ("soonest", "price_low", "price_high", "newest", "live", "ending") else None
    try:
        max_price_val = float(max_price) if max_price and max_price.strip() else None
    except (ValueError, TypeError):
        max_price_val = None
    user = auth.get_current_user(request)
    favorites: set[str] = set()
    saved_ids: set[int] = set()
    try:
        with db.get_conn() as conn:
            results = [
                _enrich(dict(r)) for r in db.search_breaks(
                    conn, q=q or None, format=format,
                    max_price=max_price_val, source=source,
                    live_only=True if live else None, region=region,
                    sort=sort, auction_only=True if auctions else None,
                    limit=PAGE_SIZE, offset=offset,
                )
            ]
            if user:
                favorites = db.favorite_breakers(conn, user["id"])
                saved_ids = db.saved_listing_ids(conn, user["id"])
    except Exception:
        results = []
    # Render just the cards via the shared partial
    cards = []
    for b in results:
        cards.append(templates.get_template("_card.html").render({
            "b": b, "user": user, "favorites": favorites, "saved_ids": saved_ids,
            "q": q or "", "format": format or "", "source": source or "",
            "max_price": max_price or "", "live": live,
        }))
    return HTMLResponse("\n".join(cards))


@app.get("/go/{break_id}")
def go_outbound(request: Request, break_id: int, dest: str = Query(default="")):
    """Outbound click tracker (Brian 2026-10-07): logs the click for sales
    metrics, then redirects. dest must match one of the break's known URLs
    (prevents open-redirect abuse)."""
    target = "/"
    try:
        with db.get_conn() as conn:
            row = db.get_break(conn, break_id)
            if row:
                row = dict(row)
                allowed = {row.get("source_url"), row.get("affiliate_url"),
                           row.get("video_url")}
                try:
                    import json as _json
                    vl = row.get("video_links")
                    if isinstance(vl, str):
                        vl = _json.loads(vl)
                    for link in (vl or []):
                        if isinstance(link, dict) and link.get("url"):
                            allowed.add(link["url"])
                except Exception:
                    pass
                allowed.discard(None)
                allowed.discard("")
                target = dest if dest in allowed else (
                    row.get("affiliate_url") or row.get("source_url") or "/")
                user = auth.get_current_user(request)
                db.log_event(
                    conn, "outbound_click",
                    user_id=user["id"] if user else None,
                    breaker=row.get("breaker"), break_id=break_id,
                    platform=row.get("source"),
                    meta={"dest": target,
                          "kind": "video" if target == row.get("video_url")
                                  or (dest in allowed and "video" in (dest or ""))
                                  else "listing"},
                )
    except Exception:
        pass
    return RedirectResponse(target, status_code=302)


@app.get("/break/{break_id}", response_class=HTMLResponse)
def detail(request: Request, break_id: int):
    try:
        with db.get_conn() as conn:
            row = db.get_break(conn, break_id)
            if row:
                row = _enrich(dict(row))
                # Brian 2026-10-07: track listing views for sales metrics.
                user = auth.get_current_user(request)
                db.log_event(
                    conn, "break_viewed",
                    user_id=user["id"] if user else None,
                    breaker=row.get("breaker"), break_id=break_id,
                    platform=row.get("source"),
                )
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


# ---------------------------------------------------------------------------
# Analytics dashboard (Brian 2026-10-07): sales metrics — clicks, views,
# saves, follows per breaker, demand signals, trends. ADMIN_KEY-gated.
# ---------------------------------------------------------------------------

@app.get("/admin/stats", response_class=HTMLResponse)
def admin_stats(
    request: Request,
    key: str | None = Query(default=None),
    days: int = Query(default=30),
):
    if not _admin_key_ok(key):
        return templates.TemplateResponse(request, "admin_stats.html", {
            "denied": True, "key": key or "",
        })
    days = days if days in (7, 30, 90) else 30
    try:
        with db.get_conn() as conn:
            overview = db.analytics_overview(conn, days)
            leaderboard = db.analytics_breaker_leaderboard(conn, days)
            daily = db.analytics_daily(conn, days)
            searches = db.analytics_top_searches(conn, days)
            platforms = db.analytics_platform_split(conn, days)
        error = None
    except Exception as exc:
        overview, leaderboard, daily, searches, platforms = {}, [], [], [], []
        error = f"Database unavailable: {exc}"
    return templates.TemplateResponse(request, "admin_stats.html", {
        "denied": False, "key": key or "", "days": days, "error": error,
        "overview": overview, "leaderboard": leaderboard, "daily": daily,
        "searches": searches, "platforms": platforms,
    })


@app.get("/admin/stats/export")
def admin_stats_export(
    key: str | None = Query(default=None),
    days: int = Query(default=30),
):
    """CSV of the per-breaker rollup for sales outreach."""
    from fastapi.responses import PlainTextResponse
    if not _admin_key_ok(key):
        return PlainTextResponse("denied", status_code=403)
    days = days if days in (7, 30, 90) else 30
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["breaker", "outbound_clicks", "listing_views", "saves",
                "follows", "breaks_listed", f"period_days={days}"])
    try:
        with db.get_conn() as conn:
            for r in db.analytics_breaker_leaderboard(conn, days, limit=1000):
                w.writerow([r["breaker"], r["clicks"], r["views"],
                            r["saves"], r["follows"], r["breaks_listed"]])
    except Exception as exc:
        return PlainTextResponse(f"error: {exc}", status_code=500)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition":
                                      "attachment; filename=breaker-stats.csv"})


@app.get("/admin/releases", response_class=HTMLResponse)
def admin_releases(
    request: Request,
    key: str | None = Query(default=None),
):
    """Release calendar management (Idea 2026-10-07): add/remove release dates
    without touching code or the cron schedule."""
    if not _admin_key_ok(key):
        return templates.TemplateResponse(request, "admin_releases.html", {
            "denied": True, "key": key or "",
        })
    try:
        with db.get_conn() as conn:
            releases = db.list_all_releases(conn)
        error = None
    except Exception as exc:
        releases, error = [], f"Database unavailable: {exc}"
    from datetime import date
    return templates.TemplateResponse(request, "admin_releases.html", {
        "denied": False, "key": key or "", "releases": releases, "error": error,
        "today": date.today().isoformat(),
    })


@app.post("/admin/releases/add")
def admin_release_add(
    key: str = Form(default=""),
    product_name: str = Form(default=""),
    release_date: str = Form(default=""),
    notes: str = Form(default=""),
):
    if not _admin_key_ok(key):
        return RedirectResponse("/admin/releases", status_code=303)
    try:
        with db.get_conn() as conn:
            db.add_release(conn, product_name, release_date, notes)
    except Exception:
        pass
    return RedirectResponse(f"/admin/releases?key={key}", status_code=303)


@app.post("/admin/releases/{release_id}/remove")
def admin_release_remove(
    release_id: int,
    key: str = Form(default=""),
):
    if not _admin_key_ok(key):
        return RedirectResponse("/admin/releases", status_code=303)
    try:
        with db.get_conn() as conn:
            db.remove_release(conn, release_id)
    except Exception:
        pass
    return RedirectResponse(f"/admin/releases?key={key}", status_code=303)


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
# Whatnot show submissions: sellers submit -> Brian reviews -> breaks feed
# ---------------------------------------------------------------------------

@app.get("/submit-show", response_class=HTMLResponse)
def submit_show_form(request: Request):
    """Public form for Whatnot sellers to submit upcoming shows (Brian 2026-10-08)."""
    user = auth.get_current_user(request)
    return templates.TemplateResponse(request, "submit_show.html", {
        "request": request, "user": user, "submitted": False,
    })


@app.post("/submit-show")
def submit_show(
    request: Request,
    seller_username: str = Form(default=""),
    show_title: str = Form(default=""),
    show_url: str = Form(default=""),
    starts_at: str = Form(default=""),
    format: str = Form(default="box_break"),
    description: str = Form(default=""),
    consent: str = Form(default=""),
    website: str = Form(default=""),  # honeypot
):
    if website.strip():
        return RedirectResponse("/submit-show?submitted=1", status_code=303)
    seller_username = seller_username.strip().lstrip("@")
    show_title = show_title.strip()
    show_url = show_url.strip()
    if not (seller_username and show_title and show_url and starts_at and consent):
        return RedirectResponse("/submit-show?submitted=0", status_code=303)
    # show_url must be a whatnot.com link
    if "whatnot.com" not in show_url.lower():
        return RedirectResponse("/submit-show?submitted=0", status_code=303)
    try:
        import datetime
        # datetime-local gives "YYYY-MM-DDTHH:MM"; treat as UTC if no tz
        dt = datetime.datetime.fromisoformat(starts_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return RedirectResponse("/submit-show?submitted=0", status_code=303)
    valid_formats = {"pyt", "random", "personal", "case_break", "box_break"}
    fmt = format if format in valid_formats else "box_break"
    try:
        with db.get_conn() as conn:
            db.add_whatnot_submission(conn, seller_username, show_title,
                                      show_url, dt, fmt, description)
    except Exception:
        return RedirectResponse("/submit-show?submitted=0", status_code=303)
    return RedirectResponse("/submit-show?submitted=1", status_code=303)


@app.get("/admin/whatnot", response_class=HTMLResponse)
def admin_whatnot(request: Request, key: str | None = Query(default=None)):
    """Brian's review queue for Whatnot show submissions."""
    if not _admin_key_ok(key):
        return templates.TemplateResponse(request, "admin_whatnot.html", {
            "denied": True, "pending": [], "reviewed": [], "key": key or "",
        })
    try:
        with db.get_conn() as conn:
            pending = db.list_whatnot_submissions(conn, status="pending")
            reviewed = [r for r in db.list_whatnot_submissions(conn)[:50]
                        if r["status"] != "pending"]
    except Exception as exc:
        return templates.TemplateResponse(request, "admin_whatnot.html", {
            "denied": False, "error": f"Database unavailable: {exc}",
            "pending": [], "reviewed": [], "key": key or "",
        })
    return templates.TemplateResponse(request, "admin_whatnot.html", {
        "denied": False, "pending": pending, "reviewed": reviewed,
        "key": key or "",
    })


@app.post("/admin/whatnot/{submission_id}/approve")
def admin_whatnot_approve(request: Request, submission_id: int,
                          key: str = Form(default="")):
    if not _admin_key_ok(key):
        return RedirectResponse("/admin/whatnot", status_code=303)
    try:
        with db.get_conn() as conn:
            db.review_whatnot_submission(conn, submission_id, approved=True)
    except Exception:
        pass
    return RedirectResponse(f"/admin/whatnot?key={key}", status_code=303)


@app.post("/admin/whatnot/{submission_id}/reject")
def admin_whatnot_reject(request: Request, submission_id: int,
                         key: str = Form(default="")):
    if not _admin_key_ok(key):
        return RedirectResponse("/admin/whatnot", status_code=303)
    try:
        with db.get_conn() as conn:
            db.review_whatnot_submission(conn, submission_id, approved=False)
    except Exception:
        pass
    return RedirectResponse(f"/admin/whatnot?key={key}", status_code=303)


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


@app.post("/admin/maintenance")
def admin_maintenance(
    request: Request,
    key: str = Form(default=""),
):
    """DB-only maintenance: parse break times, mark live, purge junk.
    No eBay API calls — uses only our database (Brian 2026-10-06)."""
    if not _admin_key_ok(key):
        return RedirectResponse("/admin/suggestions", status_code=303)
    from datetime import datetime, timezone, timedelta
    from .ebay_video import parse_break_time
    results = []
    try:
        with db.get_conn() as conn:
            # 1. Parse break_time_text → starts_at
            rows = conn.execute(
                "SELECT id, break_time_text FROM breaks WHERE source='ebay' "
                "AND break_time_text IS NOT NULL AND starts_at IS NULL"
            ).fetchall()
            n_parsed = 0
            for r in rows:
                parsed = parse_break_time(r["break_time_text"])
                if parsed:
                    conn.execute(
                        "UPDATE breaks SET starts_at=%s WHERE id=%s",
                        (parsed, r["id"]),
                    )
                    n_parsed += 1
            results.append(f"parsed {n_parsed} break times")
            # 2. Mark as live if started within 4h
            rows2 = conn.execute(
                "SELECT id, starts_at FROM breaks WHERE source='ebay' "
                "AND starts_at IS NOT NULL AND NOT COALESCE(is_live, FALSE)"
            ).fetchall()
            n_live = 0
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
            results.append(f"marked {n_live} as live")
            # 3. Purge junk
            n_purged = db.purge_junk_breaks(conn)
            results.append(f"purged {n_purged} junk")
            # 4. Seed new roster channels from seed files
            from pathlib import Path
            seed_path = Path(__file__).with_name("seed_channels.txt")
            if seed_path.exists():
                lines = seed_path.read_text().splitlines()
                n_yt = db.seed_manual_channels(conn, lines)
                results.append(f"seeded {n_yt} YouTube channels")
            twitch_seed_path = Path(__file__).with_name("seed_twitch_channels.txt")
            if twitch_seed_path.exists():
                lines = twitch_seed_path.read_text().splitlines()
                n_tw = db.seed_manual_twitch_channels(conn, lines)
                results.append(f"seeded {n_tw} Twitch channels")
            # 5. Remove deactivated channels (Brian 2026-10-06)
            for dead_cid in ["UCRhDZLZKF8oDfGBtQkxBoNA", "UCDcQAgUJ687of2SOfOhrExw"]:
                conn.execute(
                    "DELETE FROM youtube_channels WHERE channel_id=%s",
                    (dead_cid,),
                )
            # Remove banned breakers from Twitch (Brian 2026-10-06)
            for dead_login in ["backyardbreaks"]:
                conn.execute(
                    "DELETE FROM twitch_channels WHERE login=%s",
                    (dead_login,),
                )
            results.append("removed 2 dormant YT + 1 banned Twitch")
            # 6. Prune stale listings (Brian 2026-10-06)
            # YouTube: live streams older than 6h are over; upcoming breaks 2h past start are stale
            n_yt = conn.execute(
                """DELETE FROM breaks WHERE source='youtube' AND (
                    (COALESCE(is_live, FALSE) AND starts_at < NOW() - INTERVAL '6 hours')
                    OR (NOT COALESCE(is_live, FALSE) AND starts_at IS NOT NULL AND starts_at < NOW() - INTERVAL '2 hours')
                )"""
            ).rowcount
            # eBay: breaks that started >4h ago are over (matches live window)
            n_ebay = conn.execute(
                """DELETE FROM breaks WHERE source='ebay'
                   AND starts_at IS NOT NULL AND starts_at < NOW() - INTERVAL '4 hours'"""
            ).rowcount
            results.append(f"pruned {n_yt} stale YouTube + {n_ebay} stale eBay")
            # 7. Remove ended auctions, sold-out BIN, vanished listings (Brian 2026-10-07)
            ended = db.prune_ended_ebay(conn)
            results.append(f"pruned ended/sold eBay {ended}")
    except Exception as e:
        results.append(f"error: {e}")
    # Return as plain text for easy checking
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("\n".join(results))


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
    ("fanatics", run_fanatics),
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
# Brian 2026-10-07: ROSTER-ONLY — no keyword searches outside approved rosters.
REFRESH_SOURCES = (
    ("youtube-roster", run_youtube_roster),
    ("twitch-roster", run_twitch_roster),
    ("fanatics", run_fanatics),
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
        return RedirectResponse("/my-breaks", status_code=303)
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
            # Brian 2026-10-07: track signups for growth metrics.
            db.log_event(conn, "signup", user_id=user_id)
    except Exception:
        return templates.TemplateResponse(request, "signup.html", {
            "error": "Something went wrong creating your account. Try again."})
    resp = RedirectResponse("/", status_code=303)
    _set_session_cookie(resp, request, user_id)
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str | None = Query(default=None)):
    if auth.get_current_user(request):
        return RedirectResponse(next or "/my-breaks", status_code=303)
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


@app.get("/my-breaks", response_class=HTMLResponse)
def my_breaks(request: Request):
    """Brian 2026-10-08: saved breaks, favorite breakers, and live from followed
    breakers live here — separate from account settings."""
    try:
        user = _require_user(request)
    except _LoginRequired:
        return RedirectResponse("/login?next=/my-breaks", status_code=303)
    try:
        with db.get_conn() as conn:
            favs = db.list_favorites(conn, user["id"])
            live = [_enrich(r) for r in
                    db.breaks_for_breakers(conn, [f["breaker"] for f in favs])]
            saved_listings = [_enrich(dict(r)) for r in
                              db.list_saved_listings(conn, user["id"])]
        error = None
    except Exception as exc:
        favs, live, saved_listings, error = [], [], [], f"Database unavailable: {exc}"
    return templates.TemplateResponse(request, "my_breaks.html", {
        "user": user, "favorites": favs, "live": live,
        "saved_listings": saved_listings, "error": error,
    })


@app.get("/account", response_class=HTMLResponse)
def account(request: Request, notice: str | None = Query(default=None)):
    """Account settings only (Brian 2026-10-08) — profile, security, delete.
    Saved breaks/breakers moved to /my-breaks."""
    try:
        user = _require_user(request)
    except _LoginRequired:
        return RedirectResponse("/login?next=/account", status_code=303)
    notices = {
        "email_updated": "Email address updated.",
        "password_updated": "Password updated.",
    }
    return templates.TemplateResponse(request, "account.html", {
        "user": user, "error": None,
        "notice": notices.get(notice or ""),
    })


def _account_error(request: Request, user: dict, msg: str):
    """Re-render the account settings page with an error."""
    return templates.TemplateResponse(request, "account.html", {
        "user": user, "error": msg, "notice": "",
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
    following = None
    if breaker:
        try:
            with db.get_conn() as conn:
                if breaker in db.favorite_breakers(conn, user["id"]):
                    db.remove_favorite(conn, user["id"], breaker)
                    following = False
                    db.log_event(conn, "breaker_unfollowed",
                                 user_id=user["id"], breaker=breaker)
                else:
                    db.add_favorite(conn, user["id"], breaker)
                    following = True
                    db.log_event(conn, "breaker_followed",
                                 user_id=user["id"], breaker=breaker)
        except Exception:
            pass
    # Brian 2026-10-07: AJAX toggles get instant JSON; plain forms get the redirect.
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True, "breaker": breaker, "following": following}
    return RedirectResponse(dest, status_code=303)


@app.post("/saved-listings/toggle")
def saved_listing_toggle(
    request: Request,
    break_id: int = Form(default=0),
    next: str = Form(default="/"),
):
    """Brian 2026-10-07: the star on a card saves THAT LISTING."""
    try:
        user = _require_user(request)
    except _LoginRequired:
        return RedirectResponse("/signup", status_code=303)
    dest = next if next.startswith("/") and not next.startswith("//") else "/"
    saved = None
    if break_id:
        try:
            with db.get_conn() as conn:
                if break_id in db.saved_listing_ids(conn, user["id"]):
                    db.unsave_listing(conn, user["id"], break_id)
                    saved = False
                    db.log_event(conn, "listing_unsaved",
                                 user_id=user["id"], break_id=break_id)
                else:
                    db.save_listing(conn, user["id"], break_id)
                    saved = True
                    # Grab breaker/platform for the sales rollup.
                    br = db.get_break(conn, break_id)
                    br = dict(br) if br else {}
                    db.log_event(conn, "listing_saved",
                                 user_id=user["id"], break_id=break_id,
                                 breaker=br.get("breaker"),
                                 platform=br.get("source"))
        except Exception:
            pass
    # Brian 2026-10-07: AJAX toggles get instant JSON; plain forms get the redirect.
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True, "break_id": break_id, "saved": saved}
    return RedirectResponse(dest, status_code=303)

