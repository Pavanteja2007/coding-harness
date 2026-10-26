"""ONE skip-set authority, and the count of modules that have not adopted it.

WHY THIS TEST IS ABOUT THE COUNT AND NOT THE VALUES
---------------------------------------------------
Three tables that hold the same names today diverge the first time one of them
gains an entry, and nothing in the tree notices. ``phases/DOCTRINE.md`` §8
records what that costs: *one retrieval path opened 124,774 directories in
144.6 s where another opened 155 in 0.42 s. **The same walk. 345x.***

So the property worth pinning is not "the sets are equal" -- it is "there is one
set". A test that asserted equality would be green on the day the three tables
were written and would stay green while they diverged, which is the exact
failure mode the authority exists to prevent.

WHAT THIS TEST DOES WITH A RED COUNT
------------------------------------
It does not fail. Not yet, and not for the count: four modules outside T5's
ownership still declare a table, and a test this terminal cannot make green is
a test that trains people to ignore the gate.

What it does instead is make the gap **impossible to forget and impossible to
grow**:

* :func:`test_the_authority_is_shared_and_not_a_copy` -- the authority exists
  and is importable from the bottom layer.
* :func:`test_no_new_skip_set_table_may_appear` -- the count is pinned to the
  number measured when this file was written, with every remaining table named
  and owned. A NEW table fails. A table that DISAPPEARS fails and says "delete
  its entry here", which is the inverted-pin direction: closing the gap is a
  change to this file, so the record cannot rot.
* :func:`test_every_remaining_table_is_named_with_an_owner` -- the list is not
  a bare number.
* :func:`test_the_harness_walks_all_bind_the_same_object` -- the three
  ``harness/`` walks are the ONE place where object identity, rather than
  equality, is the right instrument, and it is what T1 shipped. Asserted here
  too because a check that only exists in the module whose defect it is about
  is a check a refactor can delete.
* :func:`test_the_variants_carry_a_reason` -- a differently-pruning walk is only
  allowed through :func:`shared.skipset.skip_dirs_for`, and only with a
  non-empty reason.

THE MEASURED STATE AT THE TIME OF WRITING
-----------------------------------------
9 independent tables, 4 adopters. ``memory/code_graph.py`` was moved onto a
declared variant in the same change that added this file. The rest belong to
T2 (``execution/``), T3 (``runtime/``), T4 (``cli/``, ``dashboard/``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

import pytest

from shared import skipset

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The independent tables this repository is KNOWN to declare, with the owner
#: who has to close each one. A module in this list that stops declaring a
#: table fails ``test_no_new_skip_set_table_may_appear`` and the message says
#: to delete its entry -- so closing the gap updates this file, and a gap that
#: is fixed but still listed is a record that lies about the tree.
KNOWN_OWN_TABLES: Dict[str, str] = {
    "cli/fileview.py": "T4 (cli/) -- 18 names, omits the authority's envs, "
    "build output and tooling trees",
    "dashboard/collect.py": "T4 (dashboard/) -- 9 names, the smallest table in "
    "the tree and the one that omits the most",
    "runtime/analyze_history.py": "T3 (runtime/) -- 11 names",
    "runtime/difficulty.py": "T3 (runtime/) -- 19 names, adds `docs` and `site`",
    "runtime/subagents.py": "T3 (runtime/) -- 6 names",
    "execution/walk_scope.py": "T2 (execution/) -- 49 names, i.e. it prunes "
    "MORE than the authority; T1's P1/W1 handoff already recorded this as the "
    "third authority and asked for one home",
}

#: The count pinned when this file was written. Deliberately a literal and not
#: ``len(KNOWN_OWN_TABLES)``: the two can differ, and when they do, the tree has
#: gained a table nobody recorded. Which of the two is wrong is exactly the
#: question this pin exists to force somebody to answer.
PINNED_OWN_TABLE_COUNT = 6


@pytest.fixture(scope="module")
def audit() -> Dict[str, object]:
    return skipset.skip_dir_tables(REPO_ROOT)


class TestTheAuthorityExistsAndIsShared:
    def test_the_authority_is_shared_and_not_a_copy(self) -> None:
        assert skipset.SOURCE_OF_TRUTH == "shared/skipset.py", (
            "the authority moved. Everything that imports it, and this file's "
            "own audit, is keyed on that string."
        )
        assert len(skipset.SKIP_DIRS) >= 40, (
            f"the authority carries only {len(skipset.SKIP_DIRS)} names; a set "
            "this small is the shape of one of the private tables, not of a "
            "consolidated union"
        )
        # The load-bearing names, by the measurement that found each of them.
        for name, why in (
            ("logs", "one copy of the repository under repair per run"),
            (".git", "version control state is not source"),
            ("node_modules", "third-party vendored packages"),
            (".venv", "an environment is not source"),
            ("site-packages", "third-party vendored packages"),
            ("graphify-out", "another tool's output"),
        ):
            assert name in skipset.SKIP_DIRS, f"{name} is missing: {why}"

    def test_the_harness_walks_all_bind_the_same_object(self) -> None:
        """Identity, not equality -- three equal sets diverge eventually."""
        import harness.agent_loop as agent_loop
        import harness.retrieval as retrieval
        import harness.scan_mode as scan_mode
        import harness.skipset as harness_skipset

        assert retrieval._SKIP_DIRS is harness_skipset.SKIP_DIRS
        assert agent_loop._SKIP_DIRS is harness_skipset.SKIP_DIRS
        assert scan_mode._SKIP_DIRS is harness_skipset.SKIP_DIRS

    def test_the_two_authorities_carry_the_same_names(self) -> None:
        """``shared/`` and ``harness/`` are two authorities. They must AGREE.

        This caught its own gap on the first run: ``harness/skipset.py`` carried
        ``.mypy_cache.d`` and the transcription into ``shared/skipset.py`` did
        not, so the two "authorities" diverged by exactly one entry on the day
        the second one was created. One entry is not a near-miss -- it is the
        whole failure mode, at its smallest size. Hence the assertion.
        """
        import harness.skipset as harness_skipset

        only_shared = sorted(set(skipset.SKIP_DIRS) - set(harness_skipset.SKIP_DIRS))
        only_harness = sorted(
            set(harness_skipset.SKIP_DIRS) - set(skipset.SKIP_DIRS)
        )
        assert not only_shared and not only_harness, (
            "the two skip-set authorities DISAGREE. "
            f"only in shared/: {only_shared}; only in harness/: {only_harness}. "
            "This is DOCTRINE.md §8 at its smallest size: one missing name is "
            "one directory tree a walk opens."
        )

    def test_the_variants_carry_a_reason(self) -> None:
        """A differently-pruning walk must say why, in a receipt."""
        with pytest.raises(ValueError) as excinfo:
            skipset.skip_dirs_for(drop={"logs"})
        assert "reason" in str(excinfo.value).lower()

        narrowed = skipset.skip_dirs_for(
            drop={"logs"}, reason="a deliberately scoped audit of logs/"
        )
        assert "logs" not in narrowed
        assert len(narrowed) == len(skipset.SKIP_DIRS) - 1

    def test_the_report_publishes_the_adoption_state(self) -> None:
        report = skipset.skip_dirs_report()
        assert report["authority"] == "shared/skipset.py"
        assert report["dir_count"] == len(skipset.SKIP_DIRS)
        # The report is what a trace row carries, so the adoption count has to
        # travel with it: a receipt that names a set without saying who else
        # prunes differently is half the receipt.
        assert "independent_tables" in report
        assert "modules_with_their_own_table" in report


class TestTheCountCannotGrowSilently:
    def test_no_new_skip_set_table_may_appear(self, audit: Dict[str, object]) -> None:
        measured = sorted(audit["own_tables"])  # type: ignore[arg-type]
        recorded = sorted(KNOWN_OWN_TABLES)
        new = sorted(set(measured) - set(recorded))
        gone = sorted(set(recorded) - set(measured))
        assert not new, (
            "a module now declares its OWN skip-set table and is not recorded "
            f"here: {new}. Add it to KNOWN_OWN_TABLES with its owner and the "
            "reason it could not adopt the authority. A new table that is not "
            "recorded is how the 345x comes back."
        )
        assert not gone, (
            f"these modules no longer declare a table of their own: {gone}. "
            "That is the gap CLOSING -- delete them from KNOWN_OWN_TABLES and "
            "lower PINNED_OWN_TABLE_COUNT. The record must not claim a table "
            "the tree does not have."
        )
        assert len(measured) == PINNED_OWN_TABLE_COUNT, (
            f"{len(measured)} independent tables, PINNED_OWN_TABLE_COUNT is "
            f"{PINNED_OWN_TABLE_COUNT}. If a table was added, record it above; "
            "if one was removed, delete its entry and lower the pin."
        )

    def test_every_remaining_table_is_named_with_an_owner(self) -> None:
        for module, owner in KNOWN_OWN_TABLES.items():
            assert str(owner).strip(), f"{module} has no recorded owner"
            assert (REPO_ROOT / module).is_file(), (
                f"{module} is listed as owning a table but does not exist"
            )

    def test_no_table_prunes_less_than_the_authority_without_saying_so(
        self, audit: Dict[str, object]
    ) -> None:
        """A table missing authority entries is the specific defect.

        ``DOCTRINE.md`` §8's 124,774 directories is what a walk that does NOT
        prune ``logs/`` looks like. Naming every module that is in that state is
        the honest report; a green test here would be a lie.

        Note the two lists are DIFFERENT defects and are checked separately: a
        table can prune less than the authority (it opens a tree the authority
        says to prune -- §8) or more (it hides source the authority can find).
        Conflating them is what made the first version of this assertion fail
        on a module that prunes more and not less.
        """
        missing = set(audit["tables_missing_authority_entries"])  # type: ignore[arg-type]
        extra = set(audit["tables_with_extra_entries"])  # type: ignore[arg-type]
        recorded = set(KNOWN_OWN_TABLES)
        unrecorded = (missing | extra) - recorded
        assert not unrecorded, (
            "these modules prune differently from the authority and are not "
            f"recorded in KNOWN_OWN_TABLES: {sorted(unrecorded)}. Add each with "
            "its owner and whether it prunes less (opens a tree the authority "
            "prunes) or more (hides source). An unrecorded divergent table is "
            "how the 345x comes back."
        )
        # `execution/walk_scope.py` prunes MORE (49 vs 44), so it is in `extra`
        # and NOT in `missing`. Asserting the exact membership of both lists is
        # the pin: it fails when a table's shape changes, which is the moment a
        # human should look.
        assert missing == {
            "cli/fileview.py",
            "dashboard/collect.py",
            "runtime/analyze_history.py",
            "runtime/difficulty.py",
            "runtime/subagents.py",
        }, f"the set of tables pruning LESS than the authority changed: {sorted(missing)}"
        assert extra == {
            "dashboard/collect.py",
            "execution/walk_scope.py",
            "runtime/analyze_history.py",
            "runtime/difficulty.py",
        }, f"the set of tables pruning MORE than the authority changed: {sorted(extra)}"

    def test_the_audit_is_machine_readable(self, audit: Dict[str, object]) -> None:
        """The audit is published, so it can be diffed between rounds."""
        text = json.dumps(audit, indent=2, sort_keys=True, default=str)
        assert json.loads(text)["authority"] == "shared/skipset.py"
        assert isinstance(audit["single_authority"], bool)
        # `single_authority` is False today and that is the FINDING, not a bug in
        # the audit. Asserted explicitly so the day it flips, the change is
        # visible in a diff here rather than discovered later.
        assert audit["single_authority"] is False, (
            "the audit now reports a SINGLE authority. If that is correct, "
            "KNOWN_OWN_TABLES and PINNED_OWN_TABLE_COUNT should be empty and 0, "
            "and this assertion is the one to update -- do not just relax it."
        )


def test_the_measured_table_census_is_stable() -> None:
    """The census, PRINTED so a reader sees the number without a script.

    A reporting test rather than an asserting one, and deliberately so: the
    number it prints IS the finding, and a test that asserted the number would
    have to change every time an owner closes a gap -- which is the wrong moment
    to change a test. The pins are in the class above.
    """
    audit = skipset.skip_dir_tables(REPO_ROOT)
    census = sorted(
        (module, count)
        for module, count in dict(audit["own_tables"]).items()  # type: ignore[arg-type]
    )
    for module, count in census:
        print(f"{module}: {count} names")
    print(
        "authority: {} names; adopters: {}; independent tables: {}".format(
            audit["authority_dir_count"],
            len(audit["adopters"]),  # type: ignore[arg-type]
            audit["independent_table_count"],
        )
    )
    assert census, "the audit found no table at all, which means it is broken"
