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
    lazily in :meth:`as_mapping` to keep ``__init__`` minimal — frozen
    dataclasses can't easily wrap-on-construct without ``__post_init__``.
    """

    _values: dict[str, str] = field(default_factory=dict)  # type: ignore[var-annotated]

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
