"""Tests for RegistryDigestClient (STAGE-003-008).

Anonymous bearer-token flows for Docker Hub, GHCR, quay.io, registry.k8s.io.
Rate-limit header parsing. Per-image failure isolation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import structlog

from homelab_monitor.kernel.docker.registry_digest_client import (
    FetchedDigest,
    FetchError,
    RegistryDigestClient,
    _parse_rate_limit_remaining,  # pyright: ignore[reportPrivateUsage]
)

_HTTP_STATUS_OK = 200
_HTTP_STATUS_NOT_FOUND = 404
_HTTP_STATUS_UNAUTHORIZED = 401
_HTTP_STATUS_TOO_MANY_REQUESTS = 429
_HTTP_STATUS_SERVER_ERROR = 500
_RATE_LIMIT_REMAINING_HIGH = 100
_RATE_LIMIT_REMAINING_MEDIUM = 42


@pytest.mark.asyncio
async def test_docker_hub_anonymous_flow_returns_digest() -> None:
    """Docker Hub (anonymous) flow: token endpoint + manifest HEAD -> digest."""
    log = structlog.get_logger()

    # Mock token endpoint response
    token_response = AsyncMock()
    token_response.status_code = _HTTP_STATUS_OK
    token_response.json = MagicMock(return_value={"token": "fake-token-123"})

    # Mock manifest HEAD response
    manifest_response = AsyncMock()
    manifest_response.status_code = _HTTP_STATUS_OK
    manifest_response.headers = {
        "Docker-Content-Digest": "sha256:abcdef1234567890",
        "RateLimit-Remaining": "100",
    }

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = token_response
    mock_http.head.return_value = manifest_response

    client = RegistryDigestClient(http_client=mock_http, log=log)
    result = await client.fetch_latest_digest("nginx:latest")

    assert isinstance(result, FetchedDigest)
    assert result.digest == "sha256:abcdef1234567890"
    assert result.rate_limit_remaining == _RATE_LIMIT_REMAINING_HIGH
    assert result.registry == "docker.io"


@pytest.mark.asyncio
async def test_docker_hub_library_namespace_expansion() -> None:
    """postgres:16 expands to library/postgres (Docker Hub library image)."""
    log = structlog.get_logger()

    token_response = AsyncMock()
    token_response.status_code = _HTTP_STATUS_OK
    token_response.json = MagicMock(return_value={"token": "token123"})

    manifest_response = AsyncMock()
    manifest_response.status_code = _HTTP_STATUS_OK
    manifest_response.headers = {"Docker-Content-Digest": "sha256:xyz"}

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = token_response
    mock_http.head.return_value = manifest_response

    client = RegistryDigestClient(http_client=mock_http, log=log)
    result = await client.fetch_latest_digest("postgres:16")

    assert isinstance(result, FetchedDigest)
    # Verify that the token endpoint was called with library/postgres
    token_call = mock_http.get.call_args
    assert "library/postgres" in str(token_call)


@pytest.mark.asyncio
async def test_ghcr_anonymous_flow_returns_digest() -> None:
    """GHCR (anonymous) flow: token endpoint + manifest HEAD."""
    log = structlog.get_logger()

    token_response = AsyncMock()
    token_response.status_code = _HTTP_STATUS_OK
    token_response.json = MagicMock(return_value={"token": "ghcr-token"})

    manifest_response = AsyncMock()
    manifest_response.status_code = _HTTP_STATUS_OK
    manifest_response.headers = {"Docker-Content-Digest": "sha256:ghcr123"}

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = token_response
    mock_http.head.return_value = manifest_response

    client = RegistryDigestClient(http_client=mock_http, log=log)
    result = await client.fetch_latest_digest("ghcr.io/myorg/myrepo:v1")

    assert isinstance(result, FetchedDigest)
    assert result.digest == "sha256:ghcr123"
    assert result.registry == "ghcr.io"


@pytest.mark.asyncio
async def test_quay_io_no_token_endpoint_called() -> None:
    """quay.io public images don't require token endpoint (returns None)."""
    log = structlog.get_logger()

    # No token endpoint call; go straight to manifest HEAD
    manifest_response = AsyncMock()
    manifest_response.status_code = _HTTP_STATUS_OK
    manifest_response.headers = {"Docker-Content-Digest": "sha256:quay123"}

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.head.return_value = manifest_response

    client = RegistryDigestClient(http_client=mock_http, log=log)
    result = await client.fetch_latest_digest("quay.io/myorg/myrepo:latest")

    assert isinstance(result, FetchedDigest)
    assert result.digest == "sha256:quay123"
    # Token endpoint should not be called for quay.io
    mock_http.get.assert_not_called()


