# agent_sdk/ — transport-neutral SDK and local Agent Server

## VEX-CEILING-16 - the agent is now reachable from the product surface (2026-09-26)

`Agent` and `AgentServer` were complete and tested but not reachable. This
round put both behind real CLI commands and found the integration gaps that
only appear when a real agent is driven by a real protocol.

### `neo serve` and `neo acp` (`cli/serve.py`, CLI-owned)

- **`neo serve`** drives `AgentServer`. Loopback by default; a non-loopback
  bind needs `--allow-non-loopback` AND a token, and says so in the operator's
  own words. `--json` prints ONE line so a script can read the bound port
  from a single `read()`. Verified against a real subprocess:
  `/health` -> 200 `{"healthy": true, "server": "neo-agent-server"}` and
  `/v1/capabilities` -> 200 `protocol_version: 1`.
- **`neo acp`** binds a real `Agent` to `acp.server.ACPServer` over
  `ProcessStdioTransport`, the server-side mirror of the client transport.
  This closes the "Connect `ACPServer` to `agent_sdk.Agent`" handoff below.

### Constructor shapes a caller must know (both were wrong in my first cut)

- **`Agent(repo_path, *, transport=..., log_root=..., config=..., model=...)`**
  - the first positional is the REPOSITORY, not a transport. `Agent(transport)`
  builds a `LocalTransport` whose `repo_path` is the transport object, and
  fails with `repository path is not a directory: ...<LocalTransport object>`.
  Let `Agent` own its transport.
- **`AgentServer(repo_path, *, host=..., port=..., log_root=..., config=...,
  token=..., allow_non_loopback=...)`** - same shape. Passing `repo_path`
  in a kwargs dict AND the host positionally raises
  `got multiple values for argument 'repo_path'`.

### A `run_id` is not a conversation id

`ACPServer` used to pass the ACP session id to any parameter named `run_id`.
`Agent.stream(request, *, run_id=..., ...)` therefore replayed a run that
does not exist instead of starting one. See `acp/AGENTS.md` for the full
fix. **The consequence for SDK consumers:** the conversation identity
travels as `session_id` (and through `**kwargs` when the method has one);
`run_id` selects an existing run and must be left alone by an adapter.

### The agent's terminal value is not a JSON type

A real `Event` reaching an ACP result killed `json.dumps`. The adapter now
projects through `acp.server._json_safe_public`. **For SDK consumers:** an
event stream's terminal item is an `Event` object, not a dict - read it
through the public `Event` accessors (or `Events`), and do not assume a wire
shape.

### Verification actually run

- `tests/test_agent_sdk.py tests/test_agent_server.py tests/test_acp.py` ->
  part of a **245 passed, 1 skipped** run (with the CLI release suites).
- NEW `tests/test_ceiling16_surfaces.py::TestIntegrationHandshakes` -> 3
  passed, including a real `AgentServer` subprocess handshake and a real
  `Agent` + `ACPServer` + stdio-transport `session/prompt` that returns
  `end_turn` / `completed_unverified` / `verified: false`.
- **No live-provider lane and no Docker lane were run.** Every model call in
  the new tests is an injected callable.
- `python -m ruff check` clean on the files this round touched.

### Still open for the SDK/platform owners

- `session/prompt` carries the stop reason, status, and verified flag
  correctly, but the terminal `result` is a lossy projection of the SDK
  `Event`; the visible answer rides `session/update` chunks. A typed
  terminal-event -> ACP result projection would make the answer first-class.
- No supervision or reconnect story for `AgentServer`: a crashed server is
  restarted by whoever launched it.

## Packaging integration (2026-09-25)

- `agent_sdk` is in the explicit setuptools package list. The verified 0.2.1
  wheel contains all 12 SDK modules and the normalized sdist contains the same
  source package.
- `tests/test_installed_user_flow.py` installs only the immutable wheel into a
  private venv, runs outside the checkout under `python -I -B`, executes a
  deterministic `LocalAgent.query()`, and validates its answer, result schema,
  trace path, and contiguous replay events.
- `scripts/clean_room_matrix.py` runs the same functional SDK smoke for wheel
  and sdist lanes across the release Python matrix. The current Python 3.10
  wheel and sdist lanes both passed this check.
- Exact-candidate installed-wheel verification: **5 passed**, including SDK
  smoke and the real Windows deferred-uninstall flow.

## Built surfaces

