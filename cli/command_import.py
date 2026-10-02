"""``/adopt`` - bring another agent's setup over, CONVERTING and never moving.

This module is the ONE implementation of "import from claude / codex /
gemini / a path".  The slash verb is the primary surface
(:func:`adopt_command`); :func:`register_adopt_parser` is a thin argparse
registration that delegates to the same function, so a script and a session
cannot drift.  There is no second dispatcher and no per-verb ``if verb ==``
ladder: :data:`ADOPT_VERBS` is data and :func:`_dispatch` is a mapping.

Five rules the design exists to keep, each pinned by a test named after it:

1. **CONVERT, DO NOT MOVE.**  Every path this module writes to is under the
   destination ``.neo`` tree or the destination command/skill/agent roots.
   The source directory is opened READ-ONLY and a test hashes the whole tree
   before and after an apply.
2. **REPORT EVERY ITEM THAT COULD NOT BE CONVERTED, with a reason each.**  A
   silent drop is the worst outcome an adoption tool can produce, so the
   receipt always enumerates ``unconvertible`` rows and a summary line states
   the count.  :data:`UNCONVERTIBLE_REASONS` is a CLOSED vocabulary, so a
   dropped file can be counted rather than argued about.
3. **DRY RUN BY DEFAULT.**  ``plan`` is the default verb and writes nothing;
   ``apply`` requires an explicit verb *and* (for a trust-requiring source)
   an approval.
4. **IMPORTING IS A TRUST EVENT.**  :func:`blast_radius` builds the report and
   :func:`trust_lines` renders it by DELEGATING to
   ``cli.plugin_runtime.trust_lines`` - the same renderer the ``/plugin``
   trust prompt uses - over ``cli.plugin_runtime.TrustReport``, the same type.
   An unapproved trust-requiring import is REFUSED and writes nothing.
5. **NO VERDICT VOCABULARY.**  This module declares no completion status and
   cannot mint one.  An AST test fails if the strings
   ``completed_verified`` / ``completed_unverified`` / ``run_verdict`` /
   ``status_is_success`` / ``agent_contracts`` appear in code.

The ``/import`` name collision is resolved in :data:`ADOPT_COMMAND_NAME` -
read that docstring before renaming anything.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
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

from cli import commands as _commands

__all__ = [
    "ADOPTION_ROOT",
    "ADOPT_ALIASES",
    "ADOPT_COMMAND_NAME",
    "ADOPT_FLAG_EQUIVALENT",
    "ADOPT_HEADLESS_POLICY",
    "ADOPT_SPEC",
    "ADOPT_VERBS",
    "UNCONVERTIBLE_REASONS",
    "BlastRadius",
    "ImportItem",
    "ImportPlan",
    "ImportReceipt",
    "SourceRef",
    "adopt_command",
    "adopt_lines",
    "blast_radius",
    "build_plan",
    "escape_lines",
    "hash_tree",
    "import_lines",
    "namespace_for",
    "register_adopt_parser",
    "resolve_source",
    "safe_lines",
    "scan_source",
    "source_catalog",
    "trust_lines",
]


# --------------------------------------------------------------------------
# The name collision: `/import` ALREADY EXISTS and means something else.
# --------------------------------------------------------------------------
#
# `/import <path>` is a live `cli/commands.py` row: `argument_policy="required"`,
# `headless_policy="mapped"`, summary "import a session export as a new
# conversation", and `neo import` is a real argparse subcommand that reaches
# `cli.interactive._import_command` -> `cli.session.import_session`.
#
# Three options were considered and two were REJECTED:
#
# * **Namespace it to `/import <agent>`** - REJECTED.  It makes `/import x`
#   mean two things and turns a working command into a type error.  It is
#   exactly the "silently overload one verb" defect the brief names.
# * **Extend it with a subcommand** (`/import session <path>` and
#   `/import agent <src>`) - REJECTED.  Rule 8 forbids changing what works
#   today, and the bare `/import <path>` form would have to keep working while
#   also teaching a new vocabulary.  It would also leave `neo import <path>`
#   (a real, mapped subcommand) ambiguous.
#
# CHOSEN: **a different name, `/adopt`**, with `/import-agent` as an alias in
# `cli/command_aliases.py`.  Reasons, in order:
#
#   * "adopt" describes the actual action - the source keeps running, a
#     converted copy appears - and it shares no stem with `/import`;
#   * a user who types `/import claude` still gets the historical
#     "no such export: claude" sentence, which is TRUE, instead of an
#     unannounced change of meaning;
#   * the script surface is `neo adopt`, and `/import` / `neo import` keep
#     owning session exports with no cross-reference to this feature.
#
# If a future round prefers `adopt` spelled differently, rename
# `ADOPT_COMMAND_NAME` and the alias tuple together; the registry row is
# declared here as a real `CommandSpec` built by the registry's own class, so
# a rename cannot produce a row the registry would reject.

ADOPT_COMMAND_NAME = "/adopt"

#: Declared aliases for the registry row.  The registry owner puts these on
#: ``CommandSpec.aliases``; they are data here so the alias table and the row
#: cannot disagree about what the feature is called.
ADOPT_ALIASES: Tuple[str, ...] = ("/import-agent",)

#: The headless policy the registry row declares, and the flag a script user
#: is pointed at.  ``mapped`` is honest: ``adopt_command`` is pure of
#: interactivity - it never prompts, it takes an injected ``approve``.
ADOPT_HEADLESS_POLICY = "mapped"
ADOPT_FLAG_EQUIVALENT = "neo adopt <claude|codex|gemini|path> --apply"

#: The conversion namespace root, relative to the destination repository.
#: Everything an import writes that is NOT a live command/skill/agent lands
#: here, so the user can diff it before deciding anything.
ADOPTION_ROOT = ".neo/adopt"


#: The closed verb vocabulary.  A word outside this set is a usage error that
#: lists this tuple - the same shape ``cli/commands.py``'s
#: ``SUBCOMMANDS`` uses for every other verbed command.
ADOPT_VERBS: Tuple[str, ...] = ("plan", "apply", "scan", "list")

#: Why an item could not be converted.  A CLOSED set so a report can count
#: reasons instead of printing prose nobody can tally.
UNCONVERTIBLE_REASONS: Dict[str, str] = {
    "unsupported_kind": "this source kind has no Neo equivalent",
    "unreadable": "the source file could not be read",
    "unparseable": "the source file is not the format this kind requires",
    "empty": "the source file is empty",
    "no_target": "no destination root exists for this kind",
    "unsafe_name": "the name is not safe as a path segment",
    "not_a_directory": "the source path is not a directory",
    "not_present": "the source path does not exist",
}

#: Component kinds, in the order a report prints them.
COMPONENT_KINDS: Tuple[str, ...] = (
    "instruction",
    "command",
    "skill",
    "subagent",
    "mcp_server",
    "settings",
    "hook",
)

#: Item dispositions, in report order.
ITEM_ACTIONS: Tuple[str, ...] = ("converted", "skipped", "unconvertible")


# --------------------------------------------------------------------------
# Source layouts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceLayout:
    """Where one foreign agent keeps a given component kind."""

    kind: str
    #: A path relative to the source root, or a callable ``(root) -> Path``.
    locator: Any
    #: The filename stem that carries the item's own name, if any.
    name_from: str = "stem"


@dataclass(frozen=True)
class SourceSpec:
    """One recognised foreign agent's configuration layout."""

    label: str
    summary: str
    instructions: Tuple[str, ...]
    command_dirs: Tuple[str, ...]
    command_suffixes: Tuple[str, ...]
    skill_dirs: Tuple[str, ...]
    agent_dirs: Tuple[str, ...]
    mcp_files: Tuple[str, ...]
    #: Files that are read only so their loss can be REPORTED.
    reported_only: Tuple[str, ...]


SOURCE_CATALOG: Dict[str, SourceSpec] = {
    "claude": SourceSpec(
        label="claude",
        summary="Claude Code (~/.claude or a .claude/ directory)",
        instructions=("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md"),
        command_dirs=("commands", ".claude/commands"),
        command_suffixes=(".md",),
        skill_dirs=("skills", ".claude/skills"),
        agent_dirs=("agents", ".claude/agents"),
        mcp_files=(".mcp.json", ".claude/.mcp.json"),
        reported_only=(
            "settings.json",
            "settings.local.json",
            "hooks",
            ".claude/settings.json",
            ".claude/settings.local.json",
        ),
    ),
    "codex": SourceSpec(
        label="codex",
        summary="Codex CLI (~/.codex or a .codex/ directory)",
        instructions=("AGENTS.md", ".codex/AGENTS.md"),
        command_dirs=("prompts", ".codex/prompts"),
        command_suffixes=(".md",),
        skill_dirs=("skills", ".codex/skills"),
        agent_dirs=("agents", ".codex/agents"),
        mcp_files=("config.toml", ".codex/config.toml"),
        reported_only=("rules", ".codex/rules"),
    ),
    "gemini": SourceSpec(
        label="gemini",
        summary="Gemini CLI (~/.gemini or a .gemini/ directory)",
        instructions=("GEMINI.md", ".gemini/GEMINI.md"),
        command_dirs=("commands", ".gemini/commands"),
        # Gemini commands are TOML with a `prompt` key, not markdown.
        command_suffixes=(".toml",),
        skill_dirs=("skills", ".gemini/skills"),
        agent_dirs=("agents", ".gemini/agents"),
        mcp_files=("settings.json", ".gemini/settings.json"),
        reported_only=("extensions", ".gemini/extensions"),
    ),
}

