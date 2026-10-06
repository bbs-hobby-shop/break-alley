"""Postgres helpers (psycopg v3)."""
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from . import config


@contextmanager
def get_conn():
    conn = psycopg.connect(config.DATABASE_URL, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


UPSERT_BREAK_SQL = """
INSERT INTO breaks (
    source, source_url, breaker, product_raw, product_normalized,
    sport, format, price, currency, starts_at, is_live,
    slots_total, slots_remaining, thumbnail_url, title_raw,
    affiliate_url, expires_at, channel_id
) VALUES (
    %(source)s, %(source_url)s, %(breaker)s, %(product_raw)s, %(product_normalized)s,
    %(sport)s, %(format)s, %(price)s, %(currency)s, %(starts_at)s, %(is_live)s,
    %(slots_total)s, %(slots_remaining)s, %(thumbnail_url)s, %(title_raw)s,
    %(affiliate_url)s, %(expires_at)s, %(channel_id)s
)
ON CONFLICT (source, source_url) DO UPDATE SET
    breaker = EXCLUDED.breaker,
    product_raw = EXCLUDED.product_raw,
    product_normalized = EXCLUDED.product_normalized,
    sport = EXCLUDED.sport,
    format = EXCLUDED.format,
    price = EXCLUDED.price,
    currency = EXCLUDED.currency,
    starts_at = EXCLUDED.starts_at,
    is_live = EXCLUDED.is_live,
    slots_total = EXCLUDED.slots_total,
    slots_remaining = EXCLUDED.slots_remaining,
    thumbnail_url = EXCLUDED.thumbnail_url,
    title_raw = EXCLUDED.title_raw,
    affiliate_url = EXCLUDED.affiliate_url,
    fetched_at = NOW(),
    expires_at = EXCLUDED.expires_at,
    channel_id = EXCLUDED.channel_id;
"""


def upsert_break(conn, row: dict) -> None:
    # channel_id only exists on YouTube rows; default it so eBay/Twitch/demo
    # rows don't KeyError on the named param.
    row.setdefault("channel_id", None)
    conn.execute(UPSERT_BREAK_SQL, row)


# ---------------------------------------------------------------------------
# YouTube channel roster (cheap per-channel monitoring; see app/youtube_roster.py)
# ---------------------------------------------------------------------------

UPSERT_CHANNEL_SQL = """
INSERT INTO youtube_channels (channel_id, title, source)
VALUES (%(channel_id)s, %(title)s, %(source)s)
ON CONFLICT (channel_id) DO UPDATE SET
    title = COALESCE(EXCLUDED.title, youtube_channels.title),
    last_hit_at = NOW();
"""


def upsert_youtube_channel(conn, channel_id: str, title: str | None = None,
                            source: str = "search") -> None:
    """Discovery hook: add a channel to the roster (or refresh it) and stamp
    last_hit_at — the channel just produced a kept break."""
    conn.execute(UPSERT_CHANNEL_SQL, {
        "channel_id": channel_id, "title": title, "source": source,
    })


def mark_channels_checked(conn, channel_ids: list[str]) -> None:
    """Stamp last_checked_at for channels a roster poll actually covered."""
    if not channel_ids:
        return
    conn.execute(
        "UPDATE youtube_channels SET last_checked_at = NOW() "
        "WHERE channel_id = ANY(%s)",
        (channel_ids,),
    )


def get_roster_channels(conn, limit: int) -> list[dict]:
    """Active roster channels for one poll, hottest first.

    Ordering puts channels that recently produced kept breaks first, so when
    the roster is capped the most productive channels are the ones checked.
    Among never-hit channels, the least-recently-checked go first.
    """
    return conn.execute(
        """SELECT channel_id, title FROM youtube_channels
           WHERE active
           ORDER BY last_hit_at DESC NULLS LAST,
                    last_checked_at ASC NULLS FIRST
           LIMIT %s""",
        (limit,),
    ).fetchall()


def seed_youtube_channels_from_breaks(conn) -> int:
    """Backfill the roster from channel_ids already stored on youtube breaks.

    Idempotent (ON CONFLICT DO NOTHING): safe to run at the start of every
    roster poll. Returns the number of channels added.
    """
    return conn.execute(
        """INSERT INTO youtube_channels (channel_id, title, source)
           SELECT DISTINCT channel_id, MAX(breaker), 'seed'
           FROM breaks
           WHERE source = 'youtube' AND channel_id IS NOT NULL
           GROUP BY channel_id
           ON CONFLICT (channel_id) DO NOTHING"""
    ).rowcount


def seed_manual_channels(conn, channel_ids: list[str]) -> int:
    """Upsert hand-picked channel IDs (from app/seed_channels.txt) as
    source='manual'. Idempotent: safe to run at the start of every roster
    poll. Returns the number of channels added."""
    n = 0
    for cid in channel_ids:
        cid = (cid or "").strip()
        if not cid or cid.startswith("#"):
            continue
        # allow trailing "  # comment" on the line
        cid = cid.split("#", 1)[0].strip().split()[0]
        cur = conn.execute(
            """INSERT INTO youtube_channels (channel_id, source)
               VALUES (%(cid)s, 'manual')
               ON CONFLICT (channel_id) DO NOTHING""",
            {"cid": cid},
        )
        n += cur.rowcount
    return n


def add_breaker_suggestion(conn, input_text: str, note: str | None = None) -> int:
    """Queue a community breaker suggestion for review. Returns the new id."""
    cur = conn.execute(
        """INSERT INTO breaker_suggestions (input_text, note)
           VALUES (%(input_text)s, %(note)s) RETURNING id""",
        {"input_text": input_text.strip()[:200], "note": (note or "").strip()[:500] or None},
    )
    return cur.fetchone()["id"]


def list_breaker_suggestions(conn, status: str | None = None) -> list[dict]:
    """Newest-first suggestions, optionally filtered by status."""
    if status:
        rows = conn.execute(
            """SELECT * FROM breaker_suggestions WHERE status = %(status)s
               ORDER BY created_at DESC""",
            {"status": status},
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM breaker_suggestions ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def review_breaker_suggestion(conn, suggestion_id: int, approved: bool,
                              channel_id: str | None = None,
                              reviewer_note: str | None = None) -> None:
    """Approve (with resolved UC channel_id) or reject a suggestion."""
    conn.execute(
        """UPDATE breaker_suggestions
           SET status = %(status)s, channel_id = %(channel_id)s,
               reviewer_note = %(reviewer_note)s, reviewed_at = NOW()
           WHERE id = %(sid)s""",
        {"status": "approved" if approved else "rejected",
         "channel_id": channel_id, "reviewer_note": reviewer_note,
         "sid": suggestion_id},
    )


# ---------------------------------------------------------------------------
# User accounts
# ---------------------------------------------------------------------------

def create_user(conn, email: str, password_hash: str) -> int:
    """Insert a user; email is stored lowercased. Raises on duplicate."""
    cur = conn.execute(
        """INSERT INTO users (email, password_hash)
           VALUES (%(email)s, %(password_hash)s) RETURNING id""",
        {"email": email.strip().lower(), "password_hash": password_hash},
    )
    return cur.fetchone()["id"]


def get_user_by_email(conn, email: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM users WHERE email = %(email)s",
        {"email": email.strip().lower()},
    ).fetchone()
    return dict(row) if row else None


def get_user_by_id(conn, user_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM users WHERE id = %(id)s", {"id": user_id}
    ).fetchone()
    return dict(row) if row else None


def update_user_email(conn, user_id: int, email: str) -> None:
    """Change email (stored lowercased). Raises on duplicate."""
    conn.execute(
        "UPDATE users SET email = %(email)s WHERE id = %(id)s",
        {"email": email.strip().lower(), "id": user_id},
    )


def update_password_hash(conn, user_id: int, password_hash: str) -> None:
    conn.execute(
        "UPDATE users SET password_hash = %(h)s WHERE id = %(id)s",
        {"h": password_hash, "id": user_id},
    )


def delete_user(conn, user_id: int) -> None:
    """Delete the account; favorites + saved searches cascade."""
    conn.execute("DELETE FROM users WHERE id = %(id)s", {"id": user_id})


def add_favorite(conn, user_id: int, breaker: str, channel_id: str | None = None) -> None:
    conn.execute(
        """INSERT INTO user_favorites (user_id, breaker, channel_id)
           VALUES (%(user_id)s, %(breaker)s, %(channel_id)s)
           ON CONFLICT (user_id, breaker) DO NOTHING""",
        {"user_id": user_id, "breaker": breaker.strip()[:120],
         "channel_id": channel_id},
    )


def remove_favorite(conn, user_id: int, breaker: str) -> None:
    conn.execute(
        "DELETE FROM user_favorites WHERE user_id = %(user_id)s AND breaker = %(breaker)s",
        {"user_id": user_id, "breaker": breaker},
    )


def list_favorites(conn, user_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT breaker, channel_id, created_at FROM user_favorites
           WHERE user_id = %(user_id)s ORDER BY created_at DESC""",
        {"user_id": user_id},
    ).fetchall()]


def favorite_breakers(conn, user_id: int) -> set[str]:
    return {r["breaker"] for r in list_favorites(conn, user_id)}


def breaks_for_breakers(conn, breakers: list[str], limit: int = 100) -> list[dict]:
    """Live-first breaks from the given breaker names (for My Breakers)."""
    if not breakers:
        return []
    return [dict(r) for r in conn.execute(
        """SELECT id, source, source_url, breaker, product_raw, product_normalized,
                  sport, format, price, currency, starts_at, is_live,
                  slots_total, slots_remaining, thumbnail_url, title_raw
           FROM breaks
           WHERE breaker = ANY(%(breakers)s)
           ORDER BY is_live DESC, starts_at NULLS LAST, fetched_at DESC
           LIMIT %(limit)s""",
        {"breakers": breakers, "limit": limit},
    ).fetchall()]


def save_search(conn, user_id: int, name: str, q: str | None,
                format: str | None, source: str | None,
                max_price: float | None) -> int:
    cur = conn.execute(
        """INSERT INTO saved_searches (user_id, name, q, format, source, max_price)
           VALUES (%(user_id)s, %(name)s, %(q)s, %(format)s, %(source)s, %(max_price)s)
           RETURNING id""",
        {"user_id": user_id, "name": name.strip()[:80], "q": q,
         "format": format, "source": source, "max_price": max_price},
    )
    return cur.fetchone()["id"]


def list_saved_searches(conn, user_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT id, name, q, format, source, max_price, created_at
           FROM saved_searches WHERE user_id = %(user_id)s
           ORDER BY created_at DESC""",
        {"user_id": user_id},
    ).fetchall()]


def delete_saved_search(conn, user_id: int, search_id: int) -> None:
    conn.execute(
        "DELETE FROM saved_searches WHERE id = %(id)s AND user_id = %(user_id)s",
        {"id": search_id, "user_id": user_id},
    )


def last_data_update(conn):
    """Newest fetched_at across breaks — the honest 'data updated X ago'."""
    row = conn.execute("SELECT MAX(fetched_at) AS t FROM breaks").fetchone()
    return row["t"] if row and row["t"] else None


def user_stats(conn) -> dict:
    """Signup growth snapshot for the admin page."""
    total = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    today = conn.execute(
        "SELECT COUNT(*) AS c FROM users WHERE created_at >= NOW() - INTERVAL '24 hours'"
    ).fetchone()["c"]
    week = conn.execute(
        "SELECT COUNT(*) AS c FROM users WHERE created_at >= NOW() - INTERVAL '7 days'"
    ).fetchone()["c"]
    latest = [dict(r) for r in conn.execute(
        "SELECT email, created_at FROM users ORDER BY created_at DESC LIMIT 10"
    ).fetchall()]
    return {"total": total, "today": today, "week": week, "latest": latest}


# Self-healing schema migration for the cron pollers (they don't run
# schema.sql — only the web service does on deploy). Idempotent: safe to
# run at the start of every ingest run.
SCHEMA_MIGRATIONS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'breaks') THEN
        IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'breaks_source_check') THEN
            ALTER TABLE breaks DROP CONSTRAINT breaks_source_check;
        END IF;
        ALTER TABLE breaks ADD CONSTRAINT breaks_source_check
            CHECK (source IN ('ebay', 'youtube', 'twitch'));
        IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'breaks_format_check') THEN
            ALTER TABLE breaks DROP CONSTRAINT breaks_format_check;
        END IF;
        ALTER TABLE breaks ADD CONSTRAINT breaks_format_check
            CHECK (format IN ('pyt','random','division','hit_draft','personal','case_break','group_break','unknown'));
    END IF;
