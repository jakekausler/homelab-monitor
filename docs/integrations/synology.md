# Synology integration

Read-only (observe-only) monitoring of a Synology DSM v7 NAS (DS3622xs+) over the
LAN. The integration uses DSM's session-auth REST API: the kernel logs in with a
service-account password (`SYNO.API.Auth` version 7, `method=login`), receives a
session id (SID), and carries it as the `_sid` query param on every subsequent
`/webapi/entry.cgi` call. One session is reused and re-authenticated when DSM
returns body error **119** (session expired). The DSM serves a self-signed
certificate (`CN=synology`), so the monitor connects with **TLS verification
disabled** (`verify=False`) on a dedicated HTTP client scoped to this one target.
The password is read at login time from the secret store and is **never logged**
nor included in any error message. **Observe-only:** the client exposes only GET
read helpers; no write methods exist.

## Secret

One secret backs this integration:

| Secret name             | Used by                       | When first used |
| ----------------------- | ----------------------------- | --------------- |
| `synology_dsm_password` | collectors (DSM API read)     | this stage      |

There is **no read/write split** — DSM has no "read-only admin" role.

### Create the DSM service account

1. In DSM, open **Control Panel → User & Group → User → Create**.
2. Create a dedicated account named `homelab-monitor` (do **not** reuse your
   personal DSM account — the monitor never touches it).
3. Add the account to the **administrators** group. **This is required:** recon
   showed a non-admin account returns DSM error 105 (permission denied) on nearly
   every system-health API (storage, SMART, utilization, backup, security,
   upgrade), and Synology has no "read-only admin" role. The exposure is mitigated
   because **our collector code only ever calls read methods and the entire
   integration is observe-only** (no write paths exist) — the same posture EPIC-006
   noted for Pi-hole's app password.
4. Create the account **without 2-factor authentication**. Headless login cannot
   satisfy a 2FA challenge; a 2FA-protected account will fail to authenticate.

### Store the secret

```bash
echo "<dsm-service-account-password>" | hm secrets set synology_dsm_password --from-stdin
```

Reveal a stored secret for inspection (requires the reveal flag):

```bash
HOMELAB_MONITOR_REVEAL=1 hm secrets get synology_dsm_password
```

## Configuration

| Env var                             | Default                     | Meaning                                            |
| ----------------------------------- | --------------------------- | -------------------------------------------------- |
| `HOMELAB_MONITOR_SYNOLOGY_URL`      | `https://192.168.2.4:5001`  | DSM base URL (HTTPS; trailing slash stripped).     |
| `HOMELAB_MONITOR_SYNOLOGY_ACCOUNT`  | `homelab-monitor`           | DSM service-account name (not a secret).           |

The default targets the verified DSM host IP (`192.168.2.4`) on the HTTPS DSM port
(`5001`). If the NAS is elsewhere, override `HOMELAB_MONITOR_SYNOLOGY_URL`.

## Self-signed TLS (`verify=False`) and threat model

The DSM serves a self-signed certificate (`CN=synology`) with no CA chain, so the
monitor connects to it with **TLS verification disabled** (`verify=False`) on a
**dedicated** HTTP client scoped to this one target. All other outbound HTTP keeps
full certificate verification.

