"""Tests for the Round-8 context-engineering additions in
harness.retrieval: size_context_budget (dynamic budget allocation from
real task signals — Task A) and rerank_files (sub-step relevance
re-ranking — Task B).
"""

import json
from pathlib import Path

from harness.retrieval import rerank_files, size_context_budget


# ---------------------------------------------------------------------------
# size_context_budget (Task A)
# ---------------------------------------------------------------------------

LONG_ISSUE = (
    "Since the 2.3 release, orders with more than 50 line items are "
    "silently truncated when exported through the report API. "
    "Reproduction: create an order via POST /orders with 60 items, then "
    "GET /orders/{id}/export — the CSV contains only the first 50 rows "
    "and the total row shows the truncated subtotal. We first noticed in "
    "production (see incident 4711) but it reproduces on a clean checkout "
    "with pytest tests/test_export.py::test_many_line_items. Suspected "
    "causes: a pagination default leaking into the export path, or the "
    "report generator reusing the list view's slicing. Please also check "
    "whether the streaming exporter shares the same cap, and fix the "
    "underlying truncation rather than raising the page size."
)


def _write_repo(root: Path, n_files: int) -> Path:
    pkg = root / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    for i in range(n_files):
        (pkg / f"mod_{i:03d}.py").write_text(
            f"def fn_{i}(x):\n    return x + {i}\n", encoding="utf-8"
        )
    return root


def test_budget_shape_and_signals_reported(tmp_path):
    root = _write_repo(tmp_path, 40)
    budget = size_context_budget("the pop method is broken", str(root))
    assert set(budget.keys()) == {"max_files", "max_lines", "budget_signals"}
    sig = budget["budget_signals"]
    assert sig["issue_chars"] == len("the pop method is broken")
    assert sig["repo_files"] == 40
    assert sig["files_touched"] == 0
    assert isinstance(budget["max_files"], int) and budget["max_files"] >= 2
    assert isinstance(budget["max_lines"], int) and budget["max_lines"] >= 30


def test_simple_task_gets_smaller_budget_than_complex(tmp_path):
    """THE Task-A scenario: short issue + tiny repo (simple) vs long issue
    + large repo (complex) — the complex task must pull more context."""
    small_repo = _write_repo(tmp_path / "small", 10)
    big_repo = _write_repo(tmp_path / "big", 450)

    simple = size_context_budget("pop is broken", str(small_repo))
    complex_ = size_context_budget(LONG_ISSUE, str(big_repo))
    assert complex_["max_files"] > simple["max_files"]
    assert complex_["max_lines"] > simple["max_lines"]


def test_budget_grows_with_files_already_touched(tmp_path):
    """Mid-task signal: an agent 3 files deep needs MORE context, not
    less (more surface in play must stay coherent)."""
    root = _write_repo(tmp_path, 100)
    early = size_context_budget(
        "medium issue about exporting orders", str(root), files_touched=[]
    )
    late = size_context_budget(
        "medium issue about exporting orders",
        str(root),
        files_touched=["a.py", "b.py", "c.py", "d.py"],
    )
    assert late["max_files"] >= early["max_files"]
    assert late["max_lines"] >= early["max_lines"]
    assert late["budget_signals"]["files_touched"] == 4


def test_budget_clamped_to_absolute_bounds(tmp_path):
    root = _write_repo(tmp_path, 2)  # tiny repo, one-line issue
    tiny = size_context_budget("x", str(root), files_touched=[])
    huge = size_context_budget(
        LONG_ISSUE * 3, str(root), files_touched=[f"f{i}.py" for i in range(12)]
    )
    assert 2 <= tiny["max_files"] <= 8
    assert 30 <= tiny["max_lines"] <= 120
    assert 2 <= huge["max_files"] <= 8
    assert 30 <= huge["max_lines"] <= 120


def test_budget_never_raises_on_bad_repo_path(tmp_path):
    # a budget signal must never kill retrieval
    budget = size_context_budget("issue", str(tmp_path / "does-not-exist"))
    assert budget["budget_signals"]["repo_files"] == 0
    assert budget["max_files"] >= 2


def test_budget_respects_explicit_repo_file_count(tmp_path):
    """Callers may precompute the count (run_task passes it once); the
    function must not re-walk the repo when given it."""
    root = _write_repo(tmp_path, 30)
    b1 = size_context_budget("issue text here", str(root), repo_file_count=123)
    assert b1["budget_signals"]["repo_files"] == 123


def test_budget_linear_between_thresholds(tmp_path):
    root = _write_repo(tmp_path, 200)  # mid-sized repo
    mid = size_context_budget(
        "a medium-length issue text, not one line, not a multi-paragraph report",
        str(root),
    )
    base = size_context_budget(
        "a medium-length issue text, not one line, not a multi-paragraph report",
        str(root),
        base_files=4,
        base_lines=60,
    )
    # mid-band signal: adjustment strictly between the extremes
    assert -1.0 < mid["budget_signals"]["adjustment"] < 1.0
    assert mid["max_files"] == base["max_files"]  # same inputs, same caps


