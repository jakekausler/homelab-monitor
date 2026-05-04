"""Meta-tests verifying the lint + type-check pipeline rejects bad inputs.

These tests do not execute production code paths — they spawn `ruff` and
`pyright` against synthetic broken inputs and assert that those tools
exit non-zero. Excluded from coverage because tests live outside the
``[tool.coverage.run] source`` package, not via ``omit``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def test_ruff_rejects_unused_import(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("import os\nx: int = 1\n")
    result = subprocess.run(
        ["uv", "run", "ruff", "check", "--quiet", "--select", "F401", str(bad)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, "ruff should flag unused import"


def test_pyright_rejects_type_mismatch(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("x: int = 'not an int'\n")
    result = subprocess.run(
        ["uv", "run", "pyright", str(bad)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, "pyright should flag type mismatch"
