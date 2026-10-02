# Headless operation, CI, and SDK

The reliable non-interactive surfaces are explicit subcommands and the Python library. Bare `neo` is intentionally not a piped prompt protocol.

## JSON commands

```bash
neo fix --repo . --issue "..." --target-test tests/test_x.py::test_case --json
neo status --task-id TASK_ID --json
neo scan --repo . --json
neo analyze-history --json
```

`fix --json` and `status --json` keep stdout to one JSON document. The document includes the task identity, status, attempts, usage/cost where available, verification flags, diff, trace/log path, and an exit reason. Human output and provider banners are redirected away from the JSON stream.

Use the numeric exit code as the category signal:

```text
0  success
1  task-level failure
2  usage/configuration error
3  environment error
4  model/network/authentication error
130 interrupted
```

A benchmark command is an orchestration operation, not a single task verdict. Inspect its report and per-task statuses as well as the process code.

## CI example

```powershell
$env:NEO_MODEL = "your-model"
$env:NEO_PROVIDER = "openai"
$env:NEO_BASE_URL = "https://your-router.example/v1"
$env:NEO_API_KEY = $env:CI_MODEL_KEY
$env:NEO_NO_ONBOARD = "1"

python -m evals.run --suite prompt-regression --check --json
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

python -m evals.run --suite daily-driver --no-docker --json
if ($LASTEXITCODE -eq 2) {
    Write-Error "daily-driver readiness failed; inspect the report"
    exit 2
}
```

`--no-docker` is a deliberate blocked lane, not a Docker pass. A CI job that requires Docker should run the real lane and fail when the daemon is unavailable. The daily-driver report records Docker, live-provider, feature, LSP, and manual-repair lanes separately.

## Library: verified fix

The stable compatibility facade remains `harness.core.run_task`:

```python
from pathlib import Path

from harness.core import run_task
from shared.types import Task

result = run_task(
    Task(
        task_id="ci-fix-001",
        repo_path=".",
        issue_text="The parser drops the final empty record.",
        config={
            "model": "your-model",
            "provider": "openai",
            "base_url": "https://your-router.example/v1",
            "target_test": "tests/test_parser.py::test_empty_input",
            "max_retries": 2,
            "log_root": "logs",
        },
    ),
    log_root=Path("logs"),
)
print(result.status, result.verification)
```

Keep the log root outside the repository when scripting a run. The API key is consumed by the router and is not written into the persisted task configuration.

## Library: strict kernel and events

The versioned Boundary-0 contracts live in `shared.agent_contracts`:

```python
from shared.agent_contracts import CompletionStatus, RunEvent, RunSpec

spec = RunSpec(
    session_id="session-1",
    run_id="run-1",
    request="Explain the parser entry point",
    repository_identity=".",
    strategy="question",
)
assert spec.schema_version == 1
assert CompletionStatus.COMPLETED_VERIFIED.value == "completed_verified"
```

Use `harness.agent_kernel.replay_run(path)` for a pure, deterministic projection of a trace. It does not execute tools or call a model.

## Packaged Agent SDK

The `agent_sdk/` tree provides local and remote `Agent`/`Conversation` facades, event streaming, cancellation, replay, resume, workspace lifecycle, and a local HTTP server. It is included in the current 0.2.1 wheel and sdist. The release gate installs only the immutable wheel outside the checkout, verifies module origin, runs a deterministic local query, and validates its trace and replay events.

From a source checkout, a minimal local shape is:

```python
from agent_sdk import Agent

agent = Agent(
    repo_path=".",
    log_root="logs",
    model="your-model",
)
try:
    result = agent.run("Explain the parser entry point", wait=True)
    print(result.status)
finally:
    agent.close()
```

Inject a callable model or a verifier in tests; the SDK's own tests and the clean installed-wheel release smoke use deterministic local callables.

## Trace inspection

The authoritative task trace is `logs/{task_id}/trace.jsonl`. The normalized cross-module overlay is opt-in through `NEO_TRACE_DIR`:

```powershell
$env:NEO_TRACE_DIR = "logs"
python -m shared.traceview TASK_ID --logs-root logs --summary
python -m shared.traceview TASK_ID --logs-root logs --json
```

The viewer merges the harness trace, unified tracing, worker journal, and model ledger read-only. Use the original files for full prompt and tool-output fidelity.
