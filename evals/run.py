"""The prompt-regression eval runner (Task B core).

Method (mirrors runtime/ablation.py's paired-arm discipline, applied to
prompts):

  - SAME fixed task set (evals.tasks.all_tasks) for every arm.
  - Each arm = a dict of task.config overrides (a PROMPT/feature
    configuration), run through the REAL harness.core.run_task with a
    scripted model (deterministic; zero network) and the REAL Docker
    sandbox + verifier. Nothing stubbed except the model reply.
  - Per task we score:
      outcome            success/failed/error/timeout (status)
      attempts           how many verify-gated retries it took
      verified           target + regression + not flaky (from the result)
      loop_integrity     trace.jsonl has task_start AND task_end AND
                         result; no plan_parse_error; no unhandled
                         no-command nudges loop; files_touched recorded
  - A REGRESSION = a task (or integrity check) whose outcome worsens
    vs the baseline arm; the report says exactly which check moved.

Arms shipped by the harness itself live in ARMS below; the improvement
round's prompt changes are the ON state of their config keys, so the
comparison arms toggle them OFF one at a time AND all at once
("pre_round" = the pre-improvement prompt surface).

  Determinism notes (honest): scripted models make replies identical,
  but wall-clock timing still varies (Docker warm/cold). Timing is
  reported but never a gate. Each task runs with a FRESH log dir
  (logs/evals/<run-id>/<arm>/<slug> as log_root) so runs never share
  state; the memory-informed-planning arm additionally gets an ISOLATED,
  pre-seeded decision store so its behavior is reproducible (a real
  store with exactly one relevant decision + noise entries).

  Output: logs/evals/<run-id>/eval_report.json + stdout matrix; --json
  emits only the machine-readable report (for CI gating).

"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals import tasks as eval_tasks  # noqa: E402 — path boot first
from harness.deps import set_call_model  # noqa: E402
from shared.types import Task  # noqa: E402

# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------

# Base config every arm shares (lean, deterministic, REAL Docker verify).
_BASE: Dict[str, Any] = {
    "test_command": "python -m pytest -q",
    "command_timeout_s": 60,
    "verify_timeout_s": 180,
    "max_step_turns": 8,
    "max_retries": 2,
    # product-output paths are not what the eval measures; keep runs lean
    "git_output": False,
    "rationale_log": False,
    # routing is NOT under test (single-tier scripted model); the router
    # still runs and records usage — the eval's loop uses it verbatim.
    "adaptive_routing": False,
    "model_tiers": None,
    "use_mock_provider": True,
}

# The improvement-round prompt features, as config keys (T1's landed set).
_ROUND_KEYS = [
    "plan_with_memory",  # memory-informed planning (planner prompt)
    "lint_gate",  # structured lint feedback (repair loop)
    "docs_lookup_enabled",  # DOCS escape (step system prompt)
    "agent_tests",  # agent-written regression tests (step prompt)
    "web_fetch_enabled",  # FETCH web-page reading (step system prompt)
    "skills_enabled",  # skills system (planner prompt; Plugins round)
]

ARMS: Dict[str, Dict[str, Any]] = {
    # what ships today: every improvement-round prompt feature ON
    "baseline": {},
    # each feature off, one at a time — isolates its prompt change
    "no_memory": {"plan_with_memory": False},
    "no_lint": {"lint_gate": False, "lint_names": False},
    "no_docs": {"docs_lookup_enabled": False},
    "no_agent_tests": {"agent_tests": False},
    "no_webfetch": {"web_fetch_enabled": False},
    "no_skills": {"skills_enabled": False},
    # the pre-improvement prompt surface, wholesale
    "pre_round": {k: False for k in _ROUND_KEYS},
}


class _FeedbackAwareScriptedModel:
    def __init__(
        self,
        plan: List[Dict[str, Any]],
        scripts: Dict[int, List[Any]],
        feedback_contracts: Dict[str, Any],
    ) -> None:
        from tests.fake_model import SESSION_START_MARKER, ScriptedModel

        self.inner = ScriptedModel(plan=plan, scripts=scripts)
        self.session_marker = SESSION_START_MARKER
        self.feedback_contracts = feedback_contracts
        self.attempt_by_step: Dict[int, int] = {}
        self.observations: List[Dict[str, Any]] = []
        self.missing_observations: List[Dict[str, Any]] = []
        self.prompts: List[str] = []

    def __call__(self, messages: List[Dict[str, str]], **kwargs: Any) -> str:
        self.prompts.append(
            "\n".join(str(item.get("content") or "") for item in messages)
        )
        system = next(
            (
                str(item.get("content") or "")
                for item in messages
                if item.get("role") == "system"
            ),
            "",
        )
        if "planning a bug fix" in system:
            return self.inner(messages, **kwargs)
        match = re.search(r"your step is #(\d+) of", system)
        step_id = int(match.group(1)) if match else 0
        users = [
            str(item.get("content") or "")
            for item in messages
            if item.get("role") == "user"
        ]
        session_start = bool(users and self.session_marker in users[-1])
        if session_start:
            self.attempt_by_step[step_id] = self.attempt_by_step.get(step_id, 0) + 1
        attempt = self.attempt_by_step.get(step_id, 1)
        contract = self._contract(step_id, attempt)
        if contract:
            existing = next(
                (
                    item
                    for item in self.observations
                    if item.get("step") == step_id and item.get("attempt") == attempt
                ),
                None,
            )
            if existing is not None and existing.get("ok") is not True:
                return "echo FEEDBACK_CONTRACT_MISSING"
            if existing is None:
                prompt = "\n".join(
                    str(item.get("content") or "") for item in messages
                ).casefold()
                markers = [str(marker) for marker in contract.get("all_of", [])]
                missing = [
                    marker for marker in markers if marker.casefold() not in prompt
                ]
                observation = {
                    "step": step_id,
                    "attempt": attempt,
                    "description": str(contract.get("description") or "ACI feedback"),
                    "required": markers,
                    "missing": missing,
                    "ok": not missing,
                }
                self.observations.append(observation)
                if missing:
                    self.missing_observations.append(observation)
                    return "echo FEEDBACK_CONTRACT_MISSING"
        return self.inner(messages, **kwargs)

    def _contract(self, step_id: int, attempt: int) -> Dict[str, Any]:
        values = self.feedback_contracts.get(str(step_id), [])
        if not isinstance(values, list):
            return {}
        for value in values:
            if isinstance(value, dict) and int(value.get("attempt", 0)) == attempt:
                return dict(value)
        return {}

    def get_last_usage(self) -> Dict[str, Any]:
        return self.inner.get_last_usage()

    def feedback_report(self) -> Dict[str, Any]:
        """Return observed explicit ACI feedback contracts for this model."""
        required = bool(self.feedback_contracts)
        return {
            "required": required,
            "ok": not required
            or (
                bool(self.observations)
                and not self.missing_observations
                and all(item.get("ok") is True for item in self.observations)
            ),
            "observations": list(self.observations),
            "missing": list(self.missing_observations),
            "negative_control_triggered": bool(self.missing_observations),
        }


_ENV_MISSING = object()


def _env_value(name: str) -> Any:
    return os.environ.get(name, _ENV_MISSING)


def _restore_env(name: str, value: Any) -> None:
    if value is _ENV_MISSING:
        os.environ.pop(name, None)
    else:
        os.environ[name] = str(value)


def _selection_values(
    value: Any, label: str, allow_none: bool
) -> Tuple[Optional[List[Any]], List[Dict[str, Any]]]:
    if value is None:
        if allow_none:
            return None, []
        return None, [
            {"code": "empty_selection", "message": f"{label} selection is empty"}
        ]
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        return None, [
            {
                "code": "invalid_selection",
                "message": f"{label} selection must be a sequence",
            }
        ]
    if not values:
        return values, [
            {"code": "empty_selection", "message": f"{label} selection is empty"}
        ]
    return values, []


def _normalise_names(
    values: Optional[List[Any]],
    label: str,
    known: List[str],
    allow_all: bool = False,
) -> Tuple[List[str], List[Dict[str, Any]]]:
    if values is None:
        return [], []
    errors: List[Dict[str, Any]] = []
    names: List[str] = []
    seen = set()
    for raw in values:
        if not isinstance(raw, str):
            errors.append(
                {
                    "code": "invalid_selection",
                    "message": f"{label} selection contains a non-string value",
                }
            )
            continue
        name = raw.strip()
        if not name:
            errors.append(
                {
                    "code": "empty_selection",
                    "message": f"{label} selection contains an empty value",
                }
            )
            continue
        if name in seen:
            errors.append(
                {
                    "code": "duplicate_selection",
                    "message": f"{label} selection contains duplicate {name!r}",
                }
            )
            continue
        seen.add(name)
        names.append(name)
    unknown = [
        name
        for name in names
        if name not in known and not (allow_all and name == "all")
    ]
    for name in unknown:
        errors.append(
            {
                "code": "unknown_selection",
                "message": f"unknown {label} {name!r}",
            }
        )
    if allow_all and "all" in names:
        if len(names) != 1:
            errors.append(
                {
                    "code": "invalid_selection",
                    "message": f"{label} 'all' cannot be combined with other values",
                }
            )
        else:
            return list(known), errors
    selected = [name for name in known if name in seen]
    return selected, errors


def _active_feature_coverage() -> Dict[str, Any]:
    features: Dict[str, Any] = {}
    errors: List[Dict[str, Any]] = []
    pre_round = ARMS.get("pre_round", {})
    for key in _ROUND_KEYS:
        candidates = [
            name
            for name, overrides in ARMS.items()
            if name not in {"baseline", "pre_round"}
            and overrides.get(key) is False
            and all(value is False for value in overrides.values())
        ]
        one_key_arm = candidates[0] if candidates else None
        pre_round_ok = pre_round.get(key) is False
        covered = one_key_arm is not None and pre_round_ok
        features[key] = {
            "one_key_arm": one_key_arm,
            "one_key_arm_covered": one_key_arm is not None,
            "pre_round": pre_round_ok,
            "covered": covered,
        }
        if not covered:
            errors.append(
                {
                    "code": "feature_coverage",
                    "message": f"round feature {key!r} lacks complete arm coverage",
                }
            )
    uncovered = ["self_critique"]
    uncovered_reasons = {
        "self_critique": "no real eval scenario and ablation arm are present"
    }
    pre_round_keys = {key: pre_round.get(key) is False for key in _ROUND_KEYS}
    return {
        "features": list(_ROUND_KEYS),
        "active_features": list(_ROUND_KEYS) + uncovered,
        "active_feature_count": len(_ROUND_KEYS) + len(uncovered),
        "covered_feature_count": sum(1 for row in features.values() if row["covered"]),
        "matrix_complete": not errors,
        "complete": not errors and not uncovered,
        "round_keys": features,
        "pre_round": {
            "arm": "pre_round",
            "keys": pre_round_keys,
            "ok": all(pre_round_keys.values()),
        },
        "uncovered": uncovered,
        "uncovered_reasons": uncovered_reasons,
        "ok": not errors,
        "errors": errors,
    }


def _validate_selections(
    arms: Any, task_slugs: Any, quick: bool
) -> Tuple[List[str], List[str], List[Dict[str, Any]], Dict[str, Any]]:
    known_arms = list(ARMS)
    known_tasks = list(eval_tasks.task_slugs())
    raw_arms, arm_shape_errors = _selection_values(arms, "arm", allow_none=False)
    raw_tasks, task_shape_errors = _selection_values(
        task_slugs, "task", allow_none=True
    )
    errors = arm_shape_errors + task_shape_errors
    selected_arms, arm_errors = _normalise_names(
        raw_arms, "arm", known_arms, allow_all=True
    )
    selected_tasks, task_errors = _normalise_names(raw_tasks, "task", known_tasks)
    errors.extend(arm_errors)
    errors.extend(task_errors)
    if raw_tasks is None:
        selected_tasks = list(known_tasks)
    if quick:
        fixture_slugs = {str(task["slug"]) for task in eval_tasks.FIXTURE_TASKS}
        if raw_tasks is not None:
            outside_quick = [
                slug for slug in selected_tasks if slug not in fixture_slugs
            ]
            if outside_quick:
                errors.append(
                    {
                        "code": "empty_selection",
                        "message": "quick mode selected no fixture tasks: "
                        + ", ".join(outside_quick),
                    }
                )
        selected_tasks = [slug for slug in selected_tasks if slug in fixture_slugs]
    if not selected_arms:
        if not arm_shape_errors and not arm_errors:
            errors.append({"code": "empty_selection", "message": "no arms selected"})
    elif "baseline" not in selected_arms:
        errors.append(
            {
                "code": "missing_baseline",
                "message": "baseline arm is mandatory",
            }
        )
    elif not any(arm != "baseline" for arm in selected_arms):
        errors.append(
            {
                "code": "baseline_only",
                "message": "at least one non-baseline arm is required",
            }
        )
    if not selected_tasks:
        errors.append({"code": "empty_selection", "message": "no tasks selected"})
    comparison_count = len(selected_tasks) * max(0, len(selected_arms) - 1)
    if comparison_count == 0:
        errors.append(
            {"code": "zero_comparisons", "message": "eval has zero comparisons"}
        )
    coverage = _active_feature_coverage()
    return selected_arms, selected_tasks, errors, coverage


def validate_selections(
    arms: Any, task_slugs: Any, quick: bool = False
) -> Tuple[List[str], List[str]]:
    """Validate raw eval selections before any task repository is built."""
    selected_arms, selected_tasks, errors, coverage = _validate_selections(
        arms, task_slugs, quick
    )
    errors.extend(
        error for error in coverage["errors"] if error["code"] == "feature_coverage"
    )
    if errors:
        raise ValueError("; ".join(str(error["message"]) for error in errors))
    return selected_arms, selected_tasks


def _new_run_dir(out_root: Path) -> Path:
    out_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for _ in range(20):
        candidate = out_root / f"{stamp}-{uuid.uuid4().hex}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError("could not allocate a unique eval run directory")


def _write_report(report: Dict[str, Any], run_dir: Path) -> str:
    run_dir.mkdir(parents=True, exist_ok=True)
    out = run_dir / "eval_report.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return str(out)


def _trace_events(log_dir: Path, task_id: str) -> List[Dict[str, Any]]:
    trace_path = log_dir / task_id / "trace.jsonl"
    events: List[Dict[str, Any]] = []
    if not trace_path.is_file():
        return events
    try:
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                events.append(value)
    except OSError:
        return []
    return events


def _value_matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(
            _value_matches(actual.get(key), value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        if isinstance(actual, list):
            return any(
                _value_matches(actual_item, value)
                for actual_item in actual
                for value in expected
            )
        return actual in expected
    if isinstance(actual, list) and isinstance(expected, str):
        return expected in actual
    return actual == expected


def _receipt_requirements(task_spec: Dict[str, Any], arm: str) -> Dict[str, Any]:
    requirements = task_spec.get("required_receipts")
    if requirements is None:
        requirements = task_spec.get("receipts")
    if not isinstance(requirements, dict):
        return {}
    selected = requirements.get(arm, requirements.get("*", {}))
    if selected is None:
        return {}
    if isinstance(selected, dict):
        return selected
    if isinstance(selected, list):
        return {str(index): value for index, value in enumerate(selected)}
    return {}


def _receipt_checks(
    log_dir: Path, task_id: str, requirements: Dict[str, Any]
) -> Dict[str, Any]:
    events = _trace_events(log_dir, task_id)
    checks: Dict[str, Any] = {}
    missing: List[str] = []
    for kind, expected in requirements.items():
        matches = []
        for event in events:
            if event.get("kind") != str(kind):
                continue
            data = event.get("data")
            if _value_matches(data, expected):
                matches.append(data)
        checks[str(kind)] = {
            "ok": bool(matches),
            "event_count": sum(1 for event in events if event.get("kind") == str(kind)),
        }
        if not matches:
            missing.append(str(kind))
    return {"ok": not missing, "required": checks, "missing": missing}


def _forbidden_receipt_requirements(
    task_spec: Dict[str, Any], arm: str
) -> Dict[str, Any]:
    requirements = task_spec.get("forbidden_receipts")
    if not isinstance(requirements, dict):
        return {}
    selected = requirements.get(arm, requirements.get("*", {}))
    if isinstance(selected, dict):
        return selected
    if isinstance(selected, list):
        return {str(index): value for index, value in enumerate(selected)}
    return {}


def _forbidden_receipt_checks(
    log_dir: Path, task_id: str, requirements: Dict[str, Any]
) -> Dict[str, Any]:
    events = _trace_events(log_dir, task_id)
    checks: Dict[str, Any] = {}
    present: List[str] = []
    for kind, forbidden in requirements.items():
        matches = [
            event.get("data")
            for event in events
            if event.get("kind") == str(kind)
            and _value_matches(event.get("data"), forbidden)
        ]
        checks[str(kind)] = {
            "ok": not matches,
            "event_count": sum(1 for event in events if event.get("kind") == str(kind)),
        }
        if matches:
            present.append(str(kind))
    return {"ok": not present, "checked": checks, "present": present}


def _evaluate_receipts(
    task_spec: Dict[str, Any], arm: str, log_dir: Path, task_id: str
) -> Dict[str, Any]:
    required = _receipt_checks(log_dir, task_id, _receipt_requirements(task_spec, arm))
    forbidden = _forbidden_receipt_checks(
        log_dir,
        task_id,
        _forbidden_receipt_requirements(task_spec, arm),
    )
    return {
        "ok": required["ok"] and forbidden["ok"],
        "required": required["required"],
        "missing": required["missing"],
        "forbidden": forbidden,
    }


def _failed_result(error: str) -> Dict[str, Any]:
    return {
        "status": "crash",
        "attempts": 0,
        "verified": False,
        "cost_usd": 0.0,
        "model_calls": 0,
        "wall_s": 0.0,
        "error": error,
        "integrity": {"ok": False},
        "receipts": {"ok": False, "required": {}, "missing": ["task"]},
        "ok": False,
    }


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root)
        if any(
            part in {".git", "__pycache__", ".pytest_cache"} for part in relative.parts
        ):
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _comparison_result_is_valid(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("status") in {None, "crash"}:
        return False
    integrity = result.get("integrity")
    receipts = result.get("receipts")
    if not isinstance(integrity, dict) or integrity.get("ok") is not True:
        return False
    if not isinstance(receipts, dict) or receipts.get("ok") is not True:
        return False
    return all(
        integrity.get(key) is True
        for key in ("trace_has_task_start", "trace_has_task_end", "trace_has_result")
    )


def _valid_comparison_count(report: Dict[str, Any]) -> int:
    arms = report.get("arms")
    tasks = report.get("task_slugs")
    if not isinstance(arms, dict) or not isinstance(tasks, list):
        return 0
    baseline = arms.get("baseline", {}).get("results", {})
    count = 0
    for slug in tasks:
        baseline_result = baseline.get(slug)
        if (
            not isinstance(baseline_result, dict)
            or baseline_result.get("ok") is not True
        ):
            continue
        for arm, data in arms.items():
            if arm == "baseline" or not isinstance(data, dict):
                continue
            result = data.get("results", {}).get(slug)
            if _comparison_result_is_valid(result):
                count += 1
    return count


def _result_is_ok(result: Dict[str, Any], expects_retry: bool = False) -> bool:
    try:
        attempts = int(result.get("attempts", 0))
    except (TypeError, ValueError):
        return False
    return (
        result.get("status") == "success"
        and result.get("verified") is True
        and result.get("integrity", {}).get("ok") is True
        and result.get("receipts", {}).get("ok") is True
        and (attempts >= 2 if expects_retry else True)
    )


def _run_one(
    task_spec: Dict[str, Any],
    arm_overrides: Dict[str, Any],
    log_root: Path,
    decision_db: Optional[Path] = None,
    arm_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one eval task through the real loop and return its score."""
    from harness.deps import reset_overrides
    from shared import tracing as _tracing

    task_id = task_spec["slug"]
    task_log_root = Path(log_root)
    task_trace_dir = task_log_root / task_id
    trace_root_env = _env_value("NEO_TRACE_DIR")
    decisions_db_env = _env_value("HARNESS_DECISIONS_DB")
    started = time.time()
    model: Optional[_FeedbackAwareScriptedModel] = None
    try:
        cfg = dict(_BASE)
        cfg.update(arm_overrides)
        task_cfg = task_spec.get("config") or {}
        for key, value in task_cfg.items():
            cfg.setdefault(key, value)
        cfg["target_test"] = task_spec.get("target")
        task = Task(
            task_id=task_id,
            repo_path=task_spec["repo"],
            issue_text=task_spec["issue"],
            config=cfg,
        )
        reset_overrides()
        os.environ["NEO_TRACE_DIR"] = str(task_trace_dir)
        _tracing._reset_cache()
        raw_scripts = task_spec["script"]["scripts"]
        scripts: Dict[int, list] = {}
        for sid, value in raw_scripts.items():
            scripts[int(sid)] = (
                [value] if value and isinstance(value[0], str) else list(value)
            )
        model = _FeedbackAwareScriptedModel(
            plan=task_spec["script"]["plan"],
            scripts=scripts,
            feedback_contracts=task_spec.get("feedback_contracts") or {},
        )
        set_call_model(model)
        if decision_db is not None:
            os.environ["HARNESS_DECISIONS_DB"] = str(decision_db)
        from harness.core import run_task

        result = run_task(task, log_root=task_log_root)
        outcome = {
            "status": result.status,
            "attempts": result.attempts,
            "verified": bool(
                result.verification is not None
                and result.verification.target_test_passed
                and result.verification.regression_passed
                and not result.verification.flaky
            ),
            "cost_usd": result.cost_usd,
            "model_calls": len(result.model_calls),
            "wall_s": round(time.time() - started, 1),
            "error": None,
        }
    except Exception as exc:
        outcome = {
            "status": "crash",
            "attempts": 0,
            "verified": False,
            "cost_usd": 0.0,
            "model_calls": 0,
            "wall_s": round(time.time() - started, 1),
            "error": f"{exc!r}\n{traceback.format_exc()[-2000:]}",
        }
    finally:
        _restore_env("HARNESS_DECISIONS_DB", decisions_db_env)
        _restore_env("NEO_TRACE_DIR", trace_root_env)
        _tracing._reset_cache()
        try:
            reset_overrides()
        except Exception:
            pass

    feedback = (
        model.feedback_report()
        if model is not None
        else {
            "required": bool(task_spec.get("feedback_contracts")),
            "ok": not task_spec.get("feedback_contracts"),
            "observations": [],
            "missing": [],
            "negative_control_triggered": False,
        }
    )
    outcome["aci_feedback"] = feedback
    outcome["integrity"] = _integrity_checks(task_log_root, task_id)
    receipt_arm = arm_name
    if receipt_arm is None:
        candidates = [
            name for name, overrides in ARMS.items() if overrides == arm_overrides
        ]
        receipt_arm = candidates[0] if len(candidates) == 1 else ""
    outcome["receipts"] = _evaluate_receipts(
        task_spec, receipt_arm, task_log_root, task_id
    )
    expects_retry = bool(task_spec.get("expects_retry"))
    outcome["ok"] = _result_is_ok(outcome, expects_retry) and feedback["ok"] is True
    return outcome


