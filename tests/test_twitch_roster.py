"""Mock-based tests for the Twitch channel-roster system.

NEVER hits the real Twitch API or a real database:
  - the API client layer (twitch_roster.streams_by_logins / httpx.get) is
    monkeypatched with canned streams payloads
  - Postgres is replaced by FakeConn, which records SQL + params and returns
    canned rows

Run:  .venv/bin/python tests/test_twitch_roster.py
"""
import sys
from contextlib import contextmanager
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db, ingest, twitch, twitch_roster  # noqa: E402


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

    def __init__(self, roster_logins=None, seed_rowcount=0):
        self.roster_logins = roster_logins or []
        self.seed_rowcount = seed_rowcount
        self.executes = []  # list of (sql, params)

    def execute(self, sql, params=None):
        self.executes.append((sql, params))
        if "SELECT login, display_name FROM twitch_channels" in sql:
            return FakeResult(rows=self.roster_logins)
        if "INSERT INTO twitch_channels" in sql and "'manual'" in sql:
            return FakeResult(rowcount=self.seed_rowcount)
        return FakeResult()

    def sqls_containing(self, needle):
        return [s for s, _ in self.executes if needle in s]


def _http_error(status=429):
    req = httpx.Request("GET", "https://api.twitch.tv/helix/streams")
    return httpx.HTTPStatusError(f"HTTP {status}", request=req,
                                 response=httpx.Response(status, request=req))


def _stream(login, user_name, title, started="2026-10-06T15:00:00Z"):
    return {
        "user_id": "999",
        "user_login": login,
        "user_name": user_name,
        "game_name": "Just Chatting",
        "title": title,
        "viewer_count": 42,
        "started_at": started,
        "thumbnail_url": "https://static-cdn.jtvnw.net/x-{width}x{height}.jpg",
    }


# one live break, one live non-break, one offline (absent from response)
LOGIN_HOT = "laytonsportscards"
LOGIN_COLD = "somevlogger"
LOGIN_OFFLINE = "jaspysbreaks"

STREAMS = {
    LOGIN_HOT: _stream(LOGIN_HOT, "LaytonSportsCards",
                       "LIVE: 2026 Bowman Chrome Baseball 6 Box PYT Break #12"),
    LOGIN_COLD: _stream(LOGIN_COLD, "SomeVlogger",
                        "My card collection tour and mail day"),
}


