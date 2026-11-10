# -*- coding: utf-8 -*-
"""
server/app/services/ai_service.py
---------------------------------
Orchestration for the commercial AILabTools cloud filters.

Pipeline (all server-side; the desktop client only sends image + filter +
idempotency key + its serial/HWID):

  1. authorize the license (active + bound + not expired/disabled) —
     server-side, never trusting the client's local state.
  2. load the filter from the admin catalogue (must be enabled).
  3. validate the image (type + size).
  4. enforce the per-filter supplier COST CEILING before doing anything.
  5. reserve (debit) the sale price atomically — this is what prevents a
     double-spend across concurrent requests.
  6. call the provider (mock in dev; AILabTools in prod).
       success           -> keep the charge, mark 'succeeded'.
       definite failure   -> REFUND, mark 'failed'  (customer not charged).
       timeout / unknown  -> keep the charge, mark 'unknown' (NO blind
                             refund/retry — reconcile before releasing).

Idempotency: every request carries a client-generated key, stored UNIQUE on
``ai_requests``. A retry with the same key never charges or calls twice — it
returns the recorded outcome (or "in progress" while the first is in flight).

The AILabTools API key is read from config/env and used ONLY here; it is
never returned to the client and never logged.
"""

from __future__ import annotations

import base64
import math
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .. import config as _cfg
from ..database import get_connection
from . import credits_service as _credits
from . import events_service as _evt
from . import license_service as _lic


# ---------------------------------------------------------------------------
# Request status machine (ai_requests.status)
# ---------------------------------------------------------------------------
ST_PENDING = "pending"      # row created, not yet charged
ST_CHARGED = "charged"      # reserved, provider call in flight
ST_SUCCEEDED = "succeeded"  # provider returned an image; charge stands
ST_FAILED = "failed"        # definite failure; refunded (or never charged)
ST_UNKNOWN = "unknown"      # ambiguous (timeout/no response); charge held


# ---------------------------------------------------------------------------
# Generic user-facing messages (never leak provider internals to the client).
# ---------------------------------------------------------------------------
MSG_SERVICE_UNAVAILABLE = "השירות אינו זמין זמנית. אנא פנה לתמיכה."
MSG_TRY_AGAIN = "אירעה תקלה זמנית. נסה שוב מאוחר יותר."
MSG_BAD_IMAGE = "התמונה אינה נתמכת. יש להשתמש ב-JPG או PNG בגודל סביר."
MSG_LICENSE_BLOCKED = "הרישיון אינו פעיל. פעולות AI אינן זמינות."
MSG_INSUFFICIENT = "אין מספיק יתרת קרדיטים לביצוע הפעולה."
MSG_FILTER_UNAVAILABLE = "הפילטר אינו זמין."


def _now_iso() -> str:
    return _credits._now_iso()


# ---------------------------------------------------------------------------
# Provider result
# ---------------------------------------------------------------------------
@dataclass
class ProviderResult:
    ok: bool                         # True = image produced
    definite_failure: bool           # True = provider replied with an error
    image_bytes: Optional[bytes] = None
    provider_request_id: str = ""
    provider_log_id: str = ""
    error_code: str = ""             # technical code (admin only)
    error_detail: str = ""           # technical detail (admin only)


class ProviderTimeout(Exception):
    """No response from the provider (ambiguous — may have succeeded)."""


# ---------------------------------------------------------------------------
# Mock provider — in-process, no network, no cost. Used for all dev/testing.
# Behaviour can be steered for tests via params["mock_force"]:
#   "fail"      -> definite failure (provider error response)
#   "timeout"   -> raises ProviderTimeout (ambiguous path)
#   "nocredits" -> ERROR_NOT_ENOUGH_CREDITS (our provider balance empty)
# Any other value -> success, echoing the input image back.
# ---------------------------------------------------------------------------
def _mock_apply(image_bytes: bytes, params: Dict[str, Any]) -> ProviderResult:
    force = str((params or {}).get("mock_force", "")).strip().lower()
    rid = "mock_" + secrets.token_hex(6)
    if force == "timeout":
        raise ProviderTimeout("mock timeout")
    if force == "nocredits":
        return ProviderResult(
            ok=False, definite_failure=True,
            provider_request_id=rid,
            error_code="ERROR_NOT_ENOUGH_CREDITS",
            error_detail="mock: provider account out of credits",
        )
    if force == "fail":
        return ProviderResult(
            ok=False, definite_failure=True,
            provider_request_id=rid,
            error_code="MOCK_FAIL",
            error_detail="mock: forced failure",
        )
    # success — echo the original image back as the "processed" result.
    return ProviderResult(
        ok=True, definite_failure=False,
        image_bytes=image_bytes, provider_request_id=rid,
        provider_log_id="mocklog_" + secrets.token_hex(4),
    )


