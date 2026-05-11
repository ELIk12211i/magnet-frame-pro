# Frontend Contract — Magnet Frame Pro License Server

This document describes **everything the frontend agent needs to know** to
build the admin dashboard templates. The backend routes are already
wired up — the frontend just needs to create the templates listed below,
consuming the context dictionaries exactly as documented.

---

## Directory layout you own

```
server/app/templates/
  admin/
    login.html
    dashboard.html
    generator.html
    licenses.html
    license_detail.html
    license_not_found.html   (optional — used for 404s)
    events.html
    (+ any _partials/base.html you want to share)

server/app/static/admin/
    (your own CSS/JS files)

server/app/static/dist/
    (build artefacts if you use a bundler)
```

**Do NOT edit any `.py` file** — the backend agent owns those.

Static files are served at `/static/...`. So a file at
`server/app/static/admin/main.css` is reachable at
`/static/admin/main.css`.

---

## Global context

Every page handler passes these keys in its context, so your base
template can assume they exist:

| Key          | Type            | Notes                                                        |
| ------------ | --------------- | ------------------------------------------------------------ |
| `request`    | FastAPI Request | provided automatically by Jinja2Templates                    |
| `page_title` | `str`           | e.g. `"Dashboard"`, `"Generate License"`                     |
| `active_nav` | `str`           | `"dashboard"` / `"generator"` / `"licenses"` / `"events"`    |
| `user`       | `str` or `None` | current admin username, `None` on the login page             |

All HTML pages should:
- Be RTL-aware where appropriate (the app is in Hebrew for end users,
  but the admin dashboard can be English — your call).
- Include CSRF-style protection? **Not required** — sessions are
  cookie-based with `SameSite=Lax`, and all write actions are POSTs
  from the admin's own domain.
- Log out via `POST /admin/logout` (a simple form is fine).

---

## Page-by-page contract

### 1) `admin/login.html`

Rendered by `GET /admin/login` and (with `error` set) by
`POST /admin/login` on failure.

Context:

```python
{
    "page_title": "Login",
    "active_nav": "login",
    "user": None,
    "error":    None | str,       # error message to show, else None
    "username": str,              # previously typed username (for re-render)
    "next":     str,              # optional redirect target after success
}
```

Required form:

```html
<form method="post" action="/admin/login">
  <input name="username" type="text"     value="{{ username }}" required>
  <input name="password" type="password" required>
  <input name="next"     type="hidden"   value="{{ next }}">
  <button type="submit">Sign in</button>
</form>
```

On success the backend sets the `admin_session` cookie and redirects to
`/admin/dashboard` (or to `next` if it starts with `/admin`).

---

### 2) `admin/dashboard.html`

Rendered by `GET /admin/` and `GET /admin/dashboard`.

Context:

```python
{
    "page_title": "Dashboard",
    "active_nav": "dashboard",
    "user":       str,
    "stats": {
        "total":           int,     # total licenses
        "active":          int,
        "unused":          int,
        "expired":         int,
        "disabled":        int,
        "reset":           int,
        "trials_total":    int,
        "activations_30d": int,
        "activations_7d":  int,
        "activations_24h": int,
        "events_24h":      int,
        "by_status":       dict,    # e.g. {"active": 5, "unused": 3, ...}
        "by_type":         dict,    # e.g. {"yearly": 5, "lifetime": 2, ...}
    },
    "recent_licenses": [  # up to 10, newest first
        {
            "serial_key":         "MFP-2026-ABCD-1234",
            "license_type":       "yearly",
            "status":             "active",
            "machine_id":         "...",
            "activated_at":       "2026-03-01T...Z" or None,
            "expires_at":         "2027-03-01T...Z" or None,
            "created_at":         "2026-03-01T...Z",
            "customer_name":      "",
            "customer_email":     "",
            "last_validation_at": "...",
            "disabled_at":        None or "...",
            "disabled_reason":    "",
            "disabled_by":        "",
            "notes":              "",
        },
        ...
    ],
    "recent_events": [  # up to 20, newest first
        {
            "id": 42,
            "serial_key": "MFP-2026-...",
            "machine_id": "...",
            "event_type": "activation_success",
            "message":    "...",
            "ip":         "...",
            "actor":      "admin" / "client" / "system" / "",
            "created_at": "2026-03-01T...Z",
        },
        ...
    ],
}
```