END $$;

-- Roster table for cheap per-channel YouTube monitoring (schema.sql is the
-- canonical definition; this keeps cron pollers working on older DBs).
CREATE TABLE IF NOT EXISTS youtube_channels (
    channel_id      TEXT PRIMARY KEY,
    title           TEXT,
    source          TEXT NOT NULL DEFAULT 'search'
                    CHECK (source IN ('search', 'manual', 'seed')),
    added_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_checked_at TIMESTAMPTZ,
    last_hit_at     TIMESTAMPTZ,
    active          BOOLEAN NOT NULL DEFAULT TRUE
);
ALTER TABLE breaks ADD COLUMN IF NOT EXISTS channel_id TEXT;
CREATE INDEX IF NOT EXISTS idx_youtube_channels_active   ON youtube_channels (active);
CREATE INDEX IF NOT EXISTS idx_youtube_channels_last_hit ON youtube_channels (last_hit_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_breaks_channel_id ON breaks (channel_id);

-- Community breaker suggestions (public form -> review queue -> roster).
CREATE TABLE IF NOT EXISTS breaker_suggestions (
    id            SERIAL PRIMARY KEY,
    input_text    TEXT NOT NULL,
    note          TEXT,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'approved', 'rejected')),
    channel_id    TEXT,
    reviewer_note TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_breaker_suggestions_status ON breaker_suggestions (status);

