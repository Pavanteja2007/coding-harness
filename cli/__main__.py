"""Allow ``python -m cli`` alongside the ``harness`` console script."""
import sys

from cli.main import main

if __name__ == "__main__":
    sys.exit(main())
