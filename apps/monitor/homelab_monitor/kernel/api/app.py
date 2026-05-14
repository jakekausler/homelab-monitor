"""FastAPI application factory."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from homelab_monitor import __version__
from homelab_monitor.kernel.api.errors import register_error_handlers
from homelab_monitor.kernel.api.lifespan import lifespan
from homelab_monitor.kernel.api.middleware import (
    AccessLogMiddleware,
    AuthMiddleware,
    CspHeadersMiddleware,
    RequestIdMiddleware,
)
from homelab_monitor.kernel.api.routers import (
    admin,
    alerts,
    collectors,
    crons,
    events,
    grafana,
    health,
    heartbeat,
    karma,
    logs,
    metrics,
    observability,
)
from homelab_monitor.kernel.api.routers import auth as auth_router

logger = logging.getLogger(__name__)


def create_app(*, lifespan_enabled: bool = True) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        lifespan_enabled: If True, attach the async lifespan context manager
            (database, scheduler, etc.). If False, create schema-only mode for
            OpenAPI export (no I/O).

    Returns:
        FastAPI: Configured application instance.
    """
    app = FastAPI(
        title="homelab-monitor",
        version=__version__,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=lifespan if lifespan_enabled else None,
    )

    # Register exception handlers
    register_error_handlers(app)

    # Add middleware. Starlette wraps in REVERSE registration order — the LAST
    # add_middleware call is the OUTERMOST layer at runtime (fires last in request).
    # Desired request flow (IN → OUT):
    #   RequestIdMiddleware → AuthMiddleware → AccessLogMiddleware → CspHeadersMiddleware → handler
    # So registration order (LAST added = outermost):
    #   add_middleware(CspHeadersMiddleware) # innermost (closest to handler) — first added
    #   add_middleware(AccessLogMiddleware)  # middle
    #   add_middleware(AuthMiddleware)       # middle (resolves auth)
    #   add_middleware(RequestIdMiddleware)  # outermost (fires first on request, last on response)
    app.add_middleware(CspHeadersMiddleware)
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(RequestIdMiddleware)

    # Include routers
    app.include_router(health.router, prefix="/api")
    app.include_router(auth_router.router, prefix="/api")
    app.include_router(collectors.router, prefix="/api")
    app.include_router(events.router, prefix="/api")
    app.include_router(metrics.router, prefix="/api")
    app.include_router(logs.router, prefix="/api")
    app.include_router(alerts.router, prefix="/api")
    app.include_router(admin.router, prefix="/api")
    app.include_router(karma.router, prefix="/api")
    app.include_router(grafana.router, prefix="/api")
    app.include_router(heartbeat.router, prefix="/api")
    app.include_router(crons.router, prefix="/api")
    app.include_router(observability.router)  # mounted at root: /metrics

    # Mount the built UI as static files with SPA fallback.
    # In production (docker), HOMELAB_MONITOR_UI_DIR defaults to /app/ui (where the built UI lives).
    # In dev (local), the UI is served separately by Vite, so this mount is skipped.
    ui_dir = Path(os.getenv("HOMELAB_MONITOR_UI_DIR", "/app/ui"))
    index_html = ui_dir / "index.html"
    if ui_dir.is_dir() and index_html.is_file():
        # Starlette's StaticFiles with html=True enables SPA mode:
        # - Serves files (JS, CSS, etc.) as-is when they match.
        # - For non-existent paths (client-side router routes), falls back to index.html.
        # - API routes (already handled by FastAPI routers above) are not affected.
        app.mount("/", StaticFiles(directory=ui_dir, html=True), name="ui")
        logger.info(f"UI mounted at / from {ui_dir}")
    else:
        logger.debug(
            f"UI directory not found or incomplete: {ui_dir} (expected {index_html}). "
            "Skipping UI mount."
        )

    return app
