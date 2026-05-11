# Magnet Frame Pro — License Server

FastAPI + SQLite service that issues and validates license keys for the
Magnet Frame Pro desktop client, plus a session-protected admin
dashboard (Jinja + Bootstrap RTL) for managing them from the browser.

## Quick links

- **[RUN.md](RUN.md)** — install, run, log in, create a license, troubleshooting.
- **[API.md](API.md)** — full HTTP contract for `/license/*` and `/admin/api/*`.
- **[FRONTEND_CONTRACT.md](FRONTEND_CONTRACT.md)** — templates and context variables.

## High-level overview

```
server/
├── app/
│   ├── main.py              FastAPI entry point, mounts routes + static
│   ├── auth.py              Session-based admin login (pbkdf2_sha256)
│   ├── database.py          SQLite schema + migrations, seeds default admin
│   ├── schemas.py           Pydantic request/response models
│   ├── routes/
│   │   ├── licenses.py      /license/*  (public, desktop client)
│   │   ├── admin_pages.py   /admin/*    (HTML, session-protected)
│   │   └── admin_api.py     /admin/api/* (JSON, session-protected)
│   ├── services/            license_service, events_service, stats_service
│   ├── templates/admin/     Jinja templates (base.html + admin/*.html)
│   └── static/admin/        CSS + JS for the admin UI
├── admin/
│   └── create_license.py    CLI key generator (python -m admin.create_license)
├── requirements.txt         fastapi, uvicorn, starlette<0.47, jinja2, ...
├── RUN.md                   ← start here
├── API.md
├── README.md                (this file)
├── run_server.bat           one-click Windows launcher
├── create_test_licenses.bat one-click dev seeder
└── licenses.db              SQLite store (auto-created on first boot)
```

## Run in 30 seconds

```cmd
cd server
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then open **http://127.0.0.1:8000/admin/** and log in with the
credentials you configured in `server/.env` (see `.env.example` for
the required variables). See [RUN.md](RUN.md) for the full guide.
