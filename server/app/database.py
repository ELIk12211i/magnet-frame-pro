# -*- coding: utf-8 -*-
"""
server/app/database.py
----------------------
SQLite wrapper for the Magnet Frame Pro license server.

Tables
------
* ``licenses``        — one row per serial key (paid, lifetime or trial).
* ``trials``          — tracks which machines have already started a trial.
* ``activations``     — one row per (serial_key, machine_id) attempt.
* ``events``          — full audit log of every license-related event.
* ``admin_users``     — admin accounts for the web dashboard.
* ``admin_sessions``  — server-side sessions for signed-in admins.

Event type strings used by the events table:
  activation_success, activation_failed, activation_machine_mismatch,
  activation_already_used,
  validation_success, validation_failed, validation_expired,
  trial_started, trial_already_used, trial_expired,
  yearly_expired, switched_to_demo, reset_machine, disabled_license,
  license_created, license_disabled, license_enabled, license_reset,
  license_extended, admin_login, admin_logout, admin_login_failed
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import os
import sqlite3
from pathlib import Path
from typing import Iterator

from . import config as _cfg


# ---------------------------------------------------------------------------
# Database path resolution.
# Supports:
#   - Absolute path via DATABASE_PATH
#   - Relative path (resolved against server/ folder)
# ---------------------------------------------------------------------------
_SERVER_ROOT = Path(__file__).resolve().parent.parent  # → server/

_raw_db = _cfg.DATABASE_PATH or "licenses.db"
_db_path = Path(_raw_db)
if not _db_path.is_absolute():
    _db_path = _SERVER_ROOT / _db_path
DB_PATH: Path = _db_path


def _connect() -> sqlite3.Connection:
    """Open a new connection with sensible defaults.

    - ``check_same_thread=False`` lets a connection survive being handed
      between FastAPI worker threads (each request already gets its own
      connection via :func:`get_connection`, but this keeps us forward
      compatible with connection pooling).
    - ``timeout=15.0`` makes writes wait instead of failing with
      "database is locked" under normal WAL concurrency.
    """
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 15000;")
    return conn


@contextlib.contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """Context manager yielding a connection; commits on normal exit."""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, ddl: str
) -> None:
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


# ---------------------------------------------------------------------------
# Password hashing helpers live here to avoid a circular import with auth.py
# during first-boot seeding.
# ---------------------------------------------------------------------------
def _hash_password(password: str) -> str:
    """Produce a ``pbkdf2_sha256$<iter>$<salt>$<hash>`` string."""
    import hashlib
    import base64
    import secrets as _secrets

    iterations = 200_000
    salt = _secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    salt_b64 = base64.b64encode(salt).decode("ascii")
    digest_b64 = base64.b64encode(digest).decode("ascii")
    return f"pbkdf2_sha256${iterations}${salt_b64}${digest_b64}"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _seed_default_plans(conn: sqlite3.Connection) -> None:
    """Insert the out-of-the-box subscription plans on first DB boot.

    The catalogue is intentionally short — admins can add/remove plans
    at any time from the ``/admin/plans`` dashboard page.
    """
    now = _now_iso()
    defaults = [
        # (name, days, license_type, sort_order, is_system)
        # "תוכנית ניסיון" is flagged as system so the admin can edit its
        # fields but not delete it — it's the plan the desktop client's
        # "התחל תוכנית ניסיון" button maps to.
        ("תוכנית ניסיון",    14,   "trial_14_days", 10, 1),
        ("חודשי",            30,   "yearly",        20, 0),
        ("3 חודשים",         90,   "yearly",        30, 0),
        ("6 חודשים",         180,  "yearly",        40, 0),
        ("שנתי",             365,  "yearly",        50, 0),
        # "5 שנים" matches the second plan card on the purchase site
        # (1825 days). The site's plan-key mapping uses the Hebrew name
        # verbatim so changing this string requires an update to
        # ``site/index.html`` as well.
        ("5 שנים",           1825, "yearly",        55, 0),
        ("לצמיתות",          None, "lifetime",      60, 0),
    ]
    for name, days, lic_type, sort_order, is_system in defaults:
        conn.execute(
            """
            INSERT OR IGNORE INTO subscription_plans
            (name, days, license_type, is_active, sort_order,
             created_at, updated_at, is_system)
            VALUES (?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (name, days, lic_type, sort_order, now, now, is_system),
        )


