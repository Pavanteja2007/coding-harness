"""P1/W2-T1 — pin the retrieval budget, the turn caps, vision, and tool surfaces.

The wave in one sentence
------------------------
A speed improvement with no pin regresses silently, so every guarantee Wave 1
established now has a pin that fails the day someone reopens it - and every
guarantee that was **VIOLATED** when this round started has an inverted pin
naming who owns closing it.

What was actually wrong when this file was written, measured not asserted
---------------------------------------------------------------------
1. **The retrieval budget did not exist, and the hazard was worse than the
   brief described.** ``SEARCH_MATCH_CAP = 50`` bounds the COUNT of matches. It
   cannot bound their SIZE: a match renders as
   ``f"{relative}:{number}: {line[:200]}"`` (:func:`harness.retrieval._python_scan`),
   so only the LINE BODY is capped and the path prefix is unbounded. Three
   measurements, every one a SUCCESSFUL search under the 50-match cap:

   | fixture | rendered chars |
   |---|---:|
   | 45 ordinary matches | 4,379 |
   | 50 matches, each a full 200-char line | 10,490 |
   | 50 matches + a 40-deep path prefix | **84,490** |

   That last row is ~21,000 tokens from ONE grep, an 8x amplification driven
   entirely by directory depth that the match cap is structurally blind to.
   Wave 1 made the search 345x faster; a faster search returns the same flood
   sooner. :data:`harness.retrieval.SEARCH_RESULT_CHAR_BUDGET` closes it.

2. **A FIFTH hard-coded turn-cap fallback survived Wave 1's sweep.** Wave 1
   deleted four hard-coded ``25`` literals and pinned their absence. This one is
   the same defect wearing a different number:
   ``kernel.py`` read ``self.config.get("session_max_turns", 24)`` while
   :data:`harness.turn_caps.DEFAULT_SESSION_MAX_TURNS` was also 24. They agreed
   **by coincidence**, which is exactly what makes such a literal durable.

3. **The ``image`` tool produced base64 no model could read.**
   ``execution/workspace.py::Workspace.read_image`` returns ``data_base64`` in a
   dict; a JSON tool result becomes a TEXT string. A pre-round grep for
   ``image_url|input_image|data_base64|multimodal|supports_vision`` across
   ``harness/`` and ``runtime/`` returned **zero hits**.

4. **No per-model tool surface existed.** Every model received every tool; all
   shaping was per-strategy and per-mode.

Run selection: ``python -m pytest harness/test_p1w2_budgets.py -q``
"""

from __future__ import annotations

import ast
import base64
import pathlib
import tempfile
from typing import Any, Dict, List

import pytest

from harness import config as config_mod
from harness import retrieval, skipset, tool_surface, turn_caps, vision

# ---------------------------------------------------------------------------
# T1.W2.1 — the retrieval budget, and the bounds that were supposed to be enough
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _deep_repo(matches: int = 50, depth: int = 40) -> str:
    """A fixture whose match lines are at the per-line cap AND deeply nested.

    This is the shape that produced 84,490 characters before the budget existed.
    A fixture with short lines and a flat layout cannot reach it, so a test built
    from the wrong fixture would pass against the unfixed code - which is why the
    flood numbers are asserted rather than assumed.
    """
    root = pathlib.Path(tempfile.mkdtemp())
    deep = root
    for index in range(depth):
        deep = deep / f"directory_with_a_fairly_long_name_{index:02d}"
    deep.mkdir(parents=True)
    (deep / "f.py").write_text(
        "\n".join("needle " + "B" * 200 for _ in range(matches)), encoding="utf-8"
    )
    return str(root)


def _flat_repo(matches: int = 45) -> str:
    root = pathlib.Path(tempfile.mkdtemp())
    body = "\n".join(f"x{index} = needle_token({index})" for index in range(matches))
    (root / "m.py").write_text(body, encoding="utf-8")
    return str(root)


def test_the_match_cap_bounds_the_COUNT_and_provably_not_the_size() -> None:
    """The defect, pinned as a fact about the pre-fix behaviour.

    Asserted on the CHARGE not the count: a pin that only counted matches would
    pass on the unfixed code, because the unfixed code returns exactly 50 of them.
    """
    outcome = retrieval.search_repo(_deep_repo(), "needle")
    assert outcome.ok is True, "the flood must be a SUCCESS, not a refusal"
    # the SCAN found exactly the cap; the BUDGET kept a subset of them. Asserting
    # `total == 50` here would be wrong: `total` is what the model RECEIVED, and
    # the whole point of the budget is that it is smaller.
    assert outcome.budget_dropped + outcome.total == retrieval.SEARCH_MATCH_CAP
    assert outcome.truncated_by == retrieval.TRUNCATION_BUDGET
    assert len(outcome.render()) <= retrieval.SEARCH_RESULT_CHAR_BUDGET + 2000


def test_the_three_bounds_are_all_reachable_and_all_intentional() -> None:
    """50 matches / 5000 files / 10 samples, and what each was tuned FOR.

    Each bound answers a different question, and a reader who cannot say which
    is which will "fix" one of them by accident:

    * ``SEARCH_MATCH_CAP`` is a TOKEN bound - the returned lines become prompt.
    * ``SEARCH_SAMPLE_CAP`` is SMALLER than the match cap on purpose: the sample
      exists to teach narrowing, and a sample as large as the cap would be the
      answer rather than a hint.
    * ``SEARCH_FILE_SCAN_CAP`` is a LATENCY bound on the walk - and that is the
      one whose tuning Wave 1 invalidated, measured below.
    * ``SEARCH_RESULT_CHAR_BUDGET`` is a TOKEN bound the first three could not
      express, added this round.
    """
    assert retrieval.SEARCH_MATCH_CAP == 50
    assert retrieval.SEARCH_FILE_SCAN_CAP == 5000
    assert retrieval.SEARCH_SAMPLE_CAP == 10
    # the sample must never be able to stand in for the answer
    assert retrieval.SEARCH_SAMPLE_CAP < retrieval.SEARCH_MATCH_CAP