Link every license card / row to `/admin/licenses/{serial_key}`.

---

### 3) `admin/generator.html`

Rendered by both `GET /admin/generator` and `POST /admin/generator`.

Context:

```python
{
    "page_title": "Generate License",
    "active_nav": "generator",
    "user":       str,
    "error":      None | str,     # shown above the form on validation errors
    "created":    None | dict,    # license dict (see below) on success

    "form": {   # current/previous field values for re-rendering
        "license_type":   "yearly" | "lifetime" | "trial",
        "days":           "",      # string, to survive round-trips
        "customer_name":  "",
        "customer_email": "",
        "notes":          "",
    },
}
```

`created` on success has the full unified license shape:

```python
{
    "success":        True,
    "status":         "unused",
    "license_type":   "yearly",
    "license_key":    "MFP-2026-ABCD-1234",
    "serial_key":     "MFP-2026-ABCD-1234",  # same as license_key
    "machine_id":     None,
    "activated_at":   None,
    "expires_at":     None | "2027-...",
    "customer_name":  "",
    "customer_email": "",
    "is_demo":        False,
    "message":        "הרישיון נוצר בהצלחה.",
    "notes":          "",
    "created_at":     "2026-03-01T...Z",
}
```

Required form:

```html
<form method="post" action="/admin/generator">
  <select name="license_type">
    <option value="yearly"  {% if form.license_type == 'yearly'  %}selected{% endif %}>Yearly</option>
    <option value="lifetime"{% if form.license_type == 'lifetime'%}selected{% endif %}>Lifetime</option>
    <option value="trial"   {% if form.license_type == 'trial'   %}selected{% endif %}>Trial (14d)</option>
  </select>
  <input name="days"           type="number" value="{{ form.days }}">
  <input name="customer_name"  type="text"   value="{{ form.customer_name }}">
  <input name="customer_email" type="email"  value="{{ form.customer_email }}">
  <textarea name="notes">{{ form.notes }}</textarea>
  <button type="submit">Generate</button>
</form>
```

After success, show `created.license_key` prominently with a copy button.

---

### 4) `admin/licenses.html`

Rendered by `GET /admin/licenses`.

Query-string filters the handler understands: `q`, `status`, `type`,
`page`, `limit`.

Context:

```python
{
    "page_title": "Licenses",
    "active_nav": "licenses",
    "user":       str,
    "filters": {
        "q":      "",
        "status": "" | "active" | "unused" | "expired" | "disabled" | "reset",
        "type":   "" | "yearly" | "lifetime" | "trial_14_days",
    },
    "page":  int,
    "limit": int,
    "total": int,
    "pages": int,
    "items": [license dict, ...],   # same fields as recent_licenses above
}
```

Build filter form with GET method and link pagination via query string.
Each row should link to `/admin/licenses/{serial_key}`.

---

### 5) `admin/license_detail.html`

Rendered by `GET /admin/licenses/{serial_key}`.

Context:

```python
{
    "page_title": "License MFP-...",
    "active_nav": "licenses",
    "user":       str,
    "license": {
        "serial_key":         "MFP-2026-ABCD-1234",
        "license_type":       "yearly",
        "machine_id":         "..." or None,
        "status":             "active" | ...,
        "activated_at":       "..." or None,
        "expires_at":         "..." or None,
        "last_validation_at": "..." or None,
        "customer_name":      "",
        "customer_email":     "",
        "created_at":         "...",
        "disabled_at":        None or "...",
        "disabled_reason":    "",
        "disabled_by":        "",
        "notes":              "",
    },
    "activations": [  # newest first, status in {active, rejected, reset, expired}
        {
            "id": 1,
            "serial_key": "...",
            "machine_id": "...",
            "activated_at": "...",
            "last_seen_at": "...",
            "status":       "active",
            "ip":           "...",
            "user_agent":   "...",
            "notes":        "",
        },
        ...
    ],
    "events": [ event dict, ... ],   # newest first, up to 50
}
```

Required action forms on this page:

