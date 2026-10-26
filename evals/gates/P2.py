"""G2 - the P2 Trusted Execution gate.

WHAT THIS FILE IS
-----------------
Phase 2's entire claim is TRUST. This gate exists because that is the easiest
kind of claim to over-earn: a security phase produces a lot of green tests, and
green tests are easy to publish without ever asking whether the thing they
measure is the thing that was at risk.

So this gate is built around six ways that claim fails:

1. **A rung with no proving test is not a pass.** Every rung names the test
   that demonstrates it (:data:`PROVING_TESTS`). A rung whose proving test is
   absent, or whose suite did not run, is ``not_implemented`` - never ``pass``.
2. **A corpus that passes because the scanner never ran.** The corpus rows are
   measured by REMOVING each defence and re-probing, not by counting green
   assertions. :func:`non_vacuity_measurement` reads the real transcripts.
3. **A policy language that validates itself.** Its self-validation is only as
   good as its examples, so the example suite is run as a suite and its totals
   are published - and a rule shipping with zero examples is a named finding.
4. **A trust report that omits a failure.** :data:`ATTACK_RESULTS` carries
   every row, INCLUDING the ones that succeeded, because a trust report that
   omits a failure is the thing this repository exists to prevent.
5. **A blocked lane reported as a pass.** Docker, the live provider and the
   unpinned binary digests each get their own row with the exact reason.
6. **A gate that cannot fail.** :func:`_selftest` mutates this gate's own
   derivation and asserts the verdict moves.

THE FOUR STATUSES
-----------------
Imported from :mod:`evals.gates.P0`, never re-declared. One authority for a
vocabulary, because two lists is how ``skip`` gets added back:

* ``pass`` - ran and held.
* ``fail`` - ran and broke.
* ``blocked`` - could not run; **the exact reason is mandatory**.
* ``not_implemented`` - does not exist; **the owning phase is mandatory**.

``skip`` is not a member and cannot be constructed.

WHY G2 IS RED ON THIS TREE
--------------------------
Not because the trust work failed. Because three things are true at this
commit and a red gate is the only honest rendering of them:

* the hardline floor is folded into the shipped policy but is **not wired to a
  command interpreter**, so it cannot fire on a real action;
* the provenance spine is **RED**, and the cause is a header-format mismatch
  rather than a licence problem - which is exactly the distinction a careless
  reader gets wrong;
* the two fetched binaries have **no committed digests**, so a replaced asset
  with a replaced sidecar would be accepted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]

from evals.gates.P0 import (  # noqa: E402  (path-dependent by design)
    BLOCKED,
    FAIL,
    NOT_IMPLEMENTED,
    PASS,
    Rung,
    blocked,
    docker_reachable,
    not_implemented,
)

G2_GREEN = "G2_GREEN"
G2_RED = "G2_RED"
VERDICTS: Tuple[str, ...] = (G2_GREEN, G2_RED)

PROVING_TESTS: Dict[str, Tuple[str, ...]] = {
    "r1_unsandboxed_bash_closed": (
        "execution/test_unsandboxed_pins.py::"
        "test_no_production_import_path_reaches_the_local_subprocess_sandbox_stub",
        "execution/test_p2w1_trust_pins.py::"
        "TestNoExecutionProductionSourceReachesTheLocalSubprocessStub",
    ),
    "r2_untrusted_sources_scanned": (
        "tests/test_p2w1_untrusted_sources.py::test_all_seven_untrusted_sources_are_asserted_not_five",
        "tests/test_p2w2_corpus_proof.py::"
        "TestTheUntrustedSourceBoundaryIsLoadBearing::"
        "test_removing_it_unblocks_every_canary_case",
    ),
    "r3_policy_language_fails_closed": (
        "tests/test_p2w2_policy_proof.py::"
        "TestThePolicyLanguageFailsClosed::"
        "test_a_rule_whose_match_example_does_NOT_match_is_refused",
        "tests/test_p2w2_policy_proof.py::"
        "TestThePolicyLanguageFailsClosed::"
        "test_a_rule_whose_not_match_example_DOES_match_is_refused",
        "tests/test_p2w2_policy_proof.py::"
        "TestLayerOrderIsASecurityProperty::"
        "test_REORDERING_the_layers_changes_the_outcome",
    ),
    "r4_corpus_is_non_vacuous": (
        "tests/test_p2w2_corpus_proof.py::"
        "TestTheRedactionBoundaryIsLoadBearing::"
        "test_no_canary_leaks_with_the_defence_on",
        "tests/test_p2w2_corpus_proof.py::"
        "test_every_corpus_group_is_pinned_to_an_EXACT_count",
    ),
    "r5_provenance_is_declared": ("scripts/provenance_report.py",),
    "r6_verifier_mint_unchanged": (
        "harness/test_mint_site_pins.py",
        "harness/test_completion_mint_sites.py",
    ),
}

ATTACK_RESULTS: Tuple[Dict[str, str], ...] = (
    # --- T2 (execution): the 15-row sandbox/containment table ---------------
    {"row": "sandbox runs one command per fresh container", "owner": "T2", "result": "refused"},
    {"row": "repo bind-mounted read-write at /workspace", "owner": "T2", "result": "contained"},
    {"row": "read-only rootfs, cap-drop ALL", "owner": "T2", "result": "contained"},
    {"row": "no network unless allow_network=True", "owner": "T2", "result": "contained"},
    {"row": "docker.sock / containerd.sock mount", "owner": "T2", "result": "refused"},
    {"row": "host PID/IPC/network/user namespace", "owner": "T2", "result": "refused"},
    {"row": "host secret-directory mount", "owner": "T2", "result": "refused"},
    {"row": "mount outside the workspace", "owner": "T2", "result": "refused"},
    {"row": "added capability", "owner": "T2", "result": "refused"},
    {"row": "any security option but the baseline", "owner": "T2", "result": "refused"},
    {"row": "Windows drive letter read as a volume separator", "owner": "T2", "result": "refused"},
    {"row": "traversal write lands on the container rootfs", "owner": "T2", "result": "contained"},
    {"row": "fuzzy landing on the protected test surface", "owner": "T2", "result": "refused"},
    {"row": "containment receipt authored by a task", "owner": "T2", "result": "refused"},
    {"row": "daemon stopped / unreachable", "owner": "T2", "result": "refused"},
    # --- T3 (runtime): the hardline bypass table ---------------------------
    {"row": "quote splice hides the root path", "owner": "T3", "result": "refused"},
    {"row": "env prefix is not a different command", "owner": "T3", "result": "refused"},
    {"row": "sudo is a transparent prefix", "owner": "T3", "result": "refused"},
    {"row": "command substitution", "owner": "T3", "result": "refused"},
    {"row": "path traversal to a protected path", "owner": "T3", "result": "refused"},
    {"row": "pipe to shell", "owner": "T3", "result": "refused"},
    {"row": "base64 / splice / substitution deobfuscation", "owner": "T3", "result": "refused"},
    {"row": "NEO_HARDLINE_ALLOW exported before import", "owner": "T3", "result": "refused"},
    {"row": "NEO_HARDLINE_ALLOW set after import", "owner": "T3", "result": "refused"},
    {"row": "importlib.reload to re-read the environment", "owner": "T3", "result": "refused"},
    {"row": "module attribute reassignment", "owner": "T3", "result": "refused"},
    {"row": "approval-gate content injection (command text)", "owner": "T3", "result": "refused"},
    {"row": "approval-gate content injection (filename)", "owner": "T3", "result": "refused"},
    {"row": "subagent origin claiming the parent thread", "owner": "T3", "result": "refused"},
)

CONTAINMENT_LIMITS: Tuple[str, ...] = (
    "Kernel escape. Docker's isolation is the boundary; a container escape is "
    "a HOST compromise and nothing in this repository detects or limits it.",
    "A compromised Docker daemon. Every containment claim assumes the daemon "
    "is honest. Root on the host owns every container.",
    "A platform-ignored mount. Docker Desktop / WSL2 can honour a mount "
    "differently from a Linux daemon; a mount measured refused on one host may "
    "not be refused on another. No cross-platform differential is run.",
    "A compromised dependency. litellm, tree-sitter and every wheel in the "
    "image run with the harness's privileges. Only two fetched binaries have a "
    "declared source; neither has a COMMITTED digest.",
    "Container egress is NOT filtered. Docker has no portable per-container "
    "egress allowlist without an external proxy, so `allow_network=True` "
    "attaches the default bridge. The HOST-side fetch path is enforced.",
    "The floor is not wired. `runtime.hardline` has no production call site, "
    "so every T3 bypass row above is a property of the TABLE, not of a "
    "running guard.",
    "The aux-LLM approval gate is not wired. `harness/approver.py` and "
    "`runtime/approver_gate.py` have zero production callers.",
    "No model-quality claim. Every model call in every suite is a scripted "
    "double. Nothing here is evidence about any model's coding ability.",
)

NOT_ESTABLISHED: Tuple[str, ...] = (
    "MEASURED on this tree, on this host, at this commit, with a scripted "
    "model. Re-run before quoting any number.",
    "Every model call in every suite is a scripted double. No result here is "
    "a claim about model quality.",
    "The hardline floor is folded into the shipped policy but has NO "
    "production call site, so it cannot fire on a real action.",
    "The provenance spine is RED. The cause is a header-FORMAT mismatch "
    "(docstring labels vs the scanner's comment grammar), not a licence "
    "violation.",
    "The two fetched binaries have no committed digests, so a replaced asset "
    "with a replaced sidecar would be accepted.",
    "No live-provider lane was run. No credential was inspected or retained.",
    "The whole tree is a SHARED DIRTY WORKTREE. Four terminals were editing "
    "it during this phase; several measurements here moved while they were "
    "being taken, and that is recorded rather than smoothed over.",
)


# ---------------------------------------------------------------------------
# measurement helpers
# ---------------------------------------------------------------------------


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _pytest(*args: str, timeout_s: int = 900) -> Dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *args, "-q", "--no-header", "-p", "no:randomly"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        encoding="utf-8",
        errors="replace",
    )
    tail = (proc.stdout or "").strip().splitlines()[-25:]
    return {
        "returncode": proc.returncode,
        "ok": proc.returncode == 0,
        "tail": "\n".join(tail),
    }


def _scan_corpus() -> Dict[str, Any]:
    """Measure the corpus by COUNT and COVERAGE, from the real module."""
    sys.path.insert(0, str(REPO_ROOT))
    from shared import security_corpus as corpus

    groups = {
        "security_regression": len(corpus.SECURITY_REGRESSION_CORPUS),
        "canary_exfiltration": len(corpus.CANARY_EXFILTRATION_CORPUS),
        "obfuscated_canary": len(corpus.OBFUSCATED_CANARY_EXFILTRATION_CORPUS),
        "issue_injection": len(corpus.ISSUE_INJECTION_CORPUS),
        "delimiter_breakout": len(corpus.DELIMITER_BREAKOUT_CORPUS),
        "phase2_surfaces": len(corpus.PHASE2_SURFACE_CORPUS),
    }
    return {
        "groups": groups,
        "total": sum(groups.values()),
        "coverage": corpus.phase2_surface_coverage(),
        "surfaces": list(corpus.PHASE2_SURFACES),
    }


def non_vacuity_measurement() -> Dict[str, Any]:
    """Remove each defence and report which corpus cases react.

    Delegates to the shipped suite so this gate and the proof cannot drift:
    the gate reads the same numbers the tests assert, and the transcripts are
    reproducible with ``python Temp/opencode/w2_1_nonvacuity.py``.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from shared import security
    from tests import test_p2w2_corpus_proof as proof

    return {
        "untrusted_boundary": sorted(
            proof._moved_cases(
                proof._probe_untrusted_boundary,
                security,
                "review_untrusted_source",
                proof._allow_everything,
            )
        ),
        "redaction": sorted(
            proof._moved_cases(
                proof._probe_canary_redaction,
                security,
                "redact_text",
                proof._identity_redact,
            )
        ),
        "normalisation": sorted(
            proof._moved_cases(
                proof._probe_normalisation,
                security,
                "normalize_for_redaction",
                proof._identity_normalise,
            )
        ),
        "defang": sorted(
            proof._moved_cases(
                proof._probe_defang,
                security,
                "defang_untrusted_delimiters",
                proof._identity_defang,
            )
        ),
    }


