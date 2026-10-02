"""Held-out comparison of the two difficulty predictors (R2-13, item 2/3).

This module exists to answer ONE question with data, not with an assertion:

    does the R2-13 STRUCTURAL predictor beat the incumbent lexical one on
    data neither of them was read from?

and then to obey the answer. If the structural predictor does not win the
held-out split, this module says so in the report, in the exit code, and in
the ``--apply`` behaviour: **nothing is written and the incumbent stays the
default.** That is the same discipline the project already applied to the v2
recalibration (``runtime.analyze_history``: "held-out delta zero ... not
applied") and to the multi-arm routing ablation ("the ensemble did NOT
clearly beat ... on this task set"). A predictor that ships without beating
its predecessor is worse than the predecessor, because it looks measured.

## Where the data comes from

REAL run history under ``--logs-root`` (default ``logs``), consumed through
``runtime.analyze_history``'s public functions:

* ``scan_tasks`` applies the documented exclusion filters (scripted/fake
  runs, archived trees, non-fix modes) and validates the lifecycle;
* ``calibration_rows`` applies the documented STRICT label policy — only a
  verifier-refused ``failed`` on the routed-cheap tier is ``hard``, a success
  is ``easy``, and endpoint deaths (``error``/``timeout``) are EXCLUDED
  entirely, because endpoint latency is not bug difficulty.

Nothing here invents a label and nothing is synthesised. The two extra
per-task fields the structural features need (``issue_text`` and the declared
``target_test``) are read back out of each accepted task's own
``trace.jsonl``, which is the documented public trace format, and an
unresolvable one is reported as unresolvable rather than defaulted to zero.

## What it measures

``runtime.difficulty.compare_predictors`` decides the winner on HELD-OUT bug
GROUPS (a group never straddles the split: the same bug re-run across
ablation windows is one observation, not two). The report states the winner,
whether the result is *decidable* (a holdout with no hard labels cannot
validate a difficulty predictor), the structural-feature coverage actually
achieved, and both predictors' per-split missed/false escalations.

Usage::

    python -m evals.difficulty_holdout --json
    python -m evals.difficulty_holdout --logs-root logs --holdout-frac 0.25
    python -m evals.difficulty_holdout --apply     # writes ONLY on ship:true

Exit codes: ``0`` the comparison ran and its verdict was reported (whatever
the verdict), ``2`` no logs root / nothing to compare, ``3`` the structural
predictor WON the held-out split (so a caller can gate a release on the
opposite of the usual). The verdict itself is always in the report JSON; the
exit code is a convenience, never the only signal.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - path boot
    sys.path.insert(0, str(REPO_ROOT))

from runtime import analyze_history, difficulty  # noqa: E402

#: Where a validated structural calibration would live. The router reads this
#: exact path; nothing writes it unless ``--apply`` AND ``ship`` are both true.
CALIBRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "runtime"
    / ("difficulty_structural_calibration.json")
)

#: Bounded walk budget for the structural features. Matches the module default
#: so a report is reproducible; the receipt records truncation either way.
WALK_BUDGET = difficulty.MAX_WALK_ENTRIES


def _first_task_start(trace_path: Path) -> Dict[str, Any]:
    """Return the first ``task_start`` payload from a trace, or ``{}``.

    Assumes the documented ``{"kind": ..., "data": {...}}`` trace rows. A
    missing file, an unparseable line, or a trace with no ``task_start`` all
    return ``{}`` — a task whose context cannot be read is an unresolvable
    row, reported as one, never scored as a featureless row.
    """
    try:
        with trace_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if row.get("kind") == "task_start" or row.get("event") == "task_start":
                    data = row.get("data")
                    if isinstance(data, dict):
                        return data
                    if row.get("event") == "task_start":
                        return row
    except OSError:
        return {}
    return {}


def _declared_target_test(trace_path: Path) -> Tuple[Optional[str], str]:
    """Return the task's declared target test and where it came from.

    ``config["target_test"]`` is the declaration. When it is absent the
    ``baseline_verify`` row's ``target_test`` is the verifier's own record of
    what it ran, which is a real declared target for the purposes of "does the
    target test exist" — the feature asks whether the repository can answer for
    the test the run actually checks, not whether the operator typed it twice.
    The SOURCE is returned alongside so a reader can tell which one it was.
    """
    target: Optional[str] = None
    source = "absent"
    try:
        with trace_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                data = row.get("data") if isinstance(row.get("data"), dict) else row
                kind = row.get("kind") or row.get("event")
                if kind == "task_start":
                    declared = (data.get("config") or {}).get("target_test")
                    if isinstance(declared, str) and declared.strip():
                        return declared.strip(), "config"
                elif kind == "baseline_verify" and target is None:
                    declared = data.get("target_test")
                    if isinstance(declared, str) and declared.strip():
                        target = declared.strip()
                        source = "baseline_verify"
    except OSError:
        return None, "unreadable"
    return target, source


def _touched_symbols(trace_path: Path) -> List[str]:
    """Collect the symbols a task reported touching, from its own trace.

    Assumes a ``git``/``edit`` style row carrying ``files``; a task that
    reported none yields an empty list, which the fan-in feature reports as
    UNMEASURED rather than as zero blast radius.
    """
    found: List[str] = []
    try:
        with trace_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "symbol" not in line and "files" not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                data = row.get("data") if isinstance(row.get("data"), dict) else row
                for value in data.get("symbols") or ():
                    if isinstance(value, str) and value.strip():
                        found.append(value.strip())
    except OSError:
        return found
    return found[:64]


def build_observations(
    logs_root: Path, *, walk_budget: int = WALK_BUDGET
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Build one comparison row per real, labelled, adaptively-routed task.

    Assumes ``logs_root`` is a directory of run artifacts. Returns
    ``(rows, coverage)`` where ``coverage`` reports what could and could not be
    resolved, so a report can state its own coverage instead of implying the
    structural features were fully measured.
    """
    diagnostics: Dict[str, int] = {}
    records = analyze_history.scan_tasks(logs_root, diagnostics)
    labelled = analyze_history.calibration_rows(records)
    by_id = {record["task_id"]: record for record in records}
    rows: List[Dict[str, Any]] = []
    unresolved: Dict[str, int] = {}
    coverage = {
        "logs_root": str(logs_root),
        "scan_diagnostics": dict(diagnostics),
        "accepted_records": len(records),
        "labelled_rows": len(labelled),
        "rows_built": 0,
        "repo_path_resolved": 0,
        "repo_path_missing": 0,
        "issue_text_resolved": 0,
        "target_test_declared": 0,
        "target_test_sources": {},
        "unresolved_reasons": unresolved,
        "walk_budget": walk_budget,
        # PER-FEATURE coverage. Reported separately because the features fail
        # to resolve for very different reasons, and "features_resolved" alone
        # would let a row that measured one of six features read as a fully
        # measured row.
        "feature_measured": {
            "repo_size": 0,
            "test_density": 0,
            "target_test_missing": 0,
            "fan_in": 0,
            "bug_class": 0,
            "multi_module": 0,
        },
        "feature_notes": {
            "fan_in": "requires a per-task touched-SYMBOL list; run history "
            "records touched FILES, so this feature is unmeasured unless a "
            "producer emits symbols",
            "target_test_missing": "requires a declared target test; runs that "
            "rely on test autodetection declare none, which is reported as "
            "unmeasured rather than as 'the target is missing'",
            "multi_module": "requires the task's expected changed files; a run "
            "that records none cannot be scored for blast radius",
        },
    }
    for label_row in labelled:
        record = by_id.get(label_row.get("task_id"))
        if record is None:
            unresolved["record_missing"] = unresolved.get("record_missing", 0) + 1
            continue
        trace_path = (
            logs_root
            / str(record.get("rel_dir", "")).replace("\\", "/")
            / ("trace.jsonl")
        )
        start = _first_task_start(trace_path)
        issue = start.get("issue_text")
        repo_path = start.get("repo_path") or record.get("repo")
        if not isinstance(issue, str) or not issue.strip():
            unresolved["issue_text"] = unresolved.get("issue_text", 0) + 1
            continue
        coverage["issue_text_resolved"] += 1
        if repo_path and Path(str(repo_path)).is_dir():
            coverage["repo_path_resolved"] += 1
        else:
            coverage["repo_path_missing"] += 1
        target_test, target_source = _declared_target_test(trace_path)
        if target_test:
            coverage["target_test_declared"] += 1
            coverage["target_test_sources"][target_source] = (
                coverage["target_test_sources"].get(target_source, 0) + 1
            )
        context = {
            "repo_path": str(repo_path) if repo_path else None,
            "issue_text": issue,
            "target_test": target_test,
            "changed_files": [],
            "touched_symbols": _touched_symbols(trace_path),
        }
        legacy_hint, legacy_info = _legacy_hint(issue)
        structural_hint, structural_info = difficulty.predict_structural(context)
        features = structural_info["features"]
        for name, resolved in (features.get("feature_coverage") or {}).items():
            if resolved and name in coverage["feature_measured"]:
                coverage["feature_measured"][name] += 1
        rows.append(
            {
                "task_id": label_row["task_id"],
                "group_id": label_row["group_id"],
                "repo_key": label_row.get("repo_key"),
                "label": label_row["label"],
                "status": label_row.get("status"),
                "attempts": label_row.get("attempts"),
                "legacy_hint": legacy_hint,
                "legacy_score": (legacy_info.get("features") or {}).get("score"),
                "structural_hint": structural_hint,
                "structural_score": features.get("score"),
                "features_resolved": bool(features.get("resolved_feature_count")),
                "bug_class": features.get("bug_class"),
                "repo_shape_measured": bool(
                    (features.get("repo_shape") or {}).get("measured")
                ),
                "fan_in_measured": bool((features.get("fan_in") or {}).get("measured")),
                "target_test_source": target_source,
                "resolved_feature_count": features.get("resolved_feature_count"),
                "feature_count": features.get("feature_count"),
            }
        )
    coverage["rows_built"] = len(rows)
    return rows, coverage


