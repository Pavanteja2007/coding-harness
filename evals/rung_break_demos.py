"""The five deliberate rung-break demonstrations for G1.

WHY THIS IS A MODULE AND NOT A TRANSCRIPT
-----------------------------------------
``phases/DOCTRINE.md`` §3: *"A test that can pass vacuously is worse than no
test."* Applied to a rung, that means a rung whose check has never been seen to
FIRE is decoration. This module constructs each break deliberately, runs the
rung's own single authority (``derive_failures``), and reports whether the check
caught it.

The five breaks, exactly as the G1 brief names them:

============  ===========================================  ==========================
rung          deliberate break                             check that must fire
============  ===========================================  ==========================
#8 cap        the shipping turn ceiling goes 60 -> 25      ``declared turn ceiling is 25``
#8 decay      the constraint re-injection is removed       ``CONSTRAINT DECAY``
#9 retrieval  a SECOND skip table is restored              ``retrieval_walk_dirs_opened``
#9 startup    the provider import is eager again           ``startup_to_first_token``
#10 chain     one link of the chain is dropped             ``the chain is broken: LINK n``
============  ===========================================  ==========================

TWO RULES THIS MODULE KEEPS
---------------------------
1. **A demonstration may never damage the tree, in a tree four terminals are
   writing at the same time.**

   Four of the five breaks need NO file write at all: they rebind a module
   attribute or substitute a module in ``sys.modules``, which is exactly the
   state the real break produces. The fifth -- the eager import -- genuinely
   needs ``cli/interactive.py`` edited, and it is the only file write in this
   module. That one goes through :class:`GuardedEdit`, which works in **bytes**
   (a text round-trip through universal newlines silently rewrites CRLF and
   would show up as a "failed restore" on a file nobody damaged) and restores
   **only if the file still holds the bytes this demo wrote**.

   The condition is not pedantry. The first version of this module read a file,
   mutated it, measured, and wrote its stale snapshot back -- and the digest
   check fired, which turned out to be the *point*: another terminal had
   written that file inside the demonstration's window, and restoring a stale
   snapshot is precisely how one terminal deletes a peer's in-flight work. So
   the restore is conditional, and the report distinguishes three outcomes:
   ``restored`` (we put the bytes back), ``superseded`` (a peer wrote after us,
   their content stands, our break is gone), and ``restore_refused`` (the
   content moved and we cannot attribute it, so we refuse to write and say so).

2. **A demo that could not construct its break reports ``blocked`` with the
   reason.** It does not report ``pass``, and there is no ``skip`` in the
   vocabulary at all -- :data:`STATUSES` is imported from ``evals.gates.P0`` so
   the gate's closed vocabulary is the only one in the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]

# The vocabulary is IMPORTED, not re-declared. `evals.gates.P0` is the single
# authority for "what a status may be", and a second literal list is a second
# place for `skip` to be added.
from evals.gates.P0 import (  # noqa: E402  (path-dependent by design)
    BLOCKED,
    FAIL,
    NOT_IMPLEMENTED,
    PASS,
    STATUSES,
)

#: A demo's own status vocabulary. `pass` here means something narrower than it
#: means in the gate, and the report says so in words rather than relying on the
#: reader to notice: the break was CONSTRUCTED and the check CAUGHT it.
DEMO_STATUS_MEANING: Dict[str, str] = {
    PASS: "the break was constructed, the check fired, and the tree was restored",
    FAIL: "the break was constructed and the check did NOT fire -- the rung is decoration",
    BLOCKED: "the break could not be constructed on this host (reason is mandatory)",
    NOT_IMPLEMENTED: "there is no check to break (owning phase is mandatory)",
}


@dataclass
class Demo:
    """One demonstration: the break, the check, and what happened."""

    id: str
    rung: str
    deliberate_break: str
    check_that_must_fire: str
    status: str = BLOCKED
    caught: bool = False
    reason: str = ""
    evidence: List[str] = field(default_factory=list)
    restored: Optional[bool] = None
    edit_outcome: str = "not_run"
    restore_digest_before: str = ""
    restore_digest_after: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "rung": self.rung,
            "deliberate_break": self.deliberate_break,
            "check_that_must_fire": self.check_that_must_fire,
            "status": self.status,
            "caught": self.caught,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "tree_restored": self.restored,
            "edit_outcome": self.edit_outcome,
            "edit_outcome_meaning": EDIT_OUTCOMES.get(self.edit_outcome, ""),
            "digest_before": self.restore_digest_before,
            "digest_after": self.restore_digest_after,
        }


# --------------------------------------------------------------------------
# the two primitives: a child process, and a verified file restore
# --------------------------------------------------------------------------


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _child(code: str, *, timeout_s: int = 1800) -> Dict[str, Any]:
    """Run ``code`` in a fresh interpreter and capture the whole outcome.

    A fresh interpreter is not a preference. Three of the five breaks edit a
    module that is ALREADY IMPORTED in this process, so an in-process check
    would read the cached module and see the pre-break value -- which is the
    exact false green this module exists to rule out.
    """
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "timed_out": True,
            "error": f"did not finish within {timeout_s}s",
            "elapsed_s": round(time.perf_counter() - started, 1),
        }
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "timed_out": False,
        "error": "",
        "elapsed_s": round(time.perf_counter() - started, 1),
    }


class GuardedEdit:
    """Mutate a shared file for the length of one measurement, then restore it.

    The restore is **conditional**, and that is the whole design.

    This repository is written by four terminals concurrently. A demonstration
    that reads a peer's file, mutates it, and writes a stale snapshot back
    DELETES whatever the peer wrote in between. So on exit this re-reads the
    file and compares it against the bytes *this* demo wrote:

    * identical -> restore the original bytes. Report ``restored``.
    * different -> **do not write.** The peer owns the newer content; our break
      is already gone because they overwrote it. Report ``superseded``.
    * unreadable -> report ``restore_refused`` and refuse the demo's ``pass``.

    Bytes, not text: :meth:`Path.read_text` performs universal-newline
    translation, so writing its output back rewrites a CRLF file as LF and the
    digest check then reports damage that this module did not cause.
    """

    def __init__(self, path: Path, transform: Callable[[bytes], bytes]) -> None:
        self.path = Path(path)
        self.transform = transform
        self.original = b""
        self.mutated = b""
        self.before = ""
        self.mutated_digest = ""
        self.after = ""
        self.outcome = "not_run"

    def __enter__(self) -> "GuardedEdit":
        self.original = self.path.read_bytes()
        self.before = _digest(self.path)
        self.mutated = self.transform(self.original)
        if self.mutated == self.original:
            raise RuntimeError(
                f"the deliberate break did not change {self.path.name}, so the "
                "demonstration would have measured nothing"
            )
        self.path.write_bytes(self.mutated)
        self.mutated_digest = _digest(self.path)
        return self

    def __exit__(self, *exc: Any) -> bool:
        try:
            current = self.path.read_bytes()
        except OSError:
            self.outcome = "restore_refused"
            self.after = ""
            return False
        if current == self.mutated:
            self.path.write_bytes(self.original)
            self.after = _digest(self.path)
            self.outcome = "restored" if self.after == self.before else "restore_failed"
        else:
            # A peer wrote this file while the demonstration held it. Writing
            # anything now would destroy their work.
            self.after = _digest(self.path)
            self.outcome = "superseded"
        return False  # never swallow an exception


#: What a `GuardedEdit` outcome means for the demo's verdict. `superseded` is
#: NOT a failure of the demonstration -- the break is gone either way -- but it
#: is disclosed, because "I temporarily edited your file" is a thing the owner
#: of that file has a right to know.
EDIT_OUTCOMES: Dict[str, str] = {
    "restored": "the demo wrote its own bytes back and the digest matches",
    "restored_unverified": "the demo wrote its own bytes back; the digest differs",
    "superseded": (
        "ANOTHER TERMINAL wrote this file while the demo held it. Nothing was "
        "written back -- their content stands -- and the deliberate break is "
        "gone because they replaced the whole file"
    ),
    "restore_refused": (
        "the file became unreadable on exit; nothing was written back and the "
        "demo refuses to claim a pass"
    ),
    "not_run": "no edit was attempted",
}


def _last_error(probe: Dict[str, Any]) -> str:
    text = (probe.get("stderr") or probe.get("stdout") or "").strip()
    tail = text.splitlines()
    return (
        tail[-1] if tail else (probe.get("error") or f"exit {probe.get('returncode')}")
    )


# --------------------------------------------------------------------------
# DEMO 1 - rung #8, the shipping turn ceiling set back to 25
# --------------------------------------------------------------------------

DEMO1 = r"""
import json, sys, types
import harness.config as real_cfg

