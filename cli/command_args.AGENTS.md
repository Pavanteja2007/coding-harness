# VEX-CS-05 — flags and argument parsing (`cli/command_args.py`)

**Session:** `VEX-CS-05-args-flags` · **Date:** 2026-09-30
**Machine-readable handoff:** `logs/command-surface/terminal-05.json`
**Measurements:** `logs/command-surface/terminal-05-measure.json`, produced by
`logs/command-surface/terminal05_measure.py` (host-only: no Docker, no
provider, no network, no credential).

**Files created:** `cli/command_args.py` (NEW), `tests/test_command_args.py`
(NEW), `logs/command-surface/terminal05_measure.py` (NEW).
**Files edited:** none. `cli/tui.py` and `cli/commands.py` were NOT opened for
editing and NOT written to. `cli/interactive.py` was read only.

**Why this file is separate from `cli/AGENTS.md`:** that file is being appended
to concurrently by the other four terminals in this wave, and a lost update is
worse than a second file. Fold this section in when the wave settles. Nothing
else in the tree is affected.

**No `INTERFACES.md` contract, event kind, journal field, serialized field,
completion status or verifier mint changed, and no `harness/config.py`
`DEFAULTS` key was added** — pinned by four source-level tests in the suite
(`TestWhatThisModuleMustNotGrow`), all read with `ast` rather than a substring
scan so a docstring cannot satisfy or break them.

---

## 0. Read this first — the six numbers

| measurement | number |
|---|---:|
| `prepare()` on a historical `$ARGUMENTS` template, 40-word args | **0.57 ms** median, 0.67 ms p95 |
| `prepare()` on the richest supported body (frontmatter + 6 placeholder forms + a `!` line) | **0.66 ms** median, 0.94 ms p95 |
| `split_arguments` on `"hello world" second` | **0.014 ms** median |
| `split_arguments` on 40 words | **0.37 ms** median |
| one real `!` subprocess (`echo`) round trip | **58.7 ms** |
| a real `!` timeout, 0.25 s budget | **632 ms** observed — it binds (see §5) |

`prepare()` on a **400-word** argument string is **4.49 ms** median / 5.40 ms
p95. That is the one figure worth knowing before mounting this on a keystroke
path: it is linear in argument length, so a caller preparing on every
keystroke should prepare on SUBMIT. `ARGUMENTS_MAX_TOKENS = 512` is the point
at which the cost stops growing.

Every figure is from `terminal05_measure.py` on this host, 200 samples per row,
`time.perf_counter`, in the JSON. None is a recollection.

---

## 1. What is built

One self-contained, total, dependency-free module. A dispatcher adopts it with
**one call** — `prepare(template, name=..., arguments=...)` — and gets back a
`PreparedPrompt` carrying the finished body, the resolved flags, the parsed
arguments, every `!` result, the shell-risk audit, and a `usage_error` sentence
if something was refused. **Nothing in the product calls it yet**; §4 is the
mount, and that sentence is the honest state of the feature, not a caveat.

| piece | symbol | note |
|---|---|---|
| flag declarations | `FlagSpec`, `COMMAND_FLAGS`, `declare_flags`, `flags_for`, `command_flag_table` | the ONE table every reader shares |
| the undeclared-flag refusal | `undeclared_flag_message`, `CommandArgError` | always lists what the command *does* declare |
| the tokenizer | `split_arguments` | `shlex.split(posix=True)`, plus one documented character guard |
| the parse | `parse_arguments` → `ParsedArguments` | flags, named bindings, positionals, warnings |
| the document | `parse_command_document` → `CommandDocument` | optional, deliberately tiny frontmatter |
| the placeholders | `substitute`, `has_argument_slot`, `environment_values` | `$ARGUMENTS`, `$ARGUMENTS[N]`, `$N`, `$name`, `${NAME}`, `\$` |
| the `!` preprocessor | `run_bang`, `preprocess_bang`, `BangResult` | runs BEFORE substitution; output is labelled |
| the safety audit | `shell_risk` → `ShellRisk` | reports, never rewrites |
| discoverability | `argument_usage`, `flag_help_lines` | the `/help <command>` block |
| **the one entry point** | `prepare` → `PreparedPrompt` | order is the contract; see §3 |

