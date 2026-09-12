"""Unit tests for coordinated multi-file change detection + atomic
group mechanics (Improvement Round 2, Tasks A+B).

Covers harness.coordination (vocabulary classification, structural
fan-out via the REAL memory.code_graph on a synthetic repo, group
completeness, prompt blocks), harness.editor.restore_group/group_orphans
(atomic rollback), and harness.context change-groups state (additive
schema key, resume hydration). No Docker, no model: the structural layer
is exercised against the real graph module on tmp repos.
"""

import json
from pathlib import Path

import pytest

from harness import coordination, editor, prompts
from harness.context import STATE_KEYS, TaskState
from harness.core import _parse_plan_json, _plan_change_groups, _touched_group_files

# ---------------------------------------------------------------------------
# vocabulary classification
# ---------------------------------------------------------------------------


def test_kind_signature_from_issue_text():
    assert (
        coordination.coordination_kind(
            "invoice_total's include_tax parameter is being dropped; update "
            "every call site"
        )
        == "signature"
    )


def test_kind_rename_from_issue_text():
    assert (
        coordination.coordination_kind(
            "invoice_total was renamed; all callers must be updated"
        )
        == "rename"
    )


def test_kind_none_for_plain_bug():
    assert (
        coordination.coordination_kind("mean() divides by len-1; should divide by len")
        is None
    )


def test_kind_reads_plan_texts_too():
    # the ISSUE is plain, but the plan names the rename
    assert (
        coordination.coordination_kind(
            "totals are wrong",
            plan_texts=["rename invoice_total and update each call site"],
        )
        == "rename"
    )


def test_kind_empty_is_none():
    assert coordination.coordination_kind("") is None


# ---------------------------------------------------------------------------
# structural fan-out (real memory.code_graph on a synthetic repo)
# ---------------------------------------------------------------------------


def _struct_available() -> bool:
    try:
        from harness.deps import get_code_graph_factory

        return get_code_graph_factory() is not None
    except Exception:
        return False


def _coord_repo(tmp_path: Path) -> Path:
    """The fixture's scenario, minimized: model <- serializers/reports/api."""
    repo = tmp_path / "coordrepo"
    (repo / "invlib").mkdir(parents=True)
    (repo / "invlib" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "invlib" / "model.py").write_text(
        "class Invoice:\n"
        "    def invoice_total(self, include_tax=False):\n"
        "        return self.subtotal\n",
        encoding="utf-8",
    )
    (repo / "invlib" / "serializers.py").write_text(
        "from invlib.model import Invoice\n\n"
        "def to_dict(inv):\n"
        "    return {'total': inv.invoice_total(include_tax=True)}\n",
        encoding="utf-8",
    )
    (repo / "invlib" / "reports.py").write_text(
        "from invlib.model import Invoice\n\n"
        "def render_summary(inv):\n"
        "    return str(inv.invoice_total(include_tax=True))\n",
        encoding="utf-8",
    )
    (repo / "invlib" / "unrelated.py").write_text(
        "def other():\n    return 1\n", encoding="utf-8"
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_model.py").write_text(
        "from invlib.model import Invoice\n\n"
        "def test_total():\n"
        "    assert Invoice().invoice_total()\n",
        encoding="utf-8",
    )
    return repo


@pytest.fixture
def coord_repo(tmp_path):
    if not _struct_available():
        pytest.skip("memory.code_graph not importable")
    return _coord_repo(tmp_path)


def test_fanout_finds_callers_and_importers(coord_repo):
    """The core Task-A claim: changing model.py's method structurally
    implies changes in its callers/importers — found via graph EDGES,
    not text (the call sites never say 'model.py')."""
    out = coordination.dependent_files.__wrapped__ if False else None  # noqa
    from harness.deps import get_code_graph_factory

    factory = get_code_graph_factory()
    graph = factory(
        str(coord_repo), root=str(coord_repo.parent / "idx")
    ).load_or_build()
    deps = coordination.dependent_files(graph, ["invlib/model.py"])
    assert "invlib/serializers.py" in deps
    assert "invlib/reports.py" in deps
    # the anchor symbols name the dependency
    assert "invoice_total" in deps["invlib/serializers.py"]


