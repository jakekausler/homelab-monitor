"""Tests for path_resolver.py (STAGE-003-009 Wave G)."""

from __future__ import annotations

from pathlib import Path

from homelab_monitor.kernel.docker.build_sources_schema import BuildContextRemap
from homelab_monitor.kernel.docker.path_resolver import PathResolver


def _remap(host_prefix: str, container_prefix: str) -> BuildContextRemap:
    return BuildContextRemap(host_prefix=host_prefix, container_prefix=container_prefix)


# ---------------------------------------------------------------------------
# Empty remap list
# ---------------------------------------------------------------------------


def test_empty_remaps_returns_input_as_path_str() -> None:
    """Empty remap list: resolve(str) returns Path(input) identity."""
    resolver = PathResolver([])
    assert resolver.resolve("/some/path/to/app") == Path("/some/path/to/app")


def test_empty_remaps_returns_input_as_path_obj() -> None:
    """Empty remap list: resolve(Path) returns same Path identity."""
    resolver = PathResolver([])
    assert resolver.resolve(Path("/some/path/to/app")) == Path("/some/path/to/app")


# ---------------------------------------------------------------------------
# Single remap — exact and subdir matches
# ---------------------------------------------------------------------------


def test_single_exact_prefix_match() -> None:
    """Exact prefix match: input == host_prefix → returns container_prefix."""
    resolver = PathResolver([_remap("/storage/programs", "/host-build-contexts/programs")])
    result = resolver.resolve("/storage/programs")
    assert result == Path("/host-build-contexts/programs")


def test_single_prefix_match_with_subdir() -> None:
    """Subdir match: host_prefix + '/' + suffix → container_prefix + suffix."""
    resolver = PathResolver([_remap("/storage/programs", "/host-build-contexts/programs")])
    result = resolver.resolve("/storage/programs/bills/web")
    assert result == Path("/host-build-contexts/programs/bills/web")


def test_prefix_boundary_no_substring_match() -> None:
    """'/storage/programs2/x' does NOT match prefix '/storage/programs' (boundary rule)."""
    resolver = PathResolver([_remap("/storage/programs", "/host-build-contexts/programs")])
    result = resolver.resolve("/storage/programs2/foo")
    # No match → identity
    assert result == Path("/storage/programs2/foo")


def test_no_match_returns_identity() -> None:
    """Path that doesn't match any remap is returned unchanged."""
    resolver = PathResolver([_remap("/storage/programs", "/host-build-contexts/programs")])
    result = resolver.resolve("/completely/different/path")
    assert result == Path("/completely/different/path")


# ---------------------------------------------------------------------------
# First-match-wins
# ---------------------------------------------------------------------------


def test_first_match_wins_with_overlapping_prefixes() -> None:
    """When two remaps could match, the first one in the list wins."""
    resolver = PathResolver(
        [
            _remap("/storage/programs/bills", "/specific/bills"),
            _remap("/storage/programs", "/host-build-contexts/programs"),
        ]
    )
    result = resolver.resolve("/storage/programs/bills/web")
    assert result == Path("/specific/bills/web")


def test_second_remap_used_when_first_does_not_match() -> None:
    """When first remap doesn't match, second remap is tried."""
    resolver = PathResolver(
        [
            _remap("/storage/other", "/host/other"),
            _remap("/storage/programs", "/host-build-contexts/programs"),
        ]
    )
    result = resolver.resolve("/storage/programs/myapp")
    assert result == Path("/host-build-contexts/programs/myapp")


# ---------------------------------------------------------------------------
# str vs Path input symmetry
# ---------------------------------------------------------------------------


def test_accepts_str_input() -> None:
    """resolve() works with str input."""
    resolver = PathResolver([_remap("/storage/programs", "/host/programs")])
    assert resolver.resolve("/storage/programs/app") == Path("/host/programs/app")


def test_accepts_path_input() -> None:
    """resolve() works with Path input and returns same result as str."""
    resolver = PathResolver([_remap("/storage/programs", "/host/programs")])
    assert resolver.resolve(Path("/storage/programs/app")) == Path("/host/programs/app")


def test_str_and_path_inputs_are_equivalent() -> None:
    """resolve(str) and resolve(Path) produce identical results."""
    resolver = PathResolver([_remap("/storage/programs", "/host/programs")])
    path_str = "/storage/programs/subdir"
    assert resolver.resolve(path_str) == resolver.resolve(Path(path_str))


# ---------------------------------------------------------------------------
# Trailing-slash behavior (documented as implementation-defined)
# ---------------------------------------------------------------------------


def test_resolve_documents_trailing_slash_behavior() -> None:
    """Trailing-slash input is not normalized; behavior is implementation-defined."""
    resolver = PathResolver(
        [
            BuildContextRemap(
                host_prefix="/storage/programs",
                container_prefix="/host-build-contexts/programs",
            )
        ]
    )
    # No assertion on specific output — just verify it doesn't crash and produces SOME path
    result = resolver.resolve("/storage/programs/")
    assert isinstance(result, Path)
