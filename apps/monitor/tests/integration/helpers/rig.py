"""STAGE-001-021 integration test helper: Rig.

Wraps the common operations that integration tests perform against the live
docker-compose.test.yml stack:

  - Set fixture-host's controllable cpu_percent gauge via POST /control.
  - Plant a log line via noisy-logger (drives vector -> VL).
  - Wait (poll with deadline) for an alert to surface in /api/alerts.
  - Wait for a previously-firing alert to enter resolved state.
  - Consume the SSE stream and wait for an event of a given kind.
  - Cookie-session login + CSRF cookie capture (the alert list / SSE endpoints
    are session-only).

URL resolution: env vars (MONITOR_URL, FIXTURE_HOST_URL, NOISY_LOGGER_URL,
AM_URL, VL_URL) with sensible defaults for compose-internal DNS. The integration
tests inside the `integration-tests` container always have the env set.

Auth: session cookie for /api/* GETs (alerts/events). Token from
/shared/rig-token for token-only routes (e.g., /api/hb/*). Tests requesting
the rig fixture always get an already-logged-in client; the token is
exposed as a separate property.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

DEFAULT_MONITOR_URL = "http://monitor:9090"
DEFAULT_FIXTURE_HOST_URL = "http://fixture-host:8000"
DEFAULT_NOISY_LOGGER_URL = "http://noisy-logger:8001"
DEFAULT_AM_URL = "http://alertmanager:9093"
DEFAULT_VL_URL = "http://victorialogs:9428"
DEFAULT_RIG_TOKEN_FILE = "/shared/rig-token"

_HTTP_OK = 200


@dataclass(frozen=True, slots=True)
class RigUrls:
    """Resolved URLs for the rig fixtures + monitor."""

    monitor: str
    fixture_host: str
    noisy_logger: str
    alertmanager: str
    victorialogs: str

    @classmethod
    def from_env(cls) -> RigUrls:
        return cls(
            monitor=os.environ.get("MONITOR_URL", DEFAULT_MONITOR_URL).rstrip("/"),
            fixture_host=os.environ.get("FIXTURE_HOST_URL", DEFAULT_FIXTURE_HOST_URL).rstrip("/"),
            noisy_logger=os.environ.get("NOISY_LOGGER_URL", DEFAULT_NOISY_LOGGER_URL).rstrip("/"),
            alertmanager=os.environ.get("AM_URL", DEFAULT_AM_URL).rstrip("/"),
            victorialogs=os.environ.get("VL_URL", DEFAULT_VL_URL).rstrip("/"),
        )


def _read_token() -> str:
    """Read the bootstrap token from the shared volume."""
    path = Path(os.environ.get("RIG_API_TOKEN_FILE", DEFAULT_RIG_TOKEN_FILE))
    deadline = time.time() + 30
    while time.time() < deadline:
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
        time.sleep(0.5)
    msg = f"rig token file {path} not present or empty after 30s -- monitor bootstrap likely failed"
    raise RuntimeError(msg)


class Rig:
    """Test rig facade.

    Construct via `Rig.boot()` -- it reads env, opens an httpx.Client, and
    logs in to capture the session cookie + CSRF token. Tests call methods
    like `set_fixture_cpu(95)` then `wait_for_alert("FixtureHostHighCPU", ...)`.

    Always use as a context manager:

        with Rig.boot() as rig:
            rig.set_fixture_cpu(95)
            alert = rig.wait_for_alert("FixtureHostHighCPU", source_tool="vmalert-metrics")

    The context manager closes the underlying httpx.Client (no leaked
    sockets across pytest runs).
    """

    def __init__(self, urls: RigUrls, token: str, client: httpx.Client) -> None:
        self.urls = urls
        self.token = token
        self._client = client
        # Populated by _login(); CSRF header value for state-changing requests.
        self._csrf_token: str | None = None

    @classmethod
    @contextmanager
    def boot(cls) -> Generator[Rig, None, None]:
        urls = RigUrls.from_env()
        token = _read_token()
        client = httpx.Client(timeout=10.0, follow_redirects=False)
        try:
            rig = cls(urls=urls, token=token, client=client)
            rig._login()
            yield rig
        finally:
            client.close()

    # ----- auth helpers -----

    def _login(self) -> None:
        """Cookie-session login as the rig admin user; capture CSRF cookie."""
        username = os.environ.get("RIG_ADMIN_USERNAME", "admin")
        password = os.environ.get("RIG_ADMIN_PASSWORD", "rig-admin-pw-12chars")
        resp = self._client.post(
            f"{self.urls.monitor}/api/auth/login",
            json={"username": username, "password": password},
        )
        if resp.status_code != _HTTP_OK:
            msg = (
                f"rig login failed: status={resp.status_code} body={resp.text[:200]} "
                f"(check RIG_ADMIN_USERNAME / RIG_ADMIN_PASSWORD env match monitor bootstrap)"
            )
            raise RuntimeError(msg)
        # Session cookie is set via Set-Cookie; httpx.Client stores it on self._client.cookies.
        # CSRF cookie is also set; capture it for state-changing GET-test scenarios.
        self._csrf_token = self._client.cookies.get("homelab_monitor_csrf")

    def _session_headers(self) -> dict[str, str]:
        """Headers for cookie-session state-changing requests (CSRF only)."""
        return {"X-CSRF-Token": self._csrf_token} if self._csrf_token else {}

    def _token_headers(self) -> dict[str, str]:
        """Headers for token-auth requests."""
        return {"Authorization": f"Bearer {self.token}"}

    # ----- fixture-host control -----

    def set_fixture_cpu(self, value: int) -> None:
        """POST to fixture-host /control to mutate the cpu_percent gauge."""
        if not 0 <= value <= 100:  # noqa: PLR2004
            msg = f"fixture cpu value out of range: {value}"
            raise ValueError(msg)
        resp = httpx.post(
            f"{self.urls.fixture_host}/control",
            json={"cpu_percent": value},
            timeout=5.0,
        )
        resp.raise_for_status()

    # ----- noisy-logger driver -----

    def plant_log_via_noisy_logger(self, line: str) -> None:
        """POST to noisy-logger /log to print the line (drives vector -> VL)."""
        resp = httpx.post(
            f"{self.urls.noisy_logger}/log",
            json={"line": line},
            timeout=5.0,
        )
        resp.raise_for_status()

    # ----- alert polling -----

    def wait_for_alert(
        self,
        alertname: str,
        *,
        source_tool: str | None = None,
        severity: str | None = None,
        timeout_s: float = 60.0,
        poll_interval_s: float = 2.0,
    ) -> dict[str, Any]:
        """Poll GET /api/alerts until an alert with `alertname` (+ optional filters) appears.

        Returns the matching alert dict. Raises TimeoutError on timeout.

        `source_tool` and `severity` are matched as exact strings against the
        alert's flat fields (per AlertView schema). `alertname` is matched
        against `labels.alertname`.
        """
        deadline = time.time() + timeout_s
        last_seen: list[dict[str, Any]] = []
        while time.time() < deadline:
            resp = self._client.get(f"{self.urls.monitor}/api/alerts?status=firing&limit=200")
            if resp.status_code == _HTTP_OK:
                data = resp.json()
                items = data.get("items", [])
                last_seen = items
                for item in items:
                    if item.get("labels", {}).get("alertname") != alertname:
                        continue
                    if source_tool is not None and item.get("source_tool") != source_tool:
                        continue
                    if severity is not None and item.get("severity") != severity:
                        continue
                    return item
            time.sleep(poll_interval_s)
        last_seen_info = [
            (a.get("labels", {}).get("alertname"), a.get("source_tool")) for a in last_seen
        ]
        msg = (
            f"wait_for_alert({alertname!r}, source_tool={source_tool!r}, "
            f"severity={severity!r}) timed out after {timeout_s}s. "
            f"Last seen alerts: {last_seen_info}"
        )
        raise TimeoutError(msg)

    def wait_for_resolution(
        self,
        alert_id: str,
        *,
        timeout_s: float = 60.0,
        poll_interval_s: float = 2.0,
    ) -> dict[str, Any]:
        """Poll GET /api/alerts/{alert_id} until resolved_at is non-null.

        Returns the resolved alert dict. Raises TimeoutError on timeout.
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            resp = self._client.get(f"{self.urls.monitor}/api/alerts/{alert_id}")
            if resp.status_code == _HTTP_OK:
                data = resp.json()
                alert = data.get("alert", {})
                if alert.get("resolved_at"):
                    return alert
            time.sleep(poll_interval_s)
        msg = f"wait_for_resolution({alert_id!r}) timed out after {timeout_s}s"
        raise TimeoutError(msg)

    # ----- SSE consumer -----

    def wait_for_sse_event(
        self,
        kind: str,
        *,
        timeout_s: float = 60.0,
        match_alert_id: str | None = None,
    ) -> dict[str, Any]:
        """Open SSE stream; return first event payload matching `kind`.

        If `match_alert_id` is provided, additionally requires
        `payload['alert_id'] == match_alert_id` (skip events for other alerts).

        Raises TimeoutError if no matching event arrives within `timeout_s`.

        Implementation: opens GET /api/events with cookie auth, parses the
        SSE wire format line-by-line. The kernel emits `event: alert.firing`
        and `event: alert.resolved` (see kernel/alerts/events.py). Each event
        block ends with a blank line.
        """
        deadline = time.time() + timeout_s
        try:
            with self._client.stream(
                "GET",
                f"{self.urls.monitor}/api/events",
                timeout=httpx.Timeout(timeout_s, read=timeout_s),
            ) as resp:
                if resp.status_code != _HTTP_OK:
                    msg = f"SSE connect failed: status={resp.status_code}"
                    raise RuntimeError(msg)
                current_event: str | None = None
                current_data: list[str] = []
                for raw_line in resp.iter_lines():
                    if time.time() > deadline:
                        break
                    if raw_line == "":
                        # End of event block
                        if current_event == kind and current_data:
                            try:
                                payload = json.loads("".join(current_data))
                            except json.JSONDecodeError:
                                current_event = None
                                current_data = []
                                continue
                            if match_alert_id is None or payload.get("alert_id") == match_alert_id:
                                return payload
                        current_event = None
                        current_data = []
                        continue
                    if raw_line.startswith(":"):
                        # SSE comment (keepalive); skip
                        continue
                    if raw_line.startswith("event:"):
                        current_event = raw_line[len("event:") :].strip()
                    elif raw_line.startswith("data:"):
                        current_data.append(raw_line[len("data:") :].strip())
                    # Ignore `id:` lines and anything else
        except httpx.ReadTimeout as exc:
            msg = f"wait_for_sse_event({kind!r}) timed out after {timeout_s}s"
            raise TimeoutError(msg) from exc
        msg = f"wait_for_sse_event({kind!r}) loop exited without match"
        raise TimeoutError(msg)

    # ----- generic monitor client -----

    def get(self, path: str, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
        """Issue a GET against the monitor with the rig's session cookie attached."""
        return self._client.get(f"{self.urls.monitor}{path}", **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
        """Issue a POST against the monitor with cookie + CSRF header attached.

        For state-changing requests over the cookie auth path. Token-auth
        requests should construct their own headers via self.token.
        """
        merged_headers = dict(kwargs.pop("headers", {}))
        merged_headers.update(self._session_headers())
        return self._client.post(f"{self.urls.monitor}{path}", headers=merged_headers, **kwargs)
