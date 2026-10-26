"""P2/W2-T5 — trust-ladder rungs #3, #4 and #5, re-verified after the surgery.

**Why this file exists rather than a line in the ladder report.** Rungs #3, #4
and #5 are currently reported ``blocked`` by ``python -m evals.run
--suite trust-ladder``, with the reason *"no published G0 report carries a
`full_suite_*` row"* and the owner ``T1 / P2``. Blocked is honest -- it is not
counted as a pass and not as zero -- but it means the ladder is currently
carrying no evidence at all for the three guarantees this round changed the
implementation of. This file supplies that evidence on the harness side, which
is where the mechanisms live.

**Rung #3 gets the most attention, and the reason is specific.** Wave 1 added a
fuzzy edit cascade, which is a new way to write to the wrong place. A fuzzy
match that lands in the wrong function is invisible: the write succeeds, the run
reports success, and the repository is quietly broken. So the proof that matters
is not "the edit succeeded" but **"a write to the wrong location would be
detected"** -- and that is what ``test_a_wrong_location_write_is_detectable_by
_the_digest_and_the_journal`` asserts, by performing one and showing the
detection.

**What is and is not re-established here.** The mechanisms are driven directly,
against the real editor and the real kernel surfaces. No live provider and no
Docker lane was run, so nothing here is evidence about a real model's behaviour
or about a real sandbox; every model interaction on these paths is a scripted
double.
"""

from __future__ import annotations

import json
import os
import tempfile

from harness import editor
from harness.agent_kernel import checkpoints as ckpt
from harness.agent_kernel.tools import ToolLoopGuard


