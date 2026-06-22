"""unbound-control access layer (STAGE-006-003).

Runs ``unbound-control stats_noreset`` inside the ``pihole-unbound`` container via
the Docker socket exec API (:meth:`DockerSocketClient.exec_capture`), captures and
de-multiplexes stdout, and parses the ``key=value`` lines into a typed
:class:`UnboundStats`. This module emits NO metrics — it is consumed by
STAGE-006-013, which turns the parsed map into Prometheus series.

Design decisions (LOCKED in Design):
  - Decision B: keep the FULL parsed map (every ``key=value`` line) in
    ``UnboundStats.raw``; detect extended-statistics; record a raw line count.
  - Decision C: the exec backend is INJECTED via the narrow :class:`ExecCapture`
    Protocol (not the concrete DockerSocketClient) for testability.
  - Decision D: return-not-raise. Every failure becomes an :class:`UnboundError`
    with a discriminating ``reason``. ``extended_enabled=False`` is NOT an error
    (the single-threaded build without extended-statistics is a healthy state).

unbound ``stats_noreset`` emits ~154 ``key=value`` lines on this host (single-thread
build -> only ``thread0.*``). Values are int or float; ALL are parsed as float.
Extended-statistics (``extended-statistics: yes``) adds ``histogram.*`` and
``num.query.type.*`` families (among others); their presence is the sentinel for
``extended_enabled``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from homelab_monitor.kernel.docker.socket_client import (
    DockerSocketConnectionError,
    DockerSocketProtocolError,
    ExecResult,
)

_UNBOUND_CONTROL_CMD: list[str] = ["unbound-control", "stats_noreset"]
_DEFAULT_TIMEOUT_SECONDS: float = 5.0


class ExecCapture(Protocol):
    """Structural type for the exec backend the access layer depends on.

    Implemented by :class:`DockerSocketClient`. Depending on this Protocol rather
    than the concrete client keeps :func:`fetch_unbound_stats` testable with a
    lightweight fake (Decision C).
    """

    async def exec_capture(
        self,
        *,
        container_id: str,
        cmd: list[str],
        timeout_seconds: float,
    ) -> ExecResult: ...


@dataclass(frozen=True, slots=True)
class UnboundStats:
    """Parsed ``unbound-control stats_noreset`` output.

    ``raw`` is the FULL ``key=value`` map (every parsed line; values as floats).
    ``extended_enabled`` is True iff the extended-statistics families are present.
    ``raw_line_count`` is the number of successfully parsed key=value pairs.
    """

    raw: dict[str, float]
    extended_enabled: bool
    raw_line_count: int


UnboundErrorReason = Literal[
    "container_unreachable",
    "socket_error",
    "control_error",
    "empty_output",
    "parse_error",
]


@dataclass(frozen=True, slots=True)
class UnboundError:
    """A typed, non-raising unbound access-layer failure.

    ``reason`` discriminates the failure class:
      - ``container_unreachable`` — Docker protocol error (non-200 / bad JSON from
        the exec endpoints); the container/exec target is wrong or gone.
      - ``socket_error`` — Docker socket connect/transport failure OR an exec timeout.
      - ``control_error`` — the exec ran but ``unbound-control`` exited non-zero
        (the unbound daemon / control socket is down or misconfigured).
      - ``empty_output`` — exit 0 but stdout was empty / whitespace-only.
      - ``parse_error`` — stdout had non-blank lines but NONE parsed as key=value.
    """

    reason: UnboundErrorReason
    message: str


def parse_unbound_stats(stdout: str) -> UnboundStats | UnboundError:
    """Pure parser: turn ``unbound-control stats_noreset`` stdout into UnboundStats.

    Splits on newlines; for each non-blank line, splits on the FIRST ``=`` and
    parses the right side as float. A line without ``=`` or whose value is not a
    float is SKIPPED (non-fatal) as long as SOME line parses.

    Returns:
      - ``UnboundError("empty_output")`` if stdout is empty / whitespace-only.
      - ``UnboundError("parse_error")`` if there were non-blank lines but ZERO
        parsed successfully.
      - ``UnboundStats`` otherwise. ``extended_enabled`` is True iff any key starts
        with ``histogram.`` or ``num.query.type.`` (the cleanest sentinel families).
    """
    raw: dict[str, float] = {}
    saw_non_blank = False
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        saw_non_blank = True
        key, sep, value = stripped.partition("=")
        if not sep:
            continue
        try:
            raw[key] = float(value)
        except ValueError:
            continue
    if not raw:
        if not saw_non_blank:
            return UnboundError(
                reason="empty_output", message="unbound-control returned empty output"
            )
        return UnboundError(
            reason="parse_error",
            message="unbound-control output had no parseable key=value lines",
        )
    extended_enabled = any(
        k.startswith("histogram.") or k.startswith("num.query.type.") for k in raw
    )
    return UnboundStats(raw=raw, extended_enabled=extended_enabled, raw_line_count=len(raw))


async def fetch_unbound_stats(
    *,
    exec_backend: ExecCapture,
    container: str,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> UnboundStats | UnboundError:
    """Run ``unbound-control stats_noreset`` in ``container`` and parse the result.

    Never raises. Maps every failure to a typed :class:`UnboundError`:
      - DockerSocketConnectionError (incl. exec timeout) -> ``socket_error``.
      - DockerSocketProtocolError                        -> ``container_unreachable``.
      - exit_code != 0                                   -> ``control_error``.
      - exit 0 + empty/garbage stdout                    -> ``empty_output`` / ``parse_error``
        (delegated to :func:`parse_unbound_stats`).
    """
    try:
        result = await exec_backend.exec_capture(
            container_id=container,
            cmd=_UNBOUND_CONTROL_CMD,
            timeout_seconds=timeout_seconds,
        )
    except DockerSocketConnectionError as exc:
        return UnboundError(reason="socket_error", message=str(exc))
    except DockerSocketProtocolError as exc:
        return UnboundError(reason="container_unreachable", message=str(exc))
    if result.exit_code != 0:
        detail = result.stderr.strip() or f"exit {result.exit_code}"
        return UnboundError(
            reason="control_error",
            message=f"unbound-control failed (exit {result.exit_code}): {detail}",
        )
    return parse_unbound_stats(result.stdout)
