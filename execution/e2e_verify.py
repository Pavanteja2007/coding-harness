"""P4/W1 T2.W1.1 — end-to-end verification. Does the thing WORK?

Anthropic named the exact failure Phase 4 exists to fix:

    *"One final major failure mode that we observed was Claude's tendency to
    mark a feature as complete without proper testing ... would fail to
    recognize that the feature didn't work end-to-end."*

    *"it made the agent always start the local development server and use the
    Puppeteer MCP to start a new chat, send a message, and receive a
    response. This ensured that Claude could quickly identify if the app had
    been left in a broken state."*

``execution/verify.py`` answers *"do the tests pass?"* — the correctness of the
code **as tested**. This module answers *"does the thing work?"* — the
correctness of **the behaviour a user sees**.

--------------------------------------------------------------------------
THE DISTINCTION IS LOAD-BEARING, AND IT IS SYMMETRIC
--------------------------------------------------------------------------

``E2E_VERIFY_DISTINCTION`` states it, and :func:`distinction_lines` renders it
for a surface. Both directions are enforced, not just described:

* a repository that runs ONLY e2e has **flaky signal** — an e2e check starts
  servers, waits on ports and touches the whole stack, so a passing e2e proves
  nothing about whether a change broke an edge case a unit test covers;
* a repository that runs ONLY unit verification **misses integration failures
  entirely** — the suite can be entirely green over a feature whose server does
  not start.

So :func:`neither_substitutes` is the checkable form: given a verify result and
an e2e report, it refuses any claim that one answers for the other. And
``execution.verification_evidence.ledger_evidence`` refuses to call a feature
``passing`` unless BOTH settled.

--------------------------------------------------------------------------
FIVE STATES, AND `blocked` IS NOT `false`
--------------------------------------------------------------------------

``E2E_STATES`` is a closed FIVE-value vocabulary:

============  =============================================================
``passed``    the check ran and the feature behaved.
``failed``    the check ran and the feature did not behave.
``blocked``   the check could not run — with a MANDATORY reason.
``not_run``   the check was not attempted (no declaration, or the run policy
              said not to).
``untrustworthy``  **Wave 2.** the check ran (or could have) and its verdict is
              VOID in both directions. It is not a passing feature and not a
              broken one.
============  =============================================================

``blocked`` and ``failed`` are recorded differently by Phase 3's ledger
because they are different facts: "the feature is broken" and "I could not
check" cannot both be repaired by fixing the feature. A ``blocked`` e2e check
**never** becomes ``failed``, and an absent check is ``not_run`` rather than a
pass.

``untrustworthy`` is the fifth fact of the same kind, and it is the one Wave 1
could not name. Its whole argument is one sentence: **the check ran, exited 0,
and asserts nothing.** That is not ``passed`` — reporting it as ``passed``
converts an honest gap into a false pass, which is the specific failure this
project exists to prevent. It is also not ``failed``: nothing was shown to be
broken, and a ledger that renders it ``failed`` sends a repair loop after a
working tree. So it is a third thing, and it DOMINATES the report verdict: a
report containing one green row and one void row must not read ``passed``.

The state is decided by :mod:`execution.e2e_trust`, which owns the three
questions that produce it — is this check the repository's or the agent's
(rows 1-3), is it strong enough to decide anything (row 4), and did it run for
a plausible amount of time (row 6). The two constructors that build the row are
this module's :func:`untrustworthy_result` and the trust gate's, and
``E2EResult`` REQUIRES a reason on every ``untrustworthy`` row and REFUSES a
trust receipt on any other state — a decoration on a green row is worse than
no field, because it teaches readers to skip it.

--------------------------------------------------------------------------
TIMEOUT, FLAKINESS AND ENVIRONMENT ARE SEPARATE AXES
--------------------------------------------------------------------------

An e2e check that times out is not a failing feature and not a passing one.
So ``E2EResult`` carries ``timed_out``, ``flaky_check`` and ``environment`` as
fields **independent of** ``state``, and a timeout is recorded as
``state="blocked"`` with ``timed_out=True`` and a reason — a reader can tell
"this check could not finish" from "the daemon was down" from "the feature is
broken" without parsing prose. ``flaky_check`` follows
``execution/flake_gate.py``'s rule exactly: fewer than two observations is
``not_run``, never ``not_flaky``.

--------------------------------------------------------------------------
THE RUN POLICY — SET, DECIDED, DOCUMENTED
--------------------------------------------------------------------------

An e2e check may start a server. That is slow and it is expensive, so **e2e
does NOT run on every iteration by default.** ``E2E_RUN_POLICIES`` is closed:

``on_demand``     (DEFAULT) runs only when a caller explicitly asks.
``on_final_gate`` runs at the gating verification of an attempt, not per step.
``per_iteration`` runs every time. Measured cost is in
                  ``execution/verify_cache.py``'s cost table; this is the
                  setting to use when debugging, not when shipping.

An unstated policy means users discover the cost by being surprised, so
:func:`should_run_e2e` returns the decision **with its reason** on every call,
including the ones that say no.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "DEFAULT_E2E_RERUNS",
    "DEFAULT_E2E_RUN_POLICY",
    "DEFAULT_E2E_TIMEOUT_S",
    "E2E_BLOCKED",
    "E2E_FAILED",
    "E2E_NOT_RUN",
    "E2E_PASSED",
    "E2E_RUN_POLICIES",
    "E2E_SCHEMA_VERSION",
    "E2E_SPEC_FILENAMES",
    "E2E_STATES",
    "E2E_UNTRUSTWORTHY",
    "E2E_UNTRUSTWORTHY_REASON",
    "E2E_VERIFY_DISTINCTION",
    "ENVIRONMENT_KINDS",
    "ENV_CONTAINER_BROKEN",
    "ENV_DAEMON_UNAVAILABLE",
    "ENV_NO_TOOLCHAIN",
    "ENV_OFFLINE",
    "ENV_OK",
    "ENV_PORT_UNAVAILABLE",
    "ENV_SPEC_UNREADABLE",
    "ENV_TIMEOUT_BUDGET",
    "MIN_REPETITIONS_FOR_DETECTION",
    "POLICY_ON_DEMAND",
    "POLICY_ON_FINAL_GATE",
    "POLICY_PER_ITERATION",
    "SPEC_ABSENT",
    "SPEC_FOUND",
    "SPEC_LOAD_STATUSES",
    "SPEC_MALFORMED",
    "SPEC_UNREADABLE",
    "E2ECheck",
    "E2EPolicyDecision",
    "E2EReport",
    "E2EResult",
    "E2ESpec",
    "SpecLoad",
    "check_for_spec",
    "declaration_digest",
    "distinction_lines",
    "load_e2e_spec",
    "neither_substitutes",
    "render_block",
    "render_report",
    "run_e2e",
    "run_e2e_check",
    "run_e2e_for_features",
    "should_run_e2e",
    "untrustworthy_result",
]


E2E_SCHEMA_VERSION = 1

#: Declaration files, in search order. ``.neo/e2e.json`` first because a
#: repository that adopted the convention keeps its declarations with its other
#: harness configuration rather than at the root.
E2E_SPEC_FILENAMES: Tuple[str, ...] = (
    ".neo/e2e.json",
    "e2e.json",
)


# --------------------------------------------------------------------------
# States
# --------------------------------------------------------------------------

E2E_PASSED = "passed"
E2E_FAILED = "failed"
E2E_BLOCKED = "blocked"
E2E_NOT_RUN = "not_run"
E2E_UNTRUSTWORTHY = "untrustworthy"

#: The closed FIVE-value vocabulary. Phase 3a's three boolean states are not
#: reachable from here and must not be synthesised from these.
#:
#: ``untrustworthy`` is Wave 2's fifth state and it exists because the other
#: four all describe a *measurement*, while this one describes the ABSENCE of a
#: usable measurement. A check that ran, exited 0, and asserts nothing is not
#: ``passed`` — reporting it as ``passed`` is precisely the failure this module
#: exists to prevent, because it converts an honest gap into a false pass. It is
#: also not ``failed``: nothing was shown to be broken, and a ledger that renders
#: it ``failed`` sends a repair loop to fix a working tree.
E2E_STATES: Tuple[str, str, str, str, str] = (
    E2E_PASSED,
    E2E_FAILED,
    E2E_BLOCKED,
    E2E_NOT_RUN,
    E2E_UNTRUSTWORTHY,
)

#: The sentence that has to appear on every ``untrustworthy`` row. Published so
#: the four other states' docstrings can name it and so a surface can render it
#: without re-deriving the argument.
E2E_UNTRUSTWORTHY_REASON = (
    "this check ran but cannot decide the question it was declared to decide, "
    "so its verdict is void in BOTH directions: it is not a passing feature and "
    "not a broken one. 'the thing worked' and 'the thing was checked' are "
    "different claims, and only a check that can actually fail carries the first"
)


# --------------------------------------------------------------------------
# Environment — a separate axis, not a state
# --------------------------------------------------------------------------

ENV_OK = "ok"
ENV_DAEMON_UNAVAILABLE = "daemon_unavailable"
ENV_NO_TOOLCHAIN = "no_toolchain"
ENV_OFFLINE = "offline"
ENV_PORT_UNAVAILABLE = "port_unavailable"
ENV_SPEC_UNREADABLE = "spec_unreadable"
ENV_TIMEOUT_BUDGET = "timeout_budget"
ENV_CONTAINER_BROKEN = "container_broken"

#: Closed vocabulary for WHY a check could not run. A reader must be able to
#: tell a broken machine from a missing toolchain from a feature that broke.
ENVIRONMENT_KINDS: Tuple[str, ...] = (
    ENV_OK,
    ENV_DAEMON_UNAVAILABLE,
    ENV_NO_TOOLCHAIN,
    ENV_OFFLINE,
    ENV_PORT_UNAVAILABLE,
    ENV_SPEC_UNREADABLE,
    ENV_TIMEOUT_BUDGET,
    ENV_CONTAINER_BROKEN,
)

#: The subset that means "the machine, not the feature". A check in one of
#: these is `blocked`, never `failed`.
ENVIRONMENT_FAULTS: Tuple[str, ...] = (
    ENV_DAEMON_UNAVAILABLE,
    ENV_NO_TOOLCHAIN,
    ENV_OFFLINE,
    ENV_PORT_UNAVAILABLE,
    ENV_SPEC_UNREADABLE,
    ENV_CONTAINER_BROKEN,
)


# --------------------------------------------------------------------------
# The distinction, as data
# --------------------------------------------------------------------------

E2E_VERIFY_DISTINCTION: Dict[str, Dict[str, str]] = {
    "verify": {
        "asks": "do the tests pass?",
        "measures": "the correctness of the code AS TESTED",
        "sees": "functions, units, import graphs, a fixture tree",
        "cannot_see": (
            "a server that does not start, a route that 404s, a feature wired "
            "to nothing, a database that is not migrated, a UI that renders "
            "blank — every one of those can sit behind a fully green suite"
        ),
        "cost": "a container per command; seconds to minutes",
        "stability": "high; unit tests are the most deterministic signal a repository has",
    },
    "e2e": {
        "asks": "does the thing work?",
        "measures": "the correctness of THE BEHAVIOUR A USER SEES",
        "sees": "a started server, a real request, a real response",
        "cannot_see": (
            "edge cases and error paths no check exercises; a check that "
            "asserts one happy path proves one happy path"
        ),
        "cost": (
            "one container PLUS a server startup PLUS a port allocation PLUS a "
            "readiness wait — measured in verify_cache.COST_TABLE"
        ),
        "stability": (
            "lower; a server bind race or a readiness poll is indistinguishable "
            "from a real failure unless the port is allocated per session "
            "(execution/port_alloc.py) and the reason is reported"
        ),
    },
    "neither_substitutes": {
        "e2e_only": (
            "a repository that runs only e2e has flaky signal: a passing check "
            "proves nothing about the edge cases unit tests cover, and one "
            "server-startup race reads as a broken feature"
        ),
        "verify_only": (
            "a repository that runs only unit verification misses integration "
            "failures entirely; the suite can be entirely green over a feature "
            "whose server does not start"
        ),
        "both_needed": (
            "verify answers whether the code is correct as tested; e2e answers "
            "whether the feature works. A feature may only be marked passing "
            "when BOTH have settled, which is what "
            "execution.verification_evidence.ledger_evidence enforces"
        ),
    },
}


def distinction_lines() -> List[str]:
    """Render :data:`E2E_VERIFY_DISTINCTION` as plain lines a surface can print."""
    lines = ["end-to-end verification vs unit verification — they are different questions"]
    for name in ("verify", "e2e"):
        row = E2E_VERIFY_DISTINCTION[name]
        lines.append(f"  {name}:")
        for key in ("asks", "measures", "sees", "cannot_see", "cost", "stability"):
            lines.append(f"    {key:<12}: {row[key]}")
    lines.append("  neither substitutes for the other:")
    for key in ("e2e_only", "verify_only", "both_needed"):
        lines.append(f"    {key:<12}: {E2E_VERIFY_DISTINCTION['neither_substitutes'][key]}")
    return lines


# --------------------------------------------------------------------------
# The run policy
# --------------------------------------------------------------------------

POLICY_ON_DEMAND = "on_demand"
POLICY_ON_FINAL_GATE = "on_final_gate"
POLICY_PER_ITERATION = "per_iteration"

#: Closed. An unrecognised policy resolves to the cheapest one, and the reason
#: says so — a typo must never silently start running servers.
E2E_RUN_POLICIES: Tuple[str, str, str] = (
    POLICY_ON_DEMAND,
    POLICY_ON_FINAL_GATE,
    POLICY_PER_ITERATION,
)

#: **The policy this repository ships.** e2e is expensive and starts servers, so
#: it does not run on every iteration. A caller that wants it asks.
DEFAULT_E2E_RUN_POLICY = POLICY_ON_DEMAND

DEFAULT_E2E_TIMEOUT_S = 300
DEFAULT_E2E_RERUNS = 1

#: Same threshold ``execution/flake_gate.py`` uses. One observation cannot
#: distinguish "stable" from "never checked".
MIN_REPETITIONS_FOR_DETECTION = 2


@dataclass(frozen=True)
class E2EPolicyDecision:
    """Whether e2e runs, and the sentence saying why either way."""

    run: bool
    policy: str
    reason: str
    requested: bool = False
    is_final_gate: bool = False
    declared_checks: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run": bool(self.run),
            "policy": self.policy,
            "reason": self.reason,
            "requested": bool(self.requested),
            "is_final_gate": bool(self.is_final_gate),
            "declared_checks": int(self.declared_checks),
            "note": (
                "an e2e check may start a server. That is slow and expensive, "
                "so the shipped default is on_demand: e2e does NOT run on "
                "every iteration. This decision is reported on every call "
                "because an unstated policy means users discover the cost by "
                "being surprised."
            ),
        }


def should_run_e2e(
    *,
    policy: str = DEFAULT_E2E_RUN_POLICY,
    requested: bool = False,
    is_final_gate: bool = False,
    declared_checks: int = 0,
) -> E2EPolicyDecision:
    """Decide whether this call runs e2e, and say why.

    Every return carries a reason, including the "no" answers. The ordering
    matters and is the policy:

    1. **no declaration** → never run, and the reason is that nothing was
       declared (this is the ``not_run`` case, and it is not a pass);
    2. **explicitly requested** → run, whatever the policy: a human asking is
       an authorisation, not a suggestion;
    3. ``per_iteration`` → run;
    4. ``on_final_gate`` and this is not the gate → do not run, with the reason
       naming where it WILL run;
    5. ``on_demand`` (default) → do not run, and the receipt says how to ask.

    An unrecognised policy resolves to ``on_demand`` and says it was
    unrecognised, so a typo cannot switch servers on.
    """
    count = int(declared_checks or 0)
    named = str(policy or "")
    resolved = named if named in E2E_RUN_POLICIES else POLICY_ON_DEMAND

    if count <= 0:
        return E2EPolicyDecision(
            run=False,
            policy=resolved,
            reason=(
                "the repository declares no end-to-end check, so there is "
                "nothing to run. This is reported as not_run, which is NOT a "
                "pass: nobody established that the feature works end to end."
            ),
            requested=bool(requested),
            is_final_gate=bool(is_final_gate),
            declared_checks=0,
        )
    if requested:
        return E2EPolicyDecision(
            run=True,
            policy=resolved,
            reason=(
                "an explicit request is an authorisation: e2e runs whatever the "
                "run policy says, because a human asking is not a suggestion"
            ),
            requested=True,
            is_final_gate=bool(is_final_gate),
            declared_checks=count,
        )
    if resolved == POLICY_PER_ITERATION:
        return E2EPolicyDecision(
            run=True,
            policy=resolved,
            reason=(
                "the policy is per_iteration, so the check runs on every call. "
                "This is the debugging setting: measured cost is in "
                "execution/verify_cache.py::COST_TABLE"
            ),
            requested=False,
            is_final_gate=bool(is_final_gate),
            declared_checks=count,
        )
    if resolved == POLICY_ON_FINAL_GATE and not is_final_gate:
        return E2EPolicyDecision(
            run=False,
            policy=resolved,
            reason=(
                "the policy is on_final_gate and this is not the gating call, "
                "so the check is deferred rather than paid for on every "
                "iteration. It runs at the final verification of an attempt."
            ),
            requested=False,
            is_final_gate=bool(is_final_gate),
            declared_checks=count,
        )
    if resolved == POLICY_ON_DEMAND:
        return E2EPolicyDecision(
            run=False,
            policy=resolved,
            reason=(
                "the shipped policy is on_demand: an end-to-end check may start "
                "a server, which is slow and expensive, so it does not run on "
                "every iteration. Pass requested=True (or an explicit "
                "run_e2e call) to run it now."
            ),
            requested=False,
            is_final_gate=bool(is_final_gate),
            declared_checks=count,
        )
    return E2EPolicyDecision(
        run=True,
        policy=resolved,
        reason="the policy is on_final_gate and this IS the gating call",
        requested=False,
        is_final_gate=bool(is_final_gate),
        declared_checks=count,
    )


# --------------------------------------------------------------------------
# The declaration
# --------------------------------------------------------------------------


class E2ESpecError(ValueError):
    """A declared e2e spec could not be read. Reported, never silently empty."""


@dataclass(frozen=True)
class E2ECheck:
    """One declared end-to-end check: "how would a human check this feature?"

    This is the machinery to RUN the answer a repository already supplies. The
    *declaration* is the repository's; this module's job is to execute one and
    report the four states honestly.
    """

    id: str
    command: str
    feature: str = ""
    description: str = ""
    timeout_s: int = DEFAULT_E2E_TIMEOUT_S
    allow_network: bool = False
    writable_roots: Tuple[str, ...] = ()
    env: Tuple[Tuple[str, str], ...] = ()
    expects_exit: Optional[int] = 0
    reruns: int = DEFAULT_E2E_RERUNS
    publish_port: Optional[int] = None
    server_start: str = ""
    server_ready: str = ""
    server_ready_attempts: int = 40
    server_ready_interval_s: float = 0.25

    def __post_init__(self) -> None:
        if not str(self.id or "").strip():
            raise E2ESpecError("an end-to-end check needs a non-empty id")
        if not str(self.command or "").strip():
            raise E2ESpecError(
                f"end-to-end check {self.id!r} declares no command, so there is "
                f"nothing to run. A check with no command is not a check that "
                f"passes; it is a check that cannot be run."
            )
        if int(self.timeout_s) <= 0:
            raise E2ESpecError(
                f"end-to-end check {self.id!r} has timeout_s={self.timeout_s!r}; "
                f"a timeout budget of zero would report every run as blocked"
            )
        if int(self.reruns) < 1:
            raise E2ESpecError(
                f"end-to-end check {self.id!r} declares reruns={self.reruns!r}; "
                f"at least one observation is required or the result is not_run "
                f"with no evidence behind it"
            )

    @property
    def has_server(self) -> bool:
        """Whether this check starts something that needs a readiness wait."""
        return bool(self.server_start or self.publish_port is not None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "command": self.command,
            "feature": self.feature,
            "description": self.description,
            "timeout_s": int(self.timeout_s),
            "allow_network": bool(self.allow_network),
            "writable_roots": list(self.writable_roots),
            "expects_exit": self.expects_exit,
            "reruns": int(self.reruns),
            "publish_port": self.publish_port,
            "has_server": self.has_server,
            "server_start": self.server_start,
            "server_ready": self.server_ready,
        }

    @classmethod
    def from_mapping(cls, raw: Any) -> "E2ECheck":
        """Build a check from one declared row, refusing an unusable one."""
        if not isinstance(raw, Mapping):
            raise E2ESpecError(
                f"an end-to-end check must be an object, got {type(raw).__name__}"
            )
        env_raw = raw.get("env") or {}
        env: Tuple[Tuple[str, str], ...] = ()
        if isinstance(env_raw, Mapping):
            env = tuple((str(k), str(v)) for k, v in env_raw.items())
        elif isinstance(env_raw, (list, tuple)):
            env = tuple((str(k), str(v)) for k, v in env_raw)
        expects = raw.get("expects_exit", 0)
        if expects is not None:
            try:
                expects = int(expects)
            except (TypeError, ValueError):
                raise E2ESpecError(
                    f"end-to-end check {raw.get('id')!r} declares a "
                    f"non-integer expects_exit {expects!r}"
                ) from None
        return cls(
            id=str(raw.get("id") or ""),
            command=str(raw.get("command") or ""),
            feature=str(raw.get("feature") or ""),
            description=str(raw.get("description") or ""),
            timeout_s=int(raw.get("timeout_s") or DEFAULT_E2E_TIMEOUT_S),
            allow_network=bool(raw.get("allow_network", False)),
            writable_roots=tuple(str(item) for item in (raw.get("writable_roots") or ())),
            env=env,
            expects_exit=expects,
            reruns=int(raw.get("reruns") or DEFAULT_E2E_RERUNS),
            publish_port=(
                int(raw["publish_port"]) if raw.get("publish_port") is not None else None
            ),
            server_start=str(raw.get("server_start") or ""),
            server_ready=str(raw.get("server_ready") or ""),
            server_ready_attempts=int(raw.get("server_ready_attempts") or 40),
            server_ready_interval_s=float(raw.get("server_ready_interval_s") or 0.25),
        )


@dataclass(frozen=True)
class E2ESpec:
    """A repository's declared end-to-end checks."""

    checks: Tuple[E2ECheck, ...]
    source: str = ""
    source_digest: str = ""
    schema_version: int = E2E_SCHEMA_VERSION

    def __post_init__(self) -> None:
        seen = set()
        for check in self.checks:
            if check.id in seen:
                raise E2ESpecError(
                    f"duplicate end-to-end check id {check.id!r}; two rows with "
                    f"the same id cannot be told apart in a report"
                )
            seen.add(check.id)

    def __len__(self) -> int:
        return len(self.checks)

    def __iter__(self):
        return iter(self.checks)

    @property
    def features(self) -> Tuple[str, ...]:
        """The distinct features these checks speak for, in declaration order."""
        seen: List[str] = []
        for check in self.checks:
            key = check.feature or check.id
            if key not in seen:
                seen.append(key)
        return tuple(seen)

    def get(self, check_id: str) -> Optional[E2ECheck]:
        for check in self.checks:
            if check.id == check_id:
                return check
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "source_digest": self.source_digest,
            "check_count": len(self.checks),
            "checks": [check.to_dict() for check in self.checks],
            "features": list(self.features),
        }


