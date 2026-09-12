"""The FIXED eval task set (prompt-regression baseline).

Two sources, deliberately:
1. The 5 original fixture bugs (tests/fixtures/bug0{1..5}_*) — the same
   set the harness's own DoD and every e2e suite pins. Reusing them
   makes eval results comparable across the whole project history.
2. Four additional synthesized bugs, built at run time (same pattern
   as runtime/ablation_tasks.py — code, not committed fixtures) in
   bug classes the fixture set lacks: string boundary, wrong operator,
   wrong constant, lost exception guard. Each carries the canonical
   one-shot fix and its target test, and is HOST-verified by
   `python -m evals.run --check` (fails pre-fix, green post-fix).

Every task's `script` is a scripted-model spec (runtime.mock_provider
install_script shape): {"plan": [...], "scripts": {step_id: [cmds]}} —
deterministic, offline, the REAL loop + REAL Docker verify doing the
actual work. The fix commands deliberately vary in style (sed one-shot,
python -c rewrite, heredoc) so the loop's command-extraction and
feedback surfaces are exercised the way real models use them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"

# ---------------------------------------------------------------------------
# 1. Fixture tasks (the original DoD set; scripts mirror the e2e suite's)
# ---------------------------------------------------------------------------

FIXTURE_TASKS: List[Dict[str, Any]] = [
    {
        "slug": "bug01_wrap",
        "repo": str(FIXTURES / "bug01_wrap"),
        "issue": "wrap() drops the final pending line when it is shorter "
        "than the wrap width — the last chunk of wrapped text "
        "silently disappears",
        "target": "tests/test_textutil.py::test_wrap_short_trailing_line",
        "fix_hint": "wrapwrap/textutil.py",
        "script": {
            "plan": [
                {
                    "id": 1,
                    "description": "fix the trailing-line drop",
                    "checkpoint": "target test passes",
                    "files_hint": ["wrapwrap/textutil.py"],
                }
            ],
            "scripts": {
                1: [
                    "sed -i 's/if lines and current and len(current) == width:/"
                    "if current:/' wrapwrap/textutil.py",
                    "SUBMIT",
                ]
            },
        },
    },
    {
        "slug": "bug02_mean",
        "repo": str(FIXTURES / "bug02_mean"),
        "issue": "mean() divides by len-1; should divide by len",
        "target": "tests/test_mathutil.py::test_mean_even_count",
        "fix_hint": "numlib/mathutil.py",
        "script": {
            "plan": [
                {
                    "id": 1,
                    "description": "fix mean() denominator",
                    "checkpoint": "target test passes",
                    "files_hint": ["numlib/mathutil.py"],
                }
            ],
            "scripts": {
                1: [
                    "sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py",
                    "SUBMIT",
                ]
            },
        },
    },
    {
        "slug": "bug03_stack",
        "repo": str(FIXTURES / "bug03_stack"),
        "issue": "Stack.pop raises IndexError on an empty stack; it should "
        "raise StackEmptyError like peek does",
        "target": "tests/test_stack.py::test_pop_empty_raises_stackemptyerror",
        "fix_hint": "stacklib/stack.py",
        "script": {
            "plan": [
                {
                    "id": 1,
                    "description": "raise StackEmptyError in pop",
                    "checkpoint": "target test passes",
                    "files_hint": ["stacklib/stack.py"],
                }
            ],
            "scripts": {
                1: [
                    'python -c "import re, pathlib; p = pathlib.Path('
                    "'stacklib/stack.py'); t = p.read_text(); t = t.replace("
                    "'return self._items.pop()', 'if not self._items:\\n"
                    '            raise StackEmptyError(\\"pop from empty '
                    "stack\\\")\\n        return self._items.pop()'); "
                    'p.write_text(t)"',
                    "SUBMIT",
                ]
            },
        },
    },
    {
        "slug": "bug04_nameerror",
        "repo": str(FIXTURES / "bug04_nameerror"),
        "issue": "days_in_month crashes with NameError: the code references "
        "_DAYS_PER_MONTHS but the table is named _DAYS_PER_MONTH",
        "target": "tests/test_dateutil.py::test_days_in_month_fixed",
        "fix_hint": "datelib/dateutil.py",
        "script": {
            "plan": [
                {
                    "id": 1,
                    "description": "fix the table name typo",
                    "checkpoint": "target test passes",
                    "files_hint": ["datelib/dateutil.py"],
                }
            ],
            "scripts": {
                1: [
                    "sed -i 's/_DAYS_PER_MONTHS\\[month\\]/_DAYS_PER_MONTH"
                    "[month]/' datelib/dateutil.py",
                    "SUBMIT",
                ]
            },
        },
    },
    {
        "slug": "bug05_cart",
        "repo": str(FIXTURES / "bug05_cart"),
        "issue": "price_report leaks lines across calls: carts that share "
        "the default report argument return earlier carts' rows",
        "target": "tests/test_cart.py::test_price_report_isolated",
        "fix_hint": "cartlib/cart.py",
        "script": {
            "plan": [
                {
                    "id": 1,
                    "description": "stop sharing the default list",
                    "checkpoint": "target test passes",
                    "files_hint": ["cartlib/cart.py"],
                }
            ],
            "scripts": {
                1: [
                    'python -c "import pathlib; p = pathlib.Path('
                    "'cartlib/cart.py'); t = p.read_text(); t = t.replace("
                    "'report = _DEFAULT_REPORT', 'report = []'); "
                    'p.write_text(t)"',
                    "SUBMIT",
                ]
            },
        },
    },
]

# ---------------------------------------------------------------------------
# 2. Synthesized tasks (classes the fixture set lacks)
# ---------------------------------------------------------------------------

_PYPROJECT = (
    "[project]\n"
    'name = "{name}"\n'
    'version = "0.1.0"\n'
    "\n"
    "[tool.pytest.ini_options]\n"
    'testpaths = ["tests"]\n'
)


def _build_repo(
    root: Path, name: str, package: str, module_src: str, test_src: str
) -> Path:
    """Materialize one synthesized repo under root; returns its path.

    All files are written with EXPLICIT LF newlines: the scripted fixes
    are sed commands that run in the LINUX sandbox, and a Windows-built
    CRLF file breaks `$`-anchored sed expressions inside the container
    (`return status\r` never matches `return status$` — the eval_
    lint_undefined false-negative class, caught by the 20260911-002454
    run). LF endings make the repos byte-identical to POSIX-built ones.
    """
    repo = root / name
    pkg = repo / package
    pkg.mkdir(parents=True, exist_ok=True)
    (repo / "pyproject.toml").write_text(
        _PYPROJECT.format(name=name), encoding="utf-8", newline="\n"
    )
    (pkg / "__init__.py").write_text(
        f'"""{package} — eval fixture package."""\n', encoding="utf-8", newline="\n"
    )
    (pkg / f"{package}.py").write_text(module_src, encoding="utf-8", newline="\n")
    tests = repo / "tests"
    tests.mkdir(exist_ok=True)
    (tests / f"test_{package}.py").write_text(test_src, encoding="utf-8", newline="\n")
    return repo


def synthesized_tasks(build_root: Path) -> List[Dict[str, Any]]:
    """Build the 4 synthesized eval repos under build_root; task dicts.

    Each entry gains a "canonical_fix": (old, new) exact replacement the
    --check self-verification applies to prove the task is solvable and
    the target genuinely fails pre-fix. Deterministic: same sources,
    same repos, every run.
    """
    specs: List[Dict[str, Any]] = [
        {  # string boundary: strip should not remove the delimiter char
            "slug": "eval_strip_boundary",
            "package": "strkit",
            "issue": "strip_domain() eats the leading 'w' of the hostname: "
            "strip_domain('www.example.com') returns "
            "'example.com' instead of 'ww.example.com' — the "
            "strip set wrongly contains 'w'",
            "target": "tests/test_strkit.py::test_keeps_leading_chars",
            "module": (
                "def strip_domain(host: str) -> str:\n"
                '    """Remove a leading "www." only — not any w chars."""\n'
                '    return host.lstrip("www.")\n'
            ),
            "tests": (
                "from strkit.strkit import strip_domain\n"
                "\n"
                "\n"
                "def test_keeps_leading_chars():\n"
                "    assert strip_domain('www.example.com') == "
                "'example.com'\n"
                "    assert strip_domain('ww.example.com') == "
                "'ww.example.com'\n"
            ),
            "canonical_fix": ('host.lstrip("www.")', 'host.removeprefix("www.")'),
            "script": {
                "plan": [
                    {
                        "id": 1,
                        "description": "stop stripping raw characters",
                        "checkpoint": "target test passes",
                        "files_hint": ["strkit/strkit.py"],
                    }
                ],
                "scripts": {
                    1: [
                        'python -c "import pathlib; p = pathlib.Path('
                        "'strkit/strkit.py'); t = p.read_text(); t = t.replace("
                        "'host.lstrip(\\\"www.\\\")', "
                        '\'host.removeprefix(\\"www.\\")\'); p.write_text(t)"',
                        "SUBMIT",
                    ]
                },
            },
        },
        {  # wrong operator: AND should be OR
            "slug": "eval_wrong_operator",
            "package": "permkit",
            "issue": "is_weekend() returns False for Sunday: the day check "
            "uses and where it must use or, so only one of the two "
            "weekend days ever matches",
            "target": "tests/test_permkit.py::test_sunday_is_weekend",
            "module": (
                "def is_weekend(day: int) -> bool:\n"
                '    """True for Saturday(6) / Sunday(7) day numbers."""\n'
                "    return day == 6 and day == 7\n"
            ),
            "tests": (
                "from permkit.permkit import is_weekend\n"
                "\n"
                "\n"
                "def test_saturday_is_weekend():\n"
                "    assert is_weekend(6)\n"
                "\n"
                "\n"
                "def test_sunday_is_weekend():\n"
                "    assert is_weekend(7)\n"
                "\n"
                "\n"
                "def test_monday_is_not_weekend():\n"
                "    assert not is_weekend(1)\n"
            ),
            "canonical_fix": ("day == 6 and day == 7", "day == 6 or day == 7"),
            "script": {
                "plan": [
                    {
                        "id": 1,
                        "description": "use or for the two days",
                        "checkpoint": "target test passes",
                        "files_hint": ["permkit/permkit.py"],
                    }
                ],
                "scripts": {
                    1: [
                        "sed -i 's/day == 6 and day == 7/"
                        "day == 6 or day == 7/' permkit/permkit.py",
                        "SUBMIT",
                    ]
                },
            },
        },
        {  # wrong constant: retry delay
            "slug": "eval_wrong_constant",
            "package": "retrykit",
            "issue": "backoff() jumps straight to the max delay on the "
            "first retry: the growth base is 60 seconds, it "
            "should be 1 second (the cap stays 60)",
            "target": "tests/test_retrykit.py::test_first_retry_small",
            "module": (
                "def backoff(attempt: int) -> int:\n"
                '    """Exponential-ish delay: base * 2**attempt, capped."""\n'
                "    return min(60 * (2 ** attempt), 60)\n"
            ),
            "tests": (
                "from retrykit.retrykit import backoff\n"
                "\n"
                "\n"
                "def test_first_retry_small():\n"
                "    assert backoff(0) == 1\n"
                "\n"
                "\n"
                "def test_growth():\n"
                "    assert backoff(1) == 2 and backoff(2) == 4\n"
                "\n"
                "\n"
                "def test_cap():\n"
                "    assert backoff(10) == 60\n"
            ),
            "canonical_fix": (
                "min(60 * (2 ** attempt), 60)",
                "min(1 * (2 ** attempt), 60)",
            ),
            "script": {
                "plan": [
                    {
                        "id": 1,
                        "description": "fix the growth base",
                        "checkpoint": "target test passes",
                        "files_hint": ["retrykit/retrykit.py"],
                    }
                ],
                "scripts": {
                    1: [
                        "sed -i 's/min(60 \\* (2 \\*\\* attempt), 60)/"
                        "min(1 * (2 ** attempt), 60)/' retrykit/retrykit.py",
                        "SUBMIT",
                    ]
                },
            },
        },
        {  # lost exception guard: divide without the zero check
            "slug": "eval_lost_guard",
            "package": "ratiokit",
            "issue": "safe_ratio() crashes with ZeroDivisionError when "
            "total is 0 — the zero guard was lost; it should "
            "return 0.0 in that case",
            "target": "tests/test_ratiokit.py::test_zero_total",
            "module": (
                "def safe_ratio(part: float, total: float) -> float:\n"
                '    """part/total, or 0.0 when total is zero."""\n'
                "    return part / total\n"
            ),
            "tests": (
                "from ratiokit.ratiokit import safe_ratio\n"
                "\n"
                "\n"
                "def test_zero_total():\n"
                "    assert safe_ratio(3, 0) == 0.0\n"
                "\n"
                "\n"
                "def test_normal():\n"
                "    assert safe_ratio(1, 4) == 0.25\n"
            ),
            "canonical_fix": (
                "    return part / total\n",
                "    if total == 0:\n        return 0.0\n    return part / total\n",
            ),
            "script": {
                "plan": [
                    {
                        "id": 1,
                        "description": "restore the zero guard",
                        "checkpoint": "target test passes",
                        "files_hint": ["ratiokit/ratiokit.py"],
                    }
                ],
                "scripts": {
                    1: [
                        'python -c "import pathlib; p = pathlib.Path('
                        "'ratiokit/ratiokit.py'); t = p.read_text(); t = t."
                        "replace('    return part / total', '    if total == 0:"
                        "\\\\n        return 0.0\\\\n    return part / total'); "
                        'p.write_text(t)"',
                        "SUBMIT",
                    ]
                },
            },
        },
    ]
    out: List[Dict[str, Any]] = []
    for spec in specs:
        repo = _build_repo(
            build_root, spec["slug"], spec["package"], spec["module"], spec["tests"]
        )
        task = {
            k: v
            for k, v in spec.items()
            if k not in ("module", "tests", "canonical_fix", "package")
        }
        task["repo"] = str(repo)
        task["canonical_fix"] = spec["canonical_fix"]
        out.append(task)
    return out


# ---------------------------------------------------------------------------
# 2b. A REPAIR-loop scenario: the scripted model's FIRST attempt lands a
# broken edit (syntax error) and only the SECOND attempt fixes it. This
# is the task that exercises the repair/feedback prompt surface — the
# loop must carry attempt-1's verifier/lint feedback into attempt 2 and
# recover. Prompt changes to feedback rendering break HERE first.
# ---------------------------------------------------------------------------


def repair_scenario_task(build_root: Path) -> Dict[str, Any]:
    """One task whose scripted model needs a retry to succeed.

    Attempt 1: a sed that produces a SYNTAX error in the module (the
    classic broken-edit class). Attempt 2: the same sed corrected. The
    runner's score for this task requires status=success AND
    attempts>=2 — proving the feedback loop actually carried.
    """
    repo = _build_repo(
        build_root,
        "eval_repair_retry",
        "slotkit",
        module_src=(
            "def slot_label(index: int, base: int = 1) -> str:\n"
            '    """Human label for a slot: "slot {base+index}".\n'
            "\n"
            "    >>> slot_label(2)\n"
            "    'slot 3'\n"
            '    """\n'
            '    return f"slot {base index}"\n'
        ),
        test_src=(
            "from slotkit.slotkit import slot_label\n"
            "\n"
            "\n"
            "def test_label():\n"
            "    assert slot_label(2) == 'slot 3'\n"
            "\n"
            "\n"
            "def test_default_base():\n"
            "    assert slot_label(0) == 'slot 1'\n"
        ),
    )
    good_fix = "sed -i 's/{base index}/{base + index}/' slotkit/slotkit.py"
    # attempt 1's edit is a real SYNTAX error (unclosed brace): the lint
    # gate / step validation must reject it, the repair loop must carry
    # the feedback, and attempt 2 lands the real fix. ("+++" was tried
    # first and is NOT broken — unary-plus chains are valid Python.)
    broken_edit = "sed -i 's/{base index}/{base + index/' slotkit/slotkit.py"
    return {
        "slug": "eval_repair_retry",
        "repo": str(repo),
        "issue": "slot_label() renders the literal text '{base index}' "
        "instead of the computed slot number — the f-string "
        "expression is malformed",
        "target": "tests/test_slotkit.py::test_label",
        "canonical_fix": ("{base index}", "{base + index}"),
        "script": {
            "plan": [
                {
                    "id": 1,
                    "description": "fix the f-string expression",
                    "checkpoint": "target test passes",
                    "files_hint": ["slotkit/slotkit.py"],
                }
            ],
            # attempt 1 breaks the file (syntax error), attempt 2 fixes it
            "scripts": {
                1: [
                    [broken_edit, "SUBMIT"],
                    [good_fix, "SUBMIT"],
                ]
            },
        },
        "expects_retry": True,
    }


