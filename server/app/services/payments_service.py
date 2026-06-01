# -*- coding: utf-8 -*-
"""
server/app/services/payments_service.py
----------------------------------------
Provider-agnostic PAYMENT INITIATION layer — the *outbound* half of the
purchase flow, mirroring ``app.routes.webhooks`` (the inbound half).

Given a pending order it returns a hosted-payment-page URL. The website
redirects the customer there; the customer types their card details ON
THE PROVIDER'S secure page, so **card data never touches our server**
(PCI scope stays with the provider). After payment the provider calls our
webhook (``/webhook/payment/{provider}``) which issues the license.

How to wire a real provider later (≈15 min):
  1. In ``server/.env`` set:  PAYMENT_PROVIDER=grow   (or cardcom/payplus/stripe)
  2. Add that provider's credentials to ``.env`` (api key / terminal / secret).
  3. Fill in the matching ``_create_<provider>`` function below — build the
     provider's "create payment page" API call and return the redirect URL.
     Put the order id in the provider's custom/metadata field so the webhook
     can correlate it back.
  4. Set WEBHOOK_SECRET in ``.env`` (the webhook already verifies it).

Until a provider is configured, ``PAYMENT_PROVIDER`` defaults to "none" and
``is_configured()`` returns False — the website then falls back to the dev
"issue without charging" flow so the funnel stays testable.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)


class PaymentNotConfigured(Exception):
    """Raised when no real payment provider is wired up yet."""


_DISABLED = ("", "none", "off", "disabled", "dev")


def provider_name() -> str:
    """The active provider id, lower-cased (``"none"`` when unset)."""
    return (os.environ.get("PAYMENT_PROVIDER") or "none").strip().lower()


def is_configured() -> bool:
    """True when a real provider is selected (not the dev/none fallback)."""
    return provider_name() not in _DISABLED


def create_payment_session(
    *,
    order: Dict[str, Any],
    amount_cents: int,
    currency: str,
    return_url: str,
    cancel_url: str,
    customer_name: str = "",
    customer_email: str = "",
    customer_phone: str = "",
    description: str = "",
) -> Dict[str, Any]:
    """Create a hosted payment session at the active provider.

    Returns ``{"provider": str, "payment_url": str, "reference": str}``.
    Raises :class:`PaymentNotConfigured` if no provider is set up yet.
    """
    name = provider_name()
    if not is_configured():
        raise PaymentNotConfigured("PAYMENT_PROVIDER is not set (dev mode)")

    ctx = dict(
        order=order,
        amount_cents=amount_cents,
        currency=currency,
        return_url=return_url,
        cancel_url=cancel_url,
        customer_name=customer_name,
        customer_email=customer_email,
        customer_phone=customer_phone,
        description=description,
    )
    adapters = {
        "grow":    _create_grow,
        "meshulam": _create_grow,   # alias
        "cardcom": _create_cardcom,
        "payplus": _create_payplus,
        "stripe":  _create_stripe,
    }
    fn = adapters.get(name)
    if not fn:
        raise PaymentNotConfigured(f"Unknown PAYMENT_PROVIDER={name!r}")
    return fn(**ctx)


# ---------------------------------------------------------------------------
# Provider adapters — implement the one you choose. Each must return:
#   { "provider": <name>, "payment_url": <hosted page url>, "reference": <id> }
# and embed ``order["id"]`` in the provider's custom/metadata field so the
# webhook can map the callback back to the order.
# ---------------------------------------------------------------------------
def _todo(provider: str) -> Dict[str, Any]:
    raise PaymentNotConfigured(
        f"PAYMENT_PROVIDER={provider} selected but its adapter is not "
        f"implemented yet (see payments_service.py)."
    )


def _create_grow(**ctx: Any) -> Dict[str, Any]:
    # TODO Grow/Meshulam — POST to the "createPaymentProcess" endpoint with
    # pageCode + sum + successUrl=return_url + cancelUrl=cancel_url and the
    # order id in a custom field; response contains the redirect ``url``.
    return _todo("grow")


def _create_cardcom(**ctx: Any) -> Dict[str, Any]:
    # TODO Cardcom — "LowProfile/Create" returns a hosted ``url`` to redirect
    # to. Pass ReturnValue=order_id so the webhook can correlate it.
    return _todo("cardcom")


def _create_payplus(**ctx: Any) -> Dict[str, Any]:
    # TODO PayPlus — "PaymentPages/generateLink" returns
    # data.payment_page_link. Put order id in more_info / custom fields.
    return _todo("payplus")


def _create_stripe(**ctx: Any) -> Dict[str, Any]:
    # TODO Stripe — create a Checkout Session (mode="payment") with
    # success_url=return_url, cancel_url=cancel_url and
    # metadata={"order_id": ...}; return session.url.
    return _todo("stripe")
