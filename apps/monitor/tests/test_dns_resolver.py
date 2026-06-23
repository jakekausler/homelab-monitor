"""Unit tests for the async UDP DNS probe helper (STAGE-006-014).

100% branch coverage across: query build, QNAME encode (basic + trailing dot),
response parse (ok / servfail / nxdomain / refused / no_answer / truncated /
malformed-short / wrong-qr / wrong-id / other-rcode), transport success,
timeout, socket error, close-raises-suppressed, and the protocol done-guards.
"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from homelab_monitor.kernel.dns import resolve_a
from homelab_monitor.kernel.dns import resolver as dns_resolver

# Header layout: [0:2]=ID, [2]=flags_hi(QR/Op/TC/RD), [3]=flags_lo(RA/Z/RCODE),
#                [4:6]=QDCOUNT, [6:8]=ANCOUNT, [8:10]=NSCOUNT, [10:12]=ARCOUNT.

# NOERROR + 1 answer -> ok. flags_hi=0x81 (QR=1, RD=1), flags_lo=0x80
# (RA=1, rcode=0), QDCOUNT=1, ANCOUNT=1. Trailing bytes (the answer body) are
# NOT parsed.
_RESP_OK = (
    b"\x12\x34\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00"
    b"\x00\x00\x00\x01\x00\x01"  # filler answer-ish bytes; parser ignores past byte 8
)

# SERVFAIL: flags_lo low nibble = 2. ANCOUNT=0.
_RESP_SERVFAIL = b"\x12\x34\x81\x82\x00\x01\x00\x00\x00\x00\x00\x00"

# NXDOMAIN: flags_lo low nibble = 3.
_RESP_NXDOMAIN = b"\x12\x34\x81\x83\x00\x01\x00\x00\x00\x00\x00\x00"

# REFUSED: flags_lo low nibble = 5.
_RESP_REFUSED = b"\x12\x34\x81\x85\x00\x01\x00\x00\x00\x00\x00\x00"

# NOERROR but ANCOUNT=0 -> no_answer (resolver replied, no A record).
_RESP_NO_ANSWER = b"\x12\x34\x81\x80\x00\x01\x00\x00\x00\x00\x00\x00"

# Truncated: flags_hi has TC bit (0x02) set: 0x81 | 0x02 = 0x83. rcode 0.
_RESP_TRUNCATED = b"\x12\x34\x83\x80\x00\x01\x00\x00\x00\x00\x00\x00"

# Too-short / malformed: fewer than 12 bytes.
_RESP_SHORT = b"\x12\x34\x81"

# Wrong ID: 0x9999 != 0x1234.
_RESP_WRONG_ID = b"\x99\x99\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00"

# QR bit clear (flags_hi = 0x01, RD only, QR=0) -> malformed (not a response).
_RESP_NO_QR = b"\x12\x34\x01\x80\x00\x01\x00\x01\x00\x00\x00\x00"

# Other non-zero rcode (rcode=4 NOTIMP): flags_lo low nibble = 4 -> malformed
# branch.
_RESP_OTHER_RCODE = b"\x12\x34\x81\x84\x00\x01\x00\x00\x00\x00\x00\x00"


class _FakeTransport:
    def __init__(self, on_close_raise: bool = False) -> None:
        self.sent: list[bytes] = []
        self.closed = False
        self._on_close_raise = on_close_raise

    def sendto(self, data: bytes, addr: object = None) -> None:
        self.sent.append(data)

    def close(self) -> None:
        self.closed = True
        if self._on_close_raise:
            raise OSError("close failed")


def _install_fake_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: bytes | None,
    create_raises: Exception | None = None,
    close_raises: bool = False,
    deliver: bool = True,
) -> _FakeTransport:
    """Patch the running loop's create_datagram_endpoint.

    - create_raises: endpoint creation raises this (socket_error / timeout test).
    - response: bytes the protocol receives via datagram_received (if deliver).
    - deliver=False: never deliver -> on_response future never resolves (timeout).
    """
    transport = _FakeTransport(on_close_raise=close_raises)

    async def fake_create(
        protocol_factory: object, *args: object, **kwargs: object
    ) -> tuple[_FakeTransport, object]:
        if create_raises is not None:
            raise create_raises
        protocol = cast(
            "dns_resolver._DnsClientProtocol",  # pyright: ignore[reportPrivateUsage]
            protocol_factory(),  # type: ignore[misc]
        )
        if deliver and response is not None:
            protocol.datagram_received(response, ("1.2.3.4", 53))
        return transport, protocol

    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "create_datagram_endpoint", fake_create)
    return transport


def test_encode_qname_basic() -> None:
    """_encode_qname("dns.google.com") encodes correctly."""
    result = dns_resolver._encode_qname("dns.google.com")  # pyright: ignore[reportPrivateUsage]
    assert result == b"\x03dns\x06google\x03com\x00"


def test_encode_qname_trailing_dot() -> None:
    """_encode_qname with trailing dot (empty label -> continue branch)."""
    result = dns_resolver._encode_qname("dns.google.com.")  # pyright: ignore[reportPrivateUsage]
    assert result == b"\x03dns\x06google\x03com\x00"


def test_build_query_structure() -> None:
    """_build_query produces correct header structure."""
    query = dns_resolver._build_query("dns.google.com")  # pyright: ignore[reportPrivateUsage]
    assert query[0:2] == b"\x12\x34"  # ID
    assert query[2:4] == b"\x01\x00"  # flags with RD set
    assert query[4:6] == b"\x00\x01"  # QDCOUNT=1
    assert query[-4:] == b"\x00\x01\x00\x01"  # QTYPE A, QCLASS IN


def test_parse_ok() -> None:
    """_parse_response with NOERROR + answer -> ok=True."""
    result = dns_resolver._parse_response(  # pyright: ignore[reportPrivateUsage]
        _RESP_OK, 0.01
    )
    assert result.ok is True
    assert result.rcode == 0
    assert result.truncated is False
    assert result.error is None
    assert result.latency_seconds == pytest.approx(0.01)  # pyright: ignore[reportUnknownMemberType]


def test_parse_servfail() -> None:
    """_parse_response with SERVFAIL (rcode 2)."""
    result = dns_resolver._parse_response(  # pyright: ignore[reportPrivateUsage]
        _RESP_SERVFAIL, 0.02
    )
    assert result.ok is False
    assert result.rcode == 2  # noqa: PLR2004
    assert result.error == "servfail"


def test_parse_nxdomain() -> None:
    """_parse_response with NXDOMAIN (rcode 3)."""
    result = dns_resolver._parse_response(  # pyright: ignore[reportPrivateUsage]
        _RESP_NXDOMAIN, 0.03
    )
    assert result.ok is False
    assert result.rcode == 3  # noqa: PLR2004
    assert result.error == "nxdomain"


def test_parse_refused() -> None:
    """_parse_response with REFUSED (rcode 5)."""
    result = dns_resolver._parse_response(  # pyright: ignore[reportPrivateUsage]
        _RESP_REFUSED, 0.04
    )
    assert result.ok is False
    assert result.rcode == 5  # noqa: PLR2004
    assert result.error == "refused"


def test_parse_no_answer() -> None:
    """_parse_response with NOERROR but ANCOUNT=0."""
    result = dns_resolver._parse_response(  # pyright: ignore[reportPrivateUsage]
        _RESP_NO_ANSWER, 0.05
    )
    assert result.ok is False
    assert result.rcode == 0
    assert result.error == "no_answer"


def test_parse_truncated() -> None:
    """_parse_response with TC bit set (truncated, checked before rcode)."""
    result = dns_resolver._parse_response(  # pyright: ignore[reportPrivateUsage]
        _RESP_TRUNCATED, 0.06
    )
    assert result.ok is False
    assert result.truncated is True
    assert result.error == "truncated"


def test_parse_short_malformed() -> None:
    """_parse_response with too-short data."""
    result = dns_resolver._parse_response(  # pyright: ignore[reportPrivateUsage]
        _RESP_SHORT, 0.07
    )
    assert result.ok is False
    assert result.rcode == -1
    assert result.error == "malformed"


def test_parse_wrong_id() -> None:
    """_parse_response with wrong ID (mismatch)."""
    result = dns_resolver._parse_response(  # pyright: ignore[reportPrivateUsage]
        _RESP_WRONG_ID, 0.08
    )
    assert result.ok is False
    assert result.rcode == -1
    assert result.error == "id_mismatch"


def test_parse_no_qr() -> None:
    """_parse_response with QR bit clear (not a response)."""
    result = dns_resolver._parse_response(  # pyright: ignore[reportPrivateUsage]
        _RESP_NO_QR, 0.09
    )
    assert result.ok is False
    assert result.error == "malformed"


def test_parse_other_rcode() -> None:
    """_parse_response with other non-zero rcode (falls through to malformed)."""
    result = dns_resolver._parse_response(  # pyright: ignore[reportPrivateUsage]
        _RESP_OTHER_RCODE, 0.10
    )
    assert result.ok is False
    assert result.rcode == 4  # noqa: PLR2004
    assert result.error == "malformed"


@pytest.mark.asyncio
async def test_resolve_a_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_a with successful response."""
    _install_fake_endpoint(monkeypatch, response=_RESP_OK)

    result = await resolve_a("1.2.3.4", "dns.google.com", timeout_seconds=1.0)

    assert result.ok is True
    assert result.error is None


