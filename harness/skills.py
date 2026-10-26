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

import fnmatch
import hashlib
import os
import platform
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from harness.skill_enforcement import enforce_skill_policy
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
    "load_skills_for_prompt",
    "load_visibility",
    "plugin_name_for",
    "progressive_disclosure_report",
    "render_skill_index",
    "render_skills_block",
    "resolve_support_file",
    "scan_skills_for_task",
    "skill_catalog",
    "skill_metadata",
    "skill_token_cost",
    "skill_view",
    "skill_view_spec",
    "skills_prompt_report",
    "supporting_files",
    "visibility_store_path",
]

# Frontmatter: a leading `---` block of `key: value` lines. Tolerates a
# missing frontmatter (body-only SKILL.md still loads; name falls back
# to the folder name, description to "").
_FM_BOUNDARY = re.compile(r"^---\s*$")
_FM_LINE = re.compile(r"^([A-Za-z][\w-]*)\s*:\s*(.*)$")
_FM_ITEM = re.compile(r"^\s+-\s+(.*)$")

#: The frontmatter keys whose value may be a YAML **block sequence** rather than
#: an inline one. A closed set, because "which keys accept a list" is a parsing
#: decision and an open-ended one would let a `- item` under an unrelated key
#: be absorbed into that key's declaration.
#:
#: ``permissions``/``tools`` are Ceiling-12's declarations. The rest are P3b/W1.2's
#: load-time requirements and path triggers — and they are here because their
#: ABSENCE is a silent defect: a skill declaring ``paths:`` as a block parsed to
#: an empty list, so the gate it asked for was never armed.
_FM_BLOCK_KEYS: frozenset[str] = frozenset(
    {
        "permissions",
        "tools",
        "paths",
        "trigger-paths",
        "trigger_paths",
        "requires-binaries",
        "requires_binaries",
        "requires-env",
        "requires_env",
        "requires-os",
        "requires_os",
        "match",
        "matches",
        "keywords",
    }
)

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
        "always_active",
        "body",
        "declared_tools",
        "description",
        "match_terms",
        "model_tier",
        "name",
        "origin",
        "permission_claims",
        "requires_binaries",
        "requires_env",
        "requires_os",
        "review",
        "source",
        "trigger_paths",
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
        requires_binaries: Any = None,
        requires_env: Any = None,
        requires_os: Any = None,
        trigger_paths: Any = None,
        always_active: bool = False,
        match_terms: Any = None,
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
        # P3b/W1.2. All four are keyword-defaulted, so every existing
        # construction site — including the ones in `tests/` — keeps working
        # byte-identically.
        self.requires_binaries = tuple(requires_binaries or ())
        self.requires_env = tuple(requires_env or ())
        self.requires_os = tuple(requires_os or ())
        self.trigger_paths = tuple(trigger_paths or ())
        # P3b/W2.3. The always-on tier. See `always_active`'s use in
        # `load_skills_for_prompt` for exactly which gate this suppresses and,
        # more importantly, which it deliberately does NOT.
        self.always_active = bool(always_active)
        # P3b/W2.3. The AUTHOR-DECLARED MATCH VOCABULARY. See `_ranked_matches`
        # for why a description alone cannot carry a convention skill's
        # relevance, and why this is not a replacement for the description but
        # a supplement to it.
        self.match_terms = tuple(match_terms or ())

    @property
    def tainted(self) -> bool:
        """Return whether the body tripped the untrusted-content review."""
        return bool(getattr(self.review, "tainted", False))

    @property
    def path_triggered(self) -> bool:
        """Whether this skill declares ANY path trigger.

        A skill with no ``paths`` frontmatter is always-eligible; a skill with
        ``paths`` is eligible only once the run has touched a matching file.
        The two are different states and the receipt says which, because
        "gated and waiting" and "not gated" must never render alike.
        """
        return bool(self.trigger_paths)

    def runtime_blockers(
        self,
        *,
        path_lookup: Optional[Callable[[str], bool]] = None,
        env: Optional[Mapping[str, str]] = None,
        system: Optional[str] = None,
    ) -> List[str]:
        """Return why this skill cannot load HERE, or ``[]``.

        OpenClaw's mechanism, and the reason it exists is quoted in the phase
        brief: declaring runtime requirements and filtering at load time
        *"prevent[s] 'how to use kubectl' from polluting the prompt on a machine
        without it."* A prompt full of instructions for absent software is not
        merely wasteful — it teaches the model to call things that are not there,
        and the model has no way to tell a missing binary from a mis-declared
        requirement.

        Three requirement kinds, checked in the order a reader would:

        * ``requires-os`` — the current platform must be one of the named ones.
          Matched case-insensitively against ``platform.system()``'s value and
          against the leading component of ``sys.platform``, because the two
          spell ``win32`` and ``Windows`` respectively and an author will use
          whichever their own machine printed.
        * ``requires-binaries`` — each named program must be findable on PATH.
          The lookup is INJECTED so a test can drive every branch without
          touching the real PATH, and so this function stays pure.
        * ``requires-env`` — each named variable must be set AND non-empty. An
          empty variable is treated as unset: an export that failed halfway is
          the common case, and treating it as present is how a skill's
          precondition silently stops being one.

        An unrecognised requirement is NOT a blocker. A skill this loader has
        never heard of is loaded, because refusing it would make adding a skill
        type a breaking change.
        """
        blockers: List[str] = []
        current_os = str(system if system is not None else platform.system()).lower()
        for wanted in self.requires_os:
            token = str(wanted).strip().lower()
            if not token:
                continue
            aliases = {
                token,
                token.replace("windows", "win32").replace("macos", "darwin"),
            }
            if not aliases & {current_os, str(sys.platform).lower()}:
                blockers.append(
                    f"requires os {wanted!r}; this machine is {current_os!r}"
                )
        lookup = path_lookup if path_lookup is not None else _binary_on_path
        for binary in self.requires_binaries:
            name = str(binary).strip()
            if not name:
                continue
            try:
                found = bool(lookup(name))
            except Exception:
                found = False
            if not found:
                blockers.append(f"requires {name!r} on PATH")
        environment = env if env is not None else os.environ
        for variable in self.requires_env:
            key = str(variable).strip()
            if not key:
                continue
            if not str(environment.get(key, "") or "").strip():
                blockers.append(f"requires environment variable {key!r} to be set")
        return blockers

    def triggers_on(self, path: Any) -> bool:
        """Whether ``path`` matches this skill's declared triggers.

        A skill with no triggers returns ``False`` — NOT ``True``. "No trigger
        declared" and "triggered" are different, and a matcher that answered
        ``True`` for the unconstrained case would make every path-triggered
        skill eligible on the first file touched, which is the gate doing
        nothing.
        """
        target = str(path or "").replace("\\", "/").lstrip("./")
        if not target or not self.trigger_paths:
            return False
        for pattern in self.trigger_paths:
            text = str(pattern).strip().replace("\\", "/")
            if not text:
                continue
            if fnmatch.fnmatch(target, text) or fnmatch.fnmatch(target, f"*/{text}"):
                return True
            # A bare directory prefix matches everything under it, which is what
            # an author writing `paths: [k8s/]` means. The trailing separator is
            # NORMALISED first: the first version appended a second one, so
            # `k8s/` compared against `k8s//` and never matched a single file
            # under the directory it named. A gate that cannot fire looks
            # exactly like a gate that is not armed.
            if any(ch in text for ch in "*?["):
                continue
            prefix = text.rstrip("/")
            if not prefix:
                continue
            if target == prefix or target.startswith(f"{prefix}/"):
                return True
        return False


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
            # indented `- item` lines. Only the keys in `_FM_BLOCK_KEYS` accept a
            # block, so a `- item` under any other key is ignored rather than
            # absorbed -- absorbing it would silently move an unrelated key's
            # value into the wrong declaration.
            #
            # P3b/W1.2 added the runtime-requirement and path-trigger keys here.
            # They were NOT working before this: `paths:` followed by `- k8s/`
            # parsed to an EMPTY list, so a path-gated skill was never gated and
            # every path-triggered skill sat in every prompt. The failure is
            # invisible from the outside -- the skill loads fine, it just never
            # waits -- which is why it is worth stating that the parser, not the
            # matcher, was the broken half.
            item = _FM_ITEM.match(lines[i])
            if item and last_key in _FM_BLOCK_KEYS:
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
        # P3b/W1.2: the load-time requirement and the path trigger. Both are
        # INERT data on this object — nothing here grants anything — and both
        # are read by the loader below, which is the only place a skill's
        # applicability is decided.
        requires_binaries=_declaration_items(
            meta.get("requires-binaries") or meta.get("requires_binaries")
        ),
        requires_env=_declaration_items(
            meta.get("requires-env") or meta.get("requires_env")
        ),
        requires_os=_declaration_items(
            meta.get("requires-os") or meta.get("requires_os")
        ),
        trigger_paths=_declaration_items(
            meta.get("paths") or meta.get("trigger-paths") or meta.get("trigger_paths")
        ),
        # P3b/W2.3. The always-on tier, read from four spellings because the
        # gate keys this file already accepts `paths` / `trigger-paths` /
        # `trigger_paths` and an author who writes one spelling of a concept
        # should not silently lose it by writing another.
        always_active=_declaration_flag(
            meta.get("always")
            or meta.get("always-on")
            or meta.get("always_on")
            or meta.get("always-active")
            or meta.get("always_active")
        ),
        # P3b/W2.3. Declared match vocabulary.
        match_terms=_declaration_items(
            meta.get("match") or meta.get("matches") or meta.get("keywords")
        ),
    )


