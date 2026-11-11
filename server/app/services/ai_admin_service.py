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

from ..database import get_connection, get_config, set_config
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
                "used_agorot": 0, "frozen_agorot": 0,
                "ledger": [], "requests": [],
                "counts": {"sent": 0, "succeeded": 0, "failed": 0,
                           "unknown": 0},
            }
        cust = conn.execute(
            "SELECT public_id FROM customers WHERE id = ?", (cid,)
        ).fetchone()
        wal = conn.execute(
            "SELECT balance_agorot, frozen_agorot FROM wallets "
            "WHERE customer_id = ?", (cid,)
        ).fetchone()
        ledger = conn.execute(
            "SELECT * FROM credit_ledger WHERE customer_id = ? "
            "ORDER BY id DESC LIMIT 100", (cid,)
        ).fetchall()
        reqs = conn.execute(
            "SELECT * FROM ai_requests WHERE customer_id = ? "
            "ORDER BY id DESC LIMIT 100", (cid,)
        ).fetchall()
        # Lifetime net consumption (charges minus refunds) — not limited to
        # the 100-row ledger window above, so it stays accurate for heavy
        # users. Charge is negative, refund positive, so a refunded charge
        # nets to zero.
        used_row = conn.execute(
            "SELECT COALESCE(-SUM(amount_agorot), 0) AS used "
            "FROM credit_ledger WHERE customer_id = ? "
            "AND entry_type IN ('charge', 'refund')", (cid,)
        ).fetchone()
        used_agorot = int(used_row["used"] or 0) if used_row else 0
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
        "frozen_agorot": (
            int(wal["frozen_agorot"])
            if wal and wal["frozen_agorot"] is not None else 0),
        "used_agorot": used_agorot,
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
    # ui_category — admin-chosen DISPLAY drawer/group the filter appears under
    # in the desktop app (free text, e.g. "פילטרים מצויירים"). Separate from
    # ``category``/``ailab_kind`` (which drives provider routing + the client's
    # anti-crop zoom heuristic), so renaming the drawer never changes behaviour.
    d["ui_category"] = str(cfg.get("ui_category") or "")
    d["preview_url"] = str(cfg.get("preview_url") or "")
    # Routing fields (editable in admin) — provider/provider_path/result_kind
    # are columns already in ``d``; image_field / form / zooms live in cfg.
    d["image_field"] = str(cfg.get("image_field") or "")
    # EFFECTIVE zoom state (matches the client catalogue): explicit admin flag
    # OR the legacy cartoon heuristic. So the edit-form checkbox shows the true
    # behaviour for seeded cartoon filters that have no explicit flag yet.
    d["zooms"] = bool(cfg.get("zooms")) or (kind == "cartoon")
    d["form_json"] = (
        _json.dumps(form, ensure_ascii=False)
        if isinstance(form, dict) and form else ""
    )
    d["sale_price_ils"] = (int(d.get("sale_price_agorot") or 0)) / 100.0
    d["cost_ceiling_ils"] = (int(d.get("cost_ceiling_agorot") or 0)) / 100.0
    return d