### The three reference commands, declared

```
/code-review     [--fix]
/security-review [--fix]
/simplify        [--fix]
```

`COMMAND_FLAGS` also ships `/my-skill` with `--fix`, `--level <low|medium|high>`
and `--budget <BUDGET>` as a worked example of a valued flag and a choice.
`COMMAND_FLAGS` is **empty for all 56 registry commands on purpose**: adding a
flag to a shipped command changes what an existing invocation means, and this
round is not allowed to change what they mean. A caller registers its own row
through `declare_flags` — the only writer — and the parse path, the usage line,
`/help` and the palette then all read the same table.

---

## 2. The rules that are commonly got wrong, and where each is pinned

| rule | test |
|---|---|
| **`$0` is the FIRST argument** (0-based, and `$N` == `$ARGUMENTS[N]`) | `test_dollar_zero_is_the_first_argument_not_the_second` |
| an out-of-range **indexed** placeholder is left **UNCHANGED** | `test_an_out_of_range_indexed_placeholder_stays_unchanged` |
| a **named** placeholder with no value expands to **EMPTY** | `test_a_named_placeholder_with_no_value_expands_to_empty` |
| an **undeclared** `$word` is left **UNCHANGED** (`$HOME` is not eaten) | `test_an_undeclared_dollar_word_is_left_unchanged` |
| `\$1.00` is a literal dollar and does not substitute | `test_an_escaped_dollar_placeholder_does_not_substitute` |
| a body with no `$ARGUMENTS` gets `ARGUMENTS: …` appended | `test_a_body_with_no_placeholder_gets_the_arguments_appended` |
| a `!` line carrying a placeholder is **refused**, never executed | `test_a_bang_line_carrying_a_placeholder_is_refused_and_never_executed` |
| `!` output is **LABELLED** on both boundaries | `test_the_output_is_labelled_on_both_boundaries` |
| an undeclared flag is a usage error **listing the declared ones** | `test_an_undeclared_flag_is_a_usage_error_that_lists_the_declared_ones` |
| every existing template substitutes **byte-identically** | `test_every_command_template_in_the_tree_matches_fill_template` |

**The asymmetry is deliberate and stated in the module docstring:** an
out-of-range index leaves the token visible (a reader can see the template
wanted something nobody typed), while a named parameter with no value expands to
empty (silence is the correct rendering of "not supplied").

---

## 3. The order inside `prepare()` IS the safety property

```
1. parse the document            (frontmatter off, body retained)
2. parse the arguments           (shlex; declared flags; declared names)
3. run the `!` lines             <-- BEFORE any substitution
4. substitute                    ($ARGUMENTS / $N / $name / ${NAME} / \$)
5. append the arguments          (only when the body had no $ARGUMENTS slot)
6. audit                         (shell_risk + every warning, reported)
```

A `!` line is replaced **wholesale** by a labelled block, so no argument value
can be part of an executed command line *even in principle*. On top of that, a
`!` line that still contains a placeholder is **refused and never run**:
running first is what stops an argument reaching a shell, and refusing the line
is what stops a template *author* from putting one there (`!echo $ARGUMENTS`
would hand unsanitised caller input to a shell — exactly the
`/x foo.txt; rm -rf ~` case).

**Measured, not asserted** (`measure_safety` in the JSON):
`marker_created: false`, `commands_executed: ["echo safe"]`,
`hostile_argument_in_any_command: false`,
`prompt_still_carries_the_argument: true`. The argument reaches the prompt and
nowhere near a shell.

Two more properties of the same kind, and the tests that hold them:

