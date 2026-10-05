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
    affiliate_url, expires_at
) VALUES (
    %(source)s, %(source_url)s, %(breaker)s, %(product_raw)s, %(product_normalized)s,
    %(sport)s, %(format)s, %(price)s, %(currency)s, %(starts_at)s, %(is_live)s,
    %(slots_total)s, %(slots_remaining)s, %(thumbnail_url)s, %(title_raw)s,
    %(affiliate_url)s, %(expires_at)s
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
    expires_at = EXCLUDED.expires_at;
"""


def upsert_break(conn, row: dict) -> None:
    conn.execute(UPSERT_BREAK_SQL, row)


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
