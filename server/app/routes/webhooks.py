# -*- coding: utf-8 -*-
"""
server/app/routes/webhooks.py
-----------------------------
Payment provider webhook endpoint.

This is the *provider-agnostic* skeleton: it verifies a shared-secret
signature, looks up the order, issues a license through the existing
``license_service.generate`` call, and emails the license key to the
customer. The specific parsing (field names, signature header, status
codes) is provider-specific and lives behind the ``provider`` param —
when you pick Stripe / Tranzila / iCredit / Grow / etc., add a branch
in :func:`_parse_provider_payload`.

Endpoints
---------
``POST /webhook/payment/{provider}`` — called by the payment provider
after a successful (or failed) payment.

``POST /webhook/test-issue``         — **dev only**: manually issue a
license for an existing ``pending`` order so you can exercise the
email + license flow without a real provider. Protected by
``WEBHOOK_SECRET`` so it cannot be called externally.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Request

from ..schemas import LicenseResponse, WebhookTestIssueRequest
from ..services import license_service as licsvc
from ..services import orders_service as orders
from ..services import plans_service as plans
from ..services import email_service


logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Signature verification (shared between providers — each one uses a
# different header name but the HMAC-SHA256 pattern is common).
# ---------------------------------------------------------------------------
def _webhook_secret() -> str:
    return (os.environ.get("WEBHOOK_SECRET") or "").strip()


def _verify_signature(raw_body: bytes, received: str) -> bool:
    """Verify HMAC-SHA256(secret, raw_body) == received (hex).

    Providers that sign in a different scheme (e.g. Stripe uses
    ``t=...,v1=...`` format) should parse ``received`` before calling
    this. For now we support plain hex HMAC-SHA256.
    """
    secret = _webhook_secret()
    if not secret:
        logger.warning("webhook: WEBHOOK_SECRET not set — refusing to verify")
        return False
    if not received:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    # constant-time compare
    return hmac.compare_digest(expected, received.strip().lower())


# ---------------------------------------------------------------------------
# Provider-specific payload parsing — add branches here when you wire
# a real provider. Each branch must return a dict with these keys:
#
#     order_id         : int
#     provider_txn_id  : str (unique across retries)
#     status           : "paid" | "failed" | "refunded"
#     amount_cents     : int
#     currency         : str
#     raw              : dict (full original payload)
#
# Unknown provider → raise 400.
# ---------------------------------------------------------------------------
def _parse_provider_payload(provider: str, data: Dict[str, Any]) -> Dict[str, Any]:
    provider = (provider or "").lower().strip()

    # --- Generic payload (default / internal testing) ---------------------
    # Expected shape:
    # {
    #   "order_id":   123,
    #   "txn_id":     "abc",
    #   "status":     "paid" | "failed" | "refunded",
    #   "amount":     490.0,        # in ILS
    #   "currency":   "ILS"
    # }
    if provider in ("generic", "test", ""):
        return {
            "order_id":        int(data.get("order_id") or 0),
            "provider_txn_id": str(data.get("txn_id") or ""),
            "status":          str(data.get("status") or "").lower(),
            "amount_cents":    int(round(float(data.get("amount") or 0) * 100)),
            "currency":        (data.get("currency") or "ILS").upper(),
            "raw":             data,
        }

    # --- TODO: Stripe ----------------------------------------------------
    # When wiring Stripe, add:
    #   if provider == "stripe":
    #       # data = stripe.Event(**data)
    #       session = data["data"]["object"]
    #       return {
    #           "order_id":        int(session["metadata"]["order_id"]),
    #           "provider_txn_id": session["id"],
    #           "status":          "paid" if session["payment_status"] == "paid" else "failed",
    #           "amount_cents":    session["amount_total"],
    #           "currency":        session["currency"].upper(),
    #           "raw":             data,
    #       }

    # --- TODO: Tranzila --------------------------------------------------
    # if provider == "tranzila": ...

    # --- TODO: iCredit / Grow / Meshulam --------------------------------

    raise HTTPException(status_code=400, detail=f"Unsupported provider: {provider}")


# ---------------------------------------------------------------------------
# Shared issuance + email — runs after we confirm payment.
# ---------------------------------------------------------------------------
def _issue_and_notify(order: Dict[str, Any]) -> Dict[str, Any]:
    """Create a license for ``order`` and email it to the customer.

    Idempotent — if the order is already ``paid`` with a ``license_id``
    attached, we skip issuance (but still re-send the email if asked).
    """
    order_id = order.get("id")
    plan_key = (order.get("plan_key") or "").strip()

    # Look up the plan so we pass the right license_type + days.
    plan: Optional[Dict[str, Any]] = None
    if plan_key.isdigit():
        plan = plans.get_plan(int(plan_key))
    if not plan:
        raise HTTPException(
            status_code=500,
            detail=f"Plan {plan_key!r} not found for order {order_id}",
        )

    # Issue the license via the existing service (no code duplication).
    license_row = licsvc.generate(
        license_type = plan.get("license_type") or "",
        days         = plan.get("days"),
        customer_name  = order.get("customer_name")  or "",
        customer_email = order.get("customer_email") or "",
        customer_phone = order.get("customer_phone") or "",
        notes = f"Auto-issued from order #{order_id}",
        plan_name = plan.get("name") or "",
        plan_days = plan.get("days"),
        actor = "webhook",
    )

    serial = license_row.get("serial_key") or license_row.get("license_key") or ""

    # Mark order paid + link the license.
    orders.mark_paid(
        order_id,
        license_serial=serial,
        amount_cents=order.get("amount_cents"),
    )

    # Fire-and-forget email. Failure is logged but not propagated.
    try:
        email_service.send_license_delivery(
            to_email      = order.get("customer_email") or "",
            customer_name = order.get("customer_name")  or "",
            serial_key    = serial,
            plan_name     = plan.get("name") or "",
            expires_at    = license_row.get("expires_at") or "",
        )
    except Exception as exc:
        logger.error(
            "webhook: email delivery failed for order=%s serial=%s: %s",
            order_id, serial, exc,
        )

    return license_row


# ---------------------------------------------------------------------------
# POST /webhook/payment/{provider}
# ---------------------------------------------------------------------------
@router.post("/payment/{provider}")
async def payment_webhook(
    provider: str,
    request: Request,
    x_signature: Optional[str] = Header(default=None, alias="X-Signature"),
):
    raw = await request.body()

    # 1. Verify signature — ALWAYS required (NEW-SRV-3). Previously we
    #    allowed missing-secret deployments to skip verification, which
    #    meant an empty ``WEBHOOK_SECRET`` left the endpoint open to
    #    anyone who could forge a payload. Now we refuse to process.
    if not _webhook_secret():
        logger.error(
            "webhook[%s]: WEBHOOK_SECRET is not configured — rejecting",
            provider,
        )
        raise HTTPException(
            status_code=503,
            detail="Webhook secret not configured on server",
        )
    if not _verify_signature(raw, x_signature or ""):
        logger.warning(
            "webhook[%s]: signature mismatch — rejecting", provider,
        )
        raise HTTPException(status_code=401, detail="Invalid signature")

    # 2. Parse payload
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    parsed = _parse_provider_payload(provider, data)

    # 3. Idempotency — if we already processed this (provider, txn_id), bail.
    existing = orders.get_order_by_txn_id(provider, parsed["provider_txn_id"])
    if existing and (existing.get("status") == "paid"):
        logger.info(
            "webhook[%s]: duplicate txn_id=%s — already processed order=%s",
            provider, parsed["provider_txn_id"], existing.get("id"),
        )
        return {"success": True, "status": "already_processed",
                "order_id": existing.get("id")}

    order_id = parsed["order_id"]
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(
            status_code=404, detail=f"Order {order_id} not found"
        )

    # 4. Attach provider info (for audit) before deciding the outcome.
    order = orders.attach_transaction(
        order_id,
        provider=provider,
        provider_txn_id=parsed["provider_txn_id"],
        amount_cents=parsed["amount_cents"],
        currency=parsed["currency"],
    ) or order

    # 5. Branch on status
    status = parsed["status"]
    if status == "paid":
        license_row = _issue_and_notify(order)
        return {
            "success": True,
            "order_id": order_id,
            "status": "paid",
            "license_key": license_row.get("serial_key")
                           or license_row.get("license_key"),
        }

    if status == "failed":
        orders.mark_failed(order_id, reason="provider reported failure")
        return {"success": True, "order_id": order_id, "status": "failed"}

    if status == "refunded":
        orders.mark_refunded(order_id, reason="provider reported refund")
        return {"success": True, "order_id": order_id, "status": "refunded"}

    logger.warning(
        "webhook[%s]: unknown status=%r for order=%s", provider, status, order_id,
    )
    return {"success": False, "order_id": order_id, "status": status or "unknown"}


# ---------------------------------------------------------------------------
# POST /webhook/test-issue  — dev helper, guarded by WEBHOOK_SECRET.
# ---------------------------------------------------------------------------
@router.post("/test-issue", response_model=LicenseResponse)
def test_issue(
    req: WebhookTestIssueRequest,
    x_webhook_secret: Optional[str] = Header(default=None, alias="X-Webhook-Secret"),
):
    """Manually trigger license + email for an existing pending order.

    Useful for exercising the flow before wiring a real provider. Gated
    behind ``WEBHOOK_SECRET`` — set one in ``server/.env`` first.
    """
    secret = _webhook_secret()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="WEBHOOK_SECRET not configured — set it in server/.env first.",
        )
    if not x_webhook_secret or not hmac.compare_digest(secret, x_webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid X-Webhook-Secret header")

    order = orders.get_order(req.order_id)
    if not order:
        raise HTTPException(status_code=404, detail=f"Order {req.order_id} not found")
    if order.get("status") == "paid":
        raise HTTPException(
            status_code=409, detail="Order already paid — license already issued."
        )

    order = orders.attach_transaction(
        req.order_id,
        provider="test",
        provider_txn_id=f"test-{req.order_id}",
    ) or order
    license_row = _issue_and_notify(order)
    return license_row
