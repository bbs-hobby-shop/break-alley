"""User auth: bcrypt passwords, signed session cookies, login throttling.

Sessions are stateless signed cookies (itsdangerous) carrying the user id.
No server-side session store to manage.
"""
import time

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import config, db

SESSION_COOKIE = "ba_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def _serializer() -> URLSafeTimedSerializer:
    if not config.SECRET_KEY:
        raise RuntimeError("SECRET_KEY is not set in the environment.")
    return URLSafeTimedSerializer(config.SECRET_KEY, salt="ba-session")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def check_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except (ValueError, TypeError):
        return False


def create_session(user_id: int) -> str:
    return _serializer().dumps({"uid": user_id})


def read_session(token: str | None) -> int | None:
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=SESSION_MAX_AGE)
        return int(data.get("uid"))
    except (BadSignature, SignatureExpired, ValueError, TypeError):
        return None


def get_current_user(request) -> dict | None:
    """Return the logged-in user dict, or None. Never raises."""
    try:
        user_id = read_session(request.cookies.get(SESSION_COOKIE))
        if not user_id:
            return None
        with db.get_conn() as conn:
            return db.get_user_by_id(conn, user_id)
    except Exception:
        return None


def valid_email(email: str) -> bool:
    email = (email or "").strip()
    return bool(email) and "@" in email and "." in email.split("@")[-1] and len(email) <= 254


# --- login/signup throttling: max 10 attempts per IP per 10 minutes ---

_attempts: dict[str, list[float]] = {}
_THROTTLE_MAX = 10
_THROTTLE_WINDOW = 600


def _prune(ip: str, now: float) -> list[float]:
    recent = [t for t in _attempts.get(ip, []) if now - t < _THROTTLE_WINDOW]
    _attempts[ip] = recent
    return recent


def throttle_check(ip: str) -> bool:
    """True if the attempt may proceed; records the attempt."""
    now = time.time()
    recent = _prune(ip, now)
    if len(recent) >= _THROTTLE_MAX:
        return False
    recent.append(now)
    _attempts[ip] = recent
    return True


def throttle_wait_seconds(ip: str) -> int:
    now = time.time()
    recent = _prune(ip, now)
    if not recent:
        return 0
    return max(0, int(_THROTTLE_WINDOW - (now - recent[0])))
