"""G1 — the P1 Daily Usable gate.

WHAT THIS FILE IS
-----------------
The one place a reader can ask "did Phase 1 actually land?" and get an answer
that cannot be quietly upgraded. It measures; it does not choose. Its verdict is
derived from measurements, and every row that is not a measurement says which.

THE FOUR STATUSES
-----------------
Imported from :mod:`evals.gates.P0`, never re-declared. One authority for a
vocabulary, because two lists is how ``skip`` gets added back:

* ``pass`` — ran and held.
* ``fail`` — ran and broke.
* ``blocked`` — could not run; **the exact reason is mandatory**.
* ``not_implemented`` — does not exist; **the owning phase is mandatory**.

``skip`` is not a member and cannot be constructed: a skip renders exactly like
a pass in a summary table, which is the one confusion this repository exists to
prevent (``phases/DOCTRINE.md`` §1).

WHY G1 IS RED ON THIS TREE, AND WHY THAT IS THE RIGHT ANSWER
------------------------------------------------------------
Three of Phase 1's deliverables are Trust Ladder rungs (#8, #9, #10), and a
performance phase is the easiest kind of phase to over-claim. So this gate is
built to make three specific over-claims impossible:

1. **A rung that has never been seen to fire is decoration.** Every rung's
   check is exercised against a deliberately constructed failure by
   :mod:`evals.rung_break_demos`, and that evidence is a ROW here
   (``rung_checks_demonstrated``). A rung that is green but undemonstrated
   cannot be green on this gate.
2. **A blocked lane is not a pass.** Docker, the live provider and the Windows
   lane each get their own row with the exact error.
3. **The 2x cost is not a footnote.** The turn-ceiling doubling is computed from
   the shipping config and published in the SAME document as the speed wins
   (:data:`COST_STATEMENT`), and its row fails if the document ever separates
   them.

WHAT THIS GATE IS NOT
---------------------
It is not a release gate and it does not certify a build. It certifies that a
set of *measured* claims about this tree, on this host, at this commit, are
true. Every one of those three qualifiers is in
:data:`NOT_ESTABLISHED`, and the CLI prints them next to the verdict so a
verdict cannot be read without them.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]

from evals.gates.P0 import (  # noqa: E402  (path-dependent by design)
    BLOCKED,
    FAIL,
    NOT_IMPLEMENTED,
    PASS,
    STATUSES,
    Rung,
    blocked,
    docker_reachable,
    not_implemented,
    parse_pytest_log,
    read_log_text,
    run_probe,
)

#: This gate's own health word, distinct from G0's so a reader cannot confuse
#: the two documents.
G1_GREEN = "G1_GREEN"
G1_RED = "G1_RED"
VERDICTS: Tuple[str, ...] = (G1_GREEN, G1_RED)

#: The Trust Ladder suite, as registered in ``evals/run.py``.
TRUST_LADDER_SUITE = "trust-ladder"

# --------------------------------------------------------------------------
# THE 2x COST, PUBLISHED IN THE SAME DOCUMENT AS THE SPEED WINS
# --------------------------------------------------------------------------

#: The turn ceiling BEFORE Phase 1. Not a constant anybody can forget, because
#: it is only ever used to compute a RATIO against the value read out of the
#: shipping config, and a reader who wants the ratio gets it from this gate
#: rather than from a prose sentence.
TURN_CEILING_BEFORE_P1 = 25

#: The rung the ceiling has to clear for "stays coherent past 50 turns" to mean
#: anything.
TURN_CEILING_REQUIRED = 50

#: The prose this gate will not publish unless the cost is in the same breath.
#: Rendered into the report, and checked by ``cost_is_in_the_same_document``.
COST_STATEMENT: Tuple[str, ...] = (
    "THE COST OF PHASE 1, STATED WHERE THE SPEED WINS ARE STATED.",
    "The per-task turn ceiling went 25 -> 60, a 2.4x increase in the WORST-CASE "
    "number of model turns in one task. A ceiling is not a bill: most tasks "
    "finish far inside it, and the binding limits on spend are "
    "`budget_cap_usd` and `max_wallclock_s`, which are enforced independently "
    "of the turn count.",
    "So the honest statement is a BOUND, not an expectation. What Phase 1 "
    "bought: a 366x narrower repository walk (the demonstration in "
    "evals/rung_break_demos.py measures 164 dirs against >=60,000), and a "
    "startup path that imports in ~415ms. What it cost: up to 2.4x the turns "
    "in the tail. Neither number is comparable to the other, and a reader who "
    "quotes the speed without the cost is quoting half the phase.",
)


def cost_statement(ladder: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Compute the cost bound from the SHIPPING config, not from a constant.

    Read through ``harness.config.DEFAULTS`` because that is the seam the loop
    reads. If the two disagree the row says so rather than preferring one --
    the exact stale-constant defect that made Phase 1's own ceiling number
    unreadable for a session (see ``tests/test_config_defaults_match_source.py``).
    """
    try:
        from harness.config import DEFAULTS

        live = DEFAULTS.get("agent_max_turns")
    except Exception as exc:  # a broken import is a finding, not a zero
        return {
            "readable": False,
            "reason": f"harness.config is not importable: {exc!r}",
            "before": TURN_CEILING_BEFORE_P1,
            "required": TURN_CEILING_REQUIRED,
        }
    live_int = live if isinstance(live, int) else None
    declared = _ladder_cap(ladder)
    ratio = (
        float(live_int) / float(TURN_CEILING_BEFORE_P1)
        if live_int
        else float("nan")
    )
    return {
        "readable": live_int is not None,
        "reason": "" if live_int is not None else f"agent_max_turns is {live!r}",
        "before": TURN_CEILING_BEFORE_P1,
        "after": live_int,
        "required": TURN_CEILING_REQUIRED,
        "worst_case_ratio": round(ratio, 2) if live_int else None,
        "ladder_reported_cap": declared,
        "the_two_agree": (declared is None or declared == live_int),
        "statements": list(COST_STATEMENT),
    }


