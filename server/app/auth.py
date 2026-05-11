# -*- coding: utf-8 -*-
"""
server/app/auth.py
------------------
Session-based admin authentication for the License Server dashboard.

The `/license/*` public API remains unauthenticated (the desktop client
consumes it). Everything under `/admin/*` (UI pages + JSON API) is
protected by a signed-in session stored in the ``admin_sessions`` table
and exposed to the browser via an HTTP-only cookie.

Password storage format:
    pbkdf2_sha256$<iterations>$<base64 salt>$<base64 digest>

All stdlib — no third-party dependency.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import secrets as _secrets
from typing import Optional

from fastapi import Request
from fastapi.responses import RedirectResponse

from . import config as _cfg
from .database import get_connection


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

_HASH_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    """Return a pbkdf2_sha256 encoded hash string."""
    salt = _secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _HASH_ITERATIONS
    )
    return (
        f"pbkdf2_sha256${_HASH_ITERATIONS}$"
        f"{base64.b64encode(salt).decode('ascii')}$"
        f"{base64.b64encode(digest).decode('ascii')}"
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification of *password* against *stored* hash."""
    try:
        algo, iters_s, salt_b64, digest_b64 = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iters = int(iters_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iters
        )
        return hmac.compare_digest(candidate, expected)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def _iso(dt: _dt.datetime) -> str:
    return dt.isoformat()


def _parse(iso_str: Optional[str]) -> Optional[_dt.datetime]:
    if not iso_str:
        return None
    try:
        s = str(iso_str)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = _dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d
    except Exception:
        return None


def create_session(username: str, ip: str = "", user_agent: str = "") -> str:
    """Insert a new session row and return the opaque session token."""
    token = _secrets.token_urlsafe(48)
    now = _now()
    expires = now + _dt.timedelta(days=max(1, int(_cfg.SESSION_LIFETIME_DAYS)))
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO admin_sessions
                (token, username, created_at, expires_at, ip, user_agent)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (token, username, _iso(now), _iso(expires),
             (ip or "")[:64], (user_agent or "")[:512]),
        )
    return token


def destroy_session(token: str) -> None:
    """Delete a session by token. No-op if not found."""
    if not token:
        return
    try:
        with get_connection() as conn:
            conn.execute(
                "DELETE FROM admin_sessions WHERE token = ?", (token,)
            )
    except Exception:
        pass


def _purge_expired_sessions() -> None:
    try:
        with get_connection() as conn:
            conn.execute(
                "DELETE FROM admin_sessions WHERE expires_at < ?",
                (_iso(_now()),),
            )
    except Exception:
        pass


def _lookup_session(token: str) -> Optional[str]:
    """Return the username if the token is valid and not expired."""
    if not token:
        return None
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT username, expires_at FROM admin_sessions
                 WHERE token = ?
                """,
                (token,),
            ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    exp = _parse(row["expires_at"])
    if exp is None or exp <= _now():
        # Expired — clean it up lazily.
        destroy_session(token)
        return None
    return row["username"]


def authenticate(username: str, password: str) -> bool:
    """Return True iff (username, password) match a row in admin_users."""
    if not username or not password:
        return False
    with get_connection() as conn:
        row = conn.execute(
            "SELECT password_hash FROM admin_users WHERE username = ?",
            (username.strip(),),
        ).fetchone()
    if row is None:
        return False
    return verify_password(password, row["password_hash"])


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------

def _extract_token(request: Request) -> str:
    return request.cookies.get(_cfg.SESSION_COOKIE_NAME, "") or ""


def get_current_user(request: Request) -> Optional[str]:
    """Return the signed-in admin username, or None."""
    token = _extract_token(request)
    return _lookup_session(token)


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

class RedirectToLogin(Exception):
    """Raised by ``require_admin_page`` to trigger a 302 redirect."""
    def __init__(self, path: str = "/admin/login"):
        self.path = path


def require_admin_api(request: Request) -> str:
    """Dependency for JSON endpoints — raises 401 if not signed in."""
    from fastapi import HTTPException
    user = get_current_user(request)
    if not user:
        raise HTTPException(
            status_code=401, detail="Not authenticated"
        )
    return user


def require_admin_page(request: Request) -> str:
    """Dependency for HTML pages — raises RedirectToLogin if not signed in."""
    user = get_current_user(request)
    if not user:
        raise RedirectToLogin("/admin/login")
    return user


def redirect_to_login_response() -> RedirectResponse:
    """Helper to build a 302 to the login page."""
    return RedirectResponse("/admin/login", status_code=302)


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

def purge_expired_sessions() -> None:
    """Public wrapper so the startup hook can call it."""
    _purge_expired_sessions()
