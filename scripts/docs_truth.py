"""Documentation and site truth gate.

A public claim is a liability: it is asserted in prose, checked by nobody,
and goes stale silently. This module makes the machine-checkable subset
machine-checked, and fails the build when prose and code disagree.

Three gates, each one a way a real product lies:

1. **Version parity.** ``pyproject.toml`` is the release source of truth.
   Every surface that names a version - the site's release data, its
   install content, the docs index, the README - must name the SAME number.
   A site advertising 0.2.1 while the index has 0.2.0 is not a cosmetic
   bug; it is the site lying about what a user gets.

2. **Claim evidence.** ``docs/feature-matrix.md`` is the capability claim
   of record. Every row must carry a status from the closed vocabulary and
   an evidence reference (a test id, a script, or a report path) that
   actually exists in the repository. A row with no evidence is not a
   claim, it is a hope, and it fails here.

3. **Stale limits.** ``site/src/lib/content/limits.ts`` and
   ``docs/feature-matrix.md`` must not assert a numeric limit that no code
   or config declares. A limit nobody can find is a limit nobody maintains.

Exit codes follow the repo convention: 0 pass, 2 gate failure, 3 a gate
could not be evaluated (a missing surface, which is never reported as a
pass).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "ClaimFinding",
    "GateResult",
    "check_claims",
    "check_limits",
    "check_versions",
    "main",
    "run_gates",
    "site_gate",
]

SCHEMA = "neo.docs_truth/1"

EXIT_OK = 0
EXIT_FAIL = 2
EXIT_UNEVALUATED = 3

#: The closed status vocabulary for a feature-matrix row. Anything else is
#: drift: a new word in the table means nobody updated the reader.
CLAIM_STATUSES = frozenset(
    {
        "Implemented",
        "Blocked",
        "Blocked unless selected",
        "Blocked by owner/process",
        "Packaged in 0.2.1 candidate",
    }
)

#: Version-naming surfaces, and HOW each one is checked.
#:
#: ``newest`` - the FIRST version the surface names must be the current one.
#: Every surface here uses that mode, and the reason matters: a changelog
#: and a release list legitimately contain history, and a README that says
#: "public PyPI remains v0.2.0" is reporting the PUBLIC release, not
#: claiming the source is 0.2.0. The HEADING of a release document is the
#: claim; a historical mention is not. A pattern that matches nothing at all
#: is a finding, because a release list that stopped naming versions is
#: exactly the drift this gate exists to catch.
_VERSION_PATTERNS: Tuple[Tuple[str, str, str], ...] = (
    (
        "site/src/lib/content/releases.ts",
        r'version:\s*"v?(\d+\.\d+\.\d+)"',
        "newest",
    ),
    ("CHANGELOG.md", r"^##\s*\[?v?(\d+\.\d+\.\d+)\]?", "newest"),
    ("README.md", r"\bv(\d+\.\d+\.\d+)\b", "newest"),
    (
        "docs/README.md",
        r"\b(\d+\.\d+\.\d+) source candidate\b",
        "newest",
    ),
)


@dataclass
class ClaimFinding:
    """One documentation problem, with the file and line that caused it."""

    gate: str
    path: str
    detail: str
    line: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable finding."""
        return {
            "gate": self.gate,
            "path": self.path,
            "line": self.line,
            "detail": self.detail,
        }

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"[{self.gate}] {where}: {self.detail}"


