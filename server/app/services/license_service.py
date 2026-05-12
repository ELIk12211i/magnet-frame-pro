# -*- coding: utf-8 -*-
"""
server/app/services/license_service.py
--------------------------------------
Business logic for /license/* endpoints.

Each public function returns a dict ready to serialise as JSON, matching
the unified ``LicenseResponse`` contract (see ``schemas.py``). The
routes layer simply hands that dict back to FastAPI.

Event-logging hooks write to the ``events`` and ``activations`` tables
via :mod:`app.services.events_service`. Every hook is wrapped in a
try/except so that a logging failure can never break the licensing flow.
"""

from __future__ import annotations

import datetime as _dt
import random
import re as _re
import secrets
import string
from typing import Any, Dict, Optional

from ..database import get_connection
from . import events_service as _evt


# ---------------------------------------------------------------------------
# Serial-key format helpers
# ---------------------------------------------------------------------------
# New canonical format (used by the generator + the desktop client input):
#     XXXX-XXXXX-XXXXX-XXXX   (segments of 4-5-5-4, A-Z/0-9 only)
#
# Legacy format that must keep working for already-issued keys stored in
# existing databases:
#     MFP-YYYY-XXXX-XXXX
#
# Both regexes are anchored so callers can use ``fullmatch``/``match``.
SERIAL_PATTERN_NEW    = _re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{4}$")
SERIAL_PATTERN_LEGACY = _re.compile(r"^MFP-\d{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$")


def is_valid_serial_format(serial: str) -> bool:
    """Return True when ``serial`` matches the new OR the legacy format."""
    if not serial:
        return False
    s = serial.strip().upper()
    return bool(SERIAL_PATTERN_NEW.fullmatch(s) or SERIAL_PATTERN_LEGACY.fullmatch(s))


def normalize_serial(serial: str) -> str:
    """Strip whitespace and upper-case a serial key (the canonical form)."""
    return (serial or "").strip().upper()


# ---------------------------------------------------------------------------
# Constants (must match the client-side enums in licensing/models.py)
# ---------------------------------------------------------------------------
LICENSE_TYPE_TRIAL    = "trial_14_days"
LICENSE_TYPE_YEARLY   = "yearly"
LICENSE_TYPE_LIFETIME = "lifetime"

TRIAL_DAYS  = 14
YEARLY_DAYS = 365

# ---------------------------------------------------------------------------
# Periodic-validation windows (additive — never shortens existing behaviour).
#
# After every successful /license/validate the server writes
# ``next_validation_due_at = now + VALIDATION_RECHECK_DAYS`` on the row.
# The desktop client additionally tracks an ``offline_grace_until`` locally
# (see licensing/license_manager.py) so that short internet outages do not
# switch the app to Demo prematurely.
# ---------------------------------------------------------------------------
VALIDATION_RECHECK_DAYS = 3
VALIDATION_OFFLINE_GRACE_DAYS = 7

# ---------------------------------------------------------------------------
# Machine-rebinding cap.
#
# A purchased license is migrated to a new machine on first activation
# from that machine (so a customer can move their license between PCs).
# To prevent casual sharing, the migration is capped: after
# ``MAX_REBINDS`` distinct moves, further activations from new machines
# are rejected and the customer must contact support to reset the
# binding.  Counted by the number of rows in ``machine_history`` for
# the serial.
# ---------------------------------------------------------------------------
MAX_REBINDS = 3


# ---------------------------------------------------------------------------
# Canonical status enum for the unified response.
# ---------------------------------------------------------------------------
STATUS_ACTIVE           = "active"
STATUS_UNUSED           = "unused"
STATUS_DISABLED         = "disabled"
STATUS_EXPIRED          = "expired"
STATUS_NOT_FOUND        = "not_found"
STATUS_MACHINE_MISMATCH = "machine_mismatch"
STATUS_TRIAL_USED       = "trial_used"
STATUS_RESET            = "reset"