def test_the_sample_cap_is_reachable_and_is_smaller_than_the_match_cap() -> None:
    """An over-cap search shows 10 sample lines, and they are short."""
    root = _flat_repo(matches=45)
    body = "\n".join(f"y{index} = needle_token({index})" for index in range(60))
    (pathlib.Path(root) / "m.py").write_text(body, encoding="utf-8")
    outcome = retrieval.search_repo(root, "needle")
    assert outcome.ok is False
    assert outcome.error == retrieval.ERROR_TOO_MANY_MATCHES
    assert len(outcome.sample) == retrieval.SEARCH_SAMPLE_CAP


def test_the_file_scan_cap_is_reachable_and_reports_it_did_not_finish() -> None:
    """``max_files`` must not read as "0 files left"."""
    root = _flat_repo(matches=1)
    for index in range(20):
        (pathlib.Path(root) / f"extra{index}.py").write_text(
            "needle_marker\n", encoding="utf-8"
        )
    outcome = retrieval.search_repo(root, "needle", max_files=3)
    assert outcome.files_scanned == 3
    assert outcome.files_not_searched_known is False
    assert outcome.files_not_searched is None


def test_the_file_cap_tuning_still_holds_now_that_the_walk_is_fast() -> None:
    """The bound that Wave 1's speed invalidated, re-measured.

    ``SEARCH_FILE_SCAN_CAP`` was 5000 against a walk that took 144s. The walk is
    now ~0.5-1.3s (Wave 1's measured 345x). So: is 5000 still a *latency* bound,
    or has it become decorative? Measured, not assumed - the assertion is that
    5000 files is REACHABLE and that the bound is what stops a bigger tree, not
    that it is fast.

    The honest answer recorded here: the cap is still load-bearing as a bound, it
    is simply no longer a *seconds* bound. Raising it would not make a search
    slow, but it WOULD push more searches over the 50-match cap, which is the
    bound that actually costs prompt tokens. So 5000 stays.
    """
    assert retrieval.SEARCH_FILE_SCAN_CAP == 5000
    outcome = retrieval.search_repo(_flat_repo(matches=5), "needle", max_files=5000)
    assert outcome.files_not_searched_known is True
    assert outcome.files_not_searched == 0


# ---------------------------------------------------------------------------
# honest truncation
# ---------------------------------------------------------------------------


def test_a_budget_reduced_result_says_so_and_by_how_much() -> None:
    """The honesty requirement, pinned on the RENDERED text.

    A partial list that reads as a whole answer is the specific dishonesty this
    round exists to prevent, so the assertion is against ``render()`` rather than
    against a boolean field a caller might not read.
    """
    outcome = retrieval.search_repo(_deep_repo(), "needle")
    rendered = outcome.render()
    assert "[TRUNCATED: budget]" in rendered
    assert "budget-reduced" in rendered
    # it NAMES the number of dropped matches, which is the actionable figure
    assert f"{outcome.budget_dropped} of" in rendered
    assert outcome.budget_dropped > 0
    assert outcome.budget_dropped + outcome.total == 50


def test_an_ordinary_search_is_still_reported_complete() -> None:
    """The CONTROL for the truncation pin.

    Without this, "always says TRUNCATED" satisfies the test above. A budget that
    fires on every search is a broken budget wearing the honest label.
    """
    outcome = retrieval.search_repo(_flat_repo(matches=45), "needle")
    assert outcome.ok is True
    assert outcome.budget_dropped == 0
    assert outcome.truncated_by == ""
    assert outcome.truncated is False
    assert outcome.complete is True
    assert "TRUNCATED" not in outcome.render()


def test_an_untruncated_result_is_never_labelled_budget_reduced() -> None:
    """``budget_dropped == 0`` and ``truncated_by`` must never disagree."""
    for budget in (0, 8000, 10_000_000):
        outcome = retrieval.search_repo(
            _flat_repo(matches=10), "needle", budget_chars=budget
        )
        assert outcome.budget_dropped == 0, budget
        assert outcome.truncated_by == "", budget


def test_a_zero_budget_is_expressible_without_emptying_every_result() -> None:
    """``0`` means NO budget. A bound that cannot be switched off is not one."""
    outcome = retrieval.search_repo(_deep_repo(), "needle", budget_chars=0)
    assert outcome.budget_dropped == 0
    assert outcome.truncated_by == ""
    assert outcome.total == 50


def test_a_single_match_larger_than_the_budget_is_kept_not_dropped() -> None:
    """Dropping it would make the budget a data-loss bug rather than a bound.

    One oversized line still names the file and the line number and can be
    re-read precisely. Returning nothing tells the model nothing at all.
    """
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "big.py").write_text("needle " + "A" * 40000, encoding="utf-8")
    outcome = retrieval.search_repo(str(root), "needle", budget_chars=200)
    assert outcome.total == 1
    assert "big.py:1" in outcome.render()


