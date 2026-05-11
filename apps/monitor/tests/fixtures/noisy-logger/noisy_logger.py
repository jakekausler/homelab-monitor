"""Tiny logger fixture: prints lines to stdout on POST /log.

Vector (configured per deploy/compose/test-fixtures/vector.toml) tails this
container's docker logs and forwards lines to VictoriaLogs. The integration
test asserts a planted line surfaces in /api/logs/query.

NOT for production. Single-process.

Endpoints:
    POST /log      -> {"line": "..."} prints `line` to stdout, returns 204
    GET  /healthz  -> {"status": "ok"}

Env:
    NOISY_LOGGER_PORT  -> uvicorn port (default: 8001)
"""

from __future__ import annotations

import os
import sys

import uvicorn
from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel, Field

app = FastAPI(title="noisy-logger", version="1.0.0")


class LogBody(BaseModel):
    """POST /log payload."""

    line: str = Field(min_length=1, max_length=4096)


@app.post("/log")
async def log_line(body: LogBody) -> Response:
    """Print the line to stdout (flushed) so the docker_logs vector source picks it up."""
    print(body.line, flush=True)
    sys.stdout.flush()
    return Response(status_code=204)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Always-200 liveness."""
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("NOISY_LOGGER_PORT", "8001"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning", workers=1)
