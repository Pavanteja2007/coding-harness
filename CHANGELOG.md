# Changelog

High-level milestones of the build, newest first. This is a readable
summary of what the system can do at each stage — not a commit log.
Per-module detail lives in each module's `AGENTS.md`; the cross-module
contracts in `INTERFACES.md`; the full plan in `project-spec.md`.

## Repo & legal hygiene round (2026-09-14)

The public-repo table stakes, as one commit: MIT `LICENSE`; `SECURITY.md`
(private reporting channels, supported versions, an honest in/out-of-scope
map of every attack surface this codebase runs — sandbox, MCP server,
CLI, host-side installers, CI — plus pointers to the existing
adversarial-round evidence so reporters don't re-probe held ground);
`CODE_OF_CONDUCT.md` (Contributor Covenant v2.1, verbatim + contact
point); issue templates (bug / feature, `config.yml` routing security
reports away from public issues and linking discussions) and a PR
checklist template encoding the repo's two load-bearing rules (verifier
gate, original-repo-never-mutated) plus module-contract and lint-ratchet
hygiene. Set via the GitHub API: repo description, 12 topics,
discussions, Dependabot security updates + vulnerability alerts (20
pre-existing dependency alerts now surfaced and trackable). The social
preview image (`.github/social-preview.png`, 1280x640 brand card,
oxblood on ink) is generated and committed, but its upload is
UI-only — one manual step left:
repo Settings → Social preview → upload that file.

## Install round (2026-09-13)

Vex is now curl-installable — no PyPI package needed. Three one-line
installers live at the repo root and are reachable at predictable
raw.githubusercontent URLs (see README "Install"):

- `install.sh` — macOS / Linux / WSL: pipx if available (isolated,
  per-app venvs), else a dedicated `~/.vex-venv` + `~/.vex/bin` shims;
  idempotent profile PATH hints; friendly failures for missing/old
  Python, missing git, and the Windows-Python-under-Git-Bash case
  (redirects to the Windows installers).
- `install.ps1` — Windows PowerShell (5.1-compatible; `irm | iex`):
  same logic; python.org directory-scan fallback for "installed
  without Add-to-PATH"; USER-path registry write (idempotent, never
  setx — it truncates at 1024 chars).
