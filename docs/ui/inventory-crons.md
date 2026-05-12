# Inventory → Crons (operator guide)

> Last updated: 2026-05-12 (STAGE-002-002). Reflects shipped state at commit [TBD].

## What this tab does

The Crons tab is the operator's view of the cron registry — the set of scheduled jobs the monitor knows about. Each row is a manually registered cron; auto-discovery is not yet implemented. From this tab you can browse, filter, add, edit, or archive cron entries. Archiving is a soft-delete: the row is retained in the database with `archived_at` set, heartbeat pings from that fingerprint start returning 404, but the entry can be restored. Only authenticated operator sessions can read or mutate cron data; all state-changing requests require a CSRF token.

## Route

`/inventory/crons` (list) → `/inventory/crons/{cron_id}` (detail)

URL search params on the list route persist filter state for back-navigation. Any filter change resets `page` to 1.

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `page` | integer | `1` | 1-based page number |
| `page_size` | integer | `100` | Passed through to `GET /api/crons`; capped at 500 server-side |
| `host` | string | — | Exact hostname filter |
| `integration_mode` | `observe` \| `heartbeat` \| `both` | — | |
| `state` | `unknown` \| `running` \| `ok` \| `failed` \| `late` | — | Matches `last_seen_state` on the cron row |
| `enabled` | boolean | — | |
| `q` | string | — | Case-insensitive substring match on name OR command |
| `include_archived` | boolean | `false` | When `true`, archived rows appear with an `archived` badge |

## Columns (list)

| Column | Source field | Display notes |
| --- | --- | --- |
| Name | `name` | Linked to the detail page (`/inventory/crons/{id}`) |
| Host | `host` | Plain text, muted foreground |
| Schedule | `schedule` / `cadence_seconds` | Shows the cron expression if `schedule` is non-null; otherwise `every {cadence_seconds}s` in monospace |
| Mode | `integration_mode` | `ModeBadge` component (title-case label) |
| State | `last_seen_state` | `StateBadge` component (title-case label) |
| Last OK | — | Placeholder `—`; populated in a future stage |
| Enabled | `enabled` / `archived_at` | `Yes` or `No`; archived rows show an additional inline `archived` chip |

## Toolbar filters

All filters are ANDed. Changing any filter resets `page` to 1.

| Control | Behavior |
| --- | --- |
| Search input | Substring match on `name` OR `command`; debounces 250 ms before updating the URL |
| Host select | Populated from the distinct `host` values in the current page result set; selecting "All hosts" clears the filter |
| Integration mode select | `Observe` / `Heartbeat` / `Both` or "All modes" |
| State select | `Unknown` / `Running` / `Ok` / `Failed` / `Late` or "All states" |
| Show archived checkbox | Toggles `include_archived`; when checked, archived rows are included in the list |
| Add cron button | Opens `AddCronModal` (CronForm in `create` mode) |

Mode and state options display as title-case in the UI (`Observe`, `Heartbeat`, etc.). The underlying query parameter values remain the lowercase enum strings (`observe`, `heartbeat`, etc.).

## Detail page

The detail page (`/inventory/crons/{cron_id}`) loads `GET /api/crons/{id}?include_archived=true` so archived crons are reachable directly by URL.

Layout sections:

- **Header**: cron name (h1), `ModeBadge`, `StateBadge`, optional `archived` chip, host and command sub-line. Right side shows either an **Archive** button (destructive, opens confirm modal) or a **Restore** button if the cron is already archived.
- **Edit card** (2/3 width on large screens): `CronForm` in `edit` mode pre-populated from the fetched cron. Save → `PATCH /api/crons/{id}` → navigate back to list with all filters cleared.
- **Heartbeat state card** (1/3 width): shows `current_state`, streak count, last OK, last fail, and next-due time from the joined `heartbeats_state` row. Displays "No pings received yet." if `state` is null.
- **Next runs card**: if the cron has a schedule expression, renders `SchedulePreviewForSaved` (calls `GET /api/crons/{id}/preview-runs?count=3`). For cadence-only crons displays "Cadence-based; no schedule preview."

## Editing a cron

`CronForm` is shared between the add modal (mode `create`) and the detail page (mode `edit`). Fields:

