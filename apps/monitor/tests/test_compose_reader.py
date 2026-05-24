"""Tests for compose_reader.py (STAGE-003-009)."""

from __future__ import annotations

import stat
import sys
import textwrap
from pathlib import Path

import pytest
import structlog

from homelab_monitor.kernel.docker.build_sources_schema import BuildContextRemap
from homelab_monitor.kernel.docker.compose_reader import (
    ComposeReadError,
    ComposeService,
    read_compose,
    read_compose_set,
)
from homelab_monitor.kernel.docker.path_resolver import PathResolver


def _write_compose(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "docker-compose.yml"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    """read_compose raises ComposeReadError(reason='file_not_found') for missing file."""
    p = tmp_path / "nonexistent.yml"
    with pytest.raises(ComposeReadError) as exc_info:
        read_compose(p)
    assert exc_info.value.reason == "file_not_found"
    assert "not found" in str(exc_info.value).lower()


def test_malformed_yaml_raises_malformed(tmp_path: Path) -> None:
    """read_compose raises ComposeReadError(reason='malformed_yaml') for bad YAML."""
    p = _write_compose(tmp_path, "services: {\nbroken yaml: [unclosed\n")
    with pytest.raises(ComposeReadError) as exc_info:
        read_compose(p)
    assert exc_info.value.reason == "malformed_yaml"


def test_non_dict_root_raises_non_dict_root(tmp_path: Path) -> None:
    """read_compose raises ComposeReadError(reason='non_dict_root') for list root."""
    p = _write_compose(tmp_path, "- item1\n- item2\n")
    with pytest.raises(ComposeReadError) as exc_info:
        read_compose(p)
    assert exc_info.value.reason == "non_dict_root"


def test_non_dict_root_string_raises_non_dict_root(tmp_path: Path) -> None:
    """read_compose raises ComposeReadError(reason='non_dict_root') for scalar root."""
    p = _write_compose(tmp_path, "just a string\n")
    with pytest.raises(ComposeReadError) as exc_info:
        read_compose(p)
    assert exc_info.value.reason == "non_dict_root"


@pytest.mark.skipif(sys.platform == "win32", reason="chmod 000 unreliable on Windows")
def test_permission_denied_raises_permission_denied(tmp_path: Path) -> None:
    """read_compose raises ComposeReadError(reason='permission_denied') on unreadable file."""
    p = _write_compose(tmp_path, "services: {}\n")
    p.chmod(0o000)
    try:
        with pytest.raises(ComposeReadError) as exc_info:
            read_compose(p)
        assert exc_info.value.reason == "permission_denied"
    finally:
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)


# ---------------------------------------------------------------------------
# Empty / minimal compose files
# ---------------------------------------------------------------------------


def test_no_services_key_returns_empty_services(tmp_path: Path) -> None:
    """read_compose returns ComposeFile with empty services dict when 'services' key absent."""
    p = _write_compose(tmp_path, "version: '3.8'\n")
    result = read_compose(p)
    assert result.services == {}


def test_services_null_returns_empty_services(tmp_path: Path) -> None:
    """read_compose returns empty services when services: null."""
    p = _write_compose(tmp_path, "services:\n")
    result = read_compose(p)
    assert result.services == {}


def test_services_empty_dict_returns_empty_services(tmp_path: Path) -> None:
    """read_compose returns empty services for 'services: {}'."""
    p = _write_compose(tmp_path, "services: {}\n")
    result = read_compose(p)
    assert result.services == {}


# ---------------------------------------------------------------------------
# Service parsing — build forms
# ---------------------------------------------------------------------------


def test_build_shorthand_string(tmp_path: Path) -> None:
    """build: ./path (shorthand) sets build_context and default Dockerfile."""
    p = _write_compose(
        tmp_path,
        "services:\n  myapp:\n    build: ./myapp\n",
    )
    result = read_compose(p)
    svc = result.services["myapp"]
    assert svc.build_context == (tmp_path / "myapp").resolve()
    assert svc.build_dockerfile == "Dockerfile"
    assert svc.image is None


def test_build_dict_full_form(tmp_path: Path) -> None:
    """build: {context: ..., dockerfile: ...} sets both fields."""
    p = _write_compose(
        tmp_path,
        (
            "services:\n  myapp:\n    build:\n      context: ./src\n"
            "      dockerfile: custom.Dockerfile\n"
        ),
    )
    result = read_compose(p)
    svc = result.services["myapp"]
    assert svc.build_context == (tmp_path / "src").resolve()
    assert svc.build_dockerfile == "custom.Dockerfile"


