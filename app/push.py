"""Web Push notifications for BreakAlley Pro (Brian 2026-10-08).

Uses the Web Push protocol (RFC 8030) via pywebpush. VAPID keys come from
env vars: VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_SUBJECT.
"""
import json
import logging
import os

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
        WHERE u.is_pro = TRUE AND uf.breaker = %s
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
