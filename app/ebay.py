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
import sys
import time
import urllib.parse

import httpx

from . import config

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"

# Pagination safety cap per search_items query (Brian 2026-10-08): 10 pages
# x 200 results = 2,000 results per query, far above any realistic
# 10-seller batch, while bounding Browse API call volume.
MAX_PAGES = 10

# Brian 2026-10-08: the first paginated run got burst-throttled (429) —
# 10 back-to-back page fetches per query with zero delay. Be polite.
PAGE_DELAY = 1.0  # seconds between paginated page fetches


class EbayThrottled(Exception):
    """eBay is rate-limiting us (persistent 429)."""


def _browse_get(params: dict, token: str):
    """GET the Browse API with one 429 retry.

    Rate limits are transient: wait 5s and retry once. If still throttled,
    raise EbayThrottled so the caller can trip its circuit breaker instead
    of burning the whole 15-min run in backoff sleeps (2026-10-08: 35s of
    retries x 16 queries = a 9-minute hung run that risked overlapping the
    next cron).
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
    }
    resp = httpx.get(SEARCH_URL, params=params, headers=headers, timeout=30)
    if resp.status_code == 429:
        print("ebay: 429 rate-limited — waiting 5s and retrying once",
              file=sys.stderr)
        time.sleep(5)
        resp = httpx.get(SEARCH_URL, params=params, headers=headers, timeout=30)
        if resp.status_code == 429:
            raise EbayThrottled("eBay rate limit (429) persisted after retry")
    resp.raise_for_status()
    return resp
OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"
MARKETPLACE = "EBAY_US"

# Brian 2026-10-07: ROSTER-ONLY — keyword searches removed entirely.
# We search each approved seller directly via filter=sellers:{...}.

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


def search_items(query: str, token: str, limit: int = 200,
                 auction_only: bool = False,
                 sellers: list[str] | None = None) -> list[dict]:
    """Run one Browse API item_summary search, paginating through ALL pages.

    Returns raw itemSummary dicts.

    auction_only=True adds filter=buyingOptions:{AUCTION} so the returned
    items are auctions by construction (robust even if buyingOptions is
    absent from the response payload).

    sellers=[...] adds filter=sellers:{a|b|c} for roster-direct searching
    (Brian 2026-10-07 audit: keyword-only search missed rostered sellers'
    listings that didn't use the exact keywords).

    Brian 2026-10-08: paginate with offset — the old code took only the
    first 200 results per 10-seller batch, silently truncating before the
    break filter ran. MAX_PAGES bounds API usage (10 pages x 200 = 2,000
    results per query, far above any realistic 10-seller batch).
    """
    page_size = min(limit, 200)
    filters = []
    if auction_only:
        filters.append("buyingOptions:{AUCTION}")
    if sellers:
        sellers_str = "|".join(sellers)
        filters.append(f"sellers:{{{sellers_str}}}")
    filter_str = ",".join(filters) if filters else None

    all_items: list[dict] = []
    offset = 0
    for page in range(MAX_PAGES):
        if page > 0:
            time.sleep(PAGE_DELAY)  # don't burst-throttle eBay (429)
        params = {"q": query, "limit": page_size, "offset": offset}
        if filter_str:
            params["filter"] = filter_str
        data = _browse_get(params, token).json()
        items = data.get("itemSummaries", []) or []
        all_items.extend(items)
        total = data.get("total", 0) or 0
        # Stop when we've seen everything or the page came back short.
        if len(all_items) >= total or len(items) < page_size:
            break
        offset += page_size
    else:
        print(f"ebay: hit MAX_PAGES ({MAX_PAGES}) for query={query!r} "
              f"sellers={sellers} — results may be truncated",
              file=sys.stderr)
    return all_items


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


def fetch_all_break_listings() -> tuple[list[dict], dict]:
    """Search rostered sellers directly. Returns (raw itemSummary dicts, stats).

    Brian 2026-10-07: ROSTER-ONLY — no keyword searches outside the approved
    roster, on any platform. We search each approved seller directly via
    filter=sellers:{...} with a broad 'break' query (regular + auction
    variants). Nothing from outside the roster is ever queried.

    Brian 2026-10-08: circuit breaker — if eBay throttles us (persistent
    429), stop making API calls for the rest of the run instead of burning
    it in retries. stats["throttled"] counts throttled batches so the
    caller can fail the run visibly when no fresh data was fetched.
    """
    token = get_app_token()
    roster = load_ebay_roster()
    if not roster:
        print("ebay: roster is empty — add sellers to app/seed_ebay_sellers.txt.")
        return [], {"throttled": 0, "batches": 0}
    print(f"ebay: roster-only mode, {len(roster)} approved sellers")

    seen: dict[str, dict] = {}
    n_found = 0
    n_throttled = 0
    n_batches = 0
    sellers = sorted(roster)
    # eBay allows multiple sellers per filter; batch to stay under URL limits
    for i in range(0, len(sellers), 10):
        batch = sellers[i:i + 10]
        n_batches += 1
        try:
            # Regular listings from these sellers
            for item in search_items("break", token, sellers=batch):
                seller = ((item.get("seller") or {}).get("username") or "").lower()
                if seller not in roster:
                    continue  # safety: never trust the filter alone
                item_id = item.get("itemId") or item.get("itemWebUrl")
                if item_id and item_id not in seen:
                    seen[item_id] = item
                    n_found += 1
            # Auction listings from these sellers (Brian 2026-10-07: auction
            # box breaks were invisible because buyingOptions is not reliably
            # present in search responses)
            for item in search_items("break", token, auction_only=True, sellers=batch):
                seller = ((item.get("seller") or {}).get("username") or "").lower()
                if seller not in roster:
                    continue
                item_id = item.get("itemId") or item.get("itemWebUrl")
                if not item_id:
                    continue
                if item_id in seen:
                    seen[item_id]["_known_auction"] = True
                else:
                    item["_known_auction"] = True
                    seen[item_id] = item
                n_found += 1
        except EbayThrottled as exc:
            # Circuit breaker: eBay is throttling us — more calls are futile.
            # Stop fetching, keep what we have; the caller fails visibly if
            # nothing fresh came in.
            n_throttled += 1
            print(f"ebay: throttled on batch {n_batches} ({exc}) — "
                  f"stopping API calls for this run", file=sys.stderr)
            break
        except Exception as exc:
            print(f"ebay: seller batch failed ({exc}) — continuing",
                  file=sys.stderr)
            continue
    print(f"ebay: {n_found} listings from {len(sellers)} rostered sellers "
          f"(roster-only, no keyword search)")
    return list(seen.values()), {"throttled": n_throttled, "batches": n_batches}
