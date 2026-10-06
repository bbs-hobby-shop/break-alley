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




def test_user_stats_sql():
    conn = FakeConn(one={"c": 3}, rows=[{"email": "a@b.com", "created_at": None}])
    stats = db.user_stats(conn)
    assert stats["total"] == 3
    assert len(stats["latest"]) == 1
    sqls = [s for s, _ in conn.executes]
    assert any("FROM users" in s and "24 hours" in s for s in sqls)
    assert any("7 days" in s for s in sqls)
    print("ok test_user_stats_sql")


def test_update_user_email_sql():
    conn = FakeConn()
    db.update_user_email(conn, 7, "New@Example.com")
    sql, params = conn.executes[0]
    assert "UPDATE users SET email" in sql
    assert params == {"email": "new@example.com", "id": 7}
    print("ok test_update_user_email_sql")


def test_update_password_hash_sql():
    conn = FakeConn()
    db.update_password_hash(conn, 7, "hashed")
    sql, params = conn.executes[0]
    assert "UPDATE users SET password_hash" in sql
    assert params == {"h": "hashed", "id": 7}
    print("ok test_update_password_hash_sql")


def test_delete_user_sql():
    conn = FakeConn()
    db.delete_user(conn, 7)
    sql, params = conn.executes[0]
    assert sql.strip().startswith("DELETE FROM users")
    assert params == {"id": 7}
    print("ok test_delete_user_sql")


def test_change_password_route():
    from fastapi.testclient import TestClient
    from app import config, main
    from app.main import app

    pw_hash = auth.hash_password("oldpassword1")
    user = {"id": 3, "email": "u@x.com", "password_hash": pw_hash,
            "created_at": None}

    real_get_conn = db.get_conn
    real_check = auth.check_password

    class Ctx:
        def __init__(self, conn): self.c = conn
        def __enter__(self): return self.c
        def __exit__(self, *a): return False

    saved = {}
    def fake_get_conn():
        return Ctx(FakeConn(one={"c": 0}, rows=[]))
    db.get_conn = fake_get_conn
    main.db.get_conn = fake_get_conn
    auth.get_current_user = lambda request: user
    config.ADMIN_KEY = "x"

    # monkeypatch the update to capture
    orig_update = db.update_password_hash
    db.update_password_hash = lambda conn, uid, h: saved.update(uid=uid, h=h)
    try:
        c = TestClient(app, raise_server_exceptions=False)
        # wrong current password -> error page
        r = c.post("/account/password", data={
            "current_password": "nope", "new_password": "newpassword1",
            "confirm_password": "newpassword1"})
        assert r.status_code == 200 and "Current password is incorrect" in r.text, r.status_code
        # too short
        r = c.post("/account/password", data={
            "current_password": "oldpassword1", "new_password": "short",
            "confirm_password": "short"})
        assert "at least 8 characters" in r.text
        # mismatch
        r = c.post("/account/password", data={
            "current_password": "oldpassword1", "new_password": "newpassword1",
            "confirm_password": "different2"})
        assert "don&#39;t match" in r.text or "don't match" in r.text
        # success -> redirect with notice
        r = c.post("/account/password", data={
            "current_password": "oldpassword1", "new_password": "newpassword1",
            "confirm_password": "newpassword1"}, follow_redirects=False)
        assert r.status_code == 303 and "notice=password_updated" in r.headers["location"], r.headers.get("location")
        assert saved["uid"] == 3 and auth.check_password("newpassword1", saved["h"])
    finally:
        db.get_conn = real_get_conn
        main.db.get_conn = real_get_conn
        db.update_password_hash = orig_update
        del auth.get_current_user
    print("ok test_change_password_route")


def test_delete_account_route():
    from fastapi.testclient import TestClient
    from app import main
    from app.main import app

    pw_hash = auth.hash_password("oldpassword1")
    user = {"id": 3, "email": "u@x.com", "password_hash": pw_hash, "created_at": None}

    class Ctx:
        def __init__(self, conn): self.c = conn
        def __enter__(self): return self.c
        def __exit__(self, *a): return False

    real_get_conn = db.get_conn
    deleted = []
    def fake_get_conn():
        return Ctx(FakeConn())
    db.get_conn = fake_get_conn
    main.db.get_conn = fake_get_conn
    auth.get_current_user = lambda request: user
    orig_delete = db.delete_user
    db.delete_user = lambda conn, uid: deleted.append(uid)
    try:
        c = TestClient(app, raise_server_exceptions=False)
        # missing DELETE confirmation -> error
        r = c.post("/account/delete", data={"password": "oldpassword1", "confirm": "no"})
        assert r.status_code == 200 and "Type DELETE" in r.text
        # wrong password -> error
        r = c.post("/account/delete", data={"password": "wrong", "confirm": "DELETE"})
        assert "incorrect" in r.text
        assert deleted == []
        # success -> redirect home, cookie cleared
        r = c.post("/account/delete", data={"password": "oldpassword1", "confirm": "DELETE"},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert deleted == [3]
    finally:
        db.get_conn = real_get_conn
        main.db.get_conn = real_get_conn
        db.delete_user = orig_delete
        del auth.get_current_user
    print("ok test_delete_account_route")


if __name__ == "__main__":
    main()
