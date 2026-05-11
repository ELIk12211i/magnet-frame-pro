# -*- coding: utf-8 -*-
"""
server/app/services/email_service.py
------------------------------------
SMTP email delivery — used to send license keys to customers after a
successful purchase.

Configuration (via environment variables or server/.env):

    SMTP_HOST        e.g. smtp.gmail.com, smtp.sendgrid.net
    SMTP_PORT        587 (STARTTLS) or 465 (SSL); default 587
    SMTP_USER        SMTP username
    SMTP_PASSWORD    SMTP password / API key
    SMTP_FROM        "From" address (e.g. no-reply@magnetframe.co.il)
    SMTP_FROM_NAME   Display name for the From header
    SMTP_USE_TLS     "true"/"false"; default "true" (STARTTLS on port 587)
    SMTP_USE_SSL     "true"/"false"; default "false" (SMTPS on port 465)

If ``SMTP_HOST`` is empty the service logs a warning and returns False
without raising — so the payment flow keeps running while the operator
configures the mail credentials.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any, Dict, Optional

from .. import config as _cfg


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config readers — every call reads env so admins can rotate creds without
# restarting the process (SMTP creds are rarely on the hot path).
# ---------------------------------------------------------------------------
def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or getattr(_cfg, name, "") or default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "").lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _config() -> Dict[str, Any]:
    return {
        "host":      _env("SMTP_HOST"),
        "port":      int(_env("SMTP_PORT", "587") or 587),
        "user":      _env("SMTP_USER"),
        "password":  _env("SMTP_PASSWORD"),
        "from_addr": _env("SMTP_FROM"),
        "from_name": _env("SMTP_FROM_NAME", "Magnet Frame Pro"),
        "use_tls":   _env_bool("SMTP_USE_TLS", True),
        "use_ssl":   _env_bool("SMTP_USE_SSL", False),
    }


def is_configured() -> bool:
    c = _config()
    return bool(c["host"] and c["from_addr"])


# ---------------------------------------------------------------------------
# Template rendering — simple Python ``str.format`` placeholders so we do
# not introduce a Jinja dep just for email. The HTML template is stored in
# ``server/app/email_templates/`` and can be edited without touching code.
# ---------------------------------------------------------------------------
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "email_templates"


def _load_template(name: str) -> str:
    path = _TEMPLATES_DIR / name
    return path.read_text(encoding="utf-8")


def _render(template: str, **fields: Any) -> str:
    # We do not use ``str.format`` because customer values may contain
    # ``{`` characters (unlikely but possible). We do a safe manual
    # replace of ``{{name}}`` tokens instead.
    out = template
    for key, value in fields.items():
        token = "{{" + key + "}}"
        out = out.replace(token, str(value if value is not None else ""))
    return out


# ---------------------------------------------------------------------------
# Low-level send — raises on misconfig only if ``raise_on_error`` is True.
# ---------------------------------------------------------------------------
def _send_message(
    to_email: str,
    subject: str,
    html_body: str,
    text_body: Optional[str] = None,
    raise_on_error: bool = False,
) -> bool:
    if not to_email:
        logger.warning("email_service: refusing to send — empty to_email")
        return False

    c = _config()
    if not c["host"] or not c["from_addr"]:
        logger.warning(
            "email_service: SMTP_HOST/SMTP_FROM not configured — skipping "
            "send to %s (subject=%r)", to_email, subject,
        )
        return False

    msg = EmailMessage()
    msg["From"]    = formataddr((c["from_name"], c["from_addr"]))
    msg["To"]      = to_email
    msg["Subject"] = subject
    msg.set_content(text_body or _strip_html(html_body))
    msg.add_alternative(html_body, subtype="html")

    try:
        if c["use_ssl"]:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(c["host"], c["port"], context=ctx, timeout=30) as smtp:
                if c["user"]:
                    smtp.login(c["user"], c["password"])
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(c["host"], c["port"], timeout=30) as smtp:
                smtp.ehlo()
                if c["use_tls"]:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                if c["user"]:
                    smtp.login(c["user"], c["password"])
                smtp.send_message(msg)
        logger.info(
            "email_service: sent to %s (subject=%r)",
            _mask_email(to_email), subject,
        )
        return True
    except Exception as exc:
        logger.error(
            "email_service: send failed to %s (subject=%r): %s",
            _mask_email(to_email), subject, exc,
        )
        if raise_on_error:
            raise
        return False


def _mask_email(addr: str) -> str:
    """Return a privacy-preserving representation of ``addr`` for logs.

    ``alice@example.com`` → ``a***@example.com``. Empty strings pass
    through as ``"***"``.
    """
    if not addr or "@" not in addr:
        return "***"
    user, dom = addr.split("@", 1)
    return (user[:1] + "***@" + dom) if user else ("***@" + dom)


def _strip_html(html: str) -> str:
    """Very small HTML→text fallback for mail clients that don't render HTML."""
    import re
    text = re.sub(r"<style.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def send_license_delivery(
    to_email: str,
    customer_name: str,
    serial_key: str,
    plan_name: str,
    expires_at: Optional[str] = None,
    support_email: str = "support@magnetframe.co.il",
) -> bool:
    """Send the "your license is ready" email with the serial key.

    Returns True on successful delivery, False otherwise. Never raises —
    the caller (webhook handler) must not crash if mail fails; the
    license is still saved to the DB and the operator can resend later.
    """
    try:
        html_template = _load_template("license_delivery.html")
    except Exception as exc:
        logger.error("email_service: cannot load license_delivery.html: %s", exc)
        return False

    html = _render(
        html_template,
        customer_name=customer_name or "",
        serial_key=serial_key or "",
        plan_name=plan_name or "",
        expires_at=expires_at or "",
        support_email=support_email,
    )
    subject = "מפתח הרישיון שלך — Magnet Frame Pro"
    return _send_message(to_email, subject, html)
