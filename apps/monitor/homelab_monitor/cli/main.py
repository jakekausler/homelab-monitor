"""Main CLI entry point."""

from homelab_monitor import __version__


def main() -> None:
    """Print the homelab-monitor version and exit."""
    print(f"homelab-monitor {__version__}")
