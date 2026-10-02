"""Record a real `neo fix` run (scripted model, REAL Docker sandbox +
verifier + git output) on the smoke_repo fixture, and render the
console transcript into demo/neo-demo.gif — the README hero demo.

Everything the run does is the real product path: real run_task loop,
real Docker-sandboxed command execution, real verify (target +
regression + flake), real git-native output. The MODEL is the offline
ScriptedDemoModel (the same pattern as demo/run_demo.py) so the
recording is deterministic and needs no API key.

Usage:  python scripts/make_demo_gif.py
Output: demo/neo-demo.gif (+ a transcript .txt next to it for review)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

OUT_DIR = REPO_ROOT / "demo"
TMP = Path(os.environ.get("TEMP", REPO_ROOT)) / "opencode" / "neo-demo-gif"
TMP.mkdir(parents=True, exist_ok=True)

ISSUE = (
    "mean() in mathutil.py returns the sum instead of the arithmetic "
    "mean. tests/test_mathutil.py::test_mean fails; fix mathutil.py so "
    "it passes."
)

FIX_COMMAND = (
    r"sed -i 's/    return sum(values)/    return sum(values) \/ len(values)/' "
    "mathutil.py"
)


def main() -> int:
    # --- 1) run the REAL `neo fix` loop on a scratch copy of smoke_repo ----
    repo = TMP / "repo"
    if repo.exists():
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(repo)], check=False)
    subprocess.run(
        [
            "robocopy",
            str(REPO_ROOT / "cli" / "fixtures" / "smoke_repo"),
            str(repo),
            "/E",
        ],
        check=False,
    )

    logs = TMP / "logs"
    if logs.exists():
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(logs)], check=False)

    spec = {
        "model": "neo-demo-model",
        "plan": [
            {
                "id": 1,
                "description": "fix mean() in mathutil.py",
                "checkpoint": "test_mean passes",
            },
            {
                "id": 2,
                "description": "confirm the full suite is green",
                "checkpoint": "no regressions",
            },
        ],
        "scripts": {
            # step 1, attempt 1: diagnose, then fix, then SUBMIT
            "1": [["cat mathutil.py", FIX_COMMAND, "SUBMIT"]],
            # step 2: nothing left to do
            "2": [["SUBMIT"]],
        },
    }
    spec_file = TMP / "scripted_model.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")

    env = dict(os.environ)
    env["HARNESS_SCRIPTED_MODEL"] = str(spec_file)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("NO_COLOR", None)
    env["FORCE_COLOR"] = "0"  # plain text — the GIF renderer adds its own color

    cp = subprocess.run(
        [
            sys.executable,
            "-m",
            "cli",
            "fix",
            "--repo",
            str(repo),
            "--issue",
            ISSUE,
            "--task-id",
            "demo-hero",
            "--log-root",
            str(logs),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO_ROOT),
        env=env,
        timeout=900,
    )
    if cp.returncode != 0:
        print("neo fix FAILED rc=", cp.returncode)
        print(cp.stdout)
        print(cp.stderr)
        return 1

    (OUT_DIR / "neo-demo-transcript.txt").write_text(cp.stdout, encoding="utf-8")
    print("run ok; transcript captured:", len(cp.stdout), "chars")
    print("out:", OUT_DIR / "neo-demo-transcript.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
