# Magnet Frame Pro — License Server API

Public HTTP contract for the Magnet Frame Pro licensing system. Everything
an external admin UI or the desktop client needs is under `/license/*`.
The `/admin/*` surface exposed by `server/app/routes/admin.py` still
exists but is **not** required — you can build your own admin UI entirely
against the endpoints described here.

- Base URL in development: `http://127.0.0.1:8000`
- Base URL in production:  whatever you configure in `config.py`
  (e.g. `https://license.mydomain.com`)

All request bodies are JSON. All responses are JSON in the unified shape
described below.

---

## Unified `LicenseResponse` shape

Every `/license/*` endpoint returns this shape:

```json
{
  "success": true,
  "status": "active",
  "license_type": "yearly",
  "license_key": "MFP-2026-ABCD-1234",
  "serial_key": "MFP-2026-ABCD-1234",
  "machine_id": "abc123...",
  "activated_at": "2026-01-15T10:30:00+00:00",
  "expires_at":   "2027-01-15T10:30:00+00:00",
  "customer_name": "Acme Studio",
  "is_demo": false,
  "message": "הרישיון הופעל בהצלחה."
}
```

Field reference:

| Field           | Type    | Meaning                                                               |
|-----------------|---------|-----------------------------------------------------------------------|
| `success`       | bool    | Did the request complete logically? (True even for "license disabled" replies.) |
| `status`        | string  | Current state (see enum below). Always populated.                      |
| `license_type`  | string  | `trial_14_days` / `yearly` / `lifetime` / `null`.                      |
| `license_key`   | string  | Canonical key. Mirrors `serial_key` for convenience.                   |
| `serial_key`    | string  | Legacy alias (the desktop client reads this).                          |
| `machine_id`    | string  | Bound machine id, or `null`.                                           |
| `activated_at`  | string  | ISO-8601 UTC timestamp or `null`.                                      |
| `expires_at`    | string  | ISO-8601 UTC timestamp or `null` (`null` = never expires).             |
| `customer_name` | string  | Free-text label.                                                       |
| `is_demo`       | bool    | `true` when the client should drop to Demo mode as a result.           |
| `message`       | string  | Hebrew user-facing message.                                            |

Additional fields (`notes`, `created_at`) may also appear — ignore any
fields you don't need.

### `status` enum

| Value              | Meaning                                                |
|--------------------|--------------------------------------------------------|
| `active`           | License is bound to a machine and currently valid.     |
| `unused`           | License exists but has not been activated yet.         |
| `disabled`         | License manually disabled by the admin.                |
| `expired`          | License expiry date is in the past.                    |
| `not_found`        | No license exists with that serial key.                |
| `machine_mismatch` | Request came from the wrong machine.                   |
| `trial_used`       | A trial was already started on this machine.           |

### Status flow

```
          create                          activate
   (none) ─────────► unused ─────────────────────────► active
                         │                               │
                         │                               │  expires_at ≤ now
                         │                               ▼
                         │                            expired
                         │                               │
                         │     disable (admin)           │
                         ▼◄──────────────────────────────┘
                     disabled
                         │
                         │  enable (admin)
                         ▼
                (active if machine_id set, else unused)
```

---

## Error envelope

For input-validation errors (FastAPI 422) or domain errors the server
raises, the response carries an HTTP `4xx` status with a body like:

```json
{
  "detail": "מפתח הרישיון לא נמצא במערכת."
}
```

Logical failures that don't block the user (e.g. "validate found a
disabled license") are **not** HTTP errors — they come back with HTTP 200
and `success=true`, `is_demo=true`, and the `status` telling the client
exactly what happened.

---

## Endpoints

### 1. `POST /license/generate`

Create a new license key. **Not authenticated by the server itself — put
an auth layer (reverse proxy / API gateway) in front of this in
production.**

Request:

```json
{
  "license_type": "yearly",
  "days": 365,
  "customer_name": "Acme Studio",
  "notes": "Paid invoice #1234"
}
```

| Field           | Required                | Notes                                         |
|-----------------|-------------------------|-----------------------------------------------|
| `license_type`  | yes                     | `trial` / `yearly` / `lifetime`.              |
| `days`          | only for `yearly`       | Ignored for `trial` and `lifetime`.           |
| `customer_name` | no                      | Label shown in admin views.                   |
| `notes`         | no                      | Free-text notes stored on the license row.    |

Response (200):