# ---------------------------------------------------------------------------
# Custom exception — the routes layer turns these into HTTP 400 responses.
# ---------------------------------------------------------------------------
class LicenseError(Exception):
    """Domain error raised for any invalid license operation."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def _iso(dt: _dt.datetime | None) -> str | None:
    return None if dt is None else dt.isoformat()


def _parse(iso_str: str | None) -> _dt.datetime | None:
    if not iso_str:
        return None
    try:
        s = str(iso_str)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = _dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d
    except Exception:
        return None


def _row_to_dict(row) -> Dict[str, Any]:
    if row is None:
        return {}
    return {k: row[k] for k in row.keys()}


def _normalise_license_type(lt: str) -> str:
    v = (lt or "").strip().lower()
    if v == "trial":
        return LICENSE_TYPE_TRIAL
    return v


def _response(
    row: Dict[str, Any],
    message: str = "",
    success: bool = True,
    status: Optional[str] = None,
    is_demo: Optional[bool] = None,
) -> Dict[str, Any]:
    """Build a unified response dict."""
    final_status = (status or row.get("status") or "").strip()
    if is_demo is None:
        is_demo = final_status in (
            STATUS_DISABLED, STATUS_EXPIRED, STATUS_NOT_FOUND,
            STATUS_MACHINE_MISMATCH, STATUS_TRIAL_USED,
        )

    key = row.get("serial_key")
    return {
        "success":        bool(success),
        "status":         final_status or STATUS_UNUSED,
        "license_type":   row.get("license_type"),
        "license_key":    key,
        "serial_key":     key,
        "machine_id":     row.get("machine_id"),
        "activated_at":   row.get("activated_at"),
        "expires_at":     row.get("expires_at"),
        "customer_name":        row.get("customer_name") or "",
        "customer_first_name":  row.get("customer_first_name") or "",
        "customer_last_name":   row.get("customer_last_name") or "",
        "customer_email":       row.get("customer_email") or "",
        "customer_phone":       row.get("customer_phone") or "",
        "plan_name":            row.get("plan_name") or "",
        "plan_days":            row.get("plan_days"),
        "hardware_id":          row.get("hardware_id") or "",
        "hostname":             row.get("hostname") or "",
        "client_public_ip":     row.get("client_public_ip") or "",
        "is_demo":        bool(is_demo),
        "message":        message or "",
        "notes":          row.get("notes") or "",
        "created_at":     row.get("created_at"),
        # --- Periodic-validation bookkeeping (additive fields) ---------
        "last_validation_at":      row.get("last_validation_at"),
        "next_validation_due_at":  row.get("next_validation_due_at"),
        "validation_status":       row.get("validation_status") or "",
        "validation_message":      row.get("validation_message") or "",
        "recheck_days":            VALIDATION_RECHECK_DAYS,
        "offline_grace_days":      VALIDATION_OFFLINE_GRACE_DAYS,
    }


def _error_response(
    status_enum: str,
    message: str,
    serial_key: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "success":        True,
        "status":         status_enum,
        "license_type":   None,
        "license_key":    serial_key,
        "serial_key":     serial_key,
        "machine_id":     None,
        "activated_at":   None,
        "expires_at":     None,
        "customer_name":  "",
        "customer_email": "",
        "is_demo":        True,
        "message":        message,
        "notes":          "",
        "created_at":     None,
        # --- Periodic-validation bookkeeping (additive fields) ---------
        "last_validation_at":     None,
        "next_validation_due_at": None,
        "validation_status":      status_enum or "failed",
        "validation_message":     message or "",
        "recheck_days":           VALIDATION_RECHECK_DAYS,
        "offline_grace_days":     VALIDATION_OFFLINE_GRACE_DAYS,
    }


def _compute_expiry(license_type: str, started: _dt.datetime,
                    days: Optional[int] = None) -> str | None:
    if license_type == LICENSE_TYPE_LIFETIME:
        return None
    if license_type == LICENSE_TYPE_YEARLY:
        n = int(days) if days else YEARLY_DAYS
        return _iso(started + _dt.timedelta(days=n))
    if license_type == LICENSE_TYPE_TRIAL:
        return _iso(started + _dt.timedelta(days=TRIAL_DAYS))
    return None


def _is_expired(iso_str: str | None) -> bool:
    dt = _parse(iso_str)
    if dt is None:
        return False
    return dt <= _now()


def _safe(fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------

def activate(serial: str, mid: str, ip: str = "", user_agent: str = "",
             machine_uuid: str = "",
             hostname: str = "", public_ip: str = "") -> Dict[str, Any]:
    serial = (serial or "").strip()
    mid = (mid or "").strip()
    if not serial or not mid:
        raise LicenseError("יש לספק מפתח רישיון ומזהה מחשב.")

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM licenses WHERE serial_key = ?", (serial,)
        ).fetchone()
        if row is None:
            _safe(_evt.log_event, None, mid,
                  _evt.EVENT_ACTIVATION_FAILED,
                  message=f"serial not found: {serial}", ip=ip, actor="client")
            raise LicenseError("מפתח הרישיון לא נמצא במערכת.")

        data = _row_to_dict(row)

        if data.get("status") == STATUS_DISABLED:
            _safe(_evt.log_event, serial, mid,
                  _evt.EVENT_ACTIVATION_FAILED,
                  message="license disabled", ip=ip, actor="client")
            return _response(
                data,
                message="הרישיון בוטל — יש לפנות לתמיכה",
                status=STATUS_DISABLED,
                is_demo=True,
            )

        bound_mid  = (data.get("machine_id")  or "").strip()
        bound_hwid = (data.get("hardware_id") or "").strip()

        # Matching priority: Hardware ID first (stable across
        # reboots and network changes), then the legacy machine_id
        # (IP) for backwards compatibility with licenses issued
        # before this column existed.
        # 2026-04 product change: instead of rejecting a different-
        # machine activation outright, MIGRATE the license to the new
        # machine and record the previous binding in
        # ``machine_history`` so that machine can't fall back to a
        # fresh trial later.  Same key on the SAME machine is still
        # a no-op refresh.
        # 2026-05 update: capped at ``MAX_REBINDS`` distinct moves to
        # prevent casual sharing.  Past the cap we reject with a
        # support-prompt message.
        incoming_hwid = (machine_uuid or "").strip()

        def _count_rebinds() -> int:
            """How many times has THIS serial moved between machines?

            Returns 0 if the table is missing (older DB) so we never
            block on a schema migration glitch.
            """
            try:
                row_ = conn.execute(
                    "SELECT COUNT(*) AS c FROM machine_history "
                    "WHERE last_serial = ?",
                    (serial,),
                ).fetchone()
                return int(row_["c"]) if row_ else 0
            except Exception:
                return 0

        def _record_history(prev_mid: str) -> None:
            if not prev_mid:
                return
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO machine_history "
                    "(machine_id, last_serial, reason, recorded_at) "
                    "VALUES (?, ?, 'license_migrated', ?)",
                    (prev_mid, serial, _iso(_now())),
                )
            except Exception:
                # History tracking is opportunistic — never block a
                # legitimate activation if the insert fails.
                pass

        # Detect a pending migration (license is bound to another
        # machine).  We need the decision BEFORE recording history so
        # we can enforce the cap on the new attempt itself.
        wants_migration = False
        migrate_reason = ""
        if bound_hwid:
            if incoming_hwid and bound_hwid != incoming_hwid:
                wants_migration = True
                migrate_reason = (f"HWID migrate: bound={bound_hwid} "
                                  f"incoming={incoming_hwid}")
            elif not incoming_hwid and bound_mid and bound_mid != mid:
                wants_migration = True
                migrate_reason = f"IP migrate: bound={bound_mid}"
        elif bound_mid and bound_mid != mid:
            wants_migration = True
            migrate_reason = f"legacy migrate: bound={bound_mid}"

        if wants_migration:
            current_rebinds = _count_rebinds()
            if current_rebinds >= MAX_REBINDS:
                _safe(_evt.log_event, serial, mid,
                      _evt.EVENT_ACTIVATION_MACHINE_MISMATCH,
                      message=(f"rebind cap reached: "
                               f"{current_rebinds}/{MAX_REBINDS} "
                               f"({migrate_reason})"),
                      ip=ip, actor="client")
                raise LicenseError(
                    "רישיון זה הוחלף בין מחשבים %d פעמים. "
                    "צרו קשר עם התמיכה לאיפוס." % current_rebinds,
                    status=403,
                )

        migrated_from: str = ""
        if wants_migration:
            migrated_from = bound_mid or bound_hwid or "unknown"
            _safe(_evt.log_event, serial, mid,
                  _evt.EVENT_ACTIVATION_MACHINE_MISMATCH,
                  message=migrate_reason, ip=ip, actor="client")
            _record_history(migrated_from)

        # If a migration happened, retire the old binding *now* so the
        # rest of this function treats this activation as a same-
        # machine flow on the new machine.  We REBIND the row in-place
        # rather than create a duplicate — one license = one active
        # binding.
        if migrated_from:
            conn.execute(
                "UPDATE licenses SET machine_id = ?, hardware_id = ? "
                "WHERE serial_key = ?",
                (mid, incoming_hwid or bound_hwid, serial),
            )
            data["machine_id"]  = mid
            data["hardware_id"] = incoming_hwid or bound_hwid
            bound_mid  = mid
            bound_hwid = incoming_hwid or bound_hwid
            _safe(_evt.log_event, serial, mid,
                  _evt.EVENT_MACHINE_REBOUND,
                  message=(f"rebound from {migrated_from} "
                           f"(count={_count_rebinds()}/{MAX_REBINDS})"),
                  ip=ip, actor="client")

        # At this point the license is either unbound or belongs to
        # this machine.  The rest of the flow treats it as "same
        # machine" — update the HWID if it wasn't stored yet.
        is_same_machine = (
            (bound_hwid and incoming_hwid and bound_hwid == incoming_hwid)
            or (not bound_hwid and bound_mid == mid)
        )

        if is_same_machine and data.get("status") == "active":
            if _is_expired(data.get("expires_at")):
                conn.execute(
                    "UPDATE licenses SET status = 'expired' WHERE serial_key = ?",
                    (serial,),
                )
                data["status"] = "expired"
                _safe(_evt.log_event, serial, mid,
                      _evt.EVENT_YEARLY_EXPIRED if data.get("license_type") ==
                      LICENSE_TYPE_YEARLY else _evt.EVENT_VALIDATION_EXPIRED,
                      ip=ip, actor="client")
                raise LicenseError("תוקף הרישיון פג.")
            _safe(_evt.log_event, serial, mid,
                  _evt.EVENT_ACTIVATION_ALREADY_USED, ip=ip, actor="client")
            _safe(_evt.record_activation, serial, mid, ip, "active",
                  "already active on same machine", user_agent, machine_uuid)
            return _response(data, "הרישיון כבר פעיל במחשב זה.",
                             status=STATUS_ACTIVE, is_demo=False)

        now = _now()
        activated_at = _iso(now)
        expires_at   = _compute_expiry(str(data.get("license_type")), now)
        # Activation counts as a "license check" — set the same fields that
        # ``validate()`` maintains so the admin UI shows "אימות אחרון"
        # immediately after the first activation, not only after the
        # periodic re-validation 60 seconds later.
        next_due_iso_activate = _iso(now + _dt.timedelta(days=VALIDATION_RECHECK_DAYS))

        conn.execute(
            """
            UPDATE licenses
               SET machine_id             = ?,
                   hardware_id            = COALESCE(NULLIF(?, ''),
                                                    hardware_id),
                   hostname               = COALESCE(NULLIF(?, ''),
                                                    hostname),
                   client_public_ip       = COALESCE(NULLIF(?, ''),
                                                    client_public_ip),
                   status                 = 'active',
                   activated_at           = COALESCE(activated_at, ?),
                   expires_at             = COALESCE(expires_at, ?),
                   last_validation_at     = ?,
                   next_validation_due_at = ?,
                   validation_status      = 'success',
                   validation_message     = ?
             WHERE serial_key = ?
            """,
            (mid, incoming_hwid,
             (hostname or "").strip(), (public_ip or "").strip(),
             activated_at, expires_at,
             activated_at, next_due_iso_activate,
             "הרישיון הופעל בהצלחה.", serial),
        )
        fresh = conn.execute(
            "SELECT * FROM licenses WHERE serial_key = ?", (serial,)
        ).fetchone()

    _safe(_evt.log_event, serial, mid, _evt.EVENT_ACTIVATION_SUCCESS,
          ip=ip, actor="client")
    _safe(_evt.log_event, serial, mid, _evt.EVENT_LICENSE_VALIDATED_SUCCESS,
          message="initial activation", ip=ip, actor="client")
    _safe(_evt.record_activation, serial, mid, ip, "active", "",
          user_agent, machine_uuid)
    return _response(_row_to_dict(fresh), "הרישיון הופעל בהצלחה.",
                     status=STATUS_ACTIVE, is_demo=False)


def validate(serial: str, mid: str, ip: str = "", user_agent: str = "",
             machine_uuid: str = "",
             hostname: str = "", public_ip: str = "") -> Dict[str, Any]:
    serial = (serial or "").strip()
    mid = (mid or "").strip()
    if not serial or not mid:
        raise LicenseError("יש לספק מפתח רישיון ומזהה מחשב.")

    now = _now()
    now_iso = _iso(now)
    next_due_iso = _iso(now + _dt.timedelta(days=VALIDATION_RECHECK_DAYS))

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM licenses WHERE serial_key = ?", (serial,)
        ).fetchone()
        if row is None:
            _safe(_evt.log_event, None, mid,
                  _evt.EVENT_VALIDATION_FAILED,
                  message="serial not found", ip=ip, actor="client")
            _safe(_evt.log_event, None, mid,
                  _evt.EVENT_LICENSE_VALIDATED_FAILED,
                  message="serial not found", ip=ip, actor="client")
            return _error_response(
                STATUS_NOT_FOUND, "מפתח הרישיון לא נמצא.", serial,
            )

        data = _row_to_dict(row)
        prev_validation_status = str(data.get("validation_status") or "").strip()

        # --------- failure branches: record validation_status + emit events
        if data.get("status") == STATUS_DISABLED:
            _safe(_evt.log_event, serial, mid,
                  _evt.EVENT_VALIDATION_FAILED,
                  message="license disabled", ip=ip, actor="client")
            _safe(_evt.log_event, serial, mid,
                  _evt.EVENT_LICENSE_VALIDATED_FAILED,
                  message="license disabled", ip=ip, actor="client")
            _safe(_evt.log_event, serial, mid,
                  _evt.EVENT_LICENSE_SWITCHED_TO_DEMO_INVALID,
                  message="disabled", ip=ip, actor="client")
            conn.execute(
                """UPDATE licenses
                      SET last_validation_at = ?,
                          next_validation_due_at = NULL,
                          validation_status = ?,
                          validation_message = ?
                    WHERE serial_key = ?""",
                (now_iso, STATUS_DISABLED,
                 "הרישיון בוטל — התוכנה עוברת למצב Demo", serial),
            )
            data["last_validation_at"]     = now_iso
            data["next_validation_due_at"] = None
            data["validation_status"]      = STATUS_DISABLED
            data["validation_message"]     = "הרישיון בוטל — התוכנה עוברת למצב Demo"
            return _response(
                data,
                message="הרישיון בוטל — התוכנה עוברת למצב Demo",
                status=STATUS_DISABLED,
                is_demo=True,
            )

        # Machine binding check — prefer hardware_id (stable across
        # IP changes / reboots) when the license has one on file.
        # Fall back to the legacy machine_id (IP) only when no HWID
        # has been recorded yet.
        bound_mid  = (data.get("machine_id")  or "").strip()
        bound_hwid = (data.get("hardware_id") or "").strip()
        incoming_hwid = (machine_uuid or "").strip()

        _mismatch = False
        if bound_hwid and incoming_hwid:
            # Modern match — HWID only.  IP can change freely.
            _mismatch = (bound_hwid != incoming_hwid)
        elif bound_hwid and not incoming_hwid:
            # Client is an older build without HWID support → fall
            # back to IP.  This keeps legacy clients working.
            _mismatch = (bool(bound_mid) and bound_mid != mid)
        else:
            # No HWID stored yet → legacy IP match (original behaviour).
            _mismatch = (bool(bound_mid) and bound_mid != mid)

        if _mismatch:
            _safe(_evt.log_event, serial, mid,
                  _evt.EVENT_VALIDATION_FAILED,
                  message="wrong machine", ip=ip, actor="client")
            _safe(_evt.log_event, serial, mid,
                  _evt.EVENT_LICENSE_VALIDATED_FAILED,
                  message="wrong machine", ip=ip, actor="client")
            _safe(_evt.log_event, serial, mid,
                  _evt.EVENT_LICENSE_SWITCHED_TO_DEMO_INVALID,
                  message="machine_mismatch", ip=ip, actor="client")
            conn.execute(
                """UPDATE licenses
                      SET last_validation_at = ?,
                          validation_status = ?,
                          validation_message = ?
                    WHERE serial_key = ?""",
                (now_iso, STATUS_MACHINE_MISMATCH,
                 "הרישיון שייך למחשב אחר.", serial),
            )
            data["last_validation_at"] = now_iso
            data["validation_status"]  = STATUS_MACHINE_MISMATCH
            data["validation_message"] = "הרישיון שייך למחשב אחר."
            return _response(
                data,
                message="הרישיון שייך למחשב אחר.",
                status=STATUS_MACHINE_MISMATCH,
                is_demo=True,
            )

        if _is_expired(data.get("expires_at")):
            conn.execute(
                """UPDATE licenses
                      SET status = 'expired',
                          last_validation_at = ?,
                          next_validation_due_at = NULL,
                          validation_status = ?,
                          validation_message = ?
                    WHERE serial_key = ?""",
                (now_iso, STATUS_EXPIRED, "תוקף הרישיון פג.", serial),
            )
            data["status"] = "expired"
            data["last_validation_at"]     = now_iso
            data["next_validation_due_at"] = None
            data["validation_status"]      = STATUS_EXPIRED
            data["validation_message"]     = "תוקף הרישיון פג."
            _safe(_evt.log_event, serial, mid,
                  _evt.EVENT_VALIDATION_EXPIRED, ip=ip, actor="client")
            _safe(_evt.log_event, serial, mid,
                  _evt.EVENT_LICENSE_VALIDATED_FAILED,
                  message="expired", ip=ip, actor="client")
            _safe(_evt.log_event, serial, mid,
                  _evt.EVENT_LICENSE_SWITCHED_TO_DEMO_INVALID,
                  message="expired", ip=ip, actor="client")
            return _response(
                data,
                message="תוקף הרישיון פג.",
                status=STATUS_EXPIRED,
                is_demo=True,
            )

        if data.get("status") != "active":
            conn.execute(
                """UPDATE licenses
                      SET last_validation_at = ?,
                          validation_status = ?,
                          validation_message = ?
                    WHERE serial_key = ?""",
                (now_iso, str(data.get("status") or STATUS_UNUSED),
                 "הרישיון אינו פעיל.", serial),
            )
            data["last_validation_at"] = now_iso
            data["validation_status"]  = str(data.get("status") or STATUS_UNUSED)
            data["validation_message"] = "הרישיון אינו פעיל."
            _safe(_evt.log_event, serial, mid,
                  _evt.EVENT_VALIDATION_FAILED,
                  message=f"status={data.get('status')}", ip=ip, actor="client")
            _safe(_evt.log_event, serial, mid,
                  _evt.EVENT_LICENSE_VALIDATED_FAILED,
                  message=f"status={data.get('status')}",
                  ip=ip, actor="client")
            return _response(
                data,
                message="הרישיון אינו פעיל.",
                status=str(data.get("status") or STATUS_UNUSED),
                is_demo=True,
            )

        # -------- happy path ------------------------------------------------
        conn.execute(
            """UPDATE licenses
                  SET last_validation_at = ?,
                      next_validation_due_at = ?,
                      validation_status = 'success',
                      validation_message = ?,
                      hardware_id       = COALESCE(NULLIF(?, ''), hardware_id),
                      hostname          = COALESCE(NULLIF(?, ''), hostname),
                      client_public_ip  = COALESCE(NULLIF(?, ''), client_public_ip)
                WHERE serial_key = ?""",
            (now_iso, next_due_iso, "הרישיון תקף.",
             (machine_uuid or "").strip(),
             (hostname or "").strip(),
             (public_ip or "").strip(),
             serial),
        )
        data["last_validation_at"]     = now_iso
        data["next_validation_due_at"] = next_due_iso
        data["validation_status"]      = "success"
        data["validation_message"]     = "הרישיון תקף."

    _safe(_evt.log_event, serial, mid, _evt.EVENT_VALIDATION_SUCCESS,
          ip=ip, actor="client")
    _safe(_evt.log_event, serial, mid, _evt.EVENT_LICENSE_VALIDATED_SUCCESS,
          ip=ip, actor="client")
    # If the previous validation had failed, explicitly log a recovery event
    # so the admin dashboard can distinguish "came back online" from
    # routine periodic checks.
    if prev_validation_status and prev_validation_status not in ("", "success"):
        _safe(_evt.log_event, serial, mid,
              _evt.EVENT_LICENSE_VALIDATION_RECOVERED,
              message=f"previous={prev_validation_status}",
              ip=ip, actor="client")
    _safe(_evt.touch_activation_seen, serial, mid)
    return _response(data, "הרישיון תקף.",
                     status=STATUS_ACTIVE, is_demo=False)


def start_trial(
    mid: str, ip: str = "", user_agent: str = "",
    name: str = "", phone: str = "",
) -> Dict[str, Any]:
    mid = (mid or "").strip()
    if not mid:
        raise LicenseError("יש לספק מזהה מחשב.")

    with get_connection() as conn:
        # Pull the canonical trial length AND display name from the
        # system plan row so the admin's edits to "תוכנית ניסיון" (days
        # and name) are honored on every new activation.  Falls back
        # to the hard-coded defaults if the row is missing.
        trial_days = TRIAL_DAYS
        trial_plan_name = "תוכנית ניסיון"
        try:
            plan_row = conn.execute(
                "SELECT name, days, is_active, custom_type "
                "FROM subscription_plans "
                "WHERE is_system = 1 AND license_type = 'trial_14_days' "
                "LIMIT 1"
            ).fetchone()
            if plan_row is not None:
                if plan_row["days"]:
                    trial_days = int(plan_row["days"])
                # Prefer a custom display label when set, fall back to
                # the plan's own name.
                trial_plan_name = (
                    (plan_row["custom_type"] or "").strip()
                    or (plan_row["name"] or "").strip()
                    or trial_plan_name
                )
        except Exception:
            pass

        now = _now()
        expires_at = _iso(now + _dt.timedelta(days=trial_days))
        started_at = _iso(now)
        # Use the canonical XXXX-XXXXX-XXXXX-XXXX format so trial keys
        # look identical to admin-generated licenses.  We retry on the
        # unlikely event of a collision with an existing key.
        for _attempt in range(8):
            candidate = _generate_serial_key()
            clash = conn.execute(
                "SELECT 1 FROM licenses WHERE serial_key = ?", (candidate,)
            ).fetchone()
            if clash is None:
                serial_key = candidate
                break
        else:
            raise LicenseError("נכשלה יצירת מפתח ניסיון ייחודי.", status=500)

        # Product decision (2026-04 update per user request): one
        # trial per machine.  If this ``machine_id`` already has a
        # row in the ``trials`` table — i.e. it has previously
        # received a trial license — refuse with a clear Hebrew
        # error.  ``trials`` is the canonical source of truth: when
        # the admin deletes a trial license (or the dedicated
        # "אפס הכול" button wipes it) the row goes with it, and the
        # next attempt from the same machine is allowed through.
        existing = conn.execute(
            "SELECT serial_key FROM trials WHERE machine_id = ?", (mid,)
        ).fetchone()
        # ``machine_history`` rows mark machines whose paid license
        # was migrated AWAY (or that were retired by an admin tool).
        # Such a machine has already been "registered" with the
        # licensing system, so it doesn't get a fresh trial.
        history = conn.execute(
            "SELECT last_serial FROM machine_history WHERE machine_id = ?",
            (mid,),
        ).fetchone()
        is_blocked = existing is not None or history is not None
        if is_blocked:
            blocking_serial = ""
            if existing is not None:
                blocking_serial = str(existing["serial_key"] or "")
            elif history is not None:
                blocking_serial = str(history["last_serial"] or "")
            # Capture the lead BEFORE rejecting so the admin can
            # see exactly which contact tried — name + phone are
            # the user's input from the dialog and would otherwise
            # be discarded silently.
            try:
                with get_connection() as _conn_lead:
                    _conn_lead.execute(
                        """
                        INSERT INTO trial_leads
                        (machine_id, serial_key, name, phone, ip, user_agent, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            mid, "",
                            (name or "").strip(),
                            (phone or "").strip(),
                            ip or "", user_agent or "",
                            _iso(_now()),
                        ),
                    )
            except Exception:
                # Lead capture is opportunistic — never block a
                # rejection because of a write hiccup.
                pass
            try:
                _safe(_evt.log_event, blocking_serial, mid,
                      _evt.EVENT_TRIAL_ALREADY_USED,
                      ip=ip, actor="client")
            except Exception:
                pass
            raise LicenseError(
                "המחשב הזה כבר השתמש בניסיון. "
                "ניתן ניסיון אחד לכל מחשב.",
                status=403,
            )

        conn.execute(
            """
            INSERT INTO trials (machine_id, started_at, expires_at, serial_key)
            VALUES (?, ?, ?, ?)
            """,
            (mid, started_at, expires_at, serial_key),
        )
        # Persist the contact info supplied by the client on the
        # license row too (not just in trial_leads), so the admin's
        # "all licenses" / "lead profile" pages can show who the
        # trial belongs to without a JOIN.
        customer_name  = (name or "").strip()
        customer_phone = (phone or "").strip()
        # Defensive: wrap the license INSERT so that a schema drift
        # (e.g. a column missing on a stale production DB) cannot
        # propagate as an unhandled 500.  ``trials`` is already
        # written above which is what blocks the second click;
        # writing the licenses row is best-effort here.  If it
        # fails, the response is still synthesised from the
        # in-memory data we already have so the caller sees a
        # clean trial-activated reply.
        license_row_written = False
        try:
            conn.execute(
                """
                INSERT INTO licenses (serial_key, license_type, machine_id,
                                      status, activated_at, expires_at,
                                      created_at, notes, customer_name,
                                      customer_phone, plan_name, plan_days)
                VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    serial_key,
                    LICENSE_TYPE_TRIAL,
                    mid,
                    started_at,
                    expires_at,
                    started_at,
                    "auto-created by start_trial",
                    customer_name,
                    customer_phone,
                    trial_plan_name,
                    trial_days,
                ),
            )
            license_row_written = True
        except Exception:
            # Schema drift on a stale DB — log nothing here (the
            # caller still gets a clean response below).  The
            # ``trials`` row above is the source of truth that
            # prevents a re-trial.
            license_row_written = False

        row = None
        if license_row_written:
            try:
                row = conn.execute(
                    "SELECT * FROM licenses WHERE serial_key = ?",
                    (serial_key,),
                ).fetchone()
            except Exception:
                row = None

    # Build a baseline response from data we already hold, so the
    # rest of this function cannot crash the request even if
    # post-write helpers blow up.
    fallback_row: Dict[str, Any] = {
        "serial_key":     serial_key,
        "license_type":   LICENSE_TYPE_TRIAL,
        "machine_id":     mid,
        "status":         STATUS_ACTIVE,
        "activated_at":   started_at,
        "expires_at":     expires_at,
        "created_at":     started_at,
        "notes":          "auto-created by start_trial",
        "customer_name":  customer_name,
        "customer_phone": customer_phone,
        "plan_name":      trial_plan_name,
        "plan_days":      trial_days,
    }

    try:
        _safe(_evt.log_event, serial_key, mid, _evt.EVENT_TRIAL_STARTED,
              ip=ip, actor="client")
        _safe(_evt.record_activation, serial_key, mid, ip, "active",
              "trial", user_agent)

        # Persist the lead (name + phone) in a dedicated table so the
        # admin's "משתמשים חדשים" page can show every trial signup.
        # Non-fatal: if writing fails the trial activation still succeeds.
        try:
            with get_connection() as conn2:
                conn2.execute(
                    """
                    INSERT INTO trial_leads
                    (machine_id, serial_key, name, phone, ip, user_agent, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (mid, serial_key, (name or "").strip(), (phone or "").strip(),
                     ip or "", user_agent or "", started_at),
                )
        except Exception:
            pass

        row_dict = _row_to_dict(row) if row is not None else fallback_row
        # If the DB returned an incomplete row, top it up so the
        # response model always has the canonical trial fields.
        for k, v in fallback_row.items():
            row_dict.setdefault(k, v)
        return _response(row_dict,
                         f"הניסיון הופעל — {trial_days} ימים.",
                         status=STATUS_ACTIVE, is_demo=False)
    except Exception:
        # Last-resort safety net: return a clean response built from
        # the data we already committed so the desktop client sees
        # success instead of "server error".  Bookkeeping (events /
        # activations / leads) is best-effort.
        return _response(fallback_row,
                         f"הניסיון הופעל — {trial_days} ימים.",
                         status=STATUS_ACTIVE, is_demo=False)


def reset(serial: str, ip: str = "", actor: str = "") -> Dict[str, Any]:
    serial = (serial or "").strip()
    if not serial:
        raise LicenseError("יש לספק מפתח רישיון.")

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM licenses WHERE serial_key = ?", (serial,)
        ).fetchone()
        if row is None:
            raise LicenseError("מפתח הרישיון לא נמצא.")

        conn.execute(
            "UPDATE activations SET status = 'reset' WHERE serial_key = ?",
            (serial,),
        )
        conn.execute(
            "UPDATE licenses SET machine_id = NULL, status = 'unused' "
            "WHERE serial_key = ?",
            (serial,),
        )
        fresh = conn.execute(
            "SELECT * FROM licenses WHERE serial_key = ?", (serial,)
        ).fetchone()

    _safe(_evt.log_event, serial, None, _evt.EVENT_RESET_MACHINE,
          message=f"actor={actor}", ip=ip, actor=actor or "")
    _safe(_evt.log_event, serial, None, _evt.EVENT_LICENSE_RESET,
          message=f"actor={actor}", ip=ip, actor=actor or "")
    return _response(_row_to_dict(fresh), "הרישיון אופס ושוחרר.",
                     status=STATUS_UNUSED, is_demo=True)


