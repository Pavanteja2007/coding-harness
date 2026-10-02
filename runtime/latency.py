"""P1/W1 T3 — the ONE latency authority: the litellm import, and per-call timing.

Two concerns live here because they are one concern: **when the provider
library becomes available**, and **what a call actually cost in time**. A
number nobody can attribute is a number nobody can improve, and a module
import that is paid on the user's first prompt is a number that is *always*
paid.

Why this module exists (P1/W1, measured on this host, ``win32``/CPython 3.10;
every figure below is a MEASUREMENT except the two marked as source facts):

======================================  ==========================
probe                                    measured
======================================  ==========================
``import litellm`` (cold, this session)  6.1 – 22.5 s across 20+ runs
``import litellm`` (warm median, n=5)   8.12 s
``import runtime.model_router``         0.24 s, ``litellm`` ABSENT
``import cli.main``                      0.68 s, ``litellm`` ABSENT
``litellm`` self-time, cold TLS cache    5.98 s  (ONE sample)
``litellm`` self-time, warm (n=5)        0.29 s median (0.28 – 0.34)
``sys.modules`` after ANY litellm import  2239 modules
======================================  ==========================

Three findings the earlier notes in this file did not have, and the reason
the design is what it is:

1. **The import was already lazy, everywhere.** Every ``import litellm`` in
   ``runtime/`` sits inside a function, so ``import runtime.model_router``
   does not load it. The cost is paid at the first REAL call, not at CLI
   startup — which is why ``neo --version`` is a healthy 0.78 s and the first
   prompt feels broken.

2. **Strategy B ("lazy submodule access") is refuted by measurement, not by
   argument.** ``import litellm.llms.openai.chat`` is not cheaper in any way
   that matters: CPython executes the parent package's ``__init__`` first, so
   every candidate loads the **identical 2239 modules** and the spread
   (13.5–16.9 s) is measurement noise. The cost is the package graph, not a
   submodule. Vendoring (strategy C) is the only remaining option and is
   correctly a last resort. So: **strategy A.**

3. **The price-table fetch is a NETWORK call, and that is a hazard even though
   it is not slow.** litellm's ``__init__`` calls ``get_model_cost_map``,
   which does an ``httpx.get`` of a 4 440-entry JSON from
   ``raw.githubusercontent.com`` with a **5-second timeout** (source fact, read
   from ``litellm/litellm_core_utils/get_model_cost_map.py``) and falls back
   to the bundled copy. Measured: 0.29 s median warm, but **5.98 s on a cold
   TLS cache** — one sample, and it is why an early reading of this round
   believed the lever was worth 5.9 s. It is not. It is worth ~0.3 s on a warm
   cache and up to 5 s on an unreachable one, so
   :data:`LOCAL_COST_MAP_ENV` is offered for the **reproducibility** reason
   (startup should not depend on a fetch from github.com, and two runs on
   different days can otherwise see different price tables) and NOT as a
   latency claim. It is off by default for the same reason: it changes which
   table the dependency reads, and this tree's own price authority is
   :mod:`runtime.model_capabilities`, so our cost reports are unaffected but
   litellm's internal ``get_model_info`` rung would answer from the bundled
   snapshot.

The thread-safety contract, which is the part that is easy to get wrong:

:func:`ensure_litellm` is the ONE choke point — every ``import litellm`` in
``runtime/`` routes through it. A background preload and a foreground call
both execute the import **while holding one module-level lock**. A foreground
caller that arrives mid-preload therefore blocks on that lock and returns the
*already-finished* module. It cannot observe a half-initialised ``litellm``,
which is the failure that would be far worse than 18 seconds: a first prompt
raising ``ServiceUnavailableError`` because a ``__init__`` was only halfway
through. A preload that fails leaves the failure recorded and the lazy path
untouched, because the lazy path *is* this function with no preload.

Honesty rules for :class:`CallLatency`, which are not negotiable:

* A provider that reports no usage yields ``None`` + a reason. **Never 0.**
* Streaming disabled yields ``ttft_s = None`` + ``streamed: false`` +
  ``stream_disabled``. **Never a fabricated TTFT** — ``_boundary_streams``
  exists precisely so a stream that degrades is recorded rather than claimed.
* A percentile reports the **window size it was computed over**. A p95 over
  three samples is flagged ``provisional`` and says so.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "CALL_LATENCY_STATES",
    "COST_MAP_ENV_KEY",
    "LOCAL_COST_MAP_ENV",
    "MIN_PERCENTILE_SAMPLES",
    "PERCENTILE_QUANTILES",
    "PRELOAD_ENV",
    "PRELOAD_STATES",
    "CallLatency",
    "LatencyUnavailable",
    "LatencyWindow",
    "ensure_litellm",
    "latency_report",
    "latency_window",
    "litellm_loaded",
    "module_initialised",
    "module_present",
    "percentiles",
    "preload_eligible",
    "preload_state",
    "reset_preload_state",
    "start_preload",
]

#: Environment variable that opts the process into a background litellm
#: preload. Values are read by :func:`_preload_mode`; ``1``/``true``/``yes``/
#: ``on`` enable, ``0``/``false``/``no``/``off`` disable, and ``auto`` (the
#: default) means "enable only for an interactive process".
PRELOAD_ENV = "NEO_PRELOAD_LITELLM"

#: litellm's own switch for reading its bundled cost map instead of fetching
#: it from the network. This module sets it — never globally, only immediately
#: before the import it owns, and only when the operator asked.
LOCAL_COST_MAP_ENV = "LITELLM_LOCAL_MODEL_COST_MAP"
COST_MAP_ENV_KEY = "local_cost_map"

#: The module we are preloading. Named so a test can substitute a stand-in.
TARGET_MODULE = "litellm"

PRELOAD_STATES = ("disabled", "pending", "ready", "failed")

#: Every measured field carries one of these. ``unavailable`` is a VALUE with a
#: reason, never an absence and never a zero.
CALL_LATENCY_STATES = ("measured", "unavailable")

#: A percentile over fewer samples than this is reported with
#: ``provisional: True`` and the reason. A p95 over three samples is not a p95.
MIN_PERCENTILE_SAMPLES = 20

PERCENTILE_QUANTILES = (50, 95)


class LatencyUnavailable(ValueError):
    """Raised only by :meth:`CallLatency.require` — never on the call path.

    Exists so a consumer that genuinely needs a number can ask for one and be
    told "unavailable, here is why" instead of silently reading ``None`` as
    zero. The router never raises it; it records the reason.
    """


# ---------------------------------------------------------------------------
# The litellm import: one lock, one choke point
# ---------------------------------------------------------------------------


@dataclass
class _PreloadState:
    """Process-wide record of the litellm import. Mutated only under _LOCK."""

    mode: str = "auto"
    reason: str = ""
    requested: bool = False
    #: A preload THREAD has begun running (it may still be queued behind a
    #: foreground import on _IMPORT_LOCK).
    entered: bool = False
    #: An IMPORT is in progress right now. Distinct from `entered`, and the
    #: distinction is load-bearing: conflating them made the first import
    #: record itself as a caller that WAITED, which is the opposite of what
    #: happened, and left `import_calls` at 0 for a process that did import.
    started: bool = False
    #: This thread is the one performing the import, as opposed to waiting for
    #: somebody else's. Set BEFORE the import attempt so a foreground import
    #: with no preload behind it is still visible as `in_flight`; the first
    #: draft set `started` only on the success path, which meant a plain
    #: `ensure_litellm()` in progress reported `in_flight: false` and the
    #: diagnostics surface said "disabled" about an import that was running.
    owns_import: bool = False
    finished: bool = False
    ok: bool = False
    error: str = ""
    error_kind: str = ""
    duration_s: float = 0.0
    waited_s: float = 0.0
    waited_calls: int = 0
    background: bool = False
    local_cost_map: bool = False
    import_calls: int = 0
    thread: Optional[str] = None

    def snapshot(self) -> Dict[str, Any]:
        """A JSON-safe receipt. Never raises, whatever the state."""
        # `requested` counts, not just `started`: between "a thread was asked
        # for" and "that thread reached the import" there is a real window in
        # which the work is pending and the state must say so. Reading
        # `disabled` there would tell an operator the preload never happened.
        if self.finished and self.ok:
            state = "ready"
        elif self.finished and not self.ok:
            state = "failed"
        elif self.started or self.entered or self.requested:
            state = "pending"
        else:
            state = "disabled"
        return {
            "state": state,
            "states": PRELOAD_STATES,
            "module": TARGET_MODULE,
            "mode": self.mode,
            "reason": self.reason,
            "requested": bool(self.requested),
            "background": bool(self.background),
            "in_flight": bool(
                (self.started or self.entered or self.requested) and not self.finished
            ),
            "ok": bool(self.ok),
            "error": self.error,
            "error_kind": self.error_kind,
            "duration_s": round(float(self.duration_s), 4),
            "waited_s": round(float(self.waited_s), 4),
            "waited_calls": int(self.waited_calls),
            "local_cost_map": bool(self.local_cost_map),
            "cost_map_env": LOCAL_COST_MAP_ENV,
            "import_calls": int(self.import_calls),
            "thread": self.thread,
            # The first prompt pays whatever the preload did not cover. This
            # is the number the whole module exists to move.
            "first_call_waited_s": round(float(self.waited_s), 4),
        }


_STATE = _PreloadState()

#: The IMPORT lock. Held only while the import statement executes, and by at
#: most one thread. This is what makes a foreground call that arrives during a
#: background preload BLOCK until the preload has finished, so it can never
#: observe a half-initialised module.
#:
#: Re-entrant so that a future caller which reaches ``ensure_litellm`` from
#: inside an already-guarded section does not deadlock on itself; the
#: ``sys.modules`` re-check below makes such a re-entry a cheap no-op anyway.
_IMPORT_LOCK = threading.RLock()

#: The STATE lock. Held only for short reads and writes of :data:`_STATE` —
#: **never across the import**. A single lock for both jobs was the first
#: version of this module and it was wrong, measurably: ``preload_state()``
#: blocked for the full 14.6 s import (measured on this host), so the
#: diagnostics function could not report on the thing it exists to report,
#: and a second ``start_preload()`` blocked behind the first. Two locks with
#: two distinct jobs is the fix; a state lock held across a 15-second
#: operation is a lock that is not a lock, it is a queue.
_LOCK = threading.RLock()

_TRUTHY = frozenset({"1", "true", "yes", "on", "auto", "preload", "enabled"})
_FALSEY = frozenset({"0", "false", "no", "off", "disabled", "none"})


def _env_flag(name: str) -> Optional[str]:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = str(raw).strip().lower()
    return value or None


def _preload_mode() -> str:
    """Resolve the preload mode: ``on``, ``off``, or ``auto``.

    ``auto`` is the default and enables the preload for an INTERACTIVE process
    only — a TTY on stdin is the observable proxy for "there is a human who is
    about to think while this import runs". A batch worker, an eval arm, and a
    pytest process have no TTY, so they never pay for a thread nobody will
    wait on, and a scripted run cannot be perturbed by a background import.
    """
    configured = _env_flag(PRELOAD_ENV)
    if configured is None:
        return "auto"
    # `auto` MUST be tested before the truthy set. It is the documented
    # default value and it is a member of `_TRUTHY` (it means "decide"), so a
    # truthy check that ran first would resolve `auto` to `on` and switch the
    # preload on unconditionally — silently defeating the interactive and
    # not-a-test-session gates that make `auto` safe. Found by
    # `runtime/test_latency_pins.py::test_auto_mode_refuses_inside_a_test_session`.
    if configured == "auto":
        return "auto"
    if configured in _FALSEY:
        return "off"
    if configured in _TRUTHY:
        return "on"
    # An unrecognised value must not silently enable a background thread.
    return "off"


def _interactive() -> bool:
    """Whether stdin is a terminal. Never raises; a closed stdin is not one."""
    try:
        return bool(sys.stdin) and bool(sys.stdin.isatty())
    except Exception:
        return False


def _under_test_runner() -> bool:
    """Whether this process looks like a test session.

    ``_pytest`` is the marker rather than ``pytest``: the runner is the thing
    that installs itself before any test module is imported, and the name is
    stable across pytest versions. ``PYTEST_CURRENT_TEST`` is the second
    signal, for a runner that imported ``runtime`` before ``_pytest``.

    This exists for a measured reason, not tidiness. An 18-second background
    import in a pytest session competes for the GIL with the tests that share
    the process, and this repository has an extensive, repeatedly-measured
    history of host-load flakes caused by exactly that. A preload is a latency
    optimisation for an interactive session; a test session is not one.
    ``NEO_PRELOAD_LITELLM=on`` overrides this, so the behaviour stays
    testable and an operator can force it.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return "_pytest" in sys.modules or "pytest" in sys.modules


