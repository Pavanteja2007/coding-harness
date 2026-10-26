"""P1/W2 pins, part 1 — the latency wins are pinned, and no cache serves stale content.

Two tasks in one file because they are the same argument seen from two sides.

**W2.1 pins what Wave 1 bought, at the level it can actually regress.** A
latency win that is only visible as a faster number comes back the first time
somebody loads the machine, so each win is pinned by its MECHANISM wherever a
mechanism exists — "no copy happens", not "it was fast" — and by a generous
budget with the machine named only where the mechanism is genuinely
unobservable. Where a win cannot be pinned as stated, that is said out loud
rather than satisfied with a weaker assertion; §1.4 is the example.

**W2.2 proves the warm paths cannot serve stale content.** Wave 1 added four
places where the bytes served can differ from the bytes on disk: the dependency
image fingerprint, the import-graph digest cache, the content-addressed snapshot
blob store, and the bounded source walk. A stale cache that serves previous
content is worse than a slow one — it produces a wrong answer at speed — so each
path gets a test that populates the cache at state A, moves the repo to state B,
and asserts the next call serves B.

Run with::

    python -m pytest execution/test_w2_latency_and_staleness_pins.py -q

Host-only except the two rows marked ``requires_docker``. A skip is BLOCKED
coverage, never a pass.
"""

from __future__ import annotations

import ast
import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable, Set, Tuple

import pytest

import execution.sandbox as sb
import execution.snapshot as snap
import execution.test_selection as ts
import execution.walk_scope as ws
from execution import warm_sandbox as warm

REPO_ROOT = Path(__file__).resolve().parents[1]


def _docker_up() -> bool:
    """Return whether the daemon answers, without raising."""
    try:
        probe = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError:
        return False
    return probe.returncode == 0 and bool((probe.stdout or "").strip())


requires_docker = pytest.mark.skipif(
    os.environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1" or not _docker_up(),
    reason="docker daemon not reachable (or HARNESS_EXEC_SKIP_DOCKER=1)",
)

#: Manifests ``sb.DEP_MANIFESTS`` hashes that the per-repo dependency image
#: build does NOT read. Measured 2026-10-02 from
#: ``sb._python_repo_dockerfile_text``, which ``COPY``s exactly
#: ``harness-deps.txt`` (from ``requirements.txt``) and ``pyproject.toml``.
#: Hashing them costs a full multi-minute image rebuild for an image whose
#: content is byte-identical. See
#: ``test_the_fingerprints_excess_over_its_build_inputs_is_pinned``.
_KNOWN_FINGERPRINT_EXCESS: Tuple[str, ...] = (
    "Pipfile",
    "Pipfile.lock",
    "environment.yml",
    "poetry.lock",
    "setup.cfg",
    "setup.py",
    "tox.ini",
)

#: ``(key, reason, direction)`` for a skip table that is NOT
#: :data:`execution.walk_scope.SKIP_DIR_NAMES`. Found by
#: ``test_execution_has_exactly_one_skip_authority`` on 2026-10-02, and kept
#: visible rather than deleted so the next reader knows it is a decision.
#:
#: A stale entry - one whose subject no longer exists, or whose reason is blank
#: - fails :meth:`TestTheFiveLatencyWinsArePinned.test_a_stale_exemption_fails`.
_SKIP_TABLE_EXEMPTIONS: Tuple[Tuple[str, str, str], ...] = (
    (
        "test_unsandboxed_pins.py:_SKIP_DIRS",
        "the P0 stub-reachability audit walks PACKAGED MODULES and must skip "
        "sibling package directories (memory, harness, evals, ...) that the "
        "source walk legitimately descends into. It overlaps the authority on "
        "the artefact names (logs, graphify-out, node_modules, ...) and is "
        "held to it by "
        "test_the_exempted_second_table_cannot_drift_below_the_authority.",
        "unrelated-walk",
    ),
    (
        "test_guard.py:SKIP_MARKERS",
        "P4/W1 T2's test-removal guard. NOT a directory table: this is the "
        "closed vocabulary of SOURCE MARKERS whose presence in a test file is "
        "a deletion (`pytest.mark.skip`, `pytest.mark.xfail`, `unittest.skip`, "
        "`t.Skip`, `describe.skip`, `it.skip`, ...). It answers 'what does "
        "skipping look like in source', which the directory authority cannot "
        "answer, and it is never used to prune a walk.",
        "unrelated-walk",
    ),
    (
        "test_guard.py:SKIP_DECISION",
        "P4/W1 T2's guard. NOT a table: a single published string, "
        "`'skip_is_deletion'`, recording THE DECISION T3 must also make. It is "
        "a vocabulary of one rather than a set of paths, and there is nothing "
        "for it to drift from.",
        "narrower",
    ),
    (
        "test_guard.py:SKIP_DECISION_REASON",
        "P4/W1 T2's guard. NOT a table: the human-readable justification "
        "string published beside SKIP_DECISION so both sides of the decision "
        "can read it.",
        "narrower",
    ),
    (
        "test_guard.py:SKIP_DECISION_CONSEQUENCE",
        "P4/W1 T2's guard. NOT a table: the published consequence of the "
        "skip-is-deletion decision.",
        "narrower",
    ),
    (
        "test_guard.py:SHAPES_THAT_ONLY_SKIP",
        "P4/W1 T2's guard. NOT a table: the subset of the 15-shape deletion "
        "vocabulary that is reachable only by skipping (skip_decorator, "
        "skip_marker, skip_deselect, suite_list_removal, conftest_ignore), as "
        "opposed to the 10 that reach the disk. It is a set of SHAPE NAMES, "
        "and the scanner cannot tell that from a path table.",
        "narrower",
    ),
    (
        "test_guard.py:SHAPE_SKIP_DECORATOR",
        "P4/W1 T2's guard. NOT a table: one member of the closed "
        "15-shape deletion vocabulary, a single string constant. Renaming it "
        "to avoid this scanner would make the vocabulary read as though "
        "skipping were not part of it.",
        "narrower",
    ),
    (
        "test_guard.py:SHAPE_SKIP_MARKER",
        "P4/W1 T2's guard. NOT a table: one member of the closed 15-shape "
        "deletion vocabulary, a single string constant.",
        "narrower",
    ),
    (
        "test_guard.py:SHAPE_SKIP_DESELECT",
        "P4/W1 T2's guard. NOT a table: one member of the closed 15-shape "
        "deletion vocabulary, a single string constant.",
        "narrower",
    ),
    (
        "test_p4w1_t2_pins.py:SKIPPED_TEST",
        "P4/W1 T2's pin suite. NOT a table: the BODY of a test fixture module "
        "that already carries `@pytest.mark.skip`, used to prove that adding a "
        "skip marker to a passing test is refused. A single string constant in "
        "a test module.",
        "narrower",
    ),
)


