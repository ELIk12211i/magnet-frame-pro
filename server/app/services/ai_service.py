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
# Default display-group (drawer) labels per provider-kind, used by the public
# catalogue when the admin has not set an explicit ui_category on a filter.
# ---------------------------------------------------------------------------
_DEFAULT_GROUP_LABELS = {
    "cartoon": "פילטרים מצויירים",
    "emotion": "חיוכים ורגשות",
}


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
    provider: Optional[Dict[str, Any]] = None,
) -> ProviderResult:
    """Generic multipart-REST adapter (AILabTools-compatible).

    Everything vendor-specific comes from DATA, never code: the endpoint host
    + auth-header name + API key from the ``ai_providers`` row (``provider``),
    and the per-filter sub-path / image-field / form params / result kind from
    the filter's catalogue row. A new vendor with the same request shape is a
    new provider row — no code change here.
    """
    import httpx  # lazy import — only needed in prod

    prow = provider or {}
    # Key: provider row wins; empty falls back to the legacy env var so an
    # existing AILabTools deploy keeps working before any admin edit. Never
    # logged, never returned to the client.
    api_key = (str(prow.get("api_key") or "").strip()
               or (_cfg.AILABTOOLS_API_KEY or ""))
    if not api_key:
        # Treated as "service unavailable" (definite failure → refund).
        return ProviderResult(
            ok=False, definite_failure=True,
            error_code="NO_API_KEY",
            error_detail="provider API key is not configured",
        )
    base_url = (str(prow.get("base_url") or "").strip()
                or _cfg.AILABTOOLS_BASE_URL)
    auth_header = (str(prow.get("auth_header") or "").strip()
                   or "ailabapi-api-key")
    provider_path = filt["provider_path"]
    result_kind = filt.get("result_kind", "image_url")
    route = _filter_routing(filt)
    url = f"{base_url.rstrip('/')}/{provider_path.lstrip('/')}"
    headers = {auth_header: api_key}
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


