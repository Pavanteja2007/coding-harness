# integrations/ — External Tooling and OAuth

## Scope

This module owns dependency-light integration adapters for external MCP servers,
OAuth-protected HTTP MCP resources, and model-facing tool discovery. It does not
import `memory.mcp_client`, `mcp_server`, or any other module-owned private
implementation. The shared dirty tree is preserved; no contract, packaging, or
cross-module files are changed here.

## Built surfaces

### `mcp.py`

- `MCPTransportConfig` is an immutable, validated configuration for `stdio`,
  `sse`, and `streamable_http`. It supports command argv sequences, HTTP(S)
  endpoints, bounded timeouts/output, explicit child environments, and safe
  configuration projections.
- `MCPConnection` is the connection facade. `MCPAdapter` is the owned async
  lifecycle with `connect`, `list_tools`, `call_tool`, `close`, and async
  context-manager support.
- The official SDK is imported only when a real connector is entered:
  `mcp.client.stdio.stdio_client`, `mcp.client.sse.sse_client`, and the
  installed Streamable HTTP client factory. The Streamable HTTP spelling is
  selected from the SDK (`streamable_http_client` or the older
  `streamablehttp_client` name) without trying another transport.
- `connector_factory` and `session_factory` are injectable and accept common
  sync, async, async-context-manager, and mapping response shapes. This keeps
  transport failure, timeout, cleanup, and OAuth retry lanes offline.
- Child processes receive a narrow default environment. Provider keys, bearer
  tokens, password-shaped variables, and credential-shaped variables are not
  inherited. An explicit environment remains an intentional caller override.
- Public errors are typed and redacted: `MCPTransportError`,
  `MCPConnectionError`, `MCPTimeoutError`, `MCPAuthenticationError`,
  `MCPUnsupportedAuthError`, `MCPProtocolError`, and related aliases. Output
  and error projections are bounded. Session cleanup precedes transport
  cleanup, is idempotent, and records cleanup failures in `cleanup_errors`.
- Tool definitions and call results normalize SDK objects and mappings into
  `ToolDescriptor` values and stable dictionaries. Secret-shaped content is
  redacted and binary blocks are omitted.

### `oauth.py`

- Immutable `OAuthConfig` supports authorization-code + PKCE S256, RFC 7636
  verifier/challenge generation, optional resource/issuer parameters, and
  strict endpoint/redirect validation.
- `OAuthManager` creates cryptographically random state, stores only a
  one-way state digest, binds callbacks to the initiating client and session,
  compares digests with `hmac.compare_digest`, enforces TTL, and consumes state
  exactly once. Plaintext state and PKCE material are absent from diagnostic
  projections and representations.
- `InMemoryOAuthStateStore`, `InMemoryTokenStorage`, and `FileTokenStorage` are
  bounded. File storage uses a private directory, mode `0600` token files,
  size checks, fsync, and atomic replacement. Token values never appear in
  public representations or public error messages.
- Authorization-code exchange and refresh-token grants are injectable for
  offline tests. A real token exchange lazily uses `httpx` or `httpx2` when no
  exchanger is injected. `OAuthTokenProvider` is the adapter-facing hook.
- `MCPAdapter(oauth_provider=...)` initiates authorization after a typed 401
  and retries the failed operation exactly once. The official SDK's
  `OAuthClientProvider` can be passed as an SDK auth object; if an installed
  SDK requires a different redirect/callback provider contract, the adapter
  raises `MCPUnsupportedAuthError` with the handoff requirement instead of
  pretending the callback was handled.

### `tools.py`

- `ToolDescriptor`, `DeferredTool`, and `ToolCatalog` provide typed exact
  lookup, deterministic name/description/tag ranking, max-result bounds, eager
  model schemas, and deferred summaries.
- Deferred descriptors expose no full input schema before
  `resolve_schema`/`load_schema`. Loaders are schema-only; an optional
  executor is never invoked by resolution. Invalid, failed, or oversized
  resolutions become bounded `ToolResolutionError` values.
- `validate_json_schema` is dependency-free and covers the common MCP object,
  array, scalar, combinator, enum, and constraint keywords. Recipe/platform
  required-tool checks raise `RequiredToolsError` without executing tools.
- Search projections contain only descriptor metadata; arguments, execution
  results, and private metadata are not searched or returned by deferred
  discovery. MCP argv/config projections redact secrets before repr, diagnostics,
  or wire metadata.

## Official SDK assumptions

- Python: 3.10-compatible syntax and standard-library typing.
- MCP dependency: the project declares `mcp>=2,<3`; this workspace was
  verified against `mcp 2.1.1`.
