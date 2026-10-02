"""Flags, shell-quoted arguments, and the whole placeholder grammar for
slash-command templates.

`cli.commands.fill_template` substitutes `$ARGUMENTS` and nothing else. This
module is the FULL grammar — flags, the tokenizer, the indexed / named /
environment placeholders, and the `!` preprocessor — as a self-contained,
total, dependency-free surface a dispatcher can adopt in one call.
`fill_template` is left byte-identical: this module never wraps, patches or
replaces it, and `tests/test_command_args.py` pins that adopting this engine
changes nothing for any command file that already exists in the tree.

Five things a caller must know before wiring it in
----------------------------------------------------

1. **There is exactly one tokenizer and it is `shlex`.**
   `split_arguments` delegates the split itself to `shlex.split(posix=True)`.
   The only addition is a documented CHARACTER guard
   (`_escape_interior_quotes`) that backslash-escapes a quote character
   sitting INSIDE a bare word, because a slash command's arguments are
   natural language: `shlex` alone turns `/x don't stop` into
   `ValueError: No closing quotation` and `a"b"c` into `abc`. The guard makes
   `don't` one token and leaves `"hello world"` alone. It never decides where
   a token starts or ends, so it is not a second tokenizer, and
   `disable_interior_quote_escape=True` restores raw shlex semantics. A
   genuinely unterminated quote is still an error, because a typo the user
   made should be visible rather than guessed at.

2. **Indexed placeholders are 0-BASED.** `$0` is the FIRST argument, `$1` the
   second, and `$N` is exactly `$ARGUMENTS[N]`. Getting this backwards is the
   most common bug in a hand-rolled implementation, so it is pinned by a test
   named after the behaviour.

3. **The out-of-range / no-match asymmetry is deliberate.** An out-of-range
   INDEXED placeholder is left in the text UNCHANGED (a template that says
   `$2` and was given one argument still says `$2`, so a reader can see the
   template wanted something nobody typed). A NAMED placeholder that resolved
   to no value expands to EMPTY (the template named an optional parameter;
   silence is the correct rendering of "not supplied"). A bare `$word` that is
   NOT a declared argument name is left unchanged too, so `$HOME` in somebody's
   existing template is not eaten.

4. **`!` lines are PREPROCESSING and run BEFORE any substitution.** That
   ordering is the safety story, and it is structural rather than a review
   rule: a `!` line is replaced wholesale by a labelled output block, so no
   argument value can be part of an executed command line even in principle.
   On top of that, a `!` line that still contains a placeholder is REFUSED and
   never executed — running first is what stops an argument reaching a shell,
   and refusing the line is what stops a template AUTHOR from putting one
   there. The substituted text is LABELLED and fenced, because unlabelled
   command output inside a prompt is prompt injection, not cosmetics.

5. **The flag grammar is OPT-IN per command, and that is a compatibility
   decision.** `strict` defaults to True only when the command declares flags
   or named arguments. For every other command a `--token` stays a positional
   argument exactly as it is today, because `/fix make --no-verify` must keep
   working. Pass `strict=True`, or add a `COMMAND_FLAG_STRICT` row, to opt a
   declaration-free command into the same rule; it then reports that it
   declares no flags rather than swallowing the token. The undeclared-flag
   error always LISTS what the command does declare, because "unknown flag"
   with no alternatives is only half a message.

Two vocabularies this module deliberately does NOT have: it declares no
completion status, no verification word, and no exit code of its own beyond
`EXIT_CODES["usage_error"]` (2) for a usage error; and it adds no
`harness.config.DEFAULTS` key, because a value in DEFAULTS merges into every
task and every eval arm while "how a command line is tokenized" is not a fact
about a run. Both facts are pinned by source-level tests.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
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

__all__ = [
    "BANG_OUTPUT_MAX_CHARS",
    "BANG_PREFIX",
    "BANG_TIMEOUT_S",
    "COMMAND_FLAGS",
    "COMMAND_FLAG_STRICT",
    "COMMAND_OUTPUT_LABEL",
    "ENVIRONMENT_PLACEHOLDERS",
    "FLAG_TYPES",
    "SHELL_METACHARACTERS",
    "USAGE_ERROR_EXIT_CODE",
    "BangResult",
    "CommandArgError",
    "CommandDocument",
    "FlagSpec",
    "NamedArgument",
    "ParsedArguments",
    "PreparedPrompt",
    "ShellRisk",
    "argument_usage",
    "command_flag_table",
    "declare_flags",
    "environment_values",
    "flag_help_lines",
    "flags_for",
    "has_argument_slot",
    "parse_arguments",
    "parse_command_document",
    "prepare",
    "preprocess_bang",
    "run_bang",
    "shell_risk",
    "split_arguments",
    "substitute",
    "undeclared_flag_message",
]


# ---------------------------------------------------------------------------
# 0. Bounds. Every one is a REPORTED bound, never a silent cap.
# ---------------------------------------------------------------------------

#: The one usage-error exit code. Read from the shared contract's documented
#: value rather than inventing a seventh code: a bad command line is a usage
#: error on every surface in the product, and this module adds no new one.
USAGE_ERROR_EXIT_CODE = 2

#: Wall-clock ceiling for ONE `!` line. A command template that hangs must not
#: hold a session; the receipt says ``timed_out`` so the cut is visible rather
#: than read as "the command printed nothing".
BANG_TIMEOUT_S = 10.0

#: Output characters kept from one `!` line. A `!` line is model input and an
#: unbounded one is a context bomb. The cut is MARKED.
BANG_OUTPUT_MAX_CHARS = 4_000

#: Argument characters substituted into a template. A pasted megabyte is a
#: paste accident, not an instruction; the cut is MARKED.
ARGUMENTS_MAX_CHARS = 20_000

#: Tokens kept from one argument string. Same reason; reported.
ARGUMENTS_MAX_TOKENS = 512

_BANG_TRUNCATION_MARK = "\n... [command output truncated]"
_ARGUMENTS_TRUNCATION_MARK = "... [arguments truncated]"


# ---------------------------------------------------------------------------
# 1. FLAGS
# ---------------------------------------------------------------------------

#: The closed flag-type vocabulary. A flag outside it cannot be constructed:
#: :meth:`FlagSpec.__post_init__` raises, so a typo in a declaration is a loud
#: failure rather than a flag that silently accepts anything.
FLAG_TYPES = ("bool", "string", "int", "float", "choice")

#: Values accepted as FALSE for a bool flag, so `--fix=false` and `--fix no`
#: both work. Everything else is TRUE: a bool flag's presence is the signal and
#: inventing a third state would be a lie.
_FALSE_WORDS = frozenset({"false", "0", "no", "off"})


@dataclass(frozen=True)
class FlagSpec:
    """One declared flag: its type, its default, and one line of help."""

    name: str
    type: str = "bool"
    default: Any = None
    choices: Tuple[str, ...] = ()
    description: str = ""
    short: str = ""
    aliases: Tuple[str, ...] = ()
    metavar: str = ""

    def __post_init__(self) -> None:
        """Reject a malformed declaration before a surface can render it."""
        name = str(self.name or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            raise ValueError(f"unsupported flag name: {self.name!r}")
        if self.type not in FLAG_TYPES:
            raise ValueError(
                f"unsupported flag type: {self.type!r} (known: {', '.join(FLAG_TYPES)})"
            )
        if self.type == "choice" and not self.choices:
            raise ValueError(f"choice flag {name!r} declares no choices")
        if self.type == "bool" and self.default not in (None, True, False):
            raise ValueError(f"bool flag {name!r} must default to True or False")
        short = str(self.short or "").strip()
        if short and not re.fullmatch(r"[A-Za-z]", short):
            raise ValueError(f"flag {name!r} short form must be one letter: {short!r}")
        for alias in self.aliases:
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", str(alias)):
                raise ValueError(f"unsupported flag alias: {alias!r}")
        if self.type != "bool" and not self.metavar:
            object.__setattr__(self, "metavar", str(self.name).upper())

    @property
    def takes_value(self) -> bool:
        """Whether the flag consumes the next token (or its ``=value``)."""
        return self.type != "bool"

    @property
    def word(self) -> str:
        """The long spelling as a user types it, without the value."""
        return f"--{self.name}"

    def usage_word(self) -> str:
        """The one token a usage line shows for this flag."""
        if self.takes_value:
            return f"{self.word} <{self.metavar}>"
        return f"[{self.word}]"

    def help_line(self) -> str:
        """One help row: the flag word, then its type, default and meaning."""
        line = f"  {self.usage_word()}  {self.type}"
        if self.default not in (None, False, ""):
            line += f", default {self.default}"
        if self.choices:
            line += " (" + "|".join(self.choices) + ")"
        if self.description:
            line += f" - {self.description}"
        if self.short:
            line += f"  [short: -{self.short}]"
        if self.aliases:
            line += "  [also: " + ", ".join(self.word_aliases()) + "]"
        return line

    def word_aliases(self) -> Tuple[str, ...]:
        """The long spellings that resolve to this flag."""
        return tuple(f"--{alias}" for alias in self.aliases)

    def coerce(self, raw: Optional[str]) -> Any:
        """Convert one raw token into this flag's declared type.

        Raises :class:`CommandArgError` (a usage error, exit 2) with the
        expected shape named, because "invalid value" without the expected
        type is only half a message.
        """
        if self.type == "bool":
            return True if raw is None else str(raw).strip().lower() not in _FALSE_WORDS
        if raw is None:
            raise CommandArgError(f"{self.word} needs a value")
        text = str(raw)
        if self.type == "string":
            return text
        if self.type == "choice":
            if text not in self.choices:
                raise CommandArgError(
                    f"{self.word} must be one of: {' | '.join(self.choices)}"
                )
            return text
        try:
            return int(text) if self.type == "int" else float(text)
        except ValueError:
            raise CommandArgError(
                f"{self.word} must be a {self.type}, not {text!r}"
            ) from None


#: THE ONE SOURCE of declared flags, keyed by a bare or slashed command name.
#: Deliberately EMPTY for all 52 registry commands: adding a flag to a shipped
#: command changes what an existing invocation MEANS, and this round is not
#: allowed to change what the 52 do. A caller (a plugin, a future registry row,
#: a test) registers its own row through :func:`declare_flags`; the parse path,
#: the usage line, `/help` and the palette all read this table and nothing
#: else, so there is exactly one place a flag is declared.
COMMAND_FLAGS: Dict[str, Tuple[FlagSpec, ...]] = {
    # The three the reference ships, spelled the way the reference spells them.
    "code-review": (FlagSpec("fix", description="apply the fixes it finds"),),
    "security-review": (FlagSpec("fix", description="apply the fixes it finds"),),
    "simplify": (
        FlagSpec(
            "fix",
            description="apply the simplifications instead of only listing them",
        ),
    ),
    # One worked example of a VALUED flag and a choice, so coercion and the
    # usage line are exercised by a shipped row rather than only by a fixture.
    # `/my-skill` is a project command name, not a built-in.
    "my-skill": (
        FlagSpec("fix", description="apply the fixes it finds"),
        FlagSpec(
            "level",
            type="choice",
            choices=("low", "medium", "high"),
            default="medium",
            metavar="level",
            description="how hard the skill should work",
        ),
        FlagSpec(
            "budget",
            type="int",
            default=0,
            description="cap model spend in USD (0 = the run default)",
        ),
    ),
}

#: Commands whose flag grammar is strict even though they declare no flags.
#: Keyed the same way as :data:`COMMAND_FLAGS`.
COMMAND_FLAG_STRICT: Dict[str, bool] = {}


def _key(name: str) -> str:
    """Normalize a command name to the tables' key form (``/x`` -> ``x``)."""
    return str(name or "").strip().lower().lstrip("/")


