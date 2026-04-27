import hashlib
import hmac
import time
from http.cookies import SimpleCookie

from crm.config import (
    DEFAULT_STAFF_PASSWORD,
    DEFAULT_STAFF_USERNAME,
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE,
    SESSION_SECRET,
)


def session_signature(payload: str) -> str:
    return hmac.new(SESSION_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def session_cookie_value(username: str) -> str:
    expires_at = int(time.time()) + SESSION_MAX_AGE
    payload = f"{username}|{expires_at}"
    return f"{payload}|{session_signature(payload)}"


def clear_session_cookie_value() -> str:
    return f"{SESSION_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


def auth_cookie_header(username: str) -> str:
    return (
        f"{SESSION_COOKIE_NAME}={session_cookie_value(username)}; "
        f"Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_MAX_AGE}"
    )


def authenticated_username(cookie_header: str | None) -> str | None:
    if not cookie_header:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:
        return None

    session = cookie.get(SESSION_COOKIE_NAME)
    if not session or not session.value:
        return None

    try:
        username, expires_at, signature = session.value.split("|", 2)
    except ValueError:
        return None

    payload = f"{username}|{expires_at}"
    if not hmac.compare_digest(signature, session_signature(payload)):
        return None

    try:
        if int(expires_at) < int(time.time()):
            return None
    except ValueError:
        return None
    return username


def valid_staff_credentials(username: str, password: str) -> bool:
    return hmac.compare_digest(username, DEFAULT_STAFF_USERNAME) and hmac.compare_digest(
        password, DEFAULT_STAFF_PASSWORD
    )