@dataclass(frozen=True)
class SpecLoad:
    """The result of looking for a declaration.

    ``spec is None`` with a status other than ``absent`` is a **broken
    declaration**, and it must not read as "this repository has no end-to-end
    checks" — that is how a typo in a JSON file becomes a permanently green
    ledger. ``absent`` and ``malformed`` are separate statuses for exactly this
    reason.
    """

    spec: Optional[E2ESpec]
    status: str
    reason: str = ""
    source: str = ""

    @property
    def usable(self) -> bool:
        return self.spec is not None

    @property
    def declared(self) -> bool:
        """Whether a declaration FILE was found, whatever its contents."""
        return self.status != SPEC_ABSENT

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "declared": self.declared,
            "usable": self.usable,
            "reason": self.reason,
            "source": self.source,
            "check_count": len(self.spec) if self.spec is not None else 0,
        }


SPEC_FOUND = "found"
SPEC_ABSENT = "absent"
SPEC_MALFORMED = "malformed"
SPEC_UNREADABLE = "unreadable"

SPEC_LOAD_STATUSES: Tuple[str, str, str, str] = (
    SPEC_FOUND,
    SPEC_ABSENT,
    SPEC_MALFORMED,
    SPEC_UNREADABLE,
)


def _digest_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def declaration_digest(repo_path: str) -> Optional[str]:
    """SHA-256 of the declaration FILE's bytes, or ``None`` if there is none.

    Deliberately hashes the raw file rather than the parsed
    :class:`E2ESpec`: a MALFORMED declaration must still move a content
    address, or two repositories whose declaration is broken in different ways
    would share one.

    Total — never raises — because it is called from a cache key, and a cache
    key must not be the thing that raises. **The bare ``except`` here is
    recorded as a hazard, because it caught a real bug during this round:** the
    first version called ``hashlib`` while ``hashlib`` was imported only
    INSIDE :func:`_digest_text`, so this raised ``NameError``, the ``except``
    swallowed it, and the function returned ``None`` — which the cache key read
    as "this repository declares no end-to-end checks". A swallowed exception in
    a function whose ``None`` means "nothing is there" reports the absence of a
    thing that was in fact misconfigured. ``hashlib`` is now imported at module
    scope and there is a pin that a real declaration yields a digest.
    """
    try:
        root = Path(str(repo_path))
        for name in E2E_SPEC_FILENAMES:
            candidate = root / name
            if candidate.is_file():
                return hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError:
        return None
    return None


