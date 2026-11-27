"""The ONE place ``execution/**`` asks the hardline floor, and the ONE place a
command can be refused before it is dispatched.

WHY THIS MODULE EXISTS (P2/W2-T2, measured, not remembered)
-----------------------------------------------------------
The floor — ``runtime/hardline.py`` — is 16 patterns, a frozen at-import
decision, and a 12-step deobfuscation pass that runs BEFORE matching. Before
this wave it had **ZERO production call sites in the entire tree**: measured
with a scan for ``hardline`` across ``harness/``, ``cli/``, ``execution/`` and
``runtime/``, the only hits were ``runtime/hardline.py`` itself and its own
pin module.

The consequence was not theoretical. Every one of the brief's floor attacks
#1 (quote splice), #3 (``env`` prefix), #4 (``command`` prefix), #5 (command
chaining) and #6 (``curl | sh``) **executed inside a real container**. They
were stopped by GNU ``rm``'s own ``--no-preserve-root`` guard and by ``curl``
being absent from ``python:3.10-slim`` — not by this harness. ``rm -rf /``
reached ``/bin/bash -c`` in a container and *asked to be allowed*.

A floor that no command crosses is not a floor. This module is the crossing.

WHAT IS AND IS NOT HERE
-----------------------
* **One implementation.** This module owns a **single named refusal** and a
  delegating call. It contains **no pattern table** — matching belongs to
  ``runtime/hardline.py`` and only there. Two tables in the tree would be two
  answers to "is this command unapprovable", and this repository has already
  paid for that class repeatedly (``_DENY_PAT`` vs the floor; ``classify_retry``
  vs ``classify_provider_failure``; ``_safe_detail`` vs ``redact_secrets``).
* **The pre-spawn argv gate is untouched.** ``sandbox.assert_sandbox_argv_isolated``
  answers "what MOUNTS did this container get". This module answers "may this
  COMMAND be run at all". Two independent authorities on two axes, and the
  argv one deliberately still cannot see a dropped overlay — that asymmetry is
  load-bearing and is recorded in ``execution/AGENTS.md``.
* **The refusal is re-raised, not re-wrapped.** ``HardlineRefused`` is
  deliberately neither a ``PermissionError`` nor a ``BudgetRefused``: the
  provider gateway absorbs ``PermissionError`` as its fail-closed path, so a
  wrapped refusal would become "try the next provider". A caller that catches it
  must catch ``runtime.hardline.HardlineRefused``.

THE ONE NARROWING DEFENCE, and why it is not a second table
-----------------------------------------------------------
Attack #2 is ``$(echo <base64>|base64 -d)`` and it **succeeds against the
floor today**, for a reason this module can name exactly:

    runtime/hardline.py::_decode_base64 APPENDS the decoded plaintext to the
    end of the text ("the plaintext is a CANDIDATE"), and then
    ``_split_parts`` makes it part of a segment whose HEAD token is the
    decoder::

        "echo cm0gLXJmIC8= | base64 -d"
          -> segments ('echo cm0gLXJmIC8=', 'base64 -d rm -rf /')

``filesystem_root_wipe`` is ``command``-scoped — it anchors at the segment
head — so the segment head is ``base64`` and the payload the module just
decoded is never looked at. This is the exact mirror of the bug the same file
fixed for ``$(echo rm -rf /)``: ``_fold_substitutions`` carries the comment
*"The folded body LEADS, then the wrapper. Putting the body first is
load-bearing ... every command-anchored pattern then misses it."* The
base64 step did not get that fix.

Measured live, real container, real payload
(``base64("rm -rf /tmp/t2w2-victim")``)::

    plain  rm -rf /                          exit=1  rm: dangerous to operate on '/'
    eval $(echo <b64>|base64 -d)             exit=0  <no output>
    control: /tmp/t2w2-victim/m.txt exists   exit=0  m.txt
    after:   test -e /tmp/t2w2-victim        exit=0  GONE

The payload ran and the directory is gone. ``runtime/hardline.py`` is T3's
file and this wave may not edit it, so the underlying defect is **filed**, not
patched here.

What this module does instead is apply the floor's OWN rule to the executor:
**a shape that cannot be judged is REFUSED, not passed through.** The floor
already does this — ``deobfuscate`` returns ``failure="malformed_command"``
and ``refusal_reason`` refuses it, precisely so "nothing matched" is never read
as "allowed". This module extends the same rule one step up: if the
deobfuscation pass reports that it recovered a payload's **plaintext**, the
executor refuses, because at that moment the command contains bytes whose
effect this process has decoded and demonstrably not classified.

It is one named refusal, not a pattern table. It can only ever add a refusal.
Its blast radius is measured, not assumed: a scan of ``tests/``, ``evals/``,
``harness/``, ``cli/``, ``execution/``, ``runtime/`` and ``shared/`` finds
``base64 -d`` in exactly two places, both inside T3's own adversarial corpus,
and **zero** legitimate uses. A refusal that has no false positive in the tree
is cheap to keep and expensive to remove.

FAIL CLOSED
-----------
If ``runtime.hardline`` cannot be imported, this module raises
:class:`HardlineUnavailable` — it does **not** allow the command. A security
authority that is merely *absent* must not read as *permissive*; that is the
``execution.ingress`` fail-closed rule and the ``verification_gate`` rule, and
this is the third instance. The exception is a ``RuntimeError`` and is NOT a
``PermissionError``, for the provider-gateway reason above.

THE IMPORT IS NOT FREE, and here is the number
----------------------------------------------
``from runtime import hardline`` executes ``runtime/__init__.py``, which fires
``_start_litellm_preload()``. Measured on this host: **204 ms**, and outside a
test session it also starts a background provider-library import.

The alternative — loading ``runtime/hardline.py`` by file path under an alias —
was measured at 163 ms and **rejected**: ``runtime/hardline.py`` freezes itself
by replacing ``sys.modules[__name__].__class__`` and installs a
``sys.meta_path`` finder keyed on ``__name__ == "runtime.hardline"``. An alias
load yields a SECOND floor that the reload guard does not cover, and two floors
is precisely the defect this module exists to avoid. One background thread is
the cheaper price.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

__all__ = [
    "DECODED_PAYLOAD_STEP",
    "GATE_CONTAINMENT_ONLY",
    "GATE_FLOOR",
    "GATE_MODES",
    "OPAQUE_DECODED_PAYLOAD",
    "HardlineUnavailable",
    "enforce",
    "floor_receipt",
    "load_floor",
    "screen",
]

#: The deobfuscation step whose firing means "this harness just recovered a
#: payload's plaintext and did not classify it". Read from the floor's own
#: declared vocabulary rather than hardcoded, so the pin below can fail if the
#: step is ever renamed or removed.
DECODED_PAYLOAD_STEP = "decode_base64_payloads"

#: The refusal this module adds on top of the floor's table. NOT a pattern:
#: there is no regex, no table and no set of spellings behind it. A command
#: matches it by exactly one fact - the deobfuscation pass reported a decoded
#: payload - which is why it is safe to apply without owning the pattern list.
#:
#: It is **blanket** on purpose, and that is the whole judgement call in this
#: module. The effect of a decoded payload is **unknown by construction**: the
#: harness read plaintext it could not classify, so there is nothing to narrow
#: on. Narrowing to "decoded payloads that look dangerous" would mean owning a
#: second pattern table inside `execution/` — the exact defect this gate exists
#: to stop another module from shipping. An earlier draft of the pin suite
#: asserted the opposite (a harmless decoded payload must be accepted); it
#: failed, correctly, and the suite now pins the blanket behaviour on purpose.
#:
#: **STATUS as of 2026-10-03: this is DEFENCE-IN-DEPTH, not the only line.** It
#: was written because ``runtime/hardline.py::_decode_base64`` appended the
#: decoded plaintext behind the decoder, so the payload landed where no
#: command-anchored row could see it — a measured bypass. T3 has since fixed the
#: append AND added a ``computed_command_name`` row, so the floor refuses that
#: payload on its own and this defence no longer fires first.
#:
#: It is deliberately KEPT. A floor declining to match a shape is a NORMAL
#: state, not an exotic one, and the two layers fail independently: the day
#: T3's row is renamed or narrowed, this still holds the line. Deleting it
#: because the upstream fix landed would trade a redundant check for a single
#: point of failure on the strength of one row's continued existence.
OPAQUE_DECODED_PAYLOAD = "opaque_decoded_payload"

#: Which gate a dispatch goes through.
#:
#: :data:`GATE_FLOOR` is the product default and the only one a run can reach:
#: ``execute_sandboxed`` has no ``config`` parameter, so no ``Task.config`` key,
#: no environment variable and no model-authored value can select anything
#: else. :data:`GATE_CONTAINMENT_ONLY` exists for the measurement drivers in
#: ``execution/sandbox_adversarial.py``, ``execution/containment_audit.py`` and
#: ``execution/sandbox_stress.py``, which deliberately spawn payloads that are
#: ON the floor in order to measure the CONTAINMENT layer — a different axis.
#:
#: The literal is confined by a source pin
#: (``execution/test_p2w2_attack_pins.py::test_the_containment_only_literal_is_confined``)
#: that fails if it appears anywhere else in the tree. If you find yourself
#: adding it to a fourth file, the answer is almost certainly to measure
#: containment with a payload the floor does not hold.
GATE_MODES: Sequence[str] = ("floor", "containment_only")
GATE_FLOOR = "floor"
GATE_CONTAINMENT_ONLY = "containment_only"

#: Where the floor is consulted, as DATA so a pin can assert each site is real.
#: ``owner`` is T3 (``runtime/``) and is named so a reader knows where the
#: decision authority lives.
GATE_SITES: Sequence[Dict[str, str]] = (
    {
        "module": "execution/sandbox.py",
        "symbol": "execute_sandboxed",
        "axis": "command",
        "why": (
            "the Boundary-1 executor; the refusal is recorded even when a "
            "caller misconfigures the container, so a refused command never "
            "reaches argv construction"
        ),
    },
    {
        "module": "execution/workspace.py",
        "symbol": "SafeToolBackend._run_process_with_profile",
        "axis": "command",
        "why": (
            "the typed daily-kernel shell path, and the ONLY one that can "
            "reach the local_trusted HOST profile; gating the sandbox alone "
            "would leave the host lane unscreened"
        ),
    },
)


class HardlineUnavailable(RuntimeError):
    """The hardline floor could not be loaded, so no command was screened.

    **This is a refusal, not a pass.** The floor is an unapprovable guarantee;
    an absent authority is not a permissive one, and quietly allowing commands
    because the checker failed to import is how a security control becomes
    decorative.

    Deliberately **not** a ``PermissionError``: ``runtime/provider_gateway``
    absorbs ``PermissionError`` as its fail-closed path, and absorbing this
    would turn "nothing ran" into "try the next provider".
    """


_FLOOR: Any = None
_FLOOR_ERROR: str = ""


def load_floor() -> Any:
    """Return the loaded ``runtime.hardline`` module, or raise.

    Cached after the first success, because the floor's whole design is that it
    is decided ONCE at import: re-importing it would be the injection it
    refuses (``importlib.reload`` is already blocked inside it).

    Raises :class:`HardlineUnavailable` when the module cannot be imported. The
    first failure's text is cached too, so a broken install reports the SAME
    reason on every command instead of a fresh traceback per call.
    """
    global _FLOOR, _FLOOR_ERROR
    if _FLOOR is not None:
        return _FLOOR
    if _FLOOR_ERROR:
        raise HardlineUnavailable(_FLOOR_ERROR)
    try:
        from runtime import hardline as module
    except Exception as exc:  # pragma: no cover - a broken tree, not a policy
        _FLOOR_ERROR = (
            f"the hardline floor could not be loaded ({type(exc).__name__}: "
            f"{exc}); every command is refused rather than run unscreened"
        )
        raise HardlineUnavailable(_FLOOR_ERROR) from exc
    _FLOOR = module
    return _FLOOR


def floor_receipt(command: Any, *, site: str = "") -> Dict[str, Any]:
    """Return the floor's verdict on ``command`` as a JSON-safe receipt.

    Never raises for a normal verdict; a malformed command is a VERDICT
    (``outcome == "refused"`` with ``failure`` set), not an exception, because
    the floor already decided that "cannot judge" means "refuse" and a caller
    that wants to distinguish the two reasons can read ``failure``.

    ``outcome`` is a closed three-value vocabulary:

    ``clear``
        The floor screened the command and did not refuse it. **This is not an
        allow** — the floor is not an allowlist, and a receipt that rendered it
        as one would invite exactly the inference it does not support.
    ``refused``
        The floor refused, or this module added
        :data:`OPAQUE_DECODED_PAYLOAD`.
    ``unavailable``
        The floor could not be loaded, **or the floor itself raised while
        screening.** :func:`enforce` turns this into a refusal; a receipt that
        reported it as anything else would be the decorative-receipt failure
        mode.

    The second ``unavailable`` trigger is not hypothetical and was added because
    it happened: `runtime/hardline.py` was mid-rewrite by its owner while this
    suite ran, with a renamed module constant, and ``floor.screen`` raised
    ``NameError`` on a command it should have screened. A gate that crashes on a
    broken checker is a gate whose behaviour on a broken checker is "whatever
    the caller does with the exception" — which is how a floor becomes optional
    exactly when it is being edited. Fail closed instead.
    """
    try:
        floor = load_floor()
    except HardlineUnavailable as exc:
        return _unavailable_receipt(site, detail=str(exc))

    try:
        refusal = floor.screen(command)
        steps = tuple(floor.deobfuscate(command).steps) if not refusal else tuple(
            refusal.deobfuscation.steps
        )
        fingerprint = str(floor.floor_fingerprint().get("fingerprint") or "")
        enforced = bool(floor.floor_is_enforced())
    except Exception as exc:
        return _unavailable_receipt(
            site,
            detail=(
                f"the hardline floor raised while screening "
                f"({type(exc).__name__}: {exc}); the command is refused rather "
                "than run unscreened"
            ),
            failure="floor_raised",
        )
    base: Dict[str, Any] = {
        "site": str(site or ""),
        "deobfuscation_steps": list(steps),
        "floor_is_enforced": enforced,
        "floor_fingerprint": fingerprint,
    }
    if refusal is None:
        if DECODED_PAYLOAD_STEP in steps:
            # The narrowing defence. The floor DECODED a payload and no pattern
            # claimed it; the executor refuses rather than running bytes whose
            # effect this process has read and not classified.
            return {
                **base,
                "outcome": "refused",
                "pattern_id": OPAQUE_DECODED_PAYLOAD,
                "summary": (
                    "the command hides a payload whose plaintext the harness "
                    "decoded and did not classify"
                ),
                "blast_radius": (
                    "whatever the encoded payload does, which is unknown by "
                    "construction: that is the whole effect"
                ),
                "justification": (
                    "the deobfuscation pass recovered this payload's plaintext "
                    "and no floor row claimed it, so at this moment the command "
                    "contains bytes whose effect has been read and not judged. "
                    "A shape that cannot be judged is refused, not run."
                ),
                "matched_segment": "",
                "failure": "",
                "detail": (
                    f"deobfuscation reported {DECODED_PAYLOAD_STEP!r}; the "
                    "decoded plaintext did not lead any segment, so a "
                    "command-anchored floor row could not see it"
                ),
            }
        return {
            **base,
            "outcome": "clear",
            "pattern_id": "",
            "summary": "",
            "blast_radius": "",
            "justification": "",
            "matched_segment": "",
            "failure": "",
            "detail": "",
        }
    return {
        **base,
        "outcome": "refused",
        "pattern_id": str(refusal.pattern_id),
        "summary": str(refusal.summary),
        "blast_radius": str(refusal.blast_radius),
        "justification": str(refusal.justification),
        "matched_segment": str(refusal.matched_segment),
        "failure": str(refusal.failure or ""),
        "detail": "",
    }


def _unavailable_receipt(
    site: str, *, detail: str, failure: str = "floor_unavailable"
) -> Dict[str, Any]:
    """Return the ``unavailable`` receipt. One shape for both triggers."""
    return {
        "outcome": "unavailable",
        "site": str(site or ""),
        "pattern_id": "",
        "summary": "the hardline floor could not screen this command",
        "blast_radius": "unknown: nothing was screened",
        "justification": detail,
        "matched_segment": "",
        "failure": failure,
        "detail": detail,
        "deobfuscation_steps": [],
        "floor_is_enforced": False,
        "floor_fingerprint": "",
    }


def screen(command: Any, *, site: str = "") -> Dict[str, Any]:
    """Alias of :func:`floor_receipt`, named for the reading caller."""
    return floor_receipt(command, site=site)


def enforce(command: Any, *, site: str, repo_path: str = "") -> None:
    """Raise unless ``command`` clears the hardline floor.

    :param command: the command line about to be dispatched.
    :param site: which gate is asking. Recorded on the trace row so a reader
        can tell which of the two crossings refused.
    :param repo_path: the mounted repository, used only to derive a trace task
        id. Never read for a decision.

    Raises ``runtime.hardline.HardlineRefused`` **unchanged** when the floor
    refuses — not wrapped, not re-typed — so a caller catching the floor's own
    exception type keeps working and so the exception's deliberate
    non-``PermissionError`` type survives.

    Raises :class:`HardlineUnavailable` when the floor cannot be loaded.

    Emits a ``hardline_refusal`` unified-trace row on every refusal, carrying
    ``pattern_id``, ``blast_radius`` and ``justification`` — the three fields a
    reader needs to judge the refusal without opening the source. Emission is
    best-effort and never changes the verdict.
    """
    if site == GATE_CONTAINMENT_ONLY:
        # The measurement lane, reached only by a source literal. See
        # GATE_MODES. There is nothing to do here on purpose: the point of the
        # lane is to spawn the payload so the CONTAINMENT layer can be
        # measured, so this function is simply not called.
        return
    receipt = floor_receipt(command, site=site)
    outcome = receipt["outcome"]
    if outcome == "clear":
        return
    if outcome == "unavailable":
        _emit_refusal(repo_path, receipt)
        raise HardlineUnavailable(receipt["detail"])
    _emit_refusal(repo_path, receipt)
    floor = _FLOOR
    if floor is None:  # pragma: no cover - unreachable: refused implies loaded
        raise HardlineUnavailable(receipt["detail"])
    try:
        refusal = floor.screen(command)
    except Exception as exc:
        # The floor screened once (inside `floor_receipt`, which caught it) and
        # is now raising again. Reached only when the module changed under us
        # between the two calls; still a refusal, never an escape.
        raise HardlineUnavailable(
            f"the hardline floor raised on re-screening ({type(exc).__name__}: "
            f"{exc}); the command is refused rather than run unscreened"
        ) from exc
    if refusal is not None:
        # Re-raised as the floor's own object so `exc.refusal.as_dict()` and
        # `exc.command` exist for the journal.
        raise floor.HardlineRefused(refusal)
    try:
        shape = floor.deobfuscate(command)
    except Exception as exc:
        raise HardlineUnavailable(
            f"the hardline floor raised while deobfuscating for the refusal "
            f"record ({type(exc).__name__}: {exc}); the command is refused "
            "rather than run unscreened"
        ) from exc
    raise floor.HardlineRefused(
        floor.HardlineRefusal(
            pattern_id=OPAQUE_DECODED_PAYLOAD,
            summary=receipt["summary"],
            blast_radius=receipt["blast_radius"],
            justification=receipt["justification"],
            matched_segment=receipt["detail"],
            deobfuscation=shape,
        )
    )


def _emit_refusal(repo_path: str, receipt: Dict[str, Any]) -> None:
    """Best-effort unified-trace row for a refusal. Never raises, never decides."""
    try:
        from execution.sandbox import _trace_task_id
        from shared import tracing

        tid = _trace_task_id(str(repo_path or ""))
        if not tid:
            return
        tracing.emit(
            "execution",
            "hardline_refusal",
            task_id=tid,
            site=receipt.get("site", ""),
            outcome=receipt.get("outcome", ""),
            pattern_id=receipt.get("pattern_id", ""),
            summary=receipt.get("summary", ""),
            blast_radius=receipt.get("blast_radius", ""),
            justification=receipt.get("justification", ""),
            failure=receipt.get("failure", ""),
            detail=receipt.get("detail", ""),
            deobfuscation_steps=list(receipt.get("deobfuscation_steps") or ()),
            floor_is_enforced=bool(receipt.get("floor_is_enforced")),
            floor_fingerprint=receipt.get("floor_fingerprint", ""),
        )
    except Exception:
        pass


def gate_mode_from(value: Any, *, default: str = GATE_FLOOR) -> str:
    """Resolve a gate name, refusing anything outside :data:`GATE_MODES`.

    An unrecognised value resolves to the SAFE default rather than raising,
    because a typo in a caller must not be able to reach
    :data:`GATE_CONTAINMENT_ONLY`. The safe direction for an unknown name is
    "floor", which is the only value a run can reach.
    """
    text = str(value or "").strip().lower()
    return text if text in GATE_MODES else default


def refusal_rows(receipts: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Reduce receipts to the refusals among them, for a summary journal."""
    return [dict(item) for item in receipts if item.get("outcome") == "refused"]
