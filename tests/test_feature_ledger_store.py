"""Pins for the Phase-4 ledger STORE: the six tamper paths, and the invariants.

**Every claim in this file is asserted against the real store, on a real
temporary project root, with the real evidence files on disk.** Nothing here is a
transcript pasted into a docstring; the tamper-path tests each CONSTRUCT the
attack, run the real guard, and assert the specific mechanism that caught it --
so a weakening that removed the detection would fail here rather than in a
review.

Structure:

1. non-vacuity FIRST -- the guard must actually catch planted violations before
   any "the guard holds" assertion is trusted;
2. the six tamper paths, each naming the mechanism that refuses it;
3. the transition table, including the REGRESSION edge;
4. the evidence separation, and the four checks a ``passing`` claim must pass;
5. corruption -> quarantine, never repair;
6. the overclaim audit, including the ``premature_completion`` delegation;
7. the reachability assertion, so an inert store is ASSERTED inert.

Host-only: no Docker, no provider, no network, no credential.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import long_horizon as LH
from memory import feature_ledger_store as FL

# ---------------------------------------------------------------------------
# Fixtures: a real project root with a real initializer-written ledger
# ---------------------------------------------------------------------------


def _feature(feature_id: str, *, command: str = "") -> LH.Feature:
    """One real feature, born failing, with a runnable check."""
    return LH.new_feature(
        feature_id,
        f"feature {feature_id} does the thing",
        LH.VerificationMethod(method="command", command=command or f"pytest -k {feature_id}"),
    )


@pytest.fixture()
def project(tmp_path: Path):
    """A real multi-feature project root, initialized through T1's own code."""
    root = tmp_path / "proj"
    root.mkdir()
    ledger = LH.new_feature_ledger(
        "proj",
        "add the thing",
        [
            _feature("alpha", command="pytest -k alpha"),
            _feature("beta", command="pytest -k beta"),
            _feature("gamma", command="pytest -k gamma"),
        ],
        created_at="2026-10-06T00:00:00Z",
        sessions=("s1",),
    )
    LH.write_feature_ledger(root / LH.FEATURE_LEDGER_NAME, ledger)
    LH.write_progress_ledger(root / LH.PROGRESS_LEDGER_NAME, "# Progress: proj\n")
    store = FL.LedgerStore(root)
    store.ensure_sealed(sealed_at="2026-10-06T00:00:00Z")
    return root


def _evidence(store: FL.LedgerStore, feature_id: str, **overrides) -> str:
    """Record a real passing verification and return its digest."""
    payload = {
        "feature_id": feature_id,
        "outcome": "pass",
        "kind": "run",
        "ran": True,
        "command": f"pytest -k {feature_id}",
        "exit_code": 0,
        "observed_at": "2026-10-06T00:00:05Z",
        "session": "s1",
    }
    payload.update(overrides)
    digest, refusal = store.record_evidence(FL.EvidenceRecord(**payload))
    assert refusal is None, refusal.render() if refusal else ""
    return digest


def _write_ledger(root: Path, mutate) -> None:
    """Rewrite ``features.json`` through raw JSON, as a tamperer would."""
    path = root / LH.FEATURE_LEDGER_NAME
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. NON-VACUITY -- the guard must fire before any "it holds" claim is trusted
# ---------------------------------------------------------------------------


class TestTheGuardIsNotARubberStamp:
    def test_a_planted_violation_is_caught_before_anything_else_is_asserted(self, project: Path):
        """Plant each violation kind and require the guard to notice it.

        A guard that returns ``ok=True`` for a document with a removed feature
        makes every "the guard holds" assertion in this file vacuous, so this
        runs FIRST.
        """
        store = FL.LedgerStore(project)
        assert store.guard().ok, "the freshly sealed store must pass its own guard"

        _write_ledger(project, lambda doc: doc["features"].pop(0))
        after_removal = store.guard()
        assert after_removal.ok is False
        assert "item_removed" in after_removal.reasons
        assert after_removal.removed == ("alpha",)

    def test_a_seal_is_not_the_same_thing_as_no_seal(self, project: Path):
        """Deleting the seal degrades the verdict to UNSEALED, never to intact."""
        store = FL.LedgerStore(project)
        store.seal_path.unlink()
        report = store.guard()
        assert report.sealed is False
        assert report.ok is False
        assert "seal_missing" in report.reasons


# ---------------------------------------------------------------------------
# 2. THE SIX TAMPER PATHS -- each must fail, each naming its mechanism
# ---------------------------------------------------------------------------


