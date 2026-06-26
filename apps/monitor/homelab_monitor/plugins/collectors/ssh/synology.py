"""Combined Synology SSH probe (STAGE-008-014, EPIC-008).

Parses the live `===HM_*===` forced-command output from the Synology NAS into a curated,
bounded metric set: a SMART attribute whitelist + per-disk rollups, mdstat array health,
hwmon CPU temperatures, and liveness (uptime + load averages). The forced-command script is
already deployed on the NAS; this module is pure parsing.

This probe consolidates liveness for the synology target — the generic `uptime-synology`
probe is removed in favor of this combined probe (see register_all).

UPS metrics are intentionally NOT emitted here (DSM-API ups.py is the sole UPS source); the
upsc section stays captured in the NAS script for future UPS models.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import ClassVar, Final

from homelab_monitor.kernel.ssh.probe import ProbeMetric, ProbeOutcome, SshProbe
from homelab_monitor.kernel.ssh.result import SshCommandResult

# ============================================================================
# Metric-name constants (M_*) and whitelist
# ============================================================================

M_SMART_ATTR_RAW: Final[str] = "homelab_synology_smart_attr_raw"
M_SMART_ATTR_WORST: Final[str] = "homelab_synology_smart_attr_worst"
M_SMART_ATTR_THRESHOLD: Final[str] = "homelab_synology_smart_attr_threshold"
M_SMART_ATTR_FAILING: Final[str] = "homelab_synology_smart_attr_failing"
M_SMART_DISK_PRESENT: Final[str] = "homelab_synology_smart_disk_present"
M_MDSTAT_ARRAY_DEGRADED: Final[str] = "homelab_synology_mdstat_array_degraded"
M_MDSTAT_RESYNC_PROGRESS: Final[str] = "homelab_synology_mdstat_resync_progress_percent"
M_MDSTAT_RESYNC_SPEED: Final[str] = "homelab_synology_mdstat_resync_speed_kb_per_sec"
M_MDSTAT_DISKS_ACTIVE: Final[str] = "homelab_synology_mdstat_disks_active"
M_MDSTAT_DISKS_TOTAL: Final[str] = "homelab_synology_mdstat_disks_total"
M_CPU_TEMP: Final[str] = "homelab_synology_ssh_cpu_temp_celsius"
M_CPU_CORE_TEMP: Final[str] = "homelab_synology_ssh_cpu_core_temp_celsius"
M_UPTIME_SECONDS: Final[str] = "homelab_synology_ssh_uptime_seconds"
M_LOAD1: Final[str] = "homelab_synology_ssh_load1"
M_LOAD5: Final[str] = "homelab_synology_ssh_load5"
M_LOAD15: Final[str] = "homelab_synology_ssh_load15"

_WHITELIST_IDS: Final[frozenset[int]] = frozenset({5, 9, 194, 196, 197, 198, 199})
_TEMP_PRIMARY_ID: Final[int] = 194
_TEMP_FALLBACK_ID: Final[int] = 190
_HEALTHY_STATUS: Final[str] = "OK"

# ============================================================================
# Regex patterns (module-level compiled patterns)
# ============================================================================

_MARKER_RE: Final[re.Pattern[str]] = re.compile(r"^===(HM_[^=]*?)===\s*$")
_UPTIME_DAYS_RE: Final[re.Pattern[str]] = re.compile(r"up\s+(\d+)\s+days?,\s+(\d+):(\d+)")
_UPTIME_NODAYS_RE: Final[re.Pattern[str]] = re.compile(r"up\s+(\d+):(\d+)")
_LOAD_RE: Final[re.Pattern[str]] = re.compile(r"load average:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)")

_DISK_PATH_RE: Final[re.Pattern[str]] = re.compile(r">> Disk path:\s*(\S+)")
_DISK_MODEL_RE: Final[re.Pattern[str]] = re.compile(r">> Disk model:\s*(.+?)\s*$", re.MULTILINE)
_DISK_INFO_DELIM: Final[str] = "************ Disk Info ***************"

_SMART_SEPARATOR: Final[str] = "---------------------"
_DURATION_RE: Final[re.Pattern[str]] = re.compile(r"^(?:(\d+)h)?(?:\+?(\d+)m)?(?:\+?([\d.]+)s)?$")

_MD_HEADER_RE: Final[re.Pattern[str]] = re.compile(r"^(md\d+)\s*:\s*active\b")
_MD_BRACKET_RE: Final[re.Pattern[str]] = re.compile(r"\[(\d+)/(\d+)\]\s*\[([U_]+)\]")
_MD_FAULTY_RE: Final[re.Pattern[str]] = re.compile(r"\(F\)")
_MD_RECOVERY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:recovery|resync)\s*=\s*([\d.]+)%.*?speed=(\d+)K/sec"
)

_HWMON_INPUT_RE: Final[re.Pattern[str]] = re.compile(r"coretemp\s+temp(\d+)_input=(\d+)")
_HWMON_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"coretemp\s+temp(\d+)_label=(.+?)\s*$")
_PACKAGE_LABEL: Final[str] = "Physical id 0"
_CORE_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"^Core\s+(\d+)$")
_MILLI: Final[float] = 1000.0

# ============================================================================
# Section-splitting helper
# ============================================================================


def split_sections(stdout: str) -> dict[str, str]:
    """Split combined stdout into {marker_key: body} on ===HM_*=== lines.

    marker_key is the text between the surrounding === for plain sections ("HM_UPTIME"),
    and for SMART sections the marker carries the device path: "HM_SMART /dev/sda".
    Leading banner text before the first marker is discarded. HM_END terminates (its body,
    if any, is ignored). A missing section simply has no key -> callers treat as "".
    """
    sections: dict[str, str] = {}
    current_key: str | None = None
    current_lines: list[str] = []

    for line in stdout.splitlines():
        match = _MARKER_RE.match(line)
        if match:
            # Found a new marker
            if current_key is not None:
                sections[current_key] = "\n".join(current_lines)
            current_key = match.group(1)
            current_lines = []
            if current_key == "HM_END":
                # End marker terminates collection
                break
        # Regular line
        elif current_key is not None:
            current_lines.append(line)

    # Finalize the last section if any
    if current_key is not None and current_key != "HM_END":
        sections[current_key] = "\n".join(current_lines)

    return sections


# ============================================================================
# Uptime parser
# ============================================================================


def parse_uptime(body: str) -> tuple[list[ProbeMetric], bool]:
    """Parse the uptime section.

    Returns (metrics, load_parsed). load_parsed gates ProbeOutcome.up.
    - uptime_seconds: emitted iff "up N days, HH:MM" or "up HH:MM" matched.
    - load1/5/15: emitted iff the load-average triple matched. Slices off the Synology
      "[IO: ... CPU: ...]" suffix implicitly because _LOAD_RE stops at the third float.
    """
    metrics: list[ProbeMetric] = []
    load_parsed = False

    # Try uptime with days
    m = _UPTIME_DAYS_RE.search(body)
    if m:
        days = int(m.group(1))
        hours = int(m.group(2))
        minutes = int(m.group(3))
        seconds = days * 86400 + hours * 3600 + minutes * 60
        metrics.append(ProbeMetric(M_UPTIME_SECONDS, float(seconds), {}))
    else:
        # Try uptime without days
        m = _UPTIME_NODAYS_RE.search(body)
        if m:
            hours = int(m.group(1))
            minutes = int(m.group(2))
            seconds = hours * 3600 + minutes * 60
            metrics.append(ProbeMetric(M_UPTIME_SECONDS, float(seconds), {}))

    # Try load average
    m = _LOAD_RE.search(body)
    if m:
        load1 = float(m.group(1))
        load5 = float(m.group(2))
        load15 = float(m.group(3))
        metrics.append(ProbeMetric(M_LOAD1, load1, {}))
        metrics.append(ProbeMetric(M_LOAD5, load5, {}))
        metrics.append(ProbeMetric(M_LOAD15, load15, {}))
        load_parsed = True

    return (metrics, load_parsed)


# ============================================================================
# synodisk enum parser
# ============================================================================


def parse_synodisk_enum(body: str) -> list[tuple[str, str]]:
    """Return [(disk_basename, model), ...] for each ************ Disk Info *********** block.

    disk_basename strips /dev/ (e.g. "/dev/sda" -> "sda"). A block missing a path is skipped;
    a block missing a model uses "" for model.
    """
    disks: list[tuple[str, str]] = []
    chunks = body.split(_DISK_INFO_DELIM)

    for chunk in chunks:
        path_match = _DISK_PATH_RE.search(chunk)
        if not path_match:
            continue
        path = path_match.group(1)
        disk = path.rsplit("/", 1)[-1]

        model_match = _DISK_MODEL_RE.search(chunk)
        model = model_match.group(1) if model_match else ""

        disks.append((disk, model))

    return disks


# ============================================================================
# SMART parsing helpers
# ============================================================================


def _field(chunk: str, key: str) -> str | None:
    """Return the value after 'key:' on its line, or None if absent."""
    for line in chunk.splitlines():
        if line.startswith(key + ":"):
            return line[len(key) + 1 :].strip()
    return None


def parse_raw(raw: str) -> float | None:
    """Parse a SMART Raw value to float.

    - Pure integer ("23235") -> 23235.0.
    - Duration string ("49180h+18m+27.502s") -> total SECONDS as float.
    - Anything else (e.g. hex, garbage) -> None (caller skips the raw metric only).
    """
    raw = raw.strip()
    if not raw:
        return None

    # Try pure int
    try:
        return float(int(raw))
    except ValueError:
        pass

    # Try plain float
    try:
        return float(raw)
    except ValueError:
        pass

    # Try duration
    m = _DURATION_RE.fullmatch(raw)
    if m:
        h_str, m_str, s_str = m.groups()
        h = int(h_str) if h_str else 0
        m = int(m_str) if m_str else 0
        s = float(s_str) if s_str else 0.0
        return float(h * 3600 + m * 60) + s

    return None


def normalize_name(name: str) -> str:
    """lowercase; spaces and slashes -> underscores."""
    return name.strip().lower().replace(" ", "_").replace("/", "_")


def _count_failing(attrs_by_id: dict[int, dict[str, str | None]]) -> int:
    """Count attrs that are failing (bad status or threshold breach)."""
    count = 0
    for _, fields in attrs_by_id.items():
        status = fields["status"] or ""
        current_str = fields["current"] or ""
        threshold_str = fields["threshold"] or ""
        try:
            current = int(current_str) if current_str else 0
        except ValueError:
            current = 0
        try:
            threshold = int(threshold_str) if threshold_str else 0
        except ValueError:
            threshold = 0
        if status.upper() != _HEALTHY_STATUS or (threshold > 0 and current <= threshold):
            count += 1
    return count


def _emit_whitelisted_attr(
    disk: str,
    attr_id: int,
    fields: dict[str, str | None],
    metrics: list[ProbeMetric],
) -> None:
    """Emit raw/worst/threshold metrics for one whitelisted SMART attribute."""
    name = fields["name"] or ""
    worst_str = fields["worst"]
    threshold_str = fields["threshold"]
    raw_str = fields["raw"]
    attr_id_str = str(attr_id)

    raw_value = parse_raw(raw_str or "")
    if raw_value is not None:
        attr_name = normalize_name(name)
        metrics.append(
            ProbeMetric(
                M_SMART_ATTR_RAW,
                raw_value,
                {"disk": disk, "attr_id": attr_id_str, "attr_name": attr_name},
            )
        )

    try:
        worst = float(int(worst_str)) if worst_str else None
    except ValueError:
        worst = None
    if worst is not None:
        metrics.append(
            ProbeMetric(M_SMART_ATTR_WORST, worst, {"disk": disk, "attr_id": attr_id_str})
        )

    try:
        threshold = float(int(threshold_str)) if threshold_str else None
    except ValueError:
        threshold = None
    if threshold is not None:
        metrics.append(
            ProbeMetric(M_SMART_ATTR_THRESHOLD, threshold, {"disk": disk, "attr_id": attr_id_str})
        )


def parse_smart_block(disk: str, body: str) -> list[ProbeMetric]:
    """Parse one disk's SMART body into metrics (whitelist raw/worst/threshold + _failing)."""
    metrics: list[ProbeMetric] = []

    # Split into attribute chunks
    chunks = body.split(_SMART_SEPARATOR)

    # First pass: collect all attribute IDs to determine which to emit
    present_ids: set[int] = set()
    attrs_by_id: dict[int, dict[str, str | None]] = {}

    for chunk in chunks:
        if not chunk.strip():
            continue

        id_str = _field(chunk, "Id")
        if not id_str:
            continue

        try:
            attr_id = int(id_str)
        except ValueError:
            continue

        present_ids.add(attr_id)

        # Parse all fields for this attr
        name = _field(chunk, "Name") or ""
        current_str = _field(chunk, "Current") or ""
        worst_str = _field(chunk, "Worst") or ""
        threshold_str = _field(chunk, "Threshold") or ""
        raw = _field(chunk, "Raw") or ""
        status = _field(chunk, "Status") or ""

        attrs_by_id[attr_id] = {
            "name": name,
            "current": current_str,
            "worst": worst_str,
            "threshold": threshold_str,
            "raw": raw,
            "status": status,
        }

    # Determine the effective temperature ID for this disk
    emit_ids = set(_WHITELIST_IDS)
    if _TEMP_PRIMARY_ID not in present_ids and _TEMP_FALLBACK_ID in present_ids:
        emit_ids.add(_TEMP_FALLBACK_ID)

    # Count failing attributes (all attrs, regardless of whitelist)
    failing = _count_failing(attrs_by_id)

    # Emit whitelisted attrs
    for attr_id in sorted(emit_ids):
        if attr_id not in attrs_by_id:
            continue
        _emit_whitelisted_attr(disk, attr_id, attrs_by_id[attr_id], metrics)

    # Always emit the failing count
    metrics.append(
        ProbeMetric(
            M_SMART_ATTR_FAILING,
            float(failing),
            {"disk": disk},
        )
    )

    return metrics


