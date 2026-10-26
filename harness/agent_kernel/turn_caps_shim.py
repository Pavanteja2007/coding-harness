"""The ``harness.agent_kernel`` -> ``harness.turn_caps`` seam.

Why this module exists rather than a bare ``from ..turn_caps import``
------------------------------------------------------------------
``harness/turn_caps.py`` is the ONE authority for the turn caps, and P1/W2-T1
found a **fifth hard-coded fallback** that Wave 1's sweep missed:

    harness/agent_kernel/kernel.py:465
        max_turns=int(self.config.get("session_max_turns", 24))

Wave 1 deleted four hard-coded ``25`` literals and pinned their absence. This
one is the same defect wearing a different number: a literal that is correct
today and drifts the day :data:`harness.turn_caps.DEFAULT_SESSION_MAX_TURNS`
moves, with nothing in the tree noticing and no failure to trace it to. The
value 24 and the constant 24 agree **by coincidence**, which is exactly what
makes it durable.

Why a shim instead of importing directly
----------------------------------------
Direction of dependency. ``harness/turn_caps.py`` is a leaf: it imports only
``dataclasses`` and ``typing``. ``harness/agent_kernel/*`` is above it. A direct
relative import would be fine today and would become a cycle the day
``turn_caps`` ever needs to name a kernel type - which it will, the moment
turn-cap provenance needs to distinguish a per-task cap from a context-summary
cap in a receipt that names the kernel.

So the shim is one function with one job: resolve the context-SUMMARY cap
through the authority, accept BOTH key names (the historical
``session_max_turns`` and the honest alias
``session_context_summary_turns``), and return an int the ``ContextBuilder``
constructor can take directly. It deliberately exposes **no** other turn-caps
name, so a second reader cannot grow here.

The cap this resolves does NOT bound the conversation. It bounds how many prior
context-summary records the compiler keeps. That distinction is the entire
reason :mod:`harness.turn_caps` exists, and the wrapper name says which one it
is so the next reader of ``kernel.py:465`` cannot mistake it.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .. import turn_caps

__all__ = ["resolve_context_summary_turns"]


def resolve_context_summary_turns(
    config: Optional[Mapping[str, Any]] = None,
) -> int:
    """Return how many prior context-summary records the compiler may keep.

    Reads :func:`harness.turn_caps.resolve_caps`, so the default comes from
    :data:`harness.turn_caps.DEFAULT_SESSION_MAX_TURNS` and no literal can be
    written here. ``ContextBuilder`` requires a positive int, so the resolved
    value is floored at 1: a cap of zero context records would make the compiler
    unable to hold its own summary, and a context builder that cannot summarise
    is not a configuration.

    **This is not the conversation cap.** ``session_max_conversation_turns`` is
    the conversation bound and it is unbounded by default; nothing here reads it,
    because ``ContextBuilder`` has no conversation to bound.
    """
    caps = turn_caps.resolve_caps(config)
    return max(1, int(caps.context_summary))
