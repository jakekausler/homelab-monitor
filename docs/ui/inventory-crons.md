# Inventory → Crons (operator guide)

> Last updated: 2026-05-13 (STAGE-002-006).

## What this tab does

The Crons tab is the operator's view of the cron registry — the set of scheduled jobs the monitor knows about. Each row represents one logical cron, identified by its stable URL `fingerprint` (a 64-character hex digest of the canonical identity).

There is **no manual "Add cron" flow.** Crons enter the registry one of two ways:

1. **Wrapper `/register` handshake** — the heartbeat wrapper installed on a target host posts to `/api/register` and, if no row exists with that fingerprint, one is created. Repeat handshakes refresh `wrapper_last_seen_at` and emit a `crons.register` audit row.
2. **Disk auto-discovery** *(deferred to STAGE-002-007)* — the monitor will eventually scrape user/system crontabs on hosts it can reach and create rows directly.

From this tab you can browse, filter, edit, hide, or unhide cron entries. **Hide** is a display-and-notification suppression (per STAGE-002-005 D5) — `hidden_at IS NOT NULL` removes the cron from default views and silences its alert routing, but data capture (heartbeats, `/register` handshakes, future discovery, log-scrape, metrics) continues unchanged. Toggling **Show hidden** in the toolbar reveals hidden rows.

All routes require an authenticated session. Mutating endpoints (PATCH / DELETE) additionally require the CSRF token enforced by `require_session()`.

## Route

| Path | Component |
| --- | --- |
| `/inventory/crons` | List view (`CronsListPage` → `CronsToolbar` + `CronsTable` + `Pagination`) |
| `/inventory/crons/{fingerprint}` | Detail view (`CronDetail`) |

### Search params (list)

URL search params persist filter state across navigations. Any filter change resets `page` to 1.

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `page` | integer | `1` | 1-based page number |
| `page_size` | integer | `100` | Passed through to `GET /api/crons`; capped at 500 server-side |
| `host` | string | — | Exact hostname filter |
| `state` | `unknown` \| `running` \| `ok` \| `failed` \| `late` | — | Matches `last_seen_state` on the cron row |
| `enabled` | boolean | — | Reserved for future toolbar control; backend accepts it today |
| `wrapper_installed` | boolean | — | `true` → only rows where `wrapper_last_seen_at` is set; `false` → only rows where it is null |
| `q` | string | — | Case-insensitive substring match on `name` OR `command` |
| `include_hidden` | boolean | `false` | When `true`, hidden rows appear with a `Hidden` badge |

> **Removed in STAGE-002-004 / STAGE-002-005.** `include_archived` and `integration_mode` no longer exist anywhere in the URL surface or the API. Any external bookmark using them will be ignored by the validator.

## List view (`CronsList` + `CronsTable` + `CronsToolbar`)

### Toolbar filters

All filters are ANDed. Changing any filter resets `page` to 1.

| Control | Behavior |
| --- | --- |
| Search input | Substring match on `name` OR `command`; debounces 250 ms before updating the URL |
| Host select | Populated from the distinct `host` values in the current page result set; selecting "All hosts" clears the filter |
| State select | `Unknown` / `Running` / `Ok` / `Failed` / `Late` or "All states" |
| Wrapper select | "Any wrapper" / "Wrapper installed" / "No wrapper" — maps to the `wrapper_installed` query param |
| Show hidden checkbox | Toggles `include_hidden`; when checked, hidden rows are included in the list |

There is **no "+ Add cron" button.** The legacy `AddCronModal` and the create-mode of `CronForm` were removed in STAGE-002-004 (commit `9ff564f`).

### Empty state

When the list is empty:

> No crons yet. Crons will appear here once they are discovered or have registered a heartbeat.

### Columns

| Column | Source field | Display notes |
| --- | --- | --- |
| Name | `name` | Linked to the detail page (`/inventory/crons/{fingerprint}`) |
| Host | `host` | Plain text, muted foreground. Renders a secondary **Remote** badge inline when `source_path` is `null` (cron is known only via wrapper handshakes; the monitor has no direct file access). |
| Schedule | `schedule` / `cadence_seconds` | Monospace; cron expression if `schedule` is non-null, otherwise `every {cadence_seconds}s` |
| State | `last_seen_state` | `StateBadge` component (title-case label) |
| Last OK | — | Placeholder `—`; populated in a future stage (log-scrape / heartbeat indexing) |
| Wrapper | `wrapper_last_seen_at` | `✓` when set, `—` when null. `data-testid="wrapper-cell"` for tests. |
| Hidden | `hidden_at` | Empty when null; renders a `Hidden` badge when set (only visible when "Show hidden" is checked) |

The state and wrapper Select dropdown values are the lowercase enum strings on the wire (`ok`, `running`, etc.). The UI displays them as title-case via `titleCase()` in `badges.tsx`.

## Detail view (`CronDetail`)

The detail page loads `GET /api/crons/{fingerprint}?include_hidden=true` so hidden crons are reachable directly by URL.

### Header

```
[h1] {cron.name}   [StateBadge]  [Remote]  [Hidden]
{cron.host} · {cron.command}
```