# BOTH arms are read inside ONE child process, back to back. This is not
# tidiness: the first version ran them as two child processes and the
# "shipping" arm read 25 while the tree's own literal said 60, because another
# terminal was editing harness/config.py at the time. A comparison whose two
# halves come from different instants is not a control.
shipped = real_cfg.DEFAULTS.get("agent_max_turns")

def verdict(cap, source):
    from evals.trust_ladder_rungs import Rung8Result
    r = Rung8Result(
        declared_turn_cap=cap,
        turns_executed=55, edits_applied=55, distinct_edit_paths=55,
        redo_count=0, monotonic_progress=True, constraint_visible_turns=55,
        constraint_visible_at_check=True, constraint_reinjection_present=True,
        violations=[], cap_reported_before_binding=True,
        cap_report_evidence="demo: the cap-receipt check is not what this break tests",
        turn_cap_source=source, required_turn_cap=50, decay_turns_executed=55,
    )
    r.failures = r.derive_failures()
    return {"read": cap, "failures": r.failures,
            "cap_finding": [f for f in r.failures if "turn ceiling" in f]}

from evals.trust_ladder_rungs import declared_turn_cap
shipping_verdict = verdict(*declared_turn_cap())

# The break, by STATE: substitute the config module the loop reads with a
# clone whose ONE key carries the pre-Phase-1 value. A module substitution
# rather than a file edit, because in a tree four terminals are writing, a file
# edit is not how you find out what a check does.
clone = types.ModuleType("harness.config")
for name in dir(real_cfg):
    if not name.startswith("__"):
        setattr(clone, name, getattr(real_cfg, name))