# ============================================================================
# mdstat parser
# ============================================================================


def parse_mdstat(body: str) -> list[ProbeMetric]:
    """Parse /proc/mdstat into per-array health metrics."""
    metrics: list[ProbeMetric] = []

    # Split by array headers
    lines = body.splitlines()
    current_array: str | None = None
    current_chunk: list[str] = []

    for line in lines:
        header_match = _MD_HEADER_RE.match(line)
        if header_match:
            # Finalize previous chunk
            if current_array is not None and current_chunk:
                chunk_text = "\n".join(current_chunk)
                metrics.extend(_process_mdstat_array(current_array, chunk_text))
            current_array = header_match.group(1)
            current_chunk = [line]
        elif current_array is not None:
            current_chunk.append(line)

    # Finalize last chunk
    if current_array is not None and current_chunk:
        chunk_text = "\n".join(current_chunk)
        metrics.extend(_process_mdstat_array(current_array, chunk_text))

    return metrics


def _process_mdstat_array(array: str, chunk: str) -> list[ProbeMetric]:
    """Process a single array chunk and return its metrics."""
    metrics: list[ProbeMetric] = []

    faulty = bool(_MD_FAULTY_RE.search(chunk))
    bracket_match = _MD_BRACKET_RE.search(chunk)

    if bracket_match:
        active = int(bracket_match.group(2))
        ubits = bracket_match.group(3)
        u_count = ubits.count("U")

        # Emit disks_active and disks_total
        metrics.append(ProbeMetric(M_MDSTAT_DISKS_ACTIVE, float(active), {"array": array}))
        # disks_total = configured slot count (e.g., 12 on SHR [12/8]), not working-disk count.
        # For SHR layouts, disks_active < disks_total is NORMAL; use mdstat_array_degraded
        # for degradation status.
        metrics.append(ProbeMetric(M_MDSTAT_DISKS_TOTAL, float(len(ubits)), {"array": array}))

        # Determine degraded
        degraded = 1.0 if (u_count < active or faulty) else 0.0
    else:
        # No bracket: degraded if faulty, else 0
        degraded = 1.0 if faulty else 0.0

    # Always emit degraded
    metrics.append(ProbeMetric(M_MDSTAT_ARRAY_DEGRADED, degraded, {"array": array}))

    # Check for resync/recovery
    recovery_match = _MD_RECOVERY_RE.search(chunk)
    if recovery_match:
        progress = float(recovery_match.group(1))
        speed = float(recovery_match.group(2))
        metrics.append(ProbeMetric(M_MDSTAT_RESYNC_PROGRESS, progress, {"array": array}))
        metrics.append(ProbeMetric(M_MDSTAT_RESYNC_SPEED, speed, {"array": array}))

    return metrics


