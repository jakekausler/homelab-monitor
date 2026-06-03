# syntax=docker/dockerfile:1.7
# Custom Grafana image for homelab-monitor.
#
# Bakes in the VictoriaLogs datasource plugin (unsigned, fetched from upstream
# GitHub release at build time). The plugin install env var is also baked in
# so consumers (compose files) do NOT need to set GF_INSTALL_PLUGINS or
# GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS.
#
# Build:    docker build -t ghcr.io/jakekausler/homelab-monitor-grafana:latest .
# Tag note: STAGE-001-020 pinned grafana-oss to 12.3.3. Bump together with
#           deploy/compose/docker-compose*.yml when upgrading.

FROM grafana/grafana-oss:13.0.2

# Plugin version must match the upstream release tag exactly.
# When bumping: also update VL_PLUGIN_VERSION in this comment, the README, and
# the upstream changelog reference.
ARG VL_PLUGIN_VERSION=v0.26.3
ARG VL_PLUGIN_URL=https://github.com/VictoriaMetrics/victorialogs-datasource/releases/download/${VL_PLUGIN_VERSION}/victoriametrics-logs-datasource-${VL_PLUGIN_VERSION}.zip

# Switch to root for plugin install; switch back to grafana user before CMD.
USER root

RUN apk add --no-cache curl unzip \
 && mkdir -p /var/lib/grafana/plugins \
 && curl -fsSL "${VL_PLUGIN_URL}" -o /tmp/vl-plugin.zip \
 && unzip -q /tmp/vl-plugin.zip -d /var/lib/grafana/plugins/ \
 && rm /tmp/vl-plugin.zip \
 # The upstream zip extracts to "victoriametrics-logs-datasource/" matching
 # the Grafana plugin catalog id. If the extraction layout ever changes, fail
 # the build loudly so we update this Dockerfile rather than ship a broken image.
 && test -f /var/lib/grafana/plugins/victoriametrics-logs-datasource/plugin.json \
      || (echo "ERROR: VL plugin extracted to unexpected layout. Inspect /var/lib/grafana/plugins/" \
          && ls -la /var/lib/grafana/plugins/ \
          && exit 1) \
 && chown -R grafana:root /var/lib/grafana/plugins \
 && apk del curl unzip

# The plugin is unsigned (community release). Setting this env at image build
# time means downstream compose files can drop the env var entirely.
ENV GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS=victoriametrics-logs-datasource

USER grafana
