"""Web Push notifications for BreakAlley Pro (Brian 2026-10-08).

Uses the Web Push protocol (RFC 8030) via pywebpush. VAPID keys come from
env vars: VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_SUBJECT.
"""
import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("breakalley.push")


def _vapid():
    return {
        "public": os.environ.get("VAPID_PUBLIC_KEY", ""),
        "private": os.environ.get("VAPID_PRIVATE_KEY", ""),
        "subject": os.environ.get("VAPID_SUBJECT", "mailto:admin@breakalley.com"),
    }


def vapid_public_key() -> str:
    return _vapid()["public"]


def send_push(subscription: dict, title: str, body: str, url: str = "/",
             tag: str = "") -> str:
    """Send one push notification.

    Returns "ok" on success, "expired" when the push service says the
    subscription is gone (404/410 — safe to delete), or "failed" for any
    transient error (network blip, push-server hiccup — KEEP the
    subscription; deleting it here is what kept forcing re-enables).
    """
    v = _vapid()
    if not v["private"] or not v["public"]:
        log.warning("push: VAPID keys not configured, skipping")
        return "failed"
    try:
        from pywebpush import webpush, WebPushException

        webpush(
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
            },
            data=json.dumps({"title": title, "body": body, "url": url,
                             "tag": tag or "breakalley-push"}),
            vapid_private_key=v["private"],
            vapid_claims={"sub": v["subject"]},
        )
        return "ok"
    except Exception as e:
        # Only 410 Gone / 404 mean the subscription is dead. Everything else
        # (timeouts, 429s, 5xx) is transient — the subscription stays.
        try:
            from pywebpush import WebPushException

            if isinstance(e, WebPushException):
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status in (404, 410):
                    log.info("push expired for %s (HTTP %s)",
                             subscription.get("endpoint", "?")[:40], status)
                    return "expired"
        except Exception:
            pass
        log.info("push failed (transient) for %s: %s",
                 subscription.get("endpoint", "?")[:40], e)
        return "failed"


def notify_breaker_live(conn, breaker_name: str, break_title: str, break_url: str,
                        tag: str = "") -> int:
    """Notify all Pro users following this breaker that they're live.

    Called from pollers when a breaker transitions to live. Best-effort;
    failures are logged, expired subscriptions are pruned.
    Returns the number of devices successfully notified.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT ps.id, ps.endpoint, ps.p256dh, ps.auth, u.id AS user_id
        FROM push_subscriptions ps
        JOIN users u ON u.id = ps.user_id
        JOIN user_favorites uf ON uf.user_id = u.id
        LEFT JOIN push_prefs pp ON pp.user_id = u.id
        WHERE u.is_pro = TRUE AND uf.breaker = %s
          AND (pp.live_alerts IS NULL OR pp.live_alerts = TRUE)
        """,
        (breaker_name,),
    ).fetchall()
    if not rows:
        return 0
    dead = []
    for r in rows:
        result = send_push(
            {"endpoint": r["endpoint"], "p256dh": r["p256dh"], "auth": r["auth"]},
            title=f"{breaker_name} is LIVE",
            body=break_title,
            url=break_url,
            tag=tag,
        )
        if result == "expired":
            dead.append(r["id"])
    for sub_id in dead:
        try:
            conn.execute("DELETE FROM push_subscriptions WHERE id = %s", (sub_id,))
        except Exception:
            pass
    sent = len(rows) - len(dead)
    log.info("push: notified %d devices for %s (%d dead pruned)", sent, breaker_name, len(dead))
    return sent


# One live alert per breaker per live session (Brian 2026-10-09): a breaker
# with five live listings sends ONE "{breaker} is LIVE", not five.
LIVE_BREAKER_COOLDOWN_HOURS = 6


