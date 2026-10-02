"""Bounded DAG orchestration for isolated agent children.

The orchestrator accepts a finite, validated workflow graph. It owns claims,
approvals, worktrees, child supervision, durable graph checkpoints, handoffs,
and live status; child agents never receive a spawn tool.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from harness.agent_kernel import CompletionStatus, RunResult
from runtime.fsutil import (
    append_jsonl,
    atomic_write_json,
    extract_sensitive_config,
    is_pid_alive,
    now_epoch,
    now_iso,
    read_json,
    read_json_or_none,
    read_jsonl,
    redact_sensitive_config,
)
from runtime.paths import validate_path_segment
from runtime.roles import RoleProfile, get_role_profile
from runtime.subagents import (
    AgentDefinition,
    SpawnRequestStore,
    SubagentLimitError,
    SubagentLimits,
    SubagentRequest,
    SubagentSpawner,
)
from runtime.symbols import (
    claim_resources as build_claim_resources,
)
from runtime.symbols import (
    unclaimed_symbol_edits,
)
from runtime.worktrees import WorktreeError, WorktreeManager, WorktreeRecord
from shared import tracing
from shared.security import (
    SecurityViolation,
    append_approval_audit,
    redact_secrets,
    safe_relative_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATION_SECRETS_ENV = "NEO_ORCHESTRATION_SECRETS"
ORCHESTRATION_SCHEMA_VERSION = 1
_TERMINAL_NODE_STATUSES = {
    "completed",
    "blocked",
    "failed",
    "timeout",
    "cancelled",
    "needs_input",
}
_SUCCESS_STATUSES = {"completed_verified", "completed_unverified"}


class OrchestrationError(RuntimeError):
    """Raised when a workflow cannot be safely constructed or resumed."""


class ClaimConflict(OrchestrationError):
    """Raised when a node's declared resource scope is already claimed."""