#: The values that mean OFF. This is the repository's established rule
#: (:func:`harness.tool_surface.truthiness_of`): a typo must not be able to arm
#: a gate, and ``always: "false"`` has to mean OFF rather than "the string is
#: truthy".
_ALWAYS_OFF_VALUES: frozenset = frozenset({"", "0", "false", "no", "off", "none"})


def _declaration_flag(raw: Any) -> bool:
    """Return whether a frontmatter flag is ON, fail-closed on nonsense.

    An unrecognised value is **OFF**, not ON. The direction matters: this flag
    suppresses a gate, so reading a typo as ON would put a skill in the prompt
    that its author was trying to keep out. A gate that fails open on a typo is
    not a gate.
    """
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, (list, tuple, set, frozenset)):
        return any(_declaration_flag(item) for item in raw)
    text = str(raw).strip().lower()
    if text in _ALWAYS_OFF_VALUES:
        return False
    if text in ("1", "true", "yes", "on", "always", "active", "enabled"):
        return True
    return False


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


# ---------------------------------------------------------------------------
# P3b/W1.2 — interoperability roots, and the three disclosure tiers
# ---------------------------------------------------------------------------
#
# **The cheapest capability interoperability available, and the corpus calls it
# the sharpest interoperability datum it found.** Six systems converged on
# `.agents/skills/` as a neutral location, and OpenCode goes further and reads
# `~/.claude/skills` and `.claude/` *deliberately*, so a skill installed for
# Claude Code works unmodified here.
#
# The alternative is that every user installs their skills twice, into two
# directories, in two formats — and the second copy is the one that goes stale.
#
# Precedence, and the reason for it: a PROJECT skill wins over a global one
# (already the existing rule), and within the interop family a project-local
# `.agents/skills` wins over a user-level `~/.claude/skills`. The ordering is
# (1) `.neo/skills`, (2) `.agents/skills`, (3) `.claude/skills` — all
# project-local, most-specific first — then the global roots. A project that
# ships `.agents/skills` therefore overrides a personal `~/.claude/skills`
# entry of the same name, which is the behaviour every one of the six systems
# that reads both produces.
#
# **These roots are opt-out, not opt-in, and that is a judgement worth naming.**
# Reading another tool's directory is a read of the user's own machine by a path
# they did not type, so `SKILL_INTEROP_ENABLED = False` turns it off wholesale.
# It is ON by default because the corpus's finding is that interoperability
# paths are what make an agent harness worth keeping, and a capability nobody
# has to configure is a capability somebody will discover.