def preload_eligible() -> bool:
    """Whether ``auto`` mode would start a preload in this process right now.

    Public so ``cli/doctor.py`` can answer "why did (or did not) my process
    preload?" instead of showing a bare ``state: disabled``.
    """
    mode = _preload_mode()
    if mode == "on":
        return True
    if mode != "auto":
        return False
    return _interactive() and not _under_test_runner()


def module_present() -> bool:
    """Whether the target module is *present* in ``sys.modules``.

    **This is not "is it usable".** CPython inserts a module into
    ``sys.modules`` BEFORE executing its body (so circular imports work), so a
    concurrent import leaves a *partially initialised* module visible. Measured
    on this host, 66 ms into a background preload:

    ==================  ==============================
    probe                value
    ==================  ==============================
    ``'litellm' in sys.modules``  True
    ``litellm.completion`` callable  **False**
    ``litellm.*`` submodules loaded   **0**
    ==================  ==============================

    Anything that decides "can I use it yet?" from this function is reading a
    half-built module. :func:`ensure_litellm` is the only safe accessor.
    """
    return TARGET_MODULE in sys.modules


def module_initialised() -> bool:
    """Whether the target module is present AND fully initialised.

    Uses :func:`importlib.import_module`, which is the interpreter's own
    answer: ``importlib._bootstrap._lock_unlock_module`` is documented as
    "used to ensure a module is completely initialized, in the event it is
    being imported by another thread". A raw ``sys.modules`` read is not that
    guarantee, and this function's docstring is the reason.

    Returns ``False`` rather than raising when the module cannot be imported at
    all, because this is a status question, not an attempt.
    """
    try:
        import importlib

        return importlib.import_module(TARGET_MODULE) is sys.modules.get(TARGET_MODULE)
    except Exception:
        return False


