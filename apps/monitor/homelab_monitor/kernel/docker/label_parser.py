"""Parse `homelab-monitor.*` Docker labels into probe descriptors.

D-LABEL-NAMESPACE: `homelab-monitor.<kind>.<name>=<value>`.
  - kind ∈ {http, tcp, exec, metrics}.
  - <name> defaults to 'default' when missing (i.e., `homelab-monitor.<kind>=value`).
D-EXEC-PER-CONTAINER-LABEL: `homelab-monitor.exec_authorized=true` is a
  parser carve-out — recognized as a flag, NOT parsed into a ProbeDescriptor.
D-COLLISION-NO-PROBE: two labels resolving to the same (kind, name) tuple
  produce a LabelCollision and BOTH are removed from the descriptors list.
D-TCP-LABEL-URL-DSL: tcp values use `tcp://host:port`, `tcp://container:port`,
  or `tcp://<ip>:port`. Sentinels `host` and `container` are kept as-is for
  resolver-stage substitution.

The exec descriptor is emitted unconditionally — the global flag +
exec_authorized gating happens in probe_resolver, NOT here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlparse

_PREFIX: Final[str] = "homelab-monitor."
_EXEC_AUTHORIZED_KEY: Final[str] = "homelab-monitor.exec_authorized"
_VALID_KINDS: Final[frozenset[str]] = frozenset({"http", "tcp", "exec", "metrics"})
_DEFAULT_NAME: Final[str] = "default"
_SPLIT_MAXSPLIT: Final[int] = 1

ProbeKind = Literal["http", "tcp", "exec", "metrics"]


@dataclass(frozen=True, slots=True)
class ProbeDescriptor:
    kind: str  # one of _VALID_KINDS
    name: str
    raw_value: str


@dataclass(frozen=True, slots=True)
class LabelCollision:
    kind: str
    name: str
    conflicting_values: tuple[str, ...]  # raw values from the colliding labels


@dataclass(frozen=True, slots=True)
class MalformedLabel:
    label_key: str
    label_value: str
    reason: str  # 'unknown_kind' | 'invalid_http_url' | 'invalid_tcp_url' |
    # 'tcp_port_missing' | 'tcp_host_required' | 'empty_value' |
    # 'invalid_metrics_url'


@dataclass(frozen=True, slots=True)
class ParseResult:
    descriptors: tuple[ProbeDescriptor, ...] = ()
    collisions: tuple[LabelCollision, ...] = ()
    malformed: tuple[MalformedLabel, ...] = ()
    exec_authorized: bool = False


def parse_homelab_labels(labels: dict[str, str]) -> ParseResult:
    """Parse a container's labels into a ParseResult.

    Steps:
      1. Filter to keys starting with 'homelab-monitor.'.
      2. Carve out the exec_authorized flag.
      3. For each remaining key, split into (kind, name); validate kind.
      4. For each (kind, value), validate value shape; mark malformed if not.
      5. Detect collisions on (kind, name); remove both colliding descriptors
         and emit a LabelCollision for each collision (kind, name) pair.

    Returns a ParseResult with frozen tuples (deterministic order).
    """
    homelab: dict[str, str] = {k: v for k, v in labels.items() if k.startswith(_PREFIX)}
    exec_authorized = False
    if _EXEC_AUTHORIZED_KEY in homelab:
        val = homelab.pop(_EXEC_AUTHORIZED_KEY).strip().lower()
        exec_authorized = val == "true"

    # Bucket: (kind, name) -> list of (label_key, raw_value)
    buckets: dict[tuple[str, str], list[tuple[str, str]]] = {}
    malformed: list[MalformedLabel] = []

    # Sort keys for deterministic output
    for key in sorted(homelab.keys()):
        raw_value = homelab[key]
        suffix = key[len(_PREFIX) :]  # e.g. "http.api" or "http"
        parts = suffix.split(".", _SPLIT_MAXSPLIT)
        kind = parts[0]
        name = parts[1] if len(parts) == _SPLIT_MAXSPLIT + 1 else _DEFAULT_NAME

        if kind not in _VALID_KINDS:
            malformed.append(
                MalformedLabel(label_key=key, label_value=raw_value, reason="unknown_kind")
            )
            continue

        if not raw_value.strip():
            malformed.append(
                MalformedLabel(label_key=key, label_value=raw_value, reason="empty_value")
            )
            continue

        reason = _validate_value(kind, raw_value)
        if reason is not None:
            malformed.append(MalformedLabel(label_key=key, label_value=raw_value, reason=reason))
            continue

        buckets.setdefault((kind, name), []).append((key, raw_value))

    descriptors: list[ProbeDescriptor] = []
    collisions: list[LabelCollision] = []
    for (kind, name), entries in buckets.items():
        if len(entries) > 1:
            collisions.append(
                LabelCollision(
                    kind=kind,
                    name=name,
                    conflicting_values=tuple(sorted(v for _, v in entries)),
                )
            )
            continue
        _, raw_value = entries[0]
        descriptors.append(ProbeDescriptor(kind=kind, name=name, raw_value=raw_value))

    # Deterministic order: by (kind, name) for descriptors and collisions
    descriptors.sort(key=lambda d: (d.kind, d.name))
    collisions.sort(key=lambda c: (c.kind, c.name))
    malformed.sort(key=lambda m: m.label_key)

    return ParseResult(
        descriptors=tuple(descriptors),
        collisions=tuple(collisions),
        malformed=tuple(malformed),
        exec_authorized=exec_authorized,
    )


def _validate_value(kind: str, raw_value: str) -> str | None:  # noqa: PLR0911
    """Return None if value shape is OK, else reason code."""
    if kind in ("http", "metrics"):
        if not (raw_value.startswith("http://") or raw_value.startswith("https://")):
            return "invalid_http_url" if kind == "http" else "invalid_metrics_url"
        try:
            parsed = urlparse(raw_value)
        except ValueError:  # pragma: no cover -- urlparse rarely raises ValueError
            return "invalid_http_url" if kind == "http" else "invalid_metrics_url"
        if not parsed.netloc:
            return "invalid_http_url" if kind == "http" else "invalid_metrics_url"
        return None
    if kind == "tcp":
        if not raw_value.startswith("tcp://"):
            return "invalid_tcp_url"
        try:
            parsed = urlparse(raw_value)
        except ValueError:  # pragma: no cover -- urlparse rarely raises ValueError
            return "invalid_tcp_url"
        if not parsed.hostname:
            return "tcp_host_required"
        if parsed.port is None:
            return "tcp_port_missing"
        return None
    if kind == "exec":
        # Any non-empty string is valid at parse stage; gating happens later.
        return None
    return None  # pragma: no cover -- defensive; kind already filtered to _VALID_KINDS


__all__ = [
    "LabelCollision",
    "MalformedLabel",
    "ParseResult",
    "ProbeDescriptor",
    "ProbeKind",
    "parse_homelab_labels",
]
