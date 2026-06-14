"""Live-HA enrichment helpers for the HA detail endpoints (STAGE-005-031).

VM SELECTS the rows (LOCKED D-DETAIL-CONSUMES-VM); these helpers only JOIN the
VM-selected rows to a live-HA snapshot to fill display-only fields. The fragile
untyped-``attributes`` coercion lives in ONE tested place (``attr_str``).

SECURITY / PRIVACY (D-ENRICH-PRIVACY): enriched values are display-only. This
module NEVER logs, NEVER emits metrics, and NEVER raises with enriched content.
HA failures are handled by the CALLER (it checks ``isinstance(result, HaError)``
and skips index-building); these helpers operate only on successful snapshots.

``extract_issues`` is a kernel-local copy of the collector's ``_extract_issues``
(kernel MUST NOT import from ``plugins``); it mirrors
``kernel/ha/notifications.py::extract_notifications``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from homelab_monitor.kernel.ha.client import HaState


@dataclass(frozen=True, slots=True)
class RepairEnrichment:
    """The live-HA enrichment for one repair issue (STAGE-005-031).

    Both fields are display-only and degrade to ``None`` independently when HA
    omits the corresponding key (or supplies a non-``str`` / empty value).
    """

    description: str | None
    learn_more_url: str | None


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    """One HA entity-registry entry, reduced to the fields the cache needs.

    All three classification fields degrade INDEPENDENTLY to ``None`` when HA
    omits the key or supplies a non-``str`` value (mirrors ``attr_str``). The
    cache NEVER logs ``entity_id`` and NEVER emits it as a metric label.
    """

    entity_id: str
    disabled_by: str | None
    hidden_by: str | None
    entity_category: str | None


def build_states_index(states: list[HaState]) -> dict[str, HaState]:
    """Index HA states by ``entity_id``.

    Later entries win on a duplicate ``entity_id`` (last-write). States with an
    empty ``entity_id`` are skipped (``_parse_state`` defaults a missing id to "").
    """
    index: dict[str, HaState] = {}
    for state in states:
        if state.entity_id:
            index[state.entity_id] = state
    return index


def attr_str(state: HaState | None, key: str) -> str | None:
    """Return ``state.attributes[key]`` only when it is a ``str``; else ``None``.

    ``None`` for: no state (row absent from the snapshot), missing attribute, or
    a non-``str`` attribute value. This is the ONLY place the untyped
    ``attributes: dict[str, object]`` is read — never ``str()``-coerce arbitrary
    objects (that would leak repr-like text for dicts/lists).
    """
    if state is None:
        return None
    value = state.attributes.get(key)
    if isinstance(value, str):
        return value
    return None


def extract_issues(result: dict[str, object] | list[object]) -> list[object]:
    """Defensively extract the issues list from a ``send_command`` result.

    Kernel-local copy of the ha_repairs collector's ``_extract_issues`` (kernel
    MUST NOT import from ``plugins``). Mirrors ``extract_notifications``.

    Handles:
    (a) bare list — return as-is.
    (b) dict wrapping the list under ``issues`` — return that list.
    (c) any other dict (e.g. the ``{}`` degenerate) — return [].
    """
    payload: object = result  # widen: runtime value may be a list.
    if isinstance(payload, list):
        return payload
    issues_dict = payload
    candidate = issues_dict.get("issues")
    if isinstance(candidate, list):
        return cast("list[object]", candidate)
    return []


def build_repairs_index(
    issues: list[object],
) -> dict[tuple[str, str], RepairEnrichment]:
    """Index repair issues by ``(domain, issue_id)`` -> ``RepairEnrichment``.

    Skips non-dict entries and entries missing a non-empty ``domain`` or
    ``issue_id``. Each ``RepairEnrichment`` carries the issue's ``description``
    and ``learn_more_url`` — each is the issue's value for that key when it is a
    non-empty ``str``, else ``None`` (stock HA may omit either; they then render
    as null, which is the intended graceful behavior). The two fields degrade
    INDEPENDENTLY.
    """
    index: dict[tuple[str, str], RepairEnrichment] = {}
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        issue_dict = cast("dict[str, object]", issue)
        domain_obj = issue_dict.get("domain")
        domain = domain_obj if isinstance(domain_obj, str) else ""
        if not domain:
            continue
        issue_id_obj = issue_dict.get("issue_id")
        issue_id = issue_id_obj if isinstance(issue_id_obj, str) else ""
        if not issue_id:
            continue
        description_obj = issue_dict.get("description")
        description = (
            description_obj if isinstance(description_obj, str) and description_obj else None
        )
        learn_more_url_obj = issue_dict.get("learn_more_url")
        learn_more_url = (
            learn_more_url_obj
            if isinstance(learn_more_url_obj, str) and learn_more_url_obj
            else None
        )
        index[(domain, issue_id)] = RepairEnrichment(
            description=description,
            learn_more_url=learn_more_url,
        )
    return index


def extract_registry(result: dict[str, object] | list[object]) -> list[object]:
    """Defensively extract the entity-registry list from a ``send_command`` result.

    HA's ``config/entity_registry/list`` returns a BARE list of entry dicts, but
    this mirrors ``extract_issues``' defensiveness:
    (a) bare list — return as-is.
    (b) dict wrapping the list under ``entities`` — return that list.
    (c) any other dict (e.g. the ``{}`` degenerate) — return [].
    """
    payload: object = result  # widen: runtime value may be a list.
    if isinstance(payload, list):
        return payload
    candidate = payload.get("entities")
    if isinstance(candidate, list):
        return cast("list[object]", candidate)
    return []


def _entry_str(entry: dict[str, object], key: str) -> str | None:
    """Return ``entry[key]`` only when it is a ``str``; else ``None``.

    Mirrors ``attr_str`` for the untyped registry-entry dict: missing key or a
    non-``str`` value (None, bool, dict, list, int) -> ``None``. Never coerces.
    """
    value = entry.get(key)
    if isinstance(value, str):
        return value
    return None


def build_registry_index(entries: list[object]) -> dict[str, RegistryEntry]:
    """Index entity-registry entries by ``entity_id`` -> ``RegistryEntry``.

    Skips non-dict entries and entries missing a non-empty ``entity_id`` (str).
    ``disabled_by`` / ``hidden_by`` / ``entity_category`` are each read via the
    isinstance-guarded ``_entry_str`` (non-str or missing -> ``None``). Later
    entries win on a duplicate ``entity_id`` (last-write).
    """
    index: dict[str, RegistryEntry] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_dict = cast("dict[str, object]", entry)
        entity_id = _entry_str(entry_dict, "entity_id")
        if not entity_id:
            continue
        index[entity_id] = RegistryEntry(
            entity_id=entity_id,
            disabled_by=_entry_str(entry_dict, "disabled_by"),
            hidden_by=_entry_str(entry_dict, "hidden_by"),
            entity_category=_entry_str(entry_dict, "entity_category"),
        )
    return index
