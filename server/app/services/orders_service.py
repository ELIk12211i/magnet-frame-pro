# -*- coding: utf-8 -*-
"""
server/app/services/orders_service.py
-------------------------------------
Business logic for the ``orders`` table — tracking every purchase
attempt that originates from the public website (``site/...``) and
flows through a payment provider webhook.

Design
------
* Orders are **additive**: nothing in the existing license flow depends
  on this table, so absence of the payment integration does not break
  anything.
* ``provider_txn_id`` is UNIQUE → webhook handlers can safely call
  ``mark_paid`` more than once (idempotency).
* On successful payment the webhook route calls
  :func:`mark_paid_and_issue_license`, which creates the license and
  links it back to the order.

This module is intentionally provider-agnostic — signature verification
and redirect URLs live in the routes layer (``routes/webhooks.py``,
``routes/checkout.py``).
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any, Dict, Optional

from ..database import get_connection


# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------
STATUS_PENDING  = "pending"    # order created, waiting for payment
STATUS_PAID     = "paid"       # webhook confirmed, license issued
STATUS_FAILED   = "failed"     # payment failed or cancelled
STATUS_REFUNDED = "refunded"   # refunded after successful payment


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _row_to_dict(row) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------------------
# Create / read
# ---------------------------------------------------------------------------
def create_pending_order(
    plan_key: str,
    customer_name: str,
    customer_email: str,
    customer_phone: str = "",
    amount_cents: int = 0,
    currency: str = "ILS",
    provider: str = "unknown",
    raw_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Insert a new ``pending`` order and return the full row."""
    payload_json = json.dumps(raw_payload, ensure_ascii=False) if raw_payload else None
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO orders (
                provider, amount_cents, currency,
                plan_key, customer_name, customer_email, customer_phone,
                status, created_at, raw_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provider, int(amount_cents or 0), currency or "ILS",
                plan_key or "", customer_name or "",
                customer_email or "", customer_phone or "",
                STATUS_PENDING, _now_iso(), payload_json,
            ),
        )
        order_id = cur.lastrowid
        row = conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
    return _row_to_dict(row) or {}


def get_order(order_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE id = ?", (int(order_id),)
        ).fetchone()
    return _row_to_dict(row)


def get_order_by_txn_id(provider: str, provider_txn_id: str) -> Optional[Dict[str, Any]]:
    """Look up an order by its (provider, provider_txn_id) pair — used for
    idempotency when a webhook fires more than once for the same event.
    """
    if not provider_txn_id:
        return None
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM orders
             WHERE provider = ? AND provider_txn_id = ?
             LIMIT 1
            """,
            (provider or "", provider_txn_id),
        ).fetchone()
    return _row_to_dict(row)


def list_orders(
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Dict[str, Any]]:
    limit  = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    sql = "SELECT * FROM orders"
    params: tuple = ()
    if status:
        sql += " WHERE status = ?"
        params = (status,)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params = params + (limit, offset)
    with get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------
def attach_transaction(
    order_id: int,
    provider: str,
    provider_txn_id: str,
    amount_cents: Optional[int] = None,
    currency: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Attach provider-specific identifiers to an existing order.

    Called after the payment provider returns a transaction ID for a
    pending order. Kept separate from :func:`mark_paid` so admins can
    see "initiated at provider" orders that did not yet confirm.
    """
    with get_connection() as conn:
        fields = ["provider = ?", "provider_txn_id = ?"]
        params: list = [provider or "unknown", provider_txn_id or ""]
        if amount_cents is not None:
            fields.append("amount_cents = ?")
            params.append(int(amount_cents))
        if currency:
            fields.append("currency = ?")
            params.append(currency)
        params.append(int(order_id))
        conn.execute(
            f"UPDATE orders SET {', '.join(fields)} WHERE id = ?",
            tuple(params),
        )
        row = conn.execute(
            "SELECT * FROM orders WHERE id = ?", (int(order_id),)
        ).fetchone()
    return _row_to_dict(row)


def mark_paid(
    order_id: int,
    license_id: Optional[int] = None,
    license_serial: Optional[str] = None,
    amount_cents: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Mark an order as paid and link it to the created license."""
    with get_connection() as conn:
        fields = ["status = ?", "paid_at = ?"]
        params: list = [STATUS_PAID, _now_iso()]
        if license_id is not None:
            fields.append("license_id = ?")
            params.append(int(license_id))
        if license_serial is not None:
            fields.append("license_serial = ?")
            params.append(str(license_serial))
        if amount_cents is not None:
            fields.append("amount_cents = ?")
            params.append(int(amount_cents))
        params.append(int(order_id))
        conn.execute(
            f"UPDATE orders SET {', '.join(fields)} WHERE id = ?",
            tuple(params),
        )
        row = conn.execute(
            "SELECT * FROM orders WHERE id = ?", (int(order_id),)
        ).fetchone()
    return _row_to_dict(row)


def mark_failed(order_id: int, reason: str = "") -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE orders
               SET status = ?, failed_at = ?, failure_reason = ?
             WHERE id = ?
            """,
            (STATUS_FAILED, _now_iso(), reason or "", int(order_id)),
        )
        row = conn.execute(
            "SELECT * FROM orders WHERE id = ?", (int(order_id),)
        ).fetchone()
    return _row_to_dict(row)


def mark_refunded(order_id: int, reason: str = "") -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE orders
               SET status = ?, failure_reason = ?
             WHERE id = ?
            """,
            (STATUS_REFUNDED, reason or "", int(order_id)),
        )
        row = conn.execute(
            "SELECT * FROM orders WHERE id = ?", (int(order_id),)
        ).fetchone()
    return _row_to_dict(row)
