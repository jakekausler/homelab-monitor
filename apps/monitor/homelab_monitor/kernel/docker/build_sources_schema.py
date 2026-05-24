"""Pydantic schemas for /config/docker/build-sources.yaml.

D-BUILD-SOURCES-YAML-CONFIG: supersedes HOMELAB_MONITOR_COMPOSE_DIR when present.
D-PATH-REMAP-EXPLICIT: operator enumerates host_prefix→container_prefix; first-match-wins.
D-MULTI-COMPOSE: later files override earlier ones (matches `docker compose -f a -f b`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

BuildSourcesConfigFailureReason = Literal[
    "file_not_found",
    "malformed_yaml",
    "invalid_schema",
    "non_dict_root",
    "unknown",
]


class BuildSourcesConfigError(ValueError):
    reason: BuildSourcesConfigFailureReason

    def __init__(self, message: str, *, reason: BuildSourcesConfigFailureReason) -> None:
        super().__init__(message)
        self.reason = reason


class ComposeFileEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_path: str
    container_path: str

    @model_validator(mode="after")
    def _absolute(self) -> ComposeFileEntry:
        if not self.host_path.startswith("/"):
            raise ValueError(f"host_path must be absolute; got {self.host_path!r}")
        if not self.container_path.startswith("/"):
            raise ValueError(f"container_path must be absolute; got {self.container_path!r}")
        return self


class BuildContextRemap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_prefix: str
    container_prefix: str

    @model_validator(mode="after")
    def _absolute(self) -> BuildContextRemap:
        if not self.host_prefix.startswith("/"):
            raise ValueError(f"host_prefix must be absolute; got {self.host_prefix!r}")
        if not self.container_prefix.startswith("/"):
            raise ValueError(f"container_prefix must be absolute; got {self.container_prefix!r}")
        return self


class BuildSourcesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compose_files: list[ComposeFileEntry]
    build_context_roots: list[BuildContextRemap] = []

    @model_validator(mode="after")
    def _validate(self) -> BuildSourcesConfig:
        if not self.compose_files:
            raise ValueError("compose_files must be non-empty")
        seen_cp: set[str] = set()
        for e in self.compose_files:
            if e.container_path in seen_cp:
                raise ValueError(f"duplicate container_path {e.container_path!r}")
            seen_cp.add(e.container_path)
        seen_hp: set[str] = set()
        for r in self.build_context_roots:
            if r.host_prefix in seen_hp:
                raise ValueError(f"duplicate host_prefix {r.host_prefix!r}")
            seen_hp.add(r.host_prefix)
        return self

    @classmethod
    def load_from_path(cls, path: Path) -> BuildSourcesConfig:
        try:
            raw_text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise BuildSourcesConfigError(
                f"build-sources config not found at {path}", reason="file_not_found"
            ) from exc
        except OSError as exc:  # pragma: no cover -- defensive TOCTOU
            raise BuildSourcesConfigError(
                f"failed to read {path}: {exc}", reason="unknown"
            ) from exc
        try:
            raw: object = yaml.safe_load(raw_text) or {}
        except yaml.YAMLError as exc:
            raise BuildSourcesConfigError(
                f"malformed YAML in {path}: {exc}", reason="malformed_yaml"
            ) from exc
        if not isinstance(raw, dict):
            raise BuildSourcesConfigError(
                f"build-sources root is not a mapping in {path}", reason="non_dict_root"
            )
        try:
            return cls.model_validate(cast("dict[str, Any]", raw))
        except ValidationError as exc:
            raise BuildSourcesConfigError(
                f"invalid build-sources schema in {path}: {exc}", reason="invalid_schema"
            ) from exc


__all__: Final = [
    "BuildContextRemap",
    "BuildSourcesConfig",
    "BuildSourcesConfigError",
    "BuildSourcesConfigFailureReason",
    "ComposeFileEntry",
]