def get_info(serial: str) -> Dict[str, Any]:
    serial = (serial or "").strip()
    if not serial:
        raise LicenseError("יש לספק מפתח רישיון.")
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM licenses WHERE serial_key = ?", (serial,)
        ).fetchone()
        if row is None:
            return _error_response(
                STATUS_NOT_FOUND, "מפתח הרישיון לא נמצא.", serial,
            )
        data = _row_to_dict(row)

        status_enum = str(data.get("status") or STATUS_UNUSED)
        is_demo = status_enum in (
            STATUS_DISABLED, STATUS_EXPIRED, STATUS_NOT_FOUND,
        )
        return _response(data, "", status=status_enum, is_demo=is_demo)


# ---------------------------------------------------------------------------
# License creation / admin helpers
# ---------------------------------------------------------------------------

_KEY_ALPHABET = string.ascii_uppercase + string.digits


def _generate_serial_key() -> str:
    """Produce a fresh ``XXXX-XXXXX-XXXXX-XXXX`` key using cryptographic RNG.

    Format: four groups of A-Z / 0-9 characters separated by dashes, with
    segment lengths 4-5-5-4 (18 alphanumeric chars + 3 dashes = 21 chars).

    Examples:
        A3JE-EOSI8-2IEUJ-98Q9
        K7M2-P4X9T-L0QWE-1R8N
        Z9QF-7WERT-8YUIO-4P2L
    """
    rng = random.SystemRandom()
    g1 = "".join(rng.choices(_KEY_ALPHABET, k=4))
    g2 = "".join(rng.choices(_KEY_ALPHABET, k=5))
    g3 = "".join(rng.choices(_KEY_ALPHABET, k=5))
    g4 = "".join(rng.choices(_KEY_ALPHABET, k=4))
    return f"{g1}-{g2}-{g3}-{g4}"


