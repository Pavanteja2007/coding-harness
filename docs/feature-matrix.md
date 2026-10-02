# Feature matrix and limitations

Status is for the current source checkout, not automatically for the public PyPI wheel.

Every capability row cites the test, script, or gate that would fail if the
claim stopped being true. `python -m scripts.docs_truth` enforces that: a row
with no evidence reference, an unknown status, or a reference to a file that
does not exist fails the build. An unfalsifiable claim is not a claim.

## User-facing capabilities

| Capability | Status | Notes | Evidence |
|---|---|---|---|
| `daily` as the default agent engine | Blocked | **0/10 on real bugs vs 10/10 for the legacy path it replaces** (policy denies reading a protected test). Do not cut over until SG-01 lands | `scripts/shadow_gate.py` |
| `neo fix` verified bug path | Implemented | Docker sandbox, baseline, target, regression, flake gate, trace, rationale, git-native output. Measured on the `legacy_agent` path | `tests/test_e2e_run_task.py`, `tests/test_cli.py` |
| Interactive TUI and rich REPL | Implemented | TTY entry; TUI falls back when Textual is unavailable | `tests/test_cli_tui.py`, `tests/test_tui_contract.py` |
| General live-repository agent | Implemented | Questions, reads, edits, local commands, diff, undo, resume history | `tests/test_agent_loop.py`, `tests/test_cli_session.py` |
| Plan / Build / Explore / Review / Debug / Ask modes | Implemented | Typed kernel strategies and policy profiles | `tests/test_agent_kernel.py`, `tests/test_ceiling06_orchestration.py` |
| Headless one-shot agent (`neo -p`, `neo -`) | Implemented | Same engine as the TUI, same journal-derived JSON contract and exit codes; piped context and read-only `/plan` `/review` `/ask` | `tests/test_ceiling16_surfaces.py` |
| Integration entry points (`neo serve`, `neo acp`) | Implemented | Loopback agent server and ACP v1 over stdio with the exact editor configuration on first run | `tests/test_ceiling16_surfaces.py`, `tests/test_acp.py` |
| Install/update truth (`neo capabilities`) | Implemented | Capability probe against the live registry; public-release staleness warned once | `tests/test_ceiling16_surfaces.py` |
| Documentation and site truth gate | Implemented | Version parity with `pyproject.toml`; every claim cites evidence; no stale limits | `scripts/docs_truth.py`, `tests/test_ceiling16_surfaces.py` |
| Release evidence aggregate | Implemented | One report over source state, reproducibility, SBOM, vulnerability scan, clean-room install, installed-wheel flow, full tests; publish requires explicit human approval | `scripts/release_evidence.py`, `tests/test_ceiling16_surfaces.py` |
| Provider onboarding and custom OpenAI-compatible routers | Implemented | Health check, profiles, env/config tiers, redaction | `tests/test_cli_onboard.py`, `tests/test_cli_config.py` |
| Adaptive model routing | Implemented | Per-call tier selection and ledger; live quality depends on provider evidence | `tests/test_model_router.py`, `tests/test_ensemble.py` |
| Sessions, checkpoints, and resume | Implemented | Versioned repository/request/revision/namespace binding rejects mismatched checkpoints | `tests/test_cli_session.py`, `tests/test_ceiling03_sessions.py` |
| Approval mode and scoped approvals | Implemented | Exact effect, once/session/path/command scopes; runtime identity hardening is separate | `tests/test_difficulty_approval.py`, `tests/test_cli_command_system.py` |
| Skills and standalone skill management | Implemented | Conservative discovery, bounded injection, enable/disable | `tests/test_skills.py` |
| Plugins and safe read-only tool verbs | Implemented | Local/git install, enable/disable, deny-listed verb safety | `tests/test_cli_plugins.py` |
| External MCP connector registry | Implemented | Global/project/local precedence, bounded health/call | `tests/test_cli_connectors.py` |
| Built-in memory MCP server | Implemented | Five stdio tools; source uses the same decision/graph store | `tests/test_mcp_server.py` |
| Read-only dashboard | Implemented | Reads existing logs; no mutation backend | `tests/test_dashboard_release.py` |
| Shell completion and self-update | Implemented | Dynamic parser-backed completions; update is source/packaging dependent | `tests/test_cli_selfupdate_release.py`, `tests/test_cli_release.py` |
| Agent SDK and local Agent Server | Implemented | Installed-wheel query/replay smoke passes; **not in the public 0.2.0 wheel** | `tests/test_agent_sdk.py`, `tests/test_agent_server.py` |
| Real-OSS fix runs with real Docker | Implemented | 3 real third-party repos end-to-end with a scripted model; **no explicit human manual-repair boolean, so the repair lane is blocked** | `logs/oss-round6/multi_repo_report.json`, `tests/test_daily_driver_evals.py` |
| Editor integration over ACP v1 | Implemented | Real stdio + in-memory transports, 29 tests. **Not verified against a shipping editor (e.g. Zed); optional ACP filesystem/terminal/MCP-proxy/`session/load` methods are absent; ACP v2 not claimed** | `tests/test_acp.py` |
| Terminal accessibility: no-colour, reduced motion, high contrast | Implemented | Verified on a real attached PTY in 2 of 3 rendering profiles | `tests/test_cli_terminal_ux.py`, `tests/test_cli_theme.py` |
| Terminal accessibility: screen-reader announcements | Blocked | Promised by the terminal-UX round and recorded as delivered; **never implemented**. The terminal-UX suite has no announcement test because there is no announcement path to test | `tests/test_cli_terminal_ux.py` |
| Published benchmark with per-task detail | Implemented | Deterministic 14-task matrix, real-OSS ablation, and one measured improvement deliberately not shipped. **No SWE-bench number is claimed** | `scripts/docs_truth.py`, `tests/test_evals_run.py` |
| Version-truth reconciliation and release runbook | Implemented | Source version is 0.3.0; 0.2.0 remains the only public release; lane-by-lane evidence with blocked and not-run rows named | `pyproject.toml`, `scripts/docs_truth.py` |
| Release artifact publication | Blocked by owner/process | Requires clean reviewed tag, artifacts, and owner credentials; no upload is automated | `scripts/verify_release.py`, `scripts/release_evidence.py` |
| Live-provider product-readiness matrix | Blocked unless selected | Deterministic probes pass machinery; provider quality requires explicit lane evidence. **Currently blocked: the available credential is rejected with HTTP 401, so no live model-quality evidence exists** | `tests/test_live_quality.py` |
| Native Windows ConPTY real-terminal evidence | Blocked | Driver implemented; `CreatePseudoConsole` fails in a session with no interactive window station. WSL POSIX PTY is the real-terminal evidence | `logs/terminal-ux/terminal-09-conpty.json`, `tests/test_cli.py` |
| LSP diagnostic repair in daily-driver | Blocked | Public capability exists, but the daily-driver boundary is not fully wired | `tests/test_lsp.py` |

