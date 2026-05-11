# -*- coding: utf-8 -*-
"""
server/app/combined_main.py
---------------------------
PRODUCTION (Railway) entry point — bundles the public site + license
API + admin dashboard into a single FastAPI process.

Run with:
    cd server
    python -m uvicorn app.combined_main:app --host 0.0.0.0 --port $PORT

Routes:
    /                      — redirects to /site/
    /health                — liveness probe (used by Railway healthcheck)
    /site/*                — marketing + purchase website
    /license/*             — desktop-client license API (no auth)
    /checkout/*            — public purchase flow
    /webhook/*             — payment-provider webhooks
    /admin/login           — session-based admin login
    /admin/*               — Jinja-rendered admin dashboard (session)
    /admin/api/*           — JSON admin API (session-protected)
    /static/admin/*        — admin UI assets

Why one process?
----------------
On Railway, running both ``app.main`` and ``app.admin_main`` as
separate services costs $5 each ($10/mo total). This combined entry
keeps cost at $5/mo while still keeping the admin behind a session
login. For full isolation (separate subdomain, separate cost),
deploy ``app.main`` and ``app.admin_main`` to two Railway services
instead.

Local development still uses ``app.main`` (public, port 8000) and
``app.admin_main`` (admin, port 8001) as separate processes for
clean separation while testing.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import RedirectToLogin, purge_expired_sessions
from .database import DB_PATH, init_db
from .routes import admin_api, admin_pages, checkout, licenses, webhooks


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


app = FastAPI(
    title="Magnet Frame Pro — Combined Production Server",
    version="2.0.0",
)


# ---------------------------------------------------------------------------
# CORS — in production, lock allowed origins to the real public domain
# plus localhost (so the desktop app can still reach /license/* during
# development). Set MFP_ALLOWED_ORIGINS env var to override.
# ---------------------------------------------------------------------------
_default_origins = (
    "https://magnetframepro.co.il,"
    "https://www.magnetframepro.co.il,"
    "http://127.0.0.1:8000,"
    "http://localhost:8000"
)
_origins_raw = os.environ.get("MFP_ALLOWED_ORIGINS", _default_origins)
_origins = [o.strip() for o in _origins_raw.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
logger.info("CORS allow_origins=%s", _origins)


# ---------------------------------------------------------------------------
# Static — admin UI assets (CSS / JS shipped with the templates).
# ---------------------------------------------------------------------------
_STATIC_DIR = Path(__file__).parent / "static"
_STATIC_DIR.mkdir(parents=True, exist_ok=True)
(_STATIC_DIR / "admin").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Marketing site — static files from ``server/site/`` so Railway (which has
# Root Directory = ``server``) can find the assets. Local dev copies live
# at both server/site/ and the project-root site/; the deployed copy is
# server/site/.
# ---------------------------------------------------------------------------
_SITE_DIR = Path(__file__).parent.parent / "site"
try:
    _SITE_DIR.mkdir(parents=True, exist_ok=True)
    _site_index = _SITE_DIR / "index.html"
    if not _site_index.exists():
        _legacy = _SITE_DIR / "magnet-frame-pro-SITE.html"
        if _legacy.exists():
            try:
                _site_index.write_bytes(_legacy.read_bytes())
            except Exception:
                pass
    app.mount(
        "/site",
        StaticFiles(directory=str(_SITE_DIR), html=True),
        name="site",
    )
except Exception:
    logger.exception("site/ mount failed — purchase button will 404")


# ---------------------------------------------------------------------------
# Jinja templates (admin pages).
# ---------------------------------------------------------------------------
templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates")
)
app.state.templates = templates


# ---------------------------------------------------------------------------
# Routers — public + admin together.
# ---------------------------------------------------------------------------
app.include_router(licenses.router,    prefix="/license",    tags=["license"])
app.include_router(checkout.router,    prefix="/checkout",   tags=["checkout"])
app.include_router(webhooks.router,    prefix="/webhook",    tags=["webhook"])
app.include_router(admin_api.router,   prefix="/admin/api",  tags=["admin-api"])
app.include_router(admin_pages.router, prefix="/admin",      tags=["admin-pages"])


# ---------------------------------------------------------------------------
# Startup / health / redirects
# ---------------------------------------------------------------------------
@app.on_event("startup")
def _on_startup() -> None:
    init_db()
    purge_expired_sessions()
    logger.info("License DB ready at %s (COMBINED production server)", DB_PATH)


@app.get("/health")
def health():
    return {"ok": True, "service": "magnet-frame-pro", "version": "2.0.0"}


@app.get("/", include_in_schema=False)
def root():
    """Visitors hitting the bare root land on the marketing site."""
    return RedirectResponse("/site/", status_code=302)


# ---------------------------------------------------------------------------
# Admin exception handlers — convert RedirectToLogin into a real 302
# and pass-through HTTPException-as-redirect cleanly.
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
