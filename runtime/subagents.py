"""Versioned subagent definitions and the bounded ``task`` tool surface.

A subagent is a *definition* (versioned Markdown or TOML: role, model tier,
restricted tools, permissions, child budget) plus a *spawn* that is admitted
into the existing runtime DAG. This module never runs an agent itself: it
validates, budgets, admits, and reports. Execution stays with
:class:`runtime.orchestration.Orchestrator`, so there is exactly one
orchestrator in the product.

The bounds are structural, not advisory:

* a child definition may not name a tool its role profile does not expose, and
  no definition may name the ``task`` tool itself;
* admission refuses a spawn past the depth, fanout, node, concurrency, or cost
  limit, and the refusal is returned to the model as a bounded result;
* the summary handed back to a parent is capped, so parent context growth per
  child is bounded by construction.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from runtime.fsutil import atomic_write_json, now_iso, read_json_or_none
from runtime.paths import validate_path_segment
from runtime.roles import ROLE_ALIASES, get_role_profile
from shared.security import redact_secrets

AGENT_SCHEMA_VERSION = 1
MODEL_TIERS = ("cheap", "medium", "expensive")
DEFAULT_SUMMARY_MAX_CHARS = 2048
MAX_SUMMARY_FILES = 8
MAX_SUMMARY_FOLLOW_UPS = 4
_SEMVER = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?$")
_FRONTMATTER = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---\s*\r?\n?", re.DOTALL)
_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv", "logs"}


class AgentDefinitionError(ValueError):
    """Raised when an agent definition violates the subagent contract."""


class SubagentLimitError(RuntimeError):
    """Raised when a spawn exceeds a declared subagent bound."""


def clamp_max_children(value: Any, *, ceiling: int = 8) -> int:
    """Clamp a declared child budget into ``[0, ceiling]``."""
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise AgentDefinitionError(f"invalid max_children: {value!r}") from exc
    if number < 0:
        raise AgentDefinitionError("max_children cannot be negative")
    return min(number, ceiling)


@dataclass(frozen=True)
class SubagentLimits:
    """Bounds every subagent spawn is admitted against."""

    max_depth: int = 2
    max_children_per_parent: int = 4
    max_concurrent_children: int = 4
    max_spawn_requests_per_run: int = 16
    max_child_turns: int = 12
    max_child_cost_usd: float = 2.0
    max_total_cost_usd: float = 10.0
    summary_max_chars: int = DEFAULT_SUMMARY_MAX_CHARS

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_depth", max(1, min(3, int(self.max_depth))))
        for name in (
            "max_children_per_parent",
            "max_concurrent_children",
            "max_spawn_requests_per_run",
            "max_child_turns",
        ):
            object.__setattr__(self, name, max(1, int(getattr(self, name))))
        for name in ("max_child_cost_usd", "max_total_cost_usd"):
            value = float(getattr(self, name))
            if value <= 0:
                raise SubagentLimitError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        cap = int(self.summary_max_chars)
        if cap < 256:
            raise SubagentLimitError("summary_max_chars must be at least 256")
        object.__setattr__(self, "summary_max_chars", cap)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible limit set."""
        return asdict(self)

    @classmethod
    def from_config(cls, config: Optional[Mapping[str, Any]]) -> "SubagentLimits":
        """Build limits from a ``Task.config``-style mapping."""
        data = dict(config or {})
        fields = {
            name: data[key]
            for name, key in (
                ("max_depth", "subagent_max_depth"),
                ("max_children_per_parent", "subagent_max_children_per_parent"),
                ("max_concurrent_children", "subagent_max_concurrent"),
                ("max_spawn_requests_per_run", "subagent_max_spawn_requests"),
                ("max_child_turns", "subagent_max_child_turns"),
                ("max_child_cost_usd", "subagent_max_child_cost_usd"),
                ("max_total_cost_usd", "subagent_max_total_cost_usd"),
                ("summary_max_chars", "subagent_summary_max_chars"),
            )
            if key in data
        }
        return cls(**fields)