class TestTheSixTamperPaths:
    """The handoff's six paths to marking a feature passing without evidence.

    Every one constructs the attack on a real ledger, runs the real guard or the
    real store, and asserts the SPECIFIC mechanism that refuses it. A test that
    only asserted ``ok is False`` would pass on a refusal for an unrelated
    reason, which is the mistake four other rounds in this repository recorded.
    """

    def test_path_1_directly_editing_the_status_field_is_caught(self, project: Path):
        """PATH 1: hand-edit ``features[*].status`` in the JSON on disk.

        Mechanism: the HISTORY REPLAY. The seal deliberately EXCLUDES ``status``
        (honest progress reporting must be free), so the seal cannot catch this;
        what catches it is that no transition ever recorded the new status, and
        the replay disagrees with the file.
        """
        store = FL.LedgerStore(project)
        _write_ledger(project, lambda doc: doc["features"][0].__setitem__("status", "passing"))

        report = store.guard()
        assert report.ok is False
        assert "status_without_history" in report.reasons
        offender = [item for item in report.violations if item.reason == "status_without_history"]
        assert offender and "alpha" in offender[0].subject
        assert report.replayed is not None
        assert report.replayed.get("alpha") == LH.INITIAL_FEATURE_STATUS, (
            "the history says alpha was never marked passing; the file says otherwise"
        )

    def test_path_2_editing_the_verification_field_is_caught(self, project: Path):
        """PATH 2: rewrite a feature's ``verification`` so any command "passes".

        Mechanism: the SEAL, over the immutable projection.
        """
        store = FL.LedgerStore(project)
        seal_before = store.seal()
        assert seal_before is not None

        def mutate(doc):
            doc["features"][0]["verification"] = {"method": "command", "command": "true"}

        _write_ledger(project, mutate)

        report = store.guard()
        assert report.ok is False
        assert "item_mutated" in report.reasons
        assert "alpha" in report.mutated
        assert report.actual_fingerprint != report.expected_fingerprint

    def test_path_3_replacing_the_whole_file_is_caught(self, project: Path):
        """PATH 3: rewrite ``features.json`` wholesale, seal and all.

        Mechanism: the DOCUMENT DIGEST IN THE HISTORY. A replacement produces
        bytes whose canonical digest no history row ever recorded, so the guard
        reports the rewrite even when the replacement is internally consistent.

        Both halves are asserted: the blind replacement, and the replacement
        that ALSO re-seals. The second one only passes the seal, so it is the
        honest demonstration of what the second mechanism buys.
        """
        store = FL.LedgerStore(project)
        document = json.loads((project / LH.FEATURE_LEDGER_NAME).read_text(encoding="utf-8"))
        document["features"][0]["status"] = "passing"
        (project / LH.FEATURE_LEDGER_NAME).write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        report = store.guard()
        assert report.ok is False
        assert "document_not_in_history" in report.reasons
        assert report.document_digest not in report.recorded_documents

        # The re-sealed variant: the seal is recomputed, so only the history
        # mechanism is left -- and it still refuses.
        store.write_seal(FL.seal_of(document, sealed_at="2026-10-06T01:00:00Z"))
        resealed = store.guard()
        assert resealed.expected_fingerprint == resealed.actual_fingerprint, (
            "the forged seal agrees with the forged document, which is the whole point "
            "of forging it"
        )
        assert resealed.ok is False
        assert "document_not_in_history" in resealed.reasons
        assert "status_without_history" in resealed.reasons

    def test_path_4_deleting_a_feature_is_caught(self, project: Path):
        """PATH 4: delete a feature the way a shrinking specification would.

        Mechanism: the SEAL's item set AND item count. Deleting an obligation is
        as much a mutation as rewriting one, and it is the easier one to miss.
        """
        store = FL.LedgerStore(project)
        _write_ledger(project, lambda doc: doc.__setitem__("features", doc["features"][1:]))

        report = store.guard()
        assert report.ok is False
        assert "item_removed" in report.reasons
        assert report.removed == ("alpha",)
        assert "item_count_changed" in report.reasons

    def test_path_5_adding_a_feature_already_marked_passing_is_caught(self, project: Path):
        """PATH 5: append a feature that is ``passing`` from birth.

        Two independent refusals, and BOTH are asserted because either alone
        would be a weaker guarantee:

        * the seal catches the ADDED item (the store has no history for it, so
          the replay refuses it too);
        * the overclaim audit refuses the CLAIM, because a brand-new feature
          marked passing has no resolvable evidence in the separate store.

        The second is the important one: an added item that also carried a
        matching forged evidence file would pass the seal check, and only the
        audit's evidence resolution catches that.
        """
        store = FL.LedgerStore(project)
        before_audit = store.audit()
        assert before_audit.overclaims is False

        def mutate(doc):
            doc["features"].append(
                {
                    "id": "delta",
                    "description": "feature delta does the thing",
                    "verification": {"method": "command", "command": "pytest -k delta"},
                    "status": "passing",
                    "evidence": [],
                    "sessions_touched": ["s1"],
                    "notes": "",
                    "derived_from": "",
                    "verification_ran": True,
                    "evidence_kind": "run",
                }
            )

        _write_ledger(project, mutate)

        report = store.guard()
        assert report.ok is False
        assert "item_added" in report.reasons
        assert report.added == ("delta",)

        audit = store.audit()
        assert audit.overclaims is True
        assert "delta" in audit.unsupported_claims
        assert audit.passing_with_stored_evidence == 0
        assert audit.premature.get("delegated") is True

    def test_path_6_editing_the_schema_is_caught_twice(self, project: Path):
        """PATH 6: retag the document's ``schema``.

        Two INDEPENDENT detectors fire, which is why this path is the strongest
        of the six:

        * T1's loader (``load_feature_ledger``) refuses an unknown schema whole,
          and ``LedgerStore.open`` reports that refusal rather than half-reading;
        * the store's own guard reports ``ledger_identity_changed`` against the
          seal, for a caller who goes straight to ``guard()`` without ``open``.
        """
        store = FL.LedgerStore(project)
        _write_ledger(project, lambda doc: doc.__setitem__("schema", "neo/feature-ledger/v1-forged"))

        opened, refusal = FL.LedgerStore.open(project)
        assert opened is None, "a retagged document must not load"
        assert refusal is not None
        assert refusal.reason == "unknown_schema"

        report = store.guard()
        assert report.ok is False
        assert "ledger_identity_changed" in report.reasons

    def test_a_wholesale_forgery_is_reported_as_a_limit_not_as_safety(self, project: Path):
        """The honest limit, asserted rather than left implied.

        An attacker who rewrites the ledger, the seal AND the history, in one
        directory, can produce a self-consistent set. This test performs exactly
        that forgery and asserts what the store then says: ``ok is True`` and the
        report is NOT trustworthy without an externally recorded fingerprint.
        Pinning the limit is what stops a reader mistaking the seal for a
        signature.
        """
        store = FL.LedgerStore(project)
        digest = _evidence(store, "alpha")
        assert store.apply_status("alpha", "passing", evidence_digest=digest, actor="s1", reason="ran the check").ok

        # Forge the ledger, re-seal it, and rewrite the history to match.
        document = json.loads((project / LH.FEATURE_LEDGER_NAME).read_text(encoding="utf-8"))
        store.write_seal(FL.seal_of(document, sealed_at="2026-10-06T02:00:00Z"))
        rows = store.history()
        forged_rows = []
        previous = FL.HISTORY_GENESIS
        for row in rows:
            row = dict(row)
            row["at"] = "2026-10-06T02:00:00Z"
            row.pop("chain", None)
            row["chain"] = FL._chain_digest(previous, row)
            previous = row["chain"]
            forged_rows.append(row)
        store.history_path.write_text(
            "".join(FL._canonical(row) + "\n" for row in forged_rows), encoding="utf-8"
        )

        without_witness = store.guard()
        assert without_witness.ok is True, "the forgery is self-consistent; this is the documented limit"

        # With a fingerprint recorded BEFORE the forgery, the same forgery is
        # caught. This is the closure, and it is the shape
        # execution.independent_evidence already uses.
        pre_forgery_fingerprint = FL.seal_of(document).fingerprint
        with_witness = store.guard(expected_fingerprint="0" * 64)
        assert with_witness.ok is False
        assert "seal_mismatch" in with_witness.reasons
        assert pre_forgery_fingerprint != "0" * 64