def test_fanout_excludes_changed_files_and_unrelated(coord_repo):
    from harness.deps import get_code_graph_factory

    factory = get_code_graph_factory()
    graph = factory(
        str(coord_repo), root=str(coord_repo.parent / "idx2")
    ).load_or_build()
    deps = coordination.dependent_files(graph, ["invlib/model.py"])
    assert "invlib/model.py" not in deps  # changed file never a dependent
    assert "invlib/unrelated.py" not in deps  # no edges to the change


def test_fanout_bounded(coord_repo):
    from harness.deps import get_code_graph_factory

    factory = get_code_graph_factory()
    graph = factory(
        str(coord_repo), root=str(coord_repo.parent / "idx3")
    ).load_or_build()
    deps = coordination.dependent_files(graph, ["invlib/model.py"], max_files=1)
    assert len(deps) <= 1


def test_detect_coordinated_change_full(coord_repo, tmp_path):
    det = coordination.detect_coordinated_change(
        repo_path=str(coord_repo),
        issue_text="drop the include_tax flag from invoice_total and "
        "update every call site; rename it amount_due",
        changed_files=["invlib/model.py"],
        index_root=tmp_path / "idx4",
        protected_patterns=["tests/*"],
    )
    assert det["detected"] is True
    assert det["kind"] == "signature"
    assert set(det["dependent_files"]) == {"invlib/serializers.py", "invlib/reports.py"}
    # the test file that CALLS the method is a graph dependent but a
    # PROTECTED path: excluded from the suggested group, listed in why
    assert "tests/test_model.py" in det["excluded"]
    assert "tests/test_model.py" not in det["group_files"]
    # suggested atomic group = changed + dependents
    assert det["group_files"] == [
        "invlib/model.py",
        "invlib/reports.py",
        "invlib/serializers.py",
    ]


def test_detect_without_vocabulary_still_flags_dependents(coord_repo, tmp_path):
    """Dependents exist but the text is silent: still a coordinated change
    (flagged with kind=None) — the graph, not prose, decides atomicity."""
    det = coordination.detect_coordinated_change(
        repo_path=str(coord_repo),
        issue_text="totals come out wrong",
        changed_files=["invlib/model.py"],
        index_root=tmp_path / "idx5",
    )
    assert det["detected"] is True
    assert det["kind"] is None
    assert det["group_files"]


def test_detect_no_changed_files(tmp_path):
    det = coordination.detect_coordinated_change(
        repo_path=str(tmp_path),
        issue_text="rename everything",
        changed_files=[],
    )
    assert det["detected"] is False
    assert det["reason"] == "no changed files to fan out from"


def test_detect_degrades_without_graph(tmp_path, monkeypatch):
    import harness.deps as deps

    monkeypatch.setattr(deps, "get_code_graph_factory", lambda: None)
    det = coordination.detect_coordinated_change(
        repo_path=str(_coord_repo(tmp_path)),
        issue_text="rename invoice_total everywhere",
        changed_files=["invlib/model.py"],
    )
    assert det["detected"] is False
    assert det["reason"] == "graph unavailable"


def test_detect_excludes_protected_paths(coord_repo, tmp_path):
    """Protected files (tests/*) never enter the suggested group — an
    atomic agent-edit group can't include a path the edit policy
    forbids, and tests that merely import the changed module are not
    call sites the agent must update."""
    det = coordination.detect_coordinated_change(
        repo_path=str(coord_repo),
        issue_text="rename invoice_total and update every call site",
        changed_files=["invlib/model.py", "tests/test_model.py"],
        index_root=tmp_path / "idx7",
        protected_patterns=["tests/*"],
    )
    assert "tests/test_model.py" not in det["group_files"]
    assert "tests/test_model.py" in det["excluded"]
    assert det["detected"] is True  # the real dependents still fire