def list_filters() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM ai_filters ORDER BY sort_order, display_name"
        ).fetchall()
    return [_enrich_filter(_row_to_dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# Display categories ("מגירות") — admin CRUD
# ---------------------------------------------------------------------------
def list_categories() -> List[Dict[str, Any]]:
    """All display categories, ordered as they appear in the app."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT category_key, display_name, sort_order FROM ai_categories "
            "ORDER BY sort_order, display_name"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _new_category_key(conn) -> str:
    import secrets as _secrets
    for _ in range(20):
        key = "cat_" + _secrets.token_hex(4)
        if conn.execute(
            "SELECT 1 FROM ai_categories WHERE category_key = ?", (key,)
        ).fetchone() is None:
            return key
    return "cat_" + _secrets.token_hex(8)


def create_category(display_name: str) -> Optional[str]:
    """Create a category (auto-generated stable key). Returns its key or None."""
    name = (display_name or "").strip()
    if not name:
        return None
    now = _now_iso()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) AS m FROM ai_categories"
        ).fetchone()
        nxt = int(row["m"] or 0) + 10
        key = _new_category_key(conn)
        conn.execute(
            "INSERT INTO ai_categories (category_key, display_name, sort_order,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (key, name, nxt, now, now),
        )
    return key


def rename_category(category_key: str, display_name: str) -> bool:
    key = (category_key or "").strip()
    name = (display_name or "").strip()
    if not key or not name:
        return False
    with get_connection() as conn:
        res = conn.execute(
            "UPDATE ai_categories SET display_name = ?, updated_at = ? "
            "WHERE category_key = ?", (name, _now_iso(), key),
        )
    return res.rowcount > 0


def set_category_order(category_key: str, sort_order: Any) -> bool:
    key = (category_key or "").strip()
    if not key:
        return False
    try:
        so = int(sort_order)
    except (TypeError, ValueError):
        return False
    with get_connection() as conn:
        res = conn.execute(
            "UPDATE ai_categories SET sort_order = ?, updated_at = ? "
            "WHERE category_key = ?", (so, _now_iso(), key),
        )
    return res.rowcount > 0


def delete_category(category_key: str) -> bool:
    """Delete a category. Filters still pointing at the now-missing key fall
    back to their default (by-kind) drawer in the catalogue — no filter is lost
    or disabled; the admin can reassign them to another category."""
    key = (category_key or "").strip()
    if not key:
        return False
    with get_connection() as conn:
        res = conn.execute(
            "DELETE FROM ai_categories WHERE category_key = ?", (key,)
        )
    return res.rowcount > 0


# ---------------------------------------------------------------------------
# Supplier-cost economics (admin dashboard display)
# ---------------------------------------------------------------------------
# Real AILabTools credits billed per SUCCESSFUL image, by provider kind (from
# the official endpoint catalogue). The stored ``provider_credits`` is a
# uniform, conservative 20 used only by the cost-CEILING guard; these are the
# TRUE per-image charges used for the cost/profit display.
#   cartoon (portrait-animation) = 20 · emotion (emotion-editor) = 12
_REAL_CREDITS_BY_KIND = {
    "cartoon": 20,
    "emotion": 12,
}
_DEFAULT_USD_PER_CREDIT = 0.006   # worst (smallest) AILabTools credit pack
_DEFAULT_USD_ILS_RATE = 3.80      # ₪ per $1 — admin-editable


def get_cost_params() -> Dict[str, float]:
    """Admin-set economics for the cost/profit display (config-backed).

    ``usd_per_credit`` — what AILabTools charges you per credit (depends on the
    credit pack you bought). ``usd_ils_rate`` — ₪ per $1, the admin types it.
    """
    def _num(key: str, dflt: float) -> float:
        try:
            v = float(str(get_config(key, "") or "").strip() or dflt)
            return v if v > 0 else dflt
        except Exception:
            return dflt
    return {
        "usd_per_credit": _num("ai_usd_per_credit", _DEFAULT_USD_PER_CREDIT),
        "usd_ils_rate": _num("ai_usd_ils_rate", _DEFAULT_USD_ILS_RATE),
    }


def set_cost_params(usd_per_credit: Any, usd_ils_rate: Any) -> None:
    """Persist the admin's credit price ($) and $/₪ rate (config table)."""
    def _f(v: Any, dflt: float) -> float:
        try:
            x = float(str(v).strip().replace(",", "."))
            return x if x > 0 else dflt
        except Exception:
            return dflt
    set_config("ai_usd_per_credit",
               repr(_f(usd_per_credit, _DEFAULT_USD_PER_CREDIT)))
    set_config("ai_usd_ils_rate",
               repr(_f(usd_ils_rate, _DEFAULT_USD_ILS_RATE)))


def add_cost_fields(filters: List[Dict[str, Any]],
                    params: Optional[Dict[str, float]] = None
                    ) -> List[Dict[str, Any]]:
    """Annotate each filter with real-credit supplier cost + profit (display).

    Adds: ``real_credits``, ``cost_usd``, ``cost_ils``, ``profit_ils``.
    """
    p = params or get_cost_params()
    upc = p["usd_per_credit"]
    rate = p["usd_ils_rate"]
    for d in filters:
        kind = str(d.get("ailab_kind") or "")
        credits = _REAL_CREDITS_BY_KIND.get(kind)
        if credits is None:
            credits = int(d.get("provider_credits") or 0)
        cost_usd = credits * upc
        cost_ils = cost_usd * rate
        sale_ils = float(d.get("sale_price_ils") or 0.0)
        d["real_credits"] = credits
        d["cost_usd"] = cost_usd
        d["cost_ils"] = cost_ils
        d["profit_ils"] = sale_ils - cost_ils
    return filters


def update_filter(
    filter_key: str,
    *,
    display_name: Optional[str] = None,
    sale_price_agorot: Optional[int] = None,
    cost_ceiling_agorot: Optional[int] = None,
    is_enabled: Optional[bool] = None,
    sort_order: Optional[int] = None,
    preview_url: Optional[str] = None,
    ui_category: Optional[str] = None,
    provider: Optional[str] = None,
    provider_path: Optional[str] = None,
    result_kind: Optional[str] = None,
    image_field: Optional[str] = None,
    form: Optional[Dict[str, Any]] = None,
    zooms: Optional[bool] = None,
    ailab_kind: Optional[str] = None,
) -> bool:
    """Update an AI filter. Column fields (display_name, prices, provider,
    provider_path, result_kind, ...) write directly; routing/display fields
    kept in params_json (image_field, form, zooms, preview_url, ui_category,
    ailab_kind) are merged in WITHOUT clobbering the keys left untouched.
    Every argument is optional — only the ones passed are changed.
    """
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
    if provider is not None and provider.strip():
        updates["provider"] = provider.strip()
    if provider_path is not None:
        updates["provider_path"] = provider_path.strip()
    if result_kind is not None and result_kind.strip():
        updates["result_kind"] = result_kind.strip()
    # params_json merge is needed if ANY of these routing/display keys is set
    _merge_params = any(
        v is not None
        for v in (preview_url, ui_category, image_field, form, zooms, ailab_kind)
    )
    with get_connection() as conn:
        # Merge selected keys into params_json WITHOUT clobbering the routing
        # keys the provider relies on. Read the current JSON once.
        if _merge_params:
            row = conn.execute(
                "SELECT params_json FROM ai_filters WHERE filter_key = ?",
                (filter_key,),
            ).fetchone()
            cfg = _parse_params(row["params_json"] if row else "")
            if preview_url is not None:
                pu = preview_url.strip()
                if pu:
                    cfg["preview_url"] = pu
                else:
                    cfg.pop("preview_url", None)
            if ui_category is not None:
                uc = ui_category.strip()
                if uc:
                    cfg["ui_category"] = uc
                else:
                    cfg.pop("ui_category", None)
            if image_field is not None:
                f = image_field.strip()
                if f:
                    cfg["image_field"] = f
                else:
                    cfg.pop("image_field", None)
            if ailab_kind is not None:
                k = ailab_kind.strip()
                if k:
                    cfg["ailab_kind"] = k
                else:
                    cfg.pop("ailab_kind", None)
            if form is not None:
                if form:
                    cfg["form"] = form
                else:
                    cfg.pop("form", None)
            if zooms is not None:
                if zooms:
                    cfg["zooms"] = True
                else:
                    cfg.pop("zooms", None)
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


# ---------------------------------------------------------------------------
# Filter create / delete (admin)
# ---------------------------------------------------------------------------
def create_filter(
    filter_key: str,
    display_name: str,
    *,
    provider: str = "ailabtools",
    provider_path: str = "",
    result_kind: str = "image_url",
    image_field: str = "image",
    form: Optional[Dict[str, Any]] = None,
    ailab_kind: str = "",
    zooms: bool = False,
    sale_price_agorot: int = 0,
    cost_ceiling_agorot: int = 0,
    ui_category: str = "",
    sort_order: Optional[int] = None,
) -> Optional[str]:
    """Create a new AI filter row. Returns the filter_key, or None if the key
    is blank / already taken. The catalog is served live, so a new row appears
    in the client's AI tab with no client update."""
    filter_key = (filter_key or "").strip()
    display_name = (display_name or "").strip()
    if not filter_key or not display_name:
        return None
    cfg: Dict[str, Any] = {}
    if (ailab_kind or "").strip():
        cfg["ailab_kind"] = ailab_kind.strip()
    if (image_field or "").strip():
        cfg["image_field"] = image_field.strip()
    if form:
        cfg["form"] = form
    if (ui_category or "").strip():
        cfg["ui_category"] = ui_category.strip()
    if zooms:
        cfg["zooms"] = True
    now = _now_iso()
    with get_connection() as conn:
        if conn.execute(
            "SELECT 1 FROM ai_filters WHERE filter_key = ?", (filter_key,)
        ).fetchone() is not None:
            return None
        if sort_order is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) AS m FROM ai_filters"
            ).fetchone()
            sort_order = int(row["m"] or 0) + 10
        conn.execute(
            "INSERT INTO ai_filters (filter_key, display_name, provider,"
            " provider_path, result_kind, provider_credits, sale_price_agorot,"
            " cost_ceiling_agorot, params_json, is_enabled, sort_order,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, 1, ?, ?, ?)",
            (
                filter_key, display_name, (provider or "ailabtools").strip(),
                (provider_path or "").strip(),
                (result_kind or "image_url").strip(),
                max(0, int(sale_price_agorot)), max(0, int(cost_ceiling_agorot)),
                _json.dumps(cfg, ensure_ascii=False), int(sort_order), now, now,
            ),
        )
    return filter_key