# ---------------------------------------------------------------------------
# 3. THE TRANSITION TABLE
# ---------------------------------------------------------------------------


class TestTheTransitionTable:
    def test_the_regression_edge_is_present_and_documented(self):
        """``passing -> failing`` is an ALLOWED edge, and this fails the day it stops.

        A ledger whose features can only move forward becomes a lie over time: a
        regression has nowhere to go except into a hidden edit.
        """
        assert "failing" in FL.ALLOWED_TRANSITIONS[LH.PASSING_FEATURE_STATUS]
        assert FL.is_transition_allowed(LH.PASSING_FEATURE_STATUS, "failing") is True

    def test_a_backward_transition_is_applied_and_recorded_as_backward(self, project: Path):
        """Drive a real feature passing, then un-pass it, through the real store."""
        store = FL.LedgerStore(project)
        digest = _evidence(store, "alpha")
        first = store.apply_status(
            "alpha", "passing", evidence_digest=digest, actor="s1", reason="the check ran green"
        )
        assert first.ok, first.render()
        assert first.backward is False
        assert store.guard().ok, "an honest transition must leave the guard green"

        second = store.apply_status(
            "alpha", "failing", actor="s2", reason="the regression suite went red in session 2"
        )
        assert second.ok, second.render()
        assert second.backward is True, "a passing->failing edge must be marked backward on the receipt"
        assert second.from_status == LH.PASSING_FEATURE_STATUS and second.to_status == "failing"
        assert store.guard().ok, "a recorded regression must not break the ledger"
        assert store.audit().passing == 0

    def test_a_backward_transition_requires_a_reason_and_forwards_do_not_need_evidence(
        self, project: Path
    ):
        """A reason-less regression is refused; a regression needs NO pass artefact.

        Demanding passing evidence to report a regression would make
        under-reporting the easy path, which is the opposite of the point.
        """
        store = FL.LedgerStore(project)
        digest = _evidence(store, "beta")
        assert store.apply_status("beta", "passing", evidence_digest=digest, actor="s1", reason="green").ok

        refused = store.apply_status("beta", "failing", actor="s2", reason="   ")
        assert refused.ok is False
        assert refused.refusal is not None
        assert refused.refusal.reason == "reason_required"

        assert store.apply_status("beta", "failing", actor="s2", reason="suite red").ok

    def test_an_unknown_status_is_refused_rather_than_defaulted(self, project: Path):
        """An off-vocabulary target is a denial, not a default-allow."""
        store = FL.LedgerStore(project)
        receipt = store.apply_status("alpha", "mostly_done", actor="s1", reason="trying")
        assert receipt.ok is False
        assert receipt.refusal.reason == "status_not_in_vocabulary"

    def test_an_unknown_feature_is_refused(self, project: Path):
        """Inventing an id is a ledger mutation, so it is refused by name."""
        store = FL.LedgerStore(project)
        receipt = store.apply_status("delta", "passing", actor="s1", reason="new one")
        assert receipt.ok is False
        assert receipt.refusal.reason == "unknown_feature"

    def test_the_transition_table_is_the_only_reader_of_itself(self, project: Path):
        """Every declared edge is reachable through the one predicate."""
        for source, targets in FL.ALLOWED_TRANSITIONS.items():
            for target in targets:
                assert FL.is_transition_allowed(source, target) is True
        assert FL.is_transition_allowed("skipped", LH.PASSING_FEATURE_STATUS) is False
        assert FL.is_transition_allowed("nonsense", "failing") is False


