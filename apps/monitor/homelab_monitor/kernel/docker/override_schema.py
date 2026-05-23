"""Pydantic models for per-container Docker override YAML files.

Schema locked by STAGE-003-007 D-OVERRIDE-FILE-SCHEMA. The file basename
must equal the `container` field — defends against operator confusion when
a file is renamed without updating its `container:` key. extra="forbid"
turns typos into validation errors so they surface via the
docker_file_override_malformed suggestion path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator, model_validator


class ProbeOverride(BaseModel):
    """One probe definition inside a DockerContainerOverride.probes list."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["http", "tcp", "exec", "metrics"]
    name: str = "default"
    target: str
    enabled: bool = True
    interval_seconds: int = 30
    timeout_seconds: int = 10


class DockerContainerOverride(BaseModel):
    """Top-level schema for /config/plugins/docker/<container>.yaml."""

    model_config = ConfigDict(extra="forbid")

    container: str
    exec_authorized: bool = False  # D-EXEC-DUAL-GATE-FILE-OVERRIDE
    disabled: bool = False
    probes: list[ProbeOverride] = []

    @field_validator("container")
    @classmethod
    def _validate_container_matches_filename(cls, v: str, info: ValidationInfo) -> str:
        """`container` field must equal the file basename (post-strip)."""
        context = info.context
        if context is None or "filename_stem" not in context:
            raise ValueError(
                "DockerContainerOverride requires context={'filename_stem': '<name>'}; "
                "use DockerContainerOverride.load_from_path() or pass context explicitly"
            )
        expected = context["filename_stem"]
        if v != expected:
            msg = f"container field '{v}' must equal filename stem '{expected}'"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _validate_no_duplicate_probes(self) -> DockerContainerOverride:
        """Reject duplicate (kind, name) tuples within probes."""
        seen: set[tuple[str, str]] = set()
        for p in self.probes:
            key = (p.kind, p.name)
            if key in seen:
                raise ValueError(
                    f"duplicate probe identity {key!r} — "
                    f"each (kind, name) tuple must be unique within a file"
                )
            seen.add(key)
        return self

    @classmethod
    def load_from_path(cls, path: Path) -> DockerContainerOverride:
        """Read YAML from disk and return a validated instance.

        The filename stem (without extension) is threaded into the validator
        context so the `container:` field can be cross-checked. Raises
        `pydantic.ValidationError` on schema failures; raises `ValueError`
        when the YAML root is not a mapping; raises `yaml.YAMLError` on
        malformed YAML.
        """
        with path.open("r", encoding="utf-8") as fh:
            raw: object = yaml.safe_load(fh) or {}
        if not isinstance(raw, dict):
            msg = f"override file at {path} must be a YAML mapping, got {type(raw).__name__}"
            raise ValueError(msg)
        data = cast(dict[str, Any], raw)
        return cls.model_validate(data, context={"filename_stem": path.stem})


__all__ = ["DockerContainerOverride", "ProbeOverride"]
