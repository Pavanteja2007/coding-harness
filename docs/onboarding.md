# Onboarding: install to first verified fix

A guided path with a **checkable success condition at every step**. If a step
does not print the expected line, stop there — later steps assume it.

Expect about 15 minutes. Steps 1–4 need no model and no Docker; step 5 is the
first real run.

---

## Step 0 — know which version you are about to get

```bash
pip index versions neo-agent-cli        # or: python -m pip install neo-agent-cli
neo --version
```

| what you see | what it means |
|---|---|
| `0.2.0` | The last **published** release. This is what `pip install` gives a stranger today. |
| `0.3.0` | The current source candidate. Not published; not installable from PyPI. See `release-evidence.md`. |

`0.2.0` does **not** contain `agent_sdk`, `acp`, `integrations`, `recipes`, or
`extensions`. If you need those, you need the source checkout:

```bash
git clone https://github.com/Pavanteja2007/coding-harness
cd coding-harness
python -m pip install -e .
python -m cli --version
```

One-line installers (`install.sh`, `install.ps1`, `install.cmd`) also exist and
are PyPI-first: they install 0.2.0 unless you set `NEO_INSTALL_REPO`.

---

## Step 1 — confirm the install is sound

```bash
neo capabilities
```

**Success looks like:** a line naming the version, then `OK` for every command
and slash command, and a non-zero count if something is missing.

Two lines worth understanding:

- `neo 0.2.1 (docs describe 0.3.0)` — the installed distribution is older than
  this checkout's declared version. The probe is telling you the truth about a
  stale install. Harmless in a source checkout; not harmless if you thought you
  had 0.3.0.
- A `MISSING` line naming a surface the docs describe but the installed wheel
  lacks. This is the check that catches "the documentation describes an
  artifact nobody can install".

```bash
neo doctor
```

**Success looks like:** mostly `ok`, and — importantly — **any failure names a
remediation you can run.** A health check that raises a Python traceback is
itself a defect; see `known-issues.md` DOC-01 for one that does.

On a healthy machine you should see Docker, git identity, litellm, Textual, and
a writable settings directory all reported `ok`. Provider reachability says
`ok` with "no model configured yet" until step 3 — that is expected.

```bash
neo doctor --json > doctor.json     # attachable to a bug report
```

---

## Step 2 — prove the machinery works before you trust it

Still no model, still no Docker, ~30 seconds:

```bash
python -m evals.run --check
```

**Success looks like:** `CLEAN`, `pass_count: 14`, `fail_count: 0`,
`skip_count: 0`.

This validates that each of 14 fixed tasks genuinely fails before a fix and
passes after one. It is the cheapest possible proof that the *verifier* is
honest — which matters, because everything in step 5 depends on it.

If you want the whole thing with real Docker, ~15 min:

```bash
python -m evals.run --suite prompt-regression --json
```

**Success looks like:** `"verdict": "CLEAN"`, `"regressions": []`. 14 tasks × 8
arms through the real sandbox and verifier.

---

## Step 3 — configure a model

The wizard health-checks the endpoint before it saves anything:

```bash
neo login
neo config status
```

Or, non-interactively (preferred in CI):

```bash
export NEO_BASE_URL="https://your-router.example/v1"
export NEO_MODEL="your-model-name"
export NEO_PROVIDER="openai"
export NEO_API_KEY="your-secret"
neo config status --json
```

**Success looks like:** `neo config status` reporting the provider, model, and
base URL, with the key redacted.

> **If this fails, read the error before assuming it is the network.** A
> rejected credential surfaces through litellm as
> `InternalServerError: ... [WinError 10061] No connection could be made`,
> which looks exactly like an outage and usually is not. The layered probe is
> in `release-runbook.md` §5. This exact confusion cost this project a
> misattributed blocker (`known-issues.md` SG-03).

Never put a key in a committed `.neo/settings.toml`. Project settings are
chmod 600 on POSIX.

---

## Step 4 — see it work offline, no credentials

```bash
python demo/run_demo.py
python demo/agent_demo.py
```

**Success looks like:** both exit 0. The first shows the verifier-gated fix,
git-native artifacts, the routing summary, decision memory, and the code graph.
The second shows question, `@file` context, plan preview, approval, edit, diff,
undo, resume replay, and compaction.

Both use **scripted models** and the local subprocess sandbox fallback. They are
deterministic and reproducible; they are **not** Docker or provider evidence.
Read `../demo/README.md` before presenting either as anything else.