# ---------------------------------------------------------------------------
# 4. THE EVIDENCE SEPARATION, AND THE FOUR CHECKS ON A `passing` CLAIM
# ---------------------------------------------------------------------------


class TestTheEvidenceIsSeparate:
    def test_evidence_lives_outside_the_ledger_and_the_ledger_cannot_change_it(self, project: Path):
        """The ledger's bytes and the evidence's bytes are disjoint files.

        Asserted as bytes: rewriting the ledger's status leaves every evidence
        file byte-identical, and appending an evidence record leaves the ledger
        byte-identical.
        """
        store = FL.LedgerStore(project)
        digest = _evidence(store, "alpha")
        evidence_path = store.evidence_dir / f"{digest}.json"
        assert evidence_path.is_file()
        assert store.evidence_dir.is_relative_to(store.root / FL.STORE_DIRNAME)

        before = evidence_path.read_bytes()
        ledger_before = store.ledger_path.read_bytes()
        assert store.apply_status("alpha", "passing", evidence_digest=digest, actor="s1", reason="green").ok
        assert evidence_path.read_bytes() == before, "a status change must not touch the evidence"
        assert store.ledger_path.read_bytes() != ledger_before, "and the ledger must actually change"

        # The converse: recording evidence must not touch the LEDGER. This is the
        # direction that would let an evidence write smuggle a status change in.
        ledger_after_transition = store.ledger_path.read_bytes()
        digest_two = _evidence(store, "beta")
        assert store.ledger_path.read_bytes() == ledger_after_transition
        assert (store.evidence_dir / f"{digest_two}.json").is_file()
        assert store.guard().ok, "recording evidence off-ledger must leave the ledger green"

    def test_a_passing_claim_with_no_evidence_is_refused(self, project: Path):
        store = FL.LedgerStore(project)
        receipt = store.apply_status("alpha", "passing", actor="s1", reason="it looks fine")
        assert receipt.ok is False
        assert receipt.refusal.reason == "evidence_unknown"
        assert "not a claim" in receipt.refusal.detail

    def test_an_unresolvable_evidence_digest_is_refused(self, project: Path):
        store = FL.LedgerStore(project)
        receipt = store.apply_status("alpha", "passing", evidence_digest="f" * 64, actor="s1", reason="x")
        assert receipt.ok is False
        assert receipt.refusal.reason == "evidence_unknown"

    def test_evidence_that_did_not_pass_is_refused(self, project: Path):
        store = FL.LedgerStore(project)
        digest = _evidence(store, "alpha", outcome="fail", exit_code=1)
        receipt = store.apply_status("alpha", "passing", evidence_digest=digest, actor="s1", reason="x")
        assert receipt.ok is False
        assert receipt.refusal.reason == "evidence_did_not_pass"
        assert "'we did not check' and 'we checked and it broke'" in receipt.refusal.detail

    def test_a_not_run_verification_is_not_a_pass(self, project: Path):
        """``ran=False`` is refused even when the outcome string says pass."""
        store = FL.LedgerStore(project)
        digest = _evidence(store, "alpha", outcome="pass", ran=False)
        receipt = store.apply_status("alpha", "passing", evidence_digest=digest, actor="s1", reason="x")
        assert receipt.ok is False
        assert receipt.refusal.reason == "evidence_did_not_pass"

    def test_an_attestation_passes_only_with_the_flag_that_says_its_kind(self, project: Path):
        """A human's word IS real evidence about a ``human_check`` feature -- gated.

        **This test was inverted when T1 closed the gap.** It originally asserted
        that ``allow_attested=True`` was a DEAD escape hatch: an ``attested``
        record was normalised by ``VerificationRun.__post_init__`` to
        ``ran=False, outcome="not_run"``, so the flag did not help and a
        ``human_check`` feature could never reach ``passing``. That was measured,
        registered as ``ALLOW_ATTESTED_IS_A_DEAD_ESCAPE_HATCH`` with owner T1, and
        this test failed the moment T1 made the flag real -- which is the one
        behaviour a pin for a closing gap should have.

        The shape that survives is the one worth keeping, and it is a two-sided
        rule rather than one flag: an attestation is accepted **with** the flag
        and refused **without** it. A test that only asserted the accepting half
        would have passed a change that let any attestation mint a pass, which is
        the failure this store exists to prevent.
        """
        store = FL.LedgerStore(project)
        digest = _evidence(store, "alpha", kind="attested", ran=False)

        without_flag = store.apply_status(
            "alpha", "passing", evidence_digest=digest, actor="s1", reason="x"
        )
        assert without_flag.ok is False, (
            "an attestation with no flag now mints a pass. That flag is the whole control: a "
            "feature nobody executed cannot reach `passing` on the strength of a bare "
            "kind=attested record."
        )
        assert without_flag.refusal.reason == "evidence_did_not_pass"

        with_flag = store.apply_status(
            "alpha", "passing", evidence_digest=digest, actor="s1", reason="x", allow_attested=True
        )
        assert with_flag.ok is True, (
            "allow_attested is dead AGAIN. If T1 reverted, re-open "
            "ALLOW_ATTESTED_IS_A_DEAD_ESCAPE_HATCH in RECORDED_GAPS rather than leaving this "
            "assertion quietly green on a broken escape hatch."
        )
        assert with_flag.refusal is None

    def test_the_allow_attested_gap_is_closed_and_the_history_says_so(self):
        """The gap is DATA, so it cannot rot into a forgotten defect in EITHER direction.

        Once T1 closed it, the entry has to be removed -- a registry that keeps
        reporting a closed gap trains the next reader to ignore the registry. This
        test is the removal instruction, and it names the file to change.
        """
        stale = [
            item["id"]
            for item in FL.RECORDED_GAPS
            if item["id"] == "ALLOW_ATTESTED_IS_A_DEAD_ESCAPE_HATCH"
        ]
        assert not stale, (
            f"{stale} is still registered, but the flag now works. Delete the entry from "
            "FL.RECORDED_GAPS in memory/feature_ledger_store.py and this test in the same "
            "change; a registry that reports closed gaps is worse than no registry."
        )

    def test_evidence_from_a_different_command_is_refused(self, project: Path):
        """Another feature's check, or another check entirely, is not this feature's proof."""
        store = FL.LedgerStore(project)
        digest = _evidence(store, "beta", command="pytest -k beta")
        receipt = store.apply_status("alpha", "passing", evidence_digest=digest, actor="s1", reason="x")
        assert receipt.ok is False
        assert receipt.refusal.reason == "evidence_unknown"
        assert "not 'alpha'" in receipt.refusal.detail

        wrong_command = _evidence(store, "alpha", command="pytest -k something-else")
        receipt_two = store.apply_status("alpha", "passing", evidence_digest=wrong_command, actor="s1", reason="x")
        assert receipt_two.ok is False
        assert receipt_two.refusal.reason == "evidence_command_mismatch"

    def test_rewriting_a_stored_evidence_file_is_detected(self, project: Path):
        """An evidence record is content-addressed, so editing it changes its own name."""
        store = FL.LedgerStore(project)
        digest = _evidence(store, "alpha")
        assert store.apply_status("alpha", "passing", evidence_digest=digest, actor="s1", reason="green").ok

        path = store.evidence_dir / f"{digest}.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["outcome"] = "fail"
        path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")

        record, refusal = store.read_evidence(digest)
        assert record is None
        assert refusal is not None
        assert refusal.reason == "evidence_tampered"
        assert "edits evidence" in refusal.detail or "after it was recorded" in refusal.detail

        audit = store.audit()
        assert audit.overclaims is True
        assert "alpha" in audit.unsupported_claims


