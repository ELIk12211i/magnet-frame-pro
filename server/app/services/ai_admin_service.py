# -*- coding: utf-8 -*-
"""
server/app/services/ai_admin_service.py
----------------------------------------
Admin-facing read/write helpers for the AI-filters + credit-wallet feature.
Keeps the admin routes thin and keeps all wallet mutations going through
``credits_service`` (atomic + idempotent + agorot-only).

Read helpers are deliberately side-effect-free: opening a license page must
NOT create a wallet. A wallet/customer is provisioned lazily only when the
admin actually tops up / adjusts.
"""

from __future__ import annotations

import datetime as _dt
import json as _json
import sqlite3
from typing import Any, Dict, List, Optional

from ..database import get_connection
from . import credits_service as _credits


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _row_to_dict(r) -> Dict[str, Any]:
    return {} if r is None else {k: r[k] for k in r.keys()}


# ---------------------------------------------------------------------------
# Read: per-license credit overview (for the license-detail page)
# ---------------------------------------------------------------------------
def license_credit_overview(serial: str) -> Optional[Dict[str, Any]]:
    """Return wallet balance + ledger + AI-request history for a license.

    Side-effect-free: if the license has no customer/wallet yet it reports a
    zero balance and empty history (nothing is created). Returns None only
    when the license itself doesn't exist.
    """
    serial = (serial or "").strip()
    with get_connection() as conn:
        lic = conn.execute(
            "SELECT customer_id FROM licenses WHERE serial_key = ?", (serial,)
        ).fetchone()
        if lic is None:
            return None
        cid = lic["customer_id"]
        if not cid:
            return {
                "customer_id": None, "public_id": "", "balance_agorot": 0,
                "ledger": [], "requests": [],
                "counts": {"sent": 0, "succeeded": 0, "failed": 0,
                           "unknown": 0},
            }
        cust = conn.execute(
            "SELECT public_id FROM customers WHERE id = ?", (cid,)
        ).fetchone()
        wal = conn.execute(
            "SELECT balance_agorot FROM wallets WHERE customer_id = ?", (cid,)
        ).fetchone()
        ledger = conn.execute(
            "SELECT * FROM credit_ledger WHERE customer_id = ? "
            "ORDER BY id DESC LIMIT 100", (cid,)
        ).fetchall()
        reqs = conn.execute(
            "SELECT * FROM ai_requests WHERE customer_id = ? "
            "ORDER BY id DESC LIMIT 100", (cid,)
        ).fetchall()
        counts = {"sent": len(reqs), "succeeded": 0, "failed": 0, "unknown": 0}
        for r in reqs:
            st = r["status"]
            if st == "succeeded":
                counts["succeeded"] += 1
            elif st == "failed":
                counts["failed"] += 1
            elif st == "unknown":
                counts["unknown"] += 1
    return {
        "customer_id": int(cid),
        "public_id": (cust["public_id"] if cust else "") or "",
        "balance_agorot": int(wal["balance_agorot"]) if wal else 0,
        "ledger": [_row_to_dict(r) for r in ledger],
        "requests": [_row_to_dict(r) for r in reqs],
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# Write: admin top-up / adjust (addressed by license serial)
# ---------------------------------------------------------------------------
def topup_ils(serial: str, amount_ils: float, actor: str,
              reason: str = "") -> Dict[str, Any]:
    agorot = int(round(float(amount_ils) * 100))
    return _credits.topup_by_license(
        serial, agorot, actor=actor,
        reason=reason or "טעינה ידנית ע\"י מנהל",
    )


def adjust_ils(serial: str, amount_ils_signed: float, actor: str,
               reason: str = "") -> Dict[str, Any]:
    agorot = int(round(float(amount_ils_signed) * 100))
    return _credits.adjust_by_license(
        serial, agorot, actor=actor,
        reason=reason or "תיקון יתרה ע\"י מנהל",
    )


# ---------------------------------------------------------------------------
# Filter catalogue management
# ---------------------------------------------------------------------------
# Human-readable labels for the AILabTools routing kinds (admin display).
_KIND_LABELS = {
    "cartoon": "קריקטורה / סגנון (portrait-animation)",
    "emotion": "שינוי הבעה (emotion-editor)",
    "emotion_all": "הבעה לכל הפנים (emotion + face-analyzer)",
    "emotion_adaptive": "הבעה חכמה לכל הפנים",
}


# Category grouping for the visual admin grid.
_CATEGORY_LABELS = {
    "emotion": "הבעות",
    "emotion_all": "הבעות (כל הפרצופים)",
    "emotion_adaptive": "הבעות (כל הפרצופים)",
    "cartoon": "קריקטורות וסגנונות",
}


def _parse_params(raw: str) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        cfg = _json.loads(raw)
    except Exception:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _enrich_filter(d: Dict[str, Any]) -> Dict[str, Any]:
    """Add derived, display-only fields parsed from params_json.

    ``ailab_kind`` / ``ailab_kind_label`` / ``ailab_code`` (the style/emotion
    code sent to the provider), ``category`` / ``category_label`` for grid
    grouping, and ``preview_url`` (admin-set example image). Read-only hints —
    never written back by the enrich step.
    """
    cfg = _parse_params(d.get("params_json") or "")
    kind = str(cfg.get("ailab_kind") or "")
    form = cfg.get("form") or {}
    code = ""
    if isinstance(form, dict):
        code = str(form.get("type") or form.get("service_choice") or "")
    d["ailab_kind"] = kind
    d["ailab_kind_label"] = _KIND_LABELS.get(kind, kind or "—")
    d["ailab_code"] = code
    d["category"] = kind or "other"
    d["category_label"] = _CATEGORY_LABELS.get(kind, "אחר")
    d["preview_url"] = str(cfg.get("preview_url") or "")
    d["sale_price_ils"] = (int(d.get("sale_price_agorot") or 0)) / 100.0
    d["cost_ceiling_ils"] = (int(d.get("cost_ceiling_agorot") or 0)) / 100.0
    return d


def list_filters() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM ai_filters ORDER BY sort_order, display_name"
        ).fetchall()
    return [_enrich_filter(_row_to_dict(r)) for r in rows]