def load_e2e_spec(repo_path: str) -> SpecLoad:
    """Find and parse a repository's end-to-end declaration.

    Total: every failure is a status, never an exception. A malformed or
    unreadable declaration is reported as such so a caller can render
    ``blocked`` + a reason instead of an empty list of checks.
    """
    root = Path(str(repo_path or "")).expanduser()
    candidates: List[Path] = [root / name for name in E2E_SPEC_FILENAMES]
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        return SpecLoad(
            spec=None,
            status=SPEC_ABSENT,
            reason=(
                f"no end-to-end declaration found; looked for "
                f"{', '.join(E2E_SPEC_FILENAMES)} under {root}"
            ),
            source="",
        )
    for path in existing:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return SpecLoad(
                spec=None,
                status=SPEC_UNREADABLE,
                reason=(
                    f"an end-to-end declaration exists at {path} but could not "
                    f"be read: {type(exc).__name__}: {exc}. This is reported as "
                    f"blocked, not as 'no checks declared'."
                ),
                source=str(path),
            )
        try:
            raw = json.loads(text)
        except ValueError as exc:
            return SpecLoad(
                spec=None,
                status=SPEC_MALFORMED,
                reason=(
                    f"the end-to-end declaration at {path} is not valid JSON: "
                    f"{exc}. This is reported as blocked, not as 'no checks "
                    f"declared' — a typo in a declaration file must not read "
                    f"as a repository that has nothing to check."
                ),
                source=str(path),
            )
        if not isinstance(raw, Mapping):
            return SpecLoad(
                spec=None,
                status=SPEC_MALFORMED,
                reason=(
                    f"the end-to-end declaration at {path} is a "
                    f"{type(raw).__name__}, not an object with a 'checks' list"
                ),
                source=str(path),
            )
        rows = raw.get("checks")
        if rows is None:
            rows = []
        if not isinstance(rows, (list, tuple)):
            return SpecLoad(
                spec=None,
                status=SPEC_MALFORMED,
                reason=(
                    f"'checks' in {path} is a {type(rows).__name__}, not a list. "
                    f"An absent 'checks' key IS a repository with no "
                    f"declarations; a wrong-typed one is a broken file."
                ),
                source=str(path),
            )
        declared_version = raw.get("schema_version", E2E_SCHEMA_VERSION)
        try:
            version = int(declared_version)
        except (TypeError, ValueError):
            return SpecLoad(
                spec=None,
                status=SPEC_MALFORMED,
                reason=(
                    f"'schema_version' in {path} is {declared_version!r}, not an "
                    f"integer. An unknown schema is refused rather than guessed."
                ),
                source=str(path),
            )
        if version != E2E_SCHEMA_VERSION:
            return SpecLoad(
                spec=None,
                status=SPEC_MALFORMED,
                reason=(
                    f"{path} declares schema_version {version}; this module "
                    f"reads version {E2E_SCHEMA_VERSION}. An unknown version is "
                    f"refused rather than parsed on a guess."
                ),
                source=str(path),
            )
        checks: List[E2ECheck] = []
        for row in rows:
            try:
                checks.append(E2ECheck.from_mapping(row))
            except E2ESpecError as exc:
                return SpecLoad(
                    spec=None,
                    status=SPEC_MALFORMED,
                    reason=f"a declared check in {path} is unusable: {exc}",
                    source=str(path),
                )
        return SpecLoad(
            spec=E2ESpec(
                checks=tuple(checks),
                source=str(path),
                source_digest=_digest_text(text),
                schema_version=version,
            ),
            status=SPEC_FOUND,
            reason=f"{len(checks)} end-to-end check(s) declared in {path}",
            source=str(path),
        )
    return SpecLoad(  # pragma: no cover - unreachable; the loop returns
        spec=None,
        status=SPEC_ABSENT,
        reason="no end-to-end declaration found",
        source="",
    )