- `install.cmd` — Windows CMD: same logic; `py -3` launcher probe;
  only `exit /b` (never closes the caller's terminal); CRLF-committed
  (cmd's label scanner breaks on LF — pinned via `.gitattributes`).

Supporting changes: `vex --version` (the installers' verification
step; importlib.metadata with a source-tree fallback), packaging fix
(flat-layout multi-package builds now specify the package list
explicitly — `pip install .` from the repo was broken before), dist
name `vex-agent-cli` (console script remains `vex`).

All three verified live: WSL (venv + pipx-contract + re-run
idempotence + failure paths + Git Bash guard), PowerShell 5.1 (file +
`iex` flows, registry writes), CMD (full download-and-run flow,
same-session PATH).

## v0.1.0 (2026-09-09)

Initial tagged release: the full four-layer system, built and validated
end-to-end.

### The four layers, integrated

- **Harness** (`harness/`) — planner → step-agent → verifier-gated
  completion. Bash-only agent loop (mini-swe-agent style) with
  per-step fresh sessions, constraint re-injection, protected paths,
  RECALL (on-demand reinjection of compacted context from the task
  trace), checkpoint/resume at step granularity, and product-grade
  output on verified fixes: git branch/commit/PR description +
  grounded rationale.md. Success is only ever *claimed* when the
  target test passes AND the full suite shows no regressions — the
  verifier gate is absolute.
- **Execution** (`execution/`) — Docker sandbox: fresh `--rm`
  container per command, networkless by default, read-only rootfs,
  `--cap-drop ALL`, mem/cpu/pids limits; stateless three-valued
  verification with flake detection; orphaned-container reaping;
  cross-process image-build locking (proven at 40–50 concurrent).
- **Runtime** (`runtime/`) — process-per-task scheduler proven at
  45 concurrent tasks with 8 simultaneous mid-run hard kills
  (45/45 success, 8/8 genuine resumes, zero leaked containers);
  checkpoint/resume across kills; approval gate; per-call cost ledger.
- **Memory + MCP** (`memory/`, `mcp_server/`) — tree-sitter code
  graph + SQLite decision memory (auto-ingesting every task's
  structured state at any depth), exposed as 5 MCP tools over stdio
  to any MCP client (Claude Code, Cursor, …), plus an MCP client for
  consuming external servers.

### The novel mechanism: adaptive model routing (measured)

Per call, the runtime predicts difficulty from intrinsic signal (issue
text) + struggle signal (conversation tail) and routes easy/medium
calls to a cheap model, hard calls to an expensive one. Ablations on
real bugs, real models, full real stack:

- 5 fixture bugs: same 5/5 success at **45% of baseline cost**
- 16-task expanded set (varied bug classes + issue styles): 16/16
  both arms at **39% of baseline cost**, 3.3x faster wall
- 5 real OSS repos (more-itertools, arrow, inflect, boltons,
  python-semver): adaptive 3/5 vs always-expensive 2/5 at **24% of
  the cost** — the routing margin holds on unfamiliar multi-hundred-
  K-token repos

Honesty notes: free-tier endpoints, proxy price rates, single reps —
directional, not benchmark-grade. Details and artifact paths:
`README.md`.

### Validation beyond the fixture set

- jaraco/path (unfamiliar real OSS repo): full-DoD run — verifier-gated
  success in 1 attempt with a real cloud model ($0.053, 6 calls),
  git output, approval gate, memory ingestion. The first attempt's
  honest failure exposed and fixed a real harness bug (binary-artifact
  diff crash) — kept as part of the record.
- python-semver: module DoD, 15/15 checks (baseline → broken-state
  detection → in-sandbox fix → verified, flake-flagging, git output).
- Runtime stress: 45 tasks @ concurrency 45 with 8 mid-run kills —
  all resumed, zero leaks.
- Sandbox adversarially confirmed: 24/24 sequential + concurrent
  attack suites (escape, resource exhaustion, cross-container probes)
  — every isolation property held.

### Security hardening (Round 6)

Adversarial passes over the MCP server, CLI, and sandbox. One real
data leak found live (path traversal in `task_status` /
`harness status --task-id` on Windows drive-letter semantics), fixed
via a shared semantic task-id guard (`memory/paths.py`), and pinned
by 101 adversarial tests across the MCP + CLI boundary suites.
Full probe matrices and outcomes: `mcp_server/AGENTS.md`,
`cli/AGENTS.md`, `execution/AGENTS.md`.

### CI (Round 7)

- `.github/workflows/ci.yml` — harness + runtime suites across the OS
  matrix, light per-push stress, nightly full stress + adversarial
  abuse suites.
- `.github/workflows/memory-cli-ci.yml` — memory/MCP/CLI/dashboard
  suites, including the real stdio MCP-client round-trip, across
  Linux/Windows/macOS × Python 3.10/3.12. Includes a pinned
  regression for the SDK `errlog` import-time-binding flake fixed
  this round.

### Known limitations (documented, not hidden)

- Failure classification is deliberately not implemented (the chosen
  novel mechanism was adaptive routing, not repair strategy — the
  `last_feedback` seam remains where it would plug in).
- Free-tier endpoints made some Round-6 multi-repo runs flaky
  (empty/timeout model responses) — the harness refused to claim
  success in every such case; final sweep numbers pending endpoint
  stability.
- SWE-bench Lite deferred to Phase 6 per spec. Python-only. stdio-only
  MCP transport. No web UI beyond the read-only dashboard.

### Pre-history (Rounds 1–5, untagged)

Round-by-round build history — initial contracts and stubs
(`INTERFACES.md`), each module's Phase-1 milestone, integration of
the real stack (real Docker sandbox, real scheduler, real cloud
model), resume/checkpoint contract, adaptive-routing ablations at
two scales, demo packaging — is preserved in each module's
`AGENTS.md` and the git history. `demo/run_demo.py` tells the whole
story offline in one command.
