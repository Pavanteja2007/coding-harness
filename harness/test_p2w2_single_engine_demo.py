"""T1.W2.3 regression demonstration -- a SECOND loop, added on purpose, removed.

Run this file directly for the transcript:

    python -m harness.test_p2w2_single_engine_demo

It writes a temporary ``harness/_w2_second_loop.py`` containing a second
model-call <-> tool-execution loop, runs the single-engine pin, shows it FAIL,
removes the file, and shows it PASS again. Nothing outside the repository is
touched and the file is removed even if the pin raises.
"""

from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
BAD_SITE = os.path.join(REPO_ROOT, "harness", "_w2_second_loop.py")

_BAD_SOURCE = '''\
"""A SECOND agent engine. This file is a demonstration, not a module.

It exists only while ``harness/test_p2w2_single_engine_demo`` runs, to prove the
single-engine pin in ``harness/test_p2w2_vendor_provenance.py`` actually fires.
The next round will want to add something very much like this; the pin is why
that has to be a decision rather than a code-review comment.
"""

from __future__ import annotations


def run_second_agent_loop(history, boundary, config):
    """Iterate model-call <-> tool-execution, independently of step()."""
    for _ in range(int(config.get("agent_max_turns", 60))):
        reply = boundary.call(history)
        call = parse_tool_call(reply)
        if call is None:
            return history
        history = boundary.dispatch(history, call)
    return history


def parse_tool_call(reply):
    """Return a parsed tool call, or None. Nothing clever."""
    return None
'''

_PIN = (
    "harness/test_p2w2_vendor_provenance.py::TestTheSingleEngineInvariant::"
    "test_there_is_exactly_one_agent_loop_across_the_harness"
)


def _run_pin() -> tuple:
    """Run the single-engine pin. Returns ``(returncode, tail_of_output)``."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", _PIN, "-q", "-p", "no:randomly"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    output = (result.stdout + result.stderr).splitlines()
    return result.returncode, "\n".join(output[-24:])


def main() -> int:
    """Add a second loop, show the pin fail, remove it, show it pass."""
    print("=" * 78)
    print("T1.W2.3 -- single-engine invariant, demonstrated")
    print("=" * 78)

    assert not os.path.exists(BAD_SITE), (
        f"{BAD_SITE} already exists; refusing to overwrite it"
    )

    print("\n--- 1. BASELINE: one engine, pin green -------------------------------")
    code, tail = _run_pin()
    print(tail)
    assert code == 0, "the pin is RED on a clean tree; the demonstration is meaningless"

    print("\n--- 2. REGRESSION: a second model<->tool loop added -----------------")
    with open(BAD_SITE, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(_BAD_SOURCE)
    try:
        code, tail = _run_pin()
        print(tail)
        assert code != 0, (
            "THE PIN DID NOT FIRE. A second agent loop was added to harness/ and "
            "the invariant still passed, so the pin is decoration."
        )
        print(">>> the pin FIRED. The second engine is caught, not reviewed.")
    finally:
        if os.path.exists(BAD_SITE):
            os.remove(BAD_SITE)
            print(f"\n(removed {os.path.relpath(BAD_SITE, REPO_ROOT)})")

    print("\n--- 3. AFTER REMOVAL: pin green again -------------------------------")
    code, tail = _run_pin()
    print(tail)
    assert code == 0, "the pin stayed red after the regression was removed"
    print(">>> the pin is GREEN again. The invariant holds, and is now testable.")

    print("\n" + "=" * 78)
    print("VERDICT: the single-engine invariant is pinned, and the pin is real.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