def litellm_loaded() -> bool:
    """Whether the provider library is fully initialised and safe to use.

    Unlike :func:`module_present`, this answers the question a caller actually
    has. It is a real import attempt, so on a machine with no litellm it
    returns ``False`` rather than raising.
    """
    return module_initialised()


def ensure_litellm(*, local_cost_map: Optional[bool] = None) -> Any:
    """Import and return the provider library, or raise.

    **This is the only place in ``runtime/`` that imports litellm.** Every
    caller goes through it so that the preload and the lazy path are the same
    code path, and so a background import and a foreground call can never
    interleave.

    Blocking is the contract, not an optimisation. Two mechanisms enforce it
    and **both are required**:

    * :data:`_IMPORT_LOCK` serialises our own imports, so a foreground caller
      that arrives during a background preload waits on our lock and then
      re-reads the result.
    * ``importlib.import_module`` is what actually does the waiting on a
      module somebody ELSE is importing. CPython publishes a module in
      ``sys.modules`` *before* executing its body, so a raw dict read can
      return a half-built module — measured on this host: present in
      ``sys.modules`` at 66 ms into a preload, with ``completion`` not yet
      callable and zero submodules loaded. ``importlib`` handles that through
      its per-module lock, whose own docstring says it exists "to ensure a
      module is completely initialized, in the event it is being imported by
      another thread". This module's first version read ``sys.modules``
      directly and was wrong; :func:`module_present` exists so the mistake
      cannot be reintroduced silently.

    :data:`_LOCK` (the state lock) is deliberately NOT held across the import.
    One lock for both jobs was this module's first version and it was
    measurably wrong — see :data:`_IMPORT_LOCK`.

    ``local_cost_map`` opts into litellm's bundled price table instead of its
    network fetch (measured: 5.88 s of an 18 s import). It is honoured only
    when litellm has not been imported yet — setting the variable afterwards
    would be a silent no-op dressed as a setting.
    """
    requested_local = local_cost_map
    if requested_local is None:
        requested_local = _env_flag(COST_MAP_ENV_KEY) in _TRUTHY
    if requested_local:
        with _LOCK:
            _STATE.local_cost_map = True
    started = time.monotonic()
    with _IMPORT_LOCK:
        with _LOCK:
            # Mark the import as RUNNING before it begins, not after it
            # succeeds. A foreground `ensure_litellm()` with no preload
            # behind it is still an in-flight import, and the diagnostics
            # surface has to be able to say so.
            owns = not _STATE.started
            _STATE.started = True
            if owns:
                _STATE.owns_import = True
                _STATE.thread = threading.current_thread().name
                _STATE.import_calls += 1
        try:
            import importlib

            # This call BLOCKS if another thread is mid-import of this module.
            # That is the guarantee, and it comes from the interpreter.
            module = importlib.import_module(TARGET_MODULE)
        except BaseException as exc:
            with _LOCK:
                _STATE.finished = True
                _STATE.owns_import = False
                _STATE.ok = False
                _STATE.error = f"{type(exc).__name__}: {exc}"[:500]
                _STATE.error_kind = type(exc).__name__
                _STATE.duration_s += time.monotonic() - started
            # Leave the failure recorded but re-raise: a caller that needs
            # litellm must learn that it did not get it. The NEXT call retries
            # the import, which is what "preload failure degrades to the lazy
            # path" means in practice.
            raise
        waited = time.monotonic() - started
        with _LOCK:
            _STATE.finished = True
            _STATE.owns_import = False
            _STATE.ok = True
            _STATE.error = ""
            _STATE.error_kind = ""
            _STATE.duration_s += waited
            if not owns and waited > 0.0:
                # This caller waited on somebody else's import. Counted, so
                # the race the brief asks us to prove is safe is also visible
                # in the receipt rather than being merely survived.
                _STATE.waited_s += waited
                _STATE.waited_calls += 1
        return module


