"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from homelab_monitor import __version__
from homelab_monitor.kernel.api.errors import register_error_handlers
from homelab_monitor.kernel.api.lifespan import lifespan
from homelab_monitor.kernel.api.middleware import (
    AccessLogMiddleware,
    DevAuthMiddleware,
    RequestIdMiddleware,
)
from homelab_monitor.kernel.api.routers import collectors, events, health


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
    # add_middleware call is the OUTERMOST layer at runtime. So:
    # - RequestIdMiddleware fires FIRST (binds request_id to contextvars)
    # - AccessLogMiddleware fires next (logs the request, sees request_id)
    # - DevAuthMiddleware fires LAST (gates the actual handler dispatch)
    app.add_middleware(DevAuthMiddleware)
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIdMiddleware)

    # Include routers
    app.include_router(health.router, prefix="/api")
    app.include_router(collectors.router, prefix="/api")
    app.include_router(events.router, prefix="/api")

    return app