def _legacy_hint(issue_text: str) -> Tuple[str, Dict[str, Any]]:
    """Return the INCUMBENT predictor's hint for ``issue_text``.

    Uses ``runtime.ensemble.predict_task_difficulty`` so the incumbent is
    scored through exactly the message shape the router's planner ingress
    scores — comparing against a differently-fed incumbent would be measuring
    the harness, not the predictor.
    """
    from runtime.ensemble import predict_task_difficulty

    return predict_task_difficulty(issue_text)


def build_report(
    logs_root: Path, *, holdout_frac: float = 0.25, seed: int = 7
) -> Dict[str, Any]:
    """Run the full comparison and return a JSON-safe report.

    Assumes ``logs_root`` exists. The report carries the verdict, both
    predictors' per-split scores, the feature coverage that was actually
    achieved, and a ``disagreement`` census (which tasks the two predictors
    split on) because that census is the useful diagnostic when a challenger
    loses: it says whether the two features families even see the same tasks.
    """
    rows, coverage = build_observations(logs_root)
    verdict = difficulty.compare_predictors(rows, holdout_frac=holdout_frac, seed=seed)
    disagreements = [
        {
            "task_id": row["task_id"],
            "group_id": row["group_id"],
            "label": row["label"],
            "legacy_hint": row["legacy_hint"],
            "structural_hint": row["structural_hint"],
            "legacy_score": row["legacy_score"],
            "structural_score": row["structural_score"],
            "bug_class": row["bug_class"],
        }
        for row in rows
        if row["legacy_hint"] != row["structural_hint"]
    ]
    return {
        "kind": "r2_13_difficulty_holdout",
        "logs_root": str(logs_root),
        "holdout_frac": holdout_frac,
        "seed": seed,
        "verdict": verdict,
        "coverage": coverage,
        "disagreements": disagreements,
        "disagreement_count": len(disagreements),
        "rows": rows,
        "honesty": [
            "Rows are real adaptively-routed fix runs; labels follow the "
            "documented strict policy (verifier-refused failure = hard, "
            "success = easy, endpoint deaths excluded).",
            "The winner is decided on HELD-OUT bug groups only. A holdout with "
            "no hard labels is reported as not decidable and the incumbent is "
            "kept.",
            "ship=false means the structural predictor is NOT enabled; nothing "
            "in the router turns it on and no calibration artifact is written.",
            "DESIGN LEAKAGE, stated because it bounds what this comparison can "
            "claim: the structural feature family was chosen AFTER reading this "
            "project's own documented bug corpus (runtime/AGENTS.md names the "
            "real hard bugs: backoff-race, parse-comma, slugify-case, "
            "inflect/boltons ordinal-teen). The bug-class probes and fan-in "
            "signal were picked knowing which bug shapes this project has hit. "
            "So this is a valid test of the structural BANDS and a WEAK test "
            "of the feature DESIGN: it is not a clean out-of-sample evaluation "
            "of the idea, and a holdout win here should be read as 'the bands "
            "are not obviously wrong', not as 'these features separate easy "
            "from hard'.",
            "A difficulty predictor is only as good as its hard labels, and this "
            "history is easy-dominated by construction: the strict policy "
            "yields very few 'hard' rows, which is why the hard-label floor "
            "exists.",
        ],
    }