def update_filter(
    filter_key: str,
    *,
    display_name: Optional[str] = None,
    sale_price_agorot: Optional[int] = None,
    cost_ceiling_agorot: Optional[int] = None,
    is_enabled: Optional[bool] = None,
    sort_order: Optional[int] = None,
    preview_url: Optional[str] = None,
) -> bool:
    updates: Dict[str, Any] = {}
    if display_name is not None and display_name.strip():
        updates["display_name"] = display_name.strip()
    if sale_price_agorot is not None:
        updates["sale_price_agorot"] = max(0, int(sale_price_agorot))
    if cost_ceiling_agorot is not None:
        updates["cost_ceiling_agorot"] = max(0, int(cost_ceiling_agorot))
    if is_enabled is not None:
        updates["is_enabled"] = 1 if is_enabled else 0
    if sort_order is not None:
        updates["sort_order"] = int(sort_order)
    with get_connection() as conn:
        # Merge preview_url into params_json WITHOUT clobbering the routing
        # keys (ailab_kind / image_field / form) that the provider relies on.
        if preview_url is not None:
            row = conn.execute(
                "SELECT params_json FROM ai_filters WHERE filter_key = ?",
                (filter_key,),
            ).fetchone()
            cfg = _parse_params(row["params_json"] if row else "")
            pu = preview_url.strip()
            if pu:
                cfg["preview_url"] = pu
            else:
                cfg.pop("preview_url", None)
            updates["params_json"] = _json.dumps(cfg, ensure_ascii=False)
        if not updates:
            return False
        updates["updated_at"] = _now_iso()
        cols = ", ".join(f"{k} = ?" for k in updates)
        res = conn.execute(
            f"UPDATE ai_filters SET {cols} WHERE filter_key = ?",
            list(updates.values()) + [filter_key],
        )
    return res.rowcount > 0


def set_filter_image(filter_key: str, data: bytes, mime: str) -> bool:
    """Store an uploaded preview image (in the DB) and point the filter's
    ``preview_url`` at the public serving route. Returns True on success.

    Persisting the bytes in the DB (not on disk) keeps the image across
    Railway redeploys. A ``?v=`` cache-buster is appended so browsers and the
    app reload the new image immediately after replacement.
    """
    filter_key = (filter_key or "").strip()
    if not filter_key or not data:
        return False
    now = _now_iso()
    with get_connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM ai_filters WHERE filter_key = ?", (filter_key,)
        ).fetchone()
        if exists is None:
            return False
        conn.execute(
            "INSERT INTO ai_filter_images (filter_key, mime, data, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(filter_key) DO UPDATE SET "
            "mime = excluded.mime, data = excluded.data, "
            "updated_at = excluded.updated_at",
            (filter_key, mime or "image/jpeg", sqlite3.Binary(data), now),
        )
    # point preview_url at the serving route (cache-busted by updated time)
    ver = "".join(ch for ch in now if ch.isdigit())
    update_filter(filter_key,
                  preview_url=f"/ai/filter-image/{filter_key}?v={ver}")
    return True


