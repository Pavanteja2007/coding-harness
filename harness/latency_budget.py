"""The published per-turn latency budget for the daily loop.

What a harness turn costs, which segment pays, and whether the render path
can lag. The numbers are MEASURED, not modeled - see
:data:`SEGMENTS`/`DEEPTHS` in ``harness/test_p1w2_timing.py``. This module
adds the one row that module cannot know about: the cost of the LSP
diagnostics that now ride inside every edit result (W1.2).

Usage::

    python -m harness.latency_budget
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List

__all__ = ["diagnostics_cost", "publication"]


def diagnostics_cost(trace_path: Any = "") -> Dict[str, Any]:
    """The LSP diagnostics cost, aggregated from ONE run's trace.

    ``latency_ms`` is emitted on every ``lsp_diagnostics_observed`` row by
    the edit gate (``strategy._observe_lsp_after_mutation``). When no edit
    ran through a language server, the honest answer is that it was not
    measured on this host - a zero would read as "free" and that is not a
    measurement.

    ``trace_path`` is ONE run's ``trace.jsonl``. It is deliberately NOT a
    recursive glob over the logs root: ``logs/`` holds a ``pristine/`` and a
    ``work/`` COPY of the repository per run, so walking it is the
    ``_SKIP_DIRS`` trap that cost this repository 345x on retrieval
    (``phases/DOCTRINE.md`` §8) - the first version of this function did
    exactly that and hung.
    """
    rows: List[float] = []
    if trace_path:
        try:
            with open(trace_path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"lsp_diagnostics_observed"' not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    val = row.get("latency_ms")
                    if val is None:
                        continue
                    try:
                        rows.append(float(val))
                    except (TypeError, ValueError):
                        continue
        except OSError:
            rows = []
    if not rows:
        return {
            "segment": "diagnostics",
            "measured": False,
            "samples": 0,
            "mean_ms": None,
            "note": (
                "no lsp_diagnostics_observed rows in the trace given; edit "
                "diagnostics were not exercisable on this host (lsp_enabled "
                "is off, or no language server). A 0 here would be a lie, so "
                "it is reported as unmeasured."
            ),
        }
    rows.sort()
    return {
        "segment": "diagnostics",
        "measured": True,
        "samples": len(rows),
        "mean_ms": round(sum(rows) / len(rows), 3),
        "median_ms": round(rows[len(rows) // 2], 3),
        "max_ms": round(max(rows), 3),
    }


def publication(trace_path: Any = "") -> Dict[str, Any]:
    """The latency budget, as a JSON-able dict. The ``segments`` table is the
    measured harness turn cost; ``diagnostics`` is the honest row from
    :func:`diagnostics_cost`. The render path is asserted non-blocking by
    ``cli/test_render_path_pin.py``."""
    return {
        "module": "harness.latency_budget",
        "segments_source": "harness/test_p1w2_timing.py::SEGMENTS",
        "render_path_assertion": (
            "cli/test_render_path_pin.py: every display sink takes a "
            "sanitized/escaped value; the turn loop performs all "
            "model/subprocess/Docker waits off the render thread."
        ),
        "diagnostics": diagnostics_cost(trace_path),
    }


def main(argv: List[str]) -> int:
    trace = argv[1] if len(argv) > 1 else ""
    print(json.dumps(publication(trace), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
