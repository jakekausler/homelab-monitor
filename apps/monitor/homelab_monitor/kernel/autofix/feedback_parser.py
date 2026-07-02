"""Feedback sentinel parser (STAGE-009-009).

Reads ``*.feedback.json`` files from the transcript directory and returns
parsed items, or a synthetic ``PARSE_ERROR`` item that the caller persists
as one row + emits an ``autofix.feedback_parse_error`` audit event.

Contract (Decision C3):
  - File unreadable (OSError): return ``[]`` and let the caller log at debug.
    This is treated as "no feedback" — a claude that never wrote a sentinel
    is indistinguishable from one whose file we cannot read, and both are
    non-fatal. The orchestrator continues.
  - File readable but content invalid (not JSON, wrong top-level, missing
    required keys, structured_hint not-dict): return exactly one
    ``ParsedFeedbackItem`` with ``kind=PARSE_ERROR`` whose
    ``suggestion_text`` is the raw content truncated to
    ``SUGGESTION_TEXT_MAX`` chars with ``TRUNCATION_SUFFIX`` appended when
    truncated. Structured hint None.
  - Unknown ``kind`` value on an otherwise well-formed item: downgrade to
    ``FeedbackKind.OTHER`` (loose forward-compat; do NOT raise parse_error).
  - ``suggestion_text`` longer than ``SUGGESTION_TEXT_MAX``: truncate and
    append ``TRUNCATION_SUFFIX``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from homelab_monitor.kernel.autofix.types import (
    SUGGESTION_TEXT_MAX,
    TRUNCATION_SUFFIX,
    FeedbackKind,
    FeedbackParseError,
)


@dataclass(frozen=True, slots=True)
class ParsedFeedbackItem:
    """A single parsed feedback item before persistence assigns id + created_at."""

    kind: FeedbackKind
    suggestion_text: str
    structured_hint: dict[str, object] | None


def scan_transcript_dir_for_feedback(transcript_dir: str, snapshot_before: set[str]) -> Path | None:
    """Enumerate ``*.feedback.json`` files that did NOT exist in
    ``snapshot_before``. Return the newest by mtime, or None.

    Mirror of ``_resolve_transcript`` in orchestrator.py (same
    diff-against-snapshot pattern; different suffix, no mtime-window guard).

    Callers MUST invoke this from within the orchestrator's ``_transcript_lock``
    critical section. Without lock coverage, concurrent runs can misattribute
    feedback files across runs. See ``orchestrator.py::_exec_claude`` for the
    invariant.
    """
    try:
        after = set(os.listdir(transcript_dir))
    except OSError:
        return None
    candidates: list[str] = []
    for name in after - snapshot_before:
        if not name.endswith(".feedback.json"):
            continue
        candidates.append(name)
    if not candidates:
        return None
    newest = max(
        candidates,
        key=lambda n: os.path.getmtime(os.path.join(transcript_dir, n)),
    )
    return Path(transcript_dir) / newest


def parse_feedback_file(path: Path) -> list[ParsedFeedbackItem]:
    """Read + parse ``path``. Contract per module docstring.

    Returns:
      - ``[]`` on OSError (unreadable file — treated as no feedback).
      - one synthetic ``PARSE_ERROR`` item on any content-validity failure.
      - otherwise, the parsed items in file order.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [_synthesize_parse_error(raw, f"invalid JSON: {exc}")]

    if not isinstance(payload, list):
        return [
            _synthesize_parse_error(raw, f"top-level must be array, got {type(payload).__name__}")
        ]

    parsed: list[ParsedFeedbackItem] = []
    items: list[object] = cast(list[object], payload)
    for idx, item in enumerate(items):
        try:
            parsed.append(_parse_item(item))
        except FeedbackParseError as exc:
            return [_synthesize_parse_error(raw, f"item {idx}: {exc.detail}")]
    return parsed


def _parse_item(item: object) -> ParsedFeedbackItem:
    if not isinstance(item, dict):
        raise FeedbackParseError(f"item must be object, got {type(item).__name__}")
    item_dict: dict[str, object] = cast(dict[str, object], item)
    if "kind" not in item_dict:
        raise FeedbackParseError("missing required key 'kind'")
    if "suggestion_text" not in item_dict:
        raise FeedbackParseError("missing required key 'suggestion_text'")
    raw_kind: object = item_dict["kind"]
    if not isinstance(raw_kind, str):
        raise FeedbackParseError(f"'kind' must be string, got {type(raw_kind).__name__}")
    raw_text: object = item_dict["suggestion_text"]
    if not isinstance(raw_text, str):
        raise FeedbackParseError(f"'suggestion_text' must be string, got {type(raw_text).__name__}")
    raw_hint: object = item_dict.get("structured_hint")
    if raw_hint is not None and not isinstance(raw_hint, dict):
        raise FeedbackParseError(
            f"'structured_hint' must be object or null, got {type(raw_hint).__name__}"
        )
    # Downgrade unknown kind → OTHER (loose forward-compat).
    try:
        kind = FeedbackKind(raw_kind)
    except ValueError:
        kind = FeedbackKind.OTHER
    # PARSE_ERROR reserved for the parser's own use — treat as OTHER on wire.
    if kind is FeedbackKind.PARSE_ERROR:
        kind = FeedbackKind.OTHER
    return ParsedFeedbackItem(
        kind=kind,
        suggestion_text=_truncate(raw_text),
        structured_hint=dict(cast(dict[str, object], raw_hint)) if raw_hint is not None else None,
    )


def _synthesize_parse_error(raw_content: str, err_msg: str) -> ParsedFeedbackItem:
    """Build the single-row PARSE_ERROR item. ``err_msg`` is packed into the
    text alongside the raw content so the operator can see what failed.
    """
    body = f"parse_error: {err_msg}\n---\n{raw_content}"
    return ParsedFeedbackItem(
        kind=FeedbackKind.PARSE_ERROR,
        suggestion_text=_truncate(body),
        structured_hint=None,
    )


def _truncate(text: str) -> str:
    if len(text) <= SUGGESTION_TEXT_MAX:
        return text
    return text[: SUGGESTION_TEXT_MAX - len(TRUNCATION_SUFFIX)] + TRUNCATION_SUFFIX


__all__ = [
    "ParsedFeedbackItem",
    "parse_feedback_file",
    "scan_transcript_dir_for_feedback",
]
