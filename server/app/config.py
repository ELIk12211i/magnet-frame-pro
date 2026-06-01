# -*- coding: utf-8 -*-
"""
server/app/config.py
--------------------
Tiny config module — reads environment variables with defaults so the
server can boot zero-config in development and be overridden in prod.
"""

from __future__ import annotations

import os
from pathlib import Path


# Load a .env file if present (very lightweight parser, no extra deps).
def _load_dotenv() -> None:
    here = Path(__file__).resolve().parent.parent  # → server/
    env_path = here / ".env"
    if not env_path.exists():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
    except Exception:
        # Config loading must never crash the server.
        pass


_load_dotenv()


ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
# NOTE: SECRET_KEY is currently NOT used to sign anything. Admin sessions
# use opaque random tokens stored in the ``admin_sessions`` table (see
# ``app.auth``), so a session is invalidated by deleting its DB row, NOT
# by rotating this key. Kept for forward-compatibility (e.g. if cookie
# signing is added later) and to satisfy deployment docs. Rotating it has
# no effect on existing sessions today.
SECRET_KEY = os.environ.get("SECRET_KEY", "")
SESSION_COOKIE_NAME = os.environ.get("SESSION_COOKIE_NAME", "admin_session")
DATABASE_PATH = os.environ.get("DATABASE_PATH", "licenses.db")

# Payment provider id used by ``services.payments_service``.
# "none" (default) = dev mode (no charge); set to grow/cardcom/payplus/stripe
# once a merchant account + adapter are ready.
PAYMENT_PROVIDER = os.environ.get("PAYMENT_PROVIDER", "none")

try:
    SESSION_LIFETIME_DAYS = int(os.environ.get("SESSION_LIFETIME_DAYS", "30"))
except Exception:
    SESSION_LIFETIME_DAYS = 30
