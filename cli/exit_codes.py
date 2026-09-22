"""Vex exit codes — the stable machine contract for scripts and CI.

The categories (documented in README "Exit codes"; mirrored by the
`EXIT_CODES` table so docs and code can never drift apart):

    0    success
    1    task-level failure (the run itself did not fix the bug)
    2    usage / configuration error (bad arguments, bad files, config)
    3    environment error (Docker down, dependency missing — fix before retrying)
    4    model / network error (endpoint unreachable, auth rejected, timeout)
    130  interrupted by the user (Ctrl+C / SIGINT)

Backward compatibility: the historic contract was 0/1/2 ("0 ok / 1 task
failure / 2 usage error"). This module SPLITS two failure classes out of
the old catch-all 1 (environment vs model/network) while keeping the
meaning of 0, 1, and 2 unchanged. Legacy 0/1/2 scripts keep working;
new callers can distinguish the extra categories.

`classify_exit_code(exc)` maps an exception to its category using the
same layer mapping cli.errors._classify already documents — it is the
glue between the plain-language explainer and the numeric contract.
Never raises.
"""

from __future__ import annotations

# The stable numeric contract (single source of truth for docs + code).
EXIT_CODES: dict = {
    "success": 0,
    "task_failure": 1,  # the harness ran but did not produce a verified fix
    "usage_error": 2,  # bad arguments / malformed files / config mistakes
    "environment_error": 3,  # Docker unavailable, missing dependency, sandbox down
    "model_error": 4,  # model endpoint/network: unreachable, auth, rate limit
    "interrupted": 130,  # user Ctrl+C (128+SIGINT convention)
}

# Names for exit-code -> reason rendering (JSON mode, tests).
EXIT_REASONS: dict = {v: k for k, v in EXIT_CODES.items()}

#: Subprocess exit codes that already carry their own meaning and are
#: passed through untouched (argparse's usage errors exit 2 — a usage
#: error by our own definition, so the mapping is consistent).
_PASSTHROUGH = {0, 1, 2, 130}


def exit_code(category: str) -> int:
    """The numeric code for a category name (KeyError on a typo — this
    is an internal API; callers pass literal category names)."""
    return EXIT_CODES[category]


def reason_for(code: int) -> str:
    """The category name for a numeric code, or "unknown" (never raises;
    used for JSON output where a stray code must not crash reporting)."""
    return EXIT_REASONS.get(code, "unknown")


def is_known_code(code: int) -> bool:
    """True when `code` is one of Vex's documented exit codes."""
    return code in EXIT_REASONS


def classify_exit_code(exc: "BaseException") -> int:
    """Map one exception to a Vex exit code using the layer categories
    from cli.errors (sandbox/Docker -> environment, model/litellm ->
    model, everything else unexpected -> 1). Never raises.

    Assumes `exc` is a live exception escaping a CLI command; the
    plain-language explanation (cli.errors.explain_exception) stays the
    caller's job — this returns only the number.
    """
    try:
        from cli.errors import failure_category
    except ImportError:  # standalone use / broken install: old contract
        return EXIT_CODES["task_failure"]
    try:
        cat = failure_category(exc)
    except Exception:
        return EXIT_CODES["task_failure"]
    return EXIT_CODES.get(cat, EXIT_CODES["task_failure"])