# ---------------------------------------------------------------------------
# OpenAI adapter (Images API — gpt-image-1 / dall-e-2 "edits"). Differs from
# the generic adapter in THREE ways, which is why it is its own function:
#   1. Auth is "Authorization: Bearer <key>" (the generic one sends the key
#      bare). We add the "Bearer " prefix unless the stored key already has it.
#   2. The result is data[0].b64_json (an ARRAY), or data[0].url for dall-e-2.
#   3. Errors come back as {"error": {"message", "type", "code"}}.
# The image is still a multipart file field, and the per-filter prompt / model
# / size / input_fidelity ride in the filter's ``form`` — so one OpenAI filter
# vs another is pure admin config, no code. Adding this vendor needed NO client
# change (the desktop app just sees another server-driven filter).
# ---------------------------------------------------------------------------
def _openai_apply(
    filt: Dict[str, Any], image_bytes: bytes, client_params: Dict[str, Any],
    provider: Optional[Dict[str, Any]] = None,
) -> ProviderResult:
    import httpx

    prow = provider or {}
    api_key = str(prow.get("api_key") or "").strip()
    if not api_key:
        return ProviderResult(
            ok=False, definite_failure=True, error_code="NO_API_KEY",
            error_detail="OpenAI API key is not configured")
    base_url = (str(prow.get("base_url") or "").strip()
                or "https://api.openai.com/v1")
    provider_path = str(filt.get("provider_path") or "").strip() or "images/edits"
    # OpenAI uses the "Authorization: Bearer" header. Fall back to that even if
    # the row still carries the AILabTools default header name (easy misconfig).
    auth_header = str(prow.get("auth_header") or "").strip()
    if not auth_header or auth_header.lower() == "ailabapi-api-key":
        auth_header = "Authorization"
    key_val = (api_key if api_key.lower().startswith("bearer ")
               else f"Bearer {api_key}")
    url = f"{base_url.rstrip('/')}/{provider_path.lstrip('/')}"
    headers = {auth_header: key_val}

    route = _filter_routing(filt)
    data = dict(route["form"])
    data.setdefault("model", "gpt-image-1")   # safe default; admin can override
    # OpenAI edits wants the image as a file part under ``image`` (the catalogue
    # image_field; defaults to "image"). Label it with its REAL type so OpenAI's
    # validation accepts JPEG/PNG/WebP as sent (app encodes JPEG).
    _mime = _sniff_image_mime(image_bytes)
    _ext = {"image/jpeg": "jpg", "image/png": "png",
            "image/webp": "webp"}.get(_mime, "png")
    files = {route["image_field"]:
             (f"image.{_ext}", image_bytes, _mime)}

    try:
        with httpx.Client(timeout=_cfg.AI_REQUEST_TIMEOUT_SECS) as client:
            resp = client.post(url, headers=headers, files=files, data=data)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise ProviderTimeout(str(exc)) from exc

    try:
        body = resp.json()
    except Exception:
        body = {}

    if resp.status_code == 200:
        arr = body.get("data") or []
        item = (arr[0] if isinstance(arr, list) and arr else {}) or {}
        b64 = item.get("b64_json")
        if isinstance(b64, str) and b64:
            try:
                out = base64.b64decode(b64)
            except Exception:
                out = None
            if out:
                return ProviderResult(
                    ok=True, definite_failure=False, image_bytes=out,
                    provider_request_id=str(body.get("created") or ""))
        img_url = item.get("url")
        if isinstance(img_url, str) and img_url.startswith("http"):
            try:
                with httpx.Client(timeout=_cfg.AI_REQUEST_TIMEOUT_SECS) as c2:
                    r2 = c2.get(img_url)
                if r2.status_code == 200:
                    return ProviderResult(
                        ok=True, definite_failure=False, image_bytes=r2.content,
                        provider_request_id=str(body.get("created") or ""))
            except Exception:
                pass
        return ProviderResult(
            ok=False, definite_failure=True,
            error_code="NO_IMAGE_IN_RESPONSE",
            error_detail="200 OK but no image in data[]")

    err = body.get("error") or {}
    return ProviderResult(
        ok=False, definite_failure=True,
        error_code=str(err.get("code") or err.get("type") or resp.status_code),
        error_detail=str(err.get("message") or f"http {resp.status_code}"))


def _get_provider_row(provider_key: str) -> Optional[Dict[str, Any]]:
    """Load an ai_providers row as a dict, or None. Used only server-side."""
    key = (provider_key or "").strip()
    if not key:
        return None
    try:
        with get_connection() as conn:
            r = conn.execute(
                "SELECT provider_key, display_name, base_url, auth_header, "
                "api_key, adapter, is_enabled FROM ai_providers "
                "WHERE provider_key = ?", (key,),
            ).fetchone()
    except Exception:
        return None
    return {k: r[k] for k in r.keys()} if r is not None else None


def _call_provider(
    filt: Dict[str, Any], image_bytes: bytes, params: Dict[str, Any]
) -> ProviderResult:
    # Global dev/staging kill-switch: AI_PROVIDER=mock never bills a real
    # vendor, regardless of the catalogue (keeps non-prod safe).
    if _cfg.AI_PROVIDER == "mock":
        return _mock_apply(image_bytes, params)
    # Data-driven dispatch: the filter names its provider; the provider row
    # carries host/auth/key/adapter. This is what lets an admin switch or add
    # a vendor with no code change and no client update.
    prow = _get_provider_row(filt.get("provider"))
    if prow is not None and not int(prow.get("is_enabled", 1) or 0):
        return ProviderResult(
            ok=False, definite_failure=True, error_code="PROVIDER_DISABLED",
            error_detail="the assigned provider is disabled",
        )
    return _run_adapter(filt, image_bytes, params, prow)


