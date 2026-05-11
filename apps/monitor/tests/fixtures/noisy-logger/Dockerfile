# syntax=docker/dockerfile:1.7
# Tiny logger fixture for STAGE-001-021 vector-to-VL integration test.
# NOT for production.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN pip install --no-cache-dir \
      "fastapi>=0.115,<0.116" \
      "uvicorn[standard]>=0.32,<0.33" \
      "pydantic>=2.9,<3"

WORKDIR /app
COPY noisy_logger.py /app/noisy_logger.py

EXPOSE 8001

HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=10 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/healthz').read()" || exit 1

CMD ["python", "noisy_logger.py"]
