"""Async UDP DNS A-record probe helper (STAGE-006-014).

Mirror of probe_executor.py::execute_tcp but for UDP DNS queries.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class DnsProbeResult:
    """Outcome of a single UDP A-record probe.

    NEVER constructed on a raised exception; the resolver catches all exceptions
    and maps them to a typed DnsProbeResult.

    ``ok`` is True IFF the response is a reply (QR set), NOERROR (rcode 0),
    NOT truncated, and carries at least one answer record (ANCOUNT >= 1).

    ``latency_seconds`` is meaningful (a real round-trip) IFF ``error`` is None
    OR ``error`` is a rcode-class token (servfail/nxdomain/refused/no_answer) —
    i.e. whenever a response was actually received. For no-response failures
    (timeout/socket_error/malformed/id_mismatch) latency is the elapsed time
    but the collector OMITS the latency gauge (see PiholeDnsHealthCollector).
    """

    ok: bool
    rcode: int
    truncated: bool
    latency_seconds: float
    error: str | None


# Fixed query ID. Single in-flight query per call, so a constant is safe and
# keeps tests deterministic (a random ID would make crafted-response fixtures
# non-reproducible). We still verify the response ID matches this on parse.
_QUERY_ID: Final[int] = 0x1234

_QTYPE_A: Final[int] = 1
_QCLASS_IN: Final[int] = 1

# Outcome reason tokens (None == success). These feed the collector's outcome map.
_ERR_TIMEOUT: Final[str] = "timeout"
_ERR_SOCKET: Final[str] = "socket_error"
_ERR_MALFORMED: Final[str] = "malformed"
_ERR_ID_MISMATCH: Final[str] = "id_mismatch"
_ERR_TRUNCATED: Final[str] = "truncated"
_ERR_SERVFAIL: Final[str] = "servfail"
_ERR_NXDOMAIN: Final[str] = "nxdomain"
_ERR_REFUSED: Final[str] = "refused"
_ERR_NO_ANSWER: Final[str] = "no_answer"  # NOERROR but ANCOUNT == 0


def _encode_qname(qname: str) -> bytes:
    """Encode a domain name as DNS labels: each label length-prefixed, NUL-terminated.

    "dns.google.com" -> b"\\x03dns\\x06google\\x03com\\x00".
    Empty labels (leading/trailing/double dots) are skipped so a trailing dot
    (FQDN form) is tolerated.
    """
    out = bytearray()
    for label in qname.split("."):
        if not label:
            continue
        encoded = label.encode("ascii")
        out.append(len(encoded))
        out.extend(encoded)
    out.append(0)
    return bytes(out)


def _build_query(qname: str) -> bytes:
    """Build a minimal A-record DNS query datagram.

    Header (12 bytes): ID, flags=0x0100 (RD=recursion desired), QDCOUNT=1,
    ANCOUNT=0, NSCOUNT=0, ARCOUNT=0. Question: QNAME + QTYPE=A(1) +
    QCLASS=IN(1).
    """
    header = _QUERY_ID.to_bytes(2, "big")
    header += (0x0100).to_bytes(2, "big")  # flags: RD set
    header += (1).to_bytes(2, "big")  # QDCOUNT
    header += (0).to_bytes(2, "big")  # ANCOUNT
    header += (0).to_bytes(2, "big")  # NSCOUNT
    header += (0).to_bytes(2, "big")  # ARCOUNT
    question = _encode_qname(qname)
    question += _QTYPE_A.to_bytes(2, "big")
    question += _QCLASS_IN.to_bytes(2, "big")
    return header + question


def _parse_response(data: bytes, latency: float) -> DnsProbeResult:  # noqa: PLR0911
    """Parse ONLY: ID match, QR bit, TC bit, RCODE, ANCOUNT. Never raises.

    DNS header byte layout (big-endian):
      bytes 0-1 : ID
      byte 2    : QR(0x80) Opcode TC(0x02) RD
      byte 3    : RA Z RCODE(low 4 bits = & 0x0F)
      bytes 4-5 : QDCOUNT
      bytes 6-7 : ANCOUNT
    """
    if len(data) < 12:  # need a full header  # noqa: PLR2004
        return DnsProbeResult(
            ok=False,
            rcode=-1,
            truncated=False,
            latency_seconds=latency,
            error=_ERR_MALFORMED,
        )

    resp_id = int.from_bytes(data[0:2], "big")
    if resp_id != _QUERY_ID:
        return DnsProbeResult(
            ok=False,
            rcode=-1,
            truncated=False,
            latency_seconds=latency,
            error=_ERR_ID_MISMATCH,
        )

    flags_hi = data[2]
    flags_lo = data[3]
    qr = bool(flags_hi & 0x80)
    truncated = bool(flags_hi & 0x02)
    rcode = flags_lo & 0x0F
    ancount = int.from_bytes(data[6:8], "big")

    if not qr:  # not a response (QR bit clear) -> malformed for our purposes
        return DnsProbeResult(
            ok=False,
            rcode=rcode,
            truncated=truncated,
            latency_seconds=latency,
            error=_ERR_MALFORMED,
        )

    if truncated:
        return DnsProbeResult(
            ok=False,
            rcode=rcode,
            truncated=True,
            latency_seconds=latency,
            error=_ERR_TRUNCATED,
        )

    if rcode == 2:  # SERVFAIL  # noqa: PLR2004
        return DnsProbeResult(
            ok=False,
            rcode=rcode,
            truncated=False,
            latency_seconds=latency,
            error=_ERR_SERVFAIL,
        )
    if rcode == 3:  # NXDOMAIN  # noqa: PLR2004
        return DnsProbeResult(
            ok=False,
            rcode=rcode,
            truncated=False,
            latency_seconds=latency,
            error=_ERR_NXDOMAIN,
        )
    if rcode == 5:  # REFUSED  # noqa: PLR2004
        return DnsProbeResult(
            ok=False,
            rcode=rcode,
            truncated=False,
            latency_seconds=latency,
            error=_ERR_REFUSED,
        )
    if rcode != 0:  # any other non-zero rcode -> treat as malformed/unknown failure
        return DnsProbeResult(
            ok=False,
            rcode=rcode,
            truncated=False,
            latency_seconds=latency,
            error=_ERR_MALFORMED,
        )

    # rcode == 0 (NOERROR)
    if ancount < 1:  # answered, but no A record -> failure (no_answer)
        return DnsProbeResult(
            ok=False,
            rcode=0,
            truncated=False,
            latency_seconds=latency,
            error=_ERR_NO_ANSWER,
        )

    return DnsProbeResult(
        ok=True,
        rcode=0,
        truncated=False,
        latency_seconds=latency,
        error=None,
    )


class _DnsClientProtocol(asyncio.DatagramProtocol):
    """Collects the first datagram into a future. Mirrors the one-shot query model."""

    def __init__(self, on_response: asyncio.Future[bytes]) -> None:
        self._on_response = on_response

    def datagram_received(self, data: bytes, addr: object) -> None:
        if not self._on_response.done():
            self._on_response.set_result(data)

    def error_received(self, exc: Exception) -> None:
        if not self._on_response.done():
            self._on_response.set_exception(exc)


async def resolve_a(
    resolver_ip: str,
    qname: str,
    *,
    port: int = 53,
    timeout_seconds: float,
) -> DnsProbeResult:
    """Send one UDP A-record query to ``resolver_ip:port`` for ``qname``.

    NEVER raises — maps timeout / OSError / socket error / malformed query
    (non-ASCII qname) / malformed-too-short response / ID-mismatch to a typed
    :class:`DnsProbeResult`. UDP analogue of ``execute_tcp``.
    """
    start = time.monotonic()
    loop = asyncio.get_running_loop()
    on_response: asyncio.Future[bytes] = loop.create_future()

    transport: asyncio.DatagramTransport | None = None
    try:
        query = _build_query(qname)
        transport, _protocol = await asyncio.wait_for(
            loop.create_datagram_endpoint(
                lambda: _DnsClientProtocol(on_response),
                remote_addr=(resolver_ip, port),
            ),
            timeout=timeout_seconds,
        )
        transport.sendto(query)
        data = await asyncio.wait_for(on_response, timeout=timeout_seconds)
    except UnicodeError:  # non-ASCII qname could not be label-encoded
        return DnsProbeResult(
            ok=False,
            rcode=-1,
            truncated=False,
            latency_seconds=time.monotonic() - start,
            error=_ERR_MALFORMED,
        )
    except TimeoutError:
        return DnsProbeResult(
            ok=False,
            rcode=-1,
            truncated=False,
            latency_seconds=time.monotonic() - start,
            error=_ERR_TIMEOUT,
        )
    except (OSError, ConnectionError) as exc:  # socket creation / send / network error
        _ = exc  # error detail intentionally not surfaced in the typed token
        return DnsProbeResult(
            ok=False,
            rcode=-1,
            truncated=False,
            latency_seconds=time.monotonic() - start,
            error=_ERR_SOCKET,
        )
    finally:
        if transport is not None:  # pragma: no branch -- set whenever the try body completed
            with contextlib.suppress(OSError):
                transport.close()

    return _parse_response(data, time.monotonic() - start)