def create_license(
    license_type: str,
    customer_name: str = "",
    customer_first_name: str = "",
    customer_last_name: str = "",
    customer_email: str = "",
    customer_phone: str = "",
    notes: str = "",
    expires_at: Optional[str] = None,
    days: Optional[int] = None,
    plan_name: str = "",
    plan_days: Optional[int] = None,
    actor: str = "",
) -> Dict[str, Any]:
    """Insert a new unused license key and return the raw row dict.

    ``customer_name`` is the combined display name.  When
    ``customer_first_name`` / ``customer_last_name`` are given we also
    persist them in their own columns so downstream consumers can show
    "last, first" style sorting.  If ``customer_name`` is empty we
    derive it from the first/last pair.
    """
    license_type = _normalise_license_type(license_type)
    if license_type not in (
        LICENSE_TYPE_YEARLY, LICENSE_TYPE_LIFETIME, LICENSE_TYPE_TRIAL
    ):
        raise LicenseError(f"Unknown license_type: {license_type}")

    now = _now()
    created_at = _iso(now)

    # Auto-compose customer_name from first+last if caller didn't
    # provide an explicit combined value.
    if not (customer_name or "").strip():
        parts = [p for p in ((customer_first_name or "").strip(),
                             (customer_last_name  or "").strip()) if p]
        customer_name = " ".join(parts)

    if expires_at is None and days:
        if license_type == LICENSE_TYPE_LIFETIME:
            expires_at = None
        else:
            expires_at = _iso(now + _dt.timedelta(days=int(days)))

    last_err: Optional[Exception] = None
    for _ in range(8):
        key = _generate_serial_key()
        try:
            with get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO licenses (serial_key, license_type, machine_id,
                                          status, activated_at, expires_at,
                                          created_at, notes, customer_name,
                                          customer_first_name,
                                          customer_last_name,
                                          customer_email, customer_phone,
                                          plan_name, plan_days)
                    VALUES (?, ?, NULL, 'unused', NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (key, license_type, expires_at, created_at,
                     notes or "", customer_name or "",
                     customer_first_name or "",
                     customer_last_name or "",
                     customer_email or "", customer_phone or "",
                     plan_name or "",
                     plan_days if plan_days is not None else days),
                )
                row = conn.execute(
                    "SELECT * FROM licenses WHERE serial_key = ?", (key,)
                ).fetchone()
            _safe(_evt.log_event, key, None, _evt.EVENT_LICENSE_CREATED,
                  message=f"type={license_type} customer={customer_name}",
                  actor=actor or "")
            return _row_to_dict(row)
        except Exception as e:
            last_err = e
            continue

    raise LicenseError(
        f"Failed to generate a unique license key: {last_err}", status=500
    )