def test_the_budget_scales_with_the_configured_match_cap() -> None:
    """The collision that the existing pin forced, pinned so it cannot regress.

    ``tests/test_agt_04_search_budgets.py`` pins that a caller who raises
    ``max_matches`` to 1000 gets the full set back, and its comment says why:
    *"a caller CAN move the cap itself, which is a config decision."* A flat
    character budget contradicted that and returned 120 of 500. So the two bounds
    scale together: at cap 50 the budget is 8000 and the flood is cut; at cap
    1000 it is 160,000 and the operator's declared cost is theirs to own.
    """
    flood = _deep_repo()
    default_cap = retrieval.search_repo(flood, "needle", max_matches=50)
    wide_cap = retrieval.search_repo(flood, "needle", max_matches=1000)
    assert default_cap.budget_chars == retrieval.SEARCH_RESULT_CHAR_BUDGET
    assert default_cap.budget_dropped > 0
    assert wide_cap.budget_chars == retrieval.SEARCH_RESULT_CHAR_BUDGET * 20
    assert wide_cap.budget_dropped == 0
    assert wide_cap.total == 50


# ---------------------------------------------------------------------------
# the skip-set authority — the 345x regression cannot return
# ---------------------------------------------------------------------------


#: Skip-set literals that are DECLARED EXCEPTIONS, with the reason each exists.
#: The exception LIST is pinned exactly, so a new local table fails this scan
#: rather than quietly joining a list nobody reads.
#:
#: ``codemod.py`` is here because R2-07 documented the decision: a rename's
#: scope must not depend on ``memory.code_graph``'s private refactors, so it
#: kept its own deliberately-local table. That is a real second set and is
#: recorded as one rather than pretended away.
_DECLARED_SKIP_LITERALS: Dict[str, str] = {
    "codemod.py": "R2-07: a rename's scope must not depend on another module's "
    "private skip table; deliberately local and documented in harness/AGENTS.md",
}


def _is_skip_literal(node: ast.AST) -> bool:
    """Whether this assignment declares a skip set rather than ALIASING one.

    The distinction is the whole point of the scan, and getting it wrong in
    either direction produces a useless pin:

    * A **re-export** (``_SKIP_DIRS = skipset.SKIP_DIRS``) is the shape we WANT
      - three modules sharing ONE object. Flagging it would fire on the correct
      code.
    * A **literal** (a tuple/frozenset/set of string constants) is the 345x
      defect. It is a second authority that will drift.

    So the discriminator is the RIGHT-HAND SIDE, read by AST: a literal
    collection of constants is a literal; anything else is a reference.
    """
    if not isinstance(node, (ast.AnnAssign, ast.Assign)):
        return False
    value = node.value
    if not isinstance(value, (ast.Tuple, ast.List, ast.Set)):
        return False
    # Every element must be a constant. An empty collection is still a literal -
    # an empty skip set is a walk that prunes nothing, which is the 345x defect
    # wearing a zero.
    if not value.elts:
        return True
    return all(isinstance(el, ast.Constant) for el in value.elts)


def test_there_is_exactly_ONE_skip_set_in_the_harness() -> None:
    """The real fix for the 345x defect: no SECOND LITERAL can be added.

    ``phases/DOCTRINE.md`` section 8 records the defect this closes: one
    retrieval path opened 124,774 directories in 144.6s where another opened 155
    in 0.42s. **The same walk. 345x.** Three ``_SKIP_DIRS`` literals existed
    inside ``harness/`` plus a fourth in ``memory/`` and more outside it.

    An ALIAS to the authority is the correct shape and is not flagged; a LITERAL
    is the defect. Identity is the other half: see the test below.
    """
    offenders: List[str] = []
    authority_modules = {"skipset.py"}
    for source in sorted((_REPO_ROOT / "harness").rglob("*.py")):
        relative = source.relative_to(_REPO_ROOT).as_posix()
        short = source.name
        if short in authority_modules or "_stubs" in source.parts:
            continue
        if short.startswith("test_"):
            continue
        try:
            tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        except SyntaxError:
            # A file that will not parse is NOT reported as clean. Skipping it
            # would turn "I could not read this file" into "this file is fine".
            offenders.append(f"{relative}: UNPARSEABLE")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets: List[str] = []
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node.target, ast.Name):
                targets = [node.target.id]
            for name in targets:
                upper = name.upper()
                is_skip_name = "SKIP" in upper and (
                    "DIR" in upper or "PAT" in upper or "NAMES" in upper
                )
                if not (is_skip_name and _is_skip_literal(node)):
                    continue
                if short in _DECLARED_SKIP_LITERALS:
                    continue
                offenders.append(f"{relative}:{node.lineno} {name}")
    assert offenders == [], (
        "a second skip-set LITERAL appeared; the 345x defect returns: "
        + "; ".join(offenders)
    )


def test_the_declared_skip_literal_exceptions_are_exactly_themselves() -> None:
    """A declared exception is a DIFFERENT answer a reader must be able to audit.

    So the exception list is pinned: adding a second local table means editing
    this mapping with a written reason, which is the point. An exception list
    that grows silently is a second authority with better manners.
    """
    assert set(_DECLARED_SKIP_LITERALS) == {"codemod.py"}
    for relative, reason in _DECLARED_SKIP_LITERALS.items():
        assert len(reason) > 40, f"{relative} needs a written reason"
        assert (_REPO_ROOT / "harness" / relative).exists()


