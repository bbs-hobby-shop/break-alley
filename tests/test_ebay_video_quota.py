"""Tests for eBay video-info check-once semantics (Browse API quota protection).

Regression context (2026-10-06): the eBay poller cron failed twice in one
evening. Root cause: needs_video_info() returned True for every listing with
video_url NULL -- including the ~78% of listings whose sellers never include
video info -- so every 15-min run re-ran getItem for ~360 listings (~35k
Browse calls/day vs eBay's ~5k/day application quota). Quota exhaustion
makes the search calls 429, the unguarded raise_for_status() exits 1, and
the whole run (including all listing updates) is lost.

NEVER hits the real eBay API or a real database. Run:
  .venv/bin/python tests/test_ebay_video_quota.py
"""
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, ebay, ebay_video, ingest  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeResult:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class FakeConn:
    """Answers the video_checked_at lookup with canned rows; records writes."""

    def __init__(self, checked_urls=()):
        self.checked_urls = set(checked_urls)
        self.executes = []  # list of (sql, params)

    def execute(self, sql, params=None):
        self.executes.append((sql, params))
        if "SELECT video_checked_at FROM breaks" in sql:
            url = (params or {}).get("url")
            if url in self.checked_urls:
                return FakeResult({"video_checked_at": "2026-10-06T18:00:00+00:00"})
            return FakeResult(None)
        return FakeResult(None)

    def updates_to(self, url):
        return [
            (s, p) for s, p in self.executes
            if "video_checked_at" in s and "UPDATE" in s
            and (p or {}).get("url") == url
        ]


# ---------------------------------------------------------------------------
# needs_video_info: check-once semantics
# ---------------------------------------------------------------------------

def test_new_listing_needs_check():
    conn = FakeConn()
    assert db.needs_video_info(conn, "https://www.ebay.com/itm/111") is True


def test_unchecked_existing_listing_needs_check():
    conn = FakeConn()
    assert db.needs_video_info(conn, "https://www.ebay.com/itm/222") is True


def test_checked_listing_without_video_is_never_refetched():
    # The core regression: a listing that was already checked and has no
    # video info must NOT be re-fetched on subsequent runs.
    conn = FakeConn(checked_urls={"https://www.ebay.com/itm/333"})
    assert db.needs_video_info(conn, "https://www.ebay.com/itm/333") is False


def test_mark_video_checked_writes_timestamp():
    conn = FakeConn()
    db.mark_video_checked(conn, "https://www.ebay.com/itm/444")
    assert len(conn.updates_to("https://www.ebay.com/itm/444")) == 1


# ---------------------------------------------------------------------------
# run_ebay: marks checked after upsert; survives a poison row
# ---------------------------------------------------------------------------

def test_run_ebay_marks_checked_and_survives_poison_row():
    good = {"itemWebUrl": "https://www.ebay.com/itm/111", "title": "box break"}
    poison = {"itemWebUrl": "https://www.ebay.com/itm/999", "title": "bad"}

    upserted = []
    extracted = []

    orig_fetch = ebay.fetch_all_break_listings
    orig_normalize = ingest.normalize_ebay_item
    orig_get_conn = db.get_conn
    orig_upsert = db.upsert_break
    orig_extract = ebay_video.extract_for_listing
    orig_configured = ingest.config.ebay_configured
    conn = FakeConn()

    @contextmanager
    def fake_get_conn():
        yield conn

    try:
        ingest.config.ebay_configured = lambda: True
        ebay.fetch_all_break_listings = lambda: [good, poison]
        def fake_normalize(item, affiliate_url=None):
            if item is poison:
                raise ValueError("boom: bad price")
            return {"source_url": item["itemWebUrl"], "title_raw": item["title"]}
        ingest.normalize_ebay_item = fake_normalize
        db.get_conn = fake_get_conn
        db.upsert_break = lambda c, row: upserted.append(row["source_url"])
        def fake_extract(url):
            extracted.append(url)
            return {"video_url": None, "video_platform": None,
                    "video_links": [], "break_time_text": None}
        ebay_video.extract_for_listing = fake_extract

        rc = ingest.run_ebay()

        assert rc == 0, "one poison row must not fail the run"
        assert upserted == ["https://www.ebay.com/itm/111"]
        assert extracted == ["https://www.ebay.com/itm/111"], \
            "video extraction attempted exactly once for the new listing"
        assert len(conn.updates_to("https://www.ebay.com/itm/111")) == 1, \
            "listing marked checked after upsert so it is never re-fetched"
        assert conn.updates_to("https://www.ebay.com/itm/999") == [], \
            "poison row skipped before any video work"
    finally:
        ingest.config.ebay_configured = orig_configured
        ebay.fetch_all_break_listings = orig_fetch
        ingest.normalize_ebay_item = orig_normalize
        db.get_conn = orig_get_conn
        db.upsert_break = orig_upsert
        ebay_video.extract_for_listing = orig_extract


def test_run_ebay_all_poison_returns_nonzero():
    orig_fetch = ebay.fetch_all_break_listings
    orig_normalize = ingest.normalize_ebay_item
    orig_get_conn = db.get_conn
    orig_configured = ingest.config.ebay_configured
    conn = FakeConn()

    @contextmanager
    def fake_get_conn():
        yield conn

    try:
        ingest.config.ebay_configured = lambda: True
        ebay.fetch_all_break_listings = lambda: [{"itemWebUrl": "https://www.ebay.com/itm/999"}]
        def always_boom(item, affiliate_url=None):
            raise ValueError("boom")
        ingest.normalize_ebay_item = always_boom
        db.get_conn = fake_get_conn
        rc = ingest.run_ebay()
        assert rc == 1, "total failure must still mark the cron run failed"
    finally:
        ingest.config.ebay_configured = orig_configured
        ebay.fetch_all_break_listings = orig_fetch
        ingest.normalize_ebay_item = orig_normalize
        db.get_conn = orig_get_conn


if __name__ == "__main__":
    test_new_listing_needs_check()
    test_unchecked_existing_listing_needs_check()
    test_checked_listing_without_video_is_never_refetched()
    test_mark_video_checked_writes_timestamp()
    test_run_ebay_marks_checked_and_survives_poison_row()
    test_run_ebay_all_poison_returns_nonzero()
    print("all ebay video quota tests passed")
