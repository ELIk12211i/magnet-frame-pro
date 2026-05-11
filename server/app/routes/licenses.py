# -*- coding: utf-8 -*-
"""
server/app/routes/licenses.py
-----------------------------
Public ``/license/*`` HTTP routes — the entire contract the desktop
client and any admin UI need to talk to the license server.

NOTE ON AUTH
------------
These endpoints are intentionally **not** authenticated: the desktop
client calls them directly from customer machines. In production you
should firewall (or reverse-proxy with auth) the write endpoints
``/license/generate``, ``/license/disable`` and ``/license/enable``.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth import require_admin_api
from ..schemas import (
    ActivateRequest,
    DisableRequest,
    EnableRequest,
    GenerateRequest,
    LicenseInfoRequest,
    LicenseResponse,
    ResetRequest,
    TrialRequest,
    ValidateRequest,
)
from ..services import events_service as _evt
from ..services import license_service as svc


# Fields on a license row that contain personally-identifiable info.
# Stripped from public ``/license/info`` / ``/license/license-info``
# responses — callers without an admin session see only operational
# fields (status, expiry, plan) so the desktop client can still display
# "your license expires on X", without leaking other customers' details
# to anyone who guesses a serial.
_PII_FIELDS = (
    "customer_name",
    "customer_first_name",
    "customer_last_name",
    "customer_email",
    "customer_phone",
    "hardware_id",
    "machine_uuid",
    "hostname",
    "public_ip",
    "client_public_ip",
    "notes",
)


def _strip_pii(response: dict) -> dict:
    """Return a shallow copy of ``response`` without PII fields."""
    if not isinstance(response, dict):
        return response
    out = dict(response)
    for k in _PII_FIELDS:
        out.pop(k, None)
    return out


def _is_admin_request(request: Request) -> bool:
    """True if the caller has a valid admin session cookie."""
    try:
        require_admin_api(request)
        return True
    except Exception:
        return False


router = APIRouter()


def _convert(err: svc.LicenseError) -> HTTPException:
    return HTTPException(status_code=err.status, detail=str(err))


def _client_ip(request: Request) -> str:
    try:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.client.host if request.client else ""
    except Exception:
        return ""


def _user_agent(request: Request) -> str:
    try:
        return (request.headers.get("user-agent") or "")[:512]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Client-driven flow (used by the desktop app)
# ---------------------------------------------------------------------------

@router.post("/activate", response_model=LicenseResponse)
def activate(req: ActivateRequest, request: Request):
    try:
        return svc.activate(
            req.serial_key, req.machine_id,
            ip=_client_ip(request), user_agent=_user_agent(request),
            machine_uuid=(req.machine_uuid or "").strip(),
            hostname=(req.hostname or "").strip(),
            public_ip=(req.public_ip or "").strip(),
        )
    except svc.LicenseError as err:
        raise _convert(err)


@router.post("/validate", response_model=LicenseResponse)
def validate(req: ValidateRequest, request: Request):
    try:
        return svc.validate(
            req.serial_key, req.machine_id,
            ip=_client_ip(request), user_agent=_user_agent(request),
            machine_uuid=(req.machine_uuid or "").strip(),
            hostname=(req.hostname or "").strip(),
            public_ip=(req.public_ip or "").strip(),
        )
    except svc.LicenseError as err:
        raise _convert(err)


@router.post("/start-trial", response_model=LicenseResponse)
def start_trial(req: TrialRequest, request: Request):
    try:
        return svc.start_trial(
            req.machine_id,
            ip=_client_ip(request), user_agent=_user_agent(request),
            name=(req.name or ""), phone=(req.phone or ""),
        )
    except svc.LicenseError as err:
        raise _convert(err)


@router.post("/reset", response_model=LicenseResponse,
             dependencies=[Depends(require_admin_api)])
def reset(req: ResetRequest, request: Request):
    """Release a license's machine-binding. **Admin-only** — resetting a
    customer's binding is privileged (NEW-SRV-1).
    """
    try:
        return svc.reset(
            req.serial_key, ip=_client_ip(request),
            actor=req.actor or "",
        )
    except svc.LicenseError as err:
        raise _convert(err)


@router.get("/info/{serial_key}", response_model=LicenseResponse)
def info(serial_key: str, request: Request):
    """Public read of a license — PII fields are stripped for anonymous
    callers (NEW-SRV-2). An authenticated admin request returns the full
    payload including customer contact details.
    """
    try:
        data = svc.get_info(serial_key)
    except svc.LicenseError as err:
        raise _convert(err)
    if _is_admin_request(request):
        return data
    return _strip_pii(data)


@router.get("/whoami")
def whoami(request: Request):
    """Report back the client's IP as observed by the server.

    Used by the desktop client's settings screen to show the same
    "public IP" the admin dashboard sees on the activations row —
    regardless of whether the client is talking to a local server
    (127.0.0.1) or a remote one (the real WAN IP).
    """
    # ``X-Forwarded-For`` honours front-end proxies / load balancers.
    fwd = (request.headers.get("x-forwarded-for") or "").strip()
    if fwd:
        # The header can contain multiple IPs — the first is the origin.
        ip = fwd.split(",")[0].strip()
    else:
        ip = (request.client.host if request.client else "") or ""
    return {"ip": ip}


@router.post("/license-info", response_model=LicenseResponse)
def license_info(req: LicenseInfoRequest, request: Request):
    """POST alternative to ``GET /license/info/{key}`` — same PII policy."""
    try:
        data = svc.get_info(req.serial_key)
    except svc.LicenseError as err:
        raise _convert(err)
    if _is_admin_request(request):
        return data
    return _strip_pii(data)


# ---------------------------------------------------------------------------
# Client telemetry — write-only, used by the desktop client to announce
# offline-detected states so the admin dashboard stays accurate even when
# the app cannot reach the server to run a real ``/validate``.
# ---------------------------------------------------------------------------

class ReportStatusRequest(BaseModel):
    """Body for ``POST /license/report-status``."""
    serial_key: Optional[str] = Field(None, max_length=64)
    machine_id: Optional[str] = Field(None, max_length=128)
    event_type: str = Field(..., max_length=64,
                            description=(
                                "license_validation_skipped_no_internet | "
                                "license_validation_required | "
                                "license_switched_to_demo_due_to_validation_timeout"
                            ))
    message:    Optional[str] = Field("", max_length=500)


_CLIENT_REPORTABLE_EVENTS = {
    _evt.EVENT_LICENSE_VALIDATION_SKIPPED_NO_INTERNET,
    _evt.EVENT_LICENSE_VALIDATION_REQUIRED,
    _evt.EVENT_LICENSE_SWITCHED_TO_DEMO_TIMEOUT,
}


@router.post("/report-status")
def report_status(req: ReportStatusRequest, request: Request):
    """Log a client-side validation/offline event in the server's audit log.

    The endpoint never fails loudly — even if the event type is not in
    the allow-list, we respond 200 so the client does not blow up on a
    version mismatch.
    """
    try:
        ev_type = (req.event_type or "").strip()
        if ev_type not in _CLIENT_REPORTABLE_EVENTS:
            # Still log it (as an unknown client-side hint) — keeps forward
            # compatibility without polluting the canonical enum.
            ev_type = ev_type or "license_client_status"
        _evt.log_event(
            (req.serial_key or None),
            (req.machine_id or None),
            ev_type,
            message=(req.message or "")[:500],
            ip=_client_ip(request),
            actor="client",
        )
    except Exception:
        pass
    return {"success": True}


# ---------------------------------------------------------------------------
# Admin-driven endpoints (public by design; firewall in production).
# ---------------------------------------------------------------------------

@router.post("/generate", response_model=LicenseResponse)
def generate(req: GenerateRequest, request: Request):
    try:
        return svc.generate(
            license_type=req.license_type,
            days=req.days,
            customer_name=req.customer_name or "",
            customer_email=req.customer_email or "",
            notes=req.notes or "",
        )
    except svc.LicenseError as err:
        raise _convert(err)


@router.post("/disable", response_model=LicenseResponse)
def disable(req: DisableRequest, request: Request):
    try:
        return svc.disable_license(
            req.serial_key,
            reason=req.reason or "",
            actor=req.actor or "",
            ip=_client_ip(request),
        )
    except svc.LicenseError as err:
        raise _convert(err)


@router.post("/enable", response_model=LicenseResponse)
def enable(req: EnableRequest, request: Request):
    try:
        return svc.enable_license(
            req.serial_key,
            actor=req.actor or "",
            ip=_client_ip(request),
        )
    except svc.LicenseError as err:
        raise _convert(err)
