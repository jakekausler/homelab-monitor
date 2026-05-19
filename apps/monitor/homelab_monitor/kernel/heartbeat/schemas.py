"""Pydantic query-param schemas for the heartbeat receiver endpoints.

These are NOT used as request bodies (the endpoints take no body); FastAPI
binds them to query parameters via ``Annotated[..., Query()]``.

Cap rationale:
- ``duration``: 0 .. 86400 s (24 h). Anything longer is almost certainly a
  client bug or unit confusion (ms -> s) and should 422 rather than corrupt
  the metric range.
- ``exit_code``: 0 .. 255 (POSIX ``waitpid`` truncates exit status to 8 bits).
  Anything outside is a malformed client payload.
- ``run_id``: charset [A-Za-z0-9._-], length 1-64. Becomes the cron_runs
  TEXT PRIMARY KEY; charset rejects quotes/semicolons/whitespace/control chars.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_core import PydanticUndefined

# run_id validation: client-supplied, becomes the cron_runs TEXT PRIMARY KEY.
# Charset [A-Za-z0-9._-] comfortably fits a 36-char UUID
# (/proc/sys/kernel/random/uuid) and a reasonable superset, while rejecting
# quotes/semicolons/whitespace/control chars. A violation -> HTTP 422.
_RUN_ID_PATTERN = r"^[A-Za-z0-9._-]+$"
_RUN_ID_MAX_LEN = 64


class HeartbeatStartQuery(BaseModel):
    """Query params for ``POST /api/hb/{cron_id}/start``.

    ``run_id`` (optional) is the A-mode wrapper-generated run identifier — when
    present, the receiver INSERTs a cron_runs row. Absent => legacy behavior.
    """

    model_config = ConfigDict(extra="forbid")
    run_id: str | None = Field(
        default=None,
        pattern=_RUN_ID_PATTERN,
        min_length=1,
        max_length=_RUN_ID_MAX_LEN,
    )


class HeartbeatOkQuery(BaseModel):
    """Query params for ``POST /api/hb/{cron_id}/ok``."""

    model_config = ConfigDict(extra="forbid")
    duration: float | None = Field(default=None, ge=0, le=86400)
    run_id: str | None = Field(
        default=None,
        pattern=_RUN_ID_PATTERN,
        min_length=1,
        max_length=_RUN_ID_MAX_LEN,
    )
    exit_code: int | None = Field(default=None, ge=0, le=255)


class HeartbeatFailQuery(BaseModel):
    """Query params for ``POST /api/hb/{cron_id}/fail``."""

    model_config = ConfigDict(extra="forbid")
    duration: float | None = Field(default=None, ge=0, le=86400)
    exit_code: int | None = Field(default=None, ge=0, le=255)
    run_id: str | None = Field(
        default=None,
        pattern=_RUN_ID_PATTERN,
        min_length=1,
        max_length=_RUN_ID_MAX_LEN,
    )


def query_model[T: BaseModel](model_cls: type[T]) -> Callable[..., Awaitable[T]]:
    """Return a FastAPI dependency that validates ALL query params via Pydantic AND
    exposes individual ``Query()`` parameters so OpenAPI introspection emits them.

    Strategy:
    1. Dynamically build an ``async def _dep(request, **fields)`` whose signature
       has one ``inspect.Parameter`` per model field, each annotated with the
       field's type and defaulting to a ``Query()`` marker.  FastAPI reads this
       signature to populate the OpenAPI schema.
    2. Include ``request: Request`` as the first parameter so the body can inspect
       the FULL ``request.query_params`` dict and reject rogue keys before calling
       ``model_validate``.  This preserves the ``extra='forbid'`` contract even
       though FastAPI only passes *known* query params via ``**fields``.
    3. Call ``model_cls.model_validate(dict(request.query_params))`` (not kwargs)
       so Pydantic sees every key and fires ``extra='forbid'`` for unknown ones.
    """
    # Build the dynamic parameter list.
    params: list[inspect.Parameter] = [
        inspect.Parameter(
            "request",
            kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=Request,
        )
    ]
    for field_name, field_info in model_cls.model_fields.items():
        default = field_info.default
        if default is PydanticUndefined:
            # Required field — no default
            query_default: Any = ...  # pragma: no cover
        else:
            query_default = default

        annotation = field_info.annotation
        if annotation is None:  # pragma: no cover
            annotation = Any  # type: ignore[assignment]

        params.append(
            inspect.Parameter(
                field_name,
                kind=inspect.Parameter.KEYWORD_ONLY,
                default=Query(query_default, description=field_info.description),
                annotation=annotation,
            )
        )

    async def _dep(request: Request, **_kwargs: Any) -> T:  # type: ignore[misc]  # noqa: ANN401
        # _kwargs holds FastAPI-injected known params (for OpenAPI wiring only).
        # We validate from the raw query string so extra='forbid' fires correctly.
        try:
            return model_cls.model_validate(dict(request.query_params))
        except ValidationError as exc:
            errors = exc.errors(include_url=False)
            for e in errors:
                ctx = e.get("ctx")
                if isinstance(ctx, dict) and isinstance(ctx.get("error"), Exception):
                    e["ctx"] = {"error": str(ctx["error"])}
            raise HTTPException(
                status_code=422,
                detail={"errors": errors},
            ) from exc

    # Replace the function's signature so FastAPI sees the per-field Query() params.
    _dep.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]

    return _dep  # type: ignore[return-value]
