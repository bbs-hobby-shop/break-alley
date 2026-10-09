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


def send_push(subscription: dict, title: str, body: str, url: str = "/") -> bool:
    """Send one push notification. Returns True on success."""
    v = _vapid()
    if not v["private"] or not v["public"]:
        log.warning("push: VAPID keys not configured, skipping")
        return False
    try:
        from pywebpush import webpush, WebPushException

        webpush(
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
            },
            data=json.dumps({"title": title, "body": body, "url": url}),
            vapid_private_key=v["private"],
            vapid_claims={"sub": v["subject"]},
        )
        return True
    except Exception as e:
        # 410 Gone / 404 = subscription expired, caller should delete it
        log.info("push failed for %s: %s", subscription.get("endpoint", "?")[:40], e)
        return False


def notify_breaker_live(conn, breaker_name: str, break_title: str, break_url: str):
    """Notify all Pro users following this breaker that they're live.

    Called from pollers when a breaker transitions to live. Best-effort;
    failures are logged, expired subscriptions are pruned.
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
        return
    dead = []
    for r in rows:
        ok = send_push(
            {"endpoint": r["endpoint"], "p256dh": r["p256dh"], "auth": r["auth"]},
            title=f"{breaker_name} is LIVE",
            body=break_title,
            url=break_url,
        )
        if not ok:
            dead.append(r["id"])
    for sub_id in dead:
        try:
            conn.execute("DELETE FROM push_subscriptions WHERE id = %s", (sub_id,))
        except Exception:
            pass
    log.info("push: notified %d devices for %s (%d dead pruned)", len(rows) - len(dead), breaker_name, len(dead))


def check_and_notify_new_live(conn):
    """Find live breaks not yet notified, push to Pro followers, mark notified.

    Called after poller runs. Safe to run repeatedly; uses live_push_log
    to avoid duplicate notifications for the same break.
    """
    rows = conn.execute(
        """
        SELECT b.id, b.breaker, b.title_raw, b.source_url
        FROM breaks b
        LEFT JOIN live_push_log l ON l.break_id = b.id
        WHERE b.is_live = TRUE AND l.break_id IS NULL
        LIMIT 50
        """
    ).fetchall()
    for r in rows:
        try:
            notify_breaker_live(conn, r["breaker"] or "A breaker", r["title_raw"], r["source_url"])
            conn.execute(
                "INSERT INTO live_push_log (break_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (r["id"],),
            )
        except Exception as e:
            log.warning("push check failed for break %s: %s", r["id"], e)
    if rows:
        log.info("push: checked %d newly-live breaks", len(rows))


# ---------------------------------------------------------------------------
# Per-type notification preferences (Brian 2026-10-09)
# ---------------------------------------------------------------------------

def get_push_prefs(conn, user_id: int) -> dict:
    """Return {"live_alerts": bool, "starting_soon": bool}. No row = all on."""
    try:
        row = conn.execute(
            "SELECT live_alerts, starting_soon FROM push_prefs WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    except Exception:
        row = None
    if not row:
        return {"live_alerts": True, "starting_soon": True}
    return {"live_alerts": bool(row["live_alerts"]),
            "starting_soon": bool(row["starting_soon"])}


def set_push_prefs(conn, user_id: int, live_alerts: bool, starting_soon: bool) -> None:
    conn.execute(
        """
        INSERT INTO push_prefs (user_id, live_alerts, starting_soon, updated_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (user_id) DO UPDATE
        SET live_alerts = EXCLUDED.live_alerts,
            starting_soon = EXCLUDED.starting_soon,
            updated_at = NOW()
        """,
        (user_id, live_alerts, starting_soon),
    )


# ---------------------------------------------------------------------------
# Starting-soon reminders (Brian 2026-10-09)
# ---------------------------------------------------------------------------

def check_and_notify_starting_soon(conn, minutes_ahead: int = 30):
    """Remind Pro users when a followed/saved break starts within minutes_ahead.

    Called after poller runs alongside the live check. One reminder per
    break/user pair (starting_soon_log). Respects the starting_soon pref.
    Best-effort; never raises.
    """
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT b.id AS break_id, b.breaker, b.title_raw,
                   b.source_url, b.starts_at, u.id AS user_id
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
    for r in rows:
        try:
            mins_left = max(1, int((r["starts_at"] - now).total_seconds() // 60))
        except Exception:
            mins_left = minutes_ahead
        subs = conn.execute(
            "SELECT id, endpoint, p256dh, auth FROM push_subscriptions "
            "WHERE user_id = %s",
            (r["user_id"],),
        ).fetchall()
        dead = []
        for s in subs:
            ok = send_push(
                {"endpoint": s["endpoint"], "p256dh": s["p256dh"],
                 "auth": s["auth"]},
                title=f"{r['breaker'] or 'A breaker'} starts in {mins_left} min",
                body=r["title_raw"] or "",
                url=r["source_url"] or "/",
            )
            if ok:
                total_sent += 1
            else:
                dead.append(s["id"])
        for sub_id in dead:
            try:
                conn.execute("DELETE FROM push_subscriptions WHERE id = %s",
                             (sub_id,))
                total_dead += 1
            except Exception:
                pass
        try:
            conn.execute(
                "INSERT INTO starting_soon_log (break_id, user_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (r["break_id"], r["user_id"]),
            )
        except Exception:
            pass
    log.info("push: starting-soon reminders for %d break/user pairs "
             "(%d sent, %d dead pruned)", len(rows), total_sent, total_dead)
