"""Docker intent schema, parser, and validator (STAGE-009-014).

The fixer emits ``<transcript_dir>/<run_id>/docker-intent.json`` — a JSON array
of :class:`DockerIntent` objects — after its Claude analysis. The orchestrator
parses that file post-exec, validates each intent against the runbook's
resolved :class:`~homelab_monitor.kernel.autofix.types.ResolvedGrants`
envelope, and (for valid intents in real-exec mode) invokes
``DockerSocketClient.restart_container``. This module owns the schema, the
parser, and the pure validator — the executor lives on the orchestrator.

Contract (non-negotiable #2 docker dimension, non-negotiable #3 identity):
  - Fixer never holds a docker socket. It writes JSON only.
  - Every intent is validated against the runbook's declared
    :class:`~homelab_monitor.kernel.runbooks.config.DockerCapability` envelope
    BEFORE any docker call. An intent whose container ≠ the envelope's
    ``container`` OR whose action ∉ ``allowed_actions`` is rejected without
    touching docker.
  - Missing envelope (``grants.docker_container is None``) rejects every
    intent — a runbook that did not declare docker scope cannot exercise it.

File-level failures (missing JSON, non-list top-level, per-entry validation
failure) raise :class:`MalformedIntentFileError` so the orchestrator can emit
one ``autofix.intent_malformed`` audit and stop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from homelab_monitor.kernel.autofix.types import ResolvedGrants

# Cap detail strings to prevent audit-table bloat from oversized pydantic error
# messages / IO error messages (M4).
_MAX_DETAIL_LENGTH = 1024


class DockerIntent(BaseModel):
    """One fixer-emitted docker action intent.

    ``action`` is a ``Literal["restart"]`` on purpose: the MVP surface is
    intentionally narrow, and expanding it is a deliberate schema change
    (not a config-file typo). Future actions (``stop`` / ``start`` /
    ``recreate``) join by literal-widening + validator branch + envelope
    ``allowed_actions`` alignment.
    """

    model_config = ConfigDict(extra="forbid")

    container: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,254}$",
        description=(
            "Docker container name to target. Must match Docker's container-name form: "
            "starts with an alphanumeric, then any of [A-Za-z0-9_.-], length 1-256. "
            "Enforced defense-in-depth (STAGE-009-014 I4) against path-traversal and "
            "audit-DoS via oversized/malformed names."
        ),
    )
    action: Literal["restart"]


class MalformedIntentFileError(Exception):
    """Raised by :func:`parse_intents` when the file exists but is not a
    well-formed intent document.

    ``reason`` is a short machine-stable token used by the orchestrator as
    the ``reason`` field of an ``autofix.intent_malformed`` audit row.
    ``detail`` is a longer human-readable message (may include a truncated
    exception ``repr``).

    Reason token vocabulary (locked):
      - ``"not_json"`` — file exists but ``json.loads`` failed.
      - ``"not_a_list"`` — parsed JSON is not a list at top level.
      - ``"invalid_entry"`` — an individual entry failed
        :meth:`DockerIntent.model_validate`.
    """

    def __init__(self, reason: str, *, detail: str = "") -> None:
        super().__init__(detail if detail else reason)
        self.reason = reason
        self.detail: str = detail


@dataclass(frozen=True, slots=True)
class IntentAccept:
    """Validator verdict: intent may be executed."""


@dataclass(frozen=True, slots=True)
class IntentDeny:
    """Validator verdict: intent is rejected. ``reason`` is a short human
    string suitable for the ``reason`` field of an ``autofix.intent_denied``
    audit row (also readable by operators in Karma / the runs UI)."""

    reason: str


IntentValidation = IntentAccept | IntentDeny


def parse_intents(intent_path: Path) -> list[DockerIntent]:
    """Read and parse ``intent_path``.

    Returns:
      - ``[]`` if the file does not exist (the common case — most runs have
        no intents).

    Raises:
      - :class:`MalformedIntentFileError` with ``reason="not_json"`` when
        the file exists but cannot be read (OSError / UnicodeDecodeError) or
        when ``json.loads`` fails.
      - :class:`MalformedIntentFileError` with ``reason="not_a_list"`` when
        the top-level JSON value is not a list.
      - :class:`MalformedIntentFileError` with ``reason="invalid_entry"``
        when any individual entry fails :meth:`DockerIntent.model_validate`.
        ``detail`` includes the pydantic error and the entry index.
    """
    if not intent_path.exists():
        return []
    try:
        raw = intent_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # An existing-but-unreadable file is treated as malformed rather than
        # silently ignored: a runbook that emitted an intent file we cannot
        # read is a state the operator must see. UnicodeDecodeError is raised
        # when the file contains non-UTF8 bytes; OSError covers permission/IO
        # failures.
        # Fix M4: Truncate detail to ≤1024 chars to prevent large payloads.
        raw_detail = f"read failed: {exc}"
        if len(raw_detail) > _MAX_DETAIL_LENGTH:
            detail = raw_detail[:_MAX_DETAIL_LENGTH] + "..."
        else:
            detail = raw_detail
        raise MalformedIntentFileError(reason="not_json", detail=detail) from exc
    try:
        payload: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MalformedIntentFileError(reason="not_json", detail=str(exc)) from exc
    if not isinstance(payload, list):
        raise MalformedIntentFileError(
            reason="not_a_list",
            detail=f"top-level must be JSON array, got {type(payload).__name__}",
        )
    entries: list[object] = cast(list[object], payload)
    intents: list[DockerIntent] = []
    for idx, entry in enumerate(entries):
        try:
            intents.append(DockerIntent.model_validate(entry))
        except ValidationError as exc:
            # Fix M4: Truncate detail to ≤1024 chars to prevent large payloads.
            raw_detail = f"entry {idx}: {exc}"
            if len(raw_detail) > _MAX_DETAIL_LENGTH:
                detail = raw_detail[:_MAX_DETAIL_LENGTH] + "..."
            else:
                detail = raw_detail
            raise MalformedIntentFileError(
                reason="invalid_entry",
                detail=detail,
            ) from exc
    return intents


def validate_intent(intent: DockerIntent, grants: ResolvedGrants) -> IntentValidation:
    """Validate ``intent`` against the runbook's resolved envelope.

    Reject in strict order:
      1. envelope missing docker (``grants.docker_container is None``)
      2. container mismatch (``intent.container != grants.docker_container``)
      3. action not in ``grants.docker_allowed_actions``
    """
    if grants.docker_container is None:
        return IntentDeny(reason="docker not in scoped_capabilities")
    if intent.container != grants.docker_container:
        return IntentDeny(reason=f"container '{intent.container}' not in envelope")
    if intent.action not in grants.docker_allowed_actions:
        return IntentDeny(reason=f"action '{intent.action}' not in allowed_actions")
    return IntentAccept()


__all__ = [
    "DockerIntent",
    "IntentAccept",
    "IntentDeny",
    "IntentValidation",
    "MalformedIntentFileError",
    "parse_intents",
    "validate_intent",
]
