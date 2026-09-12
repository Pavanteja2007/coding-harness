"""Allow ``python -m cli`` alongside the ``vex`` console script."""

import sys


def _run() -> int:
    try:
        from cli.main import main
    except ImportError as exc:
        # Broken install / missing dependency (Task D): plain language, no
        # traceback. This is the FIRST import of the package a user can
        # hit, so an install problem lands exactly here.
        print(
            f"error: the Vex CLI could not be imported: {exc}\n"
            'check: was the package installed? Run:  pip install -e ".[dev]"\n'
            "check: are you in the right environment (venv active)?",
            file=sys.stderr,
        )
        return 2
    return main()


if __name__ == "__main__":
    sys.exit(_run())