- **State badge** uses `last_seen_state` from the cron row (same component as the list).
- **Remote badge** (secondary variant) appears when `source_path` is `null`.
- **Hidden badge** (muted variant) appears when `hidden_at` is non-null.

### Layout — 2×2 grid

On viewports ≥ 1024 px (Tailwind `lg:`) the four cards form a 2×2 grid:

```
┌──────────────────────────┬──────────────────────────┐
│ Heartbeat state          │ Disk source              │
│ (read-only)              │ (read-only)              │
├──────────────────────────┼──────────────────────────┤
│ Monitoring policy        │ Actions                  │
│ (editable)               │                          │
└──────────────────────────┴──────────────────────────┘
```

On viewports < 1024 px the grid collapses to a single column in this order:

1. Heartbeat state
2. Disk source
3. Monitoring policy
4. Actions

### Panel 1 — Heartbeat state (read-only)

Sourced from the `heartbeats_state` row joined onto the cron (`detail.data.state`). Renders the rows:

| Row | Source |
| --- | --- |
| Current | `state.current_state` (rendered via `StateBadge`) |
| Streak | `state.current_streak` (integer) |
| Last OK | `formatRelative(state.last_ok_at)` |
| Last Fail | `formatRelative(state.last_fail_at)` |
| Next due | `formatRelative(state.expected_next_at)` |
| Last duration | `{state.last_duration_seconds}s` (only when non-null) |
| Last exit code | `{state.last_exit_code}` (only when non-null) |
| Wrapper | "Wrapper last seen {relative}" (with absolute UTC tooltip) when `cron.wrapper_last_seen_at` is set; "No wrapper installed (heartbeats from ad-hoc curl)" otherwise |

When the state row is missing entirely (no pings ever received), the panel shows `No pings received yet.` plus the Wrapper row.

### Panel 2 — Disk source (read-only)

Sourced entirely from the cron row.

| Row | Source |
| --- | --- |
| Host | `cron.host` |
| Source path | `cron.source_path` (monospace) or `—` when null |
| Schedule | `cron.schedule ?? "every {cadence_seconds}s"` (monospace). When `cron.schedule_canonical` is set, it appears as a `title` attribute tooltip on the schedule value. |
| Command | `cron.command` (monospace, breaks on long lines) |

When `cron.source_path` is `null`, a blue info banner renders at the top of the panel (`data-testid="remote-banner"`):

> Remote cron on `{host}`. The monitor doesn't have direct file access to this host. Wrapper-based heartbeats are the only signal.

When `source_path` is set, the banner is omitted.

A "Last discovered" row will be added once STAGE-002-007 ships disk discovery and populates `last_discovered_at`.

### Panel 3 — Monitoring policy (editable)

Renders the edit-only `CronForm` pre-populated from the fetched cron. Fields:

| Field | Validation | Default |
| --- | --- | --- |
| Name | Required, 1–200 chars | from cron |
| Expected grace (seconds) | Integer 0–86400 | `300` |
| Enabled | Checkbox | `true` |

Submit issues `PATCH /api/crons/{fingerprint}` with only the editable fields. Success → `toast.success("Cron updated")`. Failure → `toast.error(<api message>)`.

The legacy create-mode fields (`host`, `command`, `integration_mode`, `schedule`, `cadence_seconds`, schedule/cadence radio) are gone. `schedule` and `cadence_seconds` are immutable at the UI layer; they are properties of the cron identity, not policy.

### Panel 4 — Actions

Two action rows separated by a thin divider.

**Hide / Unhide**

- For a visible cron: a destructive **Hide** button. Click → `DELETE /api/crons/{fingerprint}` → `toast.success("Cron hidden")` → navigate to `/inventory/crons` with `include_hidden=true` so the just-hidden row remains visible with its `Hidden` badge.
- For a hidden cron: a primary **Unhide** button. Click → `PATCH /api/crons/{fingerprint}` with `{ "hidden_at": null }` → `toast.success("Cron restored")`. The user stays on the detail page; the badge disappears on refresh.
- No confirmation modal. The legacy `ConfirmDeleteModal` (typed-name confirmation, "Archive cron?" heading) was removed in STAGE-002-004.

**Install heartbeat wrapper** *(disabled)*

- The button is rendered but `disabled`. Hovering surfaces a tooltip:

  > Local install ships in STAGE-002-009. Remote install requires cross-host work in EPIC-015 / EPIC-017.

- A wrapper-health badge will land in this row once STAGE-002-010 wires vmalert / Alertmanager labels for the wrapper.

## Mutation flow & toasts

All mutations on the detail page route through `sonner` toasts (top-right by default):

| Action | Endpoint | Success toast | Failure toast |
| --- | --- | --- | --- |
| Save policy | `PATCH /api/crons/{fingerprint}` | `Cron updated` | `Update failed` (or API error message) |
| Hide | `DELETE /api/crons/{fingerprint}` | `Cron hidden` | `Hide failed` (or API error message) |
| Unhide | `PATCH /api/crons/{fingerprint}` body `{hidden_at: null}` | `Cron restored` | `Restore failed` (or API error message) |

