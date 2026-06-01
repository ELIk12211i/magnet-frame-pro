# -*- coding: utf-8 -*-
"""
server/app/routes/admin_pages.py
--------------------------------
Admin UI — Jinja page handlers mounted at ``/admin/*``.

These handlers:
  1. Check auth (except ``/login``), redirecting to ``/admin/login`` if not
     signed in.
  2. Query data via the service layer.
  3. Render a Jinja template with a context dict.

The templates themselves live under ``server/app/templates/admin/*.html``
and are created/maintained by the frontend agent. See
``server/FRONTEND_CONTRACT.md`` for the full list of templates and the
context variables each receives.

If a template is missing, handlers fall back to a plain-text placeholder
so the routes remain pingable during bootstrapping.
"""

from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import auth as _auth
from .. import config as _cfg
from ..database import get_connection
from ..services import events_service as _evt
from ..services import license_service as svc
from ..services import stats_service as _stats


logger = logging.getLogger(__name__)


router = APIRouter()


# Shared templates instance — built once per process.
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Jinja filters — convert UTC ISO timestamps to Israel local time for display
# ---------------------------------------------------------------------------

def _israel_dst_window(year: int) -> tuple:
    """Return the (start, end) LOCAL-time boundaries of Israeli DST for *year*.

    Israel's rule (current since 2013):
      * DST starts at 02:00 on the Friday before the last Sunday of March.
      * DST ends   at 02:00 on the last Sunday of October.
    """
    # Start: Friday before the last Sunday of March.
    last = _dt.date(year, 3, 31)
    while last.weekday() != 6:   # 6 = Sunday
        last -= _dt.timedelta(days=1)
    start_friday = last - _dt.timedelta(days=2)
    dst_start = _dt.datetime.combine(start_friday, _dt.time(2, 0))

    # End: last Sunday of October at 02:00.
    last_oct = _dt.date(year, 10, 31)
    while last_oct.weekday() != 6:
        last_oct -= _dt.timedelta(days=1)
    dst_end = _dt.datetime.combine(last_oct, _dt.time(2, 0))
    return dst_start, dst_end


class _IsraelTZ(_dt.tzinfo):
    """DST-aware Asia/Jerusalem fallback (used only when tzdata is missing)."""

    def _is_dst(self, dt_: _dt.datetime) -> bool:
        naive = dt_.replace(tzinfo=None)
        start, end = _israel_dst_window(naive.year)
        return start <= naive < end

    def utcoffset(self, dt_):
        return _dt.timedelta(hours=3) if dt_ and self._is_dst(dt_) \
            else _dt.timedelta(hours=2)

    def dst(self, dt_):
        return _dt.timedelta(hours=1) if dt_ and self._is_dst(dt_) \
            else _dt.timedelta(0)

    def tzname(self, dt_):
        return "IDT" if dt_ and self._is_dst(dt_) else "IST"


try:
    from zoneinfo import ZoneInfo as _ZoneInfo  # Python 3.9+
    _ISRAEL_TZ: Any = _ZoneInfo("Asia/Jerusalem")
except Exception:  # pragma: no cover — tzdata missing
    _ISRAEL_TZ = _IsraelTZ()


def _parse_utc_iso(value: Any) -> Optional[_dt.datetime]:
    """Parse an ISO-8601 timestamp string (optionally with Z or offset).

    Returns a timezone-aware ``datetime`` in UTC, or ``None`` if the input
    is empty/invalid.
    """
    if not value:
        return None
    try:
        s = str(value).strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = _dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d.astimezone(_dt.timezone.utc)
    except Exception:
        return None


def _fmt_local_dt(value: Any):
    """Jinja filter returning a ``<time>`` element that JS converts to browser time.

    Emits::

        <time class="js-local-dt" datetime="2026-04-15T21:04:56+00:00">
          2026-04-16 00:04:56
        </time>

    The visible text is the server's best-effort Israel time (so non-JS
    clients still see the right number); a small script in ``base.html``
    replaces the content with the browser's local representation of
    ``datetime`` — that way the admin UI always shows the correct time
    regardless of the server's tz database.
    """
    from markupsafe import Markup
    d = _parse_utc_iso(value)
    if d is None:
        return Markup("—")
    iso = d.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    local = d.astimezone(_ISRAEL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return Markup(f'<time class="js-local-dt" datetime="{iso}">{local}</time>')


def _fmt_local_date(value: Any):
    """Jinja filter: date-only variant (JS renders browser-local YYYY-MM-DD)."""
    from markupsafe import Markup
    d = _parse_utc_iso(value)
    if d is None:
        return Markup("—")
    iso = d.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    local = d.astimezone(_ISRAEL_TZ).strftime("%Y-%m-%d")
    return Markup(f'<time class="js-local-date" datetime="{iso}">{local}</time>')


# Register filters on both template instances — the router-local one *and*
# the shared app-wide one mounted in ``main.py`` (both share the same env
# type but different Environment objects).
for _tpl in (templates,):
    try:
        _tpl.env.filters["localdt"] = _fmt_local_dt
        _tpl.env.filters["localdate"] = _fmt_local_date
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client_ip(request: Request) -> str:
    try:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.client.host if request.client else ""
    except Exception:
        return ""


def _ua(request: Request) -> str:
    try:
        return (request.headers.get("user-agent") or "")[:512]
    except Exception:
        return ""


def _require_user_or_redirect(request: Request) -> str:
    """Return the current user, or raise HTTPException(303) to /admin/login."""
    user = _auth.get_current_user(request)
    if not user:
        # 303 See Other so browsers switch POST → GET on redirect.
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})
    return user


