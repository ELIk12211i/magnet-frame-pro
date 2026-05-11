# -*- coding: utf-8 -*-
"""
server/app/routes/admin_api.py
------------------------------
Admin-only JSON API. Mounted at ``/admin/api/*``.

All endpoints require a valid admin session cookie. Responses use the
shape::

    {"ok": true,  "data": ...}
    {"ok": false, "error": "..."}

This API is intended to be consumed by the Jinja frontend (or by any
JavaScript code the frontend agent might add). It is **not** meant for
the desktop client — that one uses the ``/license/*`` endpoints.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..auth import require_admin_api
from ..database import get_connection
from ..schemas import GenerateRequest
from ..services import events_service as _evt
from ..services import license_service as svc
from ..services import stats_service as _stats


router = APIRouter()


# ---------------------------------------------------------------------------
# Request models (admin-specific)
# ---------------------------------------------------------------------------

class AdminDisableRequest(BaseModel):
    reason: Optional[str] = Field("", max_length=2000)


class AdminActorOnlyRequest(BaseModel):
    reason: Optional[str] = Field("", max_length=2000)


class AdminExtendRequest(BaseModel):
    days: int = Field(..., ge=1, le=3650)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client_ip(request: Request) -> str:
    try:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.client.host if request.client else ""
    except Exception:
        return ""


def _ok(data: Any) -> Dict[str, Any]:
    return {"ok": True, "data": data}


def _err(message: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=message)


def _row_to_dict(row) -> Dict[str, Any]:
    return {k: row[k] for k in row.keys()} if row is not None else {}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/stats")
def stats(user: str = Depends(require_admin_api)):
    return _ok(_stats.overview())


@router.get("/licenses")
def list_licenses(
    user: str = Depends(require_admin_api),
    q: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    type: Optional[str] = Query(None, alias="type"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=200),
):
    where: List[str] = []
    params: List[Any] = []

    if q:
        like = f"%{q.strip()}%"
        where.append(
            "(serial_key LIKE ? OR COALESCE(customer_name, '') LIKE ? "
            "OR COALESCE(customer_email, '') LIKE ? "
            "OR COALESCE(machine_id, '') LIKE ?)"
        )
        params.extend([like, like, like, like])
    if status:
        where.append("status = ?")
        params.append(status)
    if type:
        where.append("license_type = ?")
        params.append(type)

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    offset = (page - 1) * limit

    with get_connection() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM licenses{clause}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"""SELECT * FROM licenses{clause}
                 ORDER BY created_at DESC, serial_key DESC
                 LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()

    return _ok({
        "total": total,
        "page": page,
        "limit": limit,
        "items": [_row_to_dict(r) for r in rows],
    })


@router.get("/licenses/{serial_key}")
def license_detail(serial_key: str, user: str = Depends(require_admin_api)):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM licenses WHERE serial_key = ?", (serial_key,)
        ).fetchone()
    if row is None:
        raise _err("license not found", status=404)

    activations = _evt.list_activations_for_serial(serial_key)
    events = _evt.recent_events_for_serial(serial_key, n=50)
    return _ok({
        "license": _row_to_dict(row),
        "activations": activations,
        "events": events,
    })


@router.post("/licenses")
def create_license(
    req: GenerateRequest,
    request: Request,
    user: str = Depends(require_admin_api),
):
    try:
        data = svc.generate(
            license_type=req.license_type,
            days=req.days,
            customer_name=req.customer_name or "",
            customer_email=req.customer_email or "",
            notes=req.notes or "",
            actor=user,
        )
        return _ok(data)
    except svc.LicenseError as e:
        raise _err(str(e), status=e.status)


@router.post("/licenses/{serial_key}/disable")
def disable_license(
    serial_key: str,
    request: Request,
    body: AdminDisableRequest = AdminDisableRequest(),
    user: str = Depends(require_admin_api),
):
    try:
        data = svc.disable_license(
            serial_key,
            reason=body.reason or "",
            actor=user,
            ip=_client_ip(request),
        )
        return _ok(data)
    except svc.LicenseError as e:
        raise _err(str(e), status=e.status)


@router.post("/licenses/{serial_key}/enable")
def enable_license(
    serial_key: str,
    request: Request,
    user: str = Depends(require_admin_api),
):
    try:
        data = svc.enable_license(
            serial_key,
            actor=user,
            ip=_client_ip(request),
        )
        return _ok(data)
    except svc.LicenseError as e:
        raise _err(str(e), status=e.status)


@router.post("/licenses/{serial_key}/reset")
def reset_license(
    serial_key: str,
    request: Request,
    user: str = Depends(require_admin_api),
):
    try:
        data = svc.reset(
            serial_key,
            ip=_client_ip(request),
            actor=user,
        )
        return _ok(data)
    except svc.LicenseError as e:
        raise _err(str(e), status=e.status)


@router.post("/licenses/{serial_key}/extend")
def extend_license(
    serial_key: str,
    body: AdminExtendRequest,
    request: Request,
    user: str = Depends(require_admin_api),
):
    try:
        data = svc.extend_license(
            serial_key,
            days=body.days,
            actor=user,
            ip=_client_ip(request),
        )
        return _ok(data)
    except svc.LicenseError as e:
        raise _err(str(e), status=e.status)


@router.get("/activations")
def list_activations(
    user: str = Depends(require_admin_api),
    serial_key: Optional[str] = None,
    machine_id: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
):
    return _ok(_evt.list_activations(
        filters={"serial_key": serial_key, "machine_id": machine_id},
        page=page,
        limit=limit,
    ))


@router.get("/events")
def list_events(
    user: str = Depends(require_admin_api),
    serial_key: Optional[str] = None,
    machine_id: Optional[str] = None,
    event_type: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
):
    return _ok(_evt.list_events(
        filters={
            "serial_key": serial_key,
            "machine_id": machine_id,
            "event_type": event_type,
            "since": since,
            "until": until,
        },
        page=page,
        limit=limit,
    ))