def check_for_spec(repo_path: str) -> bool:
    """Whether a usable declaration exists. Convenience, not a gate.

    A caller that needs to know WHY uses :func:`load_e2e_spec`; this boolean
    collapses ``malformed`` and ``absent``, which is why it is not the
    authority and is not what :func:`run_e2e` reads.
    """
    return load_e2e_spec(repo_path).usable


# --------------------------------------------------------------------------
# The result
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class E2EResult:
    """One check's outcome, with timeout/flakiness/environment kept apart.

    ``state`` is the four-value verdict. ``timed_out``, ``flaky_check`` and
    ``environment`` are SEPARATE axes: a reader can tell a check that could not
    finish, a check whose machine was broken, and a check that found the
    feature broken, without reading prose.
    """

    check_id: str
    feature: str
    state: str
    reason: str
    timed_out: bool = False
    flaky_check: str = "not_run"
    environment: str = ENV_OK
    duration_s: float = 0.0
    exit_code: Optional[int] = None
    observations: Tuple[str, ...] = ()
    repetitions: int = 0
    raw_output: str = ""
    port: Optional[int] = None
    port_allocation: Dict[str, Any] = field(default_factory=dict)
    container_name: str = ""
    declared: bool = True
    ran: bool = False
    #: Wave 2. The trust receipt. ``{}`` for every state except
    #: ``untrustworthy``, which REQUIRES it with a reason — the same rule
    #: ``blocked`` and ``not_run`` already follow.
    trust: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state not in E2E_STATES:
            raise ValueError(
                f"unknown end-to-end state {self.state!r}; the vocabulary is "
                f"closed: {E2E_STATES}"
            )
        if self.environment not in ENVIRONMENT_KINDS:
            raise ValueError(
                f"unknown environment {self.environment!r}; the vocabulary is "
                f"closed: {ENVIRONMENT_KINDS}"
            )
        if self.state in (E2E_FAILED, E2E_PASSED) and not self.ran:
            raise ValueError(
                f"end-to-end check {self.check_id!r} reports {self.state!r} "
                f"without having run. A decided verdict with no run is the "
                f"exact shape this module exists to refuse."
            )
        if self.state == E2E_UNTRUSTWORTHY and not str(self.trust.get("reason") or ""):
            raise ValueError(
                f"end-to-end check {self.check_id!r} reports "
                f"{E2E_UNTRUSTWORTHY!r} with no trust reason. A state that means "
                f"'this measurement is void' must say WHY, or it is an "
                f"unexplained refusal and a reader cannot tell it from a bug."
            )
        if self.state != E2E_UNTRUSTWORTHY and self.trust:
            # A trust receipt on a state that made a claim is the shape that
            # trains readers to skip the field: a reader learns to ignore it
            # the first time it appears on a green row.
            raise ValueError(
                f"end-to-end check {self.check_id!r} carries a trust receipt "
                f"while reporting {self.state!r}. The trust layer only speaks "
                f"for the {E2E_UNTRUSTWORTHY!r} state; on any other state a "
                f"trust field is a decoration that reads as a verdict."
            )

    @property
    def passed(self) -> bool:
        """Whether the feature behaved. ``False`` for everything else.

        This is the honest single-reader answer and it deliberately collapses
        ``blocked``, ``not_run`` and ``untrustworthy`` into ``False`` —
        because ``e2e_settled``/``state`` is the field a consumer must branch
        on, and a consumer that reads only this one learns "did it work", not
        "was it checked". ``neither_substitutes`` and
        ``execution.verification_evidence.ledger_evidence`` are the readers that
        require the distinction.
        """
        return self.state == E2E_PASSED

    @property
    def settled(self) -> bool:
        """Whether the check produced a verdict, as opposed to a block.

        ``untrustworthy`` is NOT settled even when it ran: the run happened and
        the measurement is still void, so there is no verdict about the feature
        either way.
        """
        return self.state in (E2E_PASSED, E2E_FAILED)

    @property
    def blocked(self) -> bool:
        return self.state == E2E_BLOCKED

    @property
    def untrustworthy(self) -> bool:
        """The check RAN (or could have) and its verdict is void."""
        return self.state == E2E_UNTRUSTWORTHY

    @property
    def flaky(self) -> bool:
        """Flakiness DETECTED. ``not_run`` is ``False`` — read ``flaky_check``."""
        return self.flaky_check == "flaky_detected"

    @property
    def flake_rate(self) -> Optional[float]:
        """How OFTEN this check produced the OTHER answer, as a fraction.

        The brief's row 9 asks for a flaky check to be "reported flaky, **with
        the rate**", because a bare boolean cannot distinguish a check that was
        wrong once in three from one that was wrong twice out of two, and the
        two deserve different operator responses.

        Defined as *the fraction of observations that disagreed with the reported
        state* — so ``0.0`` means every observation agreed, ``0.5`` means half of
        them did not. ``None`` when there are **no** observations, which is the
        honest reading: an unmeasured rate is not ``0.0``, and ``0.0`` beside a
        ``passed`` row would claim a consistency nobody checked.

        A **property, not a field**, so it cannot drift from ``observations``:
        a field would be set at construction and then disagree with the
        observations the moment a row was built a second way.
        """
        if not self.observations:
            return None
        disagreeing = sum(1 for item in self.observations if item != self.state)
        return round(float(disagreeing) / float(len(self.observations)), 4)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check_id": self.check_id,
            "feature": self.feature,
            "state": self.state,
            "reason": self.reason,
            "ran": bool(self.ran),
            "declared": bool(self.declared),
            "settled": self.settled,
            "timed_out": bool(self.timed_out),
            "flaky_check": self.flaky_check,
            "flaky": bool(self.flaky),
            "flake_rate": self.flake_rate,
            "untrustworthy": bool(self.untrustworthy),
            "environment": self.environment,
            "duration_s": round(float(self.duration_s), 4),
            "exit_code": self.exit_code,
            "observations": list(self.observations),
            "repetitions": int(self.repetitions),
            "port": self.port,
            "port_allocation": dict(self.port_allocation),
            "container_name": self.container_name,
            "raw_output_chars": len(self.raw_output or ""),
            "trust": dict(self.trust),
        }

    def render(self) -> List[str]:
        lines = [
            f"{self.check_id}: {self.state.upper()}"
            + (f" (feature: {self.feature})" if self.feature else "")
        ]
        lines.append(f"  reason      : {self.reason}")
        lines.append(f"  ran         : {self.ran}   timed_out: {self.timed_out}")
        lines.append(f"  flaky_check : {self.flaky_check}")
        rate = self.flake_rate
        lines.append(
            "  flake_rate  : "
            + ("NOT MEASURED (no observations)" if rate is None else f"{rate:.0%} disagreed")
        )
        lines.append(f"  environment : {self.environment}")
        lines.append(
            f"  observations: {self.repetitions} -> {list(self.observations) or 'none'}"
        )
        if self.duration_s:
            lines.append(f"  duration    : {self.duration_s:.2f}s")
        if self.port is not None:
            lines.append(f"  port        : {self.port}")
        if self.untrustworthy:
            lines.append("  trust       : VERDICT VOID — this row cannot decide the question")
            for key in sorted(self.trust):
                value = self.trust[key]
                if isinstance(value, (list, tuple)):
                    value = ", ".join(str(item) for item in value) or "(none)"
                lines.append(f"    {key:<22}: {value}")
        return lines


