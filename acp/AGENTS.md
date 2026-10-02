# ACP module handoff

## VEX-CEILING-16 - the request/result paths a real agent exercises (2026-09-26)

Ceiling-16 wired `neo acp` (`cli/serve.py::acp_stdio_serve`) to this
adapter through a real `agent_sdk.Agent`. Getting a real handshake to
complete found three defects in `acp/server.py` that the existing
stub-agent tests could not, because a stub returns strings and a real agent
returns objects. **All three are fixed here; the 29-test suite plus a real
handshake is green.**

### 1. A SYNC stream could never terminate (`_consume_stream`)

```python
item = await asyncio.to_thread(next, iterator)   # raises StopIteration
except StopIteration: break
```

`StopIteration` cannot cross a Future boundary: asyncio converts it to
`TypeError: StopIteration interacts badly with generators and cannot be
raised into a Future`. The exception escaped the loop, so **every
`session/prompt` against a real agent hung forever** - it looked like a slow
model, not a bug. Fixed with a module sentinel and `next(it,
_ITERATOR_DONE)`, which never raises:

```python
_ITERATOR_DONE: Any = object()
item = await asyncio.to_thread(next, iterator, _ITERATOR_DONE)
if item is _ITERATOR_DONE:
    break
```

### 2. A CONVERSATION id was being sent as a RUN id (`_invoke_agent_method`)

The signature matcher treated `run_id` as a session name. `agent_sdk.Agent.stream`
has `stream(request, *, run_id=..., after_sequence=..., **kwargs)`, so the
ACP session id was bound to `run_id`, the adapter then asked for the events
of a run that does not exist, and the turn failed closed with
`event journal does not exist: .../sess_<id>/trace.jsonl`. A prompt and a
replay are different operations; conflating them makes one unusable.

Session names are now split into **strong** (`session_id`, `sessionId`,
`session`, `conversation_id`, `conversation`) and **weak** (`run_id`). Only
strong names are bound during the normal pass. A weak name is filled in at
the very end, and only when the agent names NEITHER a conversation
parameter nor `**kwargs` - delivering nothing is worse than delivering the
conversation id under a loose name.

### 3. `cwd` is no longer injected through `**kwargs`

The `accepts_kwargs` branch used to do `kwargs.setdefault("cwd", cwd)`. A
method with `**kwargs` frequently forwards them into a typed constructor -
`agent_sdk`'s `RunRequest` is the real example, and it has no `cwd` field -
so every real-agent prompt died with
`RunRequest.__init__() got an unexpected keyword argument 'cwd'`.
Guessing an argument name is how "prompt the agent" becomes a TypeError.
`cwd` is now bound ONLY to an explicitly named cwd-ish parameter; `session_id`
still flows through `**kwargs` (that convention is the tested contract and
every agent in the suite names it).

### 4. A terminal result must be JSON-serializable (`_result_model`)

`_result_model` passed the agent's terminal value straight into
`ACPPromptResult.result`. A real `agent_sdk.Event` is not a JSON type, so
`asend` died inside `json.dumps` and the peer saw no response at all. New
`_json_safe_public` projects an arbitrary public value: primitives pass
through, mappings / `to_dict()` objects / sequences are walked recursively
(depth- and width-bounded), and anything else becomes its bounded string
form. Lossy, but always parseable - and it means a server can never fail to
answer with an unserializable payload.

### 5. The transport contract this module already had, spelled out

`ACPServer._reader_loop` distinguishes END OF STREAM from an idle poll by
reading `transport.closed`. A transport that never exposes `closed` leaves
the server running after its peer disconnects. The server-side stdio
transport `cli/serve.py::ProcessStdioTransport` implements the whole set:
`start`, `areceive(timeout)`, `asend`, `aclose`, `close`, and `closed`.
It is documented in `cli/AGENTS.md`; the three non-obvious parts are the
polled `queue.Queue` hand-off (a sync `start()` is invoked in a worker
thread, so the loop cannot be captured), text-not-bytes writes, and the
`closed` property.

### What this round did NOT change

- Protocol version stays `1`, still fail-closed on anything else.
- `StdioACPTransport` (the CLIENT side) is untouched.
- The 29-test suite passes unmodified. No test pin was weakened.
- Optional ACP filesystem / terminal / MCP proxy methods are still absent,
  and permission callbacks are still supported rather than enforced.

## Built surfaces

- `acp.models` defines ACP protocol version `1`, JSON-RPC constants, redacted `ACPError`, `ACPRequest`, `ACPResponse`, `ACPCapabilities`, `ACPSession`, and `ACPPromptResult` models, plus canonical status and stop-reason policy.
- `acp.transport` provides injectable newline-delimited JSON-RPC transports: `InMemoryACPTransport` and `StdioACPTransport`. The stdio transport uses an argv vector with `shell=False`, scrubs the child environment through `shared.security.scrub_environment`, keeps protocol data on stdout, drains stderr into a bounded redacted tail without logging, and supports timeout, cancellation, and close.
- `acp.client` implements concurrent initialize, optional authenticate, session/new, session/prompt, session/cancel, session/set_mode, update callbacks/history, raw requests/notifications, shutdown, and close. A background reader matches responses by JSON-RPC id while preserving session/update arrival order and retaining out-of-order responses.
- Optional permission hooks are available as `on_permission_request`/`permission_handler` (or `permission_hook`) and `on_permission_response`/`permission_response_handler` (or `permission_response`) on `ACPClient`; the server accepts the corresponding `permission_handler`/`on_permission_request` and response hooks for agent-originated permission requests. Without a request hook, the client returns a safe reject outcome; without a response hook, normal behavior is unchanged.
- `acp.server` implements the agent-facing side of the same protocol. It consumes only public `query`, `run`, `stream`, `cancel`, `replay`, and `close` attributes on a duck-typed Agent or Conversation. It does not import or inspect private agent implementation modules.
- Server stream chunks become `session/update` notifications. Diagnostics and public projections pass through `shared.security` redaction while wire protocol payloads remain byte-faithful JSON values. A completion is marked verified only when explicit target and regression evidence is clean; a model success claim without evidence is downgraded to `completed_unverified`.
- `acp.protocol` and package exports provide compatibility imports for the model and transport surfaces.