# ============================================================================
# hwmon parser
# ============================================================================


def parse_hwmon(body: str) -> list[ProbeMetric]:
    """Parse hwmon coretemp lines into package + per-core temps (celsius)."""
    metrics: list[ProbeMetric] = []

    # Build inputs and labels dicts
    inputs: dict[int, int] = {}
    labels: dict[int, str] = {}

    for line in body.splitlines():
        input_match = _HWMON_INPUT_RE.search(line)
        if input_match:
            temp_num = int(input_match.group(1))
            milli_value = int(input_match.group(2))
            inputs[temp_num] = milli_value

        label_match = _HWMON_LABEL_RE.search(line)
        if label_match:
            temp_num = int(label_match.group(1))
            label_text = label_match.group(2)
            labels[temp_num] = label_text

    # Early exit if no inputs
    if not inputs:
        return []

    # Find package temp (Physical id 0) or fallback to max
    pkg_temp_num: int | None = None
    for temp_num, label in labels.items():
        if label == _PACKAGE_LABEL:
            pkg_temp_num = temp_num
            break

    if pkg_temp_num is not None and pkg_temp_num in inputs:
        pkg = inputs[pkg_temp_num] / _MILLI
    else:
        # Fallback to max
        pkg = max(inputs.values()) / _MILLI

    metrics.append(ProbeMetric(M_CPU_TEMP, pkg, {}))

    # Emit per-core temps
    for temp_num, label in labels.items():
        core_match = _CORE_LABEL_RE.match(label)
        if core_match and temp_num in inputs:
            core_num = core_match.group(1)
            temp_c = inputs[temp_num] / _MILLI
            metrics.append(ProbeMetric(M_CPU_CORE_TEMP, temp_c, {"core": core_num}))

    return metrics


