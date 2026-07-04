"""Bounded transcript reader for the runs-history API (STAGE-009-011).

Path-traversal defense-in-depth: caller passes a base_dir (the configured
FIXER_TRANSCRIPT_DIR); this reader will refuse any transcript_path that
resolves outside that base_dir. Size cap = 512 KiB; if the file is larger,
the TAIL is returned (the interesting output of a claude runbook run is
at the end) with truncated=True.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Cap for a single transcript read. 512 KiB is enough for a typical claude
# exec (~10-30 KB per turn, ~30 turns max at rate limits) with headroom;
# larger files are tail-truncated. Deliberately not configurable — this is
# the browser DOM upper bound, not an operational tunable.
MAX_TRANSCRIPT_BYTES = 512 * 1024


@dataclass(frozen=True, slots=True)
class TranscriptContent:
    text: str
    truncated: bool
    size_bytes: int


def read_transcript(transcript_path: str, *, base_dir: Path) -> TranscriptContent | None:
    """Return the transcript contents, or None on any read-blocking condition.

    Returns None when:
    - transcript_path is empty
    - transcript_path (resolved) is NOT within base_dir (path-traversal guard)
    - the file does not exist / is not a regular file

    Raises OSError on genuine IO failures (permission denied, decode
    catastrophe). Callers translate that to a 500. Missing/traversal/empty
    conditions collapse into a 404 at the router edge (same shape from the
    UI's perspective: "no transcript available").
    """
    if not transcript_path:
        return None

    resolved = Path(transcript_path).resolve()
    if not resolved.is_relative_to(base_dir):
        return None
    if not resolved.is_file():
        return None

    size_bytes = resolved.stat().st_size
    if size_bytes <= MAX_TRANSCRIPT_BYTES:
        raw = resolved.read_bytes()
        truncated = False
    else:
        # Read the tail. Use a fresh open() rather than read_bytes() so we
        # don't slurp the whole file into memory first.
        with resolved.open("rb") as fh:
            fh.seek(size_bytes - MAX_TRANSCRIPT_BYTES)
            raw = fh.read()
        truncated = True

    text = raw.decode("utf-8", errors="replace")
    return TranscriptContent(text=text, truncated=truncated, size_bytes=size_bytes)


__all__ = ["MAX_TRANSCRIPT_BYTES", "TranscriptContent", "read_transcript"]
