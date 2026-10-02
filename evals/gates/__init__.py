"""Phase gates.

One module per phase gate. `P0` is the G0 gate: the P0 Foundation verdict that
every later phase's prompt refers back to.

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
"""

__all__ = ["P0"]