@dataclass
class AgentDefinition:
    """One versioned subagent definition loaded from Markdown or TOML."""

    name: str
    role: str
    description: str = ""
    instructions: str = ""
    version: str = "1.0.0"
    model_tier: str = ""
    tools: Tuple[str, ...] = ()
    permissions: Tuple[Dict[str, Any], ...] = ()
    max_children: int = 0
    #: A definition's TURN BUDGET. ``0`` means "no definition-level budget",
    #: which every surface reports as the session default rather than as a
    #: number - because reporting ``0`` would read as "this agent may not take
    #: a turn", which is a claim nobody made.
    max_turns: int = 0
    max_cost_usd: float = 0.0
    config: Dict[str, Any] = field(default_factory=dict)
    source_path: str = ""
    schema_version: int = AGENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.name = validate_path_segment(str(self.name or ""), "agent definition name")
        # `max-turns` and `max_turns` are both spellings an author reaches
        # for. Only the canonical underscore form was read, so a definition
        # written with the frontmatter spelling got a SILENT zero - and a
        # zero turn budget reads as "the session default" on every surface,
        # which is a budget nobody set.
        if not self.max_turns and isinstance(self.config, Mapping):
            for alias in ("max-turns", "maxturns", "max_turns"):
                value = self.config.get(alias)
                if value in (None, ""):
                    continue
                try:
                    self.max_turns = max(0, int(value))
                except (TypeError, ValueError):
                    raise AgentDefinitionError(
                        f"agent {self.name} declares an unusable max-turns: {value!r}"
                    ) from None
                break
        role = str(self.role or "").strip().lower()
        self.role = ROLE_ALIASES.get(role, role)
        # Raises ValueError for an unknown role: a definition may not invent one.
        self.role = get_role_profile(self.role).name
        version = str(self.version or "").strip()
        match = _SEMVER.match(version)
        if match is None:
            raise AgentDefinitionError(
                f"agent {self.name} needs a semantic version, got {self.version!r}"
            )
        if int(match.group(1)) != AGENT_SCHEMA_VERSION:
            raise AgentDefinitionError(
                f"agent {self.name} declares unsupported major version {version}"
            )
        self.version = version
        tier = str(self.model_tier or "").strip().lower()
        if tier and tier not in MODEL_TIERS:
            raise AgentDefinitionError(
                f"agent {self.name} declares unknown model tier {self.model_tier!r}"
            )
        self.model_tier = tier
        profile = get_role_profile(self.role)
        requested = tuple(
            dict.fromkeys(
                str(item).strip().lower() for item in self.tools if str(item).strip()
            )
        )
        if "task" in requested:
            raise AgentDefinitionError(
                f"agent {self.name} may not request the task tool; recursion is bounded "
                "by the spawner, never by a definition"
            )
        unknown = sorted(set(requested) - set(profile.visible_tools))
        if unknown:
            raise AgentDefinitionError(
                f"agent {self.name} requests tools its role does not expose: {', '.join(unknown)}"
            )
        self.tools = requested or tuple(profile.visible_tools)
        self.permissions = tuple(
            dict(item) for item in self.permissions if isinstance(item, Mapping)
        )
        self.max_children = clamp_max_children(self.max_children)
        self.max_turns = max(0, int(self.max_turns))
        self.max_cost_usd = max(0.0, float(self.max_cost_usd))
        self.config = dict(self.config or {})
        self.description = str(self.description or "")
        self.instructions = str(self.instructions or "")
        self.source_path = str(self.source_path or "")

    @property
    def can_spawn(self) -> bool:
        """Return whether this definition may create further children."""
        return self.max_children > 0

    def child_config(self) -> Dict[str, Any]:
        """Return the ``Task.config`` fragment this definition pins on a child."""
        config: Dict[str, Any] = {
            "subagent_definition": self.name,
            "subagent_version": self.version,
            "subagent_role": self.role,
            "subagent_tools": list(self.tools),
        }
        if self.model_tier:
            config["subagent_model_tier"] = self.model_tier
        if self.max_turns:
            config["max_step_turns"] = self.max_turns
        if self.max_cost_usd:
            config["subagent_max_cost_usd"] = self.max_cost_usd
        for key, value in self.config.items():
            config[str(key)] = value
        return config

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible, redacted definition."""
        return redact_secrets(
            {**asdict(self), "permissions": [dict(item) for item in self.permissions]}
        )

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, source_path: str = ""
    ) -> "AgentDefinition":
        """Build a definition from a parsed mapping (Markdown frontmatter or TOML)."""
        data = dict(value or {})
        tools = data.get("tools", ())
        if isinstance(tools, str):
            tools = [item for item in re.split(r"[,\s]+", tools) if item]
        permissions = data.get("permissions", ())
        if isinstance(permissions, Mapping):
            permissions = [
                {"action": key, "tool": item} if isinstance(item, str) else dict(item)
                for key, item in permissions.items()
            ]
        return cls(
            name=str(data.get("name", "")),
            role=str(data.get("role", "implementer")),
            description=str(data.get("description", "")),
            instructions=str(data.get("instructions", "")),
            version=str(data.get("version", "1.0.0")),
            model_tier=str(data.get("model_tier", data.get("tier", ""))),
            tools=tuple(tools or ()),
            permissions=tuple(permissions or ()),
            max_children=int(data.get("max_children", 0) or 0),
            # Both spellings are accepted here rather than in one place only,
            # so a TOML definition (`max_turns`) and a markdown frontmatter
            # (`max-turns`) that mean the same thing get the same budget.
            # `__post_init__` carries the config aliases too, for a key
            # nested under `config:`.
            max_turns=int(data.get("max_turns", data.get("max-turns", 0)) or 0),
            max_cost_usd=float(data.get("max_cost_usd", 0.0) or 0.0),
            config=dict(data.get("config", {}) or {}),
            source_path=str(data.get("source_path", source_path or "")),
        )


def default_agent_roots(repo_path: str | Path | None = None) -> Tuple[Path, ...]:
    """Return the standard definition search roots, project first."""
    roots: List[Path] = []
    if repo_path:
        roots.append(Path(repo_path) / ".neo" / "agents")
    roots.append(Path.home() / ".config" / "neo" / "agents")
    return tuple(roots)


class AgentRegistry:
    """Loaded agent definitions with project-shadows-global precedence."""

    def __init__(self, definitions: Optional[Iterable[AgentDefinition]] = None) -> None:
        self._definitions: Dict[str, AgentDefinition] = {}
        for definition in definitions or ():
            self._definitions[definition.name] = definition

    def __contains__(self, name: object) -> bool:
        return str(name) in self._definitions

    def names(self) -> Tuple[str, ...]:
        """Return every registered definition name in load order."""
        return tuple(self._definitions)

    def get(self, name: str) -> AgentDefinition:
        """Return one definition by name or fail closed."""
        key = str(name or "").strip()
        try:
            return self._definitions[key]
        except KeyError as exc:
            raise AgentDefinitionError(f"unknown subagent: {name}") from exc

    def register(self, definition: AgentDefinition) -> AgentDefinition:
        """Add or replace one definition."""
        self._definitions[definition.name] = definition
        return definition

    def to_dict(self) -> Dict[str, Any]:
        """Return every definition as a JSON-compatible mapping."""
        return {name: value.to_dict() for name, value in self._definitions.items()}

    @classmethod
    def load(
        cls,
        repo_path: str | Path | None = None,
        roots: Optional[Sequence[str | Path]] = None,
    ) -> "AgentRegistry":
        """Load definitions from every root, tolerating unreadable files.

        A malformed file is reported through :attr:`diagnostics` rather than
        failing the load: one broken definition must not disable every other
        agent, and the diagnostic is what the caller surfaces.
        """
        registry = cls()
        diagnostics: List[Dict[str, str]] = []
        search = (
            [Path(item) for item in roots]
            if roots is not None
            else list(default_agent_roots(repo_path))
        )
        for root in search:
            for path in sorted(_iter_definition_files(root)):
                try:
                    data = parse_agent_file(path)
                except AgentDefinitionError as exc:
                    diagnostics.append({"path": str(path), "error": str(exc)})
                    continue
                try:
                    registry.register(
                        AgentDefinition.from_dict(data, source_path=str(path))
                    )
                except (AgentDefinitionError, ValueError) as exc:
                    diagnostics.append({"path": str(path), "error": str(exc)})
        registry.diagnostics = diagnostics  # type: ignore[attr-defined]
        return registry


def parse_agent_file(path: str | Path) -> Dict[str, Any]:
    """Parse one versioned agent definition file into a mapping."""
    target = Path(path)
    text = target.read_text(encoding="utf-8", errors="replace")
    if target.suffix.lower() == ".toml":
        return _parse_toml(text, target)
    return _parse_markdown(text, target)


def _iter_definition_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return []
    found: List[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".md", ".toml"}:
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.name.lower() in {"readme.md", "agents.md"}:
            continue
        found.append(path)
    return found


def _parse_markdown(text: str, path: Path) -> Dict[str, Any]:
    match = _FRONTMATTER.match(text)
    if match is None:
        raise AgentDefinitionError(f"{path} has no YAML frontmatter header")
    data = _parse_frontmatter(match.group(1))
    if "name" not in data and path.stem:
        data["name"] = path.stem
    data.setdefault("source_path", str(path))
    data["instructions"] = text[match.end() :].strip()
    return data


def _parse_frontmatter(block: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    key = ""
    for raw in block.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[:1].isspace() and key:
            data[key] = f"{data.get(key, '')} {raw.strip()}".strip()
            continue
        if ":" not in raw:
            raise AgentDefinitionError(f"frontmatter line is not key: value: {raw!r}")
        key, _, value = raw.partition(":")
        key = key.strip()
        data[key] = _coerce_frontmatter_value(value.strip())
    # `model-tier` is the frontmatter spelling an author reaches for and
    # `max-turns` is the matching one for a budget. Both were read only in
    # their underscore form, so a definition written the idiomatic way got a
    # SILENT default - a "medium" tier and a zero turn budget, which every
    # surface then rendered as "the session default", a value nobody chose.
    for dashed, underscored in (
        ("model-tier", "model_tier"),
        ("max-turns", "max_turns"),
        ("max-children", "max_children"),
        ("max-cost-usd", "max_cost_usd"),
    ):
        if dashed in data and underscored not in data:
            data[underscored] = data[dashed]
    return data


def _coerce_frontmatter_value(value: str) -> Any:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [
            _coerce_frontmatter_value(item.strip())
            for item in inner.split(",")
            if item.strip()
        ]
    lowered = text.lower()
    if lowered in {"true", "yes"}:
        return True
    if lowered in {"false", "no"}:
        return False
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return text


def _parse_toml(text: str, path: Path) -> Dict[str, Any]:
    try:
        import tomllib as toml_reader  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover - exercised on 3.10 via tomli
        try:
            import tomli as toml_reader  # type: ignore[no-redef]
        except ModuleNotFoundError as exc:
            raise AgentDefinitionError(
                f"reading {path} needs tomli on this interpreter; use Markdown instead"
            ) from exc
    try:
        data = dict(toml_reader.loads(text))
    except Exception as exc:
        raise AgentDefinitionError(f"{path} is not valid TOML: {exc}") from exc
    table = data.get("agent")
    if isinstance(table, Mapping):
        merged = {key: value for key, value in data.items() if key != "agent"}
        merged.update(dict(table))
        data = merged
    if "name" not in data and path.stem:
        data["name"] = path.stem
    data.setdefault("source_path", str(path))
    return data


@dataclass
class SubagentRequest:
    """One bounded request to create a child, as recorded in the queue."""

    request_id: str
    description: str
    parent_node_id: str
    agent: str = ""
    files: Tuple[str, ...] = ()
    symbols: Tuple[str, ...] = ()
    depends_on: Tuple[str, ...] = ()
    requested_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        self.request_id = validate_path_segment(
            str(self.request_id), "spawn request id"
        )
        self.description = " ".join(str(self.description or "").split())
        if not self.description:
            raise SubagentLimitError("a spawn request needs a description")
        self.parent_node_id = validate_path_segment(
            str(self.parent_node_id or "root"), "spawn parent node id"
        )
        self.files = tuple(
            dict.fromkeys(
                str(item).replace("\\", "/").strip()
                for item in self.files
                if str(item).strip()
            )
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

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible request."""
        return redact_secrets(asdict(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SubagentRequest":
        """Rebuild a request from persisted JSON."""
        data = dict(value or {})
        return cls(
            request_id=str(data.get("request_id", "")),
            description=str(data.get("description", "")),
            parent_node_id=str(data.get("parent_node_id", "root")),
            agent=str(data.get("agent", "")),
            files=tuple(data.get("files", ())),
            symbols=tuple(data.get("symbols", ())),
            depends_on=tuple(data.get("depends_on", ())),
            requested_at=str(data.get("requested_at", now_iso())),
        )


class SpawnRequestStore:
    """Durable cross-process queue of bounded spawn requests.

    A child agent runs in its own process, so a ``task`` call cannot mutate the
    parent's in-memory graph. It records a request here; the orchestrator
    admits it at the next tool boundary. The store is the only cross-process
    seam, and it is bounded by :class:`SubagentLimits`.
    """

    def __init__(
        self, root: str | Path, *, limits: Optional[SubagentLimits] = None
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.limits = limits or SubagentLimits()

    def path_for(self, request_id: str) -> Path:
        """Return the durable path of one request record."""
        return (
            self.root / f"{validate_path_segment(str(request_id), 'request id')}.json"
        )

    def submit(
        self,
        description: str,
        parent_node_id: str,
        *,
        agent: str = "",
        files: Sequence[str] = (),
        symbols: Sequence[str] = (),
        depends_on: Sequence[str] = (),
        request_id: str = "",
    ) -> SubagentRequest:
        """Record one request, refusing the run-level request flood."""
        pending = self.pending()
        if len(pending) >= self.limits.max_spawn_requests_per_run:
            raise SubagentLimitError(
                f"subagent request cap reached ({self.limits.max_spawn_requests_per_run} pending)"
            )
        selected = (
            request_id
            or f"spawn-{len(pending) + 1}-{abs(hash((description, parent_node_id))) % 9973:04d}"
        )
        request = SubagentRequest(
            request_id=selected,
            description=description,
            parent_node_id=parent_node_id,
            agent=agent,
            files=tuple(files),
            symbols=tuple(symbols),
            depends_on=tuple(depends_on),
        )
        atomic_write_json(self.path_for(request.request_id), request.to_dict())
        return request

    def pending(self) -> List[SubagentRequest]:
        """Return every request still awaiting admission, oldest first."""
        selected: List[SubagentRequest] = []
        for path in sorted(self.root.glob("*.json")):
            data = read_json_or_none(path)
            if (
                not isinstance(data, dict)
                or data.get("admitted")
                or data.get("refused")
            ):
                continue
            try:
                selected.append(SubagentRequest.from_dict(data))
            except (SubagentLimitError, ValueError):
                continue
        return selected

    def resolve(
        self, request_id: str, *, node_id: str = "", reason: str = ""
    ) -> Dict[str, Any]:
        """Mark one request admitted or refused with a durable reason."""
        path = self.path_for(request_id)
        data = read_json_or_none(path)
        if not isinstance(data, dict):
            raise SubagentLimitError(f"unknown spawn request: {request_id}")
        data["admitted"] = bool(node_id)
        data["refused"] = not node_id
        data["node_id"] = str(node_id)
        data["reason"] = str(reason)
        data["resolved_at"] = now_iso()
        atomic_write_json(path, data)
        return data

    def all(self) -> List[Dict[str, Any]]:
        """Return every request record including resolved ones."""
        return [
            data
            for data in (
                read_json_or_none(path) for path in sorted(self.root.glob("*.json"))
            )
            if isinstance(data, dict)
        ]


@dataclass
class SubagentAdmission:
    """The bound-checked outcome of one spawn attempt."""

    admitted: bool
    request_id: str
    parent_node_id: str
    node_id: str = ""
    agent: str = ""
    role: str = ""
    depth: int = 0
    reason: str = ""
    limits: Dict[str, Any] = field(default_factory=dict)
    estimated_cost_usd: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible admission record."""
        return asdict(self)

    def render(self, *, summary: str = "") -> str:
        """Render the model-facing result of a spawn attempt.

        The receipt is bounded by construction: it names the decision, the
        visible cap that applied, and where the child's own summary will land.
        """
        payload: Dict[str, Any] = {
            "admitted": self.admitted,
            "request_id": self.request_id,
            "node_id": self.node_id,
            "agent": self.agent,
            "role": self.role,
            "depth": self.depth,
            "reason": self.reason,
            "limits": self.limits,
        }
        if summary:
            payload["summary"] = summary
        return json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )


@dataclass
class ChildSummary:
    """Bounded structured summary returned to a parent for one child."""

    node_id: str
    agent: str = ""
    role: str = ""
    status: str = "unknown"
    changed_files: List[str] = field(default_factory=list)
    changed_file_count: int = 0
    verification: str = ""
    cost_usd: float = 0.0
    turns: int = 0
    trace_path: str = ""
    workspace_path: str = ""
    follow_ups: List[str] = field(default_factory=list)
    error: str = ""
    truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible summary."""
        return redact_secrets(asdict(self))

    def render(self, *, max_chars: int = DEFAULT_SUMMARY_MAX_CHARS) -> str:
        """Render the summary as a bounded single-line JSON receipt."""
        payload = json.dumps(
            self.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        if len(payload) <= max_chars:
            return payload
        self.truncated = True
        reduced = ChildSummary(
            node_id=self.node_id,
            agent=self.agent,
            role=self.role,
            status=self.status,
            changed_files=self.changed_files[:2],
            changed_file_count=self.changed_file_count,
            verification=self.verification[:200],
            cost_usd=self.cost_usd,
            turns=self.turns,
            trace_path=self.trace_path,
            workspace_path="",
            follow_ups=self.follow_ups[:1],
            error=self.error[:200],
            truncated=True,
        )
        payload = json.dumps(
            reduced.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return payload[: max_chars - 1] + "…"


def summarize_child(
    node_id: str,
    handoff: Mapping[str, Any],
    *,
    agent: str = "",
    limits: Optional[SubagentLimits] = None,
) -> ChildSummary:
    """Build a bounded summary from a persisted handoff mapping."""
    data = dict(handoff or {})
    files = [str(item) for item in data.get("changed_files", []) if str(item).strip()]
    verification = data.get("verification") or ""
    if isinstance(verification, (list, tuple)):
        parts = []
        for item in verification:
            if isinstance(item, Mapping):
                parts.append(
                    f"{item.get('name', item.get('kind', 'check'))}={item.get('ok', item.get('passed', '?'))}"
                )
            else:
                parts.append(str(item))
        verification = "; ".join(parts)
    follow_ups = [
        str(item) for item in data.get("follow_up_needs", []) if str(item).strip()
    ]
    return ChildSummary(
        node_id=str(node_id),
        agent=str(agent or data.get("agent", "")),
        role=str(data.get("role", "")),
        status=str(data.get("status", "unknown")),
        changed_files=files[:MAX_SUMMARY_FILES],
        changed_file_count=len(files),
        verification=str(verification)[:400],
        cost_usd=float(data.get("cost_usd", 0.0) or 0.0),
        turns=int(data.get("turns", data.get("attempts", 0)) or 0),
        trace_path=str(data.get("trace_path", "")),
        workspace_path=str(data.get("workspace_path", "")),
        follow_ups=follow_ups[:MAX_SUMMARY_FOLLOW_UPS],
        error=str(data.get("error", ""))[:400],
    )


class SubagentSpawner:
    """Admit bounded child spawns into an existing orchestrator's DAG.

    The spawner owns no execution machinery. It resolves a definition, checks
    the declared bounds against the orchestrator's live state, and hands the
    resulting node to :meth:`runtime.orchestration.Orchestrator.spawn_child`,
    which is the single admission point and the single orchestrator.
    """

    def __init__(
        self,
        orchestrator: Any,
        *,
        limits: Optional[SubagentLimits] = None,
        registry: Optional[AgentRegistry] = None,
        repo_path: str | Path | None = None,
        default_agent: str = "",
    ) -> None:
        self.orchestrator = orchestrator
        self.limits = limits or SubagentLimits()
        self.registry = (
            registry if registry is not None else AgentRegistry.load(repo_path)
        )
        self.default_agent = str(default_agent or "")
        self.spawned: Dict[str, str] = {}

    # -- resolution -----------------------------------------------------
    def resolve_definition(self, name: str = "") -> AgentDefinition:
        """Return the requested definition, or the configured default."""
        key = str(name or self.default_agent or "").strip()
        if key:
            return self.registry.get(key)
        for candidate in self.registry.names():
            definition = self.registry.get(candidate)
            if definition.role == "implementer":
                return definition
        if self.registry.names():
            return self.registry.get(self.registry.names()[0])
        return AgentDefinition(
            name="inline-implementer",
            role="implementer",
            description="Fallback definition derived from the implementer role profile.",
            version=f"{AGENT_SCHEMA_VERSION}.0.0",
            max_children=0,
        )

    def describe(self) -> Dict[str, Any]:
        """Return the visible concurrency cap and per-child status view."""
        active = list(
            getattr(self.orchestrator, "active_children", lambda: {})().values()
        )
        return {
            "limits": self.limits.to_dict(),
            "agents": {
                name: {
                    "role": definition.role,
                    "version": definition.version,
                    "model_tier": definition.model_tier,
                    "max_children": definition.max_children,
                    "tools": list(definition.tools),
                }
                for name, definition in (
                    (item, self.registry.get(item)) for item in self.registry.names()
                )
            },
            "default_agent": self.default_agent,
            "active_children": active,
            "active_count": len(active),
            "spawned": dict(self.spawned),
        }

    # -- admission ------------------------------------------------------
    def admit(self, request: SubagentRequest) -> SubagentAdmission:
        """Check every bound and return the admission decision.

        Refusals are values, not exceptions, so the model sees a bounded
        result and the parent keeps its turn budget.
        """
        limits = self.limits
        definition = self.resolve_definition(request.agent)
        parent_depth = self.node_depth(request.parent_node_id)
        depth = parent_depth + 1
        capacity = self.graph_capacity()
        visible = {
            "max_depth": limits.max_depth,
            "max_children_per_parent": limits.max_children_per_parent,
            "max_concurrent_children": limits.max_concurrent_children,
            "active_children": self.active_count(),
            "dynamic_children": capacity["dynamic_children"],
            "max_dynamic_children": capacity["max_dynamic_children"],
            "nodes": capacity["nodes"],
            "max_nodes": capacity["max_nodes"],
        }
        base = SubagentAdmission(
            admitted=False,
            request_id=request.request_id,
            parent_node_id=request.parent_node_id,
            agent=definition.name,
            role=definition.role,
            depth=depth,
            limits=visible,
        )
        if definition.max_children <= 0:
            return _refuse(base, f"agent {definition.name} may not create children")
        parent_children = self.child_count(request.parent_node_id)
        if parent_children >= limits.max_children_per_parent:
            return _refuse(
                base,
                f"parent {request.parent_node_id} already has {parent_children} children "
                f"(cap {limits.max_children_per_parent})",
            )
        if depth > limits.max_depth:
            return _refuse(
                base,
                f"depth {depth} exceeds the subagent depth cap of {limits.max_depth}",
            )
        if capacity["dynamic_children"] >= capacity["max_dynamic_children"]:
            return _refuse(
                base,
                f"dynamic child cap reached ({capacity['max_dynamic_children']} spawned)",
            )
        if capacity["nodes"] >= capacity["max_nodes"]:
            return _refuse(
                base,
                f"workflow node cap reached ({capacity['max_nodes']} nodes)",
            )
        if self.active_count() >= limits.max_concurrent_children:
            return _refuse(
                base,
                f"{self.active_count()} children are already running "
                f"(visible cap {limits.max_concurrent_children})",
            )
        estimate = float(
            definition.max_cost_usd
            or get_role_profile(definition.role).estimated_cost_usd
        )
        if estimate > limits.max_child_cost_usd:
            return _refuse(
                base,
                f"estimated child cost ${estimate:.4f} exceeds the per-child cap "
                f"${limits.max_child_cost_usd:.4f}",
            )
        if self.total_cost() + estimate > limits.max_total_cost_usd:
            return _refuse(
                base,
                f"workflow cost ${self.total_cost():.4f} + ${estimate:.4f} exceeds the "
                f"subagent budget ${limits.max_total_cost_usd:.4f}",
            )
        base.estimated_cost_usd = estimate
        base.admitted = True
        return base

    def spawn(
        self,
        description: str,
        parent_node_id: str = "root",
        *,
        agent: str = "",
        files: Sequence[str] = (),
        symbols: Sequence[str] = (),
        depends_on: Sequence[str] = (),
        request_id: str = "",
    ) -> SubagentAdmission:
        """Submit, admit, and enqueue one bounded child spawn."""
        request = SubagentRequest(
            request_id=request_id or f"spawn-{len(self.spawned) + 1}",
            description=description,
            parent_node_id=parent_node_id,
            agent=agent,
            files=tuple(files),
            symbols=tuple(symbols),
            depends_on=tuple(depends_on),
        )
        decision = self.admit(request)
        if not decision.admitted:
            return decision
        definition = self.resolve_definition(request.agent)
        try:
            node = self.orchestrator.spawn_child(
                request,
                role=definition.role,
                definition=definition,
            )
        except (SubagentLimitError, ValueError) as exc:
            # The DAG-level admission is the backstop; a refusal is still a
            # value, so the model's turn budget survives it. The orchestrator's
            # own error type is deliberately not imported here (it imports this
            # module), so a limit breach surfaces as its own typed error.
            return _refuse(decision, str(exc))
        decision.admitted = True
        decision.node_id = node.node_id
        self.spawned[request.request_id] = node.node_id
        return decision

    def graph_capacity(self) -> Dict[str, int]:
        """Return the orchestrator's node/dynamic-node capacity counters."""
        spec = getattr(self.orchestrator, "spec", None)
        limits = getattr(spec, "limits", None)
        nodes = tuple(getattr(spec, "nodes", ()) or ())
        dynamic = sum(
            1
            for node in nodes
            if str(getattr(node, "metadata", {}).get("origin", "")) == "subagent"
        )
        return {
            "nodes": len(nodes),
            "max_nodes": int(getattr(limits, "max_nodes", 0) or 0),
            "dynamic_children": dynamic,
            "max_dynamic_children": int(getattr(limits, "max_dynamic_nodes", 0) or 0),
        }

    def summary(self, node_id: str) -> ChildSummary:
        """Return the bounded summary for one child, or an honest placeholder."""
        handoff = None
        try:
            handoff = self.orchestrator.handoff(node_id)
        except Exception:
            handoff = None
        if handoff is None:
            return ChildSummary(
                node_id=str(node_id),
                status=self.node_status(node_id),
                error="child handoff is not available yet",
            )
        agent = ""
        for request_id, spawned in self.spawned.items():
            if spawned == node_id:
                try:
                    agent = self.resolve_definition(self.request_agent(request_id)).name
                except AgentDefinitionError:
                    agent = ""
                break
        return summarize_child(node_id, handoff, agent=agent, limits=self.limits)

    def request_agent(self, request_id: str) -> str:
        """Return the agent name recorded for one admitted spawn request."""
        store = getattr(self.orchestrator, "spawn_requests", None)
        if store is None:
            return ""
        for record in store.all():
            if record.get("request_id") == request_id:
                return str(record.get("agent", ""))
        return ""

    def node_status(self, node_id: str) -> str:
        """Return the orchestrator's status for one node id."""
        nodes = getattr(getattr(self.orchestrator, "state", None), "nodes", {})
        state = nodes.get(str(node_id))
        return str(getattr(state, "status", "unknown"))

    def node_depth(self, node_id: str) -> int:
        """Return the parent-chain depth of a node (0 for a root node)."""
        spec = getattr(self.orchestrator, "spec", None)
        if spec is None:
            return 0
        depth = 0
        current = str(node_id)
        seen: set[str] = set()
        while True:
            if current in seen:
                return depth
            seen.add(current)
            try:
                parent = spec.node_map()[current].parent_id
            except (KeyError, AttributeError):
                return depth
            if not parent:
                return depth
            depth += 1
            current = parent

    def child_count(self, node_id: str) -> int:
        """Return how many children a parent node already has."""
        spec = getattr(self.orchestrator, "spec", None)
        if spec is None:
            return 0
        try:
            return len(spec.children_of(str(node_id)))
        except (KeyError, AttributeError):
            return 0

    def active_count(self) -> int:
        """Return the orchestrator's live child count."""
        return int(getattr(self.orchestrator, "active_child_count", lambda: 0)())

    def total_cost(self) -> float:
        """Return the orchestrator's spent-plus-reserved cost."""
        state = getattr(self.orchestrator, "state", None)
        if state is None:
            return 0.0
        return float(getattr(state, "spent_cost_usd", 0.0)) + float(
            getattr(state, "reserved_cost_usd", 0.0)
        )


def _refuse(base: SubagentAdmission, reason: str) -> SubagentAdmission:
    base.admitted = False
    base.reason = reason
    return base


__all__ = [
    "AGENT_SCHEMA_VERSION",
    "DEFAULT_SUMMARY_MAX_CHARS",
    "EFFORT_DISPLAY_FIELDS",
    "MODEL_TIERS",
    "AgentCostView",
    "AgentDefinition",
    "AgentDefinitionError",
    "AgentRegistry",
    "ChildSummary",
    "SpawnRequestStore",
    "SubagentAdmission",
    "SubagentLimitError",
    "SubagentLimits",
    "SubagentRequest",
    "SubagentSpawner",
    "agent_cost_view",
    "agent_roster",
    "clamp_max_children",
    "default_agent_roots",
    "describe_agent",
    "parse_agent_file",
    "render_agent_lines",
    "summarize_child",
]


# ---------------------------------------------------------------------------
# Showing an agent: what it costs, not just what it is
# ---------------------------------------------------------------------------
#
# An agent definition names a MODEL TIER, a tool set, a turn budget and a
# child budget. Those four facts ARE its price, and a surface that shows only
# the role name is hiding the numbers a person needs before spawning one.
#
# The effort half is the subtle one. `cli.models.EffortVariant` already made
# the distinction this module must not blur:
#
#   * `level`    - what was ASKED FOR.
#   * `honoured` - whether a real provider parameter is on the request.
#
# They are separate fields because conflating them is the defect: "asked for
# high" and "ran at high" are different facts, and a receipt that renders
# them as one word lies to whichever of them is false. `AgentCostView`
# carries both, plus `parameter` even when `honoured` is False, so a reader
# can be told WHICH parameter was declined.

#: The effort facts a surface must show, in the order it should show them.
#: Declared here so a renderer cannot quietly drop one - `honoured` without
#: `level` is the lie this whole block exists to prevent.
EFFORT_DISPLAY_FIELDS: Tuple[str, ...] = (
    "level",
    "honoured",
    "parameter",
    "status",
    "vocabulary",
)


@dataclass(frozen=True)
class AgentCostView:
    """One agent's cost-relevant facts, with effort kept honest.

    `model_tier` is the DEFINITION's tier (what the author asked for);
    `effort` is the RESOLVED ladder answer for the model that tier will
    actually run on. They are different questions and are reported
    separately, for the same reason `level` and `honoured` are.
    """

    name: str
    role: str
    model_tier: str = ""
    tools: Tuple[str, ...] = ()
    max_turns: int = 0
    max_children: int = 0
    max_cost_usd: float = 0.0
    effort: Mapping[str, Any] = field(default_factory=dict)
    version: str = ""
    source: str = ""
    can_spawn: bool = False
    diagnostics: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible row. The effort block keeps BOTH halves."""
        return redact_secrets(
            {
                "name": self.name,
                "role": self.role,
                "model_tier": self.model_tier,
                "tools": list(self.tools),
                "max_turns": int(self.max_turns),
                "max_children": int(self.max_children),
                "max_cost_usd": float(self.max_cost_usd),
                "max_cost_declared": bool(self.max_cost_usd),
                "effort": dict(self.effort or {}),
                # `effort_level` is what the MODEL will actually use - `auto`
                # when it will send nothing. `effort_requested_level` is the
                # level the definition's tier translated to. They are separate
                # fields because conflating them is how a receipt ends up
                # saying "medium" beside `honoured: False`, which reads as a
                # medium call that quietly did nothing.
                "effort_level": str((self.effort or {}).get("level", "")),
                "effort_requested_level": str(
                    (self.effort or {}).get("tier_requested_level", "")
                ),
                "effort_tier": str((self.effort or {}).get("tier", "")),
                "effort_honoured": bool((self.effort or {}).get("honoured", False)),
                "effort_parameter": str((self.effort or {}).get("parameter", "")),
                "effort_status": str((self.effort or {}).get("status", "")),
                "version": self.version,
                "source": self.source,
                "can_spawn": bool(self.can_spawn),
                "diagnostics": list(self.diagnostics),
            }
        )

    def lines(self) -> List[str]:
        """Render the cost facts as PLAIN lines.

        Plain, never markup: a model tier, a tool name and a source path are
        DATA. A caller that renders these through a markup parser must
        escape them, and the pinned proof for that is a hostile name rendered
        through a REAL console and asserted still VISIBLE - a substring
        assertion passes while the message is being eaten.
        """
        effort = dict(self.effort or {})
        level = str(effort.get("level") or "auto")
        honoured = bool(effort.get("honoured", False))
        parameter = str(effort.get("parameter") or "")
        status = str(effort.get("status") or "unreported")
        vocabulary = [str(item) for item in (effort.get("vocabulary") or ())]

        out = [
            f"{self.name} (role: {self.role or 'unspecified'})",
            f"  model tier: {self.model_tier or 'the session default'}",
            # `level` and `honoured` are separate LINES on purpose. One line
            # reading "effort: high (honoured)" is exactly the conflation.
            f"  effort level: {level}",
            (
                f"  effort sent: {parameter} = {effort.get('value')!r} ({effort.get('family')})"
                if honoured
                else f"  effort sent: nothing ({status})"
            ),
            (f"  effort vocabulary: {'/'.join(vocabulary) or 'none declared'}"),
            (
                f"  effort note: this model sends {parameter} for other levels"
                if (not honoured and parameter)
                else ""
            ),
            # Count first, then the names: `tools (3): read, edit, test` reads
            # as a count of a set, where `tools: a, b, c (3)` reads as a
            # three-item list that a truncation might have shortened.
            f"  tools ({len(self.tools)}): {', '.join(self.tools) or 'none declared'}",
            # A non-positive cap is NOT a cap: 0 turns would mean the agent must not
            # run at all, and rendering that as the session's own budget
            # would report a definition nobody can use as one that quietly
            # inherited a working limit. The view carries no way to tell an
            # ABSENT key from a declared 0, so the line says the session
            # default and the raw number stays in `to_dict()`.
            f"  max turns: {self.max_turns if self.max_turns > 0 else 'the session default'}",
            (
                f"  max children: {self.max_children}"
                if self.can_spawn
                else "  max children: 0 (cannot spawn)"
            ),
            (
                f"  max cost usd: {self.max_cost_usd}"
                if self.max_cost_usd
                else "  max cost usd: the session default"
            ),
            f"  version: {self.version or 'unversioned'}",
        ]
        if self.source:
            out.append(f"  source: {self.source}")
        for note in self.diagnostics:
            out.append(f"  note: {note}")
        return [line for line in out if line]


def tier_requested_effort(tier: Any) -> str:
    """Translate a declared model TIER into an effort-ladder LEVEL.

    The two vocabularies are DIFFERENT and mapping them inline is how a
    renderer starts reporting a tier as a level. `MODEL_TIERS` is
    `(cheap, medium, expensive)` - a class of MODEL. The effort ladder is
    `(low, medium, high, xhigh, ...)` - a parameter ON one model. The only
    rung the two share is `medium`, and `medium` is the ladder's lower bound,
    so the mapping is deliberately in the SAFE direction: a tier never
    implies a level above `medium`. An author who wants a specific level
    sets it with `/effort`; an agent definition declares which class of
    model it wants and no more.

    An unrecognised or absent tier is `auto` - the provider's own default -
    never a guess.
    """
    text = str(tier or "").strip().lower()
    if text in MODEL_TIERS:
        return "low" if text == "cheap" else "medium"
    return "auto"


def agent_effort(model: Optional[str], tier: str = "") -> Dict[str, Any]:
    """Resolve an agent's declared tier against a model's real effort ladder.

    Delegates to `runtime.model_capabilities.picker_variant`, the ONE effort
    authority, so the ladder a person sees here is the ladder the router
    will use. The tier is first translated by :func:`tier_requested_effort`,
    because a tier is not a level; A definition with no tier reports `auto` -
    the provider's own default - rather than inventing a level nobody asked
    for. The record carries BOTH (`tier` and `tier_requested_level`) so a
    surface can render the declared class of model and the resolved
    parameter without either being mistaken for the other.

    Never raises: an unimportable authority returns an `unreported` variant
    with `honoured: False`, which is the honest answer and cannot be
    mistaken for a level that was sent.
    """
    requested = tier_requested_effort(tier)
    try:
        from runtime.model_capabilities import picker_variant

        record = picker_variant(requested, model)
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "level": requested,
            "honoured": False,
            "parameter": "",
            "value": None,
            "status": "unreported",
            "family": "",
            "detail": f"the effort authority is unavailable: {exc}",
            "vocabulary": [],
            "in_vocabulary": False,
            "effective_effort": "auto",
            "keybind": "",
        }
    receipt = (
        dict(record)
        if isinstance(record, Mapping)
        else {
            "level": requested,
            "honoured": False,
            "parameter": "",
            "value": None,
            "status": "unreported",
            "detail": "the effort authority returned an unusable receipt",
            "vocabulary": [],
            "in_vocabulary": False,
            "effective_effort": "auto",
            "keybind": "",
        }
    )
    receipt["tier"] = str(tier or "").strip().lower()
    receipt["tier_requested_level"] = requested
    return receipt


def agent_cost_view(
    definition: AgentDefinition,
    *,
    model: Optional[str] = None,
) -> AgentCostView:
    """Project one definition into its cost view, resolving the real ladder."""
    tier = str(getattr(definition, "model_tier", "") or "")
    return AgentCostView(
        name=str(definition.name),
        role=str(definition.role),
        model_tier=tier,
        tools=tuple(definition.tools or ()),
        max_turns=int(definition.max_turns or 0),
        max_children=int(definition.max_children or 0),
        max_cost_usd=float(definition.max_cost_usd or 0.0),
        effort=agent_effort(model, tier),
        version=str(definition.version or ""),
        source=str(definition.source_path or ""),
        can_spawn=bool(definition.can_spawn),
    )


def describe_agent(
    name: str,
    *,
    repo_path: str | Path | None = None,
    roots: Optional[Sequence[str | Path]] = None,
    model: Optional[str] = None,
) -> AgentCostView:
    """Load one agent definition and return its cost view.

    Assumes `name` is a bare agent name (no extension). An unknown name or a
    malformed file raises `AgentDefinitionError` - a surface shows the
    refusal, and a "shows nothing" answer would read as "this agent is
    free".
    """
    registry = AgentRegistry.load(repo_path=repo_path, roots=roots)
    return agent_cost_view(registry.get(str(name or "").strip()), model=model)


def agent_roster(
    repo_path: str | Path | None = None,
    *,
    roots: Optional[Sequence[str | Path]] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Return every loadable agent with its cost facts.

    Sorted by name so the listing is stable across runs (the registry's own
    order depends on directory iteration). A definition that failed to load
    is reported under `diagnostics` rather than omitted: an invisible agent
    is indistinguishable from an agent nobody wrote.
    """
    registry = AgentRegistry.load(repo_path=repo_path, roots=roots)
    views = [
        agent_cost_view(registry.get(name), model=model)
        for name in sorted(registry.names())
    ]
    diagnostics = [
        dict(item) for item in (getattr(registry, "diagnostics", None) or [])
    ]
    return {
        "agents": [view.to_dict() for view in views],
        "count": len(views),
        "diagnostics": diagnostics,
        "roots": [str(item) for item in (roots or default_agent_roots(repo_path))],
    }


def render_agent_lines(view: AgentCostView) -> List[str]:
    """Return the plain lines for one agent cost view."""
    return view.lines()
