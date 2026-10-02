"""Dynamic, machine-readable plan state with bounded step counts.

A plan here is a list of :class:`PlanStep` values. Every step is planned *by
behavior and files*: a step without a behavior is a planning error, and an
edit step without a file is refused. The step count is bounded to
``[PLAN_STEP_FLOOR, max_steps]`` with a hard ceiling of
:data:`PLAN_STEP_CEILING`.

The module is deliberately model-free. It owns the state machine, the
replan policy, and the persistence; a planner (model, or a human) supplies the
behaviors. Replanning is driven by measured step outcomes
(:func:`assess_step_outcome`) rather than by prose: a step that exhausts its
turn budget, touches no file, or violates a constraint is superseded by a
re-scoped step and the plan version is bumped, with the reason recorded in the
machine-readable state.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from runtime.fsutil import atomic_write_json, now_iso
from runtime.paths import validate_path_segment

PLAN_SCHEMA_VERSION = 1
PLAN_STEP_FLOOR = 2
PLAN_STEP_CEILING = 12
DEFAULT_MAX_PLAN_STEPS = 12
DEFAULT_STEP_TURN_BUDGET = 12

STEP_KINDS = ("edit", "explore", "verify", "review")
STEP_STATUSES = ("pending", "running", "completed", "failed", "superseded")
REPLAN_REASONS = ("constraint_violation", "turns_exhausted", "no_files_touched")


class PlanError(ValueError):
    """Raised when a plan or a plan step violates the planning contract."""


@dataclass
class PlanStep:
    """One independently checkable unit of planned work.

    ``behavior`` is mandatory and is the acceptance statement for the step.
    ``files`` is mandatory for ``edit`` steps: a plan is planned by behavior
    *and* files, never by prose alone. ``symbols`` is the optional AST-symbol
    scope the step is allowed to touch (see :mod:`runtime.symbols`).
    """

    step_id: str
    behavior: str
    kind: str = "edit"
    files: Tuple[str, ...] = ()
    symbols: Tuple[str, ...] = ()
    depends_on: Tuple[str, ...] = ()
    status: str = "pending"
    attempts: int = 0
    turn_budget: int = 0
    turns_used: int = 0
    files_touched: Tuple[str, ...] = ()
    constraint_violations: Tuple[str, ...] = ()
    verification: str = "pending"
    replan_reason: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        self.step_id = validate_path_segment(str(self.step_id or ""), "plan step id")
        self.behavior = " ".join(str(self.behavior or "").split())
        if not self.behavior:
            raise PlanError(f"plan step {self.step_id} has no behavior")
        self.kind = str(self.kind or "edit").strip().lower()
        if self.kind not in STEP_KINDS:
            raise PlanError(f"unsupported plan step kind: {self.kind}")
        self.files = _normalized_paths(self.files, "plan step files")
        if self.kind == "edit" and not self.files:
            raise PlanError(
                f"plan step {self.step_id} is an edit step with no target file; "
                "a plan is planned by behavior and files"
            )
        self.symbols = tuple(
            dict.fromkeys(
                str(item).strip() for item in self.symbols if str(item).strip()
            )
        )
        self.depends_on = tuple(
            dict.fromkeys(
                str(item).strip() for item in self.depends_on if str(item).strip()
            )
        )
        if self.step_id in self.depends_on:
            raise PlanError(f"plan step {self.step_id} cannot depend on itself")
        self.status = str(self.status or "pending").strip().lower()
        if self.status not in STEP_STATUSES:
            raise PlanError(f"unsupported plan step status: {self.status}")
        self.attempts = max(0, int(self.attempts))
        self.turn_budget = max(0, int(self.turn_budget))
        self.turns_used = max(0, int(self.turns_used))
        self.files_touched = _normalized_paths(self.files_touched, "touched files")
        self.constraint_violations = tuple(
            str(item).strip()
            for item in self.constraint_violations
            if str(item).strip()
        )
        self.verification = str(self.verification or "pending").strip().lower()
        self.replan_reason = str(self.replan_reason or "").strip()
        self.note = str(self.note or "")

    @property
    def is_terminal(self) -> bool:
        """Return whether the step can no longer make progress in this version."""
        return self.status in {"completed", "superseded"}

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible step."""
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlanStep":
        """Build a step from persisted or model-supplied JSON."""
        data = dict(value or {})
        return cls(
            step_id=str(data.get("step_id", data.get("id", ""))),
            behavior=str(data.get("behavior", data.get("description", ""))),
            kind=str(data.get("kind", "edit")),
            files=tuple(data.get("files", ())),
            symbols=tuple(data.get("symbols", ())),
            depends_on=tuple(data.get("depends_on", ())),
            status=str(data.get("status", "pending")),
            attempts=int(data.get("attempts", 0)),
            turn_budget=int(data.get("turn_budget", 0)),
            turns_used=int(data.get("turns_used", 0)),
            files_touched=tuple(data.get("files_touched", ())),
            constraint_violations=tuple(data.get("constraint_violations", ())),
            verification=str(data.get("verification", "pending")),
            replan_reason=str(data.get("replan_reason", "")),
            note=str(data.get("note", "")),
        )


