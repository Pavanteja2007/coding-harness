"""One-shot setup for the live scripted TUI demo (no API key needed):
copies tests/fixtures/bug02_mean to a temp dir and writes the
scripted-model spec the harness's documented test hook consumes
(HARNESS_SCRIPTED_MODEL). Prints the two env-var lines to paste.
"""

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = Path(r"C:\Users\pavan\AppData\Local\Temp\neo-live-demo")
REPO = BASE / "repo"
SPEC = BASE / "model_spec.json"

if REPO.exists():
    shutil.rmtree(REPO)
BASE.mkdir(parents=True, exist_ok=True)
shutil.copytree(ROOT / "tests" / "fixtures" / "bug02_mean", REPO)
shutil.rmtree(REPO / ".harness", ignore_errors=True)
shutil.rmtree(REPO / ".pytest_cache", ignore_errors=True)

spec = {
    "plan": [
        {
            "id": 1,
            "description": "inspect mean() and its divisor",
            "checkpoint": "understand the off-by-one",
            "files_hint": ["numlib/mathutil.py"],
        },
        {
            "id": 2,
            "description": "fix the divisor to len(values) and verify",
            "checkpoint": "tests/test_mathutil.py passes",
            "files_hint": ["numlib/mathutil.py"],
        },
    ],
    "scripts": {
        "1": [["cat numlib/mathutil.py", "SUBMIT"]],
        "2": [
            [
                (
                    'python -c "import pathlib; '
                    "p = pathlib.Path('numlib/mathutil.py'); "
                    "s = p.read_text(); "
                    "s = s.replace('len(values) - 1', 'len(values)'); "
                    'p.write_text(s)"'
                ),
                "python -m pytest -q tests/test_mathutil.py",
                "SUBMIT",
            ]
        ],
    },
}
SPEC.write_text(json.dumps(spec, indent=2), encoding="utf-8")

print(f"repo ready: {REPO}")
print(f"spec ready: {SPEC}")
print()
print("Run in the repo dir (PowerShell):")
print(f'  $env:HARNESS_SCRIPTED_MODEL = "{SPEC}"')
print(f'  $env:HARNESS_LOGS_DIR = "{BASE / "logs"}"')
print("  neo")
print()
print("Then in the TUI:")
print("  repo " + str(REPO))
print("  mean() in numlib/mathutil.py divides by len-1 instead of len; fix it")
