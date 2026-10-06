"""Tests for user accounts: hashing, sessions, throttling, and DB helpers.

No real database: Postgres is replaced by FakeConn. bcrypt runs for real
(fast enough at these sizes).

Run:  .venv/bin/python tests/test_accounts.py
"""
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth, db  # noqa: E402


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
    def __init__(self, one=None, rows=None):
        self.executes = []
        self._one = one or {}
        self._rows = rows or []

    def execute(self, sql, params=None):
        self.executes.append((sql, params))
        return FakeResult(rows=self._rows, one=self._one)


def test_password_roundtrip():
    h = auth.hash_password("correct horse 123")
    assert h != "correct horse 123"
    assert auth.check_password("correct horse 123", h)
    assert not auth.check_password("wrong", h)
    assert not auth.check_password("x", "not-a-hash")
    print("ok test_password_roundtrip")


def test_session_roundtrip():
    with patch.object(auth.config, "SECRET_KEY", "test-secret-123"):
        token = auth.create_session(42)
        assert auth.read_session(token) == 42
        assert auth.read_session(None) is None
        assert auth.read_session("") is None
        # tampered
        assert auth.read_session(token + "x") is None
        assert auth.read_session("garbage") is None
    print("ok test_session_roundtrip")


def test_session_needs_secret():
    with patch.object(auth.config, "SECRET_KEY", ""):
        try:
            auth.create_session(1)
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected RuntimeError without SECRET_KEY")
    print("ok test_session_needs_secret")


def test_valid_email():
    assert auth.valid_email("a@b.com")
    assert not auth.valid_email("nope")
    assert not auth.valid_email("a@b")
    assert not auth.valid_email("")
    print("ok test_valid_email")


def test_throttle():
    ip = f"9.9.9.{int(time.time()) % 250}"
    for _ in range(10):
        assert auth.throttle_check(ip)
    assert not auth.throttle_check(ip)  # 11th blocked
    assert auth.throttle_wait_seconds(ip) > 0
    print("ok test_throttle")


def test_create_user_sql():
    conn = FakeConn(one={"id": 5})
    uid = db.create_user(conn, "  Ted@Example.com ", "hash")
    assert uid == 5
    sql, params = conn.executes[0]
    assert "INSERT INTO users" in sql and "RETURNING id" in sql
    assert params["email"] == "ted@example.com"  # lowercased + stripped
    print("ok test_create_user_sql")


def test_get_user_by_email_lowercases():
    conn = FakeConn(one={"id": 1, "email": "a@b.com"})
    db.get_user_by_email(conn, "A@B.COM")
    _, params = conn.executes[0]
    assert params["email"] == "a@b.com"
    print("ok test_get_user_by_email_lowercases")


def test_favorites_sql():
    conn = FakeConn()
    db.add_favorite(conn, 3, "  Layton Sports Cards  ", channel_id="UCx")
    sql, params = conn.executes[0]
    assert "INSERT INTO user_favorites" in sql
    assert "ON CONFLICT (user_id, breaker) DO NOTHING" in sql
    assert params["breaker"] == "Layton Sports Cards"
    db.remove_favorite(conn, 3, "Layton Sports Cards")
    sql2, _ = conn.executes[1]
    assert "DELETE FROM user_favorites" in sql2
    print("ok test_favorites_sql")


def test_breaks_for_breakers_empty():
    conn = FakeConn()
    assert db.breaks_for_breakers(conn, []) == []
    assert conn.executes == []  # no query at all
    print("ok test_breaks_for_breakers_empty")


def test_breaks_for_breakers_sql():
    conn = FakeConn(rows=[{"id": 1}])
    rows = db.breaks_for_breakers(conn, ["A", "B"])
    assert len(rows) == 1
    sql, params = conn.executes[0]
    assert "breaker = ANY" in sql
    assert "is_live DESC" in sql
    assert params["breakers"] == ["A", "B"]
    print("ok test_breaks_for_breakers_sql")


def test_saved_search_sql():
    conn = FakeConn(one={"id": 9})
    sid = db.save_search(conn, 2, "  Prizm footy  ", "prizm", "pyt", "youtube", 50.0)
    assert sid == 9
    sql, params = conn.executes[0]
    assert "INSERT INTO saved_searches" in sql
    assert params["name"] == "Prizm footy"
    assert params["max_price"] == 50.0
    db.delete_saved_search(conn, 2, 9)
    sql2, params2 = conn.executes[1]
    assert "DELETE FROM saved_searches" in sql2
    assert params2 == {"id": 9, "user_id": 2}  # scoped to owner
    print("ok test_saved_search_sql")


def main():
    fns = sorted((k, v) for k, v in globals().items()
                 if k.startswith("test_") and callable(v))
    for _, fn in fns:
        fn()
    print("\nAll account tests passed.")


if __name__ == "__main__":
    main()