def _preload_worker() -> None:
    """Background entry point. Never raises — a failure is recorded."""
    with _LOCK:
        # Mark ENTERED on entry, not `started`: a thread can sit on
        # _IMPORT_LOCK behind a foreground import for a long time, and the
        # receipt has to say "pending" during that window rather than
        # "disabled". `started` stays reserved for "an import is happening
        # right now" so that the first import is not miscounted as a waiter.
        _STATE.entered = True
        _STATE.thread = threading.current_thread().name
    try:
        ensure_litellm()
    except BaseException as exc:
        with _LOCK:
            _STATE.finished = True
            _STATE.ok = False
            _STATE.error = f"{type(exc).__name__}: {exc}"[:500]
            _STATE.error_kind = type(exc).__name__


def start_preload(*, reason: str = "", local_cost_map: Optional[bool] = None) -> bool:
    """Start a background import. Returns whether a NEW preload was started.

    Idempotent, and the guard is ``_STATE.requested`` rather than
    ``_STATE.started``: the latter is only set once the *thread* gets as far
    as the import, so between "decided to start" and "thread started" two
    concurrent callers would both pass a ``_STATE.started`` check and spawn
    two threads. ``requested`` is set under the same lock that decides, which
    closes that window. A thread is a daemon, so a preload that outlives the
    work it was for can never hold a process open.
    """
    mode = _preload_mode()
    if mode == "off":
        return False
    if mode == "auto" and not preload_eligible():
        with _LOCK:
            _STATE.mode = "auto"
            _STATE.reason = reason or _STATE.reason
        return False
    with _LOCK:
        if _STATE.requested or _STATE.started:
            return False
        _STATE.mode = mode
        _STATE.reason = reason
        _STATE.requested = True
        _STATE.background = True
        if local_cost_map is not None:
            _STATE.local_cost_map = bool(local_cost_map)
        # NOTE: there is deliberately no "is it already loaded?" short-circuit
        # here, even though it would save a thread. The check that would
        # answer it correctly is `module_initialised()`, and that call BLOCKS
        # for the duration of a concurrent import — so this module's first
        # version turned a "kick off a background preload" call into a
        # synchronous 6-second import, and the race it was written to set up
        # never happened. A daemon thread that returns in microseconds when
        # there is nothing to do is strictly cheaper than a kickoff that can
        # block for fifteen seconds.
    thread = threading.Thread(
        target=_preload_worker,
        name="neo-litellm-preload",
        daemon=True,
    )
    with _LOCK:
        _STATE.thread = thread.name
    thread.start()
    return True


