"""T1.W1.2 — the journal's redactor is bounded, and the regression is pinned.

`phases/DOCTRINE.md` §8 records the measured defect: `shared.security.redact_text`
was quadratic in a single-character run, `"y"*40000` hung the journal for
minutes, and it sat on the path of *every* journal write — so a minified asset
or a base64 blob in one tool result could wedge the whole system.
`harness/AGENTS.md` records the fix landing as 32 s -> 0.020 s.

A fix that is only recorded in prose is not a fix. This file is the evidence
that the fix is still present, that it is still REACHED from the harness's own
journal path (not merely importable), and that a pathological payload cannot
reach the authority at all because `harness/redaction.py` caps the input first.

**Budgets are generous on purpose.** These are regression pins, not benchmarks:
the numbers below are 15x-40x the measured cost on the development host, so a
host that is 3x slower than the one these were taken on still passes, while a
return to the quadratic shape (which was minutes, not seconds) fails by three
orders of magnitude. A perf pin that is tight enough to notice a slow host is
a flake generator and gets deleted by the next terminal.

Measured on the development host at the time of writing (recorded so a
regression can be told apart from a slower machine):

    redact_text("y" * 40000)                 0.025 s
    redact_text("a" * 200000)                0.136 s
    redact_text(("sk-" + "A"*39 + "\\n")*500) 0.012 s
    redact_text(1_000_000 chars of prose)     0.886 s
    redact_text("y" * 1_000_000)             0.817 s   <- linear, not quadratic

Host-only: no Docker, no provider, no network.
"""

from __future__ import annotations

import time

import pytest

from harness import redaction
from harness.agent_kernel.events import RunEventJournal
from harness.redaction import (
    JOURNAL_TEXT_CAP,
    redact_for_journal,
    redact_text_for_journal,
)
from harness.trace import TraceLogger

#: Generous budgets. See the module docstring for why they are this loose.
BUDGET_REPEATED_RUN_40K_S = 2.0
BUDGET_REPEATED_RUN_200K_S = 2.0
BUDGET_MANY_SECRETS_S = 2.0
BUDGET_ONE_MEGABYTE_S = 10.0
#: A journal write is a file append plus a redactor pass. Generous because the
#: assertion is "the write completes", not "the write is fast".
BUDGET_JOURNAL_WRITE_S = 5.0


def _elapsed(callable_, *args, **kwargs) -> tuple:
    start = time.perf_counter()
    value = callable_(*args, **kwargs)
    return value, time.perf_counter() - start


# ---------------------------------------------------------------------------
# 1. The fix is present in the authority
# ---------------------------------------------------------------------------


def test_the_quadratic_shape_has_not_returned_to_the_shared_authority() -> None:
    """`"y"*40000` must cost well under a budget, not minutes.

    This is the exact measurement `phases/DOCTRINE.md` §8 says hung the journal
    for minutes. Asserted against the authority directly, because a harness
    test that only measured its own boundary could not tell a fixed authority
    from a harness cap hiding an unfixed one.
    """
    from shared.security import redact_text

    _, elapsed = _elapsed(redact_text, "y" * 40_000)
    assert elapsed < BUDGET_REPEATED_RUN_40K_S, (
        f'redact_text("y"*40000) took {elapsed:.3f}s '
        f"(budget {BUDGET_REPEATED_RUN_40K_S}s). A per-character run that "
        f"scales quadratically again is the DOCTRINE.md §8 defect."
    )


def test_the_authority_is_linear_across_a_ten_fold_length_range() -> None:
    """Ten times the input must not cost anything like a hundred times the work.

    A quadratic redactor passes any single-size budget and fails only on the
    *shape*, so this asserts the ratio. The floor matters: a measurement too
    small to time reliably would make the ratio meaningless, so the input is
    big enough that the fixed per-call cost is not the dominant term.
    """
    from shared.security import redact_text

    small = "y" * 25_000
    large = small * 10
    _, small_s = _elapsed(redact_text, small)
    _, large_s = _elapsed(redact_text, large)
    # 10x input. Quadratic would be ~100x. 25x leaves room for a slow host and
    # for the authority's own span-chunking overhead, while still failing a
    # quadratic return by 4x.
    assert large_s < small_s * 25 + 0.5, (
        f"10x input cost {large_s / max(small_s, 1e-9):.1f}x the time "
        f"({small_s:.4f}s -> {large_s:.4f}s); quadratic would be ~100x."
    )