```html
<!-- Disable -->
<form method="post" action="/admin/licenses/{{ license.serial_key }}/disable">
  <input name="reason" type="text" placeholder="Reason">
  <button type="submit">Disable</button>
</form>

<!-- Enable -->
<form method="post" action="/admin/licenses/{{ license.serial_key }}/enable">
  <button type="submit">Enable</button>
</form>

<!-- Reset machine binding -->
<form method="post" action="/admin/licenses/{{ license.serial_key }}/reset">
  <button type="submit">Reset Machine</button>
</form>

<!-- Extend expiry by N days -->
<form method="post" action="/admin/licenses/{{ license.serial_key }}/extend">
  <input name="days" type="number" min="1" max="3650" required>
  <button type="submit">Extend</button>
</form>
```

All of these redirect back (303) to this same detail page after the
action completes.

---

### 6) `admin/license_not_found.html` (optional)

Rendered when the serial key doesn't exist; 404 response.

```python
{
    "page_title": "License Not Found",
    "active_nav": "licenses",
    "user":       str,
    "serial_key": "MFP-...",
}
```

Fallback behaviour: if the template is missing, the backend emits
`[template missing: admin/license_not_found.html]`.

---

### 7) `admin/events.html`

Rendered by `GET /admin/events`.

Query-string filters: `serial_key`, `machine_id`, `event_type`, `since`,
`until`, `page`, `limit`.

Context:

```python
{
    "page_title": "Events",
    "active_nav": "events",
    "user":       str,
    "filters": {
        "serial_key": "",
        "machine_id": "",
        "event_type": "",
        "since":      "",
        "until":      "",
    },
    "page":  int,
    "limit": int,
    "total": int,
    "pages": int,
    "items": [ event dict, ... ],
}
```

Known `event_type` values worth filtering on:

- `activation_success`, `activation_failed`, `activation_machine_mismatch`,
  `activation_already_used`
- `validation_success`, `validation_failed`, `validation_expired`
- `trial_started`, `trial_already_used`, `trial_expired`
- `yearly_expired`, `switched_to_demo`
- `license_created`, `license_disabled`, `license_enabled`,
  `license_reset`, `license_extended`, `disabled_license`, `reset_machine`
- `admin_login`, `admin_login_failed`, `admin_logout`

---

## Admin JSON API (optional — for frontend JS if needed)

All JSON endpoints live at `/admin/api/*` and respond with
`{"ok": true, "data": ...}` on success or a 4xx/5xx with a FastAPI
`{"detail": "..."}` body on failure. They require the same session
cookie that your Jinja pages use, so a fetch() from the dashboard is
automatically authenticated.

Endpoints (identical data shapes to those documented above):

- `GET  /admin/api/stats`
- `GET  /admin/api/licenses?q=&status=&type=&page=&limit=`
- `GET  /admin/api/licenses/{serial_key}`
- `POST /admin/api/licenses` — body: `{license_type, days?, customer_name?, customer_email?, notes?}`
- `POST /admin/api/licenses/{serial_key}/disable` — body: `{reason?}`
- `POST /admin/api/licenses/{serial_key}/enable`
- `POST /admin/api/licenses/{serial_key}/reset`
- `POST /admin/api/licenses/{serial_key}/extend` — body: `{days}`
- `GET  /admin/api/activations?serial_key=&machine_id=&page=&limit=`
- `GET  /admin/api/events?serial_key=&machine_id=&event_type=&since=&until=&page=&limit=`

---

## Fallback rendering

If you push the code before templates exist, each route returns a
`200 text/plain` body of the form:

```
[template missing: admin/dashboard.html] — ctx keys: active_nav, page_title, ...
```

so you can discover the context keys live, without breaking the
server.

---

## Admin login

- URL: `http://localhost:8003/admin/login`
- Username: set via `ADMIN_USERNAME` env var (or `server/.env`)
- Password: set via `ADMIN_PASSWORD` env var (or `server/.env`)

Credentials are only seeded on the first boot, when the `admin_users`
table is empty. The password is hashed with pbkdf2_sha256 and stored
in `admin_users` — changing env vars after the first boot does **not**
update the stored password (edit it via the DB or a migration script).

**Never deploy without setting your own credentials.**