def declare_flags(name: str, specs: Iterable[FlagSpec]) -> Tuple[FlagSpec, ...]:
    """Declare the flag grammar for one command and return what is declared.

    The ONLY writer of :data:`COMMAND_FLAGS`. A caller that wants ``/x`` to
    have a grammar registers it once, at the place that owns ``/x``, and every
    reader (parsing, usage lines, ``/help``, the palette) then sees one table.
    Re-declaring REPLACES rather than merges: a merge nobody can predict is a
    second source of truth wearing one table's name.
    """
    key = _key(name)
    if not key:
        raise ValueError("a flag grammar needs a command name")
    declared = tuple(specs)
    for spec in declared:
        if not isinstance(spec, FlagSpec):
            raise TypeError(f"{key}: not a FlagSpec: {spec!r}")
    if declared:
        COMMAND_FLAGS[key] = declared
    else:
        COMMAND_FLAGS.pop(key, None)
    return COMMAND_FLAGS.get(key, ())


def flags_for(name: str) -> Tuple[FlagSpec, ...]:
    """Return the flags one command declares (empty when it declares none)."""
    return COMMAND_FLAGS.get(_key(name), ())


def command_flag_table() -> Dict[str, Tuple[str, ...]]:
    """Every command that declares a flag, as ``{name: ("--fix", ...)}``.

    The receipt a surface needs to answer "which commands have flags?" without
    reading this module. A command absent from it declares no flags, and
    :func:`undeclared_flag_message` says exactly that when one is typed.
    """
    return {key: tuple(f.word for f in specs) for key, specs in COMMAND_FLAGS.items()}


def undeclared_flag_message(
    name: str, token: str, *, arguments: Sequence["NamedArgument"] = ()
) -> str:
    """The one usage-error sentence for a flag the command does not declare.

    It always LISTS what the command does declare, and says "declares no
    flags" when that is the truth: a message that only says "unknown flag"
    leaves a person guessing, and a person who guesses types the next typo.
    """
    specs = flags_for(name)
    label = _key(name) or "this command"
    token_text = str(token or "").strip()
    if not specs:
        if arguments:
            return f"{label} takes no flags; it takes " + " ".join(
                f"<{arg.name}>" for arg in arguments
            )
        return f"{label} declares no flags, so {token_text!r} is not a flag here"
    known = " ".join(spec.usage_word() for spec in specs)
    return f"{label} does not declare {token_text!r}; it takes: {known}"


# ---------------------------------------------------------------------------
# 2. ARGUMENT PARSING (shlex, and only shlex)
# ---------------------------------------------------------------------------

_INTERIOR_QUOTES = frozenset("'\"")
_WHITESPACE = frozenset(" \t\r\n")


def _at_boundary(text: str, index: int) -> bool:
    """Whether ``index`` is the start/end of ``text`` or sits on whitespace."""
    return index < 0 or index >= len(text) or text[index] in _WHITESPACE


def _escape_interior_quotes(text: str) -> str:
    """Backslash-escape a quote character sitting INSIDE a bare word.

    A slash command's arguments are natural language and POSIX shell semantics
    are hostile to that: ``shlex.split("don't stop")`` raises ``No closing
    quotation`` and ``shlex.split('a"b"c')`` returns ``['abc']`` — one is an
    error a user cannot act on, the other silently deletes characters. A quote
    at or beside a token boundary is real quoting and is left exactly as
    written, so ``"hello world" second`` and ``say "hi" now`` keep shell
    meaning.

    This is a CHARACTER guard, not a second tokenizer: it never decides where a
    token starts or ends, it only stops shlex from treating an apostrophe in the
    middle of a word as an opening quote. A genuinely unterminated quote still
    errors afterwards, which is the point.
    """
    out: List[str] = []
    for index, char in enumerate(text):
        if char not in _INTERIOR_QUOTES:
            out.append(char)
            continue
        if _at_boundary(text, index - 1) or _at_boundary(text, index + 1):
            out.append(char)
        else:
            out.append("\\" + char)
    return "".join(out)


