-- Box Break Aggregator schema (Postgres)
-- Run: psql "$DATABASE_URL" -f schema.sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS breaks (
    id              SERIAL PRIMARY KEY,
    source          TEXT NOT NULL CHECK (source IN ('ebay', 'youtube', 'twitch', 'fanatics', 'whatnot')),
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
ALTER TABLE breaks ADD COLUMN IF NOT EXISTS video_url TEXT;        -- link to live video/channel (eBay listings)
ALTER TABLE breaks ADD COLUMN IF NOT EXISTS video_platform TEXT;  -- YouTube, eBay Live, Facebook, etc.
ALTER TABLE breaks ADD COLUMN IF NOT EXISTS break_time_text TEXT; -- human-readable break time from listing
ALTER TABLE breaks ADD COLUMN IF NOT EXISTS video_links JSONB;    -- array of {url, platform} for multi-platform
ALTER TABLE breaks ADD COLUMN IF NOT EXISTS is_auction BOOLEAN NOT NULL DEFAULT FALSE;  -- eBay auction (vs Buy It Now)
ALTER TABLE breaks ADD COLUMN IF NOT EXISTS auction_ends_at TIMESTAMPTZ;  -- eBay auction end time (for countdown)
ALTER TABLE breaks ADD COLUMN IF NOT EXISTS current_bid NUMERIC(10,2);    -- eBay auction current bid price

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

-- Fanatics Live shop roster: approved breaker shops polled via the public
-- GraphQL API (liveStreams). Mirrors the Twitch roster approach — only
-- rostered shops' streams are ingested.
CREATE TABLE IF NOT EXISTS fanatics_shops (
    shop_id         TEXT PRIMARY KEY,            -- Fanatics shop UUID
    name            TEXT,                        -- shop display name
    slug            TEXT,                        -- URL slug for fanatics.live/shops/<slug>
    source          TEXT NOT NULL DEFAULT 'manual'
                    CHECK (source IN ('search', 'manual', 'seed')),
    added_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_checked_at TIMESTAMPTZ,                 -- last roster poll that covered it
    last_hit_at     TIMESTAMPTZ,                 -- last poll where it produced a kept break
    active          BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_fanatics_shops_active   ON fanatics_shops (active);
CREATE INDEX IF NOT EXISTS idx_fanatics_shops_last_hit ON fanatics_shops (last_hit_at DESC NULLS LAST);

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
-- Brian 2026-10-07: star on a listing saves THAT LISTING (not the breaker).
-- Breaker follows stay in user_favorites via the separate follow button.
CREATE TABLE IF NOT EXISTS saved_listings (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    break_id   INTEGER NOT NULL REFERENCES breaks(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, break_id)
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

-- First-party analytics (Brian 2026-10-07): event log for sales metrics —
-- outbound clicks per breaker, saves, follows, searches, signups, listing views.
-- Powers /admin/stats. No third-party trackers; all data stays in our DB.
CREATE TABLE IF NOT EXISTS analytics_events (
    id         SERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,  -- 'outbound_click' | 'listing_saved' | 'listing_unsaved'
                               -- | 'breaker_followed' | 'breaker_unfollowed'
                               -- | 'search' | 'signup' | 'break_viewed'
    user_id    INTEGER,         -- NULL for anonymous visitors
    breaker    TEXT,            -- breaker name when the event is breaker-scoped
    break_id   INTEGER,         -- break id when the event is listing-scoped
    platform   TEXT,            -- 'ebay' | 'youtube' | 'twitch' | 'fanatics'
    meta       JSONB,           -- extra context: search query/filters, referrer, etc.
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_events_type_time ON analytics_events (event_type, created_at);
CREATE INDEX IF NOT EXISTS idx_events_breaker ON analytics_events (breaker);
CREATE INDEX IF NOT EXISTS idx_events_break_id ON analytics_events (break_id);

-- Release calendar (Idea 2026-10-07): card product release dates that trigger
-- the extra YouTube roster pass. Editable via /admin — no code changes needed
-- to add/remove release nights. The pass reads from this table, not hardcoded dates.
CREATE TABLE IF NOT EXISTS release_calendar (
    id           SERIAL PRIMARY KEY,
    product_name TEXT NOT NULL,
    release_date DATE NOT NULL,
    notes        TEXT,                     -- source/confirmation note
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (product_name, release_date)
);
CREATE INDEX IF NOT EXISTS idx_release_calendar_date ON release_calendar (release_date);

-- Seed: confirmed October 2026 releases (Idea 2026-10-07). Dates verified
-- 2026-10-07 against DK Network, Bleacher Seats, CardAtlas, DA Card World,
-- CardPulse. Oct 30 products per the original brief. Idempotent.
INSERT INTO release_calendar (product_name, release_date, notes) VALUES
 ('2026 Topps Allen & Ginter Baseball', '2026-10-07', 'Confirmed: multiple calendars. First pass ran 2026-10-07.'),
 ('2026 Topps Museum Collection Baseball', '2026-10-07', 'From the original brief; first pass ran 2026-10-07.'),
 ('2026-27 Upper Deck Series 1 Hockey', '2026-10-07', 'Confirmed: Cardlines hobby release date.'),
 ('2026 Topps Update Series Baseball', '2026-10-14', 'Confirmed: DK Network, Bleacher Seats.'),
 ('2025-26 Panini Select Basketball', '2026-10-14', 'Confirmed: DK Network, Bleacher Seats.'),
 ('2026 Panini Donruss Optic NWSL Soccer', '2026-10-14', 'Confirmed: DK Network, Bleacher Seats.'),
 ('2026 Topps Chrome Formula 1', '2026-10-15', 'Confirmed: CardPulse officially announced.'),
 ('2026 Topps Heritage Football', '2026-10-21', 'Confirmed: DA Card World presell Oct 21, 2026.'),
 ('2026 Panini Obsidian Football', '2026-10-21', 'Confirmed: Bleacher Seats 10/21/26.'),
 ('2026 Panini Donruss Football', '2026-10-28', 'Confirmed: Bleacher Seats 10/28/26.'),
 ('2026 Panini Crown Royale NWSL Soccer', '2026-10-28', 'Confirmed: Bleacher Seats, CardPulse.'),
 ('2026-27 Topps Basketball', '2026-10-28', 'Confirmed: Bleacher Seats 10/28/26.'),
 ('2026 Bowman U Best Football', '2026-10-30', 'From the original brief.'),
 ('2026 Topps Inception Football', '2026-10-30', 'From the original brief.')
ON CONFLICT (product_name, release_date) DO NOTHING;

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

-- Whatnot show submissions (Brian 2026-10-08): sellers voluntarily submit
-- their upcoming Whatnot shows; Brian approves them in /admin/whatnot and
-- they go live in the breaks table with source='whatnot'.
CREATE TABLE IF NOT EXISTS whatnot_show_submissions (
    id              SERIAL PRIMARY KEY,
    seller_username TEXT NOT NULL,
    show_title      TEXT NOT NULL,
    show_url        TEXT NOT NULL,
    starts_at       TIMESTAMPTZ NOT NULL,
    format          TEXT CHECK (format IN ('pyt','random','division','hit_draft','personal','case_break','group_break','team_break','player_break','box_break','unknown')),
    description     TEXT,
    consent         BOOLEAN NOT NULL DEFAULT FALSE,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_whatnot_submissions_status ON whatnot_show_submissions (status);

-- Release detail pages (Brian 2026-10-08): each upcoming set gets a detail
-- page with manufacturer info, box configuration, and key hits.
ALTER TABLE release_calendar ADD COLUMN IF NOT EXISTS manufacturer TEXT;
ALTER TABLE release_calendar ADD COLUMN IF NOT EXISTS sport TEXT;
ALTER TABLE release_calendar ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE release_calendar ADD COLUMN IF NOT EXISTS box_config TEXT;
ALTER TABLE release_calendar ADD COLUMN IF NOT EXISTS key_hits TEXT;
ALTER TABLE release_calendar ADD COLUMN IF NOT EXISTS product_url TEXT;