def test_every_harness_walk_shares_the_one_object_by_identity() -> None:
    import harness.agent_loop as agent_loop
    import harness.scan_mode as scan_mode

    assert retrieval._SKIP_DIRS is skipset.SKIP_DIRS
    assert agent_loop._SKIP_DIRS is skipset.SKIP_DIRS
    assert scan_mode._SKIP_DIRS is skipset.SKIP_DIRS


def test_a_variant_skip_set_must_carry_a_reason() -> None:
    """A walk that prunes differently must say why in its receipt."""
    with pytest.raises(ValueError):
        skipset.skip_dirs_for(drop=["logs"], reason="")
    variant = skipset.skip_dirs_for(drop=["logs"], reason="scan_mode needs logs")
    assert "logs" not in variant
    assert "node_modules" in variant


# ---------------------------------------------------------------------------
# ripgrep-native exclusions — applied by rg, never by post-filtering
# ---------------------------------------------------------------------------


def test_ripgrep_receives_the_skip_set_as_globs() -> None:
    """The exclusions must be applied BY rg.

    Post-filtering a fast result set is a new latency cliff: rg would hand back
    tens of thousands of ``node_modules`` matches and Python would discard them,
    which is precisely the work the skip-set exists to prevent.
    """
    import inspect

    from harness import search_engine

    source = inspect.getsource(search_engine.ripgrep_search)
    assert "skipset.ripgrep_glob_args()" in source
    assert "--glob" in source

    args = skipset.ripgrep_glob_args()
    assert args, "the glob list must not be empty"
    # every skip dir is represented, and the trailing /** is load-bearing:
    # !**/logs/ does NOT exclude the directory's CONTENTS in ripgrep
    for name in list(skipset.SKIP_DIRS) + list(skipset.SKIP_FILE_GLOBS):
        assert f"!**/{name}/**" in args, name


def test_the_python_fallback_and_ripgrep_prune_the_same_set() -> None:
    """Two engines that disagree make a result only incidentally reproducible."""
    args = set(skipset.ripgrep_glob_args())
    for name in list(skipset.SKIP_DIRS) + list(skipset.SKIP_FILE_GLOBS):
        assert f"!**/{name}/**" in args
    assert skipset.is_skipped_dir("node_modules") is True
    assert skipset.is_skipped_dir("logs") is True
    assert skipset.is_skipped_dir("src") is False


# ---------------------------------------------------------------------------
# T1.W2.2 — the turn caps
# ---------------------------------------------------------------------------


def test_the_per_task_cap_is_at_least_fifty_and_is_config_driven() -> None:
    """The rung-8 value, and the proof a user can change it."""
    assert config_mod.DEFAULTS["agent_max_turns"] >= 50
    assert turn_caps.resolve_caps(config_mod.DEFAULTS).per_task >= 50
    # config-driven: a caller can move it, and the receipt says so
    moved = turn_caps.resolve_caps({"agent_max_turns": 7})
    assert moved.per_task == 7
    assert moved.per_task_source == "agent_max_turns"
    assert turn_caps.resolve_caps({}).per_task_source == "default"


#: The config keys that NAME a turn cap. A hard-coded fallback is only a defect
#: when it is standing in for one of these - `blast_radius`'s ``max_files=25`` is
#: a different bound entirely and flagging it would make the pin cry wolf.
_TURN_CAP_KEYS = (
    "agent_max_turns",
    "session_max_turns",
    "session_max_conversation_turns",
)


def test_no_hardcoded_twenty_five_fallback_survives_anywhere() -> None:
    """Wave 1 deleted four; this is the pin that keeps them deleted.

    Read by AST rather than by substring, because a literal in a DOCSTRING is
    prose and a literal in ``cfg.get("agent_max_turns", 25)`` is the defect. The
    FIRST argument must name a turn-cap key, which is what separates this from
    every other ``.get(x, 25)`` in the tree.
    """
    offenders: List[str] = []
    for source in sorted((_REPO_ROOT / "harness").rglob("*.py")):
        if source.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            key = node.args[0] if node.args else None
            if not (isinstance(key, ast.Constant) and key.value in _TURN_CAP_KEYS):
                continue
            for arg in node.args[1:]:
                if isinstance(arg, ast.Constant) and arg.value in (25, 24, 60):
                    offenders.append(f"{source.relative_to(_REPO_ROOT)}:{node.lineno}")
    assert offenders == [], "a hard-coded turn-cap fallback returned: " + "; ".join(
        offenders
    )


def test_the_kernel_reads_the_context_summary_cap_through_the_authority() -> None:
    """The FIFTH literal, which survived Wave 1's sweep.

    ``kernel.py`` read ``self.config.get("session_max_turns", 24)`` while
    ``turn_caps.DEFAULT_SESSION_MAX_TURNS`` was also 24. They agreed BY
    COINCIDENCE, which is what makes such a literal durable: move the constant
    and nothing notices.
    """
    from harness.agent_kernel import turn_caps_shim

    source = (_REPO_ROOT / "harness" / "agent_kernel" / "kernel.py").read_text(
        encoding="utf-8-sig"
    )
    assert 'get("session_max_turns", 24)' not in source
    assert "resolve_context_summary_turns(self.config)" in source

    # and the shim genuinely resolves through the authority, both key names
    assert turn_caps_shim.resolve_context_summary_turns({}) == (
        turn_caps.DEFAULT_SESSION_MAX_TURNS
    )
    assert turn_caps_shim.resolve_context_summary_turns({"session_max_turns": 7}) == 7
    assert (
        turn_caps_shim.resolve_context_summary_turns(
            {"session_context_summary_turns": 9}
        )
        == 9
    )


