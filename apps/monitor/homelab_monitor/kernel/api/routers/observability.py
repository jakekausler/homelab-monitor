"""Observability endpoints — Prometheus exposition for vmagent scrape.

Mounted at the ROOT (no ``/api`` prefix) and intentionally unauthenticated.
The endpoint is exposed only on the internal compose network; vmagent scrapes
it without credentials.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from starlette.responses import Response

from homelab_monitor.kernel.api.dependencies import get_prom_registry

router = APIRouter()


@router.get("/metrics")
async def metrics_exposition(
    registry: CollectorRegistry = Depends(get_prom_registry),  # noqa: B008
) -> Response:
    """Return the prometheus_client text-format exposition.

    Auth: NONE (intentional — internal scrape only). ``CONTENT_TYPE_LATEST``
    is ``text/plain; version=0.0.4; charset=utf-8``.
    """
    body = generate_latest(registry)
    return Response(content=body, media_type=CONTENT_TYPE_LATEST)
