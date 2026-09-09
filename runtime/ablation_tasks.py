"""Expanded ablation task set (Round 3, Task B): 11 additional buggy repos,
synthesized deterministically at run time (code, not committed fixtures).

Why synthesized rather than committed under tests/fixtures: the ablation
task set is RUN STATE (varies per experiment round), not T1's test surface.
Building from code keeps the set reproducible, reviewable, and out of
Terminal 1's territory. Each repo lands under the run's own out dir.

Design goals (the ablation measures text-driven difficulty routing, so the
ISSUE TEXT distribution matters as much as the bug classes):
  - ~5 easy one-liner texts (predictor should route cheap)
  - ~4 medium texts (reproduce recipes / error wording)
  - 2 deliberately SCARY texts (stack traces, "race", "intermittent",
    "encoding") over trivial one-token fixes — probes FALSE ESCALATION
    (does text-based prediction waste expensive calls on hard-LOOKING
    easy bugs?) and, when the cheap model still fails, real escalation.
  - bug classes beyond T1's five: wrong index, off-by-one slice, missing
    transform, constant-instead-of-formula, case-order, exception type,
    int-vs-float division, mutation-during-iteration, wrong variable,
    wrong comparison target.

Every repo: pyproject.toml (pytest testpaths), one package module with the
bug, one test module with exactly one failing test (others pass, so the
suite-as-gate semantics match T1's fixtures).

`python -m runtime.ablation_tasks --check` self-verifies the whole set on
the HOST (plain pytest, no Docker, no network): buggy repo FAILS its target
test, canonical-patched repo PASSES the full suite. Run this before trusting
an ablation that uses the set.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List


def _pyproject(name: str) -> str:
    return (
        "[project]\n"
        f'name = "{name}"\n'
        'version = "0.1.0"\n'
        "\n"
        "[tool.pytest.ini_options]\n"
        'testpaths = ["tests"]\n'
    )


# Each task: slug, package/module, buggy source, tests, issue text (the
# predictor's input — style deliberately varied), target test node id, and
# the canonical one-shot fix (old -> new exact string replacement) used by
# the --check self-verification.
EXTRA_TASKS: List[Dict[str, Any]] = [
    {
        "slug": "max3-middle",
        "pkg": "maxlib", "module": "threeway",
        "issue": "max3() returns the wrong value: for 1, 2, 3 it gives 2 "
                 "instead of 3.",
        "code": (
            'def max3(a, b, c):\n'
            '    """Return the largest of the three values."""\n'
            '    order = sorted([a, b, c])\n'
            '    return order[1]\n'
        ),
        "tests": (
            'from maxlib.threeway import max3\n'
            "\n"
            "\n"
            'def test_max3_ascending():\n'
            '    assert max3(1, 2, 3) == 3\n'
            "\n"
            "\n"
            'def test_max3_unordered():\n'
            '    assert max3(5, 1, 4) == 5\n'
            "\n"
            "\n"
            'def test_max3_with_ties():\n'
            '    assert max3(2, 2, 1) == 2\n'
        ),
        "target": "tests/test_threeway.py::test_max3_ascending",
        "fix": ("order[1]", "order[2]"),
    },
    {
        "slug": "truncate-short",
        "pkg": "textlib", "module": "cutter",
        "issue": "truncate() cuts one character too many: "
                 "truncate('abcdef', 4) returns 'abc' but should return "
                 "'abcd'.",
        "code": (
            'def truncate(text, width):\n'
            '    """Cut text to at most `width` characters."""\n'
            '    if len(text) <= width:\n'
            '        return text\n'
            '    return text[:width - 1]\n'
        ),
        "tests": (
            'from textlib.cutter import truncate\n'
            "\n"
            "\n"
            'def test_short_enough_untouched():\n'
            '    assert truncate("ab", 5) == "ab"\n'
            "\n"
            "\n"
            'def test_truncate_exact_width():\n'
            '    assert truncate("abcd", 4) == "abcd"\n'
            "\n"
            "\n"
            'def test_truncate_longer():\n'
            '    assert truncate("abcdef", 4) == "abcd"\n'
        ),
        "target": "tests/test_cutter.py::test_truncate_longer",
        "fix": ("text[:width - 1]", "text[:width]"),
    },
    {
        "slug": "parse-comma",
        "pkg": "numtext", "module": "reader",
        "issue": "parse_int() crashes on numbers written with comma "
                 "separators. Reproduce: parse_int('1,234') should return "
                 "1234 but raises ValueError: invalid literal for int().",
        "code": (
            'def parse_int(text):\n'
            '    """Parse "1,234"-style digit-grouped input into an int."""\n'
            '    return int(text)\n'
        ),
        "tests": (
            'from numtext.reader import parse_int\n'
            "\n"
            "\n"
            'def test_plain():\n'
            '    assert parse_int("42") == 42\n'
            "\n"
            "\n"
            'def test_grouped_digits():\n'
            '    assert parse_int("1,234") == 1234\n'
            "\n"
            "\n"
            'def test_millions():\n'
            '    assert parse_int("2,000,000") == 2000000\n'
        ),
        "target": "tests/test_reader.py::test_grouped_digits",
        "fix": ("return int(text)",
                'return int(text.replace(",", ""))'),
    },
    {
        "slug": "path-slash",
        "pkg": "pathlibx", "module": "norm",
        "issue": "normalize_path() never removes the trailing slash: "
                 "normalize_path('a/b/') returns 'a/b/' instead of 'a/b'.",
        "code": (
            'def normalize_path(path):\n'
            '    """Drop a trailing slash: "a/b/" -> "a/b"."""\n'
            '    if path.endswith("/"):\n'
            '        return path\n'
            '    return path\n'
        ),
        "tests": (
            'from pathlibx.norm import normalize_path\n'
            "\n"
            "\n"
            'def test_no_trailing_slash():\n'
            '    assert normalize_path("a/b") == "a/b"\n'
            "\n"
            "\n"
            'def test_trailing_slash_removed():\n'
            '    assert normalize_path("a/b/") == "a/b"\n'
            "\n"
            "\n"
            'def test_root_left_alone():\n'
            '    assert normalize_path("/") == "/"\n'
        ),
        "target": "tests/test_norm.py::test_trailing_slash_removed",
        "fix": ('    if path.endswith("/"):\n'
                '        return path\n'
                '    return path',
                '    if len(path) > 1 and path.endswith("/"):\n'
                '        return path[:-1]\n'
                '    return path'),
    },
    {
        # SCARY text, trivial bug: the false-escalation probe.
        "slug": "backoff-race",
        "pkg": "retrylib", "module": "backoff",
        "issue": (
            "Retry workers crash intermittently under concurrent load; "
            "backoff timing seems involved. Log excerpt:\n"
            "```\nTraceback (most recent call last):\n"
            '  File "retrylib/backoff.py", line 4, in retry_delay\n'
            "ValueError: retry storm: every attempt waited 1s, workers "
            "piled up\n```\n"
            "Possibly a race between the scheduler and the timer; the "
            "timing of retries looks constant instead of spreading out. "
            "Not sure if it's flaky-clock related."
        ),
        "code": (
            'def retry_delay(attempt):\n'
            '    """Exponential backoff in seconds: attempt 1 -> 1, 2 -> 2,'
            ' 3 -> 4, 4 -> 8."""\n'
            '    return 1\n'
        ),
        "tests": (
            'from retrylib.backoff import retry_delay\n'
            "\n"
            "\n"
            'def test_first_attempt():\n'
            '    assert retry_delay(1) == 1\n'
            "\n"
            "\n"
            'def test_exponential_growth():\n'
            '    assert retry_delay(3) == 4\n'
            "\n"
            "\n"
            'def test_fourth_attempt():\n'
            '    assert retry_delay(4) == 8\n'
        ),
        "target": "tests/test_backoff.py::test_exponential_growth",
        "fix": ("return 1", "return 2 ** (attempt - 1)"),
    },
    {
        # Unicode/encoding-heavy text, trivial case-order bug.
        "slug": "slugify-case",
        "pkg": "weblib", "module": "slug",
        "issue": (
            "Slugs render wrong for any title with letters: 'Hello World' "
            "becomes 'HELLO-WORLD' but the site expects 'hello-world'. "
            "Suspect the unicode/encoding normalization pipeline (UTF-8 "
            "titles, case folding, i18n locales) mishandles the "
            "transformation order somewhere in slug.py."
        ),
        "code": (
            'def slugify(text):\n'
            '    """URL slug: lowercase, spaces become dashes."""\n'
            '    return text.replace(" ", "-").upper()\n'
        ),
        "tests": (
            'from weblib.slug import slugify\n'
            "\n"
            "\n"
            'def test_simple():\n'
            '    assert slugify("hello world") == "hello-world"\n'
            "\n"
            "\n"
            'def test_mixed_case():\n'
            '    assert slugify("Hello World") == "hello-world"\n'
            "\n"
            "\n"
            'def test_single_word():\n'
            '    assert slugify("Hello") == "hello"\n'
        ),
        "target": "tests/test_slug.py::test_mixed_case",
        "fix": ('text.replace(" ", "-").upper()',
                'text.replace(" ", "-").lower()'),
    },
    {
        "slug": "median-odd",
        "pkg": "statlib", "module": "middle",
        "issue": "median_odd() returns the upper value for odd-length "
                 "lists: median_odd([1, 2, 3]) gives 3 instead of 2.",
        "code": (
            'def median_odd(values):\n'
            '    """Median of an odd-length list (list is not modified)."""\n'
            '    ordered = sorted(values)\n'
            '    n = len(ordered)\n'
            '    return ordered[(n + 1) // 2]\n'
        ),
        "tests": (
            'from statlib.middle import median_odd\n'
            "\n"
            "\n"
            'def test_three():\n'
            '    assert median_odd([1, 2, 3]) == 2\n'
            "\n"
            "\n"
            'def test_five_unsorted():\n'
            '    assert median_odd([9, 1, 5, 3, 7]) == 5\n'
            "\n"
            "\n"
            'def test_single():\n'
            '    assert median_odd([42]) == 42\n'
        ),
        "target": "tests/test_middle.py::test_three",
        "fix": ("ordered[(n + 1) // 2]", "ordered[n // 2]"),
    },
    {
        "slug": "lookup-default",
        "pkg": "tablib", "module": "lookup",
        "issue": "lookup() raises KeyError when the key is missing. "
                 "Reproduce: lookup({'a': 1}, 'b', 0) should return 0 but "
                 "raises KeyError: 'b'. The default parameter is never "
                 "used.",
        "code": (
            'def lookup(table, key, default):\n'
            '    """Return table[key], or `default` when key is absent."""\n'
            '    return table[key]\n'
        ),
        "tests": (
            'from tablib.lookup import lookup\n'
            "\n"
            "\n"
            'def test_present():\n'
            '    assert lookup({"a": 1}, "a", 0) == 1\n'
            "\n"
            "\n"
            'def test_missing_returns_default():\n'
            '    assert lookup({"a": 1}, "b", 0) == 0\n'
            "\n"
            "\n"
            'def test_missing_default_string():\n'
            '    assert lookup({}, "k", "none") == "none"\n'
        ),
        "target": "tests/test_lookup.py::test_missing_returns_default",
        "fix": ("return table[key]", "return table.get(key, default)"),
    },
    {
        "slug": "average-floor",
        "pkg": "statlib2", "module": "mean2",
        "issue": "average() loses the fractional part: average([2, 3]) "
                 "returns 2 instead of 2.5.",
        "code": (
            'def average(scores):\n'
            '    """Arithmetic mean as a float."""\n'
            '    total = sum(scores)\n'
            '    return total // len(scores)\n'
        ),
        "tests": (
            'from statlib2.mean2 import average\n'
            "\n"
            "\n"
            'def test_whole_result():\n'
            '    assert average([2, 2]) == 2.0\n'
            "\n"
            "\n"
            'def test_fractional_result():\n'
            '    assert average([2, 3]) == 2.5\n'
            "\n"
            "\n"
            'def test_single():\n'
            '    assert average([7]) == 7.0\n'
        ),
        "target": "tests/test_mean2.py::test_fractional_result",
        "fix": ("total // len(scores)", "total / len(scores)"),
    },
    {
        "slug": "drop-negatives",
        "pkg": "listlib", "module": "filtering",
        "issue": (
            "drop_negatives() sometimes leaves negative numbers behind. "
            "It doesn't happen for every input — occasionally one slips "
            "through, maybe depending on where in the list it sits. "
            "drop_negatives([1, -2, -3, 4]) should return [1, 4] but "
            "returns [1, -3, 4]."
        ),
        "code": (
            'def drop_negatives(items):\n'
            '    """Return items with negative numbers removed."""\n'
            '    for x in items:\n'
            '        if x < 0:\n'
            '            items.remove(x)\n'
            '    return items\n'
        ),
        "tests": (
            'from listlib.filtering import drop_negatives\n'
            "\n"
            "\n"
            'def test_no_negatives():\n'
            '    assert drop_negatives([1, 2]) == [1, 2]\n'
            "\n"
            "\n"
            'def test_adjacent_negatives():\n'
            '    assert drop_negatives([1, -2, -3, 4]) == [1, 4]\n'
            "\n"
            "\n"
            'def test_all_negative():\n'
            '    assert drop_negatives([-1, -2]) == []\n'
        ),
        "target": "tests/test_filtering.py::test_adjacent_negatives",
        "fix": (  # canonical rewrite for --check (model may differ)
            "    for x in items:\n"
            "        if x < 0:\n"
            "            items.remove(x)\n"
            "    return items",
            "    return [x for x in items if x >= 0]",
        ),
    },
    {
        "slug": "interval-end",
        "pkg": "rangefmt", "module": "render",
        "issue": "interval() prints the start twice: interval(1, 5) returns "
                 "'[1, 1)' instead of '[1, 5)'.",
        "code": (
            'def interval(start, end):\n'
            '    """Render the half-open interval "[start, end)"."""\n'
            '    return f"[{start}, {start})"\n'
        ),
        "tests": (
            'from rangefmt.render import interval\n'
            "\n"
            "\n"
            'def test_basic():\n'
            '    assert interval(1, 5) == "[1, 5)"\n'
            "\n"
            "\n"
            'def test_same_point():\n'
            '    assert interval(3, 3) == "[3, 3)"\n'
        ),
        "target": "tests/test_render.py::test_basic",
        "fix": ('return f"[{start}, {start})"',
                'return f"[{start}, {end})"'),
    },
]