#: A bare path source discovers by CONVENTION, exactly the component kinds
#: ``cli/plugin_runtime.discover_components`` recognises.  One enumeration for
#: the trust report and for the scan, so they cannot disagree.
GENERIC_LAYOUT = SourceSpec(
    label="import",
    summary="a directory, scanned by the same conventions the plugin loader uses",
    instructions=("AGENTS.md", "CLAUDE.md", "GEMINI.md", "NEO.md"),
    command_dirs=("commands",),
    command_suffixes=(".md",),
    skill_dirs=("skills",),
    agent_dirs=("agents",),
    mcp_files=(".mcp.json",),
    reported_only=("hooks", "monitors", "bin", "lsp"),
)


def source_catalog() -> Dict[str, SourceSpec]:
    """Return the recognised source labels mapped to their layout."""
    return dict(SOURCE_CATALOG)


#: Label -> the user-home directory it lives in, resolved lazily so a test
#: can pin ``HOME`` and nothing touches the developer's real config.
_HOME_LAYOUT = {
    "claude": (".claude",),
    "codex": (".codex",),
    "gemini": (".gemini",),
}


# --------------------------------------------------------------------------
# Resolving a source
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRef:
    """One resolved import source: a label, a root, and whether it was found."""

    label: str
    root: Path
    known: bool
    detail: str
    home_relative: str = ""

    @property
    def exists(self) -> bool:
        """True when the root is a directory that exists right now."""
        try:
            return self.root.is_dir()
        except OSError:
            return False

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection with no live objects."""
        return {
            "label": self.label,
            "root": str(self.root),
            "known": self.known,
            "detail": self.detail,
            "home_relative": self.home_relative,
            "exists": self.exists,
        }


def namespace_for(label: str) -> str:
    """Return the conversion namespace for a source label.

    The namespace is the FIRST ``:`` segment of the slash name a component is
    reachable under, so it is also the label the trust report groups by.  It
    is sanitised because a label reaches a filesystem path: a source named
    ``../evil`` must not be able to write outside the destination tree, and
    :func:`_safe_segment` is the single place that decides.
    """
    return _safe_segment(label or "import")


_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_segment(name: str) -> str:
    """Reduce an arbitrary string to one safe path segment.

    Path separators, drive letters, dots-only names and empty strings are all
    refused rather than clamped, because a name that had to be rewritten is a
    name the user cannot predict and a silent rewrite is how an import lands
    somewhere nobody looked.
    """
    raw = str(name or "").strip()
    if not raw:
        return ""
    if raw in {".", ".."} or "/" in raw or "\\" in raw or ":" in raw:
        return ""
    if "\x00" in raw:
        return ""
    cleaned = _SAFE_SEGMENT.sub("-", raw).strip("-.")
    return cleaned


def _neo_home() -> Path:
    """Return the Neo home, honouring ``NEO_HOME`` like every other surface."""
    from memory.paths import neo_home

    return Path(neo_home())


def _default_roots(label: str, base: Optional[Path] = None) -> List[Path]:
    """Return the candidate default roots for a recognised label, in order.

    ``base`` is the caller's working directory and is used INSTEAD of the
    process CWD: a surface that resolved a source relative to a repository must
    not silently look in whatever directory the process happens to be in, which
    is how a test ends up reading the developer's real ``~/.claude``.
    """
    out: List[Path] = []
    here = Path(base) if base else Path.cwd()
    for rel in _HOME_LAYOUT.get(label, ()):  # e.g. ".claude"
        out.append(Path.home() / rel)
        out.append(here / rel)
    return out


def resolve_source(argument: Any, *, cwd: Optional[Any] = None) -> SourceRef:
    """Resolve one user-supplied source argument into a :class:`SourceRef`.

    ``argument`` is ``"claude"``, ``"codex"``, ``"gemini"``, or a path.  A
    recognised label searches its home AND working-directory locations and
    reports the FIRST that exists; when none does, the label is still returned
    with ``known=True`` and ``exists=False`` so the refusal can NAME the
    locations it looked in rather than saying "not found".

    Never raises: a hostile argument produces a ``SourceRef`` whose ``exists``
    is False and whose ``detail`` says why.
    """
    raw = str(argument or "").strip().strip('"').strip("'")
    base = Path(cwd) if cwd else Path.cwd()
    if not raw:
        return SourceRef(
            label="",
            root=base,
            known=False,
            detail="no source named; try: /adopt claude | /adopt codex | "
            "/adopt gemini | /adopt <path>",
        )

    lowered = raw.lower()
    if lowered in SOURCE_CATALOG:
        spec = SOURCE_CATALOG[lowered]
        tried: List[str] = []
        for candidate in _default_roots(lowered, base):
            text = str(candidate)
            tried.append(text)
            try:
                if candidate.is_dir():
                    return SourceRef(
                        label=lowered,
                        root=candidate,
                        known=True,
                        detail=f"{spec.summary}",
                        home_relative=candidate.name,
                    )
            except OSError:
                continue
        return SourceRef(
            label=lowered,
            root=Path(base),
            known=True,
            detail=(
                f"{spec.summary}; looked in "
                + ", ".join(tried[:4])
                + " and found nothing"
            ),
        )

    # A path. Relative paths resolve against ``base``, never the process CWD.
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        exists = candidate.exists()
        is_dir = candidate.is_dir()
    except OSError as exc:
        return SourceRef(
            label="import",
            root=base,
            known=False,
            detail=f"cannot read that path: {exc}",
        )
    label = candidate.name or "import"
    if exists and not is_dir:
        return SourceRef(
            label="import",
            root=candidate.parent,
            known=True,
            detail="that path is a file; name the directory holding the agent's config",
        )
    if not exists:
        return SourceRef(
            label="import",
            root=candidate,
            known=True,
            detail=f"no such directory: {candidate}",
        )
    # A PATH to a foreign agent's own directory is read with THAT agent's
    # layout, not the generic one: `/adopt ~/.claude` must report Claude's
    # TOML-vs-markdown command shapes and Claude's settings file, exactly as
    # `/adopt claude` does. Scanning it generically would silently under-read
    # the source, which is the failure this whole module exists to prevent.
    known = _LAYOUT_BY_DIRNAME.get(label.strip().strip(".").lower())
    if known:
        return SourceRef(
            label=known,
            root=candidate,
            known=True,
            detail=SOURCE_CATALOG[known].summary,
            home_relative=label,
        )
    return SourceRef(
        label=label,
        root=candidate,
        known=True,
        detail=GENERIC_LAYOUT.summary,
    )


#: Directory names that identify a foreign agent's layout, so a path and a
#: label mean the same thing.
_LAYOUT_BY_DIRNAME = {"claude": "claude", "codex": "codex", "gemini": "gemini"}


# --------------------------------------------------------------------------
# Items and plans
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportItem:
    """One source file, what it is, and what would happen to it."""

    kind: str
    name: str
    source_path: str
    target_path: str
    action: str
    reason: str = ""
    detail: str = ""
    active: bool = False
    #: The conversion namespace this item was scanned under, so a writer can
    #: re-derive the namespaced name without being told twice.
    namespace: str = ""
    #: A PORTABLE reference to the source (``<label>/<path relative to the
    #: source root>``). This - never the absolute path - is what goes into a
    #: converted file, because ``harness.skills``' untrusted-content boundary
    #: REFUSES a body carrying a host-absolute path (measured: a
    #: ``C:\\Users\\...`` string makes the whole skill unloadable), and because
    #: ``.neo/skills/**`` is committable and a home directory in it is a
    #: disclosure. The absolute path still rides the plan for the report.
    source_ref: str = ""
    #: Free-form rows for the trust report: what this item would reach.
    reaches: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection with no live objects."""
        return {
            "kind": self.kind,
            "name": self.name,
            "source_path": self.source_path,
            "source_ref": self.source_ref,
            "target_path": self.target_path,
            "action": self.action,
            "reason": self.reason,
            "detail": self.detail,
            "active": self.active,
            "namespace": self.namespace,
            "reaches": list(self.reaches),
        }


