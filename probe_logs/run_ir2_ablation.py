"""Improvement Round 2 three-arm ablation driver (2026-09-10).

Runs the three arms SEQUENTIALLY as separate `python -m runtime.ablation`
invocations sharing one --out (the runner's merge behavior accumulates
arms into summary.json + arm_runs provenance — verified Round 4). Per-arm
invocation gives per-arm crash resilience: a dead arm doesn't take the
others' data with it.

Arms: off (always-expensive baseline) -> on (single-attempt adaptive,
unchanged) -> ensemble (multi-candidate). Same task set (--tasks all,
16 bugs) and defaults as the v4 run for comparability.

Detached invocation (PowerShell):
  Start-Process -WindowStyle Hidden python -ArgumentList `
    'probe_logs/run_ir2_ablation.py' -RedirectStandardOutput `
    'probe_logs/ir2-driver.log' -RedirectStandardError 'probe_logs/ir2-driver.err'
"""

import subprocess
import sys
import time
from pathlib import Path

OUT = "logs/ablations/ir2-final"
ARMS = ("off", "on", "ensemble")

log = Path("probe_logs/ir2-driver.log")
log.parent.mkdir(exist_ok=True)


def main() -> int:
    rc = 0
    for arm in ARMS:
        t0 = time.time()
        cmd = [
            sys.executable,
            "-m",
            "runtime.ablation",
            "--tasks",
            "all",
            "--arm",
            arm,
            "--out",
            OUT,
        ]
        with open(log, "a", encoding="utf-8") as f:
            f.write(
                f"\n=== arm={arm} start {time.strftime('%H:%M:%S')} "
                f"cmd={' '.join(cmd)}\n"
            )
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        with open(log, "a", encoding="utf-8") as f:
            f.write(r.stdout)
            if r.stderr:
                f.write("\n[stderr]\n" + r.stderr)
            f.write(
                f"\n=== arm={arm} end rc={r.returncode} wall={time.time() - t0:.0f}s\n"
            )
        print(
            f"[driver] arm={arm} rc={r.returncode} wall={time.time() - t0:.0f}s",
            flush=True,
        )
        if r.returncode != 0:
            rc = r.returncode
            break  # later arms would share whatever just broke; keep data
    print(f"[driver] done rc={rc}; summary: {OUT}/summary.json", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
