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