def split_arguments(
    text: str, *, disable_interior_quote_escape: bool = False
) -> List[str]:
    """Split one raw argument string into tokens with SHELL-STYLE QUOTING.

    ``"/my-skill \\"hello world\\" second"`` yields ``["hello world",
    "second"]``: the first argument is ``hello world`` and the second is
    ``second``. The split itself is ``shlex.split(..., posix=True)`` — this
    module writes no tokenizer of its own and says so in its docstring.

    Assumes ``text`` is everything typed after the command name. Empty or
    whitespace-only yields ``[]``. Raises :class:`CommandArgError` (a usage
    error, exit 2) for an unterminated quote rather than guessing, because a
    silent guess is how ``"rm -rf`` becomes a command. Never returns more than
    :data:`ARGUMENTS_MAX_TOKENS` tokens, and says so in the last one.
    """
    raw = str(text or "")
    if not raw.strip():
        return []
    guarded = raw if disable_interior_quote_escape else _escape_interior_quotes(raw)
    import shlex  # local: keeps the module's import cost off every other path

    try:
        tokens = shlex.split(guarded)
    except ValueError as exc:
        raise CommandArgError(
            f"could not read the arguments: {exc}. Quote a value that contains "
            "spaces, and close every quote you open."
        ) from None
    if len(tokens) > ARGUMENTS_MAX_TOKENS:
        dropped = len(tokens) - ARGUMENTS_MAX_TOKENS
        tokens = tokens[:ARGUMENTS_MAX_TOKENS]
        tokens[-1] = (
            f"{tokens[-1]}{_ARGUMENTS_TRUNCATION_MARK} (+{dropped} more dropped)"
        )
    return tokens


# ---------------------------------------------------------------------------
# 3. THE FLAG / POSITIONAL SPLIT
# ---------------------------------------------------------------------------


class CommandArgError(ValueError):
    """A usage error in a command's arguments. Exit code 2, never a traceback.

    Carries the sentence a surface should print verbatim plus the declared
    alternatives when the error is an undeclared flag — the two halves of a
    message that lets somebody act without reading documentation.
    """

    exit_code = USAGE_ERROR_EXIT_CODE

    def __init__(self, message: str, *, declared: Tuple[str, ...] = ()) -> None:
        super().__init__(str(message))
        self.message = str(message)
        self.declared = tuple(declared)

    def usage_line(self) -> str:
        """The refusal plus the alternatives, ready to print."""
        if not self.declared:
            return self.message
        return f"{self.message}\n  takes: {' '.join(self.declared)}"


@dataclass(frozen=True)
class ParsedArguments:
    """One parsed argument string: tokens, declared flags, named bindings.

    ``raw`` is the ORIGINAL text after the command name and is what
    ``$ARGUMENTS`` expands to. That is deliberate and load-bearing: it is
    byte-identical to what :func:`cli.commands.fill_template` substitutes, so
    a template that only uses ``$ARGUMENTS`` produces the same prompt under
    both engines. The INDEXED and NAMED forms expand to the PARSED tokens,
    which is what makes ``$0`` a real first argument rather than the first
    WORD of the raw text.

    ``undeclared_flags`` is populated only for a LENIENT grammar (a command
    that declares nothing, where ``--token`` stays a positional so an existing
    invocation keeps working). A strict grammar raises instead, which is why a
    surface can treat a non-empty tuple as "say something".

    ``indexed`` is what ``$0`` / ``$1`` / ``$ARGUMENTS[N]`` walk: the tokens
    that are NOT consumed by a DECLARED flag. So ``/code-review --fix
    src/a.py`` gives ``$0 == "src/a.py"`` rather than ``"--fix"`` — a flag is a
    flag, not an argument, and one rule ("declared flags do not occupy an index")
    beats a conditional one. A command with no grammar has no declared flags, so
    ``indexed`` equals ``tokens`` and nothing changes for the 52.
    """

    raw: str
    tokens: Tuple[str, ...] = ()
    indexed: Tuple[str, ...] = ()
    positional: Tuple[str, ...] = ()
    flags: Mapping[str, Any] = field(default_factory=dict)
    named: Mapping[str, str] = field(default_factory=dict)
    undeclared_flags: Tuple[str, ...] = ()
    strict: bool = False
    truncated: bool = False
    warnings: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the parse consumed the whole string without surprises."""
        return not self.undeclared_flags

    def get(self, name: str, default: Any = None) -> Any:
        """Read one flag value by its declared name."""
        return self.flags.get(_key(name), default)

    def __getitem__(self, index: int) -> str:
        """Read one INDEXED argument by its 0-based position."""
        return self.indexed[index]

    def to_dict(self) -> Dict[str, Any]:
        """A JSON-friendly record of this parse."""
        return {
            "raw": self.raw,
            "tokens": list(self.tokens),
            "indexed": list(self.indexed),
            "positional": list(self.positional),
            "flags": dict(self.flags),
            "named": dict(self.named),
            "undeclared_flags": list(self.undeclared_flags),
            "strict": self.strict,
            "truncated": self.truncated,
            "warnings": list(self.warnings),
        }


_NEGATIVE_NUMBER = re.compile(r"^-[0-9]+(\.[0-9]+)?$")


def _flag_index(flags: Sequence[FlagSpec]) -> Dict[str, FlagSpec]:
    """Map every accepted spelling (long, alias, short) to its spec."""
    index: Dict[str, FlagSpec] = {}
    for spec in flags:
        index[f"--{spec.name}"] = spec
        for alias in spec.aliases:
            index[f"--{alias}"] = spec
        if spec.short:
            index[f"-{spec.short}"] = spec
    return index


def _flag_defaults(flags: Sequence[FlagSpec]) -> Dict[str, Any]:
    """Every declared flag's default, so a receipt can show all of them."""
    values: Dict[str, Any] = {}
    for spec in flags:
        if spec.default is not None:
            values[spec.name] = spec.default
        elif spec.type == "bool":
            values[spec.name] = False
        elif spec.type == "int":
            values[spec.name] = 0
        elif spec.type == "float":
            values[spec.name] = 0.0
        else:
            values[spec.name] = ""
    return values