@dataclass(frozen=True)
class ImportPlan:
    """The whole answer to "what would adopting this source do"."""

    source: SourceRef
    namespace: str
    repo_path: str
    items: Tuple[ImportItem, ...] = ()
    dry_run: bool = True
    #: Files written by an ``apply``.  Empty for a plan.
    written: Tuple[str, ...] = ()
    #: Target paths that already existed and were left byte-identical.
    preserved: Tuple[str, ...] = ()
    errors: Tuple[str, ...] = ()
    approval: str = "not_required"

    # -- projections -------------------------------------------------------

    @property
    def converted(self) -> Tuple[ImportItem, ...]:
        """Items an apply would write."""
        return tuple(i for i in self.items if i.action == "converted")

    @property
    def skipped(self) -> Tuple[ImportItem, ...]:
        """Items deliberately not imported (already present, or empty)."""
        return tuple(i for i in self.items if i.action == "skipped")

    @property
    def unconvertible(self) -> Tuple[ImportItem, ...]:
        """Items that could NOT be converted, each with a closed-set reason."""
        return tuple(i for i in self.items if i.action == "unconvertible")

    def by_kind(self, kind: str) -> Tuple[ImportItem, ...]:
        """Return every item of one kind, converted or not."""
        return tuple(i for i in self.items if i.kind == kind)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection with no live objects."""
        return {
            "source": self.source.to_dict(),
            "namespace": self.namespace,
            "repo_path": self.repo_path,
            "verb": "plan" if self.dry_run else "apply",
            "counts": {
                "items": len(self.items),
                "converted": len(self.converted),
                "skipped": len(self.skipped),
                "unconvertible": len(self.unconvertible),
                "written": len(self.written),
                "preserved": len(self.preserved),
            },
            "items": [i.to_dict() for i in self.items],
            "written": list(self.written),
            "preserved": list(self.preserved),
            "errors": list(self.errors),
            "approval": self.approval,
            "blast_radius": blast_radius(self).to_dict(),
        }


# --------------------------------------------------------------------------
# Destination roots
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Destination:
    """Every directory an import may write, resolved once."""

    repo: Path
    neo_dir: Path
    adopt_dir: Path
    commands_dir: Path
    skills_dir: Path
    agents_dir: Path
    connectors_file: Path
    global_root: Path

    @classmethod
    def resolve(
        cls, repo_path: Optional[Any], *, neo_home: Optional[Any] = None
    ) -> "Destination":
        """Resolve the destination roots for a repository.

        ``neo_home`` is honoured when given so a test never writes into the
        developer's real ``%APPDATA%\\neo``; when absent the product's own
        resolver is used, which is what a session must do.
        """
        repo = Path(repo_path or Path.cwd()).resolve()
        neo_dir = repo / ".neo"
        home = Path(neo_home) if neo_home else _neo_home()
        return cls(
            repo=repo,
            neo_dir=neo_dir,
            adopt_dir=neo_dir / "adopt",
            commands_dir=neo_dir / "commands",
            skills_dir=neo_dir / "skills",
            agents_dir=neo_dir / "agents",
            connectors_file=neo_dir / "connectors.toml",
            global_root=home / "config",
        )


# --------------------------------------------------------------------------
# Reading the source
# --------------------------------------------------------------------------


def _read_text(path: Path) -> Tuple[Optional[str], str]:
    """Read one file as text. Returns ``(text, error)``; never raises.

    ``errors="replace"`` because a foreign config with a stray byte must be
    reported as unconvertible with a reason, not take the import down.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace"), ""
    except OSError as exc:
        return None, f"unreadable: {exc}"
    except Exception as exc:  # defensive: a weird path type, a broken mount
        return None, f"unreadable: {exc}"


def _iter_dir(root: Path, rel: str, suffixes: Sequence[str]) -> List[Path]:
    """Return the sorted files under ``root/rel`` with a known suffix."""
    directory = root / rel
    try:
        if not directory.is_dir():
            return []
        return sorted(
            p
            for p in directory.iterdir()
            if p.is_file()
            and p.suffix.lower() in {s.lower() for s in suffixes}
            and not p.name.startswith(".")
        )
    except OSError:
        return []


