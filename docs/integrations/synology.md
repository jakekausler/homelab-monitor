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

## Logs

DSM logs arrive via DSM remote-syslog forwarding → vector → VictoriaLogs (built in a
later stage), scoped under `service="synology-*"` labels. **No DSM API is used for
log polling.**