@pytest.mark.asyncio
async def test_registry_k8s_io_no_token_endpoint_called() -> None:
    """registry.k8s.io public images don't require token endpoint."""
    log = structlog.get_logger()

    manifest_response = AsyncMock()
    manifest_response.status_code = _HTTP_STATUS_OK
    manifest_response.headers = {"Docker-Content-Digest": "sha256:k8s123"}

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.head.return_value = manifest_response

    client = RegistryDigestClient(http_client=mock_http, log=log)
    result = await client.fetch_latest_digest("registry.k8s.io/pause:3.8")

    assert isinstance(result, FetchedDigest)
    assert result.digest == "sha256:k8s123"
    mock_http.get.assert_not_called()


@pytest.mark.asyncio
async def test_default_registry_uses_v2_token_endpoint() -> None:
    """Custom registry uses default token endpoint pattern."""
    log = structlog.get_logger()

    token_response = AsyncMock()
    token_response.status_code = _HTTP_STATUS_OK
    token_response.json = MagicMock(return_value={"token": "custom-token"})

    manifest_response = AsyncMock()
    manifest_response.status_code = _HTTP_STATUS_OK
    manifest_response.headers = {"Docker-Content-Digest": "sha256:custom123"}

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = token_response
    mock_http.head.return_value = manifest_response

    client = RegistryDigestClient(http_client=mock_http, log=log)
    result = await client.fetch_latest_digest("myregistry.example/repo:tag")

    assert isinstance(result, FetchedDigest)
    assert result.digest == "sha256:custom123"
    assert result.registry == "myregistry.example"


@pytest.mark.asyncio
async def test_oci_manifest_accept_header_present() -> None:
    """Manifest HEAD includes OCI media type in Accept header."""
    log = structlog.get_logger()

    token_response = AsyncMock()
    token_response.status_code = 200
    token_response.json = MagicMock(return_value={"token": "token"})

    manifest_response = AsyncMock()
    manifest_response.status_code = 200
    manifest_response.headers = {"Docker-Content-Digest": "sha256:digest"}

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = token_response
    mock_http.head.return_value = manifest_response

    client = RegistryDigestClient(http_client=mock_http, log=log)
    await client.fetch_latest_digest("nginx:latest")

    # Check that manifest HEAD was called with Accept header including OCI media type
    manifest_call = mock_http.head.call_args
    headers = manifest_call[1]["headers"]
    assert "application/vnd.oci.image.manifest.v1+json" in headers["Accept"]


@pytest.mark.asyncio
async def test_v2_docker_accept_header_present() -> None:
    """Manifest HEAD includes Docker V2 media type in Accept header."""
    log = structlog.get_logger()

    token_response = AsyncMock()
    token_response.status_code = _HTTP_STATUS_OK
    token_response.json = MagicMock(return_value={"token": "token"})

    manifest_response = AsyncMock()
    manifest_response.status_code = _HTTP_STATUS_OK
    manifest_response.headers = {"Docker-Content-Digest": "sha256:digest"}

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = token_response
    mock_http.head.return_value = manifest_response

    client = RegistryDigestClient(http_client=mock_http, log=log)
    await client.fetch_latest_digest("nginx:latest")

    manifest_call = mock_http.head.call_args
    headers = manifest_call[1]["headers"]
    assert "application/vnd.docker.distribution.manifest.v2+json" in headers["Accept"]


@pytest.mark.asyncio
async def test_parse_failure_returns_fetch_error_parse_failed() -> None:
    """Unparseable image ref (e.g., <none>) returns FetchError with reason=parse_failed."""
    log = structlog.get_logger()

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    client = RegistryDigestClient(http_client=mock_http, log=log)

    # <none> is unparseable
    result = await client.fetch_latest_digest("<none>")

    assert isinstance(result, FetchError)
    assert result.reason == "parse_failed"
    assert result.registry == "unknown"


@pytest.mark.asyncio
async def test_404_manifest_returns_not_found() -> None:
    """Manifest HEAD returns 404 -> FetchError with reason=not_found."""
    log = structlog.get_logger()

    token_response = AsyncMock()
    token_response.status_code = 200
    token_response.json = MagicMock(return_value={"token": "token"})

    manifest_response = AsyncMock()
    manifest_response.status_code = _HTTP_STATUS_NOT_FOUND

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = token_response
    mock_http.head.return_value = manifest_response

    client = RegistryDigestClient(http_client=mock_http, log=log)
    result = await client.fetch_latest_digest("nginx:nonexistent-tag")

    assert isinstance(result, FetchError)
    assert result.reason == "not_found"