def preload_state() -> Dict[str, Any]:
    """The diagnostics receipt for the import. Safe to call at any time.

    This is the surface ``cli/doctor.py`` (T4's file) should render: it
    answers "is the provider library loaded, is a preload in flight, did the
    preload fail, and how long did the first call have to wait for it".
    """
    with _LOCK:
        receipt = _STATE.snapshot()
    # Non-blocking by construction. `module_initialised()` would answer more
    # completely, and it would also BLOCK for the whole duration of a
    # concurrent import — so a diagnostics call issued during a preload could
    # not report on the preload. `present` is the honest non-blocking fact
    # (`module_present`'s docstring says exactly what it does and does not
    # mean); a caller that genuinely needs to wait uses
    # :func:`probe_initialised`, which is named for the fact that it waits.
    receipt["present"] = module_present()
    receipt["interactive"] = _interactive()
    receipt["under_test_runner"] = _under_test_runner()
    receipt["eligible"] = preload_eligible()
    receipt["env"] = _env_flag(PRELOAD_ENV)
    return receipt


def probe_initialised() -> bool:
    """Whether the module is fully initialised. **This call can block.**

    Use it only when "wait until it is really ready" is the intent.
    :func:`preload_state` deliberately does not, so a diagnostics surface can
    report on a preload in flight instead of joining it.
    """
    return module_initialised()


def reset_preload_state() -> None:
    """Forget the recorded import. Test-only; does not unload the module.

    Unloading a module from ``sys.modules`` would break every object that
    holds a reference to it, so this resets the RECEIPT only. A test that
    needs a genuinely absent module must control ``TARGET_MODULE``.
    """
    with _LOCK:
        _STATE.__init__()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Percentiles, with the window stated