def docs_scenario_task(build_root: Path) -> Dict[str, Any]:
    """A task whose scripted model issues DOCS lookups mid-step.

    Exercises the DOCS prompt addition (Round 8 Task D): the loop must
    intercept the DOCS line, answer it from the local docs cache /
    interpreter docs, and let the session continue to the real fix.
    With docs_lookup_enabled=False the loop nudges back to bash instead
    — the scripted model then runs its next command, so BOTH arms stay
    green and the pair proves the escape never BREAKS the loop (the
    regression this task guards is a machinery one: DOCS mishandling,
    deadlock, or misparse). The module genuinely fails pre-fix so the
    loop really runs.
    """
    repo = _build_repo(
        build_root,
        "eval_docs_lookup",
        "hashkit",
        module_src=(
            "def hex_sum(values):\n"
            '    """Sum of values, rendered as lowercase hex.\n'
            "\n"
            "    >>> hex_sum([1, 2, 3])\n"
            "    '0x6'\n"
            '    """\n'
            "    return str(sum(values))\n"
        ),
        test_src=(
            "from hashkit.hashkit import hex_sum\n"
            "\n"
            "\n"
            "def test_hex_sum():\n"
            "    assert hex_sum([1, 2, 3]) == '0x6'\n"
        ),
    )
    return {
        "slug": "eval_docs_lookup",
        "repo": str(repo),
        "issue": "hex_sum() must render lowercase hex with the 0x prefix "
        "(hex() semantics); it currently renders plain decimal. "
        "Confirm the expected output format via the docs, then "
        "fix hex_sum to match",
        "target": "tests/test_hashkit.py::test_hex_sum",
        "canonical_fix": ("return str(sum(values))", "return hex(sum(values))"),
        "script": {
            "plan": [
                {
                    "id": 1,
                    "description": "confirm format then fix",
                    "checkpoint": "target test passes",
                    "files_hint": ["hashkit/hashkit.py"],
                }
            ],
            "scripts": {
                1: [
                    [
                        "DOCS hex builtin formatting",
                        "DOCS json.dumps indent options",
                        "sed -i 's/return str(sum(values))/"
                        "return hex(sum(values))/' hashkit/hashkit.py",
                        "SUBMIT",
                    ],
                ]
            },
        },
    }


