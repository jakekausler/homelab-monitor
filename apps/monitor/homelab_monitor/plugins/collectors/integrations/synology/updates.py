"""synology_updates collector — DSM update availability + installed-package versions.

EPIC-008 STAGE-008-012. Fetches THREE CO-EQUAL DSM APIs once per 1-hour tick:
  - SYNO.Core.Upgrade.Server/check  -> {"update": {...}}  (DSM update availability)
  - SYNO.Core.Package/list          -> {"packages": [...]} (installed packages)
  - SYNO.Core.Package.Server/list   -> {"packages": [...], "beta_packages": [...]}
                                       (server catalogue; the per-package version join)

CO-EQUAL COMBINE (mirrors STAGE-008-010 backup.py / STAGE-008-011 replication.py):
there is NO primary. ``_fetch`` records-and-continues on ANY fetch's client error;
the run is ok=False ONLY when ALL THREE fetches fail
(``ok = upgrade_resp is not None or pkg_resp is not None or server_resp is not None``).
A partial failure is a DEGRADED ok=True run. ``_emit`` ALWAYS runs.

ALWAYS-EMIT 0-BASELINE (Wave-B contract): ``dsm_update_available``,
``dsm_update_is_security``, ``package_count`` and ``packages_with_updates_count`` are
the alertable scalars and ALWAYS emit, even when their source fetch failed — their
DEFAULTS (0.0) are seeded into ``_Built`` BEFORE any parse so a failed fetch still
emits the 0-baseline.

PER-PACKAGE UPDATE JOIN: build ``server_versions: dict[id -> version]`` from the
NON-beta server catalogue (beta entries skipped), then for each installed package
emit ``package_update_available{package}`` = 1.0 iff the server version differs from
the installed version. A package absent from the server map -> 0.0. If the server
fetch failed the map is empty -> every package -> 0.0 (a valid degraded branch).

FIELD KEYS ARE LIVE-VERIFIED (captured JSON), centralized in ``_UPGRADE_FIELDS`` /
``_PKG_FIELDS`` / ``_SERVER_FIELDS``. The tolerant helpers degrade a wrong/absent
field to None -> that label/series is dropped.

CARDINALITY: every family is cap-routed through ``capped_emitter`` +
``cap_for_synology`` (default 500). ``metrics_emitted`` = sum of
``emit_family() + 1`` per family + the api_took gauges from each successful fetch.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar, Final

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorEvent, CollectorResult
from homelab_monitor.kernel.synology.client import SynologyResponse
from homelab_monitor.kernel.synology.errors import SynologyError
from homelab_monitor.plugins.collectors.integrations.synology._shared import (
    as_dict,
    as_list_of_dicts,
    bool_to_gauge,
    cap_for_synology,
    capped_emitter,
    client_unconfigured_result,
    fetch_or_result,
    nested,
)

# --- Metric family names
# Labels (kept out of inline comments to stay <=100 cols):
#   dsm_update_available     : no labels  bool_to_gauge(update.available), None->0.0 (always)
#   dsm_update_is_security   : no labels  bool_to_gauge(isSecurityVersion), None->0.0 (always)
#   dsm_update_info          : {available_version, type} = 1.0  (only when available & version)
#   package_info             : {package, name, version} = 1.0  per installed package
#   package_count            : no labels  len(installed packages) (always; default 0)
#   package_update_available : {package}  1.0 iff server version != installed (always per pkg)
#   packages_with_updates_count : no labels  count of pkgs with update (always; default 0)
M_DSM_UPDATE_AVAILABLE: Final[str] = "homelab_synology_dsm_update_available"
M_DSM_UPDATE_IS_SECURITY: Final[str] = "homelab_synology_dsm_update_is_security"
M_DSM_UPDATE_INFO: Final[str] = "homelab_synology_dsm_update_info"
M_PACKAGE_INFO: Final[str] = "homelab_synology_package_info"
M_PACKAGE_COUNT: Final[str] = "homelab_synology_package_count"
M_PACKAGE_UPDATE_AVAILABLE: Final[str] = "homelab_synology_package_update_available"
M_PACKAGES_WITH_UPDATES_COUNT: Final[str] = "homelab_synology_packages_with_updates_count"

# --- Live-VERIFIED DSM field keys (captured JSON). logical name -> DSM key.
_UPGRADE_FIELDS: Final[dict[str, str]] = {
    "available": "available",
    "version": "version",
    "type": "type",
}

# Nested path to the DSM security flag inside the update record.
_SEC_PATH: Final[tuple[str, str]] = ("version_details", "isSecurityVersion")

_PKG_FIELDS: Final[dict[str, str]] = {
    "id": "id",
    "name": "name",
    "version": "version",
}

_SERVER_FIELDS: Final[dict[str, str]] = {
    "id": "id",
    "version": "version",
    "is_security_version": "is_security_version",
    "beta": "beta",
}


# ---------------------------------------------------------------------------
# Multi-fetch wrapper: record-and-continue for INDEPENDENT fetches
# (copied verbatim from STAGE-008-010 backup.py / STAGE-008-011 replication.py)
# ---------------------------------------------------------------------------


def _fetch(
    ctx: CollectorContext,
    response: SynologyResponse | SynologyError,
    start: float,
    emitted: list[int],
    errors: list[str],
) -> SynologyResponse | None:
    """Wrap fetch_or_result for INDEPENDENT (non-early-returning) fetches.

    On a client error fetch_or_result returns a CollectorResult (errors
    populated); we record those error strings into ``errors`` and return None
    instead of aborting. On success it has already emitted api_took + bumped
    emitted[0]; we return the SynologyResponse.
    """
    r = fetch_or_result(ctx, response, start, emitted)
    if isinstance(r, CollectorResult):
        errors.extend(r.errors)
        return None
    return r


# ---------------------------------------------------------------------------
# Per-tick observation accumulator
# ---------------------------------------------------------------------------


class _Built:
    """Per-tick observation lists, one per cap-routed metric family.

    The four ALWAYS-EMIT scalar families are seeded with their 0-baseline default
    in __init__ so a failed/absent fetch still emits the alertable 0. The parse
    passes OVERWRITE those single-element lists when their fetch succeeds.
    """

    __slots__ = (
        "dsm_update_available_obs",
        "dsm_update_info_obs",
        "dsm_update_is_security_obs",
        "package_count_obs",
        "package_info_obs",
        "package_update_available_obs",
        "packages_with_updates_count_obs",
    )

    def __init__(self) -> None:
        """Initialise lists; seed the always-emit scalars with their 0-baseline."""
        self.dsm_update_available_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]
        self.dsm_update_is_security_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]
        self.dsm_update_info_obs: list[tuple[dict[str, str], float]] = []
        self.package_info_obs: list[tuple[dict[str, str], float]] = []
        self.package_count_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]
        self.package_update_available_obs: list[tuple[dict[str, str], float]] = []
        self.packages_with_updates_count_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]


# ---------------------------------------------------------------------------
# Parse passes
# ---------------------------------------------------------------------------


def _parse_upgrade(built: _Built, upgrade_payload: dict[str, object]) -> None:
    """Parse the {"update": {...}} slice into the DSM-update families.

    OVERWRITES the seeded available / is_security scalars. Emits dsm_update_info
    only when update is available AND a non-empty version string is present.
    A missing/non-dict ``update`` leaves the seeded 0-baseline scalars and no info.
    """
    update = as_dict(nested(upgrade_payload, "update"))
    if update is None:
        return

    available = bool_to_gauge(update.get(_UPGRADE_FIELDS["available"]))
    available_val = available if available is not None else 0.0
    built.dsm_update_available_obs = [({}, available_val)]

    is_security = bool_to_gauge(nested(update, *_SEC_PATH))
    is_security_val = is_security if is_security is not None else 0.0
    built.dsm_update_is_security_obs = [({}, is_security_val)]

    # available_val is bool_to_gauge output or the 0.0 default — only ever 0.0 or 1.0
    if available_val != 1.0:
        return
    version = update.get(_UPGRADE_FIELDS["version"])
    if not (isinstance(version, str) and version.strip()):
        return
    info_labels = {"available_version": version.strip()}
    type_val = update.get(_UPGRADE_FIELDS["type"])
    if isinstance(type_val, str) and type_val.strip():
        info_labels["type"] = type_val.strip()
    built.dsm_update_info_obs.append((info_labels, 1.0))


def _pkg_id(record: dict[str, object]) -> str | None:
    """Return the usable {package} key for a record, or None to skip its series."""
    raw = record.get(_PKG_FIELDS["id"])
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _server_versions(server_payload: dict[str, object]) -> dict[str, str]:
    """Build the NON-beta {id -> version} catalogue used by the per-package join.

    Iterates the ``packages`` list ONLY (NOT ``beta_packages``). Skips any entry
    flagged beta (``beta`` truthy) and any entry without a usable id+version.
    """
    versions: dict[str, str] = {}
    for entry in as_list_of_dicts(nested(server_payload, "packages")):
        if bool_to_gauge(entry.get(_SERVER_FIELDS["beta"])) == 1.0:
            continue
        ident = entry.get(_SERVER_FIELDS["id"])
        version = entry.get(_SERVER_FIELDS["version"])
        if not (isinstance(ident, str) and ident.strip()):
            continue
        if not (isinstance(version, str) and version.strip()):
            continue
        versions[ident.strip()] = version.strip()
    return versions


def _parse_packages(
    built: _Built, pkg_payload: dict[str, object], server_versions: dict[str, str]
) -> None:
    """Parse installed packages: info series, count, and the per-package update join.

    OVERWRITES the seeded package_count / packages_with_updates_count scalars.
    ``package_count`` counts ALL installed records (before the id-skip — a malformed
    record is still an installed package). For each record WITH a usable id, emits
    ``package_info`` (+ optional name/version labels) and ``package_update_available``
    (1.0 iff the server version differs from the installed version).
    """
    packages = as_list_of_dicts(nested(pkg_payload, "packages"))
    built.package_count_obs = [({}, float(len(packages)))]

    with_updates = 0
    for record in packages:
        ident = _pkg_id(record)
        if ident is None:
            continue

        info_labels = {"package": ident}
        name = record.get(_PKG_FIELDS["name"])
        if isinstance(name, str) and name.strip():
            info_labels["name"] = name.strip()
        installed_version = record.get(_PKG_FIELDS["version"])
        if isinstance(installed_version, str) and installed_version.strip():
            info_labels["version"] = installed_version.strip()
        built.package_info_obs.append((info_labels, 1.0))

        update = 0.0
        server_version = server_versions.get(ident)
        if (
            server_version is not None
            and isinstance(installed_version, str)
            and installed_version.strip()
            and server_version != installed_version.strip()
        ):
            update = 1.0
            with_updates += 1
        built.package_update_available_obs.append(({"package": ident}, update))

    built.packages_with_updates_count_obs = [({}, float(with_updates))]


def _emit(
    ctx: CollectorContext, built: _Built, events: list[CollectorEvent], emitted: list[int]
) -> None:
    """Cap-route every family through one CappedEmitter."""
    emitter = capped_emitter(ctx, events)

    def family(name: str, obs: list[tuple[dict[str, str], float]]) -> None:
        emitted[0] += emitter.emit_family(name, cap_for_synology(name), obs) + 1

    family(M_DSM_UPDATE_AVAILABLE, built.dsm_update_available_obs)
    family(M_DSM_UPDATE_IS_SECURITY, built.dsm_update_is_security_obs)
    family(M_DSM_UPDATE_INFO, built.dsm_update_info_obs)
    family(M_PACKAGE_INFO, built.package_info_obs)
    family(M_PACKAGE_COUNT, built.package_count_obs)
    family(M_PACKAGE_UPDATE_AVAILABLE, built.package_update_available_obs)
    family(M_PACKAGES_WITH_UPDATES_COUNT, built.packages_with_updates_count_obs)


class SynologyUpdatesCollector(BaseCollector):
    """Emit DSM update availability + installed-package versions/updates from 3 DSM APIs.

    Polls once per 1-hour tick in the ``synology`` concurrency group. No fetch is
    primary: a single fetch failing records its error but keeps ok=True with the
    other families still emitted; ok=False ONLY when ALL THREE fetches fail. An
    unconfigured client is ok=False. The dsm_update_available / dsm_update_is_security
    / package_count / packages_with_updates_count scalars ALWAYS emit at their
    0-baseline (the alertable Wave-B contract).
    """

    name: ClassVar[str] = "synology_updates"
    interval: ClassVar[timedelta] = timedelta(seconds=3600)
    timeout: ClassVar[timedelta] = timedelta(seconds=30)
    concurrency_group: ClassVar[str] = "synology"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Fetch upgrade + package + server lists co-equally, parse, emit families."""
        start = time.monotonic()
        if ctx.synology is None:
            return client_unconfigured_result(start)

        emitted: list[int] = [0]
        errors: list[str] = []
        events: list[CollectorEvent] = []
        built = _Built()

        # CO-EQUAL fetch 1: DSM upgrade check.
        upgrade_resp = _fetch(ctx, await ctx.synology.upgrade_check(), start, emitted, errors)
        if upgrade_resp is not None:
            upgrade_payload = as_dict(upgrade_resp.payload)
            if upgrade_payload is not None:
                _parse_upgrade(built, upgrade_payload)

        # CO-EQUAL fetch 2: package-server catalogue (the join source — parse FIRST).
        server_resp = _fetch(ctx, await ctx.synology.package_server_list(), start, emitted, errors)
        server_versions: dict[str, str] = {}
        if server_resp is not None:
            server_payload = as_dict(server_resp.payload)
            if server_payload is not None:
                server_versions = _server_versions(server_payload)

        # CO-EQUAL fetch 3: installed package list (joins against server_versions).
        pkg_resp = _fetch(ctx, await ctx.synology.package_list(), start, emitted, errors)
        if pkg_resp is not None:
            pkg_payload = as_dict(pkg_resp.payload)
            if pkg_payload is not None:
                _parse_packages(built, pkg_payload, server_versions)

        # ALWAYS emit (even on a fully-failed run: empty families emit drop gauge only,
        # the seeded 0-baseline scalars still emit their single series).
        _emit(ctx, built, events, emitted)

        # CO-EQUAL: ok=False ONLY when ALL THREE fetches failed.
        ok = upgrade_resp is not None or pkg_resp is not None or server_resp is not None
        return CollectorResult(
            ok=ok,
            metrics_emitted=emitted[0],
            errors=errors,
            events=events,
            duration_seconds=time.monotonic() - start,
        )
