# Docker container probes

The monitor can actively probe Docker containers (HTTP, TCP, exec, metrics) and
emit `homelab_probe_up` (1 = healthy, 0 = failing) and
`homelab_probe_duration_seconds`. These feed two vmalert rules:

- **`DockerProbeFailing`** — fires when `homelab_probe_up == 0` for 2 minutes
  (`deploy/vmalert/metrics/docker_probes.yaml`).
- **`DockerProbeSlow`** — fires when `homelab_probe_duration_seconds > 2` for 5
  minutes (`deploy/vmalert/metrics/container_lifecycle.yaml`).

Both stay silent until at least one probe is configured.

## First-party sidecars (shipped)

The bundled stack already probes its own sidecars via compose labels in
`deploy/compose/docker-compose.yml`:

| Service          | Probe label                                                        |
| ---------------- | ------------------------------------------------------------------ |
| `monitor`        | `homelab-monitor.http.health: "http://container:9090/api/healthz"` |
| `grafana`        | `homelab-monitor.http.health: "http://container:3000/api/health"`  |
| `victoriametrics`| `homelab-monitor.http.health: "http://container:8428/health"`      |
| `alertmanager`   | `homelab-monitor.http.health: "http://container:9093/-/healthy"`   |

The `container` token resolves to the probed container's own IP on
`homelab-monitor-net`. No operator action is required for these.

## Probing your own containers

Two mechanisms, either works:

### Option A — compose labels

Add a label of the form `homelab-monitor.<kind>.<name>=<target>` to any
container the monitor can see (kinds: `http`, `tcp`, `exec`, `metrics`):

```yaml
services:
  myapp:
    image: myorg/myapp:latest
    labels:
      homelab-monitor.http.health: "http://container:8080/healthz"
```

(cadvisor whitelists `homelab-monitor.*` labels; the probe discoverer reads them
from the container's `Config.Labels`.)

### Option B — per-container override file

Drop a YAML file at
`$HOMELAB_MONITOR_OVERRIDES_DIR/plugins/docker/<container>.yaml` on the host
(default host path `/var/lib/homelab-monitor/overrides`, mounted read-only at
`/config` inside the container). The file basename MUST equal the `container:`
field.

```yaml
container: myapp           # must equal the filename stem (myapp.yaml)
exec_authorized: false     # gate for exec-kind probes (default false)
disabled: false            # set true to disable all probes for this container
probes:
  - kind: http             # http | tcp | exec | metrics
    name: health           # default: "default"
    target: "http://container:8080/healthz"
    enabled: true          # default: true
    interval_seconds: 30   # default: 30
    timeout_seconds: 10    # default: 10
```

## Effect

Once a probe is configured (either mechanism), `homelab_probe_up` and
`homelab_probe_duration_seconds` begin emitting for that container, and the
`DockerProbeFailing` / `DockerProbeSlow` alerts go live for it.
