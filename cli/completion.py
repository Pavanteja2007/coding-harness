"""Shell completion generation for `vex` (Task D — CLI citizenship).

`vex completion bash|zsh|fish|powershell` prints a completion script
for that shell to stdout; the user (or the README's install commands)
redirects it into their shell's completion location. `vex completion
--install` writes the current shell's script to its conventional
location and prints what it did.

How it works, per shell:
- bash: a `complete -o nosort -F _vex vex` function that asks the CLI
  itself for completions (`vex __completions -- "<words>"`) — dynamic,
  so new subcommands/flags are picked up without regenerating anything.
- zsh: a compdef/compinit-compatible #compdef script using the same
  backend via `compadd`.
- fish: a `complete -c vex` driver using the same backend.
- powershell: a Register-ArgumentCompleter using the same backend.

The backend is `python -m cli __completions -- W1 W2 ...` (also a hidden
`vex __completions` subcommand): it prints one candidate per line, plus
a trailing ``--`` marker line (the sentinel `--` protects empty word
lists from argparse's required-subparser error, not a completion).

The whole surface is derived from the REAL argparse parser
(build_parser()), so flags/subcommands can never drift from --help.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

#: How the completion backend is invoked from the generated scripts.
#: ``{python}`` and ``{module}`` are substituted at generation time so
#: the scripts are self-contained (no vex import at shell start).
_BACKEND_TMPL = "{python} -m cli __completions"


def _backend_invocation() -> str:
    """The literal backend command the generated scripts call.

    The portable choice is the interpreter that generated the script +
    the module form (works for pip, pipx, venv, and source installs
    alike).
    """
    quoted_py = shlex.quote(sys.executable)
    return _BACKEND_TMPL.format(python=quoted_py)


def collect_completions(words: List[str]) -> List[str]:
    """Completion candidates for `words` (the shell's current words,
    last one being the partially-typed word).

    Assumes words is the argv tail as the shell sees it (no program
    name); returns the matching subcommands/flags/positional choices,
    derived from the real parser. Never raises: any parse weirdness
    degrades to "no suggestions" — a completion hook must never spam
    a user with a traceback.
    """
    try:
        from cli.main import build_parser

        parser = build_parser()
        return _complete(parser, words)
    except Exception:
        return []


def _visible_subcommands(parser: argparse.ArgumentParser) -> List[str]:
    """Subcommand names, EXCLUDING hidden ones (``__completions``)."""
    out = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                if not name.startswith("__"):
                    _ = sub  # (name only; help text unused here)
                    out.append(name)
    return out


def _complete(parser: argparse.ArgumentParser, words: List[str]) -> List[str]:
    """The core matcher: walk `words` through the parser tree."""
    if not words:
        return _visible_subcommands(parser)
    partial = words[-1]
    prev = words[-2] if len(words) >= 2 else ""

    # Still at the top level?
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            {n: (action, sub) for n, sub in action.choices.items()}
            break

    # Walk to the deepest matching subparser along `words[:-1]`
    # (config -> set needs TWO hops; each level has its own registry).
    parser_ctx: argparse.ArgumentParser = parser
    for w in words[:-1]:
        nxt = None
        for action in parser_ctx._actions:
            if isinstance(action, argparse._SubParsersAction) and w in action.choices:
                nxt = action.choices[w]
                break
        if nxt is None:
            break
        parser_ctx = nxt

    if parser_ctx is parser:
        # completing a top-level subcommand or a top-level flag
        cands = _visible_subcommands(parser)
        cands += [a.option_strings[0] for a in parser._actions if a.option_strings]
        return sorted({c for c in cands if c.startswith(partial)})

    # flags of the (sub)command we're inside
    cands = [a.option_strings[0] for a in parser_ctx._actions if a.option_strings]
    # nested subcommands (config/memory/plugin/mcp have them)
    cands += _visible_subcommands(parser_ctx)
    # positional choices (e.g. none today, but future-proof)
    for a in parser_ctx._actions:
        if a.choices and not a.option_strings:
            cands += [str(c) for c in a.choices]
    # value completion for flags that take choices (--tier)
    if prev.startswith("--"):
        for a in parser_ctx._actions:
            if prev in a.option_strings and a.choices:
                cands += [str(c) for c in a.choices]
    return sorted({c for c in cands if c.startswith(partial)})


# ---------------------------------------------------------------------------
# The hidden backend subcommand
# ---------------------------------------------------------------------------


def cmd_completions(args: argparse.Namespace) -> int:
    """Hidden `__completions` subcommand: print candidates, then ``--``.

    One candidate per line so shells can read it with plain read loops;
    the trailing ``--`` line is the end-of-list sentinel (also lets an
    empty result be distinguished from a crash by the caller).
    """
    words = [w for w in (getattr(args, "words", []) or []) if w != "--"]
    for c in collect_completions(words):
        print(c)
    print("--")
    return 0


# ---------------------------------------------------------------------------
# Generated scripts
# ---------------------------------------------------------------------------


def script_bash() -> str:
    """bash completion script (dynamic; backend-derived)."""
    backend = _backend_invocation()
    return f"""\
