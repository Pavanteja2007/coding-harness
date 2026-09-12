"""Plain-language error explanations for CLI-level failures (Task D).

The contract: when something fails at the CLI level, the user sees
(1) WHAT went wrong in plain language, (2) WHICH CHECK to run next —
never a raw Python traceback. Narrowly scoped to error-message quality:
the interactive mode, theming, and live status live elsewhere.

Design: `explain_exception(exc)` maps the exception TYPES the stack
below the CLI actually raises (Docker/sandbox, model/router/litellm,
approval, filesystem, config) to a short diagnosis + a "check this"
line. Anything unmapped still gets a clean one-line message plus where
the full traceback is saved — an unexplained failure must never be
SILENT, and a debuggable failure must never dump 40 lines on a user.

Assumes exc is a live exception (or None); never raises itself.
"""

from __future__ import annotations

import os
import re
import tempfile
import traceback
from pathlib import Path
from typing import Optional

# The full traceback is written under $VEX_TRACEBACK_DIR when one is
# saved at all (kept out of the user's face, available for bug reports).
# The env var is read at CALL time so tests/runs can redirect it.
_TB_ENV = "VEX_TRACEBACK_DIR"


def _hint_docker() -> list[str]:
    return [
        "check: is Docker running? (`docker version` must show a Server section)",
        "check: on Windows/macOS, start Docker Desktop; on Linux: sudo service docker start",
    ]


def _hint_model() -> list[str]:
    return [
        "check: your model endpoint/key — pass --api-key (or set the env var your provider uses)",
        "check: --api-base if you use a custom/openai-compatible router",
        "check: the model name (--model) is valid for that endpoint",
    ]


def _safe_str(exc: BaseException) -> str:
    """str(exc) that never raises (an exception whose __str__ explodes
    must not take the failure handler down with it)."""
    try:
        return str(exc)
    except Exception:
        return f"<unprintable {type(exc).__name__}>"


def _classify(exc: BaseException) -> tuple[str, list[str]]:
    """Map one exception to (plain-language cause, checks to run).

    Ordered by the layers the CLI actually sits on: the module a
    message names tells the user WHOSE problem it likely is.
    """
    name = type(exc).__name__
    msg = _safe_str(exc)

    # --- sandbox / Docker layer -------------------------------------------
    if name == "SandboxUnavailableError" or "docker" in msg.lower():
        return (
            "Vex could not run commands in its Docker sandbox — every fix runs "
            "inside a container for safety, so no sandbox means no run.",
            _hint_docker(),
        )
    if "image" in msg.lower() and ("build" in msg.lower() or "pull" in msg.lower()):
        return (
            "Building the sandbox image for that repository failed "
            "(its dependency manifest may be broken, or the registry was "
            "unreachable).",
            [
                "check: `docker build` works at all (`docker run --rm hello-world`)",
                "check: the repo's requirements.txt / pyproject dependencies install cleanly",
            ],
        )

    # --- model / router / litellm layer -----------------------------------
    if "litellm" in msg.lower() or name in (
        "AuthenticationError",
        "RateLimitError",
        "APIConnectionError",
        "Timeout",
        "ServiceUnavailableError",
        "NotFoundError",
    ):
        if "auth" in msg.lower() or name == "AuthenticationError":
            return (
                "The model endpoint rejected the credentials — the API key is "
                "missing, wrong, or expired.",
                _hint_model(),
            )
        if name == "RateLimitError" or "rate" in msg.lower():
            return (
                "The model endpoint is rate-limiting this key — too many "
                "requests in a short window.",
                ["check: wait a moment and retry; or use a different key/endpoint"],
            )
        return (
            "The model endpoint could not be reached or answered with an "
            "error (network, timeout, or a transient provider issue).",
            [*_hint_model(), "check: the endpoint is up (a trivial curl to it works)"],
        )
    if "api_key" in msg.lower() or "api key" in msg.lower():
        return (
            "No model credentials were provided — a fix run needs a model "
            "backend to do the planning and editing.",
            _hint_model(),
        )

    # --- approval gate ------------------------------------------------------
    if "approval" in msg.lower():
        return (
            "The human-approval gate could not complete — the run parked "
            "waiting for an approve/reject decision and never got one.",
            [
                "check: use `vex fix --approval` in a terminal so the prompt appears",
                "check: or answer from an interactive session with /approve or /reject",
            ],
        )

    # --- filesystem / repo layer -------------------------------------------
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return (
            "A required file or directory does not exist at the path given.",
            [
                "check: the path printed above (does it exist? correct drive/case on Windows?)"
            ],
        )
    if isinstance(exc, PermissionError):
        return (
            "The process does not have permission to read or write something it needs.",
            [
                "check: the path printed above is not locked by another program",
                "check: on Windows, files under a running container mount can be locked",
            ],
        )
    if isinstance(exc, OSError):
        return (
            f"The operating system refused an operation: {msg}",
            [
                "check: the path/permissions above; antivirus or sync clients can also lock files"
            ],
        )

    # --- config layer --------------------------------------------------------
    if "config" in msg.lower() and "toml" in msg.lower():
        return (
            "The Vex config file (~/.vex/config.toml or $VEX_CONFIG) could "
            "not be parsed.",
            [
                "check: the file is valid TOML (`python -c \"import tomllib,sys; tomllib.load(open(sys.argv[1],'rb'))\" <file>` on 3.11+)"
            ],
        )

    # --- fallback: honest, short, never silent ------------------------------
    first_line = msg.splitlines()[0] if msg else name
    return (
        f"An unexpected internal error occurred ({name}: {first_line}).",
        [
            "check: re-run with the same command — if it repeats, please open an issue",
            "check: the full traceback was saved (path printed below) — attach it to the report",
        ],
    )


def _save_traceback(exc: BaseException) -> Optional[str]:
    """Persist the full traceback to a file for debugging; None on failure.

    Never lets debugging infrastructure make a failure worse.
    """
    try:
        # Read the env var at CALL time (see _TB_ENV note above).
        tb_dir = os.environ.get(_TB_ENV, "")
        root = Path(tb_dir) if tb_dir else Path(tempfile.gettempdir())
        root.mkdir(parents=True, exist_ok=True)
        import time as _time

        slug = re.sub(r"[^A-Za-z0-9_-]+", "_", type(exc).__name__)[:40]
        f = root / f"vex-traceback-{int(_time.time())}-{slug}.txt"
        f.write_text("".join(traceback.format_exception(exc)), encoding="utf-8")
        return str(f)
    except Exception:
        return None


def explain_exception(exc: BaseException, *, save_traceback: bool = True) -> None:
    """Print the plain-language explanation of `exc` to stderr (Vex theme).

    What the user sees: one-line diagnosis, "check:" lines, and — for
    unmapped exception classes — the path to the saved full traceback.
    Raw tracebacks are never printed to the terminal.

    Assumes it runs inside a CLI command's except-block; the exit-code
    contract (0/1/2) is the caller's to keep.
    """
    # Local import: cli.ui imports rich; keep this module importable
    # standalone (it is also exercised by tests without the full CLI).
    from cli import ui

    err = ui.err_console()
    cause, checks = _classify(exc)

    err.print(f"[vex.error]error: {cause}[/]")
    for c in checks:
        err.print(f"[vex.muted]{c}[/]")

    tb_path = _save_traceback(exc) if save_traceback else None
    if tb_path:
        err.print(f"[vex.muted]full traceback saved: {tb_path}[/]")
    # Never print the raw traceback inline — that is the whole point.