def _integrity_checks(log_dir: Path, task_id: str) -> Dict[str, Any]:
    """Loop-integrity score from the task's own trace.jsonl.

    These are the machinery checks a prompt regression breaks FIRST:
    an incomplete trace (loop died mid-flight), an unparseable planner
    reply, a nudge loop (model replies stopped extracting commands), or
    a success with no recorded files. Assumes ``log_dir`` is the arm root
    and ``run_task`` created ``log_dir/{task_id}`` beneath it.
    """
    events = _trace_events(log_dir, task_id)
    kinds = [str(e.get("kind", "")) for e in events]

    has = lambda k: k in kinds  # noqa: E731
    no_cmd = sum(
        1
        for e in events
        if e.get("kind") == "tool_result"
        and "no runnable bash command" in str(e.get("data", ""))
    )
    state: Dict[str, Any] = {}
    state_path = log_dir / task_id / "state.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}

    checks: Dict[str, Any] = {
        "trace_has_task_start": has("task_start"),
        "trace_has_task_end": has("task_end"),
        "trace_has_result": has("result"),
        "no_plan_parse_error": not has("plan_parse_error"),
        "no_nudge_loop": no_cmd <= 3,
        "files_touched_recorded": bool(state.get("files_touched")),
    }
    checks["ok"] = all(checks.values())
    return checks


