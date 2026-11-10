# -*- coding: utf-8 -*-
"""
server/app/routes/ai.py
-----------------------
Public ``/ai/*`` routes used by the desktop client for cloud AI filters.

Auth model mirrors ``/license/*``: no admin session — the caller is
identified by ``serial_key`` + ``machine_uuid`` (HWID) and every request is
authorized server-side (active license, bound machine) before any credit is
touched. The AILabTools key lives only on the server; the client never sees
it.
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from .. import config as _cfg
from ..services import ai_service as _ai
from ..services import credits_service as _credits
from ..services import license_service as _lic


router = APIRouter()


def _client_ip(request: Request) -> str:
    try:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.client.host if request.client else ""
    except Exception:
        return ""


@router.get("/filters")
def filters():
    """Catalogue of enabled filters (name + sale price). No secrets."""
    return {"success": True, "filters": _ai.list_enabled_filters()}


@router.post("/balance")
def balance(
    serial_key: str = Form(...),
    machine_uuid: str = Form(""),
    machine_id: str = Form(""),
):
    """Return the wallet balance for a license + whether AI is allowed now."""
    auth = _lic.authorize_for_ai(serial_key, machine_uuid=machine_uuid,
                                 mid=machine_id)
    try:
        info = _credits.get_balance_for_license(serial_key)
        bal = info["balance_agorot"]
    except _credits.CreditsError:
        bal = 0
    return {
        "success": True,
        "license_ok": bool(auth["ok"]),
        "license_status": auth["status"],
        "balance_agorot": bal,
    }


@router.post("/apply")
async def apply(
    request: Request,
    serial_key: str = Form(...),
    machine_uuid: str = Form(""),
    machine_id: str = Form(""),
    filter_key: str = Form(...),
    idempotency_key: str = Form(...),
    params: Optional[str] = Form(None),
    image: UploadFile = File(...),
):
    """Run one cloud filter. Multipart: fields + the image file."""
    # Parse optional JSON params leniently.
    parsed_params = {}
    if params:
        try:
            parsed_params = json.loads(params)
            if not isinstance(parsed_params, dict):
                parsed_params = {}
        except Exception:
            parsed_params = {}

    # Read the upload with a hard cap so a huge body can't exhaust memory.
    max_bytes = max(1, _cfg.AI_MAX_IMAGE_MB) * 1024 * 1024
    image_bytes = await image.read(max_bytes + 1)
    if len(image_bytes) > max_bytes:
        return JSONResponse(
            {"success": False, "error_code": "BAD_IMAGE",
             "message": _ai.MSG_BAD_IMAGE}, status_code=413)

    result = _ai.run_filter(
        serial=serial_key,
        machine_uuid=machine_uuid,
        mid=machine_id,
        filter_key=filter_key,
        image_bytes=image_bytes,
        idempotency_key=idempotency_key,
        params=parsed_params,
        ip=_client_ip(request),
    )
    status = int(result.pop("http_status", 200))
    return JSONResponse(result, status_code=status)
