# -*- coding: utf-8 -*-
"""
server/app/routes/checkout.py
-----------------------------
Public endpoints for the purchase website (``site/...``).

Two endpoints:

* ``POST /checkout/create`` — called when the customer submits the
  checkout form on the website. Creates a ``pending`` order and
  returns ``order_id`` + metadata. The website then redirects the
  customer to the payment provider (the redirect URL is computed in
  this handler once a provider is chosen).

* ``GET  /checkout/plans`` — lightweight public list of active plans
  for the website to render prices (no admin auth needed, read-only).

The actual payment + license issuance happens in
:mod:`app.routes.webhooks` after the provider confirms the payment.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from ..schemas import CheckoutCreateRequest, CheckoutCreateResponse, PublicPlan
from ..services import email_service
from ..services import license_service as licsvc
from ..services import orders_service as orders
from ..services import payments_service as payments
from ..services import plans_service as plans


logger = logging.getLogger(__name__)

router = APIRouter()


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _client_ip(request: Request) -> str:
    try:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.client.host if request.client else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# GET /checkout/plans
# ---------------------------------------------------------------------------
@router.get("/plans", response_model=list[PublicPlan])
def public_plans():
    """Return the list of plans the website should render.

    We expose only the subset of fields the website needs (id, name,
    days, license_type, price_ils, sort_order) — never admin-only
    fields.
    """
    try:
        rows = plans.list_plans(include_inactive=False)
    except Exception as exc:
        logger.error("checkout/plans: failed to list plans: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to load plans")

    out: list[dict] = []
    for r in rows:
        out.append({
            "id":           r.get("id"),
            "name":         r.get("name") or "",
            "days":         r.get("days") or 0,
            "license_type": r.get("license_type") or "",
            "price_ils":    float(r.get("price_ils") or 0),
            "sort_order":   int(r.get("sort_order") or 0),
        })
    return out


# ---------------------------------------------------------------------------
# GET /checkout/config
# ---------------------------------------------------------------------------
@router.get("/config")
def public_config():
    """Tell the website which checkout path to use.

    ``payment_configured=True`` → the site should POST to ``/checkout/create``
    and redirect the customer to the returned ``next_step`` (the provider's
    secure hosted page). ``False`` → no provider wired up yet; the site uses
    the dev ``/checkout/complete-dev`` issue-without-charge fallback.
    """
    return {
        "payment_configured": payments.is_configured(),
        "provider": payments.provider_name(),
    }


# ---------------------------------------------------------------------------
# POST /checkout/create
# ---------------------------------------------------------------------------
@router.post("/create", response_model=CheckoutCreateResponse)
def create_checkout(req: CheckoutCreateRequest, request: Request):
    """Create a pending order from a website checkout submission.

    Does NOT charge the customer — that happens at the payment
    provider. The returned ``order_id`` is used by:

    1. The website to key the "thank you / waiting for confirmation"
       page.
    2. The webhook handler to correlate the incoming payment
       notification with this order (via ``metadata.order_id`` passed
       to the payment provider).
    """
    name  = (req.customer_name  or "").strip()
    email = (req.customer_email or "").strip().lower()
    phone = (req.customer_phone or "").strip()
    plan_id = int(req.plan_id or 0)

    if not name:
        raise HTTPException(status_code=400, detail="יש לספק שם לקוח.")
    if not email or not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="כתובת אימייל לא תקינה.")
    if plan_id <= 0:
        raise HTTPException(status_code=400, detail="יש לבחור תוכנית רישוי.")

    # Resolve the plan so we can snapshot its price + name on the order.
    plan = plans.get_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="התוכנית שנבחרה לא נמצאה.")
    if not plan.get("is_active"):
        raise HTTPException(status_code=400, detail="התוכנית שנבחרה אינה פעילה.")

    price_ils = float(plan.get("price_ils") or 0)
    amount_cents = int(round(price_ils * 100))

    row = orders.create_pending_order(
        plan_key=str(plan_id),
        customer_name=name,
        customer_email=email,
        customer_phone=phone,
        amount_cents=amount_cents,
        currency="ILS",
        provider="pending",  # real provider is attached after redirect
        raw_payload={
            "source":    "website",
            "client_ip": _client_ip(request),
            "user_agent": (request.headers.get("user-agent") or "")[:512],
            "plan_snapshot": {
                "name":         plan.get("name"),
                "days":         plan.get("days"),
                "license_type": plan.get("license_type"),
                "price_ils":    price_ils,
            },
        },
    )

    order_id = row.get("id")
    # NEW-SRV-6: avoid writing the raw email to the server log. The
    # ``orders`` row carries the full address; logs only need a
    # privacy-preserving hint (first char + domain) for triage.
    def _mask_email(addr: str) -> str:
        if not addr or "@" not in addr:
            return "***"
        user, dom = addr.split("@", 1)
        return (user[:1] + "***@" + dom) if user else ("***@" + dom)

    logger.info(
        "checkout/create: order=%s plan=%s email=%s amount=%s",
        order_id, plan_id, _mask_email(email), amount_cents,
    )

    # Ask the payment layer for a hosted-payment-page URL. The customer
    # enters their card on the PROVIDER's secure page — never here. If no
    # provider is wired up yet (dev mode) we return an empty ``next_step``
    # and the website falls back to the dev issue-without-charge flow.
    origin = str(request.base_url).rstrip("/")
    return_url = f"{origin}/site/?status=success&order={order_id}"
    cancel_url = f"{origin}/site/?status=cancel&order={order_id}"

    payment_url = ""
    provider = payments.provider_name()
    configured = payments.is_configured()
    if configured:
        try:
            session = payments.create_payment_session(
                order=row,
                amount_cents=amount_cents,
                currency="ILS",
                return_url=return_url,
                cancel_url=cancel_url,
                customer_name=name,
                customer_email=email,
                customer_phone=phone,
                description=str(plan.get("name") or "Magnet Frame PRO"),
            )
            payment_url = str(session.get("payment_url") or "")
        except payments.PaymentNotConfigured as exc:
            logger.warning("checkout/create: payment provider not ready: %s", exc)
            configured = False
        except Exception as exc:
            logger.error("checkout/create: payment session failed: %s", exc)
            configured = False

    return {
        "order_id": order_id,
        "status":   row.get("status") or "pending",
        "amount_cents": amount_cents,
        "currency": "ILS",
        "next_step": payment_url,           # redirect target (empty in dev mode)
        "provider":  provider,
        "payment_configured": configured,
        "message":  ("הזמנה נוצרה - ממשיכים לתשלום מאובטח."
                     if payment_url else "הזמנה נוצרה."),
    }


# ---------------------------------------------------------------------------
# POST /checkout/complete-dev
# ---------------------------------------------------------------------------
@router.post("/complete-dev")
def complete_checkout_dev(req: CheckoutCreateRequest, request: Request):
    """Create-and-fulfil a checkout in ONE call — dev / no-payment mode.

    The production flow splits the purchase across three round-trips:
    ``/checkout/create`` (records the lead) → payment provider → webhook
    → license + email. While the payment provider isn't wired up yet
    the website needs a single endpoint that:

      1. Validates the customer details + chosen plan.
      2. Records an ``orders`` row (so the admin dashboard's "הזמנות"
         page lists the lead).
      3. Issues a real license via ``license_service.generate`` and
         marks the order as paid.
      4. Sends the license-delivery email (fire-and-forget — SMTP
         failures don't roll back the license).
      5. Returns the license key so the website can show it on the
         "thank you" page.

    DEV ONLY — must be disabled or replaced with the real webhook
    integration before shipping to production. The endpoint has no
    payment verification and will happily issue a paid license to any
    caller who posts a valid plan_id.
    """
    name  = (req.customer_name  or "").strip()
    email = (req.customer_email or "").strip().lower()
    phone = (req.customer_phone or "").strip()
    id_number     = (req.customer_id_number or "").strip()
    business_name = (req.business_name      or "").strip()
    business_id   = (req.business_id        or "").strip()
    plan_id = int(req.plan_id or 0)

    if not name:
        raise HTTPException(status_code=400, detail="יש לספק שם לקוח.")
    if not email or not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="כתובת אימייל לא תקינה.")
    if plan_id <= 0:
        raise HTTPException(status_code=400, detail="יש לבחור תוכנית רישוי.")

    plan = plans.get_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="התוכנית שנבחרה לא נמצאה.")
    if not plan.get("is_active"):
        raise HTTPException(status_code=400, detail="התוכנית שנבחרה אינה פעילה.")

    price_ils = float(plan.get("price_ils") or 0)
    amount_cents = int(round(price_ils * 100))

    # Build a structured ``customer_extra`` blob that the admin "לקוחות"
    # tab decodes from orders.raw_payload. Keep blanks out so older
    # rows without these fields stay clean.
    customer_extra: dict = {}
    if id_number:     customer_extra["id_number"]     = id_number
    if business_name: customer_extra["business_name"] = business_name
    if business_id:   customer_extra["business_id"]   = business_id

    # 1) Pending order so the admin "הזמנות" page captures the lead
    # even if license issuance below explodes.
    order_row = orders.create_pending_order(
        plan_key=str(plan_id),
        customer_name=name,
        customer_email=email,
        customer_phone=phone,
        amount_cents=amount_cents,
        currency="ILS",
        provider="dev",
        raw_payload={
            "source":     "website-dev",
            "client_ip":  _client_ip(request),
            "user_agent": (request.headers.get("user-agent") or "")[:512],
            "plan_snapshot": {
                "name":         plan.get("name"),
                "days":         plan.get("days"),
                "license_type": plan.get("license_type"),
                "price_ils":    price_ils,
            },
            "customer_extra": customer_extra,
        },
    )
    order_id = order_row.get("id")

    # Compose a human-readable notes string for the license so the
    # admin License page sees the same details without parsing JSON.
    notes_lines = [f"Auto-issued from website checkout (order #{order_id})"]
    if id_number:     notes_lines.append(f"id_number: {id_number}")
    if business_name: notes_lines.append(f"business: {business_name}")
    if business_id:   notes_lines.append(f"tax_id: {business_id}")
    if (req.notes or "").strip():
        notes_lines.append(req.notes.strip())
    license_notes = " | ".join(notes_lines)

    # 2) License issuance via the existing service — same code path the
    # admin "Generate license" button uses, so the audit log + serial
    # format stay consistent.
    try:
        license_row = licsvc.generate(
            license_type   = plan.get("license_type") or "",
            days           = plan.get("days"),
            customer_name  = name,
            customer_email = email,
            customer_phone = phone,
            notes          = license_notes,
            plan_name      = plan.get("name") or "",
            plan_days      = plan.get("days"),
            actor          = "website-checkout",
        )
    except Exception as exc:
        logger.error("checkout/complete-dev: license generate failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="יצירת הרישיון נכשלה — נסה שוב או צור קשר עם התמיכה.",
        )

    serial = license_row.get("serial_key") or license_row.get("license_key") or ""

    # 3) Mark order paid + link the license so the admin can correlate
    # the row with the issued key.
    try:
        orders.mark_paid(
            order_id,
            license_serial=serial,
            amount_cents=amount_cents,
        )
    except Exception as exc:
        logger.error(
            "checkout/complete-dev: mark_paid failed for order=%s: %s",
            order_id, exc,
        )

    # 4) Truly fire-and-forget email — runs on a background thread so a
    # slow SMTP server (or a misconfigured app password) can't block the
    # HTTP response. The customer sees their key immediately; the email
    # arrives whenever SMTP completes (or never, without breaking the
    # purchase).
    import threading
    def _send_email_bg():
        try:
            email_service.send_license_delivery(
                to_email      = email,
                customer_name = name,
                serial_key    = serial,
                plan_name     = plan.get("name") or "",
                expires_at    = license_row.get("expires_at") or "",
            )
        except Exception as exc:
            logger.warning(
                "checkout/complete-dev: email delivery failed for order=%s: %s",
                order_id, exc,
            )
    threading.Thread(target=_send_email_bg, daemon=True).start()

    logger.info(
        "checkout/complete-dev: order=%s plan=%s serial=%s amount=%s",
        order_id, plan_id, serial, amount_cents,
    )

    # 5) Single response shape covering the data the success page
    # template needs (order_id for support reference, serial for the
    # main display, expires_at for the "valid until" line).
    return {
        "order_id":    order_id,
        "license_key": serial,
        "serial_key":  serial,
        "expires_at":  license_row.get("expires_at"),
        "plan_name":   plan.get("name") or "",
        "amount_cents": amount_cents,
        "currency":    "ILS",
        "status":      "paid",
        "message":     "הרכישה הושלמה — המפתח שלך נוצר ונשלח למייל.",
    }
