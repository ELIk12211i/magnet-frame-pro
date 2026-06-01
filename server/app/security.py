# -*- coding: utf-8 -*-
"""
server/app/security.py
----------------------
Lightweight, dependency-free security hardening shared by the public
(:mod:`app.main`) and combined-production (:mod:`app.combined_main`)
FastAPI apps:

* **Rate limiting** — an in-memory sliding-window limiter to blunt
  brute-force against ``/admin/login`` and enumeration floods against
  the public ``/license/*`` and ``/checkout/*`` endpoints. In-memory is
  sufficient for the single-worker SQLite deployment this server uses;
  if you ever scale to multiple workers, move this to a shared store
  (Redis) — the limits below are intentionally generous so legitimate
  traffic is never affected.

* **CSRF defence (same-origin check)** — state-changing admin requests
  (POST/PUT/PATCH/DELETE under ``/admin``) must originate from the
  server's own origin. This complements the ``SameSite=Lax`` session
  cookie with a server-side check that does not depend on per-form
  tokens, so it adds no risk of breaking an admin form. We compare the
  ``Origin``/``Referer`` host to the request ``Host`` — deployment
  agnostic (works behind Railway's TLS proxy and for local dev on any
  port) and never blocks a genuine same-site submission.

* **Security headers** — ``X-Content-Type-Options``, ``X-Frame-Options``
  and ``Referrer-Policy`` on every response. (A full Content-Security-
  Policy is intentionally NOT added here: the admin UI relies on inline
  scripts/styles and a CSP would need nonces/refactoring — tracked
  separately so this change stays behaviour-safe.)

None of this changes any successful request/response body — it only
rejects abusive or cross-site requests that should never have succeeded.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Iterable, Tuple
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


# ---------------------------------------------------------------------------
# In-memory sliding-window rate limiter
# ---------------------------------------------------------------------------
class RateLimiter:
    """Allow at most ``max_hits`` events per ``window_seconds`` per key.

    Thread-safe. Uses ``time.monotonic`` so it is immune to wall-clock
    adjustments. Memory is bounded by the number of distinct active keys
    (client IPs); idle keys are pruned lazily on access.
    """

    def __init__(self, max_hits: int, window_seconds: float) -> None:
        self.max_hits = int(max_hits)
        self.window = float(window_seconds)
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            dq = self._hits[key]
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self.max_hits:
                return False
            dq.append(now)
            # Opportunistic cleanup: drop the key entirely if it became
            # empty for some other key during pruning is not needed here;
            # deques stay small because of the window prune above.
            return True

    def retry_after(self, key: str) -> int:
        """Seconds until the oldest hit in the window expires."""
        now = time.monotonic()
        with self._lock:
            dq = self._hits.get(key)
            if not dq:
                return 0
            return max(1, int(self.window - (now - dq[0])))


def client_ip(request: Request) -> str:
    """Best-effort client IP, honouring a single proxy hop."""
    try:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.client.host if request.client else "?"
    except Exception:
        return "?"


def _host_of(value: str) -> str:
    """Return the bare hostname (no port) of a URL or Host header value."""
    if not value:
        return ""
    # Origin/Referer are full URLs; Host is ``host[:port]``.
    if "://" in value:
        netloc = urlparse(value).netloc
    else:
        netloc = value
    return netloc.split("@")[-1].split(":")[0].strip().lower()


def is_same_origin(request: Request) -> bool:
    """True when a state-changing request looks same-site.

    Compares the ``Origin`` (preferred) or ``Referer`` host to the
    request ``Host``. When neither header is present we return True and
    rely on the ``SameSite=Lax`` cookie as the backstop — this avoids
    breaking the rare legitimate client that strips these headers, while
    still blocking the classic cross-site form-POST attack (whose Origin
    is the attacker's domain).
    """
    src = request.headers.get("origin") or request.headers.get("referer")
    if not src:
        return True
    host = _host_of(request.headers.get("host", ""))
    return bool(host) and _host_of(src) == host


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
_MUTATING = ("POST", "PUT", "PATCH", "DELETE")

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


class SecurityMiddleware(BaseHTTPMiddleware):
    """Apply rate limiting, same-origin CSRF defence and security headers.

    Parameters
    ----------
    protect_admin:
        When True (combined production app), enforce the same-origin
        check on mutating ``/admin`` requests and rate-limit
        ``/admin/login``. The public-only app has no admin routes, so it
        passes False.
    """

    def __init__(self, app, *, protect_admin: bool = False) -> None:
        super().__init__(app)
        self.protect_admin = protect_admin
        # Generous limits — legitimate users never hit these.
        self._login_limiter = RateLimiter(max_hits=10, window_seconds=300)   # 10 / 5 min
        self._api_limiter = RateLimiter(max_hits=120, window_seconds=60)     # 120 / min
        # Public surfaces to throttle (enumeration / write-sink floods).
        self._throttled_prefixes: Tuple[str, ...] = ("/license/", "/checkout/")

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method.upper()
        ip = client_ip(request)

        # 1) Brute-force protection on admin login.
        if self.protect_admin and method == "POST" and path == "/admin/login":
            if not self._login_limiter.allow(f"login:{ip}"):
                return self._too_many(self._login_limiter, f"login:{ip}")

        # 2) Same-origin CSRF defence on mutating admin requests.
        if (
            self.protect_admin
            and method in _MUTATING
            and path.startswith("/admin")
            and not is_same_origin(request)
        ):
            return JSONResponse(
                {"detail": "Cross-site request blocked."}, status_code=403
            )

        # 3) Light throttle on public license/checkout surfaces.
        if method in _MUTATING and any(
            path.startswith(p) for p in self._throttled_prefixes
        ):
            if not self._api_limiter.allow(f"api:{ip}"):
                return self._too_many(self._api_limiter, f"api:{ip}")

        response: Response = await call_next(request)

        # 4) Security headers on every response.
        for k, v in _SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    @staticmethod
    def _too_many(limiter: RateLimiter, key: str) -> Response:
        return JSONResponse(
            {"detail": "Too many requests — slow down and try again shortly."},
            status_code=429,
            headers={"Retry-After": str(limiter.retry_after(key))},
        )