#: The interop roots, in precedence order, with the origin label each gets.
#: The label is what the prompt header and the receipt show, so a reader can
#: tell WHERE a skill came from — which matters more than usual for a path
#: another tool owns.
SKILL_INTEROP_ROOTS: tuple[tuple[str, str, str], ...] = (
    # (relative-to-repo or absolute-suffix, origin label, why)
    (".agents/skills", "agents", "the neutral cross-agent skill directory"),
    (".claude/skills", "claude", "a Claude Code skill directory"),
)

#: The user-level interop root. Separated from the project-local pair because it
#: is anchored at HOME, not at the repository, and a test must be able to point
#: it somewhere without a repository.
SKILL_INTEROP_GLOBAL_RELATIVE = ".claude/skills"

#: The master switch for the REPOSITORY's interop paths.
SKILL_INTEROP_ENABLED = True

#: The config key. **Absent means "read the repository's interop roots"**, which
#: is the corpus's recommendation — a capability nobody has to configure is a
#: capability somebody eventually discovers — and an explicit ``False`` reads
#: none of them.
#:
#: These two roots are part of the REPOSITORY, in the same way ``AGENTS.md`` is,
#: which is why defaulting them on is defensible: every run against a given
#: checkout sees the same directories, so a prompt stays reproducible. That is
#: the whole distinction from :data:`SKILL_INTEROP_HOME_CONFIG_KEY` below, and
#: collapsing the two was a real defect this module shipped first.
SKILL_INTEROP_CONFIG_KEY = "skills_interop"

#: The switch for the **user-home** interop root, and it is **OFF by default.**
#:
#: The first version read ``~/.claude/skills`` on every scan whenever
#: ``skills_interop`` was on, and the two were one switch. That is wrong, and the
#: test suite is what proved it: ``tests/test_skills.py`` has three cases whose
#: contract is that a scan with nothing to match reports an explicit
#: ``"no skills found"``, and on a developer machine with Claude Code skills
#: installed they all failed, because a real ``~/.claude/skills`` was scanned.
#:
#: The reason is not testability. A run's prompt would depend on **what the
#: operator happens to have installed on their machine**: two people running the
#: same task on the same commit would see different skill blocks, and a recorded
#: trace would carry one person's personal skill content. That is the same
#: objection this repository raises against a behaviour-changing key in
#: ``DEFAULTS``, aimed at the right target — and the mitigation that key cannot
#: offer applies here: a repository's committed directories travel with the
#: checkout, a home directory does not.
#:
#: A person who wants their personal skills in their sessions sets it; the
#: per-call ``interop_home=`` override lets a test or a driver point it
#: somewhere else.
SKILL_INTEROP_HOME_CONFIG_KEY = "skills_interop_home"
SKILL_INTEROP_HOME_ENABLED = False


