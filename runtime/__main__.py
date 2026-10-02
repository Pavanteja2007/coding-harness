"""Expose the installed Neo runtime library through ``python -m runtime``."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Show runtime help and direct users to the Neo CLI for task execution."""
    parser = argparse.ArgumentParser(
        prog="python -m runtime",
        description=(
            "Neo runtime library for scheduler, workers, checkpoints, and routing. "
            "Use the neo command for user-facing task execution."
        ),
    )
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