@dataclass(frozen=True)
class E2EReport:
    """Every check's outcome plus the cost and the decision that got us here."""

    results: Tuple[E2EResult, ...]
    policy: E2EPolicyDecision
    spec_status: str = SPEC_ABSENT
    spec_reason: str = ""
    total_duration_s: float = 0.0
    session_id: str = ""

    @property
    def passed(self) -> Tuple[E2EResult, ...]:
        return tuple(row for row in self.results if row.state == E2E_PASSED)

    @property
    def failed(self) -> Tuple[E2EResult, ...]:
        return tuple(row for row in self.results if row.state == E2E_FAILED)

    @property
    def blocked(self) -> Tuple[E2EResult, ...]:
        return tuple(row for row in self.results if row.state == E2E_BLOCKED)

    @property
    def untrustworthy(self) -> Tuple[E2EResult, ...]:
        """Rows whose verdict is void in both directions. Wave 2's fifth state."""
        return tuple(row for row in self.results if row.state == E2E_UNTRUSTWORTHY)

    @property
    def not_run(self) -> Tuple[E2EResult, ...]:
        return tuple(row for row in self.results if row.state == E2E_NOT_RUN)

    @property
    def flaky(self) -> Tuple[E2EResult, ...]:
        return tuple(row for row in self.results if row.flaky)

    @property
    def flaky_check(self) -> str:
        """The report's AGGREGATE flakiness verdict.

        A string rather than a boolean, for the same reason the row's is: with
        no repetition there is no flakiness measurement, and ``False`` cannot
        say whether "stable" was shown or nothing ran. The three states are the
        ones ``execution/flake_gate.py`` already established, so there is one
        vocabulary in the tree rather than two.
        """
        checks = [row.flaky_check for row in self.results]
        if not checks:
            return "not_run"
        if any(item == "flaky_detected" for item in checks):
            return "flaky_detected"
        if all(item == "not_run" for item in checks):
            return "not_run"
        return "not_flaky"

    @property
    def timed_out(self) -> Tuple[E2EResult, ...]:
        return tuple(row for row in self.results if row.timed_out)

    @property
    def declared(self) -> bool:
        """Whether any check was declared at all — a fact separate from running."""
        return any(row.declared for row in self.results) or self.spec_status == SPEC_FOUND

    @property
    def settled(self) -> bool:
        """Whether every DECLARED check produced a verdict.

        A report with a blocked check is not settled, and that is the whole
        distinction Phase 3's ledger needs: "could not check" is not "broken".
        An ``untrustworthy`` row is not settled either, and for a sharper reason:
        it ran and produced a number, so it LOOKS settled to any reader that
        only checks "did a row appear", which is exactly why it gets its own
        state rather than a note on a green row.
        """
        return all(row.settled for row in self.results if row.declared)

    @property
    def verdict(self) -> str:
        """One word, published so a caller cannot invent its own vocabulary.

        ``untrustworthy`` is checked FIRST and dominates everything, because it
        says the measurement is void. Ranking it below ``passed`` would let a
        report contain one green row and one void row and read ``passed`` — and
        ranking it below ``failed`` would send a repair loop after a working
        tree. Both are the same mistake in opposite directions: letting a void
        measurement inherit a decision that was never made.
        """
        if not self.results:
            return "nothing_declared"
        if self.untrustworthy:
            return "untrustworthy"
        if self.blocked:
            return "blocked"
        if self.failed:
            return "failed"
        if self.not_run:
            return "not_run"
        if self.flaky:
            return "flaky"
        return "passed"

    def result_for(self, check_id: str) -> Optional[E2EResult]:
        for row in self.results:
            if row.check_id == check_id:
                return row
        return None

    def results_for_feature(self, feature: str) -> Tuple[E2EResult, ...]:
        return tuple(row for row in self.results if row.feature == feature)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "settled": self.settled,
            "declared": self.declared,
            "policy": self.policy.to_dict(),
            "spec_status": self.spec_status,
            "spec_reason": self.spec_reason,
            "session_id": self.session_id,
            "counts": {
                "passed": len(self.passed),
                "failed": len(self.failed),
                "blocked": len(self.blocked),
                "not_run": len(self.not_run),
                "untrustworthy": len(self.untrustworthy),
                "flaky": len(self.flaky),
                "timed_out": len(self.timed_out),
                "total": len(self.results),
            },
            # The aggregate verdict, so a reader counting `flaky` rows below
            # cannot mistake "no flaky row" for "flakiness was checked and not
            # found". `execution/test_flake_gate_pins.py` requires a
            # `.flaky` read to be paired with `flake_check` in the same
            # function; two same-named `to_dict` methods in this module also
            # collide in that pin's name-keyed body map, so the pairing has to
            # be real here rather than satisfied in the other class.
            "flaky_check": self.flaky_check,
            "total_duration_s": round(float(self.total_duration_s), 4),
            "results": [row.to_dict() for row in self.results],
            "distinction": E2E_VERIFY_DISTINCTION,
            "note": (
                "blocked is NOT failed and NOT skipped. 'the feature is broken' "
                "and 'I could not check' are different facts and a ledger that "
                "renders them the same cannot be repaired afterwards. "
                "untrustworthy is a THIRD such fact and a fourth: the check ran, "
                "produced a number, and that number cannot decide anything."
            ),
        }


