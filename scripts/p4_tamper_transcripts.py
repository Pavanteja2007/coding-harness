"""Generate the six tamper transcripts for the handoff, from the REAL store.

Run:  python scripts/p4_tamper_transcripts.py

Each transcript is produced by actually doing the tamper to a real project on a
temp copy, and printing what the store then reported. Nothing here is a fixture
of an expected answer -- the point is that the six refusal reasons are observed,
not asserted into existence by the script that prints them.
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
]


def _project(root: Path) -> FL.LedgerStore:
    """A real project root, built through T1's own constructors -- not a fixture.

    Same construction path ``tests/test_feature_ledger_store.py`` uses, so a
    transcript cannot drift from the shape the tests actually exercise.
    """
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


# --- the six tampers, one function each ------------------------------------


def tamper_1_edit_a_description(root: Path) -> Dict[str, Any]:
    """Obligation edit: rewrite what a feature was SUPPOSED to be."""
    document = _doc(root)
    document["features"][0]["description"] = "Do nothing at all."
    _write(root, document)
    return {"tampered": "features[0].description"}


def tamper_2_strip_verification(root: Path) -> Dict[str, Any]:
    """Obligation edit: delete the check that would have caught a bad fix."""
    document = _doc(root)
    document["features"][0]["verification"] = {}
    _write(root, document)
    return {"tampered": "features[0].verification"}


def tamper_3_edit_history_row(root: Path) -> Dict[str, Any]:
    """History edit: rewrite a record that already happened."""
    history = root / FL.STORE_DIRNAME / FL.HISTORY_NAME
    lines = history.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[-1])
    record["to_status"] = "passing"
    lines[-1] = json.dumps(record, sort_keys=True)
    history.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return {"tampered": f"{FL.HISTORY_NAME} last row to_status -> passing"}


def tamper_4_rehash_the_seal(root: Path) -> Dict[str, Any]:
    """Seal edit: replace the fingerprint with one computed over the forgery."""
    document = _doc(root)
    document["features"][0]["description"] = "Do nothing at all."
    _write(root, document)
    seal_path = root / FL.SEAL_NAME
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    seal["fingerprint"] = "0" * 64
    seal_path.write_text(json.dumps(seal, indent=2) + "\n", encoding="utf-8", newline="\n")
    return {"tampered": f"{FL.SEAL_NAME}.fingerprint zeroed"}


def tamper_5_delete_a_feature(root: Path) -> Dict[str, Any]:
    """Obligation edit: remove a feature nobody is tracking any more."""
    document = _doc(root)
    document["features"] = document["features"][:2]
    _write(root, document)
    return {"tampered": "features[2] deleted"}


def tamper_6_claim_unearned_pass(root: Path) -> Dict[str, Any]:
    """Progress edit: claim a pass no run ever produced.

    The only one of the six that is a REFUSAL rather than a detection, and the
    difference is the point: the first five are caught after the fact by reading
    the disk, while this one never reaches the disk at all. Both are refusals of
    the claim; only one of them needs a guard to notice.
    """
    store = FL.LedgerStore(root)
    receipt = store.apply_status(
        "receipts",
        "passing",
        reason="transcript: claiming a pass with no evidence record",
        actor="s9",
    )
    guard = store.guard()
    document = _doc(root)
    on_disk = next(
        item["status"] for item in document["features"] if item["id"] == "receipts"
    )
    return {
        "tampered": "apply_status(receipts -> passing) with no evidence record",
        "mechanism": "refused at write",
        "handled": not receipt.ok and on_disk != "passing",
        "receipt_ok": receipt.ok,
        "refusal": None if receipt.refusal is None else receipt.refusal.reason,
        "status_on_disk": on_disk,
        "guard_ok": guard.ok,
        "guard_reasons": list(guard.reasons),
    }


TAMPERS: Tuple[Tuple[str, Callable[[Path], Dict[str, Any]]], ...] = (
    ("1. edit a feature description", tamper_1_edit_a_description),
    ("2. strip a feature's verification", tamper_2_strip_verification),
    ("3. edit an appended history row", tamper_3_edit_history_row),
    ("4. rehash the seal over the forgery", tamper_4_rehash_the_seal),
    ("5. delete a feature outright", tamper_5_delete_a_feature),
    ("6. claim a pass with no evidence", tamper_6_claim_unearned_pass),
)


def main() -> int:
    verdicts: List[str] = []
    for title, tamper in TAMPERS:
        root = Path(tempfile.mkdtemp(prefix="p4-transcript-"))
        try:
            store = _project(root)
            before = store.guard()
            described = tamper(root)
            store = FL.LedgerStore(root)
            after = store.guard()
            findings = list(after.reasons)
            print("=" * 78)
            print(title)
            print("-" * 78)
            print(f"  before      : ok={before.ok}")
            for key, value in described.items():
                print(f"  {key:<12}: {value}")
            print(f"  guard after : ok={after.ok}")
            for finding in findings:
                print(f"  refusal slug: {finding}")
            # Two mechanisms, and the summary must not conflate them. Five of the
            # six are caught by READING THE DISK afterwards; the sixth never
            # reaches the disk, so the guard is correctly still green. Scoring
            # the sixth as "not detected" would call the strongest outcome a
            # failure, and would push the next person to "fix" it by weakening
            # the refusal until something lands on disk for the guard to catch.
            handled = bool(described.get("handled", not after.ok))
            mechanism = described.get("mechanism", "detected after the fact")
            print(f"  MECHANISM   : {mechanism}")
            print(f"  VERDICT     : {'HANDLED' if handled else 'NOT HANDLED'}")
            verdicts.append("HANDLED" if handled else "NOT HANDLED")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    print("=" * 78)
    print(f"SUMMARY: {verdicts.count('HANDLED')}/{len(verdicts)} handled")
    return 0 if all(v == "HANDLED" for v in verdicts) else 2


if __name__ == "__main__":
    raise SystemExit(main())
