# Security Policy

Vex deliberately runs model-generated commands — the whole point of the
harness is executing untrusted agent output against real repos. That
makes its own security boundary (and any hole in it) worth taking
seriously, so reports are welcome and handled promptly.

## Supported versions

| Version | Supported |
|---|---|
| `main` branch | yes |
| tagged releases (`v0.1.x`) | yes, latest tag |

Anything older than the latest tag: still report it — we'll tell you
quickly if it's already fixed.

## Reporting a vulnerability

**Please do not open a public GitHub issue for a security problem.**

Two supported channels, in order of preference:

1. **GitHub private vulnerability reporting** (preferred — routed
   straight to the maintainer, keeps details non-public until a fix
   lands): on this repo, go to the **Security** tab → **Report a
   vulnerability**. If you don't see it, use channel 2 and we'll enable
   the tab.
2. **Email**: pavanteja9030162007@gmail.com (the same address the git
   history already carries), with `[vex security]` in the subject.

Please include what you can of: affected surface (see scope below),
steps or a PoC to reproduce, and your assessment of impact. If you
intend to publish after a fix, say so and propose a date — coordinated
disclosure works, and we won't hold fixes hostage to it.

### What to expect

- Acknowledgment within **7 days**.
- Assessment + a fix-or-wontfix decision within **30 days**.
- Credit in the release notes and the advisory, if you'd like it.

Good-faith research is appreciated: no legal threats, and no
bug-bounty. Please avoid degrading the repo's services (CI, hosted
demo content) while testing.

## Scope

In scope — anything in this repository:

- `execution/` — the Docker sandbox boundary itself: container escapes,
  sandbox-flag bypasses, host-fs exposure, resource-limit evasion.
  (Current posture: read-only rootfs, `--network none`, `--cap-drop
  ALL`, `no-new-privileges`, mem/cpu/pids cgroup limits, non-root user,
  fresh `--rm` container per command; adversarially probed — 24/24
  sequential + concurrent hostile-run suites held, per
  `execution/AGENTS.md`.)
- `mcp_server/` — path traversal, injection, or auth issues in the 5
  exposed tools (the codebase already carries 101 adversarial
  regression tests from Round 6; new bypasses are very in scope).
- `cli/` — anything in `vex` that executes on the host outside the
  sandbox.
- `runtime/`, `harness/`, `memory/`, `dashboard/`, `shared/` —
  path handling, unsafe deserialization, anything that lets task data
  (repo snapshots, model output) escape its `logs/{task_id}/` boundary
  or execute outside it.
- `install.sh` / `install.ps1` / `install.cmd` — the curl-able
  installers run with user privileges on the host.
- CI workflow files (`.github/workflows/`) — injection into or
  poisoning of the build.

Out of scope (but still welcome as regular issues):

- Vulnerabilities in dependencies themselves (litellm, textual, MCP
  SDK…) — report upstream, then open a normal issue or PR here to bump.
- Model-provider or BYO-endpoint behavior beyond our code.
- The static marketing site (`site/`) — no user input is processed, but
  if you find something, a regular issue is fine.

## Known posture (context for reporters)

So effort isn't wasted on surfaces already probed: the sandbox,
MCP server, and CLI were adversarially tested in Round 6 (path
traversal, shell/SQL injection, null bytes, malformed inputs; one real
task-id path-traversal data leak was found and fixed live, pinned by
regression tests). Details and per-probe outcomes live in each module's
`AGENTS.md` and `docs/RESUME_KNOWLEDGE_BASE.md` §4.5 (resource-abuse
caps proven to fire). Cleverer bypasses of any of these are exactly
what we want to hear about.