def _run_adapter(
    filt: Dict[str, Any], image_bytes: bytes, params: Dict[str, Any],
    prow: Optional[Dict[str, Any]],
) -> ProviderResult:
    """Pick the request-builder by the provider's ``adapter`` column. Adding a
    vendor family = one new adapter here; everything else (host/key/routing)
    stays data-driven, and the client never changes."""
    adapter = str((prow or {}).get("adapter") or "ailabtools").strip().lower()
    if adapter == "mock":
        return _mock_apply(image_bytes, params)
    if adapter == "openai":
        return _openai_apply(filt, image_bytes, params, provider=prow)
    # Default adapter: generic multipart-REST (AILabTools-compatible).
    return _ailabtools_apply(filt, image_bytes, params, provider=prow)


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
    """Public catalogue for the client — only safe fields (no internal cost).

    Includes ``preview_url`` + ``category`` (parsed from params_json) so the
    desktop app can render the same preview squares the admin configures.
    """
    import json
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT filter_key, display_name, sale_price_agorot, sort_order, "
            "params_json FROM ai_filters WHERE is_enabled = 1 "
            "ORDER BY sort_order, display_name"
        ).fetchall()
        # Admin-managed display categories: {key: (display_name, sort_order)}.
        cat_rows = conn.execute(
            "SELECT category_key, display_name, sort_order FROM ai_categories"
        ).fetchall()
    cat_map = {
        str(c["category_key"]): (str(c["display_name"] or ""),
                                 int(c["sort_order"] or 0))
        for c in cat_rows
    }
    tmp = []
    for r in rows:
        cfg = {}
        try:
            cfg = json.loads(r["params_json"] or "") or {}
            if not isinstance(cfg, dict):
                cfg = {}
        except Exception:
            cfg = {}
        kind = str(cfg.get("ailab_kind") or "")
        # Resolve the DISPLAY drawer. The filter stores a category KEY in
        # ui_category; unset → fall back to its provider-kind (whose key is
        # seeded into ai_categories). The LABEL + drawer ORDER come from the
        # ai_categories row, so an admin rename/reorder follows live. Kept
        # SEPARATE from ``category`` (= ailab_kind), which the client uses for
        # its anti-crop zoom heuristic, so renaming a drawer never alters
        # routing. A deleted/unknown key falls back to a friendly default.
        gkey = str(cfg.get("ui_category") or "").strip() or kind
        if gkey in cat_map:
            group_label, group_order = cat_map[gkey]
        else:
            group_label = _DEFAULT_GROUP_LABELS.get(gkey) \
                or _DEFAULT_GROUP_LABELS.get(kind) or gkey or "פילטרים"
            group_order = 9000
        tmp.append((
            group_order, int(r["sort_order"] or 0), {
                "filter_key": r["filter_key"],
                "display_name": r["display_name"],
                "sale_price_agorot": int(r["sale_price_agorot"]),
                "preview_url": str(cfg.get("preview_url") or ""),
                "category": kind,
                "group": group_label,
                # Whether this provider zooms/crops to the subject, so the
                # client pads before upload to avoid cropping heads. Explicit
                # admin flag wins; else the legacy cartoon heuristic, so a new
                # zooming provider needs no client update.
                "zooms": bool(cfg.get("zooms")) or (kind == "cartoon"),
            },
        ))
    # Order by category order first, then the filter's own sort_order, so the
    # client's first-appearance grouping yields drawers in the admin's order.
    tmp.sort(key=lambda t: (t[0], t[1]))
    return [item for _go, _fo, item in tmp]