def _interop_home_enabled(config: Optional[Mapping[str, Any]]) -> Optional[bool]:
    """Read the home-root switch from a run config, or ``None`` for the default."""
    if not config or SKILL_INTEROP_HOME_CONFIG_KEY not in config:
        return None
    from harness.tool_surface import truthiness_of

    return bool(truthiness_of(config.get(SKILL_INTEROP_HOME_CONFIG_KEY)))


def _interop_roots(
    repo_path: Optional[str],
    *,
    enabled: Optional[bool] = None,
    include_home: Optional[bool] = None,
) -> List[tuple[Path, str]]:
    """Return the interop roots that exist, in precedence order.

    An origin label that is not one of our own is deliberate: a receipt reading
    ``from claude skills`` tells the user their Claude Code directory is being
    read, which is information they are entitled to and which no amount of
    provenance guessing can reconstruct after the fact.

    ``enabled`` is a per-call override for the repository-local roots, threaded
    as an argument rather than read from the module global inside here — see
    :func:`discover_skills`'s docstring for why a temporary global flip is not
    acceptable in a threaded host.

    ``include_home`` is a SEPARATE override for the user's home directory and
    defaults to :data:`SKILL_INTEROP_HOME_ENABLED` (``False``). One switch for
    both is the bug this signature exists to prevent: a repository's committed
    ``.claude/skills`` and an operator's ``~/.claude/skills`` are the same
    convention pointing at two different kinds of thing, and only one of them
    travels with the checkout.
    """
    active = SKILL_INTEROP_ENABLED if enabled is None else bool(enabled)
    home_active = (
        SKILL_INTEROP_HOME_ENABLED if include_home is None else bool(include_home)
    )
    found: List[tuple[Path, str]] = []
    seen: set[str] = set()
    if active and repo_path:
        base = Path(repo_path)
        for relative, origin, _why in SKILL_INTEROP_ROOTS:
            candidate = base / relative
            key = os.path.normcase(str(candidate))
            if key in seen:
                continue
            seen.add(key)
            found.append((candidate, origin))
    if not home_active:
        return found
    home_root = Path.home() / SKILL_INTEROP_GLOBAL_RELATIVE
    key = os.path.normcase(str(home_root))
    if key not in seen:
        seen.add(key)
        found.append((home_root, "claude"))
    return found


def _binary_on_path(name: str) -> bool:
    """Return whether ``name`` resolves on PATH.

    A thin wrapper over :func:`shutil.which` so the whole runtime-requirement
    check has ONE seam a test can replace, and so ``skills.py`` reads the same
    lookup the rest of the harness does rather than growing a second PATH scan.
    """
    try:
        return shutil.which(str(name)) is not None
    except Exception:
        return False


def _interop_from_config(
    config: Optional[Mapping[str, Any]],
) -> Optional[bool]:
    """Read the interop switch from a run config, or ``None`` for the default.

    Read by key PRESENCE plus :func:`harness.tool_surface.truthiness_of`, which
    is this repository's established rule (AGT-13) rather than a second one:
    ``"0"``, ``"false"``, ``"no"`` and ``"off"`` are all false, so a typo in a
    settings file cannot be the difference between reading a user's home
    directory and not.

    Returns ``None`` for an absent key, and ``None`` means "take
    :data:`SKILL_INTEROP_ENABLED`". The three states — absent, explicitly on,
    explicitly off — are kept apart because only the middle one is a deliberate
    act and only the last one is a refusal.
    """
    if not config or SKILL_INTEROP_CONFIG_KEY not in config:
        return None
    from harness.tool_surface import truthiness_of

    return bool(truthiness_of(config.get(SKILL_INTEROP_CONFIG_KEY)))


