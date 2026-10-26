"""The live interpreter and the file on disk must agree about every default.

WHAT THIS PINS, AND WHY IT HAD TO BE WRITTEN
--------------------------------------------
This file exists because it caught a measurement being wrong for an entire
session, silently.

``harness/config.py`` carries ``"agent_max_turns": 60``. It also carried ``25``
until a concurrent terminal rewrote it during Phase 1 Wave 2. **Both literals
are two characters long**, so the edit did not change the file's byte size, and
it landed inside the same filesystem-mtime granularity second. CPython decides
whether ``__pycache__/config.cpython-310.pyc`` is still valid by comparing
``(source mtime, source size)`` -- so it matched, it did not recompile, and
every process in this repository executed the OLD bytecode.

The measured symptom, on this host, with the pyc in place::

    python -c "import harness.config as c; print(c.DEFAULTS['agent_max_turns'])"
    25                       # what the product actually ran
    # and compiling the very same file from source, with no cache involved:
    60                       # what the source says

That is the worst shape a measurement defect can take: it is invisible, it is
in the *direction* of the older value, and it makes a Phase 1 claim (the 2.4x
turn-ceiling cost increase) read as though Phase 1 never happened. The Trust
Ladder reads this exact key through ``evals.trust_ladder_rungs.declared_turn_cap``,
so rung #8's ceiling check was reading the stale number too.

WHAT THIS TEST DOES
-------------------
It reads the same dict **two independent ways** and requires them to agree:

1. by importing it and asking the live interpreter (``DEFAULTS``);
2. by parsing the source file with :mod:`ast` -- no import, no bytecode, no
   ``__pycache__`` in the path at all.

Agreement between an imported value and a value parsed out of the source is the
property that a stale pyc breaks, and it is the only way to catch it without
deleting caches (deleting them is a workaround, not a check: the collision
recreates itself the next time somebody edits a number to another number of the
same width).

The pin covers the whole ``DEFAULTS`` mapping rather than the one key that
happened to be stale, because the defect is not about ``agent_max_turns``: it is
that *any* same-width edit inside one mtime second is invisible.

THE MIRROR IN THE OTHER DIRECTION
---------------------------------
Two modules that both read a config file are the same hazard even without a
cache: ``harness/editor.py:1545`` returns ``DEFAULTS[key]`` from its own import.
A second consumer of the same mapping is not a second authority here, so that is
recorded rather than pinned.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "harness" / "config.py"

#: Keys whose SOURCE form is not a literal the AST reader can evaluate. Every
#: one is named here rather than silently skipped, because a parser that skips
#: what it cannot read is how "this file is clean" becomes "I could not read this
#: file".
_NOT_LITERAL: Dict[str, str] = {
    # These are computed/merged after the literal (harness/config.py's own
    # module-level post-processing), so the AST projection legitimately cannot
    # see them. Listed rather than hidden.
}


def _defaults_from_source(path: Path = CONFIG_PATH) -> Tuple[Dict[str, Any], List[str]]:
    """Return ``(defaults, unreadable)`` by PARSING the file. Never imports it.

    Deliberately stdlib ``ast`` and nothing else. A regex would mis-read a
    comment; importing would hit the very cache this exists to audit.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    defaults: Dict[str, Any] = {}
    unreadable: List[str] = []
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign):
            continue
        target = node.target
        if not (isinstance(target, ast.Name) and target.id == "DEFAULTS"):
            continue
        if node.value is None or not isinstance(node.value, ast.Dict):
            continue
        for key_node, value_node in zip(
            node.value.keys, node.value.values, strict=False
        ):
            if not isinstance(key_node, ast.Constant) or not isinstance(
                key_node.value, str
            ):
                unreadable.append("<non-literal key>")
                continue
            try:
                defaults[key_node.value] = ast.literal_eval(value_node)
            except (ValueError, SyntaxError, TypeError):
                unreadable.append(key_node.value)
        break
    return defaults, unreadable


