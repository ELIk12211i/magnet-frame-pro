# -*- coding: utf-8 -*-
"""
server/app/main.py
------------------
PUBLIC FastAPI application — serves the marketing website + customer-
facing license API ONLY.

Run with:
    cd server
    python -m uvicorn app.main:app --reload --port 8000

Routes:
    /                      — redirects to /site/
    /health                — liveness probe
    /site/*                — marketing + purchase website (static files)
    /license/*             — client-facing license API (no auth)
    /checkout/*            — public purchase flow
    /webhook/*             — payment-provider webhooks

The admin dashboard (``/admin/*``) is intentionally NOT mounted here.
Run it as a separate process via ``app.admin_main:app`` on a different
port so the public site never exposes the admin UI or its JSON API,
even if the admin host is left publicly reachable by mistake.

Both processes share the same ``licenses.db`` (SQLite handles
concurrent read/write across processes with the configured file
lock timeout).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .database import DB_PATH, init_db
from .routes import checkout, licenses, webhooks
from .security import SecurityMiddleware


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


app = FastAPI(
    title="Magnet Frame Pro — Public License Server",
    version="2.0.0",
)


# ---------------------------------------------------------------------------
# CORS — the desktop client is NOT a browser, so CORS never applies to it;
# only browser callers (the bundled marketing/checkout site, served from
# this same origin) are affected. We therefore lock origins to the real
# domain + localhost instead of "*", mirroring the production combined
# server. Override with MFP_ALLOWED_ORIGINS. allow_credentials stays False
# (the public API uses no cookies).
# ---------------------------------------------------------------------------
_default_origins = (
    "https://magnetframepro.co.il,"
    "https://www.magnetframepro.co.il,"
    "http://127.0.0.1:8000,"
    "http://localhost:8000"
)
_origins = [
    o.strip()
    for o in os.environ.get("MFP_ALLOWED_ORIGINS", _default_origins).split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiting + security headers (no admin routes here → protect_admin=False).
app.add_middleware(SecurityMiddleware, protect_admin=False)


# ---------------------------------------------------------------------------
# Marketing / purchase website — static files served from project-root
# ``site/`` so the desktop app's "רכישה" / renew buttons can open
# ``http://127.0.0.1:8000/site/`` without depending on the licensing API.
# ---------------------------------------------------------------------------
_SITE_DIR = Path(__file__).parent.parent.parent / "site"
try:
    _SITE_DIR.mkdir(parents=True, exist_ok=True)
    _site_index = _SITE_DIR / "index.html"
    if not _site_index.exists():
        # Auto-promote a legacy SITE file as the default index so the
        # storefront works the moment the route is mounted.
        _legacy_site = _SITE_DIR / "magnet-frame-pro-SITE.html"
        if _legacy_site.exists():
            try:
                _site_index.write_bytes(_legacy_site.read_bytes())
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
# Public routers — license / checkout / webhooks ONLY.
# Admin routers (admin_pages, admin_api) deliberately omitted; run them
# via app.admin_main on a separate port.
# ---------------------------------------------------------------------------
app.include_router(licenses.router, prefix="/license", tags=["license"])
app.include_router(checkout.router, prefix="/checkout", tags=["checkout"])
app.include_router(webhooks.router, prefix="/webhook", tags=["webhook"])


# ---------------------------------------------------------------------------
# Startup / health / redirects
# ---------------------------------------------------------------------------
@app.on_event("startup")
def _on_startup() -> None:
    init_db()
    logger.info("License DB ready at %s (PUBLIC server)", DB_PATH)


@app.get("/health")
def health():
    """Lightweight liveness probe."""
    return {"ok": True, "service": "license-server-public", "version": "2.0.0"}


@app.get("/", include_in_schema=False)
def root():
    """Bare root redirects to the marketing site (not the admin UI)."""
    return RedirectResponse("/site/", status_code=302)
