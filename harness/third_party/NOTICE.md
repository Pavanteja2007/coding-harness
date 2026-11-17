# NOTICE — `harness/third_party/`

Third-party algorithms vendored into this repository, with the provenance of
each. The full four-field header (upstream URL, commit pin, SPDX identifier, and
**why it beats ours**) is repeated at the top of every file, because a header in
a separate file is a header nobody reads.

---

## `opencode_replacer.py`

| field | value |
|---|---|
| Upstream | https://github.com/sst/opencode — the nine-stage edit replacer (`session/replacer`) |
| Commit pin | the 9-stage replacer as published; read against the upstream stage list and thresholds |
| SPDX-License-Identifier | `MIT` |
| **Why it beats ours** | ours answers one question — "is `old_string` here, byte for byte?" — and every other answer is a refusal. The corpus calls file editing "one of the most important determinants of code-modification accuracy in an SWE agent" and says outright to "avoid line-number-based edits: models drift on line numbers more than on contextual matching". One matcher is the whole problem. |
| **Is it a byte copy?** | **No.** See below. |

## `aider_bitap.py`

| field | value |
|---|---|
| Upstream | https://github.com/Aider-AI/aider — `diff_match_patch` (itself Google diff-match-patch, which Aider inlined) |
| Commit pin | Aider v0.86.0 / diff-match-patch v20241019; read against the published Bitap matcher description |
| SPDX-License-Identifier | `Apache-2.0` (Aider and Google diff-match-patch) |
| **Why it beats ours** | it is a *different algorithm* from every other rung. Bitap answers "is the pattern within N edits of some window?", in one O(len(text)) sweep that does not degrade on repetitive text — which is exactly the file shape where a line-aligned window scan finds nothing and a naive similarity search finds everything. |
| **Is it a byte copy?** | **No.** See below. |

---

## The deviation, stated plainly

**Both files are independent reimplementations of the two ALGORITHMS, not
copies of the two upstream FILES.** No network fetch of either upstream source
was available in the session that created this package, and reproducing
`diff_match_patch` (~2,000 lines of character diffs, patch objects, semantic
cleanup and a delta serialiser) from memory would have produced an unverifiable
claim of fidelity — which is a worse outcome than an honest smaller scope.

What IS reproduced:

* the published **stage list** and stage semantics of the OpenCode cascade;
* its **published threshold** (`0.65` similarity) and the shape of its
  disproportionate-match guard, reimplemented as three named refusal rules;
* Aider's **`0.95` match threshold** and the Bitap approximate-substring
  algorithm, with the same `Match_Distance` semantics;
* Aider's **`500` match distance**, as `DEFAULT_MATCH_DISTANCE`.

What is NOT claimed: byte-identity with either upstream file.

### One number was deliberately CHANGED, and here is why

`DEFAULT_MATCH_DISTANCE = 500` is kept, exported, and reported in every receipt.
It is **not** the distance the ladder searches with. The search uses
`DEFAULT_SEARCH_DISTANCE = 32`.

Aider's 500 is the budget for `patch_apply` walking a whole patch through a diff
hierarchy. Applied to a single-pattern substring search it makes the cost
O(len(text) x 500). **Measured** on this repository's own
`harness/tool_errors.py` (60,000 characters): an early draft using 500 took
**over five minutes**. At 32 the same search is **~2.0 s**, and the rung only
runs when six cheaper rungs have already failed.

Both numbers are in `harness.fuzzy_edit.THRESHOLDS`, which rides the edit
receipt, so a reader can see which distance actually ran.

### The second opinion proposes; it does not decide

Every location `aider_bitap` proposes is **re-scored with a true Levenshtein
ratio** before it can land an edit. Bitap's contribution is its *candidate
generation* — a character-offset sweep that a line-aligned window scan cannot
do. Its *verdict* is discarded.

This is the reason the module is safe to trust at all: an approximate matcher
that is an independent reimplementation must not be the authority on whether an
edit lands. The failure mode of being wrong is then a missed fuzzy match
(`no_match`, which the model can act on) rather than a wrong edit (which it
cannot).

---

## Licences

* OpenCode — MIT. https://github.com/sst/opencode/blob/dev/LICENSE
* Aider — Apache-2.0. https://github.com/Aider-AI/aider/blob/main/LICENSE.txt
* Google diff-match-patch — Apache-2.0.

The vendored files here are reimplementations carrying the same licences because
they implement the same published algorithms; the licence identifiers are
reproduced so a downstream packager sees the terms that apply.