* The output blocks are **restored byte-for-byte** across substitution
  (`protect=` in `substitute`). Found by running it: the block quotes the
  command that produced it, and a later pass rewrote the label, so the receipt
  described a command the template did not contain. Pinned by
  `test_a_label_containing_a_dollar_survives_substitution_unchanged`.
* `shell_risk` **ignores its own labelled blocks**. Otherwise the audit cries
  wolf on the module's correct output and a surface learns to ignore it.
  Pinned by `test_the_shell_audit_ignores_its_own_labelled_output_blocks`.

### The tokenizer, and the one character guard

The split is `shlex.split(..., posix=True)`. There is no second tokenizer, and
the test reads the function's **AST** to prove it: the only call in
`split_arguments` that produces tokens is `shlex.split`, and across the whole
module there is one `shlex` import and one `shlex.split` call.

The guard (`_escape_interior_quotes`) backslash-escapes a quote character
sitting **inside a bare word**, because a slash command's arguments are natural
language and raw POSIX semantics are hostile to that:

| input | raw `shlex` | with the guard |
|---|---|---|
| `don't stop` | `ValueError: No closing quotation` | `["don't", "stop"]` |
| `a"b"c` | `["abc"]` — quotes silently deleted | `['a"b"c']` |
| `"hello world" second` | `["hello world", "second"]` | unchanged |

It is a character rewrite, not a split: it never decides where a token starts
or ends, and it only fires for a quote with non-whitespace on **both** sides.
`disable_interior_quote_escape=True` restores raw `shlex` semantics for a caller
who wants them, and a genuinely unterminated quote is still a usage error
(exit 2) — a typo a user made should be visible, not guessed at.

---

## 4. Handoff to 01 — `cli/commands.py` and `cli/tui.py` (NOT edited)

Four additive edits. Nothing else needs to change. `cli/tui.py` and
`cli/commands.py` were not opened.

### 4.1 `CommandSpec.flags` (ONE source — this is the mount the brief asked for)

`cli/command_args.py::COMMAND_FLAGS` is the table today. To move it onto the
registry, add one field and one line in `cli/commands.py`:

```python
# cli/commands.py — the dataclass
@dataclass(frozen=True)
class CommandSpec:
    ...
    flags: Tuple["command_args.FlagSpec", ...] = ()   # additive, defaulted

    def flag_help(self) -> list:
        """One `/help <command>` block for this command's flags."""
        from cli import command_args as _ca
        return _ca.flag_help_lines(self.name, flags=self.flags)

# cli/commands.py — the ONE writer, immediately after COMMAND_SPECS is built
from cli import command_args as _ca
for _name in ("code-review", "security-review", "simplify"):   # only if/when
    _ca.declare_flags(_name, _ca.flags_for(_name))              # these are specs
```

**Do it this way rather than duplicating the table.** `command_argparse`'s
`_validate_headless_tables` at import already shows what a second copy costs:
it has taken the whole `cli` package offline twice in this tree's history. One
table, one writer, one reader set.

The alternative — leaving `COMMAND_FLAGS` as the authority and having
`CommandSpec` read it — needs **zero** edits to `commands.py` and is what the
module is already written for. Prefer it; the field above is only for a row
that wants to own its own grammar.

### 4.2 Use `prepare()` where `fill_template()` is called today

Four call sites, all identical in shape (REPL `interactive.py:6855` and
`interactive.py:7275`, TUI `tui.py:5161` and `tui.py:5470`):

```python
# before
filled = commands_mod.fill_template(template, arguments)

# after
from cli import command_args as _ca
prompt = _ca.prepare(
    template,
    name=name,
    arguments=arguments,
    cwd=state.get("repo"),
    session_id=state.get("session_id"),
    effort=state.get("effort"),
)
if not prompt.ok:
    say(prompt.usage_error)      # plain text; escape it on the way out
    return
filled = prompt.text
```