@pytest.mark.asyncio
async def test_401_manifest_returns_auth_failed() -> None:
    """Manifest HEAD returns 401 -> FetchError with reason=auth_failed."""
    log = structlog.get_logger()

    token_response = AsyncMock()
    token_response.status_code = _HTTP_STATUS_OK
    token_response.json = MagicMock(return_value={"token": "token"})

    manifest_response = AsyncMock()
    manifest_response.status_code = _HTTP_STATUS_UNAUTHORIZED

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = token_response
    mock_http.head.return_value = manifest_response

    client = RegistryDigestClient(http_client=mock_http, log=log)
    result = await client.fetch_latest_digest("private.registry/private:image")

    assert isinstance(result, FetchError)
    assert result.reason == "auth_failed"


@pytest.mark.asyncio
async def test_429_returns_rate_limited() -> None:
    """Manifest HEAD returns 429 -> FetchError with reason=rate_limited."""
    log = structlog.get_logger()

    token_response = AsyncMock()
    token_response.status_code = _HTTP_STATUS_OK
    token_response.json = MagicMock(return_value={"token": "token"})

    manifest_response = AsyncMock()
    manifest_response.status_code = _HTTP_STATUS_TOO_MANY_REQUESTS

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = token_response
    mock_http.head.return_value = manifest_response

    client = RegistryDigestClient(http_client=mock_http, log=log)
    result = await client.fetch_latest_digest("nginx:latest")

    assert isinstance(result, FetchError)
    assert result.reason == "rate_limited"


@pytest.mark.asyncio
async def test_500_returns_network_error() -> None:
    """Manifest HEAD returns 5xx -> FetchError with reason=network_error."""
    log = structlog.get_logger()

    token_response = AsyncMock()
    token_response.status_code = _HTTP_STATUS_OK
    token_response.json = MagicMock(return_value={"token": "token"})

    manifest_response = AsyncMock()
    manifest_response.status_code = _HTTP_STATUS_SERVER_ERROR

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = token_response
    mock_http.head.return_value = manifest_response

    client = RegistryDigestClient(http_client=mock_http, log=log)
    result = await client.fetch_latest_digest("nginx:latest")

    assert isinstance(result, FetchError)
    assert result.reason == "network_error"


@pytest.mark.asyncio
async def test_missing_digest_header_returns_network_error() -> None:
    """Manifest HEAD 200 but missing Docker-Content-Digest -> network_error."""
    log = structlog.get_logger()

    token_response = AsyncMock()
    token_response.status_code = _HTTP_STATUS_OK
    token_response.json = MagicMock(return_value={"token": "token"})

    manifest_response = AsyncMock()
    manifest_response.status_code = _HTTP_STATUS_OK
    manifest_response.headers = {}  # No Docker-Content-Digest

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = token_response
    mock_http.head.return_value = manifest_response

    client = RegistryDigestClient(http_client=mock_http, log=log)
    result = await client.fetch_latest_digest("nginx:latest")

    assert isinstance(result, FetchError)
    assert result.reason == "network_error"
    assert "Docker-Content-Digest" in result.message


@pytest.mark.asyncio
async def test_token_endpoint_500_returns_network_error() -> None:
    """Token endpoint returns 500 -> FetchError with reason=network_error."""
    log = structlog.get_logger()

    token_response = AsyncMock()
    token_response.status_code = _HTTP_STATUS_SERVER_ERROR

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = token_response

    client = RegistryDigestClient(http_client=mock_http, log=log)
    result = await client.fetch_latest_digest("nginx:latest")

    assert isinstance(result, FetchError)
    assert result.reason == "network_error"


@pytest.mark.asyncio
async def test_token_endpoint_401_returns_auth_failed() -> None:
    """Token endpoint returns 401 -> FetchError with reason=auth_failed."""
    log = structlog.get_logger()

    token_response = AsyncMock()
    token_response.status_code = _HTTP_STATUS_UNAUTHORIZED

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = token_response

    client = RegistryDigestClient(http_client=mock_http, log=log)
    result = await client.fetch_latest_digest("nginx:latest")

    assert isinstance(result, FetchError)
    assert result.reason == "auth_failed"


@pytest.mark.asyncio
async def test_token_body_missing_token_returns_auth_failed() -> None:
    """Token endpoint 200 but missing token field -> auth_failed."""
    log = structlog.get_logger()

    token_response = AsyncMock()
    token_response.status_code = _HTTP_STATUS_OK
    token_response.json = MagicMock(return_value={"no_token_field": "value"})

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = token_response

    client = RegistryDigestClient(http_client=mock_http, log=log)
    result = await client.fetch_latest_digest("nginx:latest")

    assert isinstance(result, FetchError)
    assert result.reason == "auth_failed"


