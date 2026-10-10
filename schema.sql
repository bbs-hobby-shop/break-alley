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
    sport           TEXT CHECK (sport IN ('football','basketball','baseball','soccer','hockey','tcg','racing','wrestling','golf','tennis','other')),
    sports          TEXT[],   -- all matched sports (Brian 2026-10-09); card shows one tag per sport
    formats         TEXT[],   -- all matched formats (Brian 2026-10-09); card shows one tag per format
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

-- Populate release detail fields (Brian 2026-10-08): researched 2026-10-09
-- from Topps/Panini/Upper Deck official pages and hobby sources.
-- Idempotent: only fills in NULL fields, safe to re-run.
UPDATE release_calendar SET
  manufacturer = 'Topps', sport = 'Baseball',
  description = 'A&G returns with its signature blend of baseball and beyond — a 300-card base set spanning MLB stars, rookies, and non-sport personalities. New inserts include Mini Musical Methods, N43, and Career 250. Rip Cards hide mini cards inside, and 1/1 Cut Signatures feature historical figures.',
  box_config = '18 packs per box, 8 cards per pack (144 cards)',
  key_hits = '2 autograph, relic, printing plate, or Rip Card cards per box on average. Chases: Mini Baseball Autos (Ohtani, Judge, Harper), Mini Non-Baseball Autos, 1/1 Cut Signatures (Lincoln, Ruth, Clemente, Mantle), rookie autos (Murakami, Konnor Griffin, Roman Anthony).',
  product_url = 'https://ripped.topps.com/2026-topps-allen-ginter-collector-cards-guide/'
WHERE product_name = '2026 Topps Allen & Ginter Baseball' AND manufacturer IS NULL;

UPDATE release_calendar SET
  manufacturer = 'Topps', sport = 'Baseball',
  description = 'Topps'' premium high-end baseball release: a compact 100-card base set built around on-card autographs, metal-framed signatures, autograph relics, and jumbo game-used memorabilia presented as display pieces.',
  box_config = '1 pack per box, 8 cards per pack',
  key_hits = '1 autograph + 1 autograph relic + 1 relic per box on average. Chases: Museum Framed Autographs, Atelier Autographed Books, Jumbo Bat Nameplate 1/1s, Momentous Material Jumbo Patch Autos.',
  product_url = 'https://ripped.topps.com/2026-topps-museum-collection-baseball-guide/'
WHERE product_name = '2026 Topps Museum Collection Baseball' AND manufacturer IS NULL;

UPDATE release_calendar SET
  manufacturer = 'Upper Deck', sport = 'Hockey',
  description = 'Upper Deck''s flagship hockey season opener: the first 250 cards of the 2026-27 base set (198 veterans, 49 Young Guns rookies, 3 checklists). The Young Guns class is led by Porter Martone, Anton Frondell, James Hagens, and Cole Hutson.',
  box_config = '12 packs per box, 12 cards per pack, 12 boxes per case',
  key_hits = '6 Young Guns rookie cards, 1 numbered/short-print/printing plate, 1 Outburst Silver, 4 UD Canvas, 1 Blue Dazzlers per box on average. Parallel ladder: Deluxe /250, Exclusives /100, High Gloss /10, Outburst Gold 1/1.',
  product_url = 'https://upperdeck.com/2026-27-upper-deck-series-one/'
WHERE product_name = '2026-27 Upper Deck Series 1 Hockey' AND manufacturer IS NULL;

UPDATE release_calendar SET
  manufacturer = 'Topps', sport = 'Baseball',
  description = 'The final 2026 flagship release, closing Topps'' 75th-anniversary celebration: a 350-card base set of stars, top rookies, Future Stars, in-season call-ups, and traded players in new uniforms.',
  box_config = '21 packs per box, 12 cards per pack',
  key_hits = '1 autograph OR relic per box on average. Chases: Real One Autos, 75 Years of Topps Die-Cut Autos, Cover Athlete Autos, 1991 Topps Autos, City Connect relics. Rookies: JJ Wetherholt, Kevin McGonigle, Travis Bazzana.',
  product_url = 'https://ripped.topps.com/2026-topps-update-series-baseball-box-guide/'
