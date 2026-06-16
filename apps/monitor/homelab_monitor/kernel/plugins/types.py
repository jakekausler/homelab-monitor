"""Type definitions for the collector plugin layer.

Includes :class:`RunKind` and :class:`TrustLevel` enums, the :class:`CollectorConfig`
base model (subclassed per plugin to add plugin-specific fields), the
:data:`CollectorEvent` discriminated union (4 payload kinds), and
:class:`CollectorResult`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# Plugin name regex: lower-case start, then lower-case letters / digits / "_" / "-",
# 3-64 chars total. Validated on `CollectorConfig.name`.
PLUGIN_NAME_PATTERN = r"^[a-z][a-z0-9_-]{2,63}$"


class RunKind(StrEnum):
    """Where a collector's ``run`` coroutine executes.

    - ``ASYNC``: shares the FastAPI event loop (most I/O-bound collectors).
    - ``THREAD``: runs in a worker thread (sync libraries — paramiko, certain SNMP libs).
    - ``PROCESS``: runs in a worker subprocess (CPU-heavy work; pickled context).
    - ``SUBPROCESS``: spawned OS process; JSON line protocol; STAGE-001-009+.
    """

    ASYNC = "async"
    THREAD = "thread"
    PROCESS = "process"
    SUBPROCESS = "subprocess"


class TrustLevel(StrEnum):
    """Privilege tier used by the plugin host to gate capabilities.

    - ``BUILTIN``: ships in this monorepo; full kernel access.
    - ``TRUSTED``: third-party but vetted; full kernel access by default.
    - ``UNTRUSTED``: forced into subprocess execution; no DB writes; only declared secrets.
    """

    BUILTIN = "builtin"
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


class CollectorConfig(BaseModel):
    """Base config for any collector. Plugin authors subclass to add plugin-specific fields.

    Validated by Pydantic at registration time. ``name`` is regex-checked against
    :data:`PLUGIN_NAME_PATTERN` (``[a-z][a-z0-9_-]{2,63}``).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=PLUGIN_NAME_PATTERN)
    interval_seconds: int = Field(default=60, ge=1)
    timeout_seconds: int = Field(default=30, ge=1)
    enabled: bool = True
    quarantine_after: int | None = Field(default=None, ge=1)


# --- CollectorEvent payloads ----------------------------------------------------------------
#
# SCAFFOLDING: Each payload here is intentionally minimal. Future stages (especially
# STAGE-001-009 subprocess plugins, EPIC-002 heartbeats, EPIC-004 log signatures)
# will extend these with additional fields. The discriminator field ``kind`` is
# load-bearing — never rename it without updating subprocess JSON protocol §5.3.


class SuggestionEvent(BaseModel):
    """A user-visible suggestion emitted by a discoverer or collector."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["suggestion"] = "suggestion"
    title: str
    body: str
    severity: Literal["info", "warning"] = "info"


class AlertForwardEvent(BaseModel):
    """An alert the collector wants the dispatcher to forward."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["alert_forward"] = "alert_forward"
    fingerprint: str
    summary: str
    severity: Literal["info", "warning", "critical"] = "warning"


class LogSignatureEvent(BaseModel):
    """A clustered log signature with an example line."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["log_signature"] = "log_signature"
    signature: str
    count: int = Field(ge=1)
    sample_line: str


class HeartbeatEvent(BaseModel):
    """A scheduled-job heartbeat: did this thing run on time?"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["heartbeat"] = "heartbeat"
    name: str
    state: Literal["ok", "missed", "late"]


CollectorEvent = Annotated[
    SuggestionEvent | AlertForwardEvent | LogSignatureEvent | HeartbeatEvent,
    Field(discriminator="kind"),
]
"""Discriminated union of every event a collector may emit.

Use ``pydantic.TypeAdapter(CollectorEvent)`` to validate raw dicts (e.g. when
events arrive over the subprocess JSON bridge in STAGE-001-009).
"""


class CollectorResult(BaseModel):
    """The return value of :meth:`Collector.run` — success/failure + emitted artifacts."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    metrics_emitted: int = Field(default=0, ge=0)
    errors: list[str] = Field(default_factory=list)
    # type: ignore[arg-type] justification: Annotated[Union, Field(discriminator=)]
    # alias confuses default_factory typing; runtime is fine.
    events: list[CollectorEvent] = Field(default_factory=lambda: [])  # type: ignore[arg-type]
    duration_seconds: float = Field(default=0.0, ge=0.0)