def check_and_notify_new_live(conn):
    """Find live breaks not yet notified, push to Pro followers, mark notified.

    One notification per breaker per live session — a breaker with five live
    listings sends ONE "{breaker} is LIVE", not five (Brian 2026-10-09).
    Called after poller runs. Safe to run repeatedly; uses live_push_log
    to avoid duplicate notifications for the same break.
    """
    rows = conn.execute(
        """
        SELECT b.id, b.breaker, b.title_raw, b.source_url
        FROM breaks b
        LEFT JOIN live_push_log l ON l.break_id = b.id
        WHERE b.is_live = TRUE AND l.break_id IS NULL
        LIMIT 200
        """
    ).fetchall()
    # Group by breaker: one notification per breaker, not per listing.
    by_breaker = {}
    for r in rows:
        by_breaker.setdefault(r["breaker"] or "A breaker", []).append(r)
    for breaker, blist in by_breaker.items():
        try:
            # Cooldown: if we already alerted for this breaker recently it's
            # the same live session — log the new break ids silently so they
            # don't retrigger, but don't push again.
            recent = conn.execute(
                """
                SELECT 1 FROM live_push_log l
                JOIN breaks b ON b.id = l.break_id
                WHERE b.breaker = %s
                  AND l.notified_at > NOW() - (%s * INTERVAL '1 hour')
                LIMIT 1
                """,
                (breaker, LIVE_BREAKER_COOLDOWN_HOURS),
            ).fetchone()
            if not recent:
                first = blist[0]
                sent = notify_breaker_live(
                    conn, breaker, first["title_raw"], first["source_url"],
                    tag=f"live-{breaker}",
                )
            else:
                sent = 1  # cooldown active: log silently, no push
            # Only mark notified if at least one device was reached — a
            # failed send retries on the next poller run instead of being
            # silently swallowed (Brian 2026-10-09).
            if sent > 0:
                for br in blist:
                    conn.execute(
                        "INSERT INTO live_push_log (break_id) VALUES (%s) "
                        "ON CONFLICT DO NOTHING",
                        (br["id"],),
                    )
        except Exception as e:
            log.warning("push check failed for breaker %s: %s", breaker, e)
    if rows:
        log.info("push: checked %d newly-live breaks", len(rows))


# ---------------------------------------------------------------------------
# Per-type notification preferences (Brian 2026-10-09)
# ---------------------------------------------------------------------------