def generate(
    license_type: str,
    days: Optional[int] = None,
    customer_name: str = "",
    customer_first_name: str = "",
    customer_last_name: str = "",
    customer_email: str = "",
    customer_phone: str = "",
    notes: str = "",
    plan_name: str = "",
    plan_days: Optional[int] = None,
    actor: str = "",
) -> Dict[str, Any]:
    """Public wrapper around :func:`create_license` returning the unified shape."""
    row = create_license(
        license_type=license_type,
        customer_name=(customer_name or "").strip(),
        customer_first_name=(customer_first_name or "").strip(),
        customer_last_name=(customer_last_name or "").strip(),
        customer_email=(customer_email or "").strip(),
        customer_phone=(customer_phone or "").strip(),
        notes=(notes or "").strip(),
        days=days,
        plan_name=(plan_name or "").strip(),
        plan_days=plan_days,
        actor=actor,
    )
    return _response(row, "הרישיון נוצר בהצלחה.",
                     status=STATUS_UNUSED, is_demo=False)


def disable_license(
    serial: str,
    reason: str = "",
    actor: str = "",
    ip: str = "",
) -> Dict[str, Any]:
    serial = (serial or "").strip()
    if not serial:
        raise LicenseError("יש לספק מפתח רישיון.")

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM licenses WHERE serial_key = ?", (serial,)
        ).fetchone()
        if row is None:
            raise LicenseError("מפתח הרישיון לא נמצא.", status=404)
        conn.execute(
            """
            UPDATE licenses
               SET status = 'disabled',
                   disabled_at = ?,
                   disabled_reason = ?,
                   disabled_by = ?
             WHERE serial_key = ?
            """,
            (_iso(_now()), reason or "", actor or "", serial),
        )
        fresh = conn.execute(
            "SELECT * FROM licenses WHERE serial_key = ?", (serial,)
        ).fetchone()

    msg = f"actor={actor or ''}; reason={reason or ''}"
    _safe(_evt.log_event, serial, None, _evt.EVENT_DISABLED_LICENSE,
          message=msg, ip=ip, actor=actor or "")
    _safe(_evt.log_event, serial, None, _evt.EVENT_LICENSE_DISABLED,
          message=msg, ip=ip, actor=actor or "")
    return _response(_row_to_dict(fresh), "הרישיון בוטל.",
                     status=STATUS_DISABLED, is_demo=True)


