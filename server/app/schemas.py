# -*- coding: utf-8 -*-
"""
server/app/schemas.py
---------------------
Pydantic request/response models for the license API.

All /license/* endpoints return the unified ``LicenseResponse`` shape so
any consumer (desktop client or admin UI) can work against a single
contract regardless of which endpoint was called.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Public /license/* request models
# ---------------------------------------------------------------------------

class ActivateRequest(BaseModel):
    serial_key: str = Field(..., min_length=1, max_length=64)
    machine_id: str = Field(..., min_length=1, max_length=128)
    machine_uuid: Optional[str] = Field("", max_length=128)
    hostname:     Optional[str] = Field("", max_length=255)
    public_ip:    Optional[str] = Field("", max_length=64)


class ValidateRequest(BaseModel):
    serial_key: str = Field(..., min_length=1, max_length=64)
    machine_id: str = Field(..., min_length=1, max_length=128)
    machine_uuid: Optional[str] = Field("", max_length=128)
    hostname:     Optional[str] = Field("", max_length=255)
    public_ip:    Optional[str] = Field("", max_length=64)


class TrialRequest(BaseModel):
    machine_id: str = Field(..., min_length=1, max_length=128)
    # Lead-capture fields sent from the desktop "התחל תוכנית נסיון"
    # dialog — recorded in the trial_leads table so the admin sees
    # who signed up.  Both are optional to keep the endpoint
    # backwards-compatible with older clients.
    name:  Optional[str] = Field("", max_length=120)
    phone: Optional[str] = Field("", max_length=40)


class ResetRequest(BaseModel):
    serial_key: str = Field(..., min_length=1, max_length=64)
    actor: Optional[str] = Field("", max_length=128)


class LicenseInfoRequest(BaseModel):
    """Body for ``POST /license/license-info``."""
    serial_key: str = Field(..., min_length=1, max_length=64)


class GenerateRequest(BaseModel):
    """Body for ``POST /license/generate``.

    ``license_type`` accepts ``"trial"`` / ``"yearly"`` / ``"lifetime"``
    (or the internal ``trial_14_days``). ``days`` is only consulted for
    yearly licenses.
    """
    license_type:   str = Field(..., description="trial | yearly | lifetime")
    days:           Optional[int] = Field(None, ge=1, le=3650,
                                          description="Only used for yearly.")
    customer_name:  Optional[str] = Field("", max_length=256)
    customer_email: Optional[str] = Field("", max_length=256)
    notes:          Optional[str] = Field("", max_length=2000)


class DisableRequest(BaseModel):
    serial_key: str = Field(..., min_length=1, max_length=64)
    reason:     Optional[str] = Field("", max_length=2000)
    actor:      Optional[str] = Field("", max_length=128)


class EnableRequest(BaseModel):
    serial_key: str = Field(..., min_length=1, max_length=64)
    actor:      Optional[str] = Field("", max_length=128)


class ExtendRequest(BaseModel):
    """Extend a license's expiry by N days."""
    days:  int = Field(..., ge=1, le=3650)
    actor: Optional[str] = Field("", max_length=128)


# ---------------------------------------------------------------------------
# Unified response model
# ---------------------------------------------------------------------------

class LicenseResponse(BaseModel):
    """Unified response shape returned by every ``/license/*`` endpoint."""
    success:        bool = True
    status:         str
    license_type:   Optional[str] = None
    license_key:    Optional[str] = None
    serial_key:     Optional[str] = None     # legacy alias
    machine_id:     Optional[str] = None
    activated_at:   Optional[str] = None
    expires_at:     Optional[str] = None
    customer_name:  Optional[str] = ""
    customer_email: Optional[str] = ""
    is_demo:        bool = False
    message:        Optional[str] = ""

    class Config:
        extra = "allow"


# ---------------------------------------------------------------------------
# Checkout + webhook schemas (public website → server → payment provider)
# ---------------------------------------------------------------------------

class PublicPlan(BaseModel):
    """Subset of ``subscription_plans`` exposed to the public website."""
    id:           int
    name:         str
    days:         Optional[int] = None
    license_type: str
    price_ils:    float = 0.0
    sort_order:   int = 0


class CheckoutCreateRequest(BaseModel):
    """Body for ``POST /checkout/create`` — submitted by the website."""
    plan_id:        int = Field(..., ge=1, description="subscription_plans.id")
    customer_name:  str = Field(..., min_length=1, max_length=120)
    customer_email: str = Field(..., min_length=3, max_length=256)
    customer_phone: Optional[str] = Field("", max_length=40)
    # Optional invoicing / KYC fields submitted by the website's checkout
    # form. Stored in orders.raw_payload (under the ``customer_extra`` key)
    # and on the issued license's ``notes`` so the admin "Customers" tab
    # can display them without a schema migration.
    customer_id_number: Optional[str] = Field("", max_length=40)
    business_name:      Optional[str] = Field("", max_length=200)
    business_id:        Optional[str] = Field("", max_length=40)
    notes:              Optional[str] = Field("", max_length=2000)


class CheckoutCreateResponse(BaseModel):
    """Response from ``POST /checkout/create``."""
    order_id:     int
    status:       str
    amount_cents: int
    currency:     str = "ILS"
    next_step:    Optional[str] = ""  # provider-specific redirect URL
    message:      Optional[str] = ""


class WebhookTestIssueRequest(BaseModel):
    """Body for ``POST /webhook/test-issue`` — dev-only helper."""
    order_id: int = Field(..., ge=1)
