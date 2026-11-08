"""STRUCTURAL pins for the ledger store -- and these are the ones in the SMOKE lane.

``tests/test_feature_ledger_store.py`` proves the store's behaviour by driving it.
This file proves it by reading its SOURCE, and it is deliberately sub-second: a
structural pin that takes seconds does not get run on most merges, and a
structural pin that does not get run is a comment.

Three claims, each of which a behavioural test cannot make, because a behavioural
test passes just as happily against a reimplementation:

1. **the agent may change only the status field** -- and the STORE enforces it.
   Asserted as a data-flow property over the module's own constant, plus a scan
   proving no other feature field is ever assigned from a caller-supplied value;
2. **the seal covers the obligation and NOT the report.** A guard that fingerprinted
   the status would make honest progress reporting a seal violation, and one that
   fingerprinted nothing would catch no tampering at all;
3. **the store owns no second copy of the vocabulary**, and writes nothing into
   ``state.json`` -- so Boundary 4's stable six-key prefix order is untouched.

The behavioural half of the same claims lives in
``tests/test_feature_ledger_store.py`` and is in the non-blocking ``host-only``
lane. This file is in ``smoke`` because it is the cheap half.

Host-only: no Docker, no provider, no network, no credential, no subprocess.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import List

from memory import feature_ledger_store as FL


def _module_source() -> str:
    """Return the store module's own source, read from its ``__file__``."""
    return Path(inspect.getsourcefile(FL) or "").read_text(
        encoding="utf-8", errors="replace"
    )


def _function_nodes(name: str) -> List[ast.AST]:
    """Return every top-level function (or method) named ``name``, as AST nodes."""
    tree = ast.parse(_module_source())
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]


def _assigned_subscripts(function_name: str) -> List[str]:
    """Return every ``item["key"] = ...`` subscript key assigned inside ``function_name``.

    An assignment to a subscript is the only way this module can write a feature
    field, so enumerating them is an enumeration of the store's whole write
    surface for a feature row -- and the assertion below is therefore about the
    complete set, not about a sample of it.
    """
    found: List[str] = []
    for function in _function_nodes(function_name):
        for node in ast.walk(function):
            targets: List[ast.AST] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                ):
                    found.append(target.slice.value)
    return sorted(set(found))


class TestTheAgentMayChangeOnlyTheStatusField:
    def test_the_writable_set_is_exactly_the_status_field(self):
        """One definition, imported from T1 -- not a second copy."""
        import harness.long_horizon as LH

        assert FL.AGENT_WRITABLE_LEDGER_FIELDS == ("status",)
        assert FL.AGENT_WRITABLE_LEDGER_FIELDS is LH.AGENT_WRITABLE_LEDGER_FIELDS

    def test_the_store_writes_exactly_four_feature_fields_and_no_others(self):
        """The COMPLETE write surface for one feature row, read from the AST.

        Four keys are written -- ``status``, ``evidence``, ``sessions_touched`` and
        the ``sessions_touched`` helper's own -- and none of them is an
        OBLIGATION field. A future edit that adds ``description`` to this list
        fails here, before it reaches a ledger.
        """
        written = set(_assigned_subscripts("apply_status"))
        assert written == {"status", "evidence", "sessions_touched", "features"}, (
            f"apply_status now assigns {sorted(written)}; the obligation fields "
            f"{list(FL.IMMUTABLE_FEATURE_FIELDS)} must never appear in a caller's write. "
            "'features' is the LEDGER's list-of-features key, replaced wholesale with the "
            "updated rows -- it is not a feature field."
        )
        assert not (written & set(FL.IMMUTABLE_FEATURE_FIELDS))

    def test_no_immutable_field_is_assigned_anywhere_in_the_publish_path(self):
        """Widened to the WHOLE module, not just ``apply_status``.

        A second write path -- a helper, a new method -- would not be covered by
        the test above, and a write path that could edit an obligation is exactly
        the defect the seal exists to catch. So the assertion is over every
        assignment in the module.
        """
        tree = ast.parse(_module_source())
        offenders: List[str] = []
        for node in ast.walk(tree):
            targets: List[ast.AST] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                    and target.slice.value in FL.IMMUTABLE_FEATURE_FIELDS
                ):
                    offenders.append(target.slice.value)
        assert not offenders, (
            f"the module assigns immutable feature field(s) {sorted(set(offenders))} through a "
            "subscript; an obligation is not writable by anything, including this store"
        )