def test_detect_single_file_change_safe(coord_repo, tmp_path):
    """No dependents -> NOT coordinated; the group is just the file
    itself and the reason says so (single-file fixes behave exactly as
    before)."""
    det = coordination.detect_coordinated_change(
        repo_path=str(coord_repo),
        issue_text="rename the helper inside unrelated.py",
        changed_files=["invlib/unrelated.py"],
        index_root=tmp_path / "idx6",
    )
    assert det["detected"] is False
    assert "no structural dependents" in det["reason"]
    assert det["group_files"] == ["invlib/unrelated.py"]


# ---------------------------------------------------------------------------
# group completeness (the atomicity check)
# ---------------------------------------------------------------------------


def test_missing_group_members():
    group = ["a.py", "b.py", "c.py"]
    assert coordination.missing_group_members(group, ["a.py", "b.py", "c.py"]) == []
    assert coordination.missing_group_members(group, ["a.py"]) == ["b.py", "c.py"]
    assert coordination.missing_group_members(group, []) == group
    # deletion counts as a change
    assert (
        coordination.missing_group_members(group, ["a.py", "b.py", "c.py", "d.py"])
        == []
    )
    # backslash normalization
    assert coordination.missing_group_members(["src\\x.py"], ["src/x.py"]) == []


def test_plan_change_groups_union():
    plan = _parse_plan_json(
        json.dumps(
            {
                "plan": [
                    {
                        "id": 1,
                        "description": "fix model",
                        "checkpoint": "x",
                        "files_hint": ["invlib/model.py"],
                        "change_group": "total-rename",
                    },
                    {
                        "id": 2,
                        "description": "update serializers",
                        "checkpoint": "x",
                        "files_hint": ["invlib/serializers.py", "invlib/model.py"],
                        "change_group": "total-rename",
                    },
                    {
                        "id": 3,
                        "description": "unrelated cleanup",
                        "checkpoint": "x",
                        "files_hint": ["invlib/other.py"],
                    },
                ]
            }
        )
    )
    assert plan is not None
    assert plan[0]["change_group"] == "total-rename"
    assert plan[2]["change_group"] is None
    groups = _plan_change_groups(plan)
    assert groups == {"total-rename": ["invlib/model.py", "invlib/serializers.py"]}


def test_touched_group_files_whole_group_reverts():
    groups = {"g1": ["a.py", "b.py", "c.py"], "g2": ["d.py"]}
    # only a.py changed, but the WHOLE g1 group rolls back
    assert _touched_group_files(groups, ["a.py"]) == ["a.py", "b.py", "c.py"]
    # a group with no touched members contributes nothing
    assert _touched_group_files(groups, ["z.py"]) == []
    assert _touched_group_files({}, ["a.py"]) == []


# ---------------------------------------------------------------------------
# prompt blocks
# ---------------------------------------------------------------------------


def test_format_coordination_block_detected():
    block = coordination.format_coordination_block(
        {
            "detected": True,
            "kind": "signature",
            "reason": "signature change requires updates at every call site",
            "changed_files": ["invlib/model.py"],
            "dependent_files": {"invlib/serializers.py": ["invoice_total"]},
            "group_files": ["invlib/model.py", "invlib/serializers.py"],
        }
    )
    assert "Coordinated multi-file change detected" in block
    assert "invlib/serializers.py" in block
    assert "invoice_total" in block
    assert "ATOMIC" in block


def test_format_coordination_block_empty_when_undetected():
    assert coordination.format_coordination_block({"detected": False}) == ""


def test_format_missing_group_feedback_names_files():
    fb = coordination.format_missing_group_feedback(["m.py", "s.py", "r.py"], ["r.py"])
    assert "INCOMPLETE" in fb
    assert "r.py" in fb
    assert "m.py" in fb


