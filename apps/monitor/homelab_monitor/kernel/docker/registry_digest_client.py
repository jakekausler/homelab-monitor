"""Registry digest fetch client (STAGE-003-008).

D-PUBLIC-IMAGES-V1: anonymous bearer-token flow for Docker Hub, GHCR,
quay.io, registry.k8s.io. Private credentials deferred to a follow-up.

D-RATE-LIMIT-AWARE: parses RateLimit-Limit / RateLimit-Remaining headers
(Docker Hub) and returns them to the caller, which surfaces them as a
gauge metric.

D-PER-IMAGE-FAILURE-ISOLATION: this client returns a FetchError union
member rather than raising; the collector wraps each image with
try/except for defense-in-depth.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from dataclasses import dataclass as _dataclass
from typing import Final, Literal, cast

import httpx
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.docker.image_ref_parser import (
    ImageRefParseError,
    parse_image_ref,
)

_MANIFEST_ACCEPT: Final[str] = (
    "application/vnd.docker.distribution.manifest.v2+json, "
    "application/vnd.oci.image.manifest.v1+json"
)
_HTTP_OK: Final[int] = 200
_HTTP_UNAUTHORIZED: Final[int] = 401
_HTTP_NOT_FOUND: Final[int] = 404
_HTTP_TOO_MANY_REQUESTS: Final[int] = 429
_REQUEST_TIMEOUT_SECONDS: Final[float] = 10.0


@_dataclass(frozen=True, slots=True)
class _RegistryConfig:
    manifest_host: str
    token_url: str | None
    token_service: str | None


_REGISTRY_CONFIGS: dict[str, _RegistryConfig] = {
    "docker.io": _RegistryConfig(
        manifest_host="registry-1.docker.io",
        token_url="https://auth.docker.io/token",
        token_service="registry.docker.io",
    ),
    "ghcr.io": _RegistryConfig(
        manifest_host="ghcr.io",
        token_url="https://ghcr.io/token",
        token_service="ghcr.io",
    ),
    "quay.io": _RegistryConfig(
        manifest_host="quay.io",
        token_url=None,
        token_service=None,
    ),
    "registry.k8s.io": _RegistryConfig(
        manifest_host="registry.k8s.io",
        token_url=None,
        token_service=None,
    ),
}


def _config_for(registry: str) -> _RegistryConfig:
    """Return _RegistryConfig for registry, or best-effort default."""
    if registry in _REGISTRY_CONFIGS:
        return _REGISTRY_CONFIGS[registry]
    return _RegistryConfig(
        manifest_host=registry,
        token_url=f"https://{registry}/v2/token",
        token_service=registry,
    )


FetchErrorReason = Literal[
    "parse_failed", "network_error", "auth_failed", "rate_limited", "not_found"
]


@dataclass(frozen=True, slots=True)
class FetchedDigest:
    digest: str
    rate_limit_remaining: int | None
    registry: str


@dataclass(frozen=True, slots=True)
class FetchError:
    reason: FetchErrorReason
    message: str
    registry: str


RegistryFetchResult = FetchedDigest | FetchError


class RegistryDigestClient:
    """Anonymous-token registry client for digest HEAD lookups."""

    def __init__(self, http_client: httpx.AsyncClient, log: BoundLogger) -> None:
        self._http: httpx.AsyncClient = http_client
        self._log: BoundLogger = log
        self._rate_limit_cooldown_until: dict[str, float] = {}

    async def fetch_latest_digest(self, image_ref: str) -> RegistryFetchResult:  # noqa: PLR0911
        """Fetch the latest registry digest for image_ref.

        Per-image try/except is the caller's responsibility (D-PER-IMAGE-
        FAILURE-ISOLATION). This method returns a FetchError on
        recoverable failures; raises only on programmer error.
        """
        try:
            parsed = parse_image_ref(image_ref)
        except ImageRefParseError as exc:
            return FetchError(reason="parse_failed", message=str(exc), registry="unknown")

        registry = parsed.registry
        # 1. Obtain bearer token (anonymous).
        # token is str (bearer), None (no auth needed), or FetchError
        token = await self._fetch_token(registry, parsed.repo)
        if isinstance(token, FetchError):
            return token

        # 2. HEAD manifest with bearer + Accept header.
        cfg = _config_for(registry)
        manifest_url = f"https://{cfg.manifest_host}/v2/{parsed.repo}/manifests/{parsed.tag}"
        headers = {"Accept": _MANIFEST_ACCEPT}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            resp = await self._http.head(
                manifest_url,
                headers=headers,
                timeout=_REQUEST_TIMEOUT_SECONDS,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            return FetchError(reason="network_error", message=str(exc), registry=registry)

        if resp.status_code == _HTTP_NOT_FOUND:
            return FetchError(reason="not_found", message=manifest_url, registry=registry)
        if resp.status_code == _HTTP_UNAUTHORIZED:
            return FetchError(reason="auth_failed", message=manifest_url, registry=registry)
        if resp.status_code == _HTTP_TOO_MANY_REQUESTS:
            retry_after_raw = resp.headers.get("Retry-After")
            if isinstance(
                retry_after_raw, str
            ):  # pragma: no cover -- requires real 429 with str Retry-After
                try:
                    cooldown_secs = float(retry_after_raw)
                    self._rate_limit_cooldown_until[registry] = time.monotonic() + cooldown_secs
                except ValueError:
                    pass
            return FetchError(reason="rate_limited", message=manifest_url, registry=registry)
        if resp.status_code != _HTTP_OK:
            return FetchError(
                reason="network_error",
                message=f"unexpected status {resp.status_code} from {manifest_url}",
                registry=registry,
            )

        rate_limit_remaining = _parse_rate_limit_remaining(resp.headers)
        digest = resp.headers.get("Docker-Content-Digest")
        if not digest:
            return FetchError(
                reason="network_error",
                message="missing Docker-Content-Digest header",
                registry=registry,
            )
        return FetchedDigest(
            digest=digest, rate_limit_remaining=rate_limit_remaining, registry=registry
        )

    # ---- Token endpoints ----

    async def _fetch_token(self, registry: str, repo: str) -> str | None | FetchError:  # noqa: PLR0911
        """Return bearer token (str), None (no auth needed), or FetchError.

        Returns None when the registry requires no token (quay.io, registry.k8s.io).
        Returns FetchError on auth/network failure.
        """
        token_url, params = _token_endpoint_for(registry, repo)
        if token_url is None:
            return None  # no auth needed
        try:
            resp = await self._http.get(token_url, params=params, timeout=_REQUEST_TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:  # pragma: no cover -- defensive
            return FetchError(reason="network_error", message=str(exc), registry=registry)
        if resp.status_code == _HTTP_UNAUTHORIZED:
            return FetchError(reason="auth_failed", message=token_url, registry=registry)
        if resp.status_code != _HTTP_OK:
            return FetchError(
                reason="network_error",
                message=f"token endpoint status {resp.status_code}",
                registry=registry,
            )
        try:
            body = resp.json()
        except ValueError as exc:  # pragma: no cover -- defensive
            return FetchError(reason="network_error", message=str(exc), registry=registry)
        if not isinstance(body, dict):
            return FetchError(
                reason="network_error", message="non-dict token body", registry=registry
            )
        body_dict: dict[str, object] = cast("dict[str, object]", body)
        token: str | None = cast(
            "str | None", body_dict.get("token") or body_dict.get("access_token")
        )
        if not isinstance(token, str) or not token:
            return FetchError(
                reason="auth_failed", message="empty token in body", registry=registry
            )
        return token

    def cooldown_until_for(
        self, registry: str
    ) -> float | None:  # pragma: no cover -- accessor; tested via collector integration
        """Return monotonic time when rate-limit cooldown expires, or None."""
        return self._rate_limit_cooldown_until.get(registry)


def _token_endpoint_for(registry: str, repo: str) -> tuple[str | None, dict[str, str]]:
    """Return (token_url, params) for the registry, or (None, {}) if no auth needed."""
    cfg = _config_for(registry)
    if cfg.token_url is None:
        return (None, {})
    return (
        cfg.token_url,
        {
            "service": cfg.token_service or registry,
            "scope": f"repository:{repo}:pull",
        },
    )


def _parse_rate_limit_remaining(headers: httpx.Headers) -> int | None:
    """Parse the RateLimit-Remaining header. Docker Hub format is
    'N;w=Window'; we extract N.
    """
    raw = headers.get("RateLimit-Remaining") or headers.get("ratelimit-remaining")
    if raw is None:
        return None
    # Docker Hub format: '100;w=21600' OR sometimes just '100'.
    head = raw.split(";", 1)[0].strip()
    try:
        v = int(head)
        return v if v >= 0 else None
    except ValueError:
        return None


__all__ = [
    "FetchError",
    "FetchedDigest",
    "RegistryDigestClient",
    "RegistryFetchResult",
]