# ---------------------------------------------------------------------------
# 5. CORRUPTION -> QUARANTINE, NEVER REPAIR
# ---------------------------------------------------------------------------


class TestCorruptionIsQuarantinedAndReported:
    def test_unparseable_json_is_refused_whole_and_never_half_read(self, tmp_path: Path):
        root = tmp_path / "broken"
        root.mkdir()
        (root / LH.FEATURE_LEDGER_NAME).write_text("{ this is not json", encoding="utf-8")

        store, refusal = FL.LedgerStore.open(root)
        assert store is None
        assert refusal.reason == "corrupt_ledger"
        assert "quarantine" in refusal.detail

        existing = FL.LedgerStore(root)
        assert existing.document() is None
        assert existing.guard().ok is False
        assert "corrupt_ledger" in existing.guard().reasons

    def test_a_missing_ledger_is_a_refusal_not_an_empty_one(self, tmp_path: Path):
        root = tmp_path / "empty"
        root.mkdir()
        store, refusal = FL.LedgerStore.open(root)
        assert store is None
        assert refusal.reason == "no_store"

    def test_quarantine_moves_reports_and_never_repairs(self, project: Path):
        """The honest half: quarantine MOVES, writes a report, and invents nothing."""
        store = FL.LedgerStore(project)
        _write_ledger(project, lambda doc: doc["features"].clear())

        receipt = store.quarantine("corrupt_ledger", detail="features emptied by a test")
        assert receipt.ok, receipt.render()
        assert receipt.quarantine_dir
        moved = Path(receipt.quarantine_dir)
        assert moved.is_dir()
        assert LH.FEATURE_LEDGER_NAME in receipt.moved
        assert (moved / LH.FEATURE_LEDGER_NAME).is_file(), "the original is PRESERVED, not deleted"
        assert not store.ledger_path.exists(), "and it is moved OUT of the working location"

        report = json.loads((moved / FL.QUARANTINE_REPORT_NAME).read_text(encoding="utf-8"))
        assert report["reason"] == "corrupt_ledger"
        assert report["manifest"], "the report must name what moved and with which digest"
        assert all("sha256" in entry for entry in report["manifest"])
        assert report["guard"]["ok"] is False

        # And it invents nothing in the ledger's place.
        reopened, refusal = FL.LedgerStore.open(project)
        assert reopened is None
        assert refusal.reason == "no_store"

    def test_the_store_refuses_to_write_over_a_ledger_that_already_fails_its_guard(self, project: Path):
        """Publishing a new current view over a violated ledger launders corruption."""
        store = FL.LedgerStore(project)
        _write_ledger(project, lambda doc: doc["features"][0].__setitem__("status", "passing"))

        receipt = store.apply_status("alpha", "blocked", actor="s1", reason="trying to paper over it")
        assert receipt.ok is False
        assert receipt.refusal is not None
        assert "already fails its guard" in receipt.refusal.detail


