# Magnet Frame Pro — License Server — Run Guide

A quick guide to install, run, and manage the license server.
מדריך מהיר להתקנה, הרצה וניהול של שרת הרישיונות.

---

## 1. Install (one-liner)

**Windows (cmd.exe):**
```cmd
cd server && pip install -r requirements.txt
```

**Windows (PowerShell):**
```powershell
cd server; pip install -r requirements.txt
```

## 2. Run (development)

**Windows (cmd or PowerShell):**
```cmd
cd server
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Or simply double-click `server\run_server.bat`.

The server boots at **http://127.0.0.1:8000** and auto-creates
`server/licenses.db` on first boot. It also seeds one default admin account.

## 3. Log in to the dashboard

Open in your browser:

```
http://127.0.0.1:8000/admin/
```

Credentials are set via the `ADMIN_USERNAME` and `ADMIN_PASSWORD`
environment variables (or a `server/.env` file) **before the first boot**.

On first boot, if the `admin_users` table is empty, a single admin user
is seeded from those env vars. After that, the password stored in the DB
is the source of truth — changing the env var does NOT update an
already-seeded user. To rotate credentials later, edit the `admin_users`
table directly or use a migration script.

**Never deploy without setting your own credentials.**

## 4. Create a license from the UI

1. Click **יצירת רישיון** (Generate License) in the sidebar.
2. Pick the license type (`yearly` / `lifetime` / `trial`), fill customer name + email.
3. Click **צור רישיון** — the generated `MFP-YYYY-XXXX-XXXX` key appears on the same page. Copy it and send it to your customer.

You can also create licenses from the CLI:

```cmd
cd server
python -m admin.create_license yearly --count 3 --customer-name "Acme Inc."
```

or run the convenience batch file: `server\create_test_licenses.bat`.

## 5. Point the desktop app at a remote server

In `config.py` (project root, not in `server/`):

```python
LICENSE_SERVER_URL = "http://127.0.0.1:8000"   # ← change this
```

Replace with the public URL of the deployed server (e.g.
`https://license.mycompany.com`). The desktop client reads this once at
import time and uses it for every `/license/*` call.

## 6. Troubleshooting

### "Address already in use" / port 8000 busy

Stop any other process on port 8000 (check with `netstat -ano | findstr :8000`),
or run on a different port:

```cmd
python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

### `ModuleNotFoundError: No module named 'fastapi'` / `starlette`

You have not installed the requirements, or you installed them into a different
Python. Run:

```cmd
pip install -r server\requirements.txt
```

Confirm with `pip list | findstr fastapi`.

### `TypeError: TemplateResponse() takes from ... to ... positional arguments but ...`

Your Starlette version is newer than 0.47 and the legacy signature is gone.
Pin it explicitly:

```cmd
pip install "starlette<0.47"
```

This constraint is already written to `requirements.txt`; re-install it.

---

## Endpoints reference

See [`API.md`](API.md) for the full HTTP contract.
