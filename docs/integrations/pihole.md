# Pi-hole integration

Read-mostly monitoring of a Pi-hole v6 instance over the LAN. The integration uses
Pi-hole's session-auth REST API: the kernel logs in with an app password
(`POST /api/auth`), receives a session id (SID), and carries it in the `X-FTL-SID`
header on every subsequent request. One session is reused and re-authenticated on a
401. The app password is read at login time from the secret store and is never
logged nor included in any error message.

## Secrets

Two secrets back this integration:

| Secret name                | Used by                          | When first used  |
| -------------------------- | -------------------------------- | ---------------- |
| `pihole_api_password_ro`   | collectors (least-priv, read)    | this stage       |
| `pihole_api_password_rw`   | Wave-E control / write actions   | STAGE-006-018    |

Store both now even though the RW one is not exercised until STAGE-006-018.

### Generate the app passwords in Pi-hole

1. Open the Pi-hole web UI → **Settings → API**.
2. Under **app passwords**, generate **two** least-privilege app passwords:
   - one **read-only** password for the collectors → store as `pihole_api_password_ro`,
   - one **read-write** password for control actions → store as `pihole_api_password_rw`.
3. Do **not** use the Pi-hole main/admin password for either. App passwords are
   independently revocable and scope-limited.

### Store the secrets

```bash
echo "<read-only-app-password>" | hm secrets set pihole_api_password_ro --from-stdin
echo "<read-write-app-password>" | hm secrets set pihole_api_password_rw --from-stdin
```

Reveal a stored secret for inspection (requires the reveal flag):

```bash
HOMELAB_MONITOR_REVEAL=1 hm secrets get pihole_api_password_ro
```

## Configuration

| Env var                       | Default                     | Meaning                                  |
| ----------------------------- | --------------------------- | ---------------------------------------- |
| `HOMELAB_MONITOR_PIHOLE_URL`            | `http://192.168.2.148:8080` | Pi-hole base URL (trailing slash stripped).                                                                                                                                                                                                               |
| `HOMELAB_MONITOR_PIHOLE_HOST_LAN_IP`   | `(empty)`                   | Host LAN IP used to attribute Pi-hole loopback DNS clients (`127.0.0.1`/`::1`/`::`/`pi.hole`) to the monitor host. Empty (the public-release default) leaves loopback clients classified as `unattributed`; set to the host's LAN IP (e.g. `192.168.2.148`) to attribute them as `local`/`resolver_self`. |

The default targets the monitor host's LAN IP (`192.168.2.148`) because the prod monitor
runs on a bridge network and cannot reach `localhost` (its own container loopback);
Pi-hole runs host-network on the host. This mirrors the HA and Unifi integrations. If
Pi-hole runs elsewhere, override `HOMELAB_MONITOR_PIHOLE_URL` with its actual address.

Pi-hole is reached over **plain HTTP** on the LAN, so the client reuses the shared
HTTP connection pool (no dedicated TLS client).

## Logs

Pi-hole logs are scoped under the vector service label `pihole-unbound`.

## Auth model (reference)

- Login: `POST /api/auth` with `{"password": "<app-password>"}` → `session.sid`.
- Every request: `X-FTL-SID: <sid>` header.
- On `HTTP 401`: the client re-authenticates **once** and retries; a second 401 is
  surfaced as a typed `auth` error.
- Shutdown: `DELETE /api/auth` (best-effort logout; never blocks teardown).