- Verified SDK surfaces: `StdioServerParameters`, `stdio_client`,
  `ClientSession`, `sse_client`, and `streamable_http_client`. The adapter
  keeps the older Streamable HTTP function spelling as a version-compatibility
  path within the same selected transport.
- OAuth HTTP transport headers use the SDK's official SSE/Streamable HTTP
  mechanisms. SDK-specific OAuth redirect and callback handlers remain an
  explicit provider handoff; they are not duplicated or silently emulated.
- `httpx` or `httpx2` is required only for a live token-endpoint exchange when
  no exchanger is injected. All required regression lanes are offline.

## Tests and verification

`tests/test_integrations.py` covers all three configuration paths, official
SSE/Streamable HTTP factory selection, a real stdio roundtrip against a tiny
temporary `MCPServer` fixture, injected transport failure, asyncio timeout,
cleanup ordering, one-retry OAuth hooks, PKCE/state mismatch/replay/expiry,
client/session/redirect binding, token redaction, atomic token storage, refresh,
tool ranking, deferred loading, schema failures, and required-tool validation.

Final scoped commands:

```text
python -m pytest tests/test_integrations.py -q
ruff check integrations tests/test_integrations.py
ruff format --check integrations tests/test_integrations.py
```

The final run for this handoff is recorded in the assistant result: 16 tests
passed, Ruff check passed, and Ruff format check passed.

## Environmental and provider blockers

- No live OAuth provider, browser callback, network credential, or external
  MCP HTTP service was used. Live-provider behavior remains an integration
  handoff, not a claimed test result.
- The repository's dirty `mcp_server/server.py` was not edited or imported by
  the adapter. The real stdio regression therefore uses a tiny temporary
  official-SDK server fixture, keeping this module independent of that dirty
  file.
- Packaging and cross-module wiring are intentionally outside this task's
  allowed paths. Consumers should pass an `OAuthManager`/token provider and
  complete the browser callback explicitly, and should provide schema loaders
  for deferred tools.

## Exact handoffs

1. Construct `MCPAdapter` with an `MCPTransportConfig`; use injected factories
   for offline tests or leave them unset for official SDK transports.
2. For OAuth HTTP servers, provide `oauth_provider` or `token_provider`. On a
   401, obtain the returned authorization URL, complete the callback with
   `OAuthManager.handle_callback`, exchange it, and let the adapter perform
   its single retry. Do not pass a guessed SDK callback signature.
3. Register deferred tools with a schema-only loader. Call
   `resolve_schema`/`load_schema` before validation or model-schema exposure.
4. Validate recipe/platform requirements with `ToolCatalog`; no integration
   code in this directory executes a tool as a side effect of discovery.

## R2-16 - this module was reviewed, and deliberately NOT changed (2026-09-26)

The R2-16 prompt listed `integrations/` in its file ownership. It was read and
audited against the prompt's six items and none of the changes belonged here.
Recording that, because "I did not change it" is a result worth stating rather
than leaving to be inferred from an absent diff.

- **MCP namespace / least privilege / tool pinning.** `MCPAdapter.call_tool`
   is a real external-MCP call path and a plausible second enforcement point,
   but it is NOT the path the product uses: `cli/connectors.py` calls
   `memory.mcp_client` (the synchronous stdio facade), and the agent loop calls
   `memory.mcp_client` too. Adding a second gate here would create a second
   enforcement policy for the same threat, and the prompt's own test
   requirement ("the layer is either reached by a real call or absent")
   would be satisfied by a path nothing uses. The namespace layer was instead
   wired where the real call happens. `integrations.MCPAdapter` also has no
   config surface to read a connector declaration from, and giving it one would
   have meant a second, divergent declaration schema.
- **User hooks.** Nothing here is a product call path; it is an adapter
   library consumed programmatically. The hook gate went on
   `cli/connectors.call_tool`.
- **`neo migrate`.** Nothing here holds configuration or state.
- **Connector permissions.** The schema is `cli/connectors.py`
   (`[connector_permissions]`), which is the same file that resolves a
   connector label. Putting the declaration next to resolution is what makes
   the two impossible to drift.
- **Plugin install atomicity / uninstall completeness.** `cli/plugins.py`
   owns the plugins root.
- **`doctor` / support bundle.** `cli/doctor.py` owns the health surface.

The one thing worth recording for a future reader: `integrations/mcp.py` IS the
only client in the tree that preserves a tools full `inputSchema` (through
`ToolDescriptor` / `resolve_schema`). If `memory.mcp_client` is ever taught to
preserve the full descriptor - which is what the network-host gate needs, see
`mcp_server/AGENTS.md` - the fields this module already models are the
vocabulary to copy, not a new one.

No file under `integrations/` was edited this round. `tests/test_integrations.py`
was run green (part of the 76-passed MCP/integrations selection) and is
unmodified.
