"""Mock-based tests for the YouTube channel-roster system.

NEVER hits the real YouTube API or a real database:
  - the API client layer (youtube_roster._get / youtube._get) is monkeypatched
    with canned playlistItems/videos payloads
  - Postgres is replaced by FakeConn, which records SQL + params and returns
    canned rows

Run:  .venv/bin/python tests/test_youtube_roster.py
"""
import sys
from contextlib import contextmanager
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db, ingest, youtube, youtube_roster  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeResult:
    def __init__(self, rows=None, rowcount=0):
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchall(self):
        return self._rows


class FakeConn:
    """Records every execute(); answers roster/seed reads with canned data."""

    def __init__(self, roster_channels=None, seed_rowcount=0):
        self.roster_channels = roster_channels or []
        self.seed_rowcount = seed_rowcount
        self.executes = []  # list of (sql, params)

    def execute(self, sql, params=None):
        self.executes.append((sql, params))
        if "SELECT channel_id, title FROM youtube_channels" in sql:
            return FakeResult(rows=self.roster_channels)
        if "FROM breaks" in sql and "INSERT INTO youtube_channels" in sql:
            return FakeResult(rowcount=self.seed_rowcount)
        if "INSERT INTO youtube_channels" in sql and "'manual'" in sql:
            return FakeResult(rowcount=1)
        return FakeResult()

    def sqls_containing(self, needle):
        return [s for s, _ in self.executes if needle in s]


def _http_error(status=429):
    req = httpx.Request("GET", "https://www.googleapis.com/youtube/v3/x")
    return httpx.HTTPStatusError(f"HTTP {status}", request=req,
                                 response=httpx.Response(status, request=req))


# ---------------------------------------------------------------------------
# Canned API payloads
# ---------------------------------------------------------------------------

CH_A = "UCaaaaaaaaaaaaaaaaaaaaaa"  # hot breaker channel
CH_B = "UCbbbbbbbbbbbbbbbbbbbbbb"  # cold channel (vlog + stale break)

VID_BREAK = "vid_break_upcoming"   # real break, upcoming tomorrow
VID_LIVE = "vid_break_live"        # real break, live now
VID_VLOG = "vid_vlog"              # not a break
VID_PAST = "vid_break_past"        # real break, but 3 days ago

PLAYLIST_VIDEOS = {
    "UU" + CH_A[2:]: [VID_BREAK, VID_LIVE],
    "UU" + CH_B[2:]: [VID_VLOG, VID_PAST],
}


def _video_resource(vid, channel_id, title, live=None, scheduled=None,
                    started=None):
    snippet = {
        "title": title,
        "channelId": channel_id,
        "channelTitle": "Channel " + channel_id[-4:],
        "liveBroadcastContent": "upcoming",
        "thumbnails": {"default": {"url": "http://thumb/x.jpg"}},
    }
    live_details: dict = {}
    if live:
        snippet["liveBroadcastContent"] = "live"
        live_details = {"actualStartTime": started}
    elif scheduled:
        live_details = {"scheduledStartTime": scheduled}
    return {"id": vid, "snippet": snippet, "liveStreamingDetails": live_details}


VIDEO_DETAILS = {
    VID_BREAK: _video_resource(
        VID_BREAK, CH_A, "2024 Topps Chrome Baseball 2 Box Break PYT #5",
        scheduled="2026-10-07T20:00:00Z"),
    VID_LIVE: _video_resource(
        VID_LIVE, CH_A, "LIVE 5 Box 2024 Mosaic Basketball Mixer Division Break",
        live=True, started="2026-10-06T01:00:00Z"),
    VID_VLOG: _video_resource(
        VID_VLOG, CH_B, "My weekend vlog — card shop visit and mail day",
        scheduled="2026-10-07T20:00:00Z"),
    VID_PAST: _video_resource(
        VID_PAST, CH_B, "2023 Bowman 1 Box Break Personal #3",
        scheduled="2026-10-02T20:00:00Z"),
}


def fake_roster_get(url, params):
    assert "playlistItems" in url, f"unexpected roster call: {url}"
    pid = params["playlistId"]
    assert pid in PLAYLIST_VIDEOS, f"unknown playlist {pid}"
    return {"items": [{"contentDetails": {"videoId": v}}
                      for v in PLAYLIST_VIDEOS[pid][:params["maxResults"]]]}


def fake_youtube_get(url, params):
    assert "/videos" in url, f"unexpected youtube call: {url}"
    ids = params["id"].split(",")
    assert len(ids) <= 50, "videos.list batch exceeded 50 ids"
    return {"items": [VIDEO_DETAILS[v] for v in ids]}