def render_report(report: E2EReport) -> List[str]:
    """Render an :class:`E2EReport` as plain lines a surface can print."""
    lines = [
        "end-to-end verification",
        f"  verdict       : {report.verdict}  (settled: {report.settled})",
        f"  run policy    : {report.policy.policy} -> run={report.policy.run}",
        f"  policy reason : {report.policy.reason}",
        f"  declaration   : {report.spec_status} — {report.spec_reason}",
    ]
    if report.session_id:
        lines.append(f"  session       : {report.session_id}")
    if report.total_duration_s:
        lines.append(f"  total cost    : {report.total_duration_s:.2f}s")
    for row in report.results:
        lines.append("")
        lines.extend(row.render())
    if not report.results:
        lines.append("")
        lines.append("  (no end-to-end checks ran)")
    return lines


def render_block(report: E2EReport) -> str:
    """Render an :class:`E2EReport` as the block appended to ``raw_output``.

    Matches ``execution/verify.py``'s ``## verification-rung`` convention: a
    recognisable header, and it goes LAST so no later receipt can shadow it.
    """
    return "## end-to-end verification\n" + "\n".join(render_report(report)) + "\n"


# --------------------------------------------------------------------------
# Running one check
# --------------------------------------------------------------------------


def _blocked_result(
    check: E2ECheck,
    reason: str,
    *,
    environment: str = ENV_CONTAINER_BROKEN,
    timed_out: bool = False,
    duration_s: float = 0.0,
    exit_code: Optional[int] = None,
    raw_output: str = "",
    port: Optional[int] = None,
    port_allocation: Optional[Dict[str, Any]] = None,
    container_name: str = "",
) -> E2EResult:
    return E2EResult(
        check_id=check.id,
        feature=check.feature,
        state=E2E_BLOCKED,
        reason=reason,
        timed_out=bool(timed_out),
        flaky_check="not_run",
        environment=environment,
        duration_s=float(duration_s),
        exit_code=exit_code,
        observations=(),
        repetitions=0,
        raw_output=raw_output,
        port=port,
        port_allocation=dict(port_allocation or {}),
        container_name=container_name,
        declared=True,
        ran=False,
    )


def _not_run_result(check: E2ECheck, reason: str) -> E2EResult:
    return E2EResult(
        check_id=check.id,
        feature=check.feature,
        state=E2E_NOT_RUN,
        reason=reason,
        timed_out=False,
        flaky_check="not_run",
        environment=ENV_OK,
        duration_s=0.0,
        exit_code=None,
        observations=(),
        repetitions=0,
        raw_output="",
        port=None,
        port_allocation={},
        container_name="",
        declared=True,
        ran=False,
    )


def untrustworthy_result(
    check: E2ECheck,
    trust: Mapping[str, Any],
    *,
    reason: str = "",
    ran: bool = False,
    observations: Sequence[str] = (),
    duration_s: float = 0.0,
    exit_code: Optional[int] = None,
    raw_output: str = "",
    container_name: str = "",
    flaky_check: str = "not_run",
    environment: str = ENV_OK,
) -> E2EResult:
    """Build the fifth-state row: the check cannot decide its own question.

    This is the constructor the trust layer in ``execution/e2e_trust.py`` calls,
    and it is public because a caller must be able to produce the row without
    reaching into this module's privates — and because a second construction
    site is a second place the "a trust receipt requires a reason" rule could be
    forgotten.

    ``ran`` is an input rather than being derived, and that is deliberate. Two
    shapes are both ``untrustworthy`` and they must not be collapsed:

    * the check ran, exited 0, and asserts nothing (the *weak check*); the
      dangerous case, because it looks green;
    * the check was REFUSED before running because the agent had modified the
      declaration it verifies (rows 2 and 3 of the attack table); here nothing was
      measured, which is why ``observations`` defaults to empty.

    The receipt travels on the row verbatim. A reader that only sees
    ``state="untrustworthy"`` learns that something is wrong but not what, and a
    refusal that cannot be acted on is one a reader learns to ignore.
    """
    payload = dict(trust or {})
    stated = str(reason or payload.get("reason") or "").strip()
    if not stated:
        raise ValueError(
            f"an {E2E_UNTRUSTWORTHY!r} row for end-to-end check {check.id!r} "
            f"needs a reason, on the argument or in the trust receipt"
        )
    payload["reason"] = stated
    return E2EResult(
        check_id=check.id,
        feature=check.feature,
        state=E2E_UNTRUSTWORTHY,
        reason=f"{stated} — {E2E_UNTRUSTWORTHY_REASON}",
        timed_out=False,
        flaky_check=str(flaky_check or "not_run"),
        environment=str(environment or ENV_OK),
        duration_s=float(duration_s),
        exit_code=exit_code,
        observations=tuple(observations),
        repetitions=len(observations),
        raw_output=str(raw_output or ""),
        port=None,
        port_allocation={},
        container_name=str(container_name or ""),
        declared=True,
        ran=bool(ran),
        trust=payload,
    )