## Evidence and quality gates

| Gate | Current interpretation |
|---|---|
| Prompt host self-check | Real host-side task-set validation; no Docker/model required. Last run: 14/14 CLEAN. |
| Prompt-regression matrix | Real Docker sandbox and verifier, scripted model. Last run: 14 tasks × 8 arms, 98/98 valid comparisons, 0 regressions, CLEAN. |
| Daily-driver deterministic matrix | Real product code with scripted models and isolated state; proves receipts and safety, not model quality |
| Real Docker canary | Real sandbox/verifier; still not a real-provider quality result unless the provider lane also ran |
| Live-provider canary | Real provider request; does not automatically cover every daily-driver category. **Currently blocked (HTTP 401).** |
| Manual-repair sample | Requires explicit boolean observations; absent data is blocked, never inferred. **Currently blocked — 3 real-OSS runs carry no such boolean.** |
| Real attached-PTY probe | Real `NeoApp` on a real pseudo-terminal across three rendering profiles. Last run: 2 of 3 green; `TERM=dumb` fails the modal-open gate. |
| Full release gate | Must include required Docker/provider lanes; skipped lanes are not green |
| Full test suite | **Not run in this round**: the shared tree is moving. A full-suite result against a moving tree is not a gate. |

## Known limitations

- **The public PyPI release is 0.2.0. No version has ever been cut on GitHub.** The source is 0.3.0. The documentation describes 0.3.0 while `pip install` gives 0.2.0 — see `release-evidence.md`.
- **The 0.3.0 default agent engine scores 0/10 on real bugs, against 10/10 for the legacy path it replaces.** This is the release blocker; see `release-verdict.md` and `known-issues.md` SG-01.
- Every success figure published for Neo was measured on the `legacy_agent` path, not on the engine 0.3.0 makes default.
- The public 0.2.0 wheel predates the `agent_sdk`, `acp`, `integrations`, `recipes`, and `extensions` payload.
- **No live-provider evidence exists for this tree.** The available credential is rejected with HTTP 401, so no claim about any model's coding ability, real latency, or real token spend is made anywhere.
- **Real third-party repositories succeed 40–60% of the time**, not 100%. The 100% figures are on fixture and synthesized tasks.
- Every cost figure uses proxy price rates, not billing. Every task set is a single repetition with no confidence intervals.
- **The real-PTY accessibility harness is untracked** (`logs/` is gitignored), so no a11y receipt in this project is reproducible from a clean clone.
- `TERM=dumb` fails the 250 ms modal-open gate (measured 254.763 ms). `NO_COLOR` passes with 22 ms of headroom.
- Two blockers ship on the new `daily` default path: the kernel cannot read a `protected_paths` test (SG-01), and `knowledge_enabled=False` crashes (SG-02).
- `neo doctor`'s MCP check raises `ValueError: too many values to unpack`, and the `memory` connector declares no permissions.
- Strict-mode resume identity is bound to repository, request, revision, and run namespace; legacy checkpoints still require a fresh run.
- LSP repair is not a complete daily-driver capability in this checkout.
- The deterministic demo uses an explicit local subprocess sandbox fallback; it is not Docker evidence.
- The bind-mounted `/workspace` is writable and has no portable per-bind disk quota.
- Retrieval and RECALL are keyword/subword based, not semantic embedding retrieval.
- The code graph's dynamic call resolution is name-based and intentionally over-approximates.
- The Python-first smell detector does not claim equivalent JavaScript/TypeScript smell coverage.
- The Python-first ecosystem registry does not claim equivalent language coverage; Go is the second real ecosystem and other languages are a data change, not a shipped capability.
- No plugin marketplace, SWE-bench headline, interactive clarification loop, or full web product UI is claimed.
- Some historical provider and stress runs are kept as evidence with their endpoint and environment caveats; they are not current readiness claims.

## Deferred future work

The project specification keeps broader multi-file strategy diversity, self-verification, confidence-aware output, rollback UX, multi-repo memory, a plugin marketplace, a web dashboard beyond the read-only view, and language-agnostic expansion beyond the current source work as future work. They are listed here so absence is intentional and visible.