def enable_license(
    serial: str,
    actor: str = "",
    ip: str = "",
) -> Dict[str, Any]:
    serial = (serial or "").strip()
    if not serial:
        raise LicenseError("יש לספק מפתח רישיון.")

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM licenses WHERE serial_key = ?", (serial,)
        ).fetchone()
        if row is None:
            raise LicenseError("מפתח הרישיון לא נמצא.", status=404)

        data = _row_to_dict(row)
        new_status = STATUS_ACTIVE if (data.get("machine_id") or "") else STATUS_UNUSED
        conn.execute(
            """
            UPDATE licenses
               SET status = ?,
                   disabled_at = NULL,
                   disabled_reason = '',
                   disabled_by = ''
             WHERE serial_key = ?
            """,
            (new_status, serial),
        )
        fresh = conn.execute(
            "SELECT * FROM licenses WHERE serial_key = ?", (serial,)
        ).fetchone()

    msg = f"actor={actor or ''}"
    _safe(_evt.log_event, serial, None, _evt.EVENT_LICENSE_ENABLED,
          message=msg, ip=ip, actor=actor or "")

    fresh_dict = _row_to_dict(fresh)
    is_demo = new_status != STATUS_ACTIVE
    return _response(fresh_dict, "הרישיון הופעל מחדש.",
                     status=new_status, is_demo=is_demo)