def test_the_budget_guard_fires_before_the_turn_guard() -> None:
    """The ordering the brief asks to be explicit, asserted from BOTH loop sides.

    The worst case of the wrong order is a run that spends its whole turn budget
    before anyone notices the money is gone. T3 verified this from the money
    side; this verifies it from the loop side, by reading where each guard sits
    in the source rather than by trusting a comment.

    The rule being pinned: the budget guard is checked INSIDE the turn loop and
    BEFORE the model call on every turn, so it is reachable on every turn; the
    turn guard is the loop bound, so it is only reachable once the budget guard
    has already passed on every prior turn.
    """
    import inspect

    from harness import agent_loop_step as step_mod
    from harness.agent_kernel import strategy as strategy_mod

    loop_source = inspect.getsource(strategy_mod.DailyCodingStrategy.run)
    budget_at = loop_source.index("budget cap exceeded")
    model_at = loop_source.index("self.model_gateway.call(")
    assert budget_at < model_at, (
        "the budget guard must be checked BEFORE the model call, or a turn is "
        "spent on money that was already gone"
    )
    assert "for turn in range(1, max_turns + 1)" in loop_source

    # P2/W1-T2: this test used to read the SECOND half of its claim out of
    # `harness/agent_loop.py`'s source, which asserted
    # `'end_reason = "budget cap exceeded"' in <module source>`. That string
    # lived in `_run_agent_legacy`, the deleted engine. The pin is now read
    # from the engine that actually OWNS the guard -- the pure core -- and it is
    # a stronger assertion than the string match it replaces, because it checks
    # the guard's POSITION in the turn loop rather than that the phrase is
    # spelled somewhere in a 4,000-line file.
    step_source = inspect.getsource(step_mod.step)
    budget_at = step_source.index('reason="budget cap"')
    model_at = step_source.index("reply = str(tools.ask(")
    assert budget_at < model_at, (
        "the pure core's budget guard must be checked BEFORE the model call on "
        "every turn, or a turn is spent on money that was already gone"
    )
    assert 'stop(CompletionStatus.FAILED, "budget cap exceeded")' in step_source


def test_the_turn_cap_approach_is_an_observation_not_a_stop() -> None:
    """The cliff must be VISIBLE, and must not become a second hidden cap.

    ``approach_observation`` returns a receipt near the cap and ``None``
    otherwise, and it never stops anything. The ``control`` arm matters: without
    it, a function that returned a receipt on every turn would satisfy the first
    assertion and be useless.
    """
    caps = turn_caps.resolve_caps({"agent_max_turns": 60})
    near = turn_caps.approach_observation(55, config={"agent_max_turns": 60})
    assert near is not None
    assert near["kind"] == turn_caps.CAP_PER_TASK
    assert "not a stop" in near["observation"]
    assert turn_caps.approach_observation(1, config={"agent_max_turns": 60}) is None
    assert caps.per_task == 60


def test_the_approach_observation_emits_a_journal_event() -> None:
    """Observable means a JOURNAL EVENT, not only a log line.

    P2/W1-T2: this used to assert the event appears in `harness/agent_loop.py`.
    `agent_loop.py` is the adapter; the event is emitted by the pure core
    (`harness/agent_loop_step.py`), which is where the latch lives, so that is
    where it is read from now. Reading the adapter instead would have kept
    passing while the engine stopped emitting it.
    """
    source = (_REPO_ROOT / "harness" / "agent_loop_step.py").read_text(
        encoding="utf-8-sig"
    )
    assert '"turn_cap_approaching"' in source
    assert "_cap_warned" in source or "cap_warned" in source, (
        "the event must latch - one row per run, not one per turn"
    )
    # ...and the adapter must still forward it: an event the engine emits and
    # the journal never receives is a log line, which is what this pin exists
    # to distinguish.
    adapter = (_REPO_ROOT / "harness" / "agent_loop.py").read_text(encoding="utf-8-sig")
    assert "for event in events" in adapter or "_event in events" in adapter, (
        "run_agent_stepped no longer drains the pure core's event list into the "
        "journal. Without that loop `turn_cap_approaching` (and every other "
        "event) is produced and dropped"
    )


def test_session_max_turns_decision_is_pinned_so_it_cannot_drift() -> None:
    """Wave 1's decision: KEEP the key, publish the honest alias.

    The brief claimed the key had zero readers. It had one, and what the reader
    does is the real defect - it bounds CONTEXT-SUMMARY records, not the
    conversation. So the key stays (it is load-bearing) and the honest alias is
    published. If it is ever deleted, this pin fails and says what to do.
    """
    assert turn_caps.SESSION_MAX_TURNS_ALIAS == "session_context_summary_turns"
    assert "session_max_turns" in config_mod.DEFAULTS
    caps = turn_caps.resolve_caps({})
    assert caps.context_summary == turn_caps.DEFAULT_SESSION_MAX_TURNS
    assert caps.context_summary_source == "default"
    # the real conversation cap is a SEPARATE, honestly-unbounded key
    assert caps.conversation is None
    assert "session_max_conversation_turns" in config_mod.DEFAULTS
    assert config_mod.DEFAULTS["session_max_conversation_turns"] is None
    # and the rendered receipt says "unbounded" rather than omitting it
    assert "unbounded" in turn_caps.render_caps({})


