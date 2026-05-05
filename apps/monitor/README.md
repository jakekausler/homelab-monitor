# homelab-monitor — monitor app

Python/FastAPI backend kernel. See the [root README](../../README.md) and
[design spec](../../docs/superpowers/specs/2026-05-04-homelab-monitor-design.md)
for architecture decisions.

## Database & migrations

### Configuration

| Env var | Default | Notes |
|---|---|---|
| `HOMELAB_MONITOR_DB_URL` | `sqlite+aiosqlite:///./data/homelab-monitor.db` | Any SQLAlchemy async URL works (tested: aiosqlite only) |
| `HOMELAB_MONITOR_AUTO_MIGRATE` | `true` | On startup, apply all pending migrations. Set `false` to refuse boot when pending migrations exist. |

### CLI commands

```bash
# Apply all pending migrations (idempotent)
hm migrate

# Show current revision and any pending migrations
hm migrate status
# Example output:
#   Current revision : a1b2c3d4e5f6 (0001_initial_schema)
#   Pending          : none

# List all known revisions in order
hm migrate history
# Example output:
#   a1b2c3d4e5f6  0001_initial_schema  (current)
```

### Direct Alembic usage

`hm migrate` is the recommended interface. If invoking `alembic` directly, the
`alembic.ini` `sqlalchemy.url` is a deliberate placeholder and must be
overridden:

```bash
# via env var
HOMELAB_MONITOR_DB_URL=sqlite+aiosqlite:///./data/homelab-monitor.db alembic upgrade head

# or via -x flag
alembic -x url=sqlite+aiosqlite:///./data/homelab-monitor.db upgrade head
```

### Schema state

The initial migration (`0001_initial_schema.py`) creates **19 tables**:

- **4 fully-defined** per the design spec: `users`, `sessions`, `audit_log`, `api_tokens`
- **15 minimal stubs** (columns: `id`, `name`/`key`, `created_at`) — later
  stages expand these via additive migrations

Never hand-edit the database schema. All changes must go through numbered
migration files in `apps/monitor/alembic/versions/`.

## Secrets store

### Configuration

- **`HOMELAB_MONITOR_MASTER_KEY`** — base64-encoded 32-byte master key (highest priority)
- **File fallback** — `/run/secrets/master-key`, also base64-encoded (read if env var is unset)
- Generation: `head -c 32 /dev/urandom | base64`
- Refuses to start if neither is set or if the decoded key is not exactly 32 bytes
- **`HOMELAB_MONITOR_REVEAL=1`** — required for `hm secrets get` to print plaintext (defense against accidental disclosure in shell history)

### CLI commands

```bash
# Store a secret (value piped from stdin; no positional value to avoid shell-history exposure)
echo -n 'my-token' | hm secrets set unifi_password --from-stdin

# Retrieve plaintext (requires HOMELAB_MONITOR_REVEAL=1)
HOMELAB_MONITOR_REVEAL=1 hm secrets get unifi_password
# Output: plaintext value on stdout, no other output

# List all secrets — name + created_at + rotated_at; values never appear
hm secrets list

# Replace an existing secret's value
echo -n 'new-token' | hm secrets rotate unifi_password --from-stdin

# Remove a secret
hm secrets delete unifi_password

# Re-encrypt all secrets under a new master key (read base64 from stdin)
# Prints old + new key fingerprints (HMAC-based, not the keys themselves)
echo "$NEW_KEY_B64" | hm secrets rotate-master --from-stdin
```

### Master key rotation operational notes

`hm secrets rotate-master` is atomic all-or-nothing. The implementation decrypts
every row with the OLD key first, then encrypts each with the NEW key, then
commits. If ANY row fails to decrypt (corrupted disk state, tampered ciphertext),
the rotation aborts before touching any data — the operation is "all rows or none."

If you encounter a rotation failure with `AES-GCM tag verification failed`, the
offending row must be deleted before rotation can proceed:

```bash
# Find the problematic secret (the error doesn't currently identify the row by name)
hm secrets list

# After identifying which row is corrupted (e.g., via `hm secrets get` on each):
hm secrets delete <corrupted-name>

# Then retry rotation
echo "$NEW_KEY_B64" | hm secrets rotate-master --from-stdin
```

After rotation, the old key can no longer decrypt any row — `hm secrets get` will
fail with `AES-GCM tag verification failed` until the env var is updated to the
new key.

### Crypto details (for auditors)

- **AEAD**: AES-256-GCM
- **KDF**: HKDF-SHA256 with per-row 16-byte salt and HKDF info =
  `b"homelab-monitor/secrets/v1/" + secrets.id` (UUIDv7)
- **Per-encryption nonce**: 12 random bytes; never reused
- **Storage**: `ciphertext` column holds `base64(nonce||ciphertext||tag)`; `kdf_salt`
  is a separate BLOB column; `id` (UUIDv7) is bound into HKDF's info parameter so
  the key derivation is unique per row
- **Audit log**: every set/rotate/delete/rotate-master writes a row to `audit_log`
  with metadata only (name, row count) — no plaintext values ever appear in audit
  columns
