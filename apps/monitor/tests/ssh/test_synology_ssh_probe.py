"""Tests for the Synology SSH probe (STAGE-008-014).

Covers:
- parse() pure logic (exit status, all parsers: uptime, synodisk, smart, mdstat, hwmon)
- All branches per the coverage table (100% coverage)
- PluginLoader registration (synology target gets SynologyProbe; others get UptimeProbe)
- Comprehensive fixtures (full capture, synthetic edge cases)
"""

from __future__ import annotations

from typing import Final

import pytest
import structlog

from homelab_monitor.kernel.plugins.loader import PluginLoader
from homelab_monitor.kernel.ssh.probe import ProbeMetric
from homelab_monitor.kernel.ssh.result import SshCommandResult
from homelab_monitor.plugins.collectors.ssh import register_all
from homelab_monitor.plugins.collectors.ssh.synology import (
    M_CPU_CORE_TEMP,
    M_CPU_TEMP,
    M_LOAD1,
    M_LOAD5,
    M_LOAD15,
    M_MDSTAT_ARRAY_DEGRADED,
    M_MDSTAT_DISKS_ACTIVE,
    M_MDSTAT_DISKS_TOTAL,
    M_MDSTAT_RESYNC_PROGRESS,
    M_MDSTAT_RESYNC_SPEED,
    M_SMART_ATTR_FAILING,
    M_SMART_ATTR_RAW,
    M_SMART_ATTR_THRESHOLD,
    M_SMART_ATTR_WORST,
    M_SMART_DISK_PRESENT,
    M_UPTIME_SECONDS,
    SynologyProbe,
    normalize_name,
    parse_hwmon,
    parse_mdstat,
    parse_raw,
    parse_smart_block,
    parse_synodisk_enum,
    parse_uptime,
    split_sections,
)

# ============================================================================
# Named constants for magic numbers (PLR2004 avoidance)
# ============================================================================

EXPECTED_UPTIME_52_DAYS_22H_14M_SECONDS: Final[int] = 52 * 86400 + 22 * 3600 + 14 * 60
EXPECTED_LOAD1: Final[float] = 1.42
EXPECTED_LOAD5: Final[float] = 1.74
EXPECTED_LOAD15: Final[float] = 1.87

EXPECTED_PKG_TEMP_C: Final[float] = 53.0
EXPECTED_CORE_COUNT: Final[int] = 6
THREE_DISKS: Final[int] = 3

SDA_POWER_ON_HOURS_RAW: Final[float] = 23235.0
SDE_POWER_ON_HOURS_RAW: Final[float] = 49199.0
DURATION_240_SECONDS: Final[float] = 49180 * 3600 + 18 * 60 + 27.502

UPTIME_3H_14M_SECONDS: Final[int] = 3 * 3600 + 14 * 60

MD2_ACTIVE_COUNT: Final[int] = 8
MD2_TOTAL_COUNT: Final[int] = 8
MD0_ACTIVE_COUNT: Final[int] = 8
MD0_TOTAL_COUNT: Final[int] = 12
DEGRADED_MISSING_MEMBER: Final[float] = 1.0
DEGRADED_HEALTHY: Final[float] = 0.0

EXPECTED_FULL_CAPTURE_METRICS: Final[int] = 89
FLOAT_TOLERANCE: Final[float] = 0.01
SDE_AIRFLOW_TEMP_RAW: Final[float] = 45.0

# ============================================================================
# Test fixtures (inline constants)
# ============================================================================