def build_repo(dst: Path, task: Dict[str, Any]) -> Path:
    """Materialize one synthesized repo at dst (writable, fresh)."""
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    pkg_dir = dst / task["pkg"]
    tests_dir = dst / "tests"
    pkg_dir.mkdir(parents=True)
    tests_dir.mkdir(parents=True)
    (dst / "pyproject.toml").write_text(_pyproject(task["pkg"]),
                                        encoding="utf-8")
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / f'{task["module"]}.py').write_text(task["code"],
                                                   encoding="utf-8")
    (tests_dir / f'test_{task["module"]}.py').write_text(task["tests"],
                                                         encoding="utf-8")
    return dst


def build_all(root: Path) -> Dict[str, Path]:
    """Build every extra repo under root/<slug>/; returns slug -> path."""
    out: Dict[str, Path] = {}
    for t in EXTRA_TASKS:
        out[t["slug"]] = build_repo(root / t["slug"], t)
    return out


# ---------------------------------------------------------------------------
# Self-check: buggy repo fails its target, canonical fix passes the suite.
# Runs plain host pytest (no Docker, no network) — validates the TASK SET.
# ---------------------------------------------------------------------------

def _pytest(repo: Path, node: str) -> bool:
    """Run host pytest in `repo`. PYTHONDONTWRITEBYTECODE prevents a
    stale-__pycache__ false verdict: the canonical fixes for several
    tasks are byte-for-byte the SAME LENGTH as the bug (order[1]->
    order[2], upper()->lower()), so a rewritten module can share size
    AND coarse mtime with its cached buggy bytecode — CPython's
    (mtime, size) check then reuses the STALE pyc and the post-fix
    suite re-runs the BUG (found live: intermittent [BAD] verdicts,
    task-dependent)."""
    import os
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", node],
        cwd=str(repo), capture_output=True, text=True, timeout=120, env=env,
    )
    return r.returncode == 0


