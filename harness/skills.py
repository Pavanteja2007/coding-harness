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
  project:  <repo>/.vex/skills/<name>/SKILL.md
  global:   ~/.config/vex/skills/<name>/SKILL.md
  plugin:   ~/.config/vex/plugins/<plugin>/skills/<name>/SKILL.md
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

Config (task.config, defaults in harness/config.py):
  skills_enabled      master switch (False = skip the scan entirely)
  skills_max          max skills injected into one plan (default 3)
  skills_max_chars    combined char cap on the section (default 2500)
  skills_roots        extra skill search roots (list; default roots
                      are always scanned too; plugins/tests use this)

Trace: one `skills` event per plan {matched: [names], considered: N,
section_chars: M, skipped?: reason} — the scan is auditable even when
nothing applies (matched: [] + the reason), like decision_memory.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "Skill",
    "discover_skills",
    "find_applicable_skills",
    "render_skills_block",
    "scan_skills_for_task",
]

# Frontmatter: a leading `---` block of `key: value` lines. Tolerates a
# missing frontmatter (body-only SKILL.md still loads; name falls back
# to the folder name, description to "").
_FM_BOUNDARY = re.compile(r"^---\s*$")
_FM_LINE = re.compile(r"^([A-Za-z][\w-]*)\s*:\s*(.*)$")