class TestTheSealCoversTheObligationAndNotTheReport:
    def test_the_sealed_projection_excludes_every_mutable_field(self):
        """A guard that fingerprinted the STATUS would refuse honest progress."""
        sealed = set(FL.IMMUTABLE_FEATURE_FIELDS) | set(FL.IMMUTABLE_LEDGER_FIELDS)
        assert not (sealed & set(FL.MUTABLE_FEATURE_FIELDS))
        assert "status" in FL.MUTABLE_FEATURE_FIELDS and "status" not in sealed
        # And the projection really is what gets hashed, not the whole document.
        projection = FL.ledger_projection(
            {
                "schema": "neo/feature-ledger/v1",
                "project_id": "p",
                "request": "r",
                "created_at": "t",
                "features": [
                    {
                        "id": "a",
                        "description": "d",
                        "verification": {},
                        "derived_from": "",
                        "status": "failing",
                    }
                ],
            }
        )
        assert "status" not in str(projection), (
            "the projection leaked the status into the sealed fingerprint, which would make "
            "honest progress reporting a seal violation"
        )

    def test_the_seal_lives_in_its_own_file(self):
        """So a rewrite of the ledger cannot rewrite its own permission slip."""
        assert FL.SEAL_NAME != FL.FEATURE_LEDGER_NAME
        assert (FL.STORE_DIRNAME, FL.HISTORY_NAME) != (FL.STORE_DIRNAME, FL.SEAL_NAME)
        assert FL.EVIDENCE_DIRNAME != FL.QUARANTINE_DIRNAME


class TestTheStoreOwnsNoSecondVocabulary:
    def test_every_status_vocabulary_name_comes_from_the_harness(self):
        """One authority per concern, asserted over the table rather than prose."""
        import harness.long_horizon as LH

        assert sorted(FL.ALLOWED_TRANSITIONS) == sorted(LH.FEATURE_STATUSES), (
            "the transition table's keys and harness.long_horizon.FEATURE_STATUSES have "
            "diverged, which means two answers to 'what is a status'"
        )
        for source, targets in FL.ALLOWED_TRANSITIONS.items():
            for target in targets:
                assert target in LH.FEATURE_STATUSES, (
                    f"{source}->{target} names an unknown status"
                )

    def test_the_store_writes_nothing_into_a_state_file(self):
        """Boundary 4's six-key prefix is a stable contract and this module is not it."""
        source = _module_source()
        assert "state.json" not in source
        assert FL.FEATURE_LEDGER_NAME == "features.json"

    def test_every_refusal_slug_is_registered_in_the_closed_set(self):
        """A refusal carrying an unregistered reason could not be branched on."""
        assert set(FL.STORE_REFUSALS) <= set(FL.ALL_REFUSALS)
        assert len(set(FL.ALL_REFUSALS)) == len(FL.ALL_REFUSALS), (
            "duplicate slug in the table"
        )

    def test_the_two_cheap_detectors_are_delegated_not_reimplemented(self):
        """By source, because a behavioural assertion cannot tell a delegation."""
        source = _module_source()
        assert source.count("from execution.independent_evidence import") >= 2
        assert "def detect_tampering(" not in source
        assert "def premature_completion(" not in source
        assert "def check_verification_evidence(" not in source, (
            "the store must not keep its own copy of the passing-evidence rule; T1 owns it"
        )


