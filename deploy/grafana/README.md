# homelab-monitor custom Grafana image

This directory builds the Grafana image used by the homelab-monitor stack.
Both `docker-compose.yml` (prod) and `docker-compose.test.yml` (integration
test rig) consume this image.

## Why a custom image?

Grafana's `grafana-oss` upstream does not include the VictoriaLogs datasource
plugin, and installing it via `GF_INSTALL_PLUGINS` at runtime adds 30+ seconds
to every container start. The custom image bakes the plugin into a
build-time layer so cold starts complete in <10 seconds.

The plugin (`victoriametrics-logs-datasource`) is community-published as an
unsigned plugin. The Dockerfile sets
`GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS` as a baked-in `ENV` so consumers
do not need to set it.

## Bumping the plugin version

1. Find the new release tag at
   <https://github.com/VictoriaMetrics/victorialogs-datasource/releases>.
2. Edit `Dockerfile`: change the `VL_PLUGIN_VERSION=vX.Y.Z` ARG.
3. Update the version reference in this README's "Current versions" section.
4. Build locally and verify: `docker build -t homelab-grafana:test .` then
   `docker run --rm homelab-grafana:test ls /var/lib/grafana/plugins/victoriametrics-logs-datasource/`.
5. Run `make integration` to confirm the test rig's Grafana provisioning still
   detects the datasource.
6. Open a PR. STAGE-001-021 Spec C adds a CI workflow
   (`.github/workflows/grafana-image.yml`) that publishes the image to GHCR
   on changes to `deploy/grafana/**`.

## Bumping the Grafana base image

`Dockerfile` pins `grafana/grafana-oss:12.3.3`. Bump in lockstep with:

- `deploy/compose/docker-compose.yml` (`image: ghcr.io/.../homelab-monitor-grafana:...`)
- `deploy/compose/docker-compose.test.yml` (same)
- The CHANGELOG entry for the bump

## Current versions

| Component | Version |
| --- | --- |
| Grafana | 12.3.3 |
| VictoriaLogs datasource | v0.26.3 |

## Local build

```bash
docker build -t ghcr.io/jakekausler/homelab-monitor-grafana:latest deploy/grafana/
```

## Verification

```bash
docker run --rm --entrypoint sh ghcr.io/jakekausler/homelab-monitor-grafana:latest \
  -c 'cat /var/lib/grafana/plugins/victoriametrics-logs-datasource/plugin.json | head'
```

Should print the plugin.json header including `"id": "victoriametrics-logs-datasource"`.