def _frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """Parse a leading ``---`` YAML-ish frontmatter block, lossily.

    Deliberately NOT a YAML parser: the only keys an import needs are flat
    strings, and a dependency-free reader cannot be made to execute anything.
    Anything that is not ``key: value`` is ignored rather than guessed at, and
    the body is returned unchanged so the converted file keeps its text.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: Dict[str, str] = {}
    body_start = len(lines)
    for index in range(1, len(lines)):
        stripped = lines[index].strip()
        if stripped == "---":
            body_start = index + 1
            break
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            meta[key.strip().lower()] = value.strip().strip("\"'")
    else:
        return {}, text
    return meta, "\n".join(lines[body_start:]).lstrip("\n")


def _toml_prompt(text: str) -> Tuple[str, str]:
    """Extract a Gemini-style ``prompt = "..."`` command body.

    Returns ``(body, error)``.  A TOML command with no ``prompt`` key is
    unconvertible with ``unparseable``, which is a REPORT, not a guess.
    """
    match = re.search(r'(?m)^\s*prompt\s*=\s*"""(.*?)"""', text, re.DOTALL)
    if match:
        return match.group(1).strip(), ""
    match = re.search(r'(?m)^\s*prompt\s*=\s*"(.*)"\s*$', text)
    if match:
        return (
            match.group(1)
            .replace('\\"', '"')
            .replace("\\n", "\n")
            .replace("\\\\", "\\")
            .strip(),
            "",
        )
    return "", 'unparseable: no prompt = "..." key'


def _mcp_servers(text: str, *, style: str) -> Tuple[List[Dict[str, str]], str]:
    """Extract MCP server definitions from a foreign config.

    Three real shapes exist and each is parsed by its own reader:

    * ``mcpServers`` - the JSON shape Claude and Gemini both publish;
    * ``[mcp_servers.<name>]`` - the TOML shape Codex publishes;
    * ``servers`` - the older Gemini key, accepted so an old install is not
      reported as empty.

    Returns ``(rows, error)`` where a row is ``{label, command}`` and a parse
    failure is returned rather than raised.
    """
    if style == "toml":
        rows: List[Dict[str, str]] = []
        current = ""
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            section = re.match(r"^\[mcp_servers\.([^\]]+)\]$", line)
            if section:
                current = section.group(1).strip().strip('"')
                rows.append({"label": current, "command": ""})
                continue
            if line.startswith("[") and "mcp_servers" not in line:
                current = ""
                continue
            if current and "=" in line:
                key, _, value = line.partition("=")
                if key.strip() == "command":
                    rows[-1]["command"] = value.strip().strip('"')
        return [r for r in rows if r["command"]], ""
    try:
        document = json.loads(text or "{}")
    except Exception as exc:
        return [], f"unparseable: {exc}"
    if not isinstance(document, dict):
        return [], "unparseable: the document is not a JSON object"
    for key in ("mcpServers", "servers"):
        table = document.get(key)
        if not isinstance(table, dict):
            continue
        rows = []
        for label, entry in table.items():
            if isinstance(entry, dict):
                command = entry.get("command") or ""
                args = entry.get("args") or []
                if isinstance(args, list):
                    command = " ".join([str(command), *[str(a) for a in args]]).strip()
                rows.append({"label": str(label), "command": str(command)})
            elif isinstance(entry, str):
                rows.append({"label": str(label), "command": entry})
        if rows:
            return rows, ""
    return [], ""


def _command_body(item_path: Path, suffix: str) -> Tuple[str, Dict[str, str], str]:
    """Read one command file into ``(body, frontmatter, error)``."""
    text, error = _read_text(item_path)
    if text is None:
        return "", {}, error or "unreadable"
    if suffix.lower() == ".toml":
        body, error = _toml_prompt(text)
        return body, {}, error
    meta, body = _frontmatter(text)
    return body, meta, ""


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------


def _mcp_style(name: str) -> str:
    """Return ``"toml"`` or ``"json"`` for an MCP config filename."""
    return "toml" if name.lower().endswith(".toml") else "json"


def _prefixed(label: str, name: str) -> str:
    """Return the namespaced name a converted component is reachable under."""
    safe_name = _safe_segment(name)
    if not safe_name:
        return ""
    return f"{label}-{safe_name}" if label else safe_name


def scan_source(
    source: SourceRef, destination: Destination, namespace: str
) -> List[ImportItem]:
    """Read a source and return one :class:`ImportItem` per discovered thing.

    READ-ONLY by construction: this function opens files for reading and
    resolves paths, and it is the only function that knows a source layout.
    Nothing here creates a directory.

    Every kind is scanned even when its target root does not exist yet, so a
    ``no_target`` refusal is a REPORT about the destination rather than a
    silent disappearance from the source.
    """
    items: List[ImportItem] = []
    if not source.exists:
        items.append(
            ImportItem(
                kind="settings",
                name=source.label or "?",
                source_path=str(source.root),
                target_path="",
                action="unconvertible",
                reason="not_present",
                detail=source.detail,
            )
        )
        return items

    spec = SOURCE_CATALOG.get(source.label, GENERIC_LAYOUT)
    root = source.root

    # -- instructions ----------------------------------------------------
    for rel in spec.instructions:
        candidate = root / rel
        if not candidate.is_file():
            continue
        text, error = _read_text(candidate)
        if text is None:
            items.append(
                _unconvertible("instruction", candidate, "", "unreadable", error)
            )
            continue
        if not text.strip():
            items.append(
                _unconvertible("instruction", candidate, "", "empty", "no content")
            )
            continue
        target = destination.adopt_dir / namespace / candidate.name
        items.append(
            ImportItem(
                kind="instruction",
                name=candidate.name,
                source_path=str(candidate),
                target_path=str(target),
                action="converted",
                reason="",
                detail=(
                    f"{len(text.splitlines())} lines, written under {namespace}/ for "
                    "comparison; it does NOT override your AGENTS.md until you move it"
                ),
                active=False,
                source_ref=_portable_ref(root, candidate, namespace),
            )
        )

    # -- commands --------------------------------------------------------
    for rel in spec.command_dirs:
        for candidate in _iter_dir(root, rel, spec.command_suffixes):
            name = _frontmatter_name(candidate)
            safe = _safe_segment(name)
            if not safe:
                items.append(
                    _unconvertible(
                        "command",
                        candidate,
                        "",
                        "unsafe_name",
                        f"unusable name {name!r}",
                        namespace=namespace,
                        source_ref=_portable_ref(root, candidate, namespace),
                    )
                )
                continue
            body, meta, error = _command_body(candidate, candidate.suffix)
            if error:
                items.append(
                    _unconvertible(
                        "command",
                        candidate,
                        safe,
                        _reason_of(error),
                        error,
                        namespace=namespace,
                        source_ref=_portable_ref(root, candidate, namespace),
                    )
                )
                continue
            if not body.strip():
                items.append(
                    _unconvertible(
                        "command",
                        candidate,
                        safe,
                        "empty",
                        "the file has frontmatter but no prompt body",
                        namespace=namespace,
                        source_ref=_portable_ref(root, candidate, namespace),
                    )
                )
                continue
            namespaced = _prefixed(namespace, safe)
            target = (
                destination.commands_dir / f"{namespaced}{_md_suffix(candidate.suffix)}"
            )
            items.append(
                ImportItem(
                    kind="command",
                    name=safe,
                    source_path=str(candidate),
                    target_path=str(target),
                    action="converted",
                    detail=_command_detail(meta, body),
                    active=True,
                    namespace=namespace,
                    source_ref=_portable_ref(root, candidate, namespace),
                    reaches=(f"/{namespaced}",),
                )
            )

    # -- skills ----------------------------------------------------------
    for rel in spec.skill_dirs:
        directory = root / rel
        try:
            entries = (
                sorted(p for p in directory.iterdir() if p.is_dir())
                if directory.is_dir()
                else []
            )
        except OSError:
            continue
        for entry in entries:
            skill_file = entry / "SKILL.md"
            if not skill_file.is_file():
                items.append(
                    _unconvertible(
                        "skill",
                        entry,
                        "",
                        "unsupported_kind",
                        "a directory under skills/ with no SKILL.md in it",
                        namespace=namespace,
                        source_ref=_portable_ref(root, entry, namespace),
                    )
                )
                continue
            safe = _safe_segment(entry.name)
            if not safe:
                items.append(
                    _unconvertible(
                        "skill",
                        entry,
                        "",
                        "unsafe_name",
                        f"unusable name {entry.name!r}",
                        namespace=namespace,
                        source_ref=_portable_ref(root, entry, namespace),
                    )
                )
                continue
            text, error = _read_text(skill_file)
            if text is None:
                items.append(
                    _unconvertible(
                        "skill",
                        skill_file,
                        safe,
                        "unreadable",
                        error,
                        namespace=namespace,
                        source_ref=_portable_ref(root, skill_file, namespace),
                    )
                )
                continue
            if not text.strip():
                items.append(
                    _unconvertible(
                        "skill",
                        skill_file,
                        safe,
                        "empty",
                        "no content",
                        namespace=namespace,
                        source_ref=_portable_ref(root, skill_file, namespace),
                    )
                )
                continue
            meta, body = _frontmatter(text)
            namespaced = _prefixed(namespace, safe)
            target = destination.skills_dir / namespaced / "SKILL.md"
            items.append(
                ImportItem(
                    kind="skill",
                    name=safe,
                    source_path=str(skill_file),
                    target_path=str(target),
                    action="converted",
                    detail=f"description: {meta.get('description', '(none declared)')[:80]}",
                    active=True,
                    namespace=namespace,
                    source_ref=_portable_ref(root, skill_file, namespace),
                    reaches=(f"/skills {namespaced}",),
                )
            )
            for extra in sorted(entry.glob("*")):
                if extra.is_file() and extra.name != "SKILL.md":
                    items.append(
                        ImportItem(
                            kind="skill",
                            name=f"{safe}/{extra.name}",
                            source_path=str(extra),
                            target_path=str(target.parent / extra.name),
                            action="converted",
                            detail="skill support file",
                            active=True,
                            namespace=namespace,
                            source_ref=_portable_ref(root, extra, namespace),
                        )
                    )

    # -- subagents -------------------------------------------------------
    for rel in spec.agent_dirs:
        for candidate in _iter_dir(root, rel, (".md",)):
            text, error = _read_text(candidate)
            if text is None:
                items.append(
                    _unconvertible(
                        "subagent",
                        candidate,
                        "",
                        "unreadable",
                        error,
                        namespace=namespace,
                        source_ref=_portable_ref(root, candidate, namespace),
                    )
                )
                continue
            meta, body = _frontmatter(text)
            safe = _safe_segment(meta.get("name") or candidate.stem)
            if not safe:
                items.append(
                    _unconvertible(
                        "subagent",
                        candidate,
                        "",
                        "unsafe_name",
                        f"unusable name {candidate.name!r}",
                        namespace=namespace,
                        source_ref=_portable_ref(root, candidate, namespace),
                    )
                )
                continue
            if not (meta.get("description") or body.strip()):
                items.append(
                    _unconvertible(
                        "subagent",
                        candidate,
                        safe,
                        "empty",
                        "no description and no body",
                        namespace=namespace,
                        source_ref=_portable_ref(root, candidate, namespace),
                    )
                )
                continue
            namespaced = _prefixed(namespace, safe)
            target = destination.agents_dir / f"{namespaced}.md"
            items.append(
                ImportItem(
                    kind="subagent",
                    name=safe,
                    source_path=str(candidate),
                    target_path=str(target),
                    action="converted",
                    detail=_subagent_detail(meta, body),
                    active=True,
                    namespace=namespace,
                    source_ref=_portable_ref(root, candidate, namespace),
                    reaches=(f"/agents {namespaced}",),
                )
            )

    # -- MCP servers -----------------------------------------------------
    for rel in spec.mcp_files:
        candidate = root / rel
        if not candidate.is_file():
            continue
        style = _mcp_style(rel)
        text, error = _read_text(candidate)
        if text is None:
            items.append(
                _unconvertible("mcp_server", candidate, "", "unreadable", error)
            )
            continue
        if rel.lower().endswith(".toml") and not _has_mcp_servers_toml(text):
            continue  # a Codex config with no [mcp_servers] declares none
        rows, error = _mcp_servers(text, style=style)
        if error:
            items.append(
                _unconvertible("mcp_server", candidate, "", _reason_of(error), error)
            )
            continue
        if not rows:
            continue
        for row in rows:
            safe = _safe_segment(row["label"])
            if not safe:
                items.append(
                    _unconvertible(
                        "mcp_server",
                        candidate,
                        str(row["label"]),
                        "unsafe_name",
                        "unusable server label",
                        namespace=namespace,
                        source_ref=_portable_ref(root, candidate, namespace),
                    )
                )
                continue
            if not row["command"]:
                items.append(
                    _unconvertible(
                        "mcp_server",
                        candidate,
                        safe,
                        "unparseable",
                        "no launch command",
                        namespace=namespace,
                        source_ref=_portable_ref(root, candidate, namespace),
                    )
                )
                continue
            namespaced = _prefixed(namespace, safe)
            items.append(
                ImportItem(
                    kind="mcp_server",
                    name=safe,
                    source_path=str(candidate),
                    target_path=str(destination.connectors_file),
                    action="converted",
                    detail=f"[mcp_servers.{namespaced}] command = {row['command'][:120]}",
                    active=True,
                    reaches=(f"/mcp {namespaced}", row["command"]),
                )
            )

    # -- reported-only kinds -------------------------------------------
    for rel in spec.reported_only:
        candidate = root / rel
        if not candidate.exists():
            continue
        items.append(
            ImportItem(
                kind=_reported_kind(rel),
                name=Path(rel).name or rel,
                source_path=str(candidate),
                target_path="",
                action="unconvertible",
                reason="unsupported_kind",
                detail=_reported_detail(rel),
            )
        )

    return items


def _reported_kind(rel: str) -> str:
    """Map a reported-only relative path onto a component kind."""
    head = rel.strip("/").split("/")[0].lower()
    if head in {"hooks", "hook"}:
        return "hook"
    return "settings"


def _reported_detail(rel: str) -> str:
    """Say WHY a kind is reported rather than converted, in one sentence."""
    head = rel.strip("/").split("/")[0].lower()
    if head in {"hooks", "hook"}:
        return (
            "hooks fire on events this import does not translate; add them with "
            "`neo hooks list` and edit by hand"
        )
    if head == "bin":
        return (
            "an executable cannot be converted to text; copy it and declare it yourself"
        )
    if head == "rules":
        return "per-rule instruction files have no Neo equivalent; merge them into AGENTS.md"
    if head == "extensions":
        return "Gemini extensions are a packaged bundle, not loose files"
    if head in {"monitors"}:
        return "monitors have no Neo equivalent"
    if head in {"lsp", ".lsp.json"}:
        return "the LSP config shape differs; copy it to .lsp.json by hand"
    if head in {"monitors", "bin"}:
        return "no Neo equivalent"
    return (
        "this is whole-application configuration (credentials, env and model "
        "routing); importing it would silently retarget your provider"
    )


def _reason_of(error: str) -> str:
    """Map a reader error onto a closed-set reason, defaulting to unparseable."""
    for reason in UNCONVERTIBLE_REASONS:
        if error.startswith(reason):
            return reason
    return "unparseable"


def _unconvertible(
    kind: str,
    path: Path,
    name: str,
    reason: str,
    detail: str,
    *,
    namespace: str = "",
    source_ref: str = "",
) -> ImportItem:
    """Build one unconvertible item with a reason from the closed set.

    ``name`` is the DECLARED name when the caller resolved one, so the report
    names the thing the user typed rather than the file on disk. Falling back
    to ``empty.md`` instead of ``empty`` is a report nobody can match against
    their own source tree.
    """
    return ImportItem(
        kind=kind,
        name=name or (path.name if path else "?"),
        source_path=str(path),
        target_path="",
        action="unconvertible",
        reason=reason,
        detail=detail,
        namespace=namespace,
        source_ref=source_ref,
    )


def _portable_ref(root: Path, path: Path, label: str) -> str:
    """Return ``<label>/<path relative to the source root>``.

    The reference written into a converted file. NEVER an absolute path:
    ``harness.skills``' untrusted-content boundary refuses a body carrying one
    (measured on this host - a ``C:\\Users\\...`` string makes the whole skill
    unloadable), and a committable ``.neo/skills`` tree with a home directory
    in it is a disclosure, not a provenance note.
    """
    try:
        relative = path.relative_to(root)
    except (ValueError, TypeError):
        return f"{label}/{path.name}"
    text = relative.as_posix()
    return f"{label}/{text}" if text else f"{label}/{path.name}"


def _frontmatter_name(candidate: Path) -> str:
    """Return a command file's declared name, or its stem.

    Frontmatter ``name`` wins over the filename because a foreign agent's own
    name is the name the user typed it under; the filename is only the
    fallback. A missing key falls through to the stem rather than to a
    stringified ``None`` - a command called ``claude-None`` is a name nobody
    typed and nobody can find.
    """
    text, _ = _read_text(candidate)
    if text is None:
        return candidate.stem
    meta, _ = _frontmatter(text)
    return str(meta.get("name") or "").strip() or candidate.stem


def _md_suffix(suffix: str) -> str:
    """Return the converted extension for a source extension."""
    return ".md"


def _command_detail(meta: Mapping[str, str], body: str) -> str:
    """One line describing a converted command."""
    head = next((line.strip() for line in body.splitlines() if line.strip()), "")
    if meta.get("description"):
        return f"{meta['description'][:70]} · {len(body.splitlines())} lines"
    return f"{head[:70]} · {len(body.splitlines())} lines" if head else "empty body"


def _subagent_detail(meta: Mapping[str, str], body: str) -> str:
    """One line describing a converted subagent, naming what did NOT convert."""
    tools = meta.get("tools", "")
    bits = [str(meta.get("description", "")).strip()[:60] or "no description"]
    if tools:
        bits.append(f"tools: {tools[:40]}")
    bits.append("model/tools re-scoped to Neo defaults (read it before enabling)")
    if not meta:
        bits.append("no frontmatter: name and model come from the filename")
    return " · ".join(bits)


def _has_mcp_servers_toml(text: str) -> bool:
    """True when a TOML document declares an ``[mcp_servers...]`` table."""
    return bool(re.search(r"(?m)^\s*\[mcp_servers", text))


def hash_tree(root: Any, *, limit: int = 20000) -> Dict[str, str]:
    """Hash every file under a directory, for a before/after immutability proof.

    Returns ``{relative path: sha256}``.  ``limit`` bounds the walk so a
    pathological source cannot make the proof itself the expensive thing;
    a truncated result says so through ``limit`` being reached, and the caller
    compares the same way twice so the comparison stays sound.
    """
    base = Path(root)
    out: Dict[str, str] = {}
    if not base.exists():
        return out
    count = 0
    for path in sorted(base.rglob("*")):
        if count >= limit:
            break
        try:
            if not path.is_file() or path.is_symlink():
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        try:
            rel = str(path.relative_to(base))
        except ValueError:
            continue
        out[rel] = digest
        count += 1
    return out


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


def _collision_report(
    items: Sequence[ImportItem], destination: Destination
) -> List[str]:
    """Return the target paths an apply would OVERWRITE, with their status."""
    collisions: List[str] = []
    for item in items:
        if item.action != "converted" or not item.target_path:
            continue
        target = Path(item.target_path)
        if target.exists():
            collisions.append(str(target))
    return collisions


def build_plan(
    argument: Any,
    *,
    repo_path: Optional[Any] = None,
    neo_home: Optional[Any] = None,
    cwd: Optional[Any] = None,
    namespace: Optional[str] = None,
) -> ImportPlan:
    """Build the whole plan for one source. PURE: writes nothing.

    This is the function a dry run, a trust scan and an apply all read, so
    the plan a user is shown before approving is byte-for-byte the plan the
    apply executes.
    """
    source = resolve_source(argument, cwd=cwd)
    label = namespace or source.label or "import"
    safe = namespace_for(label)
    destination = Destination.resolve(repo_path, neo_home=neo_home)
    if not source.exists:
        items = scan_source(source, destination, safe)
        return ImportPlan(
            source=source,
            namespace=safe,
            repo_path=str(destination.repo),
            items=tuple(items),
            errors=(source.detail,) if source.detail else (),
        )
    try:
        items = scan_source(source, destination, safe)
    except Exception as exc:  # defensive: one bad file must not take the CLI down
        return ImportPlan(
            source=source,
            namespace=safe,
            repo_path=str(destination.repo),
            errors=(f"scan failed: {exc}",),
        )
    return ImportPlan(
        source=source,
        namespace=safe,
        repo_path=str(destination.repo),
        items=tuple(items),
    )


# --------------------------------------------------------------------------
# The trust event
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BlastRadius:
    """What this import would add to the machine.

    A thin projection of ``cli.plugin_runtime.TrustReport`` rather than a
    second trust type: :func:`trust_lines` renders it by calling
    ``plugin_runtime.trust_lines`` on the report built here, so the ``/adopt``
    trust prompt and the ``/plugin`` trust prompt are one enumeration.
    """

    name: str
    root: str
    description: str
    executables: Tuple[str, ...]
    hooks: Tuple[Dict[str, str], ...]
    mcp_servers: Tuple[Dict[str, str], ...]
    commands: Tuple[str, ...]
    skills: Tuple[str, ...]
    agents: Tuple[str, ...]
    writable_outside: Tuple[str, ...]
    requires_review: bool
    #: What the source ships that this import deliberately did not convert.
    not_converted: Tuple[str, ...]
    notes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection with no live objects."""
        return {
            "name": self.name,
            "root": self.root,
            "description": self.description,
            "executables": list(self.executables),
            "hooks": [dict(row) for row in self.hooks],
            "mcp_servers": [dict(row) for row in self.mcp_servers],
            "commands": list(self.commands),
            "skills": list(self.skills),
            "agents": list(self.agents),
            "writable_outside": list(self.writable_outside),
            "requires_review": self.requires_review,
            "not_converted": list(self.not_converted),
            "notes": list(self.notes),
        }

    def as_trust_report(self) -> Any:
        """Return this as the plugin runtime's own ``TrustReport``.

        Deliberately the SHARED type: the renderer below reads it, so a change
        to the plugin trust prompt cannot leave ``/adopt`` rendering an older
        shape.
        """
        from cli import plugin_runtime

        return plugin_runtime.TrustReport(
            name=self.name,
            root=self.root,
            description=self.description,
            executables=self.executables,
            hooks=self.hooks,
            mcp_servers=self.mcp_servers,
            commands=self.commands,
            skills=self.skills,
            agents=self.agents,
            writable_outside=self.writable_outside,
            digest_ok=True,
        )


