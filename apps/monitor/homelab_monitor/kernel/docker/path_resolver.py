"""PathResolver — apply host_prefix→container_prefix remaps. First-match-wins."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from homelab_monitor.kernel.docker.build_sources_schema import BuildContextRemap


class PathResolver:
    def __init__(self, remaps: Sequence[BuildContextRemap]) -> None:
        self._remaps: tuple[BuildContextRemap, ...] = tuple(remaps)

    def resolve(self, host_path: str | Path) -> Path:
        """Apply host_prefix→container_prefix remaps using path-segment boundary semantics.

        Args:
            host_path: Path to resolve. Behavior is undefined if input has trailing slashes;
                       callers should normalize with path-resolution tools before passing.

        Returns:
            Remapped path if a matching rule exists (first-match-wins), otherwise the
            input path as-is.
        """
        s = str(host_path)
        for r in self._remaps:
            if s == r.host_prefix or s.startswith(r.host_prefix + "/"):
                return Path(r.container_prefix + s[len(r.host_prefix) :])
        return Path(s)


__all__: Final = ["PathResolver"]