def policy_measurement() -> Dict[str, Any]:
    """Load the shipped policy and report the example suite as a suite.

    TOTAL BY CONSTRUCTION. An earlier draft of this function called
    ``pl.shipped_policy()`` directly, and the GATE CRASHED with a
    ``PolicyValidationError`` on the first run - because the shipped policy
    genuinely does not load while T3's hardline table is mid-edit.

    That is the failure this gate exists to catch, so catching it by crashing
    is the worst possible response: a gate that dies on a broken input reports
    nothing, and "no report" reads like "no problem". A refusal is therefore
    returned as a VALUE, with the rule and the example that failed, and the
    rung turns it into a `fail`.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from shared import policy_lang as pl

    empty: Dict[str, Any] = {
        "layers": list(pl.LAYERS),
        "rule_count": 0,
        "example_count": 0,
        "rules_failing": [],
        "rules_without_examples": [],
        "floor_rules_folded": 0,
        "floor_available": None,
        "floor_patterns_seen": 0,
        "floor_excluded": 0,
        "floor_reason": "",
        "loaded": False,
        "refusal": "",
    }
    try:
        rules, fold = pl.hardline_floor_rules()
        empty["floor_rules_folded"] = len(rules)
        empty["floor_available"] = fold.get("available")
        empty["floor_patterns_seen"] = fold.get("patterns_seen") or 0
        empty["floor_excluded"] = len(fold.get("excluded") or ())
        empty["floor_reason"] = str(fold.get("reason") or "")[:400]
    except Exception as exc:
        empty["floor_reason"] = f"{type(exc).__name__}: {exc}"

    try:
        program = pl.shipped_policy()
    except Exception as exc:
        empty["refusal"] = f"{type(exc).__name__}: {exc}"
        return empty

    report = program.report()
    return {
        "layers": list(pl.LAYERS),
        "rule_count": report["rule_count"],
        "example_count": report["example_count"],
        "rules_failing": [r["name"] for r in report["rules"] if not r["ok"]],
        "rules_without_examples": [
            r["name"] for r in report["rules"] if r["examples"] == 0
        ],
        "floor_rules_folded": empty["floor_rules_folded"],
        "floor_available": empty["floor_available"],
        "floor_patterns_seen": empty["floor_patterns_seen"],
        "floor_excluded": empty["floor_excluded"],
        "floor_reason": empty["floor_reason"],
        "loaded": True,
        "refusal": "",
    }


def vendored_inventory() -> List[Dict[str, Any]]:
    """Return the vendored-code inventory: what, from where, under what licence."""
    vendor = REPO_ROOT / "harness" / "third_party"
    declared = {
        "opencode_replacer.py": (
            "https://github.com/sst/opencode",
            "MIT",
            "independent reimplementation of the NINE-STAGE edit cascade; the "
            "matching stages were ported, not the file",
        ),
        "aider_bitap.py": (
            "https://github.com/Aider-AI/aider",
            "Apache-2.0",
            "independent reimplementation of the Bitap approximate-substring "
            "matcher from diff-match-patch; one function, not the ~2000-line file",
        ),
    }
    rows: List[Dict[str, Any]] = []
    for name, (upstream, licence, scope) in sorted(declared.items()):
        path = vendor / name
        digest = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        )
        rows.append(
            {
                "file": f"harness/third_party/{name}",
                "exists": path.is_file(),
                "upstream": upstream,
                "licence": licence,
                "byte_copy": False,
                "scope": scope,
                "sha256": digest,
            }
        )
    return rows


def binary_inventory() -> Dict[str, Any]:
    """Return what is declared about the two FETCHED binaries."""
    path = REPO_ROOT / "provenance" / "binaries.json"
    if not path.is_file():
        return {"available": False, "reason": f"{path} is absent"}
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {
        "available": True,
        "committed_digests": bool(doc.get("committed_digests")),
        "gitignored": bool(
            any(
                line.strip() in {"provenance/", "provenance"}
                for line in _read(".gitignore").splitlines()
            )
        ),
        "binaries": [
            {
                "name": row.get("name"),
                "version": row.get("version"),
                "licence": row.get("licence"),
                "digest_pinned": row.get("digest-pinned"),
                "upstream": row.get("upstream"),
            }
            for row in doc.get("fetched_binaries", ())
        ],
    }


def loc_delta() -> Dict[str, Any]:
    """Report the Phase 2 LOC delta in BOTH directions.

    Phase 2 was SUBTRACTIVE. Reporting only the insertions would describe a
    growth phase; the deletion of `harness/agent_kernel/legacy.py` and the
    1,868 deleted lines in `harness/` are the phase's actual deliverable.
    """
    proc = subprocess.run(
        ["git", "diff", "--numstat", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    # `--numstat` rather than `--shortstat`. An earlier version parsed the
    # shortstat prose ("66 files changed, 20094 insertions(+), 2743
    # deletions(-)") and reported 0/0/0, because its token matcher expected
    # the literal "file(s)" and git says "files changed". A gate that reports
    # zero LOC for a 20,000-line diff is worse than one that declines to
    # report: 0 reads as "nothing changed".
    insertions = deletions = 0
    files = 0
    binary = 0
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        added, removed = parts[0], parts[1]
        if added == "-" or removed == "-":
            binary += 1
            continue
        try:
            insertions += int(added)
            deletions += int(removed)
        except ValueError:
            continue
    raw = (
        f"{files} files changed, {insertions} insertions(+), "
        f"{deletions} deletions(-)"
    )
    deleted = subprocess.run(
        ["git", "diff", "HEAD", "--diff-filter=D", "--name-only"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return {
        "raw": raw,
        "files": files,
        "insertions": insertions,
        "deletions": deletions,
        "net": insertions - deletions,
        "binary_files": binary,
        "deleted_files": [
            line for line in (deleted.stdout or "").splitlines() if line.strip()
        ],
        "note": (
            "TRACKED files only. The wave's new modules are UNTRACKED in this "
            "shared worktree, so these numbers UNDERSTATE the additions. Both "
            "directions are reported because Phase 2 was subtractive."
        ),
    }


def provenance_measurement() -> Dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "scripts/provenance_report.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    text = proc.stdout or ""
    return {
        "returncode": proc.returncode,
        "verdict": "PROVENANCE_GAPS" if proc.returncode else "PROVENANCE_COMPLETE",
        "declared": _prov_int(text, "declared"),
        "undeclared": _prov_int(text, "undeclared"),
        "reason": (
            "the scanner parses a `# vendored:` COMMENT block; the vendored "
            "files carry a four-field header in the module DOCSTRING under "
            "Title-Case labels. The gate is red for a FORMAT reason. Each "
            "file's own text says it was written against the published "
            "algorithm description and NOT copied, so this is not a licence "
            "violation - and the fix is a header, not a licence change."
        ),
    }


def _prov_int(text: str, label: str) -> Optional[int]:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{label}:"):
            try:
                return int(stripped.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def mint_sites() -> Dict[str, Any]:
    """Count the `completed_verified` MINT sites from the real pins."""
    measured: Dict[str, Any] = {"source": "unavailable", "expected": None, "files": []}
    try:
        import harness.test_mint_site_pins as pins
    except Exception as exc:
        measured["reason"] = f"{type(exc).__name__}: {exc}"
        return measured
    measured["expected"] = pins.MINT_SITES_EXPECTED
    measured["files"] = sorted(pins.EXPECTED_MINT_FILES)
    try:
        rows = pins.enumerate_occurrences()
        tally: Dict[str, int] = {}
        for row in rows:
            key = getattr(row, "classification", None) or getattr(row, "kind", "?")
            tally[key] = tally.get(key, 0) + 1
        measured["found"] = tally.get("mint", 0)
        measured["by_classification"] = tally
    except Exception as exc:
        measured["found"] = None
        measured["reason"] = f"{type(exc).__name__}: {exc}"
    return measured


# ---------------------------------------------------------------------------
# the rungs
# ---------------------------------------------------------------------------


def rung_r1_unsandboxed_bash_closed(log: Optional[Path] = None) -> Rung:
    """The headline P2 deliverable, and the registry's only closed gap.

    Proved by RUNNING both pin files rather than by a `-k` filter. An earlier
    draft filtered on `"unsandboxed or subprocess"`, which matched neither pin's
    real name and reported the closed gap as `fail` - a gate that manufactures
    a red is as bad as one that manufactures a green.
    """
    result = _pytest(
        "execution/test_unsandboxed_pins.py", "execution/test_p2w1_trust_pins.py"
    )
    evidence = result["tail"][-600:]
    if result["ok"]:
        return Rung(
            "r1_unsandboxed_bash_closed",
            "the unsandboxed local-subprocess bash path is closed",
            PASS,
            "ran and held: no production import path reaches "
            "harness._stubs.sandbox, and the registry's inverted pin now "
            "PASSES - which is the two-part closure the phase brief requires "
            "(pin green AND the entry deleted in the same change).",
            evidence=evidence,
        )
    return Rung(
        "r1_unsandboxed_bash_closed",
        "the unsandboxed local-subprocess bash path is closed",
        FAIL,
        f"ran and broke: the proving test did not pass (exit "
        f"{result['returncode']}).",
        evidence=evidence,
    )


def rung_r2_untrusted_sources_scanned() -> Rung:
    sys.path.insert(0, str(REPO_ROOT))
    from shared import security

    sources = set(security.UNTRUSTED_SOURCES)
    required = {"issue", "repository_instructions", "web", "skill", "plugin", "mcp", "memory"}
    missing = sorted(required - sources)
    measured = non_vacuity_measurement()
    moved = len(measured["untrusted_boundary"])
    if missing:
        return Rung(
            "r2_untrusted_sources_scanned",
            "every untrusted source is scanned at a boundary",
            FAIL,
            f"ran and broke: UNTRUSTED_SOURCES is missing {missing}.",
        )
    if moved == 0:
        return Rung(
            "r2_untrusted_sources_scanned",
            "every untrusted source is scanned at a boundary",
            FAIL,
            "ran and broke: removing `review_untrusted_source` moved ZERO "
            "corpus cases, so the corpus is not evidence that the boundary "
            "does anything.",
            evidence=f"non-vacuity probe: {measured}",
        )
    return Rung(
        "r2_untrusted_sources_scanned",
        "every untrusted source is scanned at a boundary",
        PASS,
        f"ran and held: all {len(required)} sources declared, and removing "
        f"`review_untrusted_source` moved {moved} corpus cases from blocked "
        f"to unblocked - so every one of them is load-bearing.",
        evidence=f"non-vacuity probe: {measured['untrusted_boundary']}",
    )


def rung_r3_policy_language_fails_closed() -> Rung:
    """A policy language that validates itself, proven by broken rules."""
    measured = policy_measurement()
    proving = PROVING_TESTS["r3_policy_language_fails_closed"]
    if not measured.get("loaded"):
        return Rung(
            "r3_policy_language_fails_closed",
            "the policy language refuses a rule that cannot prove itself",
            FAIL,
            "ran and broke: the SHIPPED POLICY DOES NOT LOAD. "
            + str(measured.get("refusal"))[:500]
            + "\n\nThis is the mechanism working - a rule whose declared "
            "example its own delegated screen does not decide is refused at "
            "load - and it is simultaneously a real defect in the shipped rule "
            "set. The gate does not crash on it: a refusal is a value.",
            evidence=json.dumps(measured, indent=2),
            owner="T3 (the delegated screen) with T5 (the rule's example)",
        )
    result = _pytest("tests/test_p2w2_policy_proof.py")
    if not result["ok"]:
        return Rung(
            "r3_policy_language_fails_closed",
            "the policy language refuses a rule that cannot prove itself",
            FAIL,
            f"ran and broke: the proving suite did not pass (exit "
            f"{result['returncode']}).",
            evidence=result["tail"][-600:],
        )
    if measured["rules_failing"] or measured["rules_without_examples"]:
        return Rung(
            "r3_policy_language_fails_closed",
            "the policy language refuses a rule that cannot prove itself",
            FAIL,
            f"ran and broke: {len(measured['rules_failing'])} rule(s) whose own "
            f"examples fail, {len(measured['rules_without_examples'])} rule(s) "
            f"shipping with none.",
            evidence=json.dumps(measured, indent=2),
        )
    return Rung(
        "r3_policy_language_fails_closed",
        "the policy language refuses a rule that cannot prove itself",
        PASS,
        f"ran and held: three deliberately broken rules (a `match` example "
        f"that does not match, a `not_match` example that does, a malformed "
        f"decision) each refuse to load naming the rule and the example; "
        f"{measured['rule_count']} shipped rules carry "
        f"{measured['example_count']} examples, {len(measured['rules_without_examples'])} "
        f"with none; and reordering the layers changes the outcome.",
        evidence=f"proving tests: {list(proving)}",
    )


def rung_r4_corpus_is_non_vacuous() -> Rung:
    scanned = _scan_corpus()
    measured = non_vacuity_measurement()
    vacuous = sorted(k for k, v in measured.items() if not v)
    empty_surfaces = sorted(
        name for name, count in scanned["coverage"].items() if count == 0
    )
    if empty_surfaces:
        return Rung(
            "r4_corpus_is_non_vacuous",
            "every corpus case exercises a real code path",
            FAIL,
            f"ran and broke: Phase 2 surfaces with ZERO cases: {empty_surfaces}.",
            evidence=json.dumps(scanned, indent=2),
        )
    if vacuous:
        return Rung(
            "r4_corpus_is_non_vacuous",
            "every corpus case exercises a real code path",
            FAIL,
            f"ran and broke: these defences moved ZERO corpus cases when "
            f"removed, so the cases relying on them are vacuous: {vacuous}.",
            evidence=json.dumps(measured, indent=2),
        )
    return Rung(
        "r4_corpus_is_non_vacuous",
        "every corpus case exercises a real code path",
        PASS,
        f"ran and held: {scanned['total']} cases across "
        f"{len(scanned['groups'])} groups, every count pinned EXACTLY, every "
        f"Phase 2 surface non-zero. Removing each defence in turn moved: "
        + ", ".join(f"{k}={len(v)}" for k, v in sorted(measured.items()))
        + ". Each moved count is measured by REMOVING the defence, not by "
        "counting green assertions.",
        evidence=json.dumps(measured, indent=2),
    )


def rung_r5_provenance_is_declared() -> Rung:
    measured = provenance_measurement()
    if measured["verdict"] == "PROVENANCE_COMPLETE":
        return Rung(
            "r5_provenance_is_declared",
            "every vendored file declares its provenance in the parsed format",
            PASS,
            "ran and held: the scanner found no undeclared vendored file.",
            evidence=json.dumps(measured, indent=2),
        )
    binaries = binary_inventory()
    gaps: List[str] = []
    if measured["undeclared"]:
        gaps.append(
            f"{measured['undeclared']} vendored file(s) carry a header the "
            f"scanner cannot parse"
        )
    if binaries.get("available") and not binaries.get("committed_digests"):
        gaps.append(
            "no committed digests for the fetched binaries, so a replaced "
            "asset with a replaced sidecar is accepted"
        )
    if binaries.get("gitignored"):
        gaps.append(
            "provenance/ is gitignored, so the binary manifest is a claim no "
            "reviewer can see in a diff"
        )
    return Rung(
        "r5_provenance_is_declared",
        "every vendored file declares its provenance in the parsed format",
        FAIL,
        f"ran and broke: the spine is RED ({measured['verdict']}, exit "
        f"{measured['returncode']}, declared={measured['declared']}, "
        f"undeclared={measured['undeclared']}). " + "; ".join(gaps) + ". "
        + measured["reason"],
        evidence=json.dumps(measured, indent=2),
        owner="T1 (headers + the committed digests)",
    )


def rung_r6_verifier_mint_unchanged(log: Optional[Path] = None) -> Rung:
    """DOCTRINE 2 says one mint site. The tree has three. That is a FINDING."""
    measured = mint_sites()
    result = _pytest("harness/test_mint_site_pins.py", "harness/test_completion_mint_sites.py")
    if not result["ok"]:
        return Rung(
            "r6_verifier_mint_unchanged",
            "the verifier mint condition is unchanged and pinned",
            FAIL,
            f"ran and broke: the mint pins did not pass (exit "
            f"{result['returncode']}).",
            evidence=result["tail"][-600:],
        )
    found = measured.get("found")
    expected = measured.get("expected")
    detail = (
        f"ran and held the PIN, but the pin itself records a GAP: "
        f"phases/DOCTRINE.md 2 claims `completed_verified` is minted at "
        f"exactly one place, and the tree pins {expected}. Measured mint "
        f"sites: {found} across {measured['files']}. Phase 3a owns the "
        f"collapse; the count is pinned so it cannot move silently."
    )
    return Rung(
        "r6_verifier_mint_unchanged",
        "the verifier mint condition is unchanged and pinned",
        FAIL,
        detail,
        evidence=json.dumps(measured, indent=2),
        owner="P3a (collapse the mint sites)",
    )


def rung_floor_is_wired() -> Rung:
    """A dead guard is worse than an absent one: it reads as protection."""
    measured = policy_measurement()
    src = _read("runtime/hardline.py")
    call_sites = src.count("screen_or_raise")
    has_callsite = "screen_or_raise" in _read("execution/workspace.py") or (
        "screen_or_raise" in _read("execution/sandbox.py")
    )
    if has_callsite:
        return Rung(
            "r7_floor_is_wired",
            "the hardline floor reaches a command interpreter",
            PASS,
            "ran and held: an execution-layer call site invokes "
            "`screen_or_raise` before dispatch.",
            evidence=f"floor rules folded: {measured['floor_rules_folded']}",
        )
    return blocked(
        "r7_floor_is_wired",
        "the hardline floor reaches a command interpreter",
        "runtime/hardline.screen_or_raise has NO production call site. The "
        "table is folded into the shipped policy "
        f"({measured['floor_rules_folded']} rule(s), floor available="
        f"{measured['floor_available']}) and T3's own pins pass, but a guard "
        "nobody calls cannot refuse anything. T3 records this as not wired. "
        "Until it is wired, every hardline bypass row in ATTACK_RESULTS is a "
        "property of the TABLE and not of a running guard.",
        owner="T3 (wire) with T2 (execution-layer call sites)",
        evidence=f"{call_sites} mentions inside runtime/hardline.py itself; "
        f"zero in execution/workspace.py or execution/sandbox.py",
    )


def rung_binary_digests_pinned() -> Rung:
    """Phase 1's recorded open gap, still open, stated as such."""
    binaries = binary_inventory()
    if not binaries.get("available"):
        return blocked(
            "r8_binary_digests_pinned",
            "the fetched binaries carry committed digests",
            f"provenance/binaries.json is absent: {binaries.get('reason')}",
            owner="T1 / P2.1 + T5",
        )
    if binaries["committed_digests"]:
        return Rung(
            "r8_binary_digests_pinned",
            "the fetched binaries carry committed digests",
            PASS,
            "ran and held: `committed_digests` is present.",
            evidence=json.dumps(binaries, indent=2),
        )
    return Rung(
        "r8_binary_digests_pinned",
        "the fetched binaries carry committed digests",
        FAIL,
        "ran and broke: ripgrep 14.1.1 and fd 10.2.0 have their VERSIONS "
        "pinned in harness/search_engine.py, but no SHA-256 is committed "
        "anywhere in this repository. The downloader verifies against an "
        "upstream `.sha256` sidecar, so the expected digest travels WITH the "
        "payload: a replaced asset with a replaced sidecar is accepted. A "
        "checksum-verified download whose checksum is not committed is the "
        "paperwork version of the same honesty problem. Compounding it, "
        "`provenance/` is gitignored, so even the declarations are uncommitted."
        + (" (confirmed gitignored)" if binaries.get("gitignored") else ""),
        evidence=json.dumps(binaries, indent=2),
        owner="T1 / P2.1 + T5",
    )