@pytest.mark.asyncio
async def test_rate_limit_remaining_parsed_from_header() -> None:
    """RateLimit-Remaining with format '100;w=21600' parses to 100."""
    log = structlog.get_logger()

    token_response = AsyncMock()
    token_response.status_code = _HTTP_STATUS_OK
    token_response.json = MagicMock(return_value={"token": "token"})

    manifest_response = AsyncMock()
    manifest_response.status_code = _HTTP_STATUS_OK
    manifest_response.headers = {
        "Docker-Content-Digest": "sha256:digest",
        "RateLimit-Remaining": "100;w=21600",
    }

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = token_response
    mock_http.head.return_value = manifest_response

    client = RegistryDigestClient(http_client=mock_http, log=log)
    result = await client.fetch_latest_digest("nginx:latest")

    assert isinstance(result, FetchedDigest)
    assert result.rate_limit_remaining == _RATE_LIMIT_REMAINING_HIGH


@pytest.mark.asyncio
async def test_rate_limit_remaining_plain_int() -> None:
    """RateLimit-Remaining as plain int '42' parses to 42."""
    log = structlog.get_logger()

    token_response = AsyncMock()
    token_response.status_code = _HTTP_STATUS_OK
    token_response.json = MagicMock(return_value={"token": "token"})

    manifest_response = AsyncMock()
    manifest_response.status_code = _HTTP_STATUS_OK
    manifest_response.headers = {
        "Docker-Content-Digest": "sha256:digest",
        "RateLimit-Remaining": "42",
    }

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = token_response
    mock_http.head.return_value = manifest_response

    client = RegistryDigestClient(http_client=mock_http, log=log)
    result = await client.fetch_latest_digest("nginx:latest")

    assert isinstance(result, FetchedDigest)
    assert result.rate_limit_remaining == _RATE_LIMIT_REMAINING_MEDIUM


@pytest.mark.asyncio
async def test_rate_limit_remaining_missing_returns_none() -> None:
    """RateLimit-Remaining header missing -> rate_limit_remaining is None."""
    log = structlog.get_logger()

    token_response = AsyncMock()
    token_response.status_code = _HTTP_STATUS_OK
    token_response.json = MagicMock(return_value={"token": "token"})

    manifest_response = AsyncMock()
    manifest_response.status_code = _HTTP_STATUS_OK
    manifest_response.headers = {
        "Docker-Content-Digest": "sha256:digest",
        # No RateLimit-Remaining
    }

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = token_response
    mock_http.head.return_value = manifest_response

    client = RegistryDigestClient(http_client=mock_http, log=log)
    result = await client.fetch_latest_digest("nginx:latest")

    assert isinstance(result, FetchedDigest)
    assert result.rate_limit_remaining is None


@pytest.mark.asyncio
async def test_network_error_raises_propagates_as_fetch_error() -> None:
    """httpx.ConnectError during manifest HEAD -> FetchError with reason=network_error."""
    log = structlog.get_logger()

    token_response = AsyncMock()
    token_response.status_code = 200
    token_response.json = MagicMock(return_value={"token": "token"})

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = token_response
    mock_http.head.side_effect = httpx.ConnectError("connection refused")

    client = RegistryDigestClient(http_client=mock_http, log=log)
    result = await client.fetch_latest_digest("nginx:latest")

    assert isinstance(result, FetchError)
    assert result.reason == "network_error"


@pytest.mark.asyncio
async def test_fetch_latest_digest_returns_network_error_when_token_body_is_not_dict() -> None:
    """Token endpoint returning non-dict JSON triggers network_error."""
    log = structlog.get_logger()

    token_response = AsyncMock()
    token_response.status_code = _HTTP_STATUS_OK
    token_response.json = MagicMock(return_value=[])  # non-dict

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = token_response

    client = RegistryDigestClient(http_client=mock_http, log=log)
    result = await client.fetch_latest_digest("nginx:latest")

    assert isinstance(result, FetchError)
    assert result.reason == "network_error"
    assert "non-dict" in result.message


def test_parse_rate_limit_remaining_returns_none_for_non_integer() -> None:
    """Malformed rate-limit header (non-integer) returns None."""
    headers = httpx.Headers({"RateLimit-Remaining": "abc;w=21600"})
    result = _parse_rate_limit_remaining(headers)
    assert result is None
