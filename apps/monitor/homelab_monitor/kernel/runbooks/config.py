"""Runbook-folder config contract (STAGE-009-001).

A runbook lives in an allow-listed folder with a YAML config file describing
(a) which alerts it matches, (b) its risk posture, (c) rate-limit / cooldown
guards, and (d) the scoped capabilities it is permitted to exercise. This
module is the PARSE + VALIDATE contract only; DB persistence (the `runbooks`
table) and discovery wiring arrive in later EPIC-009 stages.

Two gates — `enabled` and `auto_trigger` — are DELIBERATELY absent from
:class:`RunbookConfig`: they are DB-only operator switches (defaulted off in
migration 0045), never author-declarable in the config file. A runbook author
cannot self-enable or self-arm auto-trigger.

The model follows the SshTargetConfig / SubprocessManifest idioms: pydantic v2,
``extra="forbid"`` everywhere, a ``@classmethod load_from_path`` that wraps
``ValidationError`` into a ``ValueError`` carrying the file path for operator
context.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

# Runbook name regex: lower-case start, then lower-case letters / digits / "_" /
# "-", 3-64 chars. Same value as plugins.PLUGIN_NAME_PATTERN today but kept a
# DISTINCT constant: a runbook is not a plugin, and coupling the two would make
# a future divergence a cross-module edit.
RUNBOOK_NAME_PATTERN = r"^[a-z][a-z0-9_-]{2,63}$"


class RiskTag(StrEnum):
    """Risk posture of a runbook. RISKY is the conservative default."""

    SAFE = "safe"
    RISKY = "risky"


class AlertMatcher(BaseModel):
    """One alert-matching predicate.

    Matches when the alert's ``alertname`` equals ``alertname`` (if set) AND all
    ``labels`` key/values are present on the alert. At least one of (``alertname``
    set, ``labels`` non-empty) must be provided — an empty matcher would match
    every alert, which is never the author's intent.
    """

    model_config = ConfigDict(extra="forbid")

    alertname: str | None = Field(default=None, min_length=1)
    labels: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_some_predicate(self) -> AlertMatcher:
        if self.alertname is None and not self.labels:
            msg = "AlertMatcher requires at least one of 'alertname' or non-empty 'labels'"
            raise ValueError(msg)
        return self


class DockerCapability(BaseModel):
    """Declared docker capability scope for a runbook."""

    model_config = ConfigDict(extra="forbid")

    container: str = Field(min_length=1)
    allowed_actions: list[str] = Field(default_factory=list)


class SshCapability(BaseModel):
    """Declared SSH capability scope: a READ-only reference to an ssh_targets id.

    No inline connection details — the target's host/key/forced-command live in
    the ``ssh_targets`` config (kernel/ssh/config.py), referenced by ``target_id``.
    """

    model_config = ConfigDict(extra="forbid")

    target_id: str = Field(min_length=1)


class ScopedCapabilities(BaseModel):
    """The capability envelope a runbook is permitted to exercise.

    At least one of ``docker`` / ``ssh`` must be declared — a runbook with NO
    actionable scope cannot do anything useful, and per the auto-fix safety
    model a runbook MUST declare its scope (defense-in-depth: no implicit broad
    access). ``egress`` is an additive allow-list of network destinations and
    does not by itself constitute a scope.
    """

    model_config = ConfigDict(extra="forbid")

    docker: DockerCapability | None = None
    ssh: SshCapability | None = None
    egress: list[Annotated[str, StringConstraints(min_length=1)]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_some_scope(self) -> ScopedCapabilities:
        if self.docker is None and self.ssh is None:
            msg = "scoped_capabilities must declare at least one of 'docker' or 'ssh'"
            raise ValueError(msg)
        return self


class RunbookConfig(BaseModel):
    """Validated runbook-folder config.

    Conservative defaults: ``risk_tag`` defaults to RISKY, ``dry_run_required``
    to True. ``rate_limit_per_hour`` / ``cooldown_seconds`` / ``scoped_capabilities``
    are REQUIRED (the author must consciously declare guard budgets and scope).
    """

    model_config = ConfigDict(extra="forbid")

    runbook: Literal[1] = Field(default=1, description="Schema version")
    name: str = Field(pattern=RUNBOOK_NAME_PATTERN)
    match_patterns: list[AlertMatcher] = Field(min_length=1)
    risk_tag: RiskTag = Field(default=RiskTag.RISKY)
    dry_run_required: bool = True
    rate_limit_per_hour: int = Field(ge=0)
    cooldown_seconds: int = Field(ge=0)
    scoped_capabilities: ScopedCapabilities

    @classmethod
    def load_from_path(cls, config_path: Path) -> RunbookConfig:
        """Read a runbook config YAML from disk and return a validated instance.

        Raises:
            ValueError: file root is not a mapping, or the content fails
                field/model validation (the pydantic ``ValidationError`` is
                wrapped with the file path for operator context).
            yaml.YAMLError: malformed YAML (propagated unwrapped).
        """
        with config_path.open("r", encoding="utf-8") as fh:
            data: object = yaml.safe_load(fh)
        if not isinstance(data, dict):
            msg = (
                f"runbook config at {config_path} must be a YAML mapping, got {type(data).__name__}"
            )
            raise ValueError(msg)
        mapping = cast(dict[str, Any], data)
        try:
            return cls.model_validate(mapping)
        except ValidationError as exc:
            msg = f"runbook config at {config_path} is invalid: {exc}"
            raise ValueError(msg) from exc


__all__ = [
    "RUNBOOK_NAME_PATTERN",
    "AlertMatcher",
    "DockerCapability",
    "RiskTag",
    "RunbookConfig",
    "ScopedCapabilities",
    "SshCapability",
]