def load_skills_for_prompt(
    skills: Sequence[Any],
    *,
    touched_paths: Optional[Iterable[str]] = None,
    path_lookup: Optional[Callable[[str], bool]] = None,
    env: Optional[Mapping[str, str]] = None,
    system: Optional[str] = None,
) -> "SkillLoadReport":
    """Return the skills ELIGIBLE for this prompt, and why the rest are not.

    This is the load-time gate the phase brief asks for, applied at the one
    place a skill list becomes model-visible.

    Three filters, in this order, and the order is a cost decision:

    1. **Runtime requirements** — a skill needing ``kubectl`` on a machine with
       no kubectl is dropped. This is the filter that stops "how to use kubectl"
       from polluting a prompt, and it is the one the corpus's OpenClaw note is
       about. **``always`` does NOT suppress this one**, and the reason is in
       the body: it is a fact about the machine, not a guess about relevance.
    2. **Path triggers** — a skill declaring ``paths: [k8s/]`` waits until the
       run has touched a matching file. Claude Code and OpenHands both gate this
       way; it is the difference between a prompt carrying one relevant skill
       and a prompt carrying all of them. **A skill declaring ``always`` skips
       this filter**, which is how a project-conventions skill stays in every
       prompt without giving up the specificity that made it worth declaring.
    3. Everything else is already filtered upstream — visibility, quarantine and
       dedup happen in :func:`discover_skills`, and this function does not
       second-guess them.

    ``touched_paths`` is the run's own set of files it has read or written. It
    is passed IN rather than read from a global so the function stays pure and a
    test can drive the whole matrix without a workspace.
    """
    touched = {
        str(item).replace("\\", "/").lstrip("./")
        for item in (touched_paths or ())
        if item
    }
    eligible: List[Any] = []
    runtime_blocked: List[Dict[str, Any]] = []
    path_blocked: List[Dict[str, Any]] = []
    for skill in skills or ():
        name = str(getattr(skill, "name", "") or "")
        blockers = skill.runtime_blockers(
            path_lookup=path_lookup, env=env, system=system
        )
        if blockers:
            runtime_blocked.append({"name": name, "blockers": blockers})
            continue
        # THE ALWAYS-ON TIER (P3b/W2.3), and the asymmetry is the whole design.
        #
        # A project-conventions skill — "how this repository does things" — must
        # be in every prompt. Before this there was **no way to say that**: a
        # skill could ungate itself by declaring no `paths:`, and any skill that
        # DID declare one was gated by a guess about which files the run would
        # touch. A convention skill therefore had to choose between being
        # ungated and being specific, and being specific silently removed it
        # from every turn that had not yet touched the matching file.
        #
        # So `always` suppresses **the path gate only**, and never the runtime
        # requirement block above. The distinction is not a preference:
        #
        # * a PATH TRIGGER is a **guess about relevance** made by the author
        #   before the run knows anything. A wrong guess costs a convention the
        #   model was supposed to be following.
        # * a RUNTIME REQUIREMENT is a **fact about the machine** ("there is no
        #   kubectl here"). Suppressing it would put instructions in the prompt
        #   that cannot be carried out, which is a different failure and a
        #   worse one.
        #
        # Everything else still applies to an always-on skill: the untrusted
        # content review, the visibility ceiling, dedup and quarantine. Those
        # are trust and author controls, not relevance guesses, and an
        # always-on skill is still untrusted content.
        if (
            skill.path_triggered
            and not getattr(skill, "always_active", False)
            and not any(skill.triggers_on(candidate) for candidate in touched)
        ):
            path_blocked.append(
                {
                    "name": name,
                    "trigger_paths": list(skill.trigger_paths),
                }
            )
            continue
        eligible.append(skill)
    return SkillLoadReport(
        eligible=eligible,
        runtime_blocked=runtime_blocked,
        path_blocked=path_blocked,
        touched=sorted(touched),
    )


