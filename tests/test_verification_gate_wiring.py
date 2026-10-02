"""R2-01 — the verification-intelligence delegation seam in `execution.verify`.

Five required proofs, each named after the behaviour it pins:

1. ``test_a_run_with_no_intelligence_config_is_byte_identical`` — a run with no
   intelligence key never enters the pipeline and is byte-identical, including
   its report rows.
2. ``test_a_ledger_enabled_run_whose_obligation_the_diff_does_not_satisfy_
   does_not_mint`` — a green visible suite plus a sealed spec naming a test the
   diff leaves failing does NOT mint success.
3. ``test_an_independent_judge_pass_cannot_override_a_failing_target_test`` —
   the judge accepting the claim while the target test fails still yields a
   non-minting result, with BOTH rungs visible.
4. ``test_a_corrupt_ledger_degrades_with_a_recorded_reason`` — an unreadable
   and an unsealed obligation set both refuse, with the reason attached, and
   neither reads as a pass.
5. ``test_every_verdict_names_the_rung_that_produced_it`` — every gate row
   carries a rung in the receipt, in ``raw_output``, and in the unified trace.

The Docker-gated class drives the real sandbox: the suites, the obligation
sweep, and the held-out judge all run in containers. A skip here is BLOCKED
coverage, never a pass.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

import execution.verification_gate as vg
import execution.verify as vf

REPO_ROOT = Path(__file__).resolve().parents[1]


def _docker_up() -> bool:
    """Return whether the Docker daemon answers; a skip is BLOCKED, not a pass."""
    try:
        cp = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return cp.returncode == 0 and bool(cp.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    os.environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1" or not _docker_up(),
    reason="docker daemon not reachable (or HARNESS_EXEC_SKIP_DOCKER=1)",
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

_APP = "def add(a, b):\n    return a + b\n"

# The visible suite. ``pytest.ini`` pins testpaths to ``visible/`` so the
# obligation file is INVISIBLE to a bare suite run and reachable only by an
# explicit node-id selection. That is what makes proof 2 a real refusal rather
# than a suite that was already red.
_PYTEST_INI = "[pytest]\ntestpaths = visible\n"

_CONFTEST = (
    "import os\nimport sys\n\n"
    "_ROOT = os.path.dirname(os.path.abspath(__file__))\n"
    "if _ROOT not in sys.path:\n    sys.path.insert(0, _ROOT)\n"
)


def _make_repo(
    root: Path, *, failing_obligation: bool, failing_target: bool = False
) -> Path:
    """Materialize the fixture repository used by the Docker-gated proofs.

    Assumes ``root`` does not exist yet or is empty. Returns the repo path.
    ``failing_obligation`` controls the hidden obligation test;
    ``failing_target`` adds a failing test INSIDE the visible suite so proof 3
    has a genuinely red baseline to refuse.
    """
    repo = root / "repo"
    (repo / "visible").mkdir(parents=True)
    (repo / "obligation").mkdir(parents=True)
    (repo / "app.py").write_text(_APP, encoding="utf-8", newline="\n")
    (repo / "pytest.ini").write_text(_PYTEST_INI, encoding="utf-8", newline="\n")
    (repo / "conftest.py").write_text(_CONFTEST, encoding="utf-8", newline="\n")
    (repo / "visible" / "test_app.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
        newline="\n",
    )
    if failing_target:
        (repo / "visible" / "test_broken.py").write_text(
            "def test_broken():\n    assert 1 == 2\n", encoding="utf-8", newline="\n"
        )
    obligation = (
        "def test_never_passes():\n    assert True\n"
        if not failing_obligation
        else ("def test_never_passes():\n    assert False\n")
    )
    (repo / "obligation" / "test_contract.py").write_text(
        obligation, encoding="utf-8", newline="\n"
    )
    return repo


def _sealed_spec(root: Path, *, items: List[Dict[str, Any]]) -> Path:
    """Write a sealed spec artifact under ``root`` and return ``root``.

    Assumes ``items`` are already ``{id, title, acceptance, tests}`` mappings.
    Sealing is what makes the obligation set protected: the seal deliberately
    excludes ``passes``, so the guard fails on a removed/added/mutated item and
    never on an honest progress flip.
    """
    from execution.spec_ledger import SpecLedger, guard_paths

    root.mkdir(parents=True, exist_ok=True)
    artifact, seal = guard_paths(str(root))
    ledger = SpecLedger.create("r2-01", items)
    ledger.save(artifact, seal=True)
    assert os.path.isfile(seal)
    return root


_TWO_ITEMS = [
    {
        "id": "visible_suite",
        "title": "the visible suite is green",
        "acceptance": ["the visible test passes"],
        "tests": ["visible/test_app.py::test_add"],
    },
    {
        "id": "hidden_contract",
        "title": "the sealed contract holds",
        "acceptance": ["the contract test passes"],
        "tests": ["obligation/test_contract.py::test_never_passes"],
    },
]


def _gate_rows(reports: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return only the rows this module contributed to ``reports``."""
    return [row for row in reports if row.get("source") == "verification_gate"]


def _rung_rows(reports: List[Dict[str, Any]], rung: str) -> List[Dict[str, Any]]:
    """Return the gate rows for one rung."""
    return [row for row in _gate_rows(reports) if row.get("rung") == rung]


_TIMING_LINE = re.compile(r"in \d+\.\d+s")


def _comparable_output(raw: str) -> str:
    """Return ``raw`` with pytest's wall-clock timings normalized away.

    ``raw_output`` is the captured pytest transcript, and pytest stamps its own
    duration into it, so two runs of the IDENTICAL code path differ in those
    digits. Byte-identity here means the bytes this boundary is responsible
    for: the commands issued, their exit codes, the verdicts, and the report
    rows. The timing stamp is the test runner's, not the verifier's, so it is
    normalized rather than asserted.
    """
    return _TIMING_LINE.sub("in <t>s", raw)