# ---------------------------------------------------------------------------
# rerank_files (Task B)
# ---------------------------------------------------------------------------


def _repo_with_two_modules(root: Path) -> Path:
    (root / "exporter").mkdir(parents=True)
    (root / "exporter" / "csv_export.py").write_text(
        "def export_orders(orders):\n    return [row for row in orders[:50]]\n",
        encoding="utf-8",
    )
    (root / "exporter" / "email_send.py").write_text(
        "def send_mail(msg):\n    print('sent', msg)\n", encoding="utf-8"
    )
    return root


def test_rerank_promotes_step_relevant_file(tmp_path):
    """THE Task-B scenario: a later sub-step about EMAIL delivery must
    re-rank email_send.py ABOVE csv_export.py, regardless of the order
    the issue-level retrieval returned them in."""
    root = _repo_with_two_modules(tmp_path)
    step = {
        "id": 3,
        "description": "fix the email delivery path so messages send",
        "checkpoint": "email send is called once per notification",
        "files_hint": [],
    }
    out = rerank_files(
        str(root), ["exporter/csv_export.py", "exporter/email_send.py"], step
    )
    assert out[0] == "exporter/email_send.py"
    assert set(out) == {"exporter/csv_export.py", "exporter/email_send.py"}


def test_rerank_files_hint_is_strongest_signal(tmp_path):
    root = _repo_with_two_modules(tmp_path)
    step = {
        "id": 1,
        "description": "inspect the exporter",
        "checkpoint": "understand the flow",
        "files_hint": ["exporter/csv_export.py"],
    }
    out = rerank_files(
        str(root), ["exporter/email_send.py", "exporter/csv_export.py"], step
    )
    assert out[0] == "exporter/csv_export.py"


def test_rerank_never_drops_candidates(tmp_path):
    """Re-ranking narrows FOCUS, it must not drop the safety net retrieval
    found: zero-scoring files keep their original relative order at the
    tail; nothing disappears."""
    root = _repo_with_two_modules(tmp_path)
    (root / "exporter" / "unrelated.py").write_text(
        "def nothing():\n    pass\n", encoding="utf-8"
    )
    step = {
        "id": 2,
        "description": "send the email",
        "checkpoint": "email sent",
        "files_hint": [],
    }
    candidates = [
        "exporter/unrelated.py",
        "exporter/email_send.py",
        "exporter/csv_export.py",
    ]
    out = rerank_files(str(root), candidates, step)
    assert set(out) == set(candidates)
    assert out[0] == "exporter/email_send.py"


def test_rerank_limit_truncates_after_reordering(tmp_path):
    root = _repo_with_two_modules(tmp_path)
    step = {
        "id": 1,
        "description": "fix the email sending",
        "checkpoint": "email goes out",
        "files_hint": [],
    }
    out = rerank_files(
        str(root), ["exporter/csv_export.py", "exporter/email_send.py"], step, limit=1
    )
    assert out == ["exporter/email_send.py"]


def test_rerank_empty_candidates_and_step_without_terms(tmp_path):
    root = _repo_with_two_modules(tmp_path)
    assert rerank_files(str(root), [], {"id": 1, "description": "x"}) == []
    # a step naming nothing: original order preserved (stable, no drops)
    out = rerank_files(
        str(root),
        ["exporter/csv_export.py", "exporter/email_send.py"],
        {
            "id": 1,
            "description": "do the thing",
            "checkpoint": "it works",
            "files_hint": [],
        },
    )
    assert out == ["exporter/csv_export.py", "exporter/email_send.py"]


def test_rerank_tolerates_missing_files(tmp_path):
    """Unreadable/missing files score their PATH terms only — never a
    crash (re-ranking must not kill a step)."""
    step = {
        "id": 1,
        "description": "fix the email sending",
        "checkpoint": "sent",
        "files_hint": [],
    }
    out = rerank_files(str(tmp_path), ["gone/email_send.py", "also/gone.py"], step)
    assert out == ["gone/email_send.py", "also/gone.py"]


def test_rerank_uses_content_terms_not_just_paths(tmp_path):
    """A step whose description mentions 'orders' must promote the file
    that CONTAINS the orders logic, even with an unrelated path."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod_a.py").write_text(
        "def export_orders():\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "mod_b.py").write_text(
        "def calendar():\n    pass\n", encoding="utf-8"
    )
    step = {
        "id": 1,
        "description": "fix orders export truncation",
        "checkpoint": "orders export complete",
        "files_hint": [],
    }
    out = rerank_files(str(tmp_path), ["pkg/mod_b.py", "pkg/mod_a.py"], step)
    assert out[0] == "pkg/mod_a.py"
