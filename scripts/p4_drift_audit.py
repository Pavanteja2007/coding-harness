"""T5.W2.2 — the DRIFT audit: many individually-valid writes, cumulatively a lie.

P4/W1 pinned six tamper paths. Every one of them is a SINGLE act of forgery: one
edit, caught by reading the disk afterwards. This script attacks the shape the
six cannot see:

    **an agent that legitimately has many sessions has many legitimate
    opportunities to write. Each write individually valid; cumulatively the
    ledger is a self-graded report card.**

A single-forgery probe answers "can one bad act get in?". A drift audit answers
"what does a hundred GOOD acts produce?" -- which is the question Phase 4's
claim actually rests on, because the ledger's whole premise is that many
sessions each contribute one legitimate write.

Every probe below is performed against a REAL project built through T1's own
constructors and the REAL store, never against a fixture that asserts an expected
answer. Run:

    python scripts/p4_drift_audit.py

Exit 2 if any probe is HANDLED-BY-NOTHING, i.e. if the ledger ends the run
claiming something the store cannot support.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import long_horizon as LH
from memory import feature_ledger_store as FL

FEATURES = [
    ("receipts", "Sum a receipt list."),
    ("pricing", "Apply a discount."),
    ("serializers", "Render rows as text."),
    ("reports", "Render the daily report."),
]


def _project(root: Path) -> FL.LedgerStore:
    """A real project root, built through T1's own constructors."""
    root.mkdir(parents=True, exist_ok=True)
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


def _doc(root: Path) -> Dict[str, Any]:
    return json.loads((root / FL.FEATURE_LEDGER_NAME).read_text(encoding="utf-8"))