```json
{
  "success": true,
  "status": "unused",
  "license_type": "yearly",
  "license_key": "MFP-2026-AB12-CD34",
  "serial_key":  "MFP-2026-AB12-CD34",
  "machine_id": null,
  "activated_at": null,
  "expires_at": "2027-04-15T10:30:00+00:00",
  "customer_name": "Acme Studio",
  "is_demo": false,
  "message": "הרישיון נוצר בהצלחה."
}
```

curl:

```bash
curl -X POST http://127.0.0.1:8000/license/generate \
  -H "Content-Type: application/json" \
  -d '{"license_type":"yearly","days":365,"customer_name":"Acme Studio"}'
```

---

### 2. `POST /license/activate`

Bind an existing serial to a machine. Called by the desktop client on
first activation.

Request:

```json
{
  "serial_key": "MFP-2026-AB12-CD34",
  "machine_id": "abc123..."
}
```

Response (200):

```json
{
  "success": true,
  "status": "active",
  "license_type": "yearly",
  "license_key": "MFP-2026-AB12-CD34",
  "machine_id": "abc123...",
  "activated_at": "2026-04-15T10:30:00+00:00",
  "expires_at":   "2027-04-15T10:30:00+00:00",
  "customer_name": "Acme Studio",
  "is_demo": false,
  "message": "הרישיון הופעל בהצלחה."
}
```

Failure cases (raised as HTTP 400):

- `"מפתח הרישיון לא נמצא במערכת."` — unknown serial.
- `"הרישיון הופעל כבר על מחשב אחר. ניתן לאפס דרך התמיכה."` — bound elsewhere.
- `"תוקף הרישיון פג."` — license already expired.

If the license is disabled, the server returns HTTP 200 with
`status="disabled"` and `is_demo=true`.

curl:

```bash
curl -X POST http://127.0.0.1:8000/license/activate \
  -H "Content-Type: application/json" \
  -d '{"serial_key":"MFP-2026-AB12-CD34","machine_id":"abc123"}'
```

---

### 3. `POST /license/validate`

Heartbeat validation called by the desktop client. **Never throws for
logical failures** — they come back as `success=true, is_demo=true` with
the actual `status` set.

Request:

```json
{
  "serial_key": "MFP-2026-AB12-CD34",
  "machine_id": "abc123..."
}
```

Response when the license is disabled (HTTP 200):

```json
{
  "success": true,
  "status": "disabled",
  "license_type": "yearly",
  "license_key": "MFP-2026-AB12-CD34",
  "machine_id": "abc123...",
  "activated_at": "2026-04-15T10:30:00+00:00",
  "expires_at":   "2027-04-15T10:30:00+00:00",
  "customer_name": "Acme Studio",
  "is_demo": true,
  "message": "הרישיון בוטל — התוכנה עוברת למצב Demo"
}
```

curl:

```bash
curl -X POST http://127.0.0.1:8000/license/validate \
  -H "Content-Type: application/json" \
  -d '{"serial_key":"MFP-2026-AB12-CD34","machine_id":"abc123"}'
```

---

### 4. `POST /license/start-trial`

Create a 14-day trial for a specific machine. Fails with HTTP 400 if a
trial was already started on that machine (`"כבר הופעל ניסיון 14 יום
במחשב זה."`).

Request:

```json
{ "machine_id": "abc123..." }
```

curl:

```bash
curl -X POST http://127.0.0.1:8000/license/start-trial \
  -H "Content-Type: application/json" \
  -d '{"machine_id":"abc123"}'
```

---

### 5. `POST /license/disable`

Mark an existing license as disabled. Writes a `license_disabled` event
with both `reason` and `actor` so the audit log renders who disabled
what. **Protect this endpoint in production (see `/license/generate`).**

Request:

```json
{
  "serial_key": "MFP-2026-AB12-CD34",
  "reason":     "Refund #4567",
  "actor":      "admin@company.com"
}
```

Response (200): unified shape with `status="disabled"`, `is_demo=true`,
`message="הרישיון בוטל."`.

curl:

```bash
curl -X POST http://127.0.0.1:8000/license/disable \
  -H "Content-Type: application/json" \
  -d '{"serial_key":"MFP-2026-AB12-CD34","reason":"refund","actor":"admin"}'
```

---

### 6. `POST /license/enable`

Re-enable a previously disabled license. If the license was bound to a
machine, its status goes back to `active`; otherwise `unused`. Writes a
`license_enabled` event carrying the `actor`. **Protect in production.**

Request:

