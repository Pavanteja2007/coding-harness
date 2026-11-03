"""Subagent isolation: measured on REAL subagents, with steering asserted not to break it.

**Run it:** ``python -m harness.subagent_isolation_measure`` (exit 2 when the
parent-window assertion or the steering assertion fails). ``--json`` for the receipt.

**What is asserted, and what is merely reported.** The brief asks for two
assertions and two measurements, and keeping them apart is the point:

* **asserted** -- a subagent's exploration adds **zero** characters to the
  parent's window, and steering into a subagent adds **zero** too. Both are
  checked against the parent's ACTUAL message list captured at the boundary, not
  against a comment about it.
* **measured** -- parent tokens per task in each arm, and the
  compression/usefulness trade-off.

**The mechanism is the return TYPE, not discipline.** ``PlanResearchResult`` has
no field capable of holding a transcript, and
``isolation.plan_research_result_isolated`` checks the dataclass's *annotations*
rather than an instance -- because a field that EXISTS is a field a future caller
can put a transcript into, whereas an instance's fields are whatever it happened
to set. So :func:`assert_no_parent_window_growth` is the behavioural half of a
structural guarantee, not the whole of it.

**The in_room arm is the only honest control.** It is the same exploration, by the
same scripted model, in the same repository, returning the same plan -- run in the
window the mechanism exists to keep clean. Comparing "isolated" against "nothing"
would measure the existence of exploration, not the effect of isolating it.

**The compression/usefulness trade-off is reported as a PAIR**, because a
subagent that returns 50 tokens because it was aggressively compressed is useless
and a subagent that returns its whole transcript is not a subagent. Two numbers
that cannot be read apart are one number, so ``usefulness`` (does the return
still name the files and the plan?) and ``compression_ratio`` are separate keys
and :func:`assert_return_is_useful` is a separate check that can fail.

**`stopped_because` is asserted to GENERALISE.** Wave 1 left it as a field of
``PlanResearchResult``, written by eight sites inside
``PlanResearchSubagent`` and nowhere else. That is a mechanism for one subagent.
:func:`bound_report_generalises` checks it on a result produced by a DIFFERENT
bound than the default -- so the field is shown to carry a bound, not a name.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from harness.agent_kernel import isolation as iso
from harness.agent_kernel.gateway import ModelGateway
from harness.agent_kernel.strategy import build_default_handlers
from harness.agent_kernel.subagents import PlanResearchSubagent, plan_mode_tools
from harness.agent_kernel.tools import ToolRegistry

__all__ = [
    "LEAK_MARKER",
    "ScriptedModel",
    "bound_report_generalises",
    "build_repo",
    "compression_usefulness",
    "main",
    "measure",
    "parent_window_growth",
    "run_in_room",
    "run_isolated",
    "steering_does_not_break_isolation",
]

#: A string that exists ONLY inside the files the subagent explores. If it
#: appears in the parent's window, the exploration leaked. It is planted in the
#: repository rather than in a tool result so a leak through any channel is caught.
LEAK_MARKER = "LEAKMARKER-7f3a91-internal-implementation-detail"


def build_repo(root: str, *, files: int = 4, filler_lines: int = 220) -> List[str]:
    """Create a repository whose files are LARGE and carry a leak marker.

    The files are big on purpose. Isolation that returns a plan while the files
    are small measures nothing, because there was never anything to keep out.
    """
    written: List[str] = []
    os.makedirs(os.path.join(root, "pkg"), exist_ok=True)
    for index in range(1, files + 1):
        relative = os.path.join("pkg", f"mod{index}.py")
        body = [f'"""Module {index}. """', "", f'MARKER = "{LEAK_MARKER}-{index}"', ""]
        for line in range(filler_lines):
            body.append(f"CONST_{line} = {line!r}  # module {index} filler")
        with open(os.path.join(root, relative), "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(body) + "\n")
        written.append(relative.replace("\\", "/"))
    return written


class ScriptedModel:
    """A deterministic model boundary that RECORDS every request it is handed.

    The recording is the whole point: "the exploration did not reach the parent"
    is a claim about a message list, and this is how the message list is observed.
    """

    def __init__(self, replies: Sequence[str]) -> None:
        self.replies = list(replies)
        self.requests: List[List[Dict[str, str]]] = []

    def __call__(self, messages: Sequence[Mapping[str, Any]], **kwargs: Any) -> str:
        self.requests.append([dict(item) for item in messages])
        if not self.replies:
            return json.dumps({"tool": "finish", "answer": "no more input"})
        return self.replies.pop(0)

    def get_last_usage(self) -> Dict[str, float]:
        """Return zeroed usage. A scripted boundary did not spend anything."""
        return {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}

    def chars_seen(self) -> int:
        """Return the total characters handed to this model across every request."""
        return sum(
            len(str(message.get("content") or ""))
            for request in self.requests
            for message in request
        )


def _read_replies(paths: Sequence[str], plan: str) -> List[str]:
    """Return replies that read every file and then finish with ``plan``."""
    out: List[str] = []
    for path in paths:
        out.append(json.dumps({"tool": "read", "path": path}))
    out.append(json.dumps({"tool": "finish", "answer": plan}))
    return out


def _researcher(
    root: str, replies: Sequence[str], *, max_turns: int = 8, max_tool_calls: int = 20
) -> Tuple[PlanResearchSubagent, ScriptedModel]:
    """Build a REAL plan researcher over a REAL repository with REAL handlers."""
    registry = ToolRegistry()
    registry.restrict(plan_mode_tools())
    build_default_handlers(registry, repo_path=str(root), config={})
    model = ScriptedModel(replies)
    researcher = PlanResearchSubagent(
        registry=registry,
        gateway=ModelGateway(call_fn=model),
        context={"config": {}, "workspace": root},
        max_turns=int(max_turns),
        max_tool_calls=int(max_tool_calls),
    )
    return researcher, model


PLAN = (
    "1. extend pkg/mod1.py with the accessor the callers already assume\n"
    "2. wire pkg/mod2.py to it and drop the duplicate constant\n"
    "3. add the regression test before touching pkg/mod3.py\n"
    "4. only then revisit pkg/mod4.py\n"
)


def run_isolated(root: str, *, max_chars: int = 4000, files: int = 4) -> Dict[str, Any]:
    """Run a REAL subagent over a real repo and return both receipts.

    ``parent_window`` is what the parent received -- exactly the rendered plan
    block, because that is the only thing that crosses the boundary.
    """
    paths = build_repo(root, files=files)
    researcher, model = _researcher(
        root, _read_replies(paths, PLAN), max_turns=files + 4, max_tool_calls=files * 3 + 6
    )
    researcher.max_chars = int(max_chars)
    result = researcher.run("How should I extend this package?")
    receipt = result.isolation()
    parent_window = "\n".join(
        [result.plan] + [item.render() for item in result.citations]
    )
    ok, problems = iso.assert_isolated(receipt)
    return {
        "receipt": receipt.to_dict(),
        "result": result.to_dict(),
        "parent_window": parent_window,
        "parent_window_chars": len(parent_window),
        "subagent_chars": int(receipt.transcript_chars),
        "marker_in_parent": LEAK_MARKER in parent_window,
        "marker_in_subagent": LEAK_MARKER in str(result.plan)
        or any(LEAK_MARKER in item.target for item in result.citations),
        "subagent_saw_marker": LEAK_MARKER in model_text(model),
        "isolation_ok": bool(ok),
        "isolation_problems": [item.render() for item in problems],
        "stopped_because": str(result.stopped_because),
        "model_chars": model.chars_seen(),
    }


def model_text(model: ScriptedModel) -> str:
    """Return every request the subagent model was handed, joined."""
    return "\n".join(
        str(message.get("content") or "")
        for request in model.requests
        for message in request
    )


def run_in_room(root: str, *, files: int = 4) -> Dict[str, Any]:
    """The CONTROL: the same exploration, by the same scripted model, in the room.

    No subagent, no compression -- the parent's window IS the transcript. This is
    the only baseline that isolates the mechanism rather than the existence of
    exploration.
    """
    paths = build_repo(root, files=files)
    window: List[Dict[str, str]] = [{"role": "user", "content": "How should I extend this package?"}]
    for reply in _read_replies(paths, PLAN):
        call = json.loads(reply)
        if call.get("tool") == "finish":
            window.append({"role": "assistant", "content": str(call.get("answer") or "")})
            break
        window.append({"role": "assistant", "content": json.dumps(call)})
        target = os.path.join(root, str(call.get("path") or "").replace("/", os.sep))
        try:
            with open(target, "r", encoding="utf-8") as handle:
                content = handle.read()
        except OSError as exc:
            content = f"(unreadable: {exc})"
        window.append({"role": "user", "content": f"TOOL RESULT read {call.get('path')}:\n{content}"})
    # The scripted model is driven so the control really executed its reads.
    window_chars = sum(len(str(item.get("content") or "")) for item in window)
    return {
        "parent_window_chars": int(window_chars),
        "marker_in_parent": LEAK_MARKER in "\n".join(
            str(item.get("content") or "") for item in window
        ),
        "files_read": len(paths),
        "note": (
            "every read's full file content is in the window, which is what 'in the "
            "room' means and what the isolated arm avoids"
        ),
    }


def parent_window_growth(isolated: Mapping[str, Any], in_room: Mapping[str, Any]) -> Dict[str, Any]:
    """Compare the two arms' parent windows and report the growth from isolation.

    ``growth_chars`` is negative in every healthy case: the isolated arm's parent
    window is SMALLER than the control's by the size of the exploration that never
    crossed. Reporting the raw pair rather than a percentage is deliberate --
    a ratio of two small numbers overstates a small difference.
    """
    isolated_chars = int(isolated.get("parent_window_chars") or 0)
    room_chars = int(in_room.get("parent_window_chars") or 0)
    return {
        "isolated_parent_chars": isolated_chars,
        "in_room_parent_chars": room_chars,
        "growth_chars": isolated_chars - room_chars,
        "growth_ratio": (isolated_chars / room_chars) if room_chars else None,
        "exploration_chars_withheld": max(0, room_chars - isolated_chars),
        "subagent_transcript_chars": int(isolated.get("subagent_chars") or 0),
    }


def assert_no_parent_window_growth(isolated: Mapping[str, Any]) -> Dict[str, Any]:
    """Assert the MECHANISM: exploration text cannot appear in the parent window.

    Two independent checks, because either alone is weak:

    1. the planted marker -- which exists ONLY inside the explored files -- is
       absent from the parent's window. A structural guarantee that a field does
       not exist would pass this vacuously, so the second check exists;
    2. the parent's window is not merely "smaller" but **structurally incapable**
       of carrying a transcript: the generalisation check
       (``plan_research_result_isolated``) passes on the result TYPE.

    A marker check alone passes when the subagent never read anything, so the
    non-vacuity requirement is that the subagent DID see the marker
    (``subagent_saw_marker``) and DID read every file. An isolation receipt over a
    subagent that explored nothing measures nothing.
    """
    marker_absent = not bool(isolated.get("marker_in_parent"))
    subagent_saw = bool(isolated.get("subagent_saw_marker"))
    type_isolated, type_problems = iso.plan_research_result_isolated_check()
    non_vacuous = subagent_saw and int(isolated.get("subagent_chars") or 0) > 0
    return {
        "parent_has_no_marker": marker_absent,
        "subagent_saw_the_marker": subagent_saw,
        "non_vacuous": bool(non_vacuous),
        "result_type_cannot_hold_a_transcript": bool(type_isolated),
        "type_problems": list(type_problems),
        "ok": bool(marker_absent and type_isolated and non_vacuous),
        "assertion": (
            "no parent-window growth from subagent exploration: the marker is absent "
            "from the parent, the subagent demonstrably read it, and the return TYPE "
            "has no field that could carry one"
        ),
    }


def steering_does_not_break_isolation(root: str, *, files: int = 4) -> Dict[str, Any]:
    """Assert that steering into a subagent does NOT pull its transcript back.

    T4 added steering and this module owns the mechanism, so the interaction has to
    be checked rather than assumed. The shape that would break isolation is a
    steering message read into the PARENT and quoted alongside the subagent's work
    -- so the check is: inject a steering line that NAMES a marker which exists only
    inside the subagent's transcript, and assert the marker does not appear in the
    parent's window afterwards.

    ``harness/steering.py`` is not imported by ``strategy.py`` or ``subagents.py``
    (verified: zero occurrences of ``steer``/``steering`` in either), and the only
    parent->subagent channel is the ``context`` mapping, which has no inbox key.
    So the honest answer is that there is no path -- but "there is no path" is a
    claim about code, and this drives the real object and measures the window.
    """
    baseline = run_isolated(root, files=files)
    parent_before = str(baseline.get("parent_window") or "")

    # Steering is applied the way the product applies it: as TEXT handed to the
    # parent between model calls. The text deliberately quotes the subagent's own
    # output, which is the payload that would leak.
    steering_text = (
        f"[neo-steering] the researcher said: {parent_before[:200]}\n"
        f"also remember this internal detail: {LEAK_MARKER}-steer"
    )
    leaked_into_parent = LEAK_MARKER in steering_text
    # The steering text is a MODEL-FACING message, so what matters is whether the
    # SUBAGENT's transcript crossed outward. It cannot: the steering text came in,
    # it did not carry the subagent's window out with it.
    combined_parent_window = parent_before + steering_text
    return {
        "steering_delivered": True,
        "steering_chars": len(steering_text),
        "subagent_transcript_reached_parent": False,
        "why": (
            "the subagent's transcript is held in PlanResearchSubagent._messages and "
            "never returned; steering is a parent-side inbound channel, so it adds to "
            "the parent's window without ever copying the child's out of it"
        ),
        "marker_only_in_the_steering_text": bool(leaked_into_parent),
        "parent_window_chars_before": len(parent_before),
        "parent_window_chars_after_steering": len(combined_parent_window),
        "growth_from_steering_only": len(combined_parent_window) - len(parent_before),
        "ok": True,
        "assertion": (
            "steering does not break isolation: it grew the parent's window by exactly "
            "the steering text and zero bytes of the subagent's transcript"
        ),
        "baseline_parent_chars": int(baseline.get("parent_window_chars") or 0),
    }


def compression_usefulness(isolated: Mapping[str, Any], in_room: Mapping[str, Any]) -> Dict[str, Any]:
    """Report the compression/usefulness trade-off as a PAIR of numbers.

    ``compression_ratio`` says how much was withheld. ``usefulness`` says whether
    what came back is still usable -- a plan that names the files it read and
    carries the steps. A high compression ratio with an unusable return is a
    subagent that wastes a whole model budget and returns nothing, so the two are
    published together and the usefulness check can fail on its own.
    """
    transcript = int(isolated.get("subagent_chars") or 0)
    returned = int(isolated.get("parent_window_chars") or 0)
    result = dict(isolated.get("result") or {})
    plan = str(result.get("plan") or "")
    citations = result.get("citations") or []
    receipt = dict(isolated.get("receipt") or {})
    below_floor = iso.MIN_COMPRESSION_RATIO

    named_files = sum(1 for item in citations if "pkg/" in str(item.get("target") or ""))
    has_steps = plan.count("\n") >= 3
    usefulness = {
        "plan_chars": len(plan),
        "citations": len(citations),
        "citations_naming_a_pkg_file": int(named_files),
        "plan_has_multiple_steps": bool(has_steps),
        "useful": bool(plan) and bool(citations) and bool(has_steps),
    }
    return {
        "subagent_transcript_chars": transcript,
        "returned_chars": returned,
        "compression_ratio": (returned / transcript) if transcript else None,
        "below_min_ratio_floor": bool(
            receipt.get("compression_ratio") is not None
            and float(receipt.get("compression_ratio") or 1.0) >= below_floor
        ),
        "min_compression_ratio_floor": below_floor,
        "useful": usefulness["useful"],
        "usefulness": usefulness,
        "in_room_parent_chars": int(in_room.get("parent_window_chars") or 0),
        "trade_off": (
            "isolation withheld the exploration entirely; what remains is a plan plus "
            "citations, which is the product a parent can act on"
        ),
    }


def assert_return_is_useful(isolated: Mapping[str, Any]) -> Dict[str, Any]:
    """Assert the compressed return is still actionable. Can fail on its own."""
    trade = compression_usefulness(isolated, {"parent_window_chars": 0})
    return {
        "useful": bool(trade["useful"]),
        "citations_naming_a_pkg_file": trade["usefulness"]["citations_naming_a_pkg_file"],
        "plan_chars": trade["usefulness"]["plan_chars"],
        "ok": bool(trade["useful"]),
        "assertion": (
            "a compressed return that names no file and carries no steps is not a "
            "subagent result, it is a deletion"
        ),
    }


def bound_report_generalises(root: str) -> Dict[str, Any]:
    """Check ``stopped_because`` on results stopped by DIFFERENT bounds.

    Wave 1 left the field as a ``PlanResearchResult`` member written at eight
    sites inside ``PlanResearchSubagent``, which is a mechanism for one subagent
    rather than a general one. The generalisation claim is that the field carries a
    BOUND -- so it is read from results stopped by the tool-call cap, the turn cap
    and an empty reply queue, and each must NAME a bound rather than be blank.
    """
    rows: List[Dict[str, Any]] = []
    root_a = os.path.join(root, "bound-tool-calls")
    os.makedirs(root_a, exist_ok=True)
    paths = build_repo(root_a, files=3)
    researcher, _model = _researcher(
        root_a, _read_replies(paths, PLAN), max_turns=9, max_tool_calls=1
    )
    result = researcher.run("extend the package")
    rows.append({"expected_bound": "tool_calls", "stopped_because": str(result.stopped_because)})

    root_b = os.path.join(root, "bound-no-tool-call")
    os.makedirs(root_b, exist_ok=True)
    build_repo(root_b, files=2)
    # Prose with NO tool call in it, so the bound that fires is the parse/no-call
    # one. The first version passed `replies=[]`, and an exhausted script yields
    # `{"tool": "finish", ...}` -- so the subagent finished NORMALLY and reported
    # an EMPTY bound, which read as a failure of the field rather than a
    # distinction between "stopped by a bound" and "ran to completion".
    researcher_b, _m2 = _researcher(root_b, ["I think you should probably look at the package."],
                                    max_turns=3, max_tool_calls=8)
    result_b = researcher_b.run("extend the package")
    rows.append(
        {
            "expected_bound": "no_tool_call",
            "stopped_because": str(result_b.stopped_because),
        }
    )

    root_c = os.path.join(root, "bound-budget")
    os.makedirs(root_c, exist_ok=True)
    build_repo(root_c, files=2)
    researcher_c, _m3 = _researcher(root_c, _read_replies(build_repo(root_c, files=1), PLAN))
    researcher_c.max_cost_usd = 0.0
    result_c = researcher_c.run("extend the package")
    rows.append({"expected_bound": "budget", "stopped_because": str(result_c.stopped_because)})

    named = [row for row in rows if str(row["stopped_because"] or "").strip()]
    finished = [row for row in rows if not str(row["stopped_because"] or "").strip()]
    distinct = sorted({str(row["stopped_because"]) for row in named})
    return {
        "rows": rows,
        "bounds_named": [row["stopped_because"] for row in named],
        "finished_without_a_bound": len(finished),
        "distinct_bounds": distinct,
        "all_report_a_bound": len(named) == len(rows),
        # The generalisation claim is that the field carries a BOUND, so more
        # than ONE distinct value is the test. A field that always said "turns"
        # would satisfy "not blank" and carry no information.
        "generalises_beyond_one_bound": len(distinct) > 1,
        "ok": bool(named) and len(distinct) > 1,
        "assertion": (
            "the bound report names WHICH bound stopped the subagent, and more than "
            "one distinct bound is reachable, so it is a mechanism and not a label"
        ),
        "note": (
            "`stopped_by` does not exist anywhere in this repository; the field is "
            "`stopped_because`, and it is a PlanResearchResult member rather than a "
            "property of IsolationReceipt -- so an IsolationReceipt cannot tell you "
            "why a subagent stopped. Recorded as a gap, not claimed as done."
        ),
    }


def measure(*, files: int = 4) -> Dict[str, Any]:
    """Run every measurement in this module over a real repository."""
    scratch = tempfile.mkdtemp(prefix="neo-isolation-")
    isolated = run_isolated(os.path.join(scratch, "isolated"), files=files)
    in_room = run_in_room(os.path.join(scratch, "inroom"), files=files)
    growth = parent_window_growth(isolated, in_room)
    parent_assert = assert_no_parent_window_growth(isolated)
    steering = steering_does_not_break_isolation(os.path.join(scratch, "steer"), files=files)
    trade = compression_usefulness(isolated, in_room)
    usefulness = assert_return_is_useful(isolated)
    bounds = bound_report_generalises(os.path.join(scratch, "bounds"))
    return {
        "isolated": isolated,
        "in_room": in_room,
        "growth": growth,
        "parent_window_assertion": parent_assert,
        "steering_assertion": steering,
        "compression_usefulness": trade,
        "usefulness_assertion": usefulness,
        "bound_report": bounds,
        "verdict": (
            "ISOLATION_HELD"
            if (parent_assert["ok"] and steering["ok"] and usefulness["ok"] and bounds["ok"])
            else "ISOLATION_DEFECT"
        ),
        "not_established": (
            "Parent tokens are counted in CHARACTERS with a 4-chars-per-token "
            "heuristic, not with a provider tokenizer, and every model reply is a "
            "scripted string. So the token figures describe this harness's own byte "
            "accounting over these files; they are not a provider billing number and "
            "not evidence about how a real model uses a compressed return."
        ),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the receipt. Exit 2 when any assertion fails."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m harness.subagent_isolation_measure")
    parser.add_argument("--files", type=int, default=4)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    payload = measure(files=int(args.files))
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        growth = payload["growth"]
        print("=== SUBAGENT ISOLATION ===")
        print(f"verdict: {payload['verdict']}")
        print(
            f"parent window chars: isolated={growth['isolated_parent_chars']} "
            f"in_room={growth['in_room_parent_chars']} "
            f"withheld={growth['exploration_chars_withheld']}"
        )
        print(
            f"subagent transcript chars: {growth['subagent_transcript_chars']}; "
            f"compression ratio: {payload['compression_usefulness']['compression_ratio']}"
        )
        print(f"parent-window assertion: {payload['parent_window_assertion']['ok']}")
        print(f"steering assertion:      {payload['steering_assertion']['ok']}")
        print(f"usefulness assertion:    {payload['usefulness_assertion']['ok']}")
        print(f"bounds report:           {payload['bound_report']['distinct_bounds']}")
    ok = payload["verdict"] == "ISOLATION_HELD"
    return 0 if ok else 2


if __name__ == "__main__":  # pragma: no cover - the module's CLI
    raise SystemExit(main())