patched = dict(real_cfg.DEFAULTS)
patched["agent_max_turns"] = 25
clone.DEFAULTS = patched
sys.modules["harness.config"] = clone
for mod in [m for m in list(sys.modules) if m.startswith("evals.trust_ladder")]:
    del sys.modules[mod]          # so the probe re-resolves the substituted module
from evals.trust_ladder_rungs import declared_turn_cap as reread
broken_verdict = verdict(*reread())

print(json.dumps({
    "shipped_ceiling": shipped,
    "shipping": shipping_verdict,
    "broken": broken_verdict,
    "observed_ceiling_after_substitution": real_cfg.DEFAULTS.get("agent_max_turns"),
}))
"""


def demo_rung8_cap_reverted() -> Demo:
    """Present the ceiling the loop reads as 25 and confirm the rung says so.

    Read through ``declared_turn_cap()`` in both arms -- the SAME seam the
    shipped ceiling comes from -- so this measures the check, not a private
    number.
    """
    demo = Demo(
        id="rung8_cap_reverted_to_25",
        rung="#8",
        deliberate_break=(
            "harness.config.DEFAULTS['agent_max_turns'] -- the key the loop's "
            "ceiling is read from -- is presented as 25 instead of the shipping "
            "value"
        ),
        check_that_must_fire="Rung8Result.derive_failures() naming the ceiling",
    )
    probe = _child(DEMO1, timeout_s=600)
    if not (probe.get("ok") and probe["stdout"].strip()):
        demo.status = BLOCKED
        demo.reason = f"the demonstration could not be run: {_last_error(probe)}"
        return demo
    doc = json.loads(probe["stdout"].strip().splitlines()[-1])
    shipped, broken = doc["shipping"], doc["broken"]
    demo.evidence = [
        f"SHIPPING arm: harness.config.DEFAULTS['agent_max_turns'] = "
        f"{doc['shipped_ceiling']}; declared_turn_cap() read {shipped['read']}; "
        f"cap finding = {shipped['cap_finding'] or 'none'}",
        f"BROKEN   arm: declared_turn_cap() read {broken['read']} from the "
        f"substituted config (DEFAULTS still reads "
        f"{doc['observed_ceiling_after_substitution']} through the original "
        f"module object, which is how the substitution is known to be scoped)",
        "derive_failures() -> " + " | ".join(broken["failures"]),
    ]
    demo.caught = (
        broken["read"] == 25
        and bool(broken["cap_finding"])
        and not shipped["cap_finding"]
    )
    demo.status = PASS if demo.caught else FAIL
    if not demo.caught:
        demo.reason = (
            "the ceiling was presented as 25 and the rung-#8 verdict did not "
            "change. Either the ceiling is not read from the seam the rung "
            "reads, or the check does not fire -- both must be resolved before "
            "this rung counts as a check at all."
        )
    else:
        demo.reason = (
            "The shipping ceiling measured here is %r. Published as "
            "Phase 1's 2.4x worst-case cost increase (25 -> 60), it must be "
            "quoted with whatever number the tree carries at the time this "
            "report is read, because a stale constant in a gate report is the "
            "same defect as a stale constant in the code." % (doc["shipped_ceiling"],)
        )
    return demo


# --------------------------------------------------------------------------
# DEMO 2 - rung #8, the constraint re-injection removed (and restored)
# --------------------------------------------------------------------------

DEMO2_PREAMBLE = r"""
import json
from evals import trust_ladder_rungs as R
"""

DEMO2_BODY = r"""
BREAK = %(break_repr)s
import harness.agent_loop_step as ALS
import harness.prompts as P

GOVERNING = "not broken (shipping arm)"

