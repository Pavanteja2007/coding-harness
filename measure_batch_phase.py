"""Task B measurement (phase-isolated): time the READ PHASE ONLY —
8 independent read-only ops, serial (8 sandbox calls, 8 model turns)
vs BATCH (8 sandbox calls in one turn, 2 model turns), real Docker
sandbox, no verify noise. This isolates exactly what BATCH changes."""

import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from harness.tools import run_batch  # noqa: E402
from harness.deps import reset_overrides, set_call_model  # noqa: E402
from harness import tools as tool_mod  # noqa: E402
from shared.types import Task  # noqa: E402

REPO = (REPO_ROOT / "tests" / "fixtures" / "bug02_mean").resolve()

READS = [
    "cat numlib/mathutil.py",
    "cat tests/test_mathutil.py",
    "cat pyproject.toml",
    "ls numlib",
    "ls tests",
    "grep -n def numlib/mathutil.py",
    "grep -n assert tests/test_mathutil.py",
    "find . -name '*.py'",
]

# --- serial: 8 BashSession.run calls (8 sandbox round-trips) ---------
session = tool_mod.BashSession(str(REPO), 60, 3000)
t0 = time.time()
for cmd in READS:
    session.run(cmd)
serial_read_s = time.time() - t0

# --- batch: one run_batch call (8 concurrent sandbox round-trips) ----
t0 = time.time()
records, _ = run_batch(str(REPO), READS, 60, 3000, max_workers=4)
batch_read_s = time.time() - t0

ok_serial = len(session.commands) == 8
ok_batch = len(records) == 8 and all(r.get("exit_code") == 0 for r in records)

print(
    json.dumps(
        {
            "operations": len(READS),
            "serial_read_phase_s": round(serial_read_s, 2),
            "batch_read_phase_s": round(batch_read_s, 2),
            "saved_s": round(serial_read_s - batch_read_s, 2),
            "read_phase_speedup": round(serial_read_s / batch_read_s, 2),
            "serial_all_ok": ok_serial,
            "batch_all_ok": ok_batch,
        },
        indent=2,
    )
)
