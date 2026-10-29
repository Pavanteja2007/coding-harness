"""P2/W1-T4 — skill DECLARATIONS are enforced, and lint feedback carries LOCALITY.

Two halves, both about the same failure: a corrective signal that names a
problem without naming where it is.

**Task A — ``extensions/skill_policy.py`` gets a production caller.** It parsed
a skill's declared model tier and permission claims and did not enforce them. A
skill declaring ``model-tier: expensive`` got no escalation; a skill declaring a
permission it did not have was not stopped.

**Task B — a lint failure says WHICH FUNCTION it is in.** Aider's distinctive
contribution is not its three-stage lint pipeline but ``TreeContext``: the
corrective message points at the enclosing function around the error lines.

Run: ``python -m pytest harness/test_p2w1_skill_policy_and_locality.py -q``
"""

from __future__ import annotations

from typing import Any, Dict

import harness.config as config_mod
import harness.skills as skills_mod
from harness import lint, skill_enforcement
from harness.skill_enforcement import (
    ENFORCEMENT_KEYS,
    enclosing_scope_block,
    enforce_skill_policy,
    session_permissions_for_run,
)


def _skill(name: str, **declaration: Any):
    """Return a real ``harness.skills.Skill`` carrying a declaration."""
    return skills_mod.Skill(
        name=name,
        description=f"how to work on {name}",
        body=f"# {name}\n\nInstructions for {name}.",
        source=f"/skills/{name}/SKILL.md",
        origin="project",
        model_tier=declaration.get("model_tier", ""),
        permission_claims=declaration.get("permission_claims"),
        declared_tools=declaration.get("declared_tools"),
    )


def _scan(*skills: Any, **overrides: Any) -> Dict[str, Any]:
    receipts = [
        {"name": skill.name, "reason": "issue words", "matched_terms": ["x"]}
        for skill in skills
    ]
    names = [skill.name for skill in skills]
    scan: Dict[str, Any] = {
        "skills_block": "\n".join(f"### Skill: {n}" for n in names),
        "matched": names,
        "receipts": receipts,
        "rendered": names,
        "omitted": [],
        "considered": len(names),
    }
    scan.update(overrides)
    return scan


# ---------------------------------------------------------------------------
# Task A: enforcement
# ---------------------------------------------------------------------------


def test_enforcement_is_off_by_default_and_the_reason_is_stated() -> None:
    """Opt-in by key presence, and an OFF run says so rather than staying silent."""
    receipt = enforce_skill_policy(_scan(_skill("a")), {})
    assert receipt["enforced"] is False
    assert "off" in receipt["skipped"]
    assert receipt["unsatisfied"] == []


def test_no_enforcement_key_is_in_the_harness_defaults() -> None:
    """A default is merged into EVERY task and every eval arm.

    A truthy ``skill_policy_enforce`` in ``DEFAULTS`` would switch every run in
    the project onto a different architecture the day the key landed -- the
    exact failure ``harness/config.py``'s own comment warns about.
    """
    for key in ENFORCEMENT_KEYS:
        assert key not in config_mod.DEFAULTS, (
            f"{key!r} is in harness/config.py::DEFAULTS. A default there is merged "
            "into every task and every eval arm, so it would switch every run in "
            "the project at once"
        )


def test_the_envelope_is_built_from_the_RUN_and_never_from_a_declaration() -> None:
    """The ceiling is made of facts about the run, never of the thing it caps.

    A skill declaring a permission is a markdown file on disk. Letting that into
    the envelope would make the cap a no-op.
    """
    envelope = session_permissions_for_run(
        {"model_tier": "cheap"}, visible_tools=("read", "grep")
    )
    assert envelope is not None
    assert envelope.max_model_tier == "cheap"
    assert envelope.allows_tool("read") is True
    assert envelope.allows_tool("bash") is False
    # An explicit operator envelope wins and is capped by the visible surface.
    explicit = session_permissions_for_run(
        {
            "model_tier": "expensive",
            "session_permissions": {"allow_tools": ["read", "write", "bash"]},
        },
        visible_tools=("read", "write"),
    )
    assert explicit is not None
    assert explicit.allows_tool("write") is True
    assert explicit.allows_tool("bash") is False, (
        "the operator's envelope listed bash but the RUN does not expose it; the "
        "run's surface is the tighter of the two, and that is the correct "
        "direction"
    )


