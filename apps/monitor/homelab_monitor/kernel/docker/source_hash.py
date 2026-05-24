"""Build-context source-tree hasher (STAGE-003-009).

D-HASHING-BOUNDED + D-HASHING-LIMITS-STAGEDOC-PLUS-ABORT. Deterministic
SHA-256 of the build-context tree, respecting .dockerignore via pathspec.

Limits (env-overridable):
  HOMELAB_MONITOR_BUILD_HASH_MAX_FILE_BYTES   default 10*1024*1024 (10 MB)
  HOMELAB_MONITOR_BUILD_HASH_MAX_TOTAL_BYTES  default 1*1024**3   (1 GB)
  HOMELAB_MONITOR_BUILD_HASH_MAX_FILE_COUNT   default 50_000
  HOMELAB_MONITOR_BUILD_HASH_MAX_DEPTH        default 30

On exceed: returns SourceHashResult(hash="OVERSIZED:<reason>",
exceeded=<reason>). Sentinel hash never matches any real hash, so
update_available flips to 1 and the badge surfaces "needs operator
attention".

Per-file failure (permission denied, symlink-target gone) → skipped,
counted, logged by the caller. The hasher does NOT follow symlinks
(followlinks=False on os.walk).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import pathspec

_DEFAULT_MAX_FILE_BYTES: Final[int] = 10 * 1024 * 1024
_DEFAULT_MAX_TOTAL_BYTES: Final[int] = 1 * 1024**3
_DEFAULT_MAX_FILE_COUNT: Final[int] = 50_000
_DEFAULT_MAX_DEPTH: Final[int] = 30

_DOCKERIGNORE_FILENAME: Final[str] = ".dockerignore"

ExceededReason = Literal["context_too_large", "permission_denied"]


@dataclass(frozen=True, slots=True)
class SourceHashLimits:
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES
    max_file_count: int = _DEFAULT_MAX_FILE_COUNT
    max_depth: int = _DEFAULT_MAX_DEPTH

    @classmethod
    def from_env(cls) -> SourceHashLimits:
        """Resolve limits from env vars; fall back to defaults on parse failure."""
        return cls(
            max_file_bytes=_resolve_int(
                "HOMELAB_MONITOR_BUILD_HASH_MAX_FILE_BYTES", _DEFAULT_MAX_FILE_BYTES
            ),
            max_total_bytes=_resolve_int(
                "HOMELAB_MONITOR_BUILD_HASH_MAX_TOTAL_BYTES", _DEFAULT_MAX_TOTAL_BYTES
            ),
            max_file_count=_resolve_int(
                "HOMELAB_MONITOR_BUILD_HASH_MAX_FILE_COUNT", _DEFAULT_MAX_FILE_COUNT
            ),
            max_depth=_resolve_int("HOMELAB_MONITOR_BUILD_HASH_MAX_DEPTH", _DEFAULT_MAX_DEPTH),
        )


@dataclass(frozen=True, slots=True)
class SourceHashResult:
    hash: str  # "<64-hex>" OR "OVERSIZED:<reason>"
    files_hashed: int
    bytes_hashed: int
    files_skipped: int  # per-file errors (permission denied, vanished, oversized-single-file)
    exceeded: ExceededReason | None  # set when an abort-on-exceed limit fired


def _resolve_int(env_var: str, default: int) -> int:
    raw = os.environ.get(env_var)
    if not raw:
        return default
    try:
        v = int(raw)
        if v < 1:
            return default
        return v
    except ValueError:
        return default


def _load_dockerignore(build_context: Path) -> pathspec.PathSpec:
    """Load .dockerignore from the build context root, if present."""
    path = build_context / _DOCKERIGNORE_FILENAME
    if not path.is_file():
        return pathspec.PathSpec.from_lines("gitwildmatch", [])
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:  # pragma: no cover -- requires hardware fault between is_file and read_text
        return pathspec.PathSpec.from_lines("gitwildmatch", [])
    return pathspec.PathSpec.from_lines("gitwildmatch", lines)


def compute_source_hash(  # noqa: PLR0912, PLR0915 -- bounded loop with 4 explicit guards + per-file try/except; refactoring would obscure the bounded contract
    build_context: Path,
    *,
    limits: SourceHashLimits | None = None,
) -> SourceHashResult:
    """Deterministically hash the build-context tree.

    Returns SourceHashResult with sentinel hash on abort-on-exceed.

    Determinism: walks os.walk(followlinks=False), filters via .dockerignore,
    sorts by relpath, hashes each file's content with SHA-256, then hashes
    the sorted concatenation of "<relpath>\\0<filesha256>\\0" entries.

    Per-file failures (permission denied, vanished mid-walk) are SKIPPED
    and counted in `files_skipped`. Whole-walk failures (build_context
    doesn't exist) are caller's responsibility — pass an extant path.
    """
    limits = limits or SourceHashLimits()
    ignore = _load_dockerignore(build_context)

    file_entries: list[tuple[str, str]] = []  # (relpath, file_sha256_hex)
    total_bytes = 0
    files_hashed = 0
    files_skipped = 0
    exceeded: ExceededReason | None = None
    root_depth = len(build_context.parts)

    for dirpath_str, dirnames, filenames in os.walk(build_context, followlinks=False):
        dirpath = Path(dirpath_str)
        depth = len(dirpath.parts) - root_depth
        if depth >= limits.max_depth:
            # Don't descend deeper than max_depth.
            dirnames[:] = []

        # Prune ignored directories in place (skip their contents entirely).
        kept_dirs: list[str] = []
        for d in dirnames:
            rel = _relpath(dirpath / d, build_context)
            # pathspec gitwildmatch expects "/"-suffix to match directories.
            if not ignore.match_file(rel + "/") and not ignore.match_file(rel):
                kept_dirs.append(d)
        dirnames[:] = kept_dirs

        for fname in sorted(filenames):
            # Docker never includes .dockerignore itself in the build context.
            if fname == _DOCKERIGNORE_FILENAME and dirpath == build_context:
                continue
            file_path = dirpath / fname
            rel = _relpath(file_path, build_context)
            if ignore.match_file(rel):
                continue
            if file_path.is_symlink():
                # D-HASHING-BOUNDED: don't follow symlinks; record path but
                # use the link target string as the "content" so any change
                # is reflected.
                try:
                    target = os.readlink(file_path)
                except OSError:  # pragma: no cover -- symlink vanishes mid-walk
                    files_skipped += 1
                    continue
                fh = hashlib.sha256()
                fh.update(b"symlink\0")
                fh.update(target.encode("utf-8", errors="replace"))
                file_entries.append((rel, fh.hexdigest()))
                files_hashed += 1
                if files_hashed > limits.max_file_count:
                    exceeded = "context_too_large"
                    break
                continue
            try:
                size = file_path.stat().st_size
            except (FileNotFoundError, PermissionError):  # pragma: no cover -- TOCTOU/chmod race
                files_skipped += 1
                continue
            if size > limits.max_file_bytes:
                # D-HASHING-LIMITS-STAGEDOC-PLUS-ABORT: abort the whole walk.
                exceeded = "context_too_large"
                break
            if total_bytes + size > limits.max_total_bytes:
                exceeded = "context_too_large"
                break
            try:
                file_hash = _hash_file(file_path)
            except PermissionError:
                files_skipped += 1
                exceeded = "permission_denied"
                break
            except (FileNotFoundError, OSError):  # pragma: no cover -- TOCTOU or I/O error mid-read
                files_skipped += 1
                continue
            file_entries.append((rel, file_hash))
            files_hashed += 1
            total_bytes += size
            if files_hashed > limits.max_file_count:
                exceeded = "context_too_large"
                break
        if exceeded is not None:
            break

    if exceeded is not None:
        return SourceHashResult(
            hash=f"OVERSIZED:{exceeded}",
            files_hashed=files_hashed,
            bytes_hashed=total_bytes,
            files_skipped=files_skipped,
            exceeded=exceeded,
        )

    # Deterministic outer hash: sort by relpath, concatenate.
    file_entries.sort()
    outer = hashlib.sha256()
    for rel, fh in file_entries:
        outer.update(rel.encode("utf-8", errors="replace"))
        outer.update(b"\0")
        outer.update(fh.encode("ascii"))
        outer.update(b"\0")
    return SourceHashResult(
        hash=outer.hexdigest(),
        files_hashed=files_hashed,
        bytes_hashed=total_bytes,
        files_skipped=files_skipped,
        exceeded=None,
    )


def _hash_file(path: Path, *, chunk_size: int = 64 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _relpath(p: Path, root: Path) -> str:
    """Forward-slash relative path for pathspec + hash determinism."""
    return p.relative_to(root).as_posix()


__all__ = [
    "ExceededReason",
    "SourceHashLimits",
    "SourceHashResult",
    "compute_source_hash",
]
