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
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 1000 homelab \
 && useradd --system --uid 1000 --gid homelab --create-home --home-dir /home/homelab homelab

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/apps/monitor /app/apps/monitor
# UI bundle (built in CI before docker build runs).
COPY apps/ui/dist /app/ui

USER homelab

EXPOSE 9090

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9090/api/healthz').read()" || exit 1

WORKDIR /app/apps/monitor
CMD ["python", "-m", "uvicorn", "homelab_monitor.kernel.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "9090"]