def fake_streams_by_logins(logins, token):
    assert token == "fake-token"
    return ({l: STREAMS[l] for l in logins if l in STREAMS},
            (len(logins) + 99) // 100 if logins else 0)


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


def _twitch_creds():
    return [(config, "TWITCH_CLIENT_ID", "fake-id"),
            (config, "TWITCH_CLIENT_SECRET", "fake-secret"),
            (twitch_roster, "get_app_token", lambda: "fake-token")]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_roster_fetch_filters_and_attributes():
    conn = FakeConn(roster_logins=[
        {"login": LOGIN_HOT, "display_name": "LaytonSportsCards"},
        {"login": LOGIN_COLD, "display_name": "SomeVlogger"},
        {"login": LOGIN_OFFLINE, "display_name": "JaspysBreaks"},
    ])
    with patch([(twitch_roster, "streams_by_logins", fake_streams_by_logins),
                *_twitch_creds()]):
        rows, hit, checked, stats = twitch_roster.fetch_roster_breaks(conn)

    assert [r["title_raw"] for r in rows] == \
        ["LIVE: 2026 Bowman Chrome Baseball 6 Box PYT Break #12"], rows
    assert hit == {LOGIN_HOT: "LaytonSportsCards"}, hit  # only the hot login
    assert checked == [LOGIN_HOT, LOGIN_COLD, LOGIN_OFFLINE], checked
    assert stats["n_kept"] == 1 and stats["n_non_break"] == 1, stats
    row = rows[0]
    assert row["source"] == "twitch" and row["is_live"] is True
    assert row["source_url"] == f"https://www.twitch.tv/{LOGIN_HOT}"
    assert row["format"] == "pyt", row["format"]
    print("ok test_roster_fetch_filters_and_attributes")


def test_streams_batched_100_per_call():
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        logins = [v for k, v in (params or []) if k == "user_login"]
        assert len(logins) <= 100, "streams batch exceeded 100 logins"
        calls.append(logins)
        req = httpx.Request("GET", url)
        return httpx.Response(200, json={"data": []}, request=req)

    logins = [f"ch{i:03d}" for i in range(250)]
    with patch([(twitch_roster.httpx, "get", fake_get)]):
        streams, n_ok = twitch_roster.streams_by_logins(logins, "tok")
    assert n_ok == 3 and len(calls) == 3, (n_ok, len(calls))
    assert streams == {}
    print("ok test_streams_batched_100_per_call")


def test_roster_cap_respected():
    logins = [{"login": f"ch{i:03d}", "display_name": f"c{i}"} for i in range(350)]
    conn = FakeConn(roster_logins=logins)
    with patch([(twitch_roster, "streams_by_logins",
                 lambda logins, token: ({}, 1)),
                *_twitch_creds()]):
        twitch_roster.fetch_roster_breaks(conn)
    select_params = [p for s, p in conn.executes
                     if "SELECT login, display_name FROM twitch_channels" in s]
    assert select_params, "roster SELECT never ran"
    assert select_params[0][0] == twitch_roster.MAX_ROSTER_CHANNELS == 300, \
        select_params[0]
    print("ok test_roster_cap_respected")


def test_roster_total_api_failure():
    conn = FakeConn(roster_logins=[{"login": LOGIN_HOT, "display_name": "x"}])

    def always_fail(url, params=None, headers=None, timeout=None):
        raise _http_error(429)

    with patch([(twitch_roster.httpx, "get", always_fail),
                *_twitch_creds()]):
        with fake_db_conn(conn):
            rc = ingest.run_twitch_roster()
    assert rc == 1, "total API failure must exit non-zero"
    # Slice kept: no wipe, no break inserts, no roster stamps
    assert not conn.sqls_containing("DELETE FROM breaks"), "must not wipe on failure"
    assert not conn.sqls_containing("INSERT INTO breaks"), "must not write on failure"
    assert not conn.sqls_containing("SET last_checked_at"), "must not stamp unchecked"
    print("ok test_roster_total_api_failure")


def test_ingest_run_writes_and_wipes_slice():
    conn = FakeConn(roster_logins=[
        {"login": LOGIN_HOT, "display_name": "LaytonSportsCards"},
        {"login": LOGIN_COLD, "display_name": "SomeVlogger"},
    ])
    with patch([(twitch_roster, "streams_by_logins", fake_streams_by_logins),
                *_twitch_creds()]):
        with fake_db_conn(conn):
            rc = ingest.run_twitch_roster()
    assert rc == 0
    assert conn.sqls_containing("DELETE FROM breaks WHERE source = 'twitch'"), \
        "roster owns the transient twitch slice: wipe + rewrite"
    n_inserts = len(conn.sqls_containing("INSERT INTO breaks"))
    assert n_inserts == 1, f"expected 1 kept row upserted, got {n_inserts}"
    hit_upserts = [p for s, p in conn.executes if "ON CONFLICT (login)" in s]
    assert any(p["login"] == LOGIN_HOT for p in hit_upserts), "hit login stamped"
    assert conn.sqls_containing("SET last_checked_at"), "checked logins stamped"
    print("ok test_ingest_run_writes_and_wipes_slice")


def test_unknown_format_stored_as_box_break():
    # A kept break with no specific format signal still lands in a category.
    conn = FakeConn(roster_logins=[{"login": LOGIN_HOT, "display_name": "x"}])
    streams = {LOGIN_HOT: _stream(LOGIN_HOT, "LaytonSportsCards",
                                  "Break #99 — 2026 Bowman Chrome")}
    with patch([(twitch_roster, "streams_by_logins",
                 lambda logins, token: (streams, 1)),
                *_twitch_creds()]):
        with fake_db_conn(conn):
            rc = ingest.run_twitch_roster()
    assert rc == 0
    inserts = [(s, p) for s, p in conn.executes if "INSERT INTO breaks" in s]
    assert len(inserts) == 1
    assert inserts[0][1]["format"] == "box_break", inserts[0][1]["format"]
    print("ok test_unknown_format_stored_as_box_break")


def test_seed_manual_twitch_channels():
    conn = FakeConn()
    lines = [
        "LaytonSportsCards  # confirmed",   # casing normalized
        "# a comment line",
        "",
        "backyardbreaks",
        "laytonsportscards  # duplicate",
    ]
    n = db.seed_manual_twitch_channels(conn, lines)
    assert n == 0  # FakeConn rowcount defaults to 0; 3 real inserts attempted
    logins = [p["login"] for _, p in conn.executes]
    assert logins == ["laytonsportscards", "backyardbreaks",
                      "laytonsportscards"], logins
    assert all("'manual'" in sql for sql, _ in conn.executes)
    print("ok test_seed_manual_twitch_channels")


def main():
    test_roster_fetch_filters_and_attributes()
    test_streams_batched_100_per_call()
    test_roster_cap_respected()
    test_roster_total_api_failure()
    test_ingest_run_writes_and_wipes_slice()
    test_unknown_format_stored_as_box_break()
    test_seed_manual_twitch_channels()
    print("\nAll twitch roster tests passed.")


if __name__ == "__main__":
    main()