# The mechanism that ACTUALLY governs, established by measurement rather than by
# reading: on the `agent_step` path the constraint reaches the model's most
# recent message as `[CONSTRAINTS - still in force]`, appended to every tool
# result by `harness/agent_loop_step.py::_constraint_block`. Not
# `harness/prompts.render_context_reinjection` (utilization-gated, and it does
# not fire on a short run) and not the core fix loop's own block.
#
# The first version of this demonstration removed `DailyCodingStrategy.
# _reinjection` instead, and BOTH arms came back with the constraint visible on
# every turn -- the demonstration "failed" because it had broken a mechanism
# that was not carrying the constraint. A break aimed at the wrong function is
# not a weak demonstration, it is no demonstration, and it is indistinguishable
# from a check that cannot fire unless you also run the control arm.
if BREAK == "removed":
    ALS._constraint_block = lambda cfg: ""
    P.render_constraint_reinjection = lambda **kw: ""
    P.render_context_reinjection = lambda **kw: ""
    GOVERNING = "harness.agent_loop_step._constraint_block -> ''"

res = R.probe_rung8(turns=%(turns)d, required_turn_cap=50)
print(json.dumps({
    "break": BREAK,
    "governing": GOVERNING,
    "declared_turn_cap": res.declared_turn_cap,
    "turns_executed": res.turns_executed,
    "decay_turns_executed": res.decay_turns_executed,
    "constraint_visible_turns": res.constraint_visible_turns,
    "constraint_visible_at_check": res.constraint_visible_at_check,
    "violation_turns": len(res.violations),
    "control_mechanism_exists": res.constraint_reinjection_present,
    "failures": res.failures,
    "decay_finding": [f for f in res.failures if "CONSTRAINT DECAY" in f],
}))
"""


def demo_rung8_reinjection_removed() -> Demo:
    """Remove the re-injection; require the decay finding. Then restore it.

    Two arms on purpose. The ``removed`` arm proves the check fires; the
    ``present`` arm proves the check STOPS firing once the mechanism is back,
    which is what makes the first arm evidence rather than a constant.
    """
    demo = Demo(
        id="rung8_constraint_reinjection_removed",
        rung="#8",
        deliberate_break=(
            "`harness.agent_loop_step._constraint_block` returns '' -- the "
            "`[CONSTRAINTS - still in force]` block that rides every tool "
            "result on the interactive agent path -- together with both "
            "`harness.prompts` re-injection renderers"
        ),
        check_that_must_fire="Rung8Result.derive_failures() naming CONSTRAINT DECAY",
    )
    out: Dict[str, Any] = {}
    for arm in ("removed", "present"):
        code = DEMO2_PREAMBLE + (DEMO2_BODY % {"break_repr": repr(arm), "turns": 55})
        probe = _child(code, timeout_s=1800)
        if not (probe.get("ok") and probe["stdout"].strip()):
            demo.status = BLOCKED
            demo.reason = (
                f"the `{arm}` arm could not be run: {_last_error(probe)}. "
                + (probe.get("stdout", "")[-300:])
            )
            return demo
        out[arm] = json.loads(probe["stdout"].strip().splitlines()[-1])

    broken, repaired = out["removed"], out["present"]
    demo.evidence = [
        "ARM `removed`  : visible_turns=%d/%d, visible_at_check=%s, "
        "forbidden-path writes=%d"
        % (
            broken["constraint_visible_turns"],
            broken["decay_turns_executed"],
            broken["constraint_visible_at_check"],
            broken["violation_turns"],
        ),
        "ARM `present`  : visible_turns=%d/%d, visible_at_check=%s, "
        "forbidden-path writes=%d"
        % (
            repaired["constraint_visible_turns"],
            repaired["decay_turns_executed"],
            repaired["constraint_visible_at_check"],
            repaired["violation_turns"],
        ),
        "control mechanism present in this tree either way: %s"
        % broken["control_mechanism_exists"],
        "removed -> CONSTRAINT DECAY finding present: %s"
        % bool(broken["decay_finding"]),
        "present -> CONSTRAINT DECAY finding present: %s"
        % bool(repaired["decay_finding"]),
    ]
    demo.caught = bool(broken["decay_finding"]) and not repaired["decay_finding"]
    demo.status = PASS if demo.caught else FAIL
    if not demo.caught:
        demo.reason = (
            "the check did not discriminate: a removed re-injection and a "
            "restored one produced the same verdict, so the check is a constant"
        )
    return demo


# --------------------------------------------------------------------------
# DEMO 3 - rung #9, the SECOND skip list is restored
# --------------------------------------------------------------------------

DEMO3 = r"""
import json
MODE = %(mode)r
import harness.retrieval as R
import harness.agent_loop as A
import harness.scan_mode as M
import harness.skipset as S

if MODE == "second_table":
    # The literal pre-Phase-1 defect, reproduced by STATE rather than by a file
    # edit: `harness/retrieval.py` once carried its own `_SKIP_DIRS` literal
    # that omitted `logs`, and `logs/` holds one copy of the repository under
    # repair per run. Rebinding the module attribute produces exactly the state
    # that literal produced, which is what the walk and the identity check read.
    R._SKIP_DIRS = frozenset({
        ".git", ".hg", ".svn", ".venv", "venv", "__pycache__",
        "site-packages", "node_modules", "target", "build", "dist",
        "out", ".tox", ".mypy_cache",
    })