#: Command prefixes that fetch code from a network at launch.  Used only to
#: label a trust row's reach; a command that matches nothing is reported
#: ``unknown`` rather than ``local``, because "we did not recognise this" and
#: "this cannot leave the machine" are different claims.
_NETWORK_LAUNCHERS = (
    "npx",
    "uvx",
    "bunx",
    "pnpm dlx",
    "npm exec",
    "pipx run",
    "docker",
)


def _launch_reach(command: str) -> str:
    """Label what an MCP launch command can reach, honestly.

    ``network`` when it fetches code at launch, ``local`` when it names a bare
    executable on this machine, and ``unknown`` otherwise.  ``unknown`` is the
    default on purpose: a server whose reach nobody can determine is a
    question, not a permission.
    """
    text = str(command or "").strip()
    if not text:
        return "unknown"
    head = text.split()[0].lower()
    for marker in _NETWORK_LAUNCHERS:
        if head == marker or text.lower().startswith(marker + " "):
            return "network"
    if any(token in text.lower() for token in ("http://", "https://", "ws://")):
        return "network"
    return "local"


def blast_radius(plan: ImportPlan) -> BlastRadius:
    """Enumerate what an import would put on the machine, before any write.

    Computed from the PLAN, never from the destination: reading what is
    already installed would answer "what is true now", and the question here is
    "what would this add".
    """
    executables: List[str] = []
    mcp: List[Dict[str, str]] = []
    for item in plan.items:
        if item.kind == "mcp_server" and item.action == "converted":
            _, _, tail = (item.detail or "").partition("command = ")
            launch = tail.strip()
            mcp.append(
                {
                    "label": f"{plan.namespace}-{item.name}",
                    "launch": launch,
                    "reach": _launch_reach(launch),
                }
            )
        for reach in item.reaches:
            if reach.startswith("__executable__"):
                executables.append(reach.split(":", 1)[-1])
    return BlastRadius(
        name=f"adopted from {Path(plan.source.root).name or plan.source.label or 'a path'}",
        root=str(plan.source.root),
        description=plan.source.detail,
        executables=tuple(executables),
        hooks=(),
        mcp_servers=tuple(mcp),
        commands=tuple(
            f"/{Path(i.target_path).stem}"
            for i in plan.by_kind("command")
            if i.action == "converted"
        ),
        skills=tuple(
            Path(i.target_path).parent.name
            for i in plan.by_kind("skill")
            if i.action == "converted" and Path(i.target_path).name == "SKILL.md"
        ),
        agents=tuple(
            Path(i.target_path).stem
            for i in plan.by_kind("subagent")
            if i.action == "converted"
        ),
        writable_outside=(),
        # An MCP server definition, a hook or an executable is the trust event.
        requires_review=bool(mcp or executables),
        not_converted=tuple(
            f"{i.kind} {i.name}: {i.reason} ({i.detail})" for i in plan.unconvertible
        ),
        notes=(
            "the source directory was opened read-only and is unchanged",
            "converted commands and skills are namespaced, so your own still win",
        ),
    )