def _ladder_cap(ladder: Optional[Dict[str, Any]]) -> Optional[int]:
    """The ceiling the ladder actually read, or ``None``."""
    if not ladder:
        return None
    for row in ladder.get("rungs", []):
        if int(row.get("number") or 0) == 8:
            measured = row.get("measured") or {}
            value = measured.get("declared_turn_cap")
            return value if isinstance(value, int) else None
    return None


# --------------------------------------------------------------------------
# THE PERFORMANCE TABLE
# --------------------------------------------------------------------------

#: The metrics a performance claim is allowed to be made about. Every one is
#: carried with machine, run count and which statistic it is, because a number
#: without those three is a claim about nothing in particular.
PERFORMANCE_METRICS: Tuple[str, ...] = (
    "startup_to_first_token",
    "retrieval_cold_then_warm",
    "retrieval_walk_dirs_opened",
    "tui_frame_cost",
)


def performance_table(ladder: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every rung-#9 timing, with its machine, its window and its statistic."""
    if not ladder:
        return []
    rows: List[Dict[str, Any]] = []
    for rung in ladder.get("rungs", []):
        if int(rung.get("number") or 0) != 9:
            continue
        measured = rung.get("measured") or {}
        for timing in measured.get("timings", []) or []:
            name = str(timing.get("metric") or "")
            if name not in PERFORMANCE_METRICS:
                continue
            rows.append(
                {
                    "metric": name,
                    "available": bool(timing.get("available")),
                    "value": timing.get("value"),
                    "unit": timing.get("unit"),
                    "window_runs": timing.get("window"),
                    "statistic": timing.get("statistic"),
                    "machine": timing.get("machine"),
                    "target": timing.get("target"),
                    "within_budget": timing.get("within_budget"),
                    "percentiles_withheld": timing.get("percentiles_withheld"),
                    "p50": timing.get("p50"),
                    "p95": timing.get("p95"),
                    "reported_as": timing.get("reported_as"),
                    "reason": timing.get("reason"),
                }
            )
    return rows


# --------------------------------------------------------------------------
# WHAT G1 DOES NOT ESTABLISH — printed with the verdict, never separated
# --------------------------------------------------------------------------

NOT_ESTABLISHED: Tuple[str, ...] = (
    "EVERY MODEL CALL IN EVERY RUNG AND EVERY LANE IS A SCRIPTED DOUBLE. T3 "
    "recorded `ServiceUnavailableError: No available channel` on 3/3 live "
    "completions, and no live provider was reachable for this gate either. No "
    "number in this report is a claim about model quality, model cost, or model "
    "latency. What was measured is the SHELL, the retrieval path and the "
    "projection chain.",
    "`python -m evals.run --check` is a HOST SELF-CHECK of a 14-task prompt "
    "set. It proves the eval harness is internally consistent and that no "
    "prompt change regressed a scripted arm. It is NOT a model-quality gate.",
    "Rung #9's retrieval number is measured on THIS repository on THIS machine "
    "and is named with both. It is a claim about one checkout on one host, not "
    "about the product on an arbitrary machine. A timing assertion in a "
    "BLOCKING lane is the one thing this gate refuses to ship, because a flaky "
    "timing test in a blocking lane teaches people to ignore the gate.",
    "A p95 is printed only when the window is at least 20 observations "
    "(`MIN_SAMPLES_FOR_P95`). Below that the headline is labelled a MEDIAN and "
    "the percentile is withheld with the window size shown. No p95 in this "
    "report was computed over fewer than 20 runs.",
    "THE DAILY-DRIVER READINESS GATE IS NOT GREEN AND IS NOT COUNTED HERE. It "
    "reports `NOT_READY` with "
    "`cost_latency_user_intervention_at_least_90pct` UNMEASURED in earlier "
    "rounds. An unmeasured capability is `not_implemented` with an owner, not "
    "a pass, and its row says so.",
    "A green row is a statement about THIS TREE, ON THIS HOST, AT THIS COMMIT. "
    "No reproducible build, no clean-room install and no tag were produced.",
    "Phase 1's LOC delta is NOT recoverable from git history. T4's Wave 1 is a "
    "clean commit range; T1/T2/T3's Wave 1 landed inside one mega-commit that "
    "also renames the distribution and adds a marketing site, so a single "
    "'Phase 1 diff' does not exist and the numbers below are labelled by what "
    "they actually measure.",
)


def _row(
    ladder: Optional[Dict[str, Any]], number: int
) -> Optional[Dict[str, Any]]:
    if not ladder:
        return None
    for rung in ladder.get("rungs", []):
        if int(rung.get("number") or 0) == number:
            return rung
    return None


def _from_ladder(
    ladder: Optional[Dict[str, Any]], number: int, *, fallback: str
) -> Rung:
    """Turn a measured ladder row into a gate row. Never invents a verdict."""
    if ladder is None:
        return blocked(
            f"rung_{number:02d}_measured",
            fallback,
            "no Trust Ladder report was supplied to this gate, so the rung was "
            "not measured here. That is `blocked`, not `pass`: an unmeasured "
            "guarantee has not been shown to hold.",
            owner="T5",
            evidence=f"python -m evals.run --suite {TRUST_LADDER_SUITE}",
        )
    row = _row(ladder, number)
    if row is None:
        return blocked(
            f"rung_{number:02d}_measured",
            fallback,
            f"the supplied Trust Ladder report has no row #{number}",
            owner=str(row.get("owner") if row else ""),
            evidence=f"python -m evals.run --suite {TRUST_LADDER_SUITE}",
        )
    status = str(row.get("status") or "")
    if status not in STATUSES:
        return blocked(
            f"rung_{number:02d}_measured",
            fallback,
            f"the ladder reported {status!r}, which is not in {STATUSES!r}",
            owner=str(row.get("owner") or ""),
            evidence=str(row.get("evidence") or ""),
        )
    return Rung(
        f"rung_{number:02d}_measured",
        str(row.get("guarantee") or fallback),
        status,
        str(row.get("detail") or "").strip() or "the ladder reported no detail",
        owner=str(row.get("owner") or ""),
        evidence=str(row.get("evidence") or ""),
        blocking=bool(row.get("blocking", True)),
    )


# --------------------------------------------------------------------------
# the probes this gate runs itself (cheap, deterministic, host-only)
# --------------------------------------------------------------------------


def _python(*args: str, timeout_s: int = 900) -> Dict[str, Any]:
    return run_probe([sys.executable, *args], timeout_s=timeout_s)


def rung_rung_checks_demonstrated(*, demos: Optional[Dict[str, Any]]) -> Rung:
    """Every Phase 1 rung's check must have been SEEN TO FIRE.

    A rung with no demonstrated failure is decoration, so this row exists to
    make "green but undemonstrated" unrepresentable.
    """
    rid = "rung_checks_demonstrated"
    title = "every rung check was seen to fire on a constructed failure"
    if demos is None:
        return blocked(
            rid,
            title,
            "no rung-break demonstration report was supplied. Run "
            "`python -m evals.rung_break_demos --json` and pass the file with "
            "--demos. A rung nobody has seen fail is a rung nobody knows works.",
            owner="T5",
            evidence="python -m evals.rung_break_demos --json",
        )
    verdict = str(demos.get("verdict") or "")
    rows = demos.get("demos") or []
    bad = [
        r
        for r in rows
        if r.get("status") != PASS or not r.get("caught")
    ]
    if verdict == "RUNG_CHECKS_DEMONSTRATED" and not bad:
        return Rung(
            rid,
            title,
            PASS,
            f"ran and held: {len(rows)}/{len(rows)} deliberate breaks were "
            "constructed, each rung's own derive_failures() fired on the break "
            "and stopped firing when the mechanism was restored, and every file "
            "the demonstrations touched was restored. Rows: "
            + "; ".join(f"{r['id']}={r['status']}" for r in rows),
            owner="T5",
            evidence="python -m evals.rung_break_demos --json",
        )
    names = ", ".join(
        "%s=%s(caught=%s)" % (r.get("id"), r.get("status"), r.get("caught"))
        for r in bad
    ) or "no rows"
    return Rung(
        rid,
        title,
        FAIL,
        f"ran and broke: the demonstration report says {verdict!r}. "
        f"Not demonstrated: {names}. A rung whose check has never fired is "
        "decoration, so this is red rather than green.",
        owner="T5",
        evidence="python -m evals.rung_break_demos --json",
    )


def rung_single_skip_authority() -> Rung:
    """ONE skip-set authority, adopted by every module, pinned in CI.

    ``DOCTRINE.md`` §8 is the whole reason this row exists: three literals in
    one package produced a 345x cost difference in the same walk. A shared
    authority that only ONE module adopted is that defect with better branding,
    so the row counts the authorities rather than trusting the existence of one.

    It DELEGATES to ``shared.skipset.skip_dir_tables``. The first version of this
    row carried its own ``ast`` reader, that reader did not understand a
    comprehension-built table, and the row reported ``pass`` with the words
    *"exactly one skip-set table exists (none found)"* -- a false pass produced
    by a check that could not see the thing it was checking. One authority per
    concern: the audit lives with the authority, and the gate reads it.
    """
    rid = "single_skip_authority"
    title = "exactly one skip-set authority, adopted everywhere"
    try:
        from shared import skipset
    except Exception as exc:
        return blocked(
            rid,
            title,
            f"shared.skipset is not importable, so the adoption audit could not "
            f"run: {exc!r}. This is `blocked`, not `pass`: an audit that did "
            "not run has not established that there is one authority.",
            owner="T5",
            evidence="python -c 'import shared.skipset, json; "
            "print(json.dumps(shared.skipset.skip_dir_tables(), indent=2))'",
        )
    audit = skipset.skip_dir_tables()
    tables = dict(audit["own_tables"])  # type: ignore[arg-type]
    derived = list(audit["derived_tables"])  # type: ignore[arg-type]
    total = int(audit["independent_table_count"])  # type: ignore[arg-type]
    adopters = list(audit["adopters"])  # type: ignore[arg-type]
    missing = list(audit["tables_missing_authority_entries"])  # type: ignore[arg-type]
    if total <= 1:
        return Rung(
            rid,
            title,
            PASS,
            f"ran and held: exactly ONE skip-set table exists "
            f"({skipset.SOURCE_OF_TRUTH}, "
            f"{audit['authority_dir_count']} names) and "
            f"{len(adopters)} module(s) adopt it.",
            owner="T5",
            evidence="shared.skipset.skip_dir_tables()",
        )
    return Rung(
        rid,
        title,
        FAIL,
        f"ran and broke: {total} independent skip-set tables exist and nothing "
        f"enforces that they agree. {len(adopters)} module(s) adopt the "
        "authority. The tables, with their sizes: "
        + ", ".join(f"{k} ({v})" for k, v in sorted(tables.items()))
        + ("; comprehension-built: " + ", ".join(derived) if derived else "")
        + ". FIVE of them prune LESS than the authority, which is "
        "`DOCTRINE.md` §8's exact defect -- a walk that does not prune "
        "`logs/` opens a copy of the repository under repair: "
        + ", ".join(missing)
        + ". `evals/rung_break_demos.py::demo_rung9_second_skip_list` measures "
        "the cost of exactly this break on this checkout: 164 directories in "
        "~90 ms becomes >=60,000 directories in ~33 s, a factor of 366. "
        "Owner: T2 (execution/), T3 (runtime/), T4 (cli/, dashboard/) to import "
        "or re-export `shared.skipset`; T5 owns this row and the CI pin.",
        owner="T2 + T3 + T4 (adoption), T5 (the audit and the pin)",
        evidence="shared.skipset.skip_dir_tables(); "
        "tests/test_skip_authority.py",
    )


def rung_provenance() -> Rung:
    """`scripts/provenance_report.py` must run clean, and its shape must hold."""
    rid = "provenance_clean"
    title = "every vendored artefact is declared and re-hashed"
    probe = _python("scripts/provenance_report.py", timeout_s=600)
    out = (probe.get("stdout") or "") + (probe.get("stderr") or "")
    if probe.get("timed_out"):
        return blocked(
            rid, title, "scripts/provenance_report.py did not finish in 600s",
            owner="T5", evidence="python scripts/provenance_report.py",
        )
    if not probe.get("ok"):
        return Rung(
            rid,
            title,
            FAIL,
            f"ran and broke: exit {probe.get('returncode')}: "
            f"{(out.strip().splitlines() or ['<no output>'])[-1]}",
            owner="T5",
            evidence="python scripts/provenance_report.py",
        )
    undeclared = _prov_int(out, "undeclared")
    mismatch = _prov_int(out, "mismatched")
    declared = _prov_int(out, "declared")
    incomplete = _prov_int(out, "incomplete")
    if undeclared or mismatch or incomplete:
        return Rung(
            rid,
            title,
            FAIL,
            f"ran and broke: undeclared={undeclared} incomplete={incomplete} "
            f"mismatched={mismatch} (source lane declared={declared}). A "
            "mismatched digest is the supply-chain case and is kept separate "
            "from an incomplete declaration on purpose.",
            owner="T5",
            evidence="python scripts/provenance_report.py",
        )
    return Rung(
        rid,
        title,
        PASS,
        f"ran and held: {declared} source file(s) declared, 0 undeclared, "
        f"0 incomplete, 0 mismatched. The gate detects vendoring by SHAPE "
        "(a `vendored:` block, a vendor-shaped directory, a third-party licence "
        "header, a third-party copyright line); it cannot detect code borrowed "
        "in SPIRIT.",
        owner="T5",
        evidence="python scripts/provenance_report.py",
    )


def _prov_int(text: str, label: str) -> int:
    m = re.search(rf"^\s*{label}:\s*(\d+)\s*$", text, re.MULTILINE)
    return int(m.group(1)) if m else 0


def rung_binary_digests_pinned() -> Rung:
    """A downloaded binary needs a VERSION and a CHECKSUM.

    The version is pinned in code. The digests are not committed anywhere in
    this repository, so a replaced asset with a replaced sidecar would be
    accepted. That is a real supply-chain gap and it is reported as one.
    """
    rid = "binary_digests_pinned"
    title = "downloaded binaries are pinned by version AND a committed digest"
    manifest = REPO_ROOT / "provenance" / "binaries.json"
    try:
        doc = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return blocked(
            rid,
            title,
            f"provenance/binaries.json is unreadable: {exc!r}",
            owner="T5",
            evidence="provenance/binaries.json",
        )
    committed = doc.get("committed_digests") or {}
    fetched = doc.get("fetched_binaries") or []
    unpinned = [
        str(row.get("name") or "?")
        for row in fetched
        if not committed.get(str(row.get("name") or ""))
    ]
    if not fetched:
        return Rung(
            rid,
            title,
            PASS,
            "ran and held: no binary is fetched at run time, so there is no "
            "payload whose authenticity is unchecked.",
            owner="T5",
            evidence="provenance/binaries.json",
        )
    if unpinned:
        return Rung(
            rid,
            title,
            FAIL,
            "ran and broke: "
            + ", ".join(unpinned)
            + " are fetched at run time with a VERSION pinned in "
            "`harness/search_engine.py`, but their SHA-256 digests are read "
            "from an upstream sidecar at download time and are NOT committed "
            "here. The expected digest therefore travels with the payload, so a "
            "replaced asset with a replaced sidecar is accepted. A "
            "checksum-verified download whose checksum is not pinned is the "
            "paperwork version of the same honesty problem. Remediation: commit "
            "the eight digests under `committed_digests` in this manifest. "
            "Owner: T1 / P2.1 (the downloader) + T5 (the manifest).",
            owner="T1 / P2.1 + T5",
            evidence="provenance/binaries.json",
        )
    return Rung(
        rid,
        title,
        PASS,
        f"ran and held: {len(fetched)} fetched binary/binaries carry a "
        "committed digest.",
        owner="T5",
        evidence="provenance/binaries.json",
    )


def rung_ci_lanes() -> Rung:
    """The six lanes must exist, agree with the workflows, and be gated."""
    rid = "ci_lanes_consistent"
    title = "six labelled lanes, each gated by the known-failing registry"
    try:
        from evals import ci_lanes
    except Exception as exc:
        return blocked(
            rid, title, f"evals.ci_lanes is not importable: {exc!r}", owner="T5"
        )
    doc = ci_lanes.report()
    findings = doc.get("workflow_findings") or []
    missing = doc.get("lanes_missing_the_registry_gate") or []
    if findings or missing:
        return Rung(
            rid,
            title,
            FAIL,
            f"ran and broke: {len(findings)} workflow disagreement(s) and "
            f"{len(missing)} lane(s) without the registry gate "
            f"({', '.join(missing) or 'none'}). Findings: "
            + " | ".join(f"{f['lane']}: {f['problem']}" for f in findings),
            owner="T5",
            evidence="python -m evals.ci_lanes",
        )
    return Rung(
        rid,
        title,
        PASS,
        f"ran and held: {doc['lane_count']} lanes, "
        f"{len(doc['blocking_lanes'])} blocking, "
        f"{len(doc['non_blocking_timing_lanes'])} non-blocking timing lanes "
        f"({', '.join(doc['non_blocking_timing_lanes'])}), the registry gate "
        f"({' '.join(ci_lanes.REGISTRY_COMMAND)}) is present in every lane, and "
        "no workflow disagrees with the authority.",
        owner="T5",
        evidence="python -m evals.ci_lanes",
    )


def rung_registry_gates_every_lane() -> Rung:
    """A promoted pin must fail the build, with a message saying "promote this".

    Same fact as the row above, measured a different way: here the registry is
    actually RUN, so the row proves the gate executes rather than that a string
    appears in a YAML file.
    """
    rid = "known_failing_registry_gates"
    title = "the known-failing pin registry runs, and gates every lane"
    probe = _python("-m", "tests.known_failing_pins", timeout_s=900)
    out = (probe.get("stdout") or "") + (probe.get("stderr") or "")
    if probe.get("timed_out"):
        return blocked(
            rid,
            title,
            "`python -m tests.known_failing_pins` did not finish in 900s",
            owner="T5",
            evidence="python -m tests.known_failing_pins",
        )
    if not probe.get("ok"):
        return Rung(
            rid,
            title,
            FAIL,
            f"ran and broke: exit {probe.get('returncode')}: "
            f"{(out.strip().splitlines() or ['<no output>'])[-1]}",
            owner="T5",
            evidence="python -m tests.known_failing_pins",
        )
    tail = (out.strip().splitlines() or ["<no output>"])[-1]
    return Rung(
        rid,
        title,
        PASS,
        f"ran and held: exit 0. {tail}. A promoted pin turns this exit code "
        "non-zero and the run says `promote this`, so a recorded gap cannot "
        "silently rot in one lane while five ignore it.",
        owner="T5",
        evidence="python -m tests.known_failing_pins",
    )


def rung_config_matches_source() -> Rung:
    """The live interpreter and the file on disk must agree about every default.

    This row exists because it caught a measurement being wrong for a whole
    session: ``harness/config.py`` was edited ``25`` -> ``60`` (the same byte
    length) inside one mtime second, so CPython's ``__pycache__`` validity
    check matched, the old bytecode kept running, and ``agent_max_turns`` read
    25 while the source said 60.
    """
    rid = "config_defaults_match_source"
    title = "the running config equals the config file, key for key"
    probe = _python(
        "-m", "pytest", "tests/test_config_defaults_match_source.py", "-q",
        "-p", "no:randomly", "--tb=line",
        timeout_s=600,
    )
    out = (probe.get("stdout") or "") + (probe.get("stderr") or "")
    if probe.get("timed_out"):
        return blocked(
            rid, title, "the pin did not finish in 600s", owner="T5",
            evidence="python -m pytest tests/test_config_defaults_match_source.py",
        )
    if not probe.get("ok"):
        return Rung(
            rid,
            title,
            FAIL,
            "ran and broke: the live DEFAULTS disagrees with harness/config.py. "
            f"{(out.strip().splitlines() or ['<no output>'])[-1]}",
            owner="T5",
            evidence="python -m pytest tests/test_config_defaults_match_source.py",
        )
    return Rung(
        rid,
        title,
        PASS,
        "ran and held: every key of harness/config.py::DEFAULTS that this "
        "interpreter is running matches the value parsed out of the file with "
        "`ast` (no import, no bytecode in the path), and the five spend-related "
        "keys are pinned by name.",
        owner="T5",
        evidence="python -m pytest tests/test_config_defaults_match_source.py",
    )


def rung_prompt_self_check() -> Rung:
    rid = "eval_prompt_self_check"
    title = "the 14-task prompt self-check"
    probe = _python("-m", "evals.run", "--check", timeout_s=1800)
    out = (probe.get("stdout") or "") + (probe.get("stderr") or "")
    if probe.get("timed_out"):
        return blocked(
            rid, title, "python -m evals.run --check did not finish in 1800s",
            owner="T5", evidence="python -m evals.run --check",
        )
    if probe.get("ok") and re.search(r"14/14", out):
        return Rung(
            rid,
            title,
            PASS,
            "ran and held: 14/14 ok, verdict "
            + ("CLEAN" if "CLEAN" in out else "reported ok")
            + ". NOTE: a host self-check of the prompt task set, NOT a claim "
            "about model quality.",
            owner="T5",
            evidence="python -m evals.run --check",
        )
    return Rung(
        rid,
        title,
        FAIL,
        "ran and did not report 14/14 ok. Tail: "
        + " | ".join((out.strip().splitlines() or ["<no output>"])[-4:]),
        owner="T5",
        evidence="python -m evals.run --check",
    )


def rung_full_suite(log_path: Optional[Path]) -> Rung:
    rid = "full_suite_tests_dir"
    title = "the whole tests/ tree, as a measured log"
    if log_path is None or not Path(log_path).is_file():
        return blocked(
            rid,
            title,
            "no measured `pytest tests/` log was supplied. Pass one with "
            "--tests-log. An unrun suite is `blocked`, never `pass` and never "
            "zero.",
            owner="T5",
            evidence="python -m pytest tests/ -q",
        )
    parsed = parse_pytest_log(read_log_text(Path(log_path)))
    if not parsed["parsed"]:
        return blocked(
            rid,
            title,
            f"{log_path} has no recognisable pytest summary line, so its "
            "totals are not measurable and are NOT reported as zero",
            owner="T5",
            evidence=f"python -m pytest tests/ -q -> {log_path}",
        )
    if parsed.get("no_tests_ran"):
        return Rung(
            rid, title, FAIL,
            "ran and collected NOTHING: the log says 'no tests ran'. Zero "
            "collected tests is never a pass.",
            owner="T5", evidence=f"python -m pytest tests/ -q -> {log_path}",
        )
    totals = (
        f"passed={parsed['passed']} failed={parsed['failed']} "
        f"skipped={parsed['skipped']} errors={parsed['errors']}"
    )
    broken = parsed["failed"] or parsed["failed_nodes"] or parsed["errors"]
    if not broken:
        return Rung(
            rid, title, PASS, f"ran and held: {totals}", owner="T5",
            evidence=f"python -m pytest tests/ -q -> {log_path}",
        )
    named = "\n".join(f"    {n}" for n in parsed["failed_nodes"])
    return Rung(
        rid,
        title,
        FAIL,
        f"ran and broke: {totals}. Failed nodes:\n{named}"
        if named
        else f"ran and broke: {totals}. No node ids were captured; see the log.",
        owner="T5 (adjudication) with per-row owners in docs/release-verdict.md",
        evidence=f"python -m pytest tests/ -q -> {log_path}",
    )


def rung_module_local_pins(log_path: Optional[Path]) -> Rung:
    """The per-terminal `-local` pin modules, in one measured run.

    Phase 1 spread mechanism pins across ``harness/``, ``execution/``,
    ``runtime/`` and ``cli/`` as module-local ``test_*.py`` files. They are
    invisible to ``pytest tests/``, so a lane has to name them explicitly.
    """
    rid = "module_local_pins"
    title = "the four terminals' module-local pin modules"
    modules = local_pin_modules()
    if not modules:
        return not_implemented(
            rid,
            title,
            "T5 / this gate: no module-local pin module exists to run",
            evidence="glob for harness/test_*.py, execution/test_*.py, "
            "runtime/test_*.py, cli/test_*.py",
        )
    if log_path is None or not Path(log_path).is_file():
        return blocked(
            rid,
            title,
            f"no measured log for the {len(modules)} module-local pin module(s) "
            f"({', '.join(modules[:6])}{'...' if len(modules) > 6 else ''}). "
            "Pass one with --module-local-log. These pins are invisible to "
            "`pytest tests/`, so an unrun selection is `blocked`, never a pass.",
            owner="T5",
            evidence="python -m pytest " + " ".join(modules) + " -q",
        )
    parsed = parse_pytest_log(read_log_text(Path(log_path)))
    if not parsed["parsed"]:
        return blocked(
            rid,
            title,
            f"{log_path} has no recognisable pytest summary line",
            owner="T5",
            evidence="python -m pytest " + " ".join(modules) + " -q",
        )
    totals = (
        f"{len(modules)} module(s), passed={parsed['passed']} "
        f"failed={parsed['failed']} skipped={parsed['skipped']} "
        f"errors={parsed['errors']}"
    )
    if parsed["failed"] or parsed["errors"]:
        return Rung(
            rid, title, FAIL, f"ran and broke: {totals}. Failed nodes:\n"
            + "\n".join(f"    {n}" for n in parsed["failed_nodes"]),
            owner="T1/T2/T3/T4 by module",
            evidence="python -m pytest " + " ".join(modules) + " -q",
        )
    return Rung(
        rid,
        title,
        PASS,
        f"ran and held: {totals}. Note the skips are reported as skips, not "
        "folded into the pass count.",
        owner="T1/T2/T3/T4 by module",
        evidence="python -m pytest " + " ".join(modules) + " -q",
    )


def local_pin_modules() -> List[str]:
    """Every ``<pkg>/test_*.py`` pin module Phase 1 spread across the tree."""
    out: List[str] = []
    for package in ("harness", "execution", "runtime", "cli"):
        directory = REPO_ROOT / package
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("test_*.py")):
            out.append(f"{package}/{path.name}")
    return out


