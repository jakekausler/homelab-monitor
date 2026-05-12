# EPIC-011: Discovery & suggestion engine UX

## Status: Not Started

## Overview

Polish and expand the discovery + suggestion subsystem. Earlier epics introduced ad-hoc suggestions (Docker socket events from EPIC-003, cron auto-discovery from EPIC-002, new-client suggestions from EPIC-007, etc.). This epic builds the unified suggestion-inbox UX, the periodic full-system rescan job, and the smarter "I noticed X" prompts that respect previous "ignore" decisions.

After this epic, the user has a single place ("Discovery & suggestions" screen) where every "found something new" event appears, and every dismissed suggestion stays dismissed.

## Source documents

- Spec §2 Q27 (auto-discovery decisions: A+B+C+D+E+F+H), §3.1 (discovery engine + suggestion engine), §6.1 (`suggestions` table), §9.2 (Discovery & suggestions screen).

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