def delete_license(
    serial: str,
    actor: str = "",
    ip: str = "",
) -> Dict[str, Any]:
    """Permanently remove a license and all its activation history.

    Returns a minimal dict ``{"success": True, "serial_key": serial}`` on
    success. Raises :class:`LicenseError` if the serial is missing from
    the DB. The associated activations rows are removed as well so the
    dashboard counts stay consistent. The *events* table is intentionally
    preserved so the audit log still reflects what happened historically.
    """
    serial = (serial or "").strip()
    if not serial:
        raise LicenseError("יש לספק מפתח רישיון.")

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM licenses WHERE serial_key = ?", (serial,)
        ).fetchone()
        if row is None:
            raise LicenseError("מפתח הרישיון לא נמצא.", status=404)

        conn.execute(
            "DELETE FROM activations WHERE serial_key = ?", (serial,),
        )
        conn.execute(
            "DELETE FROM trials WHERE serial_key = ?", (serial,),
        )
        conn.execute(
            "DELETE FROM licenses WHERE serial_key = ?", (serial,),
        )

    _safe(_evt.log_event, serial, None, _evt.EVENT_LICENSE_DELETED,
          message=f"actor={actor or ''}", ip=ip, actor=actor or "")
    return {"success": True, "serial_key": serial}


def edit_license(
    serial: str,
    created_at: Optional[str] = None,
    activated_at: Optional[str] = None,
    expires_at: Optional[str] = None,
    license_type: Optional[str] = None,
    customer_first_name: Optional[str] = None,
    customer_last_name: Optional[str] = None,
    customer_phone: Optional[str] = None,
    customer_email: Optional[str] = None,
    notes: Optional[str] = None,
    actor: str = "",
    ip: str = "",
) -> Dict[str, Any]:
    """Patch individual fields on an existing license.

    Any argument left as ``None`` is untouched.  Passing an empty
    string explicitly clears the field.  Date values should be
    ISO-8601; a ``YYYY-MM-DDTHH:MM`` datetime-local value is accepted
    and interpreted as UTC (caller is expected to convert if needed).

    Raises ``LicenseError`` when the license doesn't exist.
    """
    serial = (serial or "").strip()
    if not serial:
        raise LicenseError("יש לספק מפתח רישיון.")

    def _norm_iso(v: Optional[str]) -> Optional[str]:
        """Normalise an incoming date string into the DB's ISO format.

        Accepts either a full ISO string or the browser's
        ``datetime-local`` shape (``YYYY-MM-DDTHH:MM``).  Empty string
        → stored as NULL.  Unparseable → raises ValueError.
        """
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            return ""  # explicit clear
        # datetime-local lacks seconds + timezone; pad them in.
        if len(s) == 16 and s[10] == "T":
            s = s + ":00"
        try:
            # Fromisoformat handles naive + tz-aware.  Naive inputs
            # are treated as **UTC** for consistency with server storage.
            dt = _dt.datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.timezone.utc)
            return _iso(dt)
        except ValueError as exc:
            raise ValueError(f"ערך תאריך לא תקין: {v!r}") from exc

    updates: Dict[str, Any] = {}

    if created_at is not None:
        val = _norm_iso(created_at)
        if val == "":
            raise LicenseError("תאריך יצירה לא יכול להיות ריק.")
        updates["created_at"] = val

    if activated_at is not None:
        val = _norm_iso(activated_at)
        updates["activated_at"] = val if val else None

    if expires_at is not None:
        val = _norm_iso(expires_at)
        updates["expires_at"] = val if val else None
        # Reconcile ``status`` whenever the expiry date changes so the
        # admin UI never shows "active" with a past date (or vice-versa).
        # * new date is in the PAST → mark expired
        # * new date is in the FUTURE and license was expired → revive
        #   it to the natural state ("active" if bound to a machine,
        #   otherwise "unused").
        try:
            new_exp = val or ""
            # Pull the current row once so we can see the old status /
            # machine binding in a single read.
            with get_connection() as conn:
                cur_row = conn.execute(
                    "SELECT status, machine_id, license_type "
                    "FROM licenses WHERE serial_key = ?",
                    (serial,),
                ).fetchone()
            cur_status = (cur_row["status"] if cur_row else "") or ""
            cur_mid    = (cur_row["machine_id"] if cur_row else "") or ""
            cur_type   = (cur_row["license_type"] if cur_row else "") or ""

            if new_exp:
                if _is_expired(new_exp):
                    # Past date → force expired.
                    updates["status"] = STATUS_EXPIRED
                    updates["validation_status"]  = "expired"
                    updates["validation_message"] = "תוקף הרישיון פג."
                else:
                    # Future date → revive if previously expired.
                    if cur_status.lower() == STATUS_EXPIRED:
                        new_status = STATUS_ACTIVE if cur_mid else STATUS_UNUSED
                        updates["status"] = new_status
                        updates["validation_status"]  = "success"
                        updates["validation_message"] = "הרישיון תקף."
            else:
                # Cleared expiry — only lifetime makes sense, caller
                # handled that below via license_type=lifetime.
                pass
        except Exception:
            # Reconciliation is best-effort; failure here shouldn't
            # prevent the actual date change from landing.
            pass

    if license_type is not None:
        lt = str(license_type).strip()
        # Empty string means "don't change" — skip gracefully.
        if lt:
            if lt not in (LICENSE_TYPE_YEARLY, LICENSE_TYPE_LIFETIME, LICENSE_TYPE_TRIAL):
                raise LicenseError(f"סוג רישיון לא תקין: {lt!r}")
            updates["license_type"] = lt
            # Lifetime licenses MUST have no expires_at — validate()
            # would otherwise still downgrade them when the date hits.
            if lt == LICENSE_TYPE_LIFETIME:
                updates["expires_at"] = None
            # Transitioning INTO a bounded kind without a supplied
            # expires_at?  Keep whatever value is already stored — the
            # caller is expected to have passed one in the same request.

    for col, new in (
        ("customer_first_name", customer_first_name),
        ("customer_last_name",  customer_last_name),
        ("customer_phone",      customer_phone),
        ("customer_email",      customer_email),
        ("notes",               notes),
    ):
        if new is not None:
            updates[col] = str(new).strip()

    # Re-compose customer_name from first + last whenever either is
    # edited so the legacy display column stays in sync.
    if ("customer_first_name" in updates or
            "customer_last_name"  in updates):
        with get_connection() as conn:
            row = conn.execute(
                "SELECT customer_first_name, customer_last_name "
                "FROM licenses WHERE serial_key = ?", (serial,),
            ).fetchone()
        cur_first = updates.get("customer_first_name",
                                row["customer_first_name"] if row else "")
        cur_last  = updates.get("customer_last_name",
                                row["customer_last_name"] if row else "")
        updates["customer_name"] = (f"{cur_first} {cur_last}".strip()
                                    or updates.get("customer_name", ""))

    if not updates:
        # Nothing to do; return the current row unchanged.
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM licenses WHERE serial_key = ?", (serial,)
            ).fetchone()
        if row is None:
            raise LicenseError("מפתח הרישיון לא נמצא.", status=404)
        return _row_to_dict(row)

    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    params = list(updates.values()) + [serial]

    with get_connection() as conn:
        res = conn.execute(
            f"UPDATE licenses SET {set_clause} WHERE serial_key = ?",
            params,
        )
        if res.rowcount == 0:
            raise LicenseError("מפתח הרישיון לא נמצא.", status=404)
        fresh = conn.execute(
            "SELECT * FROM licenses WHERE serial_key = ?", (serial,)
        ).fetchone()

    _safe(
        _evt.log_event, serial, None, _evt.EVENT_LICENSE_CREATED,  # re-use generic tag
        message=f"edited fields: {', '.join(updates.keys())}",
        ip=ip, actor=actor or "",
    )
    return _row_to_dict(fresh)