def _render(
    request: Request,
    template: str,
    ctx: Dict[str, Any],
    status_code: int = 200,
):
    """Render a Jinja template, falling back to a plain-text placeholder."""
    ctx = dict(ctx)
    ctx.setdefault("request", request)
    tpl_path = _TEMPLATES_DIR / template
    if not tpl_path.exists():
        # Log the detail server-side; return a generic message so we never
        # leak template names / context keys to the browser.
        logger.error(
            "admin template missing: %s (ctx keys: %s)",
            template,
            ", ".join(sorted(k for k in ctx.keys() if k != "request")),
        )
        return PlainTextResponse("Page temporarily unavailable.",
                                 status_code=status_code)
    try:
        return templates.TemplateResponse(
            template, ctx, status_code=status_code
        )
    except Exception:
        # Log the full traceback server-side; never echo exception text.
        logger.exception("admin template render error: %s", template)
        return PlainTextResponse("Page temporarily unavailable.",
                                 status_code=500)


def _request_is_https(request) -> bool:
    """True when the request reached us over HTTPS.

    Railway (and most PaaS) terminate TLS at an edge proxy and forward
    plain HTTP internally, so ``request.url.scheme`` is often ``http``
    even for a genuine HTTPS request — the authoritative signal is the
    ``X-Forwarded-Proto`` header. We check both so the cookie is marked
    ``Secure`` in production while staying unset for local ``http`` dev
    (where a ``Secure`` cookie would never be sent back).
    """
    if request is None:
        return False
    try:
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        if proto:
            return proto == "https"
        return request.url.scheme == "https"
    except Exception:
        return False


def _set_session_cookie(response, token: str, request=None) -> None:
    response.set_cookie(
        key=_cfg.SESSION_COOKIE_NAME,
        value=token,
        max_age=_cfg.SESSION_LIFETIME_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=_request_is_https(request),
        path="/",
    )


def _clear_session_cookie(response) -> None:
    response.delete_cookie(
        key=_cfg.SESSION_COOKIE_NAME, path="/"
    )


# ---------------------------------------------------------------------------
# Auth pages
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request, next: Optional[str] = Query(None)):
    # If already signed in, send them to the dashboard.
    if _auth.get_current_user(request):
        return RedirectResponse("/admin/dashboard", status_code=302)

    ctx = {
        "page_title": "Login",
        "active_nav": "login",
        "user": None,
        "error": None,
        "username": "",
        "next": next or "",
    }
    return _render(request, "admin/login.html", ctx)


@router.post("/login", include_in_schema=False)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
):
    username = (username or "").strip()
    if _auth.authenticate(username, password):
        token = _auth.create_session(
            username, ip=_client_ip(request), user_agent=_ua(request),
        )
        try:
            _evt.log_event(None, None, _evt.EVENT_ADMIN_LOGIN,
                           message=f"user={username}",
                           ip=_client_ip(request), actor=username)
        except Exception:
            pass
        target = next.strip() or "/admin/dashboard"
        if not target.startswith("/admin"):
            target = "/admin/dashboard"
        resp = RedirectResponse(target, status_code=302)
        _set_session_cookie(resp, token, request)
        return resp

    # Failed login — re-render with error.
    try:
        _evt.log_event(None, None, _evt.EVENT_ADMIN_LOGIN_FAILED,
                       message=f"user={username}",
                       ip=_client_ip(request), actor=username)
    except Exception:
        pass
    ctx = {
        "page_title": "Login",
        "active_nav": "login",
        "user": None,
        "error": "Invalid username or password",
        "username": username,
        "next": next or "",
    }
    return _render(request, "admin/login.html", ctx, status_code=401)


@router.post("/logout", include_in_schema=False)
def logout(request: Request):
    token = request.cookies.get(_cfg.SESSION_COOKIE_NAME, "")
    if token:
        _auth.destroy_session(token)
    try:
        _evt.log_event(None, None, _evt.EVENT_ADMIN_LOGOUT,
                       ip=_client_ip(request),
                       actor=_auth.get_current_user(request) or "")
    except Exception:
        pass
    resp = RedirectResponse("/admin/login", status_code=302)
    _clear_session_cookie(resp)
    return resp


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse, include_in_schema=False)
@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request):
    user = _require_user_or_redirect(request)
    ctx = {
        "page_title": "Dashboard",
        "active_nav": "dashboard",
        "user": user,
        "stats": _stats.overview(),
        "recent_licenses": _stats.recent_licenses(n=10),
        "recent_events": _evt.recent_events(n=20),
    }
    return _render(request, "admin/dashboard.html", ctx)


# ---------------------------------------------------------------------------
# License generator
# ---------------------------------------------------------------------------