class TestTheLiveInterpreterAgreesWithTheFileOnDisk:
    """A stale ``__pycache__`` is invisible; this is the only thing that sees it."""

    def test_the_defaults_mapping_is_readable_from_source_at_all(self) -> None:
        from_source, unreadable = _defaults_from_source()
        assert len(from_source) > 100, (
            "the AST projection of harness/config.py::DEFAULTS collapsed to "
            f"{len(from_source)} keys; a projection that reads almost nothing "
            "cannot be used to audit the real mapping"
        )
        # A key the parser could not evaluate is either a non-literal (named in
        # _NOT_LITERAL) or a defect in the projection. Both are reported.
        unexpected = sorted(set(unreadable) - set(_NOT_LITERAL))
        assert not unexpected, (
            "these DEFAULTS keys are not literals this projection can evaluate, "
            f"so they are NOT covered by this pin: {unexpected}. Either add them "
            "to _NOT_LITERAL with a reason, or the projection is wrong."
        )

    def test_the_live_defaults_match_the_source_byte_for_byte(self) -> None:
        import harness.config as config_mod

        from_source, _ = _defaults_from_source()
        live: Dict[str, Any] = dict(config_mod.DEFAULTS)
        source_keys = set(from_source)
        live_keys = set(live)

        missing = sorted(source_keys - live_keys)
        extra = sorted(live_keys - source_keys)
        assert not missing, (
            "the live DEFAULTS is missing keys the source declares: "
            f"{missing}. A key that vanished between the file and the "
            "interpreter is a stale __pycache__, not a refactor."
        )
        assert not extra, (
            "the live DEFAULTS carries keys the source does not declare: "
            f"{extra}. Something mutates the mapping at import time; name it."
        )

        differing = {
            key: (from_source[key], live[key])
            for key in sorted(source_keys)
            if from_source[key] != live[key]
            or type(from_source[key]) is not type(live[key])
        }
        assert not differing, (
            "the live DEFAULTS disagrees with harness/config.py on "
            f"{len(differing)} key(s): {differing}\n"
            "This is what a stale __pycache__ looks like: an edit that changed "
            "a value to another value of the SAME byte length, inside the same "
            "mtime second, leaves CPython's (mtime, size) validity check "
            "matching, so the old bytecode keeps running. The value the product "
            "executes is then not the value the source declares. Fix by "
            "recompiling (`python -m compileall -f harness`) -- the pin exists "
            "so the next occurrence is noticed rather than measured."
        )

    def test_the_specific_key_the_trust_ladder_reads(self) -> None:
        """Rung #8's ceiling check reads this exact key. Pin it by name.

        A general "everything agrees" pin is easy to weaken by skipping a key.
        This one names the key the ladder depends on, so weakening the pin above
        does not silently disarm rung #8.
        """
        import harness.config as config_mod

        from_source, _ = _defaults_from_source()
        assert "agent_max_turns" in from_source, (
            "agent_max_turns left DEFAULTS; the Trust Ladder's declared_turn_cap "
            "reads it and would report the ceiling as unreadable"
        )
        assert live_matches(config_mod, from_source, "agent_max_turns")


def live_matches(config_mod: Any, from_source: Dict[str, Any], key: str) -> bool:
    """Whether one key agrees between the live mapping and the parsed source."""
    return from_source.get(key) == config_mod.DEFAULTS.get(key)


@pytest.mark.parametrize(
    "key",
    [
        # The ceilings Phase 1 doubled. Each one is a spend number, so each one
        # is a claim a reader will quote, so each is pinned by name.
        "agent_max_turns",
        "session_max_turns",
        "budget_cap_usd",
        "max_wallclock_s",
        "max_retries",
    ],
)
def test_a_spend_related_default_is_not_a_stale_bytecode(key: str) -> None:
    import harness.config as config_mod

    from_source, _ = _defaults_from_source()
    assert key in from_source, f"{key} is not a literal in harness/config.py"
    assert live_matches(config_mod, from_source, key), (
        f"{key} is {config_mod.DEFAULTS.get(key)!r} in the live interpreter and "
        f"{from_source.get(key)!r} in the source. A stale __pycache__ makes the "
        "product run a number the file does not declare, and this key is spend."
    )
