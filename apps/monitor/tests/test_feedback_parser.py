"""Unit tests for the feedback sentinel parser (STAGE-009-009).

Covers every branch in:
  - parse_feedback_file / _parse_item / _synthesize_parse_error / _truncate
  - scan_transcript_dir_for_feedback

100% branch coverage target on feedback_parser.py.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from homelab_monitor.kernel.autofix.feedback_parser import (
    ParsedFeedbackItem,
    parse_feedback_file,
    scan_transcript_dir_for_feedback,
)
from homelab_monitor.kernel.autofix.types import (
    SUGGESTION_TEXT_MAX,
    TRUNCATION_SUFFIX,
    FeedbackKind,
)

# ---------------------------------------------------------------------------
# parse_feedback_file: valid content
# ---------------------------------------------------------------------------


def test_parse_valid_single_item(tmp_path: Path) -> None:
    path = tmp_path / "a.feedback.json"
    path.write_text(
        json.dumps(
            [
                {
                    "kind": "missing_capability",
                    "suggestion_text": "need ssh access",
                    "structured_hint": {"target": "udm"},
                }
            ]
        ),
        encoding="utf-8",
    )
    items = parse_feedback_file(path)
    assert len(items) == 1
    assert items[0] == ParsedFeedbackItem(
        kind=FeedbackKind.MISSING_CAPABILITY,
        suggestion_text="need ssh access",
        structured_hint={"target": "udm"},
    )


def test_parse_valid_multi_item_preserves_order(tmp_path: Path) -> None:
    path = tmp_path / "a.feedback.json"
    path.write_text(
        json.dumps(
            [
                {"kind": "config_change", "suggestion_text": "first"},
                {"kind": "blocked", "suggestion_text": "second"},
                {"kind": "worked_around", "suggestion_text": "third"},
            ]
        ),
        encoding="utf-8",
    )
    items = parse_feedback_file(path)
    assert len(items) == 3  # noqa: PLR2004
    assert [i.kind for i in items] == [
        FeedbackKind.CONFIG_CHANGE,
        FeedbackKind.BLOCKED,
        FeedbackKind.WORKED_AROUND,
    ]
    assert [i.suggestion_text for i in items] == ["first", "second", "third"]


def test_parse_empty_list_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "a.feedback.json"
    path.write_text("[]", encoding="utf-8")
    assert parse_feedback_file(path) == []


# ---------------------------------------------------------------------------
# parse_feedback_file: per-item validity failures -> synthetic PARSE_ERROR
# ---------------------------------------------------------------------------


def test_parse_missing_kind_field(tmp_path: Path) -> None:
    path = tmp_path / "a.feedback.json"
    path.write_text(json.dumps([{"suggestion_text": "x"}]), encoding="utf-8")
    items = parse_feedback_file(path)
    assert len(items) == 1
    assert items[0].kind is FeedbackKind.PARSE_ERROR
    assert "missing required key 'kind'" in items[0].suggestion_text


def test_parse_missing_suggestion_text_field(tmp_path: Path) -> None:
    path = tmp_path / "a.feedback.json"
    path.write_text(json.dumps([{"kind": "other"}]), encoding="utf-8")
    items = parse_feedback_file(path)
    assert len(items) == 1
    assert items[0].kind is FeedbackKind.PARSE_ERROR
    assert "missing required key 'suggestion_text'" in items[0].suggestion_text


def test_parse_structured_hint_not_dict_or_null(tmp_path: Path) -> None:
    path = tmp_path / "a.feedback.json"
    path.write_text(
        json.dumps(
            [{"kind": "other", "suggestion_text": "x", "structured_hint": ["not", "a", "dict"]}]
        ),
        encoding="utf-8",
    )
    items = parse_feedback_file(path)
    assert len(items) == 1
    assert items[0].kind is FeedbackKind.PARSE_ERROR
    assert "structured_hint" in items[0].suggestion_text


def test_parse_item_not_object(tmp_path: Path) -> None:
    path = tmp_path / "a.feedback.json"
    path.write_text(json.dumps(["not-an-object"]), encoding="utf-8")
    items = parse_feedback_file(path)
    assert len(items) == 1
    assert items[0].kind is FeedbackKind.PARSE_ERROR
    assert "item must be object" in items[0].suggestion_text


def test_parse_kind_not_string(tmp_path: Path) -> None:
    path = tmp_path / "a.feedback.json"
    path.write_text(json.dumps([{"kind": 123, "suggestion_text": "x"}]), encoding="utf-8")
    items = parse_feedback_file(path)
    assert len(items) == 1
    assert items[0].kind is FeedbackKind.PARSE_ERROR
    assert "'kind' must be string" in items[0].suggestion_text


def test_parse_suggestion_text_not_string(tmp_path: Path) -> None:
    path = tmp_path / "a.feedback.json"
    path.write_text(json.dumps([{"kind": "other", "suggestion_text": 42}]), encoding="utf-8")
    items = parse_feedback_file(path)
    assert len(items) == 1
    assert items[0].kind is FeedbackKind.PARSE_ERROR
    assert "'suggestion_text' must be string" in items[0].suggestion_text


def test_parse_item_error_short_circuits_whole_file(tmp_path: Path) -> None:
    """Any single item-level failure aborts the whole file -> one PARSE_ERROR row."""
    path = tmp_path / "a.feedback.json"
    path.write_text(
        json.dumps(
            [
                {"kind": "other", "suggestion_text": "ok"},
                {"suggestion_text": "missing kind"},
            ]
        ),
        encoding="utf-8",
    )
    items = parse_feedback_file(path)
    assert len(items) == 1
    assert items[0].kind is FeedbackKind.PARSE_ERROR
    assert "item 1:" in items[0].suggestion_text


# ---------------------------------------------------------------------------
# parse_feedback_file: structured_hint None handling
# ---------------------------------------------------------------------------


def test_parse_structured_hint_explicit_null(tmp_path: Path) -> None:
    path = tmp_path / "a.feedback.json"
    path.write_text(
        json.dumps([{"kind": "other", "suggestion_text": "x", "structured_hint": None}]),
        encoding="utf-8",
    )
    items = parse_feedback_file(path)
    assert len(items) == 1
    assert items[0].structured_hint is None


def test_parse_structured_hint_missing_entirely(tmp_path: Path) -> None:
    path = tmp_path / "a.feedback.json"
    path.write_text(json.dumps([{"kind": "other", "suggestion_text": "x"}]), encoding="utf-8")
    items = parse_feedback_file(path)
    assert len(items) == 1
    assert items[0].structured_hint is None


# ---------------------------------------------------------------------------
# Unknown kind -> downgrade to OTHER (not a parse error)
# ---------------------------------------------------------------------------


def test_parse_unknown_kind_downgrades_to_other(tmp_path: Path) -> None:
    path = tmp_path / "a.feedback.json"
    path.write_text(
        json.dumps([{"kind": "invented_kind", "suggestion_text": "x"}]), encoding="utf-8"
    )
    items = parse_feedback_file(path)
    assert len(items) == 1
    assert items[0].kind is FeedbackKind.OTHER
    assert items[0].suggestion_text == "x"


def test_parse_kind_parse_error_on_wire_downgrades_to_other(tmp_path: Path) -> None:
    """PARSE_ERROR is reserved for synthetic rows; on the wire it downgrades to OTHER."""
    path = tmp_path / "a.feedback.json"
    path.write_text(json.dumps([{"kind": "parse_error", "suggestion_text": "x"}]), encoding="utf-8")
    items = parse_feedback_file(path)
    assert len(items) == 1
    assert items[0].kind is FeedbackKind.OTHER


# ---------------------------------------------------------------------------
# suggestion_text truncation
# ---------------------------------------------------------------------------


def test_parse_suggestion_text_over_max_is_truncated(tmp_path: Path) -> None:
    text = "x" * (SUGGESTION_TEXT_MAX + 100)
    path = tmp_path / "a.feedback.json"
    path.write_text(json.dumps([{"kind": "other", "suggestion_text": text}]), encoding="utf-8")
    items = parse_feedback_file(path)
    assert len(items) == 1
    assert len(items[0].suggestion_text) == SUGGESTION_TEXT_MAX
    assert items[0].suggestion_text.endswith(TRUNCATION_SUFFIX)


def test_parse_suggestion_text_exactly_max_not_truncated(tmp_path: Path) -> None:
    text = "x" * SUGGESTION_TEXT_MAX
    path = tmp_path / "a.feedback.json"
    path.write_text(json.dumps([{"kind": "other", "suggestion_text": text}]), encoding="utf-8")
    items = parse_feedback_file(path)
    assert len(items) == 1
    assert items[0].suggestion_text == text
    assert not items[0].suggestion_text.endswith(TRUNCATION_SUFFIX)


# ---------------------------------------------------------------------------
# Malformed top-level content
# ---------------------------------------------------------------------------


def test_parse_non_json_content(tmp_path: Path) -> None:
    path = tmp_path / "a.feedback.json"
    path.write_text("not valid json{{{", encoding="utf-8")
    items = parse_feedback_file(path)
    assert len(items) == 1
    assert items[0].kind is FeedbackKind.PARSE_ERROR
    assert "invalid JSON" in items[0].suggestion_text
    assert "not valid json{{{" in items[0].suggestion_text


def test_parse_top_level_object_not_list(tmp_path: Path) -> None:
    path = tmp_path / "a.feedback.json"
    path.write_text(json.dumps({"kind": "other", "suggestion_text": "x"}), encoding="utf-8")
    items = parse_feedback_file(path)
    assert len(items) == 1
    assert items[0].kind is FeedbackKind.PARSE_ERROR
    assert "top-level must be array" in items[0].suggestion_text


def test_parse_file_does_not_exist_returns_empty(tmp_path: Path) -> None:
    """Per module contract: unreadable file (OSError) -> [] (treated as no feedback)."""
    missing = tmp_path / "does-not-exist.feedback.json"
    assert parse_feedback_file(missing) == []


def test_parse_empty_file_is_parse_error(tmp_path: Path) -> None:
    """Zero-byte file is readable (empty string) but invalid JSON -> PARSE_ERROR."""
    path = tmp_path / "a.feedback.json"
    path.write_text("", encoding="utf-8")
    items = parse_feedback_file(path)
    assert len(items) == 1
    assert items[0].kind is FeedbackKind.PARSE_ERROR
    assert "invalid JSON" in items[0].suggestion_text


def test_parse_error_body_truncated_when_raw_content_huge(tmp_path: Path) -> None:
    """Synthetic PARSE_ERROR body (err_msg + raw content) is also subject to truncation."""
    huge = "z" * (SUGGESTION_TEXT_MAX + 500)
    path = tmp_path / "a.feedback.json"
    path.write_text(huge, encoding="utf-8")
    items = parse_feedback_file(path)
    assert len(items) == 1
    assert items[0].kind is FeedbackKind.PARSE_ERROR
    assert len(items[0].suggestion_text) == SUGGESTION_TEXT_MAX
    assert items[0].suggestion_text.endswith(TRUNCATION_SUFFIX)


# ---------------------------------------------------------------------------
# scan_transcript_dir_for_feedback
# ---------------------------------------------------------------------------


def test_scan_empty_dir_returns_none(tmp_path: Path) -> None:
    result = scan_transcript_dir_for_feedback(str(tmp_path), snapshot_before=set())
    assert result is None


def test_scan_dir_with_only_transcript_files_returns_none(tmp_path: Path) -> None:
    (tmp_path / "run-1.transcript").write_text("hello", encoding="utf-8")
    result = scan_transcript_dir_for_feedback(str(tmp_path), snapshot_before=set())
    assert result is None


def test_scan_dir_with_one_new_feedback_file_returns_it(tmp_path: Path) -> None:
    (tmp_path / "run-1.feedback.json").write_text("[]", encoding="utf-8")
    result = scan_transcript_dir_for_feedback(str(tmp_path), snapshot_before=set())
    assert result == tmp_path / "run-1.feedback.json"


def test_scan_dir_with_two_new_files_returns_newest_by_mtime(tmp_path: Path) -> None:
    older = tmp_path / "older.feedback.json"
    newer = tmp_path / "newer.feedback.json"
    older.write_text("[]", encoding="utf-8")
    # Ensure a distinct mtime ordering regardless of filesystem timestamp resolution.
    time.sleep(0.01)
    newer.write_text("[]", encoding="utf-8")
    now = time.time()
    os.utime(older, (now - 100, now - 100))
    os.utime(newer, (now, now))

    result = scan_transcript_dir_for_feedback(str(tmp_path), snapshot_before=set())
    assert result == newer


def test_scan_file_in_snapshot_before_not_returned(tmp_path: Path) -> None:
    (tmp_path / "run-1.feedback.json").write_text("[]", encoding="utf-8")
    result = scan_transcript_dir_for_feedback(
        str(tmp_path), snapshot_before={"run-1.feedback.json"}
    )
    assert result is None


def test_scan_dir_path_does_not_exist_returns_none(tmp_path: Path) -> None:
    missing_dir = tmp_path / "does-not-exist"
    result = scan_transcript_dir_for_feedback(str(missing_dir), snapshot_before=set())
    assert result is None
