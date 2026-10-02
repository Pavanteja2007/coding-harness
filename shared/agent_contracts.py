"""Versioned, dependency-light contracts for Neo agent runs."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional

SCHEMA_VERSION = 1


class CompletionStatus(str, Enum):
    """Canonical terminal or suspended outcome for one agent run."""

    COMPLETED_VERIFIED = "completed_verified"
    COMPLETED_UNVERIFIED = "completed_unverified"
    NEEDS_INPUT = "needs_input"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"

    def __str__(self) -> str:
        """Return the stable serialized status value."""
        return self.value


RUN_STATUSES = frozenset(status.value for status in CompletionStatus)
SIDE_EFFECT_CLASSES = frozenset(
    {"read_only", "workspace_write", "process", "network", "external", "control"}
)
PERMISSION_ACTIONS = frozenset({"allow", "ask", "deny"})


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _copy_mapping(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _schema_version(value: Any) -> int:
    try:
        version = int(SCHEMA_VERSION if value in (None, 0) else value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid agent contract schema version: {value!r}") from exc
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported agent contract schema version {version}; expected {SCHEMA_VERSION}"
        )
    return version


def _json_dict(value: Mapping[str, Any]) -> Dict[str, Any]:
    result = dict(value)
    try:
        json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("agent contract must be JSON serializable") from exc
    return result


@dataclass
class SessionState:
    """Compact durable state for one conversation session."""

    session_id: str
    summary: str = ""
    active_task: str = ""
    active_run_id: str = ""
    repository_identity: str = ""
    prior_diff: str = ""
    unresolved_questions: List[str] = field(default_factory=list)
    project_instructions: List[str] = field(default_factory=list)
    turns: List[Dict[str, Any]] = field(default_factory=list)
    changed_files: List[str] = field(default_factory=list)
    plan_steps: List[str] = field(default_factory=list)
    todo_items: List[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.session_id = str(self.session_id or "")
        self.summary = str(self.summary or "")
        self.active_task = str(self.active_task or "")
        self.active_run_id = str(self.active_run_id or "")
        self.repository_identity = str(self.repository_identity or "")
        self.prior_diff = str(self.prior_diff or "")
        self.unresolved_questions = [
            str(item) for item in self.unresolved_questions or []
        ]
        self.project_instructions = [
            str(item) for item in self.project_instructions or []
        ]
        self.turns = [
            dict(item) for item in self.turns or [] if isinstance(item, Mapping)
        ]
        self.changed_files = [
            str(item).replace("\\", "/") for item in self.changed_files or []
        ]
        self.plan_steps = [str(item) for item in self.plan_steps or []]
        self.todo_items = [str(item) for item in self.todo_items or []]
        self.updated_at = float(self.updated_at or time.time())
        self.schema_version = _schema_version(self.schema_version)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible state object."""
        return _json_dict(asdict(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SessionState":
        """Build state from a versioned serialized mapping."""
        data = dict(value or {})
        return cls(
            session_id=data.get("session_id", ""),
            summary=data.get("summary", ""),
            active_task=data.get("active_task", ""),
            active_run_id=data.get("active_run_id", ""),
            repository_identity=data.get("repository_identity", ""),
            prior_diff=data.get("prior_diff", ""),
            unresolved_questions=data.get("unresolved_questions", []),
            project_instructions=data.get("project_instructions", []),
            turns=data.get("turns", []),
            changed_files=data.get("changed_files", []),
            plan_steps=data.get("plan_steps", []),
            todo_items=data.get("todo_items", []),
            updated_at=data.get("updated_at", time.time()),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )


@dataclass
class RunSpec:
    """Inputs and identity for one resumable agent run."""

    session_id: str
    run_id: str
    request: str
    repository_identity: str = ""
    strategy: str = "daily"
    workspace_policy: Dict[str, Any] = field(default_factory=dict)
    verification_policy: Dict[str, Any] = field(default_factory=dict)
    parent_run_id: Optional[str] = None
    resume_token: Optional[str] = None
    turn_id: str = "turn-1"
    repository: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    repo_path: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.session_id = str(self.session_id or "")
        self.run_id = str(self.run_id or "")
        self.request = str(self.request or "")
        self.repository_identity = str(
            self.repository_identity or self.repository or self.repo_path or ""
        )
        self.repository = str(self.repository or self.repository_identity or "")
        self.repo_path = str(self.repo_path or self.repository_identity or "")
        self.strategy = str(self.strategy or "daily").strip().lower()
        self.turn_id = str(self.turn_id or "turn-1")
        self.workspace_policy = _copy_mapping(self.workspace_policy)
        self.verification_policy = _copy_mapping(self.verification_policy)
        self.metadata = _copy_mapping(self.metadata)
        self.schema_version = _schema_version(self.schema_version)

    @property
    def parent_run(self) -> Optional[str]:
        """Return the parent run identifier."""
        return self.parent_run_id

    def validate(self) -> "RunSpec":
        """Validate required identity fields and return this spec."""
        missing = [
            name
            for name, item in (
                ("session_id", self.session_id),
                ("run_id", self.run_id),
                ("request", self.request),
                ("repository_identity", self.repository_identity),
            )
            if not item
        ]
        if missing:
            raise ValueError("RunSpec missing required fields: " + ", ".join(missing))
        return self

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible representation."""
        return _json_dict(asdict(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunSpec":
        """Build a spec from a versioned serialized mapping."""
        data = dict(value or {})
        return cls(
            session_id=data.get("session_id", ""),
            run_id=data.get("run_id", ""),
            request=data.get("request", ""),
            repository_identity=data.get(
                "repository_identity", data.get("repo_path", data.get("repository", ""))
            ),
            strategy=data.get("strategy", "daily"),
            workspace_policy=data.get("workspace_policy", {}),
            verification_policy=data.get("verification_policy", {}),
            parent_run_id=data.get("parent_run_id", data.get("parent_run")),
            resume_token=data.get("resume_token"),
            turn_id=data.get("turn_id", "turn-1"),
            repository=data.get("repository", ""),
            metadata=data.get("metadata", {}),
            repo_path=data.get("repo_path", ""),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )


@dataclass
class ToolCall:
    """A validated stable identity for one requested tool invocation."""

    call_id: str = field(default_factory=lambda: _new_id("call"))
    tool: str = ""
    arguments: Dict[str, Any] = field(default_factory=dict)
    side_effect_class: str = "read_only"
    target: str = ""
    status: str = "pending"
    result_reference: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.call_id = str(self.call_id or _new_id("call"))
        self.tool = str(self.tool or "").strip().lower()
        self.arguments = _copy_mapping(self.arguments)
        self.side_effect_class = str(self.side_effect_class or "read_only")
        self.target = str(self.target or "")
        self.status = str(self.status or "pending")
        self.result_reference = str(self.result_reference or "")
        self.metadata = _copy_mapping(self.metadata)
        self.schema_version = _schema_version(self.schema_version)

    @property
    def id(self) -> str:
        """Return the stable call identifier."""
        return self.call_id

    @property
    def name(self) -> str:
        """Return the tool name."""
        return self.tool

    @property
    def args(self) -> Dict[str, Any]:
        """Return typed tool arguments."""
        return self.arguments

    def with_status(self, status: str, result_reference: str = "") -> "ToolCall":
        """Return a copy with a terminal or pending status."""
        return ToolCall(
            call_id=self.call_id,
            tool=self.tool,
            arguments=dict(self.arguments),
            side_effect_class=self.side_effect_class,
            target=self.target,
            status=status,
            result_reference=result_reference or self.result_reference,
            metadata=dict(self.metadata),
            schema_version=self.schema_version,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible representation."""
        return _json_dict(asdict(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ToolCall":
        """Build a call from serialized fields, accepting legacy aliases."""
        data = dict(value or {})
        tool = str(data.get("tool", data.get("name", "")) or "").strip().lower()
        if "arguments" in data:
            arguments = data.get("arguments", {})
        elif tool in {"mcp", "mcp_call"} and any(
            key in data for key in ("server", "name")
        ):
            arguments = {
                key: item
                for key, item in data.items()
                if key
                not in {
                    "call_id",
                    "id",
                    "tool",
                    "side_effect_class",
                    "side_effect",
                    "target",
                    "status",
                    "result_reference",
                    "result_ref",
                    "metadata",
                    "schema_version",
                }
            }
        else:
            arguments = data.get("args", {})
        return cls(
            call_id=data.get("call_id", data.get("id", "")),
            tool=tool,
            arguments=arguments,
            side_effect_class=data.get(
                "side_effect_class", data.get("side_effect", "read_only")
            ),
            target=data.get("target", ""),
            status=data.get("status", "pending"),
            result_reference=data.get("result_reference", data.get("result_ref", "")),
            metadata=data.get("metadata", {}),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )


@dataclass
class PermissionDecision:
    """The result of evaluating one typed call against policy."""

    matched_rule: str
    action: str
    scope: str
    actor: str
    call_id: str
    exact_effect: str
    reason: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.matched_rule = str(self.matched_rule or "default")
        self.action = str(self.action or "deny").lower()
        self.scope = str(self.scope or "once")
        self.actor = str(self.actor or "agent")
        self.call_id = str(self.call_id or "")
        self.exact_effect = str(self.exact_effect or "")
        self.reason = str(self.reason or "")
        if self.action not in PERMISSION_ACTIONS:
            raise ValueError(f"unsupported permission action: {self.action}")
        self.schema_version = _schema_version(self.schema_version)

    @property
    def decision(self) -> str:
        """Return the decision action alias."""
        return self.action

    @property
    def effect(self) -> str:
        """Return the exact-effect alias."""
        return self.exact_effect

    @property
    def allowed(self) -> bool:
        """Return whether execution may proceed without another prompt."""
        return self.action == "allow"

    @property
    def needs_approval(self) -> bool:
        """Return whether an approval decision is required."""
        return self.action == "ask"

    @property
    def terminal(self) -> bool:
        """Return whether refusal is terminal for this call."""
        return self.action == "deny"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible representation."""
        return _json_dict(asdict(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PermissionDecision":
        """Build a decision from serialized fields."""
        data = dict(value or {})
        return cls(
            matched_rule=data.get("matched_rule", "default"),
            action=data.get("action", "deny"),
            scope=data.get("scope", "once"),
            actor=data.get("actor", "agent"),
            call_id=data.get("call_id", ""),
            exact_effect=data.get("exact_effect", ""),
            reason=data.get("reason", ""),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )


@dataclass
class RunEvent:
    """One ordered event in a run's authoritative journal."""

    sequence: int
    timestamp: float
    session_id: str
    run_id: str
    turn_id: str
    event_type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.sequence = int(self.sequence)
        self.timestamp = float(self.timestamp)
        self.session_id = str(self.session_id or "")
        self.run_id = str(self.run_id or "")
        self.turn_id = str(self.turn_id or "")
        self.event_type = str(self.event_type or "")
        self.payload = _copy_mapping(self.payload)
        self.schema_version = _schema_version(self.schema_version)

    @property
    def seq(self) -> int:
        """Return the short sequence alias."""
        return self.sequence

    @property
    def kind(self) -> str:
        """Return the legacy trace discriminator."""
        if self.event_type == "run_started":
            return "task_start"
        if self.event_type == "run_finished":
            return "task_end"
        return self.event_type

    @property
    def data(self) -> Dict[str, Any]:
        """Return the legacy trace payload alias."""
        return self.payload

    def to_dict(self) -> Dict[str, Any]:
        """Return normalized fields plus legacy trace aliases."""
        return {
            "ts": round(self.timestamp, 3),
            "sequence": self.sequence,
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "event": self.event_type,
            "payload": self.payload,
            "kind": self.kind,
            "data": self.payload,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunEvent":
        """Build an event from normalized or legacy trace data."""
        data = dict(value or {})
        return cls(
            sequence=int(data.get("sequence", 0)),
            timestamp=float(data.get("timestamp", data.get("ts", time.time()))),
            session_id=data.get("session_id", ""),
            run_id=data.get("run_id", ""),
            turn_id=data.get("turn_id", ""),
            event_type=data.get("event_type", data.get("event", data.get("kind", ""))),
            payload=data.get("payload", data.get("data", {})),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )


@dataclass
class Checkpoint:
    """A resumable snapshot of run progress and external references."""

    last_event_sequence: int
    model_context_references: List[str] = field(default_factory=list)
    agent_owned_changes: List[str] = field(default_factory=list)
    active_processes: List[Dict[str, Any]] = field(default_factory=list)
    spend: float = 0.0
    resume_token: str = ""
    session_id: str = ""
    run_id: str = ""
    turn_id: str = ""
    created_at: float = field(default_factory=time.time)
    schema_version: int = SCHEMA_VERSION
    repository_identity: str = ""
    request_identity: str = ""
    revision_identity: str = ""
    resume_namespace: str = ""
    #: AGT-08: the effort rung this run is identified by. Resuming a
    #: high-effort run at low effort is a lie about the run, so the rung joins
    #: the resume identity exactly like the revision does. Additive with a
    #: default, so an older journal row round-trips as an empty field.
    effort_identity: str = ""

    def __post_init__(self) -> None:
        self.last_event_sequence = int(self.last_event_sequence or 0)
        self.model_context_references = [
            str(item) for item in self.model_context_references or []
        ]
        self.agent_owned_changes = [
            str(item).replace("\\", "/") for item in self.agent_owned_changes or []
        ]
        self.active_processes = [dict(item) for item in self.active_processes or []]
        self.spend = float(self.spend or 0.0)
        self.resume_token = str(self.resume_token or _new_id("resume"))
        self.session_id = str(self.session_id or "")
        self.run_id = str(self.run_id or "")
        self.turn_id = str(self.turn_id or "")
        self.repository_identity = str(self.repository_identity or "")
        self.request_identity = str(self.request_identity or "")
        self.revision_identity = str(self.revision_identity or "")
        self.resume_namespace = str(self.resume_namespace or "")
        self.effort_identity = str(self.effort_identity or "")
        self.schema_version = _schema_version(self.schema_version)

    @property
    def last_event_seq(self) -> int:
        """Return the short last-event-sequence alias."""
        return self.last_event_sequence

    @property
    def request_fingerprint(self) -> str:
        """Return the request identity used by resume validation."""
        return self.request_identity

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible representation."""
        return _json_dict(asdict(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Checkpoint":
        """Build a checkpoint from serialized fields."""
        data = dict(value or {})
        return cls(
            last_event_sequence=data.get("last_event_sequence", 0),
            model_context_references=data.get("model_context_references", []),
            agent_owned_changes=data.get("agent_owned_changes", []),
            active_processes=data.get("active_processes", []),
            spend=data.get("spend", 0.0),
            resume_token=data.get("resume_token", ""),
            session_id=data.get("session_id", ""),
            run_id=data.get("run_id", ""),
            turn_id=data.get("turn_id", ""),
            created_at=data.get("created_at", time.time()),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            repository_identity=data.get(
                "repository_identity", data.get("repo_identity", "")
            ),
            request_identity=data.get(
                "request_identity",
                data.get("request_fingerprint", data.get("request_hash", "")),
            ),
            revision_identity=data.get("revision_identity", data.get("revision", "")),
            resume_namespace=data.get("resume_namespace", ""),
            effort_identity=data.get("effort_identity", ""),
        )


@dataclass
class RunResult:
    """A typed evidence-backed outcome for one agent run."""

    status: CompletionStatus | str
    answer: str = ""
    changed_files: List[str] = field(default_factory=list)
    verification_evidence: List[Dict[str, Any]] = field(default_factory=list)
    cost: float = 0.0
    attempts: int = 0
    resume_availability: str = "unavailable"
    follow_up_needs: List[str] = field(default_factory=list)
    run_id: str = ""
    session_id: str = ""
    trace_path: str = ""
    checkpoint_path: str = ""
    diff: str = ""
    model_calls: List[Dict[str, Any]] = field(default_factory=list)
    error: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        status = (
            self.status.value
            if isinstance(self.status, CompletionStatus)
            else str(self.status)
        )
        if status not in RUN_STATUSES:
            raise ValueError(f"unsupported completion status: {status}")
        self.status = status
        self.answer = str(self.answer or "")
        self.changed_files = [
            str(item).replace("\\", "/") for item in self.changed_files or []
        ]
        self.verification_evidence = [
            dict(item) for item in self.verification_evidence or []
        ]
        self.cost = float(self.cost or 0.0)
        self.attempts = int(self.attempts or 0)
        self.resume_availability = str(self.resume_availability or "unavailable")
        self.follow_up_needs = [str(item) for item in self.follow_up_needs or []]
        self.run_id = str(self.run_id or "")
        self.session_id = str(self.session_id or "")
        self.trace_path = str(self.trace_path or "")
        self.checkpoint_path = str(self.checkpoint_path or "")
        self.diff = str(self.diff or "")
        self.model_calls = [dict(item) for item in self.model_calls or []]
        self.error = str(self.error or "")
        self.metadata = _copy_mapping(self.metadata)
        self.schema_version = _schema_version(self.schema_version)

    @property
    def files_touched(self) -> List[str]:
        """Return the changed-file compatibility alias."""
        return self.changed_files

    @property
    def verification(self) -> List[Dict[str, Any]]:
        """Return verification evidence under the legacy field name."""
        return self.verification_evidence

    @property
    def cost_usd(self) -> float:
        """Return the cost compatibility alias."""
        return self.cost

    @property
    def completed_verified(self) -> bool:
        """Return whether the run has verifier-backed completion."""
        return self.status == CompletionStatus.COMPLETED_VERIFIED.value

    @property
    def ok(self) -> bool:
        """Return whether the run reached either completed state."""
        return self.status in {
            CompletionStatus.COMPLETED_VERIFIED.value,
            CompletionStatus.COMPLETED_UNVERIFIED.value,
        }

    @property
    def has_clean_verification(self) -> bool:
        """Return whether evidence proves a clean target and regression pass."""
        return any(
            item.get("kind") == "verification"
            and bool(item.get("target_passed", item.get("target_test_passed", False)))
            and bool(item.get("regression_passed", False))
            and not bool(item.get("flaky", False))
            and not item.get("error")
            for item in self.verification_evidence
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible result."""
        return _json_dict(asdict(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunResult":
        """Build a result from serialized fields."""
        data = dict(value or {})
        return cls(
            status=data.get("status", CompletionStatus.FAILED.value),
            answer=data.get("answer", ""),
            changed_files=data.get("changed_files", data.get("files_touched", [])),
            verification_evidence=data.get("verification_evidence", []),
            cost=data.get("cost", data.get("cost_usd", 0.0)),
            attempts=data.get("attempts", 0),
            resume_availability=data.get("resume_availability", "unavailable"),
            follow_up_needs=data.get("follow_up_needs", []),
            run_id=data.get("run_id", data.get("task_id", "")),
            session_id=data.get("session_id", ""),
            trace_path=data.get("trace_path", ""),
            checkpoint_path=data.get("checkpoint_path", ""),
            diff=data.get("diff", ""),
            model_calls=data.get("model_calls", []),
            error=data.get("error", ""),
            metadata=data.get("metadata", {}),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )
