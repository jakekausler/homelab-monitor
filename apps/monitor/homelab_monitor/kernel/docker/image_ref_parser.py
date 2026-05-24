"""In-house image-reference parser (D-IMAGE-REF-PARSER-INHOUSE).

Parses container image references into (registry, repo, tag, digest).
No external dependency.

Examples:
  - 'postgres:16' -> docker.io/library/postgres:16
  - 'pihole/pihole:2025.05' -> docker.io/pihole/pihole:2025.05
  - 'ghcr.io/foo/bar:v1.0' -> ghcr.io/foo/bar:v1.0
  - 'ghcr.io/foo/bar@sha256:abc' -> ghcr.io/foo/bar (digest-only)
  - 'postgres:16@sha256:abc' -> docker.io/library/postgres:16 (digest pinned)

Returns ImageRefParseError-compatible None for:
  - '<none>'
  - ''
  - bare 'sha256:abc' with no name
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

_DEFAULT_REGISTRY: Final[str] = "docker.io"
_DEFAULT_TAG: Final[str] = "latest"
_LIBRARY_NAMESPACE: Final[str] = "library"
_UNPARSEABLE_LITERALS: Final[frozenset[str]] = frozenset({"<none>", ""})


class ImageRefParseError(ValueError):
    """Raised when an image reference cannot be parsed."""


@dataclass(frozen=True, slots=True)
class ParsedImageRef:
    registry: str
    repo: str
    tag: str
    digest: str | None  # sha256:... if present in the ref, else None


def parse_image_ref(ref: str | None) -> ParsedImageRef:  # noqa: PLR0912
    """Parse a container image reference into its components.

    Raises:
        ImageRefParseError: when the ref is empty, '<none>', or a bare digest.
    """
    if ref is None or ref.strip() in _UNPARSEABLE_LITERALS:
        raise ImageRefParseError(f"unparseable image ref: {ref!r}")
    ref = ref.strip()
    # Bare digest with no name (e.g. "sha256:abc..."): unparseable.
    if ref.startswith("sha256:"):
        raise ImageRefParseError(f"bare digest with no name: {ref!r}")

    # Split off digest (if any). Tag+digest is legal: 'postgres:16@sha256:abc'.
    digest: str | None = None
    if "@" in ref:
        ref, _, digest_part = ref.rpartition("@")
        if not digest_part.startswith("sha256:"):
            raise ImageRefParseError(f"non-sha256 digest unsupported: {digest_part!r}")
        digest = digest_part

    # Detect registry. A registry is the first '/' segment IFF it contains
    # '.' or ':' (port) or equals 'localhost'. Otherwise it's part of the repo.
    first_slash = ref.find("/")
    if first_slash == -1:
        # No slash: bare image like 'postgres' or 'postgres:16'.
        # But a bare registry hostname like 'gcr.io' (name contains '.', no tag)
        # is a registry-only ref and unparseable (no repo). Check the name part
        # only — tags like ':1.0' are fine.
        name_part = ref.partition(":")[0]
        if "." in name_part:
            raise ImageRefParseError(f"registry-only ref with no repo: {ref!r}")
        registry = _DEFAULT_REGISTRY
        rest = ref
    else:
        host_candidate = ref[:first_slash]
        # Registry detection: requires DNS-like host (contains '.' or 'localhost')
        # OR host:port form (':' followed by all-digit port). A bare 'name:tag/path'
        # like 'nginx:foo/bar' is NOT a registry.
        has_dot = "." in host_candidate
        is_localhost = host_candidate == "localhost"
        has_port = False
        if ":" in host_candidate:
            _, _, after_colon = host_candidate.rpartition(":")
            has_port = after_colon.isdigit() and len(after_colon) > 0
        if has_dot or is_localhost or has_port:
            registry = host_candidate
            rest = ref[first_slash + 1 :]
        else:
            registry = _DEFAULT_REGISTRY
            rest = ref

    # C3: Validate — reject empty rest (registry-only ref like "gcr.io/")
    if not rest:
        raise ImageRefParseError(f"registry-only ref with no repo: {ref!r}")

    # Split off tag.
    if ":" in rest:
        repo, _, tag = rest.rpartition(":")
        # C3: Validate — tag must not contain '/' (would indicate bad parse)
        if "/" in tag:
            raise ImageRefParseError(f"tag contains slash: {ref!r}")
    else:
        repo = rest
        tag = _DEFAULT_TAG

    if not repo:
        raise ImageRefParseError(f"empty repo after parsing: {ref!r}")

    # Library-namespace expansion for default registry.
    if registry == _DEFAULT_REGISTRY and "/" not in repo:
        repo = f"{_LIBRARY_NAMESPACE}/{repo}"

    return ParsedImageRef(registry=registry, repo=repo, tag=tag, digest=digest)


__all__ = ["ImageRefParseError", "ParsedImageRef", "parse_image_ref"]
