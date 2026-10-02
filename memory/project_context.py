"""Project instructions and token-budgeted conversation context.

This module owns the read-only context surfaces used by daily-driver sessions.
It deliberately keeps instructions separate from learned decision memory and
never reads or changes protected-path policy. Callers receive a serializable
bundle describing every included and excluded source so a UI or trace can show
what the model was actually given.
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

__all__ = [
    "build_context",
    "discover_project_instructions",
    "estimate_tokens",
    "format_context_status",
    "render_project_instructions",
]


_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
_DIRECT_NEO_FILES = ("AGENTS.md", "INSTRUCTIONS.md")
_DEFAULT_FILE_CHARS = 12_000
_DEFAULT_CONTEXT_TOKENS = 12_000
_MAX_INSTRUCTION_BYTES = 64_000
_REDACTED = "[REDACTED_SECRET]"
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "access_key",
    "access_token",
    "auth_token",
    "authorization",
    "client_secret",
    "cookie",
    "credential",
    "password",
    "passwd",
    "private_key",
    "secret",
    "token",
)


def _redact_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int, float, bool)):
        return ""
    try:
        from memory.decision_store import redact_secrets

        return redact_secrets(value)
    except Exception:
        return re.sub(
            r"(?i)(bearer\s+)[^\s,;]+",
            r"\1" + _REDACTED,
            str(value),
        )


def _sensitive_key(value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact_value(value: Any, key: Any = None, depth: int = 0) -> Any:
    if key is not None and _sensitive_key(key):
        return _REDACTED
    if depth > 8:
        return ""
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact_value(item_value, item_key, depth + 1)
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_redact_value(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(value)


def _as_text(value: Any) -> str:
    try:
        return str(value or "")
    except Exception:
        return ""


def _repo_root(repo_path: Any) -> Optional[Path]:
    try:
        root = Path(_as_text(repo_path)).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return root if root.is_dir() else None


def _relative_path(root: Path, path: Path) -> Optional[str]:
    try:
        return path.resolve().relative_to(root).as_posix()
    except (OSError, RuntimeError, ValueError):
        return None


def _contained_file(root: Path, candidate: Path) -> Optional[Path]:
    try:
        if candidate.is_symlink():
            return None
        resolved = candidate.resolve()
        resolved.relative_to(root)
        if not resolved.is_file():
            return None
        return resolved
    except (OSError, RuntimeError, ValueError):
        return None


def _read_instruction(
    root: Path, candidate: Path, max_chars: int = _DEFAULT_FILE_CHARS
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "path": str(candidate),
        "relative_path": _relative_path(root, candidate) or str(candidate),
        "scope": ".",
        "precedence": 0,
        "content": "",
        "chars": 0,
        "truncated": False,
        "source_truncated": False,
    }
    resolved = _contained_file(root, candidate)
    if resolved is None:
        result["error"] = "not a contained regular file"
        return result
    try:
        size = resolved.stat().st_size
        read_limit = min(_MAX_INSTRUCTION_BYTES, max(1024, int(max_chars) * 4))
        with resolved.open("rb") as handle:
            raw = handle.read(read_limit)
        if b"\x00" in raw:
            result["error"] = "binary instruction file"
            return result
        content = raw.decode("utf-8", errors="replace")
        if size > len(raw):
            result["source_truncated"] = True
    except (OSError, UnicodeError) as exc:
        result["error"] = f"unreadable instruction file: {type(exc).__name__}"
        return result
    result["content"] = content
    result["chars"] = len(content)
    return result


def _bounded_instruction(content: str, limit: int) -> tuple[str, bool]:
    if limit <= 0:
        return "", True
    if len(content) <= limit:
        return content, False
    marker = "\n... [instruction budget exhausted]"
    if limit <= len(marker):
        return content[:limit], True
    return content[: limit - len(marker)].rstrip() + marker, True


def _instruction_candidates(
    root: Path, target_dir: Path
) -> List[tuple[Path, int, str]]:
    try:
        relative = target_dir.resolve().relative_to(root)
        parts = relative.parts
    except (OSError, RuntimeError, ValueError):
        parts = ()

    candidates: List[tuple[Path, int, str]] = []
    for depth in range(len(parts) + 1):
        directory = root.joinpath(*parts[:depth])
        scope = "." if depth == 0 else Path(*parts[:depth]).as_posix()
        base_precedence = depth * 100
        for filename in _INSTRUCTION_FILES:
            candidates.append((directory / filename, base_precedence, scope))
        neo = directory / ".neo"
        for offset, filename in enumerate(_DIRECT_NEO_FILES, start=10):
            candidates.append((neo / filename, base_precedence + offset, scope))
        instruction_dir = neo / "instructions"
        if instruction_dir.is_dir() and not instruction_dir.is_symlink():
            try:
                files = sorted(
                    path
                    for path in instruction_dir.iterdir()
                    if path.is_file() and path.suffix.lower() == ".md"
                )
            except OSError:
                files = []
            for offset, path in enumerate(files, start=20):
                candidates.append((path, base_precedence + offset, scope))
    return candidates


def discover_project_instructions(
    repo_path: Any,
    target_path: Any = None,
    max_chars: int = _DEFAULT_CONTEXT_TOKENS * 2,
) -> Dict[str, Any]:
    """Load deterministic repository instruction files.

    Assumes ``repo_path`` is the repository root and ``target_path`` is an
    optional file or directory within it. Root instructions are loaded first,
    followed by more-specific nested directories and ``.neo`` instruction
    files. Files outside the repository and symlinked files are refused. The
    returned mapping contains loaded files, omitted files, warnings, rendered
    text, and a source report suitable for a status surface.
    """
    root = _repo_root(repo_path)
    if root is None:
        return {
            "repo": _as_text(repo_path),
            "files": [],
            "omitted": [],
            "warnings": ["repository is not a readable directory"],
            "text": "",
            "source_report": [],
        }

    if target_path:
        try:
            raw_target = Path(_as_text(target_path)).expanduser()
            target = raw_target if raw_target.is_absolute() else root / raw_target
            target = target.resolve()
            target.relative_to(root)
            target_dir = target if target.is_dir() else target.parent
        except (OSError, RuntimeError, ValueError):
            target_dir = root
    else:
        target_dir = root

    candidates = _instruction_candidates(root, target_dir)
    unique: Dict[str, tuple[Path, int, str]] = {}
    for candidate, precedence, scope in candidates:
        key = os.path.normcase(str(candidate.absolute()))
        unique.setdefault(key, (candidate, precedence, scope))

    loaded: List[Dict[str, Any]] = []
    omitted: List[Dict[str, Any]] = []
    warnings: List[str] = []
    remaining = max(1, int(max_chars))
    ordered = sorted(
        unique.values(),
        key=lambda item: (item[1], _relative_path(root, item[0]) or str(item[0])),
    )
    for candidate, precedence, scope in ordered:
        relative = _relative_path(root, candidate)
        if remaining <= 0:
            if relative:
                omitted.append(
                    {
                        "relative_path": relative,
                        "scope": scope,
                        "precedence": precedence,
                        "reason": "instruction character budget exhausted",
                    }
                )
            continue
        record = _read_instruction(root, candidate, max_chars=max_chars)
        relative = record["relative_path"]
        if record.get("error"):
            if candidate.exists() or candidate.is_symlink():
                warnings.append(f"{relative}: {record['error']}")
            continue
        content = _as_text(record.get("content"))
        if not content.strip():
            continue
        content, was_truncated = _bounded_instruction(content, remaining)
        record["content"] = content
        record["chars"] = len(content)
        record["truncated"] = bool(record.get("source_truncated")) or was_truncated
        record["scope"] = scope
        record["precedence"] = precedence
        loaded.append(record)
        remaining -= len(content)

    rendered = render_project_instructions(loaded)
    return {
        "repo": str(root),
        "files": loaded,
        "omitted": omitted,
        "warnings": warnings,
        "text": rendered,
        "source_report": [
            {
                "source": "project_instructions",
                "path": item["relative_path"],
                "scope": item["scope"],
                "precedence": item["precedence"],
                "chars": item["chars"],
                "truncated": item["truncated"],
                "source_truncated": item.get("source_truncated", False),
            }
            for item in loaded
        ],
    }


def render_project_instructions(files: Sequence[Mapping[str, Any]]) -> str:
    """Render loaded instruction records in precedence order."""
    rendered: List[str] = []
    for item in sorted(
        (dict(value) for value in files if isinstance(value, Mapping)),
        key=lambda value: (
            int(value.get("precedence", 0)),
            str(value.get("relative_path", "")),
        ),
    ):
        content = _redact_text(item.get("content")).strip()
        if not content:
            continue
        label = _redact_text(
            item.get("relative_path") or item.get("path") or "instructions"
        )
        scope = _redact_text(item.get("scope") or ".")
        rendered.append(f"--- {label} (scope: {scope}) ---\n{content}")
    return "\n\n".join(rendered)


def estimate_tokens(text: Any) -> int:
    """Estimate model tokens using a conservative four-character heuristic."""
    value = _as_text(text)
    return max(1, math.ceil(len(value) / 4)) if value else 0


def format_context_status(bundle: Mapping[str, Any]) -> str:
    """Render a compact status receipt for a context bundle."""
    budget = int(bundle.get("token_budget") or 0)
    used = int(bundle.get("estimated_tokens") or 0)
    lines = [f"context: {used}/{budget} estimated tokens"]
    for source in bundle.get("sources", []):
        if not isinstance(source, Mapping):
            continue
        state = "included" if source.get("included") else "excluded"
        lines.append(
            f"  {source.get('source', '?')}: {state}; "
            f"chars={int(source.get('chars') or 0)}; "
            f"reason={source.get('reason', '')}"
        )
    files = bundle.get("instruction_files", [])
    if files:
        lines.append(
            "  instruction files: "
            + ", ".join(
                str(item.get("relative_path") or item.get("path"))
                for item in files
                if isinstance(item, Mapping)
            )
        )
    omitted = bundle.get("instruction_omitted", [])
    if omitted:
        lines.append(
            "  omitted instruction files: "
            + ", ".join(
                str(item.get("relative_path") or item.get("path"))
                for item in omitted
                if isinstance(item, Mapping)
            )
        )
    warnings = bundle.get("instruction_warnings", [])
    if warnings:
        lines.append(
            "  instruction warnings: " + "; ".join(str(item) for item in warnings)
        )
    return "\n".join(lines)


def _turn_text(turn: Any) -> str:
    if isinstance(turn, Mapping):
        role = _as_text(turn.get("role") or "user")
        text = _as_text(turn.get("text") or turn.get("answer") or turn.get("request"))
        task_id = _as_text(turn.get("task_id"))
        suffix = f" task:{_redact_text(task_id)}" if task_id else ""
        return f"{_redact_text(role)}{suffix}: {_redact_text(text)}"
    if hasattr(turn, "request") or hasattr(turn, "answer"):
        request = _redact_text(getattr(turn, "request", ""))
        answer = _redact_text(getattr(turn, "answer", ""))
        if request or answer:
            return f"exchange: {request} {answer}".strip()
    return _redact_text(turn)


def _render_items(values: Any, limit: int = 20) -> str:
    if isinstance(values, str):
        return _redact_text(values).strip()
    if not isinstance(values, (list, tuple, set)):
        return ""
    rendered: List[str] = []
    for value in list(values)[: max(1, int(limit))]:
        if isinstance(value, Mapping):
            text = _as_text(
                value.get("text")
                or value.get("answer")
                or value.get("summary")
                or value.get("body")
            )
            if text:
                label = _redact_text(value.get("name") or value.get("source"))
                rendered.append(
                    f"{label}: {_redact_text(text)}" if label else _redact_text(text)
                )
        else:
            text = _redact_text(value)
            if text:
                rendered.append(text)
    return "\n".join(rendered)


def _render_task(task: Any) -> str:
    if task is None:
        return ""
    source = (
        task
        if isinstance(task, Mapping)
        else {
            key: getattr(task, key, None)
            for key in (
                "task_id",
                "issue",
                "issue_text",
                "request",
                "description",
                "mode",
            )
        }
    )
    parts: List[str] = []
    for key in ("task_id", "issue", "issue_text", "request", "description", "mode"):
        value = _as_text(source.get(key))
        if value:
            parts.append(f"{key}: {_redact_text(value)}")
    if parts:
        return "\n".join(parts)
    if isinstance(task, str):
        return _redact_text(task).strip()
    return ""


def _render_skills(skills: Any) -> str:
    if isinstance(skills, str):
        return _redact_text(skills).strip()
    if isinstance(skills, Mapping):
        return _redact_text(
            skills.get("skills_block") or skills.get("text") or skills.get("body")
        ).strip()
    if not isinstance(skills, (list, tuple)):
        return ""
    rendered: List[str] = []
    for value in list(skills)[:20]:
        if isinstance(value, str):
            text = _redact_text(value).strip()
            if text:
                rendered.append(text)
            continue
        source = (
            value
            if isinstance(value, Mapping)
            else {
                key: getattr(value, key, None)
                for key in (
                    "name",
                    "origin",
                    "source",
                    "body",
                    "skills_block",
                    "text",
                    "reason",
                    "matched_terms",
                )
            }
        )
        name = _redact_text(source.get("name") or "skill")
        origin = _redact_text(source.get("origin") or source.get("source"))
        body = _redact_text(
            source.get("body") or source.get("skills_block") or source.get("text")
        )
        receipt = _redact_text(source.get("reason") or source.get("matched_terms"))
        header = f"### {name}" + (f" ({origin})" if origin else "")
        if receipt:
            header += f" [matched: {receipt}]"
        rendered.append(f"{header}\n{body}".strip())
    return "\n\n".join(item for item in rendered if item)


def _render_decisions(decisions: Any) -> str:
    if isinstance(decisions, str):
        return _redact_text(decisions).strip()
    if not isinstance(decisions, (list, tuple)):
        return ""
    rendered: List[str] = []
    for value in list(decisions)[:20]:
        if isinstance(value, Mapping):
            text = _as_text(value.get("text") or value.get("decision"))
            source = _as_text(value.get("source"))
            if text:
                rendered.append(
                    f"- {_redact_text(text)}"
                    + (f" [{_redact_text(source)}]" if source else "")
                )
        else:
            text = _as_text(getattr(value, "text", value))
            if text:
                rendered.append(f"- {_redact_text(text)}")
    return "\n".join(rendered)


def _truncate(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = "\n... [context budget exhausted]"
    if limit <= len(marker):
        return text[:limit]
    return text[: limit - len(marker)].rstrip() + marker


def _instruction_text(
    value: Any, repo_path: Any, target_path: Any
) -> tuple[str, List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    if isinstance(value, Mapping):
        files = value.get("files")
        if isinstance(files, list):
            return (
                _as_text(value.get("text")) or render_project_instructions(files),
                [dict(item) for item in files if isinstance(item, Mapping)],
                [
                    dict(item)
                    for item in value.get("omitted", [])
                    if isinstance(item, Mapping)
                ],
                [str(item) for item in value.get("warnings", []) if item],
            )
    if isinstance(value, str):
        return _redact_text(value), [], [], []
    if value is None and repo_path:
        loaded = discover_project_instructions(repo_path, target_path=target_path)
        return (
            _as_text(loaded.get("text")),
            [
                dict(item)
                for item in loaded.get("files", [])
                if isinstance(item, Mapping)
            ],
            [
                dict(item)
                for item in loaded.get("omitted", [])
                if isinstance(item, Mapping)
            ],
            [str(item) for item in loaded.get("warnings", []) if item],
        )
    return "", [], [], []


def build_context(
    session: Optional[Mapping[str, Any]] = None,
    repo_path: Any = None,
    task: Any = None,
    prior_diff: str = "",
    selected_files: Optional[Sequence[str]] = None,
    project_instructions: Any = None,
    skills: Any = None,
    decision_memory: Any = None,
    decision_store: Any = None,
    target_path: Any = None,
    recent_turn_limit: int = 8,
    token_budget: int = _DEFAULT_CONTEXT_TOKENS,
) -> Dict[str, Any]:
    """Build a bounded, auditable context bundle for one repository session.

    The function never reads unrelated repositories. If ``decision_store`` is
    supplied, its ``search`` call is always given ``repo_path``; callers may
    instead pass an already scoped decision list. Raw turns are intentionally
    excluded unless they are in the active ``turns`` list, while the session's
    raw journal remains available through the session API for retrieval.
    """
    state = dict(session or {})
    if repo_path is None:
        repo_path = state.get("repo") or state.get("repo_path")
    instruction_text, instruction_files, instruction_omitted, instruction_warnings = (
        _instruction_text(project_instructions, repo_path, target_path)
    )
    instruction_text = _redact_text(instruction_text)
    if decision_memory is None and decision_store is not None and repo_path:
        try:
            decision_memory = decision_store.search(
                _render_task(task), limit=20, repo_path=_as_text(repo_path)
            )
        except TypeError:
            decision_memory = []
        except Exception:
            decision_memory = []

    active_task = task if task is not None else state.get("active_task")
    active_turns = state.get("turns")
    if not isinstance(active_turns, list):
        active_turns = []
    recent = [
        _turn_text(turn) for turn in active_turns[-max(1, int(recent_turn_limit)) :]
    ]
    recent_text = "\n".join(item for item in recent if item)
    summary = _as_text(state.get("summary"))
    unresolved = state.get("unresolved_questions")
    unresolved_text = _render_items(unresolved)
    selected_text = _render_items(selected_files, limit=30)
    skills_text = _render_skills(skills)
    decisions_text = _render_decisions(decision_memory)
    diff_text = _as_text(prior_diff)

    specs = [
        (
            "active_task",
            "## Active task\n" + _render_task(active_task),
            100,
            "current request and task identity",
        ),
        (
            "project_instructions",
            "## Project instructions\n" + instruction_text,
            95,
            "repository instruction files, most-specific last",
        ),
        (
            "recent_turns",
            "## Recent conversation\n" + recent_text,
            90,
            "active recent turns; raw history remains retrievable",
        ),
        (
            "unresolved_questions",
            "## Unresolved questions\n" + unresolved_text,
            85,
            "questions carried across turns",
        ),
        (
            "prior_diff",
            "## Prior diff\n" + diff_text,
            80,
            "most recent relevant patch",
        ),
        (
            "skills",
            "## Applicable skills\n" + skills_text,
            70,
            "matched skill instructions and receipts",
        ),
        (
            "decision_memory",
            "## Relevant decision memory\n" + decisions_text,
            60,
            "repository-scoped learned decisions",
        ),
        (
            "summary",
            "## Conversation summary\n" + summary,
            50,
            "structured compacted history",
        ),
        (
            "selected_files",
            "## Selected files\n" + selected_text,
            40,
            "caller-selected repository files",
        ),
    ]

    budget = max(1, int(token_budget)) * 4
    used = 0
    instruction_length = len(("## Project instructions\n" + instruction_text).strip())
    instruction_reserve = (
        min(budget, instruction_length + 2) if instruction_length else 0
    )
    sections: List[str] = []
    report: List[Dict[str, Any]] = []
    for name, section, priority, reason in specs:
        content = _redact_text(section).strip()
        if not content:
            report.append(
                {
                    "source": name,
                    "included": False,
                    "chars": 0,
                    "estimated_tokens": 0,
                    "reason": "no content supplied",
                    "priority": priority,
                }
            )
            continue
        separator_cost = 2 if sections else 0
        if name != "project_instructions" and instruction_reserve:
            available = max(0, budget - used - instruction_reserve - separator_cost)
        else:
            available = budget - used - separator_cost
        if available <= 0:
            report.append(
                {
                    "source": name,
                    "included": False,
                    "chars": 0,
                    "estimated_tokens": 0,
                    "reason": "token budget exhausted",
                    "priority": priority,
                }
            )
            continue
        rendered = _truncate(content, available)
        included_chars = len(rendered)
        sections.append(rendered)
        used += included_chars + separator_cost
        if name == "project_instructions":
            instruction_reserve = 0
        report.append(
            {
                "source": name,
                "included": True,
                "chars": included_chars,
                "estimated_tokens": estimate_tokens(rendered),
                "reason": reason,
                "priority": priority,
                "truncated": rendered != content,
            }
        )

    text = "\n\n".join(sections).strip()
    return {
        "text": text,
        "sources": report,
        "instruction_files": instruction_files,
        "instruction_omitted": instruction_omitted,
        "instruction_warnings": instruction_warnings,
        "token_budget": max(1, int(token_budget)),
        "estimated_tokens": estimate_tokens(text),
        "total_chars": len(text),
        "budget_exhausted": used >= budget,
        "raw_history_count": len(
            state.get("raw_turns") or state.get("compacted_turns") or []
        ),
        "active_turn_count": len(active_turns),
    }