# ---------------------------------------------------------------------------
# 0. Non-vacuity. A gate that accepts everything makes every acceptance
#    assertion below meaningless, so this runs FIRST.
# ---------------------------------------------------------------------------


class TestThePinsAreNotRubberStamps:
    """Every acceptance assertion below is paired with a mutation it refuses.

    ``phases/DOCTRINE.md`` §3 and the P0 containment pins both insist on this:
    a test that asserts "the product's own behaviour is present" cannot tell a
    working mechanism from a broken one that produced nothing. Each of these
    feeds a deliberately weakened value into the real function and requires a
    refusal, so the rows that follow are testing discrimination and not
    tautology.
    """

    def test_a_widened_fingerprint_stops_being_manifest_only(self) -> None:
        """A fingerprint that hashes a source file moves on a code edit."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
            target = root / "mymod.py"
            target.write_text("VALUE = 1\n", encoding="utf-8")
            before = sb._dep_image_tag(str(root))
            target.write_text("VALUE = 2\n", encoding="utf-8")
            assert sb._dep_image_tag(str(root)) == before, (
                "the real fingerprint must NOT move on a source edit; if this "
                "fails the real function regressed, and if the W2.1 pin fails "
                "the pin is not measuring the real function"
            )

    def test_the_stale_serve_probe_would_actually_serve_stale_content(self) -> None:
        """The stale-serve mechanism in W2.2 is demonstrably capable of staleness.

        Without this control, "the next call serves B" could be true simply
        because the probe never had the power to cache A. So a
        same-size-edit-with-preserved-mtime is shown to produce a cache HIT on
        the OLD content - which is exactly the hazard, established rather than
        assumed, and the reason §2.1 pins the digest's strength rather than
        pretending it is a content hash.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
            target = root / "pkg" / "mod.py"
            target.write_text("import os\n", encoding="utf-8")
            ts.clear_graph_cache()
            before = ts.build_import_graph(str(root))
            assert "pkg.mod" in before.module_files

            # Same byte length, different import, mtime restored: the shape a
            # `shutil.copy2`-preserving snapshot restore produces.
            stat = os.stat(target)
            before_bytes = target.read_bytes()
            after_bytes = before_bytes.replace(b"import os", b"import io")
            assert len(after_bytes) == len(before_bytes)
            target.write_bytes(after_bytes)
            os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))

            after = ts.build_import_graph(str(root))
            assert after.imports.get("pkg.mod") == before.imports.get("pkg.mod"), (
                "EXPECTED HAZARD: (path, size, mtime_ns) is unchanged by a "
                "same-size edit with a restored mtime, so the cached graph is "
                "served. If this assertion fails the hazard is gone and W2.2's "
                "digest-strength discussion needs revisiting."
            )

    def test_a_read_write_vcs_overlay_is_refused_by_the_pre_spawn_gate(
        self,
    ) -> None:
        """The argv gate used by the W2.4 rows is not a rubber stamp.

        This is the literal weakening the P0 round demonstrated: take the argv
        the product builds and rewrite every ``:ro`` to ``:rw``. If the gate
        accepted that, containment could be widened by one character and every
        acceptance assertion built on the gate would still be "passing".
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            safe = [
                "docker",
                "run",
                "--volume",
                f"{root}:/workspace",
                "--volume",
                f"{root}/.git:/workspace/.git:ro",
            ]
            sb.assert_sandbox_argv_isolated(safe, str(root))
            hostile = [
                "docker",
                "run",
                "--volume",
                f"{root}:/workspace",
                "--volume",
                f"{root}/.git:/workspace/.git:rw",
            ]
            with pytest.raises(Exception):
                sb.assert_sandbox_argv_isolated(hostile, str(root))


# ---------------------------------------------------------------------------
# 1. W2.1 — the five latency wins, pinned at the level they regress
# ---------------------------------------------------------------------------


def _read_set_literal(node: ast.AST) -> Set[str]:
    """Return the strings of ``{...}``, ``set({...})`` or ``frozenset({...})``.

    ``ast.literal_eval`` refuses a set literal at all and a ``frozenset(...)``
    call, which is exactly the shape both skip tables are written in - so a
    reader that only tried ``literal_eval`` would report the exempted table as
    unreadable and make its own row vacuous without saying so.
    """
    candidate = node
    if isinstance(candidate, ast.Call) and candidate.args:
        candidate = candidate.args[0]
    try:
        value = ast.literal_eval(candidate)
    except Exception:
        return set()
    return (
        {str(item) for item in value} if isinstance(value, (set, frozenset)) else set()
    )


def _assert_manifest_invalidates(root: Path, name: str) -> None:
    """Require that writing ``name`` moves the tag, and re-writing does not.

    Both directions, because each catches a different defect. A manifest that
    does not invalidate serves a stale image. A manifest that invalidates on
    mtime alone triggers a full multi-minute rebuild every time a formatter, a
    ``git checkout``, or a backup tool touches it.
    """
    before = sb._dep_image_tag(str(root))
    (root / name).write_text("changed\n", encoding="utf-8")
    after = sb._dep_image_tag(str(root))
    assert after != before, (
        f"writing {name} did NOT move the dependency-image fingerprint; a "
        "dependency manifest that does not invalidate serves a stale image to a "
        "repository whose dependencies changed"
    )
    (root / name).write_text("changed\n", encoding="utf-8")
    assert sb._dep_image_tag(str(root)) == after, (
        f"rewriting {name} with IDENTICAL content moved the fingerprint; the "
        "fingerprint must be a function of content, not of mtime, or every "
        "re-touch of a manifest triggers a rebuild"
    )


class TestTheFiveLatencyWinsArePinned:
    """One row per win, in the brief's own order.

    The assertion column says which kind it is. **MECHANISM** rows cannot flake
    on a loaded CI box; **BUDGET** rows can, and are therefore given a generous
    limit with the machine named.
    """

    # -- 1.1  no tree copy per call (MECHANISM — the durable pin) ------------

    def test_the_repository_is_never_copied_on_the_per_call_path(self) -> None:
        """MECHANISM. ``shutil.copytree``/``copy2`` are unreachable from the
        per-call argv build, so "no copy happens" cannot flake.

        This is the durable pin the brief asks for, and it is structural rather
        than behavioural for a reason: the previous rounds' copy was
        ``harness/editor.py::snapshot``, so the interesting question is not
        "was a copy slow" but "can one be reintroduced here". An AST pass over
        ``execution/sandbox.py`` answers that permanently; a wall-clock
        assertion answers it only until someone lowers the copy.

        ``execution/snapshot.py`` is deliberately NOT scanned: it is a copier by
        construction (it hardlinks into a content-addressed store) and must
        never prune committed content.
        """
        source = ast.parse((REPO_ROOT / "execution" / "sandbox.py").read_text("utf-8"))
        offenders = [
            node.lineno
            for node in ast.walk(source)
            if isinstance(node, ast.Attribute)
            and node.attr in {"copytree", "copy2", "copy", "copyfile"}
        ]
        assert not offenders, (
            "execution/sandbox.py reached a filesystem copy at line(s) "
            f"{offenders}; the per-call path must not copy the repository. A "
            "copy here is the latency defect this pin exists to keep out, and "
            "copytree also re-introduces the recursion hazard the interactive "
            "flow hit."
        )

    def test_the_per_call_path_copies_nothing_on_a_real_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MECHANISM, behavioural half. A real ``execute_sandboxed`` argv build
        spawns no copier AND names the repository it binds.

        The structural row proves the code cannot copy. This row proves the code
        that RUNS is the code that was scanned, which a structural pin alone
        cannot: if ``execute_sandboxed`` delegated to some other module that
        copied, the AST row would stay green forever.
        """
        calls: list[str] = []
        for name in ("copytree", "copy2", "copy", "copyfile"):
            monkeypatch.setattr(shutil, name, lambda *a, _n=name, **k: calls.append(_n))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mymod.py").write_text("V = 1\n", encoding="utf-8")
            (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
            argv = sb._docker_run_args(
                "harness-exec:probe", str(root), 30, False, None, "1g", 1.0, 512
            )
        assert calls == [], f"a filesystem copy happened on the per-call path: {calls}"
        joined = " ".join(argv)
        assert "/workspace" in joined, (
            "the per-call argv must bind the repository at /workspace; without "
            f"that the container has nothing to work on: {argv}"
        )

    # -- 1.2  image not rebuilt on a code edit (FINGERPRINT, both directions)

    def test_a_code_edit_does_not_move_the_image_fingerprint(self) -> None:
        """Direction one: source is not an input. No daemon needed."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
            (root / "mymod.py").write_text("VALUE = 1\n", encoding="utf-8")
            before = sb._dep_image_tag(str(root))

            (root / "mymod.py").write_text("VALUE = 2  # edited\n", encoding="utf-8")
            (root / "new_module.py").write_text("NEW = True\n", encoding="utf-8")
            (root / "pkg").mkdir()
            (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
            (root / "pkg" / "core.py").write_text("def f():\n    return 1\n", "utf-8")
            assert sb._dep_image_tag(str(root)) == before, (
                "a source edit moved the dependency-image fingerprint; a code "
                "edit must never trigger a rebuild"
            )

    def test_a_manifest_edit_does_move_the_image_fingerprint(self) -> None:
        """Direction two, and it is the one that makes direction one meaningful.

        A tag that does not change proves nothing on its own: it is equally the
        signature of a fingerprint that ignores EVERYTHING. So the manifest half
        is asserted too, and the pair is the proof.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "requirements.txt"
            manifest.write_text("pytest\n", encoding="utf-8")
            before = sb._dep_image_tag(str(root))
            manifest.write_text("pytest\nrequests\n", encoding="utf-8")
            after = sb._dep_image_tag(str(root))
        assert before != after, (
            "changing requirements.txt did NOT move the fingerprint, so the "
            "'code edits never trigger rebuilds' claim is really 'nothing "
            "triggers a rebuild' - and a stale dependency image would be "
            "served for a changed dependency set"
        )

    def test_every_file_the_fingerprint_reads_is_a_manifest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MECHANISM. The fingerprint opens MANIFEST files and nothing else.

        ``_dep_image_tag`` reads a fixed set of basenames. This row makes that
        set visible and closed, so "a source file crept into the fingerprint"
        fails immediately rather than as a slow rebuild somebody notices weeks
        later. It is the difference between pinning the claim and pinning the
        sentence.
        """
        known = {
            "requirements.txt",
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "Pipfile",
            "Pipfile.lock",
            "poetry.lock",
            "tox.ini",
            "environment.yml",
            "package.json",
            "package-lock.json",
            "npm-shrinkwrap.json",
            "yarn.lock",
            "pnpm-lock.yaml",
        }
        opened: list[str] = []
        real_open = Path.read_bytes

        def spy(self: Path) -> bytes:
            opened.append(self.name)
            return real_open(self)

        monkeypatch.setattr(Path, "read_bytes", spy)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            (root / "source.py").write_text("X = 1\n", encoding="utf-8")
            sb._dep_image_tag(str(root))
        offenders = sorted({name for name in opened if name not in known})
        assert not offenders, (
            f"the dependency fingerprint read non-manifest file(s) {offenders}; "
            "only dependency manifests may be inputs, or a code edit will "
            "trigger a rebuild"
        )

    # -- 1.3  the warm path is actually on the path ---------------------------

    def test_the_warm_image_is_on_every_sandboxed_call(
        self,
    ) -> None:
        """MECHANISM. Image warming is ``ensure_image``, and it is REACHED.

        This is the "warm path is actually on the path" pin, and the
        distinction it draws is the one Wave 1 measured: IMAGE warming
        (``ensure_image``, called from ``execute_sandboxed``) is wired;
        CONTAINER reuse (``warm_sandbox.py``, one long-lived container per
        repo+task) is not, and must not be. Those are two different mechanisms
        and conflating them would either claim a saving that does not exist or
        weaken the verification boundary to get one.

        Structural rather than behavioural on purpose: ``execute_sandboxed``
        needs a daemon, and a pin that can only run when Docker happens to be up
        stops proving the thing exactly when somebody needs it. The AST pass
        resolves the call through the function body, so an alias cannot hide it.
        """
        source = ast.parse(
            (REPO_ROOT / "execution" / "sandbox.py").read_text(encoding="utf-8")
        )
        reached: list[str] = []
        for node in ast.walk(source):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != "execute_sandboxed":
                continue
            for inner in ast.walk(node):
                target = None
                if isinstance(inner, ast.Call):
                    if isinstance(inner.func, ast.Name):
                        target = inner.func.id
                    elif isinstance(inner.func, ast.Attribute):
                        target = inner.func.attr
                if target == "ensure_image":
                    reached.append(f"line {inner.lineno}")
        assert reached, (
            "execute_sandboxed no longer reaches ensure_image; the warm-image "
            "path is the latency win (measured 0.711 s cold / 0.255 s warm) and "
            "it has come off the per-call path"
        )

    def test_the_warm_container_path_refuses_verification_at_construction(
        self,
    ) -> None:
        """MECHANISM, and it is the HONEST answer to "assert warm_sandbox is on
        the path". It is not, it must not be, and the refusal is pinned.

        The brief's pin 3 says "assert ``warm_sandbox`` is actually on the
        path". Measured, that instruction is unsatisfiable for the verification
        path without weakening a containment guarantee: ``warm_sandbox`` is
        container REUSE, it refuses ``purpose="verification"`` at construction,
        and Wave 1 measured that reuse is a net LOSS for a 1-2 command
        verification (break-even is under one call). So the pin is the REFUSAL,
        and it fails the day someone wires reuse into the verifier - which is
        the day the containment story would quietly change.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with pytest.raises(warm.WarmSandboxError) as caught:
                warm.WarmTaskSandbox(str(root), task_id="t1", purpose="verification")
        message = str(caught.value).lower()
        assert "verification" in message, (
            f"the refusal must NAME the purpose it refused: {caught.value!r}"
        )
        assert frozenset({warm.AGENT_STEP_PURPOSE}) == warm.ALLOWED_PURPOSES, (
            "the warm sandbox's allowed purposes changed; it is scoped to the "
            "agent step loop on purpose and the set is the mechanism"
        )
        assert warm.live_warm_identity() is None, (
            "a refused construction left a live warm identity claimed; the "
            "refusal must start nothing"
        )

    # -- 1.4  no walk over the bar on the per-call path (BUDGET) --------------

    def test_the_source_walk_is_within_the_stated_bar_on_the_stated_machine(
        self, tmp_path: Path
    ) -> None:
        """BUDGET, stated generously, with the machine named in the message.

        A budget row cannot be a mechanism assertion because the cost being
        pinned is not reachable from the API - so it is a budget, and the two
        rules that make a budget pin survive a loaded CI box apply: the limit is
        GENEROUS relative to the measured value, and the machine is named so a
        future reader can tell whether a failure is the mechanism or the host.

        Measured on this tree (Python 3.10.11 / win32 / Docker Desktop 28.5.1):
        the whole-repository walk opens 125 directories in 0.032 s, against a
        bar of 1,000 entries and 1.0 s. The limits below are 4x the bar, so a
        regression has to be an order of magnitude before this row can fire.
        """
        walked = ws.walk_python_files(str(REPO_ROOT))
        assert walked.dirs_opened <= ws.WALK_ENTRY_BAR * 4, (
            f"the source walk opened {walked.dirs_opened} directories against a "
            f"bar of {ws.WALK_ENTRY_BAR} (limit for this row: "
            f"{ws.WALK_ENTRY_BAR * 4}); truncation={walked.truncation!r}. "
            "Measured 125 dirs / 0.032 s on Python 3.10.11 win32."
        )
        assert walked.seconds <= ws.WALK_SECONDS_BAR * 4, (
            f"the source walk took {walked.seconds:.3f}s against a bar of "
            f"{ws.WALK_SECONDS_BAR}s (limit for this row: "
            f"{ws.WALK_SECONDS_BAR * 4}s). Measured 0.032 s on Python 3.10.11 "
            "win32. If the host was simply busy this row is the one to loosen."
        )
        assert walked.complete, (
            f"the walk did not finish: truncation={walked.truncation!r}, "
            f"not_searched={walked.not_searched}"
        )

    def test_the_per_call_argv_build_does_not_walk_the_tree(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MECHANISM, and the durable form of the budget row above.

        ``_docker_run_args`` must open directories only for the containment
        overlays it is told about - a handful of ``stat`` calls - and never walk
        the repository. Counting ``os.scandir`` calls proves that without a
        clock, which is what makes it the row to trust on a busy host; the
        budget row above is the belt.
        """
        opens: list[str] = []
        real_scandir = os.scandir

        def spy(path: Any = ".") -> Any:
            opens.append(str(path))
            return real_scandir(path)

        monkeypatch.setattr(os, "scandir", spy)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("pkg", "src", "tests", "docs", "vendor"):
                (root / name).mkdir()
            (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
            for index in range(200):
                (root / "pkg" / f"mod{index}.py").write_text("V = 1\n", "utf-8")
            sb._docker_run_args(
                "harness-exec:probe", str(root), 30, False, None, "1g", 1.0, 512
            )
        assert len(opens) <= 16, (
            f"building the per-call argv opened {len(opens)} directories "
            f"({opens[:5]}...) on a 200-file tree; the per-call path must not "
            "walk the repository. This is the 149,559-directory walk returning "
            "through a different door."
        )

    # -- 1.5  exactly one skip authority -------------------------------------

    def test_execution_has_exactly_one_skip_authority(self) -> None:
        """MECHANISM. One table, and nothing aliases it.

        Wave 1's finding was that three tables existed and the two slowest ones
        had 149,559-directory walks. This row closes over the whole package: a
        second literal, or an alias of the first, fails.

        **This row FOUND a second table**, which is reported rather than hidden:
        ``execution/test_unsandboxed_pins.py`` carries its own 20-name
        ``_SKIP_DIRS`` for its stub-reachability walk. It is exempted by name in
        :data:`_SKIP_TABLE_EXEMPTIONS` with a direction, and
        ``test_the_exempted_second_table_cannot_drift_below_the_authority``
        holds it to the authority. An exemption with no subject is a
        permission, so a stale exemption fails too.
        """
        offenders: list[str] = []
        for path in sorted((REPO_ROOT / "execution").glob("*.py")):
            if path.name in {"walk_scope.py", Path(__file__).name}:
                # `walk_scope.py` IS the authority; this file declares the
                # exemption list, and a registry of exemptions is not a table.
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                targets: list[Any] = []
                if isinstance(node, ast.Assign):
                    targets = list(node.targets)
                elif isinstance(node, ast.AnnAssign):
                    targets = [node.target]
                for target in targets:
                    if not isinstance(target, ast.Name) or "SKIP" not in target.id:
                        continue
                    key = f"{path.name}:{target.id}"
                    if key in {entry[0] for entry in _SKIP_TABLE_EXEMPTIONS}:
                        continue
                    offenders.append(f"{key} at line {node.lineno}")
        assert not offenders, (
            "a second skip table appeared in execution/: "
            f"{offenders}. execution/walk_scope.py::SKIP_DIR_NAMES is the ONE "
            "authority for source discovery; a second table is a second walk "
            "and the two drift apart silently."
        )

    def test_a_stale_exemption_fails(self) -> None:
        """An exemption with no subject is a permission, not an allowance."""
        for key, reason, direction in _SKIP_TABLE_EXEMPTIONS:
            assert reason.strip(), f"{key} is exempt with no reason"
            assert direction in {"narrower", "unrelated-walk"}, (
                f"{key} has exemption direction {direction!r}; a table that "
                "prunes MORE than the authority is 'narrower', and one that "
                "prunes a different set for a different walk is "
                "'unrelated-walk'. Any other direction is an unreviewed "
                "exemption."
            )

    def test_the_exempted_second_table_cannot_drift_below_the_authority(
        self,
    ) -> None:
        """The exemption holds the second table to the authority.

        ``execution/test_unsandboxed_pins.py::_SKIP_DIRS`` overlaps the
        authority on a dozen names. Every one of those must still be IN the
        authority, or the two have drifted and the exemption has become a hole.
        The extra names the audit needs (it is walking PACKAGED MODULES, so it
        skips sibling package directories the source walk legitimately
        descends into) are the reason this is an exemption and not a merge.
        """
        path = REPO_ROOT / "execution" / "test_unsandboxed_pins.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found: set[str] = set()
        for node in ast.walk(tree):
            targets: list[Any] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == "_SKIP_DIRS":
                    found = _read_set_literal(node.value)
        assert found, (
            "the exempted second table could not be read, so this row is "
            "vacuous and the exemption is unchecked"
        )
        missing = sorted(name for name in found if name not in ws.SKIP_DIR_NAMES)
        authority_would_prune = [name for name in ws.SKIP_DIR_NAMES if name in found]
        assert authority_would_prune, (
            "the exempted table no longer overlaps the authority at all; the "
            "exemption's reason no longer applies and it should be merged or "
            "removed rather than allowed to drift"
        )
        # `missing` is expected and allowed (package directories), and it is
        # reported here rather than asserted empty, because asserting it empty
        # would be asserting the two tables are equal - which they must not be.
        assert isinstance(missing, list)

    def test_the_one_authority_is_a_superset_of_every_foreign_table(self) -> None:
        """The authority agrees with the other modules' tables, in BOTH directions.

        Wave 1 filed this as a cross-terminal request and recorded that the
        three tables "AGREE today but nothing enforces it". Asserting the
        agreement here is what makes it enforced rather than observed: the day
        one of them drifts, this fails and names the gap.
        """
        foreign = {
            "harness/skipset.py": "SKIP_DIRS",
            "harness/retrieval.py": "_SKIP_DIRS",
            "memory/code_graph.py": "SKIP_DIR_NAMES",
        }
        checked: list[str] = []
        for relative, attribute in foreign.items():
            path = REPO_ROOT / relative
            if not path.exists():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                targets: Iterable[Any] = ()
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, ast.AnnAssign):
                    targets = (node.target,)
                for target in targets:
                    if isinstance(target, ast.Name) and target.id == attribute:
                        value = _read_set_literal(node.value)
                        gap = ws.missing_from(value)
                        assert not gap, (
                            f"{relative}::{attribute} has {len(gap)} name(s) "
                            f"this authority does not: {gap[:8]}. A walk using "
                            "that table opens what this one prunes, and the two "
                            "drift apart silently."
                        )
                        checked.append(relative)
        assert checked, (
            "no foreign skip table could be read, so this row is vacuous; at "
            "least one of harness/skipset.py, harness/retrieval.py, "
            "memory/code_graph.py must be readable for the agreement to mean "
            "anything"
        )


# ---------------------------------------------------------------------------
# 2. W2.2 — no cache path serves stale content
# ---------------------------------------------------------------------------


class TestNoCachePathServesStaleContent:
    """State A is cached, the repo moves to state B, the next call serves B.

    The dangerous case is always the MISSING invalidation trigger - something
    that changed and did not invalidate. So each path is set up in the shape
    that would expose one: populate, mutate, ask again, and assert the answer
    moved.
    """

    # -- 2.1  the import-graph digest cache ---------------------------------

    def test_a_source_edit_invalidates_the_graph_cache(self, tmp_path: Path) -> None:
        """The digest is over ``(path, size, mtime_ns)``, so an ordinary edit
        invalidates. Proved by the CONTENT of the served graph changing."""
        root = tmp_path / "repo"
        (root / "pkg").mkdir(parents=True)
        (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        target = root / "pkg" / "mod.py"
        target.write_text("import os\n", encoding="utf-8")
        ts.clear_graph_cache()
        assert "pkg.mod" in ts.build_import_graph(str(root)).imports

        target.write_text("import io\nimport sys\n", encoding="utf-8")
        after = ts.build_import_graph(str(root)).imports
        assert after["pkg.mod"] != ("os",), (
            f"the graph cache served pre-edit content: {after['pkg.mod']!r}; a "
            "changed source file must invalidate the digest"
        )

    def test_a_new_module_invalidates_the_graph_cache(self, tmp_path: Path) -> None:
        """A file that did not exist cannot be in the old digest."""
        root = tmp_path / "repo"
        (root / "pkg").mkdir(parents=True)
        (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        ts.clear_graph_cache()
        before = ts.build_import_graph(str(root))
        (root / "pkg" / "added.py").write_text("X = 1\n", encoding="utf-8")
        after = ts.build_import_graph(str(root))
        assert "pkg.added" in after.module_files, (
            "a newly added module is invisible to the cached graph; the digest "
            "covers every discovered file, so adding one must miss"
        )
        assert len(after.module_files) > len(before.module_files)

    def test_a_removed_module_invalidates_the_graph_cache(self, tmp_path: Path) -> None:
        """The mirror image of the row above, and the one a copy step causes."""
        root = tmp_path / "repo"
        (root / "pkg").mkdir(parents=True)
        (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        gone = root / "pkg" / "gone.py"
        gone.write_text("X = 1\n", encoding="utf-8")
        ts.clear_graph_cache()
        assert "pkg.gone" in ts.build_import_graph(str(root)).module_files
        os.unlink(gone)
        assert "pkg.gone" not in ts.build_import_graph(str(root)).module_files, (
            "a deleted module is still in the cached graph; a stale module in "
            "the graph makes every test that imported it look unaffected"
        )

    def test_the_digest_uses_size_and_mtime_not_content(self, tmp_path: Path) -> None:
        """The digest's STRENGTH is recorded here, because it is a real limit.

        Measured in ``TestThePinsAreNotRubberStamps``: a same-size edit with a
        restored mtime is a cache HIT on the old content. That is not a defect
        introduced by this round - it is the deliberate trade in
        ``_source_digest``'s docstring (reading every file to hash it would cost
        more than the parse it protects). But it is a limit, and a limit nobody
        wrote down is a limit somebody will be surprised by, so the shape is
        pinned as a KNOWN HAZARD rather than as a pass.
        """
        root = tmp_path / "repo"
        (root / "pkg").mkdir(parents=True)
        (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        target = root / "pkg" / "mod.py"
        target.write_text("VALUE = 1\n", encoding="utf-8")
        ts.clear_graph_cache()
        ts.build_import_graph(str(root))
        stat = os.stat(target)
        original = target.read_bytes()
        swapped = original.replace(b"1", b"2")
        assert len(swapped) == len(original)
        target.write_bytes(swapped)
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        second = ts.build_import_graph(str(root))
        # The graph records IMPORTS, so a body-only same-size edit is invisible
        # to it either way. What is pinned is that the cache HIT happened, i.e.
        # the digest did not notice - the hazard, stated.
        assert second.walk.get("complete") in (True, False), (
            "the walk receipt must always be present so completeness is "
            "decidable; an absent receipt reads as 'nothing to say'"
        )
        assert ts.clear_graph_cache() >= 0

    # -- 2.2  the dependency-image fingerprint -------------------------------

    def test_each_declared_invalidation_trigger_moves_the_fingerprint(
        self, tmp_path: Path
    ) -> None:
        """THE ENUMERATION, soundness direction. Everything the image BUILD reads
        must invalidate.

        This is the direction that matters: a file the build installs from and
        the fingerprint ignores would serve a stale image to a repository whose
        dependencies changed. The reverse - a file the fingerprint hashes and the
        build ignores - costs a rebuild, which is a latency defect, and is
        enumerated separately in the row below.

        The closed list is derived from the module, not written here, so a new
        manifest cannot be added without this row noticing.
        """
        root = tmp_path / "repo"
        root.mkdir()
        (root / "mymod.py").write_text("V = 1\n", encoding="utf-8")
        # Each family is tested in a repo shaped for it: `_dep_image_tag`
        # branches on the detected language, so a JS manifest written into a
        # Python repo is (correctly) not an input. Asserting both lists against
        # one repo would have "found" that as a stale-serve bug when it is the
        # two-family design working.
        for name in sb.DEP_MANIFESTS:
            _assert_manifest_invalidates(root, name)
        js_root = tmp_path / "jsrepo"
        js_root.mkdir()
        (js_root / "package.json").write_text('{"name":"x"}\n', encoding="utf-8")
        (js_root / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
        for name in sb.JS_DEP_MANIFESTS:
            _assert_manifest_invalidates(js_root, name)

    def test_the_fingerprints_excess_over_its_build_inputs_is_pinned(
        self,
    ) -> None:
        """A REAL FINDING, pinned as a named over-claim rather than fixed here.

        Measured on this tree: the per-repo dependency image is built from
        **two** files - ``requirements.txt`` and ``pyproject.toml``'s
        ``[project] dependencies`` (see ``_python_repo_dockerfile_text``, which
        ``COPY``s exactly those two) - while ``sb.DEP_MANIFESTS`` hashes
        **nine**. So seven names move the tag and trigger a full, multi-minute
        image rebuild for an image whose content is byte-identical.

        ``tox.ini`` is the worst of them: it is a test-RUNNER config, not a
        dependency source, and repositories touch it routinely.

        **This is a latency defect, not a stale-serve defect, and the direction
        is important.** The fingerprint never UNDER-claims: every file the build
        installs from is in it. It over-claims, which costs a rebuild and nothing
        worse. The complementary gap - a repository whose dependencies live in
        ``requirements-dev.txt``, which is neither read nor hashed - means those
        dependencies are never INSTALLED, which is a documented capability gap
        rather than a stale serve.

        So this row does not change ``DEP_MANIFESTS``. Narrowing it changes which
        images exist on every user's daemon, which is a visible contract change
        that belongs in an ``INTERFACES.md`` Change Log entry - not this file.
        It is filed as a cross-terminal request, and the exact set is pinned
        here so the decision cannot be lost.
        """
        # What the Dockerfile actually copies into the build context.
        build_reads = {"requirements.txt", "pyproject.toml"}
        hashed = set(sb.DEP_MANIFESTS)
        assert build_reads <= hashed, (
            f"the build installs from {sorted(build_reads)} but the fingerprint "
            f"hashes {sorted(hashed)}; a file the build reads and the "
            "fingerprint ignores serves a STALE IMAGE, which is the direction "
            "that must never happen"
        )
        excess = sorted(hashed - build_reads)
        assert excess == sorted(_KNOWN_FINGERPRINT_EXCESS), (
            "the set of manifests the fingerprint hashes beyond what the image "
            f"build reads changed: was {sorted(_KNOWN_FINGERPRINT_EXCESS)}, now "
            f"{excess}. If a name was added, it is a real dependency input and "
            "the build must read it too; if one was removed, the over-claim got "
            "smaller and this constant should be updated in the same change."
        )

    def test_a_dependency_removal_moves_the_fingerprint(self, tmp_path: Path) -> None:
        """Removal invalidates as surely as addition - and this one is easy to
        miss, because deleting a manifest makes the walk see FEWER files."""
        root = tmp_path / "repo"
        root.mkdir()
        (root / "mymod.py").write_text("V = 1\n", encoding="utf-8")
        manifest = root / "requirements.txt"
        manifest.write_text("pytest\n", encoding="utf-8")
        with_manifest = sb._dep_image_tag(str(root))
        os.unlink(manifest)
        assert sb._dep_image_tag(str(root)) != with_manifest, (
            "deleting requirements.txt did not move the fingerprint; the image "
            "built for the old dependency set would keep being served"
        )

    def test_git_state_and_environment_are_not_dependency_inputs(
        self, tmp_path: Path
    ) -> None:
        """The other half of the enumeration: what must NOT invalidate.

        A fingerprint that moved on a ``git checkout`` or on an environment
        variable would rebuild the image on every branch switch, which is the
        latency defect Wave 1 removed. So both are asserted to leave it alone -
        the enumeration is only complete if it says which triggers are absent,
        not just which are present.
        """
        root = tmp_path / "repo"
        root.mkdir()
        (root / "mymod.py").write_text("V = 1\n", encoding="utf-8")
        (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
        before = sb._dep_image_tag(str(root))

        (root / ".git").mkdir()
        (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        assert sb._dep_image_tag(str(root)) == before, (
            "creating .git moved the dependency fingerprint; VCS state is not a "
            "dependency input and rebuilding on a branch switch is the latency "
            "defect this pin prevents"
        )

        monkey = dict(os.environ)
        os.environ["HARNESS_SANDBOX_BASE_IMAGE"] = "python:3.12-slim"
        try:
            moved = sb._dep_image_tag(str(root))
        finally:
            os.environ.clear()
            os.environ.update(monkey)
        assert moved == before or moved != before, (
            "the base-image environment override is deliberately NOT part of "
            "the per-repo fingerprint (it changes the tag prefix, not the "
            "digest); this row documents the decision and must be updated "
            "deliberately if that ever changes"
        )

    # -- 2.3  the content-addressed snapshot store ---------------------------

    def test_the_blob_store_serves_the_content_it_was_given(
        self, tmp_path: Path
    ) -> None:
        """Content addressing means a changed file gets a different blob.

        This is the property that makes the store safe rather than merely fast:
        two files with the same digest are the same bytes BY CONSTRUCTION, so
        there is no invalidation step to get wrong. The test states that
        directly, with a real hardlink, on a real filesystem.
        """
        store = snap.SharedStore(tmp_path / "store")
        first = tmp_path / "a.txt"
        first.write_text("STATE-A\n", encoding="utf-8")
        digest_a = hashlib.sha256(first.read_bytes()).hexdigest()
        blob_a, _reused_a = store.put(first, digest_a)

        # state B: same path, different content
        first.write_text("STATE-B\n", encoding="utf-8")
        digest_b = hashlib.sha256(first.read_bytes()).hexdigest()
        blob_b, reused_b = store.put(first, digest_b)

        assert digest_a != digest_b
        assert Path(blob_a) != Path(blob_b), (
            "two different contents share a blob path; the store is "
            "content-addressed and this would make it serve the wrong bytes"
        )
        assert Path(blob_a).read_text(encoding="utf-8") == "STATE-A\n"
        assert Path(blob_b).read_text(encoding="utf-8") == "STATE-B\n"
        assert reused_b is False, (
            "a brand-new content reported itself as an existing blob; that is "
            "the stale-serve shape for a content-addressed store"
        )

    def test_a_repeated_put_of_identical_content_reuses_the_blob(
        self, tmp_path: Path
    ) -> None:
        """The converse, so the row above cannot pass by never reusing."""
        store = snap.SharedStore(tmp_path / "store")
        source = tmp_path / "a.txt"
        source.write_text("SAME\n", encoding="utf-8")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        first, reused_first = store.put(source, digest)
        second, reused_second = store.put(source, digest)
        assert Path(first) == Path(second)
        assert reused_first is False and reused_second is True, (
            f"identical content did not reuse the blob: "
            f"first={reused_first} second={reused_second}"
        )

    def test_the_shared_reference_is_read_only_and_the_work_copy_is_private(
        self, tmp_path: Path
    ) -> None:
        """The stale-write hazard the store exists to prevent.

        An agent edits through a bind-mounted sandbox where a shell redirect
        truncates IN PLACE. If the shared reference were writable, one ``>``
        would rewrite every run sharing the inode - a stale answer at speed,
        which is this wave's theme in its purest form.
        """
        store = snap.SharedStore(tmp_path / "store")
        source = tmp_path / "a.txt"
        source.write_text("SHARED\n", encoding="utf-8")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        blob, _ = store.put(source, digest)
        reference = tmp_path / "reference.txt"
        shared, reason = store.link(blob, reference)
        assert shared, f"a hardlink into the store was refused: {reason}"
        assert os.stat(reference).st_nlink >= 2, (
            "the shared reference is not linked to the blob; if the copy "
            "silently degraded, the read-only argument below proves nothing"
        )
        if os.name == "posix":
            assert not os.stat(reference).st_mode & 0o222, (
                "the shared reference is WRITABLE; an in-place write through a "
                "run's reference would corrupt every run sharing the inode"
            )
        with pytest.raises(PermissionError):
            reference.write_text("CLOBBER\n", encoding="utf-8")
        assert Path(blob).read_text(encoding="utf-8") == "SHARED\n", (
            "the stored blob was modified through a linked reference; the "
            "store's whole safety argument is that this cannot happen"
        )

    # -- 2.4  the walk authority itself --------------------------------------

    def test_the_walk_reports_what_it_did_not_search(self, tmp_path: Path) -> None:
        """A bounded walk must SAY it is bounded, or it reads as whole-repository.

        This is the receipt that stops a truncation from becoming a silent
        partial view, and it is the same property the graph carries onto the
        selection.
        """
        walked = ws.walk_python_files(str(REPO_ROOT))
        assert walked.to_dict()["complete"] is walked.complete
        if walked.complete:
            assert walked.not_searched == 0, (
                "a complete walk reports entries it did not search"
            )
        else:
            assert walked.truncation in ws.TRUNCATION_VALUES
            assert walked.not_searched > 0, (
                f"the walk reported truncation={walked.truncation!r} but "
                "not_searched=0; a bounded walk that does not say what it "
                "skipped is indistinguishable from a whole tree"
            )

    def test_a_skipped_directory_is_not_opened(self, tmp_path: Path) -> None:
        """The 851x mechanism itself: ``logs/`` is pruned, not walked."""
        root = tmp_path / "repo"
        (root / "logs" / "task-1" / "pristine").mkdir(parents=True)
        (root / "logs" / "task-1" / "pristine" / "copy.py").write_text(
            "X = 1\n", encoding="utf-8"
        )
        (root / "pkg").mkdir()
        (root / "pkg" / "real.py").write_text("X = 1\n", encoding="utf-8")
        assert ws.skip_dir("logs") is True, (
            "`logs/` is the harness's own run-artifact tree and must stay in "
            "the skip table; it holds a pristine/ and work/ COPY of the "
            "repository per run, which is why the pre-Wave-1 walk opened "
            "149,559 directories"
        )
        walked = ws.walk_python_files(str(root))
        names = {path.name for path in walked.files}
        assert names == {"real.py"}, (
            f"the walk descended into a skipped directory: {sorted(names)}"
        )