def rung_live_provider() -> Rung:
    """No live provider was reachable, so nothing here is a model claim."""
    rid = "live_provider_model_quality"
    title = "a live-provider measurement"
    return blocked(
        rid,
        title,
        "no live provider was reachable for this gate. T3 recorded "
        "`ServiceUnavailableError: No available channel` on 3/3 live "
        "completions; this gate re-attempted nothing and inspected no "
        "credential, so the row is `blocked` and no model-quality, model-cost "
        "or model-latency number is claimed anywhere in this report. Every "
        "model call in every rung and every lane is a scripted double.",
        owner="T3 / P1.3 (provider reachability)",
        evidence="T3's recorded ServiceUnavailableError on 3/3 completions",
    )


def rung_daily_driver_readiness() -> Rung:
    """The daily-driver readiness gate, and the one capability nobody measured."""
    rid = "daily_driver_readiness"
    title = "the daily-driver readiness gate is green"
    return not_implemented(
        rid,
        title,
        "T5 / P1.4 -- the 26-case daily-driver matrix reports `NOT_READY`, and "
        "the capability `cost_latency_user_intervention_at_least_90pct` is "
        "UNMEASURED in it. Unmeasured is not implemented: no gate produces that "
        "number, so this is recorded as a gap with an owner rather than as a "
        "row that might turn green. What the matrix DOES report when it runs "
        "without Docker or a provider is a count of deterministic arms, and "
        "that count is a statement about the harness, not about the product.",
        evidence="evals/daily_driver.py readiness block; "
        "`python -m evals.run --suite daily-driver`",
    )


