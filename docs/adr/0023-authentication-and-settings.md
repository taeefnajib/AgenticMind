# ADR-0023: Sign-in, profiles, roles and workspace settings

**Status:** Accepted.

## Context
The API had no authentication: anyone who could reach it could read every project, run graphs with the
server's keys and approve node packages. Users asked for a user profile with sign-in, and a settings page
to choose, among other things, which model the AI workflow designer (ADR-0022) uses. The model was only
configurable through environment variables.

## Decision
- **Accounts:**
  - Local username and password accounts (no external identity provider yet).
  - The **first account is created on the setup screen** of a new workspace and is an `admin`.
  - After setup there is no open sign-up: admins add people with a first password, which they can change in
    their profile.
  - Roles:
    - `member`: builds, runs, evaluates and deploys workflows;
    - `admin`: can also manage people, change workspace settings and install or approve node packages.
  - One shared workspace: everyone sees all projects. Workspaces and per-project sharing come later.
  - The workspace always keeps at least one active admin, and an admin cannot deactivate themselves.
    Deactivating someone signs them out.
- **Passwords:**
  - Hashed with stdlib scrypt (N=2^14, r=8, p=1) and a 16-byte salt per password; no new dependency.
  - At least 8 characters, and not the username.
  - Unknown usernames are checked against a dummy hash, so they take the same time and give the same
    message as a wrong password.
  - Failed sign-ins are rate limited: 5 failures per username or per client address within 15 minutes
    returns `429` with `Retry-After`. The limiter is in memory, per process.
- **Sessions:**
  - A random 256-bit token in an `HttpOnly`, `SameSite=Lax` cookie named after the browser-facing port
    (`am_session_5173`; ADR-0024): browsers share cookies across all ports of `localhost`, so two instances
    would otherwise sign each other out. `Secure` when `AGENTICMIND_COOKIE_SECURE=1`.
  - The database stores only the token's SHA-256.
  - Sessions slide: 14 days by default (`AGENTICMIND_SESSION_DAYS`), refreshed at most every 5 minutes.
  - Each sign-in issues a new token. Signing out deletes the session.
  - Changing your password, or "Sign out other browsers", revokes your other sessions.
  - Cookies were chosen over bearer tokens because `EventSource` (run events) and file download links
    cannot send headers.
- **Guard:** one ASGI middleware (`security.ApiGuard`) covers every `/api` route, so new routers are
  protected by default.
  - With `AGENTICMIND_AUTH=required` (the default until ADR-0032 made `off` the default), a valid session is needed everywhere except
    `/api/health` and the sign-in endpoints themselves.
  - **Cross-site writes:** `POST/PUT/PATCH/DELETE` requests that carry an `Origin` (or `Referer`) must
    come from the page's own host or an origin in `AGENTICMIND_CORS_ORIGINS`.
    - SameSite alone is not enough here: every `localhost:<port>` page counts as the same site, including
      deployed apps and other local dev servers.
    - The Vite dev proxy therefore keeps the browser's `Host` (`changeOrigin: false`).
- **Sign-in off:** `AGENTICMIND_AUTH=off` keeps single-user mode (no sign-in, everyone may change settings)
  and logs a warning at startup. The API test suite runs this way by default; `test_auth.py` turns sign-in
  on.
- **Workspace settings:**
  - Stored in an `app_settings` table, keyed by name with a JSON value and who changed it last.
  - The first setting is `assistant`: provider, model, API-key **secret name**, base URL and repair
    attempts (1–5). A saved value overrides the `AGENTICMIND_ASSISTANT_*` environment defaults, and
    "Reset" removes the override.
  - Only secret names are stored, never values (constraint 6); the key itself stays in `.env` or the
    environment.
  - Only admins can save, test or list models, because a base URL decides where the key is sent.
  - **Test connection** sends one short request with the unsaved form values.
  - **Browse** lists the provider's models via OpenAI-compatible `GET {base}/models`, checked against
    OpenRouter's API reference in Nov 2026: bearer key, `data[{id, name, context_length}]`.
- **Web:**
  - A gate shows setup, sign-in or the app. A 401 on any request brings the sign-in screen back with
    "Your session ended".
  - The toolbar avatar opens a menu: Profile, Settings, Sign out.
  - The Settings dialog has these sections:
    - Profile: name, email, password, sessions;
    - AI assistant;
    - People (admins only);
    - Appearance: theme.

## Verification
- **API tests** (`test_auth.py`, 8 tests):
  - first-run setup, then 401 everywhere; a second setup is refused;
  - the cookie is HttpOnly and SameSite=Lax, and only its hash is stored; salted scrypt;
  - login and logout; same message for unknown users; the rate limit returns 429 with `Retry-After`;
  - profile edits; a password change keeps this browser and signs out others;
  - admin-only routes return 403 for members (users, settings, plugins); deactivation revokes sessions;
    last-admin and self-deactivate guards; password reset;
  - cross-site `Origin`/`Referer` refused, own host and allowed origins accepted;
  - settings validation per field; the designer uses the saved model; test connection; model list; reset;
  - sign-in off mode.
- **Live in the browser:**
  - setup → app → Settings: browsed OpenRouter's 445 models, picked a model, Test connection succeeded
    (1.6 s) → Save;
  - added a member → signed out → signed in as the member: settings are read-only and People is hidden;
  - the assistant header showed the new model and answered with it.

## Consequences
- Existing databases get the new tables through `create_all`; nothing is migrated. A workspace that was
  open shows the setup screen once.
- There is no password-reset email: an admin resets passwords from People. There is no SSO/OIDC, MFA,
  personal access tokens for scripts, or audit log yet.
- Projects record no author or owner yet. Memory isolation is still per project and namespace.

## Revisit when
OIDC/SSO, multiple workspaces with per-project permissions, API tokens, an audit log, or a horizontally
scaled API (then the login limiter needs shared storage) are needed.