# ---------------------------------------------------------------------------
# Runner + report
# ---------------------------------------------------------------------------


def _seed_decision_store(db_path: Path, repo_path: str) -> None:
    """Seed an ISOLATED decision store for the memory arms.

    One decision genuinely relevant to the eval repo's bug (so memory-
    informed planning has something real to find) plus two noise rows
    from a different repo (the planner must not be distracted). Assumes
    db_path's parents are writable; overwrites any prior file so runs
    are deterministic.
    """
    from memory.decision_store import DecisionStore

    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    store = DecisionStore(str(db_path))
    store.record(
        "In this repo the mean() helper divides by len-1; the correct "
        "denominator is len(values) — fixed once already, guard against "
        "regression.",
        category="bug",
        source="eval-seed",
        repo_path=repo_path,
        task_id="eval-seed-1",
    )
    store.record(
        "The cart package uses a shared mutable default report list — "
        "always pass an explicit list.",
        category="bug",
        source="eval-seed",
        repo_path=repo_path,
        task_id="eval-seed-2",
    )
    store.record(
        "Unrelated project convention: tabs, not spaces.",
        category="style",
        source="eval-seed",
        repo_path="C:/other/project",
        task_id="eval-noise-1",
    )
    store.close()


def run_eval(
    arms: List[str],
    task_slugs: Optional[List[str]],
    out_root: Path,
    quick: bool = False,
    json_mode: bool = False,
) -> Dict[str, Any]:
    """Run a validated eval matrix and return its persisted report.

    Raw selections are checked before ``all_tasks`` can create a repository.
    The caller owns Docker setup; this function restores process environment
    variables even when validation, construction, or execution fails.
    """
    prior_trace = _env_value("NEO_TRACE_DIR")
    prior_decisions_db = _env_value("HARNESS_DECISIONS_DB")
    from shared import tracing as _tracing

    run_dir: Optional[Path] = None
    report: Optional[Dict[str, Any]] = None

    def emit(message: str) -> None:
        print(message, file=sys.stderr if json_mode else sys.stdout)

    def finish(current: Dict[str, Any]) -> Dict[str, Any]:
        for error in current.get("feature_coverage", {}).get("errors", []):
            if error not in current.setdefault("errors", []):
                current["errors"].append(error)
        if current.get("arms") and current.get("valid_comparison_count") == 0:
            current.setdefault("errors", []).append(
                {
                    "code": "zero_valid_comparisons",
                    "message": "no trustworthy baseline/comparison pair was observed",
                }
            )
        if current.get("errors"):
            current["verdict"] = "ERROR"
        elif current.get("regressions"):
            current["verdict"] = "REGRESSIONS"
        else:
            current["verdict"] = "CLEAN"
        current["_report_path"] = _write_report(current, run_dir)
        emit(
            f"\nverdict: {current['verdict']}  "
            f"({len(current.get('regressions', []))} regression(s) vs baseline)"
        )
        emit(f"report: {current['_report_path']}")
        return current

    try:
        selected_arms, selected_tasks, errors, coverage = _validate_selections(
            arms, task_slugs, quick
        )
        run_dir = _new_run_dir(Path(out_root))
        report = {
            "ts": time.strftime("%Y%m%d-%H%M%S"),
            "run_id": run_dir.name,
            "n_tasks": len(selected_tasks),
            "task_slugs": selected_tasks,
            "selected_arms": selected_arms,
            "comparisons": len(selected_tasks) * max(0, len(selected_arms) - 1),
            "comparison_count": len(selected_tasks) * max(0, len(selected_arms) - 1),
            "valid_comparison_count": 0,
            "feature_coverage": coverage,
            "active_feature_coverage": coverage,
            "arms": {},
            "regressions": [],
            "errors": list(errors),
        }
        if errors:
            return finish(report)

        all_tasks = eval_tasks.all_tasks(run_dir)
        by_slug: Dict[str, Dict[str, Any]] = {}
        if not isinstance(all_tasks, list):
            report["errors"].append(
                {
                    "code": "task_set_invalid",
                    "message": "task builder did not return a list",
                }
            )
        else:
            for spec in all_tasks:
                if (
                    not isinstance(spec, dict)
                    or not isinstance(spec.get("slug"), str)
                    or not spec.get("slug")
                ):
                    report["errors"].append(
                        {
                            "code": "task_set_invalid",
                            "message": "task builder returned an invalid task",
                        }
                    )
                    continue
                slug = str(spec["slug"])
                if slug in by_slug:
                    report["errors"].append(
                        {
                            "code": "duplicate_task",
                            "message": f"task builder returned duplicate {slug!r}",
                        }
                    )
                by_slug[slug] = spec
        missing_tasks = [slug for slug in selected_tasks if slug not in by_slug]
        for slug in missing_tasks:
            report["errors"].append(
                {
                    "code": "missing_task",
                    "message": f"selected task {slug!r} is missing from the task set",
                    "task": slug,
                }
            )
        selected_specs = [by_slug[slug] for slug in selected_tasks if slug in by_slug]
        for spec in selected_specs:
            if not isinstance(spec.get("target"), str) or not spec.get("target"):
                report["errors"].append(
                    {
                        "code": "missing_target",
                        "message": f"task {spec['slug']!r} has no declared target",
                        "task": spec["slug"],
                    }
                )
        if report["errors"]:
            return finish(report)

        source_digests_before = {
            spec["slug"]: _tree_digest(Path(spec["repo"])) for spec in selected_specs
        }
        report["benchmark_isolation"] = {
            "unique_run_root": True,
            "source_repositories": sorted(source_digests_before),
            "source_repositories_unchanged": True,
            "one_action_per_scripted_reply": True,
        }
        os.environ["NEO_TRACE_DIR"] = str(run_dir)
        _tracing._reset_cache()
        db_path = run_dir / "memory-seed" / "decisions.db"
        seeded = False
        memory_repo = next(
            (
                spec["repo"]
                for spec in selected_specs
                if Path(spec["repo"]).name == "bug02_mean"
            ),
            None,
        )
        if memory_repo:
            _seed_decision_store(db_path, memory_repo)
            seeded = True

        for arm in selected_arms:
            arm_overrides = dict(ARMS[arm])
            effective = dict(_BASE)
            effective.update(arm_overrides)
            arm_dir = run_dir / arm
            arm_dir.mkdir(parents=True, exist_ok=True)
            emit(f"-- arm: {arm} ({len(selected_specs)} tasks)")
            arm_res: Dict[str, Any] = {}
            for spec in selected_specs:
                slug = spec["slug"]
                use_db = (
                    db_path
                    if (seeded and effective.get("plan_with_memory", True))
                    else None
                )
                try:
                    result = _run_one(
                        spec,
                        arm_overrides,
                        arm_dir,
                        decision_db=use_db,
                        arm_name=arm,
                    )
                except Exception as exc:
                    result = _failed_result(repr(exc))
                if not isinstance(result, dict):
                    result = _failed_result("task runner returned no receipt")
                requirements = _receipt_requirements(spec, arm)
                forbidden_requirements = _forbidden_receipt_requirements(spec, arm)
                observed_receipts = _evaluate_receipts(spec, arm, arm_dir, slug)
                observed_integrity = _integrity_checks(arm_dir, slug)
                missing_shape = any(
                    key not in result
                    for key in ("ok", "status", "verified", "attempts")
                )
                result["receipts"] = observed_receipts
                result["integrity"] = observed_integrity
                feedback = result.get("aci_feedback")
                if not isinstance(feedback, dict):
                    feedback = {
                        "required": bool(spec.get("feedback_contracts")),
                        "ok": not spec.get("feedback_contracts"),
                        "observations": [],
                        "missing": [],
                        "negative_control_triggered": False,
                    }
                    result["aci_feedback"] = feedback
                if spec.get("feedback_contracts") and feedback.get("ok") is not True:
                    report["errors"].append(
                        {
                            "code": "aci_feedback_missing",
                            "message": f"explicit ACI feedback missing for {arm}/{slug}",
                            "arm": arm,
                            "task": slug,
                        }
                    )
                result["ok"] = (
                    _result_is_ok(result, bool(spec.get("expects_retry")))
                    and feedback.get("ok") is True
                )
                if missing_shape or not all(
                    observed_integrity.get(key) is True
                    for key in (
                        "trace_has_task_start",
                        "trace_has_task_end",
                        "trace_has_result",
                    )
                ):
                    report["errors"].append(
                        {
                            "code": "missing_task_receipt",
                            "message": f"missing task receipt for {arm}/{slug}",
                            "arm": arm,
                            "task": slug,
                        }
                    )
                if (requirements or forbidden_requirements) and not observed_receipts[
                    "ok"
                ]:
                    report["errors"].append(
                        {
                            "code": "missing_receipt",
                            "message": f"required receipt missing for {arm}/{slug}",
                            "arm": arm,
                            "task": slug,
                        }
                    )
                arm_res[slug] = result
                mark = "ok" if result["ok"] else f"FAIL({result.get('status', '?')})"
                error_text = result.get("error")
                extra = ""
                if not result["ok"]:
                    if error_text:
                        extra = f"  err={str(error_text).splitlines()[0][:80]}"
                    else:
                        extra = f"  integrity={json.dumps(result.get('integrity', {}))[:100]}"
                emit(
                    f"   {slug:<22} {mark:<18} "
                    f"attempts={result.get('attempts', 0)} "
                    f"wall={result.get('wall_s', 0)}s{extra}"
                )
            ok = sum(1 for result in arm_res.values() if result.get("ok") is True)
            report["arms"][arm] = {
                "results": arm_res,
                "n_ok": ok,
                "n_tasks": len(selected_specs),
                "success_rate": round(ok / max(1, len(selected_specs)), 3),
            }
            emit(f"   arm summary: {ok}/{len(selected_specs)} ok")

        for arm in selected_arms:
            results = report["arms"].get(arm, {}).get("results", {})
            for slug in selected_tasks:
                if slug not in results:
                    report["errors"].append(
                        {
                            "code": "missing_task_receipt",
                            "message": f"missing result receipt for {arm}/{slug}",
                            "arm": arm,
                            "task": slug,
                        }
                    )
        base = report["arms"].get("baseline", {}).get("results", {})
        for slug in selected_tasks:
            baseline_result = base.get(slug)
            if baseline_result is None:
                report["errors"].append(
                    {
                        "code": "missing_baseline_task",
                        "message": f"baseline result missing for {slug!r}",
                        "task": slug,
                    }
                )
            elif baseline_result.get("ok") is not True:
                report["errors"].append(
                    {
                        "code": "baseline_failure",
                        "message": f"baseline task {slug!r} is not ok",
                        "task": slug,
                    }
                )

        for arm, data in report["arms"].items():
            if arm == "baseline":
                continue
            for slug, result in data.get("results", {}).items():
                baseline_result = base.get(slug)
                if baseline_result is None:
                    continue
                if baseline_result.get("ok") and not result.get("ok"):
                    report["regressions"].append(
                        {
                            "arm": arm,
                            "task": slug,
                            "baseline_status": baseline_result.get("status"),
                            "arm_status": result.get("status"),
                            "detail": json.dumps(result.get("integrity", {})),
                        }
                    )
                elif not baseline_result.get("ok") and result.get("ok"):
                    report.setdefault("improvements", []).append(
                        {
                            "arm": arm,
                            "task": slug,
                            "baseline_status": baseline_result.get("status"),
                            "arm_status": result.get("status"),
                        }
                    )
        source_digests_after = {
            spec["slug"]: _tree_digest(Path(spec["repo"])) for spec in selected_specs
        }
        changed_sources = sorted(
            slug
            for slug, digest in source_digests_before.items()
            if source_digests_after.get(slug) != digest
        )
        report["benchmark_isolation"][
            "source_repositories_unchanged"
        ] = not changed_sources
        report["benchmark_isolation"]["changed_source_repositories"] = changed_sources
        if changed_sources:
            report["errors"].append(
                {
                    "code": "source_repository_mutated",
                    "message": "benchmark source repositories changed during the run",
                    "tasks": changed_sources,
                }
            )
        report["valid_comparison_count"] = _valid_comparison_count(report)
        report["aci_feedback"] = {
            "required_task_slugs": [
                spec["slug"]
                for spec in selected_specs
                if spec.get("feedback_contracts")
            ],
            "observed": sum(
                1
                for data in report["arms"].values()
                for result in data.get("results", {}).values()
                if isinstance(result.get("aci_feedback"), dict)
                and result["aci_feedback"].get("ok") is True
            ),
            "complete": all(
                result.get("aci_feedback", {}).get("ok") is True
                for spec in selected_specs
                if spec.get("feedback_contracts")
                for data in report["arms"].values()
                for result in [data.get("results", {}).get(spec["slug"])]
                if isinstance(result, dict)
            ),
        }
        return finish(report)
    except Exception as exc:
        if report is None:
            try:
                run_dir = _new_run_dir(Path(out_root))
            except Exception:
                run_dir = Path(out_root)
            report = {
                "ts": time.strftime("%Y%m%d-%H%M%S"),
                "run_id": run_dir.name,
                "task_slugs": [],
                "selected_arms": [],
                "feature_coverage": _active_feature_coverage(),
                "active_feature_coverage": _active_feature_coverage(),
                "arms": {},
                "regressions": [],
                "errors": [],
            }
        report.setdefault("errors", []).append(
            {"code": "runner_error", "message": repr(exc)}
        )
        report["verdict"] = "ERROR"
        try:
            report["_report_path"] = _write_report(report, run_dir)
        except Exception:
            pass
        if not json_mode:
            emit(f"eval error: {exc}")
        return report
    finally:
        _restore_env("HARNESS_DECISIONS_DB", prior_decisions_db)
        _restore_env("NEO_TRACE_DIR", prior_trace)
        _tracing._reset_cache()


