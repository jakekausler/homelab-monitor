# Authentication and authorization

This document is the canonical reference for the homelab-monitor auth model.
It covers the user-facing flow (browser sessions), the programmatic flow
(API tokens), and the security properties enforced by the implementation.

## Audience

- Operators bootstrapping a fresh deployment
- Contributors adding new endpoints or auth schemes
- Auditors reviewing the security model

## Threat model (in scope)

- Unauthenticated access from the LAN to internal endpoints
- Cross-site request forgery against authenticated sessions
- Brute-force password guessing
- Stolen session cookies (replay, fixation)
- Credential leakage via logs

## Threat model (out of scope, deferred)

- Multi-factor authentication (deferred per spec §7.1; v1 single-user homelab)
- Password complexity rules beyond length (deferred — bcrypt + 12-char floor)
- SSO / OIDC delegation (architecture leaves room; not implemented)
- DDoS at the edge (operator-provided reverse proxy)

## Auth schemes

The kernel supports two complementary schemes — the route's FastAPI
dependency picks which is acceptable:

| Scheme | Mechanism | CSRF | Use case |
|---|---|---|---|
| Session | Signed cookie + double-submit token | Yes | Browser / SPA |
| Token | `Authorization: Bearer <api-token>` | No (no ambient credential) | Alertmanager, cron, scripts |

### Session cookies

**Cookie shape.** The session cookie is `<session_id>.<truncated-HMAC>`, where:

- `session_id` is a uuid4 hex (32 chars). Persisted server-side in the
  `sessions` table with `user_id`, `expires_at`, `created_ip`, `csrf_token`.
- `truncated-HMAC` is HMAC-SHA256 over `session_id` using a key HKDF-derived
  from the master key with info-string `homelab.session.v1.hmac`. Truncated
  to 16 bytes (32 hex chars). 65 chars total.

**Cookie attributes.**

| Attribute | Value | Why |
|---|---|---|
| `HttpOnly` | yes | XSS cannot read |
| `SameSite` | `Lax` | CSRF protection at navigation level |
| `Secure` | controlled by `HOMELAB_MONITOR_HTTPS_ONLY_COOKIES` (default `true`) | Refuse over plaintext HTTP in prod |
| `Path` | `/` | Single-path scope |
| `Max-Age` | `HOMELAB_MONITOR_SESSION_TTL_DAYS` (default 7 days) in seconds | Idle TTL |

**Validation pipeline (each request).**

1. `AuthMiddleware._resolve_session` reads the cookie.
2. Verify HMAC (constant-time `hmac.compare_digest`). Failure → silently
   discard, falls through to unauthenticated.
3. Look up `session_id` in DB.
4. Check `is_session_expired(expires_at)`. Past → discard.
5. Look up `user_id` → `User`. Set `request.state.user`,
   `request.state.session`, `request.state.auth_kind = "session"`.

**CSRF.** Issued cookie `homelab_monitor_csrf` (HttpOnly=false, JS-readable).
On state-changing methods (`POST/PUT/PATCH/DELETE`), the
`require_session()` dependency reads `X-CSRF-Token` from the request
headers and constant-time compares to `session.csrf_token`. Mismatch → 403
`csrf_mismatch`. SameSite=Lax provides defense-in-depth at the navigation
level; the explicit token check is the primary defense.

**Login throttling.** `InProcessLoginRateLimiter` (5 failed attempts per 5
minutes per IP, sliding window). Successful logins do NOT consume budget.
Lost on process restart (acceptable per locked decision D4 — homelab single
process; an attacker who can restart the container has higher-privilege
exec already). Deferring SQLite-backed variant to a future stage.

**Session fixation defense.** Login deletes ALL prior sessions for the
authenticating user before minting the new one. Same pattern as
change-password rotation.

**Logout.** Requires session + CSRF. Deletes the row + clears both cookies.

**Change-password.** Requires re-verification of current password (defense
in depth: session compromise alone must not yield a password change). On
success, rotates ALL sessions for the user.

### API tokens

**Format.** `homelab_<env-prefix>_<base64url-30-bytes>`. Env-prefix
defaults to `prod`; can be overridden via `HOMELAB_MONITOR_TOKEN_PREFIX`
(set to `dev` in dev environments to detect cross-environment paste
mistakes).

**Storage.** SHA-256 hex of the plaintext in `api_tokens.hash`. UNIQUE
INDEX `api_tokens_hash_idx` enforces uniqueness — duplicate-insert bugs
fail at write time rather than producing two rows that both match at
lookup. Lookup is O(log n).

**Generation.** Plaintext is printed ONCE to stdout by
`hm api-token create`. CLI help warns the token is irretrievable.

