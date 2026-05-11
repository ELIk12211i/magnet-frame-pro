# -*- coding: utf-8 -*-
"""
server/app/services/stats_service.py
------------------------------------
Dashboard aggregates — counts by status / type, activity windows.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List

from ..database import get_connection


def _iso_days_ago(days: int) -> str:
    dt = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)
    return dt.replace(microsecond=0).isoformat()


def overview() -> Dict[str, Any]:
    """Return the counts rendered on the dashboard header."""
    with get_connection() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM licenses"
        ).fetchone()["c"]

        by_status_rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM licenses GROUP BY status"
        ).fetchall()
        by_type_rows = conn.execute(
            "SELECT license_type, COUNT(*) AS c FROM licenses "
            "GROUP BY license_type"
        ).fetchall()

        since_30 = _iso_days_ago(30)
        since_7 = _iso_days_ago(7)
        since_1 = _iso_days_ago(1)

        activations_30 = conn.execute(
            "SELECT COUNT(*) AS c FROM activations WHERE activated_at >= ?",
            (since_30,),
        ).fetchone()["c"]
        activations_7 = conn.execute(
            "SELECT COUNT(*) AS c FROM activations WHERE activated_at >= ?",
            (since_7,),
        ).fetchone()["c"]
        activations_24h = conn.execute(
            "SELECT COUNT(*) AS c FROM activations WHERE activated_at >= ?",
            (since_1,),
        ).fetchone()["c"]

        events_24h = conn.execute(
            "SELECT COUNT(*) AS c FROM events WHERE created_at >= ?",
            (since_1,),
        ).fetchone()["c"]

        trials_total = conn.execute(
            "SELECT COUNT(*) AS c FROM trials"
        ).fetchone()["c"]

    by_status = {r["status"]: r["c"] for r in by_status_rows}
    by_type = {r["license_type"]: r["c"] for r in by_type_rows}

    return {
        "total":           total,
        "active":          by_status.get("active", 0),
        "unused":          by_status.get("unused", 0),
        "expired":         by_status.get("expired", 0),
        "disabled":        by_status.get("disabled", 0),
        "reset":           by_status.get("reset", 0),
        "trials_total":    trials_total,
        "activations_30d": activations_30,
        "activations_7d":  activations_7,
        "activations_24h": activations_24h,
        "events_24h":      events_24h,
        "by_status":       by_status,
        "by_type":         by_type,
    }


def recent_licenses(n: int = 10) -> List[Dict[str, Any]]:
    """Most recently created licenses (for the dashboard)."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM licenses
                ORDER BY created_at DESC, serial_key DESC
                LIMIT ?""",
            (int(n),),
        ).fetchall()
    return [dict(r) for r in rows]
