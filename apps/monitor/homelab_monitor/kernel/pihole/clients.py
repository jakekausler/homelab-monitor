"""Pi-hole DNS client classification + cardinality cap (STAGE-006-004).

A PURE, synchronous transform over raw Pi-hole client records. No I/O, no async,
no config dependency. Three consumers import this module directly:

- STAGE-006-012 (pihole collector) — calls ``classify_clients`` each tick to build
  the ``homelab_pihole_client_queries`` metric family and ``cap_domains`` for the
  top-domain family.
- STAGE-006-027 (cross-epic view) — calls ``classify_clients`` to attribute query
  counts to host vs. LAN clients in the summary view.
- EPIC-007 (Unifi) — calls ``classify_clients`` to cross-reference Pi-hole client IPs
  with Unifi MAC->hostname inventory.

``resolver_self`` vs ``local`` heuristic is BEST-EFFORT: Pi-hole exposes only the
source IP and an optional reverse-DNS hostname. The ``pi.hole`` / ``localhost``
loopback names arrive because unbound and Pi-hole share the ``pihole-unbound``
container, so the resolver's own queries originate from the loopback interface.
A loopback IP with a name NOT in ``resolver_names`` is classified ``local`` (a
process on the Pi-hole host, not the resolver). This is a heuristic; the module
does NOT attempt TCP connection inspection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from homelab_monitor.kernel.metrics.cardinality import CapResult, CardinalityCapper

# ──────────────────────────────────────────────────────────────────────────────
# Types
# ──────────────────────────────────────────────────────────────────────────────

ClientKind = Literal["local", "resolver_self", "lan", "unattributed"]

#: IPs that Pi-hole reports for the loopback interface.
_LOOPBACK_IPS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "::"})

#: Default resolver-process name heuristic (unbound + Pi-hole internal).
_DEFAULT_RESOLVER_NAMES: Final[frozenset[str]] = frozenset({"pi.hole", "localhost"})


# ──────────────────────────────────────────────────────────────────────────────
# Input / output dataclasses
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RawClient:
    """One Pi-hole client record as received from the API (STAGE-006-012).

    ``ip`` is the client IP string as Pi-hole reports it (no normalization applied
    by the caller; ``classify_one`` strips surrounding whitespace internally).
    ``name`` is the optional reverse-DNS hostname (empty string when Pi-hole has no
    PTR record; the helper treats empty the same as an unrecognized name).
    ``value`` is the query count for this client in the current polling window;
    used as the ordering key when the cardinality cap evicts LAN clients.
    ``mac`` is present only when the caller can supply it (e.g. from a Unifi
    cross-reference); it is carried through unchanged and is never used for
    classification logic.
    """

    ip: str
    name: str
    value: float
    mac: str | None = None


@dataclass(frozen=True, slots=True)
class ClassifiedClient:
    """One Pi-hole client after classification.

    ``client_kind`` is the deterministic classification result. ``host_lan_ip`` is
    non-None only for loopback clients whose ``host_lan_ip`` argument was non-empty;
    for LAN clients and unattributed loopback it is always ``None``. ``client_mac``
    is passed through from ``RawClient.mac`` unchanged.
    """

    client_ip: str
    client_name: str
    client_kind: ClientKind
    host_lan_ip: str | None
    client_mac: str | None


@dataclass(frozen=True, slots=True)
class ClientClassification:
    """Result of ``classify_clients``.

    ``kept`` contains ALL loopback-origin clients (local / resolver_self /
    unattributed) PLUS the surviving LAN clients after the cardinality cap.
    ``dropped`` is the count of LAN clients evicted by the cap (mirrors
    ``CapResult.dropped``).
    """

    kept: list[ClassifiedClient]
    dropped: int


# ──────────────────────────────────────────────────────────────────────────────
# Classification logic
# ──────────────────────────────────────────────────────────────────────────────


def classify_one(
    ip: str,
    name: str,
    *,
    host_lan_ip: str,
    resolver_names: frozenset[str] = _DEFAULT_RESOLVER_NAMES,
) -> tuple[ClientKind, str | None]:
    """Classify a single Pi-hole client IP + name into a ``ClientKind``.

    Returns ``(kind, attributed_host_lan_ip)``.

    Branch tree (in evaluation order):

    1. Normalize: ``ip_s = ip.strip()``, ``name_norm = name.strip().lower()``.
    2. Loopback test: ``ip_s in _LOOPBACK_IPS`` OR ``name_norm == "pi.hole"``.
       - If NOT loopback → return ``("lan", None)``.
    3. Loopback branch — host attribution gate:
       - If ``host_lan_ip`` is falsy (empty string or whitespace-only after strip):
         return ``("unattributed", None)``.  The kind is ALWAYS "unattributed" in
         this case regardless of resolver_names — the caller has not configured the
         host IP so the attribution is unknown.
       - If ``host_lan_ip`` is non-empty:
         - If ``name_norm in resolver_names`` → return ``("resolver_self", host_lan_ip)``.
         - Else → return ``("local", host_lan_ip)``.

    ``ip`` is stripped of surrounding whitespace; callers should pass the raw
    Pi-hole string (Pi-hole itself returns canonical IPs without extra whitespace,
    but defensive stripping costs nothing). ``name`` is stripped and lowercased for
    comparisons only; ``ClassifiedClient.client_name`` stores the original value.

    Args:
        ip: raw client IP string from Pi-hole.
        name: raw reverse-DNS name from Pi-hole (may be empty).
        host_lan_ip: LAN IP of the monitor host; empty string → unattributed.
        resolver_names: set of lowercase hostnames considered the local resolver
            process. Defaults to ``{"pi.hole", "localhost"}``.

    Returns:
        ``(ClientKind, host_lan_ip | None)`` — the host_lan_ip return value is the
        same string passed in when non-empty and a loopback client, else ``None``.
    """
    ip_s = ip.strip()
    name_norm = name.strip().lower()

    is_loopback = ip_s in _LOOPBACK_IPS or name_norm == "pi.hole"
    if not is_loopback:
        return ("lan", None)

    # Loopback client — check host attribution.
    if not host_lan_ip.strip():
        return ("unattributed", None)

    if name_norm in resolver_names:
        return ("resolver_self", host_lan_ip)
    return ("local", host_lan_ip)


def classify_clients(
    raw: list[RawClient],
    *,
    host_lan_ip: str,
    cap: int,
    resolver_names: frozenset[str] = _DEFAULT_RESOLVER_NAMES,
) -> ClientClassification:
    """Classify all raw Pi-hole clients and apply the cardinality cap to LAN clients.

    STAGE-006-012 consumer: call once per tick with the full client list from the
    Pi-hole API response.

    Algorithm:
    1. For each ``RawClient``, call ``classify_one`` to get ``(kind, attributed_ip)``.
    2. Build a ``ClassifiedClient`` for each.
    3. Partition into two lists:
       - ``loopback_clients``: kind in {"local", "resolver_self", "unattributed"}.
         These are STRUCTURALLY EXEMPT from the cap and always appear in ``kept``.
       - ``lan_candidates``: kind == "lan". These are subject to the cap.
    4. Build ``(labels, value)`` tuples for LAN candidates:
       ``labels = {"client_ip": client.client_ip}`` (single-key dict; deterministic
       sort order — a single-key dict sorts identically every call). ``value =`` the
       original ``RawClient.value`` (query count).
    5. Run ``CardinalityCapper(cap).apply(lan_obs)``.
    6. Map survivors back to their ``ClassifiedClient`` via ``client_ip`` lookup.
       Each LAN IP is a single capper candidate (deduped at source, first
       occurrence wins), so the mapping is exactly 1:1. If Pi-hole reports the
       same IP more than once in a response, only the first ``ClassifiedClient``
       is kept and the later duplicates are dropped at ingestion (they never
       enter the cap and are NOT counted in ``dropped``).
    7. Return ``ClientClassification(kept=loopback_clients + lan_survivors,
       dropped=cap_result.dropped)``.

    Args:
        raw: list of raw Pi-hole client records for one polling tick.
        host_lan_ip: LAN IP of the monitor host; empty string → loopback clients
            receive kind "unattributed" with host_lan_ip=None.
        cap: per-tick survivor budget for LAN clients (passed to CardinalityCapper).
        resolver_names: forwarded to ``classify_one``.

    Returns:
        ``ClientClassification`` with kept clients (loopback-exempt + capped LAN)
        and dropped count.
    """
    loopback_clients: list[ClassifiedClient] = []
    # Index for mapping capper survivors back to ClassifiedClient objects.
    # Key: client_ip. First-wins on duplicate IPs (see docstring).
    lan_index: dict[str, ClassifiedClient] = {}
    # List for capper input (labels, value) tuples.
    lan_obs: list[tuple[dict[str, str], float]] = []

    for rc in raw:
        kind, attributed = classify_one(
            rc.ip,
            rc.name,
            host_lan_ip=host_lan_ip,
            resolver_names=resolver_names,
        )
        cc = ClassifiedClient(
            client_ip=rc.ip.strip(),
            client_name=rc.name,
            client_kind=kind,
            host_lan_ip=attributed,
            client_mac=rc.mac,
        )
        if kind == "lan":
            # Dedup at source: each client_ip becomes exactly one capper
            # candidate (first occurrence wins). This makes the survivor mapping
            # 1:1 and prevents a duplicate ClassifiedClient appearing in `kept`
            # when Pi-hole reports the same IP twice in one response.
            if cc.client_ip not in lan_index:
                lan_index[cc.client_ip] = cc
                lan_obs.append(({"client_ip": cc.client_ip}, rc.value))
        else:
            loopback_clients.append(cc)

    cap_result: CapResult = CardinalityCapper(cap).apply(lan_obs)
    lan_survivors: list[ClassifiedClient] = []
    for labels, _value in cap_result.survivors:
        survivor_ip = labels["client_ip"]
        lan_survivors.append(lan_index[survivor_ip])

    return ClientClassification(
        kept=loopback_clients + lan_survivors,
        dropped=cap_result.dropped,
    )


def cap_domains(domains: list[tuple[str, float]], cap: int) -> CapResult:
    """Apply the cardinality cap to a list of (domain, query_count) pairs.

    No exemption logic — all domains are subject to the cap equally. The capper
    sorts by ``{"domain": d}`` labels deterministically.

    STAGE-006-012 consumer: call once per tick with the top-domain list from the
    Pi-hole API response.

    Args:
        domains: ``(domain_name, query_count)`` pairs for the current tick.
        cap: per-tick survivor budget.

    Returns:
        ``CapResult`` from ``CardinalityCapper(cap).apply(...)``.
    """
    observations: list[tuple[dict[str, str], float]] = [
        ({"domain": domain}, value) for domain, value in domains
    ]
    return CardinalityCapper(cap).apply(observations)


__all__ = [
    "ClassifiedClient",
    "ClientClassification",
    "ClientKind",
    "RawClient",
    "cap_domains",
    "classify_clients",
    "classify_one",
]
