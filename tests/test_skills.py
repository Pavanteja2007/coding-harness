"""Skills system tests (Plugins round, Task A).

Covers: SKILL.md parsing (frontmatter + fallbacks + malformed files),
discovery (project > global > plugin > extra precedence), relevance
matching (applies to matching tasks, ignores irrelevant ones), the
prompt section placement contract (after the `## Retrieved context`
cut marker so Terminal 3's difficulty predictor stays safe), the
planner content-receipt e2e through the REAL loop (a marker from a
matched skill demonstrably arrives in the planner's own user message),
the OFF arm (skills_enabled=False never scans), and config defaults.
"""

import json
import os
from pathlib import Path

from harness import prompts, skills
from harness.config import get_config
from harness.deps import reset_overrides

SKILLS_ROOT = Path(__file__).parent / "fixtures" / "skills"


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def _write_skill(root: Path, name: str, body: str, frontmatter: str = "") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(
        (f"---\n{frontmatter}---\n" if frontmatter else "") + body,
        encoding="utf-8",
    )
    return p


def test_parse_frontmatter_name_and_description(tmp_path):
    _write_skill(
        tmp_path,
        "my-skill",
        "body instructions here",
        frontmatter="name: named-skill\ndescription: Use when X applies\n",
    )
    parsed = skills._parse_skill_md(tmp_path / "my-skill" / "SKILL.md", "extra")
    assert parsed is not None
    assert parsed.name == "named-skill"
    assert parsed.description == "Use when X applies"
    assert "body instructions here" in parsed.body


def test_parse_falls_back_to_folder_name(tmp_path):
    _write_skill(tmp_path, "fallback-name", "just a body, no frontmatter")
    parsed = skills._parse_skill_md(tmp_path / "fallback-name" / "SKILL.md", "extra")
    assert parsed is not None
    assert parsed.name == "fallback-name"
    assert parsed.description == ""
    assert "just a body" in parsed.body


def test_parse_body_only_file_is_skipped(tmp_path):
    # frontmatter with NO body carries nothing to apply -> None
    _write_skill(tmp_path, "empty-body", "", frontmatter="name: x\ndescription: y\n")
    assert skills._parse_skill_md(tmp_path / "empty-body" / "SKILL.md", "x") is None


def test_parse_unreadable_returns_none(tmp_path):
    p = tmp_path / "gone" / "SKILL.md"
    assert skills._parse_skill_md(p, "x") is None


def test_parse_caps_huge_body(tmp_path):
    _write_skill(tmp_path, "huge", "x" * 100_000)
    parsed = skills._parse_skill_md(tmp_path / "huge" / "SKILL.md", "x")
    assert parsed is not None
    assert len(parsed.body) <= 20_000


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def test_discovery_scans_extra_roots(tmp_path):
    _write_skill(tmp_path, "alpha", "alpha body")
    found = skills.discover_skills(extra_roots=[str(tmp_path)])
    names = [s.name for s in found]
    assert "alpha" in names


def test_discovery_ignores_dot_dirs_and_files(tmp_path):
    _write_skill(tmp_path, ".hidden", "hidden body")
    (tmp_path / "loose-file.md").write_text("not a skill dir", encoding="utf-8")
    found = skills.discover_skills(extra_roots=[str(tmp_path)])
    assert all(s.name != ".hidden" for s in found)


def test_discovery_project_beats_global_on_name_collision(tmp_path, monkeypatch):
    # project root holds the specific skill; global holds the same NAME
    repo = tmp_path / "repo"
    (repo / ".vex" / "skills" / "dup").mkdir(parents=True)
    (repo / ".vex" / "skills" / "dup" / "SKILL.md").write_text(
        "---\nname: dup\ndescription: d\n---\nPROJECT BODY", encoding="utf-8"
    )
    groot = tmp_path / "home" / ".config" / "vex" / "skills"
    (groot / "dup").mkdir(parents=True)
    (groot / "dup" / "SKILL.md").write_text(
        "---\nname: dup\ndescription: d\n---\nGLOBAL BODY", encoding="utf-8"
    )
    monkeypatch.setenv(
        "USERPROFILE" if os.name == "nt" else "HOME", str(tmp_path / "home")
    )
    monkeypatch.setattr(skills.Path, "home", lambda: tmp_path / "home")
    found = skills.discover_skills(repo_path=str(repo))
    dups = [s for s in found if s.name == "dup"]
    assert len(dups) == 1
    assert "PROJECT BODY" in dups[0].body
    assert dups[0].origin == "project"