# vex bash completion (generated by `vex completion bash`)
_vex_completions() {{
    local IFS=$'\\n'
    local words cands
    # COMP_WORDS minus the program name, up to the cursor word
    words=("${{COMP_WORDS[@]:1:$((COMP_CWORD-1))}}" "${{COMP_WORDS[COMP_CWORD]}}")
    cands=($({backend} "${{words[@]}}"))
    local final=()
    local ended=0
    for c in "${{cands[@]}}"; do
        [ "$c" = "--" ] && {{ ended=1; continue; }}
        [ "$ended" = "1" ] && final+=("$c")
    done
    COMPREPLY=($(compgen -W "${{final[*]}}" -- "${{COMP_WORDS[COMP_CWORD]}}"))
}}
complete -o nosort -F _vex_completions vex
"""


def script_zsh() -> str:
    """zsh completion script (compdef + the same backend)."""
    backend = _backend_invocation()
    # The ${(@f)...} split form can't live inside the f-string literal
    # (unbalanced braces); stitched after formatting instead.
    body = f"""\
#compdef vex
# vex zsh completion (generated by `vex completion zsh`)
_vex_completions() {{
    local -a words cands final
    words=("${{words[@]:1:$((CURRENT-1))}}" "$words[CURRENT]")
    cands=((${{(@f)$({backend} "${{words[@]}}")}}))
    local ended=0
    for c in "${{cands[@]}}"; do
        if [ "$c" = "--" ]; then ended=1; continue; fi
        if [ "$ended" = "1" ]; then final+=("$c"); fi
    done
    _describe 'vex' final
}}
_vex_completions "$@"
"""
    return body


def script_fish() -> str:
    """fish completion script (event-driven, same backend)."""
    backend = _backend_invocation()
    return f"""\
# vex fish completion (generated by `vex completion fish`)
function __vex_completions
    set -l tokens (commandline -opc) (commandline -ct)
    {backend} $tokens[2..-1] | awk '{{ if ($0 == "--") {{ skip=1 }} else if (skip) {{ print $0 }} }}'
end
complete -c vex -f -a '(__vex_completions)'
"""


def script_powershell() -> str:
    """PowerShell Register-ArgumentCompleter script (same backend)."""
    backend = _backend_invocation()
    return f"""\
