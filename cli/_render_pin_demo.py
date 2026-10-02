"""P0/W2 T4.W2.2 — the AST-pin demonstration, runnable as a script.

    python cli/_render_pin_demo.py

Adds a deliberately-raw render site to a temporary module inside `cli/`, runs
`cli/test_render_path_pin.py`'s OWN gate against it, removes it, and runs the
gate again. Both transcripts are printed. The probe module is removed in a
`finally`, so an interrupted run cannot leave a raw render site behind in a
directory-driven pin — which would be an unpleasant surprise for whoever runs
the suite next.

This is a DEMONSTRATION, not a test. The durable version is
`cli/test_render_path_pin.py::test_the_pin_fails_on_a_deliberately_raw_render_site`,
which does the same thing inside a `finally`; this script exists so the
before/after can be read in one place without reading the test's docstring.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parent
PROBE = CLI_DIR / "_render_pin_probe.py"

RAW_SOURCE = '''"""TEMPORARY probe for the render-path pin demonstration."""

from cli import interactive
from cli import ui as _ui
from cli.runview import failure_lines


def render_untrusted_row(row):
    """A render path that skips the sanitiser, on purpose.

    `row` here is a journal row: its `detail` is untrusted content, and this
    function hands it straight to a render sink. That is the exact shape of the
    bug the pin exists to catch.
    """
    journal_value = row.get("detail") or ""
    interactive.print(f"  {journal_value}")
    _ = failure_lines(journal_value)
    return journal_value
'''

CLEAN_SOURCE = '''"""TEMPORARY positive control for the render-path pin demonstration."""

from cli import ui as _ui


def render_untrusted_row(row):
    """The same row, routed through the sanitiser."""
    journal_value = _ui.sanitize_text(row.get("detail") or "")
    print(f"  {journal_value}")
    return journal_value
'''


def _load_pin():
    spec = importlib.util.spec_from_file_location(
        "cli_render_path_pin", CLI_DIR / "test_render_path_pin.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["cli_render_path_pin"] = module
    spec.loader.exec_module(module)
    return module


def _report(pin, label: str) -> int:
    undeclared = sorted(
        {
            (site.module, site.function)
            for site in pin.render_sites()
            if pin.classify(site) == "declared"
        }
    )
    unknown = [key for key in undeclared if key not in pin.DECLARED_RENDER_FUNCTIONS]
    print(f"--- {label}")
    print(f"    unclassified render sites : {len(undeclared)}")
    print(f"    without an allowlist row : {len(unknown)}")
    for module, function in unknown:
        print(f"        UNCLASSIFIED  {module}:{function}")
    return len(unknown)


def main() -> int:
    pin = _load_pin()
    if PROBE.exists():
        PROBE.unlink()
    try:
        baseline = _report(pin, "1. baseline, no probe module present")
        print()

        PROBE.write_text(RAW_SOURCE, encoding="utf-8")
        raw = _report(pin, "2. a deliberately-raw render site is present")
        print()

        PROBE.write_text(CLEAN_SOURCE, encoding="utf-8")
        clean = _report(pin, "3. the same row, routed through the sanitiser")
        print()
    finally:
        PROBE.unlink(missing_ok=True)

    after = _report(pin, "4. the probe module is removed")
    print()
    print("verdict:")
    print(f"    baseline unclassified-without-a-row : {baseline}  (expected 0)")
    print(f"    raw probe unclassified-without-a-row : {raw}  (expected 1)")
    print(f"    sanitised probe                      : {clean}  (expected 0)")
    print(f"    after removal                        : {after}  (expected 0)")
    ok = baseline == 0 and raw == 1 and clean == 0 and after == 0
    print(f"    the pin {'CAUGHT' if ok else 'DID NOT CATCH'} the raw render site")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