def get_filter_image(filter_key: str) -> Optional[Dict[str, Any]]:
    """Return {'mime', 'data'} for a stored preview image, or None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT mime, data FROM ai_filter_images WHERE filter_key = ?",
            ((filter_key or "").strip(),),
        ).fetchone()
    if row is None:
        return None
    return {"mime": row["mime"] or "image/jpeg", "data": bytes(row["data"])}


def set_all_filters_enabled(enabled: bool) -> int:
    """Enable or disable EVERY filter at once. Returns the number of rows
    changed. Used by the admin master toggle."""
    val = 1 if enabled else 0
    with get_connection() as conn:
        res = conn.execute(
            "UPDATE ai_filters SET is_enabled = ?, updated_at = ?",
            (val, _now_iso()),
        )
    return res.rowcount


def toggle_filter(filter_key: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT is_enabled FROM ai_filters WHERE filter_key = ?",
            (filter_key,),
        ).fetchone()
        if row is None:
            return False
        new_val = 0 if int(row["is_enabled"]) else 1
        conn.execute(
            "UPDATE ai_filters SET is_enabled = ?, updated_at = ? "
            "WHERE filter_key = ?",
            (new_val, _now_iso(), filter_key),
        )
    return True


# ---------------------------------------------------------------------------
# AI requests history + unknown-request reconciliation
# ---------------------------------------------------------------------------
def list_requests(status: Optional[str] = None,
                  limit: int = 200) -> List[Dict[str, Any]]:
    where = ""
    params: list = []
    if status:
        where = " WHERE r.status = ?"
        params.append(status)
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT r.*, c.public_id AS customer_public_id,
                       f.display_name AS filter_name
                  FROM ai_requests r
                  LEFT JOIN customers c ON c.id = r.customer_id
                  LEFT JOIN ai_filters f ON f.filter_key = r.filter_key
                 {where}
                 ORDER BY r.id DESC LIMIT ?""",
            params + [int(limit)],
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def count_unknown() -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM ai_requests WHERE status = 'unknown'"
        ).fetchone()
    return int(row["c"]) if row else 0


def resolve_unknown(req_id: int, outcome: str, actor: str = "admin") -> Dict[str, Any]:
    """Controlled close of an 'unknown' (ambiguous) request.

    outcome == "succeeded": the provider actually produced the image —
      keep the charge, mark succeeded. No refund.
    outcome == "failed": the provider did NOT produce an image — refund the
      customer exactly once (idempotent on the request's refund key) and
      mark failed.

    Refund idempotency key is ``rfnd:<idem>`` — the same key the automatic
    failure path would use — so a request can never be refunded twice.
    """
    outcome = (outcome or "").strip().lower()
    if outcome not in ("succeeded", "failed"):
        raise ValueError("outcome must be 'succeeded' or 'failed'")

    with get_connection() as conn:
        req = conn.execute(
            "SELECT * FROM ai_requests WHERE id = ?", (int(req_id),)
        ).fetchone()
        if req is None:
            raise LookupError("request not found")
        if req["status"] != "unknown":
            raise ValueError("ניתן לסגור רק בקשות במצב 'לא ידוע'.")
        idem = req["idempotency_key"]
        cid = int(req["customer_id"])
        price = int(req["sale_price_agorot"])

    if outcome == "succeeded":
        with get_connection() as conn:
            conn.execute(
                "UPDATE ai_requests SET status = 'succeeded', "
                "error_code = '', error_detail = 'admin: confirmed success', "
                "finished_at = ? WHERE id = ?",
                (_now_iso(), int(req_id)),
            )
        return {"status": "succeeded", "refunded_agorot": 0}

    # failed → refund once (idempotent), then mark failed
    refund_res = _credits.refund(
        cid, price, idempotency_key="rfnd:" + idem,
        ai_request_id=int(req_id),
        reason="החזר לאחר בירור בקשה לא ידועה", actor=actor,
    )
    with get_connection() as conn:
        conn.execute(
            "UPDATE ai_requests SET status = 'failed', "
            "error_code = 'RESOLVED_FAILED', "
            "error_detail = 'admin: confirmed failure, refunded', "
            "finished_at = ? WHERE id = ?",
            (_now_iso(), int(req_id)),
        )
    return {"status": "failed",
            "refunded_agorot": price if refund_res.get("applied") else 0,
            "balance_agorot": refund_res.get("balance_agorot")}
