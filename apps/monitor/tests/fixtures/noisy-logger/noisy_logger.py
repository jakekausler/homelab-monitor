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
import time

import uvicorn
from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

app = FastAPI(title="noisy-logger", version="1.0.0")

# Constants for validation
_MAX_LINE_LENGTH = 4096


class LogBody(BaseModel):
    """POST /log payload."""

    line: str = Field(min_length=1, max_length=_MAX_LINE_LENGTH)


@app.post("/log")
async def log_line(body: LogBody) -> Response:
    """Print the line to stdout (flushed) so the docker_logs vector source picks it up."""
    print(body.line, flush=True)
    return Response(status_code=204)


class LogLinesBody(BaseModel):
    """POST /log_lines payload.

    Prints multiple lines to stdout with optional inter-line delay.
    Used by STAGE-004-001 integration tests to plant multi-line sequences
    (e.g., Python tracebacks) that the Vector multiline codec should stitch
    into a single VictoriaLogs record.
    """

    lines: list[str] = Field(min_length=1, max_length=50)
    delay_ms: int | None = Field(default=50, ge=0, le=5000)

    @field_validator("lines")
    @classmethod
    def validate_lines(cls, v: list[str]) -> list[str]:
        for line in v:
            if not (1 <= len(line) <= _MAX_LINE_LENGTH):
                msg = f"each line must be 1-{_MAX_LINE_LENGTH} chars; got length {len(line)}"
                raise ValueError(msg)
        return v


@app.post("/log_lines")
async def log_lines(body: LogLinesBody) -> Response:
    """Print each line to stdout (flushed) with optional inter-line delay.

    Used by STAGE-004-001 integration tests to emit multi-line sequences
    (Python tracebacks, Java stack traces) so the Vector multiline codec
    can be verified end-to-end via VictoriaLogs.
    """
    delay_s = (body.delay_ms or 50) / 1000.0
    for line in body.lines:
        print(line, flush=True)
        time.sleep(delay_s)
    return Response(status_code=204)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Always-200 liveness."""
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("NOISY_LOGGER_PORT", "8001"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning", workers=1)