@dataclass
class ReplanDecision:
    """Whether a measured step outcome requires a replan, and why."""

    replan: bool
    reason: str = ""
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible decision."""
        return {
            "replan": bool(self.replan),
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass
class PlanState:
    """Versioned, machine-readable plan state for one task or workflow."""

    plan_id: str
    issue: str
    steps: List[PlanStep] = field(default_factory=list)
    version: int = 1
    max_steps: int = DEFAULT_MAX_PLAN_STEPS
    status: str = "active"
    replans: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        self.plan_id = validate_path_segment(str(self.plan_id or ""), "plan id")
        self.issue = " ".join(str(self.issue or "").split())
        self.max_steps = clamp_max_steps(self.max_steps)
        self.steps = [step for step in self.steps if isinstance(step, PlanStep)]
        if not self.steps:
            raise PlanError(f"plan {self.plan_id} has no steps")
        if len(self.steps) > self.max_steps:
            raise PlanError(
                f"plan {self.plan_id} has {len(self.steps)} steps, above the "
                f"configured cap of {self.max_steps}"
            )
        if len(self.steps) < PLAN_STEP_FLOOR:
            raise PlanError(
                f"plan {self.plan_id} has {len(self.steps)} step(s); the floor is "
                f"{PLAN_STEP_FLOOR} so work is never one-shot"
            )
        known = {step.step_id for step in self.steps}
        for step in self.steps:
            unknown = [item for item in step.depends_on if item not in known]
            if unknown:
                raise PlanError(
                    f"plan step {step.step_id} depends on unknown steps: "
                    f"{', '.join(unknown)}"
                )
        self.version = max(1, int(self.version))
        self.status = str(self.status or "active").strip().lower()
        self.replans = [
            dict(item) for item in self.replans if isinstance(item, Mapping)
        ]

    # -- accessors ------------------------------------------------------
    def step(self, step_id: str) -> PlanStep:
        """Return one step by id or fail closed."""
        for item in self.steps:
            if item.step_id == str(step_id):
                return item
        raise PlanError(f"unknown plan step: {step_id}")

    def active_steps(self) -> List[PlanStep]:
        """Return the steps still expected to make progress."""
        return [step for step in self.steps if step.status in {"pending", "running"}]

    def next_step(self) -> Optional[PlanStep]:
        """Return the next runnable step, or ``None`` when none is runnable."""
        done = {step.step_id for step in self.steps if step.is_terminal}
        for step in self.steps:
            if step.status != "pending":
                continue
            if all(item in done for item in step.depends_on):
                return step
        return None

    def files_in_scope(self) -> Tuple[str, ...]:
        """Return every file any live step is allowed to touch."""
        selected: List[str] = []
        for step in self.steps:
            if step.status == "superseded":
                continue
            for path in step.files:
                if path not in selected:
                    selected.append(path)
        return tuple(selected)

    def owner_of(self, path: str) -> str:
        """Return the step id allowed to edit ``path``, or an empty string."""
        target = _normalize_path(path)
        for step in self.steps:
            if step.status == "superseded":
                continue
            if target in step.files:
                return step.step_id
        return ""

    def projection(self) -> Dict[str, Any]:
        """Return a compact status projection for dashboards and the CLI."""
        return {
            "plan_id": self.plan_id,
            "version": self.version,
            "status": self.status,
            "max_steps": self.max_steps,
            "step_count": len(self.steps),
            "completed_steps": [
                step.step_id for step in self.steps if step.status == "completed"
            ],
            "active_steps": [
                step.step_id for step in self.steps if step.status == "running"
            ],
            "pending_steps": [
                step.step_id for step in self.steps if step.status == "pending"
            ],
            "superseded_steps": [
                step.step_id for step in self.steps if step.status == "superseded"
            ],
            "replan_count": len(self.replans),
            "files_in_scope": list(self.files_in_scope()),
            "updated_at": self.updated_at,
        }

    def to_dict(self) -> Dict[str, Any]:
        """Return the full machine-readable plan state."""
        return {
            "schema_version": PLAN_SCHEMA_VERSION,
            "plan_id": self.plan_id,
            "issue": self.issue,
            "version": self.version,
            "max_steps": self.max_steps,
            "status": self.status,
            "steps": [step.to_dict() for step in self.steps],
            "replans": [dict(item) for item in self.replans],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlanState":
        """Rebuild plan state from persisted JSON."""
        data = dict(value or {})
        version = int(data.get("schema_version", PLAN_SCHEMA_VERSION))
        if version != PLAN_SCHEMA_VERSION:
            raise PlanError(f"unsupported plan schema version: {version}")
        return cls(
            plan_id=str(data.get("plan_id", "")),
            issue=str(data.get("issue", "")),
            steps=[PlanStep.from_dict(item) for item in data.get("steps", [])],
            version=int(data.get("version", 1)),
            max_steps=int(data.get("max_steps", DEFAULT_MAX_PLAN_STEPS)),
            status=str(data.get("status", "active")),
            replans=[dict(item) for item in data.get("replans", [])],
            created_at=str(data.get("created_at", now_iso())),
            updated_at=str(data.get("updated_at", now_iso())),
        )


def clamp_max_steps(value: Any) -> int:
    """Clamp a configured step cap into ``[PLAN_STEP_FLOOR, PLAN_STEP_CEILING]``."""
    try:
        cap = int(value)
    except (TypeError, ValueError) as exc:
        raise PlanError(f"invalid plan step cap: {value!r}") from exc
    return max(PLAN_STEP_FLOOR, min(PLAN_STEP_CEILING, cap))


def build_plan(
    issue: str,
    targets: Sequence[str | Mapping[str, Any]],
    *,
    plan_id: str = "plan",
    max_steps: Any = DEFAULT_MAX_PLAN_STEPS,
    turn_budget: int = DEFAULT_STEP_TURN_BUDGET,
    symbols_by_file: Optional[Mapping[str, Sequence[str]]] = None,
    append_verification: bool = True,
) -> PlanState:
    """Build a bounded plan from an issue and its target files.

    ``targets`` accepts plain file paths or mappings carrying ``file``/
    ``files``, ``behavior``/``description``, ``symbols``, and ``depends_on``.
    One target becomes one step; when there are more targets than the cap the
    files are distributed across the cap so no target is silently dropped. A
    verification step is appended so every plan ends on a checkable outcome,
    which is also what lifts a single-file task to the two-step floor.
    """
    cap = clamp_max_steps(max_steps)
    normalized = _normalize_targets(targets)
    if not normalized:
        normalized = [
            {
                "behavior": f"Locate the code responsible for: {str(issue or '').strip()}",
                "kind": "explore",
                "files": (),
            }
        ]
    if len(normalized) > cap:
        normalized = _distribute(normalized, cap)
    steps: List[PlanStep] = []
    for index, item in enumerate(normalized, start=1):
        files = tuple(item.get("files", ()))
        steps.append(
            PlanStep(
                step_id=f"s{index}",
                behavior=str(item.get("behavior") or _default_behavior(files, issue)),
                kind=str(item.get("kind") or "edit"),
                files=files,
                symbols=tuple(
                    item.get("symbols") or _symbols_for(files, symbols_by_file)
                ),
                depends_on=tuple(item.get("depends_on", ())),
                turn_budget=max(0, int(item.get("turn_budget", turn_budget))),
            )
        )
    if append_verification and len(steps) < cap:
        steps.append(
            PlanStep(
                step_id=f"s{len(steps) + 1}",
                behavior="Verify the accumulated change with the declared test evidence.",
                kind="verify",
                files=tuple(
                    dict.fromkeys(path for step in steps for path in step.files)
                ),
                turn_budget=max(1, int(turn_budget) // 2),
            )
        )
    return PlanState(
        plan_id=plan_id,
        issue=str(issue or ""),
        steps=steps,
        max_steps=cap,
    )


def plan_from_model_steps(
    plan_id: str,
    issue: str,
    payload: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    max_steps: Any = DEFAULT_MAX_PLAN_STEPS,
    turn_budget: int = DEFAULT_STEP_TURN_BUDGET,
) -> PlanState:
    """Adopt a model-authored plan, enforcing the same behavior/files contract.

    Used by the ``plan`` control tool so a planner's own decomposition becomes
    machine-readable plan state instead of prose. Steps without a behavior, and
    edit steps without a file, are refused rather than repaired silently.
    """
    cap = clamp_max_steps(max_steps)
    data = dict(payload or {}) if isinstance(payload, Mapping) else {}
    raw_steps = data.get("steps") if isinstance(payload, Mapping) else payload
    items = [dict(item) for item in (raw_steps or []) if isinstance(item, Mapping)]
    if not items:
        raise PlanError("a model plan must declare at least one step")
    if len(items) > cap:
        raise PlanError(
            f"model plan declared {len(items)} steps, above the configured cap of {cap}"
        )
    steps: List[PlanStep] = []
    for index, item in enumerate(items, start=1):
        files = item.get("files")
        if files is None and item.get("file"):
            files = [item["file"]]
        steps.append(
            PlanStep(
                step_id=str(item.get("step_id", item.get("id", f"s{index}"))),
                behavior=str(item.get("behavior", item.get("description", ""))),
                kind=str(item.get("kind", "edit")),
                files=tuple(files or ()),
                symbols=tuple(item.get("symbols") or ()),
                depends_on=tuple(item.get("depends_on") or ()),
                turn_budget=max(0, int(item.get("turn_budget", turn_budget))),
                note=str(item.get("note", "")),
            )
        )
    return PlanState(
        plan_id=plan_id, issue=str(issue or ""), steps=steps, max_steps=cap
    )


def assess_step_outcome(
    step: PlanStep,
    *,
    turns_used: Optional[int] = None,
    turn_budget: Optional[int] = None,
    files_touched: Optional[Sequence[str]] = None,
    constraint_violations: Optional[Sequence[str]] = None,
) -> ReplanDecision:
    """Decide whether a measured step outcome requires a replan.

    Triggers, in priority order: a constraint violation (the step is not
    allowed to do what it just did), an exhausted turn budget, and an edit
    step that touched no file at all. Returns a decision with a stable reason
    slug; it never mutates the plan.
    """
    violations = tuple(
        str(item).strip()
        for item in (constraint_violations or step.constraint_violations)
        if str(item).strip()
    )
    if violations:
        return ReplanDecision(
            True,
            "constraint_violation",
            f"step {step.step_id} violated: {', '.join(violations[:4])}",
        )
    budget = int(turn_budget if turn_budget is not None else step.turn_budget)
    used = int(turns_used if turns_used is not None else step.turns_used)
    if budget > 0 and used >= budget:
        return ReplanDecision(
            True,
            "turns_exhausted",
            f"step {step.step_id} used {used} of {budget} turns without completing",
        )
    touched = tuple(
        _normalize_path(item)
        for item in (files_touched if files_touched is not None else step.files_touched)
    )
    touched = tuple(item for item in touched if item)
    if step.kind == "edit" and not touched:
        return ReplanDecision(
            True,
            "no_files_touched",
            f"step {step.step_id} produced no file change for {', '.join(step.files) or 'its scope'}",
        )
    return ReplanDecision(False)


def record_step_outcome(
    plan: PlanState,
    step_id: str,
    *,
    status: Optional[str] = None,
    turns_used: Optional[int] = None,
    files_touched: Optional[Sequence[str]] = None,
    constraint_violations: Optional[Sequence[str]] = None,
    verification: Optional[str] = None,
) -> ReplanDecision:
    """Fold a measured outcome into plan state and return the replan decision.

    Mutating one step is a plain record: a turn counter, the files it actually
    touched, and any constraint violation. The returned decision is what the
    caller acts on; :func:`apply_replan` performs the re-scope.
    """
    step = plan.step(step_id)
    step.attempts += 1
    if turns_used is not None:
        step.turns_used = max(0, int(turns_used))
    if files_touched is not None:
        step.files_touched = _normalized_paths(files_touched, "touched files")
    if constraint_violations is not None:
        step.constraint_violations = tuple(
            str(item).strip() for item in constraint_violations if str(item).strip()
        )
    if status is not None:
        resolved = str(status).strip().lower()
        if resolved not in STEP_STATUSES:
            raise PlanError(f"unsupported plan step status: {status}")
        step.status = resolved
    if verification is not None:
        step.verification = str(verification).strip().lower()
    if step.status == "completed" and step.kind == "edit" and not step.files_touched:
        # A completed edit step that touched nothing is not a completion.
        step.status = "failed"
    plan.updated_at = now_iso()
    return assess_step_outcome(step)


def apply_replan(
    plan: PlanState,
    step_id: str,
    decision: ReplanDecision,
    *,
    behavior: Optional[str] = None,
    files: Optional[Sequence[str]] = None,
    symbols: Optional[Sequence[str]] = None,
    extra_targets: Sequence[str | Mapping[str, Any]] = (),
) -> PlanState:
    """Supersede one step and insert a re-scoped replacement.

    The plan version is bumped and the trigger reason is recorded, so the
    machine-readable state always shows why the work was re-decomposed. When
    the plan is already at its cap the replacement takes the superseded step's
    slot instead of growing the plan.
    """
    if not decision.replan:
        raise PlanError("apply_replan requires a replan decision")
    step = plan.step(step_id)
    if step.status == "superseded":
        raise PlanError(f"plan step {step_id} is already superseded")
    previous_version = plan.version
    step.status = "superseded"
    step.replan_reason = decision.reason
    step.note = (step.note + f" [{decision.reason}: {decision.detail}]").strip()
    scope = tuple(files) if files is not None else step.files
    if decision.reason in {"turns_exhausted", "no_files_touched"} and not scope:
        scope = step.files
    replacement_behavior = str(
        behavior
        or f"{step.behavior} (re-scoped after {decision.reason}; narrower, verifiable slice)"
    )
    replacement = PlanStep(
        step_id=f"{step.step_id}r{plan.version + 1}",
        behavior=replacement_behavior,
        kind=step.kind,
        files=scope,
        symbols=tuple(symbols) if symbols is not None else step.symbols,
        depends_on=step.depends_on,
        turn_budget=step.turn_budget,
        note=decision.detail,
    )
    index = plan.steps.index(step)
    plan.steps.insert(index + 1, replacement)
    for extra in extra_targets:
        if len(plan.steps) >= plan.max_steps:
            break
        items = _normalize_targets([extra])
        for offset, item in enumerate(items, start=1):
            if len(plan.steps) >= plan.max_steps:
                break
            plan.steps.append(
                PlanStep(
                    step_id=f"{replacement.step_id}x{offset}",
                    behavior=str(
                        item.get("behavior")
                        or _default_behavior(tuple(item.get("files", ())), plan.issue)
                    ),
                    kind=str(item.get("kind") or "edit"),
                    files=tuple(item.get("files", ())),
                    symbols=tuple(item.get("symbols", ())),
                    turn_budget=replacement.turn_budget,
                )
            )
    while len(plan.steps) > plan.max_steps:
        # Drop the newest superseded entry, never a live step.
        victim = next(
            (item for item in reversed(plan.steps) if item.status == "superseded"),
            None,
        )
        if victim is None:
            break
        plan.steps.remove(victim)
    plan.version = previous_version + 1
    plan.replans.append(
        {
            "step_id": step.step_id,
            "replacement_step_id": replacement.step_id,
            "reason": decision.reason,
            "detail": decision.detail,
            "from_version": previous_version,
            "to_version": plan.version,
            "ts": now_iso(),
        }
    )
    plan.updated_at = now_iso()
    return plan


def save_plan_state(path: str | Path, plan: PlanState) -> str:
    """Persist plan state atomically and return the written path."""
    target = Path(path)
    if target.is_dir():
        target = target / "plan_state.json"
    atomic_write_json(target, plan.to_dict())
    return str(target)


def load_plan_state(path: str | Path) -> PlanState:
    """Load plan state from disk, failing closed on a malformed document."""
    import json

    target = Path(path)
    if target.is_dir():
        target = target / "plan_state.json"
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PlanError(f"plan state is not an object: {target}")
    return PlanState.from_dict(data)


def render_plan_block(plan: PlanState, *, max_chars: int = 2000) -> str:
    """Render plan state as a bounded prompt block.

    The block is the machine-readable plan in prose clothing: one line per
    step with its id, kind, behavior, files, and dependencies, plus the plan
    version so a model can see that work was re-decomposed.
    """
    lines = [
        f"## Plan (v{plan.version}, {len(plan.steps)}/{plan.max_steps} steps, "
        f"{len(plan.replans)} replan(s))"
    ]
    for step in plan.steps:
        files = ", ".join(step.files) if step.files else "(no file scope)"
        deps = f" after {', '.join(step.depends_on)}" if step.depends_on else ""
        symbols = f" symbols={', '.join(step.symbols)}" if step.symbols else ""
        marker = {
            "pending": "[ ]",
            "running": "[~]",
            "completed": "[x]",
            "failed": "[!]",
            "superseded": "[~> superseded]",
        }.get(step.status, "[ ]")
        lines.append(
            f"{marker} {step.step_id} ({step.kind}): {step.behavior} "
            f"| files: {files}{symbols}{deps}"
        )
    if plan.replans:
        last = plan.replans[-1]
        lines.append(f"Last replan: {last.get('reason')} on {last.get('step_id')}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max(0, max_chars - 3)] + "..."
    return text


def plan_digest(plan: PlanState) -> str:
    """Return a stable digest of a plan version for identity and resume checks."""
    payload = repr(
        [
            plan.plan_id,
            plan.version,
            [
                (step.step_id, step.behavior, list(step.files), step.status)
                for step in plan.steps
            ],
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _normalize_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").strip().strip("/")


def _normalized_paths(values: Iterable[Any], label: str) -> Tuple[str, ...]:
    selected: List[str] = []
    for value in values or ():
        path = _normalize_path(value)
        if not path:
            continue
        if ".." in path.split("/"):
            raise PlanError(f"{label} rejects traversal: {value}")
        if path not in selected:
            selected.append(path)
    return tuple(selected)


def _default_behavior(files: Sequence[str], issue: str) -> str:
    if files:
        return (
            f"Change the behavior of {', '.join(files)} so: {str(issue or '').strip()}"
        )
    return f"Establish the required behavior: {str(issue or '').strip()}"


def _symbols_for(
    files: Sequence[str], symbols_by_file: Optional[Mapping[str, Sequence[str]]]
) -> Tuple[str, ...]:
    if not symbols_by_file:
        return ()
    selected: List[str] = []
    for path in files:
        for symbol in symbols_by_file.get(path, ()):  # type: ignore[union-attr]
            name = str(symbol).strip()
            if name and name not in selected:
                selected.append(name)
    return tuple(selected)


def _normalize_targets(targets: Sequence[Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for target in targets or ():
        if isinstance(target, Mapping):
            data = dict(target)
            files = data.get("files")
            if files is None and data.get("file"):
                files = [data["file"]]
            files = tuple(
                _normalize_path(item) for item in (files or ()) if _normalize_path(item)
            )
            kind = str(data.get("kind") or ("edit" if files else "explore"))
            if kind == "edit" and not files:
                raise PlanError(
                    f"plan target {data.get('step_id', data.get('id', '?'))} is an edit "
                    "with no target file; a plan is planned by behavior and files"
                )
            items.append(
                {
                    "behavior": str(
                        data.get("behavior", data.get("description", ""))
                    ).strip(),
                    "kind": kind,
                    "files": files,
                    "symbols": tuple(str(item) for item in (data.get("symbols") or ())),
                    "depends_on": tuple(
                        str(item) for item in (data.get("depends_on") or ())
                    ),
                    "turn_budget": int(data.get("turn_budget", 0) or 0),
                }
            )
            continue
        path = _normalize_path(target)
        if not path:
            continue
        items.append({"behavior": "", "kind": "edit", "files": (path,), "symbols": ()})
    return items


_WHITESPACE = re.compile(r"\s+")


def _distribute(items: List[Dict[str, Any]], cap: int) -> List[Dict[str, Any]]:
    """Fold more targets than the cap into exactly ``cap`` steps.

    Files are distributed round-robin so a wide change is still covered; each
    merged step keeps every file it absorbed, so the plan never silently drops
    a target.
    """
    buckets: List[Dict[str, Any]] = []
    for index, item in enumerate(items):
        slot = index % cap
        if slot >= len(buckets):
            buckets.append(
                {
                    "behavior": "",
                    "kind": item["kind"],
                    "files": [],
                    "symbols": [],
                    "depends_on": [],
                    "turn_budget": item.get("turn_budget", 0),
                }
            )
        bucket = buckets[slot]
        bucket["kind"] = "edit" if item["kind"] == "edit" else bucket["kind"]
        for path in item["files"]:
            if path not in bucket["files"]:
                bucket["files"].append(path)
        for symbol in item.get("symbols", ()):
            if symbol not in bucket["symbols"]:
                bucket["symbols"].append(symbol)
        if not bucket["behavior"]:
            bucket["behavior"] = item["behavior"]
    for bucket in buckets:
        bucket["files"] = tuple(bucket["files"])
        bucket["symbols"] = tuple(bucket["symbols"])
        bucket["depends_on"] = tuple(bucket["depends_on"])
        if not bucket["behavior"]:
            bucket["behavior"] = _default_behavior(bucket["files"], "")
    return buckets


def _collapse(text: str) -> str:
    return _WHITESPACE.sub(" ", str(text or "")).strip()


__all__ = [
    "DEFAULT_MAX_PLAN_STEPS",
    "DEFAULT_STEP_TURN_BUDGET",
    "PLAN_SCHEMA_VERSION",
    "PLAN_STEP_CEILING",
    "PLAN_STEP_FLOOR",
    "REPLAN_REASONS",
    "STEP_KINDS",
    "STEP_STATUSES",
    "PlanError",
    "PlanState",
    "PlanStep",
    "ReplanDecision",
    "apply_replan",
    "assess_step_outcome",
    "build_plan",
    "clamp_max_steps",
    "load_plan_state",
    "plan_digest",
    "plan_from_model_steps",
    "record_step_outcome",
    "render_plan_block",
    "save_plan_state",
]
