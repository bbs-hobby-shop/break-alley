"""Stripe billing for BreakAlley Pro (Brian 2026-10-08).

Recurring subscription: $4.99/month. Uses Stripe Checkout for signup,
the Customer Portal for management, and webhooks to keep is_pro in sync.

Env vars: STRIPE_SECRET_KEY, STRIPE_PUBLISHABLE_KEY, STRIPE_WEBHOOK_SECRET,
STRIPE_PRO_PRICE_ID.
"""
import logging
import os

log = logging.getLogger("breakalley.billing")

PRO_PRICE_MONTHLY = 499  # cents


def _client():
    import stripe

    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY not configured")
    stripe.api_key = key
    return stripe


def publishable_key() -> str:
    return os.environ.get("STRIPE_PUBLISHABLE_KEY", "")


def pro_price_id() -> str:
    return os.environ.get("STRIPE_PRO_PRICE_ID", "")


def create_checkout_session(user_id: int, email: str, base_url: str) -> str:
    """Create a Stripe Checkout session for Pro. Returns the checkout URL."""
    stripe = _client()
    price_id = pro_price_id()
    if not price_id:
        raise RuntimeError("STRIPE_PRO_PRICE_ID not configured")
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer_email=email,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{base_url}/pro/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base_url}/pro",
        metadata={"user_id": str(user_id)},
    )
    return session.url


def create_portal_session(stripe_customer_id: str, base_url: str) -> str:
    """Create a Customer Portal session for managing the subscription."""
    stripe = _client()
    session = stripe.billing_portal.Session.create(
        customer=stripe_customer_id,
        return_url=f"{base_url}/account",
    )
    return session.url


def verify_webhook(payload: bytes, sig_header: str):
    """Verify and parse a Stripe webhook. Returns the event."""
    stripe = _client()
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET not configured")
    return stripe.Webhook.construct_event(payload, sig_header, secret)


def sync_subscription(conn, event) -> None:
    """Update users.is_pro from a Stripe subscription webhook event."""
    # Stripe SDK returns an Event object; convert to a plain dict.
    if hasattr(event, "to_dict"):
        event = event.to_dict()
    if not isinstance(event, dict):
        log.warning("billing: unexpected event type %s", type(event))
        return
    etype = event.get("type", "")
    data = event.get("data", {}) or {}
    obj = data.get("object", {}) or {}

    if etype == "checkout.session.completed":
        # Link the Stripe customer to our user.
        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription")
        user_id = (obj.get("metadata") or {}).get("user_id")
        if customer_id and user_id:
            conn.execute(
                """
                UPDATE users SET stripe_customer_id = %s,
                    stripe_subscription_id = %s, is_pro = TRUE,
                    pro_expires_at = NULL
                WHERE id = %s
                """,
                (customer_id, subscription_id, int(user_id)),
            )
            log.info("billing: user %s became Pro (customer %s)", user_id, customer_id)
        return

    if etype in ("customer.subscription.updated", "customer.subscription.deleted"):
        customer_id = obj.get("customer")
        status = obj.get("status", "")
        # Active/trialing = Pro. Anything else = not Pro.
        is_pro = status in ("active", "trialing")
        # If scheduled to cancel at period end, keep Pro until then but record the date.
        cancel_at_period_end = obj.get("cancel_at_period_end", False)
        cancel_at = obj.get("cancel_at")  # Unix timestamp or None
        pro_expires_at = None
        if cancel_at_period_end and cancel_at:
            from datetime import datetime, timezone
            pro_expires_at = datetime.fromtimestamp(cancel_at, tz=timezone.utc)
        if customer_id:
            conn.execute(
                "UPDATE users SET is_pro = %s, stripe_subscription_id = %s, "
                "pro_expires_at = %s WHERE stripe_customer_id = %s",
                (is_pro, obj.get("id"), pro_expires_at, customer_id),
            )
            log.info("billing: customer %s pro=%s (status %s, cancel_at_end=%s)",
                     customer_id, is_pro, status, cancel_at_period_end)
