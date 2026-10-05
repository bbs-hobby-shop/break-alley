-- Box Break Aggregator schema (Postgres)
-- Run: psql "$DATABASE_URL" -f schema.sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS breaks (
    id              SERIAL PRIMARY KEY,
    source          TEXT NOT NULL CHECK (source IN ('ebay', 'youtube')),
    source_url      TEXT NOT NULL,
    breaker         TEXT,                       -- eBay seller ID / YouTube channel name
    product_raw     TEXT,                       -- product text as seen in the wild
    product_normalized TEXT,                    -- canonical product name (nullable until matched)
    sport           TEXT CHECK (sport IN ('football','basketball','baseball','soccer','hockey','other')),
    format          TEXT CHECK (format IN ('pyt','random','division','hit_draft','personal','case_break','unknown')),
    price           NUMERIC(10,2),
    currency        TEXT DEFAULT 'USD',
    starts_at       TIMESTAMPTZ,                -- scheduled start (streams); NULL for eBay slot listings
    is_live         BOOLEAN NOT NULL DEFAULT FALSE,
    slots_total     INTEGER,
    slots_remaining INTEGER,
    thumbnail_url   TEXT,
    title_raw       TEXT NOT NULL,
    affiliate_url   TEXT,                       -- outbound monetized link (EPN for eBay)
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    UNIQUE (source, source_url)
);

CREATE INDEX IF NOT EXISTS idx_breaks_sport      ON breaks (sport);
CREATE INDEX IF NOT EXISTS idx_breaks_format     ON breaks (format);
CREATE INDEX IF NOT EXISTS idx_breaks_is_live    ON breaks (is_live);
CREATE INDEX IF NOT EXISTS idx_breaks_starts_at  ON breaks (starts_at);
CREATE INDEX IF NOT EXISTS idx_breaks_source     ON breaks (source);
CREATE INDEX IF NOT EXISTS idx_breaks_product    ON breaks (product_normalized);
CREATE INDEX IF NOT EXISTS idx_breaks_title_trgm ON breaks USING gin (title_raw gin_trgm_ops);

-- Canonical products + known title aliases for normalization.
CREATE TABLE IF NOT EXISTS products (
    id             SERIAL PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,        -- e.g. '2024 Panini Prizm Football Hobby'
    sport          TEXT,
    aliases        TEXT[] NOT NULL DEFAULT '{}' -- lowercase alias strings matched as substrings
);
