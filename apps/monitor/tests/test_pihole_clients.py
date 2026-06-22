"""Tests for the Pi-hole client classification helper (STAGE-006-004).

Target: 100% branch coverage of classify_one and classify_clients.
"""

from __future__ import annotations

import random

from homelab_monitor.kernel.pihole.clients import (
    RawClient,
    cap_domains,
    classify_clients,
    classify_one,
)

# ──────────────────────────────────────────────────────────────────────────────
# classify_one — loopback-by-IP branches
# ──────────────────────────────────────────────────────────────────────────────

_HOST = "192.168.2.148"
_RESOLVER_NAMES: frozenset[str] = frozenset({"pi.hole", "localhost"})


def test_classify_one_loopback_127_local() -> None:
    """127.0.0.1 with non-resolver name and host set → local."""
    kind, host = classify_one("127.0.0.1", "myproccess", host_lan_ip=_HOST)
    assert kind == "local"
    assert host == _HOST


def test_classify_one_loopback_ipv6_1_local() -> None:
    """::1 with unknown name and host set → local."""
    kind, host = classify_one("::1", "somehost", host_lan_ip=_HOST)
    assert kind == "local"
    assert host == _HOST


def test_classify_one_loopback_double_colon_local() -> None:
    """:: with unknown name and host set → local."""
    kind, host = classify_one("::", "whatever", host_lan_ip=_HOST)
    assert kind == "local"
    assert host == _HOST


def test_classify_one_loopback_resolver_self_pihole() -> None:
    """127.0.0.1 with name 'pi.hole' → resolver_self, host stamped."""
    kind, host = classify_one("127.0.0.1", "pi.hole", host_lan_ip=_HOST)
    assert kind == "resolver_self"
    assert host == _HOST


def test_classify_one_loopback_resolver_self_localhost() -> None:
    """127.0.0.1 with name 'localhost' → resolver_self, host stamped."""
    kind, host = classify_one("127.0.0.1", "localhost", host_lan_ip=_HOST)
    assert kind == "resolver_self"
    assert host == _HOST


# ──────────────────────────────────────────────────────────────────────────────
# classify_one — loopback-by-name branch ("pi.hole" name, non-loopback IP)
# ──────────────────────────────────────────────────────────────────────────────


def test_classify_one_loopback_by_name_pihole() -> None:
    """Name 'pi.hole' triggers loopback path even on a non-loopback IP."""
    kind, host = classify_one("10.0.0.1", "pi.hole", host_lan_ip=_HOST)
    assert kind == "resolver_self"
    assert host == _HOST


# ──────────────────────────────────────────────────────────────────────────────
# classify_one — unattributed branch (empty host_lan_ip)
# ──────────────────────────────────────────────────────────────────────────────


def test_classify_one_loopback_empty_host_lan_ip_returns_unattributed() -> None:
    """Loopback IP with empty host_lan_ip → unattributed, host None."""
    kind, host = classify_one("127.0.0.1", "pi.hole", host_lan_ip="")
    assert kind == "unattributed"
    assert host is None


def test_classify_one_loopback_whitespace_host_lan_ip_returns_unattributed() -> None:
    """Loopback IP with whitespace-only host_lan_ip → unattributed (falsy after strip)."""
    kind, host = classify_one("127.0.0.1", "pi.hole", host_lan_ip="   ")
    assert kind == "unattributed"
    assert host is None


def test_classify_one_loopback_resolver_name_empty_host_still_unattributed() -> None:
    """Even a resolver_names match does NOT override unattributed when host is empty."""
    kind, host = classify_one("::1", "localhost", host_lan_ip="")
    assert kind == "unattributed"
    assert host is None


# ──────────────────────────────────────────────────────────────────────────────
# classify_one — LAN branch
# ──────────────────────────────────────────────────────────────────────────────


def test_classify_one_lan_client() -> None:
    """A normal LAN IP → lan, host None."""
    kind, host = classify_one("192.168.2.50", "laptop.local", host_lan_ip=_HOST)
    assert kind == "lan"
    assert host is None


def test_classify_one_lan_client_no_name() -> None:
    """LAN IP with empty name → lan (empty name does not trigger loopback)."""
    kind, host = classify_one("192.168.2.200", "", host_lan_ip=_HOST)
    assert kind == "lan"
    assert host is None


# ──────────────────────────────────────────────────────────────────────────────
# classify_one — name normalization
# ──────────────────────────────────────────────────────────────────────────────


def test_classify_one_name_normalization_uppercase() -> None:
    """'PI.HOLE' uppercased → normalized to 'pi.hole' → resolver_self."""
    kind, host = classify_one("127.0.0.1", "PI.HOLE", host_lan_ip=_HOST)
    assert kind == "resolver_self"
    assert host == _HOST


def test_classify_one_name_normalization_whitespace() -> None:
    """'  pi.hole  ' with surrounding whitespace → resolver_self after strip+lower."""
    kind, host = classify_one("127.0.0.1", "  pi.hole  ", host_lan_ip=_HOST)
    assert kind == "resolver_self"
    assert host == _HOST