WHERE product_name = '2026 Topps Update Series Baseball' AND manufacturer IS NULL;

UPDATE release_calendar SET
  manufacturer = 'Panini', sport = 'Basketball',
  description = 'Select returns with Optichrome technology but, for the first time, without NBA licensing — players appear in airbrushed images without team logos. A 400-card tiered base set (Concourse, Premier, Courtside, Mezzanine).',
  box_config = '12 packs per box, 5 cards per pack',
  key_hits = '3 autographs or memorabilia, 24 Base Prizms, 6 inserts/parallels per box on average. Chases: Colorgraphs, Rookie Jersey Autos (Edgecombe, Tre Johnson, Fears, Queen), Color Wheel and Stained Glass SSPs.'
WHERE product_name = '2025-26 Panini Select Basketball' AND manufacturer IS NULL;

UPDATE release_calendar SET
  manufacturer = 'Panini', sport = 'Soccer',
  description = 'The first-ever NWSL edition of Donruss Optic — Optichrome designs covering the National Women''s Soccer League, headlined by Marta, Temwa Chawinga, Trinity Rodman, and Sophia Wilson.',
  box_config = '16 packs per box, 5 cards per pack, 12 boxes per case',
  key_hits = '3 autographs or memorabilia, 16 inserts, 4 numbered parallels, 16 Rated Rookies per box on average. Chases: Downtown and Night Moves case hits, new Kismet case hit, Signature Series autos.',
  product_url = 'https://www.paniniamerica.net/2026-panini-donruss-optic-nwsl-trading-card-box-hobby.html'
WHERE product_name = '2026 Panini Donruss Optic NWSL Soccer' AND manufacturer IS NULL;

UPDATE release_calendar SET
  manufacturer = 'Topps', sport = 'Formula 1',
  description = 'The seventh straight season of flagship F1 Chrome: a 200-card base set with an expanded 22-driver grid plus F2/F3 talent, legends, and team content. New Prism Refractor ladder and Full Grid Refractor /22.',
  box_config = '20 packs per box, 4 cards per pack, 12 boxes per case',
  key_hits = '1 Chrome Autograph, 4 Prism Refractors, 3+ numbered parallels per box. Chases: Hamilton, Verstappen, Norris autos; Arvid Lindblad F1 Debut Patch Auto 1/1. The Grail: a 9-card program with a 24K gold 1/1 Grand Prize Auto.',
  product_url = 'https://ripped.topps.com/2026-topps-chrome-formula-1-what-box-is-best/'
WHERE product_name = '2026 Topps Chrome Formula 1' AND manufacturer IS NULL;

UPDATE release_calendar SET
  manufacturer = 'Topps', sport = 'Football',
  description = 'Heritage brings the 1976 Topps Football design to today''s game: a 400-card base set with Team Cards, League Leaders, and Super Bowl LX subsets. New inserts dig into 1976 lore: The Expansion, All-Pro Series, New Age Performers.',
  box_config = '20 packs per box, 8 cards per pack',
  key_hits = '1 autograph or relic per box on average. Chases: Real One Autos, Heritage Rookie Autos (Mendoza, Love, Simpson), Brady/Allen/Burrow autos, Walter Payton cut signatures.',
  product_url = 'https://www.topps.com/pages/topps-heritage-football'
WHERE product_name = '2026 Topps Heritage Football' AND manufacturer IS NULL;

UPDATE release_calendar SET
  manufacturer = 'Panini', sport = 'Football',
  description = 'Obsidian''s 2026 return with its trademark dark/black Opti-Chrome aesthetics and die-cut designs. Autographs from top collegiate players and retired NFL legends; Electric Etch parallels throughout.',
  box_config = '1 pack per box, 7 cards per pack, 12 boxes per case',
  key_hits = '1 patch autograph, 1 additional autograph, 2 memorabilia cards per box on average. Case hits: Black Color Blast and Black Stained Glass SSPs.',
  product_url = 'https://www.paniniamerica.net/2026-panini-obsidian-football-trading-card-box-hobby.html'
WHERE product_name = '2026 Panini Obsidian Football' AND manufacturer IS NULL;