def trust_lines(plan: ImportPlan) -> List[str]:
    """Render the trust prompt through the plugin runtime's own renderer.

    DELEGATION, not a copy: ``cli.plugin_runtime.trust_lines`` is what
    ``/plugin trust`` renders, so an adoption prompt and an install prompt
    cannot enumerate differently.
    """
    from cli import plugin_runtime

    report = blast_radius(plan).as_trust_report()
    lines = list(plugin_runtime.trust_lines(report))
    extra = [i for i in blast_radius(plan).not_converted]
    if extra:
        lines.append("")
        lines.append(f"not converted ({len(extra)}):")
        lines.extend(f"  {row}" for row in extra)
    lines.append("")
    lines.append("the source directory is left byte-identical")
    return lines


def escape_lines(lines: Iterable[str]) -> List[str]:
    """Escape plain lines for a markup-parsing sink.

    Uses rich's own ``escape`` so the escaping cannot drift from the parser
    it defends.  The alternative exit is :func:`safe_lines`, which returns
    ``rich.text.Text`` and is never parsed at all.
    """
    from rich.markup import escape

    return [escape(str(line)) for line in lines]


def safe_lines(lines: Iterable[str]) -> List[Any]:
    """Return ``rich.text.Text`` lines: the structural no-markup exit."""
    from rich.text import Text

    return [Text(str(line)) for line in lines]


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def import_lines(plan: ImportPlan) -> List[str]:
    """Render the plan as PLAIN lines (a data field, never a markup string)."""
    source = plan.source
    out: List[str] = [f"source: {source.label or '(none)'} - {source.root}"]
    if source.detail:
        out.append(f"what:   {source.detail}")
    verb = "plan (nothing written)" if plan.dry_run else "apply"
    out.append(f"verb:   {verb}")
    out.append(f"into:   {plan.repo_path}")
    out.append(f"as:     {plan.namespace}-*  (namespaced; your own names still win)")

    radius = blast_radius(plan)
    if radius.requires_review:
        out.append("")
        out.append(f"trust:  needs review - {len(radius.mcp_servers)} MCP server(s)")
        out.extend(trust_lines(plan))

    for kind in COMPONENT_KINDS:
        rows = plan.by_kind(kind)
        if not rows:
            continue
        out.append("")
        out.append(f"{kind} ({len(rows)})")
        for item in rows:
            mark = "would write" if item.action == "converted" else item.action
            if item.action == "unconvertible":
                mark = f"NOT converted: {item.reason}"
            out.append(f"  {item.name} -> {mark}")
            if item.target_path:
                out.append(f"    to:   {item.target_path}")
            if item.detail:
                out.append(f"    note: {item.detail}")

    out.append("")
    out.append(
        f"summary: {len(plan.converted)} would be written, "
        f"{len(plan.skipped)} skipped, "
        f"{len(plan.unconvertible)} NOT converted"
    )
    if plan.unconvertible:
        out.append(
            "every unconverted item is listed above with its reason; nothing was "
            "dropped silently"
        )
    if plan.dry_run and plan.converted:
        out.append("")
        out.append("re-run with `apply` to write these")
    for error in plan.errors:
        out.append(f"error: {error}")
    return out