@dataclass(frozen=True)
class SkillLoadReport:
    """Which skills loaded, and the named reason each excluded one did not."""

    eligible: List[Any] = field(default_factory=list)
    runtime_blocked: List[Dict[str, Any]] = field(default_factory=list)
    path_blocked: List[Dict[str, Any]] = field(default_factory=list)
    touched: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection naming every exclusion."""
        return {
            "eligible": [str(getattr(s, "name", "")) for s in self.eligible],
            "runtime_blocked": list(self.runtime_blocked),
            "path_blocked": list(self.path_blocked),
            "touched": list(self.touched),
            "counts": {
                "eligible": len(self.eligible),
                "runtime_blocked": len(self.runtime_blocked),
                "path_blocked": len(self.path_blocked),
            },
        }


# --- tier 1: the index, which is ALL the prompt ever carries -----------------

#: The prompt-side switch. **Absent means OFF**, which keeps every existing
#: run's prompt byte-identical; the arm that enables it is an eval arm, which is
#: the only way a prompt change is allowed to land in this project.
SKILLS_PROGRESSIVE_CONFIG_KEY = "skills_progressive"

#: The bridge tool's name and spec. Not a catalog entry, for the same reason
#: ``tool_search`` is not: it is the bridge, not a bridged artifact, and keeping
#: it out of ``harness/tools.py`` leaves that catalog's fingerprint unmoved.
SKILL_VIEW_NAME = "skill_view"


def progressive_skills_enabled(config: Optional[Mapping[str, Any]]) -> bool:
    """Whether this run's config asks for three-tier skill disclosure.

    **One reader, because two call sites answered this differently.** The kernel's
    handler registration tested ``SKILLS_PROGRESSIVE_CONFIG_KEY in cfg`` while the
    scan path decided on truthiness, so ``{"skills_progressive": False}`` reached
    a run whose ``skill_view`` answers every call with the skill blocked or
    absent — a bridge to a mechanism that is off. Same defect, same fix, and the
    same reasoning as :func:`harness.deferred_tools.deferral_enabled`: a bridge
    whose mechanism is disabled is a tool that reports a false negative about
    content the model can plainly see.

    An ABSENT key is OFF. That is the whole point of keeping the key out of
    ``harness/config.py::DEFAULTS`` — a merged default would switch every task
    and every eval arm onto a different prompt shape — so OFF is also the
    byte-identical-to-pre-Phase-3b answer, which is what keeps the prompt
    regression matrix comparable.
    """
    if not config or SKILLS_PROGRESSIVE_CONFIG_KEY not in config:
        return False
    from harness.tool_surface import truthiness_of

    return bool(truthiness_of(config.get(SKILLS_PROGRESSIVE_CONFIG_KEY)))


def _skill_view_spec() -> Any:
    """Return the ``skill_view`` bridge spec.

    Built lazily and cached, because it needs ``harness.tools.ToolSpec`` and
    this module must stay importable without the typed catalog (it is imported
    by the context compiler on paths where only the skill layer is needed).
    """
    global _SKILL_VIEW_SPEC
    if _SKILL_VIEW_SPEC is not None:
        return _SKILL_VIEW_SPEC
    try:
        from harness.tools import ToolSpec
    except Exception:  # pragma: no cover - import guard
        return None
    _SKILL_VIEW_SPEC = ToolSpec(
        name=SKILL_VIEW_NAME,
        side_effect_class="control",
        required=("name",),
        optional=("file",),
        types={"name": str, "file": str},
        description=(
            "Load one skill's full instructions, or one of its linked files, "
            "on demand. The prompt lists each skill's name and what it is for "
            "but not its body; call this when a listed skill applies."
        ),
    )
    return _SKILL_VIEW_SPEC


_SKILL_VIEW_SPEC: Any = None


def skill_view_spec() -> Any:
    """Return the ``skill_view`` spec, or ``None`` when unavailable."""
    return _skill_view_spec()


def skill_metadata(skill: Any) -> Dict[str, Any]:
    """Return everything about a skill that is SAFE to put in the prompt.

    **No body.** That is the entire point of tier 1, and the omission is
    structural rather than a filter: this function builds a new dict from named
    fields, so there is no code path by which a body could reach it. A renderer
    that took the whole skill and dropped the body would break the day somebody
    added a field.

    What IS here: the name (the invocation handle), the description (the trigger
    condition), the origin (provenance, and the user is entitled to know a skill
    came from another tool's directory), the cost of loading it (so a model can
    weigh it), and the path triggers (so a model knows a skill is waiting on a
    file it has not touched yet).
    """
    cost = skill_token_cost(skill)
    return {
        "name": str(getattr(skill, "name", "") or ""),
        "description": str(getattr(skill, "description", "") or ""),
        "origin": str(getattr(skill, "origin", "") or ""),
        "body_tokens": int(cost.get("body_tokens", 0)),
        "supporting_files": int(cost.get("supporting_files", 0)),
        "trigger_paths": list(getattr(skill, "trigger_paths", ()) or ()),
        "tainted": bool(getattr(skill, "tainted", False)),
    }


def render_skill_index(skills: Sequence[Any], *, max_chars: int = 2500) -> str:
    """Render tier 1: name + description + cost, and NOT a single body.

    Bounded, and the bound is reported: a truncation marker says how many rows
    were dropped, because a silently short index reads as the whole
    catalogue and would make a model conclude a skill does not exist.
    """
    rows = list(skills or ())
    if not rows:
        return NONE_MATCHED
    lines: List[str] = []
    used = 0
    cap = max(1, int(max_chars))
    for index, skill in enumerate(rows):
        meta = skill_metadata(skill)
        line = f"- {meta['name']} ({meta['origin']}): {meta['description']}"
        if meta["trigger_paths"]:
            line += f" [applies once you touch: {', '.join(meta['trigger_paths'])}]"
        if meta["tainted"]:
            line += " [FLAGGED UNTRUSTED CONTENT]"
        line += f" (~{meta['body_tokens']} tokens to load)"
        remaining = cap - used
        if remaining <= 0:
            break
        if len(line) + 1 > remaining:
            marker = f"... [{len(rows) - index} more skills not shown]"
            if len(marker) <= remaining:
                lines.append(marker)
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines).strip()[:cap]


def skill_view(name: str, skills: Sequence[Any]) -> Dict[str, Any]:
    """Tier 2: return one skill's body on explicit request.

    The load half of progressive disclosure, and the ONLY place a skill body
    enters a request after discovery. A refusal — an unknown name, a
    runtime-ineligible skill, a path-gated skill that has not triggered — comes
    back as ``loaded: False`` WITH the reason, because a silent empty result is
    indistinguishable from a skill with an empty body.

    ``file=`` reaches tier 3: one of the skill's ``references/`` or
    ``scripts/`` files, through :func:`resolve_support_file`, which is
    containment-checked. A viewer that could read ``../secrets.md`` would turn
    progressive disclosure into a file-disclosure primitive, so the third tier
    goes through the existing guarded reader rather than opening files here.
    """
    wanted = str(name or "").strip()
    clean = wanted.split(":", 1)[1].strip() if wanted.startswith("plugin:") else wanted
    for skill in skills or ():
        if str(getattr(skill, "name", "") or "") != clean:
            continue
        blockers = skill.runtime_blockers()
        if blockers:
            return {
                "name": clean,
                "loaded": False,
                "reason": "; ".join(blockers),
                "text": "",
                "chars": 0,
                "tokens": 0,
            }
        relative = ""
        if wanted.startswith("plugin:"):
            relative = ""
        row: Dict[str, Any] = {
            "name": clean,
            "origin": str(getattr(skill, "origin", "") or ""),
            "loaded": True,
            "reason": "",
            "text": "",
            "chars": 0,
            "tokens": 0,
        }
        if relative:
            support = resolve_support_file(skill, relative)
            support["name"] = clean
            return support
        text = str(getattr(skill, "body", "") or "")
        row["text"] = text
        row["chars"] = len(text)
        row["tokens"] = estimate_tokens(text)
        if getattr(skill, "tainted", False):
            row["tainted"] = sorted(getattr(skill.review, "categories", ()) or ())
        return row
    return {
        "name": clean,
        "loaded": False,
        "reason": (
            f"no skill named {clean!r} is available in this session; the index "
            "lists the ones that are"
        ),
        "text": "",
        "chars": 0,
        "tokens": 0,
    }


def skills_prompt_report(
    skills: Sequence[Any],
    *,
    max_chars: int = 2500,
) -> Dict[str, Any]:
    """Measure what tier 1 costs against what the bodies would have cost.

    The before/after row the phase's exit gate asks for, computed from the same
    estimator authority the prompt budget uses (:func:`estimate_tokens`, which
    delegates to ``TokenEstimator``) so the two numbers are comparable.
    """
    rows = list(skills or ())
    index_text = render_skill_index(rows, max_chars=max_chars)
    bodies = "".join(str(getattr(skill, "body", "") or "") for skill in rows)
    supporting = sum(
        int(row.get("tokens", 0)) for skill in rows for row in supporting_files(skill)
    )
    index_tokens = estimate_tokens(index_text)
    body_tokens = estimate_tokens(bodies)
    return {
        "skills": len(rows),
        "index_tokens": index_tokens,
        "index_chars": len(index_text),
        "bodies_tokens": body_tokens,
        "bodies_chars": len(bodies),
        "supporting_tokens": supporting,
        "supporting_loaded": 0,
        "saving_tokens": body_tokens - index_tokens,
        "saving_ratio": (
            (body_tokens - index_tokens) / body_tokens if body_tokens else None
        ),
        "note": (
            "index_tokens is what the prompt carries; bodies_tokens is what the "
            "same set would cost with every body preloaded; supporting files "
            "are tier 3 and are never counted as preloaded"
        ),
    }


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
    interop: Optional[bool] = None,
    interop_home: Optional[bool] = None,
) -> List[Skill]:
    """Scan project, global, plugin, interop, and extra skill locations.

    Roots are evaluated in project > interop > global > plugin > extra order.
    The optional ``diagnostics`` list receives malformed or unreadable existing
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

    ``interop`` is a per-call override for the cross-agent roots
    (``.agents/skills``, ``.claude/skills``, ``~/.claude/skills``). ``None``
    takes :data:`SKILL_INTEROP_ENABLED`; an explicit ``False`` reads none of
    another tool's directories for THIS call, which is what a user who wants
    their personal Claude Code skills out of a benchmark run needs.

    **It is threaded as an argument, not implemented by flipping the module
    global.** A module-level flag read by a helper is process-wide state, and
    this scan runs concurrently with other scans in a hosted agent; a temporary
    flip would let one caller's ``interop=False`` silently disable interop for
    every other caller in flight.
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
    # P3b/W1.2: the interoperability roots, immediately after the project's own
    # and therefore at HIGHER precedence than every global root. A project that
    # ships `.agents/skills` overrides a user's personal `~/.claude/skills`
    # entry of the same name, which is what every system reading both does.
    roots.extend(_interop_roots(repo_path, enabled=interop, include_home=interop_home))
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
    return len(_skill_words(skill) & task_words)


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


