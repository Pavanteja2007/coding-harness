"""Expose the installed Neo execution library through ``python -m execution``."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Show execution help and direct users to the Neo CLI for sandboxed fixes."""
    parser = argparse.ArgumentParser(
        prog="python -m execution",
        description=(
            "Neo execution library for Docker sandboxing, verification, and git output. "
            "Use the neo command for user-facing task execution."
        ),
    )
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
