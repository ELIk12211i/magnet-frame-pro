# -*- coding: utf-8 -*-
"""
server/admin/create_license.py
------------------------------
CLI tool that inserts unused license keys into the license database.

Usage:
    cd server
    python -m admin.create_license yearly             # one yearly license
    python -m admin.create_license yearly --count 5   # five yearly licenses
    python -m admin.create_license lifetime --count 2

The generated keys follow the ``MFP-YYYY-XXXX-XXXX`` format (English
letters + digits).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Make ``server/app/...`` importable when the script is run from either
# ``server/`` or ``server/admin/``.
# ---------------------------------------------------------------------------
_THIS_DIR   = Path(__file__).resolve().parent
_SERVER_DIR = _THIS_DIR.parent
for p in (_SERVER_DIR, _THIS_DIR):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)


from app.database import DB_PATH, init_db                      # noqa: E402
from app.services.license_service import (                     # noqa: E402
    LICENSE_TYPE_LIFETIME,
    LICENSE_TYPE_YEARLY,
    create_license,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate new license keys for Magnet Frame Pro.",
    )
    parser.add_argument(
        "kind",
        choices=[LICENSE_TYPE_YEARLY, LICENSE_TYPE_LIFETIME, "yearly", "lifetime"],
        help="License kind to create.",
    )
    parser.add_argument(
        "--count", type=int, default=1,
        help="Number of keys to create (default: 1).",
    )
    parser.add_argument(
        "--notes", type=str, default="",
        help="Optional notes attached to the generated licenses.",
    )
    parser.add_argument(
        "--customer-name", type=str, default="",
        help="Optional customer name.",
    )
    parser.add_argument(
        "--customer-email", type=str, default="",
        help="Optional customer email.",
    )
    parser.add_argument(
        "--days", type=int, default=None,
        help="Pin expiry in days (ignored for lifetime).",
    )
    args = parser.parse_args(argv)

    if args.count < 1 or args.count > 1000:
        parser.error("count must be between 1 and 1000")

    kind = args.kind
    if kind == "yearly":
        kind = LICENSE_TYPE_YEARLY
    elif kind == "lifetime":
        kind = LICENSE_TYPE_LIFETIME

    # Ensure schema exists on first run.
    init_db()

    print(f"DB: {DB_PATH}")
    print(f"Generating {args.count} {kind} key(s)...")
    print("-" * 42)
    for _ in range(args.count):
        row = create_license(
            license_type=kind,
            customer_name=args.customer_name,
            customer_email=args.customer_email,
            notes=args.notes,
            days=args.days,
            actor="cli",
        )
        print(row.get("serial_key"))
    print("-" * 42)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