# Safety cap on ONE SKILL.md body read (a pathological/huge skill must
# not blow the harness's memory, let alone the prompt).
_MAX_BODY_CHARS = 20_000

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

    Attributes: name, description (frontmatter; name falls back to the
    folder name), body (the markdown instructions after the frontmatter,
    capped at _MAX_BODY_CHARS), source (the file's path string), origin
    ("project" | "global" | "plugin" | "extra" — for prompt attribution
    and trace events).
    """

    __slots__ = ("body", "description", "name", "origin", "source")

    def __init__(
        self, name: str, description: str, body: str, source: str, origin: str
    ) -> None:
        self.name = name
        self.description = description
        self.body = body
        self.source = source
        self.origin = origin


def _parse_skill_md(path: Path, origin: str) -> Optional[Skill]:
    """Parse one SKILL.md into a Skill; None when unreadable/empty.

    Never raises: OSError/UnicodeDecodeError/malformed frontmatter all
    degrade to None (the skill is skipped; discovery must not die over
    one bad markdown file).
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    meta: Dict[str, str] = {}
    body_start = 0
    if lines and _FM_BOUNDARY.match(lines[0]):
        for i in range(1, len(lines)):
            if _FM_BOUNDARY.match(lines[i]):
                body_start = i + 1
                break
            m = _FM_LINE.match(lines[i])
            if m:
                meta[m.group(1).lower()] = m.group(2).strip()
    name = meta.get("name") or path.parent.name
    description = meta.get("description") or ""
    body = "\n".join(lines[body_start:]).strip()[:_MAX_BODY_CHARS]
    if not body:
        return None  # frontmatter-only file carries nothing to apply
    return Skill(
        name=str(name)[:80],
        description=str(description)[:1000],
        body=body,
        source=str(path),
        origin=origin,
    )


def _global_skills_root() -> Path:
    """~/.config/vex/skills (per the brief's global location contract)."""
    return Path.home() / ".config" / "vex" / "skills"


def discover_skills(
    repo_path: Optional[str] = None,
    extra_roots: Optional[List[str]] = None,
) -> List[Skill]:
    """Scan project + global (+ plugin + extra) skill locations.

    Assumes repo_path is the task's repo (project skills live under
    <repo>/.vex/skills/) and extra_roots are additional roots (config
    "skills_roots" — plugin installs and tests pin explicit roots).
    Plugin bundles contribute ~/.config/vex/plugins/*/skills/<name>/.
    Later roots NEVER override an earlier root's skill with the same
    name (project > global > plugin > extra ordering — the specific
    beats the general). Returns skills sorted by name for stable
    prompts; discovery never raises on a missing/unreadable root.
    """
    roots: List[tuple] = []
    if repo_path:
        roots.append((Path(repo_path) / ".vex" / "skills", "project"))
    roots.append((_global_skills_root(), "global"))
    plugins_root = Path.home() / ".config" / "vex" / "plugins"
    if plugins_root.is_dir():
        try:
            for plugin_dir in sorted(plugins_root.iterdir()):
                if plugin_dir.is_dir() and not plugin_dir.name.startswith("."):
                    # A `<name>.disabled` marker beside the directory mutes
                    # the plugin: its skills stay installed but undiscovered
                    # (`vex plugin disable`; re-enabled by `enable`).
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
    for r in extra_roots or []:
        roots.append((Path(r), "extra"))

    by_name: Dict[str, Skill] = {}
    for root, origin in roots:
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            skill = _parse_skill_md(entry / "SKILL.md", origin)
            if skill is None:
                continue
            # first root wins: project beats global beats plugin beats extra
            by_name.setdefault(skill.name, skill)
    return [by_name[n] for n in sorted(by_name)]


def _words(text: str) -> set:
    """Lowercased identifier-split words, stopwords dropped.

    Splits on non-alphanumerics AND camelCase boundaries (a description
    saying "DjangoConventions" matches an issue saying "django
    conventions" — same decomposition discipline as retrieval's subword
    symbol matching).
    """
    out = set()
    for tok in re.findall(r"[A-Za-z0-9]+", text or ""):
        tok = tok.lower()
        # camelCase split: lower RUN gets a leading boundary when it
        # follows an uppercase run (SimpleToken -> simple token)
        parts = re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|\d+", tok)
        for p in parts:
            p = p.lower()
            if p and p not in _STOPWORDS and len(p) > 1:
                out.add(p)
    return out


def _relevance(skill: Skill, task_words: set) -> int:
    """Overlap score between a skill's description and the task words.

    A skill with no description can still match on its NAME alone (a
    "django-conventions" skill for a Django repo is the canonical case
    the brief names — the folder name itself is the signal when the
    author wrote no description).
    """
    desc_words = _words(skill.description) | _words(skill.name)
    return len(desc_words & task_words)


def find_applicable_skills(
    skills: List[Skill],
    issue_text: str,
    retrieval_terms: Optional[List[str]] = None,
    repo_path: Optional[str] = None,
    max_skills: int = 3,
) -> List[Skill]:
    """Rank skills against the task; return the plausibly-applicable ones.

    Task vocabulary = issue words + retrieval terms + the repo's own
    path/name segments (a repo at .../my-django-app matches a django
    skill even when the issue never says "django" — the same
    repo-segment discipline decision memory uses). A skill applies when
    its description/name shares >= 1 meaningful word with the task
    vocabulary (conservative keyword overlap; no embeddings — same
    honesty as the rest of the stack, documented in the module
    docstring). Ties break by name for stable prompts.
    """
    vocab = _words(issue_text or "")
    vocab |= _words(" ".join(retrieval_terms or []))
    if repo_path:
        vocab |= _words(str(Path(repo_path).name))
    scored = []
    for skill in skills:
        score = _relevance(skill, vocab)
        if score > 0:
            scored.append((score, skill.name, skill))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [s for _, _, s in scored[: max(1, int(max_skills))]]


def render_skills_block(skills: List[Skill], max_chars: int = 2500) -> str:
    """Render matched skills as the planner-prompt section body.

    Each skill renders as a header + its body (capped); the section is
    capped at max_chars with an explicit truncation marker. Empty input
    renders as an explicit "(none matched)" so the model knows skills
    were CONSIDERED and had nothing — same contract as render_memory_block.
    """
    if not skills:
        return "(none matched)"
    lines: List[str] = []
    used = 0
    for skill in skills:
        header = f"### Skill: {skill.name} (from {skill.origin} skills)"
        chunk = f"{header}\n{skill.body}\n"
        if used + len(chunk) > max_chars and lines:
            lines.append(
                f"... [{len(skills) - len([l for l in lines if l.startswith('### Skill:')])} more truncated]"
            )
            break
        lines.append(chunk)
        used += len(chunk)
    return "\n".join(lines).strip()[:max_chars]


def scan_skills_for_task(
    repo_path: str,
    issue_text: str,
    retrieval_terms: Optional[List[str]] = None,
    extra_roots: Optional[List[str]] = None,
    max_skills: int = 3,
    max_chars: int = 2500,
) -> Dict[str, Any]:
    """The planner-time entry point: discover, match, render.

    Returns {"skills_block": str, "matched": [names], "considered": N,
    "skipped": Optional[str], "error": None} — never raises (a broken
    scan degrades to block="(none matched)" with skipped set, the
    planner runs exactly as before, trace event records why).
    """
    out: Dict[str, Any] = {
        "skills_block": "(none matched)",
        "matched": [],
        "considered": 0,
        "skipped": None,
        "error": None,
    }
    try:
        skills = discover_skills(repo_path=repo_path, extra_roots=extra_roots)
    except Exception as exc:  # never raise into the planning path
        out["error"] = f"skill discovery failed: {exc}"
        return out
    out["considered"] = len(skills)
    if not skills:
        out["skipped"] = "no skills found"
        return out
    try:
        matched = find_applicable_skills(
            skills,
            issue_text=issue_text,
            retrieval_terms=retrieval_terms,
            repo_path=repo_path,
            max_skills=max_skills,
        )
    except Exception as exc:
        out["error"] = f"skill matching failed: {exc}"
        return out
    out["matched"] = [s.name for s in matched]
    out["skills_block"] = render_skills_block(matched, max_chars=max_chars)
    return out
