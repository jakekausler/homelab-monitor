"""Watched-directory mount validation + compose-mount-line generation.

Used by scripts/generate-watched-dirs-mounts.sh to turn a list of friendly host
paths (e.g. /var /tmp) into docker-compose `<host>:/host-watch/<name>:ro` volume
lines, while guarding against collisions with the monitor's EXISTING mount
namespace. All 4 collision rules ERROR-and-ABORT (raise WatchedDirError).

CLI:
    python -m homelab_monitor.kernel.watched_dirs /var /tmp /var/log
prints the YAML volume lines (one per path) to stdout; exits non-zero with a
descriptive message on any collision / invalid path.
"""

from __future__ import annotations

import os
import sys

HOST_WATCH_PREFIX = "/host-watch"


class WatchedDirError(ValueError):
    """Raised when a watched-directory list fails a collision/validity rule."""


def container_name(friendly_path: str) -> str:
    """Derive the /host-watch mount name for a friendly host path.

    Strip leading '/', reject '/' (root), replace remaining '/' with '-'.
    Mirrors WatchedDirSizeCollector._container_name exactly.
    """
    stripped = friendly_path.strip("/")
    if not stripped:
        raise WatchedDirError(f"watched directory path may not be '/': {friendly_path!r}")
    return stripped.replace("/", "-")


def container_target(friendly_path: str) -> str:
    """Map a friendly host path to its /host-watch container target."""
    return f"{HOST_WATCH_PREFIX}/{container_name(friendly_path)}"


def _normalize(path: str) -> str:
    """Normalize + require an absolute path."""
    norm = os.path.normpath(path)
    if not os.path.isabs(norm):
        raise WatchedDirError(f"watched directory path must be absolute: {path!r}")
    return norm


def validate(paths: list[str]) -> list[str]:
    """Validate friendly host paths; return the normalized list or raise.

    Watched dirs are bind-mounted into the ISOLATED ``/host-watch/`` container
    namespace (``<host>:/host-watch/<name>:ro``), which is disjoint from every
    existing monitor mount target. A watched mount therefore cannot shadow or be
    shadowed by an existing mount, so the only real collisions are between
    watched dirs themselves.

    Rules (ALL error-and-abort):
      1. duplicate path (after normalization)
      2. root path '/' is rejected (no derivable /host-watch/<name>)
      3. derived /host-watch/<name> target collides with another watched dir's
    """
    normalized = [_normalize(p) for p in paths]

    # Rule 1: duplicate path.
    seen: set[str] = set()
    for p in normalized:
        if p in seen:
            raise WatchedDirError(f"duplicate watched directory path: {p!r}")
        seen.add(p)

    # Rule 3: target-name collision under /host-watch/.
    targets: dict[str, str] = {}
    for p in normalized:
        tgt = container_target(p)
        if tgt in targets:
            raise WatchedDirError(
                f"watched directories {targets[tgt]!r} and {p!r} both map to "
                f"container target {tgt!r}; rename one"
            )
        targets[tgt] = p

    return normalized


def mount_lines(paths: list[str]) -> list[str]:
    """Return docker-compose volume lines `<host>:/host-watch/<name>:ro`."""
    normalized = validate(paths)
    return [f"{p}:{container_target(p)}:ro" for p in normalized]


def main(argv: list[str] | None = None) -> int:
    """CLI: print compose volume lines for the given friendly host paths.

    Exit 0 + lines on stdout on success; exit 2 + message on stderr on error.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        # Empty input is valid: no watched dirs -> no lines.
        return 0
    try:
        for line in mount_lines(args):
            print(line)
    except WatchedDirError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover -- exercised via subprocess in generator
    raise SystemExit(main())
