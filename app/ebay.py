"""eBay ingestion via the official Browse API (Buy APIs).

Auth: OAuth2 client-credentials flow using the app's App ID + Cert ID
(read ONLY from env: EBAY_APP_ID / EBAY_CERT_ID — never hardcoded, never logged).

Endpoints:
  Token:  POST https://api.ebay.com/identity/v1/oauth2/token
          (HTTP Basic with base64(app_id:cert_id),
           form: grant_type=client_credentials
                 &scope=https://api.ebay.com/api/persistence)
  Search: GET https://api.ebay.com/buy/browse/v1/item_summary/search
          (Bearer token, header X-EBAY-C-MARKETPLACE-ID: EBAY_US)

No live calls are made by this module on import. Call run_ebay_ingest()
explicitly (see ingest.py).
"""
import base64
import time
import urllib.parse

import httpx

from . import config

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"
MARKETPLACE = "EBAY_US"

# (query, sport_hint) pairs polled on each run. Cheap, broad queries first.
SEARCH_QUERIES = [
    ("box break", None),
    ("card break", None),
    ("football box break PYT", "football"),
    ("basketball box break PYT", "basketball"),
    ("baseball box break PYT", "baseball"),
    ("soccer box break PYT", "soccer"),
    ("hockey box break PYT", "hockey"),
]

_token_cache: dict = {}


def get_app_token() -> str:
    """Fetch (and cache until expiry) an OAuth2 application token."""
    if not config.ebay_configured():
        raise RuntimeError("EBAY_APP_ID / EBAY_CERT_ID are not set in the environment.")
    now = time.time()
    if _token_cache.get("expires_at", 0) > now + 60:
        return _token_cache["token"]

    basic = base64.b64encode(
        f"{config.EBAY_APP_ID}:{config.EBAY_CERT_ID}".encode()
    ).decode()
    resp = httpx.post(
        TOKEN_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
        },
        data={
            "grant_type": "client_credentials",
            "scope": OAUTH_SCOPE,
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 7200))
    return _token_cache["token"]


def search_items(query: str, token: str, limit: int = 200) -> list[dict]:
    """Run one Browse API item_summary search. Returns raw itemSummary dicts."""
    params = {"q": query, "limit": min(limit, 200)}
    resp = httpx.get(
        SEARCH_URL,
        params=params,
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("itemSummaries", []) or []


def get_item(item_id: str, token: str) -> dict | None:
    """Fetch full item details via Browse API getItem. Returns dict or None."""
    url = f"https://api.ebay.com/buy/browse/v1/item/{item_id}"
    try:
        resp = httpx.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"getItem failed for {item_id}: {e}")
        return None


def build_affiliate_url(item_url: str | None) -> str | None:
    """Wrap an eBay item URL in an eBay Partner Network rover link when a
    campaign id is configured; otherwise return the plain URL."""
    if not item_url:
        return None
    if not config.EBAY_CAMPID:
        return item_url
    return (
        "https://rover.ebay.com/rover/1/711-53200-19255-0/1"
        f"?campid={urllib.parse.quote(config.EBAY_CAMPID)}"
        "&toolid=10001&customid=boxbreaks"
        f"&mpre={urllib.parse.quote(item_url, safe='')}"
    )


def load_ebay_roster() -> set[str]:
    """Approved eBay seller usernames (lowercased). Empty set = no filtering."""
    from pathlib import Path
    path = Path(__file__).with_name("seed_ebay_sellers.txt")
    sellers = set()
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                # Allow trailing comments
                sellers.add(line.split("#", 1)[0].strip().lower())
    return {s for s in sellers if s}


def fetch_all_break_listings() -> list[dict]:
    """Run every configured search query. Returns raw itemSummary dicts.

    When the eBay seller roster is non-empty, only items from roster sellers
    are returned (roster-only principle, same as YouTube/Twitch).
    """
    token = get_app_token()
    roster = load_ebay_roster()
    if roster:
        print(f"ebay: roster-only mode, {len(roster)} approved sellers")
    seen: dict[str, dict] = {}
    n_skipped = 0
    for query, _sport_hint in SEARCH_QUERIES:
        for item in search_items(query, token):
            seller = ((item.get("seller") or {}).get("username") or "").lower()
            if roster and seller not in roster:
                n_skipped += 1
                continue
            item_id = item.get("itemId") or item.get("itemWebUrl")
            if item_id and item_id not in seen:
                seen[item_id] = item
    if n_skipped:
        print(f"ebay: skipped {n_skipped} listings from non-roster sellers")
    return list(seen.values())