# Full capture with three disks: sda, sde, sdh (covers duration-raw on sde and id 190)
CAPTURE_FULL: Final[str] = """\
Could not chdir to home directory /var/services/homes/homelab-probe: Permission denied
===HM_UPTIME===
 08:10:00 up 52 days, 22:14,  1 user,  load average: 1.42, 1.74, 1.87 [IO: 0.23, 0.34, 0.40 CPU: 1.18, 1.38, 1.44]
===HM_DF===
Filesystem             1024-blocks        Used   Available Capacity Mounted on
/dev/md0                   8191352     1563480     6509088      20% /
===HM_SYNODISK_ENUM===
************ Disk Info ***************
>> Disk id: 1
>> Slot id: -1
>> Disk path: /dev/sda
>> Disk model: WD221KFGX-68B9KN0
>> Total capacity: 20490.00 GB
>> Tempeture: 44 C
************ Disk Info ***************
>> Disk id: 5
>> Slot id: -1
>> Disk path: /dev/sde
>> Disk model: ST10000VN0008-2JJ101
>> Total capacity: 9314.00 GB
>> Tempeture: 46 C
************ Disk Info ***************
>> Disk id: 8
>> Slot id: -1
>> Disk path: /dev/sdh
>> Disk model: HAT3310-12T
>> Total capacity: 11176.00 GB
>> Tempeture: 36 C
===HM_SMART /dev/sda===
Name: Reallocated_Sector_Ct
Id: 5
Current: 100
Worst: 100
Threshold: 001
Raw: 0
Status: OK
---------------------
Name: Power_On_Hours
Id: 9
Current: 098
Worst: 098
Threshold: 000
Raw: 23235
Status: OK
---------------------
Name: Temperature_Celsius
Id: 194
Current: 051
Worst: 051
Threshold: 000
Raw: 43
Status: OK
---------------------
Name: Reallocated_Event_Count
Id: 196
Current: 100
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: Current_Pending_Sector
Id: 197
Current: 100
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: Offline_Uncorrectable
Id: 198
Current: 100
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: UDMA_CRC_Error_Count
Id: 199
Current: 200
Worst: 200
Threshold: 000
Raw: 0
Status: OK
---------------------
===HM_SMART /dev/sde===
Name: Reallocated_Sector_Ct
Id: 5
Current: 100
Worst: 100
Threshold: 010
Raw: 0
Status: OK
---------------------
Name: Power_On_Hours
Id: 9
Current: 044
Worst: 044
Threshold: 000
Raw: 49199
Status: OK
---------------------
Name: Airflow_Temperature_Cel
Id: 190
Current: 055
Worst: 046
Threshold: 040
Raw: 45
Status: OK
---------------------
Name: Temperature_Celsius
Id: 194
Current: 045
Worst: 054
Threshold: 000
Raw: 45
Status: OK
---------------------
Name: Hardware_ECC_Recovered
Id: 195
Current: 100
Worst: 064
Threshold: 000
Raw: 1562472
Status: OK
---------------------
Name: Reallocated_Event_Count
Id: 196
Current: 100
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: Current_Pending_Sector
Id: 197
Current: 100
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: Offline_Uncorrectable
Id: 198
Current: 100
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: UDMA_CRC_Error_Count
Id: 199
Current: 200
Worst: 200
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: Head_Flying_Hours
Id: 240
Current: 100
Worst: 253
Threshold: 000
Raw: 49180h+18m+27.502s
Status: OK
---------------------
===HM_SMART /dev/sdh===
Name: Reallocated_Sector_Count
Id: 5
Current: 100
Worst: 100
Threshold: 010
Raw: 0
Status: OK
---------------------
Name: Power-On_hours_Count
Id: 9
Current: 085
Worst: 085
Threshold: 000
Raw: 6256
Status: OK
---------------------
Name: Temperature
Id: 194
Current: 100
Worst: 100
Threshold: 000
Raw: 36
Status: OK
---------------------
Name: Re-allocated_Sector_Event
Id: 196
Current: 100
Worst: 100
Threshold: 010
Raw: 0
Status: OK
---------------------
Name: Current_Pending_Sector
Id: 197
Current: 100
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: Offline_Uncorrectable
Id: 198
Current: 100
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: UDMA_CRC_Error_Count
Id: 199
Current: 200
Worst: 200
Threshold: 000
Raw: 0
Status: OK
---------------------
===HM_MDSTAT===
Personalities : [raid1] [raid6] [raid5] [raid4] [raidF1]
md2 : active raid6 sda3[0] sdh3[7] sdg3[6] sdf3[5] sde3[4] sdd3[3] sdc3[2] sdb3[1]
      58534275072 blocks super 1.2 level 6, 64k chunk, algorithm 2 [8/8] [UUUUUUUU]

md1 : active raid1 sda2[0] sdh2[3] sdg2[4] sdc2[7] sdd2[6] sde2[5] sdb2[2] sdf2[1]
      2097088 blocks [12/8] [UUUUUUUU____]

md0 : active raid1 sda1[0] sdc1[7] sdd1[6] sde1[5] sdg1[4] sdh1[3] sdb1[2] sdf1[1]
      8388544 blocks [12/8] [UUUUUUUU____]

unused devices: <none>
===HM_UPSC===
Init SSL without certificate database
battery.charge: 100
battery.voltage: 26.0
===HM_HWMON===
/sys/class/hwmon/hwmon0 coretemp temp1_input=53000
/sys/class/hwmon/hwmon0 coretemp temp2_input=53000
/sys/class/hwmon/hwmon0 coretemp temp3_input=53000
/sys/class/hwmon/hwmon0 coretemp temp4_input=53000
/sys/class/hwmon/hwmon0 coretemp temp5_input=53000
/sys/class/hwmon/hwmon0 coretemp temp6_input=53000
/sys/class/hwmon/hwmon0 coretemp temp7_input=53000
/sys/class/hwmon/hwmon0 coretemp temp1_label=Physical id 0
/sys/class/hwmon/hwmon0 coretemp temp2_label=Core 0
/sys/class/hwmon/hwmon0 coretemp temp3_label=Core 1
/sys/class/hwmon/hwmon0 coretemp temp4_label=Core 2
/sys/class/hwmon/hwmon0 coretemp temp5_label=Core 3
/sys/class/hwmon/hwmon0 coretemp temp6_label=Core 4
/sys/class/hwmon/hwmon0 coretemp temp7_label=Core 5
===HM_END===
"""  # noqa: E501

# Synthetic: SMART with 190 but NOT 194 (fallback branch)
SMART_190_FALLBACK_BODY: Final[str] = """Name: Reallocated_Sector_Ct
Id: 5
Current: 100
Worst: 100
Threshold: 001
Raw: 0
Status: OK
---------------------
Name: Power_On_Hours
Id: 9
Current: 098
Worst: 098
Threshold: 000
Raw: 23235
Status: OK
---------------------
Name: Airflow_Temperature_Cel
Id: 190
Current: 055
Worst: 046
Threshold: 040
Raw: 45
Status: OK
---------------------
Name: Reallocated_Event_Count
Id: 196
Current: 100
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: Current_Pending_Sector
Id: 197
Current: 100
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: Offline_Uncorrectable
Id: 198
Current: 100
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: UDMA_CRC_Error_Count
Id: 199
Current: 200
Worst: 200
Threshold: 000
Raw: 0
Status: OK
---------------------
"""

# Synthetic: SMART with failing status (not OK)
SMART_FAILING_STATUS_BODY: Final[str] = """Name: Reallocated_Sector_Ct
Id: 5
Current: 100
Worst: 100
Threshold: 001
Raw: 0
Status: OK
---------------------
Name: Power_On_Hours
Id: 9
Current: 098
Worst: 098
Threshold: 000
Raw: 23235
Status: FAILING_NOW
---------------------
Name: Temperature_Celsius
Id: 194
Current: 051
Worst: 051
Threshold: 000
Raw: 43
Status: OK
---------------------
Name: Reallocated_Event_Count
Id: 196
Current: 100
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: Current_Pending_Sector
Id: 197
Current: 100
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: Offline_Uncorrectable
Id: 198
Current: 100
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: UDMA_CRC_Error_Count
Id: 199
Current: 200
Worst: 200
Threshold: 000
Raw: 0
Status: OK
---------------------
"""

