# -*- coding: utf-8 -*-
"""
server/app/services/events_service.py
-------------------------------------
Thin helper layer that writes to the ``events`` and ``activations`` tables.

All functions use :func:`app.database.get_connection` directly and are
designed to be cheap no-throw helpers so that logging failures never
propagate out of the main licensing flow.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional

from ..database import get_connection


# ---------------------------------------------------------------------------
# Canonical event types.
# ---------------------------------------------------------------------------
EVENT_ACTIVATION_SUCCESS          = "activation_success"
EVENT_ACTIVATION_FAILED           = "activation_failed"
EVENT_ACTIVATION_MACHINE_MISMATCH = "activation_machine_mismatch"
EVENT_ACTIVATION_ALREADY_USED     = "activation_already_used"
EVENT_VALIDATION_SUCCESS          = "validation_success"
EVENT_VALIDATION_FAILED           = "validation_failed"
EVENT_VALIDATION_EXPIRED          = "validation_expired"
EVENT_TRIAL_STARTED               = "trial_started"
EVENT_TRIAL_ALREADY_USED          = "trial_already_used"
EVENT_TRIAL_EXPIRED               = "trial_expired"
EVENT_YEARLY_EXPIRED              = "yearly_expired"
EVENT_SWITCHED_TO_DEMO            = "switched_to_demo"
EVENT_RESET_MACHINE               = "reset_machine"
EVENT_DISABLED_LICENSE            = "disabled_license"
EVENT_LICENSE_CREATED             = "license_created"
EVENT_LICENSE_DISABLED            = "license_disabled"
EVENT_LICENSE_ENABLED             = "license_enabled"
EVENT_LICENSE_RESET               = "license_reset"
EVENT_LICENSE_EXTENDED            = "license_extended"
EVENT_LICENSE_DELETED             = "license_deleted"
# --- Periodic validation events (additive) -------------------------------
EVENT_LICENSE_VALIDATED_SUCCESS        = "license_validated_success"
EVENT_LICENSE_VALIDATED_FAILED         = "license_validated_failed"
EVENT_LICENSE_VALIDATION_SKIPPED_NO_INTERNET = "license_validation_skipped_no_internet"
EVENT_LICENSE_VALIDATION_REQUIRED      = "license_validation_required"
EVENT_LICENSE_VALIDATION_RECOVERED     = "license_validation_recovered"
EVENT_LICENSE_SWITCHED_TO_DEMO_TIMEOUT = "license_switched_to_demo_due_to_validation_timeout"
EVENT_LICENSE_SWITCHED_TO_DEMO_INVALID = "license_switched_to_demo_due_to_invalid_status"
EVENT_ADMIN_LOGIN                 = "admin_login"
EVENT_ADMIN_LOGOUT                = "admin_logout"
EVENT_ADMIN_LOGIN_FAILED          = "admin_login_failed"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def log_event(
    serial_key: Optional[str],
    machine_id: Optional[str],
    event_type: str,
    message: str = "",
    ip: str = "",
    actor: str = "",
) -> None:
    """Insert a single row into the ``events`` table. Never raises."""
    try:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO events (serial_key, machine_id, event_type,
                                    message, ip, actor, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    serial_key or None,
                    machine_id or None,
                    event_type,
                    message or "",
                    ip or "",
                    actor or "",
                    _now_iso(),
                ),
            )
    except Exception:
        pass


def record_activation(
    serial_key: str,
    machine_id: str,
    ip: str = "",
    status: str = "active",
    notes: str = "",
    user_agent: str = "",
    machine_uuid: str = "",
) -> None:
    """Upsert a row in the ``activations`` table. Never raises."""
    try:
        now = _now_iso()
        with get_connection() as conn:
            existing = conn.execute(
                """
                SELECT id FROM activations
                 WHERE serial_key = ? AND machine_id = ?
                 ORDER BY id DESC LIMIT 1
                """,
                (serial_key, machine_id),
            ).fetchone()

            if existing is not None:
                conn.execute(
                    """
                    UPDATE activations
                       SET last_seen_at = ?,
                           status       = ?,
                           ip           = COALESCE(NULLIF(?, ''), ip),
                           notes        = COALESCE(NULLIF(?, ''), notes),
                           user_agent   = COALESCE(NULLIF(?, ''), user_agent),
                           machine_uuid = COALESCE(NULLIF(?, ''), machine_uuid)
                     WHERE id = ?
                    """,
                    (now, status, ip or "", notes or "",
                     user_agent or "", machine_uuid or "", existing["id"]),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO activations (serial_key, machine_id,
                                             activated_at, last_seen_at,
                                             status, ip, notes, user_agent,
                                             machine_uuid)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (serial_key, machine_id, now, now,
                     status, ip or "", notes or "", user_agent or "",
                     machine_uuid or ""),
                )
    except Exception:
        pass


def touch_activation_seen(serial_key: str, machine_id: str) -> None:
    """Update ``last_seen_at`` on a matching activation, silent on error."""
    try:
        now = _now_iso()
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE activations
                   SET last_seen_at = ?
                 WHERE serial_key = ? AND machine_id = ?
                """,
                (now, serial_key, machine_id),
            )
    except Exception:
        pass


