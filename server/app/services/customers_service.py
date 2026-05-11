# -*- coding: utf-8 -*-
"""
server/app/services/customers_service.py
----------------------------------------
Aggregates a customer-centric view across ``orders`` + ``licenses``.

The admin "לקוחות" tab shows one row per unique email address with
the most recent contact details, total spend, license count, and
links to the underlying orders / licenses.

There is NO ``customers`` table — this service reduces the existing
tables into a customer projection on the fly. That keeps the schema
backwards-compatible: adding/removing fields here doesn't require a
migration.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from ..database import get_connection


logger = logging.getLogger(__name__)


def _decode_payload(raw: Optional[str]) -> Dict[str, Any]:
    """Best-effort JSON decode for orders.raw_payload."""
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _norm_email(email: Optional[str]) -> str:
    return (email or "").strip().lower()


def _key(email: str, phone: str) -> str:
    """
    Deduplicate by email primarily, fall back to phone when email is
    missing. Some legacy rows have empty emails so we still want to
    surface them under their phone number.
    """
    if email:
        return f"email:{email}"
    if phone:
        return f"phone:{phone.strip()}"
    return ""


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

def list_customers(
    q:     str  = "",
    page:  int  = 1,
    limit: int  = 25,
) -> Dict[str, Any]:
    """Return a paginated list of unique customers.

    Output shape:
        {
          "items":  [customer, ...],
          "total":  int,
          "page":   int,
          "limit":  int,
          "pages":  int,
        }

    Each customer dict has::

        {
          "email":          "x@y.com",
          "phone":          "050-...",
          "name":           "ישראל ישראלי",
          "id_number":      "",                  # ת.ז
          "business_name":  "",
          "business_id":    "",                  # ח.פ / ע.מ
          "orders_count":   int,
          "licenses_count": int,
          "total_paid":     int (cents),
          "first_seen":     "ISO",
          "last_seen":      "ISO",
          "active_license_serial": "MFP-..."   # most recent active, else most recent any
        }
    """
    q = (q or "").strip().lower()
    page  = max(1, int(page or 1))
    limit = max(5, min(200, int(limit or 25)))

    with get_connection() as conn:
        cur = conn.cursor()

        # 1) Pull every order row — orders is the canonical source of
        # customer details + paid amounts. Joined with the issued license
        # for status / serial.
        cur.execute(
            """
            SELECT
                o.id                AS order_id,
                o.customer_name     AS o_name,
                o.customer_email    AS o_email,
                o.customer_phone    AS o_phone,
                o.amount_cents      AS amount_cents,
                o.currency          AS currency,
                o.status            AS status,
                o.license_serial    AS license_serial,
                o.created_at        AS created_at,
                o.paid_at           AS paid_at,
                o.raw_payload       AS raw_payload,
                l.status            AS license_status,
                l.customer_name     AS l_name,
                l.customer_email    AS l_email,
                l.customer_phone    AS l_phone
            FROM orders o
            LEFT JOIN licenses l ON l.serial_key = o.license_serial
            ORDER BY o.created_at DESC
            """
        )
        rows = [dict(r) for r in cur.fetchall()]

        # 2) Pull every license row so admin-generated licenses (no
        # order row) still appear in the customers list.
        cur.execute(
            """
            SELECT
                serial_key,
                customer_name,
                customer_email,
                customer_phone,
                status,
                created_at,
                notes
            FROM licenses
            WHERE customer_email IS NOT NULL AND customer_email != ''
            ORDER BY created_at DESC
            """
        )
        licenses_rows = [dict(r) for r in cur.fetchall()]

    # ---- Fold into a {key: customer} map ------------------------------
    buckets: Dict[str, Dict[str, Any]] = {}

    for r in rows:
        email = _norm_email(r.get("o_email") or r.get("l_email"))
        phone = (r.get("o_phone") or r.get("l_phone") or "").strip()
        name  = (r.get("o_name")  or r.get("l_name") or "").strip()
        k = _key(email, phone)
        if not k:
            continue

        extra = _decode_payload(r.get("raw_payload")).get("customer_extra") or {}

        b = buckets.setdefault(k, {
            "email":          email,
            "phone":          phone,
            "name":           name,
            "id_number":      "",
            "business_name":  "",
            "business_id":    "",
            "orders_count":   0,
            "licenses_count": 0,
            "total_paid":     0,
            "first_seen":     r.get("created_at") or "",
            "last_seen":      r.get("created_at") or "",
            "active_license_serial": "",
            "_active_pri":    -1,  # priority of the chosen serial
        })

        # Always prefer the most recent non-empty value for free-text
        # fields, since the latest order usually carries the right one.
        if name and not b["name"]:
            b["name"] = name
        if phone and not b["phone"]:
            b["phone"] = phone
        for src_key, dst_key in (("id_number", "id_number"),
                                 ("business_name", "business_name"),
                                 ("business_id",   "business_id")):
            v = (extra.get(src_key) or "").strip()
            if v and not b[dst_key]:
                b[dst_key] = v

        b["orders_count"] += 1
        if (r.get("status") == "paid") and r.get("amount_cents"):
            b["total_paid"] += int(r["amount_cents"])

        # first_seen = oldest, last_seen = newest
        ca = r.get("created_at") or ""
        if ca:
            if not b["first_seen"] or ca < b["first_seen"]:
                b["first_seen"] = ca
            if ca > b["last_seen"]:
                b["last_seen"] = ca

        # Active license selection: prefer status=active, else paid+has
        # serial, else any serial.
        serial = r.get("license_serial") or ""
        lic_status = r.get("license_status") or ""
        pri = 0
        if serial:                  pri = 1
        if r.get("status") == "paid" and serial: pri = 2
        if lic_status == "active":  pri = 3
        if pri > b["_active_pri"]:
            b["active_license_serial"] = serial
            b["_active_pri"] = pri

    # Fold in orphan licenses (no order row).
    seen_serials = {b["active_license_serial"] for b in buckets.values()}
    for lr in licenses_rows:
        email = _norm_email(lr.get("customer_email"))
        phone = (lr.get("customer_phone") or "").strip()
        k = _key(email, phone)
        if not k:
            continue
        # If we already counted this customer's licenses from orders we
        # still need to bump licenses_count for serials we haven't seen.
        b = buckets.get(k)
        if b is None:
            b = buckets.setdefault(k, {
                "email":          email,
                "phone":          phone,
                "name":           (lr.get("customer_name") or "").strip(),
                "id_number":      "",
                "business_name":  "",
                "business_id":    "",
                "orders_count":   0,
                "licenses_count": 0,
                "total_paid":     0,
                "first_seen":     lr.get("created_at") or "",
                "last_seen":      lr.get("created_at") or "",
                "active_license_serial": "",
                "_active_pri":    -1,
            })
        serial = lr.get("serial_key") or ""
        if serial and serial not in seen_serials:
            b["licenses_count"] += 1
            seen_serials.add(serial)
            if (lr.get("status") == "active") and b["_active_pri"] < 3:
                b["active_license_serial"] = serial
                b["_active_pri"] = 3
            elif not b["active_license_serial"]:
                b["active_license_serial"] = serial
                b["_active_pri"] = 1

    # licenses_count via orders-driven set first (one per unique serial
    # per customer)
    serials_per_customer: Dict[str, set] = {}
    for r in rows:
        email = _norm_email(r.get("o_email") or r.get("l_email"))
        phone = (r.get("o_phone") or r.get("l_phone") or "").strip()
        k = _key(email, phone)
        if not k:
            continue
        s = r.get("license_serial") or ""
        if s:
            serials_per_customer.setdefault(k, set()).add(s)
    for k, serials in serials_per_customer.items():
        if k in buckets:
            # Don't overwrite the value we may have bumped from
            # orphan-licenses fold above — pick the bigger of the two.
            buckets[k]["licenses_count"] = max(
                buckets[k]["licenses_count"], len(serials)
            )

    items_all = list(buckets.values())
    for it in items_all:
        it.pop("_active_pri", None)

    # Filter
    if q:
        def _match(c: Dict[str, Any]) -> bool:
            blob = " ".join(str(c.get(k, "")) for k in
                ("email", "phone", "name", "id_number", "business_name",
                 "business_id", "active_license_serial")).lower()
            return q in blob
        items_all = [c for c in items_all if _match(c)]

    # Sort: most recent activity first.
    items_all.sort(key=lambda c: c.get("last_seen") or "", reverse=True)

    total = len(items_all)
    pages = (total + limit - 1) // limit if total else 1
    start = (page - 1) * limit
    items = items_all[start:start + limit]

    return {
        "items":  items,
        "total":  total,
        "page":   page,
        "limit":  limit,
        "pages":  pages,
    }


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

def get_customer(email: str) -> Optional[Dict[str, Any]]:
    """Return full profile of one customer keyed by email.

    Shape::

        {
          "email":          str,
          "phone":          str,
          "name":           str,
          "id_number":      str,
          "business_name":  str,
          "business_id":    str,
          "orders":   [ {id, status, amount_cents, currency, plan_key,
                         license_serial, created_at, paid_at,
                         provider, raw_payload (dict)}, ... ],
          "licenses": [ {serial_key, license_type, status, plan_name,
                         machine_id, hostname, hardware_id,
                         client_public_ip, activated_at, expires_at,
                         last_validation_at, notes}, ... ],
          "events":   [ {id, event_type, message, created_at,
                         serial_key, ip}, ... ],
          "first_seen":  str,
          "last_seen":   str,
          "total_paid":  int (cents),
        }
    """
    email_n = _norm_email(email)
    if not email_n:
        return None

    with get_connection() as conn:
        cur = conn.cursor()

        # Orders by this email
        cur.execute(
            """
            SELECT id, provider, provider_txn_id, amount_cents, currency, plan_key,
                   customer_name, customer_email, customer_phone, license_serial,
                   status, created_at, paid_at, failed_at, failure_reason,
                   raw_payload
            FROM orders
            WHERE LOWER(COALESCE(customer_email, '')) = ?
            ORDER BY created_at DESC
            """,
            (email_n,),
        )
        orders_rows: List[Dict[str, Any]] = []
        for r in cur.fetchall():
            d = dict(r)
            d["raw_payload"] = _decode_payload(d.get("raw_payload"))
            orders_rows.append(d)

        # Licenses by this email
        cur.execute(
            """
            SELECT serial_key, license_type, status, plan_name, plan_days,
                   machine_id, hostname, hardware_id, client_public_ip,
                   customer_name, customer_email, customer_phone,
                   activated_at, expires_at, last_validation_at,
                   disabled_at, disabled_reason, created_at, notes
            FROM licenses
            WHERE LOWER(COALESCE(customer_email, '')) = ?
            ORDER BY created_at DESC
            """,
            (email_n,),
        )
        licenses_rows = [dict(r) for r in cur.fetchall()]

        # Events query stays inside the same connection scope.
        serials = [l["serial_key"] for l in licenses_rows if l.get("serial_key")]
        events_rows: List[Dict[str, Any]] = []
        if serials:
            placeholders = ",".join("?" for _ in serials)
            cur.execute(
                f"""
                SELECT id, serial_key, machine_id, event_type, message,
                       ip, actor, created_at
                FROM events
                WHERE serial_key IN ({placeholders})
                ORDER BY created_at DESC
                LIMIT 50
                """,
                serials,
            )
            events_rows = [dict(r) for r in cur.fetchall()]

    if not orders_rows and not licenses_rows:
        return None

    # Aggregate identity
    name = phone = ""
    id_number = business_name = business_id = ""
    first_seen = ""
    last_seen  = ""
    total_paid = 0

    for o in orders_rows:
        if o.get("customer_name") and not name:
            name = o["customer_name"]
        if o.get("customer_phone") and not phone:
            phone = o["customer_phone"]
        extra = (o.get("raw_payload") or {}).get("customer_extra") or {}
        if (extra.get("id_number")     or "").strip() and not id_number:
            id_number     = extra["id_number"].strip()
        if (extra.get("business_name") or "").strip() and not business_name:
            business_name = extra["business_name"].strip()
        if (extra.get("business_id")   or "").strip() and not business_id:
            business_id   = extra["business_id"].strip()
        ca = o.get("created_at") or ""
        if ca:
            if not first_seen or ca < first_seen: first_seen = ca
            if ca > last_seen: last_seen = ca
        if o.get("status") == "paid" and o.get("amount_cents"):
            total_paid += int(o["amount_cents"])

    for l in licenses_rows:
        if l.get("customer_name") and not name:
            name = l["customer_name"]
        if l.get("customer_phone") and not phone:
            phone = l["customer_phone"]
        ca = l.get("created_at") or ""
        if ca:
            if not first_seen or ca < first_seen: first_seen = ca
            if ca > last_seen: last_seen = ca
        # Pull id_number/business_* from license notes too (legacy rows
        # where the structured customer_extra was never recorded).
        notes = (l.get("notes") or "")
        for fragment in notes.split("|"):
            fragment = fragment.strip()
            if fragment.startswith("id_number:") and not id_number:
                id_number = fragment.split(":", 1)[1].strip()
            elif fragment.startswith("business:") and not business_name:
                business_name = fragment.split(":", 1)[1].strip()
            elif fragment.startswith("tax_id:") and not business_id:
                business_id = fragment.split(":", 1)[1].strip()

    return {
        "email":         email_n,
        "phone":         phone,
        "name":          name,
        "id_number":     id_number,
        "business_name": business_name,
        "business_id":   business_id,
        "orders":        orders_rows,
        "licenses":      licenses_rows,
        "events":        events_rows,
        "first_seen":    first_seen,
        "last_seen":     last_seen,
        "total_paid":    total_paid,
    }