# ============================================================================
# The probe class
# ============================================================================


class SynologyProbe(SshProbe):
    """Combined Synology forced-command probe (single synology target)."""

    abstract: ClassVar[bool] = False
    target_id: ClassVar[str] = "synology"
    name: ClassVar[str] = "synology-probe"
    interval: ClassVar[timedelta] = timedelta(minutes=5)
    timeout: ClassVar[timedelta] = timedelta(seconds=30)
    concurrency_group: ClassVar[str] = "ssh_synology"
    command: ClassVar[str] = ""  # forced-command target ignores the requested command

    def parse(self, result: SshCommandResult) -> ProbeOutcome:
        if result.exit_status != 0:
            return ProbeOutcome(up=False, metrics=[])

        sections = split_sections(result.stdout)
        metrics: list[ProbeMetric] = []

        uptime_metrics, load_parsed = parse_uptime(sections.get("HM_UPTIME", ""))
        metrics.extend(uptime_metrics)

        disks = parse_synodisk_enum(sections.get("HM_SYNODISK_ENUM", ""))
        for disk, model in disks:
            metrics.append(
                ProbeMetric(
                    name=M_SMART_DISK_PRESENT,
                    value=1.0,
                    labels={"disk": disk, "model": model},
                )
            )

        for key, body in sections.items():
            if key.startswith("HM_SMART "):
                disk = key.removeprefix("HM_SMART ").rsplit("/", 1)[-1].strip()
                metrics.extend(parse_smart_block(disk, body))

        metrics.extend(parse_mdstat(sections.get("HM_MDSTAT", "")))
        metrics.extend(parse_hwmon(sections.get("HM_HWMON", "")))
        # upsc SCOPED OUT (STAGE-008-014 Design): DSM-API ups.py is sole UPS source;
        # capture stays in the NAS script for future UPS models.

        up = "HM_UPTIME" in sections and load_parsed
        return ProbeOutcome(up=up, metrics=metrics)
