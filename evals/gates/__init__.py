"""Phase gates.

One module per phase gate. `P0` is the G0 gate: the P0 Foundation verdict that
every later phase's prompt refers back to. `P1` is the G1 gate (Phase 1 Daily
Usable). `P2` is the G2 gate (Phase 2 Trusted Execution). `P3` is the **G3a**
gate (Phase 3a Memory & Compaction).

`P1`, `P2` and `P3` all IMPORT the vocabulary from `P0` rather than re-declaring
it. Two lists is how `skip` gets added back: the second list is written later,
by a terminal that no longer remembers why the first one was closed.

The vocabulary is enforced in `P0.STATUSES` and there is deliberately no
member of it that a reader could mistake for a pass:

    pass             ran and held
    fail             ran and broke
    blocked          could not run -- WITH THE EXACT REASON
    not_implemented  does not exist -- WITH THE OWNING PHASE

`skip` is not a member. A skip is indistinguishable from a pass in a summary
table, which is the precise dishonesty this repository exists to prevent
(`phases/DOCTRINE.md` §1, "Report a blocked lane as skipped" is the Never-do
row). `P0` refuses to construct a blocked rung without a reason, and its own
test suite asserts the word appears nowhere in the vocabulary.

One deliberate difference in how the gates treat an ABSENT lane, recorded here
because two rules in one package is how the next reader assumes there is only
one: `G0` and `G1` redden only on a blocking `fail`; **`G2` and `G3a` also
redden on a blocking `blocked`**. The reasoning is specific to the claims those
gates make -- a lane that could not run is precisely the lane that would have
falsified a trust or a retrieval guarantee -- so a green gate there whose
decisive measurement never ran is the exact dishonesty the vocabulary exists to
prevent. `P3.g3a_report`'s docstring states the rule again at the point of use.
"""

__all__ = ["P0", "P1", "P2", "P3"]