Threat model: the connection is to the operator's own NAS on a trusted LAN. The
blast radius of disabling verification is exactly this single host. If you front
the NAS with a proxy presenting a CA-signed certificate, point
`HOMELAB_MONITOR_SYNOLOGY_URL` at it and verification can be restored in a future
enhancement. (Mirrors the Unifi integration's posture.)

## Auth model (reference)

- Login: `GET /webapi/entry.cgi?api=SYNO.API.Auth&version=7&method=login&account=<account>&passwd=<password>&format=sid` → `data.sid`. **No `session=` param is sent** (DSM returns error 402 if present — this is NOT 2FA). If no password is stored in the secret store, the client returns an `auth` error without making a network call.
- Every request: `_sid=<sid>` query param on `/webapi/entry.cgi`.
- DSM returns **HTTP 200** for logical errors; the failure is in the body as `{"success": false, "error": {"code": N}}`. On body error **119** (session expired) the client re-authenticates **once** and retries; a second 119 is surfaced as a typed `auth` error. Body error **400** (bad credentials) → `auth`; **105** (permission denied) and other codes → `api_error`.
- Shutdown: `method=logout` (best-effort logout to free the session slot; never blocks teardown).

## Logs (syslog → VictoriaLogs)

DSM logs arrive via DSM remote-syslog forwarding → vector → VictoriaLogs, scoped
under `source_type="synology"` + `service="synology-*"` stream labels. **No DSM
API is used for log polling** (single source per signal).

### Enable DSM remote syslog (operator step)

1. In DSM, open **Control Panel → Log Center → Log Sending**.
   (On some DSM builds this is **Log Center → Send Logs**.)
2. Check **Send logs to a syslog server**.
3. **Server:** `192.168.2.148` (the monitor host LAN IP).
4. **Port:** `5515`  ·  **Transfer protocol:** `UDP`  ·  **Log format:** `BSD`
   (RFC 3164). Do NOT use TLS/RFC 5424 — the vector socket source expects plain
   UDP BSD.
5. Apply. DSM immediately sends a `System Test message from Synology Syslog
   Client from (<external-ip>)` heartbeat — that confirms the path end-to-end.

The listener is `0.0.0.0:5515/udp` in prod (host-published by the `vector`
sidecar under the `host-collectors` profile). The dedicated port `5515` (distinct
from the UDM's `5514`) is the sole device discriminator — the NAS is multi-NIC
(.4/.5/.6/.7) so we do NOT filter by source IP.

### Service labels (category-word bucketing)

All DSM lines carry PRI=14 (facility=user/info) regardless of event, so vector
buckets by the CATEGORY WORD (the token after the hostname), handling both the
colon-delimited (`Connection:`) and space-delimited (`System ...`) shapes:

| DSM category      | `service` label     | Downstream (STAGE-008-023) |
| ----------------- | ------------------- | -------------------------- |
| `Connection`      | `synology-auth`     | failed-login burst / abnormal-login |
| `Storage Manager` | `synology-smart`    | SMART-event rules          |
| `Package Center`  | `synology-package`  | package-event rules        |
| recognized, other | `synology-system`   | general error-rate         |
| parse-fail / none | `synology-other`    | counted by `synology_lines_total{parse_failed="1"}` |

Only `Connection` (auth) and `System` (system) are live-confirmed; the SMART /
package category words are pending a richer real capture (the parser is
category-generic with a safe `synology-system` fallback, so unseen categories are
never dropped).

### Redaction

Synology lines are redacted at ingest (before VictoriaLogs) by the `synology_*`
patterns (`config.py` `DEFAULT_REDACT_PATTERNS`): the account name (`User [...]`),
source address (`from [...]`, often IPv6), auth method (`via [...]`), and the
test-heartbeat external IP (`Synology Syslog Client from (...)`). A provisional
`synology_synotoken` guard covers any DSM session token if one ever appears.

### Coverage map (Amendment 5 / Q10b — nothing silently dropped)

**Carried by syslog → VictoriaLogs:**

| Signal               | Source category   | Stream            |
| -------------------- | ----------------- | ----------------- |
| Auth / login events  | `Connection`      | `synology-auth`   |
| SMART / storage logs | `Storage Manager` | `synology-smart`  |
| Package events       | `Package Center`  | `synology-package`|
| General system logs  | (recognized else) | `synology-system` |

**Covered by METRIC collectors instead (NOT syslog — single source per signal):**

| Signal                              | Collector (stage)              |
| ----------------------------------- | ------------------------------ |
| Volume / disk / SMART-attribute gauges | Wave B/C collectors (008-008…008-014) |
| Surveillance camera/event/recording state | SS-API collectors (008-015 / 008-016) |
| License / HomeMode                  | 008-017                        |
| NFS mount health                    | 008-018                        |

**No silent drops:** an unrecognized syslog line → `synology-other` + the
`synology_lines_total{parse_failed="1"}` counter (prometheus_exporter :9598);
non-forwardable signals → covered by the named metric collector above.