def _command_blocks(raw: str) -> List[str]:
    """Return the ``$ <command>\\nexit=<code>...`` headers, in order."""
    return [
        line
        for line in raw.splitlines()
        if line.startswith("$ ") or line.startswith("exit=")
    ]


# ---------------------------------------------------------------------------
# 1. byte-identical when nothing is configured
# ---------------------------------------------------------------------------


class TestAbsentConfigurationIsByteIdentical:
    def test_a_run_with_no_intelligence_config_is_byte_identical(
        self, tmp_path, monkeypatch
    ):
        """No intelligence key => the pipeline is never entered, bit for bit.

        The three calls differ only in how the (absent) configuration is
        expressed, and all three results plus their report lists must be equal
        down to the bytes of ``raw_output``. A monkeypatched pipeline that
        raises on entry makes "never entered" an assertion rather than an
        inference from the output.
        """
        repo = tmp_path / "repo"
        (repo / "tests").mkdir(parents=True)
        (repo / "app.py").write_text(_APP, encoding="utf-8", newline="\n")
        (repo / "tests" / "test_app.py").write_text(
            "from app import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
            encoding="utf-8",
            newline="\n",
        )

        def _explode(**_kwargs: Any) -> Any:
            raise AssertionError("the intelligence pipeline must not be entered")

        monkeypatch.setattr(vg, "run_intelligent_verify", _explode)

        plain: List[Dict[str, Any]] = []
        result_plain = vf.verify(
            str(repo), "tests/test_app.py::test_add", reports=plain
        )

        empty: List[Dict[str, Any]] = []
        result_empty = vf.verify(
            str(repo),
            "tests/test_app.py::test_add",
            reports=empty,
            intelligence_config={},
        )

        unrelated: List[Dict[str, Any]] = []
        result_unrelated = vf.verify(
            str(repo),
            "tests/test_app.py::test_add",
            reports=unrelated,
            intelligence_config={"some_unrelated_key": 1, "max_retries": 4},
        )

        for other in (result_empty, result_unrelated):
            assert other.target_test_passed == result_plain.target_test_passed
            assert other.regression_passed == result_plain.regression_passed
            assert other.flaky == result_plain.flaky
            assert other.baseline_passed == result_plain.baseline_passed
            assert _comparable_output(other.raw_output) == _comparable_output(
                result_plain.raw_output
            )
            assert other.structured_feedback == result_plain.structured_feedback
            # The same container commands in the same order: the delegated
            # argument changes what the caller configured, not what ran.
            assert _command_blocks(other.raw_output) == _command_blocks(
                result_plain.raw_output
            )
        assert plain == empty == unrelated
        # No gate row and no gate block may appear on the unconfigured path.
        assert _gate_rows(plain) == []
        assert "## verification-gate" not in result_plain.raw_output

    def test_a_config_carrying_only_a_falsey_value_still_enters_the_pipeline(self):
        """Presence, not truthiness: writing the key is the opt-in.

        ``verification_require_spec: False`` means the operator asked for the
        pipeline and turned one gate off inside it. Key-presence detection is
        what keeps "absent" available as a real state, and this is the mirror
        image of the Ceiling-14 ``local_first: False`` decision.
        """
        assert vg.intelligence_requested({"verification_require_spec": False}) is True
        assert vg.intelligence_requested({"verification_intelligence": False}) is True
        assert vg.intelligence_requested({"unrelated": 0}) is False
        assert vg.intelligence_requested(None) is False
        assert vg.intelligence_requested({}) is False
        assert vg.intelligence_requested("not-a-mapping") is False

    def test_no_intelligence_key_is_in_the_harness_defaults(self):
        """A default would be merged into every task and every eval arm.

        This is the regression pin for rule 5: as long as
        ``harness/config.py::DEFAULTS`` carries none of these keys, adding the
        seam cannot have switched a single existing task. If a future terminal
        adds one, this test fails and the reason is in the assertion message.
        """
        from harness.config import DEFAULTS

        merged = [key for key in vg.INTELLIGENCE_CONFIG_KEYS if key in DEFAULTS]
        assert merged == [], (
            "verification-intelligence keys are key-presence opt-ins and must not "
            f"appear in harness/config.py::DEFAULTS: {merged}"
        )

    def test_the_seam_is_the_only_addition_inside_verify(self):
        """``verify`` delegates and nothing else; the logic lives in the module.

        Proven structurally from the AST rather than by reading: the function
        body may reference the delegation helper and its own result, and may not
        import or name any intelligence module. A future inline gate would fail
        this test even if it behaved identically.
        """
        tree = ast.parse(
            (REPO_ROOT / "execution" / "verify.py").read_text(encoding="utf-8")
        )
        verify_fn = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "verify"
        )
        intelligence_names = {
            "verification_gate",
            "verification_intelligence",
            "spec_ledger",
            "independent_evidence",
            "GateVerdict",
            "IntelligenceDecision",
        }
        referenced = {
            node.id for node in ast.walk(verify_fn) if isinstance(node, ast.Name)
        } | {
            node.attr for node in ast.walk(verify_fn) if isinstance(node, ast.Attribute)
        }
        modules = {
            alias.name
            for node in ast.walk(verify_fn)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        assert not (referenced & intelligence_names), (
            f"verify() names intelligence internals directly: {referenced & intelligence_names}"
        )
        assert not any(name.split(".")[-1] in intelligence_names for name in modules), (
            f"verify() imports intelligence modules: {modules}"
        )
        assert "_intelligence_delegate" in referenced, (
            "verify() must reach the pipeline through _intelligence_delegate only"
        )
        # The delegation is the FIRST real statement, so nothing can precede it.
        body = [
            stmt
            for stmt in verify_fn.body
            if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
        ]
        first = body[0]
        assert isinstance(first, ast.Assign), ast.dump(first)
        assert any(
            isinstance(target, ast.Name) and target.id == "delegated"
            for target in first.targets
        ), "the delegation result must be bound before the pre-existing path runs"
        # And the pre-existing path must still be the whole remainder: the
        # language detection call that opens the original body follows.
        assert any(
            isinstance(stmt, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "lang" for t in stmt.targets)
            for stmt in body
        ), "the original body's first step must still run after the delegation"


# ---------------------------------------------------------------------------
# 2/3/4. the real gate, driven through the real sandbox
# ---------------------------------------------------------------------------


@requires_docker
class TestDelegatedGateThroughDocker:
    def test_a_ledger_enabled_run_whose_obligation_the_diff_does_not_satisfy_does_not_mint(
        self, tmp_path
    ):
        """A green visible suite plus an unsatisfied sealed obligation refuses.

        The repository is built so the bare suite is GREEN: ``pytest.ini`` pins
        ``testpaths`` to ``visible/`` and the obligation test lives outside it.
        The only red thing in the run is the sealed obligation, which is exactly
        the case a suite-only gate would have called verified. The result must
        not mint, and the reason must name the obligation.
        """
        repo = _make_repo(tmp_path, failing_obligation=True)
        spec_root = _sealed_spec(tmp_path / "spec", items=_TWO_ITEMS)
        reports: List[Dict[str, Any]] = []

        result = vf.verify(
            str(repo),
            "visible/test_app.py::test_add",
            reports=reports,
            intelligence_config={
                "verification_intelligence": True,
                "verification_spec_root": str(spec_root),
                "verification_require_spec": True,
            },
        )

        # The baseline rung is green: this is not a red-suite test in disguise.
        assert result.regression_passed is True
        baseline_rows = _rung_rows(reports, vg.RUNG_BASELINE)
        assert [row["passed"] for row in baseline_rows] == [True]
        assert (
            "rung=baseline gate=baseline_target_and_suite passed=true"
            in result.raw_output
        )

        # The refusal is real, attributed, and blocks the mint.
        assert result.target_test_passed is False
        assert "hidden_contract" in result.raw_output
        assert "gate=spec_obligations passed=false" in result.raw_output
        assert "mintable=false" in result.raw_output
        spec_rows = _rung_rows(reports, vg.RUNG_SPEC)
        assert {row["gate"] for row in spec_rows} == {
            vg.GATE_SPEC_INTACT,
            vg.GATE_OBLIGATIONS,
        }
        obligations = [row for row in spec_rows if "obligation" in row]
        assert {row["obligation"] for row in obligations} == {
            "visible_suite",
            "hidden_contract",
        }
        assert any(
            row["obligation"] == "visible_suite" and row["outcome"] == "pass"
            for row in obligations
        )
        assert any(
            row["obligation"] == "hidden_contract" and row["outcome"] != "pass"
            for row in obligations
        )

    def test_a_ledger_enabled_run_whose_obligations_all_hold_still_mints(
        self, tmp_path
    ):
        """The refusal in the proof above is attributable to the gate.

        An off-arm refusal would prove nothing: the tree, the spec, and the
        tests would have to be the reason. Here the identical repository shape
        satisfies every obligation, so the pipeline must leave the baseline
        verdict alone and say so with the rung attached.
        """
        repo = _make_repo(tmp_path, failing_obligation=False)
        spec_root = _sealed_spec(tmp_path / "spec", items=_TWO_ITEMS)
        reports: List[Dict[str, Any]] = []

        result = vf.verify(
            str(repo),
            "visible/test_app.py::test_add",
            reports=reports,
            intelligence_config={
                "verification_intelligence": True,
                "verification_spec_root": str(spec_root),
                "verification_require_spec": True,
            },
        )

        assert result.target_test_passed is True
        assert result.regression_passed is True
        assert result.flaky is False
        assert "mintable=true" in result.raw_output
        assert "applied=false" in result.raw_output
        assert "gate=spec_obligations passed=true" in result.raw_output
        assert _rung_rows(reports, vg.RUNG_SPEC)
        assert all(row["passed"] for row in _gate_rows(reports))

    def test_a_removed_spec_item_is_refused_and_named(self, tmp_path):
        """A shrunken obligation set is a mutation, and the seal catches it.

        This is the failure mode the ledger exists for: an agent that deletes
        the obligation it could not satisfy. The verdict must name the removed
        item rather than reporting a bare boolean.
        """
        from execution.spec_ledger import SpecLedger, guard_paths

        repo = _make_repo(tmp_path, failing_obligation=False)
        spec_root = _sealed_spec(tmp_path / "spec", items=_TWO_ITEMS)
        artifact, seal = guard_paths(str(spec_root))
        stripped = SpecLedger.load(artifact, seal_path=seal)
        stripped.replace_items([_TWO_ITEMS[0]])
        stripped.save(artifact)
        reports: List[Dict[str, Any]] = []

        result = vf.verify(
            str(repo),
            "visible/test_app.py::test_add",
            reports=reports,
            intelligence_config={
                "verification_intelligence": True,
                "verification_spec_root": str(spec_root),
                "verification_require_spec": True,
            },
        )

        assert result.regression_passed is True, "the suite itself is green"
        assert result.target_test_passed is False
        assert "hidden_contract" in result.raw_output
        assert "removed=['hidden_contract']" in result.raw_output
        intact = [
            row
            for row in _rung_rows(reports, vg.RUNG_SPEC)
            if row["gate"] == vg.GATE_SPEC_INTACT
        ]
        assert intact and intact[0]["passed"] is False

    def test_an_independent_judge_pass_cannot_override_a_failing_target_test(
        self, tmp_path
    ):
        """The judge accepting the claim does not rescue a red target test.

        The held-out acceptance suite passes on the judge's own clean copy, so
        the independent rung returns ``passed=true`` — and the result still does
        not mint, because the baseline rung is ANDed with the judge rather than
        overridden by it. Both rungs are asserted in the SAME run, which is the
        only way to show the AND rather than two independent scenarios.
        """
        from execution.independent_evidence import build_held_out_suite

        repo = _make_repo(tmp_path, failing_obligation=False, failing_target=True)
        held = build_held_out_suite(
            str(tmp_path / "heldout"),
            [
                {
                    "id": "add",
                    "expr": "add({a}, {b})",
                    "expect": "{a} + {b}",
                    "inputs": [{"a": 2, "b": 3}, {"a": -5, "b": 11}],
                }
            ],
            seed=7,
            repo_path=str(repo),
            module="app",
        )
        fingerprint = held.sealed_fingerprint
        held.conceal()
        reports: List[Dict[str, Any]] = []

        result = vf.verify(
            str(repo),
            "visible/test_broken.py::test_broken",
            reports=reports,
            intelligence_config={
                "verification_intelligence": True,
                "verification_held_out_suite": held,
                "verification_held_out_runner": None,
                "verification_independent_judge": True,
            },
        )

        independent = _rung_rows(reports, vg.RUNG_INDEPENDENT)
        assert independent, "the independent rung must have run"
        assert independent[-1]["passed"] is True, (
            "the held-out suite passes, so the judge must accept; otherwise this "
            f"test proves nothing about overriding: {independent[-1]['notes']}"
        )
        baseline = _rung_rows(reports, vg.RUNG_BASELINE)
        assert baseline and baseline[-1]["passed"] is False

        # The mint stays closed, and the raw output shows BOTH rungs disagreeing
        # in the safe direction.
        assert result.target_test_passed is False
        assert result.regression_passed is False
        assert (
            "rung=baseline gate=baseline_target_and_suite passed=false"
            in result.raw_output
        )
        assert (
            "rung=independent gate=independent_evidence passed=true"
            in result.raw_output
        )
        assert "mintable=false" in result.raw_output
        assert f"fingerprint={fingerprint}" not in result.raw_output

    def test_an_independent_judge_refusal_blocks_a_green_run(self, tmp_path):
        """The judge is additional evidence in the refusing direction too.

        The visible suite is fully green and the held-out acceptance suite fails,
        which is the reward-hacking shape the judge exists to catch. The result
        must not mint even though every visible test passes.
        """
        repo = _make_repo(tmp_path, failing_obligation=False)
        # A held-out case that the (correct) implementation cannot satisfy.
        broken_repo = repo / "app.py"
        held_dir = tmp_path / "heldout"
        (held_dir).mkdir(parents=True)
        # Build against a stub whose add() is deliberately wrong, then restore:
        # the suite is authored to a wrong expectation without touching app.py.
        original = broken_repo.read_text(encoding="utf-8")
        broken_repo.write_text(
            "def add(a, b):\n    return a - b\n", encoding="utf-8", newline="\n"
        )
        try:
            from execution.independent_evidence import build_held_out_suite

            held = build_held_out_suite(
                str(held_dir),
                [
                    {
                        "id": "add",
                        "expr": "add({a}, {b})",
                        "expect": "{a} + {b}",
                        "inputs": [{"a": 2, "b": 3}],
                    },
                    {
                        "id": "sub",
                        "expr": "add({a}, {b})",
                        "expect": "{a} - {b}",
                        "inputs": [{"a": 5, "b": 1}],
                    },
                ],
                seed=3,
                repo_path=str(repo),
                module="app",
            )
        finally:
            broken_repo.write_text(original, encoding="utf-8", newline="\n")
        held.conceal()
        reports: List[Dict[str, Any]] = []

        result = vf.verify(
            str(repo),
            "visible/test_app.py::test_add",
            reports=reports,
            intelligence_config={
                "verification_intelligence": True,
                "verification_held_out_suite": held,
                "verification_independent_judge": True,
            },
        )

        independent = _rung_rows(reports, vg.RUNG_INDEPENDENT)
        assert independent and independent[-1]["passed"] is False
        assert result.target_test_passed is False
        assert result.regression_passed is True, "the visible suite really is green"
        assert "mintable=false" in result.raw_output
        assert (
            "rung=independent gate=independent_evidence passed=false"
            in result.raw_output
        )

    def test_an_unresolvable_held_out_suite_refuses_rather_than_disappearing(
        self, tmp_path
    ):
        """Opting in and failing to get the evidence must not read as agreement.

        The judge is configured but its suite root does not exist. An absent
        independent gate would be indistinguishable from an accepting one, so
        the rung must fail with the reason attached.
        """
        repo = _make_repo(tmp_path, failing_obligation=False)
        reports: List[Dict[str, Any]] = []

        result = vf.verify(
            str(repo),
            "visible/test_app.py::test_add",
            reports=reports,
            intelligence_config={
                "verification_intelligence": True,
                "verification_independent_judge": True,
                "verification_held_out_root": str(tmp_path / "does-not-exist"),
            },
        )

        independent = _rung_rows(reports, vg.RUNG_INDEPENDENT)
        assert independent and independent[-1]["passed"] is False
        assert "could not be resolved" in " ".join(independent[-1]["notes"])
        assert result.target_test_passed is False
        assert "degraded: independent: the held-out suite could not be resolved" in (
            result.raw_output
        )

    def test_a_corrupt_ledger_degrades_with_a_recorded_reason(self, tmp_path):
        """Unparseable, unsealed, and absent obligation sets each refuse with why.

        Three degradation shapes, all of which used to be the dangerous one: a
        ledger that cannot be parsed, a ledger with no seal, and a required
        ledger that is not there. None may read as "no obligations were
        violated", and none may be silent.
        """
        repo = _make_repo(tmp_path, failing_obligation=False)

        corrupt = tmp_path / "corrupt"
        corrupt.mkdir(parents=True)
        (corrupt / "spec.json").write_text("{ this is not json", encoding="utf-8")
        (corrupt / "spec.seal.json").write_text("{ neither is this", encoding="utf-8")

        unsealed = tmp_path / "unsealed"
        unsealed.mkdir(parents=True)
        (unsealed / "spec.json").write_text(
            json.dumps(
                {
                    "spec_id": "r2-01",
                    "title": "unsealed",
                    "items": [
                        {
                            "id": "visible_suite",
                            "title": "t",
                            "acceptance": ["a"],
                            "tests": ["visible/test_app.py::test_add"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        absent = tmp_path / "absent"
        absent.mkdir(parents=True)

        cases = [
            ("unreadable", str(corrupt), ["spec unreadable"]),
            ("unsealed", str(unsealed), ["not sealed"]),
            # A required ledger that is not there is reported by the same
            # load_or_report path, so its reason is the "not found" one; what
            # matters is that it is explicit and that it FAILS.
            ("absent", str(absent), ["not found"]),
        ]
        for label, root, expected_fragments in cases:
            reports: List[Dict[str, Any]] = []
            result = vf.verify(
                str(repo),
                "visible/test_app.py::test_add",
                reports=reports,
                intelligence_config={
                    "verification_intelligence": True,
                    "verification_spec_root": root,
                    "verification_require_spec": True,
                },
            )
            assert result.regression_passed is True, f"{label}: the suite is green"
            assert result.target_test_passed is False, f"{label}: must not mint"
            intact = [
                row
                for row in _rung_rows(reports, vg.RUNG_SPEC)
                if row["gate"] == vg.GATE_SPEC_INTACT
            ]
            assert intact and intact[0]["passed"] is False, (
                f"{label}: spec_intact must fail"
            )
            notes = " ".join(intact[0]["notes"])
            for fragment in expected_fragments:
                assert fragment in notes, (
                    f"{label}: reason must mention {fragment!r}, got {notes!r}"
                )
            assert "mintable=false" in result.raw_output, label
            if label == "unreadable":
                assert (
                    "spec: the obligation set is unsealed or unreadable, so the "
                    "guard could not trust it" in result.raw_output
                ), label

    def test_a_required_spec_that_is_absent_refuses_while_an_optional_one_is_skipped(
        self, tmp_path
    ):
        """Optional is not the same as failed, and required is not the same as fine.

        With no spec artifact and no requirement, the spec rung reports a
        SKIPPED optional gate and the run mints exactly as it would have without
        the seam. Add the requirement and the identical tree stops minting.
        The two differ only in the config, which is what proves the refusal is
        the gate's and not the fixture's.
        """
        repo = _make_repo(tmp_path, failing_obligation=False)
        empty = tmp_path / "no-spec-here"
        empty.mkdir(parents=True)

        optional_reports: List[Dict[str, Any]] = []
        optional = vf.verify(
            str(repo),
            "visible/test_app.py::test_add",
            reports=optional_reports,
            intelligence_config={
                "verification_intelligence": True,
                "verification_spec_root": str(empty),
            },
        )
        assert optional.target_test_passed is True
        assert "mintable=true" in optional.raw_output
        spec_rows = _rung_rows(optional_reports, vg.RUNG_SPEC)
        assert spec_rows and spec_rows[0]["passed"] is True
        assert spec_rows[0]["mandatory"] is False

        required_reports: List[Dict[str, Any]] = []
        required = vf.verify(
            str(repo),
            "visible/test_app.py::test_add",
            reports=required_reports,
            intelligence_config={
                "verification_intelligence": True,
                "verification_spec_root": str(empty),
                "verification_require_spec": True,
            },
        )
        assert required.target_test_passed is False
        spec_rows = _rung_rows(required_reports, vg.RUNG_SPEC)
        assert spec_rows and spec_rows[0]["mandatory"] is True
        assert spec_rows[0]["passed"] is False

    def test_the_seam_terminates_without_re_running_the_suite(
        self, tmp_path, monkeypatch
    ):
        """Exactly one baseline suite run, however deep the pipeline goes.

        Recursion on this shape would multiply container runs without bound, and
        the symptom would be a slow eval rather than a wrong answer. Counting
        the real sandbox calls pins the property directly: two for the baseline
        (target + suite) plus one per obligation, and nothing more.
        """
        repo = _make_repo(tmp_path, failing_obligation=False)
        spec_root = _sealed_spec(tmp_path / "spec", items=[_TWO_ITEMS[0]])

        import execution.sandbox as sandbox_module

        calls: List[str] = []
        real = sandbox_module.execute_sandboxed

        def _counting(repo_path: str, command: str, *args: Any, **kwargs: Any) -> Any:
            calls.append(command)
            return real(repo_path, command, *args, **kwargs)

        monkeypatch.setattr(sandbox_module, "execute_sandboxed", _counting)
        # verify.py imported the symbol directly, so patch there too. The gate
        # module imports it lazily inside the function, so patching
        # execution.sandbox covers it.
        monkeypatch.setattr(vf, "execute_sandboxed", _counting)

        reports: List[Dict[str, Any]] = []
        result = vf.verify(
            str(repo),
            "visible/test_app.py::test_add",
            reports=reports,
            intelligence_config={
                "verification_intelligence": True,
                "verification_spec_root": str(spec_root),
                "verification_require_spec": True,
            },
        )

        assert result.target_test_passed is True
        obligation_calls = [
            c for c in calls if "test_app.py::test_add" in c and "-q '" in c
        ]
        assert len(calls) == 3, (
            f"expected 2 baseline runs + 1 obligation run, got {calls}"
        )
        assert len(obligation_calls) == 1, calls

    def test_a_non_gating_inner_verification_is_never_gated(
        self, tmp_path, monkeypatch
    ):
        """A subset result stays incapable of looking like a completion claim.

        ``final_gate=False`` is the repair loop's inner gate. It must not carry
        an obligation-set or judge verdict it did not earn, so the rung
        evaluation is never reached for it.
        """
        from execution.verify import inner_verify

        repo = _make_repo(tmp_path, failing_obligation=False)
        _sealed_spec(tmp_path / "spec", items=[_TWO_ITEMS[0]])

        def _explode(**_kwargs: Any) -> Any:
            raise AssertionError("the rungs must not be evaluated for a non-gating run")

        monkeypatch.setattr(vg, "evaluate", _explode)
        monkeypatch.setattr(vg, "apply_fold", _explode)

        result, _selection = inner_verify(
            str(repo),
            ["app.py"],
            target_test="visible/test_app.py::test_add",
            test_command="python -m pytest -q visible",
            reports=[],
        )
        assert "## verification-gate" not in result.raw_output
        assert result.target_test_passed is True

    def test_a_non_gating_run_declines_and_emits_a_trace_row(
        self, tmp_path, monkeypatch
    ):
        """The decline itself is observable, with its reason attached."""
        from shared import tracing

        repo = _make_repo(tmp_path, failing_obligation=False)
        trace_root = tmp_path / "trace"
        monkeypatch.setenv("NEO_TRACE_DIR", str(trace_root))
        tracing._reset_cache()

        seen: List[str] = []
        real = vg.run_intelligent_verify

        def _spy(**kwargs: Any) -> Any:
            config = kwargs.get("intelligence_config") or {}
            seen.append(str(config.get("verification_task_id", "")))
            return real(**kwargs)

        monkeypatch.setattr(vg, "run_intelligent_verify", _spy)
        reports: List[Dict[str, Any]] = []
        result = vf.verify(
            str(repo),
            "visible/test_app.py::test_add",
            final_gate=False,
            reports=reports,
            intelligence_config={
                "verification_intelligence": True,
                "verification_task_id": "r2-01-declined",
            },
        )
        assert seen == ["r2-01-declined"], "the pipeline was consulted and declined"
        assert "## verification-gate" not in result.raw_output
        assert _gate_rows(reports) == []
        events = tracing.read_task_events("r2-01-declined")
        declined = [e for e in events if e.get("event") == "verification_gate_declined"]
        assert declined, (
            f"the decline must be traced, got {[e.get('event') for e in events]}"
        )
        assert declined[0]["reason"] == "non_gating_inner_verification"
        tracing._reset_cache()


# ---------------------------------------------------------------------------
# 5. every verdict names its rung
# ---------------------------------------------------------------------------


@requires_docker
class TestEveryVerdictNamesItsRung:
    def test_every_verdict_names_the_rung_that_produced_it(self, tmp_path, monkeypatch):
        """Receipt, raw_output, and the unified trace all name the mechanism.

        Run once with every rung active so a single result carries a
        ``baseline`` verdict, two ``spec`` verdicts, and an ``independent``
        verdict. The assertion is that no gate anywhere is anonymous: a reader
        must be able to tell which mechanism claimed the run was verified
        without knowing anything about this module's internals.
        """
        from execution.independent_evidence import build_held_out_suite
        from shared import tracing

        repo = _make_repo(tmp_path, failing_obligation=False)
        spec_root = _sealed_spec(tmp_path / "spec", items=[_TWO_ITEMS[0]])
        held = build_held_out_suite(
            str(tmp_path / "heldout"),
            [
                {
                    "id": "add",
                    "expr": "add({a}, {b})",
                    "expect": "{a} + {b}",
                    "inputs": [{"a": 2, "b": 3}, {"a": -5, "b": 11}],
                }
            ],
            seed=5,
            repo_path=str(repo),
            module="app",
        )
        fingerprint = held.sealed_fingerprint
        held.conceal()
        trace_root = tmp_path / "trace"
        monkeypatch.setenv("NEO_TRACE_DIR", str(trace_root))
        tracing._reset_cache()

        reports: List[Dict[str, Any]] = []
        result = vf.verify(
            str(repo),
            "visible/test_app.py::test_add",
            reports=reports,
            intelligence_config={
                "verification_intelligence": True,
                "verification_spec_root": str(spec_root),
                "verification_require_spec": True,
                "verification_held_out_suite": held,
                "verification_independent_judge": True,
                "verification_task_id": "r2-01-rungs",
            },
        )

        # (a) the receipt: every gate row names a known rung.
        gate_rows = _gate_rows(reports)
        assert gate_rows, "the pipeline must contribute rows to the reports sink"
        for row in gate_rows:
            assert row.get("rung") in vg.RUNGS, row
            assert row.get("gate"), row
        rungs_seen = {row["rung"] for row in gate_rows}
        assert rungs_seen == {vg.RUNG_BASELINE, vg.RUNG_SPEC, vg.RUNG_INDEPENDENT}, (
            rungs_seen
        )

        # (b) raw_output: one greppable line per gate, rung in front.
        for gate_name in (
            vg.GATE_BASELINE,
            vg.GATE_SPEC_INTACT,
            vg.GATE_OBLIGATIONS,
            vg.GATE_INDEPENDENT,
        ):
            line = f"rung={_rung_for(gate_name)} gate={gate_name} passed=true"
            assert line in result.raw_output, f"missing receipt line: {line}"
        assert "traced=true" in result.raw_output

        # (c) the unified trace: the same rows, machine-readable.
        events = [
            e
            for e in tracing.read_task_events("r2-01-rungs")
            if e.get("event") == "verification_gate"
        ]
        assert events, "the gate must emit exactly one unified-trace row"
        event = events[0]
        assert event["module"] == "execution"
        assert event["mintable"] is True
        assert {v["rung"] for v in event["verdicts"]} == rungs_seen
        for verdict in event["verdicts"]:
            assert verdict["rung"] in vg.RUNGS
            assert verdict["reason"]
        assert event["keys_present"], "the activating keys travel with the verdict"
        # Never leak the held-out fingerprint into a receipt.
        assert fingerprint not in json.dumps(event)
        tracing._reset_cache()

    def test_a_dropped_verdict_with_an_unknown_rung_is_reported_not_emitted(self):
        """An unrecognised rung is a caller bug, and is surfaced as one.

        Silently emitting ``rung="heuristic"`` would put a mechanism name in a
        receipt that this module does not implement, and a reader would have no
        way to tell it apart from a real one.
        """
        errors: List[str] = []
        kept = vg._validated(
            [
                vg.GateVerdict("baseline", vg.GATE_BASELINE, True, True, "ok"),
                vg.GateVerdict("guesswork", "made_up", True, True, "nope"),
            ],
            errors,
        )
        assert [v.name for v in kept] == [vg.GATE_BASELINE]
        assert errors and "made_up" in errors[0] and "guesswork" in errors[0]


# ---------------------------------------------------------------------------
# the fold, in isolation from Docker
# ---------------------------------------------------------------------------


class _Result:
    """A minimal stand-in for ``VerificationResult`` for pure fold tests."""

    def __init__(self) -> None:
        self.target_test_passed = True
        self.baseline_passed = False
        self.regression_passed = True
        self.flaky = False
        self.raw_output = "$ python -m pytest -q\nexit=0"


def _rung_for(gate_name: str) -> str:
    """Return the rung a well-known gate name belongs to."""
    return {
        vg.GATE_BASELINE: vg.RUNG_BASELINE,
        vg.GATE_SPEC_INTACT: vg.RUNG_SPEC,
        vg.GATE_OBLIGATIONS: vg.RUNG_SPEC,
        vg.GATE_INDEPENDENT: vg.RUNG_INDEPENDENT,
    }[gate_name]


class TestTheFoldIsOneDirectional:
    def test_a_refusing_rung_clears_the_target_claim_and_records_why(self):
        """A refusal flips the claim and the reason travels with the result."""
        decision = vg.IntelligenceDecision(
            keys_present=("verification_intelligence",),
            verdicts=(
                vg.GateVerdict(vg.RUNG_BASELINE, vg.GATE_BASELINE, True, True, "green"),
                vg.GateVerdict(
                    vg.RUNG_SPEC,
                    vg.GATE_OBLIGATIONS,
                    False,
                    True,
                    "declared obligations are not satisfied: hidden_contract: ...",
                ),
            ),
        )
        result = _Result()
        returned = vg.apply_fold(result, decision)

        assert returned is result
        assert result.target_test_passed is False
        assert result.regression_passed is True, "only the claim is cleared"
        assert result.flaky is False
        assert decision.applied is True
        assert decision.folded_fields == ("target_test_passed",)
        assert "rung=spec gate=spec_obligations passed=false" in result.raw_output
        assert "declared obligations are not satisfied" in result.raw_output

    def test_an_accepting_pipeline_never_touches_the_baseline_booleans(self):
        """Nothing is promoted, and the accepting block still names the rung.

        A verified run has to be able to say WHICH mechanism claimed it, so the
        receipt is attached on the accepting path too. What must not happen is a
        boolean moving: a green pipeline is a pass through, not a promotion, and
        ``applied`` stays False because nothing was cleared.
        """
        decision = vg.IntelligenceDecision(
            keys_present=("verification_intelligence",),
            verdicts=(
                vg.GateVerdict(vg.RUNG_BASELINE, vg.GATE_BASELINE, True, True, "green"),
                vg.GateVerdict(
                    vg.RUNG_SPEC,
                    vg.GATE_OBLIGATIONS,
                    True,
                    True,
                    "all obligations hold",
                ),
            ),
        )
        result = _Result()
        before = result.raw_output
        vg.apply_fold(result, decision)

        assert result.target_test_passed is True
        assert result.regression_passed is True
        assert result.flaky is False
        assert result.raw_output != before, (
            "the accepting receipt must still be attached"
        )
        assert "mintable=true" in result.raw_output
        assert "rung=spec gate=spec_obligations passed=true" in result.raw_output
        assert decision.applied is False
        assert decision.folded_fields == ()
        assert decision.mintable is True

    def test_a_baseline_refusal_does_not_claim_credit_for_the_fold(self):
        """The block is still attached; the fold is not.

        A red baseline already blocks the mint, so ``applied`` must be False —
        otherwise a reader would attribute the block to the intelligence layer
        when the ordinary verifier is what refused.
        """
        decision = vg.IntelligenceDecision(
            keys_present=("verification_intelligence",),
            verdicts=(
                vg.GateVerdict(
                    vg.RUNG_BASELINE,
                    vg.GATE_BASELINE,
                    False,
                    True,
                    "the suite did not pass",
                ),
            ),
        )
        result = _Result()
        result.target_test_passed = False
        vg.apply_fold(result, decision)

        assert decision.applied is False
        assert decision.folded_fields == ()
        assert (
            "rung=baseline gate=baseline_target_and_suite passed=false"
            in result.raw_output
        )

    def test_a_skipped_optional_gate_cannot_block_a_mint(self):
        """``mandatory`` is what decides, not ``passed``.

        A skipped optional gate is a non-event by design, so a decision whose
        only failing gate is optional must stay mintable. This is the property
        that keeps "no spec was supplied" from reading as a refusal.
        """
        decision = vg.IntelligenceDecision(
            verdicts=(
                vg.GateVerdict(
                    vg.RUNG_SPEC,
                    vg.GATE_SPEC_INTACT,
                    False,
                    mandatory=False,
                    reason="no spec artifact was supplied",
                ),
            ),
        )
        result = _Result()
        vg.apply_fold(result, decision)

        assert decision.mintable is True
        assert result.target_test_passed is True
        assert "mintable=true" in result.raw_output
        assert "no spec artifact was supplied" in result.raw_output, (
            "the skipped optional gate is still reported, with its reason"
        )

    def test_a_skipped_mandatory_gate_counts_as_a_refusal(self):
        """A mandatory gate that could not run is a failure, not an absence.

        This is the one vacuous-pass shape the fold exists to close: a gate the
        pipeline was told to require but never evaluated.
        """
        decision = vg.IntelligenceDecision(
            verdicts=(
                vg.GateVerdict(
                    vg.RUNG_SPEC,
                    vg.GATE_SPEC_INTACT,
                    False,
                    mandatory=True,
                    reason="a spec artifact was required but none was readable",
                ),
            ),
        )
        result = _Result()
        vg.apply_fold(result, decision)

        assert decision.mintable is False
        assert result.target_test_passed is False
        assert [v.name for v in decision.refusals] == [vg.GATE_SPEC_INTACT]

    def test_a_configured_but_unusable_knob_is_reported_rather_than_silently_coerced(
        self,
    ):
        """Coercion is a substitution, and a substitution has to be visible."""
        notes = vg._config_warnings(
            {
                "verification_max_obligations": "not-a-number",
                "verification_gap_threshold_points": "wide",
                "verification_held_out_cases": {"id": "a mapping, not a list"},
                "verification_held_out_runner": "not-callable",
            }
        )
        joined = " ".join(notes)
        assert "verification_max_obligations" in joined
        assert "verification_gap_threshold_points" in joined
        assert "verification_held_out_cases" in joined
        assert "verification_held_out_runner" in joined
        assert vg.resolve_settings(
            {"verification_max_obligations": "x"}
        ).max_obligations == (vg.DEFAULT_MAX_OBLIGATIONS)

    def test_the_settings_defaults_are_bounded(self):
        """An unbounded obligation sweep or judge is a hang, not a check."""
        settings = vg.resolve_settings({})
        assert settings.max_obligations >= 1
        assert settings.gap_threshold_points > 0
        assert settings.judge_requested is False
        assert vg.resolve_settings(
            {"verification_max_obligations": 0}
        ).max_obligations == (vg.DEFAULT_MAX_OBLIGATIONS)
        assert vg.resolve_settings(
            {"verification_max_obligations": -3}
        ).max_obligations == (vg.DEFAULT_MAX_OBLIGATIONS)

    def test_bounding_the_obligation_sweep_refuses_rather_than_partially_passing(self):
        """An unchecked obligation is exactly where a shrunken spec would hide.

        A capped sweep that silently reported the checked subset as the whole
        set would be a vacuous pass, so the cap is expressed as a refusal that
        names what it did not check.
        """
        rows: List[Dict[str, Any]] = []
        unchecked = ["third", "fourth"]
        verdict = vg.GateVerdict(
            vg.RUNG_SPEC,
            vg.GATE_OBLIGATIONS,
            False,
            True,
            "2 obligation(s) were not checked because the sweep is bounded at 2: "
            "third, fourth",
        )
        decision = vg.IntelligenceDecision(
            verdicts=(verdict,),
            obligations=tuple(rows),
            unchecked_obligations=tuple(unchecked),
        )
        result = _Result()
        vg.apply_fold(result, decision)

        assert result.target_test_passed is False
        assert "unchecked_obligations=third,fourth" in result.raw_output


# ---------------------------------------------------------------------------
# the seam's own refusal shape
# ---------------------------------------------------------------------------


class TestUnimportablePipelineIsNotASilentFallback:
    def test_a_broken_pipeline_returns_a_refusal_rather_than_the_baseline(
        self, tmp_path, monkeypatch
    ):
        """An opted-in caller must never be quietly served the weaker gate.

        If the intelligence layer cannot be imported, falling back to the
        baseline would report a run as verified on the strength of fewer gates
        than the operator asked for. The result refuses, names the reason, and
        records a row so the trace shows why.
        """
        import builtins

        real_import = builtins.__import__
        reports: List[Dict[str, Any]] = []

        def _blocked(name: str, *args: Any, **kwargs: Any) -> Any:
            if (
                name.endswith("verification_gate")
                or name == "execution.verification_gate"
            ):
                raise ImportError("simulated: the intelligence layer is unavailable")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocked)
        result = vf.verify(
            str(tmp_path),
            None,
            intelligence_config={"verification_intelligence": True},
            reports=reports,
        )

        assert result.target_test_passed is False
        assert result.regression_passed is False
        assert "intelligence_unavailable" in result.raw_output
        assert "could not be imported" in result.raw_output
        rows = _gate_rows(reports)
        assert rows and rows[0]["rung"] in vg.RUNGS
        assert rows[0]["passed"] is False

    def test_a_broken_pipeline_is_irrelevant_when_nothing_was_configured(
        self, tmp_path, monkeypatch
    ):
        """With no key present the module is never imported, so a broken one is moot.

        This is the byte-identical contract stated as an ordering fact: the
        key-presence question is answered before the import is attempted, so an
        unimportable intelligence layer cannot perturb an unconfigured run.
        """
        import builtins

        real_import = builtins.__import__

        def _blocked(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.endswith("verification_gate"):
                raise AssertionError("the intelligence module must not be imported")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocked)
        # No sandbox run happens: there is no testable repo here, and the
        # "no test command found" result is produced without importing anything
        # from the intelligence subgraph.
        result = vf.verify(str(tmp_path), None, reports=[])
        assert result.target_test_passed is False
        assert "no test command found" in result.raw_output