def adopt_lines(plan: ImportPlan, *, wrote: Sequence[str] = ()) -> List[str]:
    """Render the post-apply receipt: what was written, what was preserved."""
    if not wrote:
        return import_lines(plan)
    out = [f"imported: {len(wrote)} file(s) from {plan.source.root}"]
    out.append("")
    out.extend(import_lines(plan))
    out.append("")
    out.append(f"written ({len(wrote)}):")
    out.extend(f"  {row}" for row in wrote)
    out.append("")
    out.append("the source directory is unchanged; compare, then delete what you want")
    return out


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Write a file through a unique temp name and one rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.import-{os.getpid()}-{id(path)}"
    try:
        with open(tmp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


#: Keys a converted frontmatter block carries.  ``imported_from`` is a real
#: key rather than an HTML comment because every Neo loader that parses
#: frontmatter requires the block to be the FIRST thing in the file, and a
#: comment above it makes an otherwise-valid subagent unloadable.
CONVERTED_FRONTMATTER_KEYS = ("name", "description", "role", "model_tier", "tools")


def _converted_body(item: ImportItem, source_path: Path) -> bytes:
    """Build the bytes written for one converted item.

    The frontmatter block goes FIRST and carries the NAMESPACED name, because
    both facts are load-bearing: ``runtime.subagents._parse_markdown``
    requires the block to start the file (a comment above it makes a valid
    subagent unloadable, which this round measured), and an unnamespaced name
    would let an imported agent shadow the user's own.
    """
    kind = item.kind
    if kind in {"command", "subagent", "skill"}:
        text, error = _read_text(source_path)
        if text is None:
            raise OSError(error or "unreadable")
        meta, body = _frontmatter(text)
        namespaced = (
            _prefixed(item.namespace, item.name) if item.namespace else item.name
        )
        keep = {
            key: value
            for key, value in meta.items()
            if key in CONVERTED_FRONTMATTER_KEYS and str(value).strip()
        }
        keep["name"] = namespaced
        keep.setdefault("description", f"imported from {item.source_ref}")
        keep["imported_from"] = item.source_ref or item.source_path
        header = ["---"]
        header.extend(f"{k}: {v}" for k, v in keep.items())
        header.append("---")
        header.append("")
        header.append(f"<!-- neo adopt: converted from {item.source_ref} -->")
        header.append("<!-- the source file is unchanged; delete this copy freely -->")
        return ("\n".join(header) + "\n\n" + body).encode("utf-8", "replace")
    if kind == "instruction":
        text, error = _read_text(source_path)
        if text is None:
            raise OSError(error or "unreadable")
        return text.encode("utf-8", "replace")
    if kind == "mcp_server":
        return b""  # written by the connectors merge, not as a file
    raise OSError(f"no writer for kind {kind}")


#: The key the product's own connector reader expects a project declaration
#: under, and the header that introduces it.  Read from
#: ``cli.connectors`` rather than restated, so a rename there cannot leave this
#: writer appending a table nothing reads.
_CONNECTORS_HEADER = "mcp_servers"


def _toml_basic(value: str) -> str:
    """Quote a string as a TOML basic string.

    A launch command is DATA (it can contain quotes and backslashes), and a
    value that breaks the document's parse would make the whole connectors
    file unreadable rather than one row wrong.
    """
    text = str(value or "").replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\n", " ").replace("\r", " ").replace("\x00", "")
    return f'"{text}"'


def _connectors_header() -> str:
    """Return the ``[mcp_servers]`` header the product's reader expects.

    Read from ``cli.connectors`` when it publishes the name and fallen back to
    the literal when it does not. Only ``ImportError`` is caught, because that
    is the only failure a missing optional dependency can cause here; a broader
    catch would hide a real bug in the reader behind a silent default.
    """
    try:
        from cli import connectors
    except ImportError:  # pragma: no cover - the module ships in the wheel
        return _CONNECTORS_HEADER
    return str(getattr(connectors, "MCP_SERVERS_TABLE", "") or _CONNECTORS_HEADER)


def _server_labels(text: str) -> List[str]:
    """Return the ``[mcp_servers]`` labels already present in a document."""
    header = _connectors_header()
    out: List[str] = []
    inside = False
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if line.startswith("["):
            inside = line in {f"[{header}]"}
            continue
        if not inside or not line or line.startswith("#") or "=" not in line:
            continue
        key = line.partition("=")[0].strip().strip('"').strip("'")
        if key:
            out.append(key)
    return out


def _merge_connectors(path: Path, rows: Sequence[Tuple[str, str]]) -> List[str]:
    """Append ``[mcp_servers]`` rows to a connectors file, in place.

    Conservative by construction, for three reasons a reviewer would ask about:

    * an UNPARSEABLE existing file is REFUSED rather than appended to, because
      a half-merged TOML file breaks every reader in the product, not just
      this one;
    * every existing byte is preserved verbatim - rows are inserted under the
      existing table header when there is one, and the header is appended when
      there is not;
    * an already-present label is left byte-identical, so two applies are
      idempotent and a second run reports `preserved` rather than rewriting.
    """
    from cli.connectors import _parse_toml_text  # the reader, so the writer agrees

    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise OSError(f"cannot read {path}: {exc}") from exc
        if existing.strip() and _parse_toml_text(existing) is None:
            raise OSError(
                f"{path} is not parseable TOML; refusing to append to it"
            ) from None
    present = set(_server_labels(existing))
    pending = [(label, command) for label, command in rows if label not in present]
    added = [label for label, _ in pending]
    if not added:
        return []
    header = _connectors_header()
    new_lines = [
        f"{_toml_basic(label)} = {_toml_basic(command)}" for label, command in pending
    ]

    lines = existing.splitlines()
    insert_at = None
    for index, raw in enumerate(lines):
        if raw.strip() == f"[{header}]":
            insert_at = index + 1
    if insert_at is None:
        payload = existing
        if payload and not payload.endswith("\n"):
            payload += "\n"
        if payload.strip():
            payload += "\n"
        payload += f"[{header}]\n" + "\n".join(new_lines) + "\n"
    else:
        merged = lines[:insert_at] + new_lines + lines[insert_at:]
        payload = "\n".join(merged)
        if not payload.endswith("\n"):
            payload += "\n"
    _atomic_write_bytes(path, payload.encode("utf-8"))
    return added


def apply_plan(plan: ImportPlan) -> Tuple[List[str], List[str], List[str]]:
    """Write a plan. Returns ``(written, preserved, errors)``.

    The source is never opened for writing: every destination is resolved from
    :class:`Destination`, and a test hashes the source tree before and after.
    An existing destination file is PRESERVED byte-identically unless the plan
    was built with the explicit ``overwrite`` decision recorded in
    ``plan.errors``; the default is preserve-and-report, because an import that
    silently replaces a user's own command is the worst thing this tool could
    do.
    """
    written: List[str] = []
    preserved: List[str] = []
    errors: List[str] = []
    connector_rows: List[Tuple[str, str]] = []
    connectors_file: Optional[Path] = None

    for item in plan.converted:
        target = Path(item.target_path)
        if item.kind == "mcp_server":
            _, _, tail = (item.detail or "").partition("command = ")
            connector_rows.append((f"{plan.namespace}-{item.name}", tail.strip()))
            connectors_file = target
            continue
        if target.exists():
            preserved.append(str(target))
            continue
        try:
            payload = _converted_body(item, Path(item.source_path))
        except OSError as exc:
            errors.append(f"{item.name}: {exc}")
            continue
        try:
            _atomic_write_bytes(target, payload)
        except OSError as exc:
            errors.append(f"{item.name}: {exc}")
            continue
        written.append(str(target))

    if connector_rows and connectors_file is not None:
        try:
            added = _merge_connectors(connectors_file, connector_rows)
        except OSError as exc:
            errors.append(str(exc))
        else:
            written.extend(f"{connectors_file} [{label}]" for label in added)
    return written, preserved, errors


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportReceipt:
    """The receipt every surface renders. ``lines`` is NEVER empty."""

    ok: bool
    verb: str
    lines: List[str] = field(default_factory=list)
    payload: Dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        """Truthy when the command acted, matching the other verb surfaces."""
        return bool(self.ok)


def _usage(source: Optional[SourceRef] = None) -> List[str]:
    """The one usage block, listing the valid verbs."""
    lines = [
        f"usage: {ADOPT_COMMAND_NAME} [plan|apply|scan|list] [claude|codex|gemini|<path>]"
    ]
    if source is not None and source.label:
        lines.append(f"source: {source.label} - {source.detail}")
    lines.append("  plan   print what would be written and write nothing (the default)")
    lines.append("  apply  write the converted copy; the source is never moved")
    lines.append("  scan   show only the trust report, no plan, no writes")
    lines.append(
        f"  list   the recognised sources: {', '.join(sorted(SOURCE_CATALOG))} + <path>"
    )
    return lines


def adopt_command(
    verb: Any = "",
    rest: Any = "",
    *,
    repo_path: Optional[Any] = None,
    neo_home: Optional[Any] = None,
    cwd: Optional[Any] = None,
    approve: Optional[Callable[[BlastRadius], bool]] = None,
    namespace: Optional[str] = None,
) -> ImportReceipt:
    """The ONE implementation of every ``/adopt`` verb.

    ``verb`` is one of :data:`ADOPT_VERBS`; ``rest`` is the source argument.
    ``approve`` is INJECTED because this module never prompts: a caller that
    can ask passes a callable, and a caller that cannot leaves it ``None`` -
    which REFUSES a trust-requiring import rather than proceeding. The safe
    direction is the default, so a script cannot accidentally approve on a
    user's behalf.

    Returns an :class:`ImportReceipt` whose ``lines`` are always non-empty; an
    empty render is indistinguishable from a verb that does not exist.
    """
    name = str(verb or "").strip().lower()
    argument = str(rest or "").strip()
    if not name:
        # `/adopt` bare is the plan, which is the safe reading; a bare
        # `/adopt apply` still needs a source and says so.
        name = "apply" if argument.split()[:1] == ["apply"] else "plan"
    if name not in ADOPT_VERBS:
        return ImportReceipt(
            ok=False,
            verb=name or "(none)",
            lines=[f"unknown verb: {name}", *_usage()],
            payload={
                "verb": name,
                "ok": False,
                "valid_verbs": list(ADOPT_VERBS),
                "error": "unknown_verb",
            },
        )

    if name == "list":
        lines = ["recognised sources:"]
        for label, spec in sorted(SOURCE_CATALOG.items()):
            resolved = resolve_source(label, cwd=cwd)
            lines.append(f"  {label:<7} {spec.summary}")
            lines.append(
                f"          {'found' if resolved.exists else 'not found'}: {resolved.root}"
            )
        lines.append(f"  <path>  {GENERIC_LAYOUT.summary}")
        return ImportReceipt(
            ok=True,
            verb=name,
            lines=lines,
            payload={"verb": name, "ok": True, "sources": sorted(SOURCE_CATALOG)},
        )

    if not argument:
        return ImportReceipt(
            ok=False,
            verb=name,
            lines=[f"{name} needs a source", *_usage()],
            payload={"verb": name, "ok": False, "error": "missing_source"},
        )

    # `/adopt apply claude --dry-run` and `--yes` are accepted and MEANINGFUL:
    # `--dry-run` downgrades an apply to a plan, `--yes` is an approval for a
    # non-interactive caller and is recorded as such in the receipt.
    tokens = argument.split()
    flags = {t.lower() for t in tokens if t.startswith("--")}
    positional = [t for t in tokens if not t.startswith("--")]
    if len(positional) > 1:
        return ImportReceipt(
            ok=False,
            verb=name,
            lines=[
                f"one source at a time; got {len(positional)}: {' '.join(positional)}",
                *_usage(),
            ],
            payload={"verb": name, "ok": False, "error": "too_many_sources"},
        )
    source_arg = positional[0] if positional else ""
    if "--dry-run" in flags:
        name = "plan"
    auto_approved = "--yes" in flags or "--assume-yes" in flags

    plan = build_plan(
        source_arg,
        repo_path=repo_path,
        neo_home=neo_home,
        cwd=cwd,
        namespace=namespace,
    )
    radius = blast_radius(plan)

    if name == "scan":
        lines = [
            f"blast radius for {plan.source.label or plan.source.root}",
            *trust_lines(plan),
        ]
        return ImportReceipt(
            ok=True,
            verb="scan",
            lines=lines,
            payload={
                "verb": "scan",
                "ok": True,
                "source": plan.source.to_dict(),
                "blast_radius": radius.to_dict(),
                "requires_review": radius.requires_review,
            },
        )

    if name == "plan":
        return ImportReceipt(
            ok=bool(plan.converted),
            verb="plan",
            lines=import_lines(plan),
            payload={
                "verb": "plan",
                "ok": True,
                "wrote_nothing": True,
                "source": plan.source.to_dict(),
                "requires_review": radius.requires_review,
                "plan": plan.to_dict(),
            },
        )

    # -- apply ----------------------------------------------------------
    if radius.requires_review:
        approved = False
        decided_by = "auto_flag" if auto_approved else "not_approved"
        if auto_approved:
            approved = True
        elif approve is not None:
            try:
                approved = bool(approve(radius))
            except Exception as exc:  # an approver that raises is a refusal
                decided_by = f"approver raised: {exc}"
            if approved:
                decided_by = "approver"
        plan = ImportPlan(
            source=plan.source,
            namespace=plan.namespace,
            repo_path=plan.repo_path,
            items=plan.items,
            dry_run=False,
            errors=plan.errors,
            approval=decided_by,
        )
        if not approved:
            return ImportReceipt(
                ok=False,
                verb="apply",
                lines=[
                    "refused: this source needs a trust decision; nothing was written",
                    *trust_lines(plan),
                    "",
                    "re-run with an approval, or inspect with: "
                    f"{ADOPT_COMMAND_NAME} scan {plan.source.label or plan.source.root}",
                ],
                payload={
                    "verb": "apply",
                    "ok": False,
                    "error": "trust_not_approved",
                    "approval": decided_by,
                    "requires_review": True,
                    "wrote_nothing": True,
                    "source": plan.source.to_dict(),
                    "blast_radius": radius.to_dict(),
                },
            )

    written, preserved, errors = apply_plan(plan)
    applied = ImportPlan(
        source=plan.source,
        namespace=plan.namespace,
        repo_path=plan.repo_path,
        items=plan.items,
        dry_run=False,
        written=tuple(written),
        preserved=tuple(preserved),
        errors=tuple(list(plan.errors) + errors),
        approval=plan.approval,
    )
    # An apply that completed without error is ok even when it wrote nothing:
    # a second apply of the same source is idempotent (everything `preserved`),
    # and reporting that as a failure would teach people to reach for a force
    # flag. What is NOT ok is an error, or a source with nothing convertible.
    ok = not errors and bool(plan.converted)
    lines = adopt_lines(applied, wrote=written)
    if not plan.converted:
        lines.append("")
        lines.append("nothing was convertible from this source; nothing was written")
    if preserved:
        lines.append("")
        lines.append(f"left alone, already present ({len(preserved)}):")
        lines.extend(f"  {row}" for row in preserved)
    if errors:
        lines.append("")
        lines.append(f"errors ({len(errors)}):")
        lines.extend(f"  {row}" for row in errors)
    return ImportReceipt(
        ok=ok,
        verb="apply",
        lines=lines,
        payload={
            "verb": "apply",
            "ok": ok,
            "written": list(written),
            "preserved": list(preserved),
            "errors": list(errors),
            "approval": applied.approval,
            "source": plan.source.to_dict(),
            "requires_review": radius.requires_review,
            "counts": {
                "converted": len(applied.converted),
                "unconvertible": len(applied.unconvertible),
                "written": len(written),
            },
        },
    )


def _dispatch(verb: str, rest: str, **kwargs: Any) -> ImportReceipt:
    """The one verb -> implementation mapping.

    A dict, not an ``if verb ==`` ladder: a ladder is where a second
    implementation grows, and Terminal 07's AST gate exists for exactly that.
    Every verb here reaches :func:`adopt_command`, which is the implementation.
    """
    handlers: Mapping[str, Callable[..., ImportReceipt]] = {
        "plan": lambda: adopt_command("plan", rest, **kwargs),
        "apply": lambda: adopt_command("apply", rest, **kwargs),
        "scan": lambda: adopt_command("scan", rest, **kwargs),
        "list": lambda: adopt_command("list", "", **kwargs),
    }
    handler = handlers.get(verb)
    if handler is None:
        return adopt_command(verb, rest, **kwargs)
    return handler()


# --------------------------------------------------------------------------
# The registry row
# --------------------------------------------------------------------------


def _build_spec() -> Any:
    """Build the ``/adopt`` ``CommandSpec`` using the registry's own class.

    Built through ``cli.commands.CommandSpec`` so the row is one the registry
    will ACCEPT (its ``__post_init__`` validates every policy, permission and
    recovery action) rather than a lookalike that could drift from it.
    """
    return _commands.CommandSpec(
        name=ADOPT_COMMAND_NAME,
        summary="convert another agent's setup into a namespaced copy",
        aliases=ADOPT_ALIASES,
        argument_policy="optional",
        argument_hint=f"[{ADOPT_VERBS[0]}|{ADOPT_VERBS[1]}|{ADOPT_VERBS[2]}|{ADOPT_VERBS[3]}] "
        "[claude|codex|gemini|<path>] [--dry-run|--yes]",
        idle_policy="allow",
        # Adopt is the control a person reaches for when they are moving a
        # whole setup over, and it must be reachable while a run is live.
        in_flight_policy="allow",
        palette_behavior="run",
        required_permissions=("extension:read", "workspace:write"),
        result_presentation="browser",
        failure_recovery=("edit-input", "return-safe-state"),
        headless_policy=ADOPT_HEADLESS_POLICY,
        command_type="local",
    )


#: The registry row, ready to append to ``cli/commands.py::COMMAND_SPECS``.
#: It is a real ``CommandSpec`` built by the registry's own class, so
#: appending it cannot fail ``CommandSpec.__post_init__``.
ADOPT_SPEC = _build_spec()

#: The two registry rows the import-time validators in ``cli/commands.py``
#: also require.  Declared here so the handoff is a paste, and pinned by a
#: test that they are mutually consistent.
ADOPT_HEADLESS_ROW = (ADOPT_COMMAND_NAME, ADOPT_HEADLESS_POLICY)
ADOPT_FLAG_ROW = (ADOPT_COMMAND_NAME, ADOPT_FLAG_EQUIVALENT)


def register_adopt_parser(sub: Any) -> Any:
    """Add ``neo adopt`` to an argparse subparser group. DELEGATES.

    The script surface exists so a CI job can adopt a setup, and it calls
    :func:`adopt_command` - the same function the slash verb calls.  The
    return code is the shared exit-code vocabulary
    (``cli.exit_codes.EXIT_CODES``): 0 acted, 2 a usage error, 3 an
    environment error, 1 a failed action.

    NOT MOUNTED: ``cli/main.py`` is another terminal's live file. The one-line
    mount is ``register_adopt_parser(sub)`` next to the existing
    ``capability.register_commands(...)`` call at the end of ``build_parser``,
    and it is filed in the module handoff rather than applied.
    """
    parser = sub.add_parser(
        "adopt",
        help="convert another agent's setup into a namespaced copy (never moves the source)",
        description=(
            "Read another agent's configuration and write a converted, namespaced "
            "copy into this repository. The source directory is opened read-only "
            "and is left byte-identical. Every item that cannot be converted is "
            "reported with a reason."
        ),
    )
    parser.add_argument(
        "verb",
        nargs="?",
        default="plan",
        choices=list(ADOPT_VERBS),
        help=f"what to do (default: {ADOPT_VERBS[0]}, which writes nothing)",
    )
    parser.add_argument(
        "source",
        nargs="?",
        default="",
        help="claude | codex | gemini | a path to a directory",
    )
    parser.add_argument(
        "--repo", default=None, help="destination repository (default: cwd)"
    )
    parser.add_argument(
        "--namespace", default=None, help="override the conversion namespace"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="force a plan even when the verb is apply",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="approve a trust-requiring source without asking (recorded in the receipt)",
    )
    parser.add_argument("--json", action="store_true", help="print one JSON document")

    def _handler(args: Any) -> int:
        from cli import exit_codes

        verb = "plan" if getattr(args, "dry_run", False) else (args.verb or "plan")
        receipt = adopt_command(
            verb,
            args.source or "",
            repo_path=args.repo,
            cwd=args.repo,
            namespace=args.namespace,
            approve=(lambda _radius: True) if getattr(args, "yes", False) else None,
        )
        if getattr(args, "json", False):
            print(json.dumps(receipt.payload, indent=2, sort_keys=True, default=str))
        else:
            for line in receipt.lines:
                print(line)
        if receipt.ok:
            return int(exit_codes.EXIT_CODES["success"])
        error = str(receipt.payload.get("error") or "")
        if error in {"missing_source", "unknown_verb", "too_many_sources"}:
            return int(exit_codes.EXIT_CODES["usage_error"])
        if error == "trust_not_approved":
            return int(exit_codes.EXIT_CODES["environment_error"])
        return int(exit_codes.EXIT_CODES["task_failure"])

    parser.set_defaults(func=_handler)
    return parser


# --------------------------------------------------------------------------
# Self-checks the module runs against its own source
# --------------------------------------------------------------------------

#: Completion vocabulary this module must never carry.  A cost receipt or a
#: trust report that a verifier could read is a cost that becomes a success
#: criterion.
FORBIDDEN_VOCABULARY = (
    "completed_verified",
    "completed_unverified",
    "status_is_success",
    "run_verdict",
    "agent_contracts",
)


def self_check() -> List[str]:
    """Return the module's own violations of its declared rules.

    Read from the SOURCE with ``ast``, so a reformat cannot empty the check
    and a comment cannot make it pass.  Empty means clean.
    """
    problems: List[str] = []
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    # The forbidden words appear in this function AND in the FORBIDDEN_
    # VOCABULARY declaration itself, by construction. Both are excluded by
    # NESTING, not by string matching, so a second occurrence elsewhere is
    # still found: an exemption list keyed on text is a place for the next
    # occurrence to hide.
    exempt: set = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "self_check"
        ):
            exempt.update(id(child) for child in ast.walk(node))
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
            if any(t.id == "FORBIDDEN_VOCABULARY" for t in targets):
                exempt.update(id(child) for child in ast.walk(node.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in exempt:
                continue
            if node.value in FORBIDDEN_VOCABULARY:
                problems.append(
                    f"line {node.lineno}: completion vocabulary {node.value!r}"
                )
    # The module must not import the TUI, which another terminal owns.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"cli.tui", "textual"}:
                    problems.append(f"line {node.lineno}: imports {alias.name}")
        if isinstance(node, ast.ImportFrom) and (node.module or "") in {
            "cli.tui",
            "textual",
        }:
            problems.append(f"line {node.lineno}: imports {node.module}")
    return problems
