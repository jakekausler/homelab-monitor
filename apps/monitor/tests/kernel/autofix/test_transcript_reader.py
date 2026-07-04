"""Tests for transcript_reader.py (STAGE-009-011)."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from homelab_monitor.kernel.autofix.transcript_reader import (
    MAX_TRANSCRIPT_BYTES,
    read_transcript,
)

# Margin for UTF-8 encoding variance
UTF8_MARGIN = 10


def test_happy_path_returns_content_not_truncated() -> None:
    with TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        transcript_file = base_dir / "test.txt"
        content = "Hello, World!"
        transcript_file.write_text(content)

        result = read_transcript(str(transcript_file), base_dir=base_dir)

        assert result is not None
        assert result.text == content
        assert result.truncated is False
        assert result.size_bytes == len(content.encode("utf-8"))


def test_empty_path_returns_none() -> None:
    with TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        result = read_transcript("", base_dir=base_dir)
        assert result is None


def test_missing_file_returns_none() -> None:
    with TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        result = read_transcript(str(base_dir / "missing.txt"), base_dir=base_dir)
        assert result is None


def test_path_outside_base_dir_returns_none() -> None:
    with TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        sibling_dir = Path(tmpdir).parent / "sibling"
        sibling_dir.mkdir(exist_ok=True)
        evil_file = sibling_dir / "evil.txt"
        evil_file.write_text("secret")

        # Try to escape base_dir using ../
        result = read_transcript(str(evil_file), base_dir=base_dir)
        assert result is None


def test_path_via_symlink_that_escapes_base_returns_none() -> None:
    with TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        sibling_dir = Path(tmpdir).parent / "sibling"
        sibling_dir.mkdir(exist_ok=True)
        evil_file = sibling_dir / "evil.txt"
        evil_file.write_text("secret")

        # Create a symlink inside base_dir pointing outside
        symlink = base_dir / "link.txt"
        symlink.symlink_to(evil_file)

        result = read_transcript(str(symlink), base_dir=base_dir)
        # Path.resolve() follows symlinks, so it points outside base_dir
        assert result is None


def test_oversized_file_truncates_returns_tail_and_flags_truncated() -> None:
    with TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        transcript_file = base_dir / "large.txt"
        # Create a file larger than MAX_TRANSCRIPT_BYTES
        prefix = "A" * 10000
        suffix = "B" * 10000
        content = prefix + ("X" * (MAX_TRANSCRIPT_BYTES + 1000)) + suffix
        transcript_file.write_text(content)

        result = read_transcript(str(transcript_file), base_dir=base_dir)

        assert result is not None
        assert result.truncated is True
        assert result.size_bytes == len(content.encode("utf-8"))
        # The returned text should be the tail, ending with suffix
        assert result.text.endswith(suffix)
        assert len(result.text.encode("utf-8")) <= MAX_TRANSCRIPT_BYTES + UTF8_MARGIN


def test_boundary_exact_512kib_not_truncated() -> None:
    with TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        transcript_file = base_dir / "exact.txt"
        content = "X" * MAX_TRANSCRIPT_BYTES
        transcript_file.write_text(content)

        result = read_transcript(str(transcript_file), base_dir=base_dir)

        assert result is not None
        assert result.truncated is False
        assert result.size_bytes == MAX_TRANSCRIPT_BYTES


def test_boundary_512kib_plus_one_truncated() -> None:
    with TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        transcript_file = base_dir / "plus_one.txt"
        content = "X" * (MAX_TRANSCRIPT_BYTES + 1)
        transcript_file.write_text(content)

        result = read_transcript(str(transcript_file), base_dir=base_dir)

        assert result is not None
        assert result.truncated is True
        assert result.size_bytes == MAX_TRANSCRIPT_BYTES + 1


def test_unicode_replacement_on_invalid_bytes() -> None:
    with TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        transcript_file = base_dir / "invalid.txt"
        # Write invalid UTF-8 bytes
        content = b"hello\xff\xfex world"
        transcript_file.write_bytes(content)

        result = read_transcript(str(transcript_file), base_dir=base_dir)

        assert result is not None
        assert "�" in result.text  # U+FFFD replacement character
        assert "hello" in result.text
        assert "world" in result.text


def test_size_bytes_reflects_actual_file_size_not_returned_bytes() -> None:
    with TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        transcript_file = base_dir / "large.txt"
        original_size = MAX_TRANSCRIPT_BYTES + 100000
        content = "X" * original_size
        transcript_file.write_text(content)

        result = read_transcript(str(transcript_file), base_dir=base_dir)

        assert result is not None
        assert result.size_bytes == original_size
        assert result.truncated is True
        # returned text should be smaller than size_bytes
        assert len(result.text.encode("utf-8")) < result.size_bytes