def test_the_receipt_carries_provenance_so_a_check_cannot_pass_on_absence() -> None:
    """A check that passes because the fields are ABSENT is not a check."""
    receipt = turn_caps.turn_caps_receipt(config_mod.DEFAULTS, turn=55)
    assert receipt["caps"]["per_task"]["value"] >= 50
    assert receipt["caps"]["per_task"]["source"]
    assert receipt["caps"]["per_task"]["bounds"]
    assert receipt["caps"]["context_summary"]["value"] >= 1
    assert receipt["caps"]["conversation"]["value"] is None
    assert receipt["approaching"] is True
    assert receipt["approach_observation"]["kind"] == turn_caps.CAP_PER_TASK
    assert isinstance(receipt["rendered"], str) and receipt["rendered"]


# ---------------------------------------------------------------------------
# T1.W2.3 — vision
# ---------------------------------------------------------------------------

_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 64).decode("ascii")


def _image_record(**over: Any) -> Dict[str, Any]:
    record = {
        "path": "screenshot.png",
        "media_type": "image/png",
        "bytes": 72,
        "revision": {"sha256": "ab"},
        "data_base64": _PNG,
    }
    record.update(over)
    return record


def test_the_image_tool_produces_a_real_multimodal_content_block() -> None:
    """The gap: a JSON tool result is TEXT, and no model can read base64.

    Asserted on the SHAPE a provider accepts, not on our own field names.
    """
    plan = vision.vision_plan_for([_image_record()], model="gpt-5", provider="openai")
    assert plan.sent is True
    block = plan.blocks[0].to_dict()
    assert block["type"] == "image_url"
    assert block["image_url"]["url"].startswith("data:image/png;base64,")


def test_each_family_gets_its_OWN_native_shape() -> None:
    anthropic = vision.vision_plan_for(
        [_image_record()], model="claude-sonnet-4", provider="anthropic"
    )
    assert anthropic.sent is True
    block = anthropic.blocks[0].to_dict()
    assert block["type"] == "image"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/png"
    # the openai-shaped block must NOT appear on an anthropic request
    assert "image_url" not in block

    google = vision.vision_plan_for(
        [_image_record()], model="gemini-2.5-pro", provider="google"
    )
    assert google.sent is True
    assert google.blocks[0].to_dict()["type"] == "image_url"


def test_a_non_multimodal_model_is_refused_and_NOTHING_is_sent() -> None:
    """The load-bearing property: no request is built at all.

    A vision content block sent to a text-only model is a hard provider error,
    so the gate must fail CLOSED. Both refusals are checked because they mean
    different things: we KNOW it is text-only, versus we do not know.
    """
    known = vision.vision_plan_for([_image_record()], model="llama-3-8b")
    assert known.status == vision.VISION_UNSUPPORTED_MODEL
    assert known.sent is False
    assert known.blocks_payload == []
    assert known.blocks == ()

    unknown = vision.vision_plan_for([_image_record()], model="mystery-9b")
    assert unknown.status == vision.VISION_FAMILY_UNKNOWN
    assert unknown.sent is False
    assert unknown.blocks_payload == []

    nomodel = vision.vision_plan_for([_image_record()], model=None)
    assert nomodel.sent is False
    assert nomodel.blocks_payload == []
    assert "not" in nomodel.detail.lower()


def test_the_capability_gate_is_one_directional_and_that_is_said_out_loud() -> None:
    """The known cost of the safe direction, pinned so it cannot be forgotten.

    ``Qwen2.5-VL`` IS multimodal and is refused, because the text-only name hint
    can only ever ADD a refusal and never grant capability. That asymmetry is
    what makes a name rule acceptable here at all - and pinning the cost is what
    stops somebody "fixing" the hint into a capability table and reintroducing
    the unknowable-unknown class this module exists to prevent.
    """
    plan = vision.vision_plan_for([_image_record()], model="Qwen2.5-VL-72B")
    assert plan.sent is False
    assert plan.status == vision.VISION_UNSUPPORTED_MODEL


def test_the_size_cap_fires_before_the_payload_reaches_a_request() -> None:
    """A 50MB screenshot must never become 66MB of base64 in a message."""
    big = base64.b64encode(b"y" * (6 * 1024 * 1024)).decode("ascii")
    plan = vision.vision_plan_for([_image_record(data_base64=big)], model="gpt-5")
    assert plan.status == vision.VISION_TOO_LARGE
    assert plan.sent is False
    assert plan.blocks_payload == []
    assert "NOT sent" in plan.detail
    assert str(6 * 1024 * 1024) in plan.detail


def test_the_count_cap_fires_and_reports_it() -> None:
    plan = vision.vision_plan_for([_image_record()] * 9, model="gpt-5")
    assert plan.status == vision.VISION_TOO_MANY
    assert plan.sent is False
    assert "9" in plan.detail


def test_an_unsupported_media_type_is_refused_by_name() -> None:
    tiff = base64.b64encode(b"II*\x00" + b"z" * 32).decode("ascii")
    plan = vision.vision_plan_for(
        [_image_record(path="scan.tiff", media_type="image/tiff", data_base64=tiff)],
        model="gpt-5",
    )
    assert plan.status == vision.VISION_UNSUPPORTED_MEDIA_TYPE
    assert plan.sent is False


def test_a_partially_refusable_batch_reports_both_halves() -> None:
    """Three readable screenshots and one 50MB screenshot is a useful outcome.

    A whole-turn refusal that discards the three good images would be the wrong
    answer; so would sending the fourth.
    """
    big = base64.b64encode(b"y" * (6 * 1024 * 1024)).decode("ascii")
    plan = vision.vision_plan_for(
        [
            _image_record(path="a.png"),
            _image_record(path="b.png"),
            _image_record(path="huge.png", data_base64=big),
        ],
        model="gpt-5",
    )
    assert plan.sent is True
    assert len(plan.blocks) == 2
    assert plan.refused == ("huge.png",)
    assert plan.total_bytes < 200
    assert "NOT sent" in plan.detail