**For every command file in the tree today this is a no-op**: measured 6/6
byte-identical against `fill_template` over both real templates
(`.neo/commands/fix.md` and the plugin `commands/review.md`) × three argument
shapes, and pinned by a test that globs the files itself. The one shape that
*moves* is a body using `$0` with no `$ARGUMENTS`, which now also gets the
`ARGUMENTS:` append — over-inclusion, and the model sees everything. No template
in the tree does that.

### 4.3 `/help <command>` shows the flags

`/help` is a **projection of the registry**, so this is the discoverability
requirement rather than a nice-to-have:

```python
# cli/interactive.py::render_help, for the `render_help("<command>")` branch
from cli import command_args as _ca
lines.extend(_ca.flag_help_lines(command))     # [] when nothing is declared
```

`_ca.flag_help_lines` returns `[]` for a command that declares nothing, so the
call is safe to append unconditionally with no `if`. It returns the usage line,
one row per flag (word, type, default, choices, description, short, aliases),
one row per declared parameter, and the placeholder cheat-sheet.

### 4.4 The palette (a one-liner, if 03 wants the flags visible there too)

```python
# cli/tui.py — where the palette row is built
from cli import command_args as _ca
hint = " ".join(flag.word for flag in _ca.flags_for(spec.name))
```

**Plain text on the way out.** Every string this module returns is plain; a
repository path or an argument may contain `[`, and a line that crossed into
Textual's markup parser carrying one would *delete a message* rather than print
it. Use `cli.ui.escape` on the way out, or return `rich.text.Text`.

---

## 5. The measurement found a real defect, and it is fixed

`subprocess.run(shell=True, timeout=0.25)` against `sleep 5` reported
`timed_out` only after **5096 ms**. Killing the shell leaves a grandchild
holding the output pipe, and `communicate` waits for the pipe — so a "bounded"
wait was an *unbounded* one on exactly the platform this product ships on, and
the docstring's claim that a hanging command template cannot hold a session was
a claim rather than a fact.

`run_bang` now uses `Popen` with `stdin=DEVNULL`, a merged stderr stream, a
new process group / session, and `_kill_process_tree` on expiry (`taskkill /F /T`
on Windows, `os.killpg` on POSIX) followed by a bounded re-drain.

| | before | after |
|---|---:|---:|
| 0.25 s budget on `sleep 30` | **5096 ms** | **632 ms** |

The residual 632 ms is `taskkill` itself, and the receipt says so when the tree
could not be killed. Gated by
`test_the_timeout_really_bounds_the_wait_rather_than_being_reported_late`,
which asserts a real 30-second sleep returns in under 5 s. `stdin=DEVNULL` is
part of the same fix: a `!` line that inherited stdin could steal the user's
prompt.

---

## 6. Defects this round's own tests found (four, all fixed)

1. **The label was being rewritten by substitution.** `!touch $ARGUMENTS`
   rendered a refusal whose *label* read `touch pwned; rm -rf ~` — the
   substituted text, not the template. Fixed with `protect=` literal spans
   (`_apply_outside`), which restores the blocks byte-for-byte.