@router.get("/generator", response_class=HTMLResponse, include_in_schema=False)
def generator_page(request: Request):
    user = _require_user_or_redirect(request)
    # Pull the active plan catalogue for the combobox suggestions.
    from ..services import plans_service as _plans
    plans = _plans.list_plans(include_inactive=False)
    ctx = {
        "page_title": "Generate License",
        "active_nav": "generator",
        "user": user,
        "error": None,
        "created": None,
        "plans": plans,
        "form": {
            "first_name":           "",
            "last_name":            "",
            "phone":                "",
            "license_type_name":    "",
            "days":                 "",
        },
    }
    return _render(request, "admin/generator.html", ctx)


@router.post("/generator", response_class=HTMLResponse, include_in_schema=False)
def generator_submit(
    request: Request,
    first_name: str = Form(""),
    last_name: str = Form(""),
    phone: str = Form(""),
    license_type_name: str = Form(""),
    days: str = Form(""),
):
    """Create a license from a plan name + days.

    The license-type field is a combobox: the admin either picks an
    existing plan by name (``license_type_name`` matches a row in
    ``subscription_plans``) or types a brand-new name, in which case
    we upsert that plan on the fly so it shows up in the catalogue
    for future issues.  ``days`` is always authoritative — if the
    admin overrides an existing plan's days we persist that too.
    """
    user = _require_user_or_redirect(request)
    from ..services import plans_service as _plans

    form = {
        "first_name":           first_name,
        "last_name":            last_name,
        "phone":                phone,
        "license_type_name":    license_type_name,
        "days":                 days,
    }
    plans = _plans.list_plans(include_inactive=False)
    ctx = {
        "page_title": "Generate License",
        "active_nav": "generator",
        "user": user,
        "error": None,
        "created": None,
        "plans": plans,
        "form": form,
    }

    # ---- Input validation ------------------------------------------------
    name = (license_type_name or "").strip()
    if not (first_name or "").strip() and not (last_name or "").strip():
        ctx["error"] = "יש להזין שם פרטי או שם משפחה."
        return _render(request, "admin/generator.html", ctx, status_code=400)
    if not name:
        ctx["error"] = "יש לבחור תכנית או להזין שם."
        return _render(request, "admin/generator.html", ctx, status_code=400)

    days_int: Optional[int] = None
    days_txt = (days or "").strip()
    if days_txt:
        try:
            days_int = int(days_txt)
            if days_int < 1 or days_int > 36_500:
                raise ValueError
        except ValueError:
            ctx["error"] = "כמות ימים חייבת להיות מספר שלם בין 1 ל-36500."
            return _render(request, "admin/generator.html", ctx, status_code=400)

    # ---- Resolve the plan ------------------------------------------------
    # License kind is determined by the plan itself — managed from
    # /admin/plans.  If the admin typed an unknown name we auto-create
    # a new "yearly" plan (safest default) so the combobox learns it.
    matched_plan = None
    for p in _plans.list_plans(include_inactive=True):
        if (p.get("name") or "").strip() == name:
            matched_plan = p
            break

    if matched_plan is None:
        kind_fallback = "lifetime" if days_int is None else "yearly"
        try:
            matched_plan = _plans.create_plan(
                name=name, days=days_int, license_type=kind_fallback,
                sort_order=100,
            )
        except Exception as exc:
            ctx["error"] = f"נכשלה יצירת תכנית חדשה: {exc}"
            return _render(request, "admin/generator.html", ctx, status_code=400)

    # Kind comes from the plan; days default to the plan's value unless
    # the admin explicitly typed a different number in the form.
    license_type = matched_plan.get("license_type") or "yearly"
    if license_type == "lifetime":
        final_days = None
    else:
        final_days = days_int if days_int is not None else matched_plan.get("days")
        if final_days is None:
            ctx["error"] = "יש להזין כמות ימים עבור תכנית זו."
            return _render(request, "admin/generator.html", ctx, status_code=400)

    # ---- Create the license ---------------------------------------------
    try:
        # Prefer the plan's custom display label (when the admin set
        # one in the plans catalogue) over the raw plan name.
        display_plan_name = (matched_plan.get("custom_type")
                             or matched_plan.get("name") or name)
        data = svc.generate(
            license_type=license_type,
            days=final_days,
            customer_first_name=first_name,
            customer_last_name=last_name,
            customer_phone=phone,
            plan_name=display_plan_name,
            plan_days=final_days,
            actor=user,
        )
    except svc.LicenseError as e:
        ctx["error"] = str(e)
        return _render(request, "admin/generator.html", ctx, status_code=400)

    ctx["created"] = data
    ctx["plans"]   = _plans.list_plans(include_inactive=False)
    # Clear per-customer fields after success but keep the plan choice
    # pre-filled so issuing another of the same plan is one-click.
    ctx["form"] = {
        "first_name":           "",
        "last_name":            "",
        "phone":                "",
        "license_type_name":    name,
        "days":                 str(final_days) if final_days is not None else "",
    }
    return _render(request, "admin/generator.html", ctx)


# ---------------------------------------------------------------------------
# Subscription plans management
# ---------------------------------------------------------------------------

@router.get("/plans", response_class=HTMLResponse, include_in_schema=False)
def plans_page(request: Request):
    user = _require_user_or_redirect(request)
    from ..services import plans_service as _plans
    # Surface any ?error=… / ?success=… passed back from a POST handler
    # that used a redirect-after-save.
    qp = request.query_params
    ctx = {
        "page_title": "Subscription Plans",
        "active_nav": "plans",
        "user": user,
        "plans": _plans.list_plans(include_inactive=True),
        "error":   qp.get("error")   or None,
        "success": qp.get("success") or None,
    }
    return _render(request, "admin/plans.html", ctx)


