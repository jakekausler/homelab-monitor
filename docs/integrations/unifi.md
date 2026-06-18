# Unifi integration

Read-only monitoring of a UniFi Dream Machine (UDM) controller. The integration is
**observe-only**: it issues GET requests against the controller's API and never
performs write actions (no device restart, no PoE/outlet cycling).

## Generating the read-only API key

1. In the UniFi controller UI, open **Settings → Control Plane → Integrations**.
2. **Create API key**. Copy the generated key — it is shown only once.
3. The key is **read-only by nature**; there is no separate read/write split.

A single API key authenticates both the official v1 Integrations API and the
classic API on this firmware. It is sent as the `X-API-KEY` request header.

## Configuring the secret

Store the key in the encrypted secret store under the name:

```
unifi_api_key
```

The monitor reads this secret per-request via its TTL-cached resolver, so a rotated
key is picked up without a restart. The key value is **never logged** and **never
appears in any error message**.

## Controller URL

| Env var | Default | Meaning |
| --- | --- | --- |
| `HOMELAB_MONITOR_UNIFI_URL` | `https://192.168.2.1` | Controller base URL (trailing slash stripped). |
| `HOMELAB_MONITOR_UNIFI_SITE_ID` | `default` | Controller site id (re-resolved from `v1/sites` at startup). |

## Self-signed TLS (`verify=False`) and threat model

The UDM serves a self-signed certificate (`CN=unifi.local`) with no CA chain, so the
monitor connects to it with **TLS verification disabled** (`verify=False`) on a
**dedicated** HTTP client scoped to this one target. All other outbound HTTP keeps
full certificate verification.

Threat model: the connection is to the operator's own gateway on a trusted LAN. The
blast radius of disabling verification is exactly this single host. Certificate
pinning was considered and rejected — firmware updates rotate the cert and would
silently break the integration, which is over-engineered for a read-only LAN GET
against your own gateway. If you front the controller with a proxy presenting a
CA-signed certificate, point `HOMELAB_MONITOR_UNIFI_URL` at it and verification can
be restored in a future enhancement.