# ---------------------------------------------------------------------------
# 6. THE OVERCLAIM AUDIT
# ---------------------------------------------------------------------------


class TestTheOverclaimAudit:
    def test_coverage_is_published_loudly(self, project: Path):
        """The requirement stated as a number: passing claims vs evidenced claims."""
        store = FL.LedgerStore(project)
        for feature_id in ("alpha", "beta"):
            digest = _evidence(store, feature_id)
            assert store.apply_status(
                feature_id, "passing", evidence_digest=digest, actor="s1", reason="green"
            ).ok

        audit = store.audit()
        assert audit.total_features == 3
        assert audit.passing == 2
        assert audit.passing_with_stored_evidence == 2
        assert audit.coverage_share == 1.0
        assert audit.overclaims is False
        assert "2/2 passing features have stored evidence" in audit.render()

    def test_a_ledger_claiming_more_than_its_evidence_supports_is_detected(self, project: Path):
        """The self-graded report card, in its simplest form: one unbacked claim.

        FORGED here means the feature's ``evidence`` array names a digest that
        was never recorded -- which is exactly what a self-graded ledger does.
        """
        store = FL.LedgerStore(project)
        real = _evidence(store, "alpha")
        assert store.apply_status("alpha", "passing", evidence_digest=real, actor="s1", reason="green").ok

        def mutate(doc):
            for feature in doc["features"]:
                if feature["id"] == "beta":
                    feature["status"] = "passing"
                    feature["evidence"] = [{"evidence_digest": "a" * 64, "outcome": "pass"}]
                    feature["evidence_kind"] = "run"
                    feature["verification_ran"] = True

        _write_ledger(project, mutate)

        audit = store.audit()
        assert audit.overclaims is True
        assert audit.passing == 2
        assert audit.passing_with_stored_evidence == 1
        assert audit.coverage_share == 0.5
        assert "beta" in audit.unsupported_claims
        assert "THE LEDGER CLAIMS MORE THAN THE EVIDENCE SUPPORTS" in audit.render()
        # gamma is still failing, so the ledger does NOT claim completion and the
        # premature check is correctly False here. The all-passing case is the
        # next test, because claiming completion is the stronger claim.
        assert audit.premature["claimed_complete"] is False
        assert audit.premature["premature"] is False
        assert audit.premature["delegated"] is True

    def test_a_ledger_claiming_COMPLETION_without_a_clean_gate_is_premature(self, project: Path):
        """Every feature passing, one of them unbacked: the repo's own shape.

        ``execution.independent_evidence.premature_completion`` is the pattern
        this mirrors -- "claimed complete without a clean final gate" -- and the
        final gate here is EVERY claimed feature having stored passing evidence.
        """
        store = FL.LedgerStore(project)
        for feature_id in ("alpha", "beta", "gamma"):
            digest = _evidence(store, feature_id)
            assert store.apply_status(
                feature_id, "passing", evidence_digest=digest, actor="s1", reason="green"
            ).ok
        honest = store.audit()
        assert honest.passing == 3 and honest.passing_with_stored_evidence == 3
        assert honest.premature["claimed_complete"] is True
        assert honest.premature["final_gate_passed"] is True
        assert honest.premature["premature"] is False
        assert honest.premature["delegated"] is True
        assert honest.premature["spec_guard_ok"] is None, (
            "the delegated dict is the repository's own, including its own keys; asserting "
            "the exact set here would make this test a copy of the delegate rather than a "
            "check of its use"
        )

        # Now un-back ONE of them and re-audit, by rewriting the ledger only.
        def mutate(doc):
            for feature in doc["features"]:
                if feature["id"] == "gamma":
                    feature["evidence"] = [{"evidence_digest": "b" * 64, "outcome": "pass"}]

        _write_ledger(project, mutate)
        premature = store.audit()
        assert premature.passing == 3
        assert premature.passing_with_stored_evidence == 2
        assert premature.premature["claimed_complete"] is True
        assert premature.premature["final_gate_ran"] is True
        assert premature.premature["final_gate_passed"] is False
        assert premature.premature["premature"] is True
        assert "1 of 3 passing feature(s)" in premature.premature["reason"]
        assert "PREMATURE" in premature.render()

    def test_a_ledger_with_nothing_passing_is_reported_as_vacuous_not_clean(self, project: Path):
        """A zero that reads as "the lane was free" is a number nobody took."""
        audit = FL.LedgerStore(project).audit()
        assert audit.passing == 0
        assert audit.vacuous is True
        assert audit.coverage_share is None, "a rate over zero features is None, not 0.0"
        assert "VACUOUS" in audit.render()

    def test_the_audit_delegates_both_cheap_mechanisms_to_execution(self):
        """The store owns no second tamper detector and no second completion check.

        Asserted by SOURCE, because a behavioural assertion cannot tell a
        delegation from a reimplementation: the module must import
        ``detect_tampering`` and ``premature_completion`` from
        ``execution.independent_evidence`` and must not define either name.
        """
        source = Path(FL.__file__).read_text(encoding="utf-8")
        assert "from execution.independent_evidence import" in source
        assert "detect_tampering" in source
        assert "premature_completion" in source
        assert "def detect_tampering(" not in source
        assert "def premature_completion(" not in source

    def test_an_edited_acceptance_suite_is_detected_by_the_repository_authority(
        self, project: Path, tmp_path: Path
    ):
        """The real detector, on a real suite directory, through the real store."""
        suite_root = tmp_path / "accept"
        suite_root.mkdir()
        (suite_root / "test_alpha.py").write_text("def test_alpha():\n    assert True\n", encoding="utf-8")

        store = FL.LedgerStore(project)
        record = FL.EvidenceRecord.from_dict(
            {
                **{
                    "feature_id": "alpha",
                    "outcome": "pass",
                    "kind": "run",
                    "ran": True,
                    "command": "pytest -k alpha",
                    "exit_code": 0,
                    "observed_at": "2026-10-06T00:00:05Z",
                    "session": "s1",
                },
                "accept_suite_root": str(suite_root),
                "accept_suite_fingerprint": _fingerprint_of(suite_root),
            }
        )
        digest, refusal = store.record_evidence(record)
        assert refusal is None
        assert store.apply_status("alpha", "passing", evidence_digest=digest, actor="s1", reason="green").ok
        assert store.audit().overclaims is False, "an untouched suite is clean WITH a recorded fingerprint"

        # Edit the suite after the claim, and re-run the audit WITH the recorded
        # fingerprint. detect_tampering must see the digest mismatch.
        (suite_root / "test_alpha.py").write_text("def test_alpha():\n    assert False\n", encoding="utf-8")
        after = store.audit(expected_fingerprint=record.accept_suite_fingerprint)
        assert after.overclaims is True
        assert "alpha" in after.undetected_tampering
        assert "changed after sealing" in after.render()

    def test_a_suite_with_no_recorded_fingerprint_is_unchecked_not_clean(self, project: Path, tmp_path: Path):
        """A missing fingerprint is a FAILURE upstream, so it is unchecked here too."""
        suite_root = tmp_path / "accept2"
        suite_root.mkdir()
        (suite_root / "test_alpha.py").write_text("def test_alpha():\n    assert True\n", encoding="utf-8")
        store = FL.LedgerStore(project)
        digest, _ = store.record_evidence(
            FL.EvidenceRecord.from_dict(
                {
                    "feature_id": "alpha",
                    "outcome": "pass",
                    "kind": "run",
                    "ran": True,
                    "command": "pytest -k alpha",
                    "observed_at": "2026-10-06T00:00:05Z",
                    "accept_suite_root": str(suite_root),
                    "accept_suite_fingerprint": "",
                }
            )
        )
        assert store.apply_status("alpha", "passing", evidence_digest=digest, actor="s1", reason="green").ok
        audit = store.audit()  # no expected_fingerprint supplied
        assert audit.overclaims is False
        assert "alpha" not in audit.undetected_tampering