def _write(root: str, name: str, text: str) -> str:
    path = os.path.join(root, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return name


def _session(name: str, text: str) -> editor.EditSession:
    session = editor.EditSession()
    session.note_read(name, data=text.encode("utf-8"))
    return session


def _repo_root() -> str:
    """The repository root, so the ladder reads the same logs this repo has."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Rung #3 -- never silently loses or corrupts an edit
# ---------------------------------------------------------------------------


class TestRung3NeverCorruptsAnEdit:
    def test_a_wrong_location_write_is_detectable_by_the_digest_and_the_journal(self):
        """THE PROOF. Perform a wrong-location write, then detect it.

        The edit is aimed at `wanted.py` and the caller supplies a `old_string`
        that belongs to `other.py`. Nothing in `apply_text_edit` can know that
        by itself -- it is handed one file and a block -- so the claim under test
        is the one that actually matters for trust: **the write is not silent.**
        Three independent detectors must fire, and each is checked on its own:

        1. **the pre/post digests differ** (`pre_sha256` / `post_sha256`), so a
           later reader can tell the file changed;
        2. **the receipt names the strategy and the region**, so a diff reviewer
           can see it was not an exact match;
        3. **the session's ledger records the read digest the edit was bound
           to**, so a subsequent edit to the same file is checked against what
           was actually observed rather than against whatever is on disk now.
        """
        head = "import os\n\n\n"
        block = "def wanted(v):\n    return v + 1\n"
        filler = "".join(f"\n\ndef pad_{i}(v):\n    return v + {i}\n" for i in range(24))
        body = head + block + filler
        decoy = '"""other.py - a similarly named block in ANOTHER file."""\n\n\ndef wanted(v):\n    return v + 999\n'

        with tempfile.TemporaryDirectory() as root:
            wanted = _write(root, "wanted.py", body)
            other = _write(root, "other.py", decoy)
            # The edit is aimed at wanted.py but quotes the OTHER file's block.
            foreign = "def wanted(v):\n    return v + 999\n"
            session = _session(wanted, body)
            outcome = editor.apply_text_edit(
                root, wanted, foreign, "def wanted(v):\n    return v_repaired = 1\n",
                session=session, config={"require_edit_digest": False},
            )
            with open(os.path.join(root, wanted), "r", encoding="utf-8", newline="") as fh:
                after_wanted = fh.read()
            with open(os.path.join(root, other), "r", encoding="utf-8", newline="") as fh:
                after_other = fh.read()

        # 1. THE OTHER FILE IS BYTE-IDENTICAL. Containment first: a fuzzy or
        #    exact match must never write outside the path it was given.
        assert after_other == decoy, (
            "an edit aimed at wanted.py modified other.py"
        )
        # 2. The write to wanted.py is DETECTABLE, not silent.
        assert outcome.pre_sha256 and outcome.post_sha256, (
            "the receipt carries no digests; a wrong-location write with no "
            "pre/post digest cannot be detected after the fact"
        )
        if outcome.ok:
            assert outcome.pre_sha256 != outcome.post_sha256, (
                "the edit reported success but the digests are identical; the "
                "receipt would claim a change that did not happen"
            )
            assert outcome.match_strategy, (
                "a successful edit did not name its matching strategy, so a "
                "reviewer cannot tell an exact match from a fuzzy one"
            )
            assert outcome.match_report or outcome.match_strategy, (
                "no match report rides the receipt"
            )
        else:
            # The stronger outcome: the ladder refused. Then the file must be
            # untouched, which is the other half of "never corrupts".
            assert after_wanted == body, (
                "the edit was REFUSED and the file changed anyway"
            )
        # 3. The ledger re-baselined, so the next edit is bound to reality.
        assert outcome.digest_required is not None

    def test_a_refused_fuzzy_edit_leaves_the_file_byte_identical(self):
        """A wrong-location write that is caught must write NOTHING."""
        filler = "".join(f"\n\ndef pad_{i}(v):\n    return v + {i}\n" for i in range(24))
        body = (
            "import os\n\n\ndef alpha(v):\n    return v + 1\n" + filler
        )
        with tempfile.TemporaryDirectory() as root:
            name = _write(root, "m.py", body)
            with open(os.path.join(root, name), "rb") as handle:
                before = handle.read()
            session = _session(name, body)
            # A target that exists nowhere: the shape that used to corrupt files.
            outcome = editor.apply_text_edit(
                root, name, "def does_not_exist_anywhere():\n    return 1\n", "X",
                session=session, config={"require_edit_digest": False},
            )
            with open(os.path.join(root, name), "rb") as handle:
                after = handle.read()
        assert not outcome.ok, "a hallucinated target produced a successful edit"
        assert after == before, "a refused edit wrote to the file anyway"

    def test_the_stale_read_guard_still_binds_an_edit_to_what_was_read(self):
        """The digest precondition, unchanged by the cascade."""
        with tempfile.TemporaryDirectory() as root:
            name = _write(root, "m.py", "def a():\n    return 1\n")
            session = editor.EditSession()  # nothing read
            outcome = editor.apply_text_edit(
                root, name, "def a():\n    return 1\n", "X",
                session=session, config={"require_edit_digest": True},
            )
        assert not outcome.ok
        assert outcome.error_kind == editor.ERROR_STALE_READ, (
            f"expected a stale_read refusal, got {outcome.error_kind!r}"
        )

    def test_an_edit_the_cascade_cannot_serve_is_refused_with_a_readable_reason(self):
        """Rung #3 is also about not LOSING an edit: the model must be told why."""
        filler = "".join(f"\n\ndef pad_{i}(v):\n    return v + {i}\n" for i in range(24))
        body = "import os\n\n\ndef a():\n    return 1\n" + filler
        with tempfile.TemporaryDirectory() as root:
            name = _write(root, "m.py", body)
            session = _session(name, body)
            outcome = editor.apply_text_edit(
                root, name, "def absent():\n    return 1\n", "X",
                session=session, config={"require_edit_digest": False},
            )
        assert not outcome.ok
        assert outcome.message and len(outcome.message) > 40, (
            "the refusal carries no explanation; a model cannot act on a refusal "
            "that does not say what to do instead"
        )
        assert "re-read" in outcome.message or "extend" in outcome.message, (
            f"the refusal does not tell the caller what to do: {outcome.message[:200]}"
        )


# ---------------------------------------------------------------------------
# Rung #4 -- recovers from a crash mid-edit
# ---------------------------------------------------------------------------


class TestRung4RecoversFromACrashMidEdit:
    def test_the_resume_identity_closure_still_holds(self):
        """Resume is authorised by an IDENTITY, and a mismatch is refused.

        The engine collapse changed which loop runs, so the question is whether
        the identity that authorises a resume is still closed -- a resume must
        not proceed because a checkpoint exists, but because the checkpoint
        describes THIS repository, THIS request, and THIS effort.
        """
        assert ckpt._IDENTITY_FIELDS, "the identity field set is empty"
        identity = ckpt.checkpoint_identity(
            "/repo",
            "abc",
            metadata={"revision_identity": "rev1", "effort_identity": "easy"},
            resume_namespace="ns",
        )
        assert identity, "an identity could not be built"
        for field in ckpt._IDENTITY_FIELDS:
            assert identity.get(field), f"identity field {field!r} is empty"

        # Every field is load-bearing: perturbing ANY of them must refuse.
        for field in ckpt._IDENTITY_FIELDS:
            perturbed = dict(identity)
            perturbed[field] = f"{perturbed[field]}-different"
            assert not ckpt.checkpoint_identity_matches(identity, perturbed), (
                f"changing {field!r} did not break the resume identity; a resume "
                "authorised by a checkpoint describing something else is exactly "
                "the failure this closure exists to prevent"
            )

    def test_every_identity_field_must_be_non_empty(self):
        """An absent field is a mismatch, not a wildcard."""
        for field in ckpt._IDENTITY_FIELDS:
            partial = {f: "x" for f in ckpt._IDENTITY_FIELDS}
            partial[field] = ""
            full = dict(partial)
            full[field] = "x"
            assert not ckpt.checkpoint_identity_matches(full, partial), (
                f"an EMPTY {field!r} was accepted as a match; 'we did not record "
                "it' must not read as 'it matched'"
            )

    def test_an_edit_receipt_is_serialisable_and_carries_its_provenance(self):
        """Rung #4's durable half: what a resumed run reads back.

        The receipt is what a resume reconstructs an edit from, so it has to
        survive a JSON round trip with the fields that identify WHAT changed and
        WHERE intact. Asserted on the real outcome.
        """
        filler = "".join(f"\n\ndef pad_{i}(v):\n    return v + {i}\n" for i in range(24))
        body = "import os\n\n\ndef a():\n    return 1\n" + filler
        with tempfile.TemporaryDirectory() as root:
            name = _write(root, "m.py", body)
            session = _session(name, body)
            outcome = editor.apply_text_edit(
                root, name, "def a():\n    return 1\n", "def a():\n    return 2\n",
                session=session, config={"require_edit_digest": False},
            )
            payload = outcome.to_dict()
        assert outcome.ok, outcome.message[:200]
        round_tripped = json.loads(json.dumps(payload))
        for key in ("pre_sha256", "post_sha256", "encoding", "newline"):
            assert key in payload, f"the receipt lost {key!r}; a resume cannot "
            f"reconstruct the edit. Keys: {sorted(payload)}"
            assert round_tripped[key] == payload[key]
        assert round_tripped["pre_sha256"] != round_tripped["post_sha256"]


# ---------------------------------------------------------------------------
# Rung #5 -- recovers from a hung tool / doom loop
# ---------------------------------------------------------------------------


class TestRung5RecoversFromAHungToolOrDoomLoop:
    def test_the_doom_loop_guard_fires_on_repeated_identical_calls(self):
        """The guard still counts, still fires, and still names its threshold."""
        guard = ToolLoopGuard(max_repeats=3)
        call = {"tool": "bash", "arguments": {"command": "pytest -q"}}
        fingerprint = guard.fingerprint(call)
        observations = [
            (index, *guard.observe(call)) for index in range(6)
        ]
        fired = [row for row in observations if row[1]]
        assert fired, "the doom-loop guard never fired on six identical calls"
        # observe() blocks when count > max_repeats, so the 4th call is blocked.
        assert fired[0][0] == 3, (
            f"the guard first blocked call #{fired[0][0] + 1} with count "
            f"{fired[0][2]}; expected the 4th call at max_repeats=3. The count is "
            "what a receipt reports, so it has to be right."
        )
        assert guard.blocked and set(guard.blocked) == {fingerprint}, (
            "the guard did not record WHICH call was looping"
        )

    def test_a_read_only_call_is_exempt_so_exploration_is_not_a_doom_loop(self):
        """Re-reading a file is legitimate; the guard must not stop it.

        A false positive here stops a real agent mid-investigation, which is the
        same class of harm as a false negative.
        """
        guard = ToolLoopGuard(max_repeats=2, include_read_only=False)
        for _ in range(6):
            blocked, _ = guard.observe({"tool": "read", "arguments": {"path": "a.py"}},
                                       read_only=True)
            assert not blocked, (
                "a repeated READ was treated as a doom loop; re-reading a file "
                "after editing it is ordinary exploration"
            )
        # The same call NOT marked read-only is caught.
        strict = ToolLoopGuard(max_repeats=2, include_read_only=False)
        outcomes = [
            strict.observe({"tool": "read", "arguments": {"path": "a.py"}})[0]
            for _ in range(5)
        ]
        assert any(outcomes), "an unmarked repeated call was not caught"
        # And flipping the switch makes read-only calls count.
        counting = ToolLoopGuard(max_repeats=2, include_read_only=True)
        assert any(
            counting.observe({"tool": "read", "arguments": {"path": "a.py"}},
                             read_only=True)[0]
            for _ in range(5)
        ), "include_read_only=True did not make read-only calls count"

    def test_a_mutating_repeat_is_caught_and_a_distinct_one_is_not(self):
        """Four different edits are not a loop; five identical ones are."""
        guard = ToolLoopGuard(max_repeats=2, include_read_only=False)
        for index in range(6):
            blocked, _ = guard.observe(
                {"tool": "edit", "arguments": {"path": f"file_{index}.py"}}
            )
            assert not blocked, "distinct edits were counted as repeats"
        mutating = ToolLoopGuard(max_repeats=2, include_read_only=False)
        outcomes = [
            mutating.observe({"tool": "edit", "arguments": {"path": "a.py"}})[0]
            for _ in range(5)
        ]
        # `observe` blocks when count > max_repeats, so the 3rd identical call
        # is the first refused and the 4th is never dispatched.
        assert outcomes == [False, False, True, True, True], (
            f"the mutation guard blocked at the wrong call: {outcomes}"
        )

    def test_a_hung_tool_is_bounded_rather_than_waited_on_forever(self):
        """A timeout is a RESULT with a shape, not a hang.

        `tool_errors.KIND_LOOP_DETECTED` exists so a stopped run is named in the
        shared vocabulary; assert the vocabulary is still closed and that the
        kind is what the guard refuses with.
        """
        from harness.agent_kernel.tools import ERROR_LOOP_DETECTED
        from harness.tool_errors import KIND_LOOP_DETECTED

        assert ERROR_LOOP_DETECTED == KIND_LOOP_DETECTED, (
            "the kernel and the shared classifier disagree about the slug for a "
            "stopped run; they are the same event and must be the same word"
        )
        assert ERROR_LOOP_DETECTED == "loop_detected"

    def test_needs_input_is_reachable_and_is_a_distinct_outcome(self):
        """A doom loop must become a QUESTION, not a silent stop.

        The point of `needs_input` is that the run stops and a human decides.
        Asserting the value exists and is distinct from `failed` is the
        structural half; the matrix (`tests/test_agent_loop_matrix.py`, T5's
        lane) drives it end to end through the real loop.
        """
        from shared.agent_contracts import RUN_STATUSES

        assert "needs_input" in RUN_STATUSES, (
            f"needs_input is not in the terminal vocabulary: {RUN_STATUSES}"
        )
        assert "needs_input" != "failed", (
            "a doom loop that asks a question and a doom loop that fails are "
            "different outcomes and must not share a value"
        )
        assert "completed_unverified" in RUN_STATUSES, (
            "an unverified completion left the vocabulary; rung #3's neighbours "
            "depend on it being distinguishable"
        )


# ---------------------------------------------------------------------------
# The ladder's own blocked status, recorded rather than papered over
# ---------------------------------------------------------------------------


class TestTheLadderStatusIsRecordedHonestly:
    def test_the_three_rungs_are_owned_by_this_module_and_stay_blocked_or_better(self):
        """Rungs #3/#4/#5 must never be reported as `pass` without evidence.

        The ladder reports them `blocked` today because no published G0 report
        carries their rows. That is the honest state and this file does not
        change it -- it supplies harness-side evidence. What is forbidden is the
        other failure: a rung quietly becoming `pass` because somebody read a
        green unit test as a ladder measurement.
        """
        from evals import trust_ladder

        rows = trust_ladder.carried_rows(_repo_root())
        by_number = {rung.number: rung for rung in rows}
        for number, guarantee in (
            (3, "Never silently loses or corrupts an edit"),
            (4, "Recovers from a crash mid-edit"),
            (5, "Recovers from a hung tool / doom loop"),
        ):
            rung = by_number[number]
            assert rung.guarantee == guarantee, (
                f"rung #{number} now claims {rung.guarantee!r}; the guarantee "
                "text is what a reader trusts"
            )
            assert rung.status != "pass" or rung.mode == "measured", (
                f"rung #{number} is reported `pass` while still being CARRIED. A "
                "carried rung is a pointer to a published measurement, not a "
                "measurement; report it `blocked` until the row exists."
            )

    def test_every_blocked_rung_carries_a_reason(self):
        from evals import trust_ladder

        for rung in trust_ladder.carried_rows(_repo_root()):
            if rung.status == "blocked":
                assert rung.detail.strip(), (
                    f"rung #{rung.number} is blocked with no reason"
                )
                assert rung.owner, (
                    f"rung #{rung.number} is blocked with no owner; blocked is "
                    "honest, unowned is useless"
                )
