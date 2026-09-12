"""Round-6 multi-repo ablation task set (Terminal 3, Task A): five REAL,
UNFAMILIAR OSS repos (not fixtures, not synthesized), each pinned to a
commit, each carrying one GENUINE introduced bug + a failing regression
test — Terminal 1's Round-4 OSS-run pattern (jaraco/path), scaled to a
task set, built for the adaptive-routing ablation.

Why this exists (documented decision): the Round-2..5 ablation result
(2.6-3.5x cheaper at equal success) rested on 5 fixture bugs + 11
synthesized single-file repos. The Round-6 question is whether that
result HOLDS on genuinely different, unfamiliar repos, or was partly an
artifact of the original task set. Terminal 1's Round-6 multi-repo tasks
had not landed when this round started (verified: no Round-6 entries
anywhere in the tree), so this module builds the multi-repo set itself
per the AGENTS.md convention (make a reasonable decision, document it).
If T1's set lands later, both can run: the ablation runner takes a task
set name; this one is "multirepo".

Bug choices (each a real defect class, single edit site, verified
against upstream behavior at the pinned SHA — the issue texts are
natural bug reports, NOT containing the fix):

  more-itertools (ca711220a6)  recipes.nth off-by-one: islice starts at
                              n-1, so nth(iter, n) returns the (n-1)th
                              item (wrong-index class).
  arrow          (2224255c4a) util.next_weekday weekday-mapping bug:
                              byweekday shifted by +1, every result one
                              weekday late (wrong-index-mapping class;
                              upstream's own test_next_weekday catches it).
  inflect        (262a247d2d) ordinal() ignores the teen table
                              (nth[n%100]): 111 -> "111st" (wrong-table-
                              lookup class).
  semver 3.0.4   (6adf8765f6) next_version("prerelease") drops the
                              custom prerelease_token (0.1.4 ->
                              "0.1.5-rc.1" even when token="beta")
                              (dropped-parameter class).
  boltons        (961dcff3f4) strutils.ordinalize teen rule broken:
                              checks the LAST digit for '1' instead of
                              the second-to-last, so 11 -> "11st"
                              (wrong-condition class).

Each repo dir baked under <out>/repos/<slug>/: pristine clone at the
pinned SHA with the bug applied + tests/test_regression_<slug>.py added
(the failing target test). Suite pins (test_command) come from each
repo's quirks:
  - semver: .pytest.ini addopts need pytest-cov/doctests and tests/
    contains two git SYMLINKS (Windows checkouts materialize them as
    tiny text files -> pytest SyntaxError). Pin "-o addopts=" + copy the
    symlink targets over. pythonpath=src comes from .pytest.ini (kept).
  - arrow: no pytest ini; tests import pytest-mock/pytz/simplejson only
    in some files — pin the target file explicitly via target_test and
    "python -m pytest -q" for the suite (deps image installs
    [project.dependencies] only: python-dateutil — enough for util.py
    tests; test files needing pytest-mock are not collected by the
    pinned target, and the suite pin deselects them via -p no:cacheprovider
    ... actually the suite run DOES collect test_api.py which needs
    pytest-mock. Fix: suite command pins "python -m pytest -q tests/test_util.py
    tests/test_formatter.py tests/test_arrow.py" — files whose imports
    resolve with dateutil+pytz only. pytz is NOT in [project]
    dependencies... test_arrow.py imports pytz. So the arrow suite pin
    is "python -m pytest -q tests/test_util.py" (pure stdlib+dateutil)
    plus the regression file, documented as a suite SUBSET pin.)
  - inflect: pytest.ini --doctest-modules + pythonpath; deps =
    more_itertools + typeguard (in [project] dependencies — installed
    by the deps image). doctest addopts are bypassed with "-o addopts=".
  - boltons: no test deps; suite pin "python -m pytest -q tests" (host
    suite is green; tests/conftest.py imports boltons only).
  - more-itertools: tests run unittest-style under pytest; no external
    deps. Suite = full tests dir.

`python -m runtime.multirepo_tasks --check` self-verifies ON THE HOST
(mirror of ablation_tasks --check, no Docker): for each repo, (1) the
BUGGY tree fails its regression target test, (2) after applying the
canonical fix the target passes AND the pinned suite passes, (3) the
issue-text-only difficulty prediction lands where expected (no hard-
saturation — the v1 lesson). Run this before trusting an ablation that
uses the set.

Clones are cached under the module's CACHE_ROOT (gitignored path under
logs/multirepo-cache/) keyed by repo+SHA, so re-runs don't re-download;
each bake copies the cached clone (bug applied) so runs never mutate
the cache.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
CACHE_ROOT = Path(
    os.environ.get("MULTIREPO_CACHE", REPO_ROOT / "logs" / "multirepo-cache")
)

# ---------------------------------------------------------------------------
# The task set: repo, pinned SHA, bug edit (old -> new exact replacement),
# regression test source, target node, suite pin, style label.
# ---------------------------------------------------------------------------

MULTIREPO_TASKS: List[Dict[str, Any]] = [
    {
        "slug": "miter-nth-offbyone",
        "repo": "more-itertools",
        "url": "https://github.com/more-itertools/more-itertools.git",
        "sha": "ca711220a6657a22655f6fa497524d006a735c5f",
        "style": "easy",
        "bug_file": "more_itertools/recipes.py",
        "bug_old": "    return next(islice(iterable, n, None), default)",
        "bug_new": "    return next(islice(iterable, max(n - 1, 0), None), default)",
        "issue": (
            "nth() returns the wrong item: nth(range(10), 3) gives 2 "
            "instead of 3, and nth(l, 0, 'zebra') works but every "
            "other n is off by one. The returned item is one "
            "position before the one asked for."
        ),
        "target": "tests/test_regression_miter.py::test_nth_returns_the_nth_item",
        "test_file": "tests/test_regression_miter.py",
        "test_src": (
            "from more_itertools.recipes import nth\n"
            "\n"
            "\n"
            "def test_nth_returns_the_nth_item():\n"
            "    l = list(range(10))\n"
            "    assert nth(l, 0) == 0\n"
            "    assert nth(l, 1) == 1\n"
            "    assert nth(l, 3) == 3\n"
            "    assert nth(l, 9) == 9\n"
            "    assert nth(l, 20, 'zebra') == 'zebra'\n"
        ),
        # suite SUBSET pin: test_more.py (602 tests, 57s) is unnecessary
        # blast radius for a recipes.py bug; test_recipes.py covers it in
        # ~7s. PrimeFunctionTests/TotientTests are deselected: upstream's
        # own huge-int primality tests grind for MINUTES on this host
        # (diagnosed live — 30+ digit pseudoprimes), which would blow the
        # verify_timeout_s budget without testing anything near the bug.
        "suite": (
            "python -m pytest -q tests/test_regression_miter.py "
            "tests/test_recipes.py "
            "--deselect tests/test_recipes.py::PrimeFunctionTests "
            "--deselect tests/test_recipes.py::TotientTests"
        ),
    },
    {
        "slug": "arrow-weekday-boundary",
        "repo": "arrow",
        "url": "https://github.com/arrow-py/arrow.git",
        "sha": "2224255c4acc594d734cef0bbc83360452a67983",
        "style": "medium",
        "bug_file": "arrow/util.py",
        "bug_old": (
            "    return rrule(freq=WEEKLY, dtstart=start_date, "
            "byweekday=weekday, count=1)[0]"
        ),
        "bug_new": (
            "    return rrule(freq=WEEKLY, dtstart=start_date, "
            "byweekday=(weekday + 1) % 7, count=1)[0]"
        ),
        "issue": (
            "next_weekday() returns the wrong weekday: asking for "
            "the first Monday (0) from 1970-01-01 gives Tuesday "
            "1970-01-06, asking for Thursday (3) gives Friday "
            "1970-01-02. Every result is one weekday later than "
            "requested, as if the weekday argument were being "
            "mapped through the wrong table."
        ),
        "target": "tests/test_regression_arrow.py::test_next_weekday_correct_day",
        "test_file": "tests/test_regression_arrow.py",
        "test_src": (
            "from datetime import datetime\n"
            "\n"
            "from arrow.util import next_weekday\n"
            "\n"
            "\n"
            "def test_next_weekday_correct_day():\n"
            "    # epoch 1970-01-01 is a Thursday\n"
            "    assert next_weekday(datetime(1970, 1, 1), 0) == datetime(1970, 1, 5)\n"
            "    assert next_weekday(datetime(1970, 1, 1), 1) == datetime(1970, 1, 6)\n"
            "    assert next_weekday(datetime(1970, 1, 1), 2) == datetime(1970, 1, 7)\n"
            "    assert next_weekday(datetime(1970, 1, 1), 3) == datetime(1970, 1, 1)\n"
            "    assert next_weekday(datetime(1970, 1, 1), 4) == datetime(1970, 1, 2)\n"
            "    assert next_weekday(datetime(1970, 1, 1), 5) == datetime(1970, 1, 3)\n"
            "    assert next_weekday(datetime(1970, 1, 1), 6) == datetime(1970, 1, 4)\n"
        ),
        # suite SUBSET pin: test files needing pytest-mock/pytz/simplejson
        # (dev extras, not in [project] dependencies) are excluded; the
        # regression file + util tests exercise the bug's blast radius
        # (upstream's own test_next_weekday fails under the bug).
        # -o addopts=: tox.ini's [pytest] addopts require pytest-cov
        # (dev-only) — bypassed, same class as semver's .pytest.ini.
        "suite": "python -m pytest -q -o addopts= tests/test_regression_arrow.py tests/test_util.py",
    },
    {
        "slug": "inflect-ordinal-teen",
        "repo": "inflect",
        "url": "https://github.com/jaraco/inflect.git",
        "sha": "262a247d2d99a47a520cdb2d46adb90df88b4326",
        "style": "medium",
        "bug_file": "inflect/__init__.py",
        "bug_old": "                post = nth[n % 100]",
        "bug_new": "                post = nth[n % 10]",
        "issue": (
            "ordinal() gets teens wrong: p.ordinal(111) returns "
            "'111st' instead of '111th', p.ordinal(212) gives "
            "'212nd' instead of '212th'. Numbers ending in 1/2/3 "
            "but in the second-to-last position 1 (the teens: 11th, "
            "12th, 13th) are mis-suffixed — 11/12/13 themselves work "
            "but 111..113, 211..213 etc. are all wrong."
        ),
        "target": "tests/test_regression_inflect.py::test_ordinal_teens",
        "test_file": "tests/test_regression_inflect.py",
        "test_src": (
            "import inflect\n"
            "\n"
            "\n"
            "def test_ordinal_teens():\n"
            "    p = inflect.engine()\n"
            "    for n, expected in [(111, '111th'), (112, '112th'), (113, '113th'),\n"
            "                        (211, '211th'), (212, '212th'), (213, '213th'),\n"
            "                        (1011, '1011th'), (1012, '1012th'),\n"
            "                        (21, '21st'), (22, '22nd'), (23, '23rd'),\n"
            "                        (1, '1st'), (2, '2nd'), (3, '3rd'), (4, '4th')]:\n"
            '        assert p.ordinal(n) == expected, f"ordinal({n})"\n'
        ),
        # -o addopts=: pytest.ini's --doctest-modules addopts need the doc
        # tree; -p no:ruff: the deps image installs [project.optional-
        # dependencies] groups, including inflect's 'check' extra ->
        # pytest-ruff pseudo-tests, which fail on the Docker bind mount's
        # executable bit (EXE002) and would poison every verify.
        "suite": (
            "python -m pytest -q -o addopts= -p no:ruff "
            "tests/test_regression_inflect.py tests/test_inflections.py"
        ),
    },
    {
        "slug": "semver-next-prerelease-token",
        "repo": "semver",
        "url": "https://github.com/python-semver/python-semver.git",
        "sha": "6adf8765f6e21910f1f0c13151ce84f32f8d431d",  # v3.0.4
        "style": "medium",
        "bug_file": "src/semver/version.py",
        "bug_old": "        return version.bump_prerelease(prerelease_token)",
        "bug_new": "        return version.bump_prerelease()",
        "issue": (
            "next_version('prerelease') ignores the prerelease_token "
            "argument: semver.Version.parse('0.1.4').next_version("
            "'prerelease', prerelease_token='beta') returns "
            "'0.1.5-rc.1' instead of '0.1.5-beta.1'. The default "
            "token 'rc' is always used; release candidates and "
            "betas collide in version ordering because the custom "
            "token never reaches the bump."
        ),
        "target": "tests/test_regression_semver.py::test_next_version_prerelease_token",
        "test_file": "tests/test_regression_semver.py",
        "test_src": (
            "import semver\n"
            "\n"
            "\n"
            "def test_next_version_prerelease_token():\n"
            "    v = semver.Version.parse('0.1.4')\n"
            "    assert str(v.next_version('prerelease', prerelease_token='beta')) == '0.1.5-beta.1'\n"
            "    assert str(v.next_version('prerelease')) == '0.1.5-rc.1'\n"
            "    assert str(v.next_version('patch')) == '0.1.5'\n"
            "    assert str(v.next_version('major')) == '1.0.0'\n"
        ),
        "suite": (
            "python -m pytest -q -o addopts= "
            "tests/test_regression_semver.py tests/test_bump.py tests/test_compare.py"
        ),
    },
    {
        "slug": "boltons-ordinalize-teen",
        "repo": "boltons",
        "url": "https://github.com/mahmoud/boltons.git",
        "sha": "961dcff3f42e73b245aef65e377fe82763b257bb",
        "style": "easy",
        "bug_file": "boltons/strutils.py",
        "bug_old": "            if numstr[-2] == '1':",
        "bug_new": "            if numstr[-1] == '1':",
        "issue": (
            "ordinalize() mangles teens and any number ending in 1: "
            "ordinalize(11) returns '11st' instead of '11th', "
            "ordinalize(13) gives '13st' instead of '13th', and "
            "ordinalize(21) gives '21th' instead of '21st'. The "
            "suffix rule for 1/2/3 vs teens is applied against the "
            "wrong digit."
        ),
        "target": "tests/test_regression_boltons.py::test_ordinalize_suffixes",
        "test_file": "tests/test_regression_boltons.py",
        "test_src": (
            "from boltons.strutils import ordinalize\n"
            "\n"
            "\n"
            "def test_ordinalize_suffixes():\n"
            "    assert ordinalize(0) == '0th'\n"
            "    assert ordinalize(1) == '1st'\n"
            "    assert ordinalize(2) == '2nd'\n"
            "    assert ordinalize(3) == '3rd'\n"
            "    assert ordinalize(4) == '4th'\n"
            "    assert ordinalize(11) == '11th'\n"
            "    assert ordinalize(12) == '12th'\n"
            "    assert ordinalize(13) == '13th'\n"
            "    assert ordinalize(21) == '21st'\n"
            "    assert ordinalize(22) == '22nd'\n"
            "    assert ordinalize(23) == '23rd'\n"
            "    assert ordinalize(111) == '111th'\n"
            "    assert ordinalize(112) == '112th'\n"
        ),
        "suite": "python -m pytest -q tests/test_regression_boltons.py tests/test_strutils.py",
    },
]

# Windows-clone hazards per repo (see execution/AGENTS.md "Docker setup
# quirks"): semver's tests/ holds two git symlinks that materialize as
# text files containing the link PATH (pytest then dies with a
# SyntaxError importing them). Fix at bake time: copy the target over.
_SYMLINK_FIXES: Dict[str, List[Dict[str, str]]] = {
    "semver": [
        {"link": "tests/coerce.py", "target": "docs/advanced/coerce.py"},
        {
            "link": "tests/semverwithvprefix.py",
            "target": "docs/advanced/semverwithvprefix.py",
        },
    ],
}


def _git(
    args: List[str], cwd: Optional[Path] = None, check: bool = True, timeout: int = 300
) -> subprocess.CompletedProcess:
    """Run git, Windows-safe. Assumes args is a full argv after 'git'."""
    cmd = ["git"] + args
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {' '.join(args[:3])} timed out ({timeout}s)")


def _rmtree_hard(path: Path) -> None:
    """rmtree that clears Windows read-only bits (git pack files)."""

    def _onerror(func, p, _exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass

    shutil.rmtree(path, onerror=_onerror)


def ensure_clone(spec: Dict[str, Any]) -> Path:
    """Clone+checkout the pinned SHA into the cache; returns the clone dir.

    Assumes network access on first use (the ablation itself is run-time
    networkless except model endpoints). Cached clones are reused;
    the clone is left at the pinned SHA, detached.
    """
    cache = CACHE_ROOT / f"{spec['repo']}-{spec['sha'][:10]}"
    if (cache / ".git").exists():
        head = _git(["rev-parse", "HEAD"], cwd=cache).stdout.strip()
        if head == spec["sha"]:
            return cache
        _rmtree_hard(cache)  # wrong SHA cached: rebuild
    if cache.exists():
        _rmtree_hard(cache)
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    _git(["clone", "--quiet", spec["url"], str(cache)], timeout=600)
    _git(["checkout", "--quiet", spec["sha"]], cwd=cache)
    # Symlink materialization fix (Windows clones turn git symlinks into
    # path-text files; the ablation's pytest runs would die importing them).
    for fix in _SYMLINK_FIXES.get(spec["repo"], []):
        link = cache / fix["link"]
        target = cache / fix["target"]
        if link.is_file() and target.is_file():
            shutil.copyfile(str(target), str(link))
    return cache


def bake_repo(spec: Dict[str, Any], dst: Path) -> Path:
    """Materialize the BUGGY repo at dst (a writable copy of the cached
    pinned clone with the bug applied + the regression test added).

    Assumes dst's parent exists; wipes any prior dst. The cache is never
    mutated. The bug edit is exact-string (verified by --check across
    all five repos at the pinned SHAs).
    """
    if dst.exists():
        _rmtree_hard(dst)
    cache = ensure_clone(spec)
    shutil.copytree(str(cache), str(dst), ignore=shutil.ignore_patterns(".git"))
    # strip git remnants from any ignore-failure (safety)
    if (dst / ".git").exists():
        _rmtree_hard(dst / ".git")

    src_path = dst / spec["bug_file"]
    src = src_path.read_text(encoding="utf-8")
    if spec["bug_old"] not in src:
        raise RuntimeError(
            f"{spec['slug']}: bug pattern not found in {spec['bug_file']} "
            f"at {spec['sha'][:10]} — upstream moved; re-pin"
        )
    src_path.write_text(
        src.replace(spec["bug_old"], spec["bug_new"], 1), encoding="utf-8"
    )
    test_path = dst / spec["test_file"]
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(spec["test_src"], encoding="utf-8")
    return dst


def build_all(root: Path) -> Dict[str, Path]:
    """Bake every multi-repo task under root/<slug>/; returns slug -> path."""
    out: Dict[str, Path] = {}
    for spec in MULTIREPO_TASKS:
        out[spec["slug"]] = bake_repo(spec, root / spec["slug"])
    return out


# ---------------------------------------------------------------------------
# Self-check (host, no Docker, no network after cache warm):
#   (1) buggy tree FAILS the regression target,
#   (2) canonical fix applied -> target passes AND pinned suite passes,
#   (3) the ablation issue text predicts a sane difficulty (not hard-
#       saturated — the v1 lesson; these are easy/medium bug reports).
# ---------------------------------------------------------------------------


def _pytest(repo: Path, node: Optional[str], suite_cmd: Optional[str] = None) -> bool:
    """Run host pytest in `repo` (stale-pyc safe, mirroring
    ablation_tasks._pytest). node None = run the suite command; else the
    suite command runs with the node appended — the same target-command
    semantics execution.verify uses for pinned test_commands.

    Dependency parity with the Docker deps image: the image pip-installs
    each repo's [project] dependencies; on the host we can't assume those
    packages exist, so PYTHONPATH gains the pinned-source clones of any
    cross-repo dependency (inflect needs more_itertools)."""
    import os as _os

    env = {**_os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    extra_paths = []
    if repo.name == "inflect-ordinal-teen":
        mi = next((t for t in MULTIREPO_TASKS if t["repo"] == "more-itertools"))
        clone = CACHE_ROOT / f"more-itertools-{mi['sha'][:10]}"
        if clone.exists():
            extra_paths.append(str(clone))
    if extra_paths:
        env["PYTHONPATH"] = _os.pathsep.join(
            extra_paths + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
        )
    base = (suite_cmd or "python -m pytest -q").split()
    # normalize "python -m pytest ..." to bare "..." for the subprocess
    # invocation (we always run pytest via sys.executable -m pytest)
    if base[:3] == ["python", "-m", "pytest"]:
        base = base[3:]
    argv = base + ([node] if node else [])
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"] + argv,
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
    )
    return r.returncode == 0


def check_all(root: Optional[Path] = None) -> bool:
    """Self-verify the multi-repo set on the host. Returns True iff all
    five repos: fail their target pre-fix, pass target+suite post-fix,
    and predict non-hard difficulty from the issue text alone.
    """
    from runtime.difficulty import heuristic_features, score_to_hint

    ok = True
    with tempfile.TemporaryDirectory(prefix="multirepo-check-") as tmp:
        base = Path(root) if root else Path(tmp)
        for spec in MULTIREPO_TASKS:
            slug = spec["slug"]
            repo = bake_repo(spec, base / slug)
            # (1) buggy: target must FAIL
            if _pytest(repo, spec["target"], spec["suite"]):
                print(f"[BAD ] {slug}: target PASSES on the buggy tree")
                ok = False
                continue
            # (2) canonical fix -> target passes
            mod = repo / spec["bug_file"]
            src = mod.read_text(encoding="utf-8")
            if spec["bug_new"] not in src:
                print(f"[BAD ] {slug}: fix pattern not found (bug edit failed)")
                ok = False
                continue
            mod.write_text(
                src.replace(spec["bug_new"], spec["bug_old"], 1), encoding="utf-8"
            )
            if not _pytest(repo, spec["target"], spec["suite"]):
                print(f"[BAD ] {slug}: target still failing after fix")
                ok = False
                continue
            # (2b) pinned suite passes post-fix
            if not _pytest(repo, None, spec["suite"]):
                print(f"[BAD ] {slug}: pinned suite not green post-fix")
                ok = False
                continue
            # (3) issue-text difficulty sane (not hard-saturated)
            feats = heuristic_features(spec["issue"])
            hint = score_to_hint(feats["score"])
            if hint == "hard":
                print(
                    f"[BAD ] {slug}: issue text hard-saturates the "
                    f"predictor (score {feats['score']}) — ON arm would "
                    f"always escalate"
                )
                ok = False
                continue
            print(
                f"[OK  ] {slug}: fails pre-fix, target+suite green "
                f"post-fix, issue predicts '{hint}' "
                f"(score {feats['score']})"
            )
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(prog="runtime.multirepo_tasks")
    ap.add_argument(
        "--check", action="store_true", help="self-verify the whole set on the host"
    )
    ap.add_argument(
        "--build-root",
        default=None,
        help="materialize the buggy repos under this dir and exit",
    )
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