**Validation.** `AuthMiddleware._resolve_token` reads `Authorization: Bearer ...`,
strips, and refuses empty plaintext early. Hashes and looks up by hash.
On match: best-effort `update_token_last_used`, set `request.state.token`,
`request.state.token_name`, `request.state.auth_kind = "token"`.

**CSRF immunity.** Tokens are CSRF-immune (no ambient credential — caller
explicitly opts in). Routes accepting tokens do NOT require X-CSRF-Token.

**Scopes.** Enum `Scope` defined in `kernel/auth/scopes.py`:

- `heartbeat:write` — heartbeat ingestion (STAGE-001-013)
- `alerts:ingest:write` — alert webhook (future stage)
- `read:status` — read-only status APIs

`require_token_scope(Scope.X)` enforces a SINGLE scope.
`require_user_or_token({Scope.X, Scope.Y})` accepts cookie OR token with
ANY (not all) of the listed scopes.

## Precedence

When both `Authorization: Bearer ...` and a session cookie are present,
the Bearer header wins and CSRF is NOT checked. This is deliberate —
programmatic callers must not be downgraded to a CSRF-protected flow that
demands a header they cannot easily produce.

If you add a new auth scheme, evaluate it BEFORE the cookie branch in
`AuthMiddleware.dispatch` and document the precedence in the inline
comment block.

## Auth-exempt endpoints

These NEVER require auth (set in `AUTH_EXEMPT_PATHS` in `middleware.py`):

- `GET /api/healthz`
- `GET /api/version`
- `GET /api/openapi.json`
- `GET /api/docs`, `GET /api/redoc`, `GET /api/docs/oauth2-redirect`
- `POST /api/auth/login`

`POST /api/auth/logout` is NOT exempt — the route requires `require_session()`
to pull the session and apply CSRF. Logout MUST run AuthMiddleware so the
session row can be deleted.

## CLI bootstrap

A fresh deployment has zero users. The CLI is the only path to create
the first user — there is no web bootstrap endpoint. The "first-run web
bootstrap" pattern was rejected per locked decision D5 — it tends to
survive for years and becomes a CVE.

```
hm user create <username>
```

Three independent signals tell the operator the system is pre-bootstrap:

1. CLI: `hm user list` returns empty.
2. Lifespan log: `lifespan.bootstrap_required` warning at startup.
3. `/api/version` returns `users_configured: false`.

After first user creation, `users_configured` flips to `true` (per-request
lookup, not cached).

## Audit logging

Every state-changing auth operation writes a row to `audit_log` in the
SAME transaction as the primary write:

| Verb | Trigger |
|---|---|
| `user.create` | `hm user create` |
| `user.delete` | `hm user delete` |
| `user.password_change` | `hm user passwd` OR `POST /api/auth/change-password` |
| `session.login` | `POST /api/auth/login` |
| `session.logout` | `POST /api/auth/logout` |
| `api_token.create` | `hm api-token create` |
| `api_token.revoke` | `hm api-token revoke` |

The `who` field is `str(user_id)` for self-initiated session events and
`<actor-username>` for CLI-initiated user-management actions.

## Access logging

`AccessLogMiddleware` emits `auth=session(user_id=N)` or
`auth=token:<name>` per request, enabling post-incident forensics without
exposing token plaintext.

## Configuration reference

| Env var | Default | Notes |
|---|---|---|
| `HOMELAB_MONITOR_HTTPS_ONLY_COOKIES` | `"true"` | Sets `Secure` on session and CSRF cookies. |
| `HOMELAB_MONITOR_SESSION_TTL_DAYS` | `7` | Idle session TTL in days. |
| `HOMELAB_MONITOR_BCRYPT_COST` | `12` | Bcrypt cost factor. Tests use `4` for speed. |
| `HOMELAB_MONITOR_TOKEN_PREFIX` | `"prod"` | Embedded in `homelab_<prefix>_<body>` API tokens. |
| `HOMELAB_MONITOR_MASTER_KEY` | unset | Base64-encoded master key; used to HKDF-derive the session-cookie HMAC subkey. |

## Removed

- `HOMELAB_MONITOR_DEV_AUTH` env var (placeholder from STAGE-001-010).
- `X-Auth: dev` header — no effect; ignored.
- `DevAuthMiddleware` class.
- `require_dev_auth` dependency.

## See also

- Stage spec: `epics/EPIC-001-foundation/STAGE-001-011.md`
- HTTP API reference: `apps/monitor/docs/api/README.md`
- Repository design: `apps/monitor/homelab_monitor/kernel/auth/repository.py`
