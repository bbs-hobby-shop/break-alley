"""FastAPI web UI — thin search/schedule front end (server-rendered, SEO-friendly).

Routes:
  GET /              search page with filters
  GET /break/{id}    detail page with prominent outbound link
"""
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from . import db

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Box Break Finder")


@app.get("/", response_class=HTMLResponse)
def search(
    request: Request,
    q: str | None = Query(default=None),
    sport: str | None = Query(default=None),
    format: str | None = Query(default=None),
    max_price: float | None = Query(default=None),
    source: str | None = Query(default=None),
    live: bool = Query(default=False),
):
    sport = sport or None
    format = format or None
    source = source or None
    try:
        with db.get_conn() as conn:
            results = db.search_breaks(
                conn, q=q or None, sport=sport, format=format,
                max_price=max_price, source=source,
                live_only=True if live else None,
            )
        error = None
    except Exception as exc:  # DB not up / not migrated yet
        results, error = [], f"Database unavailable: {exc}"
    return templates.TemplateResponse(request, "search.html", {
        "results": results, "error": error,
        "q": q or "", "sport": sport or "", "format": format or "",
        "max_price": max_price or "", "source": source or "", "live": live,
        "sports": ["football", "basketball", "baseball", "soccer", "hockey"],
        "formats": ["pyt", "random", "division", "hit_draft", "personal", "case_break"],
    })


@app.get("/break/{break_id}", response_class=HTMLResponse)
def detail(request: Request, break_id: int):
    try:
        with db.get_conn() as conn:
            row = db.get_break(conn, break_id)
        error = None if row else "Break not found."
    except Exception as exc:
        row, error = None, f"Database unavailable: {exc}"
    return templates.TemplateResponse(request, "detail.html", {
        "b": row, "error": error,
    })