# ---------------------------------------------------------------------------
# Real AILabTools provider. Only reached when AI_PROVIDER == "ailabtools".
# Never called in dev. Verified against the official docs (see the
# reference memo) but NOT exercised until an explicit go-ahead.
# ---------------------------------------------------------------------------
def _filter_routing(filt: Dict[str, Any]) -> Dict[str, Any]:
    """Parse the per-filter AILabTools routing config out of ``params_json``.

    Shape (set by the admin catalogue seed):
        {"ailab_kind": "cartoon"|"emotion"|...,
         "image_field": "image"|"image_target",
         "form": {"type": "pixar"} | {"service_choice": "3"}}
    Returns safe defaults when absent/malformed so a missing config degrades
    to a plain portrait-animation call rather than crashing.
    """
    import json
    raw = filt.get("params_json") or ""
    try:
        cfg = json.loads(raw) if raw else {}
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    image_field = str(cfg.get("image_field") or "image").strip() or "image"
    form_raw = cfg.get("form") or {}
    form = {str(k): str(v) for k, v in form_raw.items()} \
        if isinstance(form_raw, dict) else {}
    return {
        "kind": str(cfg.get("ailab_kind") or "").strip(),
        "image_field": image_field,
        "form": form,
    }


def _ailabtools_apply(
    filt: Dict[str, Any], image_bytes: bytes, client_params: Dict[str, Any],
) -> ProviderResult:
    import httpx  # lazy import — only needed in prod

    api_key = _cfg.AILABTOOLS_API_KEY
    if not api_key:
        # Treated as "service unavailable" (definite failure → refund).
        return ProviderResult(
            ok=False, definite_failure=True,
            error_code="NO_API_KEY",
            error_detail="AILABTOOLS_API_KEY is not configured",
        )
    provider_path = filt["provider_path"]
    result_kind = filt.get("result_kind", "image_url")
    route = _filter_routing(filt)
    url = f"{_cfg.AILABTOOLS_BASE_URL}/{provider_path.lstrip('/')}"
    headers = {"ailabapi-api-key": api_key}
    # The multipart image field name and the provider control params (type /
    # service_choice / ...) come ONLY from the admin-configured catalogue —
    # NEVER from the client request — so a client cannot steer the provider.
    files = {route["image_field"]:
             ("image.jpg", image_bytes, "application/octet-stream")}
    data = dict(route["form"])
    try:
        with httpx.Client(timeout=_cfg.AI_REQUEST_TIMEOUT_SECS) as client:
            resp = client.post(url, headers=headers, files=files, data=data)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise ProviderTimeout(str(exc)) from exc

    # Parse the unified AILabTools envelope.
    try:
        body = resp.json()
    except Exception:
        body = {}
    error_code = body.get("error_code", resp.status_code)
    rid = str(body.get("request_id", ""))
    log_id = str(body.get("log_id", ""))

    if resp.status_code == 200 and (error_code == 0 or error_code == "0"):
        out = _extract_image(body.get("data") or {}, result_kind)
        if out is None:
            return ProviderResult(
                ok=False, definite_failure=True,
                provider_request_id=rid, provider_log_id=log_id,
                error_code="NO_IMAGE_IN_RESPONSE",
                error_detail="success envelope without an image payload",
            )
        return ProviderResult(
            ok=True, definite_failure=False, image_bytes=out,
            provider_request_id=rid, provider_log_id=log_id,
        )

    # Non-200 / error_code != 0 → a definite provider failure (not billed).
    detail = ""
    try:
        ed = body.get("error_detail") or {}
        detail = f"{ed.get('code', '')}: {ed.get('message', '')}"
    except Exception:
        detail = ""
    return ProviderResult(
        ok=False, definite_failure=True,
        provider_request_id=rid, provider_log_id=log_id,
        error_code=str((body.get("error_detail") or {}).get("code")
                       or error_code),
        error_detail=detail or f"http {resp.status_code}",
    )


