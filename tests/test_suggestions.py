"""Tests for the breaker-suggestion flow (public form -> review queue -> roster).

Never hits the real YouTube API or a real database: youtube._get is
monkeypatched and Postgres is replaced by FakeConn.

Run:  .venv/bin/python tests/test_suggestions.py
"""
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, youtube  # noqa: E402


class FakeResult:
    def __init__(self, rows=None, rowcount=0, one=None):
        self._rows = rows or []
        self.rowcount = rowcount
        self._one = one or {}

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one


class FakeConn:
    def __init__(self):
        self.executes = []

    def execute(self, sql, params=None):
        self.executes.append((sql, params))
        if "RETURNING id" in sql:
            return FakeResult(one={"id": 42})
        return FakeResult()


@contextmanager
def patched_youtube_get(fn):
    orig = youtube._get
    youtube._get = fn
    try:
        yield
    finally:
        youtube._get = orig


def test_add_suggestion_sql():
    conn = FakeConn()
    sid = db.add_breaker_suggestion(conn, "  @somebreaker  ", "great breaks!")
    assert sid == 42
    sql, params = conn.executes[0]
    assert "INSERT INTO breaker_suggestions" in sql
    assert params["input_text"] == "@somebreaker"  # stripped
    assert params["note"] == "great breaks!"
    print("ok test_add_suggestion_sql")


def test_add_suggestion_truncates():
    conn = FakeConn()
    db.add_breaker_suggestion(conn, "x" * 500, "y" * 900)
    _, params = conn.executes[0]
    assert len(params["input_text"]) == 200
    assert len(params["note"]) == 500
    print("ok test_add_suggestion_truncates")


def test_list_suggestions_status_filter():
    conn = FakeConn()
    db.list_breaker_suggestions(conn, status="pending")
    sql, params = conn.executes[0]
    assert "WHERE status" in sql and params["status"] == "pending"
    conn2 = FakeConn()
    db.list_breaker_suggestions(conn2)
    sql2, _ = conn2.executes[0]
    assert "WHERE status" not in sql2
    print("ok test_list_suggestions_status_filter")


def test_review_approve_sql():
    conn = FakeConn()
    db.review_breaker_suggestion(conn, 7, True, channel_id="UCabc", reviewer_note="legit")
    sql, params = conn.executes[0]
    assert "UPDATE breaker_suggestions" in sql
    assert params["status"] == "approved"
    assert params["channel_id"] == "UCabc"
    assert params["sid"] == 7
    print("ok test_review_approve_sql")


def test_review_reject_sql():
    conn = FakeConn()
    db.review_breaker_suggestion(conn, 8, False)
    _, params = conn.executes[0]
    assert params["status"] == "rejected"
    assert params["channel_id"] is None
    print("ok test_review_reject_sql")


def _fake_get_for_handle(url, params):
    assert "channels" in url
    if params.get("forHandle") == "somebreaker":
        return {"items": [{"id": "UCsomebreakerid00000000",
                           "snippet": {"title": "Some Breaker"}}]}
    if params.get("id") == "UCdirectid00000000000001":
        return {"items": [{"id": "UCdirectid00000000000001",
                           "snippet": {"title": "Direct Channel"}}]}
    return {"items": []}


def test_resolve_handle():
    with patched_youtube_get(_fake_get_for_handle):
        cid, title = youtube.resolve_channel("@somebreaker")
    assert cid == "UCsomebreakerid00000000" and title == "Some Breaker"
    print("ok test_resolve_handle")


def test_resolve_handle_from_url():
    with patched_youtube_get(_fake_get_for_handle):
        cid, _ = youtube.resolve_channel("https://www.youtube.com/@somebreaker/videos")
    assert cid == "UCsomebreakerid00000000"
    print("ok test_resolve_handle_from_url")


def test_resolve_direct_channel_id():
    with patched_youtube_get(_fake_get_for_handle):
        cid, title = youtube.resolve_channel("UCdirectid00000000000001")
    assert cid == "UCdirectid00000000000001" and title == "Direct Channel"
    print("ok test_resolve_direct_channel_id")


def test_resolve_channel_id_from_url():
    with patched_youtube_get(_fake_get_for_handle):
        cid, _ = youtube.resolve_channel(
            "https://www.youtube.com/channel/UCdirectid00000000000001")
    assert cid == "UCdirectid00000000000001"
    print("ok test_resolve_channel_id_from_url")


def test_resolve_unknown_handle():
    with patched_youtube_get(_fake_get_for_handle):
        try:
            youtube.resolve_channel("@nosuchbreaker")
        except ValueError as exc:
            assert "nosuchbreaker" in str(exc)
        else:
            raise AssertionError("expected ValueError")
    print("ok test_resolve_unknown_handle")


def test_resolve_empty():
    try:
        youtube.resolve_channel("   ")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
    print("ok test_resolve_empty")


def main():
    fns = sorted((k, v) for k, v in globals().items()
                 if k.startswith("test_") and callable(v))
    for _, fn in fns:
        fn()
    print("\nAll suggestion tests passed.")


if __name__ == "__main__":
    main()
