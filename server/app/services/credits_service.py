# -*- coding: utf-8 -*-
"""
server/app/services/credits_service.py
---------------------------------------
Credit-wallet business logic for the commercial AILabTools AI-filters
feature.

Design rules (enforced here):
  * All money is **integer agorot** — never float.
  * One wallet per **customer** (a stable server-side identity), linked to
    one or more license serials. Credits survive license expiry / renewal.
  * Balance can never go negative (DB CHECK + an explicit guard here).
  * Every movement is written to the append-only ``credit_ledger`` with the
    resulting ``balance_after`` and an optional UNIQUE ``idempotency_key``.
  * All mutations run inside :func:`database.immediate_transaction` which
    takes the DB write-lock up front, so concurrent charges on the same
    wallet are fully serialized — no double-spend, no lost update.

The actual provider call + reserve→settle/refund orchestration lives in
``ai_service`` / ``routes/ai.py``; this module only moves credits.
"""

from __future__ import annotations

import datetime as _dt
import secrets
import sqlite3
from typing import Any, Dict, List, Optional

from ..database import get_connection, immediate_transaction


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class CreditsError(Exception):
    """Generic wallet error."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class InsufficientCredits(CreditsError):
    """Raised when a debit would drive the balance below zero."""

    def __init__(self, message: str = "אין מספיק יתרת קרדיטים."):
        super().__init__(message, status=402)


# ---------------------------------------------------------------------------
# Ledger entry types
# ---------------------------------------------------------------------------
ENTRY_TOPUP = "topup"
ENTRY_CHARGE = "charge"
ENTRY_REFUND = "refund"
ENTRY_ADJUST = "adjust"
# Freeze sweeps the spendable balance into the wallet's ``frozen_agorot``
# holding bucket (license went inactive); restore moves it back. Neither
# counts as consumption — they are excluded from the "used" total.
ENTRY_FREEZE = "freeze"
ENTRY_RESTORE = "restore"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _gen_public_id() -> str:
    return "CUST-" + secrets.token_hex(6).upper()


def _row_to_dict(row) -> Dict[str, Any]:
    return {} if row is None else {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------------------
# Customer + wallet provisioning (all helpers take an open connection so the
# caller controls the transaction boundary).
# ---------------------------------------------------------------------------
def _ensure_customer_for_license(conn: sqlite3.Connection, serial: str) -> int:
    """Return the customer_id owning ``serial``'s wallet, creating it lazily.

    If the license has no customer yet, a fresh customer is created and the
    license is linked to it. Customer email/phone/label are seeded from the
    license row for admin display only (never used for authentication).
    """
    serial = (serial or "").strip()
    if not serial:
        raise CreditsError("יש לספק מפתח רישיון.")

    lic = conn.execute(
        "SELECT serial_key, customer_id, customer_name, customer_email, "
        "customer_phone FROM licenses WHERE serial_key = ?",
        (serial,),
    ).fetchone()
    if lic is None:
        raise CreditsError("מפתח הרישיון לא נמצא.", status=404)

    cid = lic["customer_id"]
    if cid:
        return int(cid)

    now = _now_iso()
    cur = conn.execute(
        "INSERT INTO customers (public_id, label, email, phone, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (_gen_public_id(),
         (lic["customer_name"] or "").strip(),
         (lic["customer_email"] or "").strip(),
         (lic["customer_phone"] or "").strip(),
         now, now),
    )
    cid = int(cur.lastrowid)
    conn.execute(
        "UPDATE licenses SET customer_id = ? WHERE serial_key = ?",
        (cid, serial),
    )
    _ensure_wallet(conn, cid)
    return cid


def _ensure_wallet(conn: sqlite3.Connection, customer_id: int) -> int:
    """Return the wallet id for a customer, creating an empty wallet if none."""
    row = conn.execute(
        "SELECT id FROM wallets WHERE customer_id = ?", (customer_id,)
    ).fetchone()
    if row is not None:
        return int(row["id"])
    now = _now_iso()
    cur = conn.execute(
        "INSERT INTO wallets (customer_id, balance_agorot, created_at, "
        "updated_at) VALUES (?, 0, ?, ?)",
        (customer_id, now, now),
    )
    return int(cur.lastrowid)


def _get_balance(conn: sqlite3.Connection, customer_id: int) -> int:
    row = conn.execute(
        "SELECT balance_agorot FROM wallets WHERE customer_id = ?",
        (customer_id,),
    ).fetchone()
    return int(row["balance_agorot"]) if row else 0


def _get_frozen(conn: sqlite3.Connection, customer_id: int) -> int:
    row = conn.execute(
        "SELECT frozen_agorot FROM wallets WHERE customer_id = ?",
        (customer_id,),
    ).fetchone()
    return int(row["frozen_agorot"]) if row and row["frozen_agorot"] is not None else 0


def _find_ledger_by_idem(
    conn: sqlite3.Connection, idempotency_key: str
) -> Optional[sqlite3.Row]:
    if not idempotency_key:
        return None
    return conn.execute(
        "SELECT * FROM credit_ledger WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()


def _apply_entry(
    conn: sqlite3.Connection,
    customer_id: int,
    entry_type: str,
    amount_signed: int,
    *,
    reason: str = "",
    actor: str = "",
    ai_request_id: Optional[int] = None,
    idempotency_key: str = "",
) -> Dict[str, Any]:
    """Apply one signed movement to a wallet inside the caller's transaction.

    Returns ``{"balance_agorot", "applied", "ledger_id"}``. When
    ``idempotency_key`` already exists the movement is a no-op and the
    stored ``balance_after`` is returned with ``applied=False``.
    """
    if int(amount_signed) != amount_signed:
        raise CreditsError("סכום חייב להיות מספר שלם של אגורות.")
    amount_signed = int(amount_signed)

    # Idempotency: a movement already recorded under this key is returned
    # as-is without re-applying it.
    existing = _find_ledger_by_idem(conn, idempotency_key)
    if existing is not None:
        return {
            "balance_agorot": int(existing["balance_after"]),
            "applied": False,
            "ledger_id": int(existing["id"]),
        }

    wallet_id = _ensure_wallet(conn, customer_id)
    current = _get_balance(conn, customer_id)
    new_balance = current + amount_signed
    if new_balance < 0:
        raise InsufficientCredits()

    now = _now_iso()
    conn.execute(
        "UPDATE wallets SET balance_agorot = ?, updated_at = ? "
        "WHERE id = ?",
        (new_balance, now, wallet_id),
    )
    cur = conn.execute(
        """
        INSERT INTO credit_ledger
        (wallet_id, customer_id, entry_type, amount_agorot, balance_after,
         reason, actor, ai_request_id, idempotency_key, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (wallet_id, customer_id, entry_type, amount_signed, new_balance,
         reason or "", actor or "", ai_request_id,
         idempotency_key or None, now),
    )
    return {
        "balance_agorot": new_balance,
        "applied": True,
        "ledger_id": int(cur.lastrowid),
    }


