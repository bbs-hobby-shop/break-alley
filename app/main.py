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

from . import config, db, youtube
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
    try:
        with db.get_conn() as conn:
            results = [
                _enrich(dict(r)) for r in db.search_breaks(
                    conn, q=q or None, format=format,
                    max_price=max_price, source=source,
                    live_only=True if live else None,
                )
            ]
        error = None
    except Exception as exc:  # DB not up / not migrated yet
        results, error = [], f"Database unavailable: {exc}"
    return templates.TemplateResponse(request, "search.html", {
        "results": results, "error": error,
        "q": q or "", "format": format or "",
        "max_price": max_price or "", "source": source or "", "live": live,
        "suggested": suggested or "",
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
    except Exception as exc:
        return templates.TemplateResponse(request, "admin_suggestions.html", {
            "denied": False, "error": f"Database unavailable: {exc}",
            "pending": [], "reviewed": [], "key": key or "",
        })
    return templates.TemplateResponse(request, "admin_suggestions.html", {
        "denied": False, "pending": pending, "reviewed": reviewed, "key": key or "",
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
