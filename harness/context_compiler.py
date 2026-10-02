"""Cited, token-budgeted context compilation for repository work.

The compiler combines repository structure, instructions, task criteria,
selected source ranges, recent turns, learned decisions, skills, diagnostics,
and dependency context. It is deterministic and lexical/structural; it does
not claim semantic or embedding retrieval. Every included source is represented
by a stable citation and a lossless reference record so a later compaction can
retrieve the original source instead of treating a summary as authoritative.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from harness import retrieval
from harness.redaction import redact_text_for_journal

_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md", "QWEN.md")
_NEO_DIRECT_FILES = (
    "AGENTS.md",
    "INSTRUCTIONS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "QWEN.md",
)
_MAX_INSTRUCTION_BYTES = 64_000
_DEFAULT_TOTAL_TOKENS = 12_000
_DEFAULT_ROLE_WEIGHTS = {
    "project_instructions": 0.18,
    "acceptance_criteria": 0.14,
    "active_task": 0.12,
    "repository_map": 0.12,
    "selected_symbols": 0.12,
    "selected_files": 0.10,
    "recent_turns": 0.06,
    "decision_memory": 0.05,
    "skills": 0.04,
    "diagnostics": 0.03,
    "dependency_context": 0.04,
}
_MANDATORY_ROLES = ("project_instructions", "acceptance_criteria")
_SAFE_URL_USERINFO = re.compile(
    r"(?i)([a-z][a-z0-9+.-]{0,63}://[^/\s:@]{1,256}:)[^@\s/]{1,256}@"
)


def _redact_large(value: str) -> str:
    """Apply the shared secret policy without its unbounded URL matcher.

    **Kept only as the large-input fast path, and re-pointed at the one
    authority.** This function used to be a SECOND redaction implementation:
    it reached into ``shared.security``'s private compiled patterns
    (``_QUOTED_SECRET``, ``_KEY_VALUE_SECRET``, ``_SECRET_PATTERNS``,
    ``_URL_QUERY_SECRET``, ``_CLI_SECRET``, ``_redact_explicit``) and, on any
    exception, fell back to a local ``re.sub`` for a ``bearer`` prefix alone -
    a pattern set narrow enough to let an ``sk-...`` key straight through. That
    is exactly the divergence ``harness/trace.py``'s module docstring records
    this repository having already paid for once, on a path that carries
    repository file content into model context.

    The fast path itself is now **unnecessary and was measured to be**: the
    authority is linear (see the measurement in ``harness/AGENTS.md`` - 16 kB
    0.008 s, 200 kB 0.153 s, 1 MB 0.886 s, and 1 MB of a single repeated
    character 0.817 s, i.e. linear in both length and character class), because
    ``shared.security`` bounds its scan span. The old reason for this function
    ("without its unbounded URL matcher") no longer holds.

    It is therefore a thin delegate now, kept so a caller's import keeps
    working. The private-name coupling is the remaining problem and is filed
    as a cross-terminal request rather than papered over: T5 should expose a
    documented bounded entry point in ``shared.security`` so no consumer has to
    reach into its privates.
    """
    return redact_text_for_journal(value, where="context_compiler._redact_large")


def _redact(value: Any) -> str:
    """Return redacted text for untrusted repository or session content.

    **Redaction boundary (decision: redact AT THE BOUNDARY, here).** This is
    the one place repository and session content becomes text a model reads,
    so it is where the value is redacted once — the compiled block, the
    instruction files, the repository map and the cited sources all pass
    through ``_as_text`` on their way to a request.

    It previously carried a 16 KiB marker gate that returned the RAW string on
    a marker miss. A marker list is not a secret detector: a credential shape
    absent from that ten-alternative regex over a long file would have reached
    the model verbatim. The gate is removed and the value goes to the authority
    every time; the measurement above is why that is affordable.

    Fail-closed, matching ``cli.notify._redact``: if the authority raises or
    answers ``None`` the value is REPLACED with ``(detail withheld: ...)``.
    The previous ``except`` branch fell back to a narrower local pattern set,
    which is fail-OPEN on a path that carries file content.
    """
    raw = str(value or "")
    return redact_text_for_journal(raw, where="context_compiler._redact")


def _as_text(value: Any) -> str:
    """Convert a value to safe text without allowing a traceback."""
    if value is None:
        return ""
    try:
        return _redact(value)
    except Exception:
        return ""


def _canonical_path(root: Path, value: str) -> str:
    """Normalize a repo-relative path and reject escapes and symlinks."""
    text = str(value or "").replace("\\", "/").strip()
    if not text or "\x00" in text or Path(text).is_absolute():
        return ""
    if re.match(r"^[A-Za-z]:[\\/]", text):
        return ""
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return ""
    try:
        current = root
        for part in parts:
            current = current / part
            if current.is_symlink():
                return ""
        candidate = current.resolve()
        relative = candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return ""
    return relative.as_posix()


def _repo_relative(root: Path, value: Any) -> str:
    """Normalize an absolute or relative path to a safe repo-relative path."""
    text = str(value or "").replace("\\", "/").strip()
    if not text or "\x00" in text:
        return ""
    candidate = Path(text)
    if not candidate.is_absolute():
        if any(part in ("", ".", "..") for part in text.split("/")):
            return ""
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
        relative = resolved.relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return ""
    except (OSError, RuntimeError, ValueError):
        return ""
    return relative.as_posix()


def _contained_file(root: Path, path: Path) -> Optional[Path]:
    """Return a regular contained file, refusing symlink components."""
    try:
        current = root
        for part in path.relative_to(root).parts:
            current = current / part
            if current.is_symlink():
                return None
        resolved = current.resolve()
        resolved.relative_to(root)
        return resolved if resolved.is_file() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _digest_text(value: str) -> str:
    """Return a stable digest for text content."""
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()


def _json_digest(value: Any) -> str:
    """Return a stable digest for JSON-compatible values."""
    try:
        payload = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
    except (TypeError, ValueError):
        payload = repr(value)
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


def estimate_tokens(
    text: Any, provider: Optional[str] = None, chars_per_token: Optional[int] = None
) -> int:
    """Estimate model tokens with a conservative provider-aware heuristic."""
    if chars_per_token is None:
        chars_per_token = 4
    try:
        size = max(1, int(chars_per_token))
    except (TypeError, ValueError):
        size = 4
    value = _as_text(text)
    return math.ceil(len(value) / size) if value else 0


def provider_chars_per_token(
    provider: Optional[str], config: Optional[Mapping[str, Any]] = None
) -> int:
    """Return a configurable character-to-token ratio for a provider."""
    values = dict(config or {})
    explicit = values.get("context_chars_per_token")
    if explicit is None:
        by_provider = values.get("context_provider_chars_per_token")
        if isinstance(by_provider, Mapping):
            explicit = by_provider.get(str(provider or "default"))
    try:
        return max(1, int(explicit or 4))
    except (TypeError, ValueError):
        return 4


def allocate_token_budgets(
    total_tokens: int,
    roles: Optional[Sequence[str]] = None,
    provider: Optional[str] = None,
    role_weights: Optional[Mapping[str, float]] = None,
    provider_weights: Optional[Mapping[str, float]] = None,
    mandatory_roles: Sequence[str] = _MANDATORY_ROLES,
) -> Dict[str, int]:
    """Allocate a total token budget across context roles.

    Mapping provider weights are role-relative multipliers and are normalized
    so they never increase the caller-provided total. A scalar provider value
    is a uniform multiplier and is therefore allocation-neutral after
    normalization. Mandatory roles receive at least one token when the total
    budget permits it.
    """
    try:
        total = max(1, int(total_tokens))
    except (TypeError, ValueError):
        total = _DEFAULT_TOTAL_TOKENS
    names = list(
        dict.fromkeys(
            str(role) for role in (roles or _DEFAULT_ROLE_WEIGHTS) if str(role)
        )
    )
    if not names:
        names = list(_DEFAULT_ROLE_WEIGHTS)
    weights_source = dict(_DEFAULT_ROLE_WEIGHTS)
    if isinstance(role_weights, Mapping):
        for key, value in role_weights.items():
            try:
                parsed = float(value)
                weights_source[str(key)] = parsed if math.isfinite(parsed) else 1.0
            except (TypeError, ValueError):
                continue
    mandatory_names = {
        str(item)
        for item in (
            [mandatory_roles]
            if isinstance(mandatory_roles, str)
            else list(mandatory_roles or _MANDATORY_ROLES)
        )
    }
    provider_value: Any = 1.0
    if provider and isinstance(provider_weights, Mapping):
        provider_value = provider_weights.get(str(provider), 1.0)
    provider_role_weights = (
        provider_value if isinstance(provider_value, Mapping) else {}
    )
    try:
        provider_scalar = float(provider_value)
        provider_scalar = (
            max(0.1, provider_scalar) if math.isfinite(provider_scalar) else 1.0
        )
    except (TypeError, ValueError):
        provider_scalar = 1.0
    raw: Dict[str, float] = {}
    for role in names:
        try:
            value = float(weights_source.get(role, 1.0))
            if not math.isfinite(value):
                value = 1.0
        except (TypeError, ValueError):
            value = 1.0
        try:
            role_multiplier = float(
                provider_role_weights.get(
                    role,
                    provider_scalar,
                )
            )
            if not math.isfinite(role_multiplier):
                role_multiplier = 1.0
        except (TypeError, ValueError):
            role_multiplier = provider_scalar
        raw[role] = max(0.001, value) * max(0.1, role_multiplier)
    denominator = sum(raw.values()) or 1.0
    budgets = {
        role: max(0, int(total * value / denominator)) for role, value in raw.items()
    }
    remainder = total - sum(budgets.values())
    for role in sorted(raw, key=lambda item: (-raw[item], item)):
        if remainder <= 0:
            break
        budgets[role] += 1
        remainder -= 1
    mandatory = [role for role in mandatory_names if role in budgets]
    if total >= len(mandatory):
        for role in mandatory:
            if budgets[role] != 0:
                continue
            donor = max(
                (
                    name
                    for name in budgets
                    if name != role and budgets[name] > (1 if name in mandatory else 0)
                ),
                key=lambda name: (budgets[name], name),
                default=None,
            )
            if donor is not None:
                budgets[donor] -= 1
            budgets[role] = 1
    excess = sum(budgets.values()) - total
    while excess > 0:
        donor = max(
            (
                name
                for name in budgets
                if budgets[name] > (1 if name in mandatory else 0)
            ),
            key=lambda name: (budgets[name], name),
            default=None,
        )
        if donor is None:
            break
        budgets[donor] -= 1
        excess -= 1
    if not any(budgets.values()):
        budgets[names[0]] = total
    return budgets


def token_budgets_by_role(*args: Any, **kwargs: Any) -> Dict[str, int]:
    """Compatibility alias for role/provider token allocation."""
    return allocate_token_budgets(*args, **kwargs)


def estimate_context_tokens(*args: Any, **kwargs: Any) -> int:
    """Compatibility alias for provider-aware token estimation."""
    return estimate_tokens(*args, **kwargs)


def _root(repo_path: Any) -> Optional[Path]:
    """Resolve a readable repository root, refusing an empty path."""
    text = _as_text(repo_path).strip()
    if not text:
        return None
    try:
        root = Path(text).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return root if root.is_dir() else None


def _read_instruction(
    root: Path, path: Path, max_chars: int
) -> Optional[Dict[str, Any]]:
    """Read one bounded instruction file and retain a retrievable source reference."""
    contained = _contained_file(root, path)
    if contained is None:
        return None
    try:
        total_bytes = int(contained.stat().st_size)
        raw = contained.read_bytes()[: _MAX_INSTRUCTION_BYTES + 1]
        source_truncated = total_bytes > len(raw)
        raw = raw[:_MAX_INSTRUCTION_BYTES]
        if b"\x00" in raw:
            return None
        content = _as_text(raw.decode("utf-8", errors="replace"))
    except (OSError, UnicodeError, TypeError, ValueError):
        return None
    if not content.strip():
        return None
    full_content = content
    limit = max(1, int(max_chars))
    truncated = len(content) > limit
    if truncated:
        content = content[:limit].rstrip()
    relative = path.relative_to(root).as_posix()
    digest = retrieval.file_digest(contained)
    source_ref = {
        "kind": "file",
        "path": relative,
        "offset": 0,
        "length": total_bytes,
        "digest": digest,
    }
    citation = retrieval.make_citation(
        "instruction",
        relative,
        1,
        max(1, len(content.splitlines())),
        digest,
        "project_instructions",
    )
    return {
        "source": "instruction",
        "path": relative,
        "relative_path": relative,
        "scope": str(Path(relative).parent.as_posix()),
        "precedence": 0,
        "content": content,
        "full_content": full_content,
        "chars": len(content),
        "truncated": truncated or source_truncated,
        "source_truncated": source_truncated,
        "source_bytes_read": len(raw),
        "source_total_bytes": total_bytes,
        "source_ref": source_ref,
        "digest": digest,
        "citation": citation,
    }


def discover_project_instructions(
    repo_path: Any,
    target_path: Any = None,
    max_chars: int = 48_000,
) -> Dict[str, Any]:
    """Discover root-to-target project instructions in deterministic order.

    Neo instruction files and compatible AGENTS.md, CLAUDE.md, GEMINI.md, and
    QWEN.md conventions are supported. Learned decisions are never read here;
    the result contains only repository instruction sources.
    """
    root = _root(repo_path)
    if root is None:
        return {
            "repo": _as_text(repo_path),
            "files": [],
            "omitted": [],
            "warnings": ["repository is not a readable directory"],
            "text": "",
            "source_report": [],
        }
    try:
        target = Path(_as_text(target_path)).expanduser() if target_path else root
        if not target.is_absolute():
            target = root / target
        target = target.resolve()
        target.relative_to(root)
        target_dir = target if target.is_dir() else target.parent
        relative_target = target_dir.relative_to(root)
        parts = relative_target.parts
    except (OSError, RuntimeError, ValueError):
        parts = ()
    candidates: List[Tuple[Path, int, str]] = []
    for depth in range(len(parts) + 1):
        directory = root.joinpath(*parts[:depth])
        scope = "." if depth == 0 else Path(*parts[:depth]).as_posix()
        base = depth * 100
        for offset, filename in enumerate(_INSTRUCTION_FILES):
            candidates.append((directory / filename, base + offset, scope))
        neo = directory / ".neo"
        for offset, filename in enumerate(_NEO_DIRECT_FILES, start=10):
            candidates.append((neo / filename, base + offset, scope))
        instructions = neo / "instructions"
        if instructions.is_dir() and not instructions.is_symlink():
            try:
                files = sorted(
                    item
                    for item in instructions.iterdir()
                    if item.is_file() and item.suffix.lower() == ".md"
                )
            except OSError:
                files = []
            for offset, item in enumerate(files, start=20):
                candidates.append((item, base + offset, scope))
    unique: Dict[str, Tuple[Path, int, str]] = {}
    for candidate, precedence, scope in candidates:
        key = os.path.normcase(str(candidate.absolute()))
        unique.setdefault(key, (candidate, precedence, scope))
    loaded: List[Dict[str, Any]] = []
    omitted: List[Dict[str, Any]] = []
    warnings: List[str] = []
    remaining = max(1, int(max_chars))
    ordered = sorted(
        unique.values(),
        key=lambda item: (
            item[1],
            item[0].relative_to(root).as_posix()
            if item[0].is_relative_to(root)
            else str(item[0]),
        ),
    )
    readable_count = sum(1 for candidate, _, _ in ordered if candidate.exists())
    quota = max(1, remaining // max(1, readable_count))
    for candidate, precedence, scope in ordered:
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError:
            continue
        if remaining <= 0:
            omitted.append(
                {
                    "relative_path": relative,
                    "scope": scope,
                    "precedence": precedence,
                    "reason": "instruction character budget exhausted",
                }
            )
            continue
        record = _read_instruction(root, candidate, min(remaining, quota))
        if record is None:
            if candidate.exists() or candidate.is_symlink():
                warnings.append(f"{relative}: unreadable or unsafe instruction file")
            continue
        record["scope"] = scope
        record["precedence"] = precedence
        loaded.append(record)
        remaining -= record["chars"]
    if remaining > 0:
        for record in loaded:
            if remaining <= 0:
                break
            full = _as_text(record.get("full_content"))
            current = _as_text(record.get("content"))
            if len(full) > len(current):
                addition = full[len(current) : len(current) + remaining]
                record["content"] = current + addition
                record["chars"] = len(record["content"])
                record["truncated"] = len(record["content"]) < len(full)
                remaining -= len(addition)
    text = render_project_instructions(loaded)
    return {
        "repo": str(root),
        "files": loaded,
        "omitted": omitted,
        "warnings": warnings,
        "text": text,
        "source_report": [
            {
                "source": "project_instructions",
                "path": item["relative_path"],
                "scope": item["scope"],
                "precedence": item["precedence"],
                "chars": item["chars"],
                "truncated": item["truncated"],
                "source_truncated": item.get("source_truncated", False),
                "source_ref": dict(item.get("source_ref") or {}),
                "citation": item["citation"],
            }
            for item in loaded
        ],
    }


def discover_instructions(
    repo_path: Any,
    target_path: Any = None,
    max_chars: int = 48_000,
) -> Dict[str, Any]:
    """Short alias for hierarchical project instruction discovery."""
    return discover_project_instructions(repo_path, target_path, max_chars)


def render_project_instructions(files: Sequence[Mapping[str, Any]]) -> str:
    """Render instruction records in precedence order."""
    rendered: List[str] = []
    for item in sorted(
        (dict(value) for value in files if isinstance(value, Mapping)),
        key=lambda value: (
            int(value.get("precedence", 0)),
            str(value.get("relative_path", "")),
        ),
    ):
        content = _as_text(item.get("content")).strip()
        if not content:
            continue
        label = _as_text(
            item.get("relative_path") or item.get("path") or "instructions"
        )
        scope = _as_text(item.get("scope") or ".")
        rendered.append(f"--- {label} (scope: {scope}) ---\n{content}")
    return "\n\n".join(rendered)


def _task_mapping(task: Any) -> Dict[str, Any]:
    """Return task fields from a mapping or dataclass-like object."""
    if task is None:
        return {}
    if isinstance(task, Mapping):
        return dict(task)
    return {
        key: getattr(task, key, None)
        for key in (
            "task_id",
            "issue",
            "issue_text",
            "request",
            "description",
            "acceptance_criteria",
            "criteria",
            "acceptance",
            "target_test",
            "test_command",
            "config",
        )
    }


def _task_text(task: Any) -> str:
    """Render the current task without including credentials."""
    data = _task_mapping(task)
    values: List[str] = []
    for key in ("task_id", "issue", "issue_text", "request", "description"):
        value = _as_text(data.get(key))
        if value:
            values.append(f"{key}: {value}")
    config = data.get("config")
    if isinstance(config, Mapping):
        for key in ("target_test", "test_command"):
            value = _as_text(config.get(key))
            if value:
                values.append(f"{key}: {value}")
    return "\n".join(values)


def _acceptance_values(task: Any) -> List[str]:
    """Extract acceptance criteria from common task/config shapes."""
    data = _task_mapping(task)
    raw: Any = data.get("acceptance_criteria")
    if raw is None:
        raw = data.get("criteria")
    if raw is None:
        raw = data.get("acceptance")
    if raw is None and isinstance(data.get("config"), Mapping):
        config = data["config"]
        raw = config.get("acceptance_criteria") or config.get("criteria")
    values: List[str] = []
    if isinstance(raw, str):
        values = [line.strip(" -\t") for line in raw.splitlines() if line.strip(" -\t")]
    elif isinstance(raw, (list, tuple, set)):
        iterable = sorted(raw, key=_as_text) if isinstance(raw, set) else raw
        for item in iterable:
            if isinstance(item, Mapping):
                value = _as_text(
                    item.get("description") or item.get("text") or item.get("id")
                )
            else:
                value = _as_text(item)
            if value:
                values.append(value)
    if not values:
        config = data.get("config")
        if isinstance(config, Mapping):
            target = _as_text(config.get("target_test"))
            if target:
                values.append(f"target test passes: {target}")
    return list(dict.fromkeys(values))


def _turn_text(turn: Any) -> str:
    """Render one recent conversation turn."""
    if isinstance(turn, Mapping):
        role = _as_text(turn.get("role") or "turn")
        text = _as_text(
            turn.get("text")
            or turn.get("answer")
            or turn.get("request")
            or turn.get("summary")
        )
        return f"{role}: {text}" if text else ""
    request = _as_text(getattr(turn, "request", ""))
    answer = _as_text(getattr(turn, "answer", ""))
    text = " ".join(value for value in (request, answer) if value)
    return f"exchange: {text}" if text else ""


def _rank_turns(turns: Sequence[Any], terms: Sequence[str], limit: int) -> List[Any]:
    """Rank recent turns by lexical relevance and recency."""
    words = {
        word.lower()
        for term in terms
        for word in re.findall(r"[A-Za-z0-9_]+", str(term))
    }
    scored: List[Tuple[float, int, Any]] = []
    for index, turn in enumerate(turns):
        text = _turn_text(turn).lower()
        overlap = sum(1.0 for word in words if word and word in text)
        scored.append((overlap, index, turn))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    return [item[2] for item in scored[: max(1, int(limit))]]


def _normalize_skills(value: Any) -> Tuple[str, List[Dict[str, Any]]]:
    """Normalize skill blocks and return redacted source references."""
    if value is None:
        return "", []
    if isinstance(value, str):
        block = _as_text(value).strip()
        if not block:
            return "", []
        digest = _digest_text(block)
        return block, [
            {
                "source": "skill",
                "path": "skills",
                "content": block,
                "digest": digest,
                "source_ref": {
                    "kind": "inline",
                    "path": "skills",
                    "content_digest": digest,
                },
                "citation": retrieval.make_citation(
                    "skill", "skills", digest=digest, role="skills"
                ),
            }
        ]
    if isinstance(value, Mapping):
        block = _as_text(
            value.get("skills_block") or value.get("text") or value.get("body")
        ).strip()
        refs: List[Dict[str, Any]] = []
        receipts = (
            value.get("receipts", []) if isinstance(value.get("receipts"), list) else []
        )
        for item in receipts:
            if isinstance(item, Mapping):
                name = _as_text(item.get("name") or "skill")
                source = _as_text(item.get("source") or item.get("origin") or "skills")
                body = _as_text(item.get("body") or item.get("text") or block).strip()
                digest = _digest_text(body)
                refs.append(
                    {
                        "source": "skill",
                        "path": source,
                        "name": name,
                        "content": body,
                        "digest": digest,
                        "source_ref": {
                            "kind": "inline",
                            "path": source,
                            "content_digest": digest,
                        },
                        "citation": retrieval.make_citation(
                            "skill",
                            source,
                            digest=digest,
                            role="skills",
                            metadata={"name": name},
                        ),
                    }
                )
        if not refs and block:
            digest = _digest_text(block)
            refs.append(
                {
                    "source": "skill",
                    "path": "skills",
                    "content": block,
                    "digest": digest,
                    "source_ref": {
                        "kind": "inline",
                        "path": "skills",
                        "content_digest": digest,
                    },
                    "citation": retrieval.make_citation(
                        "skill", "skills", digest=digest, role="skills"
                    ),
                }
            )
        return block, refs
    if isinstance(value, (list, tuple)):
        lines: List[str] = []
        refs = []
        for item in value:
            if isinstance(item, Mapping):
                name = _as_text(item.get("name") or "skill")
                body = _as_text(
                    item.get("body") or item.get("text") or item.get("skills_block")
                ).strip()
                source = _as_text(item.get("source") or item.get("origin") or "skills")
            else:
                name = "skill"
                body = _as_text(item).strip()
                source = "skills"
            if body:
                digest = _digest_text(body)
                lines.append(f"### {name}\n{body}")
                refs.append(
                    {
                        "source": "skill",
                        "path": source,
                        "name": name,
                        "content": body,
                        "digest": digest,
                        "source_ref": {
                            "kind": "inline",
                            "path": source,
                            "content_digest": digest,
                        },
                        "citation": retrieval.make_citation(
                            "skill",
                            source,
                            digest=digest,
                            role="skills",
                            metadata={"name": name},
                        ),
                    }
                )
        return "\n\n".join(lines), refs
    block = _as_text(value).strip()
    if not block:
        return "", []
    digest = _digest_text(block)
    return block, [
        {
            "source": "skill",
            "path": "skills",
            "content": block,
            "digest": digest,
            "source_ref": {
                "kind": "inline",
                "path": "skills",
                "content_digest": digest,
            },
            "citation": retrieval.make_citation(
                "skill", "skills", digest=digest, role="skills"
            ),
        }
    ]


def _normalize_memory(value: Any) -> Tuple[str, List[Dict[str, Any]]]:
    """Normalize decision records without mixing them into instructions."""
    if value is None:
        return "", []
    if isinstance(value, str):
        text = _as_text(value).strip()
        if not text:
            return "", []
        digest = _digest_text(text)
        return text, [
            {
                "source": "decision_memory",
                "path": "decision-memory",
                "content": text,
                "digest": digest,
                "source_ref": {
                    "kind": "inline",
                    "path": "decision-memory",
                    "content_digest": digest,
                },
                "citation": retrieval.make_citation(
                    "memory", "decision-memory", digest=digest, role="decision_memory"
                ),
            }
        ]
    if isinstance(value, Mapping):
        raw = value.get("decisions") or value.get("items") or value.get("results")
        if raw is None:
            raw = (
                [value]
                if value.get("text") or value.get("decision") or value.get("summary")
                else []
            )
    else:
        raw = value
    lines: List[str] = []
    refs: List[Dict[str, Any]] = []
    for index, item in enumerate(raw if isinstance(raw, (list, tuple)) else []):
        if isinstance(item, Mapping):
            text = _as_text(
                item.get("text") or item.get("decision") or item.get("summary")
            ).strip()
            source = _as_text(
                item.get("source") or item.get("category") or "decision-memory"
            )
        else:
            text = _as_text(getattr(item, "text", item)).strip()
            source = "decision-memory"
        if not text:
            continue
        digest = _digest_text(text)
        lines.append(f"- {text}")
        refs.append(
            {
                "source": "decision_memory",
                "path": source,
                "content": text,
                "digest": digest,
                "source_ref": {
                    "kind": "inline",
                    "path": source,
                    "content_digest": digest,
                },
                "citation": retrieval.make_citation(
                    "memory",
                    source,
                    digest=digest,
                    role="decision_memory",
                    metadata={"index": index},
                ),
            }
        )
    return "\n".join(lines), refs


@dataclass
class ContextBundle:
    """A compiled context bundle with citations and lossless references."""

    text: str
    sections: List[Dict[str, Any]] = field(default_factory=list)
    citations: List[Dict[str, Any]] = field(default_factory=list)
    source_references: List[Dict[str, Any]] = field(default_factory=list)
    token_budget: int = _DEFAULT_TOTAL_TOKENS
    estimated_tokens: int = 0
    provider: str = ""
    role: str = ""
    chars_per_token: int = 4
    role_budgets: Dict[str, int] = field(default_factory=dict)
    role_weights: Dict[str, float] = field(default_factory=dict)
    provider_weights: Dict[str, Any] = field(default_factory=dict)
    omitted: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    compacted: bool = False
    cache_key: str = ""
    source_digest: str = ""
    index_digest: str = ""
    cache_hit: bool = False

    def as_dict(self, include_source: bool = True) -> Dict[str, Any]:
        """Return a serializable bundle, optionally omitting source bodies."""
        references = copy.deepcopy(self.source_references)
        sections = copy.deepcopy(self.sections)
        if not include_source:
            for reference in references:
                reference.pop("content", None)
            for section in sections:
                section.pop("mandatory_parts", None)
                for reference in section.get("source_refs", []):
                    if isinstance(reference, dict):
                        reference.pop("content", None)
        return {
            "text": self.text,
            "sections": sections,
            "citations": copy.deepcopy(self.citations),
            "source_references": references,
            "token_budget": self.token_budget,
            "estimated_tokens": self.estimated_tokens,
            "provider": self.provider,
            "role": self.role,
            "chars_per_token": self.chars_per_token,
            "role_budgets": dict(self.role_budgets),
            "role_weights": dict(self.role_weights),
            "provider_weights": copy.deepcopy(self.provider_weights),
            "omitted": copy.deepcopy(self.omitted),
            "warnings": list(self.warnings),
            "compacted": self.compacted,
            "cache_key": self.cache_key,
            "source_digest": self.source_digest,
            "index_digest": self.index_digest,
            "cache_hit": self.cache_hit,
        }

    def __getitem__(self, key: str) -> Any:
        """Allow dictionary-style compatibility with mapping callers."""
        return self.as_dict()[key]

    def compact(self, token_budget: Optional[int] = None) -> "ContextBundle":
        """Return a shorter bundle while retaining every source reference."""
        try:
            limit = int(token_budget if token_budget is not None else self.token_budget)
        except (TypeError, ValueError):
            limit = _DEFAULT_TOTAL_TOKENS
        if limit < 1:
            limit = 1
        source_sections: List[Dict[str, Any]] = []
        for section in self.sections:
            item = dict(section)
            refs = item.get("source_refs") or []
            existing_parts = item.get("mandatory_parts")
            if item.get("mandatory") and refs:
                contents = [
                    _as_text(reference.get("content"))
                    for reference in refs
                    if isinstance(reference, Mapping)
                    and _as_text(reference.get("content"))
                ]
                if contents:
                    item["text"] = "\n".join(contents)
                    if (
                        not isinstance(existing_parts, (list, tuple))
                        or not existing_parts
                    ):
                        item["mandatory_parts"] = contents
            source_sections.append(item)
        reports, text, omitted, compacted = _fit_sections(
            source_sections,
            limit,
            self.provider,
            {"context_chars_per_token": self.chars_per_token},
        )
        compact_citations: List[Dict[str, Any]] = []
        seen_citations: set[str] = set()
        for report in reports:
            if not report.get("included"):
                continue
            for reference in report.get("source_refs", []):
                if not isinstance(reference, Mapping):
                    continue
                citation = dict(reference.get("citation") or {})
                citation_id = _as_text(citation.get("id"))
                if citation_id and citation_id not in seen_citations:
                    seen_citations.add(citation_id)
                    compact_citations.append(citation)
        compact_roles = allocate_token_budgets(
            limit,
            roles=[str(item.get("name")) for item in reports],
            provider=self.provider,
            role_weights=self.role_weights or None,
            provider_weights=self.provider_weights or None,
        )
        return ContextBundle(
            text=text,
            sections=reports,
            citations=compact_citations,
            source_references=copy.deepcopy(self.source_references),
            token_budget=limit,
            estimated_tokens=estimate_tokens(text, self.provider, self.chars_per_token),
            provider=self.provider,
            role=self.role,
            chars_per_token=self.chars_per_token,
            role_budgets=compact_roles,
            role_weights=dict(self.role_weights),
            provider_weights=copy.deepcopy(self.provider_weights),
            omitted=omitted + copy.deepcopy(self.omitted),
            warnings=list(self.warnings),
            compacted=bool(compacted or self.compacted),
            cache_key=self.cache_key,
            source_digest=self.source_digest,
            index_digest=self.index_digest,
            cache_hit=self.cache_hit,
        )


def _head_tail(value: str, limit: int) -> str:
    """Shorten text deterministically while retaining both ends."""
    text = str(value or "")
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = " ... "
    if limit <= len(marker):
        return text[:limit]
    keep = limit - len(marker)
    head = (keep + 1) // 2
    tail = keep - head
    return text[:head].rstrip() + marker + (text[-tail:].lstrip() if tail else "")


def _mandatory_value(section: Mapping[str, Any]) -> str:
    """Render a mandatory section without verbose headers."""
    name = str(section.get("name") or "")
    parts = section.get("mandatory_parts")
    if isinstance(parts, (list, tuple)) and parts:
        return " ".join(
            _as_text(part).strip() for part in parts if _as_text(part).strip()
        )
    text = _as_text(section.get("text")).strip()
    if name == "project_instructions":
        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if (
                not stripped
                or stripped.startswith("---")
                or stripped.startswith("## Project instructions")
            ):
                continue
            lines.append(stripped)
        return " ".join(lines)
    if name == "acceptance_criteria":
        lines = []
        for line in text.splitlines():
            stripped = re.sub(r"^\s*(?:[-*]\s+|\d+[.)]\s+)", "", line)
            if stripped and not stripped.startswith("Acceptance criteria:"):
                lines.append(stripped)
        return "; ".join(lines)
    return text


def _fit_sections(
    sections: List[Dict[str, Any]],
    total_tokens: int,
    provider: Optional[str],
    config: Optional[Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], str, List[Dict[str, Any]], bool]:
    """Fit sections to a token budget while reserving mandatory context."""
    try:
        total = max(1, int(total_tokens))
    except (TypeError, ValueError):
        total = _DEFAULT_TOTAL_TOKENS
    ratio = provider_chars_per_token(provider, config)
    char_budget = total * ratio
    role_names = [str(item.get("name")) for item in sections]
    role_limits = allocate_token_budgets(
        total,
        roles=role_names,
        provider=provider,
        role_weights=(config or {}).get("context_role_weights")
        if isinstance((config or {}).get("context_role_weights"), Mapping)
        else None,
        provider_weights=(config or {}).get("context_provider_weights")
        if isinstance((config or {}).get("context_provider_weights"), Mapping)
        else None,
    )
    mandatory = [item for item in sections if item.get("mandatory")]
    mandatory_values: List[str] = []
    mandatory_quotas = [
        max(1, role_limits.get(str(item.get("name")), 0) * ratio) for item in mandatory
    ]
    quota_total = sum(mandatory_quotas) or 1
    if quota_total > char_budget:
        mandatory_quotas = [
            max(1, int(value * char_budget / quota_total)) for value in mandatory_quotas
        ]
    for index, item in enumerate(mandatory):
        mandatory_quota = (
            mandatory_quotas[index] if index < len(mandatory_quotas) else 1
        )
        parts = item.get("mandatory_parts")
        if isinstance(parts, (list, tuple)) and parts:
            part_quota = max(1, mandatory_quota // max(1, len(parts)))
            compact_parts = [_head_tail(_as_text(part), part_quota) for part in parts]
            mandatory_values.append(" ".join(value for value in compact_parts if value))
        else:
            mandatory_values.append(_head_tail(_mandatory_value(item), mandatory_quota))
    any_truncation = False
    used = 0
    rendered_values: Dict[int, str] = {}
    for index, item in enumerate(mandatory):
        value = mandatory_values[index] if index < len(mandatory_values) else ""
        rendered_values[id(item)] = value
        used += len(value) + (2 if used else 0)
    if used > char_budget:
        while used > char_budget and mandatory_values:
            index = max(
                range(len(mandatory_values)), key=lambda pos: len(mandatory_values[pos])
            )
            if len(mandatory_values[index]) <= 1:
                break
            mandatory_values[index] = _head_tail(
                mandatory_values[index],
                max(1, len(mandatory_values[index]) - (used - char_budget) - 1),
            )
            used = sum(
                len(value) + (2 if pos else 0)
                for pos, value in enumerate(mandatory_values)
            )
    for index, item in enumerate(mandatory):
        value = mandatory_values[index] if index < len(mandatory_values) else ""
        rendered_values[id(item)] = value
    optional_used = used
    output: List[Dict[str, Any]] = []
    texts: List[str] = []
    omitted: List[Dict[str, Any]] = []
    for item in sections:
        report = dict(item)
        name = str(item.get("name") or "")
        if item.get("mandatory"):
            value = rendered_values.get(id(item), "")
            truncated = bool(value and value != _mandatory_value(item))
            any_truncation = any_truncation or truncated
            report.update(
                {
                    "text": value,
                    "included": bool(value),
                    "truncated": truncated,
                    "rendered_chars": len(value),
                    "estimated_tokens": estimate_tokens(value, provider, ratio),
                    "reason": "mandatory context" if value else "no content supplied",
                }
            )
            if value:
                texts.append(value)
            else:
                omitted.append({"source": name, "reason": report["reason"]})
            output.append(report)
            continue
        full = _as_text(item.get("text")).strip()
        if not full:
            report.update({"included": False, "reason": "no content supplied"})
            output.append(report)
            continue
        separator = 2 if texts else 0
        remaining = char_budget - optional_used - separator
        role_limit = role_limits.get(name, 0) * ratio
        available = max(0, min(remaining, role_limit))
        if available <= 0:
            report.update({"included": False, "reason": "token budget exhausted"})
            omitted.append({"source": name, "reason": report["reason"]})
            output.append(report)
            continue
        value = _head_tail(full, available)
        if value != full:
            any_truncation = True
        optional_used += len(value) + separator
        texts.append(value)
        report.update(
            {
                "text": value,
                "included": True,
                "truncated": value != full,
                "rendered_chars": len(value),
                "estimated_tokens": estimate_tokens(value, provider, ratio),
                "reason": "included within role budget",
            }
        )
        output.append(report)
    text = "\n\n".join(texts)
    if len(text) > char_budget:
        text = _head_tail(text, char_budget)
        any_truncation = True
    return output, text, omitted, any_truncation


class ContextCache:
    """Small in-memory cache keyed by request and source/index digest."""

    def __init__(self, max_entries: int = 32) -> None:
        try:
            self.max_entries = max(1, int(max_entries))
        except (TypeError, ValueError):
            self.max_entries = 32
        self._entries: Dict[str, Tuple[str, ContextBundle]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str, digest: str) -> Optional[ContextBundle]:
        """Return a cached bundle only when its content digest matches."""
        entry = self._entries.get(str(key))
        if entry is None or entry[0] != str(digest):
            self.misses += 1
            return None
        self.hits += 1
        return copy.deepcopy(entry[1])

    def put(self, key: str, digest: str, bundle: ContextBundle) -> None:
        """Store a bundle with its source/index digest."""
        self._entries[str(key)] = (str(digest), copy.deepcopy(bundle))
        while len(self._entries) > self.max_entries:
            self._entries.pop(next(iter(self._entries)))

    def clear(self) -> None:
        """Clear all cached bundles and counters."""
        self._entries.clear()
        self.hits = 0
        self.misses = 0


class ContextCompiler:
    """Compile repository context into a cited, budgeted bundle."""

    def __init__(
        self,
        repo_path: Any = None,
        config: Optional[Mapping[str, Any]] = None,
        cache: Optional[ContextCache] = None,
        lsp_manager: Any = None,
        decision_store: Any = None,
        skills: Any = None,
    ) -> None:
        self.repo_path = _as_text(repo_path)
        self.config = dict(config or {})
        self.cache = (
            cache
            if cache is not None
            else ContextCache(int(self.config.get("context_cache_entries", 32)))
        )
        self.lsp_manager = lsp_manager
        self.decision_store = decision_store
        self.skills = skills
        self._skill_scan_cache: Dict[str, Any] = {}
        self.index_root = self.config.get("index_root") or self.config.get(
            "_code_graph_root"
        )

    def _selected_paths(self, values: Sequence[Any]) -> List[str]:
        """Normalize selected file arguments to repo-relative paths."""
        root = _root(self.repo_path)
        if root is None:
            return []
        result: List[str] = []
        for value in values or []:
            if isinstance(value, Mapping):
                value = (
                    value.get("file") or value.get("path") or value.get("relative_path")
                )
            normalized = _canonical_path(root, _as_text(value))
            if normalized and normalized not in result:
                result.append(normalized)
        return result

    def _source_ref(self, record: Mapping[str, Any], role: str) -> Dict[str, Any]:
        """Normalize a source record with its full body for later retrieval."""
        citation = dict(record.get("citation") or {})
        if not citation:
            citation = retrieval.make_citation(
                str(record.get("source") or role),
                _as_text(record.get("file") or record.get("path")),
                int(record.get("line") or 0),
                int(record.get("end_line") or 0) or None,
                _as_text(record.get("digest")),
                role,
            )
        result = {
            "source": str(record.get("source") or role),
            "path": _as_text(
                record.get("file") or record.get("path") or citation.get("path")
            ),
            "line": int(record.get("line") or citation.get("line") or 0),
            "end_line": int(record.get("end_line") or citation.get("end_line") or 0),
            "content": _as_text(
                record.get("full_content")
                or record.get("text")
                or record.get("source")
                or record.get("content")
            ),
            "digest": _as_text(record.get("digest") or citation.get("digest")),
            "citation": citation,
        }
        source_ref = record.get("source_ref")
        if isinstance(source_ref, Mapping):
            result["source_ref"] = dict(source_ref)
        else:
            result["source_ref"] = {
                "kind": "inline",
                "path": result["path"],
                "content_digest": result["digest"] or _digest_text(result["content"]),
            }
        if "source_truncated" in record:
            result["source_truncated"] = bool(record.get("source_truncated"))
        if "source_bytes_read" in record:
            result["source_bytes_read"] = int(record.get("source_bytes_read") or 0)
        if "source_total_bytes" in record:
            result["source_total_bytes"] = int(record.get("source_total_bytes") or 0)
        if record.get("id") or record.get("node_id"):
            result["id"] = _as_text(record.get("id") or record.get("node_id"))
        return result

    def _instruction_section(
        self,
        project_instructions: Any,
        max_chars: int,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Normalize explicit or discovered instructions as one role."""
        if project_instructions is None:
            data = discover_project_instructions(self.repo_path, max_chars=max_chars)
        elif isinstance(project_instructions, Mapping):
            data = dict(project_instructions)
        else:
            data = {"text": _as_text(project_instructions), "files": []}
        files = [
            dict(item) for item in data.get("files", []) if isinstance(item, Mapping)
        ]
        if not files and data.get("text"):
            files = [
                {
                    "relative_path": "instructions",
                    "content": _as_text(data.get("text")),
                    "citation": retrieval.make_citation(
                        "instruction", "instructions", role="project_instructions"
                    ),
                }
            ]
        refs = [self._source_ref(item, "project_instructions") for item in files]
        text = _as_text(data.get("text")) or render_project_instructions(files)
        return (
            {
                "name": "project_instructions",
                "text": f"## Project instructions\n{text}" if text else "",
                "mandatory": True,
                "priority": 100,
                "reason": "repository instructions are mandatory",
                "source_refs": refs,
                "instruction_omitted": [
                    dict(item)
                    for item in data.get("omitted", [])
                    if isinstance(item, Mapping)
                ],
                "instruction_warnings": [
                    str(item) for item in data.get("warnings", []) if item
                ],
                "mandatory_parts": [
                    _as_text(reference.get("content"))
                    for reference in refs
                    if _as_text(reference.get("content"))
                ],
            },
            refs,
        )

    def _task_sections(
        self, task: Any, issue_text: str
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Build task and acceptance sections with separate citations."""
        task_value = task if task is not None else {"issue_text": issue_text}
        task_data = _task_mapping(task_value)
        task_text = _task_text(task_value) or _as_text(issue_text)
        criteria = _acceptance_values(task_value)
        refs: List[Dict[str, Any]] = []
        sections: List[Dict[str, Any]] = []
        if task_text:
            task_path = _as_text(task_data.get("task_id")) or "task"
            task_digest = _digest_text(task_text)
            task_refs = [
                {
                    "source": "active_task",
                    "path": task_path,
                    "content": task_text,
                    "digest": task_digest,
                    "source_ref": {
                        "kind": "inline",
                        "path": task_path,
                        "content_digest": task_digest,
                    },
                    "citation": retrieval.make_citation(
                        "task",
                        task_path,
                        digest=task_digest,
                        role="active_task",
                    ),
                }
            ]
            sections.append(
                {
                    "name": "active_task",
                    "text": f"## Current task\n{task_text}",
                    "mandatory": False,
                    "priority": 95,
                    "reason": "current task identity and request",
                    "source_refs": task_refs,
                }
            )
            refs.extend(task_refs)
        criteria_text = "\n".join(f"- {value}" for value in criteria)
        if criteria_text:
            sections.append(
                {
                    "name": "acceptance_criteria",
                    "text": f"Acceptance criteria:\n{criteria_text}",
                    "mandatory": True,
                    "priority": 100,
                    "reason": "acceptance criteria are mandatory",
                    "mandatory_parts": list(criteria),
                    "source_refs": [
                        {
                            "source": "acceptance_criteria",
                            "path": "task",
                            "content": criteria_text,
                            "digest": _digest_text(criteria_text),
                            "source_ref": {
                                "kind": "inline",
                                "path": "task",
                                "content_digest": _digest_text(criteria_text),
                            },
                            "citation": retrieval.make_citation(
                                "criteria",
                                "task",
                                digest=_digest_text(criteria_text),
                                role="acceptance_criteria",
                            ),
                        }
                    ],
                }
            )
            refs.extend(sections[-1]["source_refs"])
        return sections, refs

    def _selected_sections(
        self,
        selected_files: Sequence[Any],
        selected_symbols: Sequence[Any],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Load selected file ranges and exact symbol ranges."""
        sections: List[Dict[str, Any]] = []
        refs: List[Dict[str, Any]] = []
        records: List[Dict[str, Any]] = []
        for value in selected_files or []:
            if isinstance(value, Mapping):
                path = value.get("file") or value.get("path")
                start = value.get("line", value.get("start_line", 1))
                end = value.get("end_line")
            else:
                path = value
                start = 1
                end = None
            normalized = self._selected_paths([path])
            if not normalized:
                continue
            found = retrieval.retrieve_range(
                self.repo_path,
                normalized[0],
                start_line=start,
                end_line=end,
                include_source=True,
            )
            records.extend(found)
        for value in selected_symbols or []:
            if isinstance(value, Mapping):
                symbol = (
                    value.get("symbol") or value.get("name") or value.get("qualified")
                )
                path = value.get("file") or value.get("path")
                start = value.get("line", value.get("start_line"))
                end = value.get("end_line")
            else:
                symbol = value
                path = None
                start = None
                end = None
            if not symbol:
                continue
            found = retrieval.retrieve_exact_symbol(
                self.repo_path,
                str(symbol),
                target_file=path,
                start_line=start,
                end_line=end,
                include_source=True,
                index_root=self.index_root,
            )
            records.extend(found)
        if records:
            for record in records:
                refs.append(self._source_ref(record, "selected"))
            file_text = "\n\n".join(
                f"### {record['path']}:{record['line']}-{record['end_line']}\n{record['text']}"
                for record in records
            )
            sections.append(
                {
                    "name": "selected_files",
                    "text": f"## Selected source\n{file_text}",
                    "mandatory": False,
                    "priority": 85,
                    "reason": "caller-selected files and symbols",
                    "source_refs": refs,
                }
            )
        return sections, refs

    def _memory_sections(
        self,
        decision_memory: Any,
        decision_store: Any,
        issue_text: str,
        terms: Sequence[str],
    ) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
        """Query scoped decision memory and keep it separate from instructions."""
        warnings: List[str] = []
        value = decision_memory
        if value is None and decision_store is not None and self.repo_path:
            try:
                value = decision_store.search(
                    " ".join(str(term) for term in terms),
                    limit=20,
                    repo_path=self.repo_path,
                )
            except TypeError:
                warnings.append("decision store does not support repository scoping")
                value = []
            except Exception as exc:
                warnings.append(f"decision store unavailable: {type(exc).__name__}")
                value = []
        text, refs = _normalize_memory(value)
        if not text:
            return None, refs, warnings
        return (
            {
                "name": "decision_memory",
                "text": f"## Relevant decision memory\n{text}",
                "mandatory": False,
                "priority": 60,
                "reason": "repository-scoped learned decisions",
                "source_refs": refs,
            },
            refs,
            warnings,
        )

    def _skill_sections(
        self, skills: Any, issue_text: str, terms: Sequence[str]
    ) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
        """Normalize supplied skills or scan applicable project skills."""
        warnings: List[str] = []
        value = skills
        if value is None and self.config.get("skills_enabled", True) and self.repo_path:
            try:
                from harness.skills import scan_skills_for_task

                configured_roots = self.config.get("skills_roots") or []
                if isinstance(configured_roots, (str, Path)):
                    configured_roots = [configured_roots]
                value = scan_skills_for_task(
                    repo_path=self.repo_path,
                    issue_text=issue_text,
                    retrieval_terms=list(terms),
                    extra_roots=[str(root) for root in configured_roots],
                    max_skills=int(self.config.get("skills_max", 3)),
                    max_chars=int(self.config.get("skills_max_chars", 2500)),
                )
            except Exception as exc:
                warnings.append(f"skill discovery unavailable: {type(exc).__name__}")
                value = None
        text, refs = _normalize_skills(value)
        if not text:
            return None, refs, warnings
        return (
            {
                "name": "skills",
                "text": f"## Applicable skills\n{text}",
                "mandatory": False,
                "priority": 55,
                "reason": "applicable task instructions",
                "source_refs": refs,
            },
            refs,
            warnings,
        )

    def _lsp_section(
        self, manager: Any, paths: Sequence[str]
    ) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
        """Collect optional LSP diagnostics without making them mandatory."""
        if manager is None:
            return None, [], []
        warnings: List[str] = []
        values: List[Any] = []
        timeout = self.config.get("lsp_timeout_s")
        root = _root(self.repo_path)
        if root is None:
            return None, [], ["LSP diagnostics unavailable: invalid repository root"]
        for path in paths or []:
            relative = _repo_relative(root, path)
            if not relative:
                warnings.append("LSP diagnostics unavailable: path outside repository")
                continue
            candidate = str(root / relative)
            try:
                if timeout is None:
                    values.extend(manager.get_diagnostics(candidate))
                else:
                    try:
                        values.extend(
                            manager.get_diagnostics(candidate, timeout_s=float(timeout))
                        )
                    except TypeError:
                        values.extend(manager.get_diagnostics(candidate))
            except Exception as exc:
                warnings.append(f"LSP diagnostics unavailable: {type(exc).__name__}")
        if not values:
            status = getattr(manager, "status", None)
            if callable(status):
                try:
                    state = status()
                    if isinstance(state, Mapping) and state.get("error"):
                        warnings.append(_as_text(state["error"]))
                except Exception:
                    pass
            return None, [], warnings
        lines: List[str] = []
        refs: List[Dict[str, Any]] = []
        for value in values:
            data = value.as_dict() if hasattr(value, "as_dict") else dict(value)
            file = _as_text(data.get("file") or data.get("path"))
            line = int(data.get("line") or 0)
            message = _as_text(data.get("message"))
            severity = _as_text(data.get("severity") or "diagnostic")
            lines.append(f"- {file}:{line}: {severity}: {message}")
            diagnostic_digest = _digest_text(message)
            refs.append(
                {
                    "source": "lsp_diagnostic",
                    "path": file,
                    "line": line,
                    "end_line": int(data.get("end_line") or line),
                    "content": message,
                    "digest": diagnostic_digest,
                    "source_ref": {
                        "kind": "inline",
                        "path": file,
                        "content_digest": diagnostic_digest,
                    },
                    "citation": retrieval.make_citation(
                        "lsp",
                        file,
                        line,
                        int(data.get("end_line") or line),
                        diagnostic_digest,
                        "diagnostics",
                    ),
                }
            )
        return (
            {
                "name": "diagnostics",
                "text": "## LSP diagnostics\n" + "\n".join(lines),
                "mandatory": False,
                "priority": 75,
                "reason": "language-server diagnostics",
                "source_refs": refs,
            },
            refs,
            warnings,
        )

    def _merged_instruction_data(
        self,
        supplied: Any,
        target_paths: Sequence[str],
        max_chars: int,
    ) -> Any:
        """Merge root-to-leaf instruction chains without duplicating sources."""
        if supplied is not None:
            return supplied
        results = [
            discover_project_instructions(
                self.repo_path,
                target_path=target,
                max_chars=max_chars,
            )
            for target in (target_paths or [None])
        ]
        files: List[Dict[str, Any]] = []
        seen: set[str] = set()
        omitted: List[Dict[str, Any]] = []
        warnings: List[str] = []
        for result in results:
            warnings.extend(str(item) for item in result.get("warnings", []))
            omitted.extend(
                dict(item)
                for item in result.get("omitted", [])
                if isinstance(item, Mapping)
            )
            for item in result.get("files", []):
                if not isinstance(item, Mapping):
                    continue
                key = str(item.get("relative_path") or item.get("path") or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                files.append(dict(item))
        files.sort(
            key=lambda item: (
                int(item.get("precedence", 0)),
                str(item.get("relative_path", "")),
            )
        )
        return {
            "repo": self.repo_path,
            "files": files,
            "omitted": omitted,
            "warnings": list(dict.fromkeys(warnings)),
            "text": render_project_instructions(files),
        }

    def compile(
        self,
        issue_text: str = "",
        task: Any = None,
        repo_path: Any = None,
        target_test: Optional[str] = None,
        target_path: Optional[str] = None,
        selected_files: Optional[Sequence[Any]] = None,
        selected_symbols: Optional[Sequence[Any]] = None,
        changed_files: Optional[Sequence[str]] = None,
        changed_symbols: Optional[Sequence[str]] = None,
        recent_turns: Optional[Sequence[Any]] = None,
        session: Optional[Mapping[str, Any]] = None,
        project_instructions: Any = None,
        skills: Any = None,
        decision_memory: Any = None,
        decision_store: Any = None,
        lsp_manager: Any = None,
        provider: Optional[str] = None,
        role: Optional[str] = None,
        token_budget: Optional[int] = None,
        trace: Any = None,
        task_id: Optional[str] = None,
        use_cache: bool = True,
    ) -> ContextBundle:
        """Compile all available context roles into a cited bundle."""
        if repo_path is not None:
            self.repo_path = _as_text(repo_path)
        repo_root = _root(self.repo_path)
        if decision_store is None:
            decision_store = self.decision_store
        if (
            decision_store is None
            and decision_memory is None
            and self.config.get(
                "decision_memory_enabled",
                self.config.get("include_decision_memory", False),
            )
        ):
            try:
                from harness.deps import get_decision_store_factory

                factory = get_decision_store_factory()
                if factory is not None:
                    decision_store = factory()
            except Exception:
                decision_store = None
        if skills is None:
            skills = self.skills
        manager = lsp_manager if lsp_manager is not None else self.lsp_manager
        issue = _as_text(issue_text)
        if not issue and task is not None:
            issue = _task_text(task)
        if not issue and session is not None:
            issue = _as_text(session.get("active_task"))
        terms = retrieval.extract_terms(issue)
        try:
            preliminary_source_hash = (
                retrieval.source_digest(self.repo_path)
                if repo_root is not None
                else "unavailable"
            )
        except Exception:
            preliminary_source_hash = "unavailable"
        skill_discovery_warnings: List[str] = []
        skills_cacheable = True
        if (
            skills is None
            and repo_root is not None
            and self.config.get("skills_enabled", True)
        ):
            configured_roots = self.config.get("skills_roots") or []
            if isinstance(configured_roots, (str, Path)):
                configured_roots = [configured_roots]
            try:
                from harness.skills import discover_skills

                skill_inventory = [
                    {
                        "name": skill.name,
                        "description": skill.description,
                        "body": skill.body,
                        "source": skill.source,
                        "origin": skill.origin,
                    }
                    for skill in discover_skills(
                        repo_path=str(repo_root),
                        extra_roots=[str(root) for root in configured_roots],
                    )
                ]
                skill_inventory_digest = _json_digest(skill_inventory)
            except Exception:
                skill_inventory = []
                skill_inventory_digest = "unavailable"
            skill_request_key = _json_digest(
                {
                    "repo": str(repo_root),
                    "source": preliminary_source_hash,
                    "skill_inventory": skill_inventory_digest,
                    "issue": issue,
                    "terms": list(terms),
                    "roots": [str(root) for root in configured_roots],
                    "max_skills": self.config.get("skills_max", 3),
                    "max_chars": self.config.get("skills_max_chars", 2500),
                }
            )
            if skill_request_key in self._skill_scan_cache:
                skills = copy.deepcopy(self._skill_scan_cache[skill_request_key])
            else:
                try:
                    from harness.skills import scan_skills_for_task

                    discovered = scan_skills_for_task(
                        repo_path=str(repo_root),
                        issue_text=issue,
                        retrieval_terms=list(terms),
                        extra_roots=[str(root) for root in configured_roots],
                        max_skills=int(self.config.get("skills_max", 3)),
                        max_chars=int(self.config.get("skills_max_chars", 2500)),
                    )
                    skills = discovered if discovered is not None else ""
                    self._skill_scan_cache[skill_request_key] = copy.deepcopy(skills)
                    while len(self._skill_scan_cache) > 32:
                        self._skill_scan_cache.pop(next(iter(self._skill_scan_cache)))
                except Exception as exc:
                    skill_discovery_warnings.append(
                        f"skill discovery unavailable: {type(exc).__name__}"
                    )
                    skills_cacheable = False
        task_data = _task_mapping(task)
        task_config = task_data.get("config")
        target = target_test or _as_text(
            task_config.get("target_test") if isinstance(task_config, Mapping) else ""
        )
        selected_values = list(selected_files or [])
        selected_paths = self._selected_paths(selected_values)
        selected_fingerprint: List[Dict[str, Any]] = []
        for value in selected_values:
            if isinstance(value, Mapping):
                path = value.get("file") or value.get("path")
                start = value.get("line", value.get("start_line", 1))
                end = value.get("end_line")
            else:
                path = value
                start = 1
                end = None
            normalized = self._selected_paths([path])
            selected_fingerprint.append(
                {
                    "path": normalized[0] if normalized else "",
                    "start": start,
                    "end": end,
                }
            )
        changed_values: List[str] = []
        if repo_root is not None:
            for value in changed_files or []:
                relative = _repo_relative(repo_root, value)
                if relative and relative not in changed_values:
                    changed_values.append(relative)
        target_candidates: List[str] = []
        if target_path:
            target_candidates.append(_as_text(target_path))
        if target:
            target_candidates.append(str(target).split("::", 1)[0].split(" - ", 1)[0])
        target_candidates.extend(selected_paths)
        target_candidates.extend(changed_values)
        target_candidates = list(
            dict.fromkeys(value for value in target_candidates if value)
        )
        try:
            total_tokens = int(
                token_budget
                if token_budget is not None
                else self.config.get("context_token_budget", _DEFAULT_TOTAL_TOKENS)
            )
        except (TypeError, ValueError):
            total_tokens = _DEFAULT_TOTAL_TOKENS
        total_tokens = max(1, total_tokens)
        provider_name = _as_text(provider or self.config.get("provider"))
        if role:
            self.config["context_role"] = str(role)
        index_root = self.index_root
        if index_root is not None and not isinstance(index_root, Path):
            index_root = Path(str(index_root))
        input_fingerprint = {
            "repo": self.repo_path,
            "issue": issue,
            "task_id": _as_text(task_data.get("task_id")),
            "criteria": _acceptance_values(task),
            "target_test": target,
            "target_path": target_candidates,
            "selected_files": selected_paths,
            "selected_file_ranges": selected_fingerprint,
            "selected_symbols": [str(value) for value in (selected_symbols or [])],
            "task_rendered": _task_text(task),
            "changed_files": changed_values,
            "changed_symbols": [str(value) for value in (changed_symbols or [])],
            "recent_turns": [_turn_text(value) for value in (recent_turns or [])],
            "session_turns": [
                _turn_text(value)
                for value in (
                    (session or {}).get("turns", [])
                    if isinstance(session, Mapping)
                    else []
                )
            ],
            "session_summary": _as_text(
                (session or {}).get("summary") if isinstance(session, Mapping) else ""
            ),
            "instructions": _json_digest(project_instructions),
            "skills": _json_digest(skills),
            "memory": _json_digest(decision_memory),
            "role": _as_text(role),
            "provider": provider_name,
            "token_budget": total_tokens,
            "config": {
                key: self.config.get(key)
                for key in (
                    "context_role_weights",
                    "context_provider_weights",
                    "context_provider_chars_per_token",
                    "context_chars_per_token",
                    "context_map_symbols",
                    "context_dependency_limit",
                    "context_recent_turns",
                    "skills_enabled",
                    "skills_max",
                    "skills_max_chars",
                    "skills_roots",
                    "lsp_timeout_s",
                )
                if key in self.config
            },
        }
        request = input_fingerprint
        try:
            source_hash = preliminary_source_hash
            index_hash = retrieval.index_digest(index_root)
        except Exception:
            source_hash, index_hash = "unavailable", "unavailable"
        combined_digest = _json_digest({"source": source_hash, "index": index_hash})
        cache_key = _json_digest(request)
        cache_allowed = use_cache and self.cache is not None and repo_root is not None
        if decision_store is not None or manager is not None or not skills_cacheable:
            cache_allowed = False
        if cache_allowed:
            cached = self.cache.get(cache_key, combined_digest)
            if cached is not None:
                cached.cache_hit = True
                if trace is not None:
                    emit_context_trace(cached, trace, task_id=task_id)
                return cached
        sections: List[Dict[str, Any]] = []
        all_refs: List[Dict[str, Any]] = []
        warnings: List[str] = list(skill_discovery_warnings)
        omitted: List[Dict[str, Any]] = []
        instruction_chars = max(
            256, total_tokens * provider_chars_per_token(provider_name, self.config) * 2
        )
        instruction_data = self._merged_instruction_data(
            project_instructions,
            target_candidates,
            instruction_chars,
        )
        instruction_section, refs = self._instruction_section(
            instruction_data, instruction_chars
        )
        sections.append(instruction_section)
        all_refs.extend(refs)
        if isinstance(instruction_data, Mapping):
            warnings.extend(
                str(item) for item in instruction_data.get("warnings", []) if item
            )
            omitted.extend(
                {"source": "project_instructions", **dict(item)}
                for item in instruction_data.get("omitted", [])
                if isinstance(item, Mapping)
            )
        task_sections, refs = self._task_sections(task, issue)
        sections.extend(task_sections)
        all_refs.extend(refs)
        if repo_root is None:
            map_result = {
                "strategy": "unavailable",
                "terms": retrieval.extract_terms(issue),
                "symbols": [],
                "files": [],
                "index_digest": "unavailable",
                "source_digest": "unavailable",
            }
            warnings.append("repository map unavailable: invalid repository root")
        else:
            map_result = retrieval.rank_repository_map(
                self.repo_path,
                issue_text=issue,
                target_test=target,
                selected_files=selected_paths,
                changed_files=changed_values,
                changed_symbols=[str(value) for value in (changed_symbols or [])],
                limit=int(self.config.get("context_map_symbols", 12)),
                index_root=index_root,
            )
        if index_root is not None:
            index_hash = _as_text(
                map_result.get("index_digest")
            ) or retrieval.index_digest(index_root)
            combined_digest = _json_digest({"source": source_hash, "index": index_hash})
        if map_result.get("symbols"):
            lines = [
                f"- {item['qualified']} ({item['file']}:{item['line']}-{item['end_line']}) score={item['score']}"
                for item in map_result["symbols"]
            ]
            map_refs = [
                {
                    "source": "repository_map",
                    "path": item["file"],
                    "line": item["line"],
                    "end_line": item["end_line"],
                    "content": item["qualified"],
                    "digest": item["citation"].get("digest", ""),
                    "source_ref": {
                        "kind": "file_range",
                        "path": item["file"],
                        "line": item["line"],
                        "end_line": item["end_line"],
                        "content_digest": item["citation"].get("digest", ""),
                    },
                    "citation": item["citation"],
                }
                for item in map_result["symbols"]
            ]
            sections.append(
                {
                    "name": "repository_map",
                    "text": "## Repository map\n" + "\n".join(lines),
                    "mandatory": False,
                    "priority": 80,
                    "reason": "weighted graph repository map",
                    "source_refs": map_refs,
                }
            )
            all_refs.extend(map_refs)
        selected_section_list, refs = self._selected_sections(
            selected_values,
            list(selected_symbols or []),
        )
        sections.extend(selected_section_list)
        all_refs.extend(refs)
        turns = list(recent_turns or [])
        if not turns and isinstance(session, Mapping):
            turns = list(session.get("turns") or [])
        turns = _rank_turns(
            turns, terms, int(self.config.get("context_recent_turns", 8))
        )
        if turns:
            turn_text = "\n".join(
                _turn_text(turn) for turn in turns if _turn_text(turn)
            )
            turn_refs = []
            for index, turn in enumerate(turns):
                turn_content = _turn_text(turn)
                if not turn_content:
                    continue
                turn_digest = _digest_text(turn_content)
                turn_path = f"turn-{index}"
                turn_refs.append(
                    {
                        "source": "recent_turn",
                        "path": turn_path,
                        "content": turn_content,
                        "digest": turn_digest,
                        "source_ref": {
                            "kind": "inline",
                            "path": turn_path,
                            "content_digest": turn_digest,
                        },
                        "citation": retrieval.make_citation(
                            "turn",
                            turn_path,
                            digest=turn_digest,
                            role="recent_turns",
                        ),
                    }
                )
            sections.append(
                {
                    "name": "recent_turns",
                    "text": f"## Recent relevant turns\n{turn_text}",
                    "mandatory": False,
                    "priority": 70,
                    "reason": "recent turns ranked against current task terms",
                    "source_refs": turn_refs,
                }
            )
            all_refs.extend(turn_refs)
        memory_section, refs, memory_warnings = self._memory_sections(
            decision_memory,
            decision_store,
            issue,
            terms,
        )
        warnings.extend(memory_warnings)
        if memory_section:
            sections.append(memory_section)
            all_refs.extend(refs)
        skill_section, refs, skill_warnings = self._skill_sections(skills, issue, terms)
        warnings.extend(skill_warnings)
        if skill_section:
            sections.append(skill_section)
            all_refs.extend(refs)
        diagnostic_section, refs, lsp_warnings = self._lsp_section(
            manager,
            list(dict.fromkeys(selected_paths + changed_values)),
        )
        warnings.extend(lsp_warnings)
        if diagnostic_section:
            sections.append(diagnostic_section)
            all_refs.extend(refs)
        dependency = retrieval.changed_symbol_context(
            self.repo_path,
            changed_files=changed_values,
            changed_symbols=[str(value) for value in (changed_symbols or [])],
            target_test=target,
            index_root=index_root,
            include_source=False,
            limit=int(self.config.get("context_dependency_limit", 12)),
        )
        if (
            dependency.get("changed")
            or dependency.get("callers")
            or dependency.get("callees")
        ):
            lines = []
            refs = []
            for label in ("changed", "callers", "callees", "importers"):
                for item in dependency.get(label, []):
                    lines.append(
                        f"- {label}: {item['qualified']} ({item['file']}:{item['line']})"
                    )
                    refs.append(
                        {
                            "source": "dependency_context",
                            "path": item["file"],
                            "line": item["line"],
                            "end_line": item["end_line"],
                            "content": item["qualified"],
                            "digest": item["citation"].get("digest", ""),
                            "source_ref": {
                                "kind": "file_range",
                                "path": item["file"],
                                "line": item["line"],
                                "end_line": item["end_line"],
                                "content_digest": item["citation"].get("digest", ""),
                            },
                            "citation": item["citation"],
                        }
                    )
            sections.append(
                {
                    "name": "dependency_context",
                    "text": "## Dependency and blast radius\n" + "\n".join(lines),
                    "mandatory": False,
                    "priority": 78,
                    "reason": "direct callers, callees, and import dependents",
                    "source_refs": refs,
                }
            )
            all_refs.extend(refs)
        role_weights = (
            dict(self.config.get("context_role_weights"))
            if isinstance(self.config.get("context_role_weights"), Mapping)
            else {}
        )
        provider_weights = (
            dict(self.config.get("context_provider_weights"))
            if isinstance(self.config.get("context_provider_weights"), Mapping)
            else {}
        )
        role_limits = allocate_token_budgets(
            total_tokens,
            roles=[str(item["name"]) for item in sections],
            provider=provider_name,
            role_weights=role_weights or None,
            provider_weights=provider_weights or None,
        )
        ratio = provider_chars_per_token(provider_name, self.config)
        reports, text, fit_omitted, compacted = _fit_sections(
            sections,
            total_tokens,
            provider_name,
            self.config,
        )
        omitted.extend(fit_omitted)
        citations: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for report in reports:
            if not report.get("included"):
                continue
            for reference in report.get("source_refs", []):
                if not isinstance(reference, Mapping):
                    continue
                citation = dict(reference.get("citation") or {})
                citation_id = _as_text(citation.get("id"))
                if citation_id and citation_id not in seen:
                    seen.add(citation_id)
                    citations.append(citation)
        bundle = ContextBundle(
            text=text,
            sections=reports,
            citations=citations,
            source_references=all_refs,
            token_budget=total_tokens,
            estimated_tokens=estimate_tokens(text, provider_name, ratio),
            provider=provider_name,
            role=_as_text(role),
            chars_per_token=ratio,
            role_budgets=role_limits,
            role_weights=role_weights,
            provider_weights=provider_weights,
            omitted=omitted,
            warnings=warnings,
            compacted=compacted,
            cache_key=cache_key,
            source_digest=source_hash,
            index_digest=index_hash,
        )
        if cache_allowed:
            self.cache.put(cache_key, combined_digest, bundle)
        if trace is not None:
            emit_context_trace(bundle, trace, task_id=task_id)
        return bundle

    def build(self, *args: Any, **kwargs: Any) -> ContextBundle:
        """Alias for :meth:`compile` for builder-style callers."""
        return self.compile(*args, **kwargs)


def compile_context(
    repo_path: Any = None,
    issue_text: str = "",
    task: Any = None,
    config: Optional[Mapping[str, Any]] = None,
    **kwargs: Any,
) -> ContextBundle:
    """Compile a context bundle with a short functional entry point."""
    return ContextCompiler(repo_path, config=config).compile(
        issue_text=issue_text,
        task=task,
        **kwargs,
    )


def build_context(
    repo_path: Any = None,
    issue_text: str = "",
    task: Any = None,
    config: Optional[Mapping[str, Any]] = None,
    include_source: bool = True,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Compile and return a JSON-friendly context mapping."""
    if (
        isinstance(repo_path, Mapping)
        and "repo" not in repo_path
        and "repo_path" not in repo_path
    ):
        session = repo_path
        repo_path = session.get("repo") or session.get("repo_path")
        if task is None and session.get("active_task") is not None:
            task = session.get("active_task")
        kwargs.setdefault("session", session)
    return compile_context(repo_path, issue_text, task, config, **kwargs).as_dict(
        include_source
    )


def trace_receipt(bundle: ContextBundle) -> Dict[str, Any]:
    """Return compact context metadata suitable for a trace event."""
    return {
        "token_budget": bundle.token_budget,
        "estimated_tokens": bundle.estimated_tokens,
        "provider": bundle.provider,
        "role": bundle.role,
        "chars_per_token": bundle.chars_per_token,
        "role_budgets": dict(bundle.role_budgets),
        "cache_key": bundle.cache_key,
        "source_digest": bundle.source_digest,
        "index_digest": bundle.index_digest,
        "cache_hit": bundle.cache_hit,
        "compacted": bundle.compacted,
        "citations": copy.deepcopy(bundle.citations),
        "sections": [
            {
                "source": section.get("name"),
                "included": bool(section.get("included")),
                "truncated": bool(section.get("truncated")),
                "reason": section.get("reason", ""),
                "citation_ids": [
                    _as_text(reference.get("citation", {}).get("id"))
                    for reference in section.get("source_refs", [])
                    if section.get("included") and isinstance(reference, Mapping)
                ],
            }
            for section in bundle.sections
        ],
        "omitted": copy.deepcopy(bundle.omitted),
        "warnings": list(bundle.warnings),
    }


def emit_context_trace(
    bundle: ContextBundle,
    trace: Any = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Emit a context receipt to a TraceLogger and unified tracing overlay."""
    receipt = trace_receipt(bundle)
    if trace is not None:
        logger = getattr(trace, "log", None)
        if callable(logger):
            try:
                payload = dict(receipt)
                if task_id:
                    payload["task_id"] = str(task_id)
                logger("context", payload)
            except Exception:
                pass
    if task_id:
        try:
            from shared.tracing import emit

            emit("harness", "context_compiled", task_id=str(task_id), **receipt)
        except Exception:
            pass
    return receipt
