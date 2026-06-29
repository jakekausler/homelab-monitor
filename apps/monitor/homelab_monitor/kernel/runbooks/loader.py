"""Pure filesystem loader for the runbook registry.

Scans a runbooks-root directory for runbook folders. Each runbook folder must
contain a ``runbook.yaml`` (parsed + validated via RunbookConfig.load_from_path)
and a non-empty ``CLAUDE.md`` (presence + non-empty only; NOT parsed here).

CLAUDE.md is the prompt body consumed by the STAGE-009-005 orchestrator; this
stage only validates it exists and is non-empty. Folders whose name starts with
``_`` are skipped (so ``runbooks/_examples/`` and exemplar folders are never
auto-registered).

This module is intentionally pure: no DB, no settings, no environment. The
registry repository orchestrates loader + DB.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import yaml

from homelab_monitor.kernel.runbooks.config import RunbookConfig

RUNBOOK_CONFIG_FILENAME = "runbook.yaml"
RUNBOOK_PROMPT_FILENAME = "CLAUDE.md"


@dataclass(slots=True, frozen=True)
class LoadedRunbook:
    """A successfully loaded runbook folder.

    ``folder`` is the absolute folder path; ``config`` is its validated config.
    """

    folder: Path
    config: RunbookConfig


@dataclass(slots=True, frozen=True)
class LoadError:
    """A per-folder validation error (reported, never fatal)."""

    path: str
    message: str


@dataclass(slots=True, frozen=True)
class ScanResult:
    """Structured result of scanning a runbooks root."""

    loaded: list[LoadedRunbook] = field(default_factory=lambda: cast(list[LoadedRunbook], []))
    errors: list[LoadError] = field(default_factory=lambda: cast(list[LoadError], []))


def scan_runbooks(root: Path) -> ScanResult:
    """Scan ``root`` for runbook folders. Pure; never raises on a bad folder.

    A folder is skipped (silently, not an error) if its name starts with ``_``.
    For each non-skipped folder, validates:
      1. ``runbook.yaml`` exists and parses + validates -> RunbookConfig.
      2. ``CLAUDE.md`` exists and is non-empty.
    Any failure on a folder appends a LoadError; other folders continue.

    If ``root`` does not exist or is not a directory, returns an empty result
    with a single LoadError for the root.
    """
    result_loaded: list[LoadedRunbook] = []
    result_errors: list[LoadError] = []

    if not root.is_dir():
        result_errors.append(
            LoadError(path=str(root), message=f"runbooks root {root} is not a directory")
        )
        return ScanResult(loaded=result_loaded, errors=result_errors)

    for folder in sorted(root.iterdir()):
        if not folder.is_dir():
            continue
        if folder.name.startswith("_"):
            continue

        config_path = folder / RUNBOOK_CONFIG_FILENAME
        prompt_path = folder / RUNBOOK_PROMPT_FILENAME

        if not config_path.is_file():
            result_errors.append(
                LoadError(
                    path=str(folder),
                    message=f"missing {RUNBOOK_CONFIG_FILENAME}",
                )
            )
            continue

        try:
            config = RunbookConfig.load_from_path(config_path)
        except (ValueError, yaml.YAMLError) as exc:
            # load_from_path raises path-tagged ValueError on non-mapping /
            # ValidationError, and propagates yaml.YAMLError unwrapped on
            # malformed YAML. Both are reported, not fatal (design rule #4).
            result_errors.append(LoadError(path=str(folder), message=str(exc)))
            continue

        if not prompt_path.is_file():
            result_errors.append(
                LoadError(
                    path=str(folder),
                    message=f"missing {RUNBOOK_PROMPT_FILENAME}",
                )
            )
            continue

        # Non-empty check: strip to treat whitespace-only as empty.
        if not prompt_path.read_text(encoding="utf-8").strip():
            result_errors.append(
                LoadError(
                    path=str(folder),
                    message=f"{RUNBOOK_PROMPT_FILENAME} is empty",
                )
            )
            continue

        result_loaded.append(LoadedRunbook(folder=folder, config=config))

    return ScanResult(loaded=result_loaded, errors=result_errors)


__all__ = [
    "RUNBOOK_CONFIG_FILENAME",
    "RUNBOOK_PROMPT_FILENAME",
    "LoadError",
    "LoadedRunbook",
    "ScanResult",
    "scan_runbooks",
]