```json
{ "serial_key": "MFP-2026-AB12-CD34", "actor": "admin@company.com" }
```

curl:

```bash
curl -X POST http://127.0.0.1:8000/license/enable \
  -H "Content-Type: application/json" \
  -d '{"serial_key":"MFP-2026-AB12-CD34","actor":"admin"}'
```

---

### 7. `POST /license/reset`

Unbind a license from its machine (keeps the license row but clears
`machine_id` and flips status to `unused`).

Request:

```json
{ "serial_key": "MFP-2026-AB12-CD34" }
```

curl:

```bash
curl -X POST http://127.0.0.1:8000/license/reset \
  -H "Content-Type: application/json" \
  -d '{"serial_key":"MFP-2026-AB12-CD34"}'
```

---

### 8. `GET /license/info/{serial_key}`

Return the current state of a license in the unified shape. Useful for
an admin UI's detail view.

Response when the serial does not exist:

```json
{
  "success": true,
  "status":  "not_found",
  "license_type": null,
  "license_key":  "MFP-2026-ZZZZ-9999",
  "serial_key":   "MFP-2026-ZZZZ-9999",
  "machine_id":   null,
  "activated_at": null,
  "expires_at":   null,
  "customer_name": "",
  "is_demo": true,
  "message": "מפתח הרישיון לא נמצא."
}
```

curl:

```bash
curl http://127.0.0.1:8000/license/info/MFP-2026-AB12-CD34
```

---

## Endpoint summary

| Method | Path                          | Purpose                                    |
|--------|-------------------------------|--------------------------------------------|
| POST   | `/license/generate`           | Create a new license key.                  |
| POST   | `/license/activate`           | Bind a serial to a machine.                |
| POST   | `/license/validate`           | Heartbeat-check a bound license.           |
| POST   | `/license/start-trial`        | Start a 14-day trial on this machine.      |
| POST   | `/license/disable`            | Disable a license (with reason + actor).   |
| POST   | `/license/enable`             | Re-enable a disabled license.              |
| POST   | `/license/reset`              | Unbind a license from its machine.         |
| GET    | `/license/info/{serial_key}`  | Look up the current state of a license.    |
| GET    | `/health`                     | Liveness probe (not under `/license/*`).   |

---

## Pointing the desktop client at an external server

Edit one line in `config.py`:

```python
LICENSE_SERVER_URL = "https://license.mydomain.com"
```

Everything else — API client, manager, UI dialogs — is already wired to
read this constant. No rebuild of other modules is required.

---

## Calling the API from a browser-based admin UI (CORS)

The server ships with a permissive `CORSMiddleware` (`allow_origins=["*"]`)
so an admin UI hosted on **any** origin can call `/license/*` from
JavaScript without a preflight rejection. Example:

```js
const base = "https://license.mydomain.com";

async function generateLicense({ licenseType, days, customerName }) {
  const res = await fetch(`${base}/license/generate`, {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      license_type:  licenseType,     // "trial" | "yearly" | "lifetime"
      days:          days,            // number or null
      customer_name: customerName || "",
      notes:         "",
    }),
  });
  return await res.json();
}
```

**Tighten this in production.** In `server/app/main.py` replace the
wildcard with your admin UI's exact origin:

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://admin.mycompany.com"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
```

### Auth

The three admin-oriented endpoints (`/license/generate`,
`/license/disable`, `/license/enable`) are **not** authenticated by the
server itself. That's intentional — the assumption is that your admin UI
sits behind an auth layer you control:

- A reverse proxy (nginx / Caddy / Cloudflare Access) enforcing auth.
- An API gateway validating a bearer token before proxying to this server.
- A cloud platform's built-in IAP (Google IAP, AWS IAM, etc.).

If you deploy the raw FastAPI server to the open internet without one of
those layers, **add authentication before doing anything else.**

---

## Events (audit trail)

Every successful or failed license operation writes a row to the
`events` table. Relevant event types for the new endpoints:

| `event_type`        | Emitted by           | `message` contents                      |
|---------------------|----------------------|-----------------------------------------|
| `license_created`   | `/license/generate`  | `type=... customer=...`                 |
| `license_disabled`  | `/license/disable`   | `actor=...; reason=...`                 |
| `disabled_license`  | `/license/disable`   | (legacy alias, kept for back-compat)    |
| `license_enabled`   | `/license/enable`    | `actor=...`                             |

You can surface these in an admin UI by hitting the existing
`GET /admin/api/events` route or by running your own SQL against the
`events` table.