def test_build_dict_no_dockerfile_defaults_to_dockerfile(tmp_path: Path) -> None:
    """build: {context: ...} with no dockerfile defaults to 'Dockerfile'."""
    p = _write_compose(
        tmp_path,
        "services:\n  myapp:\n    build:\n      context: ./app\n",
    )
    result = read_compose(p)
    svc = result.services["myapp"]
    assert svc.build_context == (tmp_path / "app").resolve()
    assert svc.build_dockerfile == "Dockerfile"


def test_image_only_no_build_context(tmp_path: Path) -> None:
    """Service with image: only has build_context=None and build_dockerfile=None."""
    p = _write_compose(
        tmp_path,
        "services:\n  db:\n    image: postgres:16\n",
    )
    result = read_compose(p)
    svc = result.services["db"]
    assert svc.build_context is None
    assert svc.build_dockerfile is None
    assert svc.image == "postgres:16"


def test_both_image_and_build_sets_both(tmp_path: Path) -> None:
    """Service with both image: and build: preserves both fields."""
    p = _write_compose(
        tmp_path,
        "services:\n  app:\n    image: myimage:latest\n    build: ./app\n",
    )
    result = read_compose(p)
    svc = result.services["app"]
    assert svc.image == "myimage:latest"
    assert svc.build_context == (tmp_path / "app").resolve()


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def test_labels_dict_form(tmp_path: Path) -> None:
    """labels: {key: value} dict form is parsed correctly."""
    p = _write_compose(
        tmp_path,
        (
            "services:\n  app:\n    image: nginx\n    labels:\n"
            "      com.example.foo: bar\n      version: '1.0'\n"
        ),
    )
    result = read_compose(p)
    svc = result.services["app"]
    assert svc.labels == {"com.example.foo": "bar", "version": "1.0"}


def test_labels_list_form(tmp_path: Path) -> None:
    """labels: ['k=v'] list form is parsed correctly."""
    p = _write_compose(
        tmp_path,
        (
            "services:\n  app:\n    image: nginx\n    labels:\n"
            "      - com.docker.compose.service=app\n      - version=2.0\n"
        ),
    )
    result = read_compose(p)
    svc = result.services["app"]
    assert svc.labels["com.docker.compose.service"] == "app"
    assert svc.labels["version"] == "2.0"


def test_labels_list_item_without_equals_skipped(tmp_path: Path) -> None:
    """labels: list items without '=' are skipped silently."""
    p = _write_compose(
        tmp_path,
        "services:\n  app:\n    image: nginx\n    labels:\n      - noequals\n      - key=value\n",
    )
    result = read_compose(p)
    svc = result.services["app"]
    assert "noequals" not in svc.labels
    assert svc.labels["key"] == "value"


# ---------------------------------------------------------------------------
# Service entry not a dict — skipped silently
# ---------------------------------------------------------------------------


def test_non_dict_service_entry_skipped(tmp_path: Path) -> None:
    """Service entries that are not dicts (e.g. YAML anchors/null) are skipped."""
    p = _write_compose(
        tmp_path,
        "services:\n  placeholder: null\n  real:\n    image: nginx\n",
    )
    result = read_compose(p)
    assert "placeholder" not in result.services
    assert "real" in result.services


def test_string_service_entry_skipped(tmp_path: Path) -> None:
    """String-valued service entries are skipped."""
    p = _write_compose(
        tmp_path,
        "services:\n  bad: just-a-string\n  good:\n    image: nginx\n",
    )
    result = read_compose(p)
    assert "bad" not in result.services
    assert "good" in result.services


# ---------------------------------------------------------------------------
# Unicode + profiles
# ---------------------------------------------------------------------------


def test_unicode_service_name_preserved(tmp_path: Path) -> None:
    """Unicode characters in service names are preserved."""
    p = _write_compose(
        tmp_path,
        "services:\n  mon-réseau:\n    image: nginx\n",
    )
    result = read_compose(p)
    assert "mon-réseau" in result.services