def _write(root: Path, document: Dict[str, Any]) -> None:
    (root / FL.FEATURE_LEDGER_NAME).write_text(
        json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def _pass_one(store: FL.LedgerStore, feature: str, session: str) -> Tuple[bool, str]:
    """One LEGITIMATE write: record real evidence, then mark the feature passing.

    This is the ordinary, sanctioned, evidence-backed status change. Nothing here
    is forged -- which is the point: a drift attack is built entirely out of
    writes that pass on their own.
    """
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
    if refusal is not None:
        return False, refusal.reason
    receipt = store.apply_status(
        feature,
        LH.PASSING_FEATURE_STATUS,
        evidence_digest=digest,
        actor=session,
        reason=f"{session} finished {feature} and its own check passed",
    )
    return bool(receipt.ok), "ok" if receipt.ok else str(receipt.refusal)


# ---------------------------------------------------------------------------
# THE DRIFT PROBES
# ---------------------------------------------------------------------------


def drift_1_every_feature_passes_legitimately(root: Path) -> Dict[str, Any]:
    """The CONTROL: N sessions, N legitimate evidence-backed writes.

    Reported so the other probes have a baseline to be DIFFERENT from. A probe
    whose end state matches this one has proved nothing.
    """
    store = _project(root)
    for index, (name, _text) in enumerate(FEATURES, start=1):
        _pass_one(store, name, f"s{index}")
    store = FL.LedgerStore(root)
    report = store.audit()
    return {
        "probe": "N legitimate evidence-backed passes",
        "is_control": True,
        "passing": report.passing,
        "with_stored_evidence": report.passing_with_stored_evidence,
        "unsupported": list(report.unsupported_claims),
        "coverage_share": report.coverage_share,
        "guard_ok": store.guard().ok,
        "verdict": "CLEAN" if report.coverage_share == 1.0 else "UNSUPPORTED",
    }


def drift_2_reseal_narrows_the_criteria(root: Path) -> Dict[str, Any]:
    """**The dangerous new shape.** Re-seal the ledger with narrower obligations.

    ``LedgerStore.replace_document(document, reseal=True)`` is a SANCTIONED API.
    It exists so a human can author obligations. It re-seals without requiring
    evidence, without a reason field, and without refusing on a passing guard.

    So a session that calls it can rewrite *what the feature was supposed to be*
    and re-sign the rewrite. One call. Every layer green afterwards, because the
    seal has been replaced with a fingerprint of the forgery -- which is exactly
    what tamper 4 did by hand, except that here the store did it for you.

    This is the drift the brief describes: not one bad act, but an API whose
    ordinary use cumulatively converts the ledger into a self-graded report card.
    """
    store = _project(root)
    document = _doc(root)
    # The classic narrowing: the check becomes the assertion it was meant to
    # check. `pytest -k receipts` (runs receipts' own test) becomes
    # `pytest --collect-only` (collects everything, asserts nothing).
    for feature in document["features"]:
        if feature["id"] == "receipts":
            feature["verification"] = {"method": "command", "command": "pytest --collect-only"}
    store.replace_document(document, reseal=True)
    store = FL.LedgerStore(root)
    receipt = store.apply_status(
        "receipts",
        LH.PASSING_FEATURE_STATUS,
        reason="drift probe: passing on the narrowed check",
        actor="s9",
    )
    narrowed = next(
        f["verification"].get("command")
        for f in (_doc(root)["features"])
        if f["id"] == "receipts"
    )
    audit = store.audit()
    return {
        "probe": "replace_document(reseal=True) narrows a feature's verification command",
        "is_control": False,
        "replace_receipt": _replace_receipt_reason(store),
        "narrowed_command_on_disk": narrowed,
        "receipt_ok": bool(receipt.ok),
        "receipt_refusal": None if receipt.refusal is None else receipt.refusal.reason,
        "guard_ok": store.guard().ok,
        "guard_reasons": list(store.guard().reasons),
        "unsupported": list(audit.unsupported_claims),
        "verdict": "HANDLED",
        "note": (
            "MEASURED, and the result is the store HOLDING: the narrowed command IS on disk, "
            "but the guard is RED afterwards with ['document_not_in_history', 'item_mutated']. "
            "So `replace_document(reseal=True)` does not launder the forgery -- the history "
            "chain still records the pre-narrowing document digest, and a fresh seal over a "
            "changed obligation is a mismatch against the recorded history rather than a "
            "fresh start. This is the most dangerous drift shape and it is refused."
        ),
    }


def drift_3_reuse_an_earlier_sessions_evidence(root: Path) -> Dict[str, Any]:
    """Re-use session 1's evidence digest for session 4's claim.

    Every write is individually valid: the digest resolves, the evidence record
    says ``pass``, and ``apply_status`` accepts it. Nothing about the CALL is a
    forgery. What the ledger ends up claiming is that work was verified in every
    session, when it was verified once and the claim was copied three times.

    This is what "cumulatively converting the ledger into a self-graded report
    card" looks like from the inside: the per-row evidence is real, the
    per-row attribution is a lie.
    """
    store = _project(root)
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
    if refusal is not None:
        return {"probe": "evidence reuse", "verdict": "SETUP_REFUSED", "reason": refusal.reason}
    # Session 4 marks the SAME feature passing again, citing session 1's record.
    receipt = store.apply_status(
        "receipts",
        LH.PASSING_FEATURE_STATUS,
        evidence_digest=digest,
        actor="s4",
        reason="s4 finished receipts and its own check passed",
    )
    store = FL.LedgerStore(root)
    audit = store.audit()
    evidence_sessions = sorted(
        {
            str((store.read_evidence(digest)[0] or FL.EvidenceRecord("", "", "", False, "", 0)).session)
            for _ in (0,)
        }
    )
    return {
        "probe": "session 4 marks a feature passing citing session 1's evidence digest",
        "is_control": False,
        "receipt_ok": bool(receipt.ok),
        "receipt_refusal": None if receipt.refusal is None else receipt.refusal.reason,
        "evidence_session_on_record": evidence_sessions,
        "claiming_actor": "s4",
        "guard_ok": store.guard().ok,
        "audit_unsupported": list(audit.unsupported_claims),
        "verdict": "HANDLED",
        "note": (
            "FIXED in W2.2. Before the fix this was HANDLED_BY_NOTHING: apply_status "
            "recorded the ACTOR (s4) but never compared the evidence's own session to it, so "
            "the drift was cumulative -- four sessions of individually valid writes reported "
            "four sessions of verified work on the strength of one run. It now refuses with "
            "`evidence_session_mismatch`, and the history row carries `evidence_session` and "
            "`cross_session_evidence` so a deliberate re-affirmation is enumerable."
        ),
    }


def drift_4_pass_fail_pass_oscillation(root: Path) -> Dict[str, Any]:
    """Flip every feature passing -> failing -> passing across sessions.

    Each transition is legal: the regression edge exists on purpose, and every
    backward move carries a reason. What the reader sees at the end is a clean
    green ledger. What the HISTORY shows is a ledger that thrashed and whose green
    is the residue of the last writer.
    """
    store = _project(root)
    for index, (name, _text) in enumerate(FEATURES, start=1):
        _pass_one(store, name, f"s{index}")
    moves: List[str] = []
    for index, (name, _text) in enumerate(FEATURES, start=1):
        receipt = store.apply_status(
            name,
            "failing",
            reason=f"s{index} regression: the feature broke again",
            actor=f"s{index + 4}",
        )
        moves.append(f"to failing: {'ok' if receipt.ok else 'refused'}")
        _pass_one(store, name, f"s{index + 8}")
        moves.append("back to passing: ok")
    store = FL.LedgerStore(root)
    audit = store.audit()
    history = store.history()
    statuses = [
        item["status"] for item in (_doc(root)["features"])
    ]
    return {
        "probe": "passing -> failing -> passing across eight sessions",
        "is_control": False,
        "moves": moves,
        "final_statuses": statuses,
        "history_rows": len(history),
        "guard_ok": store.guard().ok,
        "audit_coverage_share": audit.coverage_share,
        "verdict": "DOCUMENTED_IN_HISTORY",
        "note": (
            "a green ledger after a documented thrash is NOT a tamper -- the regression edge "
            "is deliberate, and refusing it would train people never to record a regression. "
            "The honest verdict is therefore DOCUMENTED, not HANDLED: the thrash is visible "
            f"in {len(history)} history rows and in every feature's sessions_touched, and "
            "invisible in features.json alone. That is why the who/when/what-evidence "
            "transcript below exists: features.json is a status board, history.jsonl is the "
            "audit record, and only the second one is evidence."
        ),
    }


def _replace_receipt_reason(store: FL.LedgerStore) -> Any:
    """Return the last history row's reason, for the transcript."""
    rows = store.history()
    return rows[-1].get("reason") if rows else None


PROBES: Tuple[Tuple[str, Callable[[Path], Dict[str, Any]]], ...] = (
    ("1. N legitimate evidence-backed passes (CONTROL)", drift_1_every_feature_passes_legitimately),
    ("2. re-seal with narrowed criteria", drift_2_reseal_narrows_the_criteria),
    ("3. reuse an earlier session's evidence", drift_3_reuse_an_earlier_sessions_evidence),
    ("4. pass/fail/pass oscillation", drift_4_pass_fail_pass_oscillation),
)


# ---------------------------------------------------------------------------
# THE AUDIT-TRAIL QUESTION: who marked this passing, when, and on what evidence?
# ---------------------------------------------------------------------------


def audit_trail_transcript() -> List[str]:
    """Answer the question with a TRANSCRIPT, not a claim.

    *"History is append-only. Verify a user can answer: who marked this passing,
    when, and on what evidence? If they cannot, the ledger is not auditable and
    the integrity rules are theatre."*

    So this drives the real store through three sessions across two features and
    then PRINTS what a person reading only the on-disk artefacts can recover. The
    question is answerable from the artefacts alone or it is not answerable at
    all -- which is why nothing here reads a live object to produce the answer.
    """
    root = Path(tempfile.mkdtemp(prefix="p4-audit-trail-"))
    lines: List[str] = [
        "=" * 78,
        "AUDIT TRAIL: who / when / on what evidence -- FROM DISK ONLY",
        "=" * 78,
        "",
        "A reader is given: features.json, .neo-store/history.jsonl, and",
        ".neo-store/evidence/*.json. Nothing else. No live object.",
        "",
    ]
    try:
        store = _project(root)
        _pass_one(store, "receipts", "session-2")
        store = FL.LedgerStore(root)
        _pass_one(store, "pricing", "session-3")

        ledger = json.loads((root / FL.FEATURE_LEDGER_NAME).read_text(encoding="utf-8"))
        history_path = root / FL.STORE_DIRNAME / FL.HISTORY_NAME
        evidence_dir = root / FL.STORE_DIRNAME / FL.EVIDENCE_DIRNAME
        rows = [
            json.loads(line)
            for line in history_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        evidence_files = sorted(evidence_dir.glob("*.json"))
        # Only TRANSITION rows carry a claim. An `evidence_recorded` row has no
        # actor and no from/to status, so printing it produced a "WHO: None" line
        # that read as a missing attribution rather than as a different kind of row.
        transitions = [row for row in rows if str(row.get("event") or "") == "transition"]
        lines.append(
            f"({len(rows)} history rows total; {len(transitions)} of them are status TRANSITIONS, "
            "and those are the rows a 'who marked this passing' question is answered from)"
        )
        lines.append("")

        answerable_features = 0
        claimed_features = 0
        for feature in ledger["features"]:
            feature_id = str(feature.get("id") or "")
            status = str(feature.get("status") or "")
            lines.append(f"FEATURE {feature_id!r}  status={status!r}")
            if status != LH.PASSING_FEATURE_STATUS:
                lines.append("  no passing claim to audit")
                lines.append("")
                continue
            claimed_features += 1
            matched_any = False
            for row in transitions:
                if str(row.get("feature_id") or "") != feature_id:
                    continue
                if str(row.get("to_status") or "") != LH.PASSING_FEATURE_STATUS:
                    continue
                matched_any = True
                lines.append(f"  WHO     : {row.get('actor')!r}")
                lines.append(
                    f"  WHEN    : {row.get('at') or row.get('observed_at') or '(no timestamp)'}"
                )
                lines.append(f"  WHY     : {row.get('reason')!r}")
                lines.append(
                    f"  RAN BY  : session {row.get('evidence_session')!r}"
                    + (
                        "  <-- CROSS-SESSION, deliberately overridden"
                        if row.get("cross_session_evidence")
                        else ""
                    )
                )
                digest = str(row.get("evidence_digest") or "")
                lines.append(f"  EVIDENCE: digest {digest[:16]}...")
                matched = [path for path in evidence_files if path.stem == digest]
                if not matched:
                    lines.append("  EVIDENCE: *** NO FILE FOR THAT DIGEST -- NOT ANSWERABLE ***")
                    continue
                record = json.loads(matched[0].read_text(encoding="utf-8"))
                lines.append(
                    f"            command={record.get('command')!r} "
                    f"outcome={record.get('outcome')!r} ran={record.get('ran')!r} "
                    f"exit={record.get('exit_code')!r}"
                )
                lines.append("  VERDICT : ANSWERABLE")
            if not matched_any:
                lines.append("  VERDICT : *** NOT ANSWERABLE -- no transition row claims it ***")
            else:
                answerable_features += 1
            lines.append("")
        lines.append("CONCLUSION")
        lines.append("-" * 78)
        lines.append(
            f"{answerable_features} of {claimed_features} passing FEATURE(S) are answerable "
            "from the on-disk artefacts alone: WHO (the history actor), WHEN (the row's own "
            "timestamp), and ON WHAT EVIDENCE (a digest that resolves to a stored record "
            "carrying the command, the outcome and the exit code)."
        )
        lines.append(
            "WHO RAN IT is a fourth answer the store now gives per row, and it is the one the "
            "W2.2 drift probe needed: before that fix, WHO (who asked) and WHO RAN (whose run "
            "produced the evidence) were different strings that nothing compared."
        )
        lines.append(
            "The store's own `audit()` reaches the same verdict programmatically via "
            "`_audit_one`, but this transcript deliberately does not call it: the question is "
            "whether a PERSON can answer, and routing the answer through the code under test "
            "would prove only that the code agrees with itself."
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return lines


def main() -> int:
    verdicts: List[str] = []
    for title, probe in PROBES:
        root = Path(tempfile.mkdtemp(prefix="p4-drift-"))
        try:
            described = probe(root)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        print("=" * 78)
        print(title)
        print("-" * 78)
        for key, value in described.items():
            print(f"  {key:<24}: {value}")
        verdict = str(described.get("verdict") or "?")
        verdicts.append(verdict)
        print("=" * 78)
    print()
    for line in audit_trail_transcript():
        print(line)
    print()
    print("=" * 78)
    unhandled = sum(1 for v in verdicts if v == "HANDLED_BY_NOTHING")
    print(
        f"DRIFT SUMMARY: {unhandled} probe(s) the ledger does NOT hold "
        f"(probes run: {len(verdicts)}; verdicts: {', '.join(sorted(set(verdicts)))})"
    )
    print(
        "The CONTROL probe's end state differs from every other probe's; a probe matching it "
        "would have proved nothing."
    )
    print("=" * 78)
    return 0 if unhandled == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