- `Agent` and `Conversation` expose the same synchronous query/run, asynchronous handle, stream, replay, history, cancellation, resume, listing, and close contract over local or remote transports.
- `LocalTransport` creates one `AgentKernel` per active run under an absolute log root, injects `ModelGateway(call_fn=...)` for callable models, stores the kernel before worker start, and cancels that kernel directly. No `SessionController`, CLI, UI, or kernel-private module is imported.
- `LocalTransport`, `Agent`, `LocalAgent`, `Conversation`, and `AgentServer` accept optional `hooks`/`hook_manager` injection. Task-before denial or short-circuit produces a traced canonical `blocked` result before model or tool execution; task-after and completion hooks are observational.
- Strict `daily`, `planning`, `question`, and `research` strategies use SDK-owned public facades over `PolicyEngine` and `ToolRegistry`. Permission hooks can only escalate `allow` to `ask`/`deny`; tool-before denial returns a failed tool result before the handler, and after hooks observe the actual result. `verified_fix` and `legacy_agent` receive task-level hooks only because their public strategies do not expose the strict policy/registry seam.
- `RemoteTransport` and `RemoteAgent` use stdlib `urllib`, bearer authentication, typed HTTP failures, JSON event backlog, SSE parsing, and sequence-based reconnect.
- `Result` wraps `shared.agent_contracts.RunResult`, supports mapping access, and downgrades a forged `completed_verified` result without clean evidence.
- `Event`/`EventEnvelope`, `Events`, and `VersionNegotiation` project canonical schema version 1, redact through `shared.security`, validate replay through the public kernel API, and provide contiguous reconnect iteration.
- `Tool`, `RunRequest`, `QueryRequest`, `RunHandle`, `LocalWorkspaceManager`, `RemoteWorkspaceManager`, and `Workspace` provide typed public value objects and durable managed-workspace lifecycle records.
- `AgentServer` is a loopback-default `ThreadingHTTPServer` using only the standard library. It exposes health/capabilities/version negotiation, query/run/status/list/cancel/resume, replay/backlog, SSE, WebSocket upgrade/frame/control messages, workspace REST, and OpenAI-shaped `/v1/models` and `/v1/chat/completions` endpoints.
- Server-managed workspace directories are exposed to remote clients as remote workspace records. No SSH or remote filesystem backend is claimed.
- `AgentServer`, `RemoteTransport`, `RemoteAgent`, `LocalTransport`, `Agent`, and `Conversation` support duck-typed injected `ToolCatalog` discovery through list/search/get/explicit schema methods. Deferred descriptors remain schema-free until `/v1/tools/{name}/schema` or `resolve_tool_schema`; absent catalogs return empty discovery and exact 404s rather than fabricated built-ins.
- Remote clients are loopback/HTTPS constrained, reject URL userinfo and implicit redirects, require versioned responses, and never execute client-local hooks on the peer.
- Server requests and workspace creation are confined to the configured repository; managed copies reject symlinks, credential-shaped files, and unbounded sources, and workspace records are validated on reload.
- Local sessions serialize same-session kernel work, cancellation reports the actual kernel acceptance, resume validates journal/checkpoint identity and strategy, and replay never suppresses malformed terminal journals.
- `AgentServer` can be stopped and restarted with a fresh owned local transport; injected transports are explicitly non-restartable.

## Tests

- `tests/test_agent_sdk.py` covers headless query/run, result mapping, direct cancellation, resume, replay/tamper rejection, reconnect deduplication, version envelopes, redaction, tools, lifecycle hook isolation/bridges, deferred catalog discovery, durable workspace lifecycle, and import boundaries.
- `tests/test_agent_server.py` covers REST/version errors, local/remote parity, async lifecycle, SSE Last-Event-ID replay, remote SSE parsing, WebSocket handshake/frame/control messages, bearer errors, workspaces, OpenAI responses, catalog routes/deferred schemas, and server task-hook denial.
- Verified command: `python -m pytest tests/test_agent_sdk.py tests/test_agent_server.py -q -p no:randomly` → **36 passed**.
- Verified command: `python -m pytest tests/test_agent_sdk.py tests/test_agent_server.py tests/test_agent_kernel.py tests/test_security_regressions.py -q -p no:randomly` → **50 passed, 4 platform skips** for the combined kernel/security selection.
- Extended owned verification: `python -m pytest tests/test_agent_sdk.py tests/test_agent_server.py tests/test_acp.py tests/test_extensions.py tests/test_recipes.py tests/test_integrations.py -q -p no:randomly` → **123 passed, 1 platform skip**.
- Verified command: `python -m ruff check agent_sdk tests/test_agent_sdk.py tests/test_agent_server.py` → **passed**.
- Verified command: `python -m ruff format --check agent_sdk tests/test_agent_sdk.py tests/test_agent_server.py` → **passed**.

## Blocked environmental lanes

- No live provider credential or network model lane was used; all tests inject deterministic local callables.
- No Docker-backed verifier lane was needed or run by this module.
- A broader `tests/test_agent_loop.py` selection was attempted but exceeded the host timeout while other test processes were active; the previously hanging case passes in isolation. Do not report that broader selection as green.
- Packaging is integrated and verified; the former source-only SDK limitation is closed for the current 0.2.1 candidate.

## Cross-owner requests

- Harness owner: keep `harness.agent_kernel` public exports (`AgentKernel`, `ModelGateway`, `RunSpec`, `RunEventJournal`, `replay_run`, `PolicyEngine`, `ToolRegistry`, `CompletionPolicy`, and `build_default_handlers`) compatible with the Boundary-0 contracts used here; do not require SDK consumers to import kernel-private modules.
- Packaging/release owner request is complete: `agent_sdk` is packaged and passes clean installed-wheel functional smoke.
- Integrations owner: keep `ToolCatalog` duck-typed discovery methods (`all_descriptors`/`search`/`resolve` and async `resolve_schema`) bounded, deferred, and secret-redacted; the SDK intentionally does not import a concrete catalog implementation.
- ACP/platform owners: consume `Agent`, `Conversation`, `Result`, `Event`, and `VersionNegotiation` rather than transport or kernel internals; preserve sequence cursors when reconnecting.
- Security owner: review the SDK/server redaction boundary if public error or event projections gain new fields.
- Runtime/execution owners: no cross-module change is required for the offline local/remote parity lane; Docker and live-provider evidence remains owned by their existing lanes.