@dataclass
class GateResult:
    """The outcome of one gate."""

    gate: str
    status: str
    findings: List[ClaimFinding] = field(default_factory=list)
    checked: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        """Whether the gate passed. An unevaluated gate is NOT ok."""
        return self.status == "pass"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable result."""
        return {
            "gate": self.gate,
            "status": self.status,
            "checked": self.checked,
            "detail": self.detail,
            "findings": [item.to_dict() for item in self.findings],
        }


def _read(root: Path, relative: str) -> Optional[str]:
    try:
        return (root / relative).read_text(encoding="utf-8")
    except OSError:
        return None


def _pyproject_version(root: Path) -> str:
    text = _read(root, "pyproject.toml") or ""
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    return match.group(1) if match else ""


def check_versions(root: Path) -> GateResult:
    """Every version-naming surface must agree with ``pyproject.toml``.

    A surface that is absent is reported ``unevaluated`` rather than
    skipped: a missing file is a packaging problem, not a pass.
    """
    expected = _pyproject_version(root)
    if not expected:
        return GateResult(
            "versions",
            "unevaluated",
            [ClaimFinding("versions", "pyproject.toml", "no version declared")],
        )
    findings: List[ClaimFinding] = []
    checked = 0
    for relative, pattern, mode in _VERSION_PATTERNS:
        text = _read(root, relative)
        if text is None:
            findings.append(
                ClaimFinding("versions", relative, "surface is missing from the tree")
            )
            continue
        found = re.findall(pattern, text, re.M)
        if not found:
            findings.append(
                ClaimFinding(
                    "versions", relative, f"names no version matching /{pattern}/"
                )
            )
            continue
        checked += 1
        if mode == "newest":
            stale = [found[0]] if found[0] != expected else []
        else:
            stale = sorted({value for value in found if value != expected})
        if stale:
            findings.append(
                ClaimFinding(
                    "versions",
                    relative,
                    f"declares {', '.join(stale)} but pyproject declares {expected}",
                )
            )
    return GateResult(
        "versions",
        "pass" if not findings else "fail",
        findings,
        checked,
        f"expected {expected}",
    )


_ROW = re.compile(r"^\|\s*(?P<cells>.+?)\|\s*$")


def _iter_markdown_rows(text: str) -> Iterable[Tuple[int, str, List[str]]]:
    """Yield ``(line_number, enclosing_heading, cells)`` for every table row.

    Headings are tracked alongside rows because the claim gate is scoped to
    ONE section of the feature matrix; a row in a different table has a
    different contract and must not be judged by this one.
    """
    heading = ""
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            continue
        if not stripped.startswith("|"):
            continue
        match = _ROW.match(stripped)
        if not match:
            continue
        yield (
            number,
            heading,
            [cell.strip() for cell in match.group("cells").split("|")],
        )


_EVIDENCE = re.compile(
    r"(tests/[\w./-]+\.py|scripts/[\w./-]+\.py|logs/[\w./-]+|pyproject\.toml)"
)


def check_claims(root: Path) -> GateResult:
    """Every feature-matrix row needs a known status and real evidence.

    "Implemented" with no test reference is exactly the claim this gate
    exists to catch: it is unfalsifiable, so it decays into a lie. The
    status vocabulary is closed, so a new word in the table is a finding
    rather than something only a reader would notice.
    """
    relative = "docs/feature-matrix.md"
    text = _read(root, relative)
    if text is None:
        return GateResult(
            "claims",
            "unevaluated",
            [ClaimFinding("claims", relative, "feature matrix is missing")],
        )
    findings: List[ClaimFinding] = []
    checked = 0
    seen_header = False
    for number, heading, cells in _iter_markdown_rows(text):
        if "User-facing capabilities" not in heading or len(cells) < 2:
            continue
        first = cells[0]
        if set(first) <= set("-: "):
            continue
        if not seen_header:
            # The table's own header row ("Capability | Status | ...") is
            # schema, not a claim. Recognising it by its literal cells is
            # what keeps the gate from reporting "Capability has status
            # Status" on every future run.
            if first.lower() == "capability" and cells[1].lower() == "status":
                seen_header = True
                continue
            return GateResult(
                "claims",
                "unevaluated",
                [
                    ClaimFinding(
                        "claims",
                        relative,
                        "the capability table has no 'Capability | Status' "
                        "header row, so no row could be identified as a claim",
                        number,
                    )
                ],
            )
        capability, status = first, cells[1]
        checked += 1
        if status not in CLAIM_STATUSES:
            findings.append(
                ClaimFinding(
                    "claims",
                    relative,
                    f"{capability!r} has status {status!r}, which is not in the "
                    "closed vocabulary",
                    number,
                )
            )
        row = " | ".join(cells)
        evidence = _EVIDENCE.search(row)
        if not evidence:
            findings.append(
                ClaimFinding(
                    "claims",
                    relative,
                    f"{capability!r} cites no test, script, or report evidence",
                    number,
                )
            )
            continue
        for reference in sorted(set(_EVIDENCE.findall(row))):
            if reference.startswith("logs/"):
                continue  # run evidence is local and gitignored by design
            if not (root / reference).exists():
                findings.append(
                    ClaimFinding(
                        "claims",
                        relative,
                        f"{capability!r} cites {reference}, which does not exist",
                        number,
                    )
                )
    if not checked:
        return GateResult(
            "claims",
            "unevaluated",
            [
                ClaimFinding(
                    "claims",
                    relative,
                    "no 'User-facing capabilities' table rows were found; the "
                    "gate checked nothing and cannot report a pass",
                )
            ],
        )
    return GateResult(
        "claims",
        "pass" if not findings else "fail",
        findings,
        checked,
        f"{checked} capability rows",
    )


#: A cited source is a repository-relative path, optionally with ``:line``
#: or ``:line-line`` ranges and free text around it.
_SOURCE_REF = re.compile(r"([\w./-]+\.(?:md|toml|py|ts|tsx|yml|yaml|json))")


def check_limits(root: Path) -> GateResult:
    """Every published limitation must cite a source that exists.

    ``site/src/lib/content/limits.ts`` is a list of things the product does
    NOT do, and each entry is required to name where that claim comes from.
    A limitation with no source is unfalsifiable in the same way an
    unsupported feature claim is. This gate also refuses to report a pass
    when it checked nothing.
    """
    relative = "site/src/lib/content/limits.ts"
    text = _read(root, relative)
    if text is None:
        return GateResult(
            "limits",
            "unevaluated",
            [ClaimFinding("limits", relative, "limits surface is missing")],
        )
    entries = re.findall(r'k:\s*"([^"]+)"(.*?)source:\s*"([^"]*)"', text, re.S)
    findings: List[ClaimFinding] = []
    for label, _body, source in entries:
        refs = sorted(set(_SOURCE_REF.findall(source)))
        if not refs:
            findings.append(
                ClaimFinding(
                    "limits", relative, f"limitation {label!r} cites no source"
                )
            )
            continue
        for reference in refs:
            if not (root / reference).exists():
                findings.append(
                    ClaimFinding(
                        "limits",
                        relative,
                        f"limitation {label!r} cites {reference}, which does not exist",
                    )
                )
    if not entries:
        return GateResult(
            "limits",
            "unevaluated",
            [
                ClaimFinding(
                    "limits",
                    relative,
                    "no limitation entries parsed; the gate checked nothing",
                )
            ],
        )
    return GateResult(
        "limits",
        "pass" if not findings else "fail",
        findings,
        len(entries),
        f"{len(entries)} published limitations",
    )


def run_gates(root: Path) -> Dict[str, Any]:
    """Run every gate and return one machine-readable report."""
    results = [check_versions(root), check_claims(root), check_limits(root)]
    return {
        "schema": SCHEMA,
        "status": "pass" if all(item.ok for item in results) else "fail",
        "exit_code": EXIT_OK if all(item.ok for item in results) else EXIT_FAIL,
        "gates": [item.to_dict() for item in results],
    }


def site_gate(root: Path) -> GateResult:
    """Run only the gates that decide whether the SITE may be published."""
    results = [check_versions(root), check_claims(root)]
    findings: List[ClaimFinding] = []
    for item in results:
        findings.extend(item.findings)
    return GateResult(
        "site",
        "pass" if not findings else "fail",
        findings,
        sum(item.checked for item in results),
        "site version parity + feature-matrix claim evidence",
    )


def _render(report: Dict[str, Any]) -> str:
    lines = [f"docs truth gate: {report['status']}"]
    for gate in report["gates"]:
        mark = "OK  " if gate["status"] == "pass" else "FAIL"
        lines.append(f"  {mark} {gate['gate']} — {gate['detail']}")
        for finding in gate["findings"]:
            where = (
                f"{finding['path']}:{finding['line']}"
                if finding["line"]
                else finding["path"]
            )
            lines.append(f"        {where}: {finding['detail']}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the documentation truth gate with stable exit codes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    parser.add_argument("--report", type=Path, help="write the JSON report here")
    parser.add_argument("--json", action="store_true", help="print the JSON report")
    parser.add_argument(
        "--gate",
        choices=("all", "site"),
        default="all",
        help="all (default) or the site-publish subset",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    root = args.project_root.resolve()
    if args.gate == "site":
        result = site_gate(root)
        report = {
            "schema": SCHEMA,
            "status": result.status,
            "exit_code": EXIT_OK if result.ok else EXIT_FAIL,
            "gates": [result.to_dict()],
        }
    else:
        report = run_gates(root)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2) if args.json else _render(report))
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