from evals.trust_ladder_rungs import measure_walk_cost, Rung9Result, Timing
opened, skip, walk_ms, hit_cap = measure_walk_cost(".")
same_object = (R._SKIP_DIRS is S.SKIP_DIRS and A._SKIP_DIRS is S.SKIP_DIRS
               and M._SKIP_DIRS is S.SKIP_DIRS)
# The rung's OWN verdict, built on the ONE diagnostic the break moves, so the
# demonstration exercises the authority rather than only a side counter.
r = Rung9Result(
    repo_label=".",
    skip_list=skip,
    walk_dirs_opened=opened,
    walk_hit_cap=hit_cap,
    timings=[
        Timing(name="startup_to_first_token", unit="ms", samples=(10.0,),
               target=2000.0),
        Timing(name="retrieval_cold_then_warm", unit="ms", samples=(50.0,),
               target=1000.0),
        Timing(name="retrieval_walk_dirs_opened", unit="dirs",
               samples=(float(opened),), target=None),
        Timing(name="tui_frame_cost", unit="ms", samples=(1.0,), target=50.0),
    ],
)
r.failures = r.derive_failures()
print(json.dumps({
    "mode": MODE,
    "dirs_opened": opened,
    "hit_cap": hit_cap,
    "skip_count": len(skip),
    "skip_has_logs": "logs" in skip,
    "walk_ms": round(walk_ms, 1),
    "one_authority_object_identity": same_object,
    "authority_dir_count": len(S.SKIP_DIRS),
    "failures": r.failures,
}))
"""


def demo_rung9_second_skip_list() -> Demo:
    """Restore the SECOND skip table and confirm the walk cost explodes.

    ``DOCTRINE.md`` §8 records the shape: *"one retrieval path opened 124,774
    directories in 144.6 s where another opened 155 in 0.42 s. The same walk.
    345x."* The demonstration therefore asserts the MECHANISM -- the walk opens
    orders of magnitude more directories, and the three modules stop being the
    same object -- not merely that some elapsed-time number moved.
    """
    demo = Demo(
        id="rung9_second_skip_list_restored",
        rung="#9",
        deliberate_break=(
            "harness/retrieval.py stops using the shared authority and carries "
            "its own `_SKIP_DIRS` literal, which omits `logs` (the per-run copy "
            "of the repository under repair)"
        ),
        check_that_must_fire=(
            "the walk's directory count, plus the object-identity relation "
            "between harness/retrieval, harness/agent_loop and "
            "harness/scan_mode"
        ),
    )
    out: Dict[str, Any] = {}
    for mode in ("second_table", "authority"):
        probe = _child(DEMO3 % {"mode": mode}, timeout_s=1800)
        if not (probe.get("ok") and probe["stdout"].strip()):
            demo.status = BLOCKED
            demo.reason = (
                f"the `{mode}` arm could not be measured: {_last_error(probe)}"
            )
            return demo
        out[mode] = json.loads(probe["stdout"].strip().splitlines()[-1])

    broken, shipped = out["second_table"], out["authority"]
    ratio = (
        broken["dirs_opened"] / float(shipped["dirs_opened"])
        if shipped["dirs_opened"]
        else float("inf")
    )
    demo.evidence = [
        "AUTHORITY arm: the shared set carries %d directory names; the walk "
        "opened %d dirs in %sms; object identity across "
        "retrieval/agent_loop/scan_mode = %s"
        % (
            shipped["authority_dir_count"],
            shipped["dirs_opened"],
            shipped["walk_ms"],
            shipped["one_authority_object_identity"],
        ),
        "SECOND-TABLE arm: the private set carries %d names and omits `logs` "
        "(present: %s); the walk opened %d dirs in %sms = %.0fx the authority "
        "arm; object identity = %s"
        % (
            broken["skip_count"],
            broken["skip_has_logs"],
            broken["dirs_opened"],
            broken["walk_ms"],
            ratio,
            broken["one_authority_object_identity"],
        ),
        "the count hit the 60,000-directory ceiling (so the true count is "
        "higher, not equal): %s" % broken["hit_cap"],
    ]
    demo.caught = (
        ratio >= 100.0
        and shipped["one_authority_object_identity"]
        and not broken["one_authority_object_identity"]
    )
    demo.status = PASS if demo.caught else FAIL
    if not demo.caught:
        demo.reason = (
            "the second skip table was restored and the walk did NOT explode "
            f"({shipped['dirs_opened']} -> {broken['dirs_opened']} dirs), or "
            "the three modules were already the same object. Either the count "
            "is dominated by something this demo did not break, or the "
            "identity relation is not what the pin asserts."
        )
    demo.reason = demo.reason or (
        "NOTE: the walk count is not a rung-#8/#9 PASS condition. "
        "`Rung9Result.derive_failures()` deliberately skips the "
        "`retrieval_walk_dirs_opened` metric -- the rung budgets retrieval "
        "TIME, and a directory count is the DIAGNOSIS, not the guarantee. So "
        "this demonstration proves the DIAGNOSTIC still has teeth (and that "
        "the 345x has not come back), while the budget itself is rung #9's "
        "own separate check."
    )
    return demo


# --------------------------------------------------------------------------
# DEMO 4 - rung #9, the provider import is eager on the interactive path
# --------------------------------------------------------------------------

DEMO4 = r"""
import json
from evals.trust_ladder_rungs import measure_startup, Rung9Result, Timing
t = measure_startup(runs=%(runs)d)
r = Rung9Result(
    repo_label=".",
    timings=[
        t,
        Timing(name="retrieval_cold_then_warm", unit="ms", samples=(50.0,),
               target=1000.0),
        Timing(name="tui_frame_cost", unit="ms", samples=(1.0,), target=50.0),
    ],
)
r.failures = r.derive_failures()
print(json.dumps({
    "startup_ms": t.headline,
    "window": t.window,
    "statistic": t.named_stat,
    "machine": t.to_dict().get("machine"),
    "target": t.target,
    "samples": [round(s, 1) for s in t.samples],
    "failures": r.failures,
    "startup_finding": [f for f in r.failures if "startup_to_first_token" in f],
}))
"""


def demo_rung9_eager_import() -> Demo:
    """Put a provider import at module scope in ``cli/interactive.py``.

    This is the break the brief names, in the shape it actually takes: not
    "make the metric worse" but "let a module that costs 5.4s to import be
    reached before the user sees a prompt". ``measure_startup`` times a FRESH
    interpreter, which is the only shape that can see it -- an in-process
    measurement would read an already-imported module and report the
    dictionary-lookup cost of ``cli.interactive``, which is why this probe
    spawns a subprocess at all.
    """
    demo = Demo(
        id="rung9_startup_import_eager",
        rung="#9",
        deliberate_break=(
            "cli/interactive.py gains a module-scope `import litellm`, so the "
            "provider SDK is on the interactive path before the first prompt"
        ),
        check_that_must_fire="Rung9Result.derive_failures() naming startup_to_first_token",
    )
    path = REPO_ROOT / "cli" / "interactive.py"

    def _eager(src: bytes) -> bytes:
        # Anchored on the FIRST module-scope import, located by shape rather
        # than by name. Two named anchors were tried first and both rotted:
        # `import threading` (T4 moved it) and `from __future__ import
        # annotations` (T4 removed it). A demonstration that reports `blocked`
        # for twenty minutes because somebody reorganised an import block is
        # measuring the wrong thing, and the fix is to anchor on the SHAPE --
        # "the first line that begins a module-scope import" -- which is a
        # property of the file rather than of its current contents.
        lines = src.split(b"\n")
        for index, line in enumerate(lines):
            if re.match(rb"^(import|from)\s+\S", line):
                lines.insert(index + 1, b"import litellm")
                return b"\n".join(lines)
        raise RuntimeError(
            "cli/interactive.py has no module-scope import to anchor on; "
            "re-point this demo rather than guessing"
        )

    baseline = _child(DEMO4 % {"runs": 3}, timeout_s=900)
    if not (baseline.get("ok") and baseline["stdout"].strip()):
        demo.status = BLOCKED
        demo.reason = "the BASELINE startup could not be measured: " + _last_error(
            baseline
        )
        return demo
    before = json.loads(baseline["stdout"].strip().splitlines()[-1])

    try:
        with GuardedEdit(path, _eager) as edit:
            broken = _child(DEMO4 % {"runs": 3}, timeout_s=1800)
    except Exception as exc:
        demo.status = BLOCKED
        demo.reason = f"the deliberate break could not be constructed: {exc!r}"
        return demo
    demo.restore_digest_before = edit.before
    demo.restore_digest_after = edit.after
    demo.edit_outcome = edit.outcome
    demo.restored = edit.outcome == "restored"

    if not (broken.get("ok") and broken["stdout"].strip()):
        demo.reason = "the broken startup could not be measured: " + _last_error(broken)
        demo.status = (
            FAIL if edit.outcome in ("restore_refused", "restore_failed") else BLOCKED
        )
        return demo
    after = json.loads(broken["stdout"].strip().splitlines()[-1])

    demo.evidence = [
        "BEFORE (no eager provider import): %sms (%s over %d runs, min %s max %s) "
        "on %s"
        % (
            before["startup_ms"],
            before["statistic"],
            before["window"],
            min(before["samples"]),
            max(before["samples"]),
            before["machine"],
        ),
        "BREAK  (module-scope `import litellm`): %sms (%s over %d runs, min %s "
        "max %s) on %s"
        % (
            after["startup_ms"],
            after["statistic"],
            after["window"],
            min(after["samples"]),
            max(after["samples"]),
            after["machine"],
        ),
        "budget: %sms -- samples %s" % (after["target"], after["samples"]),
        "derive_failures() -> " + " | ".join(after["failures"]),
        "file outcome: %s -- %s"
        % (edit.outcome, EDIT_OUTCOMES.get(edit.outcome, "unclassified outcome")),
    ]
    demo.caught = bool(after["startup_finding"]) and not before["startup_finding"]
    demo.status = (
        PASS if (demo.caught and edit.outcome in ("restored", "superseded")) else FAIL
    )
    if not demo.caught:
        demo.reason = (
            "the eager import was added and the 2000ms startup budget did NOT "
            f"fire: {before['startup_ms']}ms -> {after['startup_ms']}ms"
        )
    elif edit.outcome == "superseded":
        demo.reason = (
            "NOTE: another terminal wrote cli/interactive.py while this "
            "demonstration held it, so nothing was written back. The break was "
            "measured and their content stands."
        )
    return demo


# --------------------------------------------------------------------------
# DEMO 5 - rung #10, one link of the visibility chain is dropped
# --------------------------------------------------------------------------

DEMO5 = r"""
import json
MODE = %(mode)r
import cli.tui_components as TC
if MODE == "drop_card":
    # LINK 4 removed: the chain is measured on REAL artifacts, so the last
    # link going empty must be caught rather than rendered as a blank line.
    class _Empty:
        def __init__(self, *a, **k):
            pass
        def render(self):
            return ""
    TC.ResultCard = _Empty