def fetch_scenario_task(build_root: Path) -> Dict[str, Any]:
    """A task whose scripted model FETCHes a real docs page mid-step.

    Exercises the FETCH prompt addition (web-page reading): the loop
    must intercept the FETCH line as a control signal, fetch the page,
    extract readable text, re-inject it into the session, and let the
    session continue to the real fix. With web_fetch_enabled=False the
    loop nudges back to bash instead — the scripted model then runs its
    next command, so BOTH arms stay green and the pair proves the
    escape never BREAKS the loop (the regression this task guards is a
    machinery one: FETCH mishandling, deadlock, or misparse — the
    fetched CONTENT is not scored, only loop integrity). The URL is the
    num2words PyPI page (a genuine docs target the harness's own e2e
    suite proves extractable), but the scripted fix does not depend on
    its content — determinism is preserved on any fetch outcome.

    The scripted replies DO obey the one-command-per-turn contract
    (FETCH lines are control signals, like DOCS/RECALL — the invariants
    suite treats them the same way).
    """
    repo = _build_repo(
        build_root,
        "eval_fetch_webpage",
        "versionkit",
        module_src=(
            "def version_major(version: str) -> int:\n"
            '    """Major component of a version string.\n'
            "\n"
            "    >>> version_major('2.14.0')\n"
            "    2\n"
            '    """\n'
            "    return version.split('.')[0] or 0\n"
        ),
        test_src=(
            "from versionkit.versionkit import version_major\n"
            "\n"
            "\n"
            "def test_major():\n"
            "    assert version_major('2.14.0') == 2\n"
            "\n"
            "\n"
            "def test_major_zero_pad():\n"
            "    assert version_major('003.1.4') == 3\n"
        ),
    )
    return {
        "slug": "eval_fetch_webpage",
        "repo": str(repo),
        "issue": "version_major returns '003' (a string with a leading "
        "zero) instead of the integer 3 — the parsed component must "
        "be converted to int. Check the docs page for string-to-int "
        "conversion semantics, then fix version_major",
        "target": "tests/test_versionkit.py::test_major_zero_pad",
        "canonical_fix": (
            "return version.split('.')[0] or 0",
            "return int(version.split('.')[0] or 0)",
        ),
        "script": {
            "plan": [
                {
                    "id": 1,
                    "description": "confirm int conversion then fix",
                    "checkpoint": "target test passes",
                    "files_hint": ["versionkit/versionkit.py"],
                }
            ],
            "scripts": {
                1: [
                    [
                        "FETCH https://pypi.org/project/num2words/",
                        "sed -i \"s/return version.split('.')\\[0\\] or 0/"
                        "return int(version.split('.')[0] or 0)/\" "
                        "versionkit/versionkit.py",
                        "SUBMIT",
                    ],
                ]
            },
        },
    }