def init_db() -> None:
    """Create (or migrate) the tables. Idempotent — safe on every boot."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        # ------------------------------------------------------------------
        # Base tables — original schema (unchanged for backwards compat).
        # ------------------------------------------------------------------
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS licenses (
                serial_key   TEXT PRIMARY KEY,
                license_type TEXT NOT NULL,
                machine_id   TEXT,
                status       TEXT NOT NULL DEFAULT 'unused',
                activated_at TEXT,
                expires_at   TEXT,
                created_at   TEXT NOT NULL,
                notes        TEXT
            );

            CREATE TABLE IF NOT EXISTS trials (
                machine_id  TEXT PRIMARY KEY,
                started_at  TEXT NOT NULL,
                expires_at  TEXT NOT NULL,
                serial_key  TEXT NOT NULL
            );

            -- Tracks every machine that has EVER held a paid license
            -- (or had its binding migrated away).  start_trial consults
            -- this so a machine whose license moved to another box
            -- can't fall back to a fresh 14-day trial as if it were
            -- new.  Wiped together with the rest of the licensing
            -- history by the admin "אפס הכול" button.
            CREATE TABLE IF NOT EXISTS machine_history (
                machine_id   TEXT PRIMARY KEY,
                last_serial  TEXT,
                reason       TEXT NOT NULL,
                recorded_at  TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_licenses_machine
                ON licenses(machine_id);
            CREATE INDEX IF NOT EXISTS idx_licenses_status
                ON licenses(status);
            CREATE INDEX IF NOT EXISTS idx_licenses_type
                ON licenses(license_type);
            CREATE INDEX IF NOT EXISTS idx_licenses_created
                ON licenses(created_at);
            """
        )

        # ------------------------------------------------------------------
        # Additive migrations on licenses.
        # ------------------------------------------------------------------
        _add_column_if_missing(conn, "licenses", "customer_name",
                               "TEXT DEFAULT ''")
        _add_column_if_missing(conn, "licenses", "customer_email",
                               "TEXT DEFAULT ''")
        _add_column_if_missing(conn, "licenses", "customer_phone",
                               "TEXT DEFAULT ''")
        _add_column_if_missing(conn, "licenses", "customer_first_name",
                               "TEXT DEFAULT ''")
        _add_column_if_missing(conn, "licenses", "customer_last_name",
                               "TEXT DEFAULT ''")
        # Plan-name snapshot — the ``subscription_plans.name`` chosen
        # when the license was issued.  Surfaced in the desktop client
        # settings panel so users see "פרימיום — למשך 90 יום" instead
        # of the raw ``license_type`` enum value.
        _add_column_if_missing(conn, "licenses", "plan_name",
                               "TEXT DEFAULT ''")
        _add_column_if_missing(conn, "licenses", "plan_days",
                               "INTEGER")
        # Hardware ID is the canonical license-binding identifier —
        # stable across network changes, reboots, Windows updates.
        # ``machine_id`` (IP) remains for diagnostics / display only.
        _add_column_if_missing(conn, "licenses", "hardware_id",
                               "TEXT DEFAULT ''")
        # Client-reported machine details (hostname + public IP as
        # observed by the client's own network stack) — so the admin
        # dashboard can show exactly what the user sees in the app.
        _add_column_if_missing(conn, "licenses", "hostname",
                               "TEXT DEFAULT ''")
        _add_column_if_missing(conn, "licenses", "client_public_ip",
                               "TEXT DEFAULT ''")
        _add_column_if_missing(conn, "licenses", "last_validation_at",
                               "TEXT")
        _add_column_if_missing(conn, "licenses", "disabled_at",
                               "TEXT")
        _add_column_if_missing(conn, "licenses", "disabled_reason",
                               "TEXT DEFAULT ''")
        _add_column_if_missing(conn, "licenses", "disabled_by",
                               "TEXT DEFAULT ''")
        # ----- Periodic validation bookkeeping (additive, non-breaking) -----
        _add_column_if_missing(conn, "licenses", "next_validation_due_at",
                               "TEXT")
        _add_column_if_missing(conn, "licenses", "validation_status",
                               "TEXT DEFAULT ''")
        _add_column_if_missing(conn, "licenses", "validation_message",
                               "TEXT DEFAULT ''")

        # ------------------------------------------------------------------
        # Additive: activations + events + indexes.
        # ------------------------------------------------------------------
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS activations (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                serial_key    TEXT NOT NULL,
                machine_id    TEXT NOT NULL,
                activated_at  TEXT NOT NULL,
                last_seen_at  TEXT,
                status        TEXT NOT NULL DEFAULT 'active',
                ip            TEXT,
                notes         TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_activations_serial
                ON activations(serial_key);
            CREATE INDEX IF NOT EXISTS idx_activations_machine
                ON activations(machine_id);
            CREATE INDEX IF NOT EXISTS idx_activations_last_seen
                ON activations(last_seen_at);

            CREATE TABLE IF NOT EXISTS events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                serial_key TEXT,
                machine_id TEXT,
                event_type TEXT NOT NULL,
                message    TEXT,
                ip         TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_serial
                ON events(serial_key);
            CREATE INDEX IF NOT EXISTS idx_events_type
                ON events(event_type);
            CREATE INDEX IF NOT EXISTS idx_events_created
                ON events(created_at);
            """
        )

        # Additive on activations/events.
        _add_column_if_missing(conn, "activations", "user_agent",
                               "TEXT DEFAULT ''")
        _add_column_if_missing(conn, "activations", "machine_uuid",
                               "TEXT DEFAULT ''")
        _add_column_if_missing(conn, "events", "actor",
                               "TEXT DEFAULT ''")

        # ------------------------------------------------------------------
        # Subscription plans — user-defined plan catalogue feeding the
        # license generator UI.  Plans are simple (name + days) so an
        # admin can add "3 חודשים" / "חצי שנה" / "פרימיום" etc. at any time.
        # ------------------------------------------------------------------
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS subscription_plans (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,
                days        INTEGER,
                license_type TEXT NOT NULL DEFAULT 'yearly',
                is_active   INTEGER NOT NULL DEFAULT 1,
                sort_order  INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_plans_active
                ON subscription_plans(is_active);
            CREATE INDEX IF NOT EXISTS idx_plans_sort
                ON subscription_plans(sort_order);
            """
        )

        # Additive migration: free-text custom type label for plans.
        _add_column_if_missing(
            conn, "subscription_plans", "custom_type", "TEXT DEFAULT ''",
        )

        # Additive migration: system-plan flag.  System plans cannot be
        # deleted or renamed through the admin UI — they are reserved
        # for fixed app flows (e.g. the "תוכנית ניסיון" row that the
        # desktop client activates via the "התחל תוכנית נסיון" button).
        _add_column_if_missing(
            conn, "subscription_plans", "is_system", "INTEGER NOT NULL DEFAULT 0",
        )

        # Additive migration: price in ILS (used by the public website
        # checkout to render prices next to each plan).
        _add_column_if_missing(
            conn, "subscription_plans", "price_ils", "REAL NOT NULL DEFAULT 0",
        )

        # Seed the default catalogue on first run.  Admins can edit
        # or delete these later through the /admin/plans page.
        row = conn.execute("SELECT COUNT(*) AS c FROM subscription_plans").fetchone()
        if (row["c"] if row else 0) == 0:
            _seed_default_plans(conn)
        else:
            # One-shot migration for older databases: rename the legacy
            # "ניסיון" plan to "תוכנית ניסיון" and mark it as system so
            # it becomes the canonical trial plan.  If the admin had
            # already renamed / deleted it, we simply re-insert the row
            # (IGNORE on conflict) and flag it as system.
            try:
                conn.execute(
                    "UPDATE subscription_plans "
                    "SET name = 'תוכנית ניסיון', is_system = 1 "
                    "WHERE name = 'ניסיון'"
                )
                # Guarantee the row exists even on DBs where admins
                # renamed/deleted it — recreate with default values.
                row = conn.execute(
                    "SELECT id FROM subscription_plans WHERE is_system = 1"
                ).fetchone()
                if row is None:
                    now = _now_iso()
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO subscription_plans
                        (name, days, license_type, is_active, sort_order,
                         created_at, updated_at, is_system)
                        VALUES ('תוכנית ניסיון', 14, 'trial_14_days', 1, 10, ?, ?, 1)
                        """,
                        (now, now),
                    )
            except Exception:
                pass

        # Additive migration — ensure the two website-card plans
        # ("שנתי" 365-day + "5 שנים" 1825-day) exist and are active
        # on every boot. The site's pricing cards reference these
        # specifically; if the admin deactivated or deleted them in
        # an older session the website couldn't resolve a plan_id at
        # checkout. INSERT OR IGNORE keeps any custom name/price the
        # admin set; the UPDATE below force-reactivates a row that
        # exists but was hidden via is_active=0.
        try:
            now = _now_iso()
            for _name, _days, _sort in (
                ("שנתי",   365,  50),
                ("5 שנים", 1825, 55),
            ):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO subscription_plans
                    (name, days, license_type, is_active, sort_order,
                     created_at, updated_at, is_system)
                    VALUES (?, ?, 'yearly', 1, ?, ?, ?, 0)
                    """,
                    (_name, _days, _sort, now, now),
                )
                # Re-activate a row that already existed but was
                # marked is_active=0 by an admin — the website needs
                # both cards live regardless of admin toggles.
                conn.execute(
                    """
                    UPDATE subscription_plans
                       SET is_active = 1
                     WHERE days = ?
                       AND license_type = 'yearly'
                       AND is_active = 0
                    """,
                    (_days,),
                )
        except Exception:
            pass

        # ------------------------------------------------------------------
        # Orders — one row per checkout attempt originating from the
        # public website (``site/``).  The payment webhook updates the
        # row as the transaction progresses (pending → paid / failed /
        # refunded) and links the issued license back via
        # ``license_serial``.  ``provider_txn_id`` is UNIQUE to
        # guarantee idempotency when a webhook fires more than once.
        # ------------------------------------------------------------------
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                provider        TEXT NOT NULL DEFAULT 'unknown',
                provider_txn_id TEXT,
                amount_cents    INTEGER NOT NULL DEFAULT 0,
                currency        TEXT NOT NULL DEFAULT 'ILS',
                plan_key        TEXT NOT NULL DEFAULT '',
                customer_name   TEXT NOT NULL DEFAULT '',
                customer_email  TEXT NOT NULL DEFAULT '',
                customer_phone  TEXT NOT NULL DEFAULT '',
                license_id      INTEGER,
                license_serial  TEXT,
                status          TEXT NOT NULL DEFAULT 'pending',
                created_at      TEXT NOT NULL,
                paid_at         TEXT,
                failed_at       TEXT,
                failure_reason  TEXT,
                raw_payload     TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_provider_txn
                ON orders(provider, provider_txn_id)
                WHERE provider_txn_id IS NOT NULL AND provider_txn_id != '';
            CREATE INDEX IF NOT EXISTS idx_orders_status
                ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_orders_email
                ON orders(customer_email);
            CREATE INDEX IF NOT EXISTS idx_orders_created
                ON orders(created_at DESC);
            """
        )

        # ------------------------------------------------------------------
        # Trial leads — captured from the desktop "התחל תוכנית נסיון"
        # dialog.  Every click creates a row here so the admin can see
        # who requested a trial, even before the client follows up.
        # A row exists per (machine_id, serial_key) pair so re-clicks
        # (the trial limit was removed) create additional rows instead
        # of overwriting the first one — the admin keeps the full
        # history.
        # ------------------------------------------------------------------
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trial_leads (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                machine_id  TEXT NOT NULL,
                serial_key  TEXT,
                name        TEXT NOT NULL DEFAULT '',
                phone       TEXT NOT NULL DEFAULT '',
                ip          TEXT NOT NULL DEFAULT '',
                user_agent  TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trial_leads_created
                ON trial_leads(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_trial_leads_machine
                ON trial_leads(machine_id);
            """
        )

        # ------------------------------------------------------------------
        # NEW admin auth tables.
        # ------------------------------------------------------------------
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS admin_users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admin_sessions (
                token       TEXT PRIMARY KEY,
                username    TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                expires_at  TEXT NOT NULL,
                ip          TEXT,
                user_agent  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_admin_sessions_expires
                ON admin_sessions(expires_at);
            """
        )

        # ------------------------------------------------------------------
        # Seed a default admin user if the table is empty.
        # ------------------------------------------------------------------
        row = conn.execute("SELECT COUNT(*) AS c FROM admin_users").fetchone()
        if (row["c"] if row else 0) == 0:
            username = (os.environ.get("ADMIN_USERNAME")
                        or _cfg.ADMIN_USERNAME or "").strip()
            password = (os.environ.get("ADMIN_PASSWORD")
                        or _cfg.ADMIN_PASSWORD or "")
            if not username or not password:
                raise RuntimeError(
                    "Cannot seed admin_users: ADMIN_USERNAME and "
                    "ADMIN_PASSWORD must be set via environment variables "
                    "or server/.env before first boot."
                )
            conn.execute(
                """
                INSERT INTO admin_users (username, password_hash, created_at)
                VALUES (?, ?, ?)
                """,
                (username, _hash_password(password), _now_iso()),
            )