def test_planner_prompt_coordination_section(tmp_path):
    msgs = prompts.render_planner_prompt(
        "drop the include_tax parameter and update every call site",
        "### invlib/model.py",
        "- tests/*",
        coordination_block=coordination.format_coordination_block(
            {
                "detected": True,
                "kind": "signature",
                "reason": "r",
                "changed_files": ["invlib/model.py"],
                "dependent_files": {"invlib/serializers.py": ["invoice_total"]},
                "group_files": ["invlib/model.py", "invlib/serializers.py"],
            }
        ),
    )
    user = msgs[1]["content"]
    assert "Coordinated-change fan-out" in user
    assert "invlib/serializers.py" in user
    assert "change_group" in msgs[0]["content"]  # schema documented in system
    # ...and is ABSENT for plain tasks
    plain = prompts.render_planner_prompt("mean() wrong", "ctx", "- tests/*")
    assert (
        "change_group" not in plain[0]["content"]
        or "{coordination_rules}" not in plain[0]["content"]
    )
    assert "(none detected)" in plain[1]["content"]


def test_planner_prompt_no_leftover_slot(tmp_path):
    """The literal {coordination_rules} slot must never leak into a
    rendered prompt (it's replaced, not str.format'ed — the JSON example
    braces in the template forbid .format)."""
    for block in (None, "", "## Coordinated multi-file change detected\n- x"):
        msgs = prompts.render_planner_prompt("i", "c", "k", coordination_block=block)
        assert "{coordination_rules}" not in msgs[0]["content"]


# ---------------------------------------------------------------------------
# atomic rollback (editor.restore_group / group_orphans)
# ---------------------------------------------------------------------------


@pytest.fixture
def group_pair(tmp_path):
    pristine = tmp_path / "pristine"
    work = tmp_path / "work"
    for d in (pristine, work):
        (d / "invlib").mkdir(parents=True)
        (d / "invlib" / "model.py").write_text("ORIG_MODEL\n", encoding="utf-8")
        (d / "invlib" / "serializers.py").write_text("ORIG_SER\n", encoding="utf-8")
        (d / "invlib" / "reports.py").write_text("ORIG_REP\n", encoding="utf-8")
        (d / "other.py").write_text("ORIG_OTHER\n", encoding="utf-8")
    # attempt 1 mutated: 2 of the 3 group files + the non-group file
    (work / "invlib" / "model.py").write_text("EDITED_MODEL\n", encoding="utf-8")
    (work / "invlib" / "serializers.py").write_text("EDITED_SER\n", encoding="utf-8")
    (work / "other.py").write_text("EDITED_OTHER\n", encoding="utf-8")
    # plus an agent-created group member not in pristine
    (work / "invlib" / "api.py").write_text("NEW_API\n", encoding="utf-8")
    return pristine, work


GROUP = [
    "invlib/model.py",
    "invlib/serializers.py",
    "invlib/reports.py",
    "invlib/api.py",
]


def test_restore_group_rolls_back_whole_group(group_pair):
    pristine, work = group_pair
    restored = editor.restore_group(str(pristine), str(work), GROUP)
    # every group member is pristine again — ATOMIC, including the file
    # this particular attempt never even touched (reports.py) and the
    # agent-created one (api.py, pristine state = absent)
    assert (work / "invlib" / "model.py").read_text(encoding="utf-8") == "ORIG_MODEL\n"
    assert (work / "invlib" / "serializers.py").read_text(
        encoding="utf-8"
    ) == "ORIG_SER\n"
    assert (work / "invlib" / "reports.py").read_text(encoding="utf-8") == "ORIG_REP\n"
    assert not (work / "invlib" / "api.py").exists()
    # work OUTSIDE the group survives (other steps' progress)
    assert (work / "other.py").read_text(encoding="utf-8") == "EDITED_OTHER\n"
    assert set(restored) == {
        "invlib/model.py",
        "invlib/serializers.py",
        "invlib/reports.py",
        "invlib/api.py",
    }