# ---------------------------------------------------------------------------
# Admin FILTER LAB — run a provider call from the dashboard to verify a new
# provider / filter / routing END-TO-END, WITHOUT touching any customer wallet,
# license gate, or the ai_requests ledger. The vendor still bills its own
# credits for a real call, so this is an admin-only tool. ``use_mock`` runs the
# in-process mock (free, no network) to sanity-check wiring first.
# ---------------------------------------------------------------------------
def _sniff_image_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def admin_test_filter(
    image_bytes: bytes,
    *,
    filter_key: Optional[str] = None,
    provider_key: str = "",
    provider_path: str = "",
    image_field: str = "image",
    form: Optional[Dict[str, Any]] = None,
    result_kind: str = "image_url",
    use_mock: bool = False,
) -> Dict[str, Any]:
    """Run one provider call for admin verification. Returns a plain dict with
    the outcome + (on success) the result image as a base64 data URL ready to
    drop into an <img>. Never raises for provider errors — they come back in
    ``error_code`` / ``error_detail``. No wallet / ledger side effects."""
    import time as _time
    import json as _json

    if not image_bytes:
        return {"ok": False, "error_code": "NO_IMAGE",
                "error_detail": "לא נבחרה תמונת בדיקה.", "latency_ms": 0}

    # Build the filt dict: from a saved filter, or ad-hoc from the form fields.
    if filter_key:
        with get_connection() as conn:
            r = conn.execute(
                "SELECT filter_key, provider, provider_path, result_kind, "
                "params_json FROM ai_filters WHERE filter_key = ?",
                (filter_key,),
            ).fetchone()
        if r is None:
            return {"ok": False, "error_code": "NO_FILTER",
                    "error_detail": "הפילטר לא נמצא.", "latency_ms": 0}
        filt = {k: r[k] for k in r.keys()}
    else:
        params = {
            "image_field": (image_field or "image").strip() or "image",
            "form": form or {},
        }
        filt = {
            "filter_key": "__lab__",
            "provider": (provider_key or "").strip(),
            "provider_path": (provider_path or "").strip(),
            "result_kind": (result_kind or "image_url").strip() or "image_url",
            "params_json": _json.dumps(params, ensure_ascii=False),
        }

    prow = _get_provider_row(filt.get("provider"))
    # Resolve the URL for display (no secret in it).
    disp_url = ""
    if prow is not None:
        base = (str(prow.get("base_url") or "").strip()
                or _cfg.AILABTOOLS_BASE_URL)
        disp_url = f"{base.rstrip('/')}/{str(filt.get('provider_path') or '').lstrip('/')}"

    t0 = _time.time()
    try:
        if use_mock:
            res = _mock_apply(image_bytes, {})
        elif prow is not None and not int(prow.get("is_enabled", 1) or 0):
            res = ProviderResult(ok=False, definite_failure=True,
                                 error_code="PROVIDER_DISABLED",
                                 error_detail="הספק מושבת.")
        else:
            res = _run_adapter(filt, image_bytes, {}, prow)
    except ProviderTimeout as exc:
        return {"ok": False, "error_code": "TIMEOUT",
                "error_detail": f"אין תשובה מהספק: {exc}",
                "latency_ms": int((_time.time() - t0) * 1000),
                "provider_key": filt.get("provider") or "",
                "url": disp_url, "used_mock": bool(use_mock)}
    except Exception as exc:  # defensive — never 500 the admin page
        return {"ok": False, "error_code": "LAB_ERROR",
                "error_detail": str(exc),
                "latency_ms": int((_time.time() - t0) * 1000),
                "provider_key": filt.get("provider") or "",
                "url": disp_url, "used_mock": bool(use_mock)}

    latency = int((_time.time() - t0) * 1000)
    out: Dict[str, Any] = {
        "ok": bool(res.ok),
        "latency_ms": latency,
        "error_code": res.error_code or "",
        "error_detail": res.error_detail or "",
        "provider_request_id": res.provider_request_id or "",
        "provider_key": filt.get("provider") or "",
        "url": disp_url,
        "used_mock": bool(use_mock),
    }
    if res.ok and res.image_bytes:
        mime = _sniff_image_mime(res.image_bytes)
        b64 = base64.b64encode(res.image_bytes).decode("ascii")
        out["result_data_url"] = f"data:{mime};base64,{b64}"
        out["result_bytes_len"] = len(res.image_bytes)
    return out


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