@router.post("/plans", response_class=HTMLResponse, include_in_schema=False)
def plans_create(
    request: Request,
    name: str = Form(...),
    days: str = Form(""),
    license_type: str = Form("yearly"),
    sort_order: str = Form("0"),
    custom_type: str = Form(""),
):
    user = _require_user_or_redirect(request)
    from ..services import plans_service as _plans
    try:
        days_int = int(days) if (days or "").strip() else None
    except Exception:
        days_int = None
    try:
        so_int = int(sort_order) if (sort_order or "").strip() else 0
    except Exception:
        so_int = 0

    ctx = {
        "page_title": "Subscription Plans",
        "active_nav": "plans",
        "user": user,
        "plans": _plans.list_plans(include_inactive=True),
        "error": None,
        "success": None,
    }
    try:
        # If "מותאם" was selected, the custom_type free-text value
        # is the admin's intended display label.  We map the
        # underlying license_type to "yearly" (most flexible behaviour
        # — has expiry + days) so the system keeps working as before.
        effective_type = license_type
        effective_custom = (custom_type or "").strip()
        if license_type == "custom":
            effective_type = "yearly"
        _plans.create_plan(
            name=name, days=days_int,
            license_type=effective_type,
            sort_order=so_int,
            custom_type=effective_custom,
        )
        ctx["success"] = f"התכנית '{name}' נוצרה בהצלחה."
        ctx["plans"] = _plans.list_plans(include_inactive=True)
    except Exception as exc:
        ctx["error"] = str(exc)
    return _render(request, "admin/plans.html", ctx)


@router.post("/plans/{plan_id}/toggle",
             response_class=HTMLResponse, include_in_schema=False)
def plans_toggle(request: Request, plan_id: int):
    _require_user_or_redirect(request)
    from ..services import plans_service as _plans
    _plans.toggle_plan(plan_id)
    return RedirectResponse(url="/admin/plans", status_code=303)


@router.post("/plans/{plan_id}/edit",
             response_class=HTMLResponse, include_in_schema=False)
