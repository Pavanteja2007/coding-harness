"""Typed tool catalog, validation, loop protection, and dispatch.

The catalog itself is NOT defined here. ``harness.tools.typed_tool_specs()``
is the single source of truth for every tool name, schema, alias, and effect
class; :func:`builtin_tool_specs` derives this module's kernel ``ToolSpec``
objects from it and :meth:`ToolRegistry.schemas` returns the canonical
provider schemas verbatim. The kernel therefore cannot hold a second,
divergent tool list, and ``harness.tools.catalog_parity_report`` proves it.

Three safety properties live here because they must hold for EVERY dispatch
path, including the ones a strategy installs itself:

* **Digest-bound edits** - ``read`` returns a content digest, and the
  catalogued mutations reject a mismatched ``expected_revision`` with an
  explicit ``stale_read`` result before the handler ever runs.
* **No silent ambiguity** - a multi-match ``edit`` is refused as
  ``ambiguous_match`` instead of picking a location.
* **Loop protection** - a repeated identical call is refused after a bounded
  number of identical observations, and protocol failures are counted
  separately from task failures.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from harness.tools import (
    canonical_tool_aliases,
    catalog_fingerprint,
    catalog_parity_report,
    typed_tool_specs,
)
from harness.tools import (
    typed_tool_schemas as _canonical_schemas,
)

from .contracts import ToolCall

# Stable error slugs. Callers (TUI, SDK, evals) match on these instead of
# parsing prose, so they must not drift.
ERROR_STALE_READ = "stale_read"
ERROR_AMBIGUOUS_MATCH = "ambiguous_match"
ERROR_NO_MATCH = "no_match"
ERROR_LOOP_DETECTED = "loop_detected"
ERROR_NO_RUNTIME = "no_runtime"
ERROR_VALIDATION = "validation_error"
ERROR_HANDLER = "handler_error"

# Mutating tools that must carry the digest of the content the model read.
_REVISION_BOUND_TOOLS = frozenset({"edit", "write", "rename", "delete", "apply_patch"})
_EXPECTED_ARGUMENTS = ("expected_revision", "expected_sha256", "expected_file_hash")


class ToolValidationError(ValueError):
    """Raised when a model tool call violates its typed schema."""


@dataclass
class ToolSpec:
    """Schema and safety metadata for one derived catalog tool.

    Instances are produced by :func:`builtin_tool_specs` from the canonical
    ``harness.tools`` catalog; they are never hand-written.
    """

    name: str
    side_effect_class: str
    required: Sequence[str] = ()
    optional: Sequence[str] = ()
    types: Mapping[str, type | tuple[type, ...]] = field(default_factory=dict)
    read_only: bool = False
    aliases: Sequence[str] = ()

    def __post_init__(self) -> None:
        self.name = str(self.name or "").strip().lower()
        self.side_effect_class = str(self.side_effect_class or "read_only")
        self.required = tuple(str(item) for item in self.required or ())
        self.optional = tuple(str(item) for item in self.optional or ())
        self.types = dict(self.types or {})
        self.read_only = bool(self.read_only)
        self.aliases = tuple(str(item).strip().lower() for item in self.aliases or ())

    @property
    def arguments(self) -> tuple[str, ...]:
        """Return every accepted argument name, required first."""
        return (*self.required, *self.optional)


class ToolResult:
    """A small result wrapper retaining a stable reference for journals.

    **Redaction boundary decision: redact AT THE JOURNAL, not here.**
    `output` is raw `stdout`/`stderr` from a command the sandbox ran, and it
    has TWO consumers with opposite trust requirements:

    * the model, which needs the bytes verbatim to do the work — redacting
      before the model would change what the agent is allowed to read, and
      AGT-04's `cap_tool_output` is deliberately a *storage* bound on the
      conversation, not a redaction;
    * everything a person reads — the journal row, `shared/tracing`, the
      receipt, the CLI — which leaves the process and therefore needs the
      authority.

    Those are separated by the boundary, so the redaction lives where the value
    leaves the process: `harness/agent_kernel/events.py::RunEventJournal.append`
    (the authoritative journal) and `harness/trace.py::TraceLogger.log` (the
    legacy tracer). Both call `harness.redaction.redact_for_journal`, which is
    fail-closed and caps each string at `JOURNAL_TEXT_CAP` (200 000) BEFORE the
    redactor runs. Every journal-side consumer is therefore safe by
    construction rather than by four separate decisions.

    What is NOT covered, deliberately: a caller that reads `result.output`
    directly and prints it without going through a journal. That is a
    presentation decision, and presentation is T4's (`cli/ui.py`), which is
    already fixing the strip-ANSI-before-redact ordering defect on that side.
    """

    def __init__(
        self,
        ok: bool,
        output: Any,
        reference: str = "",
        *,
        error_kind: str = "",
        digest: str = "",
    ) -> None:
        self.ok = bool(ok)
        self.output = output
        self.reference = str(reference or "")
        self.error_kind = str(error_kind or "")
        self.digest = str(digest or "")

    def as_dict(self) -> Dict[str, Any]:
        """Return a serializable tool result."""
        return {
            "ok": self.ok,
            "output": self.output,
            "reference": self.reference,
            "error_kind": self.error_kind,
            "digest": self.digest,
        }


ToolHandler = Callable[[ToolCall, Mapping[str, Any]], Any]


def json_safe(value: Any, *, _depth: int = 0) -> Any:
    """Return a JSON-serializable projection of an arbitrary value.

    Assumes provider payloads are usually already JSON, but a fake or a
    misbehaving adapter can hand the journal an object that ``json.dumps``
    would raise on. Unserializable leaves become their ``repr`` bounded to a
    short string, containers are projected recursively, and recursion is
    bounded so a self-referential payload cannot hang the caller.
    """
    if _depth > 8:
        return "<max-depth>"
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, Mapping):
        return {
            str(key): json_safe(item, _depth=_depth + 1)
            for key, item in list(value.items())[:512]
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item, _depth=_depth + 1) for item in list(value)[:512]]
    return repr(value)[:512]


def json_fingerprint(value: Any) -> str:
    """Return a stable digest for one JSON-like value."""
    payload = json.dumps(
        json_safe(value), sort_keys=True, separators=(",", ":"), default=repr
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ToolLoopGuard:
    """Detect a doom loop of repeated identical tool calls.

    A call is identified by its canonical name plus its JSON-normalized
    arguments, so a byte-identical repeat is recognized even when the
    provider re-serializes the arguments differently. Read-only tools are
    exempt by default: re-reading a file is idempotent and refusing it would
    break legitimate exploration.
    """

    def __init__(
        self,
        *,
        max_repeats: int = 3,
        include_read_only: bool = False,
    ) -> None:
        self.max_repeats = max(1, int(max_repeats))
        self.include_read_only = bool(include_read_only)
        self._counts: Dict[str, int] = {}
        self.blocked: List[str] = []

    def fingerprint(self, call: ToolCall | Mapping[str, Any]) -> str:
        """Return the repeat-detection fingerprint for one call."""
        tool = call.tool if isinstance(call, ToolCall) else str(call.get("tool") or "")
        arguments = call.arguments if isinstance(call, ToolCall) else dict(call or {})
        return json_fingerprint([str(tool or "").strip().lower(), arguments])

    def observe(self, call: ToolCall, *, read_only: bool = False) -> tuple[bool, int]:
        """Record one call and return ``(blocked, identical_count)``."""
        key = self.fingerprint(call)
        count = self._counts.get(key, 0) + 1
        self._counts[key] = count
        exempt = read_only and not self.include_read_only
        blocked = not exempt and count > self.max_repeats
        if blocked:
            self.blocked.append(key)
        return blocked, count

    def preview(self, call: ToolCall, *, read_only: bool = False) -> tuple[bool, int]:
        """Return what the NEXT :meth:`observe` would return, recording nothing.

        A pure query: it computes the count this call *would* produce and
        whether that count trips the bound, and leaves the guard untouched. Use
        it to ask "would this be a repeat?" without spending the observation —
        for instance when a caller wants to know the answer before deciding
        whether to dispatch at all. A caller that then DOES dispatch must use
        :meth:`note_repeat` (via :meth:`ToolRegistry.note_repeat`) instead, or
        the count never advances and the bound is never reached.
        """
        key = self.fingerprint(call)
        count = self._counts.get(key, 0) + 1
        exempt = read_only and not self.include_read_only
        return (not exempt and count > self.max_repeats), count

    def armed(
        self,
        *,
        max_repeats: Optional[int] = None,
        include_read_only: Optional[bool] = None,
    ) -> None:
        """Apply dispatch-time bounds without counting anything.

        The strategy pre-flights repeats before dispatch, so the guard's bounds
        have to already reflect the run's config; otherwise the pre-flight
        would answer with the class defaults while :meth:`observe` inside
        dispatch answered with the configured ones, and the two could disagree
        about whether the same call is a doom loop.
        """
        if max_repeats is not None:
            self.max_repeats = max(1, int(max_repeats))
        if include_read_only is not None:
            self.include_read_only = bool(include_read_only)

    def reset(self) -> None:
        """Forget every observed call."""
        self._counts.clear()
        self.blocked.clear()

    def report(self) -> Dict[str, Any]:
        """Return a machine-readable summary of loop observations."""
        repeated = sorted(
            (key for key, count in self._counts.items() if count > 1),
            key=lambda key: (-self._counts[key], key),
        )
        return {
            "max_repeats": self.max_repeats,
            "include_read_only": self.include_read_only,
            "observed": len(self._counts),
            "repeated": len(repeated),
            "blocked": len(self.blocked),
            "repeated_fingerprints": repeated[:20],
        }


class _FileView:
    """Read-only view of the repository a dispatch is allowed to observe."""

    def __init__(self, root: str = "", workspace: Any = None, journal: Any = None):
        self.root = str(root or "")
        self._workspace = workspace
        self._journal = journal

    def _relative(self, path: str) -> str:
        text = str(path or "").replace("\\", "/")
        if self.root:
            root = str(self.root).replace("\\", "/").rstrip("/")
            if text.startswith(root + "/"):
                return text[len(root) + 1 :]
        return text

    def text(self, relative: str) -> Optional[str]:
        """Return the current content of one repository-relative path."""
        if self._workspace is not None:
            return self._workspace.read_text(self._relative(relative))
        if self._journal is not None:
            return self._journal.read(self._relative(relative))
        if not self.root:
            return None
        candidate = Path(self.root) / Path(self._relative(relative))
        if not candidate.is_file():
            return None
        return candidate.read_text(encoding="utf-8", errors="replace")

    def revision(self, relative: str) -> Optional[str]:
        """Return the current content digest of one path, or ``None``."""
        key = self._relative(relative)
        if self._workspace is not None:
            revision = self._workspace.revision(key)
            return str(revision.sha256) if getattr(revision, "exists", False) else ""
        text = self.text(relative)
        if text is None:
            return None
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _patch_paths(patch: str) -> List[str]:
    """Return the repository-relative paths a unified patch targets."""
    paths: List[str] = []
    for line in str(patch or "").splitlines():
        if not line.startswith("--- "):
            continue
        candidate = line[4:].strip().split("\t")[0].strip()
        for prefix in ("a/", "b/"):
            if candidate.startswith(prefix):
                candidate = candidate[len(prefix) :]
        if candidate in {"/dev/null", ""}:
            continue
        if candidate not in paths:
            paths.append(candidate)
    return paths


def _file_view(context: Mapping[str, Any], repo_path: str = "") -> _FileView:
    """Resolve the file view for one dispatch context."""
    backend = context.get("execution_backend")
    workspace = getattr(backend, "workspace", None) if backend is not None else None
    journal = context.get("workspace")
    root = repo_path or str(context.get("repo_path") or "")
    if not root:
        spec = context.get("spec")
        root = str(getattr(spec, "repo_path", "") or "")
    return _FileView(root=root, workspace=workspace, journal=journal)


def _expected_digest(value: Any) -> str:
    """Return the digest encoded in a model-supplied expected revision."""
    if value is None:
        return ""
    if isinstance(value, Mapping):
        return str(value.get("sha256") or value.get("hash") or "")
    return str(value or "")


class ToolRegistry:
    """Register typed tools and execute only validated, digest-bound calls.

    The registry is handler-free on construction: a strategy installs its own
    handlers with :meth:`set_handler`, and any catalogued tool that still has
    no handler is dispatched through the run's safe execution backend (or
    refused honestly when no backend is bound).
    """

    def __init__(self, specs: Optional[Iterable[ToolSpec]] = None) -> None:
        self._specs: Dict[str, ToolSpec] = {}
        self._handlers: Dict[str, ToolHandler] = {}
        for spec in specs or builtin_tool_specs():
            self.register(spec)
        self.protocol_failures = 0
        self.task_failures = 0
        self.stale_reads = 0
        self.ambiguous_matches = 0
        self.deduplicated_events = 0
        self.bound_revisions = 0
        self.unbound_revisions = 0
        self._loop = ToolLoopGuard()
        self._seen_event_ids: set[str] = set()
        # Digest provenance for revision binding. A digest is NEVER minted at
        # dispatch time; it is recorded when the session reads a file or the
        # first time the registry has to observe one, so binding a missing
        # ``expected_revision`` quotes a real earlier observation.
        self._read_digests: Dict[str, str] = {}
        self._first_observed: Dict[str, str] = {}

    # -- catalog ---------------------------------------------------------

    @property
    def names(self) -> List[str]:
        """Return registered canonical and alias names."""
        return sorted(self._specs)

    @property
    def canonical_names(self) -> List[str]:
        """Return registered canonical names only, in catalog order."""
        seen: set[str] = set()
        output: List[str] = []
        for name, spec in self._specs.items():
            if name == spec.name and spec.name not in seen:
                seen.add(spec.name)
                output.append(spec.name)
        return output

    def register(self, spec: ToolSpec, handler: Optional[ToolHandler] = None) -> None:
        """Register a canonical tool and its aliases."""
        if not spec.name:
            raise ValueError("tool name is required")
        self._specs[spec.name] = spec
        for alias in spec.aliases:
            self._specs[alias] = spec
        if handler is not None:
            self._handlers[spec.name] = handler

    def set_handler(self, name: str, handler: ToolHandler) -> None:
        """Set or replace a canonical tool handler."""
        canonical = self.canonical_name(name)
        if canonical is None:
            raise KeyError(name)
        self._handlers[canonical] = handler

    def handler_for(self, name: str) -> Optional[ToolHandler]:
        """Return the installed handler for a tool or alias, if any."""
        canonical = self.canonical_name(name)
        return self._handlers.get(canonical) if canonical else None

    def spec(self, name: str) -> Optional[ToolSpec]:
        """Return a tool specification by canonical or alias name."""
        return self._specs.get(str(name or "").strip().lower())

    def canonical_name(self, name: str) -> Optional[str]:
        """Return the canonical name for a tool or alias."""
        spec = self.spec(name)
        return spec.name if spec else None

    def restrict(self, names: Sequence[str]) -> None:
        """Keep only the named canonical tools, their aliases, and handlers.

        An alias in ``names`` keeps its canonical tool, so a role that asks
        for ``ask`` still receives the catalogued ``question`` tool instead of
        silently losing it.
        """
        allowed = {str(name).strip().lower() for name in names or () if str(name)}
        kept = {
            name: spec
            for name, spec in self._specs.items()
            if spec.name in allowed or any(alias in allowed for alias in spec.aliases)
        }
        canonical = {spec.name for spec in kept.values()}
        self._specs = kept
        self._handlers = {
            name: handler
            for name, handler in self._handlers.items()
            if name in canonical
        }

    def schemas(self) -> List[Dict[str, Any]]:
        """Return provider schemas for the registered tools.

        The schemas are the canonical ``harness.tools`` entries filtered to
        this registry, so the kernel's advertised schema can never differ
        from the production catalog's.
        """
        registered = set(self.canonical_names)
        return [
            schema
            for schema in typed_tool_schemas()
            if str(schema.get("function", {}).get("name") or "") in registered
        ]

    def can_run_parallel(self, calls: Sequence[ToolCall | Mapping[str, Any]]) -> bool:
        """Return whether a batch contains only explicitly read-only tools.

        The answer comes from the catalog's ``read_only`` declaration, never
        from a list of tool names held here. ``side_effect_class`` is consulted
        as well because a derived spec that forgot to carry the declaration must
        not silently become "parallel-safe" by omitting it.
        """
        if not calls:
            return False
        for call in calls:
            try:
                normalized = self.validate(call)
            except ToolValidationError:
                return False
            if normalized.side_effect_class != "read_only":
                return False
            spec = self.spec(normalized.tool)
            if spec is None or not spec.read_only:
                return False
        return True

    def concurrency_class(self, call: ToolCall | Mapping[str, Any]) -> str:
        """Return ``"concurrent"`` or ``"sequential"`` for one call.

        The single scheduling decision, asked of the spec. A call whose tool is
        unknown or whose arguments fail validation is ``"sequential"`` — the
        conservative answer, because the alternative is running something we
        could not classify at the same time as something else.

        This is the REPORTING form (it names the class for a trace row). Code
        that has to branch on the answer should call :meth:`is_concurrent`,
        which returns a real bool.
        """
        return "concurrent" if self.is_concurrent(call) else "sequential"

    def is_concurrent(self, call: ToolCall | Mapping[str, Any]) -> bool:
        """Return whether one call may run while another is running.

        The boolean form of :meth:`concurrency_class`. It exists because a
        stringly-typed answer is a trap: ``bool("sequential")`` is ``True``, so
        a scheduler that treated the class name as a truth value would run
        every mutation as if it were a read.
        """
        try:
            normalized = self.validate(call)
        except ToolValidationError:
            return False
        spec = self.spec(normalized.tool)
        if spec is None or not spec.read_only:
            return False
        return normalized.side_effect_class == "read_only"

    def arm_loop_guard(self, config: Mapping[str, Any]) -> None:
        """Apply the run's doom-loop bounds to the guard without counting.

        The strategy pre-flights repeats before dispatch (a doom loop is a
        decision for the user, not a refusal the model can retry), so the
        guard's bounds must already be the run's before either side asks.
        """
        values = dict(config or {})
        self._loop.armed(
            max_repeats=max(
                1,
                int(values.get("max_repeat_tool_calls", self._loop.max_repeats) or 1),
            ),
            include_read_only=bool(
                values.get("loop_guard_read_only", self._loop.include_read_only)
            ),
        )

    def note_repeat(
        self, call: ToolCall | Mapping[str, Any], config: Mapping[str, Any]
    ) -> tuple[bool, int, str]:
        """Record one call against the doom-loop guard and report the verdict.

        Returns ``(is_doom_loop, identical_count, fingerprint)``. This is the
        recording entry point the DISPATCHER uses, and it is why the guard can
        work at all: the strategy asks this before executing anything, so a
        repeated identical call can be escalated to a decision the user makes
        rather than refused and re-emitted.

        The returned fingerprint is what the caller passes back as
        ``loop_guard_pre_checked`` in the dispatch context, so the registry's
        own last-line check does not count the same call a second time. One
        observation per call, or the bound is reached at half the number of
        turns it claims — which is why the count is taken here and not in both
        places.
        """
        self.arm_loop_guard(config or {})
        try:
            normalized = self.validate(call)
        except ToolValidationError:
            return False, 0, ""
        spec = self.spec(normalized.tool)
        key = self._loop.fingerprint(normalized)
        blocked, count = self._loop.observe(
            normalized, read_only=bool(spec and spec.read_only)
        )
        return blocked, count, key

    def preview_repeat(
        self, call: ToolCall | Mapping[str, Any], config: Mapping[str, Any]
    ) -> tuple[bool, int]:
        """Return ``(is_doom_loop, identical_count)`` WITHOUT recording.

        A pure query against the guard, for a caller that wants the answer
        before deciding whether to dispatch. Because it records nothing, the
        same call previews identically on every call — so a dispatcher that
        previews and then dispatches must record the observation some other
        way, or the bound is never reached. :meth:`note_repeat` is that way;
        the strategy uses it.
        """
        self.arm_loop_guard(config or {})
        try:
            normalized = self.validate(call)
        except ToolValidationError:
            return False, 0
        spec = self.spec(normalized.tool)
        return self._loop.preview(normalized, read_only=bool(spec and spec.read_only))

    # -- validation ------------------------------------------------------

    def validate(self, call: ToolCall | Mapping[str, Any]) -> ToolCall:
        """Validate and normalize a call before policy evaluation.

        Raises :class:`ToolValidationError` for an unknown tool, an unknown or
        missing argument, a wrong argument type, or a path that is not
        repository-relative. A validation failure is a PROTOCOL failure, not
        a task failure, and is counted as such.
        """
        normalized = call if isinstance(call, ToolCall) else ToolCall.from_dict(call)
        spec = self.spec(normalized.tool)
        if spec is None:
            self.protocol_failures += 1
            raise ToolValidationError(f"unknown tool: {normalized.tool or '<empty>'}")
        args = json_safe(dict(normalized.arguments or {}))
        if not isinstance(args, dict):  # pragma: no cover - json_safe invariant
            self.protocol_failures += 1
            raise ToolValidationError(f"{normalized.tool} arguments must be an object")
        allowed = set(spec.required) | set(spec.optional) | {"call_id"}
        unknown = sorted(set(args) - allowed)
        if unknown:
            self.protocol_failures += 1
            raise ToolValidationError(
                f"{normalized.tool} received unknown arguments: {', '.join(unknown)}"
            )
        missing = [name for name in spec.required if name not in args]
        if missing and not self._defer_missing_revision(spec.name, missing, args):
            self.protocol_failures += 1
            raise ToolValidationError(
                f"{normalized.tool} missing required arguments: {', '.join(missing)}"
            )
        for name, expected in spec.types.items():
            if name not in args:
                continue
            value = args[name]
            allowed_types = (
                tuple(expected) if isinstance(expected, tuple) else (expected,)
            )
            if not any(type(value) is item for item in allowed_types):
                self.protocol_failures += 1
                raise ToolValidationError(
                    f"{normalized.tool}.{name} has invalid type {type(value).__name__}"
                )
        for name in ("path", "target", "file", "source_path", "destination_path"):
            if name in args and args[name] is not None:
                text = str(args[name]).replace("\\", "/")
                if (
                    text.startswith("/")
                    or re.match(r"^[A-Za-z]:", text)
                    or ".." in PathParts(text)
                ):
                    self.protocol_failures += 1
                    raise ToolValidationError(
                        f"{normalized.tool}.{name} must be repository-relative"
                    )
        normalized.arguments = args
        normalized.tool = spec.name
        normalized.side_effect_class = spec.side_effect_class
        if not normalized.target:
            normalized.target = str(
                args.get("path")
                or args.get("command")
                or args.get("url")
                or args.get("server")
                or args.get("destination_path")
                or ""
            )
        return normalized

    def validate_many(
        self, calls: Sequence[ToolCall | Mapping[str, Any]]
    ) -> tuple[List[ToolCall], List[Dict[str, Any]]]:
        """Validate a batch, separating usable calls from protocol errors.

        The error entries are the model-facing feedback: each carries a
        stable ``error_kind`` and a message the model can self-correct from.
        """
        accepted: List[ToolCall] = []
        errors: List[Dict[str, Any]] = []
        for raw in calls or ():
            try:
                accepted.append(self.validate(raw))
            except ToolValidationError as exc:
                errors.append(
                    {
                        "error_kind": ERROR_VALIDATION,
                        "error": str(exc),
                        "raw": json_safe(
                            raw.to_dict() if isinstance(raw, ToolCall) else raw
                        ),
                    }
                )
        return accepted, errors

    @staticmethod
    def _defer_missing_revision(
        tool: str, missing: Sequence[str], args: Mapping[str, Any]
    ) -> bool:
        """Return whether a missing required argument is a bindable digest.

        The catalog requires ``expected_revision`` on every revision-bound
        mutation. When the model omits it, the registry binds the digest of a
        real earlier observation (the session's own read, or the first time
        this registry observed the file) BEFORE validation, so this is a
        normal path, not a skipped check. Every other missing required
        argument is still a hard protocol error.
        """
        if tool not in _REVISION_BOUND_TOOLS:
            return False
        deferred = set()
        if tool == "apply_patch":
            if "expected_revisions" in missing:
                deferred.add("expected_revisions")
        elif "expected_revision" in missing:
            deferred.add("expected_revision")
        return bool(deferred) and set(missing) <= deferred

    # -- safe-edit preconditions ----------------------------------------

    def _bind_arguments(
        self,
        call: ToolCall | Mapping[str, Any],
        view: _FileView,
        config: Mapping[str, Any],
    ) -> tuple[ToolCall | Mapping[str, Any], Optional[ToolResult]]:
        """Bind an omitted digest onto a call's arguments before validation.

        Returns the (possibly rebuilt) call payload and, when the strict
        digest policy refuses an unbound mutation, the refusal result.
        """
        if isinstance(call, ToolCall):
            tool = self.canonical_name(call.tool) or ""
            arguments = dict(call.arguments or {})
        else:
            payload = dict(call or {})
            tool = self.canonical_name(str(payload.get("tool") or "")) or ""
            arguments = dict(payload.get("arguments") or {})
            if not arguments:
                arguments = {
                    key: value
                    for key, value in payload.items()
                    if key not in {"tool", "call_id", "id", "event_id"}
                }
        if tool not in _REVISION_BOUND_TOOLS:
            return call, None
        require_read = bool(config.get("require_edit_digest"))
        if tool == "apply_patch":
            if any(arguments.get(key) for key in ("expected_revisions",)):
                return call, None
            bound: Dict[str, str] = {}
            for relative in _patch_paths(str(arguments.get("patch") or "")):
                digest = self._observe(view, relative, require_read=require_read)
                if digest:
                    bound[relative] = digest
            if bound and len(bound) == len(
                _patch_paths(str(arguments.get("patch") or ""))
            ):
                arguments["expected_revisions"] = bound
                self.bound_revisions += 1
            else:
                self.unbound_revisions += 1
                if require_read:
                    return call, self._unbound_refusal(
                        tool, "the patch targets files this run never read"
                    )
        elif any(arguments.get(key) for key in _EXPECTED_ARGUMENTS):
            return call, None
        else:
            digest = self._observe(
                view, str(arguments.get("path") or ""), require_read=require_read
            )
            if digest:
                arguments["expected_revision"] = digest
                self.bound_revisions += 1
            else:
                self.unbound_revisions += 1
                if require_read:
                    return call, self._unbound_refusal(
                        tool,
                        f"{arguments.get('path') or '<path>'} was never read in this run",
                    )
        if isinstance(call, ToolCall):
            call.arguments = arguments
            return call, None
        payload = dict(call)
        payload["arguments"] = arguments
        return payload, None

    def _observe(
        self, view: _FileView, relative: str, *, require_read: bool = False
    ) -> str:
        """Return the recorded digest for a path, observing it once if needed.

        The first observation happens the first time this registry has to
        look at the path, which is strictly before any mutation it applies,
        so a bound revision is never a hash of the post-call state. With
        ``require_read`` only a digest the session actually READ counts, so
        the strict policy refuses a file it never inspected.
        """
        key = str(relative or "").replace("\\", "/")
        if not key:
            return ""
        known = self._read_digests.get(key) or self._first_observed.get(key)
        if known:
            return known
        if require_read:
            return ""
        try:
            digest = view.revision(key) or ""
        except Exception:
            return ""
        if digest:
            self._first_observed[key] = digest
        return digest

    @staticmethod
    def _unbound_refusal(tool: str, reason: str) -> ToolResult:
        """Refuse an unbound mutation when the strict digest policy is on."""
        return ToolResult(
            False,
            f"TOOL ERROR [{ERROR_STALE_READ}]: {tool} requires the content digest "
            f"of the file you read, and {reason}. Read the file first and pass "
            "its sha256 as expected_revision.",
            error_kind=ERROR_STALE_READ,
        )

    def _check_revisions(self, call: ToolCall, view: _FileView) -> Optional[ToolResult]:
        """Refuse a mutation whose expected digest no longer matches."""
        if call.tool not in _REVISION_BOUND_TOOLS:
            return None
        arguments = dict(call.arguments or {})
        if call.tool == "apply_patch":
            expected = arguments.get("expected_revisions")
            if not isinstance(expected, Mapping):
                return None
            targets = {
                str(key): _expected_digest(value) for key, value in expected.items()
            }
        else:
            supplied = next(
                (arguments[key] for key in _EXPECTED_ARGUMENTS if arguments.get(key)),
                None,
            )
            if supplied is None:
                return None
            targets = {str(arguments.get("path") or ""): _expected_digest(supplied)}
        for relative, digest in targets.items():
            if not relative or not digest:
                continue
            try:
                current = view.revision(relative)
            except Exception:
                # Unreadable (protected/symlink/vanished): the mutation
                # handler and permission policy own this refusal.
                return None
            if current is None or str(current) != digest:
                self.stale_reads += 1
                self.protocol_failures += 1
                return ToolResult(
                    False,
                    f"TOOL ERROR [{ERROR_STALE_READ}]: {relative} changed since it was "
                    f"read (expected sha256 {digest[:12]}, current "
                    f"{str(current or 'absent')[:12]}). Re-read the file, then retry.",
                    error_kind=ERROR_STALE_READ,
                )
        return None

    def _check_uniqueness(
        self, call: ToolCall, view: _FileView
    ) -> Optional[ToolResult]:
        """Refuse a replacement whose target text is missing or ambiguous.

        A file that cannot be read at all (protected path, symlink component,
        vanished) is NOT reported as a missing match: the mutation handler
        and the permission policy own that refusal and must stay the
        authority. This check only speaks when the current content was
        actually observed.
        """
        if call.tool != "edit":
            return None
        arguments = dict(call.arguments or {})
        old_string = str(arguments.get("old_string") or "")
        if not old_string or arguments.get("hunk_id"):
            return None
        try:
            content = view.text(str(arguments.get("path") or ""))
        except Exception:
            return None
        if content is None:
            return None
        matches = content.count(old_string)
        if matches == 1:
            return None
        if matches == 0:
            self.protocol_failures += 1
            return ToolResult(
                False,
                f"TOOL ERROR [{ERROR_NO_MATCH}]: old_string was not found in "
                f"{arguments.get('path')}. Re-read the file and copy the exact text.",
                error_kind=ERROR_NO_MATCH,
            )
        self.ambiguous_matches += 1
        self.protocol_failures += 1
        return ToolResult(
            False,
            f"TOOL ERROR [{ERROR_AMBIGUOUS_MATCH}]: old_string matches {matches} "
            f"places in {arguments.get('path')}. Extend old_string so it is unique "
            "before editing.",
            error_kind=ERROR_AMBIGUOUS_MATCH,
        )

    def _check_loop(
        self,
        call: ToolCall,
        config: Mapping[str, Any],
        *,
        pre_checked: Any = (),
    ) -> Optional[ToolResult]:
        """Refuse a repeated identical call once the bound is exceeded.

        This is the LAST line of defence. The strategy records the observation
        itself through :meth:`note_repeat` — so it can escalate a doom loop to a
        decision the user has to make before anything executes — and passes the
        fingerprints it recorded in ``loop_guard_pre_checked``. Counting them
        again here would reach the bound in half the turns, so a pre-checked
        call skips the observation entirely.

        The refusal still exists for a caller that dispatches directly (the
        registry's own ``dispatch``, an SDK, a test): a guard whose only
        enforcement point is the loop above it is not a guard.
        """
        self.arm_loop_guard(config or {})
        spec = self.spec(call.tool)
        if self._loop.fingerprint(call) in set(pre_checked or ()):
            return None
        blocked, count = self._loop.observe(
            call, read_only=bool(spec and spec.read_only)
        )
        if not blocked:
            return None
        self.protocol_failures += 1
        return ToolResult(
            False,
            f"TOOL ERROR [{ERROR_LOOP_DETECTED}]: {call.tool} was already requested "
            f"{count} times with identical arguments and was not executed again. "
            "Change the arguments, use a different tool, or finish.",
            error_kind=ERROR_LOOP_DETECTED,
        )

    # -- dispatch --------------------------------------------------------

    def execute(
        self,
        call: ToolCall | Mapping[str, Any],
        context: Optional[Mapping[str, Any]] = None,
    ) -> ToolResult:
        """Validate, guard, dispatch, and annotate one tool result."""
        ctx = dict(context or {})
        config = ctx.get("config") if isinstance(ctx.get("config"), Mapping) else {}
        view = _file_view(ctx)
        payload, refusal = self._bind_arguments(call, view, dict(config or {}))
        if refusal is not None:
            return refusal
        normalized = self.validate(payload)
        for guard in (
            lambda: self._check_loop(
                normalized, config or {}, pre_checked=ctx.get("loop_guard_pre_checked")
            ),
            lambda: self._check_revisions(normalized, view),
            lambda: self._check_uniqueness(normalized, view),
        ):
            refusal = guard()
            if refusal is not None:
                return refusal
        handler = self._handlers.get(normalized.tool)
        if handler is None:
            handler = self._fallback_handler(normalized)
        if handler is None:
            return ToolResult(
                False,
                f"TOOL ERROR: no handler registered for {normalized.tool}",
                error_kind=ERROR_NO_RUNTIME,
            )
        try:
            output = handler(normalized, ctx)
        except Exception as exc:
            self.task_failures += 1
            return ToolResult(
                False,
                f"TOOL ERROR [{type(exc).__name__}]: {exc}",
                error_kind=ERROR_HANDLER,
            )
        if isinstance(output, ToolResult):
            result = output
        else:
            reference = ""
            if isinstance(output, Mapping):
                reference = str(
                    output.get("reference") or output.get("result_reference") or ""
                )
            result = ToolResult(True, output, reference)
        if not result.ok:
            self.task_failures += 1
        if normalized.tool == "read" and result.ok:
            self._attach_digest(result, normalized, view)
        elif result.ok and normalized.tool in _REVISION_BOUND_TOOLS:
            self._refresh_after_mutation(normalized, view)
        return result

    def _refresh_after_mutation(self, call: ToolCall, view: _FileView) -> None:
        """Re-baseline the recorded digests after this run's own mutation.

        A mutation the agent itself applied legitimately becomes the new
        expectation, so the NEXT edit must not be refused as stale against the
        pre-mutation content. Only paths this run changed are refreshed; every
        other file keeps the digest that was actually read.
        """
        arguments = dict(call.arguments or {})
        if call.tool == "apply_patch":
            targets = _patch_paths(str(arguments.get("patch") or ""))
        else:
            targets = [
                str(arguments[key])
                for key in ("path", "source_path", "destination_path")
                if arguments.get(key)
            ]
        for relative in targets:
            key = str(relative).replace("\\", "/")
            if not key:
                continue
            self._read_digests.pop(key, None)
            self._first_observed.pop(key, None)
            try:
                digest = view.revision(key) or ""
            except Exception:
                digest = ""
            if digest:
                self._first_observed[key] = digest

    def dispatch(
        self,
        calls: Sequence[ToolCall | Mapping[str, Any]],
        context: Optional[Mapping[str, Any]] = None,
    ) -> List[ToolResult]:
        """Validate and execute a batch, converting validation errors to results.

        Every schema violation becomes a model-facing tool result instead of
        an exception, so one bad call in a batch never costs the session its
        remaining valid calls.
        """
        ctx = dict(context or {})
        config = ctx.get("config") if isinstance(ctx.get("config"), Mapping) else {}
        view = _file_view(ctx)
        results: List[ToolResult] = []
        for raw in calls or ():
            payload, refusal = self._bind_arguments(raw, view, dict(config or {}))
            if refusal is not None:
                results.append(refusal)
                continue
            try:
                results.append(self.execute(payload, ctx))
            except ToolValidationError as exc:
                results.append(
                    ToolResult(
                        False,
                        f"TOOL ERROR [{ERROR_VALIDATION}]: {exc}. "
                        "Emit one valid typed tool call.",
                        error_kind=ERROR_VALIDATION,
                    )
                )
        return results

    def _attach_digest(
        self, result: ToolResult, call: ToolCall, view: _FileView
    ) -> None:
        """Append the content digest a mutation must quote back."""
        if not isinstance(result.output, str):
            return
        try:
            digest = view.revision(str(call.arguments.get("path") or ""))
        except Exception:
            return
        if not digest:
            return
        result.digest = digest
        self._read_digests[str(call.arguments.get("path") or "").replace("\\", "/")] = (
            digest
        )
        size = len(result.output)
        result.output = (
            f"{result.output}\n\n[neo-file-digest] path="
            f"{call.arguments.get('path')} sha256={digest} chars={size}\n"
            "Pass this sha256 back as expected_revision on edit/write/rename/"
            "delete so a stale read is refused instead of overwriting a change."
        )

    def _fallback_handler(self, call: ToolCall) -> Optional[ToolHandler]:
        """Return a backend-dispatching handler for a handler-less tool.

        The kernel's :class:`PolicyEngine` is the authorization authority for
        this path; the safe backend is used for execution only. When no
        backend is bound the call is refused honestly rather than executed
        through an untyped fallback.
        """
        if call.tool in {"todo", "plan", "finish", "cancel", "question", "task"}:
            return self._control_handler
        if call.tool in {"web_search", "web_fetch"}:
            return self._network_handler
        return self._backend_handler

    def _backend_handler(self, call: ToolCall, context: Mapping[str, Any]) -> Any:
        """Dispatch one catalogued tool through the run's safe backend."""
        backend = context.get("execution_backend")
        if backend is None:
            return ToolResult(
                False,
                f"TOOL ERROR [{ERROR_NO_RUNTIME}]: {call.tool} requires the safe tool "
                "backend, which is not bound to this run.",
                error_kind=ERROR_NO_RUNTIME,
            )
        result = backend.execute(call.tool, dict(call.arguments))
        if not getattr(result, "ok", False):
            error = str(getattr(result, "error", "") or f"{call.tool} failed")
            kind = (
                ERROR_STALE_READ
                if "stale" in error.lower() or "conflict" in error.lower()
                else ERROR_HANDLER
            )
            return ToolResult(False, f"TOOL ERROR [{kind}]: {error}", error_kind=kind)
        return ToolResult(
            True,
            getattr(result, "value", result),
            str(getattr(result, "operation_id", "") or ""),
        )

    def _control_handler(self, call: ToolCall, context: Mapping[str, Any]) -> Any:
        """Record a control-signal tool into the run's own event journal.

        ``task`` is a BOUNDED spawn: it goes through
        :func:`harness.agent_kernel.subagents.dispatch_task_tool`, which
        admits the request against the subagent limits and hands the child to
        the one existing ``runtime.orchestration.Orchestrator``. Every call —
        admitted, queued, or refused — is still journaled as a
        ``control_intent`` event, so the intent is auditable either way.
        """
        session = context.get("session")
        record = {"tool": call.tool, "arguments": json_safe(call.arguments)}
        result: Optional[ToolResult] = None
        if call.tool == "task":
            from harness.agent_kernel.subagents import dispatch_task_tool

            result = dispatch_task_tool(call, context)
            record["admitted"] = bool(result.ok)
            record["node_id"] = result.operation_id
            record["receipt"] = str(result.output)[:512]
            tasks = getattr(session, "todo_items", None)
            if isinstance(tasks, list):
                tasks.append(json.dumps(record, ensure_ascii=False, sort_keys=True))
        events = context.get("events")
        append = getattr(events, "append", None)
        if callable(append):
            try:
                append(
                    {
                        "event_type": "control_intent",
                        "session_id": getattr(session, "session_id", ""),
                        "run_id": getattr(context.get("spec"), "run_id", ""),
                        "payload": record,
                    }
                )
            except Exception:
                pass
        if result is not None:
            return result
        return ToolResult(True, record)

    def _network_handler(self, call: ToolCall, context: Mapping[str, Any]) -> Any:
        """Fetch a URL or run a bounded web search."""
        try:
            from harness.webfetch import fetch_and_render

            if call.tool == "web_fetch" or call.arguments.get("url"):
                text, result = fetch_and_render(
                    str(call.arguments.get("url") or ""),
                    max_chars=int(call.arguments.get("max_chars", 3000)),
                )
                return ToolResult(bool(getattr(result, "ok", False)), text)
        except Exception as exc:
            return ToolResult(
                False,
                f"TOOL ERROR [{ERROR_HANDLER}]: fetch failed: {exc}",
                error_kind=ERROR_HANDLER,
            )
        backend = context.get("execution_backend")
        if backend is None:
            return ToolResult(
                False,
                f"TOOL ERROR [{ERROR_NO_RUNTIME}]: web_search requires the safe tool "
                "backend (no bounded host search fallback is configured).",
                error_kind=ERROR_NO_RUNTIME,
            )
        return self._backend_handler(call, context)

    # -- observability ---------------------------------------------------

    def note_event(self, event_id: str) -> bool:
        """Return whether a provider event id is new, marking it seen.

        Retried or duplicated provider stream events are dropped here so a
        tool executes exactly once per logical event.
        """
        key = str(event_id or "").strip()
        if not key or key in self._seen_event_ids:
            if key:
                self.deduplicated_events += 1
            return False
        self._seen_event_ids.add(key)
        return True

    def failure_report(self) -> Dict[str, Any]:
        """Return protocol failures tracked separately from task failures."""
        return {
            "protocol_failures": self.protocol_failures,
            "task_failures": self.task_failures,
            "stale_reads": self.stale_reads,
            "ambiguous_matches": self.ambiguous_matches,
            "deduplicated_events": self.deduplicated_events,
            "bound_revisions": self.bound_revisions,
            "unbound_revisions": self.unbound_revisions,
            "read_digests": len(self._read_digests),
            "loop": self._loop.report(),
        }

    def reset_loop_guard(self) -> None:
        """Forget every observed call so a new phase starts clean."""
        self._loop.reset()

    def loop_guard_report(self) -> Dict[str, Any]:
        """Return the guard's bounds and counters.

        The strategy reads ``max_repeats`` from here so the bound a user is told
        about in a doom-loop question is the bound the guard is actually armed
        with, rather than a second copy of the same number.
        """
        return self._loop.report()


# -- catalog derivation --------------------------------------------------


def builtin_tool_specs() -> List[ToolSpec]:
    """Return the kernel's tool specs derived from the one canonical catalog.

    The returned specs are derived on every call from
    ``harness.tools.typed_tool_specs()``; this module deliberately holds no
    hand-written tool list, so the kernel and the production catalog cannot
    drift apart.
    """
    return [
        ToolSpec(
            name=spec.name,
            side_effect_class=spec.side_effect_class,
            required=spec.required,
            optional=spec.optional,
            types=dict(spec.types),
            # The concurrency class is READ FROM THE CATALOG, never re-derived
            # here: a dispatcher that computed it from the effect class would
            # have a second answer to "may this run in parallel?" and the two
            # would drift.
            read_only=bool(
                getattr(spec, "read_only", spec.side_effect_class == "read_only")
            ),
            aliases=spec.aliases,
        )
        for spec in typed_tool_specs()
    ]


#: Backwards-compatible alias: the kernel catalog IS the canonical catalog.
catalog_specs = builtin_tool_specs


def catalog_parity() -> Dict[str, Any]:
    """Return the machine-checkable proof that both catalogs are identical."""
    return catalog_parity_report(builtin_tool_specs())


def catalog_identity() -> Dict[str, Any]:
    """Return names, aliases, and the fingerprint of the one catalog."""
    return {
        "fingerprint": catalog_fingerprint(),
        "names": [spec.name for spec in typed_tool_specs()],
        "aliases": canonical_tool_aliases(),
    }


def typed_tool_schemas() -> List[Dict[str, Any]]:
    """Return the canonical provider schemas (re-exported for convenience)."""
    return list(_canonical_schemas())


# -- response parsing ----------------------------------------------------


def parse_model_response(
    value: Any,
    known_tools: Optional[Iterable[str]] = None,
    *,
    seen_event_ids: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Parse native structured calls or the text fallback into raw calls.

    Invalid JSON, non-object arguments, and unknown tools are returned as an
    explicit ``malformed`` marker rather than raised, so the kernel can feed
    the problem back to the model instead of ending the session. Repeated
    provider events (same call id) are dropped when ``seen_event_ids`` is
    supplied. Every returned mapping is JSON-serializable so an assistant
    turn can never wedge the journal on reload.
    """
    allowed = {
        str(item).strip().lower() for item in known_tools or () if str(item).strip()
    }
    if seen_event_ids is not None:
        return dedupe_tool_calls(
            _parse_unfiltered(value, allowed), seen_event_ids=seen_event_ids
        )
    return _parse_unfiltered(value, allowed)


def _parse_unfiltered(value: Any, allowed: set[str]) -> List[Dict[str, Any]]:
    """Parse a provider payload into raw calls without event deduplication."""
    if isinstance(value, Mapping):
        raw_calls: List[Any]
        if isinstance(value.get("tool_calls"), list):
            raw_calls = value["tool_calls"]
        elif value.get("tool") or value.get("name"):
            raw_calls = [value]
        else:
            raw_calls = []
        return [_normalize_raw_call(item, allowed) for item in raw_calls]
    if isinstance(value, list):
        return [_normalize_raw_call(item, allowed) for item in value]
    text = str(value or "").strip()
    if not text:
        return [{"malformed": "empty model response", "recovery": "emit_one_tool_call"}]
    text = re.sub(
        r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL
    ).strip()
    if text.startswith("{") or text.startswith("["):
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            return [
                {
                    "malformed": f"invalid JSON tool response: {exc}",
                    "recovery": "emit_one_tool_call",
                }
            ]
        return _parse_unfiltered(parsed, allowed)
    match = re.match(r"^\s*([A-Za-z][\w-]*)\b(.*)$", text, re.DOTALL)
    if not match:
        return [
            {
                "malformed": "unrecognized tool response",
                "recovery": "emit_one_tool_call",
            }
        ]
    name, remainder = match.group(1).lower(), match.group(2).strip()
    args: Dict[str, Any] = {}
    if remainder:
        try:
            parsed = json.loads(remainder)
            if isinstance(parsed, dict):
                args = parsed
            else:
                args = {"value": parsed}
        except ValueError:
            if name in {
                "read",
                "glob",
                "grep",
                "memory",
                "fetch",
                "web_fetch",
                "shell",
                "bash",
                "process",
            }:
                args = {
                    "path"
                    if name == "read"
                    else "pattern"
                    if name in {"glob", "grep"}
                    else "query"
                    if name == "memory"
                    else "url"
                    if name in {"fetch", "web_fetch"}
                    else "command": remainder
                }
            elif name in {"done", "finish"}:
                args = {"answer": remainder}
            elif name in {"verify", "test", "lint", "typecheck", "build"}:
                args = {}
            else:
                return [
                    {
                        "malformed": f"text call {name!r} needs JSON arguments",
                        "recovery": "emit_one_tool_call",
                    }
                ]
    return [_normalize_raw_call({"tool": name, **args}, allowed)]


def _normalize_raw_call(value: Any, allowed: set[str]) -> Dict[str, Any]:
    """Normalize one provider tool call into a JSON-safe raw mapping."""
    if not isinstance(value, Mapping):
        return {
            "malformed": "tool call is not an object",
            "recovery": "emit_one_tool_call",
        }
    function = (
        value.get("function") if isinstance(value.get("function"), Mapping) else {}
    )
    name = value.get("tool") or value.get("name") or function.get("name")
    name = str(name or "").strip().lower()
    arguments: Any = None
    if "arguments" in value:
        arguments = value.get("arguments")
    elif "arguments" in function:
        arguments = function.get("arguments")
    else:
        excluded = {"tool", "id", "call_id", "function", "event_id"}
        if name not in {"mcp", "mcp_call"}:
            excluded.add("name")
        arguments = {key: item for key, item in value.items() if key not in excluded}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except ValueError:
            return {
                "malformed": "tool arguments are not valid JSON",
                "recovery": "emit_one_tool_call",
            }
    if not isinstance(arguments, Mapping):
        return {
            "malformed": "tool arguments must be an object",
            "recovery": "emit_one_tool_call",
        }
    if not name or (allowed and name not in allowed):
        return {
            "malformed": f"unknown tool: {name or '<empty>'}",
            "recovery": "emit_one_tool_call",
        }
    safe = json_safe(dict(arguments))
    normalized = {"tool": name, **safe, "arguments": safe}
    call_id = value.get("call_id") or value.get("id")
    if call_id:
        normalized["call_id"] = json_safe(call_id)
    event_id = value.get("event_id")
    if event_id:
        normalized["event_id"] = json_safe(event_id)
    return normalized


def dedupe_tool_calls(
    calls: Sequence[Mapping[str, Any]],
    *,
    seen_event_ids: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Drop retried or duplicated provider events by event id.

    A call is considered a duplicate when it repeats an earlier
    ``event_id``/``call_id`` in the same batch, or when that id was already
    recorded in ``seen_event_ids`` (the caller then owns the accumulation).
    Malformed markers always survive: dropping them would hide a real
    protocol failure from the model.
    """
    output: List[Dict[str, Any]] = []
    local: set[str] = set()
    for call in calls or ():
        if "malformed" in call:
            output.append(dict(call))
            continue
        identity = str(
            call.get("event_id") or call.get("call_id") or call.get("id") or ""
        )
        if identity and (
            identity in local
            or (seen_event_ids is not None and identity in seen_event_ids)
        ):
            continue
        if identity:
            local.add(identity)
            if seen_event_ids is not None:
                seen_event_ids.add(identity)
        output.append(dict(call))
    return output


def PathParts(value: str) -> List[str]:
    """Return path components for validation without importing pathlib rules."""
    return [part for part in str(value).replace("\\", "/").split("/") if part]


__all__ = [
    "ERROR_AMBIGUOUS_MATCH",
    "ERROR_HANDLER",
    "ERROR_LOOP_DETECTED",
    "ERROR_NO_MATCH",
    "ERROR_NO_RUNTIME",
    "ERROR_STALE_READ",
    "ERROR_VALIDATION",
    "ToolLoopGuard",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "ToolValidationError",
    "builtin_tool_specs",
    "catalog_identity",
    "catalog_parity",
    "catalog_specs",
    "dedupe_tool_calls",
    "json_fingerprint",
    "json_safe",
    "parse_model_response",
    "typed_tool_schemas",
]
