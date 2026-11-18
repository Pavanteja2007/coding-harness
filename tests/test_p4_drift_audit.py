"""P4/W2.2 — the DRIFT pins. Many individually-valid writes, cumulatively a lie.

``tests/test_feature_ledger_store.py`` and ``tests/test_ledger_store_structural_
pins.py`` cover single acts of forgery: one edit, caught by reading the disk.
This file covers the shape they cannot see, and the shape Phase 4's claim
actually rests on -- because the ledger's whole premise is that **many sessions
each contribute one legitimate write**.

    Each write individually valid. Cumulatively the ledger is a self-graded
    report card.

Every test here drives the REAL store on a REAL project built through T1's own
constructors. None asserts a fixture's answer; each one tampers and then reads
what the store says.

Host-only: no Docker, no provider, no network, no credential.

THE PIN THAT MATTERS MOST
-------------------------
``TestTheDriftAuditCannotBeSatisfiedByAnEmptyLedger`` is the non-vacuity gate.
The drift probes share a control probe, and a control that agreed with every
other probe would make all four of them prove nothing -- so the control's end
state is asserted to DIFFER from each probe's, which is the only way a reader
can tell the probes are discriminating.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

from harness import long_horizon as LH
from memory import feature_ledger_store as FL

FEATURES = (
    ("receipts", "Sum a receipt list."),
    ("pricing", "Apply a discount."),
    ("serializers", "Render rows as text."),
    ("reports", "Render the daily report."),
)


def _project() -> FL.LedgerStore:
    """A real project, built through T1's constructors and the real store."""
    root = Path(tempfile.mkdtemp(prefix="p4w2-drift-"))
    ledger = LH.new_feature_ledger(
        "shop",
        "add the shop features",
        [
            LH.new_feature(
                name,
                text,
                LH.VerificationMethod(method="command", command=f"pytest -k {name}"),
            )
            for name, text in FEATURES
        ],
        created_at="2026-10-06T00:00:00Z",
        sessions=("s1",),
    )
    LH.write_feature_ledger(root / LH.FEATURE_LEDGER_NAME, ledger)
    LH.write_progress_ledger(root / LH.PROGRESS_LEDGER_NAME, "# Progress: shop\n")
    store = FL.LedgerStore(root)
    store.ensure_sealed(sealed_at="2026-10-06T00:00:00Z")
    return store


def _pass_one(store: FL.LedgerStore, feature: str, session: str) -> Any:
    """One LEGITIMATE, evidence-backed status change. Nothing forged."""
    digest, refusal = store.record_evidence(
        FL.EvidenceRecord(
            feature_id=feature,
            outcome="pass",
            kind="run",
            ran=True,
            command=f"pytest -k {feature}",
            exit_code=0,
            observed_at="2026-10-06T00:00:05Z",
            session=session,
        )
    )
    assert refusal is None, f"setup refused: {refusal.render()}"
    return store.apply_status(
        feature,
        LH.PASSING_FEATURE_STATUS,
        evidence_digest=digest,
        actor=session,
        reason=f"{session} finished {feature} and its own check passed",
    )