def test_discovery_never_raises_on_missing_roots(tmp_path):
    # all roots missing (repo without .vex, no global dir) -> []
    assert skills.discover_skills(repo_path=str(tmp_path)) == []


# ---------------------------------------------------------------------------
# matching: relevant skill applies, irrelevant ones are ignored
# ---------------------------------------------------------------------------


def _fixture_skills():
    return skills.discover_skills(extra_roots=[str(SKILLS_ROOT)])


def test_matching_skill_applies_on_django_task():
    matched = skills.find_applicable_skills(
        _fixture_skills(),
        issue_text="The queryset in views.py returns duplicated rows; fix the ORM call",
        retrieval_terms=["queryset", "orm", "views"],
        repo_path="C:/x/my-django-app",
    )
    names = [s.name for s in matched]
    assert "django-conventions" in names


def test_irrelevant_skills_are_ignored_on_plain_task():
    matched = skills.find_applicable_skills(
        _fixture_skills(),
        issue_text="the wrapping helper drops the last line of input",
        retrieval_terms=["wrap", "lines"],
        repo_path="C:/x/textutils",
    )
    assert matched == []


def test_pandas_skill_matches_dataframe_task_not_django_task():
    pandas_matched = skills.find_applicable_skills(
        _fixture_skills(),
        issue_text="DataFrame.apply computes NaN rows wrong",
        retrieval_terms=["dataframe", "apply"],
        repo_path="C:/x/dfops",
    )
    assert [s.name for s in pandas_matched] == ["pandas-vectorization"]
    # ...and the same skill does NOT match a Django task
    django_matched = skills.find_applicable_skills(
        _fixture_skills(),
        issue_text="The queryset in views.py returns duplicated rows",
        retrieval_terms=["queryset", "orm"],
        repo_path="C:/x/my-django-app",
    )
    assert "pandas-vectorization" not in [s.name for s in django_matched]


def test_repo_name_alone_matches_framework_skill():
    """The brief's canonical case: a django-conventions skill for a Django
    repo — matched even when the issue never says 'django' (the repo's
    own path/name segments are task vocabulary)."""
    matched = skills.find_applicable_skills(
        _fixture_skills(),
        issue_text="duplicate rows come back from the listing query",
        retrieval_terms=["listing"],
        repo_path="C:/work/django-blog",
    )
    assert "django-conventions" in [s.name for s in matched]


def test_max_skills_bounds_matches(tmp_path):
    for i in range(5):
        _write_skill(
            tmp_path,
            f"sk{i}",
            f"body {i}",
            frontmatter=f"name: sk{i}\ndescription: matches django orm views\n",
        )
    matched = skills.find_applicable_skills(
        skills.discover_skills(extra_roots=[str(tmp_path)]),
        issue_text="django orm views bug",
        repo_path="C:/x/r",
        max_skills=2,
    )
    assert len(matched) == 2


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_render_empty_is_explicit_none():
    assert skills.render_skills_block([]) == "(none matched)"


def test_render_caps_section(tmp_path):
    _write_skill(
        tmp_path,
        "big",
        "B" * 3000,
        frontmatter="name: big\ndescription: django orm\n",
    )
    block = skills.render_skills_block(
        skills.discover_skills(extra_roots=[str(tmp_path)]), max_chars=1000
    )
    assert len(block) <= 1500


def test_render_carries_name_and_origin():
    block = skills.render_skills_block(_fixture_skills()[:1])
    assert "### Skill:" in block
    assert "(from" in block  # origin attribution


# ---------------------------------------------------------------------------
# scan entry point (the planner-time surface)
# ---------------------------------------------------------------------------


