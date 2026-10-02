"""Skills system (Task A): auto-invoked markdown instruction packs.

A skill is a folder containing a SKILL.md file — the same format the
environment itself uses for its built-in skills, copied deliberately
rather than reinvented:

    <skill-dir>/<name>/SKILL.md
    ---
    name: <skill name>            (frontmatter; falls back to the folder
    description: <when it applies>  name when absent)
    ---
    (body: the actual instructions / best practices)

Locations (both scanned; project skills win on name collisions —
committed, team-shared knowledge is more specific than personal):
  project:  <repo>/.neo/skills/<name>/SKILL.md
  global:   ~/.config/neo/skills/<name>/SKILL.md
  plugin:   ~/.config/neo/plugins/<plugin>/skills/<name>/SKILL.md
  (custom roots via config "skills_roots" — tests/CLI use it; the
   default roots are always included)

Auto-invocation: before planning a fix, the harness scans available
skills' DESCRIPTIONS against the task (issue text + repo vocabulary +
retrieval terms) and reads the full SKILL.md for any that plausibly
apply — mirroring how skills are actually used in practice, not stored
inertly. Matching is deliberately conservative (keyword/word-overlap,
no embeddings — same honesty as decision memory): a skill applies when
its description shares meaningful words with the task; the matched
body is injected into the planner prompt as a `## Applicable skills`
section placed AFTER `## Retrieved context` (the same placement
discipline as memory/coordination: Terminal 3's difficulty predictor
cuts the first user message at that marker, so skills must not shift
difficulty scoring — regression-tested from the runtime side).

Everything here is best-effort BY DESIGN: a missing/unreadable/malformed
skill degrades to "not applicable" and planning proceeds exactly as
before (trace event, never a crash — planning must not die over a
malformed markdown file).

TRUST BOUNDARY: a SKILL.md is untrusted content. Project, global, and plugin
skill bodies are all authored outside the harness, and the harness injects the
matching body verbatim into the planner prompt. Every parsed body therefore
passes `shared.security.review_untrusted_source(source="skill")` (fail-closed)
before it is admitted: a body that tries to issue instructions is quarantined
(excluded from the prompt, reported in the receipt), and a body that merely
trips a rule is admitted with its taint visible in the rendered block. The
per-skill review record is carried on the Skill object and surfaced in
`build_skill_receipt` so a plan's skill injection is auditable.

Config (task.config, defaults in harness/config.py):
  skills_enabled      master switch (False = skip the scan entirely)
  skills_max          max skills injected into one plan (default 3)
  skills_max_chars    combined char cap on the section (default 2500)
  skills_roots        extra skill search roots (list; default roots
                      are always scanned too; plugins/tests use this)
  skills_untrusted_mode  per-source policy override for the skill boundary
                      ("block" default | "quarantine" | "flag" | "allow")

Trace: one `skills` event per plan {matched: [names], considered: N,
section_chars: M, skipped?: reason} — the scan is auditable even when
nothing applies (matched: [] + the reason), like decision_memory.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from shared.security import (
    DEFAULT_UNTRUSTED_MAX_CHARS,
    review_untrusted_source,
    taint_wrap,
)

__all__ = [
    "CATALOG_KINDS",
    "COMMAND_SOURCE_KINDS",
    "MAX_STORED_VISIBILITIES",
    "NONE_MATCHED",
    "PLUGIN_NAMESPACE_SEPARATOR",
    "SKILL_VISIBILITIES",
    "VISIBILITY_STORE_VERSION",
    "CommandSource",
    "Skill",
    "attach_declarations",
    "build_skill_receipt",
    "command_sources",
    "discover_skills",
    "estimate_tokens",
    "explain_skill_matches",
    "find_applicable_skills",
    "load_visibility",
    "plugin_name_for",
    "progressive_disclosure_report",
    "render_skills_block",
    "resolve_support_file",
    "scan_skills_for_task",
    "skill_catalog",
    "skill_token_cost",
    "supporting_files",
    "visibility_store_path",
]

# Frontmatter: a leading `---` block of `key: value` lines. Tolerates a
# missing frontmatter (body-only SKILL.md still loads; name falls back
# to the folder name, description to "").
_FM_BOUNDARY = re.compile(r"^---\s*$")
_FM_LINE = re.compile(r"^([A-Za-z][\w-]*)\s*:\s*(.*)$")
_FM_ITEM = re.compile(r"^\s+-\s+(.*)$")

# Safety cap on ONE SKILL.md body read (a pathological/huge skill must
# not blow the harness's memory, let alone the prompt).
_MAX_BODY_CHARS = 20_000
_MAX_SKILL_FILE_BYTES = 64_000

#: The rendered block when a scan matched nothing. Named because it is a
#: CONTRACT, not a cosmetic string: it is the only text a model can receive
#: instead of a skill, so a receipt that mistakes it for a delivered skill
#: claims model content that does not exist. `build_skill_receipt` and the
#: compiled-bundle receipt in `harness/knowledge.py` both read this.
NONE_MATCHED = "(none matched)"

_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "when",
        "this",
        "that",
        "is",
        "are",
        "be",
        "been",
        "was",
        "it",
        "its",
        "as",
        "at",
        "by",
        "from",
        "into",
        "if",
        "then",
        "than",
        "so",
        "such",
        "use",
        "used",
        "using",
        "uses",
        "but",
        "not",
        "no",
        "do",
        "does",
        "did",
        "how",
        "what",
        "which",
        "who",
        "will",
        "would",
        "should",
        "can",
        "could",
        "may",
        "might",
        "must",
        "shall",
        "about",
        "over",
        "under",
        "between",
        "you",
        "your",
        "we",
        "our",
        "they",
        "their",
        "them",
        "these",
        "those",
        "any",
        "all",
        "some",
        "more",
        "most",
        "other",
        "another",
        "each",
        "every",
        "both",
        "few",
        "many",
        "much",
        "own",
        "same",
        "too",
        "very",
        "just",
        "also",
        "only",
        "per",
        "via",
        "etc",
        "e",
        "g",
        # task-domain words that appear in nearly EVERY issue and every
        # skill description — zero discriminative signal for matching
        # (a skill must match on its SUBJECT, not on "fix the bug"):
        "fix",
        "fixes",
        "fixing",
        "fixed",
        "bug",
        "bugs",
        "error",
        "errors",
        "fail",
        "fails",
        "failing",
        "failed",
        "failure",
        "task",
        "tasks",
        "issue",
        "issues",
        "repo",
        "repos",
        "repository",
        "project",
        "projects",
        "code",
        "best",
        "practice",
        "practices",
        "convention",
        "conventions",
        "behavior",
        "test",
        "tests",
        "testing",
        "python",
    }
)


class Skill:
    """One parsed SKILL.md (duck-typed data holder; never raises).

    Attributes: name, description (frontmatter; name falls back to
    the folder name), body (the markdown instructions after the frontmatter,
    capped at _MAX_BODY_CHARS), source (the file's path string), origin
    ("project" | "global" | "plugin" | "extra" — for prompt attribution
    and trace events). ``review`` is the untrusted-content review record for
    the body (``None`` only when a caller constructed a Skill by hand); it is
    what lets the renderer show taint and the receipt report a quarantine.

    The four ``version``/``model_tier``/``permission_claims``/``declared_tools``
    fields are the Ceiling-12 versioned declaration (frontmatter ``version``,
    ``model-tier``, ``permissions``, ``tools``). They are inert data on this
    object: enforcement lives in :mod:`extensions.skill_policy`, which
    intersects them with the parent session's permission envelope. A skill can
    therefore *declare* anything, and nothing here grants it.
    """

    __slots__ = (
        "body",
        "declared_tools",
        "description",
        "model_tier",
        "name",
        "origin",
        "permission_claims",
        "review",
        "source",
        "version",
    )

    def __init__(
        self,
        name: str,
        description: str,
        body: str,
        source: str,
        origin: str,
        review: Any = None,
        version: int = 1,
        model_tier: str = "",
        permission_claims: Any = None,
        declared_tools: Any = None,
    ) -> None:
        self.name = name
        self.description = description
        self.body = body
        self.source = source
        self.origin = origin
        self.review = review
        self.version = int(version or 1)
        self.model_tier = str(model_tier or "")
        self.permission_claims = tuple(permission_claims or ())
        self.declared_tools = tuple(declared_tools or ())

    @property
    def tainted(self) -> bool:
        """Return whether the body tripped the untrusted-content review."""
        return bool(getattr(self.review, "tainted", False))


def _has_symlink_component(path: Path) -> bool:
    try:
        current = Path(path)
        while True:
            if current.is_symlink():
                return True
            parent = current.parent
            if parent == current:
                return False
            current = parent
    except (OSError, RuntimeError, ValueError):
        return True


def _parse_skill_md(
    path: Path, origin: str, *, untrusted_mode: Optional[str] = None
) -> Optional[Skill]:
    """Parse one SKILL.md into a Skill; None when unreadable/empty/quarantined.

    Never raises: OSError/UnicodeDecodeError/malformed frontmatter all
    degrade to None (the skill is skipped; discovery must not die over
    one bad markdown file). A body that the untrusted-content boundary
    quarantines also degrades to None — the skill is dropped rather than
    partially admitted, because half an instruction pack is still an
    instruction pack.
    """
    try:
        if path.is_symlink() or _has_symlink_component(path.parent):
            return None
        size = path.stat().st_size
        with path.open("rb") as handle:
            raw = handle.read(min(_MAX_SKILL_FILE_BYTES, max(1024, size)))
        if b"\x00" in raw:
            return None
        text = raw.decode("utf-8", errors="replace")
    except (OSError, UnicodeError):
        return None
    lines = text.splitlines()
    meta: Dict[str, str] = {}
    body_start = 0
    if lines and _FM_BOUNDARY.match(lines[0]):
        closing = None
        last_key = ""
        for i in range(1, len(lines)):
            if _FM_BOUNDARY.match(lines[i]):
                closing = i
                break
            m = _FM_LINE.match(lines[i])
            if m:
                last_key = m.group(1).lower()
                meta[last_key] = m.group(2).strip()
                continue
            # YAML block sequences: `permissions:` / `tools:` followed by
            # indented `- item` lines. Only those two keys accept a block, so a
            # `- item` under any other key is ignored rather than absorbed.
            item = _FM_ITEM.match(lines[i])
            if item and last_key in ("permissions", "tools"):
                meta[last_key] = (meta[last_key] + "\n" + item.group(1)).strip()
        if closing is None:
            return None
        body_start = closing + 1
    name = meta.get("name") or path.parent.name
    description = meta.get("description") or ""
    body = "\n".join(lines[body_start:]).strip()
    if len(body) > _MAX_BODY_CHARS:
        marker = "\n... [skill body truncated]"
        body = body[: max(0, _MAX_BODY_CHARS - len(marker))].rstrip() + marker
    if not body:
        return None
    # Untrusted-content boundary. The body is what gets injected into the
    # planner prompt, so it is reviewed here — the first point at which the
    # skill's own instructions are known — rather than at render time.
    review = review_untrusted_source(
        body,
        source="skill",
        mode=untrusted_mode,
        max_chars=max(_MAX_BODY_CHARS, DEFAULT_UNTRUSTED_MAX_CHARS),
    )
    if review.blocked:
        return None
    return Skill(
        name=str(name)[:80],
        description=str(description)[:1000],
        body=review.text,
        source=str(path),
        origin=origin,
        review=review,
        version=_declaration_version(meta),
        model_tier=str(meta.get("model-tier") or meta.get("model_tier") or "")[:40],
        permission_claims=_declaration_claims(meta),
        declared_tools=_declaration_tools(meta),
    )


def _declaration_version(meta: Dict[str, str]) -> int:
    """Return the frontmatter ``version`` as a positive int (1 when unusable).

    A skill file is untrusted content: a malformed version must not break
    discovery, so an unparseable value is reported as version 1 and the
    declaration layer records the anomaly rather than raising.
    """
    raw = str(meta.get("version") or "").strip()
    if not raw:
        return 1
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


def _declaration_items(raw: str) -> tuple:
    """Split a frontmatter declaration value into a tuple of items.

    Accepts a JSON list/object, a YAML block sequence, and a comma/semicolon
    separated inline list. Returns an empty tuple for anything else so the
    policy layer sees "no claims" instead of a string it would have to guess at.
    """
    text = str(raw or "").strip()
    if not text:
        return ()
    if text.startswith("[") or text.startswith("{"):
        try:
            import json

            loaded = json.loads(text)
        except ValueError:
            loaded = None
        if isinstance(loaded, list):
            return tuple(item for item in loaded if item is not None)
        if isinstance(loaded, dict):
            return (loaded,)
    items = []
    for line in text.splitlines():
        for part in line.split(","):
            # Strip a YAML FLOW-list bracket from each end. `tools: [read,
            # task]` is the idiomatic one-line authoring form and it is not
            # JSON, so it misses the loader above and would otherwise arrive
            # as the two nonsense names "[read" and "task]" - a declared tool
            # that matches no profile, for a reason nobody could see.
            candidate = part.strip().strip("-").strip().strip("[]").strip()
            candidate = candidate.strip("\"'").strip()
            if candidate:
                items.append(candidate)
    return tuple(items)


def _declaration_claims(meta: Dict[str, str]) -> tuple:
    """Return the frontmatter ``permissions`` list as raw claim values.

    Parsing is deliberately deferred to ``extensions.skill_policy`` so the
    permission vocabulary has exactly one implementation. This returns the
    author text verbatim (bounded) and lets the policy layer decide what is
    admissible.
    """
    return _declaration_items(meta.get("permissions", ""))


def _declaration_tools(meta: Dict[str, str]) -> tuple:
    """Return the frontmatter ``tools`` list (CSV, YAML block, or JSON)."""
    return _declaration_items(meta.get("tools", ""))


def _plugin_skill_roots() -> List[tuple[Path, str, str]]:
    """Return every enabled plugin's skills root as ``(path, plugin, origin)``.

    Separate from :func:`discover_skills` for one reason that matters: normal
    discovery DEDUPES on the bare name, so a plugin skill whose name collides
    with a project skill is gone before anybody can namespace it. Namespacing
    exists precisely to stop a plugin from taking (or being taken over by) a
    bare name, so the plugin half has to be discoverable INDEPENDENTLY of the
    bare-name precedence rule - which is what this scan is for.
    """
    out: List[tuple[Path, str, str]] = []
    global_roots = [_global_config_root()]
    legacy_root = _legacy_global_root()
    if legacy_root is not None and legacy_root != global_roots[0]:
        global_roots.append(legacy_root)
    for global_root in global_roots:
        plugins_root = global_root / "plugins"
        if not plugins_root.is_dir() or _has_symlink_component(plugins_root):
            continue
        try:
            plugin_dirs = sorted(plugins_root.iterdir())
        except OSError:
            continue
        for plugin_dir in plugin_dirs:
            if not plugin_dir.is_dir() or plugin_dir.name.startswith("."):
                continue
            if _has_symlink_component(plugin_dir):
                continue
            try:
                if (plugin_dir.parent / f"{plugin_dir.name}.disabled").is_file():
                    continue
            except OSError:
                pass
            out.append((plugin_dir / "skills", plugin_dir.name, "plugin"))
    return out


def _global_config_root() -> Path:
    override = os.environ.get("NEO_GLOBAL_ROOT")
    if override:
        return Path(override).expanduser()
    config = os.environ.get("NEO_CONFIG")
    if config:
        return Path(config).expanduser().parent
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "neo"
    if os.name == "nt":
        return Path.home() / "AppData" / "Roaming" / "neo"
    return Path.home() / ".config" / "neo"


def _global_skills_root() -> Path:
    """Return the platform-appropriate global Neo skills directory."""
    return _global_config_root() / "skills"


def _global_plugins_root() -> Path:
    return _global_config_root() / "plugins"


def _legacy_global_root() -> Optional[Path]:
    if os.environ.get("NEO_GLOBAL_ROOT"):
        return None
    return Path.home() / ".config" / "neo"


def discover_skills(
    repo_path: Optional[str] = None,
    extra_roots: Optional[List[str]] = None,
    diagnostics: Optional[List[Dict[str, Any]]] = None,
    *,
    untrusted_mode: Optional[str] = None,
    hidden_names: Optional[Iterable[str]] = None,
) -> List[Skill]:
    """Scan project, global, plugin, and extra skill locations.

    Roots are evaluated in project > global > plugin > extra order. The
    optional ``diagnostics`` list receives malformed or unreadable existing
    skill files without making discovery fail. ``untrusted_mode`` overrides
    the skill trust policy for this scan; a body the boundary quarantines is
    recorded in ``diagnostics`` and excluded, so a hostile SKILL.md is an
    auditable skip rather than a silent omission.

    ``hidden_names`` is the VISIBILITY ceiling: a skill whose name (or
    namespaced ``plugin:name``) appears there is not discovered at all, so
    "hidden from the model" is enforced where the model is handed the list
    rather than filtered at the render.

    Passing ``hidden_names=None`` (the default) means "no caller ceiling",
    and the PERSISTED store is consulted instead - so a skill a person hid is
    absent from every scan, including ones this round did not touch. Passing
    an explicit iterable OVERRIDES the store, which is what a test and a
    caller with its own ceiling need. An install with no store and no
    argument sees exactly what it always saw.
    """
    if hidden_names is None:
        # `load_visibility` returns (mapping, note); a scan wants the names.
        stored, _note = load_visibility(repo_path)
        hidden = set(stored)
    else:
        hidden = {
            str(item).strip()
            for item in (hidden_names or ())
            if str(item or "").strip()
        }
    roots: List[tuple] = []
    if repo_path:
        roots.append((Path(repo_path) / ".neo" / "skills", "project"))
    global_roots = [_global_config_root()]
    legacy_root = _legacy_global_root()
    if legacy_root is not None and legacy_root != global_roots[0]:
        global_roots.append(legacy_root)
    seen_roots = set()
    for global_root in global_roots:
        skills_root = global_root / "skills"
        root_key = os.path.normcase(str(skills_root.resolve()))
        if root_key not in seen_roots:
            roots.append((skills_root, "global"))
            seen_roots.add(root_key)
        plugins_root = global_root / "plugins"
        if plugins_root.is_dir() and not _has_symlink_component(plugins_root):
            try:
                for plugin_dir in sorted(plugins_root.iterdir()):
                    if (
                        plugin_dir.is_dir()
                        and not plugin_dir.name.startswith(".")
                        and not _has_symlink_component(plugin_dir)
                    ):
                        try:
                            if (
                                plugin_dir.parent / f"{plugin_dir.name}.disabled"
                            ).is_file():
                                continue
                        except OSError:
                            pass
                        roots.append((plugin_dir / "skills", "plugin"))
            except OSError:
                pass
    for root in extra_roots or []:
        roots.append((Path(root), "extra"))

    by_name: Dict[str, Skill] = {}
    for root, origin in roots:
        if not root.is_dir() or _has_symlink_component(root):
            if root.is_symlink() and diagnostics is not None:
                diagnostics.append(
                    {"source": str(root), "error": "symlinked skill root refused"}
                )
            continue
        try:
            entries = sorted(root.iterdir())
        except OSError as exc:
            if diagnostics is not None:
                diagnostics.append({"source": str(root), "error": str(exc)})
            continue
        for entry in entries:
            if (
                not entry.is_dir()
                or entry.name.startswith(".")
                or _has_symlink_component(entry)
            ):
                continue
            skill_file = entry / "SKILL.md"
            skill = _parse_skill_md(skill_file, origin, untrusted_mode=untrusted_mode)
            if skill is None:
                if diagnostics is not None and skill_file.exists():
                    quarantined = _skill_is_quarantined(skill_file, untrusted_mode)
                    diagnostics.append(
                        {
                            "source": str(skill_file),
                            "error": (
                                "skill body quarantined by the untrusted-content policy"
                                if quarantined
                                else "invalid or empty skill file"
                            ),
                            "quarantined": quarantined,
                        }
                    )
                continue
            if _is_hidden(skill, hidden):
                continue
            by_name.setdefault(skill.name, skill)
    return [by_name[name] for name in sorted(by_name)]


# ---------------------------------------------------------------------------
# The visibility ceiling: what the model is NOT shown
# ---------------------------------------------------------------------------
#
# Hiding a skill has to be enforced where the model is handed the list, not
# filtered out of a rendered block afterwards. So the ceiling lives HERE, in
# the module every scan already calls, and it is read from a file under the
# Neo home - never from the repository, because a preference about which
# skills cost context must not dirty a checkout.
#
# The store is deliberately stdlib-only and NEO_HOME-aware:
#
#   * `harness` may not import `cli` (the dependency direction is enforced
#     elsewhere), so the path resolution is duplicated rather than delegated.
#     That is a four-line function and the alternative is a cycle.
#   * `NEO_HOME` wins, which is how a test run never marks the developer's
#     real preferences as changed.
#   * an unreadable, malformed, or unsupported-version document is REFUSED
#     WHOLE, so every skill is visible again. Half-applying a visibility
#     store would silently re-expose skills a person hid, and "half a
#     preference" is not a state anybody can reason about.

#: Bumped when the persisted visibility document changes shape.
VISIBILITY_STORE_VERSION = 1

#: A hand-edited store may not become an unbounded read on the render path.
MAX_STORED_VISIBILITIES = 256

#: The visibility states a skill can be in. `hidden` is the only one a
#: person sets; `visible` is the absence of a record, named so a receipt can
#: state a state rather than implying one.
SKILL_VISIBILITIES: tuple[str, ...] = ("visible", "hidden")


def _visibility_home(home: Optional[Any] = None) -> Path:
    """Return the Neo home that owns visibility state."""
    override = str(home or os.environ.get("NEO_HOME") or "").strip()
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "neo"
    base = os.environ.get("XDG_DATA_HOME") or ""
    if base:
        return Path(base) / "neo"
    return Path.home() / ".local" / "share" / "neo"


def visibility_store_path(
    repo_path: Optional[Any] = None, *, home: Optional[Any] = None
) -> Path:
    """Return the per-repository visibility document's path.

    Keyed by a hash of the normalised absolute path, so two repositories with
    the same folder name never share one store and one repository always
    resolves to the same file.
    """
    text = str(repo_path or "").strip()
    if text:
        try:
            resolved = str(Path(text).expanduser().resolve())
        except (OSError, RuntimeError, ValueError):
            resolved = text
        folded = os.path.normcase(resolved)
        digest = hashlib.sha256(folded.encode("utf-8", "replace")).hexdigest()[:10]
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(folded).name or "repo") or "repo"
        name = f"{slug}-{digest}"
    else:
        name = "no-repo"
    return _visibility_home(home) / "skills" / f"{name}.json"


def load_visibility(
    repo_path: Optional[Any] = None, *, home: Optional[Any] = None
) -> tuple[Dict[str, str], str]:
    """Return ``(hidden_names, note)`` from the visibility store.

    ``note`` is non-empty whenever anything was refused, and the returned
    mapping is then only what could be read legally - never a partially
    understood value silently applied. A MISSING FILE is the normal case and
    produces no note: "nothing hidden yet" is not a warning, and warning
    about it on every scan would train people to ignore warnings.
    """
    try:
        target = visibility_store_path(repo_path, home=home)
    except Exception as exc:  # pragma: no cover - defensive
        return ({}, f"unusable store path: {exc}")
    try:
        if not target.is_file():
            return ({}, "")
        raw = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ({}, f"unreadable: {exc.__class__.__name__}")
    try:
        import json as _json

        document = _json.loads(raw)
    except Exception:
        return ({}, "not valid JSON: every skill is visible again")
    if not isinstance(document, Mapping):
        return ({}, "not a document: every skill is visible again")
    if document.get("version") != VISIBILITY_STORE_VERSION:
        return ({}, f"unsupported store version {document.get('version')!r}")
    hidden = document.get("hidden")
    if not isinstance(hidden, list):
        return ({}, "no visibility list in the document: every skill is visible")
    if len(hidden) > MAX_STORED_VISIBILITIES:
        return ({}, "the store holds more entries than the cap allows")
    kept: List[str] = []
    refused: List[str] = []
    for item in hidden:
        name = str(item or "").strip()
        if not name:
            continue
        if name in SKILL_VISIBILITIES:
            refused.append(name)
            continue
        kept.append(name)
    note = (
        "unusable value refused for: " + ", ".join(sorted(refused)) if refused else ""
    )
    return ({name: "hidden" for name in kept}, note)


def _is_hidden(skill: "Skill", hidden: set) -> bool:
    """Return whether the visibility ceiling excludes this skill.

    Both spellings are checked: the bare name and the namespaced
    `plugin:<skill>` form, because a user hides a plugin skill by the name
    the catalogue shows them and a filter that understood only one spelling
    would leave the other working - a visibility control with a hole in it
    is worse than none, because it reports a state it is not enforcing.
    """
    if not hidden:
        return False
    name = str(getattr(skill, "name", "") or "")
    if name in hidden:
        return True
    plugin = plugin_name_for(skill)
    return bool(plugin) and f"{plugin}{PLUGIN_NAMESPACE_SEPARATOR}{name}" in hidden


def _skill_is_quarantined(skill_file: Path, untrusted_mode: Optional[str]) -> bool:
    """Return whether a skill file exists but its body is refused.

    Used only to label a discovery diagnostic accurately: the boundary already
    dropped the skill, and a caller reading the diagnostics list needs to know
    whether the file was rejected or simply unparseable. Reads through the
    same bounded path as the parser and never raises.
    """
    try:
        if skill_file.is_symlink() or _has_symlink_component(skill_file.parent):
            return False
        size = skill_file.stat().st_size
        with skill_file.open("rb") as handle:
            raw = handle.read(min(_MAX_SKILL_FILE_BYTES, max(1024, size)))
        if b"\x00" in raw:
            return False
        text = raw.decode("utf-8", errors="replace")
    except (OSError, UnicodeError):
        return False
    return review_untrusted_source(
        text, source="skill", mode=untrusted_mode, max_chars=_MAX_BODY_CHARS
    ).blocked


def _words(text: str) -> set:
    """Lowercased identifier-split words, stopwords dropped.

    Splits on non-alphanumerics AND camelCase boundaries (a description
    saying "DjangoConventions" matches an issue saying "django
    conventions" — same decomposition discipline as retrieval's subword
    symbol matching).
    """
    out = set()
    for tok in re.findall(r"[A-Za-z0-9]+", text or ""):
        # camelCase split: lower RUN gets a leading boundary when it
        # follows an uppercase run (SimpleToken -> simple token)
        parts = re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|\d+", tok)
        for p in parts:
            p = p.lower()
            if p and p not in _STOPWORDS and len(p) > 1:
                out.add(p)
                if len(p) > 4:
                    if p.endswith("ing"):
                        out.add(p[:-3] + "e")
                    for suffix in ("ing", "ed", "s"):
                        if p.endswith(suffix):
                            out.add(p[: -len(suffix)])
                            break
    return out


def _relevance(skill: Skill, task_words: set) -> int:
    """Return the number of meaningful skill/task vocabulary overlaps."""
    desc_words = _words(skill.description) | _words(skill.name)
    return len(desc_words & task_words)


def _repo_words(repo_path: Optional[str]) -> set:
    if not repo_path:
        return set()
    try:
        name = Path(str(repo_path)).expanduser().resolve().name
    except (OSError, RuntimeError, ValueError):
        name = Path(str(repo_path)).name
    return _words(name)


def _receipt_sort_key(item: tuple[Dict[str, Any], Skill]) -> tuple:
    receipt, skill = item
    return (-int(receipt["score"]), skill.name)


def _ranked_matches(
    skills: List[Skill],
    issue_text: str,
    retrieval_terms: Optional[List[str]],
    repo_path: Optional[str],
    max_skills: int,
) -> List[tuple[Dict[str, Any], Skill]]:
    issue_words = _words(issue_text or "")
    retrieval_words = _words(" ".join(retrieval_terms or []))
    repo_vocabulary = _repo_words(repo_path)
    ranked: List[tuple[Dict[str, Any], Skill]] = []
    for skill in skills:
        skill_words = _words(skill.description) | _words(skill.name)
        issue_matches = sorted(skill_words & issue_words)
        retrieval_matches = sorted(skill_words & retrieval_words)
        repo_matches = sorted(skill_words & repo_vocabulary)
        matched = sorted(set(issue_matches + retrieval_matches + repo_matches))
        if not matched:
            continue
        reasons: List[str] = []
        if issue_matches:
            reasons.append("issue: " + ", ".join(issue_matches))
        if retrieval_matches:
            reasons.append("retrieval: " + ", ".join(retrieval_matches))
        if repo_matches:
            reasons.append("repository: " + ", ".join(repo_matches))
        receipt = {
            "name": skill.name,
            "origin": skill.origin,
            "source": skill.source,
            "score": len(matched),
            "matched_terms": matched,
            "reason": "; ".join(reasons),
            "tainted": skill.tainted,
            "taint_categories": list(skill.review.categories) if skill.tainted else [],
            "taint_severity": (
                str(getattr(skill.review, "severity", "")) if skill.tainted else ""
            ),
            # Ceiling-12 versioned declaration. This is the DECLARED data only:
            # `extensions.skill_policy` is what intersects it with the parent
            # session's envelope before anything is honoured, so a receipt can
            # show what a skill asked for without implying it was granted.
            "declaration": {
                "version": int(getattr(skill, "version", 1) or 1),
                "model_tier": str(getattr(skill, "model_tier", "") or ""),
                "tools": [
                    str(item) for item in (getattr(skill, "declared_tools", ()) or ())
                ],
                "permission_claims": [
                    str(item)
                    for item in (getattr(skill, "permission_claims", ()) or ())
                ],
            },
        }
        ranked.append((receipt, skill))
    ranked.sort(key=_receipt_sort_key)
    return ranked[: max(1, int(max_skills))]


def explain_skill_matches(
    skills: List[Skill],
    issue_text: str,
    retrieval_terms: Optional[List[str]] = None,
    repo_path: Optional[str] = None,
    max_skills: int = 3,
) -> List[Dict[str, Any]]:
    """Return ranked match receipts explaining why each skill was selected."""
    return [
        receipt
        for receipt, _skill in _ranked_matches(
            skills, issue_text, retrieval_terms, repo_path, max_skills
        )
    ]


def find_applicable_skills(
    skills: List[Skill],
    issue_text: str,
    retrieval_terms: Optional[List[str]] = None,
    repo_path: Optional[str] = None,
    max_skills: int = 3,
) -> List[Skill]:
    """Rank skills against issue, retrieval, and full repository vocabulary."""
    return [
        skill
        for _receipt, skill in _ranked_matches(
            skills, issue_text, retrieval_terms, repo_path, max_skills
        )
    ]


def render_skills_block(
    skills: List[Skill],
    max_chars: int = 2500,
    receipts: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Render skills with origin, source, and match receipts under a hard cap."""
    if not skills:
        return NONE_MATCHED
    cap = max(1, int(max_chars))
    receipt_by_name = {
        str(item.get("name")): item
        for item in (receipts or [])
        if isinstance(item, dict)
    }
    lines: List[str] = []
    used = 0
    for index, skill in enumerate(skills):
        receipt = receipt_by_name.get(skill.name, {})
        terms = ", ".join(str(term) for term in receipt.get("matched_terms", []))
        source = str(receipt.get("source") or skill.source)
        header = (
            f"### Skill: {skill.name} (from {skill.origin} skills; source: {source}"
        )
        if terms:
            header += f"; matched: {terms}"
        header += ")"
        body = skill.body
        if skill.tainted and getattr(skill.review, "text", None):
            # Taint must be visible wherever the body is injected, not only in
            # the trace: a reader of the prompt (or a later turn) has to be
            # able to tell a flagged skill body from ordinary instructions.
            body = taint_wrap(skill.review, include_banner=False)
            header += f"; tainted[{','.join(skill.review.categories) or 'flagged'}]"
        chunk = f"{header}\n{body}\n"
        remaining = cap - used
        if remaining <= 0:
            break
        if len(chunk) > remaining:
            if lines:
                omitted = len(skills) - index
                marker = f"... [{omitted} more truncated]"
                if len(marker) <= remaining:
                    lines.append(marker)
                break
            marker = "\n... [skill body truncated]"
            chunk = chunk[: max(0, remaining - len(marker))].rstrip() + marker
        lines.append(chunk)
        used += len(chunk)
        if used >= cap:
            break
    return "\n".join(lines).strip()[:cap]


def build_skill_receipt(
    scan: Mapping[str, Any],
    *,
    model_content: bool = False,
) -> Dict[str, Any]:
    """Return a stable receipt for discovery and model-context delivery.

    Assumes ``scan`` is the mapping returned by :func:`scan_skills_for_task`.
    ``model_content`` must be set only after the rendered block has been
    placed in the first model request; the receipt never infers delivery
    from discovery alone.
    """
    data = dict(scan or {})
    block = str(data.get("skills_block") or "")
    rendered = [str(item) for item in (data.get("rendered") or []) if str(item)]
    diagnostics = [dict(item) for item in (data.get("diagnostics") or [])]
    receipt = {
        "matched": [str(item) for item in (data.get("matched") or [])],
        "considered": int(data.get("considered") or 0),
        "receipts": list(data.get("receipts") or []),
        "rendered": rendered,
        "omitted": [str(item) for item in (data.get("omitted") or [])],
        "section_chars": int(data.get("section_chars") or len(block)),
        "skipped": data.get("skipped"),
        "error": data.get("error"),
        "tainted": sorted(
            {
                str(item.get("name"))
                for item in (data.get("receipts") or [])
                if isinstance(item, Mapping) and item.get("tainted")
            }
        ),
        "quarantined": sorted(
            {str(item.get("source")) for item in diagnostics if item.get("quarantined")}
        ),
        "model_content": bool(model_content and rendered),
        # Ceiling-12: a stable summary of the versioned declarations the
        # selected skills carried, so a trace reader can see which skills asked
        # for a model tier or extra permission without re-parsing every skill.
        "declarations": {
            str(item.get("name")): item.get("declaration")
            for item in (data.get("receipts") or [])
            if isinstance(item, Mapping) and item.get("declaration")
        },
        "prompt_digest": hashlib.sha256(block.encode("utf-8", "replace")).hexdigest()[
            :16
        ]
        if block and block != NONE_MATCHED
        else "",
    }
    return receipt


def scan_skills_for_task(
    repo_path: str,
    issue_text: str,
    retrieval_terms: Optional[List[str]] = None,
    extra_roots: Optional[List[str]] = None,
    max_skills: int = 3,
    max_chars: int = 2500,
    *,
    untrusted_mode: Optional[str] = None,
    hidden_names: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """The planner-time entry point: discover, match, render.

    Returns {"skills_block": str, "matched": [names], "considered": N,
    "skipped": Optional[str], "error": None} — never raises (a broken
    scan degrades to block="(none matched)" with skipped set, the
    planner runs exactly as before, trace event records why).

    ``untrusted_mode`` overrides the skill trust policy for this scan. Bodies
    the boundary quarantines never reach ``skills_block``; bodies it only
    flags are rendered with their taint visible and listed in the receipt's
    ``tainted`` key. ``hidden_names`` is the same visibility ceiling
    :func:`discover_skills` takes, threaded through here so a hidden skill is
    absent from the scan rather than filtered out of a block afterwards - a
    scan that "looked at" a hidden skill and dropped it would still report it
    in ``considered``.
    """
    out: Dict[str, Any] = {
        "skills_block": NONE_MATCHED,
        "matched": [],
        "receipts": [],
        "rendered": [],
        "omitted": [],
        "diagnostics": [],
        "considered": 0,
        "section_chars": 0,
        "skipped": None,
        "error": None,
    }
    diagnostics: List[Dict[str, Any]] = []
    try:
        skills = discover_skills(
            repo_path=repo_path,
            extra_roots=extra_roots,
            diagnostics=diagnostics,
            untrusted_mode=untrusted_mode,
            hidden_names=hidden_names,
        )
    except Exception as exc:
        out["error"] = f"skill discovery failed: {exc}"
        return out
    out["diagnostics"] = diagnostics
    if diagnostics:
        out["error"] = "one or more skill files were invalid"
    out["considered"] = len(skills)
    if not skills:
        out["skipped"] = "no skills found"
        return out
    try:
        receipts = explain_skill_matches(
            skills,
            issue_text=issue_text,
            retrieval_terms=retrieval_terms,
            repo_path=repo_path,
            max_skills=max_skills,
        )
        names = {str(receipt["name"]) for receipt in receipts}
        matched = [skill for skill in skills if skill.name in names]
        out["matched"] = [str(receipt["name"]) for receipt in receipts]
        out["receipts"] = receipts
        out["skills_block"] = render_skills_block(
            matched,
            max_chars=max_chars,
            receipts=receipts,
        )
        out["section_chars"] = len(out["skills_block"])
        out["rendered"] = [
            name
            for name in out["matched"]
            if f"### Skill: {name}" in out["skills_block"]
        ]
        out["omitted"] = [
            name for name in out["matched"] if name not in out["rendered"]
        ]
    except Exception as exc:
        out["error"] = f"skill matching failed: {exc}"
    return out


# ---------------------------------------------------------------------------
# Per-skill token cost, and the progressive-disclosure measurement
# ---------------------------------------------------------------------------
#
# A skill is cheap because only its NAME and DESCRIPTION are loaded at
# startup; the body is read when the skill is invoked. That property is the
# entire reason a skill catalogue can grow without a context bill, and it is
# only a property if something MEASURES it. So the cost of a skill is three
# separate numbers and never one:
#
#   * `catalog_tokens`  - what the model can see without invoking anything.
#                         This is the number a user is managing.
#   * `body_tokens`     - what the body would cost IF it were preloaded.
#                         It is NOT paid unless the skill is invoked.
#   * `supporting_tokens` - the cost of `references/` and `scripts/`, which
#                         are resolved on demand and never preloaded.
#
# Collapsing those three into one "skill size" is how a catalogue starts
# lying about its own price.

#: How many characters the repository's token estimator assumes per token.
#: Read from `harness.agent_kernel.budget` (the one authority) and pinned
#: here only as the fallback for an install with no kernel package.
_DEFAULT_CHARS_PER_TOKEN = 4.0


def estimate_tokens(text: Any) -> int:
    """Return the token estimate for ``text``, rounding UP.

    The divisor is read from :mod:`harness.agent_kernel.budget` so this
    module cannot drift from the estimator that sizes an actual request. A
    kernel-less install falls back to the same constant rather than
    inventing one. Never raises: an unusable value measures zero, which is
    visible in the receipt rather than silently rounded away.
    """
    raw = "" if text is None else str(text)
    if not raw:
        return 0
    divisor = _DEFAULT_CHARS_PER_TOKEN
    try:
        from harness.agent_kernel.budget import CHARS_PER_TOKEN

        divisor = float(CHARS_PER_TOKEN) or _DEFAULT_CHARS_PER_TOKEN
    except Exception:  # pragma: no cover - defensive
        pass
    return int(len(raw) / divisor + 0.999999)


def _tokens_for_chars(count: Any) -> int:
    """Return the token estimate for a CHARACTER COUNT.

    Same divisor as :func:`estimate_tokens`, without materialising the text -
    which is what makes sizing a directory of supporting files cheap enough
    to do on every listing render.
    """
    try:
        chars = int(count)
    except (TypeError, ValueError):
        return 0
    if chars <= 0:
        return 0
    divisor = _DEFAULT_CHARS_PER_TOKEN
    try:
        from harness.agent_kernel.budget import CHARS_PER_TOKEN

        divisor = float(CHARS_PER_TOKEN) or _DEFAULT_CHARS_PER_TOKEN
    except Exception:  # pragma: no cover - defensive
        pass
    return int(chars / divisor + 0.999999)


def _skill_root(skill: Any) -> Path:
    """Return the directory holding a skill's SKILL.md."""
    source = str(getattr(skill, "source", "") or "")
    return Path(source).parent if source else Path(".")


def _safe_child(root: Path, relative: str) -> Optional[Path]:
    """Resolve ``relative`` under ``root``, refusing every escape.

    Three refusals, all structural: a symlink anywhere on the path (the
    skill boundary already refuses those for SKILL.md itself, and a
    supporting file must not be the hole in that wall), an absolute path,
    and any resolved path outside ``root``.
    """
    text = str(relative or "").strip().replace("\\", "/")
    if not text or text.startswith("/") or ".." in text.split("/"):
        return None
    candidate = root / text
    try:
        if _has_symlink_component(candidate):
            return None
        resolved = candidate.resolve()
        base = root.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved != base and base not in resolved.parents:
        return None
    return candidate


def supporting_files(skill: Any) -> List[Dict[str, Any]]:
    """List a skill's supporting files, WITHOUT reading any of them.

    The listing is the point: a user managing context budget needs to know a
    `references/` directory exists and how big it is, and the only honest
    way to size it is to measure it. The measurement is a stat, not a load -
    nothing here reads content, so a 5 MB reference pack costs a directory
    walk, not a context injection.

    Only the two conventional subdirectories are listed (`references/` and
    `scripts/`); a skill's own `SKILL.md` is the body, not a supporting
    file. Never raises: an unreadable directory is an empty list.
    """
    root = _skill_root(skill)
    out: List[Dict[str, Any]] = []
    for folder in ("references", "scripts"):
        base = _safe_child(root, folder)
        if base is None or not base.is_dir():
            continue
        try:
            entries = sorted(base.rglob("*"))
        except OSError:
            continue
        for entry in entries:
            if not entry.is_file() or entry.name.startswith("."):
                continue
            if _has_symlink_component(entry):
                continue
            try:
                size = int(entry.stat().st_size)
            except OSError:
                continue
            try:
                relative = entry.relative_to(root).as_posix()
            except ValueError:
                continue
            out.append(
                {
                    "skill": str(getattr(skill, "name", "") or ""),
                    "relative": relative,
                    "folder": folder,
                    "bytes": size,
                    # Arithmetic, not a materialised string: sizing a 5 MB
                    # reference pack by building a 5 MB string first would
                    # make the measurement itself the cost it measures.
                    "tokens": _tokens_for_chars(size),
                    "loaded": False,
                    "note": "resolved on demand; never preloaded",
                }
            )
    return out


def resolve_support_file(skill: Any, relative: str) -> Dict[str, Any]:
    """Read ONE supporting file, bounded, on explicit request.

    This is the load half of progressive disclosure, and it is deliberately
    the ONLY place a supporting file's bytes enter the process. A refusal
    (traversal, a symlink, an oversized file, an unreadable path) returns a
    ``loaded: False`` row carrying the reason, because a supporting file
    that silently did not load is indistinguishable from one that does not
    exist.
    """
    row: Dict[str, Any] = {
        "skill": str(getattr(skill, "name", "") or ""),
        "relative": str(relative or ""),
        "loaded": False,
        "reason": "",
        "chars": 0,
        "tokens": 0,
    }
    root = _skill_root(skill)
    target = _safe_child(root, relative)
    if target is None:
        row["reason"] = (
            "refused: the path escapes the skill directory, is absolute, or "
            "traverses a symlink"
        )
        return row
    if not target.is_file():
        row["reason"] = "no such supporting file"
        return row
    try:
        size = int(target.stat().st_size)
        if size > _MAX_SKILL_FILE_BYTES:
            row["reason"] = (
                f"refused: {size} bytes exceeds the {_MAX_SKILL_FILE_BYTES}-byte "
                "skill file cap"
            )
            return row
        with target.open("rb") as handle:
            raw = handle.read(_MAX_SKILL_FILE_BYTES)
        if b"\x00" in raw:
            row["reason"] = "refused: the file is binary"
            return row
        text = raw.decode("utf-8", errors="replace")
    except (OSError, UnicodeError) as exc:
        row["reason"] = f"unreadable: {exc.__class__.__name__}"
        return row
    row["loaded"] = True
    row["chars"] = len(text)
    row["tokens"] = estimate_tokens(text)
    row["text"] = text
    return row


def skill_token_cost(skill: Any) -> Dict[str, Any]:
    """Return the per-skill token cost as THREE separate numbers.

    See the section comment: `catalog_tokens` is what the model can see for
    free, `body_tokens` is what invoking costs, and `supporting_tokens` is
    what the on-demand files would cost. A listing that shows one number
    would either hide the price of a free catalogue entry or charge the user
    for a body nobody loaded.
    """
    name = str(getattr(skill, "name", "") or "")
    description = str(getattr(skill, "description", "") or "")
    body = str(getattr(skill, "body", "") or "")
    support = supporting_files(skill)
    support_tokens = sum(int(item["tokens"]) for item in support)
    catalog_text = f"{name}\n{description}".strip()
    catalog_tokens = estimate_tokens(catalog_text)
    body_tokens = estimate_tokens(body)
    return {
        "name": name,
        "origin": str(getattr(skill, "origin", "") or ""),
        "source": str(getattr(skill, "source", "") or ""),
        "catalog_chars": len(catalog_text),
        "catalog_tokens": catalog_tokens,
        "body_chars": len(body),
        "body_tokens": body_tokens,
        "supporting_files": len(support),
        "supporting_tokens": support_tokens,
        # What the skill would cost if EVERY body were preloaded. The
        # difference from `catalog_tokens` is the progressive-disclosure
        # saving, and it is reported rather than implied.
        "preloaded_tokens": catalog_tokens + body_tokens,
        "saving_tokens": body_tokens,
        "tainted": bool(getattr(skill, "tainted", False)),
        "version": int(getattr(skill, "version", 1) or 1),
        "model_tier": str(getattr(skill, "model_tier", "") or ""),
        "declared_tools": [
            str(item) for item in (getattr(skill, "declared_tools", ()) or ())
        ],
    }


def progressive_disclosure_report(skills: Any) -> Dict[str, Any]:
    """Measure what progressive disclosure actually saves, over a real set.

    The claim "only name+description load at startup" is worth nothing
    without the number, so this returns BOTH totals: what the catalogue
    costs as loaded, and what the same set would cost if every body were
    preloaded. The difference is the saving, stated as tokens AND as a
    ratio, because a ratio with a tiny denominator is not a percentage
    anybody should act on - `saving_ratio` is ``None`` when the preloaded
    cost is zero.
    """
    rows = [skill_token_cost(skill) for skill in (skills or ())]
    catalog_tokens = sum(int(row["catalog_tokens"]) for row in rows)
    preloaded_tokens = sum(int(row["preloaded_tokens"]) for row in rows)
    supporting_tokens = sum(int(row["supporting_tokens"]) for row in rows)
    saving = preloaded_tokens - catalog_tokens
    return {
        "skills": len(rows),
        "catalog_tokens": catalog_tokens,
        "preloaded_tokens": preloaded_tokens,
        "supporting_tokens": supporting_tokens,
        "supporting_loaded": 0,
        "saving_tokens": saving,
        "saving_ratio": (saving / preloaded_tokens) if preloaded_tokens else None,
        "per_skill": sorted(
            rows, key=lambda row: (-int(row["preloaded_tokens"]), row["name"])
        ),
        "note": (
            "catalog_tokens is what the model sees before any skill is "
            "invoked; supporting files are resolved on demand and are "
            "never preloaded"
        ),
    }


# ---------------------------------------------------------------------------
# The merged catalogue: a flat command file and a skill create the SAME name
# ---------------------------------------------------------------------------
#
# `.neo/commands/<name>.md` is a reusable instruction template.
# `<root>/skills/<name>/SKILL.md` is an auto-invoked instruction pack with
# frontmatter, a declaration, and supporting files. They are two ways to
# write the same thing a person types as `/<name>`, so the catalogue merges
# them into ONE namespace.
#
# The precedence rule is the interesting part: **on a name clash the SKILL
# wins**. A flat file has no version, no declaration, and no taint review, so
# if the two could both be reached under one name the weaker artifact would
# be the one a user could accidentally invoke. Plugin skills are NAMESPACED
# (`plugin:<skill>`) and therefore cannot collide with anything at all.

#: The closed vocabulary of catalog artifact kinds.
CATALOG_KINDS: tuple[str, ...] = ("skill", "command_file", "plugin_skill")

#: The closed vocabulary of command-file kinds. Kept separate from
#: :data:`CATALOG_KINDS` because a caller filtering "is this a flat file"
#: must not have to subtract.
COMMAND_SOURCE_KINDS: tuple[str, ...] = ("command_file", "skill")

#: Plugin skills are namespaced with this separator, so a plugin skill can
#: never take a bare name a project or global artifact already claims.
PLUGIN_NAMESPACE_SEPARATOR = ":"


def plugin_name_for(skill: Any) -> str:
    """Return the plugin a skill came from, or ``""`` if it is not one.

    Derived from the discovered path shape
    ``<plugins>/<plugin>/skills/<name>/SKILL.md`` because the plugin identity
    is otherwise not recorded on the `Skill` object and adding a field for it
    would be a breaking change to a dataclass other modules construct.
    Returns the EMPTY STRING rather than a guessed name when the path does not
    have the shape, so a caller that namespaces on a guess cannot invent a
    namespace.
    """
    if str(getattr(skill, "origin", "")) != "plugin":
        return ""
    source = str(getattr(skill, "source", "") or "")
    if not source:
        return ""
    skill_dir = Path(source).parent
    skills_root = skill_dir.parent
    plugin_dir = skills_root.parent
    if skills_root.name != "skills" or not plugin_dir.name:
        return ""
    return plugin_dir.name


@dataclass(frozen=True)
class CommandSource:
    """One merged catalogue entry: what `/<name>` would resolve to.

    `name` is the invocable name and `qualified_name` is what the clash
    rule actually compares. They differ only for a plugin skill, which is
    namespaced and therefore immune to the merge.
    """

    name: str
    kind: str
    origin: str
    source: str
    description: str = ""
    qualified_name: str = ""
    wins_over: tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible row (no body, no author text beyond the
        description a person already sees in the list)."""
        return {
            "name": self.name,
            "qualified_name": self.qualified_name or self.name,
            "kind": self.kind,
            "origin": self.origin,
            "source": self.source,
            "description": self.description,
            "wins_over": list(self.wins_over),
        }


def _command_roots(repo_path: Optional[str]) -> List[tuple[Path, str]]:
    """Return the flat-command search roots in precedence order.

    Deliberately the SAME root shape as skill discovery (project > global >
    plugin > extra) with the SAME symlink and dot-directory refusals, so a
    catalogue cannot be assembled from a location the skill scanner would
    have refused.
    """
    roots: List[tuple[Path, str]] = []
    if repo_path:
        roots.append((Path(repo_path) / ".neo" / "commands", "project"))
    global_roots = [_global_config_root()]
    legacy_root = _legacy_global_root()
    if legacy_root is not None and legacy_root != global_roots[0]:
        global_roots.append(legacy_root)
    seen: set = set()
    for global_root in global_roots:
        for folder, origin in ((global_root / "commands", "global"),):
            key = os.path.normcase(str(folder))
            if key not in seen:
                seen.add(key)
                roots.append((folder, origin))
        plugins_root = global_root / "plugins"
        if plugins_root.is_dir() and not _has_symlink_component(plugins_root):
            try:
                plugin_dirs = sorted(plugins_root.iterdir())
            except OSError:
                plugin_dirs = []
            for plugin_dir in plugin_dirs:
                if not plugin_dir.is_dir() or plugin_dir.name.startswith("."):
                    continue
                if _has_symlink_component(plugin_dir):
                    continue
                try:
                    if (plugin_dir.parent / f"{plugin_dir.name}.disabled").is_file():
                        continue
                except OSError:
                    pass
                roots.append((plugin_dir / "commands", "plugin"))
    return roots


def _first_line_description(path: Path) -> str:
    """Return a flat command file's description line.

    A command file has no frontmatter contract, so this is the first
    non-heading, non-empty line - the same derivation `cli.commands`
    already documents for `list_commands`. Duplicated rather than imported
    because `harness` must not import `cli` (the dependency direction is
    enforced elsewhere), and because this is three lines.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped[:120]
    return ""


def command_sources(
    repo_path: Optional[str] = None,
    *,
    extra_roots: Optional[List[str]] = None,
    diagnostics: Optional[List[Dict[str, Any]]] = None,
) -> List[CommandSource]:
    """Return the MERGED catalogue of every invocable name, sorted.

    Both artifact kinds are discovered here, in one table, so a caller can
    never ask "where did this name come from" and get two answers. The merge
    rule, stated once and pinned by a test:

    * a name claimed by BOTH a flat command file and a skill resolves to
      the SKILL, and the flat file is recorded in the winner's
      `wins_over` so the collision is auditable rather than silent;
    * a plugin skill is namespaced (`plugin:<skill>`) and so collides with
      nothing;
    * within one kind, the existing precedence order applies (project >
      global > plugin > extra) and the first claimer wins.

    Never raises. A skill file whose body was quarantined is not in
    `discover_skills` output, so it cannot appear here either.
    """
    claims: Dict[str, List[CommandSource]] = {}

    def _claim(source: CommandSource) -> None:
        claims.setdefault(source.qualified_name, []).append(source)

    # Skills first, so `by_name[0]` is the winner when kinds collide.
    diagnostics_list: List[Dict[str, Any]] = [] if diagnostics is None else diagnostics
    skills = discover_skills(
        repo_path=repo_path, extra_roots=extra_roots, diagnostics=diagnostics_list
    )
    for skill in skills:
        qualified = skill.name
        if skill.origin == "plugin":
            # Namespaced so a plugin can never take a bare name. The plugin
            # identity comes from the path shape
            # (`<plugins>/<plugin>/skills/<name>/SKILL.md`), which is the only
            # place it is recorded.
            plugin_name = plugin_name_for(skill)
            qualified = (
                f"{plugin_name}{PLUGIN_NAMESPACE_SEPARATOR}{skill.name}"
                if plugin_name
                else skill.name
            )
        _claim(
            CommandSource(
                name=skill.name,
                kind="plugin_skill" if skill.origin == "plugin" else "skill",
                origin=skill.origin,
                source=skill.source,
                description=str(skill.description or ""),
                qualified_name=qualified,
            )
        )

    for root, origin in _command_roots(repo_path):
        if _has_symlink_component(root) or not root.is_dir():
            continue
        try:
            entries = sorted(root.glob("*.md"))
        except OSError:
            continue
        for path in entries:
            name = path.stem
            if not name or name.startswith("."):
                continue
            _claim(
                CommandSource(
                    name=name,
                    kind="command_file",
                    origin=origin,
                    source=str(path),
                    description=_first_line_description(path),
                    qualified_name=name,
                )
            )

    # Plugin skills are scanned SEPARATELY and namespaced, because normal
    # discovery dedupes on the bare name: a plugin skill whose name collides
    # with a project skill is gone before anybody can namespace it, which
    # would make the namespacing decorative exactly when it is needed.
    for root, plugin, _origin in _plugin_skill_roots():
        if _has_symlink_component(root) or not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if (
                not entry.is_dir()
                or entry.name.startswith(".")
                or _has_symlink_component(entry)
            ):
                continue
            parsed = _parse_skill_md(entry / "SKILL.md", "plugin")
            if parsed is None:
                continue
            _claim(
                CommandSource(
                    name=parsed.name,
                    kind="plugin_skill",
                    origin="plugin",
                    source=parsed.source,
                    description=str(parsed.description or ""),
                    qualified_name=(
                        f"{plugin}{PLUGIN_NAMESPACE_SEPARATOR}{parsed.name}"
                    ),
                )
            )

    for root in extra_roots or []:
        base = Path(root)
        if base.name == "skills" and base.is_dir():
            continue
        if _has_symlink_component(base) or not base.is_dir():
            continue
        try:
            entries = sorted(base.glob("*.md"))
        except OSError:
            continue
        for path in entries:
            name = path.stem
            if not name or name.startswith("."):
                continue
            _claim(
                CommandSource(
                    name=name,
                    kind="command_file",
                    origin="extra",
                    source=str(path),
                    description=_first_line_description(path),
                    qualified_name=name,
                )
            )

    out: List[CommandSource] = []
    for qualified in sorted(claims):
        winners = claims[qualified]
        head, *rest = winners
        out.append(
            CommandSource(
                name=head.name,
                kind=head.kind,
                origin=head.origin,
                source=head.source,
                description=head.description,
                qualified_name=qualified,
                wins_over=tuple(item.kind for item in rest),
            )
        )
    return out


def skill_catalog(
    repo_path: Optional[str] = None,
    *,
    extra_roots: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Return the full listing projection a surface renders.

    One call, one table: the merged command/skill entries, the per-skill
    token cost (item 7 - a user must be able to see what a skill costs
    BEFORE enabling it), and the measured progressive-disclosure total.
    A surface that rendered its own list would have to re-derive all three,
    which is how the cost column and the roster drift apart.
    """
    sources = command_sources(repo_path, extra_roots=extra_roots)
    skills = discover_skills(repo_path=repo_path, extra_roots=extra_roots)
    costs = {
        row["name"]: row for row in progressive_disclosure_report(skills)["per_skill"]
    }
    by_name: Dict[str, Skill] = {skill.name: skill for skill in skills}
    rows: List[Dict[str, Any]] = []
    for source in sources:
        row = source.to_dict()
        skill = by_name.get(source.name)
        cost = dict(costs.get(source.name) or {})
        if skill is not None:
            row["enabled"] = True
            row["supporting_files"] = int(cost.get("supporting_files", 0))
        else:
            row["enabled"] = False
            row["supporting_files"] = 0
        row.update(
            {
                "catalog_tokens": int(cost.get("catalog_tokens", 0)),
                "body_tokens": int(cost.get("body_tokens", 0)),
                "preloaded_tokens": int(cost.get("preloaded_tokens", 0)),
                "supporting_tokens": int(cost.get("supporting_tokens", 0)),
                "tainted": bool(cost.get("tainted", False)),
                "version": int(cost.get("version", 1) or 1),
                "model_tier": str(cost.get("model_tier", "")),
                "declared_tools": list(cost.get("declared_tools", ())),
            }
        )
        rows.append(row)
    report = progressive_disclosure_report(skills)
    return {
        "rows": rows,
        "count": len(rows),
        "skills": len(skills),
        "commands": sum(1 for row in rows if row["kind"] == "command_file"),
        "collisions": [row for row in rows if row["wins_over"]],
        "progressive_disclosure": report,
    }


def attach_declarations(
    receipt: Optional[Mapping[str, Any]],
    skills: Any,
) -> Dict[str, Any]:
    """Thread a skill's OWN frontmatter declarations onto a derived receipt.

    This is the gap Terminal-08 filed and did not close: the compiled-bundle
    receipt (`KnowledgeContext.skill_receipt`) is derived from the bundle's
    text, and the bundle's skill section does not carry parsed frontmatter,
    so the declaration fields were missing on the default daily path.

    It is closable WITHOUT touching the compiler, because the declarations
    live in the SKILL.md files the receipt already names. Given a receipt
    and the skill objects it was derived from, this re-derives the
    declaration block for each name the receipt reports as rendered and
    returns a NEW receipt with `declarations` populated.

    Two rules keep it from becoming a second, drifting authority:

    * it only ADDS `declarations` (and the `declarations_source` marker
      saying where they came from). Every key the caller passed through is
      byte-identical, so the receipt's own claims - including
      `model_content` and `NONE_MATCHED` honesty - cannot be edited here.
    * a name the receipt does NOT report as rendered gets no declaration.
      Declaring a skill the model never received would put a claim about
      an un-delivered artifact into the record, which is the same defect in
      the opposite direction.
    """
    out = dict(receipt or {})
    declared: Dict[str, Any] = {}
    for name in out.get("rendered") or []:
        key = str(name)
        skill = next(
            (item for item in (skills or ()) if str(getattr(item, "name", "")) == key),
            None,
        )
        if skill is None:
            continue
        try:
            from extensions.skill_policy import declaration_from_object

            record = declaration_from_object(skill).to_dict()
        except Exception as exc:  # pragma: no cover - defensive
            record = {"diagnostics": [f"declaration unavailable: {exc}"]}
        declared[key] = record
    out["declarations"] = declared
    out["declarations_source"] = "skill_frontmatter"
    return out