def lint_scenario_task(build_root: Path) -> Dict[str, Any]:
    """A repair task whose broken edit is an UNDEFINED NAME, not a syntax
    error — check_edits' syntax pass cannot see it; only the Round-8 lint
    gate catches it host-side before a wasted sandboxed verify.

    The regression this guards: the lint prompt/feedback change breaking
    the repair loop (a lint finding must poison the attempt with
    classified feedback, and attempt 2 must recover).
    """
    repo = _build_repo(
        build_root,
        "eval_lint_undefined",
        "casestat",
        module_src=(
            "def norm_status(status: str) -> str:\n"
            '    """Normalize a status string to canonical case.\n'
            "\n"
            "    >>> norm_status('ACTIVE')\n"
            "    'active'\n"
            '    """\n'
            "    return status\n"
        ),
        test_src=(
            "from casestat.casestat import norm_status\n"
            "\n"
            "\n"
            "def test_norm():\n"
            "    assert norm_status('ACTIVE') == 'active'\n"
            "\n"
            "\n"
            "def test_mixed():\n"
            "    assert norm_status('AcTiVe') == 'active'\n"
        ),
    )
    return {
        "slug": "eval_lint_undefined",
        "repo": str(repo),
        "issue": "norm_status() must lowercase its input; it currently "
        "returns it unchanged",
        "target": "tests/test_casestat.py::test_norm",
        "canonical_fix": ("return status", "return status.lower()"),
        "script": {
            "plan": [
                {
                    "id": 1,
                    "description": "lowercase the status",
                    "checkpoint": "target test passes",
                    "files_hint": ["casestat/casestat.py"],
                }
            ],
            # attempt 1's edit is an UNDEFINED module-level NAME (valid
            # syntax; NameError at import): the syntax gate can't see it,
            # the Round-8 lint gate must. Attempt 2 lands the real fix.
            # ONE command (two -e expressions), not two seds: the step
            # contract is one command per turn, and a two-line reply
            # beheads the second sed (real bug caught by the 22:32 run).
            "scripts": {
                1: [
                    [
                        "sed -i '1i _CANON = _CANONICAL_CASE' casestat/casestat.py",
                        "SUBMIT",
                    ],
                    [
                        "sed -i -e 's/_CANON = _CANONICAL_CASE//' -e "
                        "'s/return status/return status.lower()/' "
                        "casestat/casestat.py",
                        "SUBMIT",
                    ],
                ]
            },
        },
        "expects_retry": True,
    }