UPDATE release_calendar SET
  manufacturer = 'Panini', sport = 'Football',
  description = 'Donruss Football''s 2026 flagship: a 200-card base set with a wide parallel rainbow; autographs from all-time greats and top collegiate players; inserts headlined by Campus Kings, Downtown, and Downtown Duos.',
  box_config = '12 packs per box, 10 cards per pack, 12 boxes per case',
  key_hits = '1 autograph, 1 memorabilia card, 12 parallels, 36 inserts per box on average.',
  product_url = 'https://www.paniniamerica.net/2026-panini-donruss-football-trading-card-box-hobby.html'
WHERE product_name = '2026 Panini Donruss Football' AND manufacturer IS NULL;

UPDATE release_calendar SET
  manufacturer = 'Panini', sport = 'Soccer',
  description = 'The first-ever Crown Royale release for soccer: the classic die-cut crown base design, Crystal Purple parallels, larger Crown Control die-cuts, hard-signed autographs, and the legendary Kaboom! SSP insert.',
  box_config = '1 pack per box, 8 cards per pack',
  key_hits = '1 autograph + 2 memorabilia cards per box on average, plus 2 base parallels and 1 insert. Chase: Kaboom! SSP.'
WHERE product_name = '2026 Panini Crown Royale NWSL Soccer' AND manufacturer IS NULL;

UPDATE release_calendar SET
  manufacturer = 'Topps', sport = 'Basketball',
  description = 'Topps Flagship Basketball: a 300-card base set introducing the deep 2026 rookie class — first official rookie cards of AJ Dybantsa (cover), Darryn Peterson, Cameron Boozer; first pack-pulled LeBron James 76ers and Giannis Heat cards.',
  box_config = '20 packs per box, 12 cards per pack, 12 boxes per case',
  key_hits = '1 autograph or relic per box on average. Chases: Real One Autos, Rookie Photo Shoot Autos, 1981-82 Topps Autos, new Clear Variations /10.'
WHERE product_name = '2026-27 Topps Basketball' AND manufacturer IS NULL;

UPDATE release_calendar SET
  manufacturer = 'Topps', sport = 'Football',
  description = 'Bowman University prospects in the classic Bowman''s Best Chrome style: a 100-card set covering 81 colleges, headlined by NIL stars Arch Manning, Dante Moore, Bryce Underwood. New Quad and Hexagon multi-signer autographs.',
  box_config = '4 packs per box, 10 cards per pack, 12 boxes per case (hobby-only)',
  key_hits = '4 autographs, 3 numbered parallels, 8 inserts per box. Per case: 1 Best of the Best or Campus Crests insert.',
  product_url = 'https://ripped.topps.com/2026-bowman-u-best-football-hobby-box-guide/'
WHERE product_name = '2026 Bowman U Best Football' AND manufacturer IS NULL;

UPDATE release_calendar SET
  manufacturer = 'Topps', sport = 'Football',
  description = 'Topps'' premium artistic football brand: bold canvas designs across stars, veterans, and the 2026 rookie class headlined by Fernando Mendoza. New Charged Particles and Immersion autographs.',
  box_config = '1 pack per box, 7 cards per pack, 8 boxes per case',
  key_hits = '1 autograph per box on average. Chases: Silver Signings, Dawn of Greatness, Genesis, Dual Rookie Autos, Gold Electricity parallels.',
  product_url = 'https://www.topps.com/pages/topps-inception-football'
WHERE product_name = '2026 Topps Inception Football' AND manufacturer IS NULL;

-- BreakAlley Pro (Brian 2026-10-08): paid subscription tier via Stripe.
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_pro BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS pro_expires_at TIMESTAMPTZ;

-- Web Push subscriptions (Brian 2026-10-08): one row per browser/device.
-- Pro feature: instant alerts when followed breakers go live.
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint   TEXT NOT NULL UNIQUE,   -- push service URL (per browser)
    p256dh     TEXT NOT NULL,          -- base64url client public key
    auth       TEXT NOT NULL,          -- base64url auth secret
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user ON push_subscriptions (user_id);

-- Tracks which live breaks already triggered push notifications (avoid dupes).
CREATE TABLE IF NOT EXISTS live_push_log (
    break_id    INTEGER PRIMARY KEY REFERENCES breaks(id) ON DELETE CASCADE,
    notified_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
