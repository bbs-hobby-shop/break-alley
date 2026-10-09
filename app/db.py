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
    affiliate_url, expires_at, channel_id, country, group_key,
    video_url, video_platform, break_time_text, video_links,
    is_auction, auction_ends_at, current_bid
) VALUES (
    %(source)s, %(source_url)s, %(breaker)s, %(product_raw)s, %(product_normalized)s,
    %(sport)s, %(format)s, %(price)s, %(currency)s, %(starts_at)s, %(is_live)s,
    %(slots_total)s, %(slots_remaining)s, %(thumbnail_url)s, %(title_raw)s,
    %(affiliate_url)s, %(expires_at)s, %(channel_id)s, %(country)s, %(group_key)s,
    %(video_url)s, %(video_platform)s, %(break_time_text)s, %(video_links)s,
    %(is_auction)s, %(auction_ends_at)s, %(current_bid)s
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
    channel_id = EXCLUDED.channel_id,
    country = COALESCE(EXCLUDED.country, breaks.country),
    group_key = EXCLUDED.group_key,
    video_url = COALESCE(EXCLUDED.video_url, breaks.video_url),
    video_platform = COALESCE(EXCLUDED.video_platform, breaks.video_platform),
    break_time_text = COALESCE(EXCLUDED.break_time_text, breaks.break_time_text),
    video_links = COALESCE(EXCLUDED.video_links, breaks.video_links),
    is_auction = EXCLUDED.is_auction,
    auction_ends_at = EXCLUDED.auction_ends_at,
    current_bid = EXCLUDED.current_bid;
"""


def upsert_break(conn, row: dict) -> None:
    # channel_id only exists on YouTube rows; default it so eBay/Twitch/demo
    # rows don't KeyError on the named param.
    row.setdefault("channel_id", None)
    # country is set by the YouTube roster poller (channel's home country);
    # other sources leave it NULL (= unknown region).
    row.setdefault("country", None)
    # group_key is set by the eBay normalizer for team-by-team listings;
    # other sources leave it NULL (no grouping).
    row.setdefault("group_key", None)
    row.setdefault("country", None)
    # video_url/video_platform/break_time_text are set by the eBay video
    # extractor; other sources leave them NULL.
    row.setdefault("video_url", None)
    row.setdefault("video_platform", None)
    row.setdefault("break_time_text", None)
    row.setdefault("video_links", None)
    # is_auction/auction_ends_at/current_bid are set by the eBay normalizer;
    # other sources leave them at their defaults (not an auction).
    row.setdefault("is_auction", False)
    row.setdefault("auction_ends_at", None)
    row.setdefault("current_bid", None)
    # video_links is a list of dicts; psycopg2 needs it as JSON string
    if row["video_links"] is not None and not isinstance(row["video_links"], str):
        import json as _json
        row["video_links"] = _json.dumps(row["video_links"])
    # Every kept break lands in a format category: titles with no specific
    # format signal fall into the generic 'box_break' bucket. (The site only
    # lists real box breaks, so this is always honest. looks_like_real_break
    # still sees the raw 'unknown' before this mapping — a *detected* format
    # remains a keep signal there.)
    if row.get("format") == "unknown":
        row["format"] = "box_break"
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


def channels_missing_country(conn, channel_ids: list[str]) -> list[str]:
    """Roster channel ids whose country is still unknown."""
    if not channel_ids:
        return []
    return [r["channel_id"] for r in conn.execute(
        "SELECT channel_id FROM youtube_channels "
        "WHERE channel_id = ANY(%s) AND country IS NULL",
        (channel_ids,),
    ).fetchall()]


def set_channel_countries(conn, mapping: dict[str, str | None]) -> int:
    """Stamp known 2-letter countries onto roster channels. Returns count."""
    n = 0
    for cid, country in mapping.items():
        if not country:
            continue
        conn.execute(
            "UPDATE youtube_channels SET country = %s "
            "WHERE channel_id = %s AND country IS NULL",
            (country.upper(), cid),
        )
        n += 1
    return n


def backfill_break_countries(conn) -> int:
    """Copy channel countries onto breaks rows still missing one.

    Idempotent: only touches breaks with NULL country whose channel now has
    a known country. Returns the number of rows updated.
    """
    res = conn.execute(
        """UPDATE breaks b SET country = yc.country
           FROM youtube_channels yc
           WHERE b.channel_id = yc.channel_id
             AND b.country IS NULL
             AND yc.country IS NOT NULL""",
    )
    return res.rowcount or 0


def get_roster_channels(conn, limit: int) -> list[dict]:
    """Active roster channels for one poll, hottest first.

    Ordering puts channels that recently produced kept breaks first, so when
    the roster is capped the most productive channels are the ones checked.
    Among never-hit channels, the least-recently-checked go first.
    """
    return conn.execute(
        """SELECT channel_id, title, country FROM youtube_channels
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


# ---------------------------------------------------------------------------
# Twitch channel roster (cheap per-channel live monitoring; see app/twitch_roster.py)
# ---------------------------------------------------------------------------

UPSERT_TWITCH_CHANNEL_SQL = """
INSERT INTO twitch_channels (login, display_name, source)
VALUES (%(login)s, %(display_name)s, %(source)s)
ON CONFLICT (login) DO UPDATE SET
    display_name = COALESCE(EXCLUDED.display_name, twitch_channels.display_name),
    last_hit_at = NOW();
"""


def upsert_twitch_channel(conn, login: str, display_name: str | None = None,
                          source: str = "manual") -> None:
    """Add a Twitch login to the roster (or refresh it) and stamp last_hit_at
    — the channel just produced a kept break."""
    login = (login or "").strip().lower()
    if not login:
        return
    conn.execute(UPSERT_TWITCH_CHANNEL_SQL, {
        "login": login, "display_name": display_name, "source": source,
    })


def mark_twitch_checked(conn, logins: list[str]) -> None:
    """Stamp last_checked_at for logins a roster poll actually covered."""
    logins = [(l or "").strip().lower() for l in logins or []]
    logins = [l for l in logins if l]
    if not logins:
        return
    conn.execute(
        "UPDATE twitch_channels SET last_checked_at = NOW() "
        "WHERE login = ANY(%s)",
        (logins,),
    )


def get_twitch_roster(conn, limit: int) -> list[dict]:
    """Active roster logins for one poll, hottest first.

    Ordering puts channels that recently produced kept breaks first, so when
    the roster is capped the most productive channels are the ones checked.
    Among never-hit channels, the least-recently-checked go first.
    """
    return conn.execute(
        """SELECT login, display_name FROM twitch_channels
           WHERE active
           ORDER BY last_hit_at DESC NULLS LAST,
                    last_checked_at ASC NULLS FIRST
           LIMIT %s""",
        (limit,),
    ).fetchall()


def seed_manual_twitch_channels(conn, lines: list[str]) -> int:
    """Upsert hand-picked Twitch logins (from app/seed_twitch_channels.txt) as
    source='manual'. Idempotent: safe to run at the start of every roster
    poll. Returns the number of channels added."""
    n = 0
    for line in lines:
        login = (line or "").strip()
        if not login or login.startswith("#"):
            continue
        # allow trailing "  # comment" on the line
        login = login.split("#", 1)[0].strip().split()[0].lower()
        if not login:
            continue
        cur = conn.execute(
            """INSERT INTO twitch_channels (login, source)
               VALUES (%(login)s, 'manual')
               ON CONFLICT (login) DO NOTHING""",
            {"login": login},
        )
        n += cur.rowcount
    return n


UPSERT_FANATICS_SHOP_SQL = """
INSERT INTO fanatics_shops (shop_id, name, slug, source)
VALUES (%(shop_id)s, %(name)s, %(slug)s, %(source)s)
ON CONFLICT (shop_id) DO UPDATE SET
    name = COALESCE(EXCLUDED.name, fanatics_shops.name),
    slug = COALESCE(EXCLUDED.slug, fanatics_shops.slug),
    last_hit_at = NOW();
"""


def upsert_fanatics_shop(conn, shop_id: str, name: str | None = None,
                         slug: str | None = None,
                         source: str = "manual") -> None:
    """Add a Fanatics shop to the roster (or refresh it) and stamp last_hit_at
    — the shop just produced a kept break."""
    shop_id = (shop_id or "").strip()
    if not shop_id:
        return
    conn.execute(UPSERT_FANATICS_SHOP_SQL, {
        "shop_id": shop_id, "name": name, "slug": slug, "source": source,
    })


def mark_fanatics_checked(conn, shop_ids: list[str]) -> None:
    """Stamp last_checked_at for shops a roster poll actually covered."""
    shop_ids = [(s or "").strip() for s in shop_ids or []]
    shop_ids = [s for s in shop_ids if s]
    if not shop_ids:
        return
    conn.execute(
        "UPDATE fanatics_shops SET last_checked_at = NOW() "
        "WHERE shop_id = ANY(%s)",
        (shop_ids,),
    )


def get_fanatics_roster(conn, limit: int) -> list[dict]:
    """Active roster shops for one poll, hottest first.

    Ordering puts shops that recently produced kept breaks first, so when
    the roster is capped the most productive shops are the ones checked.
    Among never-hit shops, the least-recently-checked go first.
    """
    return conn.execute(
        """SELECT shop_id, name FROM fanatics_shops
           WHERE active
           ORDER BY last_hit_at DESC NULLS LAST,
                    last_checked_at ASC NULLS FIRST
           LIMIT %s""",
        (limit,),
    ).fetchall()


def seed_manual_fanatics_shops(conn, lines: list[str],
                               id_by_name: dict[str, str]) -> int:
    """Upsert hand-picked Fanatics shops (from app/seed_fanatics_shops.txt) as
    source='manual'. Names are resolved to shop IDs via the live shops
    directory (id_by_name: lowercased name -> shop_id). Idempotent: safe to
    run at the start of every roster poll. Returns the number added."""
    n = 0
    for line in lines:
        name = (line or "").strip()
        if not name or name.startswith("#"):
            continue
        # allow trailing "  # comment" on the line
        name = name.split("#", 1)[0].strip()
        if not name:
            continue
        shop_id = id_by_name.get(name.lower())
        if not shop_id:
            continue
        cur = conn.execute(
            """INSERT INTO fanatics_shops (shop_id, name, source)
               VALUES (%(shop_id)s, %(name)s, 'manual')
               ON CONFLICT (shop_id) DO NOTHING""",
            {"shop_id": shop_id, "name": name},
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


def add_whatnot_submission(conn, seller_username: str, show_title: str,
                           show_url: str, starts_at, format: str | None = None,
                           description: str | None = None) -> int:
    """Queue a Whatnot seller's show submission for Brian's review (Brian 2026-10-08).
    Returns the new id. Consent is recorded as TRUE — the form requires the checkbox."""
    cur = conn.execute(
        """INSERT INTO whatnot_show_submissions
           (seller_username, show_title, show_url, starts_at, format, description, consent)
           VALUES (%(seller)s, %(title)s, %(url)s, %(starts)s, %(format)s, %(desc)s, TRUE)
           RETURNING id""",
        {"seller": seller_username.strip()[:100],
         "title": show_title.strip()[:300],
         "url": show_url.strip()[:500],
         "starts": starts_at,
         "format": format or "box_break",
         "desc": (description or "").strip()[:1000] or None},
    )
    return cur.fetchone()["id"]


def list_whatnot_submissions(conn, status: str | None = None) -> list[dict]:
    """Newest-first Whatnot show submissions, optionally filtered by status."""
    if status:
        rows = conn.execute(
            """SELECT * FROM whatnot_show_submissions WHERE status = %(status)s
               ORDER BY created_at DESC""",
            {"status": status},
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM whatnot_show_submissions ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def review_whatnot_submission(conn, submission_id: int, approved: bool) -> dict | None:
    """Approve or reject a Whatnot show submission. On approval, inserts the
    show into breaks with source='whatnot' (idempotent on show_url).
    Returns the submission dict."""
    subs = conn.execute(
        "SELECT * FROM whatnot_show_submissions WHERE id = %(sid)s",
        {"sid": submission_id},
    ).fetchone()
    if not subs:
        return None
    sub = dict(subs)
    if approved and sub["status"] == "pending":
        # Publish to the breaks feed (Brian 2026-10-08).
        conn.execute(
            """INSERT INTO breaks
               (source, source_url, breaker, title_raw, starts_at, format,
                is_live, country)
               VALUES ('whatnot', %(url)s, %(breaker)s, %(title)s, %(starts)s,
                       %(format)s, FALSE, 'US')
               ON CONFLICT (source, source_url) DO NOTHING""",
            {"url": sub["show_url"], "breaker": sub["seller_username"],
             "title": sub["show_title"], "starts": sub["starts_at"],
             "format": sub["format"] or "box_break"},
        )
    conn.execute(
        """UPDATE whatnot_show_submissions
           SET status = %(status)s, reviewed_at = NOW()
           WHERE id = %(sid)s""",
        {"status": "approved" if approved else "rejected", "sid": submission_id},
    )
    sub["status"] = "approved" if approved else "rejected"
    return sub


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
    """Delete the account; favorites cascade."""
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


# Saved listings (Brian 2026-10-07): the star on a card saves THAT LISTING.
# Separate from breaker follows (user_favorites).
def save_listing(conn, user_id: int, break_id: int) -> None:
    conn.execute(
        """INSERT INTO saved_listings (user_id, break_id)
           VALUES (%(user_id)s, %(break_id)s)
           ON CONFLICT (user_id, break_id) DO NOTHING""",
        {"user_id": user_id, "break_id": break_id},
    )


def unsave_listing(conn, user_id: int, break_id: int) -> None:
    conn.execute(
        "DELETE FROM saved_listings WHERE user_id = %(user_id)s AND break_id = %(break_id)s",
        {"user_id": user_id, "break_id": break_id},
    )


def saved_listing_ids(conn, user_id: int) -> set[int]:
    return {r["break_id"] for r in conn.execute(
        "SELECT break_id FROM saved_listings WHERE user_id = %(user_id)s",
        {"user_id": user_id},
    ).fetchall()}


def list_saved_listings(conn, user_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT b.*, s.created_at AS saved_at FROM saved_listings s
           JOIN breaks b ON b.id = s.break_id
           WHERE s.user_id = %(user_id)s ORDER BY s.created_at DESC""",
        {"user_id": user_id},
    ).fetchall()]


# Analytics events (Brian 2026-10-07): first-party sales metrics.
# Never raises — analytics must never break the user-facing request.
def log_event(conn, event_type: str, user_id: int | None = None,
              breaker: str | None = None, break_id: int | None = None,
              platform: str | None = None, meta: dict | None = None) -> None:
    try:
        import json
        conn.execute(
            """INSERT INTO analytics_events
               (event_type, user_id, breaker, break_id, platform, meta)
               VALUES (%(t)s, %(u)s, %(b)s, %(bid)s, %(p)s, %(m)s)""",
            {"t": event_type, "u": user_id, "b": breaker, "bid": break_id,
             "p": platform, "m": json.dumps(meta) if meta else None},
        )
    except Exception:
        pass


# Release calendar (Idea 2026-10-07): card product releases that trigger the
# extra YouTube roster pass. The pass reads dates from here — no hardcoded dates.
def get_release_products(conn, release_date) -> list[str]:
    """Product names releasing on a given date (for the release-night pass)."""
    return [r["product_name"] for r in conn.execute(
        "SELECT product_name FROM release_calendar WHERE release_date = %s ORDER BY product_name",
        (release_date,),
    ).fetchall()]


def list_upcoming_releases(conn, days: int = 60) -> list[dict]:
    """Releases from today forward (for the site display)."""
    return [dict(r) for r in conn.execute(
        """SELECT id, product_name, release_date, notes FROM release_calendar
           WHERE release_date >= CURRENT_DATE
           ORDER BY release_date, product_name LIMIT 50""",
    ).fetchall()]

def get_release(conn, release_id: int) -> dict | None:
    """Single release with full detail fields (Brian 2026-10-08: detail pages)."""
    row = conn.execute(
        """SELECT id, product_name, release_date, notes, manufacturer, sport,
                  description, box_config, key_hits, product_url
           FROM release_calendar WHERE id = %s""",
        (release_id,),
    ).fetchone()
    return dict(row) if row else None


def update_release_details(conn, release_id: int, manufacturer: str | None = None,
                           sport: str | None = None, description: str | None = None,
                           box_config: str | None = None, key_hits: str | None = None,
                           product_url: str | None = None) -> None:
    """Fill in the detail-page fields for a release (Brian 2026-10-08)."""
    conn.execute(
        """UPDATE release_calendar
           SET manufacturer = COALESCE(%(mfr)s, manufacturer),
               sport = COALESCE(%(sport)s, sport),
               description = COALESCE(%(desc)s, description),
               box_config = COALESCE(%(box)s, box_config),
               key_hits = COALESCE(%(hits)s, key_hits),
               product_url = COALESCE(%(url)s, product_url)
           WHERE id = %(rid)s""",
        {"mfr": manufacturer, "sport": sport, "desc": description,
         "box": box_config, "hits": key_hits, "url": product_url,
         "rid": release_id},
    )



def list_all_releases(conn) -> list[dict]:
    """Full calendar for the admin page."""
    return [dict(r) for r in conn.execute(
        """SELECT id, product_name, release_date, notes FROM release_calendar
           ORDER BY release_date DESC, product_name""",
    ).fetchall()]


def add_release(conn, product_name: str, release_date: str, notes: str | None = None) -> None:
    conn.execute(
        """INSERT INTO release_calendar (product_name, release_date, notes)
           VALUES (%(name)s, %(date)s, %(notes)s)
           ON CONFLICT (product_name, release_date) DO NOTHING""",
        {"name": product_name.strip(), "date": release_date, "notes": (notes or "").strip() or None},
    )


def remove_release(conn, release_id: int) -> None:
    conn.execute("DELETE FROM release_calendar WHERE id = %s", (release_id,))


def analytics_overview(conn, days: int = 30) -> dict:
    """Top-level counts for the stats dashboard."""
    r = conn.execute(
        """SELECT
             COUNT(*) FILTER (WHERE event_type = 'outbound_click'
                             AND created_at > NOW() - (%(d)s || ' days')::INTERVAL) AS clicks,
             COUNT(*) FILTER (WHERE event_type = 'break_viewed'
                             AND created_at > NOW() - (%(d)s || ' days')::INTERVAL) AS views,
             COUNT(*) FILTER (WHERE event_type = 'listing_saved'
                             AND created_at > NOW() - (%(d)s || ' days')::INTERVAL) AS saves,
             COUNT(*) FILTER (WHERE event_type = 'breaker_followed'
                             AND created_at > NOW() - (%(d)s || ' days')::INTERVAL) AS follows,
             COUNT(*) FILTER (WHERE event_type = 'search'
                             AND created_at > NOW() - (%(d)s || ' days')::INTERVAL) AS searches,
             COUNT(*) FILTER (WHERE event_type = 'signup'
                             AND created_at > NOW() - (%(d)s || ' days')::INTERVAL) AS signups,
             (SELECT COUNT(*) FROM users) AS users_total,
             (SELECT COUNT(*) FROM breaks) AS breaks_total
           FROM analytics_events""",
        {"d": days},
    ).fetchone()
    return dict(r) if r else {}


def analytics_breaker_leaderboard(conn, days: int = 30, limit: int = 100) -> list[dict]:
    """Per-breaker sales rollup: the table Brian uses for outreach."""
    return [dict(r) for r in conn.execute(
        """SELECT
             e.breaker,
             COUNT(*) FILTER (WHERE e.event_type = 'outbound_click') AS clicks,
             COUNT(*) FILTER (WHERE e.event_type = 'break_viewed') AS views,
             COUNT(*) FILTER (WHERE e.event_type = 'listing_saved') AS saves,
             COUNT(*) FILTER (WHERE e.event_type = 'breaker_followed') AS follows,
             (SELECT COUNT(DISTINCT b.id) FROM breaks b
              WHERE b.breaker = e.breaker
                AND b.fetched_at > NOW() - (%(d)s || ' days')::INTERVAL) AS breaks_listed
           FROM analytics_events e
           WHERE e.breaker IS NOT NULL
             AND e.created_at > NOW() - (%(d)s || ' days')::INTERVAL
           GROUP BY e.breaker
           ORDER BY clicks DESC, views DESC
           LIMIT %(limit)s""",
        {"d": days, "limit": limit},
    ).fetchall()]


def analytics_daily(conn, days: int = 30) -> list[dict]:
    """Per-day event counts for trend charts."""
    return [dict(r) for r in conn.execute(
        """SELECT DATE(created_at) AS day,
                  COUNT(*) FILTER (WHERE event_type = 'outbound_click') AS clicks,
                  COUNT(*) FILTER (WHERE event_type = 'break_viewed') AS views,
                  COUNT(*) FILTER (WHERE event_type = 'listing_saved') AS saves,
                  COUNT(*) FILTER (WHERE event_type = 'breaker_followed') AS follows,
                  COUNT(*) FILTER (WHERE event_type = 'search') AS searches,
                  COUNT(*) FILTER (WHERE event_type = 'signup') AS signups
           FROM analytics_events
           WHERE created_at > NOW() - (%(d)s || ' days')::INTERVAL
           GROUP BY DATE(created_at)
           ORDER BY day""",
        {"d": days},
    ).fetchall()]


def analytics_top_searches(conn, days: int = 30, limit: int = 25) -> list[dict]:
    """Most-searched terms — shows buyer demand for sales conversations."""
    return [dict(r) for r in conn.execute(
        """SELECT LOWER(meta->>'q') AS term, COUNT(*) AS n
           FROM analytics_events
           WHERE event_type = 'search'
             AND created_at > NOW() - (%(d)s || ' days')::INTERVAL
             AND meta->>'q' IS NOT NULL AND meta->>'q' <> ''
           GROUP BY LOWER(meta->>'q')
           ORDER BY n DESC
           LIMIT %(limit)s""",
        {"d": days, "limit": limit},
    ).fetchall()]


def analytics_platform_split(conn, days: int = 30) -> list[dict]:
    """Outbound clicks by platform."""
    return [dict(r) for r in conn.execute(
        """SELECT platform, COUNT(*) AS clicks
           FROM analytics_events
           WHERE event_type = 'outbound_click'
             AND created_at > NOW() - (%(d)s || ' days')::INTERVAL
           GROUP BY platform
           ORDER BY clicks DESC""",
        {"d": days},
    ).fetchall()]


def breaks_for_breakers(conn, breakers: list[str], limit: int = 100) -> list[dict]:
    """Live-first breaks from the given breaker names (for My Breakers)."""
    if not breakers:
        return []
    return [dict(r) for r in conn.execute(
        """SELECT id, source, source_url, breaker, product_raw, product_normalized,
                  sport, format, price, currency, starts_at, is_live,
                  slots_total, slots_remaining, thumbnail_url, title_raw,
                  is_auction, auction_ends_at, current_bid, break_time_text
           FROM breaks
           WHERE breaker = ANY(%(breakers)s)
           ORDER BY is_live DESC, starts_at NULLS LAST, fetched_at DESC
           LIMIT %(limit)s""",
        {"breakers": breakers, "limit": limit},
    ).fetchall()]




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
            CHECK (source IN ('ebay', 'youtube', 'twitch', 'fanatics', 'whatnot'));
        IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'breaks_format_check') THEN
            ALTER TABLE breaks DROP CONSTRAINT breaks_format_check;
        END IF;
        ALTER TABLE breaks ADD CONSTRAINT breaks_format_check
            CHECK (format IN ('pyt','random','division','hit_draft','personal','case_break','group_break','team_break','player_break','box_break','unknown'));
    END IF;
    -- eBay auction support (Brian 2026-10-07): auction listings with countdown
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'breaks') THEN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='breaks' AND column_name='is_auction') THEN
            ALTER TABLE breaks ADD COLUMN is_auction BOOLEAN NOT NULL DEFAULT FALSE;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='breaks' AND column_name='auction_ends_at') THEN
            ALTER TABLE breaks ADD COLUMN auction_ends_at TIMESTAMPTZ;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='breaks' AND column_name='current_bid') THEN
            ALTER TABLE breaks ADD COLUMN current_bid NUMERIC(10,2);
        END IF;
    END IF;
END $$;

-- Roster table for cheap per-channel Twitch live monitoring (schema.sql is
-- the canonical definition; this keeps cron pollers working on older DBs).
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
CREATE INDEX IF NOT EXISTS idx_twitch_channels_active ON twitch_channels (active);

-- Whatnot show submissions (Brian 2026-10-08): in migrations too so the
-- table exists even if schema.sql hasn't run yet on a given database.
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

-- Roster table for Fanatics Live shops (schema.sql is the canonical
-- definition; this keeps cron pollers working on older DBs). Polled via
-- the public GraphQL API at fanatics.live/graphql (Brian 2026-10-07).
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
CREATE INDEX IF NOT EXISTS idx_fanatics_shops_active ON fanatics_shops (active);

-- Roster table for cheap per-channel YouTube monitoring (schema.sql is the
-- canonical definition; this keeps cron pollers working on older DBs).
CREATE TABLE IF NOT EXISTS youtube_channels (
    channel_id      TEXT PRIMARY KEY,
    title           TEXT,
    source          TEXT NOT NULL DEFAULT 'search'
                    CHECK (source IN ('search', 'manual', 'seed')),
    country         TEXT,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_checked_at TIMESTAMPTZ,
    last_hit_at     TIMESTAMPTZ,
    active          BOOLEAN NOT NULL DEFAULT TRUE
);
ALTER TABLE youtube_channels ADD COLUMN IF NOT EXISTS country TEXT;
ALTER TABLE breaks ADD COLUMN IF NOT EXISTS country TEXT;
CREATE INDEX IF NOT EXISTS idx_breaks_country ON breaks (country);
ALTER TABLE breaks ADD COLUMN IF NOT EXISTS channel_id TEXT;
ALTER TABLE breaks ADD COLUMN IF NOT EXISTS group_key TEXT;
CREATE INDEX IF NOT EXISTS idx_breaks_group_key ON breaks (group_key);
ALTER TABLE breaks ADD COLUMN IF NOT EXISTS video_url TEXT;
ALTER TABLE breaks ADD COLUMN IF NOT EXISTS video_platform TEXT;
ALTER TABLE breaks ADD COLUMN IF NOT EXISTS break_time_text TEXT;
ALTER TABLE breaks ADD COLUMN IF NOT EXISTS video_links JSONB;
ALTER TABLE breaks ADD COLUMN IF NOT EXISTS video_checked_at TIMESTAMPTZ;
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

-- User accounts (optional perks: favorite breakers).
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
    region     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE saved_searches ADD COLUMN IF NOT EXISTS region TEXT;
CREATE INDEX IF NOT EXISTS idx_saved_searches_user ON saved_searches (user_id);

-- Release detail pages (Brian 2026-10-08): set info for clickable titles.
ALTER TABLE release_calendar ADD COLUMN IF NOT EXISTS manufacturer TEXT;
ALTER TABLE release_calendar ADD COLUMN IF NOT EXISTS sport TEXT;
ALTER TABLE release_calendar ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE release_calendar ADD COLUMN IF NOT EXISTS box_config TEXT;
ALTER TABLE release_calendar ADD COLUMN IF NOT EXISTS key_hits TEXT;
ALTER TABLE release_calendar ADD COLUMN IF NOT EXISTS product_url TEXT;
"""


def ensure_schema(conn) -> None:
    conn.execute(SCHEMA_MIGRATIONS)


SEARCH_BASE = """
-- Brian 2026-10-07: grouping happens IN SQL via window functions, so LIMIT
-- applies after dedup. (The old Python-side grouping after LIMIT 1000 let
-- eBay's 4k rows crowd out other platforms in the All view.)
SELECT id, source, source_url, breaker, product_raw, product_normalized,
       sport, format, price, currency, starts_at, is_live,
       slots_total, slots_remaining, thumbnail_url, title_raw, affiliate_url,
       country, group_key, video_url, video_platform, break_time_text, video_links,
       is_auction, auction_ends_at, current_bid, group_count
FROM (
  SELECT breaks.*,
         ROW_NUMBER() OVER (
           PARTITION BY COALESCE(group_key, 'ungrouped-' || id::text)
           ORDER BY price NULLS LAST, id
         ) AS rn,
         COUNT(*) OVER (
           PARTITION BY COALESCE(group_key, 'ungrouped-' || id::text)
         ) AS group_count
  FROM breaks
  WHERE (CAST(%(q)s AS TEXT) IS NULL OR title_raw ILIKE '%%' || CAST(%(q)s AS TEXT) || '%%'
         OR COALESCE(product_normalized, '') ILIKE '%%' || CAST(%(q)s AS TEXT) || '%%'
         OR COALESCE(breaker, '') ILIKE '%%' || CAST(%(q)s AS TEXT) || '%%')
    AND (CAST(%(sport)s AS TEXT) IS NULL OR sport = %(sport)s)
    AND (CAST(%(format)s AS TEXT) IS NULL OR format = %(format)s)
    AND (CAST(%(max_price)s AS NUMERIC) IS NULL OR price IS NULL OR price <= %(max_price)s)
    AND (CAST(%(source)s AS TEXT) IS NULL OR source = %(source)s)
    AND (CAST(%(live_only)s AS BOOLEAN) IS NULL OR is_live = %(live_only)s)
    AND (CAST(%(auction_only)s AS BOOLEAN) IS NULL OR is_auction = %(auction_only)s)
    AND (CAST(%(region)s AS TEXT) IS NULL
         OR (CAST(%(region)s AS TEXT) = 'us'
             AND (country IS NULL OR country = 'US'))
         OR (CAST(%(region)s AS TEXT) = 'intl'
             AND country IS NOT NULL AND country <> 'US'))
    -- Hide ended auctions and sold-out BIN immediately (Brian 2026-10-07):
    -- they disappear from the site the instant they end/sell, before the
    -- background prune deletes the rows.
    AND NOT (COALESCE(is_auction, FALSE) AND auction_ends_at IS NOT NULL
             AND auction_ends_at <= NOW())
    AND NOT (COALESCE(is_auction, FALSE) = FALSE
             AND COALESCE(slots_remaining, -1) = 0)
    -- Hide Whatnot shows long after they started (Brian 2026-10-08):
    -- submitted shows have no poller to clean them up, so they fade from
    -- results 6h after start (covers show duration + buffer).
    AND NOT (source = 'whatnot' AND starts_at IS NOT NULL
             AND starts_at < NOW() - INTERVAL '6 hours')
) ranked
WHERE rn = 1
"""

# Brian 2026-10-08: sort orders live in SQL now (were Python-side), so
# LIMIT/OFFSET paginate correctly and the real total can be counted cheaply.
# (The old LIMIT 2000 + Python slicing capped the displayed total at 2000.)
SORT_SQL = {
    "soonest": "starts_at NULLS LAST, id",
    "price_low": "price NULLS LAST, id",
    "price_high": "price DESC NULLS LAST, id",
    "newest": "id DESC",
    "live": "is_live DESC, starts_at NULLS LAST, id",
    "ending": "auction_ends_at NULLS LAST, id",
}
DEFAULT_SORT_SQL = "is_live DESC, starts_at NULLS LAST, fetched_at DESC, id"

COUNT_SEARCH_SQL = "SELECT COUNT(*) FROM (" + SEARCH_BASE + ") AS c;"


def search_breaks(conn, q=None, sport=None, format=None, max_price=None,
                  source=None, live_only=None, region=None, sort=None,
                  auction_only=None, limit=None, offset=None):
    # Brian 2026-10-07: eBay team-by-team dedup now happens in SQL
    # (ROW_NUMBER window fn), so LIMIT applies after grouping.
    # Brian 2026-10-08: sorting + pagination in SQL (were Python-side with a
    # hardcoded LIMIT 2000 that capped the displayed total). The real total
    # comes from count_search_breaks().
    order_by = SORT_SQL.get(sort, DEFAULT_SORT_SQL)
    sql = SEARCH_BASE + f"ORDER BY {order_by}"
    params = {
        "q": q, "sport": sport, "format": format, "max_price": max_price,
        "source": source, "live_only": live_only, "region": region,
        "auction_only": auction_only,
    }
    if limit:
        sql += " LIMIT %(limit)s"
        params["limit"] = limit
    if offset:
        sql += " OFFSET %(offset)s"
        params["offset"] = offset
    sql += ";"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def count_search_breaks(conn, q=None, sport=None, format=None, max_price=None,
                        source=None, live_only=None, region=None,
                        auction_only=None):
    """Real result total for the current filters (Brian 2026-10-08) —
    same filters + grouping as search_breaks, but just a COUNT."""
    return conn.execute(COUNT_SEARCH_SQL, {
        "q": q, "sport": sport, "format": format, "max_price": max_price,
        "source": source, "live_only": live_only, "region": region,
        "auction_only": auction_only,
    }).fetchone()["count"]


def get_break(conn, break_id: int):
    return conn.execute(
        "SELECT * FROM breaks WHERE id = %s", (break_id,)
    ).fetchone()


def purge_junk_breaks(conn) -> int:
    """Delete breaks whose titles match non-break patterns (casino, betting,
    giveaways, etc.). Self-healing: removes junk that slipped through before
    the filter was tightened. Returns the number deleted.

    NOTE: Filtering happens in Python, not SQL — PostgreSQL's POSIX regex
    doesn't support \\b word boundaries the same way Python does.
    """
    import re
    from .normalizer import NON_BREAK_TITLE_WORDS, GAMING_WORDS
    from .ebay import load_ebay_roster
    pattern = "|".join(f"(?:{p})" for p in NON_BREAK_TITLE_WORDS)
    gaming_pattern = "|".join(f"(?:{p})" for p in GAMING_WORDS)
    roster = load_ebay_roster()
    rows = conn.execute("SELECT id, title_raw, breaker, source FROM breaks").fetchall()
    junk_ids = []
    for r in rows:
        title = r["title_raw"] or ""
        breaker = r["breaker"] or ""
        source = r["source"] or ""
        # eBay: only roster sellers allowed (Brian 2026-10-06)
        if source == "ebay" and roster and breaker.lower() not in roster:
            junk_ids.append(r["id"])
        elif title and re.search(pattern, title, re.IGNORECASE):
            junk_ids.append(r["id"])
        elif breaker and re.search(gaming_pattern, breaker, re.IGNORECASE):
            # Gaming channels are never box-break channels (Brian 2026-10-08)
            junk_ids.append(r["id"])
        elif breaker and "test.live.us-seller" in breaker.lower():
            junk_ids.append(r["id"])
    if not junk_ids:
        return 0
    return conn.execute(
        "DELETE FROM breaks WHERE id = ANY(%(ids)s)",
        {"ids": junk_ids},
    ).rowcount


def prune_ended_ebay(conn, skip_vanished: bool = False) -> dict:
    """Remove ended/sold eBay listings (Brian 2026-10-07). Three rules:

    1. ended_auctions: auction end time passed (no grace — Brian 2026-10-07
       wants them gone immediately; the search query also hides them live).
    2. sold_out: Buy It Now listings whose quantity hit 0.
    3. vanished: not seen in the feed for 24h (96 missed 15-min poller runs)
       — safety net for anything that disappeared from eBay's search.

    skip_vanished=True (Brian 2026-10-08): when eBay throttles us and no
    fresh data comes in, the vanished rule would eventually delete the
    ENTIRE eBay catalog (everything goes >24h stale). Skip it during
    throttles; ended/sold rules are still safe (based on auction times,
    not feed freshness).

    Returns dict of counts per rule.
    """
    counts = {}
    counts["ended_auctions"] = conn.execute(
        """DELETE FROM breaks WHERE source='ebay'
           AND COALESCE(is_auction, FALSE)
           AND auction_ends_at IS NOT NULL
           AND auction_ends_at <= NOW()"""
    ).rowcount
    counts["sold_out"] = conn.execute(
        """DELETE FROM breaks WHERE source='ebay'
           AND NOT COALESCE(is_auction, FALSE)
           AND slots_remaining = 0"""
    ).rowcount
    if skip_vanished:
        counts["vanished"] = 0
    else:
        counts["vanished"] = conn.execute(
            """DELETE FROM breaks WHERE source='ebay'
               AND fetched_at < NOW() - INTERVAL '24 hours'"""
        ).rowcount
    return counts


def needs_video_info(conn, source_url: str) -> bool:
    """Check if an eBay listing has never had video info extraction attempted.

    Check-once semantics (Brian 2026-10-06): listings whose sellers include no
    video info would otherwise be re-fetched on EVERY 15-min poller run --
    hundreds of wasted Browse API getItem calls per run against eBay's
    ~5k/day application quota, which starves the search calls and fails the
    run. A listing is checked the first time the poller sees it; listings
    with no video info are never re-fetched.
    """
    row = conn.execute(
        "SELECT video_checked_at FROM breaks WHERE source_url = %(url)s",
        {"url": source_url},
    ).fetchone()
    # New listing (not in DB) or existing never checked
    return row is None or row["video_checked_at"] is None


def mark_video_checked(conn, source_url: str) -> None:
    """Record that video info extraction was attempted for a listing.

    Called after every extraction attempt (hit or miss) so the listing is
    never re-fetched. Must run AFTER upsert_break so new listings exist.
    """
    conn.execute(
        "UPDATE breaks SET video_checked_at = NOW() WHERE source_url = %(url)s",
        {"url": source_url},
    )


def update_video_info(conn, source_url: str, video_url: str | None,
                      video_platform: str | None, break_time_text: str | None) -> None:
    """Update video info for an existing break."""
    conn.execute(
        """UPDATE breaks SET video_url = %(vurl)s, video_platform = %(vplat)s,
           break_time_text = %(btt)s WHERE source_url = %(url)s""",
        {"vurl": video_url, "vplat": video_platform, "btt": break_time_text,
         "url": source_url},
    )