def _skill_words(skill: Skill) -> set:
    """Return every word that makes this skill relevant.

    Three sources, and the third is the one P3b/W2.3 added:

    * the description,
    * the name,
    * the author-declared ``match:`` vocabulary.

    **Why the third exists, measured rather than assumed.** The stopword table
    (:data:`_STOPWORDS`) drops the domain-generic words a convention skill's
    relevance is *built out of* — ``test``, ``fix``, ``python``, ``bug`` — on
    the deliberate rule that "generic words never trigger". The consequence was
    measured on a labelled corpus: ``pytest-conventions``, whose description is
    *"Use when fixing bugs in Python repos whose tests run under pytest"*, had an
    **EMPTY** word overlap with every task in a pytest repository, because the
    task side contributes ``tests/test_cart.py`` and every word out of that
    (``tests``, ``test``) is a stopword. The one noun that could have carried it
    is the same noun the stopword table removes.

    So a skill whose relevance is carried by a domain-standard noun is
    **unreachable by description alone** — not badly matched, unreachable. That
    is a different defect from a weak description and it is not fixable by
    rewriting prose, because the words that would fix it are exactly the words
    the matcher discards.

    Two alternatives were considered and rejected:

    * **Un-drop the stopwords.** That widens matching globally, and Wave 1
      already recorded the failure mode that creates: with interop roots in
      scope, "a description that is a keyword salad matches almost anything" —
      a real probe caught a UI/UX skill matching *"an unrelated topic about
      numbers"* on the stopwords ``an`` and ``topic``. Trading a measured
      false-negative class for a measured false-positive class across every
      interop skill is a retrieval-quality decision, not a side effect of this
      round.
    * **Fall back to substring matching.** Wave 1 rejected it: ``an`` matches
      inside ``enhance``.
    """
    words = _words(skill.description) | _words(skill.name)
    for term in getattr(skill, "match_terms", ()) or ():
        words |= _words(str(term))
    return words


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
    #: ALWAYS-ON SKILLS ARE RANKED UNCONDITIONALLY (P3b/W2.3).
    #:
    #: They are collected here and placed FIRST, and that placement is the fix.
    #: The first version of the always-on tier only removed the PATH gate, and
    #: the measurement it was built for immediately showed the tier was
    #: **decorative**: `repo-conventions` became eligible on every task and was
    #: then dropped by the relevance matcher on four of six, because
    #: "how this repository does things" shares no meaningful word with
    #: "wire the deferred-tool floor into the strategy". A convention skill that
    #: is offered and then filtered is not in the prompt, so the tier was a
    #: promise the loader did not keep.
    #:
    #: Two properties make the unconditional placement correct rather than
    #: merely forceful:
    #:
    #: * it is bounded by construction — it is keyed on the skill's OWN
    #:   ``always:`` declaration, which is inert frontmatter an author writes in
    #:   their own repository's skill file, so the set is reviewable and cannot
    #:   grow by accident;
    #: * the runtime gate has ALREADY run by the time a skill reaches here
    #:   (``load_skills_for_prompt`` drops unmet requirements before relevance is
    #:   considered), so ``always`` still cannot put a skill in the prompt that
    #:   this machine cannot execute.
    always_on: List[tuple[Dict[str, Any], Skill]] = []
    for skill in skills:
        skill_words = _skill_words(skill)
        issue_matches = sorted(skill_words & issue_words)
        retrieval_matches = sorted(skill_words & retrieval_words)
        repo_matches = sorted(skill_words & repo_vocabulary)
        matched = sorted(set(issue_matches + retrieval_matches + repo_matches))
        is_always_on = bool(getattr(skill, "always_active", False))
        if not matched and not is_always_on:
            continue
        reasons: List[str] = []
        if issue_matches:
            reasons.append("issue: " + ", ".join(issue_matches))
        if retrieval_matches:
            reasons.append("retrieval: " + ", ".join(retrieval_matches))
        if repo_matches:
            reasons.append("repository: " + ", ".join(repo_matches))
        if is_always_on:
            # The reason is stated in the receipt rather than left implicit,
            # because a skill ranked with a score of 0 and no stated reason reads
            # as a matcher bug rather than as the tier working.
            reasons.append(
                "always-on: this skill declares it applies to every task, so it "
                "is not relevance-gated"
            )
        receipt = {
            "name": skill.name,
            "origin": skill.origin,
            "source": skill.source,
            "score": len(matched),
            "always_on": is_always_on,
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
            # show what a skill asked for without implying that it was granted.
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
        if is_always_on:
            always_on.append((receipt, skill))
        else:
            ranked.append((receipt, skill))
    always_on.sort(key=_receipt_sort_key)
    ranked.sort(key=_receipt_sort_key)
    return (always_on + ranked)[: max(1, int(max_skills))]


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
    run_config: Optional[Mapping[str, Any]] = None,
    visible_tools: Sequence[str] = (),
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
            # P3b/W1.2: the per-call interop override, so a hermetic scan (a
            # benchmark, a test) does not read the developer's home directory.
            interop=_interop_from_config(run_config),
            interop_home=_interop_home_enabled(run_config),
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
        # P2/W1-T4: the ONE production call site for ``extensions.skill_policy``.
        #
        # It runs on the RENDERED skills -- not on every discovered one and not on
        # the receipts -- because a declaration only has a meaning once its body
        # has actually reached the model. Enforcing on a skill that was omitted
        # for budget would report a refusal for something that never happened.
        #
        # ``run_config`` is None unless a caller passes one, and the whole block
        # is gated on KEY PRESENCE inside ``enforce_skill_policy``, so every
        # existing caller is byte-identical. That is the same discipline
        # ``harness/config.py``'s own comment documents: a default in DEFAULTS is
        # merged into every task and every eval arm.
        if run_config is not None:
            out["policy"] = enforce_skill_policy(
                out,
                run_config,
                skills=matched,
                visible_tools=visible_tools,
            )
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