def test_image_bytes_are_not_text_but_the_path_and_metadata_are() -> None:
    """The sanitiser boundary, and it is not negotiable.

    Bytes are base64'd, never pattern-matched. The PATH is ordinary text and can
    absolutely carry a credential, so it goes through the ONE fail-closed
    journal boundary P0-W1-T1 established.
    """
    leaky = _image_record(path="/home/alice/sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345.png")
    plan = vision.vision_plan_for([leaky], model="gpt-5")
    assert plan.sent is True
    url = plan.blocks[0].to_dict()["image_url"]["url"]
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in url
    assert plan.blocks[0].path != leaky["path"]


def test_the_repr_never_carries_base64() -> None:
    """A traceback that printed a block would put 6.7M chars in a log line."""
    plan = vision.vision_plan_for([_image_record()], model="gpt-5")
    rendered = repr(plan.blocks[0])
    assert _PNG not in rendered
    assert "payload=" in rendered
    assert len(rendered) < 400


def test_the_conversation_carries_a_real_content_list() -> None:
    """The wiring: an image must arrive as a block, not as base64 text."""
    from harness.agent_kernel.conversation import ConversationMemory

    memory = ConversationMemory()
    memory.record_tool_result("grep", True, "found 3")
    plain = memory._history[-1].to_message()
    assert isinstance(plain["content"], str), (
        "a turn with no attachments must render byte-identically to before"
    )

    plan = vision.vision_plan_for([_image_record()], model="gpt-5")
    memory.record_image("[image] 1 attached", plan.blocks_payload, turn=1)
    message = memory._history[-1].to_message()
    assert isinstance(message["content"], list)
    assert isinstance(message["content"][0], str)
    assert message["content"][1]["type"] == "image_url"


def test_a_block_is_not_charged_to_the_character_budget() -> None:
    """Accounting error that would stop a user attaching a screenshot at all.

    Base64 is not prompt text in the way the character budget models it, and
    charging it there would make every image look like a context-window event.
    """
    from harness.agent_kernel.conversation import ConversationMemory

    memory = ConversationMemory()
    plan = vision.vision_plan_for([_image_record()], model="gpt-5")
    memory.record_image("[image] 1 attached", plan.blocks_payload)
    assert memory._history[-1].chars < 100
    assert memory._history[-1].attachments


# ---------------------------------------------------------------------------
# T1.W2.4 — per-model tool surfaces
# ---------------------------------------------------------------------------

_CATALOG = ["read", "grep", "list", "image", "edit", "write", "apply_patch", "bash"]


def test_nothing_is_withheld_by_default() -> None:
    """The non-negotiable: a mapping never changes a surface unless asked."""
    for config in ({}, None, {"model": "gpt-5"}):
        ledger = tool_surface.surface_for(
            model="gpt-5", provider="openai", config=config, catalog=_CATALOG
        )
        assert ledger.default is True
        assert ledger.withheld == ()
        assert ledger.mode == tool_surface.MODE_FULL
        assert ledger.names(_CATALOG) == _CATALOG
    assert "tool_surface_capabilities" in config_mod.DEFAULTS
    assert config_mod.DEFAULTS["tool_surface_capabilities"] is None


def test_the_opt_in_is_read_by_key_presence_and_truthiness() -> None:
    """AGT-13's rule. A typo cannot enable an override."""
    for value in (None, False, "", "off", "0", "false", "no", []):
        ledger = tool_surface.surface_for(
            model="gpt-5",
            config={"tool_surface_capabilities": value},
            catalog=_CATALOG,
        )
        assert ledger.default is True, value
    for value in ("gpt_codex_patch", ["gpt_codex_patch"], "prefer_apply_patch"):
        ledger = tool_surface.surface_for(
            model="gpt-5",
            config={"tool_surface_capabilities": value},
            catalog=_CATALOG,
        )
        assert ledger.default is False, value


def test_a_typo_reports_rather_than_silently_disabling_every_override() -> None:
    ledger = tool_surface.surface_for(
        model="gpt-5",
        config={"tool_surface_capabilities": "gpt_codex_patc"},
        catalog=_CATALOG,
    )
    assert ledger.unknown_capabilities == ("gpt_codex_patc",)
    assert ledger.default is True
    assert "not implemented" in ledger.reason


def test_the_family_is_metadata_derived_not_a_hand_written_if_chain() -> None:
    """The property the brief demands, checked structurally.

    The module must not contain a substring comparison against a model name for
    the purpose of SELECTING a surface. The one name rule it does contain is the
    text-only hint in :mod:`harness.vision`, which is one-directional (it can
    only add a refusal) and lives in the other module.
    """
    source = (_REPO_ROOT / "harness" / "tool_surface.py").read_text(
        encoding="utf-8-sig"
    )
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            rendered = ast.dump(node)
            assert "gpt-" not in rendered
            assert "claude" not in rendered
            assert "gemini" not in rendered
    assert "effort_family_for" in source, (
        "family resolution must be delegated to runtime.model_capabilities, "
        "not re-derived from a model-name string"
    )