class TestAddingAMiddlewareDoesNotTouchTheLoop:
    """The source pin Phase 4 asked for, and the one that was withheld for a while.

    ``runtime/turn_middleware.py`` landed while this phase was in flight. Until it
    did, "adding a middleware must not touch the loop" was not a test, it was a
    wish -- there was no middleware to be added. So this pin is written now, and
    it is a SOURCE pin for the reason the phase named: it is about a relationship
    between two files, and it must stay fast enough to run on every merge.

    The invariant is directional. Middleware wrapping the loop is legitimate and
    is the design; the loop KNOWING about middleware is not, because a loop that
    imports its own policy layer can no longer be reasoned about as the thing
    every other layer is measured against. So the loop must not mention the
    middleware, and the middleware must not reach into the loop's internals.
    """

    LOOP = Path(__file__).resolve().parents[1] / "harness" / "agent_loop.py"
    MIDDLEWARE = Path(__file__).resolve().parents[1] / "runtime" / "turn_middleware.py"

    def test_the_middleware_module_exists_so_this_pin_is_not_vacuous(self):
        """Proven first, because a pin that finds no file proves nothing.

        This is the check whose failure mode bit the gap registry this phase: a
        pin that asserts "X does not exist" goes green forever once X stops being
        absent. So the file is required to be here.
        """
        assert self.MIDDLEWARE.exists(), (
            "runtime/turn_middleware.py is gone, so this file pins nothing. Re-point it at "
            "the module that replaced it, or delete it -- do not leave it asserting an "
            "absence."
        )

    def test_the_loop_does_not_import_or_name_the_middleware(self):
        """The loop stays unaware of its policy layer."""
        loop_source = self.LOOP.read_text(encoding="utf-8", errors="replace")
        for forbidden in (
            "turn_middleware",
            "TurnPolicyPipeline",
            "SEMANTIC_ORDERINGS",
        ):
            assert forbidden not in loop_source, (
                f"harness/agent_loop.py now mentions {forbidden!r}. Middleware wrapping the "
                "loop is the design; the loop importing its own policy layer is how the "
                "reference point stops being a reference point. Wrap it at the caller."
            )

    def test_the_middleware_does_not_import_the_loop(self):
        """And the dependency does not point back, so the graph stays acyclic."""
        middleware_source = self.MIDDLEWARE.read_text(
            encoding="utf-8", errors="replace"
        )
        assert "agent_loop" not in middleware_source, (
            "runtime/turn_middleware.py now imports the loop. The pipeline is supposed to be "
            "a wrapper, and a wrapper that imports its wrapped object is a cycle waiting "
            "for an import order to decide."
        )

    def test_no_kernel_module_imports_the_middleware_either(self):
        """Widened past the one file, because the kernel is where it would hide."""
        kernel = self.LOOP.parent / "agent_kernel"
        offenders = sorted(
            path.name
            for path in kernel.glob("*.py")
            if "turn_middleware" in path.read_text(encoding="utf-8", errors="replace")
        )
        assert not offenders, (
            f"harness/agent_kernel/{offenders} import the middleware. The pure core is the "
            "thing the policy layer is measured against; a core that imports its policies "
            "has no outside."
        )


class TestTheStructuralChecksAreNotRubberStamps:
    def test_the_subscript_scanner_finds_a_planted_write_and_the_wide_scan_finds_one_outside_the_publish_path(
        self,
    ):
        """Both scanners are proven against a REAL violation before they are trusted.

        A scanner that returns ``[]`` makes every "the store never writes an
        obligation field" assertion vacuous, so the scanners are exercised against
        a synthetic source containing exactly the shapes they must catch.
        """
        planted = (
            "def apply_status(document, feature):\n"
            '    feature["status"] = "passing"\n'
            '    item["description"] = "rewritten"\n'
        )
        tree = ast.parse(planted)
        keys = sorted(
            {
                node.targets[0].slice.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Assign)
                and isinstance(node.targets[0], ast.Subscript)
                and isinstance(node.targets[0].slice, ast.Constant)
            }
        )
        assert keys == ["description", "status"]
        assert set(keys) & set(FL.IMMUTABLE_FEATURE_FIELDS), (
            "the scanner must find a planted violation, or the pins above prove nothing"
        )

        helper_plant = 'def a_helper(feature):\n    feature["verification"] = {}\n'
        helper_tree = ast.parse(helper_plant)
        helper_writes = [
            node.targets[0].slice.value
            for node in ast.walk(helper_tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Subscript)
            and isinstance(node.targets[0].slice, ast.Constant)
        ]
        assert helper_writes == ["verification"], (
            "the module-wide scan must catch a violation in a helper, not only in apply_status"
        )
        assert _function_nodes("apply_status"), (
            "the AST function lookup must find the real method"
        )
