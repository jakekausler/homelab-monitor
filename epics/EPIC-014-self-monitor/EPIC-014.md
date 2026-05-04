# EPIC-014: Self-monitor + local-watchdog + healthchecks.io heartbeat

## Status: Not Started

## Overview

Operationalize the "who watches the watcher" subsystem from spec §2 Q20 (option E). Add the local-watchdog as a dedicated tiny container, integrate healthchecks.io public heartbeat, expand the self-monitor's metric set (queue depth, collector lag, db growth, memory), build the Self-status dashboard screen, and surface this whole subsystem as the most prominent reliability indicator on the Overview screen.

Cert/domain expiry monitoring also lands here (it's spiritually a "self-knowledge" feature: *can my services be reached over TLS?*) — it covers `/etc/letsencrypt/live/*` plus configurable additional domains.

## Source documents

- Spec §2 Q20 (option E: external healthchecks.io + local watchdog), §3.1 (self-monitor), §4.8 (self-monitoring flow), §3.2 (local-watchdog sidecar), §9.2 (Self-status screen).
- Project memory `reference_homelab_inventory.md` — the user's Route 53 + ip-update + nginx-configuator stack provides domain context.

## Stages (to decompose during epic Design phase)

| Likely stage | Theme |
|---|---|
| STAGE-014-001 | Cert-expiry collector: walks `/etc/letsencrypt/live/*` (read-only mount); emits `homelab_cert_expires_seconds{domain}`; default rules at 30d / 14d / 7d thresholds |
| STAGE-014-002 | External-domain TLS reachability collector: configured list of domains; performs an HTTPS handshake; verifies cert validity + expiry from the *served* cert (catches misconfigured nginx that serves a stale cert) |
| STAGE-014-003 | healthchecks.io integration: configured endpoint URL stored in secrets; the monitor pings it every 60s with the run id; if missed, healthchecks.io emails the user |
| STAGE-014-004 | local-watchdog container: pinned image (Alpine + curl + a tiny shell loop); pings the monitor's `/api/healthz` every 30s; after 3 consecutive failures, posts directly to Home Assistant push (with HA URL + token from a separate secrets file mounted into the watchdog only) |
| STAGE-014-005 | Self-monitor metric expansion: `homelab_self_queue_depth_*`, `homelab_self_collector_lag_seconds`, `homelab_self_db_size_bytes`, `homelab_self_memory_bytes`. The disk metric from STAGE-001-015A is already present; this stage adds the others |
| STAGE-014-006 | Self-status screen: queue depth, collector lag chart, db growth graph, disk usage breakdown (re-using STAGE-001-015A's data), healthchecks.io heartbeat status (last ping age, last response), local-watchdog status (last ping observed) |
| STAGE-014-007 | Overview self-status badge: top-right indicator (green/yellow/red) summarizing all of the above; clicking opens the Self-status screen |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:

- **The watchdog has its own credentials.** The HA bearer token used by the watchdog is a *separate* token from the main monitor's, scoped narrowly (notify only). Rotation of the main token does not require rotating the watchdog's; this is intentional separation.
- **healthchecks.io endpoint is optional.** If the secret isn't set, the public-heartbeat path is disabled and the Self-status screen shows "external heartbeat: not configured" rather than failing.
- **Watchdog never has write access** to anything except its own logs.
- **Cert-expiry tests use fixture certs** — never real Let's Encrypt for tests.

## Dependencies

- EPIC-001 (kernel, alerts, dashboard).
- EPIC-005 (HA push channel — the watchdog reuses the same `notify.mobile_app_jake_s_android` endpoint pattern).

## Notes

- The local-watchdog's container is intentionally minimal: shell + curl + a small loop. No Python, no FastAPI, no shared images. Goal: it must keep working even if the main monitor's image is broken.
- healthchecks.io free tier is sufficient (one check, email alerts). The user can pay for SMS/phone alerts if desired — that's out of scope.
- The cert-expiry collector cooperates with `nginx-configuator` (which manages cert renewal via certbot in the user's existing daily 12:00 cron) — we don't replace it; we just track expiry.
- Domain reachability checks must NOT use Pi-hole as a resolver — same circular-dependency rule as in EPIC-006. Use direct upstream resolvers (1.1.1.1 / 8.8.8.8).