# Synthetic: SMART with threshold breach (current <= threshold, threshold > 0)
SMART_FAILING_THRESHOLD_BODY: Final[str] = """Name: Reallocated_Sector_Ct
Id: 5
Current: 005
Worst: 100
Threshold: 010
Raw: 0
Status: OK
---------------------
Name: Power_On_Hours
Id: 9
Current: 098
Worst: 098
Threshold: 000
Raw: 23235
Status: OK
---------------------
Name: Temperature_Celsius
Id: 194
Current: 051
Worst: 051
Threshold: 000
Raw: 43
Status: OK
---------------------
Name: Reallocated_Event_Count
Id: 196
Current: 100
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: Current_Pending_Sector
Id: 197
Current: 100
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: Offline_Uncorrectable
Id: 198
Current: 100
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: UDMA_CRC_Error_Count
Id: 199
Current: 200
Worst: 200
Threshold: 000
Raw: 0
Status: OK
---------------------
"""

# Synthetic: SMART with threshold zero (should NOT count as failing)
SMART_THRESHOLD_ZERO_BODY: Final[str] = """Name: Reallocated_Sector_Ct
Id: 5
Current: 002
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: Power_On_Hours
Id: 9
Current: 098
Worst: 098
Threshold: 000
Raw: 23235
Status: OK
---------------------
Name: Temperature_Celsius
Id: 194
Current: 051
Worst: 051
Threshold: 000
Raw: 43
Status: OK
---------------------
Name: Reallocated_Event_Count
Id: 196
Current: 100
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: Current_Pending_Sector
Id: 197
Current: 100
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: Offline_Uncorrectable
Id: 198
Current: 100
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: UDMA_CRC_Error_Count
Id: 199
Current: 200
Worst: 200
Threshold: 000
Raw: 0
Status: OK
---------------------
"""

# Synthetic: SMART with garbage raw value that should be skipped
SMART_GARBAGE_RAW_BODY: Final[str] = """Name: Reallocated_Sector_Ct
Id: 5
Current: 100
Worst: 100
Threshold: 001
Raw: 0xDEADBEEF
Status: OK
---------------------
Name: Power_On_Hours
Id: 9
Current: 098
Worst: 098
Threshold: 000
Raw: 23235
Status: OK
---------------------
Name: Temperature_Celsius
Id: 194
Current: 051
Worst: 051
Threshold: 000
Raw: 43
Status: OK
---------------------
Name: Reallocated_Event_Count
Id: 196
Current: 100
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: Current_Pending_Sector
Id: 197
Current: 100
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: Offline_Uncorrectable
Id: 198
Current: 100
Worst: 100
Threshold: 000
Raw: 0
Status: OK
---------------------
Name: UDMA_CRC_Error_Count
Id: 199
Current: 200
Worst: 200
Threshold: 000
Raw: 0
Status: OK
---------------------
"""

# Synthetic: mdstat with missing member (degraded)
MDSTAT_DEGRADED: Final[str] = """Personalities : [raid1] [raid6] [raid5] [raid4] [raidF1]
md9 : active raid6 sda3[0] sdh3[7] sdg3[6] sdf3[5] sde3[4] sdd3[3] sdc3[2] sdb3[1]
      58534275072 blocks super 1.2 level 6, 64k chunk, algorithm 2 [8/8] [UUUUUUU_]

unused devices: <none>
"""

# Synthetic: mdstat with faulty flag (degraded)
MDSTAT_DEGRADED_FAULTY: Final[str] = """Personalities : [raid1] [raid6] [raid5] [raid4] [raidF1]
md9 : active raid6 sda3[0] sdh3[7](F) sdg3[6] sdf3[5] sde3[4] sdd3[3] sdc3[2] sdb3[1]
      58534275072 blocks super 1.2 level 6, 64k chunk, algorithm 2 [8/8] [UUUUUUUU]

unused devices: <none>
"""

# Synthetic: mdstat with no bracket (degraded if faulty)
MDSTAT_NO_BRACKET: Final[str] = """Personalities : [raid1] [raid6] [raid5] [raid4] [raidF1]
md10 : active raid1 sda1[0](F)
      8388544 blocks

unused devices: <none>
"""

# Synthetic: mdstat with resync/recovery progress
MDSTAT_REBUILDING: Final[str] = """Personalities : [raid1] [raid6] [raid5] [raid4] [raidF1]
md2 : active raid6 sda3[0] sdh3[7] sdg3[6] sdf3[5] sde3[4] sdd3[3] sdc3[2] sdb3[1]
      58534275072 blocks super 1.2 level 6, 64k chunk, algorithm 2 [8/8] [UUUUUUUU]
      [==>..................]  recovery = 12.6% (1234567/9876543) finish=120.5min speed=45678K/sec

unused devices: <none>
"""

# Synthetic: hwmon with no Physical id 0 label (uses max fallback)
HWMON_NO_PACKAGE: Final[str] = """/sys/class/hwmon/hwmon0 coretemp temp1_input=55000
/sys/class/hwmon/hwmon0 coretemp temp2_input=53000
/sys/class/hwmon/hwmon0 coretemp temp3_input=52000
/sys/class/hwmon/hwmon0 coretemp temp2_label=Core 0
/sys/class/hwmon/hwmon0 coretemp temp3_label=Core 1
"""

# Synthetic: hwmon with no input lines (empty -> no cpu_temp)
HWMON_EMPTY: Final[str] = ""

# Synthetic: uptime no-days variant
UPTIME_NODAYS: Final[str] = (
    "09:00:00 up 3:14,  1 user,  load average: 0.10, 0.20, 0.30 "
    "[IO: 0.0, 0.0, 0.0 CPU: 0.0, 0.0, 0.0]\n"
)

# Synthetic: uptime with unparseable "up" phrase (will skip seconds, but load parses)
UPTIME_UNPARSEABLE: Final[str] = (
    "09:00:00 up 0 min,  1 user,  load average: 0.10, 0.20, 0.30 "
    "[IO: 0.0, 0.0, 0.0 CPU: 0.0, 0.0, 0.0]\n"
)