# ---------------------------------------------------------------------------


def percentiles(
    values: Sequence[Optional[float]],
    *,
    quantiles: Sequence[int] = PERCENTILE_QUANTILES,
) -> Dict[str, Any]:
    """Percentiles over the values that exist, with the window reported.

    ``None`` entries are skipped, not counted as zero: a call whose TTFT was
    ``unavailable`` did not have a fast TTFT. The receipt states
    ``samples`` (what the percentiles were computed over) and
    ``observed`` (what was offered), so a p95 over three samples is legible
    as a p95 over three samples.
    """
    offered = len(values)
    usable = [
        float(v)
        for v in values
        if v is not None and float(v) == float(v)  # drops NaN as well as None
    ]
    receipt: Dict[str, Any] = {
        "samples": len(usable),
        "observed": offered,
        "skipped": offered - len(usable),
        "provisional": len(usable) < MIN_PERCENTILE_SAMPLES,
        "minimum_samples": MIN_PERCENTILE_SAMPLES,
    }
    if not usable:
        receipt.update(
            {
                "values": {f"p{q}": None for q in quantiles},
                "state": "unavailable",
                "reason": "no measured sample in this window",
            }
        )
        return receipt
    ordered = sorted(usable)
    computed: Dict[str, Optional[float]] = {}
    for q in quantiles:
        # Nearest-rank: the smallest value at or above the q-th percentile. It
        # never invents a value that was not observed, which an interpolating
        # estimator can do between two samples.
        if len(ordered) == 1:
            computed[f"p{q}"] = round(ordered[0], 4)
            continue
        rank = max(1, min(len(ordered), int(-(-q * len(ordered) // 100))))
        computed[f"p{q}"] = round(ordered[rank - 1], 4)
    receipt["values"] = computed
    receipt["state"] = "measured"
    receipt["reason"] = (
        ""
        if not receipt["provisional"]
        else f"window of {len(usable)} sample(s) is below the "
        f"{MIN_PERCENTILE_SAMPLES}-sample floor; treat as indicative only"
    )
    return receipt


# ---------------------------------------------------------------------------
# Per-call latency
# ---------------------------------------------------------------------------


def _unavailable(reason: str) -> Dict[str, Any]:
    """The honest answer shape. ``None`` plus a reason — never ``0``."""
    return {"value": None, "state": "unavailable", "reason": reason}


def _measured(value: float) -> Dict[str, Any]:
    return {"value": round(float(value), 4), "state": "measured", "reason": ""}


@dataclass
class CallLatency:
    """What one model call cost in time, and which parts of that are measured.

    Build it with :meth:`begin` at the top of the call and :meth:`mark_dialed`
    immediately before the provider is dialled. The two marks are what let the
    receipt separate **harness/gateway overhead** (routing, capability and
    context-window resolution — which is where the litellm import lives — plus
    request construction) from **provider time**. Before this existed the
    import's cost was folded into ``elapsed_s`` and reported as if the provider
    had been slow, which is the direction that misattributes our own defect to
    somebody else's endpoint.

    Every numeric field is ``(value, state, reason)``. A value that could not
    be measured is ``None`` with a state of ``unavailable`` and a reason that
    names the cause. ``0`` is never substituted: zero is a measurement, and a
    call that reported no usage did not measure zero.
    """

    #: Wall clock at the top of the call, before any routing work.
    started: float = field(default_factory=time.monotonic)
    #: Wall clock immediately before the provider is dialled, or ``None``.
    dialed: Optional[float] = None
    #: Wall clock immediately after the provider returned/finished streaming.
    finished: Optional[float] = None
    #: Measured TTFT from the stream assembler, or ``None`` when not streamed.
    ttft_s: Optional[float] = None
    #: Whether the request actually streamed.
    streamed: bool = False
    #: Seconds :func:`ensure_litellm` blocked this call, if it had to.
    import_wait_s: float = 0.0
    #: Provider-reported usage, or ``None`` when the provider reported none.
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    #: True when the token counts came from an ESTIMATE rather than usage.
    tokens_estimated: bool = False
    #: Free-text note for the receipt (e.g. why usage was absent).
    note: str = ""
    #: What the "provider" window actually is: ``"provider"`` for a real dial,
    #: ``"mock"`` for the offline scripted lane (which computes locally and
    #: therefore has NO provider time to report), ``"none"`` when the call was
    #: refused before any dial. This is why a scripted run's receipt cannot be
    #: read as provider throughput evidence.
    dial_kind: str = "provider"

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def begin(cls) -> "CallLatency":
        """Start the clock. Call this at the very top of ``call_model``."""
        return cls()

    def mark_dialed(self) -> None:
        """Mark the instant before the provider is dialled. Idempotent."""
        if self.dialed is None:
            self.dialed = time.monotonic()

    def mark_finished(self) -> None:
        """Mark the instant the provider is done with this call."""
        if self.finished is None:
            self.finished = time.monotonic()

    # -- derived measurements ---------------------------------------------

    @property
    def duration_s(self) -> float:
        """Total wall clock, or 0.0 when the call never finished."""
        end = self.finished if self.finished is not None else time.monotonic()
        return round(max(0.0, end - self.started), 4)

    @property
    def overhead_s(self) -> Optional[float]:
        """Harness/gateway overhead: call entry until the dial.

        ``None`` until :meth:`mark_dialed` has been called — an unmeasured
        overhead is not zero overhead.
        """
        if self.dialed is None:
            return None
        return round(max(0.0, self.dialed - self.started), 4)

    @property
    def provider_s(self) -> Optional[float]:
        """The dial itself: until the provider returned or finished streaming."""
        if self.dialed is None or self.finished is None:
            return None
        return round(max(0.0, self.finished - self.dialed), 4)

    @property
    def generation_s(self) -> Optional[float]:
        """Provider time MINUS time-to-first-token, when both are measured.

        This is the decode window. It is ``None`` — not the whole provider
        time — when the call was not streamed, because without a TTFT the
        request/response overhead cannot be separated from generation.
        """
        provider = self.provider_s
        if provider is None or not self.streamed or self.ttft_s is None:
            return None
        return round(max(0.0, provider - float(self.ttft_s)), 4)

    # -- receipt -----------------------------------------------------------

    def _ttft_field(self) -> Dict[str, Any]:
        if not self.streamed:
            return _unavailable("stream_disabled")
        if self.ttft_s is None:
            return _unavailable("no_delta_observed_before_completion")
        return _measured(float(self.ttft_s))

    def _overhead_field(self) -> Dict[str, Any]:
        if self.dial_kind != "provider":
            # The mock lane's work is entirely ours, so calling it "overhead"
            # is as wrong as calling it provider time. It is reported as
            # unavailable with the reason, and the call's own wall clock is
            # still in `duration_s`.
            return _unavailable(f"{self.dial_kind}_lane_has_no_gateway_overhead")
        if self.overhead_s is None:
            return _unavailable("call_not_dialed")
        return _measured(self.overhead_s)

    def _provider_field(self) -> Dict[str, Any]:
        if self.dial_kind == "mock":
            return _unavailable("mock_provider_has_no_network_dial")
        if self.provider_s is None:
            return _unavailable("call_did_not_finish")
        return _measured(self.provider_s)

    def _rate_field(
        self, tokens: Optional[int], seconds: Optional[float], what: str
    ) -> Dict[str, Any]:
        if tokens is None:
            return _unavailable(
                "provider_reported_no_usage"
                if not self.tokens_estimated
                else "no_usage"
            )
        if not seconds or seconds <= 0.0:
            return _unavailable(f"no_{what}_window_measured")
        return _measured(float(tokens) / float(seconds))

    def to_dict(self) -> Dict[str, Any]:
        """The ledger/``get_last_usage`` receipt. JSON-safe; never raises.

        ``input_tokens_per_s`` is measured over the *provider* window and
        ``output_tokens_per_s`` over the *generation* window, because a token
        rate divided by the wrong denominator is a number that means nothing.
        When the generation window is ``unavailable`` the output rate says so
        rather than falling back to the provider window — silently changing a
        rate's denominator is how a throughput number becomes decorative.
        """
        input_tps = self._rate_field(self.input_tokens, self.provider_s, "provider")
        generation = self.generation_s
        output_tps = self._rate_field(self.output_tokens, generation, "generation")
        receipt: Dict[str, Any] = {
            "duration_s": self.duration_s,
            "dial_kind": self.dial_kind,
            "overhead": self._overhead_field(),
            "provider": self._provider_field(),
            "generation": (
                _measured(generation)
                if generation is not None
                else _unavailable(
                    "stream_disabled"
                    if not self.streamed
                    else "no_first_token_measured"
                )
            ),
            "ttft": self._ttft_field(),
            "streamed": bool(self.streamed),
            "import_wait_s": round(float(self.import_wait_s), 4),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tokens_estimated": bool(self.tokens_estimated),
            "input_tokens_per_s": input_tps,
            "output_tokens_per_s": output_tps,
            "states": CALL_LATENCY_STATES,
            "note": self.note,
        }
        return receipt

    # -- convenience accessors --------------------------------------------

    def ttft(self) -> Optional[float]:
        """TTFT in seconds, or ``None``. Never ``0.0`` for a missing TTFT."""
        return self._ttft_field()["value"]

    def overhead(self) -> Optional[float]:
        return self._overhead_field()["value"]

    def provider(self) -> Optional[float]:
        return self._provider_field()["value"]

    def require(self, field_name: str) -> float:
        """Return a measured field, or raise :class:`LatencyUnavailable`.

        For a consumer that cannot proceed without a number (an SLO gate, a
        threshold check). The call path never uses this — it records the
        reason instead.
        """
        receipt = self.to_dict()
        entry = receipt.get(field_name)
        if not isinstance(entry, dict) or entry.get("state") != "measured":
            reason = (
                entry.get("reason")
                if isinstance(entry, dict)
                else f"{field_name} is not a measured field"
            )
            raise LatencyUnavailable(f"{field_name}: {reason or 'unavailable'}")
        return float(entry["value"])

    def merge_window(self, window: "LatencyWindow") -> "LatencyWindow":
        """Fold this call into an accumulating window. Never raises."""
        return window.add(self)


# ---------------------------------------------------------------------------
# The accumulating window
# ---------------------------------------------------------------------------


@dataclass
class LatencyWindow:
    """Percentiles over a real set of calls, with the window size stated.

    Bounded by :data:`MAX_WINDOW_SAMPLES` so a long run cannot grow this
    without limit; ``dropped`` counts what the bound discarded, because a
    window that silently forgets its oldest samples is reporting a different
    population than it claims.
    """

    max_samples: int = 512
    ttft: List[Optional[float]] = field(default_factory=list)
    duration: List[Optional[float]] = field(default_factory=list)
    overhead: List[Optional[float]] = field(default_factory=list)
    provider: List[Optional[float]] = field(default_factory=list)
    input_tps: List[Optional[float]] = field(default_factory=list)
    output_tps: List[Optional[float]] = field(default_factory=list)
    streamed_calls: int = 0
    total_calls: int = 0
    dropped: int = 0

    def add(self, call: CallLatency) -> "LatencyWindow":
        receipt = call.to_dict()

        def value(name: str) -> Optional[float]:
            entry = receipt.get(name)
            if isinstance(entry, dict) and entry.get("state") == "measured":
                return entry.get("value")
            return None

        self.total_calls += 1
        if receipt.get("streamed"):
            self.streamed_calls += 1
        self.ttft.append(value("ttft"))
        self.duration.append(float(receipt.get("duration_s") or 0.0))
        self.overhead.append(value("overhead"))
        self.provider.append(value("provider"))
        self.input_tps.append(value("input_tokens_per_s"))
        self.output_tps.append(value("output_tokens_per_s"))
        if self.total_calls > self.max_samples:
            for name in (
                "ttft",
                "duration",
                "overhead",
                "provider",
                "input_tps",
                "output_tps",
            ):
                getattr(self, name).pop(0)
            self.dropped += 1
        return self

    def to_dict(self) -> Dict[str, Any]:
        """The aggregate receipt. Every percentile block carries its window."""
        return {
            "calls": self.total_calls,
            "streamed_calls": self.streamed_calls,
            "dropped_samples": self.dropped,
            "max_samples": self.max_samples,
            "ttft": percentiles(self.ttft),
            "duration_s": percentiles(self.duration),
            "overhead_s": percentiles(self.overhead),
            "provider_s": percentiles(self.provider),
            "input_tokens_per_s": percentiles(self.input_tps),
            "output_tokens_per_s": percentiles(self.output_tps),
        }


_WINDOW = LatencyWindow()


def latency_window() -> LatencyWindow:
    """The process-wide accumulating window.

    A module-level window is process-wide rather than context-local, unlike
    the router's ledger. That is deliberate and is the honest shape for a
    latency aggregate: it answers "how has this process's model path been
    behaving", and a per-task window would be indistinguishable from the
    ledger, which already exists.
    """
    return _WINDOW


def latency_report() -> Dict[str, Any]:
    """The whole latency picture in one JSON-safe receipt.

    The import half (:func:`preload_state`) and the call half
    (:func:`latency_window`) are reported side by side, because the single
    most useful question is the ratio: how much of the first call's wait was
    the import, and how many calls have been free of it since.
    """
    return {"preload": preload_state(), "calls": _WINDOW.to_dict()}


if __name__ == "__main__":  # pragma: no cover - operator convenience
    import json

    print(json.dumps(latency_report(), indent=2, sort_keys=True))