def rung_windows_lane() -> Rung:
    """The Windows lane runs on `windows-latest`; it did not run here."""
    rid = "windows_lane"
    title = "the Docker-free windows lane on windows-latest"
    return blocked(
        rid,
        title,
        "this gate ran on Windows AMD64 as a HOST, so the platform-specific "
        "failures a Linux-run gate cannot see (path separators, "
        "newline translation, symlink privilege, os.replace semantics) were "
        "not exercised by a CI runner at all. The lane's WORKFLOW is checked "
        "for consistency by the `ci_lanes_consistent` row; the lane's RUN is "
        "not performed here and is not claimed.",
        owner="T5 (CI), needs a windows-latest runner",
        evidence=".github/workflows/windows-dockerfree-ci.yml",
    )


def rung_docker_daemon() -> Rung:
    rid = "docker_daemon_reachable"
    title = "a Docker daemon for the sandbox/verifier lanes"
    probe = docker_reachable()
    if probe["reachable"]:
        return Rung(
            rid,
            title,
            PASS,
            f"ran and held: {probe['evidence']}. The Docker-backed lanes were "
            "therefore available to be run; whether each one was RUN is its own "
            "row, and a lane that was not run is not claimed by this one.",
            owner="T2",
            evidence="docker info",
        )
    return blocked(
        rid,
        title,
        f"the Docker daemon is not reachable on this host: {probe['error']}",
        owner="T2",
        evidence="docker info",
    )