# Synthetic: uptime with no load triple
UPTIME_NO_LOAD: Final[str] = """09:00:00 up 3:14,  1 user
"""

# ============================================================================
# Helpers
# ============================================================================


def _by_name(metrics: list[ProbeMetric], name: str) -> list[ProbeMetric]:
    """Return all metrics with the given name."""
    return [m for m in metrics if m.name == name]


# ============================================================================
# parse() tests: exit status and overall outcome
# ============================================================================


def test_exit_status_nonzero_returns_down_empty() -> None:
    """Non-zero exit status -> up=False, no metrics."""
    probe = SynologyProbe()
    result = SshCommandResult("", "", 1)
    outcome = probe.parse(result)

    assert outcome.up is False
    assert outcome.metrics == []


def test_full_capture_up_true() -> None:
    """Parse CAPTURE_FULL -> up is True."""
    probe = SynologyProbe()
    result = SshCommandResult(CAPTURE_FULL, "", 0)
    outcome = probe.parse(result)

    assert outcome.up is True
    # Basic smoke test: should have metrics
    assert len(outcome.metrics) > 0


# ============================================================================
# Uptime parser tests
# ============================================================================


def test_uptime_with_days_seconds() -> None:
    """uptime with days parsed correctly: 52d 22h 14m."""
    metrics, _ = parse_uptime(
        " 08:10:00 up 52 days, 22:14,  1 user,  load average: 1.42, 1.74, 1.87"
    )

    uptime_metrics = [m for m in metrics if m.name == M_UPTIME_SECONDS]
    assert len(uptime_metrics) == 1
    assert uptime_metrics[0].value == float(EXPECTED_UPTIME_52_DAYS_22H_14M_SECONDS)


def test_uptime_no_days_seconds() -> None:
    """uptime without days: 3h 14m."""
    metrics, _ = parse_uptime("09:00:00 up 3:14,  1 user,  load average: 0.10, 0.20, 0.30")

    uptime_metrics = [m for m in metrics if m.name == M_UPTIME_SECONDS]
    assert len(uptime_metrics) == 1
    assert uptime_metrics[0].value == float(UPTIME_3H_14M_SECONDS)


def test_uptime_unparseable_skips_seconds_keeps_load() -> None:
    """Unparseable uptime phrase -> skips seconds but load still parses."""
    metrics, load_parsed = parse_uptime(UPTIME_UNPARSEABLE)

    uptime_metrics = [m for m in metrics if m.name == M_UPTIME_SECONDS]
    assert len(uptime_metrics) == 0  # Skipped

    load_metrics = [m for m in metrics if m.name == M_LOAD1]
    assert len(load_metrics) == 1
    assert load_parsed is True


def test_uptime_no_load_marks_down() -> None:
    """No load triple -> load_parsed=False."""
    _, load_parsed = parse_uptime(UPTIME_NO_LOAD)

    assert load_parsed is False


def test_load_averages_emitted() -> None:
    """Load averages (1, 5, 15) parsed and emitted correctly."""
    metrics, _ = parse_uptime(
        " 08:10:00 up 52 days, 22:14,  1 user,  load average: 1.42, 1.74, 1.87 "
        "[IO: 0.23, 0.34, 0.40 CPU: 1.18, 1.38, 1.44]"
    )

    load1 = [m for m in metrics if m.name == M_LOAD1]
    load5 = [m for m in metrics if m.name == M_LOAD5]
    load15 = [m for m in metrics if m.name == M_LOAD15]

    assert len(load1) == 1
    assert load1[0].value == EXPECTED_LOAD1
    assert len(load5) == 1
    assert load5[0].value == EXPECTED_LOAD5
    assert len(load15) == 1
    assert load15[0].value == EXPECTED_LOAD15


# ============================================================================
# SMART parsing tests
# ============================================================================


def test_smart_disk_present_seeded() -> None:
    """Each disk in synodisk enum emits disk_present metric with model."""
    probe = SynologyProbe()
    result = SshCommandResult(CAPTURE_FULL, "", 0)
    outcome = probe.parse(result)

    disk_present = _by_name(outcome.metrics, M_SMART_DISK_PRESENT)
    assert len(disk_present) == THREE_DISKS

    # Check sda, sde, sdh by model
    models = {m.labels["model"] for m in disk_present}
    assert "WD221KFGX-68B9KN0" in models  # sda
    assert "ST10000VN0008-2JJ101" in models  # sde
    assert "HAT3310-12T" in models  # sdh


def test_smart_whitelist_raw_worst_threshold() -> None:
    """Whitelisted attrs (5,9,194,etc) emit raw, worst, threshold."""
    metrics = parse_smart_block(
        "sda", CAPTURE_FULL.split("===HM_SMART /dev/sda===")[1].split("===HM_SMART")[0]
    )

    # Id 9 should emit raw, worst, threshold
    raw_9 = [m for m in metrics if m.name == M_SMART_ATTR_RAW and m.labels.get("attr_id") == "9"]
    assert len(raw_9) == 1
    assert raw_9[0].value == SDA_POWER_ON_HOURS_RAW
    assert raw_9[0].labels["attr_name"] == "power_on_hours"

    worst_9 = [
        m for m in metrics if m.name == M_SMART_ATTR_WORST and m.labels.get("attr_id") == "9"
    ]
    assert len(worst_9) == 1
    assert worst_9[0].value == 98.0  # noqa: PLR2004

    threshold_9 = [
        m for m in metrics if m.name == M_SMART_ATTR_THRESHOLD and m.labels.get("attr_id") == "9"
    ]
    assert len(threshold_9) == 1
    assert threshold_9[0].value == 0.0


