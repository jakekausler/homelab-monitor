"""Tiny FastAPI receiver for AM webhook integration tests.

Bound to 0.0.0.0:9090 INSIDE the integration-tests container so the
Alertmanager container can POST to it via the bridge network.

NOT for production use. Has no auth.

Single-worker only — `OUT.open("a")` + `f.write(...)` is atomic on Linux for
writes under PIPE_BUF (4KB), which holds for AM payloads. Multi-worker uvicorn
(workers>1) would require fcntl-locking around the append.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request

OUT = Path(os.environ.get("WEBHOOK_RECEIVED_FILE", "/tmp/received-alerts.jsonl"))
app = FastAPI()


async def _handle_payload(req: Request) -> dict[str, int]:
    """Accept Alertmanager webhook payload; persist to NDJSON file."""
    body: dict[str, Any] = await req.json()
    with OUT.open("a") as f:
        f.write(json.dumps(body) + "\n")
        f.flush()
    return {
        "received": len(body.get("alerts", [])),
        "ingested": len(body.get("alerts", [])),
    }


@app.post("/api/alerts/ingest")
async def ingest(req: Request) -> dict[str, int]:
    """Accept Alertmanager webhook payload at monitor-compatible path."""
    return await _handle_payload(req)


@app.post("/webhook")
async def webhook(req: Request) -> dict[str, int]:
    """Accept Alertmanager webhook payload at /webhook (AM fanout target)."""
    return await _handle_payload(req)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9090, log_level="warning")