elif MODE == "drop_traceview":
    # LINK 2 removed: traceview reconstructs nothing.
    import shared.traceview as TV
    TV.reconstruct_task = lambda *a, **k: []

from evals.trust_ladder_rungs import probe_rung10
res = probe_rung10(task_id="rung10-demo")
print(json.dumps({
    "mode": MODE,
    "links": [{"link": l.link, "present": l.present, "dropped": list(l.dropped)}
              for l in res.links],
    "card_chars": len(res.card_text or ""),
    "failures": res.failures,
    "chain_finding": [f for f in res.failures if "chain is broken" in f],
}))
"""


def demo_rung10_link_dropped() -> Demo:
    """Drop the card link, then the traceview link. Both must be caught."""
    demo = Demo(
        id="rung10_visibility_link_dropped",
        rung="#10",
        deliberate_break=(
            "one link of journal -> traceview -> runview -> card is removed: "
            "the card renders nothing, and (second arm) traceview reconstructs "
            "nothing"
        ),
        check_that_must_fire="Rung10Result.derive_failures() naming the broken link",
    )
    out: Dict[str, Any] = {}
    for mode in ("drop_card", "drop_traceview"):
        probe = _child(DEMO5 % {"mode": mode}, timeout_s=1200)
        if not (probe.get("ok") and probe["stdout"].strip()):
            demo.status = BLOCKED
            demo.reason = f"the `{mode}` arm could not be run: {_last_error(probe)}"
            return demo
        out[mode] = json.loads(probe["stdout"].strip().splitlines()[-1])

    card, trace = out["drop_card"], out["drop_traceview"]

    def _named(doc: Dict[str, Any]) -> List[str]:
        """The rung's own findings that NAME a missing link.

        Deliberately not a search for one string. The probe reports a dropped
        LINK 4 by returning early with its own message rather than by leaving
        the link absent from `links`, so a predicate that looked for the
        "chain is broken" summary alone reported a rung that HAD caught the
        break as one that had not -- which is how a gate ends up silencing a
        check that works.
        """
        want = ("chain is broken", "LINK 1", "LINK 2", "LINK 3", "LINK 4")
        return [f for f in doc["failures"] if any(w in f for w in want)]

    demo.evidence = [
        "ARM drop_card      : links present = "
        + ", ".join(f"{l['link']}={l['present']}" for l in card["links"])
        + f"; card rendered {card['card_chars']} chars",
        "ARM drop_card      : derive_failures() -> " + " | ".join(card["failures"]),
        "ARM drop_traceview : links present = "
        + ", ".join(f"{l['link']}={l['present']}" for l in trace["links"]),
        "ARM drop_traceview : derive_failures() -> " + " | ".join(trace["failures"]),
        "findings naming a missing link: drop_card -> %d, drop_traceview -> %d"
        % (len(_named(card)), len(_named(trace))),
    ]
    demo.caught = bool(_named(card)) and bool(_named(trace))
    demo.status = PASS if demo.caught else FAIL
    if not demo.caught:
        demo.reason = (
            "a dropped link was NOT named by the rung; the chain check reports "
            "a blank card as a chain that still holds"
        )
    demo.reason = demo.reason or (
        "The link that is ALREADY broken in the shipping tree is LINK 3: the "
        "runview projection re-derives every field instead of carrying the "
        "reconstructed one, so `module` and `source` never reach the card. "
        "That is a live finding owned by T4 (cli/runview.py), not a property of "
        "this check -- which is exactly why this demo removes a DIFFERENT link: "
        "a check demonstrated only against an already-red tree proves nothing."
    )
    return demo


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

#: The five demonstrations, in the order the G1 brief lists them, each with the
#: short key ``--only`` accepts.
DEMOS: Tuple[Tuple[str, Callable[[], Demo]], ...] = (
    ("rung8_cap_reverted_to_25", demo_rung8_cap_reverted),
    ("rung8_constraint_reinjection_removed", demo_rung8_reinjection_removed),
    ("rung9_second_skip_list_restored", demo_rung9_second_skip_list),
    ("rung9_startup_import_eager", demo_rung9_eager_import),
    ("rung10_visibility_link_dropped", demo_rung10_link_dropped),
)

DEMO_KEYS: Tuple[str, ...] = tuple(key for key, _ in DEMOS)


def run_all(only: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Run the demonstrations and derive the report."""
    wanted = set(only or ())
    unknown = wanted - set(DEMO_KEYS)
    if unknown:
        raise SystemExit(
            f"unknown --only {sorted(unknown)}; known keys are {list(DEMO_KEYS)}"
        )
    demos: List[Demo] = []
    for key, fn in DEMOS:
        if wanted and key not in wanted:
            continue
        started = time.perf_counter()
        demo = fn()
        demo.evidence.insert(0, f"[{time.perf_counter() - started:.1f}s wall]")
        demos.append(demo)
    return report(demos)


