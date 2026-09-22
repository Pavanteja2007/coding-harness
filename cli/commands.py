"""Custom commands (Plugins round, Task B): reusable slash-command
instruction templates, modeled on Claude Code's custom slash commands.

A command is a single markdown file:

    .vex/commands/<name>.md          (project — committed, shared)
    ~/.config/vex/commands/<name>.md (global — personal)
    ~/.config/vex/plugins/<plugin>/commands/<name>.md (from a plugin)

The file's content is an instruction template the user invokes
directly in the interactive session:

    vex › /review the auth module

`$ARGUMENTS` in the template is replaced by everything after the
command name (empty string when absent); `{{arg1}}`-style positional
placeholders from some other systems are deliberately NOT supported —
one substitution slot keeps templates simple and predictable.

Dispatch (cli/interactive.py::_slash_command): when the user types
`/<name> ...` and `<name>` is not a BUILT-IN session command
(/help /status /diff /sessions /resume /approve /reject /cancel
/quiet /plan /compact /copy-diff, plus /trace /feed /steer /history
/init /model /login /logout /mcp /skills /cost /undo /clear — see
BUILTIN_SLASH_COMMANDS; /review stays custom-resolvable by design),
the custom-command loader resolves the template and the
session renders it — echo back first so the user sees exactly what
will run, then run it as a fix request whose issue text IS the filled
template (a custom command is a reusable instruction for the harness,
which is exactly what an issue is).

Discovery precedence on name collision: project > global > plugin
(same ordering rationale as skills — the specific beats the general).

All file IO is best-effort: a missing/unreadable/malformed command
file degrades to a plain "unknown command" hint, never a traceback.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

__all__ = [
    "BUILTIN_SLASH_COMMANDS",
    "command_names",
    "fill_template",
    "list_commands",
    "load_command",
]

# The built-in session commands custom names must not shadow (a plugin
# shipping /help would break the session's own surface — built-ins win
# by construction; the loader returns None for these names).
# NOTE: /review is deliberately NOT in this set even though the session
# answers a bare "/review" with the last diff + rationale: a project or
# plugin may ship a "review.md" custom command (the documented plugin
# example does), and that explicit template keeps working — the session
# dispatches "/review <args>" to the custom template when one exists
# and only renders the builtin diff+rationale view otherwise.
BUILTIN_SLASH_COMMANDS = frozenset(
    {
        "/help",
        "/status",
        "/diff",
        "/sessions",
        "/resume",
        "/approve",
        "/reject",
        "/cancel",
        "/quiet",
        "/plan",
        "/compact",
        "/copy-diff",
        "/copy",
        "/history",
        "/trace",
        "/feed",
        "/steer",
        "/init",
        "/model",
        "/login",
        "/logout",
        "/mcp",
        "/skills",
        "/cost",
        "/undo",
        "/clear",
    }
)

_ARG_SUB = re.compile(r"\$ARGUMENTS\b")

# Safety cap on one command template read (a pathological template must
# not blow the session).
_MAX_TEMPLATE_CHARS = 20_000


def _roots(repo_path: Optional[str]) -> List[Path]:
    """Command search roots in precedence order (project > global >
    plugins). Assumes repo_path is the session's repo; None = skip
    project root (list-only global/plugin commands)."""
    roots: List[Path] = []
    if repo_path:
        roots.append(Path(repo_path) / ".vex" / "commands")
    roots.append(Path.home() / ".config" / "vex" / "commands")
    plugins = Path.home() / ".config" / "vex" / "plugins"
    if plugins.is_dir():
        try:
            for p in sorted(plugins.iterdir()):
                if p.is_dir() and not p.name.startswith("."):
                    # Disabled plugins (`<name>.disabled` marker beside the
                    # directory) stay installed but undiscovered.
                    try:
                        if (p.parent / f"{p.name}.disabled").is_file():
                            continue
                    except OSError:
                        pass
                    roots.append(p / "commands")
        except OSError:
            pass
    return roots


def load_command(name: str, repo_path: Optional[str] = None) -> Optional[str]:
    """Resolve /<name> to its filled-ready template text (no $ARGUMENTS
    substitution — the caller owns that, it has the arguments).

    Assumes name is the bare command word WITHOUT the leading slash.
    Returns None for built-in names (never shadowable), unknown names,
    or unreadable files — the caller degrades to its unknown-command
    hint. Never raises.
    """
    if not name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        return None
    if f"/{name}" in BUILTIN_SLASH_COMMANDS:
        return None
    if name.startswith("."):
        return None  # hidden files never load
    for root in _roots(repo_path):
        path = root / f"{name}.md"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = text.strip()
        if text:
            return text[:_MAX_TEMPLATE_CHARS]
    return None


def list_commands(repo_path: Optional[str] = None) -> Dict[str, str]:
    """All available custom commands as {name: description-ish first line}.

    Project > global > plugin on collision (first root wins, same as
    skills). The "description" is the template's first non-heading line
    (command files have no frontmatter contract — keeping the format
    dead simple), truncated for display. Never raises.
    """
    out: Dict[str, str] = {}
    for root in _roots(repo_path):
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.glob("*.md"))
        except OSError:
            continue
        for path in entries:
            name = path.stem
            if not name or name.startswith(".") or f"/{name}" in BUILTIN_SLASH_COMMANDS:
                continue
            if name in out:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            desc = ""
            for ln in lines:
                if ln.startswith("#"):
                    continue  # skip title/heading lines
                desc = ln
                break
            if not lines:
                continue
            out[name] = desc[:120]
    return out


def command_names(repo_path: Optional[str] = None) -> List[str]:
    """Sorted names of available custom commands (for /help)."""
    return sorted(list_commands(repo_path))


def fill_template(template: str, arguments: str) -> str:
    """Substitute $ARGUMENTS in a template with the user's arguments.

    Assumes arguments is the raw text after the command name (may be
    empty); an empty substitution replaces the slot with "" and the
    harness's issue handling treats a template that still makes sense
    without arguments normally. Templates without a slot return
    unchanged.
    """
    return _ARG_SUB.sub(arguments or "", template or "")