def parse_arguments(
    text: str,
    *,
    name: str = "",
    flags: Optional[Sequence[FlagSpec]] = None,
    arguments: Sequence["NamedArgument"] = (),
    strict: Optional[bool] = None,
) -> ParsedArguments:
    """Parse one argument string into flags, named bindings and positionals.

    Assumes ``text`` is everything typed after the command name (may be
    empty). ``flags`` defaults to what ``name`` declares in
    :data:`COMMAND_FLAGS`; ``arguments`` is the document's declared named
    parameters. Raises :class:`CommandArgError` for an unterminated quote, an
    undeclared flag under a strict grammar, a value flag with no value, an
    uncoercible value, or a missing required named argument.

    **The strictness default is the compatibility decision.** It is True when
    the command declares flags OR named arguments — a command with a grammar
    gets a real grammar, and an undeclared flag is a usage error that lists
    what it does declare. It is False otherwise, so ``/fix make --no-verify``
    keeps working exactly as today and a ``--token`` stays positional. Pass
    ``strict=True`` to opt a declaration-free command in; it then reports that
    it declares no flags rather than swallowing the token. A
    ``COMMAND_FLAG_STRICT`` row always wins over the default.

    ``--`` ends the flags exactly as in a shell. A bare ``-`` and a negative
    number are positionals, not flags. Positional text beyond the declared
    parameters is NEVER dropped: it stays in ``positional`` and
    ``$ARGUMENTS``/``$N``, with a warning that names it.
    """
    raw = str(text or "")
    declared = tuple(flags_for(name) if flags is None else flags)
    named_decl = tuple(arguments or ())
    is_strict = bool(declared or named_decl) if strict is None else bool(strict)
    is_strict = bool(COMMAND_FLAG_STRICT.get(_key(name), is_strict))
    # No early return for an empty string. An empty argument list still has to
    # bind the declared named parameters, because a MISSING REQUIRED one is a
    # usage error whether or not anything was typed; returning early here would
    # let `/x` through for a command that declares a required parameter.
    tokens = split_arguments(raw)
    index = _flag_index(declared)
    values = _flag_defaults(declared)
    supplied: Dict[str, Any] = {}
    positional: List[str] = []
    undeclared: List[str] = []
    consumed: set = set()
    flags_done = False
    cursor = 0
    while cursor < len(tokens):
        token = tokens[cursor]
        cursor += 1
        if flags_done or token == "-" or _NEGATIVE_NUMBER.match(token):
            positional.append(token)
            continue
        if token == "--":
            flags_done = True
            continue
        if not (token.startswith("--") or (len(token) > 1 and token.startswith("-"))):
            positional.append(token)
            continue
        word, equals, inline = token.partition("=")
        spec = index.get(word)
        if spec is None:
            if is_strict:
                # The sentence already carries the alternatives, so the
                # `declared` kwarg is deliberately empty here: printing them
                # twice makes the refusal read like two different messages.
                raise CommandArgError(
                    undeclared_flag_message(name, word, arguments=named_decl)
                )
            undeclared.append(word)
            positional.append(token)
            continue
        consumed.add(cursor - 1)
        if not spec.takes_value:
            supplied[spec.name] = spec.coerce(inline if equals else None)
            continue
        if equals:
            raw_value: Optional[str] = inline
        elif cursor < len(tokens):
            raw_value = tokens[cursor]
            consumed.add(cursor)
            cursor += 1
        else:
            raise CommandArgError(
                f"{spec.word} needs a value",
                declared=tuple(item.usage_word() for item in declared),
            )
        supplied[spec.name] = spec.coerce(raw_value)
    values.update(supplied)
    indexed = tuple(
        token for index_of, token in enumerate(tokens) if index_of not in consumed
    )

    named: Dict[str, str] = {}
    warnings: List[str] = []
    queue = list(positional)
    for arg in named_decl:
        if arg.name in supplied:
            named[arg.name] = str(supplied[arg.name])
            continue
        if queue and arg.variadic:
            named[arg.name] = " ".join(queue)
            queue = []
            continue
        if queue:
            named[arg.name] = queue.pop(0)
            continue
        if arg.required:
            raise CommandArgError(
                f"{_key(name) or 'this command'} needs <{arg.name}>",
                declared=tuple(item.usage_word() for item in declared),
            )
        named[arg.name] = arg.default
    if queue and named_decl:
        warnings.append(
            f"{len(queue)} argument(s) after the declared "
            f"<{', <'.join(item.name for item in named_decl)}> are reachable only "
            "through $ARGUMENTS or $N"
        )
    if undeclared:
        warnings.append(
            f"{len(undeclared)} flag-looking token(s) kept as arguments because this "
            "command declares no flag grammar: " + ", ".join(undeclared)
        )
    return ParsedArguments(
        raw=raw,
        tokens=tuple(tokens),
        indexed=indexed,
        positional=tuple(positional),
        flags=values,
        named=named,
        undeclared_flags=tuple(undeclared),
        strict=is_strict,
        truncated=_ARGUMENTS_TRUNCATION_MARK in (tokens[-1] if tokens else ""),
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# 4. THE COMMAND DOCUMENT (optional frontmatter)
# ---------------------------------------------------------------------------

_FM_BOUNDARY = re.compile(r"^---\s*$")
_FM_LINE = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9_-]*)\s*:\s*(?P<value>.*?)\s*$")
_FM_ITEM = re.compile(r"^\s*-\s*(?P<value>.*?)\s*$")
_FM_ITEM_FIELD = re.compile(
    r"^\s*(?P<key>[A-Za-z][A-Za-z0-9_-]*)\s*:\s*(?P<value>.*?)\s*$"
)
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")

#: Frontmatter keys this module understands. Anything else is recorded in
#: :attr:`CommandDocument.unrecognized` and ignored, so a command file written
#: for a future feature still loads its body.
_KNOWN_KEYS = frozenset({"description", "argument-hint", "arguments"})


@dataclass(frozen=True)
class NamedArgument:
    """One declared named parameter of a command document.

    ``name`` is what ``$name`` in the body expands to. ``required`` without a
    supplied value is a usage error; otherwise ``default`` is used, and a
    parameter with neither expands to EMPTY (the documented asymmetry).
    ``variadic`` soaks up every remaining positional, so a document can say
    "these words" without knowing how many there are.
    """

    name: str
    description: str = ""
    required: bool = False
    default: str = ""
    variadic: bool = False

    def __post_init__(self) -> None:
        """Reject a name that could never match a ``$name`` placeholder."""
        if not _NAME_RE.match(str(self.name or "")):
            raise ValueError(f"unsupported argument name: {self.name!r}")

    @property
    def word(self) -> str:
        """The token a usage line shows for this parameter."""
        return f"<{self.name}>..."

    def help_line(self) -> str:
        """One help row for this declared parameter."""
        detail = "required" if self.required else "optional"
        if self.default:
            detail += f", default {self.default!r}"
        if self.variadic:
            detail += ", takes the rest"
        line = f"  {self.word}  {detail}"
        if self.description:
            line += f" - {self.description}"
        return line


@dataclass(frozen=True)
class CommandDocument:
    """A parsed command file: its body plus what its frontmatter declared.

    ``body`` is what the model sees. A file with NO frontmatter returns the
    whole text as the body and empty declarations, which is exactly what every
    command file in this tree looks like today — the format stays dead simple
    and the richer form is opt-in.
    """

    body: str
    arguments: Tuple[NamedArgument, ...] = ()
    description: str = ""
    argument_hint: str = ""
    frontmatter: bool = False
    unrecognized: Tuple[str, ...] = ()

    @property
    def declares_arguments(self) -> bool:
        """Whether this document named any parameters."""
        return bool(self.arguments)