# ---------------------------------------------------------------------------
# Public read helpers
# ---------------------------------------------------------------------------
def get_balance_for_license(serial: str) -> Dict[str, Any]:
    """Resolve (creating if needed) the wallet for a license serial.

    Returns ``{"customer_id", "wallet_id", "balance_agorot"}``. Safe to call
    on any existing license — it provisions an empty wallet on first use.
    """
    with immediate_transaction() as conn:
        cid = _ensure_customer_for_license(conn, serial)
        wid = _ensure_wallet(conn, cid)
        bal = _get_balance(conn, cid)
        frozen = _get_frozen(conn, cid)
    return {
        "customer_id": cid,
        "wallet_id": wid,
        "balance_agorot": bal,
        "frozen_agorot": frozen,
    }


def get_balance_for_customer(customer_id: int) -> int:
    with get_connection() as conn:
        return _get_balance(conn, int(customer_id))


def list_ledger(customer_id: int, limit: int = 200) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM credit_ledger WHERE customer_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (int(customer_id), int(limit)),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def credit_summaries_for_serials(serials: List[str]) -> Dict[str, Dict[str, int]]:
    """Read-only per-license AI-credit summary for the admin.

    For each serial returns ``{"balance_agorot", "used_agorot"}`` where
    *used* is net lifetime consumption (charges minus refunds). Does NOT
    provision wallets — a license with no wallet/ledger yet reports zeros.
    One query, safe for a page of licenses.
    """
    out: Dict[str, Dict[str, int]] = {
        s: {"balance_agorot": 0, "used_agorot": 0, "frozen_agorot": 0}
        for s in serials
    }
    if not serials:
        return out
    qmarks = ",".join("?" * len(serials))
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT l.serial_key AS serial,
                   COALESCE(w.balance_agorot, 0) AS bal,
                   COALESCE(w.frozen_agorot, 0) AS frozen,
                   COALESCE((
                       SELECT -SUM(cl.amount_agorot)
                       FROM credit_ledger cl
                       WHERE cl.customer_id = l.customer_id
                         AND cl.entry_type IN ('charge', 'refund')
                   ), 0) AS used
            FROM licenses l
            LEFT JOIN wallets w ON w.customer_id = l.customer_id
            WHERE l.serial_key IN ({qmarks})
            """,
            serials,
        ).fetchall()
    for r in rows:
        out[r["serial"]] = {
            "balance_agorot": int(r["bal"] or 0),
            "used_agorot": int(r["used"] or 0),
            "frozen_agorot": int(r["frozen"] or 0),
        }
    return out


def credit_summary_for_serial(serial: str) -> Dict[str, int]:
    """Single-license variant of :func:`credit_summaries_for_serials`."""
    return credit_summaries_for_serials([serial]).get(
        serial, {"balance_agorot": 0, "used_agorot": 0, "frozen_agorot": 0})


# ---------------------------------------------------------------------------
# Public mutations
# ---------------------------------------------------------------------------
def topup_by_license(
    serial: str,
    amount_agorot: int,
    *,
    actor: str = "",
    reason: str = "",
    idempotency_key: str = "",
) -> Dict[str, Any]:
    """Admin-only credit top-up, addressed by license serial."""
    amount_agorot = int(amount_agorot)
    if amount_agorot <= 0:
        raise CreditsError("סכום הטעינה חייב להיות חיובי.")
    with immediate_transaction() as conn:
        cid = _ensure_customer_for_license(conn, serial)
        res = _apply_entry(
            conn, cid, ENTRY_TOPUP, amount_agorot,
            reason=reason or "טעינה ידנית", actor=actor or "admin",
            idempotency_key=idempotency_key,
        )
    res["customer_id"] = cid
    return res


def adjust_by_license(
    serial: str,
    amount_agorot_signed: int,
    *,
    actor: str = "",
    reason: str = "",
) -> Dict[str, Any]:
    """Admin correction (positive or negative). Cannot drive balance < 0."""
    amount = int(amount_agorot_signed)
    if amount == 0:
        raise CreditsError("סכום התיקון לא יכול להיות אפס.")
    with immediate_transaction() as conn:
        cid = _ensure_customer_for_license(conn, serial)
        res = _apply_entry(
            conn, cid, ENTRY_ADJUST, amount,
            reason=reason or "תיקון יתרה", actor=actor or "admin",
        )
    res["customer_id"] = cid
    return res


def charge(
    customer_id: int,
    amount_agorot: int,
    *,
    idempotency_key: str,
    ai_request_id: Optional[int] = None,
    reason: str = "",
    actor: str = "system",
) -> Dict[str, Any]:
    """Debit a wallet (reserve on an AI request). Idempotent on the key.

    Raises :class:`InsufficientCredits` if the balance can't cover it.
    """
    amount_agorot = int(amount_agorot)
    if amount_agorot < 0:
        raise CreditsError("סכום חיוב חייב להיות אי-שלילי.")
    if not idempotency_key:
        raise CreditsError("חיוב מחייב idempotency_key.")
    with immediate_transaction() as conn:
        res = _apply_entry(
            conn, int(customer_id), ENTRY_CHARGE, -amount_agorot,
            reason=reason or "חיוב פילטר AI", actor=actor,
            ai_request_id=ai_request_id, idempotency_key=idempotency_key,
        )
    return res


def refund(
    customer_id: int,
    amount_agorot: int,
    *,
    idempotency_key: str,
    ai_request_id: Optional[int] = None,
    reason: str = "",
    actor: str = "system",
) -> Dict[str, Any]:
    """Credit back a previously-charged amount (failed generation). Idempotent."""
    amount_agorot = int(amount_agorot)
    if amount_agorot < 0:
        raise CreditsError("סכום החזר חייב להיות אי-שלילי.")
    if not idempotency_key:
        raise CreditsError("החזר מחייב idempotency_key.")
    with immediate_transaction() as conn:
        res = _apply_entry(
            conn, int(customer_id), ENTRY_REFUND, amount_agorot,
            reason=reason or "החזר על כשל פילטר AI", actor=actor,
            ai_request_id=ai_request_id, idempotency_key=idempotency_key,
        )
    return res


# ---------------------------------------------------------------------------
# Freeze / restore (license went inactive → park the balance; re-enabled →
# an admin can move it back). Both are addressed by license serial. The
# parked amount lives in ``wallets.frozen_agorot`` and is never spendable
# until restored. Neither movement counts as consumption.
# ---------------------------------------------------------------------------
def freeze_by_license(
    serial: str,
    *,
    actor: str = "",
    reason: str = "",
) -> Dict[str, Any]:
    """Sweep the spendable balance into the frozen bucket.

    Idempotent in effect: a wallet whose balance is already 0 is a no-op
    (any previously-frozen amount is preserved and accumulated). Returns
    ``{"customer_id", "balance_agorot", "frozen_agorot", "frozen_now"}``
    where ``frozen_now`` is the amount swept by THIS call.
    """
    with immediate_transaction() as conn:
        cid = _ensure_customer_for_license(conn, serial)
        wid = _ensure_wallet(conn, cid)
        bal = _get_balance(conn, cid)
        swept = 0
        if bal > 0:
            # Debit the spendable balance down to zero…
            _apply_entry(
                conn, cid, ENTRY_FREEZE, -bal,
                reason=reason or "הקפאת יתרה — רישיון לא פעיל",
                actor=actor or "system",
            )
            # …and park it in the frozen bucket (accumulating).
            now = _now_iso()
            conn.execute(
                "UPDATE wallets SET frozen_agorot = "
                "COALESCE(frozen_agorot, 0) + ?, updated_at = ? WHERE id = ?",
                (bal, now, wid),
            )
            swept = bal
        new_bal = _get_balance(conn, cid)
        new_frozen = _get_frozen(conn, cid)
    return {
        "customer_id": cid,
        "balance_agorot": new_bal,
        "frozen_agorot": new_frozen,
        "frozen_now": swept,
    }


def restore_by_license(
    serial: str,
    *,
    actor: str = "",
    reason: str = "",
) -> Dict[str, Any]:
    """Move the frozen bucket back into the spendable balance.

    No-op when nothing is frozen. Returns ``{"customer_id",
    "balance_agorot", "frozen_agorot", "restored"}`` where ``restored`` is
    the amount moved by THIS call.
    """
    with immediate_transaction() as conn:
        cid = _ensure_customer_for_license(conn, serial)
        wid = _ensure_wallet(conn, cid)
        frozen = _get_frozen(conn, cid)
        restored = 0
        if frozen > 0:
            # Credit the parked amount back onto the spendable balance…
            _apply_entry(
                conn, cid, ENTRY_RESTORE, frozen,
                reason=reason or "החזרת יתרה שהוקפאה",
                actor=actor or "admin",
            )
            # …and empty the frozen bucket.
            now = _now_iso()
            conn.execute(
                "UPDATE wallets SET frozen_agorot = 0, updated_at = ? "
                "WHERE id = ?",
                (now, wid),
            )
            restored = frozen
        new_bal = _get_balance(conn, cid)
        new_frozen = _get_frozen(conn, cid)
    return {
        "customer_id": cid,
        "balance_agorot": new_bal,
        "frozen_agorot": new_frozen,
        "restored": restored,
    }