def write_calibration(
    report: Dict[str, Any], path: Path = CALIBRATION_PATH
) -> Optional[Path]:
    """Write the structural calibration artifact ONLY when the report says so.

    Assumes ``report`` is a :func:`build_report` result. Returns the written
    path, or ``None`` when nothing was written — and a ``None`` here is the
    correct outcome, not a failure, because the gate is
    ``verdict.ship is True``. The artifact records the verdict that justified
    it, so a later reader can re-check the claim instead of trusting the file.
    """
    verdict = (report or {}).get("verdict") or {}
    if verdict.get("ship") is not True:
        return None
    payload = {
        "schema_version": 1,
        "shipped": True,
        "bands": list(difficulty._BUILTIN_STRUCTURAL_BANDS),
        "verdict": {
            "winner": verdict.get("winner"),
            "holdout_rows": verdict.get("holdout_rows"),
            "holdout_hard_labels": verdict.get("holdout_hard_labels"),
            "coverage": verdict.get("coverage"),
        },
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. ``--json`` prints only the report; exit codes as documented.

    Assumes a normal argv. A missing logs root is exit ``2`` and says so; a
    comparison that ran is exit ``0`` even when the challenger LOST, because a
    reported negative result is a successful measurement. Exit ``3`` is reserved
    for the challenger winning, so a caller can gate on it.
    """
    parser = argparse.ArgumentParser(
        description="Held-out comparison of the legacy and structural difficulty predictors."
    )
    parser.add_argument("--logs-root", default=str(REPO_ROOT / "logs"))
    parser.add_argument("--holdout-frac", type=float, default=0.25)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--json", action="store_true", help="print the report only")
    parser.add_argument(
        "--out", default=None, help="directory to write the report into"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the structural calibration ONLY if it won the held-out split",
    )
    args = parser.parse_args(argv)
    logs_root = Path(args.logs_root)
    out_dir = Path(args.out) if args.out else None
    if not logs_root.is_dir():
        report: Dict[str, Any] = {
            "kind": "r2_13_difficulty_holdout",
            "verdict": {
                "winner": "none",
                "ship": False,
                "decidable": False,
                "reason": "logs_root_absent",
                "honesty": f"no logs root at {logs_root}; nothing was compared",
            },
        }
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "difficulty_holdout_report.json").write_text(
                json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
            )
        if args.json:
            print(json.dumps(report, indent=2, default=str))
        else:
            print(f"no logs root at {logs_root}; nothing was compared")
        return 2
    report = build_report(logs_root, holdout_frac=args.holdout_frac, seed=args.seed)
    if args.apply:
        written = write_calibration(report)
        report["calibration_written"] = str(written) if written else None
        report["calibration_note"] = (
            "structural predictor won the held-out split; calibration written"
            if written
            else "structural predictor did not win; nothing written, incumbent kept"
        )
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "difficulty_holdout_report.json").write_text(
            json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
        )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        verdict = report["verdict"]
        print(
            "rows={rows} holdout_rows={holdout_rows} hard_in_holdout={hard} "
            "winner={winner} ship={ship} decidable={decidable}".format(
                rows=verdict.get("rows", 0),
                holdout_rows=verdict.get("holdout_rows", 0),
                hard=verdict.get("holdout_hard_labels", 0),
                winner=verdict.get("winner"),
                ship=verdict.get("ship"),
                decidable=verdict.get("decidable"),
            )
        )
        print(verdict.get("honesty", ""))
        for name, split in (report.get("verdict", {}).get("scores") or {}).items():
            for predictor, result in (split or {}).items():
                print(
                    f"  {name}/{predictor}: n={result.get('n', 0)} "
                    f"acc={result.get('accuracy_easy_or_hard')} "
                    f"missed={result.get('missed_escalations')} "
                    f"false={result.get('false_escalations')}"
                )
    if not report["verdict"].get("decidable"):
        return 0
    return 3 if report["verdict"].get("ship") else 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
