# -*- coding: utf-8 -*-
"""
server/app/admin_main.py
------------------------
ADMIN-ONLY FastAPI application — completely separate from the public
website (``app.main``).

Run with:
    cd server
    python -m uvicorn app.admin_main:app --reload --port 8001

Routes:
    /                      — redirects to /admin/dashboard (or /admin/login)
    /health                — liveness probe
    /static/admin/*        — admin UI assets (CSS / JS)
    /admin/*               — Jinja-rendered admin dashboard
    /admin/api/*           — JSON admin API (session-protected)

Why a separate process?
-----------------------
The public site (``app.main`` on port 8000) intentionally does NOT
mount the admin routers, so a misconfigured reverse proxy or open
firewall on the public host can never accidentally leak the admin UI.

Both processes share the same ``licenses.db`` (SQLite is fine with
concurrent reads/writes thanks to the file-lock timeout configured in
``app.database``). Order of startup does not matter — each process
calls ``init_db()`` independently.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import RedirectToLogin, purge_expired_sessions
from .database import DB_PATH, init_db
from .routes import admin_api, admin_pages


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


app = FastAPI(
    title="Magnet Frame Pro — Admin Dashboard",
    version="2.0.0",
)


# ---------------------------------------------------------------------------
# Static files — admin UI assets only. Public site assets are served by
# the other process (app.main).
# ---------------------------------------------------------------------------
_STATIC_DIR = Path(__file__).parent / "static"
_STATIC_DIR.mkdir(parents=True, exist_ok=True)
(_STATIC_DIR / "admin").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Jinja templates — shared instance for admin_pages.
# ---------------------------------------------------------------------------
templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates")
)
app.state.templates = templates


# ---------------------------------------------------------------------------
# Admin routers
# ---------------------------------------------------------------------------
app.include_router(admin_api.router, prefix="/admin/api", tags=["admin-api"])
app.include_router(admin_pages.router, prefix="/admin", tags=["admin-pages"])


# ---------------------------------------------------------------------------
# Startup / health / redirects
# ---------------------------------------------------------------------------
@app.on_event("startup")
def _on_startup() -> None:
    init_db()
    purge_expired_sessions()
    logger.info("License DB ready at %s (ADMIN server)", DB_PATH)


@app.get("/health")
def health():
    """Lightweight liveness probe."""
    return {"ok": True, "service": "license-server-admin", "version": "2.0.0"}


@app.get("/", include_in_schema=False)
def root():
    """Bare root on the admin host redirects to the dashboard.

    The admin_pages router will further redirect to /admin/login when
    the caller is unauthenticated.
    """
    return RedirectResponse("/admin/", status_code=302)


# ---------------------------------------------------------------------------
# Custom handlers — admin page routes raise RedirectToLogin when the
# caller is unauthenticated; convert it into a 302 transparently.
# ---------------------------------------------------------------------------
@app.exception_handler(RedirectToLogin)
async def _redirect_to_login(_request: Request, exc: RedirectToLogin):
    return RedirectResponse(exc.path, status_code=302)


@app.exception_handler(FastAPIHTTPException)
async def _http_exception(request: Request, exc: FastAPIHTTPException):
    if (
        exc.status_code in (302, 303)
        and exc.headers
        and exc.headers.get("Location")
    ):
        return RedirectResponse(
            exc.headers["Location"], status_code=exc.status_code
        )
    from fastapi.responses import JSONResponse
    return JSONResponse(
        {"detail": exc.detail}, status_code=exc.status_code,
        headers=exc.headers,
    )