def delete_filter(filter_key: str) -> bool:
    """Delete an AI filter and its stored preview image."""
    filter_key = (filter_key or "").strip()
    if not filter_key:
        return False
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM ai_filter_images WHERE filter_key = ?", (filter_key,)
        )
        res = conn.execute(
            "DELETE FROM ai_filters WHERE filter_key = ?", (filter_key,)
        )
    return res.rowcount > 0


# ---------------------------------------------------------------------------
# AI PROVIDERS — admin CRUD. The api_key is a secret: callers get it MASKED
# (last 4 only); it is never returned in full, never sent to the client, and
# never logged. Empty api_key means "fall back to the server env var".
# ---------------------------------------------------------------------------
def _mask_key(raw: str) -> str:
    raw = raw or ""
    if not raw:
        return ""
    if len(raw) <= 4:
        return "····"
    return "····" + raw[-4:]


def list_providers() -> List[Dict[str, Any]]:
    """All providers, with the api_key MASKED (never the real value)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT provider_key, display_name, base_url, auth_header,"
            " api_key, adapter, is_enabled FROM ai_providers"
            " ORDER BY display_name"
        ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = _row_to_dict(r)
        raw = str(d.pop("api_key", "") or "")
        d["api_key_masked"] = _mask_key(raw)
        d["has_key"] = bool(raw)
        d["is_enabled"] = int(d.get("is_enabled") or 0)
        out.append(d)
    return out


def create_provider(
    provider_key: str,
    display_name: str,
    *,
    base_url: str = "",
    auth_header: str = "ailabapi-api-key",
    api_key: str = "",
    adapter: str = "ailabtools",
    is_enabled: bool = True,
) -> Optional[str]:
    """Create a provider. Returns its key, or None if blank / already taken."""
    provider_key = (provider_key or "").strip()
    display_name = (display_name or "").strip()
    if not provider_key or not display_name:
        return None
    now = _now_iso()
    with get_connection() as conn:
        if conn.execute(
            "SELECT 1 FROM ai_providers WHERE provider_key = ?", (provider_key,)
        ).fetchone() is not None:
            return None
        conn.execute(
            "INSERT INTO ai_providers (provider_key, display_name, base_url,"
            " auth_header, api_key, adapter, is_enabled, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                provider_key, display_name, (base_url or "").strip(),
                (auth_header or "ailabapi-api-key").strip(),
                (api_key or "").strip(), (adapter or "ailabtools").strip(),
                1 if is_enabled else 0, now, now,
            ),
        )
    return provider_key


def update_provider(
    provider_key: str,
    *,
    display_name: Optional[str] = None,
    base_url: Optional[str] = None,
    auth_header: Optional[str] = None,
    api_key: Optional[str] = None,
    adapter: Optional[str] = None,
    is_enabled: Optional[bool] = None,
) -> bool:
    """Update a provider. ``api_key`` is only changed when a non-None value is
    passed; pass an empty string to CLEAR it (→ env fallback), or None (the
    default) to LEAVE THE STORED KEY UNTOUCHED. This lets the edit form omit
    the key field to keep the existing secret."""
    provider_key = (provider_key or "").strip()
    if not provider_key:
        return False
    updates: Dict[str, Any] = {}
    if display_name is not None and display_name.strip():
        updates["display_name"] = display_name.strip()
    if base_url is not None:
        updates["base_url"] = base_url.strip()
    if auth_header is not None and auth_header.strip():
        updates["auth_header"] = auth_header.strip()
    if adapter is not None and adapter.strip():
        updates["adapter"] = adapter.strip()
    if is_enabled is not None:
        updates["is_enabled"] = 1 if is_enabled else 0
    if api_key is not None:
        updates["api_key"] = api_key.strip()
    if not updates:
        return False
    updates["updated_at"] = _now_iso()
    cols = ", ".join(f"{k} = ?" for k in updates)
    with get_connection() as conn:
        res = conn.execute(
            f"UPDATE ai_providers SET {cols} WHERE provider_key = ?",
            list(updates.values()) + [provider_key],
        )
    return res.rowcount > 0


def delete_provider(provider_key: str) -> bool:
    """Delete a provider, unless any filter still points at it (returns False
    then, so the admin reassigns those filters first)."""
    provider_key = (provider_key or "").strip()
    if not provider_key:
        return False
    with get_connection() as conn:
        used = conn.execute(
            "SELECT COUNT(*) AS n FROM ai_filters WHERE provider = ?",
            (provider_key,),
        ).fetchone()
        if int(used["n"] or 0) > 0:
            return False
        res = conn.execute(
            "DELETE FROM ai_providers WHERE provider_key = ?", (provider_key,)
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
