"""Whole-folder content hash for runbook drift detection (STAGE-009-016).

Hashes every allow-listed file under a runbook folder (not just runbook.yaml)
so edits to README.md, CLAUDE.md, or auxiliary scripts invalidate a pinned
dry-run approval. Supersedes the config-only hash from STAGE-009-001.

Algorithm: sorted (byte-wise, POSIX-relative-path) file enumeration; each file
contributes `path\x00size\x00content\x00` to a single SHA-256 hasher; text
files (all allow-listed extensions) get CRLF->LF normalization before hashing.
Output is prefixed `v2:sha256:` — a permanent audit-clarity marker
distinguishing post-stage hashes from pre-stage bare-hex (v1) hashes stored in
`runbooks.content_hash` / `runbook_run_approvals.pinned_runbook_hash`.

Hard-reject cases (raise RunbookHashError, fail-loud posture): symlinks,
non-ASCII filenames, files >1 MiB, folders with >50 files, non-allowlisted
extensions.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {".md", ".yaml", ".yml", ".sh", ".txt", ".json", ".toml"}
)
MAX_FILE_SIZE: int = 1024 * 1024  # 1 MiB
MAX_FILE_COUNT: int = 50
HASH_PREFIX: str = "v2:sha256:"


class RunbookHashError(ValueError):
    """Raised when a runbook folder fails a hard-reject content-hash check."""


def compute_runbook_content_hash(folder: Path) -> str:
    """Return the v2 whole-folder content hash of ``folder``.

    Args:
        folder: Absolute path to a runbook folder.

    Returns:
        ``f"{HASH_PREFIX}<64-hex-sha256>"``.

    Raises:
        RunbookHashError: on symlink, non-ASCII filename, oversized file,
            too many files, or a non-allowlisted extension.
    """
    # Enumerate everything (files AND directories) so we can hard-reject symlinks
    # at any depth before filtering. rglob's is_file() filter would silently skip
    # symlinked directories, but Design requires a loud reject to avoid a latent
    # trap for future refactors that might enable follow-symlinks.
    all_entries = list(folder.rglob("*"))
    for entry in all_entries:
        if entry.is_symlink():
            raise RunbookHashError(f"runbook folder contains symlink: {entry}")

    # rglob is recursive: real (non-symlinked) subdirectories ARE traversed and
    # their files ARE included in the hash. This is intentional — a runbook that
    # organizes helper scripts under sub/ MUST invalidate the hash on sub/*.sh edits.
    files = [p for p in all_entries if p.is_file()]

    if len(files) > MAX_FILE_COUNT:
        raise RunbookHashError(
            f"folder exceeds file count limit ({len(files)} > {MAX_FILE_COUNT}): {folder}"
        )

    for path in files:
        if not path.name.isascii():
            raise RunbookHashError(f"filename contains non-ASCII characters: {path}")
        size = path.stat().st_size
        if size > MAX_FILE_SIZE:
            raise RunbookHashError(
                f"file exceeds size limit ({size} > {MAX_FILE_SIZE} bytes): {path}"
            )
        if path.suffix not in ALLOWED_EXTENSIONS:
            raise RunbookHashError(
                f"file has non-allowlisted extension: {path} "
                f"(allowed: {sorted(ALLOWED_EXTENSIONS)})"
            )

    sorted_files = sorted(files, key=lambda p: p.relative_to(folder).as_posix().encode())

    hasher = hashlib.sha256()
    for path in sorted_files:
        rel = path.relative_to(folder).as_posix().encode()
        content = path.read_bytes()
        # All allowlisted extensions are text; normalize CRLF -> LF.
        content = content.replace(b"\r\n", b"\n")
        hasher.update(rel + b"\x00" + str(len(content)).encode() + b"\x00" + content + b"\x00")

    return f"{HASH_PREFIX}{hasher.hexdigest()}"


__all__ = [
    "ALLOWED_EXTENSIONS",
    "HASH_PREFIX",
    "MAX_FILE_COUNT",
    "MAX_FILE_SIZE",
    "RunbookHashError",
    "compute_runbook_content_hash",
]