def _extract_image(data: Dict[str, Any], result_kind: str) -> Optional[bytes]:
    """Pull image bytes out of an AILabTools ``data`` object.

    Handles the documented shapes across all three endpoints:
      * portrait-animation  → ``data.image_url`` (fetch it).
      * emotion-editor      → ``data.image`` is base64 (decode it).
      * legacy              → ``data.binary_data_base64`` (decode it).
    ``result_kind`` ("image_url" | "base64") decides which to try first; both
    fall through to the other shape defensively.
    """
    def _try_b64(val) -> Optional[bytes]:
        if isinstance(val, list):
            val = val[0] if val else ""
        if isinstance(val, str) and val and not val.startswith("http"):
            try:
                return base64.b64decode(val)
            except Exception:
                return None
        return None

    if result_kind == "base64":
        # emotion-editor returns base64 inline in ``image`` (or legacy field).
        out = _try_b64(data.get("image")) or _try_b64(
            data.get("binary_data_base64"))
        if out:
            return out
    elif data.get("binary_data_base64"):
        out = _try_b64(data.get("binary_data_base64"))
        if out:
            return out

    # URL shape (portrait-animation ``image_url``; ``image`` may be a URL).
    url = data.get("image_url") or data.get("image") or ""
    if isinstance(url, str) and url.startswith("http"):
        import httpx  # lazy — only prod URL responses need the HTTP client
        try:
            with httpx.Client(timeout=_cfg.AI_REQUEST_TIMEOUT_SECS) as client:
                r = client.get(url)
            if r.status_code == 200:
                return r.content
        except Exception:
            return None
    return None


def _call_provider(
    filt: Dict[str, Any], image_bytes: bytes, params: Dict[str, Any]
) -> ProviderResult:
    if _cfg.AI_PROVIDER == "ailabtools":
        return _ailabtools_apply(filt, image_bytes, params)
    return _mock_apply(image_bytes, params)


# ---------------------------------------------------------------------------
# Image + cost guards
# ---------------------------------------------------------------------------
def _validate_image(image_bytes: bytes) -> Optional[str]:
    """Return an error message if the image is unsupported, else None."""
    if not image_bytes:
        return "empty image"
    max_bytes = max(1, _cfg.AI_MAX_IMAGE_MB) * 1024 * 1024
    if len(image_bytes) > max_bytes:
        return f"image too large (> {_cfg.AI_MAX_IMAGE_MB} MB)"
    is_jpeg = image_bytes[:3] == b"\xff\xd8\xff"
    is_png = image_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    if not (is_jpeg or is_png):
        return "unsupported image type (JPG/PNG only)"
    return None


def worst_case_cost_agorot(provider_credits: int) -> int:
    """Upper bound on the supplier cost of one request, in agorot.

    credits × $/credit × ₪/$ × safety-margin × 100, rounded UP.
    """
    usd = float(provider_credits) * _cfg.AI_CREDIT_USD
    ils = usd * _cfg.AI_USD_TO_ILS * _cfg.AI_COST_SAFETY_MARGIN
    return int(math.ceil(ils * 100.0))


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------
def _load_filter(conn: sqlite3.Connection, filter_key: str):
    return conn.execute(
        "SELECT * FROM ai_filters WHERE filter_key = ?", (filter_key,)
    ).fetchone()


