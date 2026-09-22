"""The mode router (Modes round, Task B) — one input, four handlers.

Classify (Task A: harness.intent) then dispatch:

- fix      -> harness.core.run_task — the existing, UNCHANGED fix-engine
- question -> harness.qa_mode.run_question — read-only Q&A
- build    -> harness.build_mode.run_build — test-authored completion
- research -> harness.research_mode.run_research — FETCH-assisted synthesis
- convo/ambiguous -> answered by the caller (the session), never a task

The router is a thin, honest dispatcher: it owns NO policy beyond mode
selection (the intent classifier decides; the handlers own everything
else). It records a ``route`` trace event when a trace is available and
resolves handlers through late imports so a broken optional mode never
breaks the import graph.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional

from harness.intent import Intent, classify_input

__all__ = ["ModeResult", "route", "route_kind"]


class ModeResult(dict):
    """Uniform outcome across the four handlers (a dict with helpers).

    Keys: mode, status ("success"|"failed"|"error"|"already_exists"),
    answer (qa/research), result (fix/build TaskResult), task_id,
    trace_path, plus mode-specific extras. Handlers construct plain
    dicts; route() wraps them so callers have one shape.
    """

    @property
    def ok(self) -> bool:
        return bool(self.get("status") == "success")


def route_kind(text: str, config: Optional[Dict[str, Any]] = None) -> Intent:
    """Classify input for routing (Task A). Thin passthrough so callers
    (and tests) have ONE import site for the routing decision."""
    return classify_input(text, config)


def route(
    text: str,
    repo_path: str,
    config: Optional[Dict[str, Any]] = None,
    log_root: Optional[Path] = None,
    trace: Optional[Any] = None,
    handlers: Optional[Dict[str, Callable[..., Dict[str, Any]]]] = None,
) -> ModeResult:
    """Classify + dispatch one user input to its mode handler.

    Returns a ModeResult (see class docstring) for the four work modes.
    convo/ambiguous come back as ModeResult(kind=..., status="reply",
    answer=intent.reply) for the SESSION to print — routing never
    launches anything for them. Assumes repo_path is a readable directory
    and config is the session/task config dict. `handlers` overrides the
    dispatch table (tests inject fakes without monkeypatching modules).
    """
    it = classify_input(text, config, trace)
    if trace is not None:
        try:
            trace.log("route", {"kind": it.kind, "reason": it.reason})
        except Exception:
            pass

    if it.kind in ("convo", "ambiguous"):
        return ModeResult(
            {
                "mode": it.kind,
                "status": "reply",
                "answer": it.reply,
                "intent": it,
            }
        )

    table = handlers or {}
    if it.kind not in table:
        if it.kind == "fix":

            def _fix(**kw):
                from harness.core import run_task
                from shared.types import Task

                cfg = dict(kw.get("config") or {})
                task = Task(
                    task_id=kw.get("task_id") or f"fix-{_short_id()}",
                    repo_path=kw["repo_path"],
                    issue_text=kw["text"],
                    config=cfg,
                )
                result = run_task(task, log_root=kw.get("log_root"))
                return {
                    "mode": "fix",
                    "status": result.status,
                    "result": result,
                    "task_id": task.task_id,
                    "trace_path": result.log_path,
                }

            table["fix"] = _fix
        elif it.kind == "question":

            def _question(**kw):
                from harness.qa_mode import run_question

                return {
                    "mode": "question",
                    **run_question(
                        question=kw["text"],
                        repo_path=kw["repo_path"],
                        config=kw.get("config"),
                        log_root=kw.get("log_root"),
                    ),
                }

            table["question"] = _question
        elif it.kind == "build":

            def _build(**kw):
                cfg = dict(kw.get("config") or {})
                if cfg.get("build_project"):
                    # Long-horizon build: a request too large for one
                    # session routes to the multi-session project layer
                    # (criteria extraction + decomposition + per-session
                    # sub-task builds with checkpoint/resume) instead of
                    # the single-session build.
                    from harness.build_plan import run_project

                    return {
                        "mode": "build",
                        **run_project(
                            request_text=kw["text"],
                            repo_path=kw["repo_path"],
                            config=cfg,
                            log_root=kw.get("log_root"),
                        ),
                    }
                from harness.build_mode import run_build

                return {
                    "mode": "build",
                    **run_build(
                        request_text=kw["text"],
                        repo_path=kw["repo_path"],
                        config=cfg,
                        log_root=kw.get("log_root"),
                    ),
                }

            table["build"] = _build
        elif it.kind == "research":

            def _research(**kw):
                from harness.research_mode import run_research

                return {
                    "mode": "research",
                    **run_research(
                        question=kw["text"],
                        config=kw.get("config"),
                        log_root=kw.get("log_root"),
                        repo_path=kw.get("repo_path"),
                    ),
                }

            table["research"] = _research

    fn = table.get(it.kind)
    if fn is None:  # unreachable; defensive
        return ModeResult(
            {"mode": it.kind, "status": "error", "answer": "", "intent": it}
        )
    out = fn(
        text=text,
        repo_path=repo_path,
        config=config,
        log_root=log_root,
    )
    out.setdefault("mode", it.kind)
    return ModeResult(out)


def _short_id() -> str:
    import uuid

    return uuid.uuid4().hex[:8]