def parse_command_document(text: str) -> CommandDocument:
    """Split an optional ``---`` frontmatter block off a command file.

    The dialect is a CLOSED, deliberately tiny subset — the same shape
    :mod:`harness.skills` parses for a ``SKILL.md``, so a command file and a
    skill file look alike to a reader: top-level ``key: value`` scalars and a
    block sequence under ``arguments:`` only. Nothing else in the tree parses
    YAML and nothing here starts.

    An item under ``arguments:`` is either a bare name (``- target``) or an
    inline field map (``- name: target`` followed by indented ``key: value``
    lines). Never raises: no frontmatter, an unterminated block, a malformed
    item and a name that could not be a placeholder all degrade to "no
    declarations" with the reason in ``unrecognized``. A command file is a
    markdown document, and a typo in its header must not stop it loading.
    """
    raw = str(text or "")
    lines = raw.splitlines()
    if not lines or not _FM_BOUNDARY.match(lines[0]):
        # No frontmatter: the body IS the file, byte for byte. Stripping here
        # would be a silent change to what `fill_template` returns for every
        # command file that exists today, and byte-identity for those is the
        # compatibility contract this module is built around.
        return CommandDocument(body=raw)
    closing = -1
    for index in range(1, len(lines)):
        if _FM_BOUNDARY.match(lines[index]):
            closing = index
            break
    if closing < 0:
        return CommandDocument(
            body=raw, unrecognized=("frontmatter: no closing '---'",)
        )
    meta: Dict[str, str] = {}
    entries: List[Dict[str, str]] = []
    current: Optional[Dict[str, str]] = None
    last_key = ""
    for line in lines[1:closing]:
        scalar = _FM_LINE.match(line)
        if scalar and not line.startswith((" ", "\t")):
            last_key = scalar.group("key").lower()
            meta[last_key] = scalar.group("value")
            current = None
            continue
        item = _FM_ITEM.match(line)
        if item and last_key == "arguments":
            fields: Dict[str, str] = {}
            head = item.group("value")
            first = _FM_ITEM_FIELD.match(head)
            if first:
                fields[first.group("key").lower()] = first.group("value")
            else:
                fields["name"] = head
            entries.append(fields)
            current = fields
            continue
        continuation = _FM_ITEM_FIELD.match(line)
        if continuation and current is not None and line.startswith((" ", "\t")):
            current[continuation.group("key").lower()] = continuation.group("value")
    body = "\n".join(lines[closing + 1 :]).strip() or raw
    declared: List[NamedArgument] = []
    unrecognized: List[str] = []
    for fields in entries:
        name = str(fields.get("name", "")).strip()
        if not _NAME_RE.match(name):
            unrecognized.append(f"arguments: unusable entry {name!r}")
            continue
        declared.append(
            NamedArgument(
                name=name,
                description=str(fields.get("description", "")),
                required=str(fields.get("required", "")).strip().lower()
                in {"true", "yes", "1"},
                default=str(fields.get("default", "")),
                variadic=str(fields.get("variadic", "")).strip().lower()
                in {"true", "yes", "1"},
            )
        )
    for key in sorted(meta):
        if key not in _KNOWN_KEYS and key != "argument_hint":
            unrecognized.append(f"frontmatter: ignored key {key!r}")
    return CommandDocument(
        body=body,
        arguments=tuple(declared),
        description=meta.get("description", ""),
        argument_hint=meta.get("argument-hint", meta.get("argument_hint", "")),
        frontmatter=True,
        unrecognized=tuple(unrecognized),
    )


# ---------------------------------------------------------------------------
# 5. ENVIRONMENT PLACEHOLDERS
# ---------------------------------------------------------------------------

#: The closed set of ``${...}`` placeholders this module expands. A name
#: OUTSIDE the set is left UNCHANGED, so ``${HOME}`` in an existing template
#: is not silently deleted; a name INSIDE the set whose value is unset
#: expands to empty, because "there is no session id" is a fact and the
#: literal token ``${NEO_SESSION_ID}`` is not.
ENVIRONMENT_PLACEHOLDERS = (
    "NEO_EFFORT",
    "NEO_SESSION_ID",
    "NEO_SKILL_DIR",
    "NEO_PLUGIN_ROOT",
    "NEO_PROJECT_DIR",
)


