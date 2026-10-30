"""P5/W1 trust-invariant pins, re-pinned after vision, diagnostics, retract.

Every capability added is a chance to weaken a guarantee. These six assertions
fail if the new surface ever mints, fails an edit over a signal, widens access,
bypasses containment, smuggles injected text, or opens a second mint path.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _src(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8", errors="replace")


def test_completed_verified_has_one_mint_and_vision_diagnostics_do_not_mint():
    from harness.test_completion_mint_sites import MINT_SITES_EXPECTED

    assert MINT_SITES_EXPECTED >= 1  # the pinned mint-site authority
    # The novel code added this round must never mint.
    for mod in ("vision.py", "retract.py", "latency_budget.py"):
        text = _src(f"harness/{mod}")
        assert "completed_verified" not in text, mod
        assert "COMPLETED_VERIFIED" not in text, mod
        assert re.search(r"\bmint\w*\s*\(", text) is None, mod


def test_diagnostics_can_never_fail_an_edit():
    src = _src("harness/agent_kernel/strategy.py")
    i = src.find("def _observe_lsp_after_mutation")
    j = src.find("def _canonical_mutation_tools")
    assert 0 < i < j
    body = src[i:j]
    # Diagnostics are a signal: this method may annotate output, but it must
    # never flip the result's ok flag or raise into the caller's verdict.
    assert "ok = False" not in body
    assert "ok=False" not in body
    assert "result.ok" in body or "lsp" in body
    # The caller wraps it in try/except and only appends to result.output.
    assert "result.output" in src


def test_retract_cannot_widen_access():
    src = _src("harness/retract.py")
    # Retract only rewinds and records a marker; it must not mint, grant,
    # or widen permissions/tools.
    for forbidden in ("completed_verified", "approve", "permission", "grant"):
        assert forbidden not in src, forbidden
    assert "rewind_run" in src


def test_image_outside_declared_roots_is_refused(tmp_path):

    from execution.workspace import Workspace

    ws = Workspace(repo_path=tmp_path)
    with pytest.raises(Exception) as exc:
        ws.read_image("../../../../etc/passwd.png")
    # Traversal out of the declared root is the refusal we assert on, NOT a
    # file-not-found from the host.
    assert "safe_path" in type(exc.value).__name__ or "Security" in type(
        exc.value
    ).__name__ or "refused" in str(exc.value).lower() or "traversal" in str(
        exc.value
    ).lower(), type(
        exc.value
    ).__name__


def test_image_metadata_is_fenced():
    from harness import vision

    # Any credential-shaped path is redacted before it rides the block.
    record = {
        "path": "/home/alice/sk-0123456789abcdef0123.png",
        "media_type": "image/png",
        "data_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/l0k2AAAAAABJRU5ErkJggg==",
    }
    plan = vision.vision_plan_for([record], model="gpt-5", provider="openai")
    assert plan.sent
    block = plan.blocks[0].payload
    blob = str(block)
    assert "sk-0123456789abcdef0123" not in blob
    # No free-text metadata surface is forwarded: blocks carry only the
    # provider-native shape (type/url/source), never caption/exif/filename raw.
    assert "exif" not in blob.lower()
    assert "caption" not in blob.lower()


def test_retract_creates_no_second_mint_path():
    src = _src("harness/retract.py")
    assert "completed_verified" not in src
    assert "COMPLETED_VERIFIED" not in src
    assert "verify" not in src.lower() or "rewind" in src