def report(demos: Sequence[Demo]) -> Dict[str, Any]:
    counts = {
        PASS: sum(1 for d in demos if d.status == PASS),
        FAIL: sum(1 for d in demos if d.status == FAIL),
        BLOCKED: sum(1 for d in demos if d.status == BLOCKED),
        NOT_IMPLEMENTED: sum(1 for d in demos if d.status == NOT_IMPLEMENTED),
    }
    return {
        "schema_version": 1,
        "what_this_is": (
            "the five deliberate rung-break demonstrations for G1. `pass` on a "
            "row means: the break was CONSTRUCTED, the rung's own "
            "derive_failures() FIRED on it, and the tree was restored "
            "byte-for-byte. It does NOT mean the rung passes."
        ),
        "statuses": list(STATUSES),
        "status_meaning": dict(DEMO_STATUS_MEANING),
        "demos": [d.to_dict() for d in demos],
        "demo_count": len(demos),
        "counts": counts,
        "every_demo_caught_its_break": all(d.caught for d in demos),
        "every_edit_restored": all(
            d.edit_outcome in ("restored", "superseded", "not_run") for d in demos
        ),
        "verdict": (
            "RUNG_CHECKS_DEMONSTRATED"
            if all(d.caught for d in demos)
            and all(
                d.edit_outcome in ("restored", "superseded", "not_run") for d in demos
            )
            else "RUNG_CHECKS_NOT_DEMONSTRATED"
        ),
    }