def environment_values(
    *,
    effort: Optional[str] = None,
    session_id: Optional[str] = None,
    skill_dir: Optional[str] = None,
    plugin_root: Optional[str] = None,
    project_dir: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Build the environment-placeholder map: arguments first, then the process.

    An explicit argument always wins over the process environment, because a
    caller that knows the active session id is more right than an ambient
    variable. Every key in :data:`ENVIRONMENT_PLACEHOLDERS` is ALWAYS present
    (empty when unset) — that is what distinguishes "known and unset" from "not
    a placeholder this module knows about". Never raises.
    """
    process = os.environ if env is None else env

    def pick(explicit: Optional[str], key: str) -> str:
        if explicit is not None:
            return str(explicit)
        return str(process.get(key, "") or "")

    return {
        "NEO_EFFORT": pick(effort, "NEO_EFFORT"),
        "NEO_SESSION_ID": pick(session_id, "NEO_SESSION_ID"),
        "NEO_SKILL_DIR": pick(skill_dir, "NEO_SKILL_DIR"),
        "NEO_PLUGIN_ROOT": pick(plugin_root, "NEO_PLUGIN_ROOT"),
        "NEO_PROJECT_DIR": pick(project_dir, "NEO_PROJECT_DIR"),
    }


# ---------------------------------------------------------------------------
# 6. `!` PREFIX - PREPROCESSING, NOT TOOL USE
# ---------------------------------------------------------------------------

#: The prefix that makes a line a shell command run BEFORE the model sees the
#: prompt. It is preprocessing, not a tool call: no tool invocation is
#: journalled and no model turn is spent on it.
BANG_PREFIX = "!"

#: The label every substituted command output carries. A LABEL IS A
#: CORRECTNESS PROPERTY, not cosmetics: text that arrives inside a prompt
#: without saying what it is is prompt injection, because a model reads prompt
#: text as instruction.
COMMAND_OUTPUT_LABEL = "command output"

_BANG_LINE = re.compile(r"^\s*!\s*(?P<command>.*)$")

#: Any placeholder at all, in any of this module's forms. Used to REFUSE a
#: `!` line that carries one, which is the whole argument-to-shell rule.
#: `\\` alone is NOT a match on purpose: on Windows it is a path separator, and
#: refusing `!git log C:\repo\src` would be a false positive on the platform
#: this product ships on. Only an escaped DOLLAR is treated as a placeholder.
_PLACEHOLDER_ANY = re.compile(r"\\\$|\$ARGUMENTS\b|\$\d+|\$\{[A-Za-z_]|\$[A-Za-z_]")


@dataclass(frozen=True)
class BangResult:
    """The outcome of one ``!`` line: what ran, what it printed, what failed."""

    command: str
    ok: bool
    output: str = ""
    exit_code: Optional[int] = None
    timed_out: bool = False
    truncated: bool = False
    refused: bool = False
    error: str = ""

    def labelled_block(self) -> str:
        """The output wrapped in a labelled, fenced block.

        BOTH boundaries name the label, so a cut in the middle of the block is
        still visibly inside a fence, and the command that produced the text is
        named so a reader can tell which line of the template it came from. A
        refused line says so in the block too: a silent omission would let a
        template author believe a `!` line ran.
        """
        head = f"[{COMMAND_OUTPUT_LABEL}: {self.command} · exit {self.exit_code}]"
        tail = f"[end {COMMAND_OUTPUT_LABEL}: {self.command}]"
        if self.refused:
            body = f"refused, not executed: {self.error}"
        elif not self.ok and self.error and not self.output:
            body = f"failed: {self.error}"
        else:
            body = self.output
        if not body.strip():
            body = "(no output)"
        return f"{head}\n{body}\n{tail}"


def _kill_process_tree(process: Any) -> bool:
    """Kill a `!` child AND the grandchildren its shell spawned.

    Measured, not assumed: on this platform ``subprocess.run(shell=True,
    timeout=0.25)`` against ``sleep 5`` reported ``timed_out`` only after
    **5096 ms**, because killing the shell leaves a grandchild holding the
    output pipe open and ``communicate`` waits for the pipe. A plain timeout
    therefore turns a bounded wait into an unbounded one on exactly the
    platform this product ships on, and "a hanging command template must not
    hold a session" would be a claim rather than a fact.

    POSIX kills the process group; Windows asks ``taskkill /T`` for the tree
    and falls back to a plain kill. Returns whether the tree was signalled so
    the caller can SAY SO rather than implying the child is gone.
    """
    try:
        if os.name == "nt":
            killed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=5.0,
                check=False,
            )
            if killed.returncode == 0:
                return True
        else:
            import signal as _signal

            os.killpg(os.getpgid(process.pid), _signal.SIGKILL)
            return True
    except Exception:
        pass
    try:
        process.kill()
        return True
    except Exception:
        return False


def run_bang(
    command: str,
    *,
    timeout_s: float = BANG_TIMEOUT_S,
    cwd: Optional[Any] = None,
    env: Optional[Mapping[str, str]] = None,
    runner: Optional[Callable[..., Any]] = None,
) -> BangResult:
    """Run one ``!`` shell line and capture its output, bounded.

    ``shell=True`` is correct here and only here: a ``!`` line IS a shell line,
    written by the command's author in the command's own file. The safety
    property that matters is upstream, in :func:`preprocess_bang` — a line
    carrying a placeholder is refused, so no caller-supplied argument can ever
    be part of the string passed here.

    Bounded three ways — a wall-clock timeout, an output cap, and a real
    process-TREE kill on expiry — and all three are REPORTED on the result,
    because a truncated block that reads like the whole output is a lie, and a
    "timed out" that returned five seconds late is the same class of lie.
    stdout and stderr are combined, because a command that explains itself on
    stderr has still explained itself, and stdin is ``DEVNULL`` so a `!` line
    cannot steal the user's prompt. Never raises: a failed run is a result, not
    an exception. A caller-supplied ``runner`` is the four-argument
    ``(command, cwd, env, timeout_s)`` seam a test drives.
    """
    text = str(command or "").strip()
    if not text:
        return BangResult(command=text, ok=False, refused=True, error="empty command")
    cap = max(1, int(BANG_OUTPUT_MAX_CHARS))
    budget = max(0.1, float(timeout_s))
    if runner is not None:
        try:
            produced = runner(text, cwd, env, timeout_s)
        except Exception as exc:  # a caller-supplied runner is untrusted too
            return BangResult(
                command=text, ok=False, error=f"{type(exc).__name__}: {exc}"
            )
        output, code = str(produced or ""), 0
    else:
        popen_kwargs: Dict[str, Any] = {
            "shell": True,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "text": True,
            "errors": "replace",
            "cwd": str(cwd) if cwd is not None else None,
            "env": dict(env) if env is not None else None,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            popen_kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(text, **popen_kwargs)
        except Exception as exc:
            return BangResult(
                command=text, ok=False, error=f"{type(exc).__name__}: {exc}"
            )
        try:
            output, _ = process.communicate(timeout=budget)
            code = int(process.returncode)
        except subprocess.TimeoutExpired:
            killed = _kill_process_tree(process)
            try:
                process.communicate(timeout=2.0)
            except Exception:
                pass
            return BangResult(
                command=text,
                ok=False,
                timed_out=True,
                error=(
                    f"timed out after {budget}s"
                    + ("" if killed else "; the process tree could not be killed")
                ),
            )
        except Exception as exc:
            return BangResult(
                command=text, ok=False, error=f"{type(exc).__name__}: {exc}"
            )
    trimmed = output.strip()
    truncated = len(trimmed) > cap
    if truncated:
        trimmed = trimmed[:cap] + _BANG_TRUNCATION_MARK
    return BangResult(
        command=text,
        ok=code == 0,
        output=trimmed,
        exit_code=code,
        truncated=truncated,
    )


def preprocess_bang(
    body: str,
    *,
    cwd: Optional[Any] = None,
    env: Optional[Mapping[str, str]] = None,
    timeout_s: float = BANG_TIMEOUT_S,
    runner: Optional[Callable[..., Any]] = None,
) -> Tuple[str, Tuple[BangResult, ...]]:
    """Replace every ``!`` line with a LABELLED block of its output.

    Runs strictly BEFORE substitution, and that ordering is the safety property
    rather than a comment: a ``!`` line is replaced wholesale, so no argument
    value can be part of an executed command line even in principle.

    A ``!`` line that still contains a placeholder is REFUSED and never
    executed. Running first is what stops an argument reaching a shell, and
    refusing the line is what stops a template AUTHOR from putting one there in
    the first place — because ``!echo $ARGUMENTS`` would otherwise hand
    unsanitised caller input to a shell, which is precisely the
    ``/x foo.txt; rm -rf ~`` case the reference calls out. A command that needs
    to touch an argument uses a parameterised tool, not a shell string.

    Returns the rewritten body and one :class:`BangResult` per ``!`` line in
    document order. Never raises.
    """
    text = str(body or "")
    lines = text.splitlines()
    if not any(_BANG_LINE.match(line) for line in lines):
        return text, ()
    out: List[str] = []
    results: List[BangResult] = []
    for line in lines:
        match = _BANG_LINE.match(line)
        if match is None:
            out.append(line)
            continue
        command = match.group("command").strip()
        if _PLACEHOLDER_ANY.search(command):
            result = BangResult(
                command=command,
                ok=False,
                refused=True,
                error=(
                    "a '!' line may not contain a placeholder ($ARGUMENTS, $N, "
                    "$name, ${NAME} or an escaped \\$): arguments are caller "
                    "input and a shell line would execute them. Pass the value "
                    "to a parameterised tool instead."
                ),
            )
        else:
            result = run_bang(
                command, cwd=cwd, env=env, timeout_s=timeout_s, runner=runner
            )
        results.append(result)
        out.extend(result.labelled_block().splitlines())
    return "\n".join(out), tuple(results)


# ---------------------------------------------------------------------------
# 7. SUBSTITUTION
# ---------------------------------------------------------------------------

#: Whether a body already HAS an argument slot. Deliberately the same shape as
#: ``cli.commands._ARG_SUB`` so "does this template take arguments" has ONE
#: answer in the tree and the append-the-arguments rule cannot disagree with
#: the substitution it follows.
_ARGUMENTS_SLOT = re.compile(r"\$ARGUMENTS\b")

_PLACEHOLDER = re.compile(
    r"""
      \\(?P<escaped>.)                        # \$ -> a literal dollar
    | \$(?P<all>ARGUMENTS\b)(?P<idx>\[\d+\])? # $ARGUMENTS / $ARGUMENTS[N]
    | \$(?P<index>\d+)                         # $0, $1, ... (0-BASED)
    | \$\{(?P<env>[A-Za-z_][A-Za-z0-9_]*)\}    # ${NEO_EFFORT} and friends
    | \$(?P<name>[A-Za-z_][A-Za-z0-9_-]*)      # $name (declared only)
    """,
    re.VERBOSE,
)


def has_argument_slot(body: str) -> bool:
    """Whether a body already refers to the user's arguments.

    True for ``$ARGUMENTS`` and for ``$ARGUMENTS[N]``, because
    ``commands._ARG_SUB``'s ``\\b`` is satisfied by the ``[``. False for a body
    that only uses ``$0`` or a declared ``$name`` — which is exactly when the
    append rule fires, so the model still sees what was typed.
    """
    return bool(_ARGUMENTS_SLOT.search(str(body or "")))


def _apply_outside(
    text: str, protect: Sequence[str], transform: Callable[[str], str]
) -> str:
    """Apply ``transform`` to every part of ``text`` that is not a protected span.

    The protected spans are LITERAL substrings restored byte-for-byte. This is
    how the ``!`` output blocks survive substitution: a block quotes the
    command that produced it, and that command is author text which may
    itself contain a ``$``. Without this, ``substitute`` would rewrite the
    label and the receipt would describe a command the template does not
    contain. Splits on exact substrings only, so there is no sentinel to
    collide with user input and no ordering to get wrong.
    """
    segments: List[Tuple[str, bool]] = [(text, False)]
    for needle in protect:
        if not needle:
            continue
        rebuilt: List[Tuple[str, bool]] = []
        for chunk, is_protected in segments:
            if is_protected or needle not in chunk:
                rebuilt.append((chunk, is_protected))
                continue
            pieces = chunk.split(needle)
            for position, piece in enumerate(pieces):
                if position:
                    rebuilt.append((needle, True))
                if piece:
                    rebuilt.append((piece, False))
        segments = rebuilt
    return "".join(chunk if flag else transform(chunk) for chunk, flag in segments)


def substitute(
    body: str,
    *,
    arguments: str = "",
    tokens: Sequence[str] = (),
    named: Optional[Mapping[str, str]] = None,
    environment: Optional[Mapping[str, str]] = None,
    protect: Sequence[str] = (),
) -> str:
    """Expand every placeholder in a command body, in one pass.

    ================  ====================================================
    ``$ARGUMENTS``    the RAW argument text, byte-identical to what
                      :func:`cli.commands.fill_template` substitutes
    ``$ARGUMENTS[N]``  argument N, **0-BASED**; out of range is left
                      UNCHANGED
    ``$0`` ``$1``     shorthand for ``$ARGUMENTS[0]`` / ``$ARGUMENTS[1]`` —
                      ``$0`` is the FIRST argument; out of range UNCHANGED
    ``$name``         a DECLARED named argument; no value expands to EMPTY;
                      an UNDECLARED ``$word`` is left UNCHANGED
    ``${NAME}``       a name in :data:`ENVIRONMENT_PLACEHOLDERS`; known but
                      unset expands to empty, an unknown name is UNCHANGED
    ``\\$``           a literal dollar, so ``\\$1.00`` stays ``$1.00``
    ================  ====================================================

    ``protect`` holds literal spans to leave byte-identical (the ``!`` output
    blocks). Assumes ``body`` is a command document's body and ``arguments``
    the raw text after the command name. Never raises. The asymmetry between
    an out-of-range INDEXED placeholder (unchanged) and a NAMED placeholder
    with no value (empty) is intentional and is pinned by tests named after it.
    """
    text = str(body or "")
    if "$" not in text and "\\" not in text:
        return text
    raw_arguments = str(arguments or "")
    if len(raw_arguments) > ARGUMENTS_MAX_CHARS:
        raw_arguments = raw_arguments[:ARGUMENTS_MAX_CHARS] + _ARGUMENTS_TRUNCATION_MARK
    positional = tuple(str(token) for token in tokens or ())
    names = dict(named or {})
    env_map = dict(environment or {})

    def at(index_text: str) -> Optional[str]:
        """Resolve a 0-BASED index, or None when it is out of range."""
        try:
            index = int(index_text)
        except ValueError:
            return None
        return positional[index] if 0 <= index < len(positional) else None

    def replace(match: "re.Match[str]") -> str:
        parts = match.groupdict()
        if parts.get("escaped") is not None:
            return parts["escaped"]
        if parts.get("all") is not None:
            bracket = parts.get("idx")
            if not bracket:
                return raw_arguments
            found = at(bracket[1:-1])
            return found if found is not None else match.group(0)
        if parts.get("index") is not None:
            found = at(parts["index"])
            return found if found is not None else match.group(0)
        if parts.get("env") is not None:
            key = parts["env"]
            return (
                env_map.get(key, "")
                if key in ENVIRONMENT_PLACEHOLDERS
                else match.group(0)
            )
        if parts.get("name") is not None:
            key = parts["name"]
            return str(names[key] or "") if key in names else match.group(0)
        return match.group(0)

    return _apply_outside(text, protect, lambda chunk: _PLACEHOLDER.sub(replace, chunk))


# ---------------------------------------------------------------------------
# 8. THE SHELL-SAFETY AUDIT
# ---------------------------------------------------------------------------

#: Characters that make a line a shell line rather than prose. Used to AUDIT a
#: prepared prompt for a line that both looks like a command and carries caller
#: input. The audit reports; it does not decide what is safe, because "looks
#: like a command" is a judgement a parser cannot make honestly.
SHELL_METACHARACTERS = frozenset(";|&$`\n\r<>*?()[]{}!\\\"'")

#: Prefixes that mark a line as a command line written in prose.
_COMMAND_LINE_PREFIXES = (
    "$ ",
    "! ",
    "> ",
    "% ",
    "# ",
    "bash ",
    "sh ",
    "cmd ",
    "powershell ",
)
_COMMAND_VERBS = frozenset(
    {"cat", "cp", "eval", "head", "mv", "rm", "source", "tail", "tee", "xargs"}
)


@dataclass(frozen=True)
class ShellRisk:
    """One line of a prepared prompt that carries caller input into a command."""

    line_number: int
    line: str
    reason: str
    metacharacters: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """A JSON-friendly record of this finding."""
        return {
            "line_number": self.line_number,
            "line": self.line,
            "reason": self.reason,
            "metacharacters": list(self.metacharacters),
        }


def shell_risk(text: str, *, exclude_labels: bool = True) -> Tuple[ShellRisk, ...]:
    """Report lines that put caller input into something a shell would read.

    This is the belt to :func:`preprocess_bang`'s braces: the braces make it
    structurally impossible for an argument to reach an EXECUTED command, and
    this reports the residual risk that an argument sits on a line a reader (or
    a model) might reasonably execute — ``run: cat $ARGUMENTS > out.txt``
    written as prose in a template. It reports and never rewrites, because
    silently editing somebody's prompt is worse than naming the line.

    ``exclude_labels=True`` skips the ``!`` output blocks this module itself
    produced: they are already fenced and already named, so flagging them would
    make the audit cry wolf on its own correct output.
    """
    findings: List[ShellRisk] = []
    in_block = False
    for number, line in enumerate(str(text or "").splitlines(), start=1):
        stripped = line.strip()
        if exclude_labels and stripped.startswith(f"[{COMMAND_OUTPUT_LABEL}"):
            in_block = True
            continue
        if exclude_labels and stripped.startswith(f"[end {COMMAND_OUTPUT_LABEL}"):
            in_block = False
            continue
        if in_block:
            continue
        metachars = tuple(
            sorted({char for char in stripped if char in SHELL_METACHARACTERS})
        )
        if not metachars:
            continue
        head = stripped.split(" ", 1)[0] if stripped else ""
        looks_like_command = stripped.startswith(_COMMAND_LINE_PREFIXES) or (
            head in _COMMAND_VERBS
        )
        if looks_like_command:
            findings.append(
                ShellRisk(
                    line_number=number,
                    line=line,
                    reason=(
                        "looks like a shell command line and carries shell "
                        "metacharacters; pass the value to a parameterised tool"
                    ),
                    metacharacters=metachars,
                )
            )
    return tuple(findings)


# ---------------------------------------------------------------------------
# 9. DISCOVERABILITY + THE ONE ENTRY POINT
# ---------------------------------------------------------------------------


def argument_usage(
    name: str,
    *,
    flags: Optional[Sequence[FlagSpec]] = None,
    arguments: Sequence[NamedArgument] = (),
) -> str:
    """One copyable usage line for a command, with its flags and parameters.

    The shape is the reference's: ``usage: /code-review [--fix] <target>...``.
    A command that declares nothing returns ``usage: /name`` and nothing else,
    so a caller can print this unconditionally.
    """
    label = "/" + _key(name) if _key(name) else "command"
    parts = [label]
    for spec in flags_for(name) if flags is None else flags:
        parts.append(spec.usage_word())
    for arg in arguments or ():
        parts.append(arg.word)
    return "usage: " + " ".join(parts)


def flag_help_lines(
    name: str,
    *,
    flags: Optional[Sequence[FlagSpec]] = None,
    arguments: Sequence[NamedArgument] = (),
) -> List[str]:
    """The ``/help <command>`` block: usage, then one row per declaration.

    This is the DISCOVERABILITY surface. A flag a person cannot read in
    ``/help`` is a flag they will not type, and requirement 5 of this round is
    that every capability is findable from the product without documentation.
    Returns ``[]`` for a command that declares nothing, so a caller can append
    it unconditionally without an ``if``.
    """
    declared = tuple(flags_for(name) if flags is None else flags)
    if not declared and not arguments:
        return []
    lines = [argument_usage(name, flags=declared, arguments=arguments)]
    if declared:
        lines.append("flags:")
        lines.extend(spec.help_line() for spec in declared)
    if arguments:
        lines.append("arguments:")
        lines.extend(arg.help_line() for arg in arguments)
    lines.append(
        "placeholders: $ARGUMENTS (all) · $ARGUMENTS[N] (0-based) · $0 $1 · "
        "$name (declared) · ${NEO_EFFORT} ${NEO_SESSION_ID} ${NEO_SKILL_DIR} "
        "${NEO_PLUGIN_ROOT} ${NEO_PROJECT_DIR} · \\$ for a literal dollar"
    )
    return lines


@dataclass(frozen=True)
class PreparedPrompt:
    """Everything a dispatcher needs from one ``/<command> ...`` line.

    ``text`` is the finished body: bang lines already replaced by labelled
    output, every placeholder expanded, and the arguments appended when the
    body had no ``$ARGUMENTS`` slot. ``ok`` is False when something was
    REFUSED — a usage error, or a ``!`` line carrying a placeholder — and
    ``usage_error`` then says why in a sentence a person can act on.

    ``warnings`` is for what did not stop the command: an argument past the
    declared parameters, a flag-looking token kept because the command has no
    grammar, a line that reads like a shell command. ``risks`` is the
    :func:`shell_risk` audit. None of the three is silent, and none of them
    rewrites the prompt.
    """

    name: str
    text: str
    arguments: ParsedArguments
    ok: bool = True
    usage_error: str = ""
    bang: Tuple[BangResult, ...] = ()
    risks: Tuple[ShellRisk, ...] = ()
    warnings: Tuple[str, ...] = ()
    document: Optional[CommandDocument] = None

    @property
    def exit_code(self) -> int:
        """0 when the prompt is usable, the usage-error code when it is not."""
        return 0 if self.ok else USAGE_ERROR_EXIT_CODE

    @property
    def flags(self) -> Mapping[str, Any]:
        """Every declared flag's resolved value (defaults included)."""
        return self.arguments.flags

    def get(self, name: str, default: Any = None) -> Any:
        """Read one resolved flag value by its declared name."""
        return self.arguments.get(name, default)

    def usage(self) -> str:
        """The usage line a refusal should print beside its reason."""
        return argument_usage(
            self.name,
            arguments=self.document.arguments if self.document else (),
        )

    def help_lines(self) -> List[str]:
        """The ``/help <command>`` block for this command."""
        return flag_help_lines(
            self.name, arguments=self.document.arguments if self.document else ()
        )

    def to_dict(self) -> Dict[str, Any]:
        """A JSON-friendly record of this preparation."""
        return {
            "name": self.name,
            "text": self.text,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "usage_error": self.usage_error,
            "arguments": self.arguments.to_dict(),
            "bang": [
                {
                    "command": item.command,
                    "ok": item.ok,
                    "exit_code": item.exit_code,
                    "refused": item.refused,
                    "timed_out": item.timed_out,
                    "truncated": item.truncated,
                    "error": item.error,
                }
                for item in self.bang
            ],
            "risks": [item.to_dict() for item in self.risks],
            "warnings": list(self.warnings),
            "usage": self.usage(),
        }


def prepare(
    text: str,
    *,
    name: str = "",
    arguments: str = "",
    cwd: Optional[Any] = None,
    env: Optional[Mapping[str, str]] = None,
    timeout_s: float = BANG_TIMEOUT_S,
    runner: Optional[Callable[..., Any]] = None,
    effort: Optional[str] = None,
    session_id: Optional[str] = None,
    skill_dir: Optional[str] = None,
    plugin_root: Optional[str] = None,
    project_dir: Optional[str] = None,
    strict: Optional[bool] = None,
) -> PreparedPrompt:
    """Prepare one command document for the model. THE ONE ENTRY POINT.

    The order is the contract, and each step depends on the previous one:

    1. **parse the document** — optional frontmatter off, body retained;
    2. **parse the arguments** — ``shlex`` split, declared flags resolved,
       declared named parameters bound, an undeclared flag refused under a
       strict grammar;
    3. **run the ``!`` lines** — BEFORE any substitution, so an argument can
       never be part of an executed command line, and a ``!`` line that still
       carries a placeholder is refused rather than run;
    4. **substitute** — ``$ARGUMENTS`` / ``$ARGUMENTS[N]`` / ``$N`` /
       ``$name`` / ``${NAME}``, with ``\\$`` for a literal dollar;
    5. **append the arguments** when the body had no ``$ARGUMENTS`` slot, as
       ``ARGUMENTS: ...``, so the model still sees what the user typed;
    6. **audit** — :func:`shell_risk` plus every warning, reported not
       swallowed.

    A surface should call this and nothing else; calling the steps by hand is
    how a caller ends up substituting before running a ``!`` line. Never
    raises: a usage error, a failed ``!`` line and a bad frontmatter all come
    back on the :class:`PreparedPrompt` with ``ok`` / ``warnings`` saying so.
    """
    document = parse_command_document(text)
    try:
        parsed = parse_arguments(
            arguments,
            name=name,
            arguments=document.arguments,
            strict=strict,
        )
    except CommandArgError as exc:
        empty = ParsedArguments(raw=str(arguments or ""))
        return PreparedPrompt(
            name=_key(name),
            text="",
            arguments=empty,
            ok=False,
            usage_error=exc.usage_line(),
            document=document,
        )
    body, bang = preprocess_bang(
        document.body, cwd=cwd, env=env, timeout_s=timeout_s, runner=runner
    )
    refusals = [item for item in bang if item.refused]
    prompt = substitute(
        body,
        arguments=parsed.raw,
        tokens=parsed.indexed,
        named=parsed.named,
        environment=environment_values(
            effort=effort,
            session_id=session_id,
            skill_dir=skill_dir,
            plugin_root=plugin_root,
            project_dir=project_dir,
            env=env,
        ),
        # The output blocks are restored byte-for-byte: a block quotes the
        # command that produced it, and that command is author text which may
        # itself contain a `$`.
        protect=[item.labelled_block() for item in bang],
    )
    if parsed.raw.strip() and not has_argument_slot(document.body):
        prompt = f"{prompt.rstrip()}\n\nARGUMENTS: {parsed.raw}"
    risks = shell_risk(prompt)
    warnings = list(parsed.warnings) + list(document.unrecognized)
    warnings.extend(f"'!{item.command}' was refused: {item.error}" for item in refusals)
    warnings.extend(f"line {item.line_number}: {item.reason}" for item in risks)
    return PreparedPrompt(
        name=_key(name),
        text=prompt,
        arguments=parsed,
        ok=not refusals,
        usage_error=(
            f"{_key(name) or 'this command'}: a '!' line may not carry caller "
            "arguments into a shell. Use a parameterised tool instead."
            if refusals
            else ""
        ),
        bang=bang,
        risks=risks,
        warnings=tuple(warnings),
        document=document,
    )