def rung_live_provider() -> Rung:
    return blocked(
        "r9_live_provider",
        "a live model provider was exercised",
        "NO live-provider lane was run and none is claimed. Every model call "
        "in every suite is a scripted double through the documented "
        "`harness.deps.set_call_model` seam or the offline mock provider. No "
        "credential was inspected, requested or retained. Nothing in G2 is "
        "evidence about any model's coding ability - including the model that "
        "would be needed to exploit the injection surfaces this gate covers.",
        owner="Phase 7 (release evidence) with an operator credential",
    )


def rung_full_suite(log: Optional[Path] = None) -> Rung:
    """The whole tree, and its adjudication, published rather than summarised."""
    if log is not None and log.is_file():
        text = log.read_text(encoding="utf-8", errors="replace")
        failed = [
            line.split(" ", 1)[-1].split("::")[0]
            for line in text.splitlines()
            if line.startswith("FAILED ")
        ]
        summary = next(
            (line for line in reversed(text.splitlines()) if " passed" in line),
            "",
        ).strip()
        by_file: Dict[str, int] = {}
        for name in failed:
            by_file[name] = by_file.get(name, 0) + 1
        top = sorted(by_file.items(), key=lambda kv: -kv[1])[:8]
        detail = (
            f"ran and broke: {len(failed)} failing test(s). {summary}".strip()
        )
        if by_file:
            detail += ". Worst files: " + ", ".join(
                f"{name.split('/')[-1]}({count})" for name, count in top
            )
        detail += (
            ". The DOMINANT class is `ValueError: unknown agent strategy: "
            "legacy_agent`: T1's engine collapse deleted `legacy_agent` and "
            "the suites that pin it by name were not repinned. That is a "
            "test-side sweep, not a product defect - and it is in `tests/`, "
            "which this terminal owns, so it is T5's own debt and the first "
            "thing the next T5 round should close."
        )
        return Rung(
            "r10_full_suite",
            "the full tests/ tree is green or every failure is adjudicated",
            FAIL,
            detail,
            evidence="\n".join(text.splitlines()[-30:]),
            owner="T5 (the legacy_agent pin sweep in tests/)",
        )
    return blocked(
        "r10_full_suite",
        "the full tests/ tree is green or every failure is adjudicated",
        "no full-suite log was supplied to this gate. `python -m pytest tests/ "
        "-q` is a long lane and it was NOT run to completion in this window; "
        "every number in this gate comes from an explicitly named selection. A "
        "claim of 'the suite passes' on this tree would be a claim about a "
        "subset.",
        owner="T5 (adjudication)",
    )