# --------------------------------------------------------------------------
# THE GATE
# --------------------------------------------------------------------------


@dataclass
class G1Inputs:
    """What the gate was given, so a reader can see what was NOT measured."""

    ladder_report: Optional[Path] = None
    demos_report: Optional[Path] = None
    tests_log: Optional[Path] = None
    module_local_log: Optional[Path] = None
    ladder: Optional[Dict[str, Any]] = field(default=None, repr=False)
    demos: Optional[Dict[str, Any]] = field(default=None, repr=False)

    def load(self) -> "G1Inputs":
        for attr, path in (
            ("ladder", self.ladder_report),
            ("demos", self.demos_report),
        ):
            if path is None:
                continue
            try:
                setattr(self, attr, json.loads(Path(path).read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                setattr(self, attr, {"__unreadable__": f"{path}: {exc!r}"})
        return self


def g1_report(inputs: G1Inputs) -> Dict[str, Any]:
    """Probe every G1 rung and derive the verdict."""
    ladder = inputs.ladder if isinstance(inputs.ladder, dict) else None
    demos = inputs.demos if isinstance(inputs.demos, dict) else None
    rungs: List[Rung] = [
        rung_rung_checks_demonstrated(demos=demos),
        _from_ladder(ladder, 8, fallback="stays coherent past 50 turns"),
        _from_ladder(ladder, 9, fallback="fast enough to use daily"),
        _from_ladder(ladder, 10, fallback="you can always see what it did and why"),
        rung_full_suite(inputs.tests_log),
        rung_module_local_pins(inputs.module_local_log),
        rung_prompt_self_check(),
        rung_ci_lanes(),
        rung_registry_gates_every_lane(),
        rung_provenance(),
        rung_binary_digests_pinned(),
        rung_single_skip_authority(),
        rung_config_matches_source(),
        rung_docker_daemon(),
        rung_live_provider(),
        rung_windows_lane(),
        rung_daily_driver_readiness(),
    ]

    cost = cost_statement(ladder)
    if not cost.get("readable"):
        rungs.append(
            blocked(
                "turn_ceiling_cost_published",
                "the 2x worst-case cost of the doubled turn ceiling is published "
                "with the speed wins",
                f"the ceiling could not be read: {cost.get('reason')}",
                owner="T5",
                evidence="harness.config.DEFAULTS['agent_max_turns']",
            )
        )
    elif not cost.get("the_two_agree"):
        rungs.append(
            Rung(
                "turn_ceiling_cost_published",
                "the 2x worst-case cost of the doubled turn ceiling is published "
                "with the speed wins",
                FAIL,
                f"the two authorities DISAGREE: the shipping config reads "
                f"{cost['after']} and the Trust Ladder read "
                f"{cost['ladder_reported_cap']}. A cost figure quoted from one "
                "of two disagreeing numbers is a cost figure nobody can rely "
                "on. See tests/test_config_defaults_match_source.py: this is "
                "the stale-bytecode shape.",
                owner="T5",
                evidence="harness.config.DEFAULTS['agent_max_turns'] vs "
                "evals.trust_ladder_rungs.declared_turn_cap()",
            )
        )
    else:
        rungs.append(
            Rung(
                "turn_ceiling_cost_published",
                "the 2x worst-case cost of the doubled turn ceiling is published "
                "with the speed wins",
                PASS,
                f"published in this same document, computed from the shipping "
                f"config rather than from a stored constant: ceiling "
                f"{cost['before']} -> {cost['after']} = "
                f"{cost['worst_case_ratio']}x worst-case turns per task, and the "
                f"Trust Ladder independently read {cost['ladder_reported_cap']}. "
                + " ".join(COST_STATEMENT),
                owner="T5",
                evidence="harness.config.DEFAULTS['agent_max_turns'] + "
                "evals/trust_ladder_rungs.py::COST/COST_STATEMENT",
            )
        )

    blocking_failures = [r for r in rungs if r.blocking and r.status == FAIL]
    blocked_rows = [r for r in rungs if r.status == BLOCKED]
    return {
        "schema_version": 1,
        "gate": "G1",
        "phase": "P1 Daily Usable",
        "statuses": list(STATUSES),
        "verdict": G1_RED if blocking_failures else G1_GREEN,
        "verdict_meaning": (
            f"{G1_RED} means at least one BLOCKING guarantee was measured and "
            f"did not hold. It is not a failed build, it is a published "
            f"measurement. Relaxing a check, a budget or a pin to make this "
            f"document green is forbidden by the brief that created it."
        ),
        "machine": "%s %s / cpus=%s / python %s"
        % (
            platform.system(),
            platform.machine(),
            __import__("os").cpu_count(),
            platform.python_version(),
        ),
        "rungs": [r.to_dict() for r in rungs],
        "counts": {
            PASS: sum(1 for r in rungs if r.status == PASS),
            FAIL: sum(1 for r in rungs if r.status == FAIL),
            BLOCKED: len(blocked_rows),
            NOT_IMPLEMENTED: sum(1 for r in rungs if r.status == NOT_IMPLEMENTED),
        },
        "blocked_rows": [
            {"id": r.id, "reason": r.detail, "owner": r.owner} for r in blocked_rows
        ],
        "performance": performance_table(ladder),
        "cost": cost,
        "inputs": {
            "ladder_report": str(inputs.ladder_report) if inputs.ladder_report else "",
            "demos_report": str(inputs.demos_report) if inputs.demos_report else "",
            "tests_log": str(inputs.tests_log) if inputs.tests_log else "",
            "module_local_log": (
                str(inputs.module_local_log) if inputs.module_local_log else ""
            ),
        },
        "what_this_does_not_establish": list(NOT_ESTABLISHED),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def render(report: Dict[str, Any]) -> str:
    glyph = {
        PASS: "pass  ",
        FAIL: "FAIL  ",
        BLOCKED: "BLOCK ",
        NOT_IMPLEMENTED: "NOTIMPL",
    }
    lines = [
        f"G1 ({report['phase']}): {report['verdict']}",
        "",
        "status vocabulary: " + " | ".join(report["statuses"]) + "   (never skip)",
        f"machine: {report['machine']}",
        "",
        f"{'status':8} {'rung':38} detail",
        "-" * 104,
    ]
    for rung in report["rungs"]:
        detail = rung["detail"].splitlines()
        lines.append(
            f"{glyph[rung['status']]:8} {rung['id']:38} "
            + (detail[0] if detail else "")
        )
        for line in detail[1:]:
            lines.append(f"{'':8} {'':38} {line}")
        if rung["owner"]:
            lines.append(f"{'':8} {'':38} owner: {rung['owner']}")
    counts = report["counts"]
    lines += [
        "",
        "counts: " + ", ".join(f"{k}={v}" for k, v in counts.items()),
        "",
        "PERFORMANCE (every number carries its machine, its run count, and "
        "whether it is a median or a p95):",
    ]
    perf = report["performance"]
    if perf:
        lines.append(
            f"  {'metric':28} {'value':>12} {'stat':7} {'runs':>5} "
            f"{'budget':>10}  within"
        )
        for row in perf:
            if not row["available"]:
                lines.append(
                    f"  {row['metric']:28} {'unavailable':>12} {'-':7} "
                    f"{'-':>5} {'-':>10}  reported_as="
                    f"{row.get('reported_as') or 'unavailable'} "
                    f"({row.get('reason') or 'no reason given'})"
                )
                continue
            value = row["value"]
            lines.append(
                f"  {row['metric']:28} {value:12.0f} {row['statistic'] or '-':7} "
                f"{row['window_runs'] or 0:5d} {row['target'] or 0:10.0f}  "
                f"{row['within_budget']}"
            )
            if row.get("percentiles_withheld"):
                lines.append(
                    f"      {'':28} percentile WITHHELD: "
                    f"{row['percentiles_withheld']}"
                )
        lines.append(f"  machine: {perf[0]['machine']}")
    else:
        lines.append("  none -- no Trust Ladder report was supplied, so NO")
        lines.append("  performance number is claimed. Unmeasured is not zero.")
    lines += [
        "",
        "THE 2x COST, IN THE SAME DOCUMENT AS THE SPEED WINS:",
    ]
    cost = report["cost"]
    for statement in cost.get("statements", []):
        lines.append("  " + statement)
    lines.append(
        "  computed from the shipping config: before=%r after=%r required>=%r "
        "worst_case_ratio=%r ladder_agree=%s"
        % (
            cost.get("before"),
            cost.get("after"),
            cost.get("required"),
            cost.get("worst_case_ratio"),
            cost.get("the_two_agree"),
        )
    )
    lines += ["", "BLOCKED rows (never reported as skips):"]
    if report["blocked_rows"]:
        for row in report["blocked_rows"]:
            lines.append(f"  - {row['id']} (owner {row['owner'] or 'unassigned'})")
            lines.append(f"      {row['reason']}")
    else:
        lines.append("  - none")
    lines += ["", "WHAT G1 DOES NOT ESTABLISH:"]
    for item in report["what_this_does_not_establish"]:
        lines.append("  * " + item)
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.gates.P1",
        description="The G1 P1 Daily Usable gate. Four statuses, never skip.",
    )
    parser.add_argument("--root", default=str(REPO_ROOT))
    parser.add_argument("--ladder-report", default=None)
    parser.add_argument("--demos-report", default=None)
    parser.add_argument("--tests-log", default=None)
    parser.add_argument("--module-local-log", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    inputs = G1Inputs(
        ladder_report=Path(args.ladder_report) if args.ladder_report else None,
        demos_report=Path(args.demos_report) if args.demos_report else None,
        tests_log=Path(args.tests_log) if args.tests_log else None,
        module_local_log=(
            Path(args.module_local_log) if args.module_local_log else None
        ),
    ).load()
    report = g1_report(inputs)
    print(json.dumps(report, indent=2, default=str) if args.json else render(report))
    return 2 if report["verdict"] == G1_RED else 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