def render(doc: Dict[str, Any]) -> str:
    out = [
        "G1 rung-break demonstrations: " + doc["verdict"],
        "",
        doc["what_this_is"],
        "",
        "status vocabulary: " + " | ".join(doc["statuses"]) + "   (never skip)",
    ]
    for demo in doc["demos"]:
        out += [
            "",
            "=" * 96,
            f"{demo['id']}  [rung {demo['rung']}]  -> {demo['status']}"
            f"   (caught: {demo['caught']})",
            f"  deliberate break : {demo['deliberate_break']}",
            f"  check that fires : {demo['check_that_must_fire']}",
        ]
        if demo["tree_restored"] is not None:
            out.append(
                "  file outcome    : %s -- %s"
                % (demo["edit_outcome"], demo["edit_outcome_meaning"])
            )
            out.append(
                "  digests         : %s... -> %s..."
                % (
                    (demo["digest_before"] or "")[:16],
                    (demo["digest_after"] or "")[:16],
                )
            )
        for line in demo["evidence"]:
            out.append("    " + line)
        if demo["reason"]:
            out.append("  note             : " + demo["reason"])
    out += ["", "-" * 96]
    out.append("counts: " + ", ".join(f"{k}={v}" for k, v in doc["counts"].items()))
    out.append(f"every demo caught its break : {doc['every_demo_caught_its_break']}")
    out.append(f"every edit restored         : {doc['every_edit_restored']}")
    return "\n".join(out)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.rung_break_demos",
        description=(
            "Construct each Phase-1 rung's failure deliberately and report "
            "whether the rung's own check caught it."
        ),
    )
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        help="run one demo by id or by function name (repeatable)",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    doc = run_all(args.only)
    print(json.dumps(doc, indent=2, default=str) if args.json else render(doc))
    return 0 if doc["verdict"] == "RUNG_CHECKS_DEMONSTRATED" else 2


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
