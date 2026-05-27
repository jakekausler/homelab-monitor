# EPIC-011: Discovery & suggestion engine UX

## Status: Not Started

## Overview

Polish and expand the discovery + suggestion subsystem. Earlier epics introduced ad-hoc suggestions (Docker socket events from EPIC-003, cron auto-discovery from EPIC-002, new-client suggestions from EPIC-007, etc.). This epic builds the unified suggestion-inbox UX, the periodic full-system rescan job, and the smarter "I noticed X" prompts that respect previous "ignore" decisions.

After this epic, the user has a single place ("Discovery & suggestions" screen) where every "found something new" event appears, and every dismissed suggestion stays dismissed.

## Source documents

- Spec §2 Q27 (auto-discovery decisions: A+B+C+D+E+F+H), §3.1 (discovery engine + suggestion engine), §6.1 (`suggestions` table), §9.2 (Discovery & suggestions screen).

## Inherited carry-forwards from EPIC-003 (Docker)

EPIC-003 (Docker collector + per-container probes + label-based discovery + diun-style updates) landed an in-epic Pending Suggestions UI under `DockerIntegrationPage` as a TEMPORARY stub (STAGE-003-005 scaffolding + STAGE-003-012 interactive wiring). When EPIC-011 builds the global Discovery & Suggestions inbox, the following decisions become EPIC-011's responsibility:

### Carry-Forward 1: PendingSuggestionsPanel fate (link vs. remove)

The in-epic `apps/ui/src/routes/integrations/PendingSuggestionsPanel.tsx` is explicitly temporary. EPIC-011 must decide:

- **Option A (link):** Keep the panel under `/integrations/docker`; replace its content with a link/summary that navigates to the global inbox filtered by `kind=docker_*`. Useful if users still expect to see Docker-specific suggestions on the Docker integration page.
- **Option B (remove):** Delete the panel from the Docker integration page entirely. All Docker suggestions appear only in the global inbox. Cleaner separation; the global inbox is the single source of truth.
- **Option C (parallel):** Both surfaces exist during a transition window. Higher maintenance cost; unlikely the right call for a homelab tool.

**Recommendation pending EPIC-011 Design:** Option B (remove) — simpler mental model, matches the "single inbox" design intent.

### Carry-Forward 2: Endpoint path consolidation

STAGE-003-012 implemented Docker-namespaced POST endpoints:
- `POST /api/integrations/docker/suggestions/{id}/accept`
- `POST /api/integrations/docker/suggestions/{id}/customize`
- `POST /api/integrations/docker/suggestions/{id}/ignore`

EPIC-011 must decide whether the global inbox:
- **Option A (route via Docker-namespaced endpoints):** Global inbox introspects suggestion `kind` and routes to the right per-integration endpoint. Keeps existing endpoints; future discoverers each contribute their own namespaced endpoints.
- **Option B (migrate to generic `/api/suggestions/*` endpoints):** Introduce `POST /api/suggestions/{id}/accept|customize|ignore` (kind-agnostic at the API layer; internally dispatches by kind). Deprecate the Docker-namespaced ones over a release. Cleaner long-term API; requires migration shim for any client that uses the old endpoints.
- **Option C (both during transition):** Same as Option B but keep the Docker-namespaced endpoints alive longer for back-compat.

**Recommendation pending EPIC-011 Design:** Option B (migrate to generic) with a deprecation window — cleaner API as more discoverers come online.

### Carry-Forward 3: Customize modal flow

STAGE-003-012's Customize modal is a Docker-specific scaffold (multi-probe repeatable form rows). EPIC-011 STAGE-011-008 (Customize flow) will likely replace this with a generic discoverer-aware customize editor. The Docker modal can be:
- Kept as-is and reused by the global inbox when the kind is docker_*
- Replaced with the generic editor
- Deleted entirely if the generic editor covers all discoverer kinds

**Recommendation pending EPIC-011 Design:** Replace with generic editor in STAGE-011-008.

### Where to find related artifacts
- Code comments: `apps/ui/src/routes/integrations/PendingSuggestionsPanel.tsx`, `SuggestionCard.tsx`, `apps/ui/src/api/docker.ts`, `apps/monitor/homelab_monitor/kernel/api/routers/docker.py` (suggestion action endpoints) all carry EPIC-011 cross-reference comments.
- Regression checklist: `epics/EPIC-003-docker/regression.md` under "STAGE-003-012 — Suggestions panel interactions + EPIC-011 carry-forward".
- Earlier carry-forward context: `epics/EPIC-003-docker/EPIC-003.md` "Cross-epic carry-forward → EPIC-011" section.

