"""The eleven surfaces Phase 3a added, and the standing test that they tell the truth.

T4.W2.5. Every surface this phase added gets the same treatment: it renders
something non-empty, and it renders **the truth**. This module is that test, in
the product rather than only in a suite, for the reason
``cli/test_render_path_pin.py`` records: a gate its own module's owner cannot
edit without a cross-terminal request is a gate that rots.

**The eleven, declared rather than discovered.**

============================  ==================================================
surface                       the edge case it must survive
============================  ==================================================
``inbox``                     no inbox at all, vs an inbox that exists and is empty
``lineage``                   no parent, a cycle, an unreadable ancestor
``tree``                      a leaf-only conversation, no tree on disk
``memory_list``               an unreadable store, an empty store
``memory_detail``             **a source session that has been DELETED**
``branch_summary``            a tiny branch, a summary with no evidence
``clearing_receipt``          nothing measured; clearing switched off
``compaction_receipt``        nothing measured; a compaction with no turns
``fork_disclosure``           no isolation mechanism at all
``merge_flow``                a branch that diverged
``prune_notice``              nothing collectable
============================  ==================================================

**Why "non-empty" is only half the assertion.** A surface that renders nothing
is an absent affordance, which this repo treats as a defect. But a surface that
renders *something wrong* is worse: ``memory_detail`` on a memory whose source
session was deleted is the case the brief names, and the failure mode is not a
blank panel - it is a confident, well-formatted citation link to a session that
is not there. So each surface declares what it must SAY, and this module
checks that it said it.

**No surface renders a traceback unless it is one.** Every receipt here is a
list of plain strings produced by a renderer that is handed untrusted content,
and the scan is over the RENDERED text rather than over the return value,
because a renderer that raises while formatting is exactly the case a
return-value check misses.

**Memory content is sanitised.** ``memory_detail`` and ``memory_list`` carry
text extracted from sessions that may contain attacker-influenced content. This
is a NEW injection surface - the text was authored elsewhere and is rendered
here - and :func:`assert_memory_content_is_sanitised` drives the real renderer
with a credential-shaped payload and requires it to come back redacted.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

__all__ = [
    "SURFACES",
    "TRACEBACK_MARKERS",
    "assert_every_surface_renders",
    "assert_memory_content_is_sanitised",
    "assert_no_traceback_unless_it_is_one",
    "surface_report",
    "surface_rows",
]

#: The eleven surfaces, in the order the brief lists them. Declared as a tuple
#: so a test can pin the SET: a surface added without a row here is invisible,
#: and a row here for a surface nobody renders is a check that measures nothing.
SURFACES: Tuple[str, ...] = (
    "inbox",
    "lineage",
    "tree",
    "memory_list",
    "memory_detail",
    "branch_summary",
    "clearing_receipt",
    "compaction_receipt",
    "fork_disclosure",
    "merge_flow",
    "prune_notice",
)

#: What each surface rendered on its edge case, and what it MUST have said.
#:
#: The right-hand column is the load-bearing half. ``must_say`` is a list of
#: SUBSTRINGS that have to appear in the rendered text, chosen so that the only
#: way to satisfy them is to actually answer the question - not by rendering
#: nothing (the non-empty check catches that) and not by rendering a confident
#: wrong answer (the substring check catches that).
SURFACE_EXPECTATIONS: Dict[str, Dict[str, Any]] = {
    "inbox": {
        "edge_case": "no inbox directory exists for this repository",
        "must_say": ("no .inbox/ directory", "never run"),
        "must_not_say": ("0 pending",),
    },
    "lineage": {
        "edge_case": "a conversation with no readable session record",
        "must_say": ("could not be read",),
    },
    "tree": {
        "edge_case": "a leaf-only conversation that has never been branched",
        "must_say": ("never been branched", "no tree yet"),
    },
    "memory_list": {
        "edge_case": "an unreadable memory store",
        "must_say": ("unavailable",),
        "must_not_say": ("0 applied",),
    },
    "memory_detail": {
        "edge_case": "a memory whose source session has been DELETED",
        "must_say": ("DANGLING", "no longer exists"),
        "must_not_say": ("open it with",),
    },
    "branch_summary": {
        "edge_case": "a summary for a tiny branch, with no evidence",
        "must_say": ("GENERATED", "NOT a transcript record", "still on disk"),
    },
    "clearing_receipt": {
        "edge_case": "nothing measured, and clearing switched off",
        "must_say": ("not measured",),
    },
    "compaction_receipt": {
        "edge_case": "nothing measured",
        "must_say": ("not measured",),
    },
    "fork_disclosure": {
        "edge_case": "no isolation mechanism available at all",
        "must_say": ("WORKING TREE",),
    },
    "merge_flow": {
        "edge_case": "a branch that diverged from its parent",
        "must_say": ("REFUSED", "diverged", "nothing was written"),
    },
    "prune_notice": {
        "edge_case": "nothing is collectable",
        "must_say": ("DRY RUN", "nothing was collected"),
    },
}

#: Substrings that mean a Python traceback reached a surface. Scanned over
#: RENDERED text, and each one is a shape this repo has recorded leaking
#: through a renderer before.
TRACEBACK_MARKERS: Tuple[str, ...] = (
    "Traceback (most recent call last)",
    'File "',
    ".py\", line ",
    "  File ",
)


# ---------------------------------------------------------------------------
# The edge-case fixtures. Each builds the SMALLEST state that exercises one
# surface's hard case, so a failure names a surface rather than a fixture.
# ---------------------------------------------------------------------------


def _scratch() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory()


def _fixture_inbox(root: Path) -> Dict[str, Any]:
    from cli import memory_ui as _memory

    status = _memory.inbox_status(root / "no-such-repository")
    return {"available": True, "lines": _memory.status_lines(status), "status": status}


def _fixture_lineage(log_root: Path) -> Dict[str, Any]:
    from cli import lineage as _lineage

    # A session id that is well-formed but names nothing on disk, so the
    # ancestry walk has to report an unreadable ancestor rather than an empty
    # lineage. The rendered LINES are what this surface owes a reader, so the
    # fixture renders them rather than returning the projection.
    rows = _lineage.lineage_rows(log_root, "sess-00000000")
    lines = _lineage.lineage_lines(rows)
    report = _lineage.rotation_events(log_root, "sess-00000000")
    if report.get("reason"):
        lines.append("  " + report["reason"])
    return {"available": True, "lines": lines, "rows": rows, "rotation": report}


def _fixture_tree(log_root: Path) -> Dict[str, Any]:
    from cli import lineage as _lineage

    snapshot = _lineage.tree_snapshot(log_root, "sess-00000000")
    return {"available": True, "lines": _lineage.tree_lines(snapshot), "tree": snapshot}


def _fixture_memory_list() -> Dict[str, Any]:
    from cli import memory_ui as _memory

    ranked = _memory.ranked_memories(None, store=_BrokenStore())
    return {"available": True, "lines": _memory.ranked_memory_lines(ranked), "ranked": ranked}


def _fixture_memory_detail(log_root: Path) -> Dict[str, Any]:
    from cli import memory_ui as _memory

    row = {
        "id": 4242,
        "text": "the deploy key lives in the vault, not in the repo",
        "source": "session",
        "provenance": {
            "session_id": "sess-deadbeef",
            "turn": 3,
            "source_type": "extracted_session",
        },
    }
    return {
        "available": True,
        "lines": _memory.memory_detail_lines(row, log_root=log_root),
        "row": row,
    }


def _fixture_branch_summary() -> Dict[str, Any]:
    from cli import lineage as _lineage

    return {
        "available": True,
        "lines": _lineage.branch_summary_lines(
            "tried an alternative and stopped",
            branch_session_id="e-branch-abc123",
            generated_by="the model that abandoned the branch",
        ),
    }


def _fixture_clearing() -> Dict[str, Any]:
    from cli import context_budget as _budget

    empty = _budget.budget_projection()
    off = _budget.budget_projection(
        meter={"rewinds": [{"tokens_reclaimed": 900, "messages": 4}]},
        config={"clearing_enabled": "off"},
    )
    return {
        "available": True,
        "lines": _budget.clearing_receipt(empty)
        + _budget.clearing_receipt(off),
    }


def _fixture_compaction() -> Dict[str, Any]:
    from cli import context_budget as _budget

    return {
        "available": True,
        "lines": _budget.compaction_receipt(_budget.budget_projection()),
    }


def _fixture_fork_disclosure() -> Dict[str, Any]:
    from cli import lineage as _lineage

    return {
        "available": True,
        "lines": _lineage.fork_isolation_lines(
            isolation="shared",
            parent_session_id="sess-parent1",
            child_session_id="sess-child01",
            parent_tree="C:/repo",
        ),
    }


def _fixture_merge_flow(log_root: Path) -> Dict[str, Any]:
    """A branch that genuinely DIVERGED, so the merge refusal is reached.

    ``SessionTree.fork`` takes no ``evidence``, so a branch minted by the
    normal path has none and can never be divergent under
    ``_do_merge``'s rule. The fixture therefore appends the branch entry
    directly with evidence naming an entry that is NOT its parent - which is
    exactly the shape a producer that tracked branch provenance would write,
    and is the only way to reach the refusal this surface exists to prove.
    """
    from cli import lineage as _lineage

    tree = _lineage._session_tree(log_root, "sess-merge01")
    if tree is None:
        return {"available": False, "lines": ["merge    unavailable"], "branch_id": ""}
    root_turn = tree.advance(turn_id="turn-1", text="the original line of work")
    root_id = str(getattr(root_turn, "id", "") or "")
    branch = tree.append(
        "branch",
        text="the other approach",
        parent_id=str(getattr(root_turn, "parent_id", "") or ""),
        evidence=("e-turn-somethingelse",),
    )
    branch_id = str(getattr(branch, "id", "") or "")
    # A `branch` entry is itself a head candidate in T3's vocabulary, so
    # appending one LEAVES THE HEAD ON THE BRANCH - and `_do_merge` correctly
    # refuses to merge the head into its own parent. Advancing a turn moves the
    # head back onto the main line, which is the state a real branch is in
    # after `fork()` (that anchors the new branch UNDER the fork point's
    # parent, deliberately, so the fork point stays the head).
    tree.advance(turn_id="turn-2", text="the conversation carried on")
    return {
        "available": True,
        "lines": _lineage._do_merge(
            branch_id, repo=None, log_root=log_root, session_id="sess-merge01"
        )["lines"],
        "branch_id": branch_id,
        "root_id": root_id,
    }


def _fixture_prune(log_root: Path) -> Dict[str, Any]:
    from cli import lineage as _lineage

    tree = _lineage._session_tree(log_root, "sess-prune01")
    if tree is not None:
        tree.advance(turn_id="turn-1", text="something to retain")
    return {
        "available": True,
        "lines": _lineage.tree_command(
            "prune", "", log_root=log_root, session_id="sess-prune01"
        )["lines"],
    }


class _BrokenStore:
    """A store handle whose every read raises, for the unavailable memory list.

    Not a mock for its own sake: it is how the "the memory store is unreachable"
    path is reached at all, and that path is one of the eleven edges because a
    renderer that reports an empty list over an unreadable store is telling a
    user their memory is under control when nothing has said so.
    """

    def search(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the store is unreachable")


# ---------------------------------------------------------------------------
# The driver.
# ---------------------------------------------------------------------------


def surface_rows(
    *, log_root: Any = None, repo_path: Any = None, memory_row: Any = None
) -> List[Dict[str, Any]]:
    """Drive all eleven surfaces on their edge case and report what each said.

    Each row is ``{surface, edge_case, lines, non_empty, said, missing,
    forbidden, available}``. ``said`` is the joined rendered text, so a caller
    (or a person reading this receipt) can read what a surface actually said
    without re-running it.

    ``memory_row`` overrides the deleted-source fixture, which is what lets a
    test drive the SAME surface with a resolvable citation and assert the two
    render differently rather than asserting two unrelated code paths.
    """
    scratch = _scratch()
    try:
        root = Path(log_root) if log_root is not None else Path(scratch.name)
        fixtures: List[Tuple[str, Dict[str, Any]]] = [
            ("inbox", _fixture_inbox(root)),
            ("lineage", _fixture_lineage(root)),
            ("tree", _fixture_tree(root)),
            ("memory_list", _fixture_memory_list()),
            (
                "memory_detail",
                _fixture_memory_detail(root)
                if memory_row is None
                else {
                    "available": True,
                    "lines": _memory_detail_for(memory_row, root),
                    "row": memory_row,
                },
            ),
            ("branch_summary", _fixture_branch_summary()),
            ("clearing_receipt", _fixture_clearing()),
            ("compaction_receipt", _fixture_compaction()),
            ("fork_disclosure", _fixture_fork_disclosure()),
            ("merge_flow", _fixture_merge_flow(root)),
            ("prune_notice", _fixture_prune(root)),
        ]
    finally:
        # The temporary directory is released after the FIXTURES are built,
        # because the rendered lines are what this function returns. Nothing
        # below re-reads the filesystem.
        pass
    try:
        scratch.cleanup()
    except OSError:
        pass

    rows: List[Dict[str, Any]] = []
    for name, fixture in fixtures:
        expectation = SURFACE_EXPECTATIONS.get(name, {})
        lines = [str(item) for item in (fixture.get("lines") or [])]
        said = "\n".join(lines)
        missing = [
            needle
            for needle in expectation.get("must_say", ())
            if needle not in said
        ]
        forbidden = [
            needle
            for needle in expectation.get("must_not_say", ())
            if needle in said
        ]
        rows.append(
            {
                "surface": name,
                "edge_case": expectation.get("edge_case", ""),
                "lines": lines,
                "non_empty": bool([item for item in lines if item.strip()]),
                "said": said,
                "missing": missing,
                "forbidden": forbidden,
                "available": bool(fixture.get("available", True)),
            }
        )
    return rows


def _memory_detail_for(row: Mapping[str, Any], log_root: Any) -> List[str]:
    from cli import memory_ui as _memory

    return _memory.memory_detail_lines(row, log_root=log_root)


def surface_report(
    *, log_root: Any = None, repo_path: Any = None, memory_row: Any = None
) -> Dict[str, Any]:
    """The whole standing test as one receipt.

    ``clean`` is the AND of every row's three properties, and it is reported
    with the FAILING rows named rather than a bare ``False``, because "the
    surfaces are not clean" is not an actionable statement.
    """
    rows = surface_rows(log_root=log_root, repo_path=repo_path, memory_row=memory_row)
    problems: List[str] = []
    for row in rows:
        if not row["non_empty"]:
            problems.append(
                f"{row['surface']}: rendered nothing for {row['edge_case']!r}"
            )
        for needle in row["missing"]:
            problems.append(
                f"{row['surface']}: did not say {needle!r} for {row['edge_case']!r}"
            )
        for needle in row["forbidden"]:
            problems.append(
                f"{row['surface']}: said {needle!r}, which is the false answer "
                f"for {row['edge_case']!r}"
            )
        leaked = [mark for mark in TRACEBACK_MARKERS if mark in row["said"]]
        for mark in leaked:
            problems.append(f"{row['surface']}: rendered a traceback marker {mark!r}")
    rendered = [name for name in SURFACES if any(r["surface"] == name for r in rows)]
    missing_surfaces = [name for name in SURFACES if name not in rendered]
    for name in missing_surfaces:
        problems.append(f"{name}: declared in SURFACES but never rendered")
    return {
        "surfaces": len(SURFACES),
        "rendered": len(rendered),
        "rows": rows,
        "problems": problems,
        "clean": not problems,
    }


# ---------------------------------------------------------------------------
# The gates.
# ---------------------------------------------------------------------------


def assert_no_traceback_unless_it_is_one(
    lines: Sequence[str], *, is_traceback: bool = False
) -> Tuple[bool, str]:
    """No rendered line carries a Python traceback marker unless it IS one.

    ``is_traceback`` is the caller's claim about what it handed the renderer.
    A renderer given a real exception may print one; a renderer given a memory
    row may not, and this is the check that keeps the difference from being
    decided by which branch of the code happened to run.
    """
    text = "\n".join(str(item) for item in (lines or ()))
    found = [mark for mark in TRACEBACK_MARKERS if mark in text]
    if found and not is_traceback:
        return False, (
            "a surface rendered a Python traceback marker "
            f"({', '.join(repr(mark) for mark in found)}) for content that is "
            "not a traceback"
        )
    return True, ""


def assert_memory_content_is_sanitised() -> Tuple[bool, str]:
    """The sanitiser is asserted on MEMORY content, not merely on diffs.

    Memory text is extracted from sessions, and a session can contain anything
    a repository or an issue carried - including a credential-shaped string. The
    diff surfaces have carried this assertion since P0; memory did not, and
    memory content is the newer injection surface.

    The payload is a hardcoded fake shape and nothing is inspected, printed or
    retained beyond this function's return value. The check drives the REAL
    renderer rather than asserting that ``sanitize_text`` works, because a gate
    that tests the sanitiser in isolation proves the sanitiser and not the
    surface that uses it.
    """
    payload = 'key=sk-FAKE-SECRET-0123456789 the deploy hook lives here'
    scratch = _scratch()
    try:
        root = Path(scratch.name)
        from cli import memory_ui as _memory

        row = {
            "id": 1,
            "text": payload,
            "source": "session",
            "provenance": {"source_type": "extracted_session"},
        }
        detail = _memory.memory_detail_lines(row, log_root=root)
        joined = "\n".join(detail)
        if "sk-FAKE-SECRET-0123456789" in joined:
            return False, (
                "the memory detail surface rendered a credential-shaped payload "
                "verbatim; memory content is a sanitiser surface like any other"
            )
        ok, why = assert_no_traceback_unless_it_is_one(detail)
        if not ok:
            return False, why

        # And the LIST surface, which renders the same text through a different
        # function. One assertion on one of them would leave the other open.
        ranked = {
            "available": True,
            "rows": [
                {
                    "id": 1,
                    "text": payload,
                    "marker": "!",
                    "untrusted": True,
                    "trust": {"class": "untrusted", "reason": "from an issue body"},
                    "ranking": {"explained": False, "reason": "not ranked"},
                }
            ],
            "considered": 1,
            "usage_applied": False,
            "usage_reason": "no usage telemetry",
            "dropped_below_floor": 0,
        }
        listing = _memory.ranked_memory_lines(ranked)
        if "sk-FAKE-SECRET-0123456789" in "\n".join(listing):
            return False, (
                "the ranked memory list rendered a credential-shaped payload "
                "verbatim; the list is a separate renderer from the detail view"
            )
        ok, why = assert_no_traceback_unless_it_is_one(listing)
        if not ok:
            return False, why
        return True, ""
    finally:
        try:
            scratch.cleanup()
        except OSError:
            pass


def assert_every_surface_renders() -> Tuple[bool, str]:
    """The standing test, as a gate the product itself runs.

    Self-checks by driving :func:`surface_report` over all eleven surfaces. It
    fails when a surface renders nothing, when it fails to say the thing its
    edge case requires, when it says the FALSE thing, or when a traceback
    reaches it - and it names which surface, because "the surfaces are not
    clean" sends a reader looking everywhere.
    """
    report = surface_report()
    if report["clean"]:
        return True, ""
    return False, "; ".join(report["problems"][:6])
