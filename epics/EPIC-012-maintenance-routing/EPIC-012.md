# EPIC-012: Maintenance windows + alert routing rules

## Status: Not Started

## Overview

Build the configurable parts of the alert lifecycle: maintenance windows (scheduled silences pushed to Alertmanager via API) and the routing-rule editor (per-severity / per-tag → channel rules with a preview "if this alert came in, it would route to..."). Earlier epics shipped hard-coded routing (everything → in-process dashboard); this epic introduces the user-managed routing tier.

## Source documents

- Spec §3.1 (maintenance manager + alert dispatcher), §6.1 (`maintenance_windows`, `routing_rules` tables), §8.3 (routing rules description), §8.4 (lifecycle including maintenance windows), §9.2 (Maintenance windows + Settings → Routing screens).

## Stages (to decompose during epic Design phase)

| Likely stage | Theme |
|---|---|
| STAGE-012-001 | Maintenance window CRUD: API endpoints, `maintenance_windows` table writes, audit log, validation (start_at < end_at; rrule parses) |
| STAGE-012-002 | Maintenance window enforcement: a scheduled job that, at the start of each window, posts silences to Alertmanager via `/api/v2/silences` matching the window's scope (label selector or explicit target list); at end, expires the silences |
| STAGE-012-003 | Maintenance window UI: calendar + list views; "Schedule new" form with scope picker, recurrence picker (rrule helper), and a "preview affected targets" panel that previews matched alerts |
| STAGE-012-004 | Routing rules CRUD: API + table writes; rules are ordered (priority), match-conditions are `severity` + label selectors |
| STAGE-012-005 | Routing rules engine: dispatcher consults `routing_rules` for every incoming alert; replaces the hard-coded "all → inproc-dashboard" from STAGE-001-013 |
| STAGE-012-006 | Routing rules UI: drag-and-drop priority editor; "if this alert came in, it would route to ..." preview computed against the current rules |
| STAGE-012-007 | Routing rules dry-run: a "test alert" feature lets the user submit a synthetic alert payload and see exactly which rules match, in priority order |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:

- **Maintenance windows are reversible** — the silence in Alertmanager has the window's id as its `id` (or stored mapping); ending a window before its scheduled end expires the silence too.
- **Routing-rule changes apply only to new alerts.** Already-fired alerts are not re-routed. Document this clearly in the UI.
- **A "no-op" routing rule is rejected** — every rule must declare at least one channel. (Suppress-by-tag belongs in maintenance windows, not in routing.)

## Dependencies

- EPIC-001 (alerts, AM, channels framework).
- EPIC-005 (HA push channel) and EPIC-013 (SMTP) typically wired before this epic so the routing UI has interesting destinations to send to. If not, the routing UI degrades to "in-process dashboard only" with explanatory text.

## Notes

- rrule parsing: `python-dateutil`'s `rrulestr`. Common patterns are exposed as one-click ("every Saturday 2am-4am", "first Sunday of the month all day").
- The maintenance window "scope" supports either an explicit list of `targets.id`s OR a label-selector expression (e.g., `kind=container, integration=plex`). Label selectors are evaluated against the `targets` table at the moment the silence is created.
- Cross-cutting: when an auto-fix run is scheduled, the runbook orchestrator can opt-in to creating a short maintenance window for the affected targets to suppress alert noise during the fix. That cross-feature wire-up is a small follow-up after this epic lands.