### Acceptance criterion for EPIC-011
When EPIC-011's Design phase begins, it MUST explicitly resolve these three carry-forward decisions (panel fate, endpoint consolidation, customize modal) in its locked design decisions list and reflect them in STAGE-011-007 (inbox UI polish) + STAGE-011-008 (customize flow) deliverables.

## Stages (to decompose during epic Design phase)

| Likely stage | Theme |
|---|---|
| STAGE-011-001 | Discoverer plugin contract — refines the `Discoverer` plugin kind from STAGE-001-006's types: declares `interval`, returns a list of `Discovery` records with stable identity hashes |
| STAGE-011-002 | Suggestion store + dedup: a found-thing produces the same suggestion identity across runs (hash of kind + identifying fields), so we never spam the user with the same suggestion |
| STAGE-011-003 | Network discoverer: ARP scan / passive sniff of LAN /16; cross-references with Unifi DHCP table (from EPIC-007) and Pi-hole DHCP / DNS log (from EPIC-006) for friendly-named device tracking |
| STAGE-011-004 | Listening-port discoverer: enumerates `ss -tlnp` on the host; emits suggestions for unmonitored services |
| STAGE-011-005 | Mount discoverer: reads `/proc/mounts`; suggests probes for new mounts |
| STAGE-011-006 | Cert-file discoverer: walks `/etc/letsencrypt/live/*` and other configured paths; emits per-domain expiry suggestions |
| STAGE-011-007 | Suggestion inbox UI polish: keyboard-first triage (j/k to navigate, a to accept, i to ignore, c to customize); bulk actions; "ignored" archive view |
| STAGE-011-008 | Customize flow: when the user clicks "Customize" on a suggestion, the dashboard opens an editor with the auto-generated config pre-filled, with help text explaining each field |
| STAGE-011-009 | "Quiet hours" for suggestions: configurable times when new-suggestion notifications are suppressed (still appear in the inbox; just no push notification) |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:

- **Ignored suggestions stay ignored.** Re-discovery during a later scan must not re-suggest something the user explicitly ignored. (User can un-ignore from the archive.)
- **Network scans are passive by default** — we read ARP, DHCP, and DNS logs; we do not blast nmap probes unless the user opts in (privacy + politeness on shared networks).
- **Discoverers respect concurrency groups** — the network discoverer joins `unifi` if it queries the UDM; the mount discoverer is local and groupless.

## Dependencies

- EPIC-001 (suggestion table + first ad-hoc discoverer).
- EPIC-002 (cron discoverer).
- EPIC-003 (Docker discoverer).
- EPIC-006 (Pi-hole DHCP table).
- EPIC-007 (Unifi DHCP / client list).

## Notes

- New-device alerts (e.g., "an unknown MAC just connected to wifi") were partially covered in EPIC-007 STAGE-007-005. This epic federates that into the unified suggestion inbox.
- The cert-file discoverer is partially redundant with the cert-expiry collector (which lives in EPIC-014's SSL/cert plumbing). The two are coordinated: the discoverer finds new certs to monitor; the collector tracks expiry per known cert.
- Spec Q27 deliberately skipped the "onboarding wizard" (G). This epic preserves that — there is no wizard; auto-discovery + suggestions does the bootstrapping.
- **EPIC-002 cron derived-state redesign (2026-05-11):** Cron discovery **deliberately bypasses** the suggestion queue. Per `docs/superpowers/specs/2026-05-11-cron-derived-state-redesign.md`, crons are derived state — they appear in the registry as soon as discovery finds them on disk (or as soon as a heartbeat wrapper POSTs `/register`). The user's only "ignore" affordance for crons is the `hidden_at` column on the cron row, not the suggestion queue. EPIC-011's unified inbox covers other resource types (hosts, services, mounts, ports, certs, network devices) but NOT crons. If a downstream stage of EPIC-011 wants to add a "discovered-but-not-yet-acted-on" tab for crons later, that's a separate decision; the default is "no cron suggestions, period."