def _classify_environment(exc: BaseException) -> str:
    """Map a raised boundary onto the environment vocabulary.

    Matched by exception NAME so this module keeps its zero-dependency
    property on ``execution.sandbox`` at classify time, and so a mid-edit of a
    sibling module produces a labelled block rather than an import error.
    """
    name = type(exc).__name__
    text = f"{name}: {exc}".lower()
    if "SandboxUnavailable" in name or "daemon" in text:
        return ENV_DAEMON_UNAVAILABLE
    if "SandboxDependency" in name or "no such file or directory" in text:
        return ENV_NO_TOOLCHAIN
    if "offline" in text or "unreachable" in text or "network" in text:
        return ENV_OFFLINE
    return ENV_CONTAINER_BROKEN


def _verdict_for_exit(exit_code: int, expects_exit: Optional[int]) -> str:
    """``passed`` only on the declared exit code; anything else is ``failed``."""
    if expects_exit is None:
        return E2E_PASSED if exit_code == 0 else E2E_FAILED
    return E2E_PASSED if exit_code == int(expects_exit) else E2E_FAILED


def _flaky_check(observations: Sequence[str]) -> str:
    """``execution/flake_gate.py``'s rule, applied to e2e observations.

    Fewer than two observations is ``not_run``, never ``not_flaky``: one
    observation cannot distinguish "stable" from "never checked", and an e2e
    check declared with ``reruns: 1`` is the common case.
    """
    distinct = set(observations)
    if len(observations) < MIN_REPETITIONS_FOR_DETECTION:
        return "not_run"
    if len(distinct) > 1:
        return "flaky_detected"
    return "not_flaky"


def run_e2e_check(
    check: E2ECheck,
    repo_path: str,
    *,
    sandbox: Optional[Callable[..., Any]] = None,
    session_id: str = "",
    env: Optional[Dict[str, str]] = None,
    allow_network: Optional[bool] = None,
) -> E2EResult:
    """Run one declared end-to-end check in a fresh sandbox container.

    Same containment as ``verify()``: a fresh container per command, network
    off unless the check DECLARES it, writes confined to declared writable
    roots, resource limits, and the same ``--network none`` / read-only-rootfs /
    dropped-capability argv. The one addition is the DECLARED port publication,
    which :func:`execution.sandbox.assert_sandbox_argv_isolated` refuses unless
    the caller declared it and which must be loopback.

    Failure handling is the point of this function:

    * a raised boundary (``SandboxUnavailableError``, ``SandboxDependencyError``)
      becomes ``blocked`` with the environment classified — **never** ``failed``;
    * a timeout becomes ``blocked`` with ``timed_out=True`` — an e2e check that
      times out is neither a failing feature nor a passing one;
    * ``reruns`` observations decide flakiness, and one observation reports
      ``not_run`` for flakiness rather than ``not_flaky``.

    The `sandbox` parameter is the injection seam the pins use to drive this
    without a daemon; production leaves it ``None`` and gets the real boundary.
    """
    from execution import sandbox as sbx

    runner = sandbox or sbx.execute_sandboxed
    started = time.perf_counter()
    port: Optional[int] = None
    allocation: Dict[str, Any] = {}

    # -- port allocation, BEFORE the container exists ----------------------
    if check.publish_port is not None:
        try:
            from execution import port_alloc

            sid = str(session_id or check.id)
            outcome = port_alloc.allocate_port(sid, reason=f"e2e check {check.id}")
            allocation = outcome
            port = outcome.get("port")
            if port is None:
                return _blocked_result(
                    check,
                    f"no host port could be allocated for session {sid!r}: "
                    f"{outcome.get('reason')}",
                    environment=ENV_PORT_UNAVAILABLE,
                    duration_s=time.perf_counter() - started,
                    port_allocation=allocation,
                )
        except Exception as exc:
            return _blocked_result(
                check,
                f"port allocation for end-to-end check {check.id!r} failed: "
                f"{type(exc).__name__}: {exc}",
                environment=ENV_PORT_UNAVAILABLE,
                duration_s=time.perf_counter() - started,
            )

    publish: Tuple[Tuple[str, int], ...] = ()
    if port is not None:
        publish = ((port_alloc_host(), int(port)),)

    merged_env: Dict[str, str] = {}
    for key, value in check.env:
        merged_env[str(key)] = str(value)
    for key, value in (env or {}).items():
        merged_env[str(key)] = str(value)
    if port is not None:
        merged_env.setdefault("NEO_E2E_PORT", str(port))

    command = check.command
    if port is not None and "{port}" in command:
        command = command.replace("{port}", str(port))
    if port is not None and "{host}" in command:
        command = command.replace("{host}", port_alloc_host())

    # -- the observations --------------------------------------------------
    observations: List[str] = []
    raw_chunks: List[str] = []
    last_code: Optional[int] = None
    container_name = ""
    timed_out = False
    for index in range(max(1, int(check.reruns))):
        try:
            kwargs: Dict[str, Any] = {
                "allow_network": (
                    bool(check.allow_network)
                    if allow_network is None
                    else bool(allow_network)
                ),
                "env": merged_env or None,
                "purpose": "verification",
            }
            if check.writable_roots:
                kwargs["writable_roots"] = tuple(check.writable_roots)
            if publish:
                kwargs["publish_ports"] = publish
            result = runner(
                str(repo_path),
                command,
                int(check.timeout_s),
                **kwargs,
            )
        except Exception as exc:
            return _blocked_result(
                check,
                f"the sandbox boundary raised while running check {check.id!r} "
                f"(observation {index + 1} of {check.reruns}): "
                f"{type(exc).__name__}: {exc}. This is BLOCKED, not failed: the "
                f"feature was not shown to be broken.",
                environment=_classify_environment(exc),
                duration_s=time.perf_counter() - started,
                port=port,
                port_allocation=allocation,
            )
        code = int(getattr(result, "exit_code", 1) or 0)
        last_code = code
        is_timeout = bool(getattr(result, "timed_out", False)) or code == 124
        if is_timeout:
            timed_out = True
            break
        observations.append(_verdict_for_exit(code, check.expects_exit))
        raw_chunks.append(
            f"--- e2e observation {index + 1}/{check.reruns} exit={code} ---"
        )
        raw_chunks.append(str(getattr(result, "stdout", "") or ""))
        raw_chunks.append(str(getattr(result, "stderr", "") or ""))
        container_name = (
            container_name
            or str(getattr(result, "container_name", "") or "")
        )

    duration = time.perf_counter() - started
    raw_output = "\n".join(chunk for chunk in raw_chunks if chunk)

    if timed_out:
        # An e2e check that times out is neither a failing feature nor a
        # passing one. `blocked` + `timed_out=True` says exactly that, and the
        # reason names the budget so an operator can see whether to raise it.
        return _blocked_result(
            check,
            f"end-to-end check {check.id!r} did not finish inside its "
            f"{int(check.timeout_s)}s budget (observation "
            f"{len(observations) + 1} of {max(1, int(check.reruns))}). A timeout "
            f"is not a failing feature and not a passing one: it is reported "
            f"as blocked with timed_out=True so the two cannot be confused.",
            environment=ENV_TIMEOUT_BUDGET,
            timed_out=True,
            duration_s=duration,
            exit_code=last_code,
            raw_output=raw_output,
            port=port,
            port_allocation=allocation,
            container_name=container_name,
        )

    flaky = _flaky_check(observations)
    # The LAST observation decides the verdict, matching
    # `execution/verify.py`'s "last run decides target_test_passed" rule.
    state = observations[-1] if observations else E2E_BLOCKED
    if state == E2E_BLOCKED:  # pragma: no cover - defensive
        return _blocked_result(
            check,
            f"end-to-end check {check.id!r} produced no observation",
            duration_s=duration,
            port=port,
            port_allocation=allocation,
        )
    if state == E2E_FAILED:
        reason = (
            f"the end-to-end check ran and exited {last_code}"
            + (
                f", not the declared {check.expects_exit}"
                if check.expects_exit is not None
                else ""
            )
            + (
                f"; the feature does not behave end to end. "
                f"{len(observations)} observation(s)."
                if len(observations) > 1
                else ""
            )
        )
    else:
        reason = (
            f"the end-to-end check ran and the feature behaved "
            f"(exit {last_code}, {len(observations)} observation(s))"
        )
    if flaky == "flaky_detected":
        reason += (
            f". FLAKY: the {len(observations)} observations disagreed "
            f"({list(observations)}). A flaky end-to-end check is not a "
            f"reliable signal in either direction."
        )

    return E2EResult(
        check_id=check.id,
        feature=check.feature,
        state=state,
        reason=reason,
        timed_out=False,
        flaky_check=flaky,
        environment=ENV_OK,
        duration_s=duration,
        exit_code=last_code,
        observations=tuple(observations),
        repetitions=len(observations),
        raw_output=raw_output,
        port=port,
        port_allocation=allocation,
        container_name=container_name,
        declared=True,
        ran=True,
    )


