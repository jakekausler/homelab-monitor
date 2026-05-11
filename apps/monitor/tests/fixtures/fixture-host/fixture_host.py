"""Tiny controllable Prometheus metrics target for STAGE-001-021 integration tests.

Exposes a single gauge `fixture_cpu_percent` with a fixed series
`host="fixture-host"`. The metric value is mutable at runtime via POST /control
so tests can deterministically trip the FixtureHostHighCPU vmalert rule
(deploy/vmalert/metrics/fixture.yaml).

NOT for production use. Single-process. State is held in a module-level dict
because workers=1 (uvicorn).

Endpoints:
    GET  /metrics  -> Prometheus text format
    POST /control  -> {"cpu_percent": <int 0-100>} mutates the gauge
    GET  /healthz  -> {"status": "ok"}

Env:
    FIXTURE_CPU_PERCENT  -> initial gauge value (default: 5)
    FIXTURE_HOST_PORT    -> uvicorn port (default: 8000)
"""

from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field

app = FastAPI(title="fixture-host", version="1.0.0")

_DEFAULT_INITIAL = 5
_VALUE: dict[str, int] = {
    "cpu_percent": int(os.environ.get("FIXTURE_CPU_PERCENT", str(_DEFAULT_INITIAL))),
}


class ControlBody(BaseModel):
    """POST /control payload."""

    cpu_percent: int = Field(ge=0, le=100)


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> str:
    """Return Prometheus text format with the controllable cpu_percent gauge."""
    val = _VALUE["cpu_percent"]
    return (
        "# HELP fixture_cpu_percent Controllable CPU percent (test fixture).\n"
        "# TYPE fixture_cpu_percent gauge\n"
        f'fixture_cpu_percent{{host="fixture-host"}} {val}\n'
    )


@app.post("/control")
async def control(body: ControlBody) -> Response:
    """Set the gauge value. 204 No Content on success."""
    if body.cpu_percent < 0 or body.cpu_percent > 100:  # noqa: PLR2004
        raise HTTPException(status_code=400, detail="cpu_percent must be 0..100")
    _VALUE["cpu_percent"] = body.cpu_percent
    return Response(status_code=204)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Always-200 liveness."""
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("FIXTURE_HOST_PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning", workers=1)
