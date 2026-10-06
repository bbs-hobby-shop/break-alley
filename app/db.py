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