@dataclass
class WorkflowLimits:
    """Finite resource and recursion limits for one workflow."""

    max_nodes: int = 32
    max_depth: int = 4
    max_fanout: int = 8
    max_concurrency: int = 4
    max_total_cost_usd: float = 10.0
    max_child_cost_usd: float = 2.0
    max_child_retries: int = 1
    max_child_wallclock_s: float = 900.0
    max_workflow_wallclock_s: float = 3600.0
    expensive_approval_threshold_usd: float = 0.5
    approval_timeout_s: Optional[float] = None
    git_timeout_s: float = 30.0
    max_patch_bytes: int = 8 * 1024 * 1024
    # -- child-spawn bounds (the bounded `task` tool is admitted here) --
    max_children_per_parent: int = 4
    max_dynamic_nodes: int = 8
    # -- claim leases: a crashed agent's claims are reclaimable --
    claim_lease_s: float = 120.0
    # -- symbol-scoped claims and serialized, verified merges --
    symbol_claims: bool = True
    merge_enabled: bool = True
    merge_verify_timeout_s: float = 300.0
    coordination_budget_fraction: float = 0.10

    def __post_init__(self) -> None:
        for name in (
            "max_nodes",
            "max_depth",
            "max_fanout",
            "max_concurrency",
            "max_child_retries",
            "max_children_per_parent",
            "max_dynamic_nodes",
        ):
            value = int(getattr(self, name))
            if value < 0 or (name != "max_child_retries" and value < 1):
                raise ValueError(f"{name} must be a non-negative bounded integer")
            object.__setattr__(self, name, value)
        for name in (
            "max_total_cost_usd",
            "max_child_cost_usd",
            "max_child_wallclock_s",
            "max_workflow_wallclock_s",
            "expensive_approval_threshold_usd",
            "git_timeout_s",
            "claim_lease_s",
            "merge_verify_timeout_s",
        ):
            value = float(getattr(self, name))
            if value <= 0:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        fraction = float(self.coordination_budget_fraction)
        if not 0 < fraction <= 1:
            raise ValueError("coordination_budget_fraction must be within (0, 1]")
        object.__setattr__(self, "coordination_budget_fraction", fraction)
        object.__setattr__(self, "symbol_claims", bool(self.symbol_claims))
        object.__setattr__(self, "merge_enabled", bool(self.merge_enabled))
        if self.approval_timeout_s is not None:
            timeout = float(self.approval_timeout_s)
            if timeout <= 0:
                raise ValueError("approval_timeout_s must be positive or None")
            object.__setattr__(self, "approval_timeout_s", timeout)
        if int(self.max_patch_bytes) < 1024:
            raise ValueError("max_patch_bytes must be at least 1024")
        object.__setattr__(self, "max_patch_bytes", int(self.max_patch_bytes))

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible limit set."""
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "WorkflowLimits":
        """Build limits from persisted or caller-provided values."""
        data = dict(value or {})
        return cls(
            **{
                key: item
                for key, item in data.items()
                if key in cls.__dataclass_fields__
            }
        )


@dataclass
class WorkflowNode:
    """One finite child or parent node in a workflow DAG."""

    node_id: str
    role: str
    request: str
    parent_id: str = ""
    depends_on: tuple[str, ...] = ()
    file_scopes: tuple[str, ...] = ()
    config: Dict[str, Any] = field(default_factory=dict)
    estimated_cost_usd: Optional[float] = None
    force_approval: bool = False
    verification_policy: Dict[str, Any] = field(default_factory=dict)
    workspace_policy: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.node_id = validate_path_segment(
            str(self.node_id or ""), "workflow node id"
        )
        self.role = get_role_profile(self.role).name
        self.request = str(self.request or "")
        self.parent_id = str(self.parent_id or "")
        self.depends_on = tuple(
            dict.fromkeys(
                str(item).strip() for item in self.depends_on if str(item).strip()
            )
        )
        if self.node_id in self.depends_on:
            raise ValueError(f"workflow node {self.node_id} cannot depend on itself")
        self.file_scopes = tuple(
            dict.fromkeys(
                str(item).replace("\\", "/").strip()
                for item in self.file_scopes
                if str(item).strip()
            )
        )
        if not isinstance(self.config, dict):
            raise ValueError("workflow node config must be an object")
        if self.estimated_cost_usd is not None:
            self.estimated_cost_usd = max(0.0, float(self.estimated_cost_usd))
        self.force_approval = bool(self.force_approval)
        if not isinstance(self.verification_policy, dict):
            raise ValueError("verification_policy must be an object")
        if not isinstance(self.workspace_policy, dict):
            raise ValueError("workspace_policy must be an object")
        if not isinstance(self.metadata, dict):
            raise ValueError("workflow node metadata must be an object")

    @property
    def profile(self) -> RoleProfile:
        """Return the immutable role profile for this node."""
        return get_role_profile(self.role)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible node definition."""
        return {
            "node_id": self.node_id,
            "role": self.role,
            "request": self.request,
            "parent_id": self.parent_id,
            "depends_on": list(self.depends_on),
            "file_scopes": list(self.file_scopes),
            "config": dict(self.config),
            "estimated_cost_usd": self.estimated_cost_usd,
            "force_approval": self.force_approval,
            "verification_policy": dict(self.verification_policy),
            "workspace_policy": dict(self.workspace_policy),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkflowNode":
        """Build a node from a serialized mapping."""
        data = dict(value or {})
        return cls(
            node_id=str(data.get("node_id", data.get("id", ""))),
            role=str(data.get("role", "implementer")),
            request=str(data.get("request", data.get("description", ""))),
            parent_id=str(data.get("parent_id", "")),
            depends_on=tuple(data.get("depends_on", data.get("dependencies", []))),
            file_scopes=tuple(data.get("file_scopes", data.get("claims", []))),
            config=dict(data.get("config", {})),
            estimated_cost_usd=data.get("estimated_cost_usd"),
            force_approval=bool(data.get("force_approval", False)),
            verification_policy=dict(data.get("verification_policy", {})),
            workspace_policy=dict(data.get("workspace_policy", {})),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class WorkflowSpec:
    """A finite parent/child workflow definition."""

    workflow_id: str
    repo_path: str
    nodes: tuple[WorkflowNode, ...]
    parent_request: str = ""
    limits: WorkflowLimits = field(default_factory=WorkflowLimits)
    config: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.workflow_id = validate_path_segment(
            str(self.workflow_id or ""), "workflow id"
        )
        self.repo_path = str(self.repo_path or "").strip()
        if not self.repo_path:
            raise ValueError("workflow repo_path is required")
        if not isinstance(self.nodes, tuple):
            self.nodes = tuple(
                item if isinstance(item, WorkflowNode) else WorkflowNode.from_dict(item)
                for item in self.nodes
            )
        if not isinstance(self.limits, WorkflowLimits):
            self.limits = WorkflowLimits.from_dict(self.limits)
        if not isinstance(self.config, dict):
            raise ValueError("workflow config must be an object")
        if not isinstance(self.metadata, dict):
            raise ValueError("workflow metadata must be an object")
        self.validate()

    def validate(self) -> "WorkflowSpec":
        """Validate identities, DAG edges, depth, fanout, and cost bounds."""
        if not self.nodes:
            raise ValueError("workflow must contain at least one node")
        if len(self.nodes) > self.limits.max_nodes:
            raise ValueError("workflow exceeds max_nodes")
        by_id: Dict[str, WorkflowNode] = {}
        for node in self.nodes:
            if node.node_id in by_id:
                raise ValueError(f"duplicate workflow node id: {node.node_id}")
            by_id[node.node_id] = node
        for node in self.nodes:
            for dependency in node.depends_on:
                if dependency not in by_id:
                    raise ValueError(
                        f"workflow node {node.node_id} references missing dependency {dependency}"
                    )
            if node.parent_id and node.parent_id not in by_id:
                raise ValueError(
                    f"workflow node {node.node_id} references missing parent {node.parent_id}"
                )
        children: Dict[str, List[str]] = {node.node_id: [] for node in self.nodes}
        for node in self.nodes:
            if node.parent_id:
                children[node.parent_id].append(node.node_id)
        if any(len(items) > self.limits.max_fanout for items in children.values()):
            raise ValueError("workflow exceeds max_fanout")
        depths: Dict[str, int] = {}
        visiting: set[str] = set()

        def visit(node_id: str) -> int:
            if node_id in depths:
                return depths[node_id]
            if node_id in visiting:
                raise ValueError("workflow dependency graph contains a cycle")
            visiting.add(node_id)
            node = by_id[node_id]
            dependencies = list(node.depends_on)
            if node.parent_id:
                dependencies.append(node.parent_id)
            depth = 0
            for dependency in dependencies:
                depth = max(depth, visit(dependency) + 1)
            visiting.remove(node_id)
            depths[node_id] = depth
            if depth > self.limits.max_depth:
                raise ValueError("workflow exceeds max_depth")
            return depth

        for node in self.nodes:
            visit(node.node_id)
        for node in self.nodes:
            estimate = node.estimated_cost_usd
            if estimate is None:
                estimate = node.profile.estimated_cost_usd
            if estimate > self.limits.max_child_cost_usd:
                raise ValueError(
                    f"workflow node {node.node_id} exceeds max_child_cost_usd"
                )
            json.dumps(redact_secrets(self.config), ensure_ascii=False)
            json.dumps(redact_secrets(node.to_dict()), ensure_ascii=False)
        return self

    def node_map(self) -> Dict[str, WorkflowNode]:
        """Return nodes keyed by stable ID."""
        return {node.node_id: node for node in self.nodes}

    def dependencies_for(self, node: WorkflowNode | str) -> tuple[str, ...]:
        """Return explicit dependencies plus the implicit parent edge."""
        selected = self.node_map()[node] if isinstance(node, str) else node
        return tuple(
            dict.fromkeys(
                (
                    *selected.depends_on,
                    *([selected.parent_id] if selected.parent_id else []),
                )
            )
        )

    def children_of(self, node_id: str) -> tuple[str, ...]:
        """Return direct child node IDs in declaration order."""
        return tuple(node.node_id for node in self.nodes if node.parent_id == node_id)

    def topological_order(self) -> tuple[str, ...]:
        """Return a deterministic dependency-first node order."""
        by_id = self.node_map()
        indegree = {node_id: 0 for node_id in by_id}
        for node in by_id.values():
            for _dependency in self.dependencies_for(node):
                indegree[node.node_id] += 1
        ready = [node.node_id for node in self.nodes if indegree[node.node_id] == 0]
        ordered: List[str] = []
        while ready:
            current = ready.pop(0)
            ordered.append(current)
            for node in self.nodes:
                if current in self.dependencies_for(node):
                    indegree[node.node_id] -= 1
                    if indegree[node.node_id] == 0:
                        ready.append(node.node_id)
        if len(ordered) != len(by_id):
            raise ValueError("workflow dependency graph contains a cycle")
        return tuple(ordered)

    def topological_nodes(self) -> tuple[WorkflowNode, ...]:
        """Return the nodes themselves in deterministic dependency-first order."""
        by_id = self.node_map()
        return tuple(by_id[node_id] for node_id in self.topological_order())

    def to_dict(self) -> Dict[str, Any]:
        """Return a versioned JSON-compatible workflow definition."""
        return {
            "schema_version": ORCHESTRATION_SCHEMA_VERSION,
            "workflow_id": self.workflow_id,
            "repo_path": self.repo_path,
            "parent_request": self.parent_request,
            "nodes": [node.to_dict() for node in self.nodes],
            "limits": self.limits.to_dict(),
            "config": dict(self.config),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkflowSpec":
        """Build a workflow from a serialized definition."""
        data = dict(value or {})
        nodes = tuple(
            item if isinstance(item, WorkflowNode) else WorkflowNode.from_dict(item)
            for item in data.get("nodes", [])
        )
        return cls(
            workflow_id=str(data.get("workflow_id", data.get("id", ""))),
            repo_path=str(data.get("repo_path", "")),
            nodes=nodes,
            parent_request=str(data.get("parent_request", "")),
            limits=WorkflowLimits.from_dict(data.get("limits")),
            config=dict(data.get("config", {})),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class HandoffSummary:
    """Structured child result passed to a parent continuation."""

    node_id: str
    run_id: str
    session_id: str
    role: str
    status: str
    answer: str = ""
    changed_files: List[str] = field(default_factory=list)
    diff: str = ""
    patch_path: str = ""
    verification_evidence: List[Dict[str, Any]] = field(default_factory=list)
    follow_up_needs: List[str] = field(default_factory=list)
    cost_usd: float = 0.0
    attempts: int = 0
    trace_path: str = ""
    checkpoint_path: str = ""
    workspace_path: str = ""
    parent_run_id: str = ""
    phase: str = "initial"
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a redacted JSON-compatible handoff."""
        return redact_secrets(asdict(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HandoffSummary":
        """Build a handoff from persisted JSON."""
        data = dict(value or {})
        return cls(
            node_id=str(data.get("node_id", "")),
            run_id=str(data.get("run_id", "")),
            session_id=str(data.get("session_id", "")),
            role=str(data.get("role", "")),
            status=str(data.get("status", "")),
            answer=str(data.get("answer", "")),
            changed_files=[str(item) for item in data.get("changed_files", [])],
            diff=str(data.get("diff", "")),
            patch_path=str(data.get("patch_path", "")),
            verification_evidence=[
                dict(item) for item in data.get("verification_evidence", [])
            ],
            follow_up_needs=[str(item) for item in data.get("follow_up_needs", [])],
            cost_usd=float(data.get("cost_usd", 0.0)),
            attempts=int(data.get("attempts", 0)),
            trace_path=str(data.get("trace_path", "")),
            checkpoint_path=str(data.get("checkpoint_path", "")),
            workspace_path=str(data.get("workspace_path", "")),
            parent_run_id=str(data.get("parent_run_id", "")),
            phase=str(data.get("phase", "initial")),
            error=str(data.get("error", "")),
        )


@dataclass
class NodeState:
    """Durable state for one workflow node and its optional continuation."""

    node_id: str
    session_id: str
    status: str = "pending"
    phase: str = "initial"
    run_id: str = ""
    continuation_run_id: str = ""
    attempts: int = 0
    reserved_cost_usd: float = 0.0
    workspace_path: str = ""
    patch_path: str = ""
    handoff_path: str = ""
    result: Dict[str, Any] = field(default_factory=dict)
    continuation_result: Dict[str, Any] = field(default_factory=dict)
    pid: int = 0
    error: str = ""
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible node state."""
        return redact_secrets(asdict(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "NodeState":
        """Build node state from persisted JSON."""
        data = dict(value or {})
        return cls(
            node_id=str(data.get("node_id", "")),
            session_id=str(data.get("session_id", "")),
            status=str(data.get("status", "pending")),
            phase=str(data.get("phase", "initial")),
            run_id=str(data.get("run_id", "")),
            continuation_run_id=str(data.get("continuation_run_id", "")),
            attempts=int(data.get("attempts", 0)),
            reserved_cost_usd=float(data.get("reserved_cost_usd", 0.0)),
            workspace_path=str(data.get("workspace_path", "")),
            patch_path=str(data.get("patch_path", "")),
            handoff_path=str(data.get("handoff_path", "")),
            result=dict(data.get("result", {})),
            continuation_result=dict(data.get("continuation_result", {})),
            pid=int(data.get("pid", 0)),
            error=str(data.get("error", "")),
            started_at=str(data.get("started_at", "")),
            finished_at=str(data.get("finished_at", "")),
        )


@dataclass
class WorkflowState:
    """Atomic graph checkpoint and aggregate resource accounting."""

    workflow_id: str
    status: str = "created"
    base_commit: str = ""
    nodes: Dict[str, NodeState] = field(default_factory=dict)
    spent_cost_usd: float = 0.0
    reserved_cost_usd: float = 0.0
    event_sequence: int = 0
    started_epoch: float = 0.0
    deadline_epoch: float = 0.0
    error: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> Dict[str, Any]:
        """Return a redacted JSON-compatible graph checkpoint."""
        return redact_secrets(
            {
                "schema_version": ORCHESTRATION_SCHEMA_VERSION,
                "workflow_id": self.workflow_id,
                "status": self.status,
                "base_commit": self.base_commit,
                "nodes": {key: value.to_dict() for key, value in self.nodes.items()},
                "spent_cost_usd": self.spent_cost_usd,
                "reserved_cost_usd": self.reserved_cost_usd,
                "event_sequence": self.event_sequence,
                "started_epoch": self.started_epoch,
                "deadline_epoch": self.deadline_epoch,
                "error": self.error,
                "metadata": dict(self.metadata),
                "created_at": self.created_at,
                "updated_at": self.updated_at,
            }
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkflowState":
        """Build graph state from persisted JSON."""
        data = dict(value or {})
        nodes = {
            str(key): item if isinstance(item, NodeState) else NodeState.from_dict(item)
            for key, item in dict(data.get("nodes", {})).items()
        }
        return cls(
            workflow_id=str(data.get("workflow_id", "")),
            status=str(data.get("status", "created")),
            base_commit=str(data.get("base_commit", "")),
            nodes=nodes,
            spent_cost_usd=float(data.get("spent_cost_usd", 0.0)),
            reserved_cost_usd=float(data.get("reserved_cost_usd", 0.0)),
            event_sequence=int(data.get("event_sequence", 0)),
            started_epoch=float(data.get("started_epoch", 0.0)),
            deadline_epoch=float(data.get("deadline_epoch", 0.0)),
            error=str(data.get("error", "")),
            metadata=dict(data.get("metadata", {})),
            created_at=str(data.get("created_at", now_iso())),
            updated_at=str(data.get("updated_at", now_iso())),
        )


class ClaimStore:
    """Durable exact-scope claims for one orchestration graph.

    A claim records its owner, its resources, and a lease. The lease is what
    makes a crashed agent's claims reclaimable: a claim whose owner is neither
    live nor heartbeating within the lease can be reclaimed by
    :meth:`reclaim_stale`, which releases exactly that owner's claims and
    leaves every live claim untouched.
    """

    def __init__(self, root: str | os.PathLike[str], *, lease_s: float = 120.0) -> None:
        self.root = Path(root)
        self.path = self.root / "claims.json"
        self.lease_s = max(1.0, float(lease_s))
        self._lock = threading.RLock()
        self._claims: Dict[str, Dict[str, Any]] = {}
        self._load()

    def acquire(
        self,
        owner: str,
        resources: Iterable[str],
        *,
        claim_id: str = "",
        pid: int = 0,
    ) -> str:
        """Acquire all resources or raise before any child is spawned."""
        owner = validate_path_segment(str(owner), "claim owner")
        normalized = tuple(dict.fromkeys(self._normalize(item) for item in resources))
        if not normalized:
            normalized = (f"worktree:{owner}",)
        with self._lock:
            for resource in normalized:
                for other_owner, claim in self._claims.items():
                    if other_owner == owner or claim.get("released"):
                        continue
                    if any(
                        self._overlap(resource, str(item))
                        for item in claim.get("resources", [])
                    ):
                        raise ClaimConflict(
                            f"resource {resource} is claimed by {other_owner}"
                        )
            selected = claim_id or f"claim-{uuid.uuid4().hex[:12]}"
            self._claims[owner] = {
                "claim_id": selected,
                "owner": owner,
                "resources": list(normalized),
                "acquired_at": now_iso(),
                "heartbeat_epoch": now_epoch(),
                "pid": int(pid or 0),
                "lease_s": self.lease_s,
                "released": False,
            }
            self._save()
            return selected

    def check_available(self, owner: str, resources: Iterable[str]) -> None:
        """Raise :class:`ClaimConflict` if any resource is already claimed.

        The read-only pre-check for :meth:`acquire`: same normalization, same
        overlap test, same skip of released claims, same message, no mutation.
        It exists so a caller can refuse BEFORE paying for the expensive setup
        a claim would have guarded (a worktree, a dependency-patch
        application), instead of creating that setup and then discovering the
        claim is unavailable.

        This can only be an optimization, never a weakening: :meth:`acquire`
        re-runs the identical check under its own lock, so anything this
        admits is still refused if the store changed in between.
        """
        owner = str(owner)
        normalized = tuple(dict.fromkeys(self._normalize(item) for item in resources))
        if not normalized:
            normalized = (f"worktree:{owner}",)
        with self._lock:
            for resource in normalized:
                for other_owner, claim in self._claims.items():
                    if other_owner == owner or claim.get("released"):
                        continue
                    if any(
                        self._overlap(resource, str(item))
                        for item in claim.get("resources", [])
                    ):
                        raise ClaimConflict(
                            f"resource {resource} is claimed by {other_owner}"
                        )

    def renew(self, owner: str, *, pid: Optional[int] = None) -> None:
        """Renew an existing claim without changing its scope."""
        with self._lock:
            claim = self._claims.get(str(owner))
            if claim is not None:
                claim["acquired_at"] = now_iso()
                claim["heartbeat_epoch"] = now_epoch()
                claim["released"] = False
                if pid:
                    claim["pid"] = int(pid)
                self._save()

    def release(self, owner: str) -> None:
        """Release all claims held by one node."""
        with self._lock:
            claim = self._claims.get(str(owner))
            if claim is not None:
                claim["released"] = True
                claim["released_at"] = now_iso()
                self._save()

    def live_owners(self) -> Tuple[str, ...]:
        """Return the owners whose claims are neither released nor expired."""
        with self._lock:
            return tuple(
                owner
                for owner, claim in self._claims.items()
                if not claim.get("released") and not self._expired(claim)
            )

    def expired_owners(self) -> Tuple[str, ...]:
        """Return the owners whose claim lease has expired."""
        with self._lock:
            return tuple(
                owner
                for owner, claim in self._claims.items()
                if not claim.get("released") and self._expired(claim)
            )

    def reclaim_stale(
        self,
        *,
        live_owners: Optional[Iterable[str]] = None,
        dead_owners: Optional[Iterable[str]] = None,
    ) -> Tuple[str, ...]:
        """Release claims held by owners that are provably not running.

        ``dead_owners`` is the authority when the caller knows liveness (a
        crashed child process, a terminated node). ``live_owners`` protects
        owners that are known alive even if their lease has lapsed, so a slow
        agent is never robbed of its scope mid-work. With neither argument the
        lease alone decides.
        """
        alive = {str(owner) for owner in (live_owners or ())}
        dead = {str(owner) for owner in (dead_owners or ())}
        reclaimed: List[str] = []
        with self._lock:
            for owner, claim in self._claims.items():
                if claim.get("released") or owner in alive:
                    continue
                if owner in dead or self._expired(claim):
                    claim["released"] = True
                    claim["released_at"] = now_iso()
                    claim["reclaimed_reason"] = (
                        "owner_dead" if owner in dead else "lease_expired"
                    )
                    reclaimed.append(owner)
            if reclaimed:
                self._save()
        return tuple(reclaimed)

    def list(self) -> Dict[str, Dict[str, Any]]:
        """Return a copy of all claims."""
        with self._lock:
            return {key: dict(value) for key, value in self._claims.items()}

    def _expired(self, claim: Mapping[str, Any]) -> bool:
        lease = float(claim.get("lease_s", self.lease_s) or self.lease_s)
        heartbeat = float(claim.get("heartbeat_epoch", 0.0) or 0.0)
        if heartbeat <= 0:
            return True
        return (now_epoch() - heartbeat) > lease

    @staticmethod
    def _normalize(resource: str) -> str:
        text = str(resource or "").replace("\\", "/").strip()
        if text.startswith("worktree:"):
            validate_path_segment(text.split(":", 1)[1], "worktree claim")
            return text
        if text.startswith("symbol:") and "::" in text:
            path = safe_relative_path(text[len("symbol:") :].split("::", 1)[0])
            name = text.split("::", 1)[1].strip()
            if not name:
                raise ValueError("symbol claim requires a symbol name")
            return f"symbol:{path}::{name}"
        if text.startswith("file:"):
            return "file:" + safe_relative_path(text.split(":", 1)[1])
        return "file:" + safe_relative_path(text)

    @staticmethod
    def _overlap(left: str, right: str) -> bool:
        if left == right:
            return True
        left_file = _claim_file(left)
        right_file = _claim_file(right)
        if left_file is None or right_file is None:
            return False
        left_symbol = left.startswith("symbol:")
        right_symbol = right.startswith("symbol:")
        if left_symbol and right_symbol:
            # Two symbols conflict only when they are the same symbol: sibling
            # symbols in one file are exactly what parallel units edit.
            return left == right
        whole_left = not left_symbol
        whole_right = not right_symbol
        nested = left_file.startswith(right_file + "/") or right_file.startswith(
            left_file + "/"
        )
        if whole_left and whole_right:
            return nested
        if whole_left:
            return left_file == right_file or right_file.startswith(left_file + "/")
        if whole_right:
            return right_file == left_file or left_file.startswith(right_file + "/")
        return False

    def _load(self) -> None:
        data = read_json_or_none(self.path)
        if not isinstance(data, dict):
            return
        for owner, value in dict(data.get("claims", {})).items():
            if isinstance(value, Mapping):
                self._claims[str(owner)] = dict(value)

    def _save(self) -> None:
        atomic_write_json(
            self.path,
            {
                "schema_version": 1,
                "updated_at": now_iso(),
                "claims": self._claims,
            },
        )


class SpawnApprovalGate:
    """Persist pre-spawn approval requests for mutating or expensive nodes."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        expensive_threshold_usd: float = 0.5,
        audit_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.expensive_threshold_usd = max(0.0, float(expensive_threshold_usd))
        self.audit_root = Path(audit_root or self.root.parent)

    def required(self, node: WorkflowNode, *, estimated_cost_usd: float) -> bool:
        """Return whether a node needs approval before any side effect."""
        return bool(
            node.force_approval
            or node.profile.mutates
            or float(estimated_cost_usd) >= self.expensive_threshold_usd
        )

    def request(
        self,
        node: WorkflowNode,
        run_id: str,
        payload: Mapping[str, Any],
        *,
        callback: Optional[Callable[[Mapping[str, Any]], Any]] = None,
        timeout_s: Optional[float] = None,
    ) -> bool:
        """Request, persist, and resolve one spawn approval decision."""
        node_id = validate_path_segment(node.node_id, "approval node id")
        run_segment = validate_path_segment(str(run_id), "approval run id")
        request_path = self.root / f"{node_id}.{run_segment}.request.json"
        decision_path = self.root / f"{node_id}.{run_id}.decision.json"
        request_payload = redact_secrets(
            {
                "schema_version": 1,
                "node_id": node_id,
                "run_id": run_id,
                "role": node.role,
                "payload": dict(payload),
                "requested_at": now_iso(),
            }
        )
        if not request_path.exists():
            atomic_write_json(request_path, request_payload)
        existing = self._decision(decision_path)
        if existing is not None:
            return existing
        decision: bool
        reason = ""
        if callback is not None:
            try:
                raw = callback(request_payload)
                decision, reason = self._normalize_callback(raw)
            except Exception as exc:
                decision = False
                reason = f"approval callback failed: {type(exc).__name__}"
        elif timeout_s is None:
            decision = False
            reason = "no approver or approval timeout configured"
        else:
            deadline = time.time() + float(timeout_s)
            decision = False
            while time.time() < deadline:
                current = self._decision(decision_path)
                if current is not None:
                    return current
                time.sleep(0.1)
            reason = "spawn approval timed out"
        atomic_write_json(
            decision_path,
            {
                "schema_version": 1,
                "node_id": node_id,
                "run_id": run_id,
                "decision": "approve" if decision else "reject",
                "reason": reason,
                "decided_at": now_iso(),
            },
        )
        try:
            append_approval_audit(
                self.audit_root,
                "approved" if decision else "rejected",
                task_id=node_id,
                run_id=run_id,
                tool="spawn",
                target=node.role,
                actor="human",
                scope="once",
                reason=reason,
                metadata={"estimated_cost_usd": payload.get("estimated_cost_usd", 0.0)},
            )
        except (OSError, SecurityViolation):
            pass
        return decision

    def decide(
        self, node_id: str, run_id: str, approve: bool, reason: str = ""
    ) -> None:
        """Write an external decision for a pending spawn request."""
        selected = validate_path_segment(str(node_id), "approval node id")
        run_segment = validate_path_segment(str(run_id), "approval run id")
        atomic_write_json(
            self.root / f"{selected}.{run_segment}.decision.json",
            {
                "schema_version": 1,
                "node_id": selected,
                "run_id": str(run_id),
                "decision": "approve" if approve else "reject",
                "reason": str(reason or ""),
                "decided_at": now_iso(),
            },
        )

    @staticmethod
    def _decision(path: Path) -> Optional[bool]:
        data = read_json_or_none(path)
        if not isinstance(data, dict):
            return None
        value = str(data.get("decision", "")).lower()
        if value == "approve":
            return True
        if value in {"reject", "denied", "expired"}:
            return False
        return None

    @staticmethod
    def _normalize_callback(value: Any) -> tuple[bool, str]:
        if isinstance(value, Mapping):
            raw = value.get("approved", value.get("decision", False))
            reason = str(value.get("reason", ""))
        elif isinstance(value, tuple) and len(value) == 2:
            raw, reason = value[0], str(value[1])
        else:
            raw, reason = value, ""
        if isinstance(raw, str):
            approved = raw.lower() in {"approve", "approved", "allow", "yes", "true"}
        else:
            approved = bool(raw)
        return approved, reason


class Orchestrator:
    """Run a finite workflow DAG with durable child recovery and status."""

    def __init__(
        self,
        spec: WorkflowSpec | Mapping[str, Any],
        logs_root: str | os.PathLike[str] = "logs",
        *,
        resume: bool = False,
        approval_callback: Optional[Callable[[Mapping[str, Any]], Any]] = None,
        child_executor: Optional[Callable[[Mapping[str, Any]], Any]] = None,
        cleanup_worktrees: bool = False,
    ) -> None:
        self.spec = (
            spec if isinstance(spec, WorkflowSpec) else WorkflowSpec.from_dict(spec)
        )
        self.logs_root = Path(logs_root).expanduser().resolve()
        self.root = self.logs_root / "orchestrations" / self.spec.workflow_id
        self.approval_callback = approval_callback
        self.child_executor = child_executor
        self.cleanup_worktrees = bool(cleanup_worktrees)
        self._lock = threading.RLock()
        self._active: Dict[str, Dict[str, Any]] = {}
        self._processes: Dict[str, subprocess.Popen[bytes]] = {}
        self._events_path = self.root / "events.jsonl"
        self._state_path = self.root / "orchestration.json"
        self._merge_lock = threading.Lock()
        self._spec_digest = _digest(self.spec.to_dict())
        self._base_spec_digest = self._spec_digest
        self._coordination_samples: List[float] = []
        self._started_monotonic = time.monotonic()
        if resume:
            if not self._state_path.exists():
                raise OrchestrationError(
                    f"workflow checkpoint does not exist: {self._state_path}"
                )
            self.state = WorkflowState.from_dict(read_json(self._state_path))
            if self.state.workflow_id != self.spec.workflow_id:
                raise OrchestrationError("workflow checkpoint identity mismatch")
            if self.state.metadata.get("spec_digest") != self._spec_digest:
                raise OrchestrationError("workflow definition changed since checkpoint")
            self._reinflate_dynamic_children()
        else:
            if self.root.exists() and any(self.root.iterdir()):
                raise OrchestrationError(
                    f"workflow directory already exists: {self.root}"
                )
            self.root.mkdir(parents=True, exist_ok=True)
            self.state = WorkflowState(
                workflow_id=self.spec.workflow_id,
                nodes={
                    node.node_id: NodeState(
                        node_id=node.node_id,
                        session_id=_session_id(self.spec.workflow_id, node.node_id),
                    )
                    for node in self.spec.nodes
                },
                metadata={"spec_digest": self._spec_digest},
            )
        self.worktrees = WorktreeManager(
            self.spec.repo_path,
            self.root / "worktrees",
            git_timeout_s=self.spec.limits.git_timeout_s,
            max_patch_bytes=self.spec.limits.max_patch_bytes,
        )
        self.claims = ClaimStore(self.root, lease_s=self.spec.limits.claim_lease_s)
        self.approvals = SpawnApprovalGate(
            self.root / "approvals",
            expensive_threshold_usd=self.spec.limits.expensive_approval_threshold_usd,
            audit_root=self.root,
        )
        self.spawn_requests = SpawnRequestStore(
            self.root / "spawn_requests",
            limits=SubagentLimits.from_config(self.spec.config),
        )
        self.subagents = SubagentSpawner(
            self,
            limits=self.spawn_requests.limits,
            repo_path=self.spec.repo_path,
            default_agent=str(self.spec.config.get("subagent_default_agent", "")),
        )
        if not resume:
            try:
                self.state.base_commit = self.worktrees.ensure_clean()
            except Exception as exc:
                self.state.status = "error"
                self.state.error = str(exc)
                self._persist()
                raise
        else:
            self.worktrees.recover()
        self._persist()

    def run(self) -> Dict[str, Any]:
        """Execute the finite graph and return its terminal status projection."""
        if self.state.status in {
            "success",
            "failed",
            "timeout",
            "cancelled",
            "blocked",
            "error",
        }:
            return self.status()
        self.state.status = "running"
        if not self.state.started_epoch:
            self.state.started_epoch = now_epoch()
            self.state.deadline_epoch = (
                self.state.started_epoch + self.spec.limits.max_workflow_wallclock_s
            )
        self._event(
            "workflow_start",
            {"nodes": len(self.spec.nodes), "resume": bool(self.state.started_epoch)},
        )
        if not self._recover_orphans():
            return self.status()
        self._reclaim_stale_claims()
        pool = ThreadPoolExecutor(max_workers=self.spec.limits.max_concurrency)
        futures: Dict[Any, Tuple[str, str]] = {}
        try:
            while True:
                if self._global_timeout():
                    self._event(
                        "workflow_timeout",
                        {"deadline_epoch": self.state.deadline_epoch},
                    )
                    self.state.status = "timeout"
                    self.state.error = "workflow wall-clock limit exceeded"
                    self._finish_terminal()
                    break
                self._prepare_ready_nodes()
                self._drain_spawn_requests()
                while len(futures) < self.spec.limits.max_concurrency:
                    candidate = self._next_candidate()
                    if candidate is None:
                        break
                    node, phase = candidate
                    try:
                        packet = self._prepare_node(node, phase)
                    except Exception as exc:
                        self._fail_node(node, phase, str(exc))
                        continue
                    future = pool.submit(self._execute_node, node, phase, packet)
                    futures[future] = (node.node_id, phase)
                if not futures:
                    self.merge_children()
                    self._settle_terminal()
                    break
                done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                for future in done:
                    node_id, phase = futures.pop(future)
                    node = self.spec.node_map()[node_id]
                    try:
                        outcome = future.result()
                    except Exception as exc:
                        self._fail_node(node, phase, str(exc))
                        continue
                    self._finish_node(node, phase, outcome)
        except KeyboardInterrupt:
            for process in list(self._processes.values()):
                _kill_process(process)
            self.state.status = "cancelled"
            self.state.error = "orchestration interrupted"
            self._finish_terminal()
            raise
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return self.status()

    def status(self) -> Dict[str, Any]:
        """Return a stable, redacted live status projection."""
        with self._lock:
            data = self.state.to_dict()
            data["active"] = [dict(value) for value in self._active.values()]
            data["claims"] = self.claims.list()
            data["worktrees"] = {
                key: value.to_dict() for key, value in self.worktrees.list().items()
            }
            data["ready"] = self._ready_projection()
            return data

    def live_status(self) -> Dict[str, Any]:
        """Return only the live scheduler projection."""
        with self._lock:
            return {
                "workflow_id": self.spec.workflow_id,
                "status": self.state.status,
                "active": [dict(value) for value in self._active.values()],
                "spent_cost_usd": self.state.spent_cost_usd,
                "reserved_cost_usd": self.state.reserved_cost_usd,
                "event_sequence": self.state.event_sequence,
            }

    def event_log(self) -> List[Dict[str, Any]]:
        """Read the append-only orchestration journal."""
        return read_jsonl(self._events_path)

    def handoff(self, node_id: str, phase: str = "final") -> Dict[str, Any]:
        """Read a persisted handoff summary for a node."""
        node = self.state.nodes[validate_path_segment(str(node_id), "node id")]
        if phase == "initial":
            path = self.root / "handoffs" / f"{node.node_id}-initial.json"
        elif node.handoff_path:
            path = Path(node.handoff_path)
        else:
            path = self.root / "handoffs" / f"{node.node_id}-final.json"
        data = read_json_or_none(path)
        if not isinstance(data, dict):
            raise OrchestrationError(f"handoff is unavailable for {node_id}")
        return data

    def active_children(self) -> Dict[str, Dict[str, Any]]:
        """Return the live child projection (node id -> live state)."""
        with self._lock:
            return {key: dict(value) for key, value in self._active.items()}

    def active_child_count(self) -> int:
        """Return how many children are running right now."""
        with self._lock:
            return len(self._active)

    def coordination_report(self) -> Dict[str, Any]:
        """Return measured coordination overhead against the declared budget.

        Coordination time is claim acquisition, worktree creation, packet
        assembly, and serialized merging — the work the orchestrator itself
        adds. It is compared with the workflow's own wall clock so the p95
        target ("coordination overhead under 10% of wall clock") is a measured
        number rather than an assertion.
        """
        with self._lock:
            samples = list(self._coordination_samples)
        wall = max(1e-6, time.monotonic() - self._started_monotonic)
        total = float(sum(samples))
        ordered = sorted(samples)
        p95 = (
            ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
            if ordered
            else 0.0
        )
        budget = self.spec.limits.coordination_budget_fraction
        return {
            "samples": len(samples),
            "total_s": round(total, 6),
            "p95_s": round(p95, 6),
            "wall_s": round(wall, 6),
            "fraction": round(total / wall, 6),
            "p95_fraction": round(p95 / wall, 6),
            "budget_fraction": budget,
            "within_budget": (total / wall) <= budget,
        }

    def spawn_child(
        self,
        request: "SubagentRequest | Mapping[str, Any]",
        *,
        role: Optional[str] = None,
        definition: Optional[AgentDefinition] = None,
    ) -> WorkflowNode:
        """Admit one bounded child into the live DAG.

        This is the single admission point for the ``task`` tool: the spawner
        checks the declared bounds, and this method re-checks the DAG-level
        bounds (node count, depth, fanout) before the node exists, so a
        mis-wired caller cannot grow the graph past its limits.
        """
        selected = (
            request
            if isinstance(request, SubagentRequest)
            else SubagentRequest.from_dict(request)
        )
        chosen_role = str(
            role or (definition.role if definition is not None else "implementer")
        )
        dynamic = [
            node.node_id
            for node in self.spec.nodes
            if str(node.metadata.get("origin", "")) == "subagent"
        ]
        if len(dynamic) >= self.spec.limits.max_dynamic_nodes:
            raise SubagentLimitError(
                f"dynamic child cap reached ({self.spec.limits.max_dynamic_nodes} spawned)"
            )
        if len(self.spec.nodes) >= self.spec.limits.max_nodes:
            raise SubagentLimitError(
                f"workflow node cap reached ({self.spec.limits.max_nodes} nodes)"
            )
        parent_id = selected.parent_node_id
        if parent_id not in self.spec.node_map():
            raise OrchestrationError(f"unknown parent node: {parent_id}")
        depth = self.subagents.node_depth(parent_id) + 1
        if depth > min(
            self.spec.limits.max_depth, self.spawn_requests.limits.max_depth + 1
        ):
            raise SubagentLimitError(
                f"child depth {depth} exceeds the workflow depth cap {self.spec.limits.max_depth}"
            )
        if (
            len(self.spec.children_of(parent_id))
            >= self.spec.limits.max_children_per_parent
        ):
            raise SubagentLimitError(
                f"parent {parent_id} reached the child cap "
                f"({self.spec.limits.max_children_per_parent})"
            )
        node_id = _unique_node_id(self.spec, selected.request_id)
        config: Dict[str, Any] = {}
        if definition is not None:
            config.update(definition.child_config())
        node = WorkflowNode(
            node_id=node_id,
            role=chosen_role,
            request=selected.description,
            parent_id=parent_id,
            depends_on=tuple(selected.depends_on),
            file_scopes=tuple(selected.files),
            config=config,
            estimated_cost_usd=None
            if definition is None
            else (
                definition.max_cost_usd
                or get_role_profile(chosen_role).estimated_cost_usd
            ),
            metadata={
                "origin": "subagent",
                "spawn_request_id": selected.request_id,
                "subagent_depth": depth,
                "subagent_symbols": list(selected.symbols),
                "subagent_version": str(definition.version)
                if definition is not None
                else "",
            },
        )
        candidate = WorkflowSpec(
            workflow_id=self.spec.workflow_id,
            repo_path=self.spec.repo_path,
            nodes=(*self.spec.nodes, node),
            parent_request=self.spec.parent_request,
            limits=self.spec.limits,
            config=dict(self.spec.config),
            metadata=dict(self.spec.metadata),
        ).validate()
        self.spec = candidate
        self.state.nodes[node_id] = NodeState(
            node_id=node_id,
            session_id=_session_id(self.spec.workflow_id, node_id),
        )
        self.state.metadata["dynamic_children"] = [
            *(self.state.metadata.get("dynamic_children") or []),
            node_id,
        ]
        self._spec_digest = _digest(candidate.to_dict())
        self.state.metadata["live_spec"] = candidate.to_dict()
        self._event(
            "child_spawn_admitted",
            {
                "node_id": node_id,
                "parent_node_id": parent_id,
                "request_id": selected.request_id,
                "role": node.role,
                "depth": depth,
                "agent": definition.name if definition is not None else "",
                "agent_version": str(definition.version)
                if definition is not None
                else "",
                "file_scopes": list(node.file_scopes),
            },
        )
        self._persist()
        return node

    def merge_children(self) -> Dict[str, Any]:
        """Serialize every completed child's patch into the integration tree.

        Merges run one at a time under a single lock, in dependency order, so
        two children can never write the integration tree concurrently. Every
        merge is followed by verification, and a patch that edits a symbol no
        claim covers is refused rather than applied.

        A node that has children is not merged: its worktree is the
        accumulation of its children's patches, so re-applying it would
        re-apply their work. Such nodes are reported as ``aggregate`` in the
        merge report instead of being silently skipped.
        """
        existing = dict(self.state.metadata.get("merges") or {})
        if existing or not self.spec.limits.merge_enabled:
            return existing
        ordered = self.spec.topological_nodes()
        candidates = [
            node
            for node in ordered
            if self.state.nodes[node.node_id].patch_path
            and self.state.nodes[node.node_id].status
            in {"completed", "initial_completed"}
        ]
        aggregates = [
            node for node in candidates if self.spec.children_of(node.node_id)
        ]
        candidates = [node for node in candidates if node not in aggregates]
        if not candidates and not aggregates:
            self.state.metadata["merges"] = {}
            return {}
        record = self.worktrees.ensure_integration()
        report: Dict[str, Any] = {}
        for node in aggregates:
            report[node.node_id] = {
                "node_id": node.node_id,
                "patch_path": self.state.nodes[node.node_id].patch_path,
                "applied": False,
                "verified": True,
                "refused": False,
                "aggregate": True,
                "checks": ["aggregate_parent_not_remerged"],
                "unclaimed_edits": [],
                "error": "",
            }
        with self._merge_lock:
            for node in candidates:
                state = self.state.nodes[node.node_id]
                patch_path = str(state.patch_path or "")
                report[node.node_id] = self._merge_one(node, record, patch_path)
                self.state.metadata["merges"] = dict(report)
                self._persist()
        self._event(
            "merge_completed",
            {
                "merges": len(report),
                "applied": sum(1 for item in report.values() if item.get("applied")),
                "verified": sum(1 for item in report.values() if item.get("verified")),
                "refused": sum(1 for item in report.values() if item.get("refused")),
                "aggregate": sum(
                    1 for item in report.values() if item.get("aggregate")
                ),
                "integration_path": record.path,
            },
        )
        return report

    def _merge_one(
        self,
        node: WorkflowNode,
        record: WorktreeRecord,
        patch_path: str,
    ) -> Dict[str, Any]:
        started = time.monotonic()
        entry: Dict[str, Any] = {
            "node_id": node.node_id,
            "patch_path": patch_path,
            "applied": False,
            "verified": False,
            "refused": False,
            "checks": [],
            "unclaimed_edits": [],
            "error": "",
        }
        if not patch_path or not Path(patch_path).is_file():
            entry["error"] = "patch is unavailable"
            entry["verified"] = self._verify_merge(record, entry, node)
            entry["seconds"] = round(time.monotonic() - started, 6)
            self._event(
                "merge_skipped", {"node_id": node.node_id, "error": entry["error"]}
            )
            return entry
        try:
            patch_text = Path(patch_path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            entry["error"] = f"patch unreadable: {exc}"
            entry["verified"] = self._verify_merge(record, entry, node)
            entry["seconds"] = round(time.monotonic() - started, 6)
            return entry
        claims = self._node_claim_resources(node, record)
        violations = unclaimed_symbol_edits(patch_text, claims, root=record.path)
        if violations:
            entry["refused"] = True
            entry["unclaimed_edits"] = violations[:8]
            entry["error"] = "patch edits symbols outside the node's claims"
            entry["verified"] = self._verify_merge(record, entry, node)
            entry["seconds"] = round(time.monotonic() - started, 6)
            self._event(
                "merge_refused",
                {
                    "node_id": node.node_id,
                    "unclaimed_edits": entry["unclaimed_edits"],
                    "claims": list(claims)[:8],
                },
            )
            return entry
        try:
            self.worktrees.apply_patch(record, patch_path, dependency_id=node.node_id)
            entry["applied"] = True
        except WorktreeError as exc:
            entry["error"] = str(exc)
            entry["seconds"] = round(time.monotonic() - started, 6)
            entry["verified"] = self._verify_merge(record, entry, node)
            self._event("merge_failed", {"node_id": node.node_id, "error": str(exc)})
            return entry
        entry["verified"] = self._verify_merge(record, entry, node)
        entry["seconds"] = round(time.monotonic() - started, 6)
        self._event(
            "merge_applied" if entry["applied"] else "merge_skipped",
            {
                "node_id": node.node_id,
                "verified": entry["verified"],
                "checks": entry["checks"][:6],
                "seconds": entry["seconds"],
            },
        )
        return entry

    def _verify_merge(
        self,
        record: WorktreeRecord,
        entry: Dict[str, Any],
        node: Optional[WorkflowNode] = None,
    ) -> bool:
        """Run the post-merge gate for one merge and record what it checked.

        The gate is always run for every merge, including a merge that applied
        nothing, so "verification ran for every merge" is a countable fact
        rather than an intention. The default checks are offline and
        deterministic (patch whitespace, Python syntax of changed files); a
        workflow that declares a test command additionally runs the real
        verifier through :func:`execution.verify.verify`.
        """
        policy = dict((node.verification_policy if node is not None else {}) or {})
        policy.update(dict(self.spec.config.get("merge_verification", {}) or {}))
        changed = list(policy.get("changed_files") or ())
        ok = True
        if not policy.get("skip_whitespace_check", False):
            try:
                self.worktrees.git_check(record)
                entry["checks"].append("git_diff_check")
            except WorktreeError as exc:
                ok = False
                entry["checks"].append(f"git_diff_check:failed:{exc}"[:200])
        for path in changed:
            full = Path(record.path) / path
            if full.suffix.lower() not in {".py", ".pyi"} or not full.is_file():
                continue
            try:
                compile(
                    full.read_text(encoding="utf-8", errors="replace"),
                    str(full),
                    "exec",
                )
                entry["checks"].append(f"compile:{path}")
            except (OSError, SyntaxError, ValueError) as exc:
                ok = False
                entry["checks"].append(f"compile:{path}:failed:{exc}"[:200])
        command = str(policy.get("test_command", "") or "")
        target = str(policy.get("target_test", "") or "")
        if command or target:
            outcome = self._run_merge_verifier(record, command=command, target=target)
            entry["checks"].append(f"verify:{outcome.get('outcome', 'error')}")
            entry["verification"] = outcome
            if outcome.get("outcome") != "passed":
                ok = False
        entry["verified"] = bool(ok)
        return bool(ok)

    def _run_merge_verifier(
        self,
        record: WorktreeRecord,
        *,
        command: str = "",
        target: str = "",
    ) -> Dict[str, Any]:
        """Run the real verifier against the integration tree, degrading honestly."""
        try:
            from execution.verify import verify as run_verify
        except Exception as exc:
            return {"outcome": "unavailable", "error": f"verifier unavailable: {exc}"}
        try:
            result = run_verify(
                record.path,
                target or None,
                rerun_for_flake_check=0,
                test_command=command or None,
                verify_timeout_s=int(self.spec.limits.merge_verify_timeout_s),
            )
        except Exception as exc:
            return {"outcome": "error", "error": str(exc)[:300]}
        return {
            "outcome": "passed"
            if getattr(result, "target_test_passed", False)
            else "failed",
            "target_test_passed": bool(getattr(result, "target_test_passed", False)),
            "regression_passed": bool(getattr(result, "regression_passed", False)),
            "flaky": bool(getattr(result, "flaky", False)),
        }

    def _drain_spawn_requests(self) -> List[Dict[str, Any]]:
        """Admit every queued ``task`` request that passes the declared bounds."""
        drained: List[Dict[str, Any]] = []
        for request in self.spawn_requests.pending():
            decision = self.subagents.admit(request)
            if not decision.admitted:
                self.spawn_requests.resolve(request.request_id, reason=decision.reason)
                self._event(
                    "subagent_spawn_refused",
                    {
                        "request_id": request.request_id,
                        "parent_node_id": request.parent_node_id,
                        "agent": decision.agent,
                        "reason": decision.reason,
                        "limits": decision.limits,
                    },
                )
                drained.append(decision.to_dict())
                continue
            try:
                definition = self.subagents.resolve_definition(request.agent)
                node = self.spawn_child(request, definition=definition)
            except (SubagentLimitError, OrchestrationError, ValueError) as exc:
                self.spawn_requests.resolve(request.request_id, reason=str(exc))
                self._event(
                    "subagent_spawn_refused",
                    {
                        "request_id": request.request_id,
                        "parent_node_id": request.parent_node_id,
                        "reason": str(exc),
                    },
                )
                drained.append(
                    {**decision.to_dict(), "admitted": False, "reason": str(exc)}
                )
                continue
            self.spawn_requests.resolve(request.request_id, node_id=node.node_id)
            drained.append(decision.to_dict())
        return drained

    def _reclaim_stale_claims(self) -> Tuple[str, ...]:
        """Release the claims of children that are provably not running."""
        live: List[str] = []
        dead: List[str] = []
        for node_id, state in self.state.nodes.items():
            if state.status in {
                "running",
                "continuation_running",
                "pending",
                "continuation_pending",
            }:
                live.append(node_id)
            if state.status in _TERMINAL_NODE_STATUSES and not state.pid:
                dead.append(node_id)
        for node_id, process in list(self._processes.items()):
            if process.poll() is None:
                live.append(node_id)
            else:
                dead.append(node_id)
        reclaimed = self.claims.reclaim_stale(live_owners=live, dead_owners=dead)
        if reclaimed:
            self._event(
                "claims_reclaimed",
                {"owners": list(reclaimed), "reason": "owner is not running"},
            )
        return reclaimed

    def _reinflate_dynamic_children(self) -> None:
        """Restore subagent-spawned nodes recorded in the durable checkpoint."""
        live_spec = self.state.metadata.get("live_spec")
        if not isinstance(live_spec, Mapping):
            return
        try:
            restored = WorkflowSpec.from_dict(live_spec)
        except (ValueError, KeyError):
            return
        known = set(self.spec.node_map())
        extra = tuple(node for node in restored.nodes if node.node_id not in known)
        if not extra:
            return
        self.spec = WorkflowSpec(
            workflow_id=self.spec.workflow_id,
            repo_path=self.spec.repo_path,
            nodes=(*self.spec.nodes, *extra),
            parent_request=self.spec.parent_request,
            limits=self.spec.limits,
            config=dict(self.spec.config),
            metadata=dict(self.spec.metadata),
        ).validate()
        self._spec_digest = _digest(self.spec.to_dict())
        for node in extra:
            self.state.nodes.setdefault(
                node.node_id,
                NodeState(
                    node_id=node.node_id,
                    session_id=_session_id(self.spec.workflow_id, node.node_id),
                ),
            )
        self._event(
            "dynamic_children_restored", {"node_ids": [node.node_id for node in extra]}
        )

    def _node_claim_resources(
        self,
        node: WorkflowNode,
        record: Optional[WorktreeRecord] = None,
    ) -> Tuple[str, ...]:
        """Return the claim resources one node holds over its file scope.

        A scope entry is either a whole file (``path``) or one AST symbol
        (``path::symbol``). A whole-file scope is a whole-file claim: the node
        owns the file end to end, including its module-level lines. A symbol
        scope is precise — such a node may not touch a sibling symbol or the
        module level in that file, which is what makes two units able to edit
        different functions of one file in parallel. ``subagent_symbols`` on a
        spawn request narrows the scope the same way.

        ``record`` is accepted for call-site symmetry and is deliberately NOT
        read: the resource set is derived from the node's own declaration, so
        it is identical before and after a worktree exists. That is what lets
        the caller screen a claim conflict *before* creating the worktree (see
        ``ClaimStore.check_available``); a real worktree dependency here would
        silently put that optimization back to doing nothing.
        """
        declared = list(node.file_scopes)
        files: List[str] = []
        symbols: Dict[str, List[str]] = {}
        for scope in declared:
            if "::" in scope:
                path, _, name = scope.partition("::")
                normalized = path.replace("\\", "/").strip()
                if normalized and name.strip():
                    symbols.setdefault(normalized, []).append(name.strip())
                    if normalized not in files:
                        files.append(normalized)
                continue
            files.append(scope)
        requested = tuple(
            str(item).strip()
            for item in node.metadata.get("subagent_symbols", [])
            if str(item).strip()
        )
        if requested and files and not self.spec.limits.symbol_claims:
            requested = ()
        for path in files:
            for name in requested:
                if name not in symbols.setdefault(path, []):
                    symbols[path].append(name)
        whole_only = [path for path in files if path not in symbols]
        if whole_only:
            return build_claim_resources(whole_only, whole_file=True)
        return build_claim_resources(files, symbols=symbols, whole_file=False)

    def _record_coordination(self, seconds: float) -> None:
        """Record one measured coordination interval."""
        with self._lock:
            self._coordination_samples.append(max(0.0, float(seconds)))

    def _prepare_ready_nodes(self) -> None:
        for node in self.spec.nodes:
            state = self.state.nodes[node.node_id]
            if state.status == "pending":
                blocked = [
                    dependency
                    for dependency in node.depends_on
                    if self.state.nodes[dependency].status
                    in {"blocked", "failed", "timeout", "cancelled", "needs_input"}
                ]
                if blocked:
                    self._set_node_terminal(
                        node, "blocked", f"dependencies failed: {', '.join(blocked)}"
                    )
                    continue
                if node.parent_id:
                    parent = self.state.nodes[node.parent_id]
                    if parent.status in {
                        "blocked",
                        "failed",
                        "timeout",
                        "cancelled",
                        "needs_input",
                    }:
                        self._set_node_terminal(
                            node, "blocked", f"parent failed: {node.parent_id}"
                        )
                        continue
                ready = all(
                    self.state.nodes[dependency].status == "completed"
                    for dependency in node.depends_on
                )
                parent_ready = not node.parent_id or self.state.nodes[
                    node.parent_id
                ].status in {"initial_completed", "completed"}
                if ready and parent_ready:
                    continue
            if state.status == "initial_completed":
                children = self.spec.children_of(node.node_id)
                if children and all(
                    self.state.nodes[child].status in _TERMINAL_NODE_STATUSES
                    for child in children
                ):
                    state.status = "continuation_pending"
                    state.phase = "continuation"
                    self._event("parent_continuation_ready", {"node_id": node.node_id})
                    self._persist()

    def _next_candidate(self) -> Optional[Tuple[WorkflowNode, str]]:
        for node in self.spec.nodes:
            state = self.state.nodes[node.node_id]
            if state.status not in {"pending", "continuation_pending"}:
                continue
            if state.status == "pending":
                if any(
                    self.state.nodes[dependency].status != "completed"
                    for dependency in node.depends_on
                ):
                    continue
                if node.parent_id and self.state.nodes[node.parent_id].status not in {
                    "initial_completed",
                    "completed",
                }:
                    continue
                phase = "initial"
            else:
                phase = "continuation"
            estimate = self._estimate(node)
            if (
                self.state.spent_cost_usd + self.state.reserved_cost_usd + estimate
                > self.spec.limits.max_total_cost_usd
            ):
                self._set_node_terminal(
                    node,
                    "blocked",
                    "aggregate cost reservation would exceed max_total_cost_usd",
                )
                continue
            return node, phase
        return None

    def _prepare_node(self, node: WorkflowNode, phase: str) -> Dict[str, Any]:
        state = self.state.nodes[node.node_id]
        coordination_started = time.monotonic()
        estimate = self._estimate(node)
        run_id = state.run_id if phase == "initial" else state.continuation_run_id
        if not run_id:
            run_id = self._new_run_id(node.node_id, phase)
            if phase == "initial":
                state.run_id = run_id
            else:
                state.continuation_run_id = run_id
        state.reserved_cost_usd = estimate
        self.state.reserved_cost_usd += estimate
        state.status = "running" if phase == "initial" else "continuation_running"
        state.phase = phase
        state.started_at = state.started_at or now_iso()
        state.error = ""
        self._event(
            "child_spawn_preparing",
            {
                "node_id": node.node_id,
                "phase": phase,
                "role": node.role,
                "run_id": run_id,
                "estimated_cost_usd": estimate,
            },
        )
        approval_payload = {
            "node_id": node.node_id,
            "role": node.role,
            "phase": phase,
            "estimated_cost_usd": estimate,
            "file_scopes": list(node.file_scopes),
            "depends_on": list(self.spec.dependencies_for(node)),
            "workspace_base_commit": self.state.base_commit,
            "mutates": node.profile.mutates,
        }
        if self.approvals.required(node, estimated_cost_usd=estimate):
            approved = self.approvals.request(
                node,
                run_id,
                approval_payload,
                callback=self.approval_callback,
                timeout_s=self.spec.limits.approval_timeout_s,
            )
            if not approved:
                self._release_node_reservation(node.node_id)
                self._set_node_terminal(
                    node, "blocked", "spawn approval rejected or unavailable"
                )
                raise OrchestrationError("spawn approval rejected")
            self._event(
                "spawn_approved",
                {"node_id": node.node_id, "phase": phase, "run_id": run_id},
            )
        if phase == "initial":
            # Refuse a conflicting scope BEFORE the worktree exists. A node
            # that ends up blocked must leave nothing behind on disk, and the
            # worktree (plus its dependency patches) is by far the most
            # expensive thing this path creates. `acquire` re-checks under its
            # own lock, so this ordering cannot admit a claim that is not
            # actually free.
            resources = [
                *self._node_claim_resources(node),
                f"worktree:{node.node_id}",
            ]
            try:
                self.claims.check_available(node.node_id, resources)
            except ClaimConflict as exc:
                self._release_node_reservation(node.node_id)
                self._set_node_terminal(node, "blocked", str(exc))
                self._record_coordination(time.monotonic() - coordination_started)
                raise
            record = self.worktrees.records.get(node.node_id)
            if record is None:
                patches = self._dependency_patches(node, include_parent_initial=True)
                record = self.worktrees.create(
                    node.node_id,
                    dependency_patches=patches,
                    base_commit=self.state.base_commit,
                )
            elif record.state != "ready":
                raise WorktreeError(
                    f"worktree for {node.node_id} is not ready: {record.state}"
                )
            state.workspace_path = record.path
            self._reclaim_node_claims(node, record)
            try:
                self.claims.acquire(node.node_id, resources, pid=os.getpid())
            except ClaimConflict as exc:
                self._release_node_reservation(node.node_id)
                self._set_node_terminal(node, "blocked", str(exc))
                self._record_coordination(time.monotonic() - coordination_started)
                raise
        else:
            record = self.worktrees.records.get(node.node_id)
            if record is None:
                self._release_node_reservation(node.node_id)
                self._set_node_terminal(
                    node, "blocked", "parent worktree is unavailable"
                )
                raise OrchestrationError("parent worktree is unavailable")
            state.workspace_path = record.path
            self._apply_child_patches(node, record)
            self.claims.renew(node.node_id)
        packet = self._make_packet(node, phase, run_id, record, estimate)
        packet["claims"] = list(self._node_claim_resources(node, record))
        state.pid = 0
        self._persist()
        self._record_coordination(time.monotonic() - coordination_started)
        return packet

    def _reclaim_node_claims(self, node: WorkflowNode, record: WorktreeRecord) -> None:
        """Free this node's own stale claim before it re-acquires its scope.

        A crashed predecessor (or an earlier attempt of this node) can leave a
        claim behind. Reclaiming only this node's own claim keeps the
        re-acquisition honest without touching a live sibling's scope.
        """
        existing = self.claims.list().get(node.node_id)
        if not existing or existing.get("released"):
            return
        pid = int(existing.get("pid", 0) or 0)
        if pid and is_pid_alive(pid) and pid != os.getpid():
            return
        self.claims.reclaim_stale(dead_owners=[node.node_id])
        self._event(
            "claims_reclaimed",
            {"owners": [node.node_id], "reason": "previous owner is not running"},
        )

    def _execute_node(
        self,
        node: WorkflowNode,
        phase: str,
        packet: Dict[str, Any],
    ) -> Dict[str, Any]:
        self._active[node.node_id] = {
            "node_id": node.node_id,
            "phase": phase,
            "status": "starting",
            "pid": 0,
            "attempt": 0,
            "started_at": now_iso(),
        }
        try:
            return self._run_child(node, phase, packet)
        finally:
            with self._lock:
                self._active.pop(node.node_id, None)
                self._processes.pop(node.node_id, None)
                state = self.state.nodes[node.node_id]
                state.pid = 0
                self._persist()

    def _run_child(
        self,
        node: WorkflowNode,
        phase: str,
        packet: Dict[str, Any],
    ) -> Dict[str, Any]:
        attempts = 0
        last_error = ""
        transient_request = (
            node.request if phase == "initial" else self._continuation_request(node)
        )
        child_config = dict(self.spec.config)
        child_config.update(node.config)
        if self.child_executor is not None:
            try:
                executor_packet = dict(packet)
                executor_packet["_transient_request"] = transient_request
                raw = self.child_executor(executor_packet)
                result = _coerce_result(raw)
                attempts = 1
            except KeyboardInterrupt:
                raise
            except BaseException as exc:
                # An injected executor that dies hard (SystemExit, os._exit in a
                # driver, a BaseException) is the same class of event as a child
                # subprocess exiting nonzero: the node fails, the run continues,
                # and the claim is released by the terminal transition.
                result = _failure_result(
                    "failed", f"injected child failure: {exc}", packet
                )
                attempts = 1
            patch_path = self._capture_child_patch(node, phase, packet)
            return {
                "result": result,
                "attempts": attempts,
                "patch_path": patch_path,
                "last_error": last_error,
            }
        while attempts <= self.spec.limits.max_child_retries:
            attempt = attempts
            attempts += 1
            attempt_dir = Path(str(packet["attempt_root"])) / f"attempt-{attempt}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            attempt_packet = dict(packet)
            attempt_packet["attempt"] = attempt
            attempt_packet["attempt_root"] = str(attempt_dir)
            attempt_packet["result_path"] = str(attempt_dir / "result.json")
            attempt_packet["resume"] = bool(
                attempt > 1 or Path(str(packet["checkpoint_path"])).exists()
            )
            attempt_packet["attempt_token"] = uuid.uuid4().hex
            self._write_packet(attempt_packet)
            token = attempt_packet["attempt_token"]
            env = self._child_environment(
                request=transient_request,
                config=child_config,
            )
            log_path = attempt_dir / "worker.log"
            started = time.monotonic()
            try:
                with log_path.open("w", encoding="utf-8") as handle:
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "runtime.orchestration_worker",
                            "--packet",
                            str(attempt_packet["packet_path"]),
                        ],
                        cwd=str(REPO_ROOT),
                        env=env,
                        stdout=handle,
                        stderr=subprocess.STDOUT,
                    )
            except OSError as exc:
                last_error = f"child process launch failed: {exc}"
                process = None
            if process is None:
                if attempts <= self.spec.limits.max_child_retries:
                    self._event(
                        "child_crash",
                        {"node_id": node.node_id, "phase": phase, "error": last_error},
                    )
                    continue
                result = _failure_result("failed", last_error, attempt_packet)
                return {
                    "result": result,
                    "attempts": attempts,
                    "patch_path": "",
                    "last_error": last_error,
                }
            with self._lock:
                self._processes[node.node_id] = process
                self._active[node.node_id].update(
                    {"status": "running", "pid": process.pid, "attempt": attempt}
                )
                self.state.nodes[node.node_id].pid = process.pid
                self._persist()
            self._event(
                "child_spawned",
                {
                    "node_id": node.node_id,
                    "phase": phase,
                    "attempt": attempt,
                    "pid": process.pid,
                    "attempt_token": token,
                },
            )
            timed_out = False
            while process.poll() is None:
                age = time.monotonic() - started
                if age > self.spec.limits.max_child_wallclock_s:
                    timed_out = True
                    _kill_process(process)
                    last_error = "child wall-clock limit exceeded"
                    self._event(
                        "child_timeout",
                        {"node_id": node.node_id, "phase": phase, "attempt": attempt},
                    )
                    break
                if self._global_timeout():
                    timed_out = True
                    _kill_process(process)
                    last_error = "workflow wall-clock limit exceeded"
                    self._event(
                        "child_workflow_timeout",
                        {"node_id": node.node_id, "phase": phase, "attempt": attempt},
                    )
                    break
                time.sleep(0.05)
            exit_code = process.poll()
            with self._lock:
                self._processes.pop(node.node_id, None)
            result_data = read_json_or_none(Path(str(attempt_packet["result_path"])))
            if exit_code == 0 and isinstance(result_data, dict):
                result = _coerce_result(result_data)
                patch_path = self._capture_child_patch(node, phase, attempt_packet)
                return {
                    "result": result,
                    "attempts": attempts,
                    "patch_path": patch_path,
                    "last_error": last_error,
                }
            if timed_out:
                last_error = last_error or "child timed out"
            else:
                last_error = last_error or (
                    f"child exited {exit_code} without a valid result"
                )
            self._event(
                "child_crash",
                {
                    "node_id": node.node_id,
                    "phase": phase,
                    "attempt": attempt,
                    "exit_code": exit_code,
                    "error": last_error,
                },
            )
            if attempts > self.spec.limits.max_child_retries or self._global_timeout():
                result = _failure_result(
                    "timeout" if timed_out else "failed", last_error, attempt_packet
                )
                return {
                    "result": result,
                    "attempts": attempts,
                    "patch_path": "",
                    "last_error": last_error,
                }
        result = _failure_result(
            "failed", last_error or "child retry budget exhausted", packet
        )
        return {
            "result": result,
            "attempts": attempts,
            "patch_path": "",
            "last_error": last_error,
        }

    def _finish_node(
        self,
        node: WorkflowNode,
        phase: str,
        outcome: Mapping[str, Any],
    ) -> None:
        state = self.state.nodes[node.node_id]
        result = outcome.get("result")
        if not isinstance(result, RunResult):
            result = _coerce_result(result)
        if (
            result.status in _SUCCESS_STATUSES
            and not node.profile.accepts_unverified
            and result.status != CompletionStatus.COMPLETED_VERIFIED.value
        ):
            result = _replace_result_status(
                result,
                CompletionStatus.BLOCKED.value,
                "role requires verifier-backed completion",
            )
        patch_path = str(outcome.get("patch_path") or "")
        state.attempts = max(state.attempts, int(outcome.get("attempts", 0)))
        state.patch_path = patch_path
        state.result = result.to_dict() if phase == "initial" else state.result
        state.continuation_result = (
            result.to_dict() if phase == "continuation" else state.continuation_result
        )
        self.state.spent_cost_usd += max(0.0, float(result.cost))
        self._release_node_reservation(node.node_id)
        handoff = HandoffSummary(
            node_id=node.node_id,
            run_id=result.run_id,
            session_id=result.session_id or state.session_id,
            role=node.role,
            status=result.status,
            answer=result.answer,
            changed_files=list(result.changed_files),
            diff=result.diff,
            patch_path=patch_path,
            verification_evidence=list(result.verification_evidence),
            follow_up_needs=list(result.follow_up_needs),
            cost_usd=result.cost,
            attempts=state.attempts,
            trace_path=result.trace_path,
            checkpoint_path=result.checkpoint_path,
            workspace_path=state.workspace_path,
            parent_run_id=str(self._parent_run_id(node)),
            phase=phase,
            error=result.error,
        )
        handoff_path = self.root / "handoffs" / f"{node.node_id}-{phase}.json"
        atomic_write_json(handoff_path, handoff.to_dict())
        state.handoff_path = str(handoff_path)
        state.error = result.error
        state.finished_at = now_iso()
        if result.status in {"completed_verified", "completed_unverified"}:
            if phase == "initial" and self.spec.children_of(node.node_id):
                state.status = "initial_completed"
                state.phase = "initial"
            else:
                state.status = "completed"
                state.phase = phase
                self.claims.release(node.node_id)
        else:
            state.status = _node_status_for_result(result.status)
            state.phase = phase
            self.claims.release(node.node_id)
        self._event(
            "child_finished",
            {
                "node_id": node.node_id,
                "phase": phase,
                "status": result.status,
                "attempts": state.attempts,
                "handoff_path": str(handoff_path),
                "patch_path": patch_path,
            },
        )
        self._persist()

    def _fail_node(self, node: WorkflowNode, phase: str, error: str) -> None:
        if self.state.nodes[node.node_id].status in _TERMINAL_NODE_STATUSES:
            self._event(
                "child_error",
                {"node_id": node.node_id, "phase": phase, "error": error},
            )
            return
        self._release_node_reservation(node.node_id)
        self._set_node_terminal(node, "failed", error)
        self._event(
            "child_error", {"node_id": node.node_id, "phase": phase, "error": error}
        )

    def _set_node_terminal(
        self, node: WorkflowNode, status: str, error: str = ""
    ) -> None:
        state = self.state.nodes[node.node_id]
        self._release_node_reservation(node.node_id)
        state.status = status
        state.phase = state.phase or "initial"
        state.error = str(error or "")
        state.finished_at = now_iso()
        self.claims.release(node.node_id)
        self._persist()

    def _settle_terminal(self) -> None:
        statuses = [state.status for state in self.state.nodes.values()]
        if all(status == "completed" for status in statuses):
            self.state.status = "success"
        elif "timeout" in statuses or self._global_timeout():
            self.state.status = "timeout"
        elif "failed" in statuses:
            self.state.status = "failed"
        elif "blocked" in statuses or "needs_input" in statuses:
            self.state.status = "blocked"
        elif "cancelled" in statuses:
            self.state.status = "cancelled"
        else:
            self.state.status = "error"
            self.state.error = "workflow has no runnable node but is not terminal"
        self._finish_terminal()

    def _finish_terminal(self) -> None:
        if self.cleanup_worktrees:
            for record in list(self.worktrees.list().values()):
                try:
                    self.worktrees.remove(record)
                except Exception as exc:
                    self._event(
                        "worktree_cleanup_blocked",
                        {"node_id": record.node_id, "error": str(exc)},
                    )
        self._event("workflow_finished", {"status": self.state.status})
        self._persist()

    def _recover_orphans(self) -> bool:
        recovered = False
        for node in self.spec.nodes:
            state = self.state.nodes[node.node_id]
            if state.status not in {"running", "continuation_running"}:
                continue
            checkpoint = read_json_or_none(
                self.root / "children" / node.node_id / "child_checkpoint.json"
            )
            pid = int(checkpoint.get("pid", 0)) if isinstance(checkpoint, dict) else 0
            if pid and is_pid_alive(pid):
                self.state.status = "blocked"
                self.state.error = f"child process {pid} is still alive"
                self._event(
                    "orphan_child_detected", {"node_id": node.node_id, "pid": pid}
                )
                self._persist()
                return False
            state.status = (
                "pending" if state.phase == "initial" else "continuation_pending"
            )
            state.pid = 0
            recovered = True
            self._event(
                "orphan_child_recovered",
                {"node_id": node.node_id, "phase": state.phase},
            )
        if recovered:
            self._persist()
        return True

    def _make_packet(
        self,
        node: WorkflowNode,
        phase: str,
        run_id: str,
        record: WorktreeRecord,
        estimate: float,
    ) -> Dict[str, Any]:
        state = self.state.nodes[node.node_id]
        config = dict(self.spec.config)
        config.update(node.config)
        config["orchestration_id"] = self.spec.workflow_id
        config["orchestration_node_id"] = node.node_id
        config["orchestration_phase"] = phase
        # A child's `task` calls are recorded here and admitted by this
        # orchestrator at the next tool boundary — the only cross-process seam,
        # and the reason a child can never grow the graph on its own.
        config["orchestration_spawn_dir"] = str(self.root / "spawn_requests")
        config.setdefault("max_wallclock_s", self.spec.limits.max_child_wallclock_s)
        config.setdefault("max_step_turns", 12)
        config.setdefault("agent_approval", "auto")
        config.setdefault("safe_tool_backend", True)
        child_root = self.root / "children" / node.node_id
        attempt_root = child_root / "attempts"
        checkpoint_path = child_root / "child_checkpoint.json"
        packet = {
            "schema_version": ORCHESTRATION_SCHEMA_VERSION,
            "orchestration_id": self.spec.workflow_id,
            "node_id": node.node_id,
            "role": node.role,
            "phase": phase,
            "run_id": run_id,
            "session_id": state.session_id,
            "parent_run_id": self._parent_run_id(node),
            "workspace_path": record.path,
            "kernel_log_root": str(child_root),
            "ledger_dir": str(child_root / "runtime"),
            "checkpoint_path": str(checkpoint_path),
            "result_path": "",
            "attempt_root": str(attempt_root),
            "packet_path": "",
            "request": "",
            "config": redact_sensitive_config(config),
            "workspace_policy": dict(node.workspace_policy),
            "verification_policy": dict(node.verification_policy),
            "estimated_cost_usd": estimate,
            "resume": False,
            "attempt": 0,
            "attempt_token": "",
            "fault": str(config.get("_orchestration_fault", "")),
        }
        return packet

    def _write_packet(self, packet: Dict[str, Any]) -> None:
        attempt_root = Path(str(packet["attempt_root"]))
        attempt_root.mkdir(parents=True, exist_ok=True)
        packet_path = attempt_root / "packet.json"
        packet["packet_path"] = str(packet_path)
        atomic_write_json(packet_path, dict(packet))

    def _child_environment(
        self,
        *,
        request: str,
        config: Mapping[str, Any],
    ) -> Dict[str, str]:
        entries = extract_sensitive_config(dict(config))
        transient = json.dumps(
            {"request": str(request), "secrets": entries},
            separators=(",", ":"),
        )
        home = self.root / "home"
        home.mkdir(parents=True, exist_ok=True)
        from shared.security import scrub_environment

        return scrub_environment(
            os.environ,
            extra={ORCHESTRATION_SECRETS_ENV: transient},
            allow=(ORCHESTRATION_SECRETS_ENV, "NEO_TRACE_DIR"),
            home=home,
        )

    def _capture_child_patch(
        self,
        node: WorkflowNode,
        phase: str,
        packet: Mapping[str, Any],
    ) -> str:
        try:
            record = self.worktrees.records[node.node_id]
            return self.worktrees.capture_patch(
                record,
                patch_path=self.worktrees.root
                / "patches"
                / f"{node.node_id}-{phase}.patch",
            )
        except (WorktreeError, OSError) as exc:
            self._event(
                "patch_capture_failed",
                {"node_id": node.node_id, "phase": phase, "error": str(exc)},
            )
            return ""

    def _dependency_patches(
        self,
        node: WorkflowNode,
        *,
        include_parent_initial: bool,
    ) -> List[Tuple[str, str]]:
        selected: List[Tuple[str, str]] = []
        for dependency in self.spec.topological_order():
            if dependency == node.node_id:
                continue
            if dependency not in self.spec.dependencies_for(node):
                continue
            state = self.state.nodes[dependency]
            if (
                state.status == "initial_completed" and include_parent_initial
            ) or state.status == "completed":
                path = state.patch_path
            else:
                continue
            if path and Path(path).is_file():
                selected.append((dependency, path))
        return selected

    def _apply_child_patches(self, node: WorkflowNode, record: WorktreeRecord) -> None:
        for child in self.spec.children_of(node.node_id):
            state = self.state.nodes[child]
            if (
                state.status in _TERMINAL_NODE_STATUSES
                and state.patch_path
                and state.patch_path not in record.dependency_patches
                and Path(state.patch_path).is_file()
            ):
                self.worktrees.apply_patch(
                    record, state.patch_path, dependency_id=child
                )

    def _continuation_request(self, node: WorkflowNode) -> str:
        handoffs = []
        for child in self.spec.children_of(node.node_id):
            state = self.state.nodes[child]
            if state.handoff_path and Path(state.handoff_path).is_file():
                value = read_json_or_none(state.handoff_path)
                if isinstance(value, dict):
                    handoffs.append(value)
        return (
            "Continue the parent workflow after all direct children reached a terminal state. "
            "Review the structured child handoffs below, reconcile their evidence, and finish "
            "with a concise parent summary. Do not spawn another agent or modify VCS metadata.\n\n"
            + json.dumps({"children": handoffs}, ensure_ascii=False, sort_keys=True)
        )

    def _parent_run_id(self, node: WorkflowNode) -> str:
        if not node.parent_id:
            return ""
        parent = self.state.nodes[node.parent_id]
        return parent.run_id or parent.continuation_run_id

    def _estimate(self, node: WorkflowNode) -> float:
        return float(
            node.estimated_cost_usd
            if node.estimated_cost_usd is not None
            else node.profile.estimated_cost_usd
        )

    def _new_run_id(self, node_id: str, phase: str) -> str:
        return _safe_id(f"{node_id}-{phase}-{uuid.uuid4().hex[:12]}")

    def _release_node_reservation(self, node_id: str) -> None:
        state = self.state.nodes[node_id]
        amount = max(0.0, state.reserved_cost_usd)
        if amount:
            self.state.reserved_cost_usd = max(
                0.0, self.state.reserved_cost_usd - amount
            )
            state.reserved_cost_usd = 0.0

    def _global_timeout(self) -> bool:
        return bool(
            self.state.deadline_epoch and now_epoch() >= self.state.deadline_epoch
        )

    def _ready_projection(self) -> List[Dict[str, Any]]:
        ready: List[Dict[str, Any]] = []
        for node in self.spec.nodes:
            state = self.state.nodes[node.node_id]
            if state.status == "pending":
                deps_ready = all(
                    self.state.nodes[dependency].status == "completed"
                    for dependency in node.depends_on
                )
                parent_ready = not node.parent_id or self.state.nodes[
                    node.parent_id
                ].status in {
                    "initial_completed",
                    "completed",
                }
                if deps_ready and parent_ready:
                    ready.append({"node_id": node.node_id, "phase": "initial"})
            elif state.status == "continuation_pending":
                ready.append({"node_id": node.node_id, "phase": "continuation"})
        return ready

    def _event(self, event: str, data: Mapping[str, Any]) -> None:
        with self._lock:
            self.state.event_sequence += 1
            safe_data = redact_secrets(dict(data))
            append_jsonl(
                self._events_path,
                {
                    "schema_version": ORCHESTRATION_SCHEMA_VERSION,
                    "sequence": self.state.event_sequence,
                    "ts": now_iso(),
                    "event": event,
                    "workflow_id": self.spec.workflow_id,
                    "data": safe_data,
                },
            )
            self._persist()
            try:
                tracing.emit(
                    "runtime",
                    event,
                    task_id=str(safe_data.get("node_id") or self.spec.workflow_id),
                    run_id=self.spec.workflow_id,
                    **{
                        key: value
                        for key, value in safe_data.items()
                        if key != "node_id"
                    },
                )
            except Exception:
                pass

    def _persist(self) -> None:
        self.state.updated_at = now_iso()
        atomic_write_json(self._state_path, self.state.to_dict())


def decompose_tasks(
    workflow_id: str,
    repo_path: str,
    parent_request: str,
    subtasks: Sequence[Mapping[str, Any] | WorkflowNode],
    *,
    limits: Optional[WorkflowLimits] = None,
    config: Optional[Mapping[str, Any]] = None,
) -> WorkflowSpec:
    """Build a finite planner-rooted DAG from explicit task descriptions."""
    root_id = "root"
    nodes: List[WorkflowNode] = [
        WorkflowNode(
            node_id=root_id,
            role="planner",
            request=str(parent_request or "Plan the bounded workflow."),
        )
    ]
    for index, item in enumerate(subtasks, start=1):
        if isinstance(item, WorkflowNode):
            node = item
            if not node.parent_id:
                node = WorkflowNode.from_dict({**node.to_dict(), "parent_id": root_id})
        else:
            data = dict(item)
            node_id = str(data.get("node_id", data.get("id", f"task-{index}")))
            node = WorkflowNode.from_dict(
                {
                    **data,
                    "node_id": node_id,
                    "role": data.get("role", "implementer"),
                    "request": data.get("request", data.get("description", "")),
                    "parent_id": data.get("parent_id", root_id),
                }
            )
        nodes.append(node)
    return WorkflowSpec(
        workflow_id=workflow_id,
        repo_path=repo_path,
        nodes=tuple(nodes),
        parent_request=str(parent_request or ""),
        limits=limits or WorkflowLimits(),
        config=dict(config or {}),
    )


def _coerce_result(value: Any) -> RunResult:
    if isinstance(value, RunResult):
        return value
    if isinstance(value, Mapping):
        return RunResult.from_dict(dict(value))
    raise OrchestrationError("child executor returned a non-result value")


def _failure_result(status: str, error: str, packet: Mapping[str, Any]) -> RunResult:
    return RunResult(
        status=status,
        run_id=str(packet.get("run_id", "")),
        session_id=str(packet.get("session_id", "")),
        trace_path=str(packet.get("trace_path", "")),
        checkpoint_path=str(packet.get("checkpoint_path", "")),
        error=str(error or status),
        follow_up_needs=[str(error or status)] if error else [],
    )


def _replace_result_status(result: RunResult, status: str, error: str) -> RunResult:
    data = result.to_dict()
    data["status"] = status
    data["error"] = str(error or result.error)
    return RunResult.from_dict(data)


def _node_status_for_result(status: str) -> str:
    if status in {"completed_verified", "completed_unverified"}:
        return "completed"
    if status in {"needs_input", "blocked", "cancelled", "timeout"}:
        return status
    return "failed"


def _unique_node_id(spec: "WorkflowSpec", request_id: str) -> str:
    """Return a collision-free node id derived from a spawn request id."""
    base = validate_path_segment(str(request_id or "spawn"), "spawn request id")
    base = f"sub-{base}"[:60]
    taken = set(spec.node_map())
    if base not in taken:
        return base
    for index in range(2, 1000):
        candidate = f"{base}-{index}"
        if candidate not in taken:
            return candidate
    raise SubagentLimitError("cannot allocate a unique child node id")


def _claim_file(resource: str) -> Optional[str]:
    """Return the repo-relative path a claim resource covers, if any."""
    text = str(resource or "")
    if text.startswith("file:"):
        return safe_relative_path(text[5:])
    if text.startswith("symbol:") and "::" in text:
        return safe_relative_path(text[len("symbol:") :].split("::", 1)[0])
    return None


def _session_id(workflow_id: str, node_id: str) -> str:
    return _safe_id(f"session-{workflow_id}-{node_id}")


def _safe_id(value: str) -> str:
    text = str(value)
    if len(text) <= 220 and all(char not in text for char in '/\\:*?"<>|'):
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"id-{digest}"


def _digest(value: Any) -> str:
    encoded = json.dumps(
        redact_secrets(value), sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _kill_process(process: subprocess.Popen[Any]) -> bool:
    if process.poll() is not None:
        return True
    try:
        process.kill()
    except ProcessLookupError:
        return True
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        return False
    return True


__all__ = [
    "ClaimConflict",
    "ClaimStore",
    "HandoffSummary",
    "NodeState",
    "OrchestrationError",
    "Orchestrator",
    "SpawnApprovalGate",
    "WorkflowLimits",
    "WorkflowNode",
    "WorkflowSpec",
    "WorkflowState",
    "decompose_tasks",
]
