"""Integration test: /api/logs/tail SSE endpoint against real VictoriaLogs.

Validates behaviour that unit/handler tests with mocked VL cannot confirm:
  1. Content-Type is text/event-stream.
  2. Log lines planted into VL appear as `event: line` SSE events within ~10s.
  3. Invalid LogsQL returns HTTP 422 (validates the VL 4xx -> 422 mapping).
  4. 503 + Retry-After when the connection cap is exceeded.

Requires the docker-compose.test.yml rig (``make integration``).  All tests
auto-skip fast when any required component is unavailable.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from .helpers.rig import Rig
from .helpers.rig_health import require_rig_components
from .helpers.vl_planter import plant_log_lines

# The tail endpoint streams indefinitely; cap client reads at this many seconds.
_CLIENT_TIMEOUT_S = 12.0
# Budget for a planted log line to appear in the tail stream.
_LINE_APPEAR_BUDGET_S = 10.0
# SSE iteration: how many bytes to read per chunk (generous for one event).
_CHUNK_BYTES = 4096

_TAIL_RETRY_AFTER_HEADER = "Retry-After"


def _dedicated_client(rig: Rig, timeout_s: float) -> httpx.Client:
    """A standalone client for streaming.

    httpx.Client is not designed to hold a long-lived streaming response while
    the same client is reused for other calls (see Scenario 4). Each streaming
    scenario opens its OWN client, seeded with the rig's session cookies, and
    closes it in a finally. Mirrors test_tail_503_cap_with_retry_after.
    """
    return httpx.Client(
        timeout=httpx.Timeout(timeout_s, read=timeout_s),
        follow_redirects=False,
        cookies=rig._client.cookies,  # pyright: ignore[reportPrivateUsage]
    )


@pytest.mark.integration
@pytest.mark.slow
def test_tail_content_type_is_event_stream() -> None:
    """GET /api/logs/tail returns Content-Type: text/event-stream."""
    require_rig_components("monitor", "victorialogs")

    with Rig.boot() as rig:
        client = _dedicated_client(rig, _CLIENT_TIMEOUT_S)
        try:
            with client.stream(
                "GET",
                f"{rig.urls.monitor}/api/logs/tail",
                params={"expr": "*"},
            ) as resp:
                # Status + headers are available as soon as the stream opens;
                # do NOT read the body — with expr=* and no new lines the server
                # sends nothing until the ~30s keepalive, which would exceed the
                # client read timeout. Asserting the response headers is enough
                # to confirm the SSE stream opened correctly.
                assert resp.status_code == 200, (  # noqa: PLR2004
                    f"Expected 200, got {resp.status_code}"
                )
                ct = resp.headers.get("content-type", "")
                assert "text/event-stream" in ct, f"Expected text/event-stream, got: {ct!r}"
        finally:
            client.close()


@pytest.mark.integration
@pytest.mark.slow
def test_tail_planted_line_appears_as_sse_event() -> None:
    """A log line planted directly into VL appears as event: line within ~10s.

    This validates the forward-cursor dedup logic works against real VL timestamps
    and that the SSE formatting round-trips correctly.
    """
    require_rig_components("monitor", "victorialogs")

    marker = f"rig-tail-{uuid.uuid4().hex}"

    with Rig.boot() as rig:
        client = _dedicated_client(rig, _LINE_APPEAR_BUDGET_S + 4)
        try:
            with client.stream(
                "GET",
                f"{rig.urls.monitor}/api/logs/tail",
                params={"expr": f'"{marker}"'},
            ) as resp:
                assert resp.status_code == 200, f"tail open failed: {resp.status_code}"  # noqa: PLR2004

                # Plant the line while the stream is open (after handshake).
                plant_log_lines(
                    host="rig-test",
                    service="tail-integration",
                    severity="info",
                    message=marker,
                    count=1,
                    base_time=datetime.now(UTC) + timedelta(seconds=1),
                    vl_url=rig.urls.victorialogs,
                )

                # Collect SSE events until we see `event: line` containing the marker
                # or time out.
                found = False
                deadline = time.time() + _LINE_APPEAR_BUDGET_S
                current_event: str | None = None
                current_data: list[str] = []

                for raw_line in resp.iter_lines():
                    if time.time() > deadline:
                        break
                    if raw_line == "":
                        # End of event block.
                        if current_event == "line" and current_data:
                            joined = "".join(current_data)
                            if marker in joined:
                                found = True
                                break
                        current_event = None
                        current_data = []
                        continue
                    if raw_line.startswith(":"):
                        # SSE comment (keepalive); skip.
                        continue
                    if raw_line.startswith("event:"):
                        current_event = raw_line[len("event:") :].strip()
                    elif raw_line.startswith("data:"):
                        current_data.append(raw_line[len("data:") :].strip())
        finally:
            client.close()

        assert found, (
            f"Marker {marker!r} did not appear as `event: line` in /api/logs/tail "
            f"within {_LINE_APPEAR_BUDGET_S}s. "
            "Possible causes: VL ingest lag, cursor dedup error, or SSE formatting bug."
        )


@pytest.mark.integration
@pytest.mark.slow
def test_tail_logsql_contract_matches_real_vl() -> None:
    """Validate the tail endpoint against VictoriaLogs v0.30.0's REAL contract.

    Confirmed during Refinement: VL returns 4xx ONLY for STRUCTURAL LogsQL
    errors (e.g. a dangling pipe `|limit 10` with no leading filter), and 200 +
    empty results for arbitrary phrase-garbage (it treats unrecognized text as a
    phrase filter). So:
      - structural-bad expr -> the pre-flight probe maps VL's 4xx -> 422 invalid_logsql.
      - phrase-garbage expr  -> valid phrase filter -> 200 event-stream, zero lines.
    This matches /logs/query and /logs/export (no client-side LogsQL validation).
    """
    require_rig_components("monitor", "victorialogs")

    with Rig.boot() as rig:
        # (a) Structural error: dangling pipe -> VL 4xx -> 422 invalid_logsql.
        structural = rig.get(
            "/api/logs/tail",
            params={"expr": "|limit 10"},
        )
        assert structural.status_code == 422, (  # noqa: PLR2004
            f"Expected 422 for structural-bad LogsQL ('|limit 10'), got "
            f"{structural.status_code}. Body: {structural.text[:400]}"
        )
        assert structural.json()["error"]["code"] == "invalid_logsql", (
            f"Expected error.code='invalid_logsql', got: {structural.json()}"
        )

        # (b) Phrase-garbage: valid LogsQL phrase filter -> 200 empty stream.
        client = _dedicated_client(rig, _CLIENT_TIMEOUT_S)
        try:
            with client.stream(
                "GET",
                f"{rig.urls.monitor}/api/logs/tail",
                params={"expr": "{{{{garbage phrase that matches nothing"},
            ) as resp:
                # Phrase-garbage is a valid LogsQL phrase filter matching nothing,
                # so the stream opens (200) but emits no `event: line` blocks until
                # the ~30s keepalive. Assert status + headers (available on open) and
                # do NOT read the body, which would block past the client timeout.
                assert resp.status_code == 200, (  # noqa: PLR2004
                    f"Expected 200 for phrase-garbage (valid phrase filter), got {resp.status_code}"
                )
                ct = resp.headers.get("content-type", "")
                assert "text/event-stream" in ct, f"Expected text/event-stream, got: {ct!r}"
        finally:
            client.close()


@pytest.mark.integration
@pytest.mark.slow
def test_tail_503_cap_with_retry_after() -> None:
    """Exceeding the tail connection cap returns 503 + Retry-After header.

    Opens N concurrent streams up to the cap, then verifies the (N+1)th
    connection gets 503.  Uses max_connections from TailConfig defaults (5).
    """
    require_rig_components("monitor", "victorialogs")

    # TailConfig default is max_connections=5 (load_tail_config()).
    # We open 5 streams, then the 6th must return 503.
    _MAX_CONNECTIONS = 5
    open_streams: list[contextlib.AbstractContextManager[httpx.Response]] = []
    open_contexts: list[httpx.Response] = []

    with Rig.boot() as rig:
        # We need a separate client per stream since httpx.Client is not
        # designed for concurrent streaming.  Use raw httpx.Client instances.
        clients: list[httpx.Client] = []
        try:
            # Open _MAX_CONNECTIONS streams.
            for i in range(_MAX_CONNECTIONS):
                c = httpx.Client(
                    timeout=httpx.Timeout(20.0, read=20.0),
                    follow_redirects=False,
                    cookies=rig._client.cookies,  # pyright: ignore[reportPrivateUsage]
                )
                clients.append(c)
                ctx = c.stream(
                    "GET",
                    f"{rig.urls.monitor}/api/logs/tail",
                    params={"expr": "*"},
                )
                resp = ctx.__enter__()
                open_streams.append(ctx)
                open_contexts.append(resp)
                assert resp.status_code == 200, f"Stream {i + 1} failed to open: {resp.status_code}"  # noqa: PLR2004

            # Now the (N+1)th connection should be rejected.
            extra_client = httpx.Client(
                timeout=httpx.Timeout(10.0, read=10.0),
                follow_redirects=False,
                cookies=rig._client.cookies,  # pyright: ignore[reportPrivateUsage]
            )
            clients.append(extra_client)
            cap_resp = extra_client.get(
                f"{rig.urls.monitor}/api/logs/tail",
                params={"expr": "*"},
            )
            assert cap_resp.status_code == 503, (  # noqa: PLR2004
                f"Expected 503 for over-cap connection, got {cap_resp.status_code}. "
                f"Body: {cap_resp.text[:300]}"
            )
            assert _TAIL_RETRY_AFTER_HEADER in cap_resp.headers, (
                f"Expected Retry-After header in 503 response, headers: {dict(cap_resp.headers)}"
            )
            retry_after = cap_resp.headers[_TAIL_RETRY_AFTER_HEADER]
            assert retry_after.isdigit() and int(retry_after) > 0, (
                f"Expected Retry-After to be a positive integer, got: {retry_after!r}"
            )

        finally:
            # Clean up: exit all open stream contexts.
            for ctx in open_streams:
                with contextlib.suppress(Exception):
                    ctx.__exit__(None, None, None)
            for c in clients:
                with contextlib.suppress(Exception):
                    c.close()