def extend_license(
    serial: str,
    days: int,
    actor: str = "",
    ip: str = "",
) -> Dict[str, Any]:
    """Add *days* to the license's expiry date. Activates expired licenses."""
    serial = (serial or "").strip()
    if not serial:
        raise LicenseError("יש לספק מפתח רישיון.")
    if not isinstance(days, int) or days <= 0:
        raise LicenseError("מספר הימים חייב להיות חיובי.")

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM licenses WHERE serial_key = ?", (serial,)
        ).fetchone()
        if row is None:
            raise LicenseError("מפתח הרישיון לא נמצא.", status=404)

        data = _row_to_dict(row)
        if data.get("license_type") == LICENSE_TYPE_LIFETIME:
            raise LicenseError("לא ניתן להאריך רישיון לכל החיים.")

        base_dt = _parse(data.get("expires_at"))
        # If already expired or never set, base extension on now.
        if base_dt is None or base_dt <= _now():
            base_dt = _now()
        new_expires = base_dt + _dt.timedelta(days=int(days))

        # If the license was 'expired', move it back to its natural state:
        # active if bound to a machine, unused otherwise.
        prev_status = str(data.get("status") or "")
        new_status = prev_status
        if prev_status == STATUS_EXPIRED:
            new_status = STATUS_ACTIVE if (data.get("machine_id") or "") \
                else STATUS_UNUSED

        conn.execute(
            "UPDATE licenses SET expires_at = ?, status = ? WHERE serial_key = ?",
            (_iso(new_expires), new_status, serial),
        )
        fresh = conn.execute(
            "SELECT * FROM licenses WHERE serial_key = ?", (serial,)
        ).fetchone()

    _safe(_evt.log_event, serial, None, _evt.EVENT_LICENSE_EXTENDED,
          message=f"actor={actor}; days={days}; new_expires={_iso(new_expires)}",
          ip=ip, actor=actor or "")
    return _response(_row_to_dict(fresh), f"תוקף הרישיון הוארך ב-{days} ימים.",
                     status=new_status, is_demo=(new_status != STATUS_ACTIVE))