def list_events(filters: Optional[Dict[str, Any]] = None,
                page: int = 1, limit: int = 50) -> Dict[str, Any]:
    """Return paginated events matching the given filters."""
    filters = filters or {}
    where: List[str] = []
    params: List[Any] = []

    if filters.get("serial_key"):
        where.append("serial_key = ?")
        params.append(filters["serial_key"])
    if filters.get("machine_id"):
        where.append("machine_id = ?")
        params.append(filters["machine_id"])
    if filters.get("event_type"):
        where.append("event_type = ?")
        params.append(filters["event_type"])
    if filters.get("since"):
        where.append("created_at >= ?")
        params.append(filters["since"])
    if filters.get("until"):
        where.append("created_at <= ?")
        params.append(filters["until"])

    clause = (" WHERE " + " AND ".join(where)) if where else ""

    page = max(1, int(page or 1))
    limit = max(1, min(500, int(limit or 50)))
    offset = (page - 1) * limit

    with get_connection() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM events{clause}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"""SELECT * FROM events{clause}
                 ORDER BY id DESC LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()

    items = [dict(r) for r in rows]
    return {"total": total, "page": page, "limit": limit, "items": items}


def list_activations(
    filters: Optional[Dict[str, Any]] = None,
    page: int = 1,
    limit: int = 50,
) -> Dict[str, Any]:
    """Paginated activations list with optional filters."""
    filters = filters or {}
    where: List[str] = []
    params: List[Any] = []

    if filters.get("serial_key"):
        where.append("serial_key = ?")
        params.append(filters["serial_key"])
    if filters.get("machine_id"):
        where.append("machine_id = ?")
        params.append(filters["machine_id"])

    clause = (" WHERE " + " AND ".join(where)) if where else ""

    page = max(1, int(page or 1))
    limit = max(1, min(500, int(limit or 50)))
    offset = (page - 1) * limit

    with get_connection() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM activations{clause}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"""SELECT * FROM activations{clause}
                 ORDER BY id DESC LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()

    items = [dict(r) for r in rows]
    return {"total": total, "page": page, "limit": limit, "items": items}


def list_activations_for_serial(serial_key: str) -> List[Dict[str, Any]]:
    """Return all activations for a license, newest first."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM activations
                WHERE serial_key = ?
                ORDER BY id DESC""",
            (serial_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def recent_events_for_serial(serial_key: str, n: int = 50) -> List[Dict[str, Any]]:
    """Last *n* events for a specific license, newest first."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM events
                WHERE serial_key = ?
                ORDER BY id DESC LIMIT ?""",
            (serial_key, int(n)),
        ).fetchall()
    return [dict(r) for r in rows]


def recent_events(n: int = 20) -> List[Dict[str, Any]]:
    """Last *n* events across all licenses, newest first."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM events
                ORDER BY id DESC LIMIT ?""",
            (int(n),),
        ).fetchall()
    return [dict(r) for r in rows]