def _doc(root: Any) -> Dict[str, Any]:
    return json.loads((Path(root) / FL.FEATURE_LEDGER_NAME).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _cleanup() -> Any:
    """Remove every temporary project the module creates."""
    made: List[Path] = []
    yield made
    for path in made:
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# THE CONTROL: many legitimate writes, and the end state they produce
# ---------------------------------------------------------------------------


class TestTheDriftControlIsCLEAN:
    def test_n_legitimate_evidence_backed_passes_leave_the_audit_fully_supported(self) -> None:
        """The baseline the other probes must be DIFFERENT from.

        Four sessions, four features, each with its own stored evidence record.
        ``coverage_share == 1.0`` and no unsupported claim. If a drift probe ended
        here it would have proved nothing, which is why this exists.
        """
        store = _project()
        for index, (name, _text) in enumerate(FEATURES, start=1):
            assert _pass_one(store, name, f"s{index}").ok
        report = FL.LedgerStore(store.root).audit()
        assert report.passing == len(FEATURES)
        assert report.passing_with_stored_evidence == len(FEATURES)
        assert report.unsupported_claims == ()
        assert report.coverage_share == 1.0
        assert FL.LedgerStore(store.root).guard().ok


# ---------------------------------------------------------------------------
# DRIFT 1 -- a RESEAL that narrows the criteria. The store holds.
# ---------------------------------------------------------------------------


class TestAResealCannotNarrowTheCriteria:
    def test_replace_document_reseal_true_still_leaves_the_guard_red(self) -> None:
        """**The most dangerous drift shape, and the store refuses it.**

        ``replace_document(document, reseal=True)`` is a SANCTIONED authoring
        API. It exists so a human can define obligations. It re-signs without
        requiring evidence.

        So the obvious drift is: rewrite what a feature was SUPPOSED to be,
        weakening its check from ``pytest -k receipts`` to
        ``pytest --collect-only``, and let the store bless the forgery. One call.

        Measured on this tree: the narrowed command **is** written to disk, and
        the guard is RED afterwards with ``item_mutated`` and
        ``document_not_in_history``. The history chain still records the
        pre-narrowing document digest, so a fresh seal over a changed obligation
        is a MISMATCH against the recorded history rather than a fresh start.

        This is the single most important thing the drift audit established, and
        it is a POSITIVE result: the seal is not a rubber stamp an authorised
        caller can re-press.
        """
        store = _project()
        document = _doc(store.root)
        for feature in document["features"]:
            if feature["id"] == "receipts":
                feature["verification"] = {"method": "command", "command": "pytest --collect-only"}
        store.replace_document(document, reseal=True)

        reopened = FL.LedgerStore(store.root)
        on_disk = next(
            f["verification"].get("command")
            for f in _doc(store.root)["features"]
            if f["id"] == "receipts"
        )
        assert on_disk == "pytest --collect-only", (
            "the tamper did not land, so this probe is measuring nothing; re-derive it"
        )
        guard = reopened.guard()
        assert not guard.ok, (
            "replace_document(reseal=True) LAUNDERED a narrowed obligation. A seal that can "
            "be re-pressed by an authorised writer is not a permission slip, and this is the "
            "single property the drift audit exists to test."
        )
        assert {"item_mutated", "document_not_in_history"} & set(guard.reasons), (
            f"the guard failed for an unexpected reason {guard.reasons}; re-derive this pin "
            "against the reasons the store actually publishes"
        )

    def test_the_narrowed_feature_cannot_then_be_marked_passing(self) -> None:
        """The second half of the same attack, and it is refused for a THIRD reason.

        Even after a deliberate re-baseline, marking the narrowed feature passing
        on a real run of the WEAKENED command is refused with ``evidence_unknown``
        -- because the evidence store holds nothing for that command. Three
        independent layers, and the probe asserts all three rather than one.
        """
        store = _project()
        document = _doc(store.root)
        for feature in document["features"]:
            if feature["id"] == "receipts":
                feature["verification"] = {"method": "command", "command": "pytest --collect-only"}
        store.replace_document(document, reseal=True)
        reopened = FL.LedgerStore(store.root)
        receipt = reopened.apply_status(
            "receipts",
            LH.PASSING_FEATURE_STATUS,
            reason="drift probe: passing on the narrowed check",
            actor="s9",
        )
        assert not receipt.ok
        assert receipt.refusal is not None
        assert receipt.refusal.reason in {"evidence_unknown", "evidence_required"}


# ---------------------------------------------------------------------------
# DRIFT 2 -- evidence reuse. THE NEW GAP, found by the audit and FIXED here.
# ---------------------------------------------------------------------------


class TestEvidenceCannotBeReusedAcrossSessions:
    def test_session_four_cannot_pass_on_session_ones_evidence(self) -> None:
        """**The gap the drift audit found, and the reason it is a gap.**

        Before the W2.2 fix, ``apply_status`` recorded the ``actor`` and checked
        the evidence resolved, said ``pass``, and came from the feature's own
        command -- but nothing compared the evidence's own ``session`` to the
        actor asking for the status change.

        So session 4 could mark a feature ``passing`` by citing session 1's
        digest. Every check passed. **That is the definition of drift**: each
        write was individually valid, the per-row evidence was real, and the
        per-row ATTRIBUTION was a lie. Four such writes report four sessions of
        verified work on the strength of one run.

        The refusal names the mismatch rather than saying "not allowed", and it
        is a CLOSED slug so a consumer can branch on it.
        """
        store = _project()
        digest, refusal = store.record_evidence(
            FL.EvidenceRecord(
                feature_id="receipts",
                outcome="pass",
                kind="run",
                ran=True,
                command="pytest -k receipts",
                exit_code=0,
                observed_at="2026-10-06T00:00:05Z",
                session="s1",
            )
        )
        assert refusal is None
        receipt = store.apply_status(
            "receipts",
            LH.PASSING_FEATURE_STATUS,
            evidence_digest=digest,
            actor="s4",
            reason="s4 finished receipts and its own check passed",
        )
        assert not receipt.ok, (
            "session 4 passed a feature on session 1's evidence. Nothing compared the "
            "evidence's session to the actor, so the ledger's per-row attribution is a lie "
            "that accumulates into a self-graded report card."
        )
        assert receipt.refusal is not None
        assert receipt.refusal.reason == "evidence_session_mismatch"
        assert "s1" in receipt.refusal.detail and "s4" in receipt.refusal.detail, (
            "the refusal must name BOTH sessions; 'not allowed' is not a reason a session "
            "can act on"
        )

    def test_the_deliberate_override_is_possible_and_is_recorded(self) -> None:
        """A refusal with no escape is a refusal people route around.

        Re-affirming a feature against an unchanged suite is legitimate, so the
        flag exists -- and ``cross_session_evidence`` rides the history row so a
        reader can ENUMERATE every override rather than infer one.
        """
        store = _project()
        digest, _ = store.record_evidence(
            FL.EvidenceRecord(
                feature_id="receipts",
                outcome="pass",
                kind="run",
                ran=True,
                command="pytest -k receipts",
                exit_code=0,
                observed_at="2026-10-06T00:00:05Z",
                session="s1",
            )
        )
        receipt = store.apply_status(
            "receipts",
            LH.PASSING_FEATURE_STATUS,
            evidence_digest=digest,
            actor="s4",
            reason="s4 re-affirmed receipts against an unchanged suite",
            allow_cross_session_evidence=True,
        )
        assert receipt.ok
        transitions = [
            row
            for row in FL.LedgerStore(store.root).history()
            if str(row.get("event") or "") == "transition"
        ]
        assert transitions, "no transition row was appended"
        assert transitions[-1]["cross_session_evidence"] is True, (
            "the override was honoured but NOT recorded, which is the same defect as an "
            "unrecorded gate"
        )
        assert transitions[-1]["evidence_session"] == "s1", (
            "the row must carry the evidence's OWN session so 'who ran it' is answerable "
            "without joining against the evidence store"
        )

    def test_the_refusal_slug_is_registered_in_the_closed_set(self) -> None:
        """A refusal carrying an unregistered reason could not be branched on."""
        assert "evidence_session_mismatch" in FL.ALL_REFUSALS
        assert "evidence_session_mismatch" in FL.STORE_REFUSALS
        assert len(set(FL.ALL_REFUSALS)) == len(FL.ALL_REFUSALS), "duplicate slug"

    def test_a_same_session_pass_is_unaffected(self) -> None:
        """**The control that makes the refusal non-vacuous.**

        A gate that refuses everything passes every refusal test. The ordinary
        case -- evidence recorded and claimed by the SAME session -- must still
        succeed, and it is asserted here rather than assumed.
        """
        store = _project()
        assert _pass_one(store, "receipts", "s1").ok
        assert _pass_one(store, "pricing", "s2").ok

    def test_an_evidence_record_with_no_session_is_not_refused(self) -> None:
        """An evidence row with no session names nobody, so there is nobody to mismatch.

        Failing closed here would make an unnamed-session record unusable, which
        is the opposite of the intended direction: the check exists to stop a
        MISATTRIBUTION, and there is no misattribution to stop.
        """
        store = _project()
        digest, refusal = store.record_evidence(
            FL.EvidenceRecord(
                feature_id="receipts",
                outcome="pass",
                kind="run",
                ran=True,
                command="pytest -k receipts",
                exit_code=0,
                observed_at="2026-10-06T00:00:05Z",
                session="",
            )
        )
        assert refusal is None
        receipt = store.apply_status(
            "receipts",
            LH.PASSING_FEATURE_STATUS,
            evidence_digest=digest,
            actor="s4",
            reason="s4 finished receipts",
        )
        assert receipt.ok, (
            "an evidence record with NO session was refused against an actor; the check is "
            "about a MISATTRIBUTION and there is none to refuse"
        )


# ---------------------------------------------------------------------------
# DRIFT 3 -- oscillation. Honest verdict is DOCUMENTED, not HANDLED.
# ---------------------------------------------------------------------------


class TestTheRegressionEdgeIsStillLegal:
    def test_pass_fail_pass_keeps_the_ledger_green_and_the_history_complete(self) -> None:
        """The oscillation probe's honest verdict is DOCUMENTED, not HANDLED.

        ``passing -> failing -> passing`` across eight sessions ends in a clean
        green ledger. Refusing it would be WRONG: the regression edge exists
        because a ledger whose features can only move forward becomes a lie over
        time -- a regression nobody can record has nowhere to go except into a
        hidden edit.

        What this test pins is therefore the thing that must be TRUE for that
        trade to be honest: **the thrash is visible in the history and in each
        feature's ``sessions_touched``, and invisible in ``features.json`` alone.**
        If a future change made it invisible in the history too, the trade would
        stop being defensible and this test would fail.
        """
        store = _project()
        for index, (name, _text) in enumerate(FEATURES, start=1):
            _pass_one(store, name, f"s{index}")
        for index, (name, _text) in enumerate(FEATURES, start=1):
            backward = store.apply_status(
                name,
                "failing",
                reason=f"s{index + 4} regression: the feature broke again",
                actor=f"s{index + 4}",
            )
            assert backward.ok and backward.backward, (
                "the regression edge stopped being legal; a ledger whose features can only "
                "move forward is a ledger that becomes a lie over time"
            )
            _pass_one(store, name, f"s{index + 8}")

        reopened = FL.LedgerStore(store.root)
        assert reopened.guard().ok
        statuses = [str(f["status"]) for f in _doc(store.root)["features"]]
        assert statuses == [LH.PASSING_FEATURE_STATUS] * len(FEATURES), (
            f"the ledger should read green at the end of the oscillation; got {statuses}"
        )
        transitions = [
            row
            for row in reopened.history()
            if str(row.get("event") or "") == "transition"
        ]
        # One pass + one regression + one re-pass per feature.
        assert len(transitions) == 3 * len(FEATURES), (
            f"the history lost a transition row: {len(transitions)} rows for "
            f"{len(FEATURES)} features x 3 moves. The ledger reads green; if the history "
            "does not show the thrash, a reader has no way to know it happened."
        )
        for feature in _doc(store.root)["features"]:
            assert len(feature.get("sessions_touched") or ()) == 3, (
                f"{feature['id']} was touched by {feature.get('sessions_touched')!r}; the "
                "per-feature trace of who touched it must survive the oscillation"
            )


# ---------------------------------------------------------------------------
# THE AUDIT-TRAIL QUESTION, and the non-vacuity gate for the whole file
# ---------------------------------------------------------------------------


class TestTheWhoWhenAndEvidenceQuestionIsAnswerableFromDiskAlone:
    def test_a_reader_with_only_the_artefacts_can_answer_who_when_and_on_what(self) -> None:
        """**If this fails, the integrity rules are theatre.**

        The brief's test: *"Verify a user can answer: who marked this passing,
        when, and on what evidence? If they cannot, the ledger is not auditable."*

        So this reads ``features.json``, ``history.jsonl`` and ``evidence/`` from
        disk with NO live store object and derives the four answers for every
        passing row: WHO (actor), WHEN (the row's own timestamp), ON WHAT
        EVIDENCE (a digest that resolves to a stored record), and WHO RAN IT
        (the evidence's own session, which is the answer the drift fix added).
        """
        store = _project()
        _pass_one(store, "receipts", "session-2")
        root = Path(store.root)
        _pass_one(FL.LedgerStore(root), "pricing", "session-3")

        ledger = json.loads((root / FL.FEATURE_LEDGER_NAME).read_text(encoding="utf-8"))
        rows = [
            json.loads(line)
            for line in (root / FL.STORE_DIRNAME / FL.HISTORY_NAME)
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        transitions = [r for r in rows if str(r.get("event") or "") == "transition"]
        evidence_files = sorted((root / FL.STORE_DIRNAME / FL.EVIDENCE_DIRNAME).glob("*.json"))

        answered = 0
        claimed = 0
        for feature in ledger["features"]:
            if str(feature.get("status") or "") != LH.PASSING_FEATURE_STATUS:
                continue
            claimed += 1
            rows_for = [
                row
                for row in transitions
                if str(row.get("feature_id") or "") == feature["id"]
                and str(row.get("to_status") or "") == LH.PASSING_FEATURE_STATUS
            ]
            assert rows_for, (
                f"{feature['id']} reads passing in features.json with no transition row "
                "claiming it -- the status board and the audit record disagree"
            )
            for row in rows_for:
                assert row.get("actor"), "a transition row with no actor answers WHO with nothing"
                assert row.get("at"), "a transition row with no timestamp answers WHEN with nothing"
                digest = str(row.get("evidence_digest") or "")
                assert digest, "a passing row with no evidence digest cannot be checked"
                matched = [p for p in evidence_files if p.stem == digest]
                assert matched, (
                    f"{feature['id']}: digest {digest[:12]} resolves to NO FILE, so 'on what "
                    "evidence' is unanswerable"
                )
                record = json.loads(matched[0].read_text(encoding="utf-8"))
                assert record.get("command"), "the evidence record carries no command"
                assert record.get("outcome") is not None, "the evidence record carries no outcome"
                assert row.get("evidence_session"), (
                    "the row carries no evidence_session, so WHO RAN IT is unanswerable "
                    "without joining against the evidence store"
                )
                answered += 1
        assert claimed == 2, f"expected two passing features, saw {claimed}"
        assert answered == claimed, (
            f"{answered} of {claimed} passing rows are answerable; the ledger is not auditable"
        )


class TestTheDriftAuditCannotBeSatisfiedByAnEmptyLedger:
    def test_the_control_probe_and_every_drift_probe_end_in_different_states(self) -> None:
        """**Non-vacuity for this whole file.**

        The four drift probes share a control. A control whose end state matched
        a probe's would make that probe prove nothing -- and an empty or skipped
        file would make all of them prove nothing, which is the exact vacuity
        ``phases/DOCTRINE.md`` section 3 warns about applied to the gate that
        exists to prevent it.

        So each probe's observable end state is collected and asserted to be
        DISTINCT from the control's and from each other's.
        """
        observed: Dict[str, Any] = {}

        control = _project()
        for index, (name, _text) in enumerate(FEATURES, start=1):
            _pass_one(control, name, f"s{index}")
        control_audit = FL.LedgerStore(control.root).audit()
        observed["control"] = (
            control_audit.coverage_share,
            len(FL.LedgerStore(control.root).history()),
            control_audit.passing,
        )

        reuse = _project()
        digest, _ = reuse.record_evidence(
            FL.EvidenceRecord(
                feature_id="receipts",
                outcome="pass",
                kind="run",
                ran=True,
                command="pytest -k receipts",
                exit_code=0,
                observed_at="2026-10-06T00:00:05Z",
                session="s1",
            )
        )
        reuse_receipt = reuse.apply_status(
            "receipts",
            LH.PASSING_FEATURE_STATUS,
            evidence_digest=digest,
            actor="s4",
            reason="s4 finished receipts",
        )
        observed["evidence_reuse"] = (
            bool(reuse_receipt.ok),
            len(FL.LedgerStore(reuse.root).history()),
            1,
        )

        reseal = _project()
        document = _doc(reseal.root)
        for feature in document["features"]:
            if feature["id"] == "receipts":
                feature["verification"] = {"method": "command", "command": "pytest --collect-only"}
        reseal.replace_document(document, reseal=True)
        observed["reseal_narrow"] = (
            FL.LedgerStore(reseal.root).guard().ok,
            len(FL.LedgerStore(reseal.root).history()),
            4,
        )

        osc = _project()
        for index, (name, _text) in enumerate(FEATURES, start=1):
            _pass_one(osc, name, f"s{index}")
        for index, (name, _text) in enumerate(FEATURES, start=1):
            osc.apply_status(
                name, "failing", reason="regression", actor=f"s{index + 4}"
            )
            _pass_one(osc, name, f"s{index + 8}")
        observed["oscillation"] = (
            FL.LedgerStore(osc.root).guard().ok,
            len(FL.LedgerStore(osc.root).history()),
            4,
        )

        assert len(set(map(str, observed.values()))) == len(observed), (
            f"two probes ended in the SAME observable state {observed}; at least one of them "
            "is measuring nothing and must be re-derived until its end state differs"
        )
        assert observed["control"] != observed["reseal_narrow"], (
            "the reseal probe ends where the control ends; it proves nothing"
        )
        assert observed["control"] != observed["evidence_reuse"], (
            "the evidence-reuse probe ends where the control ends; it proves nothing"
        )
        assert observed["reseal_narrow"][0] is False, (
            "if the reseal probe's guard is GREEN the drift went undetected and this file's "
            "central claim is false"
        )
        assert observed["evidence_reuse"][0] is False, (
            "if cross-session evidence is ACCEPTED the drift gap is open"
        )


def test_the_drift_script_runs_and_reports_every_probe() -> None:
    """The script a reviewer runs is itself pinned to RUN and to say something.

    Without this the file above could all be green while the handoff transcript
    is stale, because nothing executes ``scripts/p4_drift_audit.py``.
    """
    script = Path(__file__).resolve().parents[1] / "scripts" / "p4_drift_audit.py"
    assert script.is_file(), (
        "scripts/p4_drift_audit.py is gone. The tamper transcripts and the drift audit are "
        "the evidence for G4's integrity claim; deleting the script deletes the evidence."
    )
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(script.parent.parent),
    )
    assert proc.returncode == 0, (
        f"the drift audit exited {proc.returncode}; its output is the evidence that the "
        f"ledger holds:\n{proc.stdout[-3000:]}\n{proc.stderr[-2000:]}"
    )
    for probe in (
        "N legitimate evidence-backed passes",
        "re-seal with narrowed criteria",
        "reuse an earlier session's evidence",
        "pass/fail/pass oscillation",
        "AUDIT TRAIL",
        "DRIFT SUMMARY",
    ):
        assert probe in proc.stdout, f"the drift audit no longer reports {probe!r}"
    assert "0 probe(s) the ledger does NOT hold" in proc.stdout, (
        "the drift audit reports at least one probe the ledger does not hold"
    )