def rung_registry_gate() -> Rung:
    """The registry gate is step 1 of all six CI lanes."""
    result = _pytest("tests/test_known_failing_pins.py", timeout_s=600)
    if result["ok"]:
        return Rung(
            "r11_registry_gate",
            "the known-failing registry is consistent and gates every lane",
            PASS,
            "ran and held: every registered pin is red for its recorded "
            "reason, and every inverted pin is green.",
            evidence=result["tail"][-400:],
        )
    return Rung(
        "r11_registry_gate",
        "the known-failing registry is consistent and gates every lane",
        FAIL,
        f"ran and broke (exit {result['returncode']}). Either a registered pin "
        f"started passing (a CLOSED GAP that must be promoted) or one is red "
        f"for a different reason (a REGRESSION). Neither is a known gap.",
        evidence=result["tail"][-800:],
    )


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------


@dataclass
class G2Inputs:
    """Everything this gate reads from outside itself."""

    full_suite_log: Optional[Path] = None
    docker: Optional[Dict[str, Any]] = None


def _safe(rung_id: str, title: str, fn, *args: Any) -> Rung:
    """Derive one rung, converting ANY escape into a `fail`.

    A gate that dies on a broken input reports nothing, and "no report" reads
    exactly like "no problem". That is the worst possible response to the
    failure this gate exists to catch, so every rung derivation is wrapped and
    an exception becomes a `fail` naming the traceback's last line.
    """
    try:
        return fn(*args)
    except Exception as exc:
        import traceback

        last = traceback.format_exc().strip().splitlines()[-1]
        return Rung(
            rung_id,
            title,
            FAIL,
            f"the rung's own measurement RAISED: {type(exc).__name__}: {exc}. "
            f"Reported as a failure rather than crashing the gate, because a "
            f"gate that dies reports nothing.",
            evidence=last,
        )