def plans_edit(
    request: Request,
    plan_id: int,
    name: str = Form(""),
    days: str = Form(""),
    license_type: str = Form(""),
    sort_order: str = Form(""),
):
    """Patch a plan's fields.  Empty form inputs are treated as
    "don't touch" — only fields the admin actually changed get sent
    to :func:`update_plan`.  System-plan protection is enforced by
    the service layer (name changes are rejected with ``ValueError``).
    """
    _require_user_or_redirect(request)
    from ..services import plans_service as _plans
    from urllib.parse import quote as _q

    payload: Dict[str, Any] = {}
    if (name or "").strip():
        payload["name"] = name.strip()
    if (license_type or "").strip():
        payload["license_type"] = license_type.strip()
    if (days or "").strip():
        try:
            payload["days"] = int(days)
        except Exception:
            return RedirectResponse(
                url=f"/admin/plans?error={_q('מספר ימים לא תקין.')}",
                status_code=303,
            )
    if (sort_order or "").strip():
        try:
            payload["sort_order"] = int(sort_order)
        except Exception:
            return RedirectResponse(
                url=f"/admin/plans?error={_q('סדר תצוגה לא תקין.')}",
                status_code=303,
            )

    try:
        _plans.update_plan(plan_id, **payload)
    except (ValueError, LookupError) as exc:
        return RedirectResponse(
            url=f"/admin/plans?error={_q(str(exc))}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/admin/plans?success={_q('התכנית עודכנה בהצלחה.')}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Trial leads (משתמשים חדשים)
# ---------------------------------------------------------------------------

@router.get("/leads", response_class=HTMLResponse, include_in_schema=False)
def leads_page(request: Request):
    user = _require_user_or_redirect(request)
    from ..services import leads_service as _leads
    qp = request.query_params
    ctx = {
        "page_title": "משתמשים חדשים",
        "active_nav": "leads",
        "user": user,
        "leads": _leads.list_leads(limit=1000),
        "error":   qp.get("error")   or None,
        "success": qp.get("success") or None,
    }
    return _render(request, "admin/leads.html", ctx)


@router.get("/leads/{lead_id}", response_class=HTMLResponse,
            include_in_schema=False)
def leads_detail(request: Request, lead_id: int):
    """Full profile for a single trial lead — contact info + machine
    fingerprint + license status + activation timeline."""
    user = _require_user_or_redirect(request)
    from ..services import leads_service as _leads
    detail = _leads.get_lead(lead_id)
    if not detail:
        return RedirectResponse(url="/admin/leads", status_code=303)
    ctx = {
        "page_title": "פרופיל משתמש",
        "active_nav": "leads",
        "user":        user,
        "lead":        detail.get("lead", {}),
        "license":     detail.get("license", {}),
        "activations": detail.get("activations", []),
        "all_trials":  detail.get("all_trials", []),
    }
    return _render(request, "admin/lead_detail.html", ctx)


@router.post("/leads/{lead_id}/delete",
             response_class=HTMLResponse, include_in_schema=False)
def leads_delete(request: Request, lead_id: int):
    _require_user_or_redirect(request)
    from ..services import leads_service as _leads
    from urllib.parse import quote as _q
    try:
        _leads.delete_lead(lead_id)
    except Exception as exc:
        return RedirectResponse(
            url=f"/admin/leads?error={_q(str(exc))}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/admin/leads?success={_q('הרשומה נמחקה בהצלחה.')}",
        status_code=303,
    )


@router.post("/plans/{plan_id}/delete",
             response_class=HTMLResponse, include_in_schema=False)
def plans_delete(request: Request, plan_id: int):
    _require_user_or_redirect(request)
    from ..services import plans_service as _plans
    from urllib.parse import quote as _q
    try:
        _plans.delete_plan(plan_id)
    except ValueError as exc:
        # System plan or other business-rule refusal — surface the
        # message on the plans page instead of crashing.
        return RedirectResponse(
            url=f"/admin/plans?error={_q(str(exc))}",
            status_code=303,
        )
    return RedirectResponse(url="/admin/plans", status_code=303)


# ---------------------------------------------------------------------------
# Licenses list / detail
# ---------------------------------------------------------------------------

def _row_to_dict(row) -> Dict[str, Any]:
    return {k: row[k] for k in row.keys()} if row is not None else {}


@router.get("/licenses", response_class=HTMLResponse, include_in_schema=False)
def licenses_list(
    request: Request,
    q: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    type: Optional[str] = Query(None, alias="type"),
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=200),
):
    user = _require_user_or_redirect(request)

    where = []
    params = []
    if q:
        like = f"%{q.strip()}%"
        where.append(
            "(serial_key LIKE ? OR COALESCE(customer_name, '') LIKE ? "
            "OR COALESCE(customer_email, '') LIKE ? "
            "OR COALESCE(machine_id, '') LIKE ?)"
        )
        params.extend([like, like, like, like])
    if status:
        where.append("status = ?")
        params.append(status)
    if type:
        where.append("license_type = ?")
        params.append(type)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    offset = (page - 1) * limit

    with get_connection() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM licenses{clause}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"""SELECT * FROM licenses{clause}
                 ORDER BY created_at DESC, serial_key DESC
                 LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()

    items = [_row_to_dict(r) for r in rows]
    pages = max(1, (total + limit - 1) // limit)

    ctx = {
        "page_title": "Licenses",
        "active_nav": "licenses",
        "user": user,
        "filters": {"q": q or "", "status": status or "", "type": type or ""},
        "page": page,
        "limit": limit,
        "total": total,
        "pages": pages,
        "items": items,
    }
    return _render(request, "admin/licenses.html", ctx)


@router.get("/licenses/{serial_key}", response_class=HTMLResponse,
            include_in_schema=False)
def license_detail(request: Request, serial_key: str):
    user = _require_user_or_redirect(request)

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM licenses WHERE serial_key = ?", (serial_key,)
        ).fetchone()
    if row is None:
        ctx = {
            "page_title": "License Not Found",
            "active_nav": "licenses",
            "user": user,
            "serial_key": serial_key,
        }
        return _render(request, "admin/license_not_found.html", ctx,
                       status_code=404)

    activations = _evt.list_activations_for_serial(serial_key)
    events = _evt.recent_events_for_serial(serial_key, n=50)

    # ── Machine bookkeeping for the detail view ────────────────────
    # * ``current_machine`` — the machine currently holding the license
    #   (the ``machine_id`` column on the licenses row).
    # * ``public_ip`` / ``last_seen_at`` — pulled from the *latest*
    #   activation row that matches the current machine.  If there is
    #   no current machine (reset or never activated), we fall back to
    #   the most-recent activation overall.
    # * ``unique_machines_count`` — number of distinct ``machine_id``
    #   values that ever activated this license.  This stays
    #   monotonic — resetting a license doesn't delete history, so
    #   the counter keeps climbing as the serial moves between
    #   machines.
    license_dict = _row_to_dict(row)
    current_machine = (license_dict.get("machine_id") or "").strip()

    # ── Reconcile the status against the current expires_at ───────
    # Runs every time the admin opens the detail page so the shown
    # status matches reality — in BOTH directions:
    #   * past date → force 'expired' (even if row still says active)
    #   * future date on an expired row → revive to active / unused
    #
    # This saves the admin from having to manually toggle status after
    # editing the date.
    _exp_raw = license_dict.get("expires_at")
    _status = (license_dict.get("status") or "").lower()
    _lic_type = (license_dict.get("license_type") or "").lower()
    if _exp_raw and _lic_type != "lifetime" and _status != "disabled":
        try:
            _exp_dt = _parse_utc_iso(_exp_raw)
            if _exp_dt is not None:
                _now = _dt.datetime.now(_dt.timezone.utc)
                new_status = None
                if _exp_dt <= _now and _status != "expired":
                    new_status = "expired"
                    _msg = "תוקף הרישיון פג."
                    _val_stat = "expired"
                elif _exp_dt > _now and _status == "expired":
                    # Revive — bound to a machine → active, otherwise unused.
                    new_status = ("active"
                                  if (license_dict.get("machine_id") or "")
                                  else "unused")
                    _msg = "הרישיון תקף."
                    _val_stat = "success"
                if new_status:
                    with get_connection() as conn:
                        conn.execute(
                            "UPDATE licenses SET status=?, "
                            "validation_status=?, "
                            "validation_message=? WHERE serial_key=?",
                            (new_status, _val_stat, _msg, serial_key),
                        )
                    license_dict["status"]             = new_status
                    license_dict["validation_status"]  = _val_stat
                    license_dict["validation_message"] = _msg
                    try:
                        _evt.log_event(
                            serial_key, None,
                            _evt.EVENT_YEARLY_EXPIRED
                            if new_status == "expired"
                            and license_dict.get("license_type") == svc.LICENSE_TYPE_YEARLY
                            else (_evt.EVENT_VALIDATION_EXPIRED
                                  if new_status == "expired"
                                  else _evt.EVENT_LICENSE_VALIDATED_SUCCESS),
                            message=f"auto-reconciled to {new_status} on admin view",
                            actor=user,
                        )
                    except Exception:
                        pass
        except Exception:
            pass

    public_ip = ""
    last_seen_public = None
    current_activation = None
    for a in activations:
        if current_machine and (a.get("machine_id") or "") == current_machine:
            current_activation = a
            public_ip = a.get("ip") or ""
            last_seen_public = a.get("last_seen_at") or a.get("activated_at")
            break
    if not current_activation and activations:
        # No exact machine match (e.g. after reset); surface the
        # most-recent activation instead so the card isn't empty.
        current_activation = activations[0]
        if not public_ip:
            public_ip = current_activation.get("ip") or ""
        if not last_seen_public:
            last_seen_public = (current_activation.get("last_seen_at")
                                or current_activation.get("activated_at"))

    # Count unique machines that ever activated this serial.  We
    # de-duplicate by ``machine_id`` because a single machine can
    # have multiple activation rows (one per reset cycle).
    unique_machines = {
        (a.get("machine_id") or "").strip()
        for a in activations
        if (a.get("machine_id") or "").strip()
    }
    # Also pull machines whose paid license binding was MIGRATED
    # AWAY from this serial — they live in ``machine_history`` and
    # may not have a corresponding activation row anymore.  Merge
    # them into the unique-machines count + expose a dedicated list
    # to the template so the admin sees the full lifetime trail.
    migrated_machines: list[Dict[str, Any]] = []
    try:
        with get_connection() as _conn_mh:
            mh_rows = _conn_mh.execute(
                "SELECT machine_id, last_serial, reason, recorded_at "
                "FROM machine_history WHERE last_serial = ? "
                "ORDER BY recorded_at DESC",
                (serial_key,),
            ).fetchall()
        for r in mh_rows:
            mid = (r["machine_id"] or "").strip() if r else ""
            if not mid:
                continue
            unique_machines.add(mid)
            migrated_machines.append({
                "machine_id":  mid,
                "last_serial": r["last_serial"] or "",
                "reason":      r["reason"] or "",
                "recorded_at": r["recorded_at"] or "",
            })
    except Exception:
        # ``machine_history`` was added in a later migration —
        # tolerate older databases that haven't been upgraded yet.
        migrated_machines = []
    license_dict["public_ip"]             = public_ip
    license_dict["last_seen_at"]          = last_seen_public
    license_dict["unique_machines_count"] = len(unique_machines)

    ctx = {
        "page_title": f"License {serial_key}",
        "active_nav": "licenses",
        "user": user,
        "license": license_dict,
        "activations": activations,
        "current_machine": current_machine,
        "unique_machines_count": len(unique_machines),
        "migrated_machines": migrated_machines,
        "events": events,
    }
    return _render(request, "admin/license_detail.html", ctx)


# ---------------------------------------------------------------------------
# License actions (form POST → redirect back to detail).
# ---------------------------------------------------------------------------

@router.post("/licenses/{serial_key}/disable", include_in_schema=False)
def license_disable(
    request: Request,
    serial_key: str,
    reason: str = Form(""),
):
    from urllib.parse import quote as _q
    user = _require_user_or_redirect(request)
    try:
        svc.disable_license(
            serial_key,
            reason=reason,
            actor=user,
            ip=_client_ip(request),
        )
    except svc.LicenseError as err:
        # NEW-SRV-5: surface the error back to the admin instead of
        # swallowing silently. The detail page already renders ?error=… .
        logger.warning("admin disable_license failed for %s: %s", serial_key, err)
        return RedirectResponse(
            f"/admin/licenses/{serial_key}?error={_q(str(err))}",
            status_code=303,
        )
    return RedirectResponse(f"/admin/licenses/{serial_key}", status_code=303)


@router.post("/licenses/{serial_key}/enable", include_in_schema=False)
def license_enable(request: Request, serial_key: str):
    from urllib.parse import quote as _q
    user = _require_user_or_redirect(request)
    try:
        svc.enable_license(
            serial_key, actor=user, ip=_client_ip(request),
        )
    except svc.LicenseError as err:
        logger.warning("admin enable_license failed for %s: %s", serial_key, err)
        return RedirectResponse(
            f"/admin/licenses/{serial_key}?error={_q(str(err))}",
            status_code=303,
        )
    return RedirectResponse(f"/admin/licenses/{serial_key}", status_code=303)


@router.post("/licenses/{serial_key}/reset", include_in_schema=False)
def license_reset(request: Request, serial_key: str):
    from urllib.parse import quote as _q
    user = _require_user_or_redirect(request)
    try:
        svc.reset(serial_key, ip=_client_ip(request), actor=user)
    except svc.LicenseError as err:
        logger.warning("admin reset failed for %s: %s", serial_key, err)
        return RedirectResponse(
            f"/admin/licenses/{serial_key}?error={_q(str(err))}",
            status_code=303,
        )
    return RedirectResponse(f"/admin/licenses/{serial_key}", status_code=303)


@router.post("/licenses/{serial_key}/extend", include_in_schema=False)
def license_extend(
    request: Request,
    serial_key: str,
    days: str = Form(...),
):
    from urllib.parse import quote as _q
    user = _require_user_or_redirect(request)
    try:
        days_int = int(days)
    except (TypeError, ValueError):
        days_int = 0
    if days_int <= 0:
        return RedirectResponse(
            f"/admin/licenses/{serial_key}?error={_q('מספר ימים לא תקין.')}",
            status_code=303,
        )
    try:
        svc.extend_license(
            serial_key, days=days_int, actor=user,
            ip=_client_ip(request),
        )
    except svc.LicenseError as err:
        logger.warning("admin extend failed for %s: %s", serial_key, err)
        return RedirectResponse(
            f"/admin/licenses/{serial_key}?error={_q(str(err))}",
            status_code=303,
        )
    return RedirectResponse(f"/admin/licenses/{serial_key}", status_code=303)


@router.post("/licenses/{serial_key}/delete", include_in_schema=False)
def license_delete(request: Request, serial_key: str):
    """Permanently delete a license key (and its activation history)."""
    from urllib.parse import quote as _q
    user = _require_user_or_redirect(request)
    try:
        svc.delete_license(
            serial_key, actor=user, ip=_client_ip(request),
        )
    except svc.LicenseError as err:
        logger.warning("admin delete_license failed for %s: %s", serial_key, err)
        return RedirectResponse(
            f"/admin/licenses?error={_q(str(err))}",
            status_code=303,
        )
    return RedirectResponse("/admin/licenses", status_code=303)


@router.post("/licenses/{serial_key}/edit", include_in_schema=False)
def license_edit(
    request: Request,
    serial_key: str,
    created_at:          Optional[str] = Form(None),
    activated_at:        Optional[str] = Form(None),
    expires_at:          Optional[str] = Form(None),
    license_type:        Optional[str] = Form(None),
    customer_first_name: Optional[str] = Form(None),
    customer_last_name:  Optional[str] = Form(None),
    customer_phone:      Optional[str] = Form(None),
    customer_email:      Optional[str] = Form(None),
    notes:               Optional[str] = Form(None),
):
    """Apply manual edits made from the "ערוך ידני" modal.

    Any field left untouched by the form arrives as ``None`` and is
    skipped by the service layer.  Empty strings on date fields clear
    the stored value (except ``created_at`` which is required).
    """
    user = _require_user_or_redirect(request)
    import logging as _log
    _log.getLogger(__name__).info(
        "edit_license: serial=%s  expires=%r  type=%r  activated=%r  created=%r",
        serial_key, expires_at, license_type, activated_at, created_at,
    )
    try:
        svc.edit_license(
            serial=serial_key,
            created_at=created_at,
            activated_at=activated_at,
            expires_at=expires_at,
            license_type=license_type,
            customer_first_name=customer_first_name,
            customer_last_name=customer_last_name,
            customer_phone=customer_phone,
            customer_email=customer_email,
            notes=notes,
            actor=user,
            ip=_client_ip(request),
        )
    except svc.LicenseError as exc:
        _log.getLogger(__name__).warning(
            "edit_license: rejected: %s", exc,
        )
    except ValueError as exc:
        _log.getLogger(__name__).warning(
            "edit_license: bad value: %s", exc,
        )
    return RedirectResponse(f"/admin/licenses/{serial_key}", status_code=303)


# ---------------------------------------------------------------------------
# Orders — list of every checkout attempt from the public website
# (``site/``). Pending entries are unpaid drafts; ``paid`` rows link
# back to the issued license via ``license_serial`` / ``license_id``.
# ---------------------------------------------------------------------------

@router.get("/orders", response_class=HTMLResponse, include_in_schema=False)
def orders_list(
    request: Request,
    q: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=200),
):
    user = _require_user_or_redirect(request)

    where = []
    params: list = []
    if q:
        like = f"%{q.strip()}%"
        where.append(
            "(COALESCE(customer_name, '') LIKE ? "
            "OR COALESCE(customer_email, '') LIKE ? "
            "OR COALESCE(customer_phone, '') LIKE ? "
            "OR COALESCE(license_serial, '') LIKE ? "
            "OR CAST(id AS TEXT) LIKE ?)"
        )
        params.extend([like, like, like, like, like])
    if status:
        where.append("status = ?")
        params.append(status)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    offset = (page - 1) * limit

    with get_connection() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM orders{clause}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"""SELECT o.*,
                       p.name  AS plan_name,
                       l.serial_key AS license_serial_full,
                       l.expires_at  AS license_expires_at
                  FROM orders o
                  LEFT JOIN subscription_plans p
                         ON CAST(o.plan_key AS TEXT) = CAST(p.id AS TEXT)
                  LEFT JOIN licenses l
                         ON l.id = o.license_id
                 {clause}
                 ORDER BY o.created_at DESC, o.id DESC
                 LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()

    items = [_row_to_dict(r) for r in rows]
    pages = max(1, (total + limit - 1) // limit)

    ctx = {
        "page_title": "Orders",
        "active_nav": "orders",
        "user": user,
        "filters": {"q": q or "", "status": status or ""},
        "page": page,
        "limit": limit,
        "total": total,
        "pages": pages,
        "items": items,
    }
    return _render(request, "admin/orders.html", ctx)


# ---------------------------------------------------------------------------
# Customers — unified view aggregated across orders + licenses
# ---------------------------------------------------------------------------

@router.get("/customers", response_class=HTMLResponse, include_in_schema=False)
def customers_list(
    request: Request,
    q: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=200),
):
    user = _require_user_or_redirect(request)
    from ..services import customers_service as csvc
    data = csvc.list_customers(q=q or "", page=page, limit=limit)
    ctx = {
        "page_title": "לקוחות",
        "active_nav": "customers",
        "user": user,
        "filters": {"q": q or ""},
        "page":  data["page"],
        "limit": data["limit"],
        "total": data["total"],
        "pages": data["pages"],
        "items": data["items"],
    }
    return _render(request, "admin/customers.html", ctx)