def test_scan_returns_block_and_matched_names():
    out = skills.scan_skills_for_task(
        repo_path="C:/x/my-django-app",
        issue_text="queryset returns duplicated rows; fix the ORM call",
        retrieval_terms=["queryset", "orm"],
        extra_roots=[str(SKILLS_ROOT)],
    )
    assert out["error"] is None
    assert "django-conventions" in out["matched"]
    assert out["considered"] >= 3
    assert "Skill: django-conventions" in out["skills_block"]


def test_scan_no_skills_found_is_explicit():
    out = skills.scan_skills_for_task(
        repo_path="C:/x/plain",
        issue_text="anything",
        extra_roots=[],
    )
    assert out["matched"] == []
    assert out["skipped"] == "no skills found"
    assert out["skills_block"] == "(none matched)"


def test_scan_never_raises(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("filesystem exploded")

    monkeypatch.setattr(skills, "discover_skills", boom)
    out = skills.scan_skills_for_task(repo_path="r", issue_text="x", extra_roots=[])
    assert "filesystem exploded" in (out["error"] or "")
    assert out["skills_block"] == "(none matched)"


# ---------------------------------------------------------------------------
# prompt placement contract (the cross-module hazard)
# ---------------------------------------------------------------------------


def test_planner_prompt_carries_skills_after_retrieval_marker():
    msgs = prompts.render_planner_prompt(
        "issue text",
        "ctx block",
        "- tests/*",
        strategy="structural+grep",
        skills_block="### Skill: django-conventions\nuse the ORM",
    )
    user = msgs[1]["content"]
    assert "## Applicable skills" in user
    assert "django-conventions" in user
    # placement: AFTER retrieval (the predictor cut), BEFORE constraints
    assert (
        user.index("## Retrieved context")
        < user.index("## Applicable skills")
        < user.index("## Constraints")
    )


def test_skills_section_invisible_to_difficulty_predictor():
    """T3's difficulty estimator cuts the planner's first user message at
    '## Retrieved context' — skills sit after the cut, so they must never
    shift difficulty scoring."""
    from runtime.difficulty import _issue_text_from

    msgs = prompts.render_planner_prompt(
        "plain one-liner issue",
        "ctx",
        "(none)",
        skills_block="### Skill: scary\nrace deadlock concurrency scary words",
    )
    issue = _issue_text_from(msgs[1]["content"])
    assert "race deadlock" not in issue
    assert "plain one-liner issue" in issue


def test_planner_prompt_skills_default_is_none_placeholder():
    msgs = prompts.render_planner_prompt("i", "c", "x")
    assert "(none matched)" in msgs[1]["content"]


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_config_defaults_for_skills():
    cfg = get_config({})
    assert cfg["skills_enabled"] is True
    assert cfg["skills_max"] == 3
    assert cfg["skills_max_chars"] == 2500
    assert cfg["skills_roots"] is None
    merged = get_config({"skills_enabled": False})
    assert merged["skills_enabled"] is False


# ---------------------------------------------------------------------------
# e2e through the REAL loop (scripted model): the matched skill's body
# demonstrably arrives in the planner's OWN user message
# ---------------------------------------------------------------------------


def _planner_echo(seen, marker_holder):
    class EchoPlanner:
        def __call__(self, messages, **kwargs):
            if "planning a bug fix" in messages[0]["content"]:
                seen["planner_user"] = messages[1]["content"]
                marker_holder.append("called")
                return json.dumps(
                    {
                        "analysis": "a",
                        "plan": [
                            {
                                "id": 1,
                                "description": "fix it",
                                "checkpoint": "target passes",
                                "files_hint": [],
                            }
                        ],
                    }
                )
            return "SUBMIT"

    return EchoPlanner()


def _skill_events(logs_root, task_id):
    events = []
    trace = logs_root / task_id / "trace.jsonl"
    for line in trace.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        ev = json.loads(line)
        if ev.get("kind") == "skills":
            events.append(ev)
    return events


def test_run_task_injects_matched_skill_into_planner_prompt(tmp_path, monkeypatch):
    """Content-receipt proof: a skill whose description matches the task
    (via a unique marker word in its BODY) has that marker IN the
    planner's actual user message — not just 'the code looked right'."""
    from harness.core import run_task
    from shared.types import Task

    fixture = Path(__file__).parent / "fixtures" / "bug02_mean"
    # a skill that MATCHES the task's vocabulary, carrying a body marker
    # the issue text does not contain
    marker = "zqx-skill-marker-" + "wj7" * 3
    sroot = tmp_path / "skills"
    (sroot / "mathfix").mkdir(parents=True)
    (sroot / "mathfix" / "SKILL.md").write_text(
        "---\nname: mathfix\ndescription: use when the task is about mean, "
        "average, mathutil, or dividing in the numlib package\n---\n"
        f"Always remember the {marker} rule for arithmetic fixes.",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr("harness.skills.Path.home", lambda: tmp_path / "nowhere")

    seen = {}

    import harness.deps as deps

    deps.set_call_model(_planner_echo(seen, []))
    task = Task(
        task_id="skille2e-on",
        repo_path=str(fixture),
        issue_text="mean() divides by len-1; should divide by len",
        config={
            "test_command": "python -m pytest -q",
            "protected_paths": ["tests/*"],
            "skills_roots": [str(sroot)],
        },
    )
    try:
        run_task(task, log_root=tmp_path / "logs")
    finally:
        reset_overrides()
    assert marker in seen.get("planner_user", "")
    events = _skill_events(tmp_path / "logs", "skille2e-on")
    assert events and "mathfix" in events[0]["data"]["matched"]


def test_run_task_ignores_irrelevant_skills(tmp_path, monkeypatch):
    """An irrelevant skill's body must NOT reach the planner prompt — the
    harness reads and applies only plausibly-relevant ones."""
    from harness.core import run_task
    from shared.types import Task

    fixture = Path(__file__).parent / "fixtures" / "bug02_mean"
    marker = "zqx-irrelevant-" + "no9" * 3
    sroot = tmp_path / "skills"
    (sroot / "garden-tools").mkdir(parents=True)
    (sroot / "garden-tools" / "SKILL.md").write_text(
        "---\nname: garden-tools\ndescription: use when planting roses, "
        "pruning shrubs, or landscaping\n---\n"
        f"Follow the {marker} watering schedule.",
        encoding="utf-8",
    )
    monkeypatch.setattr("harness.skills.Path.home", lambda: tmp_path / "nowhere")

    seen = {}

    import harness.deps as deps

    deps.set_call_model(_planner_echo(seen, []))
    task = Task(
        task_id="skille2e-off-topic",
        repo_path=str(fixture),
        issue_text="mean() divides by len-1; should divide by len",
        config={
            "test_command": "python -m pytest -q",
            "protected_paths": ["tests/*"],
            "skills_roots": [str(sroot)],
        },
    )
    try:
        run_task(task, log_root=tmp_path / "logs")
    finally:
        reset_overrides()
    assert marker not in seen.get("planner_user", "")
    events = _skill_events(tmp_path / "logs", "skille2e-off-topic")
    assert events and events[0]["data"]["matched"] == []


def test_run_task_skills_disabled_is_the_off_arm(tmp_path, monkeypatch):
    """skills_enabled=False must skip the scan entirely — one code path,
    no silent half-off states."""
    from harness.core import run_task
    from shared.types import Task

    fixture = Path(__file__).parent / "fixtures" / "bug02_mean"
    seen = {}
    scanned = {"n": 0}
    real_scan = skills.scan_skills_for_task

    def counting_scan(**kwargs):
        scanned["n"] += 1
        return real_scan(**kwargs)

    monkeypatch.setattr("harness.core.skills_mod.scan_skills_for_task", counting_scan)
    monkeypatch.setattr("harness.skills.Path.home", lambda: tmp_path / "nowhere")

    import harness.deps as deps

    deps.set_call_model(_planner_echo(seen, []))
    task = Task(
        task_id="skille2e-disabled",
        repo_path=str(fixture),
        issue_text="mean() divides by len-1",
        config={
            "test_command": "python -m pytest -q",
            "protected_paths": ["tests/*"],
            "skills_enabled": False,
        },
    )
    try:
        run_task(task, log_root=tmp_path / "logs")
    finally:
        reset_overrides()
    assert scanned["n"] == 0
    events = _skill_events(tmp_path / "logs", "skille2e-disabled")
    assert events and events[0]["data"].get("skipped") == "skills_enabled=False"