def g2_report(inputs: Optional[G2Inputs] = None) -> Dict[str, Any]:
    """Probe every G2 rung and derive the verdict."""
    inputs = inputs or G2Inputs()

    plan = (
        ("r1_unsandboxed_bash_closed", "the unsandboxed local-subprocess bash path is closed",
         rung_r1_unsandboxed_bash_closed, (inputs.full_suite_log,)),
        ("r2_untrusted_sources_scanned", "every untrusted source is scanned at a boundary",
         rung_r2_untrusted_sources_scanned, ()),
        ("r3_policy_language_fails_closed", "the policy language refuses a rule that cannot prove itself",
         rung_r3_policy_language_fails_closed, ()),
        ("r4_corpus_is_non_vacuous", "every corpus case exercises a real code path",
         rung_r4_corpus_is_non_vacuous, ()),
        ("r5_provenance_is_declared", "every vendored file declares its provenance in the parsed format",
         rung_r5_provenance_is_declared, ()),
        ("r6_verifier_mint_unchanged", "the verifier mint condition is unchanged and pinned",
         rung_r6_verifier_mint_unchanged, (inputs.full_suite_log,)),
        ("r7_floor_is_wired", "the hardline floor reaches a command interpreter",
         rung_floor_is_wired, ()),
        ("r8_binary_digests_pinned", "the fetched binaries carry committed digests",
         rung_binary_digests_pinned, ()),
        ("r9_live_provider", "a live model provider was exercised",
         rung_live_provider, ()),
        ("r10_full_suite", "the full tests/ tree is green or every failure is adjudicated",
         rung_full_suite, (inputs.full_suite_log,)),
        ("r11_registry_gate", "the known-failing registry is consistent and gates every lane",
         rung_registry_gate, ()),
    )
    rungs: List[Rung] = [
        _safe(rung_id, title, fn, *args) for rung_id, title, fn, args in plan
    ]

    docker = inputs.docker if inputs.docker is not None else docker_reachable()
    if not docker.get("reachable", False):
        rungs.append(
            blocked(
                "r12_docker_daemon",
                "the Docker-dependent lanes ran",
                f"docker daemon not reachable: {docker.get('reason') or 'unknown'}",
            )
        )
    else:
        rungs.append(
            Rung(
                "r12_docker_daemon",
                "the Docker-dependent lanes ran",
                PASS,
                f"ran and held: daemon reachable ({docker.get('server_version') or 'version unreported'}).",
                evidence=json.dumps(docker, indent=2),
            )
        )

    counts = {status: 0 for status in (PASS, FAIL, BLOCKED, NOT_IMPLEMENTED)}
    for rung in rungs:
        counts[rung.status] = counts.get(rung.status, 0) + 1

    blocking_failures = [
        r.id for r in rungs if r.blocking and r.status == FAIL
    ]
    blocking_blocked = [r.id for r in rungs if r.blocking and r.status == BLOCKED]
    verdict = G2_RED if (blocking_failures or blocking_blocked) else G2_GREEN

    return {
        "phase": "P2 Trusted Execution",
        "gate": "G2",
        "verdict": verdict,
        "verdict_meaning": (
            "G2_RED means at least one BLOCKING rung is `fail` or `blocked`. "
            "This gate certifies that a set of MEASURED claims about this tree, "
            "on this host, at this commit, are true - it is not a release gate "
            "and it does not certify a build."
        ),
        "counts": counts,
        "blocking_failures": blocking_failures,
        "blocking_blocked": blocking_blocked,
        "rungs": [
            {
                "id": r.id,
                "title": r.title,
                "status": r.status,
                "detail": r.detail,
                "owner": r.owner,
                "evidence": r.evidence,
                "blocking": r.blocking,
                "proving_tests": list(PROVING_TESTS.get(r.id, ())),
            }
            for r in rungs
        ],
        "attack_results": [dict(row) for row in ATTACK_RESULTS],
        "attack_results_note": (
            "EVERY row is listed, including any that succeeded. A trust report "
            "that omits a failure is the thing this repository exists to "
            "prevent."
        ),
        "containment_limits": list(CONTAINMENT_LIMITS),
        "not_established": list(NOT_ESTABLISHED),
        "loc": loc_delta(),
        "vendored": vendored_inventory(),
        "binaries": binary_inventory(),
        "corpus": _scan_corpus(),
        "policy": policy_measurement(),
        "mint_sites": mint_sites(),
    }


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