def test_a_skill_cannot_widen_the_envelope() -> None:
    """The property the brief names: declared permissions are CAPPED by the run's.

    Same shape as OpenCode's "children inherit only the parent's deny rules" and
    Hermes' "toolsets intersected with the parent's".
    """
    greedy = _skill(
        "greedy",
        model_tier="expensive",
        declared_tools=("read", "bash", "deploy"),
        permission_claims=({"action": "allow", "tool": "bash"},),
    )
    receipt = enforce_skill_policy(
        _scan(greedy),
        {"skill_policy_enforce": True, "model_tier": "cheap"},
        skills=(greedy,),
        visible_tools=("read",),
    )
    assert receipt["enforced"] is True, receipt
    assert receipt["session"]["max_model_tier"] == "cheap"
    assert receipt["unsatisfied"], (
        "a skill declaring an expensive tier, a tool the run does not expose and "
        "a permission the run does not hold produced NO unsatisfied record. That "
        "is the whole defect this wiring exists to close"
    )
    assert receipt["unsatisfied"], receipt["unsatisfied"]
    capabilities = {row["capability"] for row in receipt["unsatisfied"]}
    assert capabilities == {"model_tier", "permission", "tool"}, capabilities
    for row in receipt["unsatisfied"]:
        assert row["reason"], row
        assert row["declared"] not in (None, ""), row
    # The TIER is the load-bearing one: a skill declaring `expensive` must come
    # back clamped to what the run pays for, not silently honoured.
    tier = next(r for r in receipt["unsatisfied"] if r["capability"] == "model_tier")
    assert tier["effective"] == "cheap"


def test_an_unsatisfied_declaration_is_recorded_with_a_reason() -> None:
    """RECORDED, never ignored -- and each row says what and why."""
    greedy = _skill("greedy", declared_tools=("deploy",))
    receipt = enforce_skill_policy(
        _scan(greedy),
        {"skill_policy_enforce": "yes"},
        skills=(greedy,),
        visible_tools=("read",),
    )
    rows = receipt["unsatisfied"]
    assert rows, receipt
    assert {row["capability"] for row in rows} == {"tool"}, rows
    for row in rows:
        assert row["skill"] == "greedy"
        assert row["declared"] == "deploy", row
        assert row["reason"], "a refusal a reader cannot interpret is a defect"


def test_an_envelope_that_does_not_exist_is_reported_not_treated_as_permission() -> None:
    """Fail-closed: "no ceiling declared" is not "everything permitted"."""
    greedy = _skill("a", declared_tools=("bash",))
    receipt = enforce_skill_policy(
        _scan(greedy),
        {"skill_policy_enforce": True},
        skills=(greedy,),
        visible_tools=(),
    )
    assert receipt["enforced"] is False
    assert "no ceiling" in receipt["skipped"]
    assert receipt["session"] is None


def test_the_receipt_says_which_declarations_were_enforced() -> None:
    """A receipt a reader can audit without re-running the scan."""
    honest = _skill("honest", declared_tools=("read",))
    receipt = enforce_skill_policy(
        _scan(honest),
        {"skill_policy_enforce": True, "model_tier": "medium"},
        skills=(honest,),
        visible_tools=("read",),
    )
    assert receipt["enforced"] is True
    assert receipt["records"], receipt
    assert receipt["records"][0]["name"] == "honest"
    assert receipt["session"]["allow_tools"] == ["read"]


def test_enforcement_never_raises_on_a_broken_scan() -> None:
    """A skill is a markdown file on disk; a broken one must not end a run."""
    for scan in ({}, {"receipts": None}, {"receipts": [None, 3, "x"]}, {"rendered": None}):
        receipt = enforce_skill_policy(
            scan, {"skill_policy_enforce": True, "model_tier": "cheap"}
        )
        assert isinstance(receipt, dict)
        assert "unsatisfied" in receipt


# ---------------------------------------------------------------------------
# Task B: locality
# ---------------------------------------------------------------------------


def test_a_parseable_error_names_its_enclosing_function_exactly() -> None:
    """The AST tier: exact, and labelled ``source="ast"``."""
    src = (
        "class Outer:\n"
        "    def method(self):\n"
        "        return 1\n"
        "\n"
        "    def other(self):\n"
        "        return 2\n"
    )
    scope = enclosing_scope_block(src, 6)
    assert scope["kind"] == "function"
    assert scope["name"] == "Outer.other"
    assert scope["header_line"] == 5
    assert scope["offset_in_scope"] == 1
    assert skill_enforcement.scope_hint(src, 6, scope) == (
        "in Outer.other (line 1 of 2)"
    )


