"""Smoke tests: verify package is importable and CLI prints version."""

from pytest import CaptureFixture

from homelab_monitor import __version__
from homelab_monitor.cli.main import main


def test_version() -> None:
    """Package version string must be 0.0.0."""
    assert __version__ == "0.0.0"


def test_main_prints_version(capsys: CaptureFixture[str]) -> None:
    """CLI main() must print a line ending with the version."""
    main()
    captured = capsys.readouterr()
    assert captured.out.strip().endswith("0.0.0")