def test_profiles_parsed_into_tuple(tmp_path: Path) -> None:
    """profiles: ['disabled'] is parsed into a tuple."""
    p = _write_compose(
        tmp_path,
        (
            "services:\n  optional:\n    image: nginx\n    profiles:\n"
            "      - disabled\n      - testing\n"
        ),
    )
    result = read_compose(p)
    svc = result.services["optional"]
    assert svc.profiles == ("disabled", "testing")


# ---------------------------------------------------------------------------
# compose_path is absolute
# ---------------------------------------------------------------------------


def test_compose_path_is_absolute(tmp_path: Path) -> None:
    """ComposeFile.compose_path is an absolute path."""
    p = _write_compose(tmp_path, "services: {}\n")
    result = read_compose(p)
    assert result.compose_path.is_absolute()


# ---------------------------------------------------------------------------
# Known-good mixed compose file
# ---------------------------------------------------------------------------


def test_mixed_compose_file(tmp_path: Path) -> None:
    """Parse a realistic compose file with multiple service types."""
    content = """\
services:
  db:
    image: postgres:16
  udo-viewer:
    build: ./udo-viewer
    labels:
      com.docker.compose.service: udo-viewer
  web:
    image: nginx:latest
    build:
      context: ./web
      dockerfile: web.Dockerfile
    labels:
      - env=production
  placeholder: null
"""
    p = _write_compose(tmp_path, content)
    result = read_compose(p)

    # db — image only
    db: ComposeService = result.services["db"]
    assert db.image == "postgres:16"
    assert db.build_context is None

    # udo-viewer — build shorthand
    uv: ComposeService = result.services["udo-viewer"]
    assert uv.build_context == (tmp_path / "udo-viewer").resolve()
    assert uv.build_dockerfile == "Dockerfile"
    assert uv.labels["com.docker.compose.service"] == "udo-viewer"

    # web — both image + build dict
    web: ComposeService = result.services["web"]
    assert web.image == "nginx:latest"
    assert web.build_context == (tmp_path / "web").resolve()
    assert web.build_dockerfile == "web.Dockerfile"
    assert web.labels["env"] == "production"

    # placeholder — null entry, skipped
    assert "placeholder" not in result.services


def test_non_dict_services_value_returns_empty_services(tmp_path: Path) -> None:
    """Non-dict services value (e.g. list) returns empty services dict."""
    p = _write_compose(tmp_path, "services:\n  - not_a_dict\n  - also_not_a_dict\n")
    result = read_compose(p)
    assert result.services == {}


def test_build_dict_without_context_leaves_build_context_none(tmp_path: Path) -> None:
    """build: {dockerfile: Dockerfile} with no context key leaves build_context None."""
    p = _write_compose(
        tmp_path,
        textwrap.dedent("""\
        services:
          app:
            build:
              dockerfile: Dockerfile
    """),
    )
    result = read_compose(p)
    svc = result.services["app"]
    assert svc.build_context is None
    # build_dockerfile is preserved when set explicitly, even without context
    assert svc.build_dockerfile == "Dockerfile"


def test_labels_list_form_parsed_into_dict(tmp_path: Path) -> None:
    """labels: ['k=v'] list form is parsed into a str→str dict."""
    p = _write_compose(
        tmp_path,
        textwrap.dedent("""\
        services:
          app:
            image: myimage
            labels:
              - com.example.foo=bar
              - com.example.baz=qux
    """),
    )
    result = read_compose(p)
    assert result.services["app"].labels == {
        "com.example.foo": "bar",
        "com.example.baz": "qux",
    }


# ---------------------------------------------------------------------------
# Multi-file aggregation (read_compose_set) — STAGE-003-009 Wave G
# ---------------------------------------------------------------------------


def _write_compose_named(directory: Path, name: str, content: str) -> Path:
    """Write a named compose file inside *directory* (directory created if needed)."""
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / name
    p.write_text(content, encoding="utf-8")
    return p


def test_read_compose_set_single_file_equivalent(tmp_path: Path) -> None:
    """read_compose_set with one path returns same services as read_compose."""
    p = _write_compose(tmp_path, "services:\n  app:\n    image: nginx\n")
    single = read_compose(p)
    merged = read_compose_set([p])
    assert merged.services.keys() == single.services.keys()
    assert merged.services["app"].image == "nginx"