def list_enabled_filters() -> list[Dict[str, Any]]:
    """Public catalogue for the client — only safe fields (no internal cost)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT filter_key, display_name, sale_price_agorot, sort_order "
            "FROM ai_filters WHERE is_enabled = 1 ORDER BY sort_order, "
            "display_name"
        ).fetchall()
    return [{"filter_key": r["filter_key"],
             "display_name": r["display_name"],
             "sale_price_agorot": int(r["sale_price_agorot"])}
            for r in rows]


# ---------------------------------------------------------------------------
# ai_requests helpers
# ---------------------------------------------------------------------------
def _get_request_by_idem(conn, idem: str):
    return conn.execute(
        "SELECT * FROM ai_requests WHERE idempotency_key = ?", (idem,)
    ).fetchone()


def _update_request(conn, req_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE ai_requests SET {cols} WHERE id = ?",
        list(fields.values()) + [req_id],
    )


def _result(http_status: int, success: bool, **extra) -> Dict[str, Any]:
    out = {"success": success, "http_status": http_status}
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def run_filter(
    *,
    serial: str,
    machine_uuid: str,
    filter_key: str,
    image_bytes: bytes,
    idempotency_key: str,
    params: Optional[Dict[str, Any]] = None,
    mid: str = "",
    ip: str = "",
) -> Dict[str, Any]:
    """Run one cloud filter end-to-end. Returns a JSON-ready dict with an
    ``http_status`` the route should use."""
    params = dict(params or {})
    idempotency_key = (idempotency_key or "").strip()
    filter_key = (filter_key or "").strip()
    if not idempotency_key:
        return _result(400, False, error_code="MISSING_IDEMPOTENCY",
                       message="missing idempotency key")

    # 1) license gate (server-side)
    auth = _lic.authorize_for_ai(serial, machine_uuid=machine_uuid, mid=mid)
    if not auth["ok"]:
        return _result(403, False, status=auth["status"],
                       error_code="LICENSE_BLOCKED",
                       message=MSG_LICENSE_BLOCKED)
    serial = auth["serial_key"]

    # 2) catalogue
    with get_connection() as conn:
        frow = _load_filter(conn, filter_key)
    if frow is None or not int(frow["is_enabled"]):
        return _result(404, False, error_code="FILTER_UNAVAILABLE",
                       message=MSG_FILTER_UNAVAILABLE)
    filt = {k: frow[k] for k in frow.keys()}
    sale_price = int(filt["sale_price_agorot"])

    # 3) image validation (before any charge)
    img_err = _validate_image(image_bytes)
    if img_err:
        return _result(400, False, error_code="BAD_IMAGE",
                       message=MSG_BAD_IMAGE, admin_detail=img_err)

    # 4) cost ceiling (before any charge / call)
    worst = worst_case_cost_agorot(int(filt["provider_credits"]))
    ceiling = int(filt["cost_ceiling_agorot"])
    if ceiling > 0 and worst > ceiling:
        _safe_event(serial, mid, "ai_cost_ceiling_block",
                    f"{filter_key}: worst={worst}ag > ceiling={ceiling}ag", ip)
        return _result(409, False, error_code="COST_CEILING",
                       message=MSG_SERVICE_UNAVAILABLE,
                       admin_detail=f"worst-case {worst}ag exceeds ceiling "
                                    f"{ceiling}ag")

    # resolve wallet / customer
    wallet = _credits.get_balance_for_license(serial)
    customer_id = wallet["customer_id"]

    # 5) idempotent request row + state machine
    with get_connection() as conn:
        existing = _get_request_by_idem(conn, idempotency_key)
        if existing is not None:
            st = existing["status"]
            if st == ST_SUCCEEDED:
                return _result(200, True, status="already_succeeded",
                               balance_agorot=_credits.get_balance_for_customer(
                                   customer_id),
                               message="הבקשה כבר הושלמה.")
            if st == ST_CHARGED:
                return _result(409, False, status="in_progress",
                               error_code="IN_PROGRESS",
                               message="הבקשה כבר בעיבוד.")
            if st == ST_UNKNOWN:
                return _result(409, False, status="unknown",
                               error_code="UNKNOWN",
                               message=MSG_SERVICE_UNAVAILABLE)
            if st == ST_FAILED:
                return _result(200, False, status="failed",
                               error_code=existing["error_code"] or "FAILED",
                               message=MSG_TRY_AGAIN,
                               balance_agorot=_credits.get_balance_for_customer(
                                   customer_id))
            # st == ST_PENDING → resumable (no charge happened yet)
            req_id = int(existing["id"])
        else:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO ai_requests
                    (customer_id, wallet_id, license_serial, machine_uuid,
                     filter_key, provider, status, sale_price_agorot,
                     provider_credits, idempotency_key, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (customer_id, wallet["wallet_id"], serial,
                     (machine_uuid or "").strip(), filter_key,
                     filt.get("provider", "ailabtools"), ST_PENDING,
                     sale_price, int(filt["provider_credits"]),
                     idempotency_key, _now_iso()),
                )
                req_id = int(cur.lastrowid)
            except sqlite3.IntegrityError:
                # Raced with a concurrent identical request — defer to it.
                return _result(409, False, status="in_progress",
                               error_code="IN_PROGRESS",
                               message="הבקשה כבר בעיבוד.")

    # 6) reserve (atomic debit). Idempotent on the charge key.
    try:
        charge_res = _credits.charge(
            customer_id, sale_price,
            idempotency_key="chg:" + idempotency_key,
            ai_request_id=req_id,
            reason=f"פילטר AI: {filt['display_name']}",
        )
    except _credits.InsufficientCredits:
        with get_connection() as conn:
            _update_request(conn, req_id, status=ST_FAILED,
                            error_code="INSUFFICIENT_CREDITS",
                            finished_at=_now_iso())
        return _result(402, False, error_code="INSUFFICIENT_CREDITS",
                       message=MSG_INSUFFICIENT,
                       balance_agorot=_credits.get_balance_for_customer(
                           customer_id))

    with get_connection() as conn:
        _update_request(conn, req_id, status=ST_CHARGED)

    # 7) call the provider
    try:
        pres = _call_provider(filt, image_bytes, params)
    except ProviderTimeout as exc:
        # Ambiguous — the call may have succeeded on the provider side.
        # Hold the charge, mark unknown, DO NOT blind-retry/refund.
        with get_connection() as conn:
            _update_request(conn, req_id, status=ST_UNKNOWN,
                            error_code="TIMEOUT",
                            error_detail=str(exc)[:500],
                            finished_at=_now_iso())
        _safe_event(serial, mid, "ai_request_unknown",
                    f"{filter_key}: timeout", ip)
        return _result(504, False, status="unknown", error_code="TIMEOUT",
                       message=MSG_SERVICE_UNAVAILABLE,
                       balance_agorot=_credits.get_balance_for_customer(
                           customer_id))

    if pres.ok and pres.image_bytes:
        with get_connection() as conn:
            _update_request(
                conn, req_id, status=ST_SUCCEEDED,
                provider_request_id=pres.provider_request_id,
                provider_log_id=pres.provider_log_id,
                finished_at=_now_iso())
        _safe_event(serial, mid, "ai_request_success", filter_key, ip)
        return _result(
            200, True, status="succeeded",
            image_base64=base64.b64encode(pres.image_bytes).decode("ascii"),
            charged_agorot=sale_price,
            balance_agorot=charge_res["balance_agorot"],
            message="")

    # 8) definite failure → refund, mark failed. Customer ends net-zero.
    refund_res = _credits.refund(
        customer_id, sale_price,
        idempotency_key="rfnd:" + idempotency_key,
        ai_request_id=req_id,
        reason=f"החזר על כשל: {filt['display_name']}",
    )
    with get_connection() as conn:
        _update_request(
            conn, req_id, status=ST_FAILED,
            provider_request_id=pres.provider_request_id,
            provider_log_id=pres.provider_log_id,
            error_code=pres.error_code or "PROVIDER_FAILURE",
            error_detail=(pres.error_detail or "")[:500],
            finished_at=_now_iso())
    # Map provider error → user message (generic; admin sees the detail).
    if pres.error_code == "ERROR_NOT_ENOUGH_CREDITS" or pres.error_code == "NO_API_KEY":
        user_msg = MSG_SERVICE_UNAVAILABLE
    elif pres.error_code in ("ERROR_NO_FACE_IN_FILE", "ERROR_FACE_NOT_FOUND"):
        user_msg = "לא זוהו פנים בתמונה."
    elif pres.error_code in ("FILE_SIZE_EXCEEDS_LIMIT",
                             "UNSUPPORTED_FILE_TYPES",
                             "FILE_RESOLUTION_EXCEEDS_LIMITS",
                             "ERROR_LOW_RESOLUTION", "ERROR_HIGH_RESOLUTION"):
        user_msg = MSG_BAD_IMAGE
    else:
        user_msg = MSG_TRY_AGAIN
    _safe_event(serial, mid, "ai_request_failed",
                f"{filter_key}: {pres.error_code}", ip)
    return _result(502, False, status="failed",
                   error_code=pres.error_code or "PROVIDER_FAILURE",
                   message=user_msg,
                   refunded_agorot=sale_price,
                   balance_agorot=refund_res["balance_agorot"])


def _safe_event(serial, mid, ev_type, message, ip) -> None:
    try:
        _evt.log_event(serial or None, mid or None, ev_type,
                       message=str(message)[:500], ip=ip or "", actor="client")
    except Exception:
        pass
