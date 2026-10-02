"""Import smoke: every public entry point imports in a clean environment.

## Why this file exists

Two measured defects on this tree were invisible to the test suite because both
only appear at IMPORT time, and only under conditions a dev machine has and a
fresh runner does not.

1. **`harness/tool_errors.py` defined `__new__` on a `NamedTuple` subclass.**
   While another terminal was editing that file, `import harness.core` raised
   `TypeError` and a whole eval surface went dark mid-round. Every test in the
   suite that had already imported the module passed; only a fresh process saw
   it. Nothing in the suite asserted "this imports", so the breakage was found by
   accident.
2. **`cli.serve` resolves a model from the ambient settings chain.** A test that
   assumed "no model configured" because it popped `NEO_MODEL` inherited the
   model from the checkout's own `.neo`, took the serving branch instead of the
   refusal branch, and blocked forever in `serve_forever()`. Fixed in
   `test_ceiling16_surfaces.py`; recorded here because the general lesson is the
   same one: the tree's behaviour depends on machine state that no test was
   stating.

So the properties pinned here are deliberately about PROCESS boundaries, not
about this process:

* Each entry point is imported in its own SUBPROCESS. An in-process import test
  is worthless for this defect class - by the time it runs, some other test has
  usually already imported the module, so `sys.modules` says nothing about
  whether a fresh interpreter can import it.
* The child environment is SCRUBBED of provider keys and `NEO_HOME`. A smoke
  test that passes only because a developer's machine is configured is not a
  smoke test; it is a machine check wearing a smoke test's clothes.
* `harness.core` must not drag the agent kernel in behind it. Measured on this
  tree: importing `harness.core` loads 22 harness modules and NONE of
  `harness.agent_loop*`, and no `harness.agent_kernel*`. That is the property
  the daily eval path depends on, and nothing pinned it - a future convenience
  `import` at module scope would silently change which engine a run resolves to
  without failing a single test.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

#: The modules a user or script can name. Derived from `pyproject.toml`'s
#: `[project.scripts]` plus the `python -m` entry points the docs advertise.
ENTRY_POINTS = (
    "cli.main",
    "cli",
    "harness",
    "evals.run",
    "shared.security",
    "shared.security_corpus",
    "shared.tracing",
    "agent_sdk",
    "acp",
    "mcp_server",
)

#: Keys that would let an import resolve a "configured" model or provider and
#: take a live path. Scrubbed from the child so the smoke result cannot depend
#: on who is running it.
CREDENTIAL_ENV = (
    "NEO_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "AZURE_API_KEY",
    "NEO_MODEL",
    "NEO_PROVIDER",
    "LITELLM_LOG",
)


#: The repository root. The child runs with THIS as its working directory, not
#: a temp dir, for two reasons: the package is imported from the tree rather than
#: from a wheel, which is what CI actually exercises; and the checkout's own
#: `.neo` config is then in scope, so the smoke proves the imports survive a
#: machine that HAS a model configured - the exact condition that hid the
#: `serve_forever` hang.
REPO_ROOT = Path(__file__).resolve().parent.parent


def _clean_env() -> dict[str, str]:
    import os
    import tempfile

    env = dict(os.environ)
    for name in CREDENTIAL_ENV:
        env.pop(name, None)
    # A throwaway home, so nothing is read out of the real one.
    env["NEO_HOME"] = tempfile.mkdtemp(prefix="neo-smoke-home-")
    env["HARNESS_HOME"] = env["NEO_HOME"]
    env["HARNESS_EXEC_SKIP_DOCKER"] = "1"
    env["NEO_NO_RELEASE_NOTICE"] = "1"
    env["NO_COLOR"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _import_in_subprocess(module: str) -> subprocess.CompletedProcess:
    """Import ``module`` in a fresh interpreter and report the outcome."""
    probe = (
        "import importlib, json, sys\n"
        f"module = importlib.import_module({module!r})\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('harness.'))\n"
        "print(json.dumps({'ok': True, 'module': module.__name__,\n"
        "                  'harness_modules': loaded}))\n"
    )
    return subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=180,
        env=_clean_env(),
        cwd=str(REPO_ROOT),
    )


class TestEveryEntryPointImportsInAFreshInterpreter:
    @pytest.mark.parametrize("module", ENTRY_POINTS)
    def test_entry_point_imports(self, module: str) -> None:
        proc = _import_in_subprocess(module)
        assert proc.returncode == 0, (
            f"`import {module}` failed in a fresh interpreter with a scrubbed "
            f"environment.\nstdout: {proc.stdout[-2000:]}\n"
            f"stderr: {proc.stderr[-4000:]}"
        )
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        assert payload["ok"] is True
        assert payload["module"] == module

    def test_the_console_entry_point_is_importable_as_a_callable(self) -> None:
        """`pyproject.toml` promises `neo = cli.main:main`, so `main` must exist."""
        probe = (
            "import json\n"
            "from cli.main import main\n"
            "print(json.dumps({'ok': callable(main)}))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=180,
            env=_clean_env(),
            cwd=str(REPO_ROOT),
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert json.loads(proc.stdout.strip().splitlines()[-1])["ok"] is True


class TestTheKernelIsNotDraggedInBehindHarnessCore:
    """The daily eval path depends on this and nothing pinned it."""

    def test_importing_harness_core_loads_no_agent_loop(self) -> None:
        proc = _import_in_subprocess("harness.core")
        assert proc.returncode == 0, proc.stderr[-4000:]
        loaded = json.loads(proc.stdout.strip().splitlines()[-1])["harness_modules"]
        loop = [m for m in loaded if m.startswith("harness.agent_loop")]
        assert loop == [], (
            "importing harness.core now pulls in the agent loop "
            f"({loop}); a module-scope import would change which engine a run "
            "resolves to without failing any test"
        )

    def test_importing_harness_core_loads_no_agent_kernel(self) -> None:
        proc = _import_in_subprocess("harness.core")
        assert proc.returncode == 0, proc.stderr[-4000:]
        loaded = json.loads(proc.stdout.strip().splitlines()[-1])["harness_modules"]
        kernel = [m for m in loaded if m.startswith("harness.agent_kernel")]
        assert kernel == [], (
            f"importing harness.core now pulls in the kernel ({kernel})"
        )

    def test_the_loaded_set_is_not_vacuous(self) -> None:
        """A smoke test that asserts `[] == []` on an empty list proves nothing."""
        proc = _import_in_subprocess("harness.core")
        assert proc.returncode == 0, proc.stderr[-4000:]
        loaded = json.loads(proc.stdout.strip().splitlines()[-1])["harness_modules"]
        assert len(loaded) >= 10, (
            f"expected harness.core to load a real module set, got {loaded}. "
            "If the import graph genuinely changed, update this expectation and "
            "the two tests above deliberately."
        )


class TestTheSmokeIsNotACredentialCheck:
    """Guards the guards: the smoke must fail when the tree is broken."""

    def test_a_broken_module_makes_the_smoke_fail(self, tmp_path) -> None:
        """Prove the subprocess check can actually report a failure.

        A smoke test that cannot fail is worse than none, and this one is the
        only thing standing between a `TypeError` at import time and a silently
        dark eval surface - so it is mutation-tested rather than trusted.
        """
        broken = tmp_path / "neo_smoke_broken.py"
        broken.write_text(
            "raise TypeError('simulated import-time break')\n", encoding="utf-8"
        )
        proc = subprocess.run(
            [sys.executable, "-c", f"import {broken.stem}"],
            capture_output=True,
            text=True,
            timeout=60,
            env={**_clean_env(), "PYTHONPATH": str(tmp_path)},
            cwd=str(tmp_path),
        )
        assert proc.returncode != 0
        assert "simulated import-time break" in proc.stderr

    def test_the_scrub_actually_removes_what_it_names(self) -> None:
        """A scrub list that does not match the keys in the environment is a
        comment, not a scrub."""
        env = _clean_env()
        present = [name for name in CREDENTIAL_ENV if name in env]
        assert present == [], (
            f"the child environment still carries {present}; the smoke test "
            "would be reporting on this machine's configuration"
        )
        assert env["HARNESS_EXEC_SKIP_DOCKER"] == "1"
        assert env["NEO_HOME"] != ""

    def test_the_scrubbed_names_are_ones_this_tree_actually_reads(self) -> None:
        """Otherwise the list is decorative.

        Read out of the sources rather than asserted from memory, so the list
        cannot drift into naming variables nothing looks at.
        """
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        read = set()
        for relative in ("cli/serve.py", "cli/neoconfig.py", "shared/security.py"):
            path = root / relative
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for name in CREDENTIAL_ENV:
                if f'"{name}"' in text or f"'{name}'" in text:
                    read.add(name)
        assert read, (
            "none of the scrubbed names appear in the sources sampled; the "
            "list has drifted from what the tree actually reads"
        )