-- User accounts (optional perks: favorite breakers, saved searches).
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS user_favorites (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    breaker    TEXT NOT NULL,
    channel_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, breaker)
);
CREATE TABLE IF NOT EXISTS saved_searches (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    q          TEXT,
    format     TEXT,
    source     TEXT,
    max_price  NUMERIC,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_saved_searches_user ON saved_searches (user_id);
"""


def ensure_schema(conn) -> None:
    conn.execute(SCHEMA_MIGRATIONS)


SEARCH_SQL = """
SELECT id, source, source_url, breaker, product_raw, product_normalized,
       sport, format, price, currency, starts_at, is_live,
       slots_total, slots_remaining, thumbnail_url, title_raw, affiliate_url
FROM breaks
WHERE (CAST(%(q)s AS TEXT) IS NULL OR title_raw ILIKE '%%' || CAST(%(q)s AS TEXT) || '%%'
       OR COALESCE(product_normalized, '') ILIKE '%%' || CAST(%(q)s AS TEXT) || '%%')
  AND (CAST(%(sport)s AS TEXT) IS NULL OR sport = %(sport)s)
  AND (CAST(%(format)s AS TEXT) IS NULL OR format = %(format)s)
  AND (CAST(%(max_price)s AS NUMERIC) IS NULL OR price IS NULL OR price <= %(max_price)s)
  AND (CAST(%(source)s AS TEXT) IS NULL OR source = %(source)s)
  AND (CAST(%(live_only)s AS BOOLEAN) IS NULL OR is_live = %(live_only)s)
ORDER BY is_live DESC, starts_at NULLS LAST, fetched_at DESC
LIMIT 200;
"""


def search_breaks(conn, q=None, sport=None, format=None, max_price=None,
                  source=None, live_only=None):
    return conn.execute(SEARCH_SQL, {
        "q": q, "sport": sport, "format": format, "max_price": max_price,
        "source": source, "live_only": live_only,
    }).fetchall()


def get_break(conn, break_id: int):
    return conn.execute(
        "SELECT * FROM breaks WHERE id = %s", (break_id,)
    ).fetchone()