@pytest.mark.asyncio
async def test_resolve_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_a times out when datagram never arrives."""
    transport = _install_fake_endpoint(monkeypatch, response=None, deliver=False)

    result = await resolve_a("1.2.3.4", "dns.google.com", timeout_seconds=0.01)

    assert result.ok is False
    assert result.error == "timeout"
    assert transport.closed is True


@pytest.mark.asyncio
async def test_resolve_a_socket_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_a socket error (endpoint creation raises)."""
    _install_fake_endpoint(
        monkeypatch,
        response=None,
        create_raises=OSError("no route"),
    )

    result = await resolve_a("1.2.3.4", "dns.google.com", timeout_seconds=1.0)

    assert result.ok is False
    assert result.rcode == -1
    assert result.error == "socket_error"
    # endpoint creation failed before transport assignment, finally block False branch


@pytest.mark.asyncio
async def test_resolve_a_close_raises_suppressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resolve_a with close() raising OSError (suppressed)."""
    transport = _install_fake_endpoint(monkeypatch, response=_RESP_OK, close_raises=True)

    result = await resolve_a("1.2.3.4", "dns.google.com", timeout_seconds=1.0)

    assert result.ok is True
    assert transport.closed is True  # close was called but raised


@pytest.mark.asyncio
async def test_resolve_a_non_ascii_qname_maps_to_malformed() -> None:
    """resolve_a with non-ASCII qname returns malformed, never raises."""
    result = await resolve_a("192.168.2.148", "münchen.example", timeout_seconds=1.0)
    assert result.ok is False
    assert result.error == "malformed"
    assert result.rcode == -1


def test_protocol_second_datagram_ignored() -> None:
    """_DnsClientProtocol: second datagram ignored (done guard)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        fut: asyncio.Future[bytes] = loop.create_future()
        protocol = dns_resolver._DnsClientProtocol(fut)  # pyright: ignore[reportPrivateUsage]

        # First datagram resolves the future
        protocol.datagram_received(_RESP_OK, ("1.2.3.4", 53))
        assert fut.result() == _RESP_OK

        # Second datagram is ignored (not-done guard is False)
        protocol.datagram_received(_RESP_SERVFAIL, ("1.2.3.4", 53))
        assert fut.result() == _RESP_OK  # still the first one
    finally:
        loop.close()


def test_protocol_error_received_sets_exception() -> None:
    """_DnsClientProtocol: error_received sets exception on future."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        fut: asyncio.Future[bytes] = loop.create_future()
        protocol = dns_resolver._DnsClientProtocol(fut)  # pyright: ignore[reportPrivateUsage]

        exc = OSError("network error")
        protocol.error_received(exc)
        assert fut.exception() is exc

        # Second error is ignored (done guard is False)
        exc2 = OSError("second error")
        protocol.error_received(exc2)
        assert fut.exception() is exc  # still the first one
    finally:
        loop.close()