@pytest.mark.parametrize(
    ("label", "size", "budget"),
    [
        ("repeated-run-200k", 200_000, BUDGET_REPEATED_RUN_200K_S),
        ("many-secrets", 500, BUDGET_MANY_SECRETS_S),
        ("one-megabyte-prose", 1_000_000, BUDGET_ONE_MEGABYTE_S),
    ],
)
def test_the_three_recorded_measurements_stay_inside_budget(
    label: str, size: int, budget: float
) -> None:
    """The three measurements named in the round's brief, as a live assertion.

    The payload is BUILT here rather than parametrized: pytest puts the
    parameter values into the node id and exports that as
    ``PYTEST_CURRENT_TEST``, and Windows caps an environment variable at
    32767 characters — a 200 000-char parametrised payload makes the whole
    suite's setup ERROR rather than fail. Measured on the development host:
    0.136 s / 0.012 s / 0.886 s respectively.
    """
    from shared.security import redact_text

    if label == "many-secrets":
        payload = ("sk-" + "A" * 39 + "\n") * size
    elif label == "one-megabyte-prose":
        payload = "ordinary prose line\n" * (size // len("ordinary prose line\n"))
    else:
        payload = "a" * size

    cleaned, elapsed = _elapsed(redact_text, payload)
    assert isinstance(cleaned, str)
    assert elapsed < budget, (
        f"{label}: redact_text on {len(payload)} chars took {elapsed:.3f}s "
        f"(budget {budget}s)"
    )


# ---------------------------------------------------------------------------
# 2. The fix is REACHED from the harness journal path
# ---------------------------------------------------------------------------


def test_the_trace_journal_is_the_path_that_reaches_the_redactor(tmp_path) -> None:
    """A `tool_result` row must actually pass through the boundary.

    Non-vacuity matters here: a test that only asserted "no secret in the row"
    would be satisfied by a trace that never wrote the row at all. This asserts
    the row EXISTS, that it carries the redacted marker, and that the raw
    credential is absent from the FILE — not just from the parsed row.
    """
    secret = "sk-" + "A" * 39
    logger = TraceLogger(tmp_path / "t1w12")
    logger.log("tool_result", {"tool": "shell", "output": f"token={secret} leaked"})

    events = logger.read_all()
    assert len(events) == 1, "the row must exist; an absent row proves nothing"
    row = events[0]
    assert row["kind"] == "tool_result"
    rendered = str(row["data"])
    assert secret not in rendered, "the raw credential reached the trace row"
    assert "[REDACTED" in rendered, (
        "the row must carry the shared authority's placeholder, not merely "
        "have dropped the value - a missing value and a redacted one are "
        "different claims"
    )

    on_disk = (tmp_path / "t1w12" / "trace.jsonl").read_text(encoding="utf-8")
    assert secret not in on_disk, "the raw credential reached the trace FILE"


def test_the_event_journal_is_the_path_that_reaches_the_redactor(tmp_path) -> None:
    """The kernel's authoritative journal is the second boundary, and it works.

    `RunEventJournal.append` is what a `harness.agent_kernel` run writes, so a
    run whose only output is the typed-kernel path is covered here and not by
    the trace assertion above. Both assertions are needed: they are two
    different files written by two different objects, and a boundary wired into
    only one of them would satisfy either test alone.
    """
    secret = "sk-" + "B" * 39
    journal = RunEventJournal(
        tmp_path / "kernel" / "trace.jsonl", session_id="s1", run_id="r1"
    )
    journal.append("tool_result", {"output": f"echo {secret}"})

    rows = journal.to_records()
    assert len(rows) == 1
    assert secret not in str(rows[0])
    assert "[REDACTED" in str(rows[0])

    on_disk = (tmp_path / "kernel" / "trace.jsonl").read_text(encoding="utf-8")
    assert secret not in on_disk


def test_a_pathological_tool_result_cannot_reach_the_redactor_at_all(
    tmp_path,
) -> None:
    """The input cap fires BEFORE the authority, and the journal still writes.

    The brief asks for an input cap at the journal-write boundary so a
    pathological tool result cannot reach the redactor. Two properties are
    asserted, and both matter:
      * the stored value is bounded (so a minified asset cannot grow the
        journal without limit);
      * the journal row still EXISTS (so a cap cannot silently swallow the
        evidence of a run — the exact "a crash satisfies a negative
        assertion" trap the doctrine warns about).
    """
    payload = "y" * (JOURNAL_TEXT_CAP * 2)
    logger = TraceLogger(tmp_path / "cap")
    _, elapsed = _elapsed(logger.log, "tool_result", {"output": payload})

    events = logger.read_all()
    assert len(events) == 1, "a capped row must still be written"
    stored = str(events[0]["data"])
    assert len(stored) < len(payload), "the stored value must be bounded"
    assert "truncated" in stored, (
        "a bounded value must SAY it was bounded, and name the original "
        "length - a reader who cannot tell a capped value from a short one is "
        "being misled, which is the failure the cap exists to prevent"
    )
    assert elapsed < BUDGET_JOURNAL_WRITE_S, (
        f"a {len(payload)}-char tool result took {elapsed:.3f}s to journal"
    )


def test_the_cap_keeps_the_head_because_that_is_where_a_secret_prefix_is() -> None:
    """Capping must not discard the head.

    A cap that kept only the tail would be a cap that changed what the
    authority is able to find: a secret's prefix is at the start, and
    discarding the start would convert "a bounded value we redacted" into "a
    bounded value that may contain an unredacted secret". The brief's
    instruction was to bound the input, not to blind the redactor.
    """
    head = "prefix-that-must-survive-"
    payload = head + "y" * (JOURNAL_TEXT_CAP + 5_000)
    capped = redact_text_for_journal(payload, cap=JOURNAL_TEXT_CAP)
    assert capped.startswith(head)
    assert "journal value truncated" in capped


def test_the_cap_can_be_switched_off_and_says_so() -> None:
    """`cap <= 0` means "no cap", and must be expressible.

    Same reason `cap_tool_output` documents its own `0`: a bound that cannot be
    lifted is not a bound, it is a policy. A caller diagnosing a false positive
    needs to be able to remove this one and see what the authority says.
    """
    payload = "y" * 50_000
    uncapped = redact_text_for_journal(payload, cap=0)
    assert "truncated" not in uncapped
    assert len(uncapped) >= 50_000 - len("[REDACTED_SECRET]")


def test_the_counter_reports_the_cap_and_not_only_the_redaction() -> None:
    """The boundary reports what it DID, so "we redacted everything" is a claim.

    A boundary that silently passed raw text would report the same numbers as
    one that redacted, which is why the counters exist and why this asserts a
    cap actually moved one.
    """
    redaction.reset_journal_redaction_report()
    redact_text_for_journal("y" * (JOURNAL_TEXT_CAP + 1_000))
    report = redaction.journal_redaction_report().as_dict()
    assert report["strings_capped"] == 1, (
        "a capped value must be counted; a boundary that caps silently cannot "
        "be distinguished from one that does not cap"
    )
    assert report["characters_withheld"] >= 1_000
    redaction.reset_journal_redaction_report()


def test_a_harmless_value_is_untouched_so_the_perf_pin_is_not_the_only_signal() -> None:
    """Redaction must be a no-op on ordinary content.

    Every existing suite asserts exact detail strings, exact receipts and
    byte-exact diffs. If this boundary changed ordinary values, this round
    would break dozens of tests for no security benefit - and, worse, a
    redactor that rewrites everything would make a leaked secret hard to spot
    in a diff.
    """
    ordinary = "No such file or directory: /tmp/example.txt (exit 1)"
    assert redact_text_for_journal(ordinary) == ordinary
    payload = {"file": "app.py", "line": 12, "kind": "syntax_error"}
    assert redact_for_journal(payload) == payload
