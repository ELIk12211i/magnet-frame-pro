# -*- coding: utf-8 -*-
"""
server/app/services/leads_service.py
------------------------------------
Read-side helpers for the ``trial_leads`` table.

Every row here represents a single click on the desktop client's
"התחל תוכנית נסיון" dialog — the admin's "משתמשים חדשים" page reads
this table so it can show who signed up for a trial, when, and with
what contact details.

Write-side logic (inserting a new lead) lives inline in
``license_service.start_trial`` to keep the trial activation atomic.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..database import get_connection


def _row(r) -> Dict[str, Any]:
    if r is None:
        return {}
    return {k: r[k] for k in r.keys()}


def list_leads(limit: int = 500) -> List[Dict[str, Any]]:
    """Return the most recent trial signups, newest first.

    The result is enriched with the matching license's ``expires_at``
    so the UI can show when each trial expires without a second
    lookup.
    """
    try:
        lim = max(1, min(int(limit), 2000))
    except Exception:
        lim = 500
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT  l.id, l.machine_id, l.serial_key, l.name, l.phone,
                    l.ip, l.user_agent, l.created_at,
                    lic.expires_at AS expires_at,
                    lic.status     AS license_status
            FROM    trial_leads l
            LEFT JOIN licenses lic ON lic.serial_key = l.serial_key
            ORDER BY l.created_at DESC
            LIMIT ?
            """,
            (lim,),
        ).fetchall()
    return [_row(r) for r in rows]


def count_leads() -> int:
    """Total number of trial signups ever recorded."""
    with get_connection() as conn:
        r = conn.execute(
            "SELECT COUNT(*) AS c FROM trial_leads"
        ).fetchone()
    return int(r["c"]) if r else 0


def get_lead(lead_id: int) -> Dict[str, Any]:
    """Fetch a single lead + every piece of machine / license info we
    have for it.

    Returns a dict shaped like:

    ``{"lead": {...}, "license": {...}, "activations": [...], "all_trials": [...]}``

    * ``lead``        — the row from ``trial_leads`` (contact info)
    * ``license``     — the matching ``licenses`` row (status, expiry,
                         hardware fingerprint, hostname, public IP…)
    * ``activations`` — every ``activations`` row for this license
                         (login/validation history, user-agents, IPs)
    * ``all_trials``  — other trials for the SAME ``machine_id`` so the
                         admin can see how many times this machine
                         clicked "Start trial" in total.
    """
    try:
        lid = int(lead_id)
    except Exception:
        return {}

    with get_connection() as conn:
        lead = conn.execute(
            "SELECT * FROM trial_leads WHERE id = ?", (lid,)
        ).fetchone()
        if lead is None:
            return {}
        lead_d = _row(lead)

        # License row (if still present — the admin may have wiped it).
        lic_d: Dict[str, Any] = {}
        if lead_d.get("serial_key"):
            lic = conn.execute(
                "SELECT * FROM licenses WHERE serial_key = ?",
                (lead_d["serial_key"],),
            ).fetchone()
            lic_d = _row(lic)

        # Activation history for this license — ordered oldest → newest
        # so the UI can show it as a timeline.
        activations: List[Dict[str, Any]] = []
        if lead_d.get("serial_key"):
            acts = conn.execute(
                "SELECT * FROM activations WHERE serial_key = ? "
                "ORDER BY activated_at ASC",
                (lead_d["serial_key"],),
            ).fetchall()
            activations = [_row(a) for a in acts]

        # Every other trial this machine has had — not the current one.
        all_trials: List[Dict[str, Any]] = []
        if lead_d.get("machine_id"):
            trials = conn.execute(
                "SELECT l.id, l.serial_key, l.name, l.phone, l.ip,"
                "       l.created_at, lic.status AS license_status,"
                "       lic.expires_at"
                " FROM trial_leads l"
                " LEFT JOIN licenses lic ON lic.serial_key = l.serial_key"
                " WHERE l.machine_id = ?"
                " ORDER BY l.created_at DESC",
                (lead_d["machine_id"],),
            ).fetchall()
            all_trials = [_row(t) for t in trials]

    return {
        "lead":        lead_d,
        "license":     lic_d,
        "activations": activations,
        "all_trials":  all_trials,
    }


def delete_lead(lead_id: int) -> bool:
    """Remove a single lead row. Returns True when a row was deleted."""
    try:
        lid = int(lead_id)
    except Exception:
        return False
    with get_connection() as conn:
        cur = conn.execute(
            "DELETE FROM trial_leads WHERE id = ?", (lid,),
        )
        return cur.rowcount > 0