## Public assumptions

- ACP v1 is represented as JSON-RPC 2.0 objects with one JSON object per line. Unsupported protocol versions and malformed envelopes fail closed.
- The client is the request/response and notification initiator. The server may send `session/update` notifications before the terminal `session/prompt` response.
- Authentication is advertised through v1 `authMethods`: default/`agent` methods use `authenticate`, `terminal` methods are never sent through `authenticate`, `never` is unauthenticated, and `host`/`bearer` descriptors are transport-owned. Host/bearer descriptors with an explicit host are loopback-only by default; non-loopback hosts require `allow_non_loopback=True`.
- JSON-RPC reader timeouts are idle polls, not connection/request deadlines. Each request waits for its exact ID, preserves deferred responses, and rejects duplicate terminal IDs without resetting a live connection.
- Boolean IDs, null request params, malformed prompt blocks, unknown required capabilities, and conflicting initialization versions fail closed with typed JSON-RPC errors.
- Cancellation keeps the transport open, awaits the v1 `session/cancel` request so the peer has processed the public cancellation path, maps a JSON-RPC request ID to the current captured run handle, bounds cleanup by a short grace period, and treats the idle-session invalid-params response as a safe no-op. Stdio frames are bounded and strict UTF-8/JSONL; redaction is applied to diagnostics and public projections, never to protocol payloads.
- The stable ACP stop-reason mapping is `end_turn` for completed or input-needing turns, `cancelled` for cancellation, `max_turn_requests` for timeout, and `refusal` for blocked/failed outcomes.
- Agent methods may be synchronous or asynchronous. Blocking agent calls and generic synchronous transport calls are moved off the event loop; the built-in in-memory async receive path polls without occupying a worker thread.
- Session and agent identities are supplied by the embedding application or generated as opaque IDs. The adapter does not assume filesystem, provider, Docker, or network availability.

## Tests

Run the offline scoped suite with:

```text
python -m pytest tests/test_acp.py -q -p no:randomly
```

The suite covers version negotiation, ordered updates and terminal results, verified-evidence gating, cancellation before and during prompts, request-ID matching/deferred responses, malformed/unknown/version failures, strict session/prompt/capability validation, authentication and modes, permission hooks, bounded/invalid stdio framing, wire-preserving diagnostics, redaction, and subprocess cleanup. It uses no network, provider, or Docker lane. Latest scoped result: **29 passed**. The idle-session cancellation regression also passed 10 consecutive isolated runs.

Run lint with:

```text
python -m ruff check acp tests/test_acp.py
```

## Source-stability and reproducibility

- The ACP source set remained byte-stable during both independent build pairs; `acp/client.py` Git blob hash: `1a0710e3c7b46e365b70db49c52e976742c72638`.
- Repeated `python -B -m build` followed by `python -B scripts/verify_release.py --compare-dist` twice with `SOURCE_DATE_EPOCH=1790072773`; both reports returned `reproducible: true`.
- Both pairs produced wheel SHA-256 `b037a7c652d32ec99e7f75d60611764ad2cbb2834a970e1c657af73c85a81696` and sdist SHA-256 `788b340ef8467d54dc735ff717d987662e71d68fba1e554e4a6ad620da456fdf`.
- The reports were written outside the checkout under `C:\Users\pavan\AppData\Local\Temp\opencode\acp-repro-pair-1` and `acp-repro-pair-2`; the checkout remains dirty and untagged, so this is verification evidence only, not a publication or clean-release claim.

## Blocked lanes

- `agent_sdk` now provides the public `Agent`/`Conversation` methods used by the adapter; the ACP implementation remains structurally duck-typed and imports no SDK-private module.
- The adapter targets the stable ACP v1 baseline (`initialize`, `session/new`, `session/prompt`, `session/cancel`, `session/update`, and optional `session/set_mode`). ACP v2 lifecycle/state semantics are not claimed.
- No external ACP package, network provider, Docker verifier, or live model process was used; the stdio fixture and in-memory transport are real local protocol lanes.
- The adapter does not implement optional ACP filesystem, terminal, MCP proxy, or session/load methods. Permission request/response hooks are supported as callbacks; filesystem, terminal, and MCP service methods still require separate public client-side service contracts and are not faked.
- The shared checkout is dirty and untagged, so the reproducibility evidence is intentionally not a clean-release or publication claim. Packaging/release owns any clean/tag gate and installed-wheel smoke lane.

## Handoffs

- Connect `ACPServer` to `agent_sdk.Agent` or `Conversation`; the adapter calls only public `stream`/`query`/`run`, `cancel`, `replay`, and `close` methods.
- Another bidirectional JSONL transport only needs public `start`, `send`/`asend`, `receive`/`areceive`, and `close` methods; the client/server adapters already accept sync and async callables.
- Add filesystem, terminal, or MCP proxy methods only after their ACP v1 client-side contracts are exposed as injected public services. Permission callbacks are already optional and transport-safe.
- Packaging/release should add `acp` to the explicit setuptools package list and run an installed stdio fixture smoke test.
- Any ACP contract change should be recorded by the integration owner; this module keeps version `1` fail-closed and does not silently accept v2 or future versions.