def _load_daily_driver():
    from evals import daily_driver

    return daily_driver


def _run_prompt_check(out_root: Path, json_mode: bool) -> int:
    try:
        run_dir = _new_run_dir(out_root)
        results = eval_tasks.check_set(run_dir / "repos")
        coverage = _active_feature_coverage()
        failures = [item for item in results if item.get("ok") is not True]
        report = {
            "suite": "prompt-regression-check",
            "verdict": "CLEAN" if not failures and coverage["ok"] else "ERROR",
            "results": results,
            "pass_count": len(results) - len(failures),
            "fail_count": len(failures),
            "skip_count": 0,
            "feature_coverage": coverage,
            "errors": [
                *[
                    {"code": "prompt_task_check", "message": str(item)}
                    for item in failures
                ],
                *coverage["errors"],
            ],
        }
        report["_report_path"] = _write_report(report, run_dir)
    except Exception as exc:
        report = {
            "suite": "prompt-regression-check",
            "verdict": "ERROR",
            "errors": [{"code": "check_error", "message": repr(exc)}],
        }
    if json_mode:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(
            f"prompt task set: {report.get('pass_count', 0)}/"
            f"{report.get('pass_count', 0) + report.get('fail_count', 0)} ok"
        )
        for error in report.get("errors", []):
            print(f"eval error: {error.get('message', error)}")
        print(f"verdict: {report['verdict']}")
        print(f"report: {report.get('_report_path', '')}")
    return 0 if report["verdict"] == "CLEAN" else 2


