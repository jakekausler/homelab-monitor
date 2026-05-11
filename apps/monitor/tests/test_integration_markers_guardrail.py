"""Guardrail: every test_* function under tests/integration/ MUST carry @pytest.mark.integration.

This guards against accidental drift: with the pyproject.toml change in
STAGE-001-021 Spec A from `--ignore=tests/integration` to `-m 'not integration'`,
any unmarked test in tests/integration/ would silently RUN during the default
unit-test pass and fail with `httpx.ConnectError` against a non-existent
docker rig. Catch that at the AST level rather than waiting for a confused
contributor to debug it in CI.

Implementation: walk the tests/integration/ tree, AST-parse each test_*.py
file, find every top-level `def test_*` function, and assert at least one
decorator on it resolves to `pytest.mark.integration` (either directly or via
`@pytest.mark.integration(...)` -- both forms are accepted as the same logical
marker).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

INTEGRATION_DIR = Path(__file__).resolve().parent / "integration"
EXEMPT_FILES: set[str] = {
    # helpers/ subdirectory contains uvicorn-runnable modules and helper
    # functions whose `test_*` prefixed members are not pytest tests.
    "test_webhook_server.py",
}


def _is_integration_marker(decorator: ast.expr) -> bool:
    """Return True if `decorator` is `@pytest.mark.integration` (with or without call)."""
    # @pytest.mark.integration  -- ast.Attribute(Attribute(Name("pytest"), "mark"), "integration")
    # @pytest.mark.integration(...) -- ast.Call(func=<Attribute as above>)
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if not isinstance(target, ast.Attribute):
        return False
    if target.attr != "integration":
        return False
    inner = target.value
    if not isinstance(inner, ast.Attribute) or inner.attr != "mark":
        return False
    base = inner.value
    return isinstance(base, ast.Name) and base.id == "pytest"


def _iter_integration_test_files() -> list[Path]:
    """Yield every test_*.py file directly under tests/integration/ (not helpers/)."""
    files: list[Path] = []
    for p in INTEGRATION_DIR.glob("test_*.py"):
        if p.name in EXEMPT_FILES:
            continue
        files.append(p)
    # Also include test_*.py in subdirs? Currently there are none, but be
    # defensive against future expansion.
    for sub in INTEGRATION_DIR.iterdir():
        if not sub.is_dir() or sub.name == "__pycache__":
            continue
        for p in sub.glob("test_*.py"):
            if p.name in EXEMPT_FILES:
                continue
            files.append(p)
    return files


def test_integration_dir_exists() -> None:
    """Sanity: tests/integration/ exists at the expected path."""
    assert INTEGRATION_DIR.is_dir(), f"{INTEGRATION_DIR} not found"


def test_at_least_one_integration_test_file() -> None:
    """Sanity: we discover at least one test file (drift detector for path changes)."""
    files = _iter_integration_test_files()
    assert files, "no test_*.py files found under tests/integration/"


@pytest.mark.parametrize("path", _iter_integration_test_files(), ids=lambda p: p.name)
def test_every_test_function_has_integration_marker(path: Path) -> None:
    """Every `def test_*` in `path` must be decorated with `@pytest.mark.integration`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    failures: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue
        # Skip pytest fixtures whose names happen to start with test_.
        if any(
            (isinstance(d, ast.Attribute) and d.attr == "fixture")
            or (
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr == "fixture"
            )
            for d in node.decorator_list
        ):
            continue
        if not any(_is_integration_marker(d) for d in node.decorator_list):
            failures.append(f"{path.name}::{node.name} (line {node.lineno})")
    assert not failures, (
        "the following integration tests are MISSING @pytest.mark.integration "
        "(would silently run + fail in default `pytest` invocation): " + ", ".join(failures)
    )
