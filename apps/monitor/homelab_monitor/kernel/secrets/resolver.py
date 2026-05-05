"""Frozen, IPC-serializable plaintext snapshot for sync callers.

Built by :class:`AsyncSecretsRepository.snapshot`. Designed for STAGE-001-009
subprocess plugins: pickle-safe, no DB references, no engine handles, no
mutation. Filtering by declared names supports the per-plugin manifest model.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass(frozen=True)
class SyncSecretsResolver:
    """Read-only view over a precomputed plaintext snapshot.

    Construction is intentionally simple — pass a ``dict[str, str]`` of
    resolved name→plaintext pairs. The dataclass freezes the wrapping shell;
    callers should treat the source dict as owned by the resolver.

    Pickle-safe (only primitive fields). MappingProxyType is constructed
    in ``__post_init__`` to prevent accidental mutation of the backing dict.
    """

    _values: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        """Wrap _values in MappingProxyType so consumers cannot mutate the source dict."""
        if not isinstance(self._values, MappingProxyType):
            object.__setattr__(self, "_values", MappingProxyType(dict(self._values)))

    def __reduce__(self) -> tuple[type[SyncSecretsResolver], tuple[dict[str, str]]]:
        """Pickle support: serialize _values as a plain dict, rebuild on unpickle.

        ``MappingProxyType`` is not directly picklable in CPython, so we round-trip
        through ``dict``. ``__post_init__`` re-wraps the dict on the receiving side,
        preserving immutability across the IPC boundary.
        """
        return (SyncSecretsResolver, (dict(self._values),))

    def get(self, name: str) -> str | None:
        """Return the plaintext value for ``name`` or ``None`` if absent."""
        return self._values.get(name)

    def list_names(self) -> list[str]:
        """Return all names in the snapshot, sorted ascending."""
        return sorted(self._values.keys())

    def as_mapping(self) -> Mapping[str, str]:
        """Return a read-only mapping view over the snapshot."""
        return MappingProxyType(self._values)

    def filtered(self, declared_names: list[str]) -> SyncSecretsResolver:
        """Return a new resolver containing only the names in ``declared_names``.

        Used at plugin boundary: a plugin manifest declares which secrets it
        needs, and the kernel hands it a resolver scoped to just those.
        Names absent from the snapshot are silently omitted (the plugin will
        see ``None`` from :meth:`get`).
        """
        subset = {n: self._values[n] for n in declared_names if n in self._values}
        return SyncSecretsResolver(_values=subset)

    def as_dict(self) -> dict[str, str]:
        """Return a dict copy of all secrets in this resolver.

        Used by subprocess plugins (STAGE-001-009) to serialize secrets into
        the stdin JSON payload. The returned dict is a copy; mutations do not
        affect this resolver.
        """
        return dict(self._values)