def test_read_compose_set_two_files_distinct_services_both_present(tmp_path: Path) -> None:
    """Two files with distinct service names: all services appear in merged result."""
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    pa = _write_compose_named(dir_a, "docker-compose.yml", "services:\n  svc-a:\n    image: a\n")
    pb = _write_compose_named(dir_b, "docker-compose.yml", "services:\n  svc-b:\n    image: b\n")
    result = read_compose_set([pa, pb])
    assert "svc-a" in result.services
    assert "svc-b" in result.services
    assert result.services["svc-a"].source_compose_path == pa.resolve()
    assert result.services["svc-b"].source_compose_path == pb.resolve()


def test_read_compose_set_overlap_later_wins(tmp_path: Path) -> None:
    """Two files share a service name — later file's definition wins."""
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    pa = _write_compose_named(
        dir_a, "docker-compose.yml", "services:\n  app:\n    image: nginx:1.24\n"
    )
    pb = _write_compose_named(
        dir_b, "docker-compose.yml", "services:\n  app:\n    image: nginx:latest\n"
    )
    result = read_compose_set([pa, pb])
    assert result.services["app"].image == "nginx:latest"
    assert result.services["app"].source_compose_path == pb.resolve()


def test_read_compose_set_missing_file_skipped(tmp_path: Path) -> None:
    """One good + one missing file: merge proceeds with the good file; no raise."""
    good = _write_compose(tmp_path, "services:\n  good:\n    image: nginx\n")
    missing = tmp_path / "nonexistent.yml"
    log = structlog.get_logger().bind(test="skip_file")
    result = read_compose_set([good, missing], log=log)  # type: ignore[arg-type]
    assert "good" in result.services
    assert "nonexistent" not in result.services


def test_read_compose_set_all_missing_raises(tmp_path: Path) -> None:
    """When all paths fail, raises the last ComposeReadError."""
    pa = tmp_path / "missing-a.yml"
    pb = tmp_path / "missing-b.yml"
    with pytest.raises(ComposeReadError) as exc_info:
        read_compose_set([pa, pb])
    assert exc_info.value.reason == "file_not_found"


def test_read_compose_set_empty_paths_raises(tmp_path: Path) -> None:
    """Empty paths list raises ComposeReadError(reason='file_not_found')."""
    with pytest.raises(ComposeReadError) as exc_info:
        read_compose_set([])
    assert exc_info.value.reason == "file_not_found"


def test_read_compose_set_resolver_remaps_build_context(tmp_path: Path) -> None:
    """path_resolver rewrites build_context absolute paths after parsing."""
    p = _write_compose(tmp_path, "services:\n  app:\n    build: /storage/programs/app\n")
    resolver = PathResolver(
        [
            BuildContextRemap(host_prefix="/storage/programs", container_prefix="/host/programs"),
        ]
    )
    result = read_compose_set([p], path_resolver=resolver)
    assert result.services["app"].build_context == Path("/host/programs/app")


def test_read_compose_set_no_resolver_leaves_context_untouched(tmp_path: Path) -> None:
    """Without path_resolver, build_context paths are not remapped."""
    p = _write_compose(tmp_path, "services:\n  app:\n    build: /storage/programs/app\n")
    result = read_compose_set([p])
    # build_context resolved relative to compose dir (absolute path used directly)
    assert result.services["app"].build_context == Path("/storage/programs/app")


def test_read_compose_set_labels_deep_copied(tmp_path: Path) -> None:
    """Mutating merged service labels does not affect subsequent reads."""
    p = _write_compose(
        tmp_path,
        "services:\n  app:\n    image: nginx\n    labels:\n      key: original\n",
    )
    result = read_compose_set([p])
    result.services["app"].labels["key"] = "mutated"
    # Re-read; the YAML on disk is unaffected (mutation was in-memory copy)
    result2 = read_compose_set([p])
    assert result2.services["app"].labels["key"] == "original"


def test_read_compose_set_compose_path_is_last_successful(tmp_path: Path) -> None:
    """compose_path in result is the last successfully-loaded file, not the last in input list."""
    good_dir = tmp_path / "good"
    good_dir.mkdir()
    good = _write_compose(good_dir, "services:\n  app:\n    image: nginx\n")
    bad_path = tmp_path / "bad.yml"  # does not exist
    result = read_compose_set([good, bad_path])
    assert result.compose_path == good.resolve()