def test_classify_one_ip_whitespace_stripped() -> None:
    """' 127.0.0.1 ' with surrounding whitespace on IP → loopback detected."""
    kind, host = classify_one(" 127.0.0.1 ", "local", host_lan_ip=_HOST)
    assert kind == "local"
    assert host == _HOST


# ──────────────────────────────────────────────────────────────────────────────
# classify_one — custom resolver_names
# ──────────────────────────────────────────────────────────────────────────────


def test_classify_one_custom_resolver_names() -> None:
    """Custom resolver_names set recognized."""
    kind, host = classify_one(
        "127.0.0.1", "myresolver", host_lan_ip=_HOST, resolver_names=frozenset({"myresolver"})
    )
    assert kind == "resolver_self"
    assert host == _HOST


def test_classify_one_name_not_in_custom_resolver_names() -> None:
    """Name not in custom resolver_names → local."""
    kind, host = classify_one(
        "127.0.0.1", "pi.hole", host_lan_ip=_HOST, resolver_names=frozenset({"myresolver"})
    )
    assert kind == "local"
    assert host == _HOST


# ──────────────────────────────────────────────────────────────────────────────
# classify_clients — basic cases
# ──────────────────────────────────────────────────────────────────────────────


def _make_lan(ip: str, value: float = 1.0) -> RawClient:
    return RawClient(ip=ip, name="device.local", value=value)


def _make_loopback(name: str = "pi.hole", ip: str = "127.0.0.1", value: float = 1.0) -> RawClient:
    return RawClient(ip=ip, name=name, value=value)


def test_classify_clients_empty_input() -> None:
    """Empty input → empty kept, zero dropped."""
    result = classify_clients([], host_lan_ip=_HOST, cap=10)
    assert result.kept == []
    assert result.dropped == 0


def test_classify_clients_loopback_always_kept() -> None:
    """Loopback client is kept regardless of cap (even cap=0)."""
    raw = [_make_loopback("pi.hole")]
    result = classify_clients(raw, host_lan_ip=_HOST, cap=0)
    assert len(result.kept) == 1
    assert result.kept[0].client_kind == "resolver_self"
    assert result.dropped == 0


def test_classify_clients_lan_under_cap_all_survive() -> None:
    """LAN clients under cap → all survive, zero dropped."""
    raw = [_make_lan(f"192.168.2.{i}") for i in range(5)]
    result = classify_clients(raw, host_lan_ip=_HOST, cap=10)
    assert len(result.kept) == 5  # noqa: PLR2004
    assert result.dropped == 0


def test_classify_clients_lan_over_cap_drops_evicted() -> None:
    """LAN clients exceeding cap → exactly cap survivors, rest dropped."""
    raw = [_make_lan(f"192.168.2.{i}", value=float(i)) for i in range(10)]
    result = classify_clients(raw, host_lan_ip=_HOST, cap=3)
    assert len(result.kept) == 3  # noqa: PLR2004
    assert result.dropped == 7  # noqa: PLR2004


def test_classify_clients_loopback_never_evicted_when_lan_exceeds_cap() -> None:
    """Loopback clients are NEVER evicted even when LAN > cap."""
    loopback = _make_loopback()
    lan_clients = [_make_lan(f"192.168.2.{i}") for i in range(20)]
    result = classify_clients([loopback, *lan_clients], host_lan_ip=_HOST, cap=5)
    # 1 loopback + 5 survivors = 6 kept
    assert len(result.kept) == 6  # noqa: PLR2004
    assert result.dropped == 15  # noqa: PLR2004
    kinds = {c.client_kind for c in result.kept}
    assert "resolver_self" in kinds


def test_classify_clients_unattributed_loopback_exempt_from_cap() -> None:
    """Loopback with empty host_lan_ip → unattributed, still kept (cap=0)."""
    raw = [_make_loopback()]
    result = classify_clients(raw, host_lan_ip="", cap=0)
    assert len(result.kept) == 1
    assert result.kept[0].client_kind == "unattributed"
    assert result.kept[0].host_lan_ip is None
    assert result.dropped == 0


def test_classify_clients_host_lan_ip_stamped_on_loopback() -> None:
    """host_lan_ip is stamped on loopback ClassifiedClient."""
    raw = [RawClient(ip="127.0.0.1", name="pi.hole", value=5.0)]
    result = classify_clients(raw, host_lan_ip="10.0.0.1", cap=10)
    assert result.kept[0].host_lan_ip == "10.0.0.1"


def test_classify_clients_lan_host_lan_ip_is_none() -> None:
    """LAN ClassifiedClient always has host_lan_ip=None."""
    raw = [_make_lan("192.168.2.50")]
    result = classify_clients(raw, host_lan_ip=_HOST, cap=10)
    assert result.kept[0].host_lan_ip is None