def get_push_prefs(conn, user_id: int) -> dict:
    """Return per-type prefs. No row = all on."""
    try:
        row = conn.execute(
            "SELECT live_alerts, starting_soon, auction_ending, new_breaks "
            "FROM push_prefs WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    except Exception:
        row = None
    if not row:
        return {"live_alerts": True, "starting_soon": True,
                "auction_ending": True, "new_breaks": True}
    return {"live_alerts": bool(row["live_alerts"]),
            "starting_soon": bool(row["starting_soon"]),
            "auction_ending": bool(row["auction_ending"]),
            "new_breaks": bool(row["new_breaks"])}


def set_push_prefs(conn, user_id: int, live_alerts: bool, starting_soon: bool,
                   auction_ending: bool = True, new_breaks: bool = True) -> None:
    conn.execute(
        """
        INSERT INTO push_prefs (user_id, live_alerts, starting_soon,
                                auction_ending, new_breaks, updated_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
        ON CONFLICT (user_id) DO UPDATE
        SET live_alerts = EXCLUDED.live_alerts,
            starting_soon = EXCLUDED.starting_soon,
            auction_ending = EXCLUDED.auction_ending,
            new_breaks = EXCLUDED.new_breaks,
            updated_at = NOW()
        """,
        (user_id, live_alerts, starting_soon, auction_ending, new_breaks),
    )


def _send_to_user(conn, user_id: int, title: str, body: str, url: str, tag: str = "") -> tuple[int, int]:
    """Send a push to all of a user's devices. Returns (sent, dead_pruned)."""
    subs = conn.execute(
        "SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = %s",
        (user_id,),
    ).fetchall()
    sent, dead = 0, []
    for s in subs:
        result = send_push(
            {"endpoint": s["endpoint"], "p256dh": s["p256dh"], "auth": s["auth"]},
            title=title, body=body, url=url or "/", tag=tag,
        )
        if result == "ok":
            sent += 1
        elif result == "expired":
            dead.append(s["id"])
    for sub_id in dead:
        try:
            conn.execute("DELETE FROM push_subscriptions WHERE id = %s", (sub_id,))
        except Exception:
            pass
    return sent, len(dead)


# ---------------------------------------------------------------------------
# Starting-soon reminders (Brian 2026-10-09)
# ---------------------------------------------------------------------------

def check_and_notify_starting_soon(conn, minutes_ahead: int = 30):
    """Remind Pro users when a followed/saved break starts within minutes_ahead.

    Saved breaks notify per break; followed-only breakers collapse to one
    notification per breaker (Brian 2026-10-09). Called after poller runs
    alongside the live check. One reminder per break/user pair
    (starting_soon_log). Respects the starting_soon pref.
    Best-effort; never raises.
    """
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT b.id AS break_id, b.breaker, b.title_raw,
                   b.source_url, b.starts_at, u.id AS user_id,
                   EXISTS (SELECT 1 FROM saved_listings s
                           WHERE s.user_id = u.id AND s.break_id = b.id
                          ) AS is_saved
            FROM breaks b
            JOIN users u ON u.is_pro = TRUE
            JOIN push_subscriptions ps ON ps.user_id = u.id
            LEFT JOIN push_prefs pp ON pp.user_id = u.id
            LEFT JOIN starting_soon_log l
                   ON l.break_id = b.id AND l.user_id = u.id
            WHERE b.is_live = FALSE
              AND b.starts_at > NOW()
              AND b.starts_at <= NOW() + (%s * INTERVAL '1 minute')
              AND l.break_id IS NULL
              AND (pp.starting_soon IS NULL OR pp.starting_soon = TRUE)
              AND (
                    EXISTS (SELECT 1 FROM user_favorites uf
                            WHERE uf.user_id = u.id AND uf.breaker = b.breaker)
                 OR EXISTS (SELECT 1 FROM saved_listings s
                            WHERE s.user_id = u.id AND s.break_id = b.id)
              )
            """,
            (minutes_ahead,),
        ).fetchall()
    except Exception as e:
        log.warning("push: starting-soon query failed: %s", e)
        return
    if not rows:
        return
    now = datetime.now(timezone.utc)
    total_sent, total_dead = 0, 0

    def mins_left(r):
        try:
            return max(1, int((r["starts_at"] - now).total_seconds() // 60))
        except Exception:
            return minutes_ahead

    def log_pair(break_id, user_id):
        # Only dedup when at least one device was reached — a failed send
        # retries next run instead of being silently swallowed.
        try:
            conn.execute(
                "INSERT INTO starting_soon_log (break_id, user_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (break_id, user_id),
            )
        except Exception:
            pass

    # Saved breaks: specific per-break notification (the exception to
    # by-breaker grouping). Followed-only: one per breaker.
    grouped = {}
    for r in rows:
        if r["is_saved"]:
            m = mins_left(r)
            sent, dead = _send_to_user(
                conn, r["user_id"],
                title=f"{r['breaker'] or 'A breaker'} starts in {m} min",
                body=r["title_raw"] or "",
                url=r["source_url"] or "/",
                tag=f"soon-{r['break_id']}",
            )
            total_sent += sent
            total_dead += dead
            if sent > 0:
                log_pair(r["break_id"], r["user_id"])
        else:
            grouped.setdefault(
                (r["user_id"], r["breaker"] or "A breaker"), []).append(r)
    for (user_id, breaker), blist in grouped.items():
        blist.sort(key=mins_left)
        m = mins_left(blist[0])
        n = len(blist)
        title = (f"{breaker} starts in {m} min" if n == 1
                 else f"{breaker} has {n} breaks starting soon")
        body = blist[0]["title_raw"] or ""
        if n > 1:
            body += f" (soonest in {m} min)"
        sent, dead = _send_to_user(
            conn, user_id,
            title=title,
            body=body,
            url=blist[0]["source_url"] or "/",
            tag=f"soon-{breaker}",
        )
        total_sent += sent
        total_dead += dead
        if sent > 0:
            for br in blist:
                log_pair(br["break_id"], br["user_id"])
    log.info("push: starting-soon reminders for %d break/user pairs "
             "(%d sent, %d dead pruned)", len(rows), total_sent, total_dead)


# ---------------------------------------------------------------------------
# Auction-ending reminders (Brian 2026-10-09)
# ---------------------------------------------------------------------------

def check_and_notify_auction_ending(conn, minutes_ahead: int = 60):
    """Remind Pro users when a saved/followed eBay auction ends soon.

    Saved auctions notify per auction; followed-only breakers collapse to one
    notification per breaker (Brian 2026-10-09). DB-only (auction_ends_at is
    already in our database) — works during 429 throttles. One reminder per
    break/user pair (auction_ending_log). Respects the auction_ending pref.
    Best-effort; never raises.
    """
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT b.id AS break_id, b.breaker, b.title_raw,
                   b.source_url, b.auction_ends_at, b.current_bid,
                   u.id AS user_id,
                   EXISTS (SELECT 1 FROM saved_listings s
                           WHERE s.user_id = u.id AND s.break_id = b.id
                          ) AS is_saved
            FROM breaks b
            JOIN users u ON u.is_pro = TRUE
            JOIN push_subscriptions ps ON ps.user_id = u.id
            LEFT JOIN push_prefs pp ON pp.user_id = u.id
            LEFT JOIN auction_ending_log l
                   ON l.break_id = b.id AND l.user_id = u.id
            WHERE COALESCE(b.is_auction, FALSE) = TRUE
              AND b.auction_ends_at > NOW()
              AND b.auction_ends_at <= NOW() + (%s * INTERVAL '1 minute')
              AND l.break_id IS NULL
              AND (pp.auction_ending IS NULL OR pp.auction_ending = TRUE)
              AND (
                    EXISTS (SELECT 1 FROM user_favorites uf
                            WHERE uf.user_id = u.id AND uf.breaker = b.breaker)
                 OR EXISTS (SELECT 1 FROM saved_listings s
                            WHERE s.user_id = u.id AND s.break_id = b.id)
              )
            """,
            (minutes_ahead,),
        ).fetchall()
    except Exception as e:
        log.warning("push: auction-ending query failed: %s", e)
        return
    if not rows:
        return
    now = datetime.now(timezone.utc)
    total_sent, total_dead = 0, 0

    def mins_left(r):
        try:
            return max(1, int((r["auction_ends_at"] - now).total_seconds() // 60))
        except Exception:
            return minutes_ahead

    def bid_body(r):
        bid = r["current_bid"]
        return (r["title_raw"] or "") + (
            f" — current bid ${float(bid):,.2f}" if bid else " — no bids yet")

    def log_pair(break_id, user_id):
        # Only dedup when at least one device was reached — a failed send
        # retries next run instead of being silently swallowed.
        try:
            conn.execute(
                "INSERT INTO auction_ending_log (break_id, user_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (break_id, user_id),
            )
        except Exception:
            pass

    # Saved auctions: specific per-auction notification (the exception to
    # by-breaker grouping). Followed-only: one per breaker.
    grouped = {}
    for r in rows:
        if r["is_saved"]:
            m = mins_left(r)
            sent, dead = _send_to_user(
                conn, r["user_id"],
                title=f"{r['breaker'] or 'A breaker'} auction ends in {m} min",
                body=bid_body(r),
                url=r["source_url"] or "/",
                tag=f"auction-{r['break_id']}",
            )
            total_sent += sent
            total_dead += dead
            if sent > 0:
                log_pair(r["break_id"], r["user_id"])
        else:
            grouped.setdefault(
                (r["user_id"], r["breaker"] or "A breaker"), []).append(r)
    for (user_id, breaker), blist in grouped.items():
        blist.sort(key=mins_left)
        m = mins_left(blist[0])
        n = len(blist)
        title = (f"{breaker} auction ends in {m} min" if n == 1
                 else f"{breaker} has {n} auctions ending soon")
        body = bid_body(blist[0])
        if n > 1:
            body += f" (soonest in {m} min)"
        sent, dead = _send_to_user(
            conn, user_id,
            title=title,
            body=body,
            url=blist[0]["source_url"] or "/",
            tag=f"auction-{breaker}",
        )
        total_sent += sent
        total_dead += dead
        if sent > 0:
            for br in blist:
                log_pair(br["break_id"], br["user_id"])
    log.info("push: auction-ending reminders for %d break/user pairs "
             "(%d sent, %d dead pruned)", len(rows), total_sent, total_dead)


# ---------------------------------------------------------------------------
# New-break alerts (Brian 2026-10-09)
# ---------------------------------------------------------------------------

def check_and_notify_new_breaks(conn):
    """Notify Pro followers when a followed breaker lists a new break.

    "New" is tracked in break_first_seen by (source, source_url) identity —
    pollers wipe/rewrite their slices, so the breaks row itself can't be
    trusted for first-seen. The very first check run primes silently (records
    everything, notifies nothing) so pre-existing breaks don't spam.
    Respects the new_breaks pref. Best-effort; never raises.
    """
    try:
        cands = conn.execute(
            """
            SELECT DISTINCT b.id, b.source, b.source_url, b.breaker, b.title_raw
            FROM breaks b
            WHERE NOT COALESCE(b.is_live, FALSE)
              AND (b.starts_at IS NULL OR b.starts_at > NOW() - INTERVAL '2 hours')
            """
        ).fetchall()
    except Exception as e:
        log.warning("push: new-break query failed: %s", e)
        return
    if not cands:
        return
    try:
        primed = conn.execute(
            "SELECT 1 FROM break_first_seen LIMIT 1").fetchone() is not None
    except Exception:
        return
    new_ids = set()
    for c in cands:
        try:
            r = conn.execute(
                """INSERT INTO break_first_seen (source, source_url)
                   VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING source""",
                (c["source"], c["source_url"]),
            ).fetchone()
            if r:
                new_ids.add(c["id"])
        except Exception:
            continue
    if not primed:
        log.info("push: new-break check primed silently with %d breaks", len(cands))
        return
    if not new_ids:
        return
    # Group by breaker: one notification per breaker per run ("X has 3 new
    # breaks"), not one per listing (Brian 2026-10-09).
    by_breaker = {}
    for c in cands:
        if c["id"] not in new_ids or not c["breaker"]:
            continue
        by_breaker.setdefault(c["breaker"], []).append(c)
    total_sent, total_dead, total_pairs = 0, 0, 0
    for breaker, blist in by_breaker.items():
        ids = [c["id"] for c in blist]
        try:
            users = conn.execute(
                """
                SELECT DISTINCT u.id AS user_id
                FROM users u
                JOIN push_subscriptions ps ON ps.user_id = u.id
                JOIN user_favorites uf ON uf.user_id = u.id AND uf.breaker = %s
                LEFT JOIN push_prefs pp ON pp.user_id = u.id
                WHERE u.is_pro = TRUE
                  AND (pp.new_breaks IS NULL OR pp.new_breaks = TRUE)
                """,
                (breaker,),
            ).fetchall()
        except Exception:
            continue
        for u in users:
            try:
                logged = {r[0] for r in conn.execute(
                    "SELECT break_id FROM new_break_log "
                    "WHERE user_id = %s AND break_id = ANY(%s)",
                    (u["user_id"], ids)).fetchall()}
            except Exception:
                continue
            fresh = [c for c in blist if c["id"] not in logged]
            if not fresh:
                continue
            n = len(fresh)
            title = (f"{breaker} has a new break" if n == 1
                     else f"{breaker} has {n} new breaks")
            body = fresh[0]["title_raw"] or ""
            if n > 1:
                body += f" (+{n - 1} more)"
            sent, dead = _send_to_user(
                conn, u["user_id"],
                title=title,
                body=body,
                url=fresh[0]["source_url"] or "/",
                tag=f"newbreak-{breaker}",
            )
            total_sent += sent
            total_dead += dead
            total_pairs += 1
            # Only dedup when at least one device was reached — a failed
            # send retries next run instead of being silently swallowed.
            if sent > 0:
                try:
                    for c in fresh:
                        conn.execute(
                            "INSERT INTO new_break_log (break_id, user_id) "
                            "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                            (c["id"], u["user_id"]),
                        )
                except Exception:
                    pass
    log.info("push: new-break alerts for %d pairs (%d sent, %d dead pruned)",
             total_pairs, total_sent, total_dead)