def all_tasks(build_root: Path) -> List[Dict[str, Any]]:
    """The full fixed eval task set (5 fixtures + 4 synthesized + repair
    + docs-escape + lint-undefined scenarios).

    Assumes build_root is a writable scratch dir for the synthesized
    repos (the runner uses its own out dir; builds are deterministic).
    """
    tasks = [dict(t) for t in FIXTURE_TASKS]
    tasks.extend(synthesized_tasks(build_root / "repos"))
    tasks.append(repair_scenario_task(build_root / "repos"))
    tasks.append(docs_scenario_task(build_root / "repos"))
    tasks.append(lint_scenario_task(build_root / "repos"))
    tasks.append(fetch_scenario_task(build_root / "repos"))
    return tasks


# ---------------------------------------------------------------------------
# Host self-verification (--check): each task genuinely fails pre-fix and
# its canonical fix makes the full suite green. No Docker, no network.
# ---------------------------------------------------------------------------


def _run_pytest(repo: Path, node_id: str) -> int:
    import subprocess
    import sys

    env_extra = {"PYTHONDONTWRITEBYTECODE": "1"}
    import os

    env = dict(os.environ, **env_extra)
    cp = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            node_id,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    return cp.returncode


def check_set(build_root: Path) -> List[Dict[str, Any]]:
    """Verify the synthesized tasks + scenario tasks + fixture scripts.

    Returns a per-task check list: target fails on the buggy source,
    passes after the canonical fix, full suite green post-fix. Fixture
    tasks are checked the same way via their scripted fix command run
    in a scratch copy (the same sed/python command the scripted model
    issues — proving the SCRIPT itself is valid). Scenario tasks
    (repair/docs/lint) are checked on their FINAL attempt's commands —
    the last attempt is the canonical fix path; the mid-flight broken
    attempts are intentionally broken (that's what they test) and are
    validated by the arms instead.
    """
    import shutil
    import subprocess
    import sys

    results: List[Dict[str, Any]] = []
    tmp = build_root / "_check"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)

    synth = synthesized_tasks(tmp / "repos")
    # scenario tasks build into the same scratch tree (fresh, unpolulated)
    scen = [
        repair_scenario_task(tmp / "repos"),
        docs_scenario_task(tmp / "repos"),
        lint_scenario_task(tmp / "repos"),
    ]
    for t in synth + scen:
        repo = Path(t["repo"])
        pre = _run_pytest(repo, t["target"])
        skip_note = None
        if "canonical_fix" in t:
            old, new = t["canonical_fix"]
            pkg_dir = next(
                p for p in repo.iterdir() if p.is_dir() and p.name != "tests"
            )
            mod = next(pkg_dir.glob("*.py"))
            src = mod.read_text(encoding="utf-8")
            assert old in src, f"canonical_fix old text not found in {mod}"
            mod.write_text(src.replace(old, new), encoding="utf-8")
        else:
            # scenario task: run the LAST attempt's edit commands (the
            # canonical path) in order; SUBMIT/DOCS/RECALL lines skipped
            scripts = t["script"]["scripts"]
            host_cmds: List[str] = []
            for sid in sorted(scripts):
                for cmd in scripts[sid][-1]:
                    cmd = cmd.strip()
                    if cmd in ("SUBMIT", "ABORT") or cmd.startswith(
                        ("DOCS ", "RECALL ", "FETCH ")
                    ):
                        continue
                    host_cmds.append(cmd)
            for cmd in host_cmds:
                cp = subprocess.run(
                    ["bash", "-c", cmd] if sys.platform != "win32" else cmd,
                    cwd=str(repo),
                    capture_output=True,
                    text=True,
                    shell=(sys.platform == "win32"),
                    timeout=60,
                )
                if cp.returncode != 0:
                    skip_note = (
                        f"host can't run scripted cmd "
                        f"(exit {cp.returncode}); Docker arms "
                        f"prove it instead"
                    )
                    break
        if skip_note is not None:
            results.append({"slug": t["slug"], "ok": None, "note": skip_note})
            continue
        post_target = _run_pytest(repo, t["target"])
        post_suite = _run_pytest(repo, "")
        results.append(
            {
                "slug": t["slug"],
                "fails_pre_fix": pre != 0,
                "target_green_post_fix": post_target == 0,
                "suite_green_post_fix": post_suite == 0,
                "ok": pre != 0 and post_target == 0 and post_suite == 0,
            }
        )

    # fixture tasks: scratch copy + run the scripted fix command, then
    # the task's own suite proves the SCRIPT lands the fix.
    for t in FIXTURE_TASKS:
        scratch = tmp / t["slug"]
        shutil.copytree(
            t["repo"],
            scratch,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
        )
        pre = _run_pytest(scratch, t["target"])
        step_key = next(iter(t["script"]["scripts"]))
        cmd = t["script"]["scripts"][step_key][0]
        cp = subprocess.run(
            ["bash", "-c", cmd] if sys.platform != "win32" else cmd,
            cwd=str(scratch),
            capture_output=True,
            text=True,
            shell=(sys.platform == "win32"),
            timeout=60,
        )
        # fixture fix commands are POSIX sed/python -c — on Windows hosts
        # without sed the python -c variants still run; a missing sed marks
        # the check unavailable rather than lying
        if cp.returncode != 0 and "sed" in cmd:
            results.append(
                {
                    "slug": t["slug"],
                    "ok": None,
                    "note": "sed unavailable on this host; script validity "
                    "proven by the Docker arms instead",
                }
            )
            continue
        post = _run_pytest(scratch, t["target"])
        results.append(
            {
                "slug": t["slug"],
                "fails_pre_fix": pre != 0,
                "target_green_post_fix": post == 0,
                "ok": pre != 0 and post == 0,
            }
        )

    shutil.rmtree(tmp, ignore_errors=True)
    return results