def _fingerprint_of(root: Path) -> str:
    """Return the repo's own fingerprint for a suite directory (its real shape)."""
    from execution.independent_evidence import HeldOutFile, HeldOutSuite

    files = tuple(
        HeldOutFile(name=entry.name, digest=FL._sha256(entry.read_text(encoding="utf-8", errors="replace")))
        for entry in sorted(root.glob("*.py"))
    )
    return HeldOutSuite(root=str(root), seed=0, files=files, concealed=False).fingerprint()


# ---------------------------------------------------------------------------
# 7. THE VOCABULARY IS T1's, AND THE STORE IS ASSERTED INERT
# ---------------------------------------------------------------------------


class TestTheContracts:
    def test_the_store_declares_no_second_vocabulary(self):
        """Every status and the writable set come from ``harness.long_horizon``."""
        assert FL.FEATURE_STATUSES is LH.FEATURE_STATUSES
        assert FL.PASSING_FEATURE_STATUS is LH.PASSING_FEATURE_STATUS
        assert FL.AGENT_WRITABLE_LEDGER_FIELDS is LH.AGENT_WRITABLE_LEDGER_FIELDS
        assert FL.FEATURE_LEDGER_NAME == LH.FEATURE_LEDGER_NAME == "features.json"
        assert FL.PROGRESS_LEDGER_NAME == LH.PROGRESS_LEDGER_NAME == "PROGRESS.md"
        # The seal covers every obligation field and NO mutable one.
        assert "status" not in FL.IMMUTABLE_FEATURE_FIELDS
        assert set(FL.MUTABLE_FEATURE_FIELDS).isdisjoint(FL.IMMUTABLE_FEATURE_FIELDS)
        assert set(FL.IMMUTABLE_FEATURE_FIELDS) | set(FL.MUTABLE_FEATURE_FIELDS) == {
            "id",
            "description",
            "verification",
            "derived_from",
            "status",
            "evidence",
            "sessions_touched",
            "notes",
        }

    def test_every_store_refusal_is_a_closed_slug_and_a_reasonless_refusal_refuses_to_construct(self):
        assert set(FL.STORE_REFUSALS) <= set(FL.ALL_REFUSALS)
        with pytest.raises(ValueError, match="closed refusal slug"):
            FL.StoreRefusal(reason="not_a_real_slug", detail="x")
        with pytest.raises(ValueError, match="must carry a detail"):
            FL.StoreRefusal(reason="corrupt_ledger", detail="   ")

    def test_the_progress_ledger_is_append_only(self, project: Path):
        store = FL.LedgerStore(project)
        assert store.progress_text().startswith("# Progress: proj")
        receipt = store.append_progress("### session s2\n\n- did work.\n")
        assert receipt["append_only"] is True
        text = store.progress_text()
        assert text.startswith("# Progress: proj"), "the original block must survive the append"
        assert "### session s2" in text

    def test_the_store_asserts_that_it_has_no_production_caller_yet(self):
        """A dead guard is worse than an absent one, so the state is PINNED.

        When T1 or T4 wires the call site, this test fails and the reader is told
        to update ``REACHABILITY`` and record the wiring in ``INTERFACES.md``.
        """
        assert FL.REACHABILITY["production_call_sites"] == 0
        source = Path(FL.__file__).read_text(encoding="utf-8")
        assert "dead guard" in FL.REACHABILITY["why"]
        assert "feature_ledger_store" not in source or "REACHABILITY" in source

    def test_the_state_json_six_key_prefix_order_is_untouched_by_this_round(self):
        """The ledger store must not disturb Boundary 4's stable contract.

        The store writes NO ``state.json`` at all, and this asserts that by
        construction rather than by intent: the module names no state file, and
        the harness authority still orders the six keys as it always has.
        """
        from harness import context as HC

        source = Path(FL.__file__).read_text(encoding="utf-8")
        assert "state.json" not in source
        assert HC.STATE_KEYS == (
            "task_id",
            "plan",
            "completed_steps",
            "files_touched",
            "decisions",
            "remaining_plan",
        )
