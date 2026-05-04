# homelab-monitor — Design Specification

**Status:** Approved (brainstorm session 2026-05-04)
**Author:** brainstorm session with Jake Kausler
**Audience:** future implementation sessions that lack the context of the original brainstorming dialogue. This document is the source of truth.

---

## 1. Purpose & scope

Build a self-hosted, modular service that detects and reports issues, anomalies, failures, warnings, and outliers across a single-user homelab, and that can — under tight safety constraints — auto-remediate certain classes of issues by invoking `claude --dangerously-skip-permissions -p <runbook-folder>`.

The homelab being monitored consists of:
- A primary Linux host (the host that runs the monitor itself; lives at `/storage/programs/homelab-monitor`).
- A Synology DS3622xs+ NAS (which hosts Surveillance Station with three Reolink cameras).
- Unifi gear: a UDM (which is the Unifi Network Application controller), a USP PDU Pro, a USW-48-PoE, two USW Flex, a USW-Lite-16-PoE, and two U7-Pro-Wall access points.
- All Docker containers running on the primary host (Home Assistant, Pi-hole, Mosquitto, Plex, Foundry VTT, Z-wave/Zigbee2MQTT bridges, Node-RED, Music Assistant, etc. — plus several services currently disabled via compose `profiles: ["disabled"]` such as Frigate and Ghost; the discoverer must handle both states. See `reference_docker_inventory.md` in the project memory for the full list).
- Host-native services (not in Docker): MariaDB and MySQL serve internal apps (`bills`, `library-organizer`). These are in scope and discovered via the listening-port and systemd-unit discoverers.
- Cron jobs (system + user) on the primary host.
- Mounts (notably `/rackstation/*` NFS/SMB mounts to the Synology).
- The pass-through ISP modem (AT&T) and WAN connection.
- TLS certificates (managed by an existing tool, `nginx-configuator`, with certbot/Let's Encrypt and AWS Route 53 DNS sync).

The monitor is for one human user (Jake) running on the LAN, with a web UI, push notifications via Home Assistant to mobile, Discord webhook delivery, and configurable email digests. It will be released open-source on GitHub and must therefore ship with safe defaults; this user's own deployment can layer host-specific overrides via a separate, private repository mounted as a volume.

### 1.1 Non-goals

- Multi-tenant or shared-account access. A small set of locally-defined users is supported, but the system is not designed for production multi-tenant operation.
- Cloud-hosted SaaS deployment.
- Kubernetes / non-Docker orchestrator support. The kernel must not preclude this, but no K8s collectors will be written in this project.
- A native mobile app. The web UI is responsive; alerts use Home Assistant's mobile-app notify integration.
- Deep ML-based capacity planning or time-series forecasting. Behavioral anomaly detection is delegated to Netdata's per-host k-means model and to vmalert rolling-baseline rules.
- Replacing existing operator tools (`nginx-configuator`, `/storage/scripts/cron/backup.sh`, `ip-update`). The monitor observes and integrates with these; it does not replace them.

---

## 2. Top-level architectural decisions

These were chosen during the brainstorm and are load-bearing.

| Decision | Choice | Rationale |
|---|---|---|
| Audience | Single user (Jake) on LAN, with web UI + notifications + CLI | Q1 |
| Deployment shape | Docker compose project on the primary host (the host is itself monitored) | Q2 |
| Detection categories | All of: service health, resource pressure, log anomalies, behavioral anomalies, security signals, backup integrity, cron results, network/connectivity, cert/domain expiry, update availability | Q4 |
| Cron monitoring model | Hybrid: log-scrape default for unmodified jobs (B), active heartbeat ping for opted-in jobs (A, gold standard), discovery+wrapper bootstrap helper (C). Cron registry stores expected cadence and grace period per job | Q5 |
| Storage | Hybrid: VictoriaMetrics for time-series, VictoriaLogs for logs, SQLite for operational state and audit. Chosen because VM/VL natively accept Prometheus exporters and have lightweight resource cost; SQLite is sufficient for state | Q6/Q7 + research |
| Pull vs push | Hybrid. Pull is the default for metrics and state (Prometheus exporters, REST APIs, SSH probes). Push is supported for cron heartbeats, webhooks, and any signal that cannot be polled | Q8 |
| Logs pipeline | Pragmatic mix of host bind-mounts (this host), syslog forwarding (Synology, UDM), and log shippers. VictoriaLogs as the store with explicit per-stream byte/line caps. Pattern matching extracts metrics; raw lines retained for forensics. Drain log clustering produces metric signatures | Q9 |
| Home Assistant integration | Bidirectional. Pull entity availability, history, batteries, automation/script failures, integration health. Custom HA-fired webhooks accepted as a push source. Push back to HA so HA can run scenes/announce on speakers when alerts fire. Connection: long-lived bearer token over LAN at `http://192.168.2.148:8123` | Q10 |
| Unifi integration | Three paths: (A) `unifi-poller`/`unpoller` Prometheus exporter for metrics, (C) UDM syslog forwarded to us, plus (B) direct UDM API as needed for custom checks. Controller runs on the UDM itself. All Unifi-derived signals in scope | Q11 |
| Synology integration | Three paths: (A) SNMP `snmp_exporter` with Synology MIB, (B) DSM API, (C) DSM syslog forwarding. All Synology signals in scope | Q12 |
| Docker monitoring | Host-wide signals via cadvisor + Docker socket (status, restart counts, exit codes, healthcheck status, image-update digests). Per-container "specific" probes configured by container labels (primary) with a per-service config file as override (A3). Diun-style image update notifications + dashboard "Pull & Restart" action; auto-update is OFF by default | Q13 |
| Auto-fix subsystem | Allow-list per alert type (A2). Each fixable issue class has a dedicated runbook folder with its own `CLAUDE.md` (B1). Claude runs as a dedicated low-privilege OS user (`homelab-fixer`) with curated file ACLs and a narrowly-scoped sudoers entry. Full audit; per-runbook rate-limit and cooldown; risk-tagged runbooks require dry-run+ack first; global kill switch in dashboard | Q14 |
| Notifications | Channels: Home Assistant push (mobile_app_jake_s_android), Discord webhook, email digest, in-dashboard live feed. Severity levels: info, warning, error, critical. Per-channel routing rules per severity. Lifecycle: acknowledge, snooze, maintenance windows, de-dup/grouping, auto-resolve | Q15 |
| Hybrid mature-tools model | Use VictoriaMetrics + VictoriaLogs + Alertmanager + vmalert (×2) + Karma + kthxbye + Grafana + Netdata as proven sidecars. The monitor service we write owns: collector framework, dispatcher, heartbeat receiver, runbook orchestrator, tool-effectiveness analyzer, discovery/suggestion engine, unified web UI | Q16 |
| Tool effectiveness analysis | First-class subsystem. Tracks alerts emitted, action rate (acked/dismissed/auto-fixed/escalated), de-dup overlap, unique-detection share per tool. Runs comparative shadow rules where applicable. Generates auto-recommendations after configurable observation windows | Q17 |
| UI screens | Overview, Alerts (Karma embed), Inventory (tabs for hosts/containers/devices/services/crons/mounts), Integrations (one sub-page per integration), Logs explorer, Metrics (Grafana embed), Runbooks, Auto-fix history, Discovery & Suggestions, Tool Analysis, Maintenance Windows, Self-Status, Settings (Channels/Routing/Digests/Auth/Secrets/Retention). Per-service drill-down sub-pages contributed by each integration plugin | Q18 |
| Visual style + frontend stack | Modern dashboard aesthetic. React 18 + Vite + TypeScript strict. Routing: TanStack Router. Server state: TanStack Query. UI primitives: Radix + Tailwind. Charts: Recharts or Tremor. Live: SSE/WebSocket. Forms: React Hook Form + Zod. Tests: Vitest + Testing Library + Playwright | Q18/Q19 |
| Backend stack | Python with strict typing (`pyright --strict` and/or `mypy --strict`) + FastAPI + asyncio. Multi-threading or `multiprocessing` for genuinely CPU-bound work. Plugin model: P3 hybrid — in-process Python plugins for built-ins, subprocess plugins for any-language and untrusted | Q19 |
| Self-monitoring | E: external healthchecks.io heartbeat for host/network death + a separate local-watchdog container that pings the main monitor and pages Home Assistant directly if the monitor dies | Q20 |
| ISP/WAN monitoring | All listed signals: WAN reachability, external IP tracking (integrating with the existing `ip-update` container), latency/jitter, multi-hop packet loss (mtr-style), speedtest (reuse UDM-side speedtest results), DNS resolution health (split: via Pi-hole and direct to 1.1.1.1), modem health (placeholder collector — future, awaiting modem model), CGNAT/inbound reachability probe, cert renewal status | Q21 |
| Retention | Per-stream retention rules with a disk-usage kill switch. Single configurable env `HOMELAB_MONITOR_DISK_BUDGET_GB` divided across VM/VL/SQLite. Self-monitor metric `homelab_self_disk_used_pct` triggers alerts at 70/85/95% with auto-shrink at the highest tier | Q22 |
| Backup | Hooks into existing `/storage/scripts/cron/backup.sh` daily 04:10. Pre-backup, monitor produces `sqlite3 .backup` snapshot and triggers VM/VL snapshot endpoints. Synology already replicates `/storage/backup/` to Backblaze. Master encryption key is NOT backed up via the normal flow; user keeps it in a password manager | Q22 |
| Auth | Local users + bcrypt + signed cookie sessions (single user to start; framework supports more). Confirm-on-destructive on kill switch, runbook real-run, secret rotate, channel/integration credential changes. API tokens (separate from sessions) for programmatic access (cron heartbeats), scoped & rotatable. Architecture leaves room for swapping to reverse-proxy/SSO later | Q23 |
| Secrets | Master key bootstrap via env (`HOMELAB_MONITOR_MASTER_KEY`) or `/run/secrets/master-key` file. AES-GCM with per-row nonce in SQLite. CLI subcommand `hm secrets set/get/list/rotate` and dashboard editor. Plugins access only via `ctx.secrets.get(name)` | Q24 |
| Testing | pytest with 100% coverage target on the kernel and aspirational on plugins. Integration tests against a `docker-compose.test.yml` rig that spins up real VM/VL/AM/Karma + monitor + fixture targets. End-to-end via Playwright. Lint/format: ruff + black. Pre-commit hook. CI: GitHub Actions | Q25 |
| Workflow | Brainstorm produces this spec; subsequently we run `epic-stage-setup` to scaffold the epic/stage structure (we do NOT use `writing-plans`). Implementation proceeds via `epic-stage-workflow` (Design → Build → Refinement → Finalize per stage) | Q26 |
| Discovery | Auto: Docker socket events, network scans, cron file scans, systemd unit / listening port scans, mounts, Pi-hole + Unifi DHCP tables. Suggestion engine surfaces "I noticed X" with Accept / Customize / Ignore. No dedicated onboarding wizard | Q27 |
| Digests | Fully configurable per recipient: cadence, sections, level of detail. Sections include: active alerts, resolved alerts, auto-fix activity, cron heartbeat report, backup status, cert/domain expiry, update availability, resource trends, tool scorecard, "what changed", noisy alert sources, plus extensibility | Q28 |
| SSH probes | Per-target dedicated low-priv users with key-restricted forced commands (`command="..."` in `authorized_keys`). Each probe declares its exact remote command; setup instructions captured per remote target | Q29 |
| Existing scripts | Generic public release defaults to A (observe, no script edits). This host applies B (heartbeat ping additions) and C (replace with managed equivalents) selectively. The codebase has a clean split: `homelab-monitor` (public) versus `homelab-monitor-overrides` (private, host-specific) | Q30 |
| Per-service deep-dive integrations | A dedicated epic explores specific integrations. The architecture must support adding integrations as plugin bundles | Q31 |
| Repo layout | Monorepo. The `homelab-monitor-overrides` host-specific layer is a separate, gitignored repo mounted as a config volume | Section 5 + post-Section-2 follow-up |
| AI-assisted development tooling | Code Review Graph (CRG) installed at project init. MCP server exposes ~28 tools (blast-radius, semantic search, etc.). `/code-review-graph:review-delta` and `/code-review-graph:review-pr` slash commands available during build/refinement/finalize phases | Post-Section-2 follow-up |
| Kernel + plugin layer | Two-layer architecture. Kernel (small, fixed): scheduler, plugin host, DB layer, secrets, auth, API router, lifecycle/health framework. Plugin layer (single contract): collectors, discoverers, enrichers, channels, runbooks, digest sections, integration bundles | Q on "everything as a plugin?" |

---

## 3. Components and responsibilities

The monitor service runs as a single Python/FastAPI process. Sidecars run as separate containers in the same compose project.

### 3.1 Backend services (single Python process)

| Component | Responsibility |
|---|---|
| **plugin host** | Discovers, loads, supervises plugins. Two execution paths: in-process Python plugins (built-ins, trusted) and subprocess plugins (any language; line-delimited JSON over stdout). Provides a stable `CollectorContext` (config, secrets, db, async http client, ssh client factory, vm-write client, vl-write client, structured logger). Trust tiers: `builtin`, `trusted`, `untrusted`. |
| **collector scheduler** | Asynchronous scheduler. Each collector declares `interval`, `timeout`, `concurrency_group`, and `run_kind` (ASYNC/THREAD/PROCESS). Enforces budgets, handles failures, emits per-run metrics (`homelab_collector_run_*`) to VM. |
| **discovery engine** | Periodic + event-driven discovery: Docker socket events, cron files, listening ports, systemd units, `/proc/mounts`, Unifi/Pi-hole DHCP, network scans of LAN /16. Emits "found new X" → suggestion queue. |
| **suggestion engine** | UI-facing. Stores suggestions, renders them in the dashboard, transitions them on user action (Accept creates target + collectors; Customize opens an editor; Ignore archives without re-suggestion). |
| **heartbeat receiver** | HTTP endpoints `/hb/<id>/start`, `/hb/<id>/ok`, `/hb/<id>/fail` — Healthchecks-style. Stores state in SQLite, exposes lateness metrics to VM. Per-id grace period and expected cadence. Auth via API tokens. |
| **alert ingestor** | Receives Alertmanager v2 webhook payloads. Records alerts to SQLite (history, fingerprint, source-tool tag). Dispatches to channels. Tracks per-alert outcome (acked / dismissed / auto-fixed / escalated) for the tool-analysis subsystem. |
| **alert dispatcher** | Per-channel adapters: Home Assistant push (POST `/api/services/notify/mobile_app_jake_s_android` with `{message, title, ...}`), Discord webhook, SMTP (queues into the digest builder OR sends "now" for critical, configurable), in-dashboard live feed via SSE. Per-severity routing rules + per-tag overrides. |
| **runbook orchestrator** | Owns auto-fix. Looks up runbook folder for an alert, checks allow-list, checks rate-limit + cooldown, dry-run gate for risky runbooks, spawns Claude as `homelab-fixer`, captures full transcript + exit, records audit. Honors global kill switch. |
| **tool-effectiveness analyzer** | For each tool (Netdata, vmalert, individual collectors, Alertmanager rules), aggregates: alerts emitted, action rate, dedup overlap, unique-detection share. Runs configurable shadow rules (e.g., parallel vmalert thresholds vs Netdata anomaly on the same metric set). Generates auto-recommendations after observation windows. Surfaces them in the Tool Analysis screen. |
| **digest builder** | Cron-driven. Builds per-recipient digests using a configurable section pipeline. Renders HTML + plaintext fallback. Includes dashboard links. Queues to SMTP. |
| **secrets store** | Encrypts/decrypts credentials with the master key. Provides `set/get/list/rotate/delete`. Used by every component that needs an API token or credential. CLI + dashboard manageable. |
| **maintenance manager** | CRUD for maintenance windows. Pushes silences to Alertmanager API on schedule. Supports recurring windows. Scopes via label selector or explicit target lists. |
| **self-monitor** | Watches the monitor's own internals: collector lag, queue depth, db growth, memory, disk usage breakdown. Pings healthchecks.io. Receives pings from the local-watchdog. Emits `homelab_self_*` metrics. |
| **API layer** | Typed FastAPI routes for every screen, plus an SSE/WebSocket channel for live updates. OpenAPI schema generated and consumed by the frontend via `openapi-typescript-codegen`. |
| **CLI** | `hm` subcommands: `secrets`, `backup`, `plugin`, `runbook`, `user`, `migrate`, `verify-config`. Same Python process, invoked via `docker exec homelab-monitor-monitor-1 hm <cmd>` or `python -m homelab_monitor.cli`. |

### 3.2 Sidecars (separate containers in the same compose)

| Sidecar | Role | Notes |
|---|---|---|
| **VictoriaMetrics** | Time-series database | Single binary, ~150–300 MB RAM idle. Snapshot endpoint used for backups. Scrape config generated by the monitor and reloaded via vmagent. |
| **VictoriaLogs** | Log store | Single binary, ~50–150 MB. Per-stream caps in config. |
| **vmagent** | Scrape & remote-write | Reads scrape config produced by the monitor (written to a shared volume). Streams to VM. Supports stream aggregation for downsampling. **Reload:** the monitor calls vmagent's `/-/reload` HTTP endpoint after writing a new config; vmagent runs with `-promscrape.config.strictParse=false` so partial generation never crashes the scraper. |
| **vmalert (metrics)** | Rule evaluator over VM | Writes alerts to Alertmanager. Rules under `deploy/vmalert/metrics/`. |
| **vmalert (logs)** | Rule evaluator over VL | Separate instance because vmalert evaluates rules against a single query backend (MetricsQL or LogsQL); the two instances target VictoriaMetrics and VictoriaLogs respectively. Rules under `deploy/vmalert/logs/`. |
| **Alertmanager** | Routing, dedup, silences | Karma + kthxbye talk to it. Webhook receiver pointed at the monitor's `/api/alerts/ingest`. |
| **Karma** | Alert lifecycle UI | Embedded into our dashboard via iframe. Provides ack/snooze/silence. |
| **kthxbye** | Auto-extends silences | Keeps a silence alive while its alert is still firing — yields "ack until resolved". |
| **Grafana** | Metrics dashboards | Embedded in our Metrics screen. Dashboards-as-code provisioned from `deploy/grafana/dashboards/`. |
| **Netdata agent** | Per-host anomaly detection | Runs as a Docker container in this compose project (the monitor host is also the monitored host; container has `/proc`, `/sys`, `/etc/os-release`, and `/var/run/docker.sock` mounted read-only). Streams metrics to VM via Prometheus remote-write. Provides 18-model k-means anomaly detection at the edge. Future: additional Netdata agents on remote hosts (Synology, etc.) stream to this same VM. |
| **vector** | Log shipper | Tails journald, docker logs, mounted log files. Writes to VL. |
| **local-watchdog** | Tiny separate container | Pings the monitor's `/healthz` every 30s. After 3 failures, posts a direct push to Home Assistant. |
| **fixer-runner** | Optional dedicated container | Runs as `homelab-fixer`; the orchestrator `docker exec`s into it to launch a `claude --dangerously-skip-permissions -p <runbook>` session. Alternative: native exec via `sudo -u homelab-fixer claude ...`. |

### 3.3 External dependencies

- **healthchecks.io** (free tier) — external heartbeat to detect host/network death. The monitor pings it every minute.
- **Synology backup hook** — the existing `/storage/scripts/cron/backup.sh` runs nightly at 04:10 and is auto-replicated to Backblaze on the Synology side. We integrate with it; we do not replace it.
- **`nginx-configuator`** at `/storage/programs/nginx-configuator/` — existing operator tool that manages nginx sites, certbot/Let's Encrypt, and Route 53 DNS. The monitor lives behind this; it does not manage its own TLS. The `sites-config.yaml` is a future-readable inventory of public-facing services.

### 3.4 Discovered/probed targets (external, partial list — discovery completes the picture)

Home Assistant • Pi-hole • Synology DS3622xs+ • Unifi UDM (controller) • Unifi switches/APs/PDU • All Docker containers • Host system metrics • Host-native MariaDB and MySQL • Crons (system + user) • Mounts (notably `/rackstation/*`) • External WAN endpoints (Cloudflare 1.1.1.1, Google) • Healthchecks.io endpoint • TLS certs (`/etc/letsencrypt/live/*`) • Container image registries (digest checks) • AT&T pass-through modem (when model is known) • Mosquitto • Z-wave/Zigbee2MQTT • Foundry VTT • Plex • Frigate (when enabled) • UPS (when present) • AWS Route 53 health (jakekausler.com record).

---

## 4. Data flows

### 4.1 Metric collection (pull)

```
collector scheduler — tick → plugin host → collector.run()
                                              │
                       ┌──────────────────────┴────────────────────┐
                       ▼                                            ▼
            scrape native exporter            run custom probe (HTTP/SNMP/SSH/Docker socket/HA API/etc.)
                       │                                            │
                       └────────── vmagent ◄────────────────────────┘
                                      │
                                      ▼
                             VictoriaMetrics
                                      │
                                      ▼
                                 vmalert (metrics)
                                      │
                                      ▼
                               Alertmanager
```

### 4.2 Heartbeat (push)

```
External cron — curl → /hb/<id>/start | /hb/<id>/ok | /hb/<id>/fail → heartbeat receiver
                                                                            │
                                                writes to SQLite (current state per id)
                                                writes to VM (lateness metric)
                                                                            │
                                                            vmalert evaluates "stale heartbeat"
                                                                            ▼
                                                                     Alertmanager
```

### 4.3 Logs

```
vector — tail → (journald, docker, mounted log files) → VictoriaLogs
                                                            │
                              periodic Drain clustering job — extracts → VM (per-signature metrics)
                                                            │
                                                vmalert (logs) evaluates rules
                                                            ▼
                                                      Alertmanager
```

### 4.4 Alert lifecycle

```
Alertmanager — webhook → alert ingestor (FastAPI)
        │                       │
        │             writes to SQLite (alerts, source_tool, fingerprint)
        │             dispatches via per-channel adapters
        │                       │
        │       ┌───────────────┼─────────────────┬───────────────┐
        │       ▼               ▼                 ▼               ▼
        │  HA push          Discord        digest queue     dashboard SSE
        │
        └── Karma (UI iframe) reads /api/v2/alerts for ack/snooze.
            kthxbye keeps silences alive until alerts resolve.
```

### 4.5 Auto-fix

```
Alert ingested → is alert in allow-list?
                       │
              ┌────────┴────────┐
             NO                YES
              │                 │
        dashboard          check rate-limit + cooldown
        "Run fix?"               │
        button               OK; claim runbook; check kill switch
                                 │
                              risk_tag == "risky"?
                                 │
                          ┌──────┴──────┐
                         YES            NO
                          │              │
                      dry-run        real run
                      requires           │
                      explicit ack       ▼
                      in dashboard  spawn `claude --dangerously-skip-permissions -p <runbook>`
                          │         as `homelab-fixer`
                          │              │
                          │       capture transcript, stdout, stderr, exit code
                          │              │
                          │       write runbook_runs + audit_log; emit metrics + dashboard event
                          │              │
                          ▼              ▼
                    user accepts dry → triggers real run
```

### 4.6 Discovery & suggestion

```
discovery engine (periodic + event-driven) → finds new container / cron / device / mount / cert
                                                        │
                                              insert into `suggestions`
                                                        │
                                              emit dashboard notification
                                                        │
                                  user accepts → creates target row + collector configs
                                  user customizes → opens editor
                                  user ignores → archived; not re-suggested
```

### 4.7 Tool effectiveness analysis

```
Every alert ingest tags `source_tool` + `fingerprint`.
Alert outcomes recorded (`alert_outcomes` table).

Periodic analyzer job:
  - per tool: alerts_emitted, action_rate
  - per fingerprint: which tools caught it (overlap matrix)
  - shadow rules: configured pairs of detectors run in parallel on the same metric set
  - results written to `tool_scorecards`

Dashboard "Tool Analysis" screen renders charts and emits auto-recommendations
(e.g., "Netdata caught 0 unique alerts in 90d; consider disabling for metric X").
Recommendations have an "Apply" action.
```

### 4.8 Self-monitoring

```
local-watchdog container — ping every 30s → main monitor /healthz
                                                       │
                                          if 3 consecutive failures:
                                          POST direct to Home Assistant push
                                                       │
main monitor — ping every 60s → healthchecks.io
                                                       │
                                       (external; if missed, healthchecks.io emails)
                                                       │
self-collector emits homelab_self_*: queue_depth, collector_lag, db_growth, mem, disk_used_pct
                                                       │
                                       vmalert rules → Alertmanager (with high severity)
```

---

## 5. Plugin / collector framework

The framework defines a stable, single contract so adding a new monitored thing is "drop a Python module" or "drop a script in a directory" — not a core change. This is the core of the modularity requirement.

### 5.1 Kernel vs plugin layer

**Kernel (small, fixed, not pluggable):** plugin host, scheduler, DB layer, secrets store, auth, API router, lifecycle/health framework.

**Plugin layer (everything else, single contract):**

| Plugin kind | Purpose | Examples |
|---|---|---|
| `collector` | Periodically gathers data; writes metrics; emits events | host (psutil), Pi-hole stats, HA entity availability, Synology SMART, container HTTP probe |
| `discoverer` | Finds new things on a schedule; emits suggestions | DockerDiscoverer, CronDiscoverer, MountDiscoverer, NetworkDiscoverer |
| `enricher` | Listens to alerts; attaches context | "for this Docker alert, attach last 50 log lines from the container" |
| `dispatcher_channel` | Outbound notification target | HAPushChannel, DiscordChannel, SMTPChannel |
| `runbook` | Folder + `CLAUDE.md` + scripts | `pihole-restart-loop`, `ha-mqtt-disconnected` |
| `digest_section` | Renders one section of a digest | "active alerts", "tool scorecard", "what changed this week" |
| `integration_bundle` | A package of collectors + discoverer(s) + default rules + UI panel + runbook | "Home Assistant integration", "Synology integration", "Unifi integration" |

### 5.2 Built-in `Collector` contract (Python, abridged)

```python
from typing import Protocol, ClassVar
from datetime import timedelta

class CollectorContext:
    config: CollectorConfig             # plugin's TOML/YAML config + secrets resolved
    db: SqliteRepository                # narrow facade — no raw cursors
    vm: MetricsWriter                   # write_gauge / write_counter / write_summary
    vl: LogsWriter                      # ingest a line/stream
    http: AsyncClient                   # shared httpx async client
    ssh: SshClientFactory               # opens connections by target_id; closed by ctx mgr
    ha: HomeAssistantClient | None
    secrets: SecretsResolver
    log: structlog.BoundLogger

class Collector(Protocol):
    name: ClassVar[str]
    interval: ClassVar[timedelta]
    timeout: ClassVar[timedelta]
    concurrency_group: ClassVar[str]    # collectors in the same group run serially
    run_kind: ClassVar[RunKind]         # ASYNC | THREAD | PROCESS

    async def run(self, ctx: CollectorContext) -> CollectorResult: ...
```

`CollectorResult` carries success/failure, metrics emitted count, and any structured events (suggestions, alerts to forward, log signatures).

### 5.3 Subprocess JSON protocol (line-delimited)

```json
{"type": "metric", "name": "pihole_blocked_today", "value": 2034, "labels": {"host":"pihole"}}
{"type": "event", "kind": "suggestion", "payload": {"new_container": "frigate", "image": "ghcr.io/..."}}
{"type": "log",    "stream": "pihole.dnsmasq", "line": "..."}
{"type": "heartbeat", "id": "rtlamr-watchdog", "state": "ok"}
{"type": "result", "ok": true, "summary": "scraped 12 metrics"}
```

Plugin process exit code interpretation: `0` = ok; non-zero → error metric + log entry. Stderr captured. Plugin host enforces timeout, kill, rate-limit.

### 5.4 Concurrency model

- Async collectors share the FastAPI loop (most are I/O bound — HTTP, SSH, sockets).
- `THREAD` for sync libraries (paramiko, snmp libraries that aren't async).
- `PROCESS` for CPU-heavy work (e.g., Drain log clustering on a big batch).
- `concurrency_group` prevents thundering herds — all collectors targeting the same UDM share group `unifi`, so the controller is never DDOSed.
- Per-collector failure budget: N consecutive failures → collector quarantined, alert fires, dashboard shows reason and offers "retry now".

### 5.5 Plugin discovery & install

- **Built-ins** ship in the monorepo under `apps/monitor/homelab_monitor/plugins/<kind>/<name>/`.
- **Third-party / overrides** — Python entry points (`homelab_monitor.collectors`, etc.) OR `/plugins/<kind>/<name>/` directories mounted as a volume from the `homelab-monitor-overrides` repo.
- **Subprocess plugins** live in their own folders with `plugin.yaml` manifests.
- **Trust tier** declared per plugin: `builtin`, `trusted`, `untrusted`. Untrusted plugins are forced to subprocess execution, granted no DB write access, no host mounts beyond their own dir, and only the secrets they explicitly declare in scope.

### 5.6 Configuration

Per-plugin TOML or YAML, validated against pydantic models.

```yaml
# plugins/pihole-stats.yaml (in-process Python plugin config)
plugin: pihole_stats
target: pihole-host
interval: 30s
url: http://localhost/admin/api.php
secret_ref: pihole_api_token
```

```yaml
# subprocess plugin manifest
manifest: 1
name: my-custom-probe
language: bash
command: ["/plugins/my-custom-probe/run.sh"]
interval: 60s
timeout: 30s
trust_level: trusted
```

### 5.7 Observability of plugins

- Every plugin run emits `homelab_collector_run_{success,duration,last_error_age}` so the system observes itself.
- Per-plugin scorecards in the tool-effectiveness analyzer.
- Live "plugin status" panel in the dashboard with last-run, next-run, last-error, "retry now" button.

---

## 6. Data model & storage

### 6.1 SQLite (operational state)

**DB layer choice:** SQLAlchemy Core (NOT the ORM) behind a narrow `SqliteRepository` facade. Plain dataclasses or pydantic models for row shapes; no implicit relationship loading. Chosen for: explicit SQL, easy mental model, predictable performance, plays cleanly with raw `sqlite3 .backup` snapshots, fewer footguns than ORM in a long-running daemon.

**Migrations strategy:** Alembic. On container startup, the monitor checks for pending migrations. Behavior is controlled by the env var `HOMELAB_MONITOR_AUTO_MIGRATE` (default `true` — auto-applies on boot; set to `false` to refuse to start until `hm migrate` is run manually).

Approximate tables (full DDL produced via Alembic migrations during implementation):

| Table | Purpose |
|---|---|
| `targets` | id, kind, name, labels (JSON), source, status, first_seen, last_seen. |
| `collectors` | id, plugin, target_id, interval, config (JSON), last_run, last_status, last_error. |
| `crons` | id, host, command, schedule, expected_grace, integration_mode (`observe` / `heartbeat` / `both`), last_seen_state. |
| `heartbeats_state` | id, current_state, last_ok_at, current_streak, expected_next_at. |
| `alerts` | id, fingerprint, source_tool, severity, status, opened_at, resolved_at, ack_at, ack_by, runbook_id, payload_json. |
| `alert_outcomes` | alert_id, outcome (`acked` / `dismissed` / `auto_fixed` / `escalated`), decided_at, decided_by. |
| `runbooks` | id, path, alert_match_patterns (JSON), risk_tag (`safe` / `risky`), dry_run_required, rate_limit_per_hour, cooldown_seconds. |
| `runbook_runs` | id, runbook_id, alert_id, mode (`dry` / `real`), prompt (path or inline), transcript_path, exit_code, started_at, ended_at, fixer_user, host. |
| `secrets` | id, name, ciphertext, kdf_salt, created_at, rotated_at. |
| `channels` | id, kind (`ha_push` / `discord` / `smtp` / `inproc_dashboard`), config_json_encrypted. |
| `routing_rules` | id, severity, tag_match (JSON), channel_id, priority. |
| `digest_configs` | id, recipient, cadence (`daily` / `weekly` / `custom`), sections (JSON), level_of_detail (JSON). |
| `maintenance_windows` | id, scope (target pattern or label selector), start_at, end_at, repeat (rrule), created_by. |
| `suggestions` | id, kind, payload_json, status (`pending` / `accepted` / `ignored` / `customized`), created_at. |
| `users` | id, username, bcrypt_hash, created_at. |
| `sessions` | id, user_id, expires_at, created_ip, csrf_token. |
| `api_tokens` | id, name, hash, scopes (JSON), created_at, last_used_at, rotated_at. |
| `audit_log` | id, who, what, when, before_json, after_json, ip. |
| `tool_scorecards` | id, tool, window, alerts_emitted, action_rate, dedup_overlap, unique_share, recommendation_text. |

Indexes: `alerts(fingerprint)`, `alerts(source_tool, opened_at)`, `runbook_runs(runbook_id, started_at)`, `targets(kind, name)`, `crons(host, command)`. WAL mode for concurrent readers. Backups via the existing `/storage/scripts/cron/backup.sh` hook (we publish a `sqlite3 .backup` snapshot to a path the backup script picks up; `.backup` is online-safe).

### 6.2 VictoriaMetrics

Schema by labels. Key metric families:

- `homelab_collector_run_*` — per collector run (success, duration_seconds, last_error_age_seconds).
- `homelab_heartbeat_*` — per-cron freshness, expected_interval, lateness.
- `homelab_target_up{kind,name}` — universal up/down metric.
- `homelab_cert_expires_seconds{domain}`.
- `homelab_image_update_available{container}`.
- All native exporter metrics: `node_exporter`, `cadvisor`, `unifi-poller`, `snmp_exporter` (Synology), `nut_exporter` (UPS when present), `smartctl_exporter`, `pihole-exporter`, etc.
- Netdata-streamed metrics (via Prometheus remote-write) keyed by `_netdata_*` namespace.
- HA-derived metrics from a Python collector hitting the HA Recorder API or websocket.

Per-stream retention rules with overrides per metric family. Default: 90 days at full resolution + 1 year downsampled at 5m via vmagent's stream aggregation. Disk kill switch: when budget threshold is hit, most aggressive retention shrinks first; alerts fire before drops.

### 6.3 VictoriaLogs

LogsQL streams keyed by `host`, `service`, `severity`. Sources:

- This host's journald via vector tailing journald → VL ingest.
- Synology syslog forwarded over UDP/TCP → VL ingest endpoint.
- UDM syslog forwarded → VL ingest endpoint.
- Docker container stdout/stderr via vector with the docker driver.
- Selected app logs via volume mounts (HA logs, Pi-hole logs).

Per-stream caps (lines/sec, bytes/day) configured per stream. Drain log clustering runs as a periodic in-process job that produces "log signature" metrics into VM, so anomaly detection works against signatures rather than raw lines.

### 6.4 Disk budget

Single configurable env: `HOMELAB_MONITOR_DISK_BUDGET_GB`. The orchestrator divides it by policy (default 60% VM / 30% VL / 10% SQLite + audit + runbook transcripts). These ratios are configurable in the application config (see §6.5); the values above are defaults. Self-monitor metric `homelab_self_disk_used_pct` raises:

- warning at 70%
- error at 85%
- critical at 95% with auto-shrink kicking in (drop oldest downsampled VM data first; then trim VL streams; SQLite and audit are last)

### 6.5 Application configuration

**Format:** YAML at `/config/homelab-monitor.yaml` (path overridable via `HOMELAB_MONITOR_CONFIG` env var). The host-specific override repo (see §11) supplies its own `homelab-monitor.yaml` and is mounted on top of the public default.

**What lives where:**

| Source | Contents |
|---|---|
| **Env vars** | Bootstrap-only items that must be available before file I/O is safe: `HOMELAB_MONITOR_MASTER_KEY`, `HOMELAB_MONITOR_CONFIG`, `HOMELAB_MONITOR_DISK_BUDGET_GB`, `HOMELAB_MONITOR_AUTO_MIGRATE`, `HOMELAB_MONITOR_LOG_LEVEL`. |
| **`homelab-monitor.yaml`** | Everything else: disk budget ratios, retention defaults, plugin discovery paths, sidecar URLs (VM/VL/AM/Karma/Grafana endpoints — internal docker network names by default), per-channel routing defaults, digest schedule cadences, healthchecks.io endpoint URL, watchdog tuning, allowed origins for the UI, session TTL, etc. |
| **SQLite (`secrets` table)** | All credentials (HA tokens, Discord webhooks, SMTP passwords, etc.). Never in the config file. Edited via dashboard or CLI. |
| **Per-plugin config** | TOML/YAML files under `/config/plugins/<kind>/<name>.{toml,yaml}` (also overridable by the override repo). Plugins reference secrets by `secret_ref:` name; the runtime resolves these via the secrets store. |

**Validation:** the application config schema is a pydantic model. On startup the monitor validates the merged config (public defaults + override layer) and refuses to start with a clear error if validation fails. `hm verify-config` runs the same check without starting.

**Reload semantics:** changes to plugin configs hot-reload the affected plugin (next scheduled tick re-reads). Changes to top-level `homelab-monitor.yaml` require a process restart and the dashboard surfaces a "config changed; restart pending" banner.

---

## 7. Security model

### 7.1 Authentication

- Local users + bcrypt + signed cookie sessions.
- Single user (Jake) at start; framework supports more.
- CSRF tokens on state-changing requests.
- Session expiry configurable (default 7 days idle).
- Sessions revocable via Settings → Auth.

### 7.2 Authorization & confirm-on-destructive

The following actions require an in-session confirm step (typed phrase or session-lifetime PIN):

- Toggling the auto-fix kill switch.
- Triggering a real (non-dry) runbook run.
- Editing or rotating a secret.
- Adding or removing an integration credential.
- Editing retention policies.
- Deleting any audit, channel, or routing-rule record.

API tokens (separate from cookie sessions) for programmatic access (cron heartbeats), with declared scopes (e.g., `heartbeat:write` for `/hb/*` endpoints only). Tokens rotatable from Settings.

### 7.3 Secrets

- Master key bootstrap via `HOMELAB_MONITOR_MASTER_KEY` env var, falling back to a `/run/secrets/master-key` file.
- AES-GCM with per-row nonce; key derived from master + per-row salt via HKDF.
- `secrets` SQLite table holds ciphertext; never logs decrypted values.
- Plugins access via `ctx.secrets.get(name)` only; raw store inaccessible to plugins.
- CLI: `hm secrets set/get/list/rotate/delete`. Dashboard editor with masked inputs.
- Secrets NEVER included in backups (master key is the user's responsibility, kept in a password manager).

### 7.4 Auto-fix isolation

The auto-fix subsystem is the single highest-blast-radius feature. Its constraints (project memory `project_autofix_safety_model.md`):

1. **Trigger:** allow-list per alert type only. Default = manual button in dashboard. Auto-trigger only for explicitly opted-in alert types.
2. **Scope:** dedicated runbook folder per issue class, each with its own `CLAUDE.md` describing allowed commands and intent. Never invoke Claude on broad/unrelated paths.
3. **Identity:** Claude runs as a dedicated low-privilege OS user (`homelab-fixer`) with curated file ACLs and a narrowly-scoped sudoers entry. Never as `jakekausler` or root.
4. **Audit:** every run logs alert ID, runbook path, prompt (or runbook hash), full transcript, stdout/stderr, exit code, started/ended timestamps, runbook hash, fixer user, host.
5. **Dry-run:** runbooks tagged `risky` must support a dry-run mode that produces a plan; the plan requires explicit user approval before a real run.
6. **Rate limit + cooldown:** max N runs per hour globally, per-runbook cooldown to prevent tight loops.
7. **Kill switch:** single dashboard control disables all auto-fix immediately. Killable mid-run via `docker kill` or POSIX signal.

Two execution flavors:
- **`fixer-runner` container** — separate container running as `homelab-fixer`, with the `claude` CLI installed; orchestrator `docker exec`'s in.
- **Native exec** — orchestrator uses `sudo -u homelab-fixer claude --dangerously-skip-permissions -p <folder>` directly. Simpler, but runs on the host filesystem.

Network egress allowed (Claude needs to reach Anthropic API); inbound network for the fixer is denied.

### 7.5 Network model

- All sidecars on a private docker network (`homelab-monitor-net`).
- Only the **frontend** (FastAPI + UI) and embedded iframes (Karma, Grafana) exposed via a single port (configurable, e.g., `:9090`).
- LAN reverse proxy (the existing `nginx-configuator` setup) terminates TLS and passes through; the monitor does not manage TLS.
- Local-watchdog has a host-network alias so it can reach the monitor's port even if docker net misbehaves.
- Outbound: HA, Pi-hole, Synology, UDM, healthchecks.io, SMTP, Discord, Backblaze API (if used), Docker Hub / GHCR (for diun digest checks).

---

## 8. Notifications

### 8.1 Channels

| Channel | Implementation |
|---|---|
| **Home Assistant push** | POST to `http://192.168.2.148:8123/api/services/notify/mobile_app_jake_s_android` with bearer token; payload `{message, title, data: {...}}`. Reuses the user's existing pattern from `/storage/scripts/on-demand/claude_ready.sh`. |
| **Discord webhook** | POST to webhook URL. Embed format with severity color, links to dashboard alert page. |
| **Email (SMTP)** | Used for daily/weekly digests. Standard SMTP with username/password from secrets; supports TLS/StartTLS. |
| **In-dashboard live feed** | SSE channel; alert appears in the Alerts screen and Overview banner without page refresh. |

### 8.2 Severity levels

- `info` — purely informational; usually digest-only.
- `warning` — needs attention but not urgent.
- `error` — something is broken and degraded.
- `critical` — page now.

### 8.3 Routing rules

Per-severity defaults plus per-tag overrides. Example:

| Severity | Channels |
|---|---|
| info | dashboard only |
| warning | dashboard + Discord |
| error | dashboard + Discord + HA push |
| critical | dashboard + Discord + HA push + immediate email |

Tag overrides allow, e.g., "any alert tagged `target_kind=cert` always emails so I don't lose it in noise."

### 8.4 Lifecycle

- **Acknowledge** — silences re-notification until issue clears or until a per-rule TTL passes. Implemented as a Karma-managed silence; kthxbye keeps it alive while the alert is firing.
- **Snooze** — suppress this alert for N hours; expires automatically.
- **Maintenance windows** — scheduled silences managed by the maintenance manager and pushed to Alertmanager via API on schedule. Recurring windows supported via rrule.
- **De-duplication / grouping** — Alertmanager handles via `group_by` and `group_interval`. The dispatcher additionally collapses related alerts into a single notification per group.
- **Auto-resolve notification** — when an alert clears, dispatcher sends a "resolved: <name>" notification with duration.

### 8.5 Daily / weekly digest

Fully configurable per recipient. Selectable sections (each implemented as a `digest_section` plugin so new ones can be added without core changes):

- Active alerts (current open issues, severity, age)
- Resolved alerts since last digest (with duration each was open)
- Auto-fix activity (Claude runs: alert, runbook, exit, dry-runs)
- Cron heartbeat report (which crons ran on time, which were late/missing)
- Backup status (Synology Hyper Backup, `/storage/scripts/cron/backup.sh`, Backblaze leg)
- Cert / domain expiry roundup (anything expiring in next N days)
- Update availability (container images, OS packages, DSM, Unifi firmware)
- Resource trends (top CPU/RAM/disk consumers, anomalies in trend)
- Tool effectiveness scorecard
- "What changed in homelab this week" — new containers, new devices, removed services, deleted crons (delta vs last digest)
- Top noisy alert sources (alerts that fired most — tuning candidates)
- Plus arbitrary additional sections contributed by integration plugins

Each section has a level-of-detail toggle (e.g., "summary line only" / "summary + per-item table" / "full detail with charts"). Format: HTML with embedded sparklines + plaintext fallback + dashboard deep-links.

---

## 9. UI structure

### 9.1 Top-level navigation

```
Overview
Alerts
Inventory ▸ Hosts / Containers / Devices / Services / Crons / Mounts
Integrations ▸ Home Assistant / Pi-hole / Synology / Unifi / Docker / ...
Logs
Metrics  (Grafana embed)
Runbooks
Auto-fix history
Discovery & suggestions
Tool analysis
Maintenance windows
Self-status
Settings ▸ Channels / Routing / Digests / Auth / Secrets / Retention
```

### 9.2 Per-screen specifications

| Screen | Notable widgets |
|---|---|
| **Overview** | Severity-grouped active alerts; 24h timeline; top noisy sources; self-status badge; "alerts today" sparkline |
| **Alerts** | Karma iframe for ack/snooze/silence; sidebar filter (severity/tool/target); right-pane drawer shows enriched detail (history, related metrics, runbook link, "Run fix" button) |
| **Inventory** | Tabbed list per kind; status, last-seen, key metric, "Open detail" |
| **Inventory → detail** | Header (status, since-time), live metric tiles, last 100 events, related alerts, related logs (LogsQL pre-filtered), per-target collector list with last-run/next-run, "Add probe" |
| **Integrations** | One sub-page per integration (HA shows entity health, Pi-hole shows query stats, Synology shows volumes/SMART/backups, Unifi shows topology + clients) — each is a *plugin-provided panel* |
| **Logs** | LogsQL explorer; stream picker; time range; live tail; saved queries; "create alert from this query" |
| **Metrics** | Grafana embed; project-owned dashboards under `deploy/grafana/dashboards/` (as code); plus a "Quick PromQL" panel |
| **Runbooks** | Catalog. Each card: name, alert match patterns, risk tag, last run, success rate, "Run now / Dry run / Edit CLAUDE.md" |
| **Auto-fix history** | Filterable table; row click opens transcript viewer (full Claude session, exit code, durations, before/after diff if generated) |
| **Discovery & suggestions** | Inbox of "I noticed X". Per-suggestion: kind, payload, "Accept (creates target+collectors) / Customize / Ignore" |
| **Tool analysis** | Per-tool scorecard. Charts: alerts-emitted, action-rate, dedup overlap, unique-share. Auto-recommendations panel with "Apply" buttons |
| **Maintenance windows** | Calendar + list; "Schedule new" form (scope = label selector or target list); pushes silences to Alertmanager |
| **Self-status** | Queue depth, collector lag, db size, disk usage breakdown, healthchecks.io heartbeat status, local-watchdog status |
| **Settings → Channels** | HA URL+token, Discord webhook, SMTP creds. Secrets edited via masked inputs. |
| **Settings → Routing** | Per-severity / per-tag rules. Drag-and-drop builder with "if this alert came in, it would route to ..." preview |
| **Settings → Digests** | Per-recipient cadence, sections, level-of-detail toggles; "Send test now" |
| **Settings → Auth** | Local users + sessions. Add/remove user, force-rotate session, session list. |
| **Settings → Secrets** | List by name (no values shown). Add/rotate/delete. |
| **Settings → Retention** | Per-stream rules; current usage gauge; kill-switch toggle |

### 9.3 Cross-cutting UX

- **Confirm-on-destructive** for kill switch, runbook real-run, secret rotate/delete, channel deletion, retention edits.
- **Live everywhere** — alert state, suggestions, auto-fix runs, collector status all push via SSE.
- **Keyboard-first** — command palette (⌘K) for nav and quick actions.
- **Empty/error states** designed up-front (e.g., "No alerts" gets a calm, useful state, not a blank page).

### 9.4 Frontend stack details

- **React 18 + Vite + TypeScript strict.**
- **Routing:** TanStack Router (typed routes).
- **Server state:** TanStack Query, fed by openapi-typescript-codegen against the FastAPI OpenAPI schema.
- **UI primitives:** Radix + Tailwind, lightly themed.
- **Charts:** Tremor (built on Recharts; provides higher-level chart components and design tokens that match the modern dashboard aesthetic).
- **Live:** native `EventSource` / `WebSocket`.
- **Forms:** React Hook Form + Zod.
- **Tests:** Vitest + Testing Library; Playwright for end-to-end against the docker-compose test rig.

---

## 10. Deployment & operations

### 10.1 Compose project

```
homelab-monitor (its own docker compose project on the primary host)
├── monitor          # FastAPI backend + plugin host + scheduler + dispatcher; serves the built React bundle as static files from /app/ui via FastAPI's StaticFiles. (No separate nginx sidecar — the LAN reverse proxy provided by the existing `nginx-configuator` setup is upstream.)
├── victoriametrics
├── victorialogs
├── vmagent
├── vmalert-metrics
├── vmalert-logs
├── alertmanager
├── karma
├── kthxbye
├── grafana
├── netdata
├── vector
├── local-watchdog
└── fixer-runner     # OPTIONAL container running as `homelab-fixer`
```

### 10.2 Volumes & mounts

| Volume | Purpose | Backup tier |
|---|---|---|
| `data/sqlite/` | Operational DB | snapshot before each backup window |
| `data/vm/` | TSDB | nightly snapshot, retained per policy |
| `data/vl/` | Logs | nightly snapshot, retained per policy |
| `data/runbook-transcripts/` | Auto-fix audit logs | always backed up; rotated by the runbook orchestrator: per-runbook keep last N transcripts (default 100) and a max age (default 365 days), configurable in `homelab-monitor.yaml`; oldest are pruned, never silently deleted (audit row in `runbook_runs` retained even after transcript file is gone) |
| `config/` | YAML/TOML configs | always backed up (gitignored from public repo) |
| `runbooks/` | Bind mount from `homelab-monitor-overrides/runbooks/` | always backed up |
| `secrets-master/` | Master key bootstrap | NOT backed up via normal flow |
| `/var/run/docker.sock:ro` | Docker discovery | n/a |
| `/proc, /sys, /etc` (read-only) | Host metrics | n/a |

### 10.3 Resource budget (defaults; configurable)

| Component | RAM idle | RAM peak | Disk |
|---|---|---|---|
| monitor (Python) | 200 MB | 500 MB | n/a |
| VM | 200 MB | 500 MB | budgeted |
| VL | 100 MB | 300 MB | budgeted |
| vmalert × 2 | 60 MB | 120 MB | n/a |
| Alertmanager | 30 MB | 80 MB | tiny |
| Karma + kthxbye | 40 MB | 80 MB | n/a |
| Grafana | 200 MB | 400 MB | small |
| Netdata | 200 MB | 400 MB | streaming, small local |
| vector | 80 MB | 150 MB | small |
| local-watchdog | 20 MB | 30 MB | n/a |
| fixer-runner | 0 idle | bursty during runs | runbook scratch |
| **Total floor** | **~1.1 GB** | **~2.5 GB** | **`HOMELAB_MONITOR_DISK_BUDGET_GB`** |

### 10.4 Backups

Nightly hook integrates with `/storage/scripts/cron/backup.sh`:

- Pre-backup: `sqlite3 .backup` snapshot of operational DB → `/storage/backup/homelab-monitor/sqlite-YYYYMMDD.sqlite`.
- VM/VL snapshot endpoints called → snapshot files written to `/storage/backup/homelab-monitor/{vm,vl}/`.
- `runbooks/`, `config/` rsynced to `/storage/backup/homelab-monitor/`.
- Synology side already replicates `/storage/backup/` to Backblaze.
- Master key NOT in backup; user keeps it in a password manager.

### 10.5 CI / release

- **GitHub** monorepo with branch protection on `main`.
- **GitHub Actions** workflows:
  - **PR check:** ruff + black + pyright (strict) + pytest (with coverage gate target 100%) + frontend tsc + vitest + Playwright (against compose test rig) + integration tests against `docker-compose.test.yml`.
  - **Main:** build & publish multi-arch container images to GHCR; auto-generate release notes from changelog.
- **Pre-commit hook:** ruff + black on changed files.
- **Code Review Graph:** initialized at clone via `pip install code-review-graph && code-review-graph install && code-review-graph build`. The `crg-daemon` runs locally and auto-rebuilds the graph as files change. Slash commands `/code-review-graph:build-graph`, `/code-review-graph:review-delta`, and `/code-review-graph:review-pr` are available during build/refinement/finalize phases. CI runs `code-review-graph build` to keep the graph current for review delta checks. `.code-review-graph/` is gitignored.
- **Dependabot / Renovate** for upstream image and dep updates.

---

## 11. Repo layout (monorepo)

```
homelab-monitor/                          (this folder, the GitHub repo)
├── README.md
├── CLAUDE.md                             (epic-stage-workflow + repo conventions, written by epic-stage-setup)
├── pyproject.toml                        (backend deps, ruff/black/pyright config)
├── package.json                          (workspace root; frontend lives in apps/ui)
├── pnpm-workspace.yaml or npm-workspaces (whichever is chosen)
├── .pre-commit-config.yaml
├── .github/
│   └── workflows/                        (ci.yml, release.yml, codeql.yml)
├── docs/
│   ├── superpowers/specs/                (this design doc lives here)
│   ├── runbooks/                         (developer-facing — distinct from runtime runbooks)
│   ├── architecture.md                   (high-level — generated from the spec)
│   └── adr/                              (architecture decision records)
├── epics/                                (epic-stage-setup creates this)
│   └── EPIC-001-foundation/
│       ├── EPIC-001.md
│       ├── STAGE-001-001.md
│       └── regression.md
├── changelog/                            (epic-stage-setup creates this)
│   ├── create_changelog.sh
│   └── .gitkeep
├── apps/
│   ├── monitor/                          (Python backend)
│   │   ├── homelab_monitor/
│   │   │   ├── kernel/                   (scheduler, plugin host, db, secrets, auth, api, lifecycle)
│   │   │   ├── plugins/
│   │   │   │   ├── collectors/
│   │   │   │   │   ├── builtin/          (host, docker, cron, mounts, certs, etc.)
│   │   │   │   │   └── integrations/     (homeassistant, pihole, synology, unifi, ...)
│   │   │   │   ├── discoverers/
│   │   │   │   ├── enrichers/
│   │   │   │   ├── channels/
│   │   │   │   └── digest_sections/
│   │   │   ├── api/                      (FastAPI routes)
│   │   │   ├── models/                   (pydantic models for API + dataclasses for DB rows; SQLAlchemy Core for queries)
│   │   │   └── cli/                      (`hm` subcommands)
│   │   ├── tests/
│   │   │   ├── unit/
│   │   │   ├── integration/              (uses docker-compose test rig)
│   │   │   └── e2e/                      (against live test rig)
│   │   └── Dockerfile
│   └── ui/                               (React + Vite + TS strict)
│       ├── src/
│       ├── tests/
│       └── playwright/
├── packages/
│   ├── shared-types/                     (TS types generated from FastAPI OpenAPI schema)
│   └── plugin-sdk-py/                    (public Python plugin SDK — a thin re-export of stable Protocols, base classes, the `CollectorContext` shape, and the `plugin.yaml` schema. Lives in-repo during early development; published to PyPI only when EPIC-019 polish phase determines the API has stabilized)
├── deploy/
│   ├── compose/
│   │   ├── docker-compose.yml            (production)
│   │   ├── docker-compose.dev.yml        (developer overrides)
│   │   └── docker-compose.test.yml       (integration test rig)
│   ├── grafana/
│   │   ├── dashboards/                   (dashboards as code)
│   │   └── provisioning/
│   ├── alertmanager/
│   │   └── alertmanager.yml.example
│   ├── vector/
│   │   └── vector.toml
│   ├── vmalert/
│   │   ├── metrics/
│   │   └── logs/
│   └── netdata/
│       └── netdata.conf
├── runbooks/                             (built-in example runbooks; user's overrides live elsewhere)
│   └── _examples/
├── scripts/
│   ├── dev.sh                            (one-command dev start)
│   ├── verify                            (canonical verify script)
│   └── reset-test-rig.sh
└── .code-review-graph/                   (gitignored; CRG local graph)
```

The `homelab-monitor-overrides` repo is **separate**, gitignored from the public release, and mounted into the running container as a volume. It contains private host-specific config, runbooks, plugins, integration credentials, and any monkey-patches the user wants for *their* install.

---

## 12. Verify command

`scripts/verify` (and a `make verify` alias) runs everything:

1. `ruff check`
2. `ruff format --check` (and/or `black --check`)
3. `pyright --strict` (or `mypy --strict`)
4. `pytest --cov --cov-fail-under=100` (kernel; aspirational on plugins)
5. `tsc --noEmit`
6. `vitest run --coverage`
7. `pnpm build` (UI build smoke check)
8. (optional) `docker compose -f deploy/compose/docker-compose.test.yml up --abort-on-container-exit --exit-code-from integration-tests`
9. (optional) `playwright test`

CI runs all steps. Pre-commit hook runs steps 1–3 + a fast subset of 4 + 5–6 on changed files.

---

## 13. Test strategy

### 13.1 Unit tests

- **Kernel:** scheduler, plugin host, secrets, auth — pure unit tests with stub plugins.
- **Plugins:** each collector / discoverer / channel / runbook has a unit test; HTTP/SSH/SNMP/Docker mocked at the boundary.
- **API:** route tests with `httpx.AsyncClient` against the FastAPI app; SQLite in-memory.

### 13.2 Integration tests

A `deploy/compose/docker-compose.test.yml` rig spins up real VM + VL + Alertmanager + monitor + a fixture target stack ("toy services" that produce predictable metrics, logs, and heartbeats). Tests cover:

- collector → VM round-trip
- vmalert rule fires → AM webhook → dispatcher → captured event
- heartbeat receiver freshness (start/ok/fail flows)
- runbook orchestrator end-to-end (with a fake `claude` CLI binary) including rate-limit, cooldown, dry-run gates, kill switch
- discovery roundtrip (planted Docker container appears as suggestion)
- suggestion accept (creates target + collector)
- tool-effectiveness analyzer with synthetic alert streams
- backup hook produces valid SQLite backup
- secret rotation works hot

### 13.3 End-to-end tests

Playwright against the test rig:

- Login → dashboard renders, alert appears, ack works, runbook runs in dry mode.
- Kill-switch toggle + confirm.
- Suggestion accept.
- Channel CRUD.
- Retention edit.

### 13.4 Coverage gate

100% on the kernel; aspirational on plugins (with realistic exemptions discussed in code review). `pyright --strict` everywhere. CI fails on regressions.

---

## 14. Epics

The brainstorm produced a candidate sequence of epics. Stage-level decomposition for EPIC-001 is provided in §15; remaining epics will be decomposed when each is begun.

| Epic | Theme |
|---|---|
| EPIC-001 | Foundation: repo skeleton, kernel, first collector, sidecars, integration test rig |
| EPIC-002 | Heartbeat receiver + cron registry + cron auto-discovery + cron heartbeat helpers |
| EPIC-003 | Docker collector + container probes + label-based discovery + diun-style updates |
| EPIC-004 | Logs pipeline (vector + VL + Drain + log signature alerts) |
| EPIC-005 | Home Assistant integration (collector + dispatcher channel) |
| EPIC-006 | Pi-hole integration |
| EPIC-007 | Unifi integration |
| EPIC-008 | Synology integration |
| EPIC-009 | Auto-fix subsystem (allow-list, runbook orchestrator, fixer-runner, audit, kill switch, dry-run) |
| EPIC-010 | Tool effectiveness analyzer + scorecards + recommendations |
| EPIC-011 | Discovery & suggestion engine UX |
| EPIC-012 | Maintenance windows + alert routing rules |
| EPIC-013 | Digest builder + email |
| EPIC-014 | Self-monitor + local-watchdog + healthchecks.io heartbeat |
| EPIC-015 | Netdata + comparative shadow rules |
| EPIC-016 | ISP/WAN collectors |
| EPIC-017 | SSH probe framework (per-target users, forced commands) |
| EPIC-018 | Per-service deep-dive integrations (Mosquitto, Z-wave, Foundry, Plex, Frigate, AT&T modem, etc.) |
| EPIC-019 | Polish, accessibility, documentation, public release |

---

## 15. EPIC-001 stage decomposition (foundation)

Each stage ends with a green `make verify`, passing tests for the new behavior, and a visible vertical slice (in the API, the dashboard, or the test rig).

| # | Stage | Vertical slice / acceptance |
|---|---|---|
| 001 | Backend Python skeleton | `pyproject.toml`, ruff/black/pyright config, pytest set up, pre-commit hook; `make verify` runs lint+type+test (with one trivial test) |
| 002 | Frontend skeleton | Vite + TS strict + vitest + eslint + prettier; `pnpm verify` runs lint+type+test |
| 003 | CI + Code Review Graph + Dependabot | GH Actions wires backend+frontend verify; CRG installed and first graph built; Dependabot config |
| 004 | SQLite + alembic + first migration | Repository facade; first migration creates `users`, `sessions`, `audit_log`; tests cover migrate up/down |
| 005 | Encrypted secrets store | Master key bootstrap; AES-GCM; `secrets` table; `hm secrets get/set/list/rotate` CLI; covers rotation |
| 006 | Collector protocol + base classes | Abstract base, types, sample noop collector, fixtures for tests |
| 007 | In-process plugin loader + scheduler | Entry-point discovery; async/thread/process run kinds; per-collector timeout; first scheduled tick visible in test |
| 008 | Concurrency groups + failure budget + quarantine | Group serialization; N-failure quarantine + reason; health metric per collector |
| 009 | Subprocess plugin runner + JSON line protocol | Manifest, exec, stdout-json parser, exit-code interpretation, timeout/kill, hello-world bash plugin |
| 010 | FastAPI app shell + healthz + structured logging + error model | App boots; `/api/healthz` works; structlog wired; uniform error responses |
| 011 | Local auth | bcrypt, sessions, login/logout endpoints, cookie middleware, CSRF, tests |
| 012 | First built-in `host` collector | psutil collector writes to a stub `MetricsWriter`; tests verify metric shape; appears in collector status API |
| 013 | Alert ingestor + first `inproc-dashboard` channel | POST to `/alerts` ingests, dedups by fingerprint, dispatches to in-memory channel; visible to API |
| 014 | UI shell + login + Overview live-tile | React app, login flow, Overview shows one live tile via SSE for the `host` collector metric |
| 015 | VictoriaMetrics + vmagent | Sidecars in compose; `MetricsWriter` switches to real VM; minimal `docker-compose.test.yml` stands up VM for the integration test |
| 016 | VictoriaLogs + vector | Sidecars; `LogsWriter` switches to real VL; vector tails docker logs of the test rig |
| 017 | Alertmanager + vmalert (metrics) + first rule | Real AM in compose; first vmalert rule fires on a known-bad host metric; webhook → ingestor → channel |
| 018 | vmalert (logs) + first log-derived rule | LogsQL rule fires on a planted error pattern; webhook delivered |
| 019 | Karma + kthxbye | Embedded in dashboard; ack works; kthxbye keeps silence alive while alert firing |
| 020 | Grafana + dashboards-as-code provisioning | Grafana provisions one default dashboard at startup; embedded in UI's Metrics screen |
| 021 | Full integration test rig + canonical e2e test | `docker-compose.test.yml` expanded; canonical e2e: collector → VM → vmalert → AM → ingestor → channel; runs in CI |

After EPIC-001 we have a vertical slice that proves the bones work end-to-end. Subsequent epics layer on horizontally per the §14 list.

---

## 16. Cross-cutting requirements

These apply to every component and stage.

- **Strict typing** — `pyright --strict` and TypeScript strict; no `Any` without a written exception.
- **No `git add -A` in any tooling or scripts** — always specific paths.
- **All persistent records are auditable** — write a row to `audit_log` for every state-changing user action.
- **Zero-downtime restart** — no in-flight state lost on container restart; SQLite is durable, in-flight collector runs are checkpointed.
- **Open-source-safe defaults** — the public release has Q30-A defaults (observe-only on existing scripts; no automated edits); the host-specific override repo applies more aggressive integrations.
- **Single source of truth for inventory** — the `targets` table; everything (alerts, runbooks, dashboards) keys to it.
- **All plugins observe themselves** — `homelab_collector_run_*` metrics are mandatory; the kernel emits them on the plugin's behalf if the plugin is in-process.
- **Self-monitor first** — the kernel architecture from STAGE-001-007 (in-process plugin loader + scheduler) onward emits `homelab_collector_run_*` self-metrics for every plugin, so the system observes itself starting with the very first collector run.
- **All internal timestamps are UTC.** The display layer converts to the configured local timezone (default: `America/New_York`, matching the user's existing container `TZ` settings). Cron schedule expressions are parsed in the host's local timezone for compatibility with the user's existing crontabs.
- **`nginx-configuator` is the actual directory name** at `/storage/programs/nginx-configuator/` (sic — not "configurator"). Do not "fix" the spelling.

---

## 17. Open items deferred to implementation

Items that the design intentionally leaves to implementation phases rather than over-specifying here:

- Specific vmalert rule definitions (per epic, codified under `deploy/vmalert/{metrics,logs}/`).
- Specific Grafana dashboards (per epic, codified under `deploy/grafana/dashboards/`).
- Per-integration plugin schemas (defined when each integration epic begins).
- Whether to use `npm`, `pnpm`, or `yarn` workspaces (decided at STAGE-001-002).
- Whether to use `mypy` or `pyright` for strict typing (decided at STAGE-001-001; both are acceptable).
- Whether `fixer-runner` is a separate container or a `sudo -u homelab-fixer` native exec (decided at the start of EPIC-009).
- AT&T modem model + scraping approach (collector deferred until the user adds the modem; the collector interface in EPIC-016 will define the contract).

---

## 18. Source of memory and references

This spec was assembled from a brainstorm dialogue and the following external research/inputs:

- VictoriaMetrics single-server documentation: https://docs.victoriametrics.com/victoriametrics/single-server-victoriametrics/
- VictoriaLogs alerting documentation: https://docs.victoriametrics.com/victorialogs/vmalert/
- vmalert + vmanomaly anomaly detection guide: https://docs.victoriametrics.com/anomaly-detection/guides/guide-vmanomaly-vmalert/
- Tiger Data licensing / TimescaleDB editions (rejected runner-up): https://www.tigerdata.com/docs/about/latest/timescaledb-editions
- Karma alert dashboard: https://github.com/prymitive/karma
- Netdata anomaly detection: https://www.netdata.cloud/features/aiml/anomaly-detection/
- Code Review Graph (MCP integration tool): https://github.com/tirth8205/code-review-graph
- Existing operator tool: `nginx-configuator` at `/storage/programs/nginx-configuator/` (managed nginx + certbot + Route 53)
- Existing compose: `/storage/docker/compose/docker-compose.yml`
- Existing scripts: `/storage/scripts/cron/backup.sh`, `/storage/scripts/rtlamr-watchdog.sh`, `/storage/scripts/startup/startup.sh`, `/storage/scripts/on-demand/claude_ready.sh`
- Project memory: `/home/jakekausler/.claude/projects/-storage-programs-homelab-monitor/memory/`
  - `reference_homelab_inventory.md`
  - `reference_docker_inventory.md`
  - `project_autofix_safety_model.md`
  - `project_repo_tooling.md`

---

End of specification.
