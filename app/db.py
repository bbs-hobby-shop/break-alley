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


SEARCH_SQL = """
SELECT id, source, source_url, breaker, product_raw, product_normalized,
       sport, format, price, currency, starts_at, is_live,
       slots_total, slots_remaining, thumbnail_url, title_raw, affiliate_url
FROM breaks
WHERE (%(q)s IS NULL OR title_raw ILIKE '%%' || %(q)s || '%%'
       OR COALESCE(product_normalized, '') ILIKE '%%' || %(q)s || '%%')
  AND (%(sport)s IS NULL OR sport = %(sport)s)
  AND (%(format)s IS NULL OR format = %(format)s)
  AND (%(max_price)s IS NULL OR price IS NULL OR price <= %(max_price)s)
  AND (%(source)s IS NULL OR source = %(source)s)
  AND (%(live_only)s IS NULL OR is_live = %(live_only)s)
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