def _run_combined_check(
    out_root: Path,
    json_mode: bool,
    manual_evidence_source: Optional[str] = None,
) -> int:
    try:
        prompt_results = eval_tasks.check_set(out_root / "c")
        daily_driver = _load_daily_driver()
        daily_report = daily_driver.run_daily_suite(
            out_root / "d",
            quick=True,
            include_docker=False,
            json_mode=json_mode,
            manual_evidence_source=manual_evidence_source,
        )
    except Exception as exc:
        report = {
            "suite": "combined-check",
            "verdict": "ERROR",
            "errors": [{"code": "check_error", "message": repr(exc)}],
        }
        if json_mode:
            print(json.dumps(report, indent=2, default=str))
        else:
            print(f"eval check error: {exc}")
        return 2
    prompt_bad = [item for item in prompt_results if item.get("ok") is not True]
    daily_bad = [
        item for item in daily_report.get("results", []) if item.get("ok") is not True
    ]
    feature_report = daily_report.get("feature_evidence", {})
    feature_results = (
        feature_report.get("results", []) if isinstance(feature_report, dict) else []
    )
    feature_bad = [item for item in feature_results if item.get("ok") is not True]
    feature_lane_required = (
        isinstance(feature_report, dict) and "status" in feature_report
    )
    feature_lane_ok = bool(
        feature_lane_required
        and feature_report.get("status") == "complete"
        and bool(feature_results)
        and not feature_bad
    )
    coverage = daily_report.get("prompt_feature_coverage", {})
    summary = daily_report.get("summary", {})
    readiness_value = summary.get("ready") if isinstance(summary, dict) else None
    readiness_ok = readiness_value is True
    readiness_blockers = (
        [
            {"code": "readiness_gate", "message": key}
            for key, value in summary.get("readiness", {}).items()
            if value is not True
        ]
        if isinstance(summary, dict)
        else []
    )
    report = {
        "suite": "combined-check",
        "verdict": (
            "CLEAN"
            if daily_report.get("matrix_check", {}).get("ok")
            and not prompt_bad
            and not daily_bad
            and not feature_bad
            and feature_lane_ok
            and coverage.get("complete")
            and readiness_ok
            else "ERROR"
        ),
        "prompt_regression": {
            "results": prompt_results,
            "pass_count": len(prompt_results) - len(prompt_bad),
            "fail_count": len(prompt_bad),
            "skip_count": 0,
            "errors": prompt_bad,
        },
        "daily_driver": daily_report,
        "feature_evidence": feature_report,
        "readiness": summary.get("readiness", {}) if isinstance(summary, dict) else {},
        "errors": [
            *[
                {"code": "prompt_task_check", "message": str(item)}
                for item in prompt_bad
            ],
            *[
                {"code": "daily_driver_check", "message": str(item)}
                for item in daily_bad
            ],
            *[
                {"code": "feature_evidence_check", "message": str(item)}
                for item in feature_bad
            ],
            *coverage.get("errors", []),
            *readiness_blockers,
        ],
    }
    daily_path = Path(
        str(
            daily_report.get("summary", {}).get(
                "report_path", out_root / "d" / "daily_driver_report.json"
            )
        )
    )
    report_path = daily_path.with_name("daily_driver_check_report.json")
    report["report_path"] = str(report_path)
    report_path.write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
    )
    if json_mode:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(
            f"prompt task set: {len(prompt_results) - len(prompt_bad)}/{len(prompt_results)} ok"
        )
        summary = daily_report.get("summary", {})
        print(
            f"daily-driver quick: {summary.get('pass_count', 0)}/"
            f"{len(daily_report.get('results', []))} arms ok"
        )
        print(f"verdict: {report['verdict']}")
        print(f"report: {report_path}")
    return 0 if report["verdict"] == "CLEAN" else 2


