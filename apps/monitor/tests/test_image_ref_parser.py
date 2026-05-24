"""Tests for image_ref_parser module."""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.docker.image_ref_parser import (
    ImageRefParseError,
    ParsedImageRef,
    parse_image_ref,
)


def test_bare_image_with_tag() -> None:
    """Test bare image name with tag expands to docker.io/library."""
    result = parse_image_ref("postgres:16")
    assert result.registry == "docker.io"
    assert result.repo == "library/postgres"
    assert result.tag == "16"
    assert result.digest is None


def test_bare_image_without_tag() -> None:
    """Test bare image name without tag defaults to latest."""
    result = parse_image_ref("postgres")
    assert result.registry == "docker.io"
    assert result.repo == "library/postgres"
    assert result.tag == "latest"
    assert result.digest is None


def test_namespaced_image() -> None:
    """Test namespaced image on default registry."""
    result = parse_image_ref("pihole/pihole:2025.05")
    assert result.registry == "docker.io"
    assert result.repo == "pihole/pihole"
    assert result.tag == "2025.05"
    assert result.digest is None


def test_full_registry_ghcr() -> None:
    """Test image with full registry and namespace."""
    result = parse_image_ref("ghcr.io/foo/bar:v1.0")
    assert result.registry == "ghcr.io"
    assert result.repo == "foo/bar"
    assert result.tag == "v1.0"
    assert result.digest is None


def test_full_registry_with_port() -> None:
    """Test image with registry including port."""
    result = parse_image_ref("localhost:5000/repo:tag")
    assert result.registry == "localhost:5000"
    assert result.repo == "repo"
    assert result.tag == "tag"
    assert result.digest is None


def test_quay_io() -> None:
    """Test quay.io registry."""
    result = parse_image_ref("quay.io/coreos/etcd:v3")
    assert result.registry == "quay.io"
    assert result.repo == "coreos/etcd"
    assert result.tag == "v3"
    assert result.digest is None


def test_registry_k8s_io() -> None:
    """Test registry.k8s.io."""
    result = parse_image_ref("registry.k8s.io/pause:3.9")
    assert result.registry == "registry.k8s.io"
    assert result.repo == "pause"
    assert result.tag == "3.9"
    assert result.digest is None


def test_digest_only_ref() -> None:
    """Test image with digest but no explicit tag."""
    result = parse_image_ref("alpine@sha256:abc123")
    assert result.registry == "docker.io"
    assert result.repo == "library/alpine"
    assert result.tag == "latest"
    assert result.digest == "sha256:abc123"


def test_tag_plus_digest() -> None:
    """Test image with both tag and digest."""
    result = parse_image_ref("postgres:16@sha256:abc123")
    assert result.registry == "docker.io"
    assert result.repo == "library/postgres"
    assert result.tag == "16"
    assert result.digest == "sha256:abc123"


def test_latest_tag_explicit() -> None:
    """Test explicit latest tag."""
    result = parse_image_ref("nginx:latest")
    assert result.registry == "docker.io"
    assert result.repo == "library/nginx"
    assert result.tag == "latest"
    assert result.digest is None


def test_none_literal_raises() -> None:
    """Test that '<none>' raises ImageRefParseError."""
    with pytest.raises(ImageRefParseError, match="unparseable"):
        parse_image_ref("<none>")


def test_empty_string_raises() -> None:
    """Test that empty string raises ImageRefParseError."""
    with pytest.raises(ImageRefParseError, match="unparseable"):
        parse_image_ref("")


def test_bare_digest_raises() -> None:
    """Test that bare digest with no name raises."""
    with pytest.raises(ImageRefParseError, match="bare digest"):
        parse_image_ref("sha256:abc123")


def test_non_sha256_digest_raises() -> None:
    """Test that non-sha256 digest raises."""
    with pytest.raises(ImageRefParseError, match="non-sha256"):
        parse_image_ref("foo@md5:xyz")


def test_localhost_no_port() -> None:
    """Test localhost without port is treated as registry."""
    result = parse_image_ref("localhost/repo:tag")
    assert result.registry == "localhost"
    assert result.repo == "repo"
    assert result.tag == "tag"
    assert result.digest is None


def test_whitespace_stripped() -> None:
    """Test that leading/trailing whitespace is stripped."""
    result = parse_image_ref("  postgres:16  ")
    assert result.registry == "docker.io"
    assert result.repo == "library/postgres"
    assert result.tag == "16"


def test_custom_registry_no_port() -> None:
    """Test custom registry with dot but no port."""
    result = parse_image_ref("myregistry.example.com/myrepo:1.0")
    assert result.registry == "myregistry.example.com"
    assert result.repo == "myrepo"
    assert result.tag == "1.0"


def test_ghcr_with_full_namespace() -> None:
    """Test ghcr with multiple path segments."""
    result = parse_image_ref("ghcr.io/org/team/image:v1.2.3")
    assert result.registry == "ghcr.io"
    assert result.repo == "org/team/image"
    assert result.tag == "v1.2.3"


def test_parsed_image_ref_is_frozen() -> None:
    """Verify ParsedImageRef is a frozen dataclass."""
    ref = parse_image_ref("postgres:16")
    with pytest.raises(AttributeError):
        ref.tag = "latest"  # type: ignore


def test_result_is_parsed_image_ref_instance() -> None:
    """Verify parse_image_ref returns a ParsedImageRef instance."""
    result = parse_image_ref("postgres:16")
    assert isinstance(result, ParsedImageRef)


def test_parse_image_ref_raises_on_empty_repo_with_only_tag() -> None:
    """An input like ':tag' has empty repo after split - raises ImageRefParseError."""
    with pytest.raises(ImageRefParseError, match="empty repo"):
        parse_image_ref(":justatag")


def test_parse_rejects_tag_with_slash() -> None:
    """Tags cannot contain slashes per OCI distribution spec."""
    with pytest.raises(ImageRefParseError, match=r"tag.*slash"):
        parse_image_ref("nginx:foo/bar")


def test_parse_rejects_registry_only() -> None:
    """A bare registry hostname with no repo is unparseable."""
    with pytest.raises(ImageRefParseError):
        parse_image_ref("gcr.io")


def test_parse_rejects_registry_with_trailing_slash() -> None:
    """A registry followed by empty path is unparseable."""
    with pytest.raises(ImageRefParseError):
        parse_image_ref("ghcr.io/")