2. **`$0` was indexing a flag.** `/code-review --fix src/a.py` gave
   `$0 == "--fix"`. Fixed with one rule — declared flags do not occupy an index
   — pinned by two tests. A conditional rule ("strip flags only when a grammar
   exists") would have been a second thing to get wrong.
3. **A missing required named argument was accepted when the argument string
   was empty**, because `parse_arguments` returned early. The early return is
   gone; binding and the required check now always run.
4. **The byte-identity gate proved almost nothing.** The discovery globbed
   `commands/*.md` under both roots and found **1** file instead of 2. Fixed in
   the test and the driver, and the count is now asserted (`>= 2`) with both
   files named, so losing either is a named failure.

A fifth, in the **measurement driver rather than the product**: it globbed the
same wrong way, so the first report claimed `templates_checked: 1`. Corrected to
2, and both names are in the JSON.

---

## 7. Verification actually run (this tree, `-p no:randomly`, `PYTHONIOENCODING=utf-8`)

- **`tests/test_command_args.py` → 153 passed** (~5 s). Host-only: no Docker, no
  provider, no network, no credential. Every shell claim is a real
  `subprocess` against a command the test itself wrote, under `tmp_path`.
  13 classes, one per required proof, one test per behaviour, named after the
  behaviour.
- **REQUIRED lane** `tests/test_cli_slash2.py` → **39 passed** (90.16 s).
- `tests/test_cli_command_system.py` → **92 passed** (6.14 s).
- `tests/test_cli_session.py` + `tests/test_cli_terminal_parity.py` → **93
  passed, 1 failed** (45.51 s). The one failure is §8.1.
- `tests/test_cli_plugins.py` → **48 passed, 1 skipped, 1 failed** (9.17 s).
  The failure is §8.1.
- `python -m ruff check --no-cache` on all three new files → **All checks
  passed**; `ruff format` applied to all three (all are new files, so no
  formatter touched another terminal's region).
  `python -m compileall -q` → exit 0. `git diff --check` → exit 0 (all three
  files are **untracked** in this shared tree, so `git diff` cannot see them;
  that is stated rather than presented as a clean check).
- `python logs/command-surface/terminal05_measure.py` → the JSON in §0.

## 8. Reds, attributed, and NOT counted as passes

**8.1 Two failures in other terminals' files, from this shared tree. Neither
involves this module, and the proof is structural: `cli/command_args.py` is not
imported by anything in the product yet** (a repository-wide search finds the
name only in the module itself, in `harness/lsp.py`'s unrelated private
`_command_args`, and in this round's test file). They are also mid-flight: the
tree failed to import three separate times during this round with
`/hooks is flag-only but names no headless equivalent`,
`_subcommand_argument_hint() takes 1 positional argument but 2 were given`, and
`/undo verb 'apply' is claimed by both commit and apply` — all three from
`cli/commands.py`'s live subcommand work.

1. `tests/test_cli_plugins.py::test_slash_unknown_lists_available_customs` —
   the unknown-command refusal no longer lists the available custom commands.
   The string `custom commands available` is still present **once** in
   `cli/interactive.py`, and `commands.unknown_command_line` is now called
   before it; the preflight VEX-TERM-UX-07 moved the refusal into the shared
   contract and the custom-command hint is not in that sentence yet.
   **Owner: whoever owns the unknown-command refusal.** The fix is to add the
   hint to `commands.unknown_command_line` (or to the preflight) rather than
   restore a second copy in the REPL.
2. `tests/test_cli_terminal_parity.py::test_a_flag_only_refusal_names_what_to_run_instead`
   — `/plugins probe` resolves to `invalid` rather than `flag`, because
   `/plugins`' new subcommand rows changed its `argument_policy` handling. The
   subcommand work in `cli/commands.py`. **Owner: that terminal.**

No assertion was weakened and no pin was retargeted by this round.

**8.2 Two lane runs were blocked by a mid-edit tree, and were re-run after it
settled.** Both required commands are green on the final tree, and the numbers
in §7 are from that final run.

## 9. Not run, and not claimed

- **No Docker lane and no live-provider lane.** Nothing here needs either, and
  no credential was inspected, printed or retained.
- **No `python -m evals.run`.** This round changed no prompt, planner, repair
  feedback or step system, so the matrix is unchanged by construction as well
  as by measurement.
- **No full-suite run.** The six lanes in §7 were run and every failure in them
  is attributed in §8.
- **No real attached-PTY campaign.** The `!` feature is proven through real
  `subprocess` calls, and the tokeniser through unit tests; a pipe is not a
  terminal and this module renders nothing, so a PTY run would prove nothing it
  has not already proved.
- **No end-to-end drive through a shell.** Nothing in the product calls
  `prepare` yet (§4), so an e2e drive is not possible until 01 mounts it. The
  closest proof today is
  `test_a_custom_command_still_loads_and_still_runs_its_filled_body`, which goes
  through the historical `commands.load_command` and asserts the filled body is
  identical.

## 10. Not implemented, stated plainly

- **Nothing in the product calls this module.** §4 is the mount. `/code-review
  --fix` is not reachable from `/` yet, and `/help <command>` does not show a
  flag table yet. This is the round's largest honest gap and it is filed as a
  request rather than smuggled in by editing another terminal's file.
- **No `CommandSpec.flags` field.** Deliberately: the field needs
  `cli/commands.py`, and the table is already the one source. The exact patch
  is in §4.1 for whoever wants the field.
- **The flag grammar is opt-in.** A command with no declared flags keeps
  `--token` as a positional argument, because `/fix make --no-verify` must keep
  working (rule 8). So "an undeclared flag is always a usage error" is TRUE only
  for a command with a grammar. `strict=True` per call, or a
  `COMMAND_FLAG_STRICT` row, opts the rest in; the refusal then says "declares
  no flags" rather than swallowing the token. This is the one place the brief's
  rule needed narrowing, and the narrowing is pinned by two tests.
- **The append rule fires for a body that uses only `$0`.** The rule is written
  against `$ARGUMENTS`, so `$0` does not suppress it. Stated in §2 and pinned;
  no template in the tree is affected.
- **Frontmatter is a deliberately tiny dialect** (top-level `key: value`, plus a
  block sequence under `arguments:`), the same shape `harness/skills.py` parses.
  No YAML dependency is added. An unrecognised key is reported in
  `unrecognized`, never swallowed.
- **No short-flag `-f` support beyond a declared `short=`.** There is no
  clustering (`-abc`) and no `-fvalue`.
- **The `!` runner is a real shell** (`shell=True`), which is correct for an
  author-written shell line and is why the placeholder REFUSAL is the load-
  bearing control rather than a sanitiser. There is no allow-list of commands
  and no sandbox: a `!` line runs with the user's privileges by design, and the
  trust prompt for that is Wave 2's `/plugin` prompt (Prompt 06), not this
  round's.
- **No command-count, file-size or nesting bound on a command document** beyond
  the existing `commands._MAX_TEMPLATE_CHARS` (20,000) that `load_command`
  already applies on read. `parse_command_document` does not re-impose it, so a
  caller bypassing `load_command` inherits no file bound.

## 11. Cross-terminal requests (NOT applied here)

1. **`cli/commands.py` owner (01)** — §4.1, §4.2, §4.3. Three additive blocks
   and no signature change. The `flags` field is optional; the `prepare` swap
   is byte-identical for every existing command file (measured).
2. **`cli/tui.py` owner (01)** — §4.2 (the TUI half of the swap) and §4.4 (the
   palette hint). Escape the lines on the way out; a repository path may
   contain `[`.
3. **`cli/commands.py` owner — the unknown-command refusal** (§8.1 item 1).
   `commands.unknown_command_line` is the one sentence every surface prints, and
   it currently drops the "custom commands available" hint that
   `tests/test_cli_plugins.py` pins. Adding it there fixes all three surfaces at
   once; restoring the REPL copy would fix one and re-introduce a second.
4. **Nobody should read this module in a completion path.** It declares no
   completion status, no verification word, and no exit code beyond the shared
   usage-error 2. Pinned by
   `test_the_only_exit_code_this_module_declares_is_the_usage_error` and
   `test_the_module_declares_no_completion_or_verification_vocabulary`.

## 12. Shared dirty tree

Nothing was reset, cleaned, checked out, restored, stashed, rebased, staged,
committed, pushed, tagged or uploaded. No VCS or release action of any kind was
taken. Four other terminals were editing `cli/commands.py` and
`cli/interactive.py` throughout; this round's three files are **NEW** and are
untracked, so `git diff` cannot show this round's delta in them. Every symbol a
re-base needs is enumerated in `logs/command-surface/terminal-05.json`.