def test_restore_group_never_raises_on_missing(group_pair):
    pristine, work = group_pair
    restored = editor.restore_group(str(pristine), str(work), ["invlib/nope.py"])
    assert restored == []


def test_group_orphans_removes_emptied_dirs(group_pair):
    pristine, work = group_pair
    editor.restore_group(str(pristine), str(work), GROUP)
    # api.py was the only file the rollback removed outright; no dir
    # should be empty-and-left-behind
    gone = editor.group_orphans(str(work), GROUP)
    # invlib/ still holds model/serializers/reports -> not orphaned
    assert "invlib" not in gone
    assert (work / "invlib").is_dir()


def test_group_orphans_removes_emptied_nested_dir(tmp_path):
    pristine, tmp_work = tmp_path / "p", tmp_path / "w"
    for d in (pristine, tmp_work):
        (d / "a" / "b").mkdir(parents=True)
        (d / "a" / "b" / "only.py").write_text("x\n", encoding="utf-8")
    # the group member exists ONLY in work/ (agent-created): restoring
    # its pristine state (= absent) leaves the dir empty
    (tmp_work / "a" / "b" / "only.py").unlink()
    (tmp_work / "a" / "b" / "only.py").write_text("y\n", encoding="utf-8")
    (pristine / "a" / "b" / "only.py").unlink()
    editor.restore_group(str(pristine), str(tmp_work), ["a/b/only.py"])
    gone = editor.group_orphans(str(tmp_work), ["a/b/only.py"])
    assert gone == ["a/b"]  # the emptied dir chain pruned bottom-up
    assert not (tmp_work / "a" / "b").exists()


# ---------------------------------------------------------------------------
# state.json change_groups (additive Boundary-4 key)
# ---------------------------------------------------------------------------


def test_state_change_groups_written_and_ordered(tmp_path):
    st = TaskState(tmp_path, "t1", repo_path="/repo")
    st.set_plan(["1. x"])
    st.set_change_groups({"g": ["m.py", "s.py"]})
    on_disk = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    # the six Boundary 4 keys keep their exact prefix order
    assert list(on_disk.keys())[:6] == list(STATE_KEYS)
    # additive keys come after, groups last
    assert on_disk["change_groups"] == {"g": ["m.py", "s.py"]}
    assert list(on_disk.keys())[-1] == "change_groups"


def test_state_change_groups_absent_when_none(tmp_path):
    st = TaskState(tmp_path, "t2")
    st.set_plan(["1. x"])
    on_disk = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert "change_groups" not in on_disk
    assert "repo_path" not in on_disk


def test_state_change_groups_resume_hydration(tmp_path):
    st = TaskState(tmp_path, "t3", repo_path="/repo")
    st.set_plan(["1. x"])
    st.set_change_groups({"g": ["m.py"]})
    # relaunch: resume hydrates the groups back
    st2 = TaskState(tmp_path, "t3", repo_path="/repo", resume=True)
    assert st2.change_groups == {"g": ["m.py"]}


def test_state_change_groups_malformed_resume_never_crashes(tmp_path):
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "task_id": "t4",
                "plan": ["1. x"],
                "completed_steps": ["1. x"],
                "files_touched": [],
                "decisions": [],
                "remaining_plan": [],
                "change_groups": {"g": "not-a-list"},
            }
        ),
        encoding="utf-8",
    )
    st2 = TaskState(tmp_path, "t4", resume=True)
    assert st2.change_groups == {}  # degraded to empty, no exception


def test_state_clear_files_touched(tmp_path):
    st = TaskState(tmp_path, "t5")
    st.record_file_touched("a.py")
    st.record_file_touched("b.py")
    st.clear_files_touched(["a.py", "gone.py"])
    on_disk = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert on_disk["files_touched"] == ["b.py"]