def test_a_nested_scope_is_qualified() -> None:
    """``Outer.method`` -- the qualifier is what makes a name findable."""
    src = (
        "class Outer:\n"
        "    class Inner:\n"
        "        def method(self):\n"
        "            return 1\n"
        "\n"
        "        def sibling(self):\n"
        "            return 2\n"
    )
    scope = enclosing_scope_block(src, 6)
    assert scope["name"] == "Outer.Inner.sibling"


def test_the_nearest_scope_wins_not_the_outermost() -> None:
    src = "def outer():\n    def inner():\n        return 1\n    return inner\n"
    assert enclosing_scope_block(src, 3)["name"] == "outer.inner"
    assert enclosing_scope_block(src, 4)["name"] == "outer"


def test_an_unparseable_file_still_names_a_scope_and_SAYS_IT_IS_A_GUESS() -> None:
    """The tier that matters most: a SYNTAX error means there is no AST.

    The hint is a labelled heuristic rather than a fact, because a
    wrong function name costs one more look while no name at all costs the model
    a guess of its own. What is not acceptable is presenting the guess as an
    AST answer.
    """
    src = (
        "class Outer:\n"
        "    def method(self):\n"
        "        return 1\n"
        "\n"
        "    def other(self):\n"
        "        x = [1, 2\n"
        "        return x\n"
    )
    check = lint.check_source_for_edit(src, "x.py")
    assert check.status == lint.CHECK_FAILED
    assert check.scope["kind"] == "function"
    assert check.scope["name"] == "other"
    assert check.scope["source"] == "indent_heuristic"
    assert check.locality == "in other (line 1)"
    assert "other" in check.message
    assert check.line == 6, (
        "the real line number must survive alongside the hint; CPython reports "
        "an unclosed bracket at the line that OPENED it, not the one it is "
        "noticed on"
    )


def test_an_error_outside_every_scope_reports_no_scope_rather_than_guessing() -> None:
    """Fail-closed. A fabricated function name is worse than none."""
    src = "x = 1\ny = [1, 2\n"
    check = lint.check_source_for_edit(src, "x.py")
    assert check.status == lint.CHECK_FAILED
    assert check.locality == ""
    assert check.scope.get("kind", "module") == "module"


def test_the_ast_tier_is_labelled_as_the_ast_tier() -> None:
    """A consumer must be able to tell a fact from a guess."""
    ok = "def f():\n    return 1\n"
    scope, hint = lint._scope_for(ok, 2)
    assert scope["source"] == "ast"
    assert hint == "in f (line 1 of 2)"


def test_locality_is_additive_and_the_line_number_block_survives() -> None:
    """The pre-existing ±3 line block must be unchanged -- this ADDS to it.

    If the locality work had replaced the context block, every existing
    ``test_agt_03_edit_lint`` assertion about it would be answering a different
    question, and the honest report would have to say the receipt changed shape.
    It did not.
    """
    src = (
        "def f():\n"
        "    a = 1\n"
        "    b = 2\n"
        "    c = [1, 2\n"
        "    return c\n"
    )
    check = lint.check_source_for_edit(src, "x.py", context_lines=3)
    assert check.context, "the line-number block must still be rendered"
    assert ">" in check.context
    assert check.locality, "and the locality hint must be there too"
    assert "line 4" in check.message or "line 4" in check.context


def test_the_receipt_carries_the_scope_and_locality_additively() -> None:
    """New keys only; no existing key changed meaning."""
    assert {"scope", "locality"} <= set(lint.EditCheck._fields)
    # ...and the two keys are the ONLY additions to the receipt. The pre-existing
    # eight are asserted present, because an "additive" change that dropped one
    # would break every consumer that reads it and nothing else would notice.
    record = lint.check_source_for_edit(
        "def f():\n    return [1, 2\n", "x.py"
    ).to_dict()
    assert set(record) == {
        "status",
        "checked",
        "file",
        "line",
        "kind",
        "message",
        "context",
        "reason",
        "scope",
        "locality",
    }


def test_an_oversized_scope_is_reported_as_truncated_rather_than_dumped() -> None:
    """A 4,000-line class must not become a 4,000-line corrective message."""
    body = "\n".join(f"    x{i} = {i}" for i in range(4000))
    src = f"class Big:\n{body}\n"
    scope = enclosing_scope_block(src, 200)
    assert scope["truncated"] is True
    assert scope["body"] == ""
    assert scope["name"] == "Big"


def test_the_scope_dict_is_json_safe() -> None:
    """It rides a trace row, so it has to survive one."""
    import json

    scope = enclosing_scope_block("def f():\n    return 1\n", 2)
    assert json.loads(json.dumps(scope))["name"] == "f"