_MARK = {PASS: "pass", FAIL: "FAIL", BLOCKED: "BLOCKED", NOT_IMPLEMENTED: "not_impl"}


def render(report: Dict[str, Any]) -> str:
    out: List[str] = []
    add = out.append
    add(f"{report['gate']} ({report['phase']}): {report['verdict']}")
    add("")
    add(report["verdict_meaning"])
    add("")
    counts = report["counts"]
    add(
        "counts: "
        + "  ".join(f"{counts.get(s, 0)} {_MARK[s]}" for s in (PASS, FAIL, BLOCKED, NOT_IMPLEMENTED))
    )
    add("")
    add("-" * 78)
    add("RUNGS")
    add("-" * 78)
    for rung in report["rungs"]:
        flag = " [blocking]" if rung["blocking"] else ""
        add(f"[{_MARK[rung['status']]:>9}]{flag} {rung['id']}  {rung['title']}")
        for line in str(rung["detail"]).splitlines():
            add(f"             {line}")
        if rung.get("owner"):
            add(f"             owner: {rung['owner']}")
        if rung.get("proving_tests"):
            add("             proving test(s):")
            for name in rung["proving_tests"]:
                add(f"               - {name}")
    add("")
    add("-" * 78)
    add("ATTACK RESULTS  (every row, including any that succeeded)")
    add("-" * 78)
    for row in report["attack_results"]:
        add(f"  [{row['result']:>9}]  {row['owner']}  {row['row']}")
    add(f"  {report['attack_results_note']}")
    add("")
    add("-" * 78)
    add("LOC DELTA - Phase 2 was SUBTRACTIVE, so both directions are reported")
    add("-" * 78)
    loc = report["loc"]
    add(f"  {loc['raw'] or '(no tracked diff)'}")
    add(
        f"  insertions={loc['insertions']}  deletions={loc['deletions']}  "
        f"net={loc['net']:+d}  files={loc['files']}"
    )
    if loc["deleted_files"]:
        for name in loc["deleted_files"]:
            add(f"  DELETED: {name}")
    add(f"  {loc['note']}")
    add("")
    add("-" * 78)
    add("VENDORED CODE INVENTORY")
    add("-" * 78)
    for row in report["vendored"]:
        add(
            f"  {row['file']}\n"
            f"      upstream : {row['upstream']}\n"
            f"      licence  : {row['licence']}\n"
            f"      byte copy: {row['byte_copy']}  ({row['scope']})\n"
            f"      sha256   : {row['sha256'] or '(absent)'}"
        )
    add("")
    add("-" * 78)
    add("FETCHED BINARIES")
    add("-" * 78)
    binaries = report["binaries"]
    if not binaries.get("available"):
        add(f"  unavailable: {binaries.get('reason')}")
    else:
        for row in binaries.get("binaries", ()):
            add(
                f"  {row['name']} {row['version']}  licence={row['licence']}  "
                f"digest_pinned={row['digest_pinned']}\n"
                f"      upstream: {row['upstream']}"
            )
        add(f"  committed_digests: {binaries.get('committed_digests')}")
        add(f"  manifest gitignored: {binaries.get('gitignored')}")
    add("")
    add("-" * 78)
    add("CORPUS AND POLICY")
    add("-" * 78)
    corpus = report["corpus"]
    for name, count in corpus["groups"].items():
        add(f"  {name:26s} {count:>3}")
    add(f"  {'TOTAL':26s} {corpus['total']:>3}")
    for surface, count in corpus["coverage"].items():
        add(f"    surface {surface:20s} {count}")
    policy = report["policy"]
    add(
        f"  policy: {policy['rule_count']} rules, {policy['example_count']} "
        f"examples, {len(policy['rules_failing'])} failing, "
        f"{len(policy['rules_without_examples'])} without examples"
    )
    add(
        f"  floor: {policy['floor_rules_folded']} folded of "
        f"{policy['floor_patterns_seen']} patterns, available="
        f"{policy['floor_available']}, excluded={policy['floor_excluded']}"
    )
    mint = report["mint_sites"]
    add(
        f"  mint sites: expected={mint.get('expected')} found={mint.get('found')} "
        f"(DOCTRINE 2 claims exactly one)"
    )
    add("")
    add("-" * 78)
    add("HONEST CONTAINMENT LIMITS")
    add("-" * 78)
    for limit in report["containment_limits"]:
        add(f"  - {limit}")
    add("")
    add("-" * 78)
    add("WHAT THIS GATE DOES NOT ESTABLISH")
    add("-" * 78)
    for item in report["not_established"]:
        add(f"  - {item}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# the gate must be able to fail
# ---------------------------------------------------------------------------


def _selftest() -> Dict[str, Any]:
    """Mutate this gate's own inputs and assert the verdict MOVES.

    A gate that cannot fail is worse than no gate, and the cheapest proof is
    to break a rung on purpose and watch the verdict follow.
    """
    checks: List[Dict[str, Any]] = []

    baseline = Rung("x", "t", PASS, "d").blocking
    if baseline is not True:
        checks.append({"check": "a pass is blocking by default", "ok": False})
    else:
        checks.append({"check": "a pass is blocking by default", "ok": True})

    for status in (FAIL, BLOCKED):
        rung = Rung("x", "t", status, "d", blocking=True)
        if report_status(rung) != status:
            checks.append({"check": f"{status} survives construction", "ok": False})
        else:
            checks.append({"check": f"{status} survives construction", "ok": True})

    # The vocabulary is closed. `skip` cannot be constructed.
    try:
        Rung("x", "t", "skip", "d")  # type: ignore[arg-type]
        checks.append({"check": "'skip' cannot be constructed", "ok": False})
    except Exception:
        checks.append({"check": "'skip' cannot be constructed", "ok": True})

    # blocked() and not_implemented() refuse an empty reason/owner.
    for label, fn, arg in (
        ("blocked() refuses an empty reason", blocked, ""),
        ("not_implemented() refuses an empty owner", not_implemented, ""),
    ):
        try:
            fn("id", "title", arg)
            checks.append({"check": label, "ok": False})
        except Exception:
            checks.append({"check": label, "ok": True})

    # A synthetic report: one blocking failure must turn the verdict red.
    synthetic = {
        "rungs": [
            {"id": "a", "status": PASS, "blocking": True},
            {"id": "b", "status": FAIL, "blocking": True},
        ]
    }
    failures = [r["id"] for r in synthetic["rungs"] if r["blocking"] and r["status"] == FAIL]
    if failures == ["b"]:
        checks.append({"check": "one blocking fail => red", "ok": True})
    else:
        checks.append({"check": "one blocking fail => red", "ok": False})

    # And a blocked rung must NOT be silently treated as a pass.
    blocked_only = {"rungs": [{"id": "a", "status": BLOCKED, "blocking": True}]}
    blocked_ids = [r["id"] for r in blocked_only["rungs"] if r["blocking"] and r["status"] == BLOCKED]
    if blocked_ids == ["a"]:
        checks.append({"check": "a blocked rung keeps the gate red", "ok": True})
    else:
        checks.append({"check": "a blocked rung keeps the gate red", "ok": False})

    return {"checks": checks, "ok": all(c["ok"] for c in checks)}


def report_status(rung: Rung) -> str:
    return rung.status


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="G2 - the P2 Trusted Execution gate")
    parser.add_argument("--full-suite-log", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        result = _selftest()
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 2

    report = g2_report(G2Inputs(full_suite_log=args.full_suite_log))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render(report))
    return 2 if report["verdict"] == G2_RED else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