The `ApiError.message` from the typed API client is preferred over the generic fallback string.

## API surface

All endpoints require session authentication. PATCH / DELETE additionally enforce CSRF via `require_session()`.

| Method | Path | Purpose | Notes |
| --- | --- | --- | --- |
| GET | `/api/crons` | Paginated list with filters | Filters: `host`, `state`, `enabled`, `wrapper_installed`, `q`, `include_hidden`, `page`, `page_size` |
| GET | `/api/crons/{fingerprint}` | Detail + joined heartbeat state | `?include_hidden=true` reaches hidden crons; 404 otherwise |
| PATCH | `/api/crons/{fingerprint}` | Partial update | Body: `name`, `expected_grace_seconds`, `enabled`, `hidden_at`. Empty diff returns 200 with no audit row. `hidden_at: null` triggers `crons.unhide`. |
| DELETE | `/api/crons/{fingerprint}` | Soft-delete (hide) | Sets `hidden_at`. 404 if missing OR already hidden. Emits `crons.hide`. |
| GET | `/api/crons/{fingerprint}/preview-runs` | Next N runs for a saved cron | `?count=N` (1–10, default 3); 404 if cadence-only |
| GET | `/api/crons/preview-runs` | Next N runs for an unsaved expression | `?expr=<cron>&count=N`; `expr` is required |

## Hidden semantics (STAGE-002-005 D5)

`hidden_at IS NOT NULL` means **display + notification suppression ONLY**:

- Hidden crons are **excluded** from `GET /api/crons` by default; pass `include_hidden=true` to include them.
- Hidden crons remain reachable by direct URL on `GET /api/crons/{fingerprint}?include_hidden=true`.
- Hidden crons still receive heartbeats (the receiver does NOT 404 hidden rows — it 404s only when the fingerprint is unknown).
- Hidden crons still emit `crons.register` audit rows on wrapper handshake.
- Future disk discovery, log-scrape, and metrics will continue to capture hidden crons.
- Alert routing layers (vmalert / Alertmanager, landing in STAGE-002-010) will treat `hidden_at IS NOT NULL` as a silence signal.

The "Show hidden" checkbox on the toolbar is the only way to bring hidden rows back into the list view.

## Known limitations / pending

- **"Last discovered" field** on the Disk source panel — deferred to STAGE-002-007 (disk discovery).
- **Wrapper-health badge** in the Actions panel — deferred to STAGE-002-010 (vmalert / Alertmanager wiring).
- **"Install heartbeat wrapper"** button — currently disabled. Local-host installation lands in STAGE-002-009; remote installation requires cross-host transport from EPIC-015 / EPIC-017.
- **Remote-cron banner copy** will be updated (or the panel restructured) when cross-host file discovery ships in EPIC-015 / EPIC-017.
- **`Last OK` column** in the list table — placeholder (`—`) until heartbeat indexing or log-scrape populates a derived value.
- **Bulk operations** (multi-row hide/unhide, multi-cron policy edit) — not supported.
- **Host dropdown** is populated from the current page result set only; no dedicated `/api/crons/hosts` endpoint.

## Cross-references

- Cron identity / fingerprint canonicalization: `docs/architecture/cron-identity.md`
- Heartbeat receiver (`/hb/...`, `/api/register`): `docs/architecture/heartbeat-receiver.md`
- Backend audit verb taxonomy: `apps/monitor/homelab_monitor/kernel/api/routers/crons.py` docstring + `apps/monitor/homelab_monitor/kernel/cron/repository.py`
- Pydantic schemas (`CronOut`, `CronUpdate`, `CronListQuery`): `apps/monitor/homelab_monitor/kernel/cron/schemas.py`
- Frontend API hooks (`useListCrons`, `useGetCron`, `useUpdateCron`, `useHideCron`, `usePreviewSavedCron`, `usePreviewExpr`): `apps/ui/src/api/crons.ts`
- Frontend route registration + search-param validator: `apps/ui/src/routes/inventory/CronsList.tsx`

## History

- **STAGE-002-006** (this rewrite) — 4-panel detail layout, sonner toasts, wrapper-installed list filter, Remote badge, Wrapper column. Removed during Refinement: per-cron audit panel and supporting `/api/crons/{fp}/audit` endpoint (mental-model mismatch with fingerprint-based identity; schedule/command changes manifest as new cron rows, not updates).
- **STAGE-002-005** — Derived-state model: `archived_at` → `hidden_at` with display-and-notification-suppression semantics; `wrapper_last_seen_at` added.
- **STAGE-002-004** — API removal: `POST /api/crons` deleted; `AddCronModal` + create-mode of `CronForm` + `ConfirmDeleteModal` removed; audit verbs `crons.create` / `crons.delete` / `crons.restore` retired.
- **STAGE-002-002** — Pre-redesign layout: Add cron button, integration_mode column/filter, `ConfirmDeleteModal` typed-name confirmation, 3-column edit/heartbeat/next-runs detail card layout. All replaced by the current design.