def test_smart_duration_raw_not_emitted_for_nonwhitelisted_id() -> None:
    """Id 240 (Head_Flying_Hours) has a duration raw but is not whitelisted -> NOT emitted."""
    metrics = parse_smart_block(
        "sde",
        CAPTURE_FULL.split("===HM_SMART /dev/sde===")[1].split("===HM_SMART /dev/sdh===")[0],
    )
    raw_240 = [
        m for m in metrics if m.name == M_SMART_ATTR_RAW and m.labels.get("attr_id") == "240"
    ]
    assert len(raw_240) == 0


def test_smart_garbage_raw_skipped_but_worst_threshold_emitted() -> None:
    """Garbage raw (0xDEADBEEF) skipped; worst/threshold still emitted."""
    metrics = parse_smart_block("test_disk", SMART_GARBAGE_RAW_BODY)

    # Id 5 should have NO raw metric (skipped)
    raw_5 = [m for m in metrics if m.name == M_SMART_ATTR_RAW and m.labels.get("attr_id") == "5"]
    assert len(raw_5) == 0

    # But worst and threshold should be present
    worst_5 = [
        m for m in metrics if m.name == M_SMART_ATTR_WORST and m.labels.get("attr_id") == "5"
    ]
    assert len(worst_5) == 1
    assert worst_5[0].value == 100.0  # noqa: PLR2004

    threshold_5 = [
        m for m in metrics if m.name == M_SMART_ATTR_THRESHOLD and m.labels.get("attr_id") == "5"
    ]
    assert len(threshold_5) == 1
    assert threshold_5[0].value == 1.0


def test_smart_190_fallback_when_194_absent() -> None:
    """When 194 absent but 190 present, emit 190 as temperature."""
    metrics = parse_smart_block("test_disk_190", SMART_190_FALLBACK_BODY)

    # Id 190 should be emitted (fallback)
    raw_190 = [
        m for m in metrics if m.name == M_SMART_ATTR_RAW and m.labels.get("attr_id") == "190"
    ]
    assert len(raw_190) == 1
    assert raw_190[0].value == SDE_AIRFLOW_TEMP_RAW


def test_smart_no_190_when_194_present() -> None:
    """When 194 present, 190 is NOT emitted (even if present)."""
    metrics = parse_smart_block(
        "sde", CAPTURE_FULL.split("===HM_SMART /dev/sde===")[1].split("===HM_SMART /dev/sdh===")[0]
    )

    # sde has BOTH 194 and 190 present; only 194 should be emitted
    raw_190 = [
        m for m in metrics if m.name == M_SMART_ATTR_RAW and m.labels.get("attr_id") == "190"
    ]
    assert len(raw_190) == 0  # 190 suppressed because 194 present


def test_smart_failing_seeded_zero() -> None:
    """Healthy disk -> failing count 0.0."""
    metrics = parse_smart_block("healthy_disk", SMART_THRESHOLD_ZERO_BODY)

    failing = _by_name(metrics, M_SMART_ATTR_FAILING)
    assert len(failing) == 1
    assert failing[0].value == 0.0


def test_smart_failing_on_bad_status() -> None:
    """Attr with Status != OK -> counts as failing."""
    metrics = parse_smart_block("bad_disk", SMART_FAILING_STATUS_BODY)

    failing = _by_name(metrics, M_SMART_ATTR_FAILING)
    assert len(failing) == 1
    assert failing[0].value == 1.0


def test_smart_failing_on_threshold_breach() -> None:
    """Attr with current<=threshold and threshold>0 -> counts as failing."""
    metrics = parse_smart_block("breach_disk", SMART_FAILING_THRESHOLD_BODY)

    failing = _by_name(metrics, M_SMART_ATTR_FAILING)
    assert len(failing) == 1
    assert failing[0].value == 1.0


def test_smart_threshold_zero_not_failing() -> None:
    """Threshold 0 -> NOT failing even if current is low."""
    metrics = parse_smart_block("zero_threshold_disk", SMART_THRESHOLD_ZERO_BODY)

    failing = _by_name(metrics, M_SMART_ATTR_FAILING)
    assert len(failing) == 1
    assert failing[0].value == 0.0


# ============================================================================
# mdstat parsing tests
# ============================================================================


def test_mdstat_healthy_raid6() -> None:
    """md2 with [8/8] [UUUUUUUU] -> degraded 0, active 8, total 8."""
    metrics = parse_mdstat(CAPTURE_FULL.split("===HM_MDSTAT===")[1].split("===HM_UPSC===")[0])

    md2_degraded = [
        m for m in metrics if m.name == M_MDSTAT_ARRAY_DEGRADED and m.labels.get("array") == "md2"
    ]
    assert len(md2_degraded) == 1
    assert md2_degraded[0].value == DEGRADED_HEALTHY

    md2_active = [
        m for m in metrics if m.name == M_MDSTAT_DISKS_ACTIVE and m.labels.get("array") == "md2"
    ]
    assert len(md2_active) == 1
    assert md2_active[0].value == float(MD2_ACTIVE_COUNT)

    md2_total = [
        m for m in metrics if m.name == M_MDSTAT_DISKS_TOTAL and m.labels.get("array") == "md2"
    ]
    assert len(md2_total) == 1
    assert md2_total[0].value == float(MD2_TOTAL_COUNT)


def test_mdstat_healthy_12slot() -> None:
    """md0/md1 with [12/8] [UUUUUUUU____] -> degraded 0, active 8, total 12."""
    metrics = parse_mdstat(CAPTURE_FULL.split("===HM_MDSTAT===")[1].split("===HM_UPSC===")[0])

    md0_degraded = [
        m for m in metrics if m.name == M_MDSTAT_ARRAY_DEGRADED and m.labels.get("array") == "md0"
    ]
    assert len(md0_degraded) == 1
    assert md0_degraded[0].value == DEGRADED_HEALTHY

    md0_active = [
        m for m in metrics if m.name == M_MDSTAT_DISKS_ACTIVE and m.labels.get("array") == "md0"
    ]
    assert len(md0_active) == 1
    assert md0_active[0].value == float(MD0_ACTIVE_COUNT)

    md0_total = [
        m for m in metrics if m.name == M_MDSTAT_DISKS_TOTAL and m.labels.get("array") == "md0"
    ]
    assert len(md0_total) == 1
    assert md0_total[0].value == float(MD0_TOTAL_COUNT)


