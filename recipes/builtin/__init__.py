"""Bundled portable recipe documents."""

from pathlib import Path

__all__ = ["builtin_root"]


def builtin_root() -> Path:
    """Return the contained directory containing bundled recipe documents."""
    return Path(__file__).parent