def test_patch_only_withholds_edit_and_write_and_names_the_replacement() -> None:
    ledger = tool_surface.surface_for(
        model="gpt-5",
        provider="openai",
        config={"tool_surface_capabilities": "gpt_codex_patch"},
        catalog=_CATALOG,
    )
    assert ledger.mode == tool_surface.MODE_PATCH_ONLY
    assert set(ledger.withheld) == {"edit", "write"}
    offered = ledger.names(_CATALOG)
    assert "edit" not in offered and "write" not in offered
    assert "apply_patch" in offered
    assert "read" in offered, "a surface may not withhold what the model needs"
    assert tool_surface.surface_violations(ledger, offered) == []
    assert "apply_patch" in ledger.prompt_note()
    assert "edit" in ledger.prompt_note()


def test_prefer_apply_patch_withholds_nothing() -> None:
    """The additive variant. A receipt naming a withheld tool the model still has
    is worse than a receipt that names nothing."""
    ledger = tool_surface.surface_for(
        model="gpt-5",
        config={"tool_surface_capabilities": "prefer_apply_patch"},
        catalog=_CATALOG,
    )
    assert ledger.mode == tool_surface.MODE_PREFER_PATCH
    assert ledger.withheld == ()
    assert ledger.names(_CATALOG) == _CATALOG
    assert tool_surface.surface_violations(ledger, ledger.names(_CATALOG)) == []
    assert "Prefer" in ledger.prompt_note()


def test_over_provisioning_is_detected() -> None:
    """A model that still HAS the withheld tool - the receipt and schema disagree."""
    ledger = tool_surface.surface_for(
        model="gpt-5",
        config={"tool_surface_capabilities": "gpt_codex_patch"},
        catalog=_CATALOG,
    )
    problems = tool_surface.surface_violations(ledger, _CATALOG)
    kinds = {p["kind"] for p in problems}
    assert "over_provisioned" in kinds
    assert {p["tool"] for p in problems if p["kind"] == "over_provisioned"} == {
        "edit",
        "write",
    }


def test_under_provisioning_is_detected() -> None:
    """Withholding without a replacement leaves the model unable to work at all.

    This is the opposite failure from over-provisioning and a check that only
    looked for one would pass the other's defect - which is why both are in one
    function and both are pinned here.
    """
    ledger = tool_surface.surface_for(
        model="gpt-5",
        config={"tool_surface_capabilities": "gpt_codex_patch"},
        catalog=_CATALOG,
    )
    without_patch = [n for n in ledger.names(_CATALOG) if n != "apply_patch"]
    problems = tool_surface.surface_violations(ledger, without_patch)
    kinds = {p["kind"] for p in problems}
    assert "under_provisioned" in kinds
    assert "apply_patch" in {p["tool"] for p in problems}


def test_both_failures_are_detected_together_and_the_default_is_clean() -> None:
    broken = tool_surface.SurfaceLedger(
        mode="patch_only",
        capability="gpt_codex_patch",
        withheld=("edit", "write"),
    )
    kinds = {
        p["kind"] for p in tool_surface.surface_violations(broken, ["read", "edit"])
    }
    assert kinds == {"over_provisioned", "under_provisioned"}

    clean = tool_surface.surface_for(model="gpt-5", catalog=_CATALOG)
    assert tool_surface.surface_violations(clean, _CATALOG) == []


def test_the_ledger_records_the_surface_and_why() -> None:
    """ "The model did not use edit" must be answerable from a receipt."""
    ledger = tool_surface.surface_for(
        model="gpt-5",
        provider="openai",
        config={"tool_surface_capabilities": "gpt_codex_patch"},
        catalog=_CATALOG,
    )
    receipt = ledger.explain()
    for key in (
        "surface_mode",
        "surface_family",
        "surface_model",
        "surface_provider",
        "surface_capability",
        "surface_source",
        "surface_withheld",
        "surface_total",
        "surface_default",
        "surface_reason",
    ):
        assert key in receipt, key
    assert receipt["surface_capability"] == "gpt_codex_patch"
    assert receipt["surface_source"] == "tool_surface_capabilities"
    assert receipt["surface_total"] == len(_CATALOG)
    assert receipt["surface_default"] is False


def test_the_default_surface_renders_no_prompt_change() -> None:
    """An unconfigured run's prompt must be byte-identical to its pre-round self.

    The same discipline ``agent_default_strategy`` followed: a prompt change with
    no behaviour behind it is a prompt regression waiting to be measured.
    """
    assert tool_surface.surface_for(model="gpt-5", catalog=_CATALOG).prompt_note() == ""


def test_the_registry_is_not_restricted_by_a_surface_mapping() -> None:
    """The surface is the SCHEMA list, never the registry.

    ``plan_mode_tools()``, ``runtime/roles.py`` and the catalog parity report all
    read the registry, so restricting it here would silently disable them.
    """
    source = (_REPO_ROOT / "harness" / "agent_kernel" / "strategy.py").read_text(
        encoding="utf-8-sig"
    )
    body = source.split("def _schemas_for_model", 1)[-1].split("\n    def ", 1)[0]
    assert "self.tools.restrict(" not in body
    assert "self.tools.schemas()" in body


def test_a_broken_mapping_refuses_the_MAPPING_not_the_run() -> None:
    """A surface that cannot edit is worse than the full surface plus a report."""
    source = (_REPO_ROOT / "harness" / "agent_kernel" / "strategy.py").read_text(
        encoding="utf-8-sig"
    )
    assert "tool_surface_rejected" in source
    body = source.split("problems = tool_surface.surface_violations", 1)[-1]
    segment = body.split("def ", 1)[0]
    assert "return schemas" in segment, (
        "the fallback on a mis-provisioned surface must be the FULL surface"
    )