| Field | Validation | Default |
| --- | --- | --- |
| Name | Required, 1–200 chars | — |
| Host | Required, 1–200 chars | — |
| Command | Required, 1–2000 chars | — |
| Integration mode | `observe` \| `heartbeat` \| `both` | `observe` |
| Schedule mode radio | `Cron expression` or `Cadence (seconds)` — mutually exclusive | `schedule` if existing cron has a schedule; `cadence` if cadence > 0 |
| Schedule expression | Required when schedule mode is active; validated server-side via debounced preview call | Last-typed value preserved across mode swaps (ref) |
| Cadence (seconds) | Integer 1–86400 when cadence mode is active | Last-typed value preserved across mode swaps (ref) |
| Expected grace (seconds) | Integer 0–86400 | `300` |
| Enabled | Checkbox | `true` |

The XOR constraint (exactly one of schedule OR cadence) is enforced in the Zod schema via `superRefine` on the client and again at the Pydantic `model_validator` level on `CronCreate` / `CronUpdate`. Schedule expression validity (croniter parsing) is checked server-side only; the form surfaces the 422 message inline via `SchedulePreviewForExpr`.

Mode swaps use `key` props to force React to remount the schedule/cadence input, preventing browser type-coercion of cron expressions to numbers.

## Archiving (soft-delete)

Clicking **Archive** on the detail page opens `ConfirmDeleteModal`:

- Heading: "Archive cron?"
- Description explains heartbeats will return 404 until restored.
- Operator must type the exact cron name into the text input; the **Archive** button stays disabled until the typed value matches.
- Confirmed → `DELETE /api/crons/{id}` → sets `archived_at` timestamp (does NOT remove the row).
- Post-archive navigation lands on the list with `include_archived: true` so the just-archived row remains visible with its `archived` badge.

**Restoring**: open the archived cron's detail page (accessible via URL or the list with "Show archived" checked). The header shows a **Restore** button instead of Archive. Clicking it sends `PATCH /api/crons/{id}` with `{ "archived_at": null }`.

Audit log records `crons.delete` on archive and `crons.restore` on restore (distinct from the `crons.update` verb used for field edits).

## API surface

All endpoints require session authentication. Mutating endpoints (POST, PATCH, DELETE) also require a CSRF token enforced by `require_session()`.

| Method | Path | Purpose | Notes |
| --- | --- | --- | --- |
| GET | `/api/crons` | Paginated list with filters | Filters: `host`, `integration_mode`, `enabled`, `state`, `q`, `include_archived`, `page`, `page_size` |
| GET | `/api/crons/{id}` | Detail + joined heartbeat state | `?include_archived=true` to reach archived crons; 404 otherwise |
| POST | `/api/crons` | Create | 201 on success; 409 on duplicate `host` + `command` |
| PATCH | `/api/crons/{id}` | Partial update | Empty diff returns 200 with no audit row; `archived_at: null` triggers restore |
| DELETE | `/api/crons/{id}` | Soft-delete | Sets `archived_at`; 404 if already archived |
| GET | `/api/crons/{id}/preview-runs` | Next N runs for a saved cron | `?count=N` (1–10, default 3); 404 if cadence-only |
| GET | `/api/crons/preview-runs` | Next N runs for an unsaved expression | `?expr=<cron>&count=N`; `expr` is required |

Preview endpoints use the same `croniter` helper server-side so the UI preview cannot drift from backend validation.

## Heartbeat integration

The heartbeat receiver paths (`/hb/{id}/start`, `/hb/{id}/ok`, `/hb/{id}/fail`) read from the same `crons` row. Changes from this stage:

- The receiver returns 404 on archived crons (cross-stage edit to `_SELECT_CRON_SQL`).
- `last_seen_state` on `CronOut` reflects the latest heartbeat outcome and is displayed in the list State column and on the detail page header badge.

## Known limitations (this stage)

- Manual registration only — auto-discovery ships in STAGE-002-007.
- "Archive" terminology is a placeholder; STAGE-002-006 renames to "Hide" with the derived-state model.
- Bulk operations not supported.
- Last OK column in the table is a placeholder (`—`); populated in a future stage.
- Cron run history view deferred to STAGE-002-008 (log-scrape).
- "Install heartbeat wrapper" button on detail page deferred to STAGE-002-009.
- Host dropdown is populated from the current page result set only, not a dedicated `/api/crons/hosts` endpoint.

## Cross-references

- Schema migration: `apps/monitor/alembic/versions/0007_crons_canonical_and_indexes.py`
- Backend audit verbs: `crons.create`, `crons.update`, `crons.delete`, `crons.restore`
- Pydantic schemas: `apps/monitor/homelab_monitor/kernel/cron/schemas.py`
- Future redesign: `docs/superpowers/specs/2026-05-11-cron-derived-state-redesign.md`
- CronForm is reused by `AddCronModal` (mode `create`) and `CronDetail` (mode `edit`)
