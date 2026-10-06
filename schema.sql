-- Box Break Aggregator schema (Postgres)
-- Run: psql "$DATABASE_URL" -f schema.sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS breaks (
    id              SERIAL PRIMARY KEY,
    source          TEXT NOT NULL CHECK (source IN ('ebay', 'youtube', 'twitch')),
    source_url      TEXT NOT NULL,
    breaker         TEXT,                       -- eBay seller ID / YouTube channel name
    product_raw     TEXT,                       -- product text as seen in the wild
    product_normalized TEXT,                    -- canonical product name (nullable until matched)
    sport           TEXT CHECK (sport IN ('football','basketball','baseball','soccer','hockey','other')),
    format          TEXT CHECK (format IN ('pyt','random','division','hit_draft','personal','case_break','group_break','team_break','player_break','box_break','unknown')),
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
    channel_id      TEXT,                       -- YouTube channelId (roster discovery + backfill)
    country         TEXT,                       -- 2-letter breaker country (US, CA, AU, ...) NULL = unknown
    group_key       TEXT,                       -- eBay: groups team-by-team listings of one break
    country         TEXT,                       -- 2-letter breaker country (US, CA, AU, ...) NULL = unknown
    UNIQUE (source, source_url)
);

-- Migrate existing databases (fresh DBs already have the column above).
ALTER TABLE breaks ADD COLUMN IF NOT EXISTS channel_id TEXT;
ALTER TABLE breaks ADD COLUMN IF NOT EXISTS country TEXT;
ALTER TABLE breaks ADD COLUMN IF NOT EXISTS group_key TEXT;

CREATE INDEX IF NOT EXISTS idx_breaks_group_key ON breaks (group_key);

CREATE INDEX IF NOT EXISTS idx_breaks_sport      ON breaks (sport);
CREATE INDEX IF NOT EXISTS idx_breaks_format     ON breaks (format);
CREATE INDEX IF NOT EXISTS idx_breaks_is_live    ON breaks (is_live);
CREATE INDEX IF NOT EXISTS idx_breaks_starts_at  ON breaks (starts_at);
CREATE INDEX IF NOT EXISTS idx_breaks_source     ON breaks (source);
CREATE INDEX IF NOT EXISTS idx_breaks_product    ON breaks (product_normalized);
CREATE INDEX IF NOT EXISTS idx_breaks_title_trgm ON breaks USING gin (title_raw gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_breaks_channel_id ON breaks (channel_id);
CREATE INDEX IF NOT EXISTS idx_breaks_country ON breaks (country);

-- YouTube channel roster for cheap per-channel monitoring (see
-- app/youtube_roster.py). Channels land here via the search poller's
-- discovery hook ('search'), the breaks-table backfill ('seed'), or manual
-- adds ('manual'). playlistItems.list (1 unit) + batched videos.list
-- (1 unit per 50 ids) replace search.list (100 units) for known channels.
CREATE TABLE IF NOT EXISTS youtube_channels (
    channel_id      TEXT PRIMARY KEY,
    title           TEXT,                        -- channel display name
    source          TEXT NOT NULL DEFAULT 'search'
                    CHECK (source IN ('search', 'manual', 'seed')),
    country         TEXT,                        -- 2-letter ISO country from snippet.country; NULL = unknown
    added_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_checked_at TIMESTAMPTZ,                 -- last roster poll that covered it
    last_hit_at     TIMESTAMPTZ,                 -- last poll where it produced a kept break
    active          BOOLEAN NOT NULL DEFAULT TRUE
);
ALTER TABLE youtube_channels ADD COLUMN IF NOT EXISTS country TEXT;

CREATE INDEX IF NOT EXISTS idx_youtube_channels_active   ON youtube_channels (active);
CREATE INDEX IF NOT EXISTS idx_youtube_channels_last_hit ON youtube_channels (last_hit_at DESC NULLS LAST);

-- Twitch channel roster: known breaker logins whose live status is polled
-- via streams?user_login= (100/call). Twitch's Search Channels endpoint
-- matches channel names, not stream titles, so searching can't find live
-- breaks — the roster is the only reliable Twitch discovery.
CREATE TABLE IF NOT EXISTS twitch_channels (
    login           TEXT PRIMARY KEY,            -- lowercased Twitch login
    display_name    TEXT,                        -- channel display name
    source          TEXT NOT NULL DEFAULT 'manual'
                    CHECK (source IN ('search', 'manual', 'seed')),
    added_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_checked_at TIMESTAMPTZ,                 -- last roster poll that covered it
    last_hit_at     TIMESTAMPTZ,                 -- last poll where it produced a kept break
    active          BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_twitch_channels_active   ON twitch_channels (active);
CREATE INDEX IF NOT EXISTS idx_twitch_channels_last_hit ON twitch_channels (last_hit_at DESC NULLS LAST);

-- Community-suggested breakers. Suggestions land here as 'pending'; a
-- reviewer (Brian) approves them into youtube_channels as source='manual'
-- via the admin page, or rejects them. Nothing here touches the roster
-- until approved.
CREATE TABLE IF NOT EXISTS breaker_suggestions (
    id            SERIAL PRIMARY KEY,
    input_text    TEXT NOT NULL,                     -- what the visitor typed: handle, URL, or name
    note          TEXT,                              -- optional visitor note
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'approved', 'rejected')),
    channel_id    TEXT,                              -- UC id, filled on approval
    reviewer_note TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_breaker_suggestions_status ON breaker_suggestions (status);

-- User accounts (optional perks: favorite breakers, saved searches).
-- Browsing stays free; accounts only unlock personal features.
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,          -- stored lowercased
    password_hash TEXT NOT NULL,                 -- bcrypt
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS user_favorites (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    breaker    TEXT NOT NULL,                    -- breaker display name
    channel_id TEXT,                             -- YouTube UC id when known
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
    region     TEXT,                        -- 'us' | 'intl' | NULL (= all regions)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE saved_searches ADD COLUMN IF NOT EXISTS region TEXT;
CREATE INDEX IF NOT EXISTS idx_saved_searches_user ON saved_searches (user_id);

-- Canonical products + known title aliases for normalization.
CREATE TABLE IF NOT EXISTS products (
    id             SERIAL PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,        -- e.g. '2024 Panini Prizm Football Hobby'
    sport          TEXT,
    aliases        TEXT[] NOT NULL DEFAULT '{}' -- lowercase alias strings matched as substrings
);

-- Migrate check constraints on existing databases (idempotent).
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
            CHECK (format IN ('pyt','random','division','hit_draft','personal','case_break','group_break','team_break','player_break','box_break','unknown'));
    END IF;
END $$;
