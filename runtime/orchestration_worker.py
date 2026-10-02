"""Subprocess entrypoint for one bounded orchestration child."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict

from harness.agent_kernel import (
    AgentKernel,
    CompletionPolicy,
    RunResult,
    RunSpec,
    build_default_handlers,
)
from runtime import mock_provider
from runtime.fsutil import (
    atomic_write_json,
    now_iso,
    read_json_or_none,
    restore_sensitive_config,
)
from runtime.model_router import set_call_context
from runtime.roles import build_role_policy, build_role_registry, get_role_profile
from shared.security import redact_text

ORCHESTRATION_SECRETS_ENV = "NEO_ORCHESTRATION_SECRETS"


def _transient_payload() -> tuple[str, list[tuple[tuple[str, ...], Any]]]:
    raw = os.environ.pop(ORCHESTRATION_SECRETS_ENV, None)
    if not raw:
        return "", []
    try:
        payload = json.loads(raw)
        request = payload.get("request", "")
        entries = payload.get("secrets", [])
        if not isinstance(request, str) or not isinstance(entries, list):
            raise ValueError
        normalized: list[tuple[tuple[str, ...], Any]] = []
        for entry in entries:
            if (
                not isinstance(entry, list)
                or len(entry) != 2
                or not isinstance(entry[0], list)
                or not all(isinstance(part, str) for part in entry[0])
            ):
                raise ValueError
            normalized.append((tuple(entry[0]), entry[1]))
        return request, normalized
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid orchestration transient payload") from exc


def _configure_model(config: Dict[str, Any], task_id: str, ledger_dir: Path) -> None:
    set_call_context(
        {
            "adaptive_routing": config.get("adaptive_routing", False),
            "model_tiers": config.get("model_tiers"),
            "difficulty_estimator": config.get("difficulty_estimator", "heuristic"),
            "difficulty_llm": config.get("difficulty_llm"),
            "provider_profile": config.get("provider_profile"),
            "model_prices": config.get("model_prices"),
            "provider": config.get("provider"),
            "model": config.get("model"),
            "api_key": config.get("api_key"),
            "api_base": config.get("api_base"),
            "use_mock_provider": config.get("use_mock_provider", False),
            "rate_limit_retries": config.get("rate_limit_retries", 2),
            "rate_limit_backoff_s": config.get("rate_limit_backoff_s", 5.0),
            "task_id": task_id,
        },
        ledger_dir=str(ledger_dir),
    )


def _install_mock(config: Dict[str, Any]) -> None:
    if not config.get("use_mock_provider", False):
        return
    turns = config.get("orchestration_turns")
    if isinstance(turns, list) and turns:
        queue = [str(item) for item in turns]

        def respond(_messages: list, _model: str) -> str:
            return (
                queue.pop(0)
                if queue
                else '{"tool":"finish","arguments":{"answer":"done"}}'
            )

        mock_provider.install(dynamic=respond)
        return
    mock_provider.install(config.get("mock_responses") or {})


def _update_checkpoint(path: Path, payload: Dict[str, Any]) -> None:
    current = read_json_or_none(path)
    data = dict(current) if isinstance(current, dict) else {}
    data.update(payload)
    data["updated_at"] = now_iso()
    atomic_write_json(path, data)


def _heartbeat(path: Path, stop: threading.Event, fields: Dict[str, Any]) -> None:
    while not stop.wait(1.0):
        try:
            _update_checkpoint(path, {**fields, "heartbeat_epoch": time.time()})
        except OSError:
            continue


def run_child(packet_path: str) -> int:
    """Run one serialized child packet through the public agent kernel."""
    packet = json.loads(Path(packet_path).read_text(encoding="utf-8"))
    if not isinstance(packet, dict) or int(packet.get("schema_version", 0)) != 1:
        raise ValueError("unsupported orchestration packet")
    request, secret_entries = _transient_payload()
    raw_config = packet.get("config", {})
    if not isinstance(raw_config, dict):
        raise ValueError("orchestration config must be an object")
    config = restore_sensitive_config(raw_config, secret_entries)
    node_id = str(packet["node_id"])
    run_id = str(packet["run_id"])
    session_id = str(packet["session_id"])
    workspace_path = Path(str(packet["workspace_path"])).resolve()
    checkpoint_path = Path(str(packet["checkpoint_path"]))
    result_path = Path(str(packet["result_path"]))
    kernel_log_root = Path(str(packet["kernel_log_root"]))
    ledger_dir = Path(str(packet["ledger_dir"]))
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = ledger_dir / "model_ledger.jsonl"
    role = get_role_profile(str(packet["role"]))
    fault = str(packet.get("fault", ""))
    resume = bool(packet.get("resume", False))
    _configure_model(config, node_id, ledger_path)
    _install_mock(config)
    stop = threading.Event()
    base_checkpoint = {
        "schema_version": 1,
        "orchestration_id": str(packet["orchestration_id"]),
        "node_id": node_id,
        "run_id": run_id,
        "session_id": session_id,
        "parent_run_id": str(packet.get("parent_run_id", "")),
        "attempt": int(packet.get("attempt", 0)),
        "attempt_token": str(packet.get("attempt_token", "")),
        "pid": os.getpid(),
        "status": "running",
        "started_at": now_iso(),
        "heartbeat_epoch": time.time(),
        "workspace_path": str(workspace_path),
        "trace_path": str(kernel_log_root / run_id / "trace.jsonl"),
        "fault_injected": False,
    }
    _update_checkpoint(checkpoint_path, base_checkpoint)
    heartbeat_fields = {
        key: base_checkpoint[key]
        for key in (
            "orchestration_id",
            "node_id",
            "run_id",
            "session_id",
            "pid",
            "attempt",
            "attempt_token",
        )
    }
    heartbeat = threading.Thread(
        target=_heartbeat,
        args=(checkpoint_path, stop, heartbeat_fields),
        daemon=True,
    )
    heartbeat.start()
    try:
        if fault == "crash" and not resume:
            _update_checkpoint(checkpoint_path, {"fault_injected": True})
            os._exit(70)
        if fault == "hang" and not resume:
            _update_checkpoint(checkpoint_path, {"fault_injected": True})
            time.sleep(3600)
        registry = build_role_registry(role)
        completion = CompletionPolicy(config=config)
        build_default_handlers(
            registry,
            repo_path=str(workspace_path),
            config=config,
            completion=completion,
        )
        policy = build_role_policy(role, session_id=session_id)
        kernel = AgentKernel(
            repo_path=str(workspace_path),
            log_root=kernel_log_root,
            config=config,
            tool_registry=registry,
            policy_engine=policy,
            completion_policy=completion,
        )
        spec = RunSpec(
            session_id=session_id,
            run_id=run_id,
            request=request or str(packet.get("request", "")),
            repository_identity=str(workspace_path),
            strategy=role.strategy,
            workspace_policy=dict(packet.get("workspace_policy", {})),
            verification_policy=dict(packet.get("verification_policy", {})),
            parent_run_id=str(packet.get("parent_run_id", "")) or None,
            repo_path=str(workspace_path),
            metadata={
                "orchestration_id": str(packet["orchestration_id"]),
                "node_id": node_id,
                "role": role.name,
            },
        )
        result = kernel.run(spec, strategy=role.strategy, resume=resume)
        if not isinstance(result, RunResult):
            result = RunResult.from_dict(dict(result))
        result.run_id = run_id
        result.session_id = session_id
        atomic_write_json(result_path, result.to_dict())
        _update_checkpoint(
            checkpoint_path,
            {
                "status": "finished",
                "finished_at": now_iso(),
                "result": result.to_dict(),
                "fault_injected": bool(fault) and not resume,
            },
        )
        return 0
    except BaseException as exc:
        error = redact_text(f"{type(exc).__name__}: {str(exc)[:500]}")
        _update_checkpoint(
            checkpoint_path,
            {
                "status": "error",
                "finished_at": now_iso(),
                "error": error,
            },
        )
        print(f"orchestration child exception: {error}", file=sys.stderr)
        return 1
    finally:
        stop.set()
        set_call_context(None)
        mock_provider.reset()


def main() -> int:
    """Parse the child packet path and run the child."""
    parser = argparse.ArgumentParser(prog="runtime.orchestration_worker")
    parser.add_argument("--packet", required=True)
    args = parser.parse_args()
    try:
        return run_child(args.packet)
    except SystemExit:
        raise
    except BaseException as exc:
        print(
            f"orchestration worker error: {redact_text(f'{type(exc).__name__}: {str(exc)[:500]}')}",
            file=sys.stderr,
        )
        return 1
    finally:
        set_call_context(None)
        mock_provider.reset()


if __name__ == "__main__":
    sys.exit(main())