def check_all() -> bool:
    """Verify every extra task: target FAILS pre-fix and PASSES post-fix
    (full suite green after the canonical fix). Returns True iff all 11 do.
    """
    ok = True
    with tempfile.TemporaryDirectory(prefix="ablation-tasks-") as tmp:
        root = Path(tmp)
        for t in EXTRA_TASKS:
            repo = build_repo(root / t["slug"], t)
            mod = repo / t["pkg"] / f'{t["module"]}.py'
            node = t["target"]
            if _pytest(repo, node):
                print(f"[BAD ] {t['slug']}: target PASSES before the fix")
                ok = False
                continue
            src = mod.read_text(encoding="utf-8")
            old, new = t["fix"]
            if old not in src:
                print(f"[BAD ] {t['slug']}: canonical fix pattern not found")
                ok = False
                continue
            mod.write_text(src.replace(old, new), encoding="utf-8")
            suite_ok = _pytest(repo, "tests") and _pytest(repo, node)
            if not suite_ok:
                print(f"[BAD ] {t['slug']}: suite still failing after fix")
                ok = False
            else:
                print(f"[OK  ] {t['slug']}: fails pre-fix, green post-fix")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(prog="runtime.ablation_tasks")
    ap.add_argument("--check", action="store_true",
                    help="self-verify the whole task set on the host")
    ap.add_argument("--build-root", default=None,
                    help="materialize the repos under this dir and exit")
    args = ap.parse_args()
    if args.check:
        return 0 if check_all() else 1
    if args.build_root:
        paths = build_all(Path(args.build_root))
        for slug, p in sorted(paths.items()):
            print(f"{slug}: {p}")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