def port_alloc_host() -> str:
    """The one host a published e2e port may bind. Delegates to the allocator."""
    from execution import port_alloc

    return port_alloc.PORT_HOST


def run_e2e(
    repo_path: str,
    *,
    feature: Optional[str] = None,
    check_id: Optional[str] = None,
    requested: bool = True,
    is_final_gate: bool = False,
    policy: str = DEFAULT_E2E_RUN_POLICY,
    sandbox: Optional[Callable[..., Any]] = None,
    session_id: str = "",
    env: Optional[Dict[str, str]] = None,
    allow_network: Optional[bool] = None,
) -> E2EReport:
    """Run a repository's declared end-to-end checks and report honestly.

    ``requested`` defaults to ``True`` because calling this function IS the
    request — the run policy governs whether a caller should have called it,
    and it is consulted here so the decision and its reason are on the receipt
    rather than invisible at the call site.

    A repository with a **malformed or unreadable** declaration produces a
    report whose single row is ``blocked`` with the parse reason. That is the
    difference between "this repository has no end-to-end checks" and "this
    repository's end-to-end checks could not be read", and Phase 3's ledger
    records them differently.
    """
    load = load_e2e_spec(repo_path)
    declared_count = len(load.spec) if load.spec is not None else 0

    decision = should_run_e2e(
        policy=policy,
        requested=requested,
        is_final_gate=is_final_gate,
        declared_checks=declared_count,
    )

    if load.status in (SPEC_MALFORMED, SPEC_UNREADABLE):
        # No command to carry, so no E2ECheck is built: the row is about the
        # DECLARATION, and constructing a check with a placeholder command
        # would make a broken file look like a runnable check.
        row = E2EResult(
            check_id="<declaration>",
            feature="",
            state=E2E_BLOCKED,
            reason=(
                f"the repository's end-to-end declaration could not be read: "
                f"{load.reason}. This is reported as BLOCKED with a reason, "
                f"not as 'no checks declared' and not as a failure: a typo in "
                f"a JSON file must not read as a repository that has nothing "
                f"to check."
            ),
            flaky_check="not_run",
            environment=ENV_SPEC_UNREADABLE,
            declared=True,
            ran=False,
        )
        return E2EReport(
            results=(row,),
            policy=decision,
            spec_status=load.status,
            spec_reason=load.reason,
            session_id=str(session_id or ""),
        )

    if load.spec is None:
        return E2EReport(
            results=(),
            policy=decision,
            spec_status=load.status,
            spec_reason=load.reason,
            session_id=str(session_id or ""),
        )

    if not decision.run:
        # A DECLARED check that the policy declined is `not_run`, one row per
        # declared check, naming why. Returning no rows at all would leave a
        # report whose `verdict` reads "nothing_declared" about a repository
        # that DID declare end-to-end checks — the declaration would vanish
        # because a policy said no, which is the one thing a policy must never
        # be able to do.
        return E2EReport(
            results=tuple(
                _not_run_result(row, decision.reason) for row in load.spec.checks
            ),
            policy=decision,
            spec_status=load.status,
            spec_reason=decision.reason,
            session_id=str(session_id or ""),
        )

    wanted = list(load.spec.checks)
    if check_id:
        selected = [row for row in wanted if row.id == check_id]
        if not selected:
            return E2EReport(
                results=(),
                policy=decision,
                spec_status=load.status,
                spec_reason=(
                    f"no declared check is named {check_id!r}; the declaration "
                    f"has {', '.join(row.id for row in wanted) or 'no checks'}"
                ),
                session_id=str(session_id or ""),
            )
        wanted = selected
    if feature:
        wanted = [row for row in wanted if row.feature == feature]
        if not wanted:
            return E2EReport(
                results=(),
                policy=decision,
                spec_status=load.status,
                spec_reason=(
                    f"no declared check is for feature {feature!r}; declared "
                    f"features: {', '.join(load.spec.features) or 'none'}"
                ),
                session_id=str(session_id or ""),
            )

    results: List[E2EResult] = []
    for check in wanted:
        results.append(
            run_e2e_check(
                check,
                repo_path,
                sandbox=sandbox,
                session_id=str(session_id or ""),
                env=env,
                allow_network=allow_network,
            )
        )
    total = sum(row.duration_s for row in results)
    return E2EReport(
        results=tuple(results),
        policy=decision,
        spec_status=load.status,
        spec_reason=load.reason,
        total_duration_s=total,
        session_id=str(session_id or ""),
    )


def run_e2e_for_features(
    repo_path: str,
    features: Sequence[str],
    **kwargs: Any,
) -> Dict[str, E2EReport]:
    """Run the declared checks for each named feature, one report per feature.

    This is the shape a feature ledger consumes: one report per feature, so a
    ledger row can be "proved from this object" rather than from a whole-run
    pass.
    """
    reports: Dict[str, E2EReport] = {}
    for name in features or ():
        reports[str(name)] = run_e2e(repo_path, feature=str(name), **kwargs)
    return reports


# --------------------------------------------------------------------------
# The non-substitution check
# --------------------------------------------------------------------------


def neither_substitutes(
    verify_result: Any,
    e2e_report: E2EReport,
) -> Dict[str, Any]:
    """Refuse any claim that one answer stands in for the other.

    This is the executable form of the distinction. It reports whether the unit
    side and the end-to-end side agree, and it states — per side — whether
    that side SETTLED, which is the only thing that makes the disagreement
    meaningful. Two sides that disagree are not a contradiction to be smoothed;
    they are two different questions with two different answers, and a reader
    needs both.
    """
    from execution.verification_evidence import (
        DIMENSION_REGRESSION,
        DIMENSION_TARGET,
        evidence_of,
    )

    evidence = evidence_of(verify_result)
    unit = {
        "settled": False,
        "target": "not_run",
        "regression": "not_run",
        "reason": "no verification evidence was attached to this result",
    }
    if evidence is not None:
        unit = {
            "settled": evidence.measured,
            "target": evidence.dimension(DIMENSION_TARGET).state,
            "regression": evidence.dimension(DIMENSION_REGRESSION).state,
            "reason": (
                "unit verification produced at least one observation"
                if evidence.measured
                else "unit verification produced no observation at all"
            ),
        }
    else:
        unit["reason"] = (
            "this VerificationResult carries no verification_evidence block, so "
            "'do the tests pass' was never answered from this object"
        )

    e2e = {
        "settled": e2e_report.settled,
        "verdict": e2e_report.verdict,
        "declared": e2e_report.declared,
        "counts": {
            "passed": len(e2e_report.passed),
            "failed": len(e2e_report.failed),
            "blocked": len(e2e_report.blocked),
            "not_run": len(e2e_report.not_run),
        },
        "reason": (
            "every declared end-to-end check produced a verdict"
            if e2e_report.settled
            else "at least one declared end-to-end check is blocked or was not run"
        ),
    }

    agree = bool(unit["settled"] and e2e["settled"] and not e2e_report.failed)
    return {
        "unit": unit,
        "e2e": e2e,
        "both_settled_and_green": agree,
        "substitution_refused": True,
        "distinction": E2E_VERIFY_DISTINCTION,
        "note": (
            "verify asks 'do the tests pass' and e2e asks 'does the thing "
            "work'. Neither answer is evidence for the other. A repository "
            "that runs only e2e has flaky signal; one that runs only unit "
            "verification misses integration failures entirely."
        ),
    }
