# syntax=docker/dockerfile:1.7
# Tiny FastAPI fixture for STAGE-001-021 integration tests.
# NOT for production. ~150 lines of Python; minimal surface.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# pip install pin: minor versions only. fastapi/uvicorn must match the monitor's
# major versions (see apps/monitor/pyproject.toml dependencies).
RUN pip install --no-cache-dir \
      "fastapi>=0.115,<0.116" \
      "uvicorn[standard]>=0.32,<0.33" \
      "pydantic>=2.9,<3"

WORKDIR /app
COPY fixture_host.py /app/fixture_host.py

EXPOSE 8000

# IMPORTANT: monitor stack uses 127.0.0.1 inside healthchecks (busybox does
# not resolve localhost). Match that convention.
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=10 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz').read()" || exit 1

CMD ["python", "fixture_host.py"]
