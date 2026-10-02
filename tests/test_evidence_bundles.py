"""Evidence bundles: sanitized, reviewable, and self-verifying.

The point of a bundle is that a quality claim is reviewable without
unzipping a gitignored log directory. These tests pin both directions:
a clean bundle verifies, and every leak class (secret, absolute path,
prompt content, tampered digest) is caught.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from evals import evidence
from shared import tracing

REPO_ROOT = Path(__file__).resolve().parents[1]


def _seed_run(
    logs: Path,
    task_id: str = "task-1",
    *,
    with_secret: bool = False,
    with_absolute_path: bool = False,
) -> Path:
    """A real run directory, optionally carrying a leak the bundle must drop."""
    task_dir = logs / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "status": "completed_verified",
        "session_id": "session-1",
        "model": "model-1",
        "cost_usd": 0.002,
        "total_tokens": 900,
        "user_interventions": 0,
    }
    (task_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    rows = [
        {"kind": "task_start", "ts": 10.0, "data": {"session_id": "session-1"}},
        {
            "kind": "model_request",
            "ts": 10.1,
            "data": {
                "model": "model-1",
                "session_id": "session-1",
                "prompt": "SECRET user content that must never be committed",
            },
        },
        {"kind": "tool_call", "ts": 10.2, "data": {"command": "pytest -q"}},
        {"kind": "final_verify", "ts": 10.3, "data": {"target_passed": True}},
        {"kind": "task_end", "ts": 10.4, "data": {"status": "completed_verified"}},
    ]
    if with_absolute_path:
        rows[0]["data"]["cwd"] = str(REPO_ROOT / "src")
    (task_dir / "trace.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    if with_secret:
        overlay = task_dir / "_trace"
        overlay.mkdir(parents=True, exist_ok=True)
        (overlay / f"{task_id}.jsonl").write_text(
            json.dumps(
                {
                    "ts": 10.5,
                    "module": "runtime",
                    "event": "model_routed",
                    "task_id": task_id,
                    "model": "model-1",
                    "api_key": "sk-live-abcdef0123456789",
                    "cost_usd": 0.002,
                }
            )
            + "\n",
            encoding="utf-8",
        )
    return task_dir


def test_a_clean_bundle_verifies_and_records_measured_evidence(tmp_path):
    logs = tmp_path / "logs"
    _seed_run(logs)
    out = tmp_path / "bundles"
    built = evidence.build_bundle(logs, out, label="test")
    verification = evidence.verify_bundle(Path(built["bundle_path"]))
    assert verification["ok"] is True, verification["errors"]
    assert verification["checked_files"] >= 2
    manifest = json.loads(
        (Path(built["bundle_path"]) / evidence.MANIFEST_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert manifest["privacy"] == "shareable"
    assert "slo_report.json" in manifest["entries"]
    assert "ci_truth.json" in manifest["entries"]
    slo = json.loads(
        (Path(built["bundle_path"]) / "slo_report.json").read_text(encoding="utf-8")
    )
    assert slo["verdict"] in ("MEETS_SLOS", "MISSES_SLOS", "INSUFFICIENT_EVIDENCE")
    assert slo["n_records_considered"] == 1


def test_a_bundle_never_carries_prompt_content_or_absolute_paths(tmp_path):
    logs = tmp_path / "logs"
    _seed_run(logs, with_secret=True, with_absolute_path=True)
    out = tmp_path / "bundles"
    built = evidence.build_bundle(logs, out, label="test")
    verification = evidence.verify_bundle(Path(built["bundle_path"]))
    assert verification["ok"] is True, verification["errors"]
    for path in Path(built["bundle_path"]).rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert "sk-live-abcdef0123456789" not in text
        assert "SECRET user content" not in text
        assert str(REPO_ROOT / "src").replace("\\", "/") not in text.replace("\\", "/")


def test_tampering_with_a_bundle_file_fails_verification(tmp_path):
    logs = tmp_path / "logs"
    _seed_run(logs)
    built = evidence.build_bundle(logs, tmp_path / "bundles", label="test")
    target = Path(built["bundle_path"]) / "slo_report.json"
    target.write_text('{"ready": true}\n', encoding="utf-8")
    verification = evidence.verify_bundle(Path(built["bundle_path"]))
    assert verification["ok"] is False
    assert any(error["code"] == "digest_mismatch" for error in verification["errors"])


def test_a_deleted_evidence_file_fails_verification(tmp_path):
    logs = tmp_path / "logs"
    _seed_run(logs)
    built = evidence.build_bundle(logs, tmp_path / "bundles", label="test")
    (Path(built["bundle_path"]) / "slo_report.json").unlink()
    verification = evidence.verify_bundle(Path(built["bundle_path"]))
    assert verification["ok"] is False
    assert any(error["code"] == "file_missing" for error in verification["errors"])


def test_an_injected_secret_in_a_bundle_file_is_caught(tmp_path):
    logs = tmp_path / "logs"
    _seed_run(logs)
    built = evidence.build_bundle(logs, tmp_path / "bundles", label="test")
    bundle = Path(built["bundle_path"])
    manifest = json.loads((bundle / evidence.MANIFEST_NAME).read_text(encoding="utf-8"))
    target = bundle / "slo_report.json"
    target.write_text(
        json.dumps({"note": "token ghp_abcdefghijklmnopqrstuvwxyz0123456789"}) + "\n",
        encoding="utf-8",
    )
    manifest["entries"]["slo_report.json"] = evidence._sha256_bytes(
        target.read_text(encoding="utf-8").encode("utf-8")
    )
    (bundle / evidence.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    verification = evidence.verify_bundle(bundle)
    assert verification["ok"] is False
    assert any(
        error["code"] == "secret_value_survived:note"
        for error in verification["errors"]
    )


def test_a_bundle_without_a_manifest_is_not_evidence(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = evidence.verify_bundle(empty)
    assert result["ok"] is False
    assert any(error["code"] == "manifest_missing" for error in result["errors"])
    tree = evidence.verify_tree(tmp_path)
    assert tree["ok"] is False


def test_span_lifecycle_is_evidence_in_a_bundle(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    _seed_run(logs)
    root = logs / "spanned"
    root.mkdir()
    monkeypatch.setenv(tracing.TRACE_ENV, str(logs))
    tracing._reset_cache()
    for kind in tracing.GENAI_SPAN_KINDS:
        with tracing.span(
            kind, kind, task_id="spanned", run_id="r", session_id="s", model="m"
        ):
            pass
    try:
        built = evidence.build_bundle(logs, tmp_path / "bundles", label="test")
    finally:
        tracing._reset_cache()
    document = json.loads(
        (Path(built["bundle_path"]) / "spans" / "spanned.json").read_text(
            encoding="utf-8"
        )
    )
    assert document["summary"]["span_count"] == len(tracing.GENAI_SPAN_KINDS)
    assert document["lifecycle"]["missing_kinds"] == []
    assert (Path(built["bundle_path"]) / "spans" / "otlp.json").is_file()
    assert evidence.verify_bundle(Path(built["bundle_path"]))["ok"] is True


def test_purge_deletes_expired_bundles_and_writes_a_receipt(tmp_path):
    out = tmp_path / "bundles"
    logs = tmp_path / "logs"
    _seed_run(logs)
    for index in range(3):
        evidence.build_bundle(logs, out, label=f"b{index}", now=1000.0 + index)

    listing = evidence.list_bundles(out)
    assert listing["n_bundles"] == 3
    assert all(row["ok"] for row in listing["bundles"])

    dry = evidence.purge(out, keep_latest=1, dry_run=True)
    assert dry["dry_run"] is True
    assert dry["n_selected"] == 2
    assert evidence.list_bundles(out)["n_bundles"] == 3

    result = evidence.purge(out, keep_latest=1)
    assert result["n_selected"] == 2
    assert result["errors"] == []
    assert evidence.list_bundles(out)["n_bundles"] == 1
    receipt = out / "purge_receipts.jsonl"
    assert receipt.is_file()
    assert (
        json.loads(receipt.read_text(encoding="utf-8").splitlines()[0])["action"]
        == "purge"
    )


def test_purge_by_age_only_removes_expired_bundles(tmp_path):
    out = tmp_path / "bundles"
    logs = tmp_path / "logs"
    _seed_run(logs)
    old = Path(evidence.build_bundle(logs, out, label="old")["bundle_path"])
    new = Path(evidence.build_bundle(logs, out, label="new")["bundle_path"])
    now = time.time()
    for path in [*sorted(old.rglob("*")), old]:
        os.utime(path, (now - 10_000, now - 10_000))
    for path in [*sorted(new.rglob("*")), new]:
        os.utime(path, (now, now))
    result = evidence.purge(out, max_age_s=100.0)
    assert result["n_selected"] == 1
    remaining = evidence.list_bundles(out)["bundles"]
    assert [row["label"] for row in remaining] == ["new"]


def test_cli_build_verify_list_and_purge(tmp_path, capsys):
    logs = tmp_path / "logs"
    _seed_run(logs)
    out = tmp_path / "bundles"
    assert (
        evidence.main(["build", "--logs-root", str(logs), "--out", str(out), "--json"])
        == 0
    )
    built = json.loads(capsys.readouterr().out)
    assert built["verification"]["ok"] is True

    assert evidence.main(["verify", str(out), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    assert evidence.main(["list", str(out), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["n_bundles"] == 1

    assert evidence.main(["purge", str(out), "--keep-latest", "0", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["errors"] == []


def test_cli_build_fails_closed_on_an_unverifiable_bundle(
    tmp_path, monkeypatch, capsys
):
    logs = tmp_path / "logs"
    _seed_run(logs)
    out = tmp_path / "bundles"

    def leak(path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ready": True}) + "\n", encoding="utf-8")
        return "0" * 64

    monkeypatch.setattr(evidence, "_write_json", leak)
    assert (
        evidence.main(["build", "--logs-root", str(logs), "--out", str(out), "--json"])
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["verification"]["ok"] is False


def test_a_json_escape_sequence_is_not_mistaken_for_a_path():
    """A raw-text path scan false-positives on `k:\\nTOOL`-style escapes.

    That false alarm was found by a real bundle build; a gate that cries
    wolf is a gate reviewers learn to ignore, so the scan is structural.
    """
    harmless = json.dumps(
        {"summary": "completed_unverified\nTOOL RESULT edit (ok):\nedited app.py\n"}
    )
    assert evidence._violations(harmless) == []


def test_a_nested_absolute_path_is_still_caught():
    text = json.dumps(
        {"outer": {"inner": [{"note": "reproduced in C:/Users/someone/secret"}]}}
    )
    violations = evidence._violations(text)
    assert any(item.startswith("absolute_path_survived") for item in violations)
    # And the sanitizer removes it from a real document.
    assert evidence._sanitize(json.loads(text)) == {
        "outer": {"inner": [{"note": "someone/secret"}]}
    }


def test_long_free_text_is_replaced_but_numbers_survive():
    document = {
        "verdict": "MEETS_SLOS",
        "ready": True,
        "cost_per_task_usd": 0.0021,
        "summary": "x" * (evidence.MAX_VALUE_CHARS + 10),
    }
    cleaned = evidence._sanitize(document)
    assert cleaned["verdict"] == "MEETS_SLOS"
    assert cleaned["ready"] is True
    assert cleaned["cost_per_task_usd"] == 0.0021
    assert cleaned["summary"].startswith("[redacted free text:")


def test_portable_label_never_leaks_the_operators_home(tmp_path):
    label = evidence.portable_label(tmp_path / "a" / "b" / "c")
    assert label == "b/c"
    assert str(tmp_path) not in label
    assert evidence.portable_label(tmp_path / "").count("/") <= 1
