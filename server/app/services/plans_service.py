# -*- coding: utf-8 -*-
"""
server/app/services/plans_service.py
------------------------------------
Business logic for the ``subscription_plans`` table.

The admin UI exposes a short CRUD around this table so users can
tailor the license-generator form: each plan is a named duration
(e.g. ``"3 חודשים" → 90 days``) and feeds the "בחר תכנית" cards on
the generator page.

Every function returns plain dicts so the route layer can hand them
straight to Jinja / JSON.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional

from ..database import get_connection


VALID_LICENSE_TYPES = ("trial_14_days", "yearly", "lifetime")


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _row(r) -> Dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict (None-safe)."""
    if r is None:
        return {}
    return {k: r[k] for k in r.keys()}


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def list_plans(include_inactive: bool = False) -> List[Dict[str, Any]]:
    """Return the plan catalogue ordered by ``sort_order`` then ``name``."""
    with get_connection() as conn:
        if include_inactive:
            rows = conn.execute(
                "SELECT * FROM subscription_plans "
                "ORDER BY sort_order ASC, name ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM subscription_plans "
                "WHERE is_active = 1 "
                "ORDER BY sort_order ASC, name ASC"
            ).fetchall()
    return [_row(r) for r in rows]


def get_plan(plan_id: int) -> Optional[Dict[str, Any]]:
    """Fetch one plan by primary key, or ``None`` if it doesn't exist."""
    try:
        pid = int(plan_id)
    except Exception:
        return None
    with get_connection() as conn:
        r = conn.execute(
            "SELECT * FROM subscription_plans WHERE id = ?", (pid,),
        ).fetchone()
    return _row(r) if r else None


# ---------------------------------------------------------------------------
# Create / Update / Delete
# ---------------------------------------------------------------------------

def create_plan(name: str, days: Optional[int],
                license_type: str = "yearly",
                sort_order: int = 0,
                custom_type: str = "") -> Dict[str, Any]:
    """Add a new plan.

    ``days`` can be ``None`` for lifetime plans.  ``license_type`` must
    be one of :data:`VALID_LICENSE_TYPES`.  Raises :class:`ValueError`
    on duplicate names or invalid input.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("שם התכנית לא יכול להיות ריק.")
    if license_type not in VALID_LICENSE_TYPES:
        raise ValueError(f"סוג רישיון לא תקין: {license_type!r}")
    if license_type == "lifetime":
        days = None
    else:
        if days is None:
            raise ValueError("יש להזין מספר ימים עבור תכנית מסוג זה.")
        try:
            days = int(days)
        except Exception:
            raise ValueError("מספר ימים לא תקין.")
        if days < 1 or days > 36_500:
            raise ValueError("מספר ימים חייב להיות בטווח 1–36500.")

    now = _now_iso()
    try:
        with get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO subscription_plans
                (name, days, license_type, is_active, sort_order,
                 created_at, updated_at, custom_type)
                VALUES (?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (name, days, license_type, int(sort_order or 0), now, now,
                 (custom_type or "").strip()),
            )
            new_id = cursor.lastrowid
    except Exception as exc:
        # SQLite duplicate-name errors land here.
        if "UNIQUE" in str(exc) or "unique" in str(exc):
            raise ValueError(f"תכנית בשם '{name}' כבר קיימת.") from exc
        raise

    fresh = get_plan(new_id)
    if fresh is None:
        raise RuntimeError("שמירת התכנית נכשלה.")
    return fresh


def update_plan(plan_id: int,
                name: Optional[str] = None,
                days: Optional[int] = None,
                license_type: Optional[str] = None,
                is_active: Optional[bool] = None,
                sort_order: Optional[int] = None) -> Dict[str, Any]:
    """Patch selected fields on an existing plan. Raises ``ValueError`` on
    invalid input or ``LookupError`` if the plan is missing."""
    current = get_plan(plan_id)
    if not current:
        raise LookupError("התכנית לא נמצאה.")

    updates: Dict[str, Any] = {}

    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("שם התכנית לא יכול להיות ריק.")
        # System plans CAN be renamed — the desktop client resolves
        # them via ``is_system + license_type``, not by name.
        updates["name"] = name

    if license_type is not None:
        if license_type not in VALID_LICENSE_TYPES:
            raise ValueError(f"סוג רישיון לא תקין: {license_type!r}")
        updates["license_type"] = license_type
        if license_type == "lifetime":
            updates["days"] = None

    if days is not None and "days" not in updates:
        try:
            days_int = int(days)
        except Exception:
            raise ValueError("מספר ימים לא תקין.")
        if days_int < 1 or days_int > 36_500:
            raise ValueError("מספר ימים חייב להיות בטווח 1–36500.")
        updates["days"] = days_int

    if is_active is not None:
        updates["is_active"] = 1 if is_active else 0

    if sort_order is not None:
        try:
            updates["sort_order"] = int(sort_order)
        except Exception:
            raise ValueError("מספר סדר לא תקין.")

    if not updates:
        return current

    updates["updated_at"] = _now_iso()

    fields = ", ".join(f"{k} = ?" for k in updates.keys())
    values = list(updates.values()) + [int(plan_id)]
    try:
        with get_connection() as conn:
            conn.execute(
                f"UPDATE subscription_plans SET {fields} WHERE id = ?",
                values,
            )
    except Exception as exc:
        if "UNIQUE" in str(exc) or "unique" in str(exc):
            raise ValueError(
                f"תכנית בשם '{updates.get('name')}' כבר קיימת."
            ) from exc
        raise
    return get_plan(plan_id) or current


def delete_plan(plan_id: int) -> bool:
    """Remove a plan. Returns True when a row was deleted.

    System plans (``is_system = 1``) are protected and will NEVER be
    deleted — :class:`ValueError` is raised so the route layer can
    surface a friendly message to the admin.
    """
    try:
        pid = int(plan_id)
    except Exception:
        return False
    with get_connection() as conn:
        row = conn.execute(
            "SELECT is_system FROM subscription_plans WHERE id = ?", (pid,),
        ).fetchone()
        if row is None:
            return False
        if int(row["is_system"] or 0) == 1:
            raise ValueError(
                "לא ניתן למחוק תכנית מערכת (תוכנית ניסיון). ניתן רק לערוך אותה."
            )
        cur = conn.execute(
            "DELETE FROM subscription_plans WHERE id = ?", (pid,),
        )
        return cur.rowcount > 0


def toggle_plan(plan_id: int) -> Optional[Dict[str, Any]]:
    """Flip the active flag on a plan. Returns the updated plan."""
    current = get_plan(plan_id)
    if not current:
        return None
    return update_plan(plan_id, is_active=not bool(current.get("is_active")))