def test_mdstat_degraded_missing_member() -> None:
    """md with [8/8] [UUUUUUU_] -> degraded 1 (u_count < active)."""
    metrics = parse_mdstat(MDSTAT_DEGRADED)

    md9_degraded = [
        m for m in metrics if m.name == M_MDSTAT_ARRAY_DEGRADED and m.labels.get("array") == "md9"
    ]
    assert len(md9_degraded) == 1
    assert md9_degraded[0].value == DEGRADED_MISSING_MEMBER


def test_mdstat_degraded_faulty_flag() -> None:
    """md with (F) faulty flag -> degraded 1."""
    metrics = parse_mdstat(MDSTAT_DEGRADED_FAULTY)

    md9_degraded = [
        m for m in metrics if m.name == M_MDSTAT_ARRAY_DEGRADED and m.labels.get("array") == "md9"
    ]
    assert len(md9_degraded) == 1
    assert md9_degraded[0].value == DEGRADED_MISSING_MEMBER


def test_mdstat_no_bracket_faulty() -> None:
    """md with no bracket but (F) -> degraded 1, no disks_active/total."""
    metrics = parse_mdstat(MDSTAT_NO_BRACKET)

    mdx_degraded = [
        m for m in metrics if m.name == M_MDSTAT_ARRAY_DEGRADED and m.labels.get("array") == "md10"
    ]
    assert len(mdx_degraded) == 1
    assert mdx_degraded[0].value == DEGRADED_MISSING_MEMBER

    # Should NOT have disks_active/total
    mdx_active = [
        m for m in metrics if m.name == M_MDSTAT_DISKS_ACTIVE and m.labels.get("array") == "md10"
    ]
    assert len(mdx_active) == 0


def test_mdstat_rebuilding_progress_and_speed() -> None:
    """md with recovery line -> emits progress and speed."""
    metrics = parse_mdstat(MDSTAT_REBUILDING)

    md2_progress = [
        m for m in metrics if m.name == M_MDSTAT_RESYNC_PROGRESS and m.labels.get("array") == "md2"
    ]
    assert len(md2_progress) == 1
    assert md2_progress[0].value == 12.6  # noqa: PLR2004

    md2_speed = [
        m for m in metrics if m.name == M_MDSTAT_RESYNC_SPEED and m.labels.get("array") == "md2"
    ]
    assert len(md2_speed) == 1
    assert md2_speed[0].value == 45678.0  # noqa: PLR2004


# ============================================================================
# hwmon parsing tests
# ============================================================================


def test_hwmon_package_and_cores() -> None:
    """hwmon with Physical id 0 and Core labels -> package + 6 cores."""
    metrics = parse_hwmon(CAPTURE_FULL.split("===HM_HWMON===")[1].split("===HM_END===")[0])

    pkg = _by_name(metrics, M_CPU_TEMP)
    assert len(pkg) == 1
    assert pkg[0].value == EXPECTED_PKG_TEMP_C

    cores = _by_name(metrics, M_CPU_CORE_TEMP)
    assert len(cores) == EXPECTED_CORE_COUNT

    # Check core labels
    core_labels = {m.labels["core"] for m in cores}
    assert core_labels == {"0", "1", "2", "3", "4", "5"}


def test_hwmon_max_fallback_when_no_package() -> None:
    """hwmon with no Physical id 0 -> uses max input."""
    metrics = parse_hwmon(HWMON_NO_PACKAGE)

    pkg = _by_name(metrics, M_CPU_TEMP)
    assert len(pkg) == 1
    # max(55000, 53000, 52000) / 1000 = 55.0
    assert pkg[0].value == 55.0  # noqa: PLR2004


def test_hwmon_empty_no_cpu_temp() -> None:
    """hwmon with no input lines -> no cpu_temp, no core metrics."""
    metrics = parse_hwmon(HWMON_EMPTY)

    assert len(metrics) == 0


# ============================================================================
# Raw value parsing tests
# ============================================================================


def test_raw_pure_int() -> None:
    """Pure integer raw value."""
    val = parse_raw("23235")
    assert val == 23235.0  # noqa: PLR2004


def test_raw_float() -> None:
    """Plain float raw value."""
    val = parse_raw("45.0")
    assert val == 45.0  # noqa: PLR2004


def test_raw_duration() -> None:
    """Duration string parsed to seconds."""
    val = parse_raw("49180h+18m+27.502s")
    assert val is not None
    assert abs(val - DURATION_240_SECONDS) < FLOAT_TOLERANCE


def test_raw_garbage_returns_none() -> None:
    """Garbage raw value returns None."""
    val = parse_raw("0xDEADBEEF")
    assert val is None


# ============================================================================
# Name normalization tests
# ============================================================================


def test_normalize_name() -> None:
    """Name normalized: lowercase, spaces/slashes -> underscores."""
    assert normalize_name("Power_On_Hours") == "power_on_hours"
    assert normalize_name("Start/Stop_Count") == "start_stop_count"
    assert normalize_name("UPPER CASE NAME") == "upper_case_name"


# ============================================================================
# Missing section tests
# ============================================================================


def test_missing_uptime_section() -> None:
    """Missing HM_UPTIME section -> no uptime metrics, up=False."""
    probe = SynologyProbe()
    no_uptime = CAPTURE_FULL.replace("===HM_UPTIME===", "===HM_MISSING===")
    result = SshCommandResult(no_uptime, "", 0)
    outcome = probe.parse(result)

    assert outcome.up is False
    uptime_metrics = _by_name(outcome.metrics, M_UPTIME_SECONDS)
    assert len(uptime_metrics) == 0


