"""Guards for the canonical Synology probe script (STAGE-008-026).

Covers:
- deploy/ssh-probes/hm-probe-synology.sh passes `sh -n` (syntax check) — catches the
  CRLF / broken-loop corruption failure mode that broke the live probe.
- The script is LF-only (no carriage returns).
- The script contains every ===HM_*=== marker the SynologyProbe parser consumes, so it
  stays in sync with apps/.../plugins/collectors/ssh/synology.py.
- The constant embedded in cli/ssh_probe.py (_SYNOLOGY_PROBE_SCRIPT) is byte-for-byte
  identical to the deploy file, enforcing a single source of truth.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Final

from homelab_monitor.cli.ssh_probe import (
    _SYNOLOGY_PROBE_SCRIPT,  # pyright: ignore[reportPrivateUsage]
)

# tests/ssh/ -> tests/ -> apps/monitor/ -> apps/ -> repo root
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[4]
_SCRIPT_PATH: Final[Path] = _REPO_ROOT / "deploy" / "ssh-probes" / "hm-probe-synology.sh"

# Markers the SynologyProbe parser splits on (see split_sections + section dispatch).
_REQUIRED_MARKERS: Final[tuple[str, ...]] = (
    "===HM_UPTIME===",
    "===HM_DF===",
    "===HM_SYNODISK_ENUM===",
    "===HM_SMART /dev/sd",
    "===HM_MDSTAT===",
    "===HM_UPSC===",
    "===HM_HWMON===",
    "===HM_END===",
)


def test_script_file_exists() -> None:
    """The canonical deploy script exists at the expected path."""
    assert _SCRIPT_PATH.is_file()


def test_script_passes_sh_n_syntax_check() -> None:
    """`sh -n` accepts the script — catches the broken-loop / corruption failure mode."""
    result = subprocess.run(
        ["sh", "-n", str(_SCRIPT_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"sh -n failed:\n{result.stderr}"


def test_script_is_lf_only() -> None:
    """The script must be LF-only; a stray CR is exactly the live-corruption symptom."""
    raw = _SCRIPT_PATH.read_bytes()
    assert b"\r" not in raw


def test_script_has_all_required_markers() -> None:
    """Every marker the parser consumes is present, keeping script + parser in sync."""
    text = _SCRIPT_PATH.read_text(encoding="utf-8")
    for marker in _REQUIRED_MARKERS:
        assert marker in text, f"missing marker {marker!r}"


def test_script_starts_with_shebang_and_exits_zero() -> None:
    """Shebang first line + explicit `exit 0` (honest-empty sections still exit 0)."""
    text = _SCRIPT_PATH.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n")
    assert "\nexit 0\n" in text


def test_embedded_constant_matches_deploy_file() -> None:
    """cli/ssh_probe.py's embedded copy is byte-for-byte the deploy file (one source of truth)."""
    file_text = _SCRIPT_PATH.read_text(encoding="utf-8")
    assert file_text == _SYNOLOGY_PROBE_SCRIPT