@router.get("/customers/{email}", response_class=HTMLResponse, include_in_schema=False)
def customer_detail(request: Request, email: str):
    user = _require_user_or_redirect(request)
    from ..services import customers_service as csvc
    # FastAPI URL-decodes the path segment, but emails with a ``+`` may
    # arrive as a space; normalize back.
    email_n = email.replace(" ", "+").strip().lower()
    profile = csvc.get_customer(email_n)
    if not profile:
        ctx = {
            "page_title": "לקוח לא נמצא",
            "active_nav": "customers",
            "user": user,
            "email": email_n,
        }
        return _render(request, "admin/customer_not_found.html", ctx)
    ctx = {
        "page_title": f"לקוח — {profile.get('name') or profile.get('email')}",
        "active_nav": "customers",
        "user": user,
        "customer": profile,
    }
    return _render(request, "admin/customer_detail.html", ctx)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@router.post("/reset-all", include_in_schema=False)
def reset_all(request: Request):
    """Wipe ALL licenses + history.

    Destructive, admin-only.  Clears:
      * ``events``        — every license event log row
      * ``activations``   — every per-machine activation record
      * ``trial_leads``   — every captured trial lead
      * ``trials``        — the one-trial-per-machine cap rows
      * ``orders``        — every checkout order
      * ``licenses``      — every license row (yearly / lifetime / trial)

    Preserved (NOT wiped):
      * ``subscription_plans`` — the plan catalogue (system + custom)
      * ``admin_users`` / ``admin_sessions`` — admin auth state

    After a successful wipe the admin lands back on the dashboard with
    a flash-style query parameter so the UI can confirm the reset.
    """
    from urllib.parse import quote as _q
    from ..database import get_connection
    user = _require_user_or_redirect(request)
    try:
        deleted_counts: Dict[str, int] = {}
        with get_connection() as conn:
            # Order matters — child tables before parents to avoid
            # FK violations even if the schema enables them.
            for tbl in (
                "events",
                "activations",
                "trial_leads",
                "trials",
                "machine_history",
                "orders",
                "licenses",
            ):
                try:
                    cur = conn.execute(f"DELETE FROM {tbl}")
                    deleted_counts[tbl] = int(cur.rowcount or 0)
                except Exception as exc:
                    logger.warning("admin reset_all: %s wipe failed: %s", tbl, exc)
                    deleted_counts[tbl] = -1
        logger.info(
            "admin reset_all by %s — deleted: %s",
            user, deleted_counts,
        )
    except Exception as exc:
        logger.exception("admin reset_all crashed: %s", exc)
        return RedirectResponse(
            f"/admin/dashboard?error={_q('איפוס נכשל: ' + str(exc))}",
            status_code=303,
        )
    return RedirectResponse(
        "/admin/dashboard?reset=ok",
        status_code=303,
    )


@router.get("/events", response_class=HTMLResponse, include_in_schema=False)
def events_list(
    request: Request,
    serial_key: Optional[str] = Query(None),
    machine_id: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
):
    user = _require_user_or_redirect(request)

    result = _evt.list_events(
        filters={
            "serial_key": serial_key,
            "machine_id": machine_id,
            "event_type": event_type,
            "since": since,
            "until": until,
        },
        page=page,
        limit=limit,
    )
    pages = max(1, (result["total"] + result["limit"] - 1) // result["limit"])

    ctx = {
        "page_title": "Events",
        "active_nav": "events",
        "user": user,
        "filters": {
            "serial_key": serial_key or "",
            "machine_id": machine_id or "",
            "event_type": event_type or "",
            "since": since or "",
            "until": until or "",
        },
        "page": result["page"],
        "limit": result["limit"],
        "total": result["total"],
        "pages": pages,
        "items": result["items"],
    }
    return _render(request, "admin/events.html", ctx)