def _run_trust_ladder(out_root: Path, json_mode: bool) -> int:
    """Run the Daily Trust Ladder suite and publish its report.

    Wraps :func:`evals.trust_ladder.ladder_report` so the ladder's report lands
    beside the other suites' reports under ``logs/evals/<run-id>/`` and so the
    JSON contract matches: stdout is JSON only in ``--json`` mode, and the exit
    code is 2 when a MEASURED rung is red.

    A ``blocked`` or ``not_implemented`` row does not make the exit code 2.
    Those are honest reports, and the decision of whether a blocked guarantee
    may ship belongs to the reader of the table, not to a process exit.
    """
    from evals import trust_ladder

    report = trust_ladder.ladder_report(REPO_ROOT)
    run_dir = _new_run_dir(Path(out_root))
    report_path = run_dir / "trust_ladder_report.json"
    report["report_path"] = str(report_path)
    report_path.write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
    )
    if json_mode:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(trust_ladder.render(report))
        counts = report["counts"]
        print(
            "counts: "
            + ", ".join(f"{k}={v}" for k, v in counts.items())
            + f"  (verdict {report['verdict']})"
        )
        print(f"report: {report_path}")
    return 2 if report["verdict"] == trust_ladder.LADDER_MEASURED else 0


def main(argv: Optional[List[str]] = None) -> int:
    from shared.brand import apply_legacy_env

    apply_legacy_env()
    parser = argparse.ArgumentParser(
        prog="python -m evals.run",
        description="Prompt-regression matrix with explicit daily-driver and combined suites.",
    )
    parser.add_argument(
        "--suite",
        choices=(
            "auto",
            "combined",
            "daily-driver",
            "prompt-regression",
            "trust-ladder",
        ),
        default="prompt-regression",
        help="prompt-regression is the default; auto is a compatibility alias",
    )
    parser.add_argument(
        "--arms", default=None, help="comma list or 'all' for the selected suite"
    )
    parser.add_argument("--tasks", default=None, help="prompt-regression task slugs")
    parser.add_argument(
        "--daily-cases", default=None, help="comma list of daily-driver case slugs"
    )
    parser.add_argument(
        "--manual-evidence",
        default=None,
        help="explicit JSON source for sampled real-development manual-repair evidence",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="prompt-regression: fixture tasks only; daily-driver: quick matrix",
    )
    parser.add_argument(
        "--json", action="store_true", help="machine-readable output only"
    )
    parser.add_argument(
        "--out-root",
        default=None,
        help="parent directory for unique eval runs (default logs/evals)",
    )
    parser.add_argument(
        "--check", action="store_true", help="host self-check for the selected suite"
    )
    parser.add_argument(
        "--no-docker",
        action="store_true",
        help="omit the real Docker canary from the daily-driver suite",
    )
    parser.add_argument(
        "--live-provider",
        action="store_true",
        help="select the real-provider daily-driver lane",
    )
    parser.add_argument(
        "--provider-model",
        default=None,
        help="explicit model for the real-provider lane",
    )
    parser.add_argument(
        "--provider", default=None, help="explicit provider for the real-provider lane"
    )
    parser.add_argument(
        "--provider-base-url", default=None, help="explicit provider base URL"
    )
    parser.add_argument(
        "--provider-key-env",
        default="NEO_API_KEY",
        help="environment variable containing the provider credential; values are never logged",
    )
    args = parser.parse_args(argv)
    out_root = Path(args.out_root) if args.out_root else (REPO_ROOT / "logs" / "evals")
    suite = "prompt-regression" if args.suite == "auto" else args.suite
    if suite == "combined":
        if args.no_docker:
            parser.error("--no-docker requires --suite daily-driver")
        if (
            args.live_provider
            or any(
                value is not None
                for value in (
                    args.provider_model,
                    args.provider,
                    args.provider_base_url,
                )
            )
            or args.provider_key_env != "NEO_API_KEY"
        ):
            parser.error("real-provider options require --suite daily-driver")
        return _run_combined_check(out_root, args.json, args.manual_evidence)

    if suite == "trust-ladder":
        # The Daily Trust Ladder (T5.W1.1). Registered as a SUITE rather than a
        # new top-level command so `evals.run` stays the single evaluation
        # entry point, and so `evals/gates/P0.py`'s
        # `--suite trust-ladder` rung stops being `not_implemented`.
        #
        # Its own options are validated here rather than forwarded blindly, so
        # a suite-specific flag used with the wrong suite is a usage error and
        # not a silently ignored argument -- the same rule the other two
        # suites follow.
        if args.daily_cases is not None:
            parser.error("--daily-cases requires --suite daily-driver")
        if args.manual_evidence is not None:
            parser.error("--manual-evidence requires a daily-driver suite")
        if args.no_docker:
            parser.error("--no-docker requires --suite daily-driver")
        if (
            args.live_provider
            or any(
                value is not None
                for value in (
                    args.provider_model,
                    args.provider,
                    args.provider_base_url,
                )
            )
            or args.provider_key_env != "NEO_API_KEY"
        ):
            parser.error("real-provider options require --suite daily-driver")
        if args.arms is not None or args.tasks is not None:
            parser.error("--arms/--tasks require --suite prompt-regression")
        return _run_trust_ladder(out_root, args.json)

    if suite == "prompt-regression":
        if args.daily_cases is not None:
            parser.error("--daily-cases requires --suite daily-driver")
        if args.manual_evidence is not None:
            parser.error("--manual-evidence requires a daily-driver suite")
        if args.no_docker:
            parser.error("--no-docker requires --suite daily-driver")
        if (
            args.live_provider
            or any(
                value is not None
                for value in (
                    args.provider_model,
                    args.provider,
                    args.provider_base_url,
                )
            )
            or args.provider_key_env != "NEO_API_KEY"
        ):
            parser.error("real-provider options require --suite daily-driver")
        if args.check:
            return _run_prompt_check(out_root, args.json)
        arms = list(ARMS) if args.arms in (None, "all") else args.arms.split(",")
        slugs = args.tasks.split(",") if args.tasks is not None else None
        try:
            report = run_eval(
                arms,
                slugs,
                out_root,
                quick=args.quick,
                json_mode=args.json,
            )
        except Exception as exc:
            report = {
                "verdict": "ERROR",
                "errors": [{"code": "runner_error", "message": repr(exc)}],
                "arms": {},
                "task_slugs": [],
                "regressions": [],
            }
        if args.json:
            print(json.dumps(report, indent=2, default=str))
        else:
            if report.get("arms") and report.get("task_slugs"):
                _print_matrix(report)
            for error in report.get("errors", []):
                print(f"eval error: {error.get('message', error)}")
        return 0 if report.get("verdict") == "CLEAN" else 2

    if args.tasks is not None:
        parser.error("--tasks requires --suite prompt-regression")
    daily_driver = _load_daily_driver()
    if args.check:
        try:
            report = daily_driver.run_daily_suite(
                out_root,
                quick=True,
                include_docker=False,
                json_mode=args.json,
                manual_evidence_source=args.manual_evidence,
                include_live_provider=args.live_provider,
                live_provider_config={
                    "model": args.provider_model,
                    "provider": args.provider,
                    "base_url": args.provider_base_url,
                    "key_env": args.provider_key_env,
                },
            )
        except Exception as exc:
            report = {
                "verdict": "ERROR",
                "errors": [{"code": "daily_driver_error", "message": repr(exc)}],
                "results": [],
            }
    else:
        daily_arms = list(daily_driver.ARMS)
        if args.arms is not None and args.arms != "all":
            daily_arms = args.arms.split(",")
        daily_cases = (
            args.daily_cases.split(",") if args.daily_cases is not None else None
        )
        try:
            report = daily_driver.run_daily_suite(
                out_root,
                case_slugs=daily_cases,
                arms=daily_arms,
                quick=args.quick,
                include_docker=not args.no_docker,
                json_mode=args.json,
                manual_evidence_source=args.manual_evidence,
                include_live_provider=args.live_provider,
                live_provider_config={
                    "model": args.provider_model,
                    "provider": args.provider,
                    "base_url": args.provider_base_url,
                    "key_env": args.provider_key_env,
                },
            )
        except Exception as exc:
            report = {
                "verdict": "ERROR",
                "errors": [{"code": "daily_driver_error", "message": repr(exc)}],
                "results": [],
            }
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        summary = report.get("summary", {})
        print(
            f"daily-driver: {summary.get('pass_count', 0)}/"
            f"{summary.get('pass_count', 0) + summary.get('fail_count', 0)} arms ok; "
            f"baseline completion {summary.get('safe_completion_rate', 0):.0%}"
        )
        for result in report.get("results", []):
            if result.get("ok") is not True:
                print(
                    f"  FAIL {result.get('scenario_id')} {result.get('case')} "
                    f"[{result.get('arm')}]: {result.get('status')} "
                    f"{result.get('reproducer', '')}"
                )
        print(f"verdict: {report.get('verdict')}")
        print(f"report: {summary.get('report_path', '')}")
    return 0 if report.get("verdict") == "CLEAN" else 2


def _print_matrix(report: Dict[str, Any]) -> None:
    slugs = report["task_slugs"]
    arms = list(report["arms"])
    print("\n== eval matrix (ok = success + verified + integrity) ==")
    header = f"{'task':<24}" + "".join(f"{a:<16}" for a in arms)
    print(header)
    print("-" * len(header))
    for slug in slugs:
        row = f"{slug:<24}"
        for arm in arms:
            r = report["arms"][arm]["results"].get(slug, {})
            mark = "ok" if r.get("ok") else (r.get("status") or "?")
            row += f"{mark:<16}"
        print(row)
    print()
    for arm in arms:
        d = report["arms"][arm]
        print(f"  {arm:<16} {d['n_ok']}/{d['n_tasks']} ok ({d['success_rate']:.0%})")


if __name__ == "__main__":
    raise SystemExit(main())