def test_classify_clients_mac_passed_through() -> None:
    """MAC is carried through unchanged for both loopback and LAN."""
    raw = [
        RawClient(ip="127.0.0.1", name="pi.hole", value=1.0, mac="aa:bb:cc:dd:ee:ff"),
        RawClient(ip="192.168.2.10", name="phone", value=2.0, mac="11:22:33:44:55:66"),
    ]
    result = classify_clients(raw, host_lan_ip=_HOST, cap=10)
    macs = {c.client_mac for c in result.kept}
    assert "aa:bb:cc:dd:ee:ff" in macs
    assert "11:22:33:44:55:66" in macs


def test_classify_clients_mac_absent_is_none() -> None:
    """RawClient without MAC → ClassifiedClient.client_mac is None."""
    raw = [_make_lan("192.168.2.10")]
    result = classify_clients(raw, host_lan_ip=_HOST, cap=10)
    assert result.kept[0].client_mac is None


def test_classify_clients_duplicate_ip_first_wins() -> None:
    """Duplicate LAN IPs: only first ClassifiedClient is kept (first-wins dedup at source)."""
    raw = [
        RawClient(ip="192.168.2.50", name="alpha", value=3.0),
        RawClient(ip="192.168.2.50", name="beta", value=1.0),
    ]
    result = classify_clients(raw, host_lan_ip=_HOST, cap=10)
    # Dedup at source: second occurrence never enters capper, so exactly one
    # client_ip="192.168.2.50" in result, and it is "alpha" (first occurrence wins).
    matches = [c for c in result.kept if c.client_ip == "192.168.2.50"]
    assert len(matches) == 1
    assert matches[0].client_name == "alpha"


def test_classify_clients_deterministic_survivor_selection() -> None:
    """Same input in different orders → same survivors (capper is deterministic)."""
    clients = [_make_lan(f"192.168.2.{i}", value=float(i)) for i in range(10)]
    shuffled = clients[:]
    random.shuffle(shuffled)
    result_a = classify_clients(clients, host_lan_ip=_HOST, cap=3)
    result_b = classify_clients(shuffled, host_lan_ip=_HOST, cap=3)
    ips_a = sorted(c.client_ip for c in result_a.kept)
    ips_b = sorted(c.client_ip for c in result_b.kept)
    assert ips_a == ips_b


def test_classify_clients_duplicate_ip_deterministic() -> None:
    """Duplicate IP in two runs → same deduplicated client_ip, exactly once both times."""
    # Build input with duplicate IP in one position; run twice to verify determinism.
    raw = [
        RawClient(ip="192.168.2.100", name="primary", value=5.0),
        RawClient(ip="192.168.2.100", name="secondary", value=2.0),
        RawClient(ip="192.168.2.101", name="other", value=3.0),
    ]
    result_a = classify_clients(raw, host_lan_ip=_HOST, cap=10)
    result_b = classify_clients(raw, host_lan_ip=_HOST, cap=10)
    # Extract and sort client_ips from both results.
    ips_a = sorted(c.client_ip for c in result_a.kept)
    ips_b = sorted(c.client_ip for c in result_b.kept)
    assert ips_a == ips_b
    # Verify the duplicate IP appears exactly once in both results.
    matches_a = [c for c in result_a.kept if c.client_ip == "192.168.2.100"]
    matches_b = [c for c in result_b.kept if c.client_ip == "192.168.2.100"]
    assert len(matches_a) == 1
    assert len(matches_b) == 1


def test_classify_clients_client_name_preserved_original_case() -> None:
    """client_name in ClassifiedClient is the original name, not lowercased."""
    raw = [RawClient(ip="127.0.0.1", name="PI.HOLE", value=1.0)]
    result = classify_clients(raw, host_lan_ip=_HOST, cap=10)
    assert result.kept[0].client_name == "PI.HOLE"


# ──────────────────────────────────────────────────────────────────────────────
# cap_domains
# ──────────────────────────────────────────────────────────────────────────────


def test_cap_domains_under_cap_all_survive() -> None:
    """Domains under cap → all survive, zero dropped."""
    domains = [("example.com", 100.0), ("google.com", 50.0)]
    result = cap_domains(domains, cap=10)
    assert result.dropped == 0
    assert len(result.survivors) == 2  # noqa: PLR2004


def test_cap_domains_over_cap_evicts() -> None:
    """Domains over cap → exactly cap survivors."""
    domains = [(f"domain{i}.com", float(i)) for i in range(10)]
    result = cap_domains(domains, cap=3)
    assert len(result.survivors) == 3  # noqa: PLR2004
    assert result.dropped == 7  # noqa: PLR2004


def test_cap_domains_empty_input() -> None:
    """Empty domain list → zero seen, zero dropped."""
    result = cap_domains([], cap=5)
    assert result.dropped == 0
    assert result.seen == 0


def test_cap_domains_deterministic() -> None:
    """Same domain list in different orders → same survivors."""
    domains = [(f"site{i}.net", float(i)) for i in range(8)]
    shuffled = domains[:]
    random.shuffle(shuffled)
    result_a = cap_domains(domains, cap=3)
    result_b = cap_domains(shuffled, cap=3)
    survivors_a = sorted(lbl["domain"] for lbl, _ in result_a.survivors)
    survivors_b = sorted(lbl["domain"] for lbl, _ in result_b.survivors)
    assert survivors_a == survivors_b