def patch(monkey_targets):
    """Context manager applying a list of (obj, name, value) patches."""
    @contextmanager
    def _ctx():
        saved = []
        for obj, name, value in monkey_targets:
            saved.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)
        try:
            yield
        finally:
            for obj, name, value in saved:
                setattr(obj, name, value)
    return _ctx()


@contextmanager
def fake_db_conn(conn):
    @contextmanager
    def _get_conn():
        yield conn
    with patch([(db, "get_conn", _get_conn)]):
        yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_uploads_playlist_id():
    assert youtube_roster.uploads_playlist_id("UCabc123") == "UUabc123"
    for bad in ("", "bad", "UUabc123", "UC"):
        try:
            youtube_roster.uploads_playlist_id(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")
    print("ok test_uploads_playlist_id")


def test_quota_math():
    # worst case allowed by the guardrails: 300 channels x 10 videos
    units = youtube_roster.estimate_roster_quota_units(300, 3000)
    assert units == 360, f"expected 360, got {units}"
    assert units < 2000, "roster worst case must stay well under ~2,000 units"
    # small run sanity
    assert youtube_roster.estimate_roster_quota_units(2, 4) == 3
    assert youtube_roster.estimate_roster_quota_units(0, 0) == 0
    print("ok test_quota_math")


def test_reuses_existing_filter():
    # The roster must reuse the search poller's normalize + filter, not copy it.
    assert youtube_roster.normalize_youtube_video is youtube.normalize_youtube_video
    assert youtube_roster.is_upcoming_or_live is youtube.is_upcoming_or_live
    assert youtube_roster.estimate_quota_units is youtube.estimate_quota_units
    print("ok test_reuses_existing_filter")


def test_roster_fetch_filters_and_attributes():
    conn = FakeConn(roster_channels=[
        {"channel_id": CH_A, "title": "Hot Breaks"},
        {"channel_id": CH_B, "title": "Cold Channel"},
    ])
    with patch([(youtube_roster, "_get", fake_roster_get),
                (youtube, "_get", fake_youtube_get),
                (config, "YOUTUBE_API_KEY", "fake-key")]):
        rows, hit_cids, checked, stats = youtube_roster.fetch_roster_breaks(conn)

    kept = sorted(r["title_raw"] for r in rows)
    assert kept == ["2024 Topps Chrome Baseball 2 Box Break PYT #5",
                    "LIVE 5 Box 2024 Mosaic Basketball Mixer Division Break"], kept
    assert hit_cids == [CH_A], hit_cids          # only the hot channel produced
    assert checked == [CH_A, CH_B], checked       # both were actually checked
    assert stats["units"] == 3, stats            # 2 playlist + 1 detail call
    assert stats["n_non_break"] == 1, stats       # the vlog
    assert stats["n_past"] == 1, stats            # the 3-day-old break
    # kept rows carry channel_id for the discovery/backfill loop
    assert all(r["channel_id"] == CH_A for r in rows), rows
    print("ok test_roster_fetch_filters_and_attributes")


def test_roster_cap_respected():
    channels = [{"channel_id": f"UC{i:022d}", "title": f"c{i}"} for i in range(350)]
    conn = FakeConn(roster_channels=channels)
    with patch([(youtube_roster, "_get", lambda u, p: {"items": []}),
                (youtube, "_get", lambda u, p: {"items": []}),
                (config, "YOUTUBE_API_KEY", "fake-key")]):
        youtube_roster.fetch_roster_breaks(conn)
    select_params = [p for s, p in conn.executes
                     if "SELECT channel_id, title FROM youtube_channels" in s]
    assert select_params, "roster SELECT never ran"
    assert select_params[0][0] == youtube_roster.MAX_ROSTER_CHANNELS == 300, \
        select_params[0]
    print("ok test_roster_cap_respected")


def test_roster_total_api_failure():
    conn = FakeConn(roster_channels=[{"channel_id": CH_A, "title": "x"}])

    def always_fail(url, params):
        raise _http_error(429)

    with patch([(youtube_roster, "_get", always_fail),
                (youtube, "_get", always_fail),
                (config, "YOUTUBE_API_KEY", "fake-key"),
                (ingest, "youtube_roster", youtube_roster)]):
        with fake_db_conn(conn):
            rc = ingest.run_youtube_roster()
    assert rc == 1, "total API failure must exit non-zero"
    # Nothing written: no break inserts, no channel stamps (nothing was checked)
    assert not conn.sqls_containing("INSERT INTO breaks"), "must not write on failure"
    assert not conn.sqls_containing("SET last_checked_at"), "must not stamp unchecked channels"
    print("ok test_roster_total_api_failure")


def test_ingest_run_writes():
    conn = FakeConn(roster_channels=[{"channel_id": CH_A, "title": "Hot Breaks"}],
                    seed_rowcount=2)
    with patch([(youtube_roster, "_get", fake_roster_get),
                (youtube, "_get", fake_youtube_get),
                (config, "YOUTUBE_API_KEY", "fake-key")]):
        with fake_db_conn(conn):
            rc = ingest.run_youtube_roster()
    assert rc == 0
    assert len(conn.sqls_containing("INSERT INTO breaks")) == 2, "2 kept rows upserted"
    assert not conn.sqls_containing("DELETE FROM breaks"), "roster never wipes the slice"
    hit_upserts = [p for s, p in conn.executes
                   if "ON CONFLICT (channel_id)" in s and "VALUES" in s]
    assert any(p["channel_id"] == CH_A for p in hit_upserts), "hit channel stamped"
    assert conn.sqls_containing("last_checked_at"), "checked channels stamped"
    assert conn.sqls_containing("FROM breaks"), "seed backfill ran"
    print("ok test_ingest_run_writes")


def test_discovery_hook_in_search_poller():
    rows = [
        {"source": "youtube", "source_url": "https://www.youtube.com/watch?v=a",
         "breaker": "Hot Breaks", "channel_id": CH_A, "title_raw": "x",
         "format": "pyt"},
        {"source": "youtube", "source_url": "https://www.youtube.com/watch?v=b",
         "breaker": "No ID Channel", "title_raw": "y", "format": "pyt"},
    ]
    conn = FakeConn()
    with patch([(youtube, "fetch_all_break_streams", lambda: (rows, 2)),
                (config, "YOUTUBE_API_KEY", "fake-key")]):
        with fake_db_conn(conn):
            rc = ingest.run_youtube()
    assert rc == 0
    channel_upserts = [p for s, p in conn.executes
                       if "ON CONFLICT (channel_id)" in s]
    assert len(channel_upserts) == 1, channel_upserts
    assert channel_upserts[0]["channel_id"] == CH_A
    assert channel_upserts[0]["title"] == "Hot Breaks"
    assert channel_upserts[0]["source"] == "search"
    print("ok test_discovery_hook_in_search_poller")


def test_upsert_break_without_channel_id():
    conn = FakeConn()
    row = {"source": "ebay", "source_url": "https://ebay.com/itm/1",
           "title_raw": "x"}  # no channel_id key at all
    db.upsert_break(conn, row)
    assert row["channel_id"] is None, "setdefault must fill channel_id"
    assert "channel_id" in conn.executes[0][0], "UPSERT must include channel_id"
    print("ok test_upsert_break_without_channel_id")


def test_seed_sql():
    conn = FakeConn(seed_rowcount=3)
    n = db.seed_youtube_channels_from_breaks(conn)
    assert n == 3
    sql = conn.executes[0][0]
    assert "ON CONFLICT (channel_id) DO NOTHING" in sql
    assert "'seed'" in sql
    print("ok test_seed_sql")


def test_seed_manual_channels():
    conn = FakeConn(seed_rowcount=1)
    lines = [
        "UCaaaaaaaaaaaaaaaaaaaaaa  # Some Breaker",
        "# a comment line",
        "",
        "UCbbbbbbbbbbbbbbbbbbbbbb",
        "UCaaaaaaaaaaaaaaaaaaaaaa  # duplicate",
    ]
    n = db.seed_manual_channels(conn, lines)
    # FakeConn returns rowcount=1 per execute: 3 real inserts attempted
    assert n == 3, n
    cids = [p["cid"] for _, p in conn.executes]
    assert cids == ["UCaaaaaaaaaaaaaaaaaaaaaa",
                    "UCbbbbbbbbbbbbbbbbbbbbbb",
                    "UCaaaaaaaaaaaaaaaaaaaaaa"], cids
    assert all("'manual'" in sql for sql, _ in conn.executes)
    print("ok test_seed_manual_channels")


def main():
    fns = sorted((k, v) for k, v in globals().items()
                 if k.startswith("test_") and callable(v))
    for _, fn in fns:
        fn()
    print("\nAll roster tests passed.")


if __name__ == "__main__":
    main()