def test_missing_mdstat_section() -> None:
    """Missing HM_MDSTAT section -> no mdstat metrics."""
    probe = SynologyProbe()
    no_mdstat = CAPTURE_FULL.replace("===HM_MDSTAT===", "===HM_MISSING===")
    result = SshCommandResult(no_mdstat, "", 0)
    outcome = probe.parse(result)

    mdstat = _by_name(outcome.metrics, M_MDSTAT_ARRAY_DEGRADED)
    assert len(mdstat) == 0


def test_missing_hwmon_section() -> None:
    """Missing HM_HWMON section -> no hwmon metrics."""
    probe = SynologyProbe()
    no_hwmon = CAPTURE_FULL.replace("===HM_HWMON===", "===HM_MISSING===")
    result = SshCommandResult(no_hwmon, "", 0)
    outcome = probe.parse(result)

    hwmon = _by_name(outcome.metrics, M_CPU_TEMP)
    assert len(hwmon) == 0


def test_missing_synodisk_enum_section() -> None:
    """Missing HM_SYNODISK_ENUM section -> no disk_present metrics."""
    probe = SynologyProbe()
    no_enum = CAPTURE_FULL.replace("===HM_SYNODISK_ENUM===", "===HM_MISSING===")
    result = SshCommandResult(no_enum, "", 0)
    outcome = probe.parse(result)

    disk_present = _by_name(outcome.metrics, M_SMART_DISK_PRESENT)
    assert len(disk_present) == 0


def test_missing_smart_section() -> None:
    """Missing HM_SMART section -> no smart metrics."""
    probe = SynologyProbe()
    no_smart = CAPTURE_FULL.split("===HM_SMART")[0] + "===HM_MDSTAT==="
    no_smart += CAPTURE_FULL.split("===HM_MDSTAT===")[1]
    result = SshCommandResult(no_smart, "", 0)
    outcome = probe.parse(result)

    smart_raw = _by_name(outcome.metrics, M_SMART_ATTR_RAW)
    assert len(smart_raw) == 0


# ============================================================================
# UPSC ignoring test
# ============================================================================


def test_upsc_ignored() -> None:
    """UPSC section present -> no ups/battery metrics emitted."""
    probe = SynologyProbe()
    result = SshCommandResult(CAPTURE_FULL, "", 0)
    outcome = probe.parse(result)

    # Assert no metric name starts with "homelab_synology_ups" or "homelab_synology_battery"
    for metric in outcome.metrics:
        assert not metric.name.startswith("homelab_synology_ups")
        assert not metric.name.startswith("homelab_synology_battery")


# ============================================================================
# Registration tests
# ============================================================================


def test_register_all_synology_gets_combined_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """The synology target registers SynologyProbe, not uptime-synology; udm keeps uptime."""
    monkeypatch.setattr(
        "homelab_monitor.plugins.collectors.ssh.load_ssh_target_configs",
        lambda: {"synology": object(), "udm": object()},
    )
    loader = PluginLoader(log=structlog.get_logger())  # pyright: ignore[reportArgumentType]
    register_all(loader)

    names = {lc.config.name for lc in loader.load_all()}
    assert "synology-probe" in names
    assert "uptime-synology" not in names
    assert "uptime-udm" in names


def test_register_all_isolates_synology_probe_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """register_all swallows SynologyProbe registration errors without re-raising."""
    monkeypatch.setattr(
        "homelab_monitor.plugins.collectors.ssh.load_ssh_target_configs",
        lambda: {"synology": object()},
    )

    calls: list[str] = []

    class _FailingLoader:
        def register(self, cls: object, cfg: object) -> None:
            calls.append(getattr(cls, "name", "?"))
            raise RuntimeError("boom")

    register_all(_FailingLoader())  # pyright: ignore[reportArgumentType]

    # Synology target attempted; exception was swallowed
    assert len(calls) == 1
    assert calls[0] == "synology-probe"


# ============================================================================
# Full capture reconciliation test
# ============================================================================


def test_full_capture_metric_count() -> None:
    """CAPTURE_FULL yields expected metric count (89)."""
    probe = SynologyProbe()
    result = SshCommandResult(CAPTURE_FULL, "", 0)
    outcome = probe.parse(result)

    # Expected breakdown:
    # Uptime: 1 uptime_seconds + 3 loads = 4
    # disk_present: 3 disks = 3
    # SMART per disk: 3 x (7 attrs x 3 metrics [raw, worst, threshold] + 1 failing) = 3 x 22 = 66
    # mdstat: 3 arrays x (degraded + disks_active + disks_total) = 3 x 3 = 9 (no resync)  # noqa: E501
    # hwmon: 1 package + 6 cores = 7
    # Total: 4 + 3 + 66 + 9 + 7 = 89
    assert len(outcome.metrics) == EXPECTED_FULL_CAPTURE_METRICS
    assert outcome.up is True


# ============================================================================
# Coverage-gap tests
# ============================================================================


def test_synodisk_enum_missing_path_skipped() -> None:
    body = (
        "************ Disk Info ***************\n"
        ">> Disk id: 99\n"
        ">> Disk model: SomeModel\n"
        ">> Total capacity: 1000.00 GB\n"
        "************ Disk Info ***************\n"
        ">> Disk id: 1\n"
        ">> Disk path: /dev/sda\n"
        ">> Disk model: WD221KFGX-68B9KN0\n"
    )
    disks = parse_synodisk_enum(body)
    assert len(disks) == 1
    assert disks[0][0] == "sda"


def test_synodisk_enum_missing_model_uses_empty() -> None:
    body = (
        "************ Disk Info ***************\n"
        ">> Disk id: 1\n"
        ">> Disk path: /dev/sda\n"
        ">> Total capacity: 1000.00 GB\n"
    )
    disks = parse_synodisk_enum(body)
    assert len(disks) == 1
    assert disks[0][1] == ""


def test_smart_block_empty_chunk_skipped() -> None:
    body = "---------------------\n---------------------\n"
    metrics = parse_smart_block("empty_disk", body)
    failing = _by_name(metrics, M_SMART_ATTR_FAILING)
    assert len(failing) == 1
    assert failing[0].value == 0.0


