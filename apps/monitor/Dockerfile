# syntax=docker/dockerfile:1.7
# Multi-stage build for homelab-monitor.
#
# BUILD CONTEXT: repo root (per .github/workflows/release.yml: context: .).
# Paths in this Dockerfile are repo-relative.
#
# Stage 1 (builder): install uv, sync the locked Python deps into a venv at /opt/venv.
# Stage 2 (runtime): copy the venv + app source + the prebuilt UI bundle.

FROM python:3.12-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# Install uv from the official source.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && curl -LsSf https://astral.sh/uv/install.sh | sh \
 && mv /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /app

# Copy only the metadata first for better Docker layer caching.
# Workspace root lockfile + pyproject; workspace member pyproject.
COPY pyproject.toml uv.lock ./
COPY apps/monitor/pyproject.toml ./apps/monitor/
COPY apps/monitor/homelab_monitor/__init__.py ./apps/monitor/homelab_monitor/__init__.py

# Sync into the venv at /opt/venv.
WORKDIR /app/apps/monitor
RUN uv sync --directory . --frozen --no-dev --no-install-project

# Copy the rest of the monitor source and re-sync (installs the project itself).
WORKDIR /app
COPY apps/monitor ./apps/monitor
WORKDIR /app/apps/monitor
RUN uv sync --directory . --frozen --no-dev


FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Runtime needs wget for healthchecks (on VM/vmagent images, not here — but
# leaving curl out keeps the image small). The monitor's own healthcheck
# uses python's urllib (already present).
# STAGE-003-010: install docker CLI + compose v2 plugin for Pull & Restart actions.
# Adds ~50 MB. Pinned via Docker's official APT repo for reproducibility.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
 && chmod a+r /etc/apt/keyrings/docker.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable" > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends docker-ce-cli docker-buildx-plugin docker-compose-plugin \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 1000 homelab \
 && groupadd --system --gid 2000 amconfig \
 && useradd --system --uid 1000 --gid homelab --create-home --home-dir /home/homelab homelab \
 && usermod -aG amconfig homelab

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/apps/monitor /app/apps/monitor
# UI bundle (built in CI before docker build runs).
COPY apps/ui/dist /app/ui

# Pre-create volume mount points. When docker mounts a named volume onto an
# existing image directory, the fresh volume inherits that directory's
# ownership + mode. The monitor container runs as an arbitrary host-specific
# UID/GID (compose `user: ${HM_CRON_HOST_UID}:${HM_CRON_HOST_GID}` — required
# so the container process UID matches the host `homelab-monitor` user that
# host-setup.sh grants crontab read ACLs to). That runtime UID does NOT match
# the image build-time UID 1000, so owner- or group-based perms cannot be
# pre-baked. These dirs are therefore made world-writable + sticky (1777, same
# semantics as /tmp): any UID the container is pinned to can write, and the
# sticky bit keeps delete semantics sane. They are single-tenant private
# volumes inside one container — no other principal can reach them.
#
# /data                    — SQLite database volume (data_monitor); monitor writes.
# /var/alertmanager-config  — Alertmanager render-on-boot output (data_alertmanager_config); monitor writes.
# /var/vector-config        — Vector cron-events render-on-boot output (data_vector_config); monitor writes.
# /storage/backup/...       — backup output (data_backup); monitor writes. Must be
#                             pre-created here or the named volume inherits docker's
#                             default root:root 0755 mountpoint and the monitor cannot write.
# The render dirs keep the setgid bit + amconfig group so files created inside
# inherit gid 2000 — the vector/alertmanager sidecars join that group via
# compose `group_add` to READ the 0640 rendered configs (3777 = setgid+sticky+rwx-all).
RUN mkdir -p /data /var/alertmanager-config /var/vector-config /var/vmalert-user-logs /var/vmalert-user-metrics /storage/backup/homelab-monitor \
 && chown -R homelab:homelab /data /storage/backup/homelab-monitor \
 && chown homelab:amconfig /var/alertmanager-config /var/vector-config /var/vmalert-user-logs /var/vmalert-user-metrics \
 && chmod 1777 /data /storage/backup/homelab-monitor \
 && chmod 3777 /var/alertmanager-config /var/vector-config /var/vmalert-user-logs /var/vmalert-user-metrics

USER homelab

EXPOSE 9090

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9090/api/healthz').read()" || exit 1

WORKDIR /app/apps/monitor
CMD ["python", "-m", "uvicorn", "homelab_monitor.kernel.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "9090"]