---

## Step 5 — the first verified fix

You need a repository with a Python test. The quickest honest one is this
project's own bundled fixture.

### In a real repository

```bash
cd /path/to/a/python/repo

neo fix --repo . \
  --issue "mean() returns the sum instead of the arithmetic mean" \
  --target-test tests/test_mathutil.py::test_mean
```

`--target-test` is not optional decoration: it is the precise contract the
verifier checks. Without it you get a run, but not a *verified* run.

### What "verified" means, exactly

A run only reports `success` when **all** of these hold:

1. the target test passes, **and**
2. the full suite shows no new failures, **and**
3. the target is not flaky across repetitions, **and**
4. no protected path (including test configuration) was modified to make it
   pass, **and**
5. at least one test was actually collected — zero collected tests is
   `no_tests_collected`, never a pass.

A run with no declared verifier reports **`completed_unverified`**. That is not
a failure and not a softer word for success; it is a different, honest answer.
If a tool you use tells you a run is "done", check which of these two it said.

### Read the evidence

```bash
neo status --task-id <task-id>
cat logs/<task-id>/rationale.md          # why, grounded in the repo
cat logs/<task-id>/git.json              # branch / commit / PR description
python -m shared.traceview <task-id> --logs-root logs --summary
```

**Success looks like:** a status line naming verification, a `rationale.md` that
cites real files, and a trace with `task_start` … `result`.

The original repository is **never** mutated. The fix is composed in private
`pristine/` and `work/` copies under the task log directory.

### Verify the claim yourself

```bash
cd logs/<task-id>/work && python -m pytest <target-test> -q
```

If that does not pass, the verifier was wrong. That is a bug worth reporting
with the trace — it is the most valuable bug this project can receive. See
`../.github/ISSUE_TEMPLATE/bug_report.md`.

---

## Step 6 — keep going, or stop cleanly

```bash
neo                      # TUI on a TTY, rich REPL otherwise
neo -p "explain this repo"          # one-shot headless
neo -                    # piped context
neo acp                  # editor integration over ACP v1
neo serve                # local agent server
neo support-bundle --out neo-bundle.zip     # attach to a bug report
neo uninstall --dry-run               # see everything it would remove
```

> ### ⚠ If you are on the 0.3.0 source checkout, pin the engine
>
> 0.3.0 makes the `daily` engine the default, and that engine is **currently
> broken for real bug-fixing**: measured 0/10 against 10/10 for the legacy
> path, because its policy refuses to let the agent read the test that defines
> success. See [`release-verdict.md`](release-verdict.md) and
> [`known-issues.md`](known-issues.md) SG-01.
>
> Until that is fixed, keep the working engine. There is no environment
> variable for it — it is a project setting. Add to `.neo/settings.toml`:
>
> ```toml
> agent_default_strategy = "legacy_agent"
> ```
>
> For a single call, pass `config={"agent_strategy": "legacy_agent"}`, or call
> `from harness.agent_loop import run_agent_legacy` directly. The compatibility
> path is fully supported, not deprecated.
>
> `neo fix` still works; it is the interactive/agent path that is affected.
> Check `release-verdict.md` before assuming this still applies.

Two more honesty notes before you build a habit on these:

- **`neo acp` is a real ACP v1 stdio adapter** (5,587 lines, 29 passing tests),
  but it has **not** been verified end-to-end against a shipping editor such as
  Zed, and it does not implement the optional ACP filesystem, terminal, MCP
  proxy, or `session/load` methods. See `feature-matrix.md`.
- **The daily interactive path now runs the `daily` engine by default.** That
  is a deliberate breaking change in 0.3.0, and it ships with two known
  blockers (`known-issues.md` SG-01, SG-02). If you hit one, that is a known
  issue, not your mistake.

---

## When something goes wrong

1. `neo doctor --json` — read the `remediation` field, it is a real command.
2. `neo status --task-id <id>` and `python -m shared.traceview <id> --summary`.
3. [Troubleshooting](troubleshooting.md) for the classified failure modes.
4. [Known issues](known-issues.md) before filing — it may already be recorded,
   with a reproducer.
5. [The bug-report template](../.github/ISSUE_TEMPLATE/bug_report.md) otherwise.
   It asks for the version, the exact command, and the trace, which is the
   minimum needed to reproduce anything.