def test_smart_block_no_id_chunk_skipped() -> None:
    body = (
        "Name: Orphan_No_Id\n"
        "Current: 100\n"
        "Worst: 100\n"
        "Threshold: 000\n"
        "Raw: 0\n"
        "Status: OK\n"
        "---------------------\n"
    )
    metrics = parse_smart_block("no_id_disk", body)
    raw = [m for m in _by_name(metrics, M_SMART_ATTR_RAW) if m.labels.get("attr_id")]
    assert raw == []


def test_smart_block_nonnumeric_current_threshold() -> None:
    body = (
        "Name: Reallocated_Sector_Ct\n"
        "Id: 5\n"
        "Current: N/A\n"
        "Worst: 100\n"
        "Threshold: N/A\n"
        "Raw: 0\n"
        "Status: OK\n"
        "---------------------\n"
    )
    metrics = parse_smart_block("weird_disk", body)
    failing = _by_name(metrics, M_SMART_ATTR_FAILING)
    assert len(failing) == 1
    assert failing[0].value == 0.0


def test_smart_block_missing_worst_or_threshold() -> None:
    body = (
        "Name: Power_On_Hours\n"
        "Id: 9\n"
        "Current: 098\n"
        "Worst: \n"
        "Threshold: \n"
        "Raw: 100\n"
        "Status: OK\n"
        "---------------------\n"
    )
    metrics = parse_smart_block("partial_disk", body)
    raw_9 = [m for m in _by_name(metrics, M_SMART_ATTR_RAW) if m.labels.get("attr_id") == "9"]
    assert len(raw_9) == 1
    assert _by_name(metrics, M_SMART_ATTR_WORST) == [] or all(
        m.labels.get("attr_id") != "9" for m in _by_name(metrics, M_SMART_ATTR_WORST)
    )


def test_mdstat_no_bracket_not_faulty_healthy() -> None:
    body = (
        "Personalities : [raid1]\n"
        "md11 : active raid1 sda1[0]\n"
        "      8388544 blocks\n"
        "\n"
        "unused devices: <none>\n"
    )
    metrics = parse_mdstat(body)
    deg = [m for m in _by_name(metrics, M_MDSTAT_ARRAY_DEGRADED) if m.labels.get("array") == "md11"]
    assert len(deg) == 1
    assert deg[0].value == 0.0


def test_split_sections_hm_end_terminates() -> None:
    text = "===HM_UPTIME===\nsome line\n===HM_END===\n===HM_HWMON===\nignored\n"
    sections = split_sections(text)
    assert "HM_UPTIME" in sections
    assert "HM_HWMON" not in sections
    assert "HM_END" not in sections


def test_split_sections_no_hm_end_finalizes_last_section() -> None:
    text = "===HM_UPTIME===\nsome line\n===HM_HWMON===\nhwmon data"
    sections = split_sections(text)
    assert "HM_UPTIME" in sections
    assert sections["HM_UPTIME"] == "some line"
    assert "HM_HWMON" in sections
    assert sections["HM_HWMON"] == "hwmon data"


def test_smart_block_nonnumeric_id_skipped() -> None:
    body = (
        "Name: Weird_Attr\n"
        "Id: notanumber\n"
        "Current: 100\n"
        "Worst: 100\n"
        "Threshold: 000\n"
        "Raw: 0\n"
        "Status: OK\n"
        "---------------------\n"
    )
    metrics = parse_smart_block("weird_disk", body)
    assert _by_name(metrics, M_SMART_ATTR_RAW) == []
    failing = _by_name(metrics, M_SMART_ATTR_FAILING)
    assert failing[0].value == 0.0


def test_smart_block_nonnumeric_worst_threshold_skipped() -> None:
    body = (
        "Name: Power_On_Hours\n"
        "Id: 9\n"
        "Current: 098\n"
        "Worst: N/A\n"
        "Threshold: N/A\n"
        "Raw: 100\n"
        "Status: OK\n"
        "---------------------\n"
    )
    metrics = parse_smart_block("bad_worst_disk", body)
    raw_9 = [m for m in _by_name(metrics, M_SMART_ATTR_RAW) if m.labels.get("attr_id") == "9"]
    assert len(raw_9) == 1
    assert _by_name(metrics, M_SMART_ATTR_WORST) == []
    assert _by_name(metrics, M_SMART_ATTR_THRESHOLD) == []


def test_raw_empty_string_returns_none() -> None:
    """Empty string raw value returns None."""
    assert parse_raw("") is None
    assert parse_raw("   ") is None


def test_smart_block_blank_raw_skipped() -> None:
    """Whitelisted attr (Id 5) blank Raw -> raw metric skipped; worst/threshold emitted."""
    body = (
        "Name: Reallocated_Sector_Ct\n"
        "Id: 5\n"
        "Current: 100\n"
        "Worst: 100\n"
        "Threshold: 016\n"
        "Raw: \n"
        "Status: OK\n"
        "---------------------\n"
    )
    metrics = parse_smart_block("blank_raw_disk", body)
    raw_5 = [m for m in _by_name(metrics, M_SMART_ATTR_RAW) if m.labels.get("attr_id") == "5"]
    assert raw_5 == []

    # worst and threshold should still be emitted
    worst_5 = [m for m in _by_name(metrics, M_SMART_ATTR_WORST) if m.labels.get("attr_id") == "5"]
    assert len(worst_5) == 1
    assert worst_5[0].value == 100.0  # noqa: PLR2004

    threshold_5 = [
        m for m in _by_name(metrics, M_SMART_ATTR_THRESHOLD) if m.labels.get("attr_id") == "5"
    ]
    assert len(threshold_5) == 1
    assert threshold_5[0].value == 16.0  # noqa: PLR2004

    # failing should be present and 0
    failing = _by_name(metrics, M_SMART_ATTR_FAILING)
    assert len(failing) == 1
    assert failing[0].value == 0.0