# vex PowerShell completion (generated by `vex completion powershell`)
Register-ArgumentCompleter -CommandName vex -ScriptBlock {{
    param($wordToComplete, $commandAst, $cursorPosition)
    $words = @($commandAst.TokenSequence |
        Where-Object {{ $_.TokenFlags -band [System.Management.Automation.Language.TokenFlags]::CommandLineEnd -eq 0 }} |
        ForEach-Object {{ $_.Text }} | Select-Object -Skip 1)
    $out = & {backend} @words 2>$null
    $started = $false
    $out | ForEach-Object {{
        if (-not $started) {{ if ($_ -eq '--') {{ $started = $true }}; return }}
        if ($_ -like "$wordToComplete*") {{
            [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
        }}
    }}
}}
"""


SCRIPTS = {
    "bash": script_bash,
    "zsh": script_zsh,
    "fish": script_fish,
    "powershell": script_powershell,
}


# ---------------------------------------------------------------------------
# Install locations
# ---------------------------------------------------------------------------


def default_install_path(shell: str) -> Optional[Path]:
    """The conventional completion install path for `shell` on this OS,
    or None when there is no conventional location (PowerShell: the
    profile is the location; caller handles it)."""
    home = Path.home()
    if shell == "bash":
        base = (
            Path(os.environ.get("XDG_DATA_HOME", str(home / ".local" / "share")))
            / "bash-completion"
            / "completions"
        )
        return base / "vex"
    if shell == "zsh":
        # oh-my-zsh first, else the standard fpath site dir
        omz = home / ".oh-my-zsh" / "completions" / "_vex"
        if (home / ".oh-my-zsh").is_dir():
            return omz
        return home / ".zfunc" / "_vex"
    if shell == "fish":
        base = (
            Path(os.environ.get("XDG_DATA_HOME", str(home / ".local" / "share")))
            / "fish"
            / "vendor_completions.d"
        )
        return base / "vex.fish"
    return None  # powershell: installed via the profile


def _detect_shell() -> Optional[str]:
    """The shell we're most likely running under (for `--install`)."""
    if os.name == "nt":
        return "powershell"
    sh = os.environ.get("SHELL", "")
    if "zsh" in sh:
        return "zsh"
    if "fish" in sh:
        return "fish"
    if "bash" in sh:
        return "bash"
    return None


def install_completion(shell: str) -> "tuple[int, str]":
    """Write this shell's script to its conventional location.

    Returns (exit_code, message) — the command's whole outcome; never
    raises (a completion helper that crashes loses all trust).
    PowerShell appends a registration line to the profile instead.
    """
    from cli import ui

    con = ui.console()
    try:
        if shell not in SCRIPTS:
            ui.err_console().print(
                f"[vex.error]error: unknown shell {shell!r} "
                f"(expected one of: {', '.join(sorted(SCRIPTS))})[/]"
            )
            return 2, f"unknown shell {shell!r}"

        if shell == "powershell":
            profile = Path.home() / "Documents" / "WindowsPowerShell"
            if os.environ.get("USERPROFILE"):
                candidate = (
                    Path(os.environ["USERPROFILE"])
                    / "Documents"
                    / "WindowsPowerShell"
                    / "Microsoft.PowerShell_profile.ps1"
                )
                if candidate.parent.exists() or not profile.exists():
                    profile = candidate
            profile.parent.mkdir(parents=True, exist_ok=True)
            # must match the generated script's first line (idempotence)
            marker = "# vex PowerShell completion"
            body = script_powershell()
            existing = profile.read_text(encoding="utf-8") if profile.exists() else ""
            if marker in existing:
                con.print(f"[vex.ok]already installed[/] [vex.muted]({profile})[/]")
                return 0, "already installed"
            with open(profile, "a", encoding="utf-8") as f:
                f.write("\n" + body + "\n")
            con.print(f"[vex.ok]appended vex completer to[/] [vex.muted]{profile}[/]")
            con.print(
                "[vex.muted]reload with: . $PROFILE  (new shells pick it up "
                "automatically)[/]"
            )
            return 0, f"appended to {profile}"

        target = default_install_path(shell)
        if target is None:  # pragma: no cover - unreachable for known shells
            ui.err_console().print(f"[vex.error]error: no install path for {shell}[/]")
            return 2, "no install path"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(SCRIPTS[shell](), encoding="utf-8")
        con.print(f"[vex.ok]wrote[/] [vex.muted]{target}[/]")
        hint = {
            "bash": "restart your shell (or `exec bash`) to load it",
            "zsh": "restart your shell (or run `exec zsh`); oh-my-zsh users "
            "may need `rm -f ~/.zcompdump*` once",
            "fish": "fish picks it up automatically on the next prompt",
        }.get(shell, "")
        if hint:
            con.print(f"[vex.muted]{hint}[/]")
        return 0, f"wrote {target}"
    except OSError as exc:
        ui.err_console().print(f"[vex.error]error: cannot write completion: {exc}[/]")
        return 1, f"write failed: {exc}"


# ---------------------------------------------------------------------------
# The public subcommand
# ---------------------------------------------------------------------------


def cmd_completion(args: argparse.Namespace) -> int:
    """`vex completion <shell> [--install]` — print or install."""
    from cli import ui

    con = ui.console()
    shell = getattr(args, "shell", None)
    if getattr(args, "install", False):
        if shell is None:
            shell = _detect_shell()
        if shell is None:
            ui.err_console().print(
                "[vex.error]error: could not detect your shell — pass one "
                f"of {', '.join(sorted(SCRIPTS))} explicitly[/]"
            )
            return 2
        code, _msg = install_completion(shell)
        return code
    if shell is None or shell not in SCRIPTS:
        ui.err_console().print(
            "[vex.error]error: specify a shell: "
            f"{', '.join(sorted(SCRIPTS))} (or --install)[/]"
        )
        return 2
    con.print(SCRIPTS[shell](), markup=False, highlight=False)
    return 0


def add_completion_parser(sub: argparse._SubParsersAction) -> None:
    """Wire the `completion` + hidden `__completions` subcommands."""
    p = sub.add_parser(
        "completion",
        help="print or install shell completions (bash, zsh, fish, powershell)",
    )
    p.add_argument(
        "shell",
        nargs="?",
        choices=sorted(SCRIPTS),
        default=None,
        help="target shell (default with --install: detected)",
    )
    p.add_argument(
        "--install",
        action="store_true",
        help="write the script to your shell's conventional completion "
        "location instead of printing it",
    )
    p.set_defaults(func=cmd_completion)

    # The backend subcommand is hidden from help AND from the choices
    # listing: argparse only omits subparsers whose parser has NO
    # add_argument-help entries, so register it with all args suppressed.
    # Words are parsed with REMAINDER so flag-shaped completion words
    # (e.g. "--mo") never trip argparse; the shell scripts pass "--"
    # before them, which REMAINDER consumes as an ordinary word.
    p_hidden = sub.add_parser(
        "__completions",
        help=None,
        description=argparse.SUPPRESS,
    )
    p_hidden.add_argument(
        "words", nargs=argparse.REMAINDER, default=[], help=argparse.SUPPRESS
    )
    p_hidden.set_defaults(func=cmd_completions)
    # 3.10/3.11's help listing includes SUPPRESSed subparsers — filter
    # the help-only registry (dispatch via sub.choices is untouched).
    sub._choices_actions = [
        a for a in sub._choices_actions if a.dest != "__completions"
    ]


def _backend_cli(words: List[str]) -> List[str]:
    """Run the backend as a subprocess would (tests)."""
    out = subprocess.run(
        [sys.executable, "-m", "cli", "__completions", "--", *words],
        capture_output=True,
        text=True,
        timeout=30,
    )
    lines = out.stdout.splitlines()
    if "--" in lines:
        lines = lines[lines.index("--") + 1 :]
    return lines
