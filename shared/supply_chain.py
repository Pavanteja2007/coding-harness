"""Supply-chain controls: dependency vulnerability scan and signer identity.

Two release-blocking controls live here, both runnable offline so the release
build never depends on a third-party service being up:

* :func:`scan_dependencies` — resolves declared dependencies out of
  ``pyproject.toml`` / ``requirements*.txt`` / ``package.json`` and matches
  them against a pinned advisory database
  (:mod:`shared.security_advisories`). Deterministic: the same manifest always
  produces the same findings.
* :func:`verify_signer_identity` — verifies *who* signed, not *that* a
  signature exists. A release gate that only checks "is there a signature"
  accepts an artifact signed by anybody; this control requires the signer
  identity to match an explicit trust policy (workflow, repository, ref,
  environment, issuer/attestation predicate).

Both are exposed through ``python -m shared.supply_chain`` so a CI job can
gate a build on them. The command-line entry point exits non-zero on a
finding, which is what makes "signer-identity mismatch fails the build" a
mechanical property rather than a review checklist item.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Union

from shared.security import redact_text
from shared.security_advisories import ADVISORIES, ADVISORY_DB_VERSION, Advisory

__all__ = [
    "ADVISORY_DB_VERSION",
    "DependencyFinding",
    "SignerCheck",
    "SignerPolicy",
    "TrustPolicy",
    "dependency_update_plan",
    "main",
    "scan_dependencies",
    "scan_manifest_file",
    "verify_signer_identity",
]

PathLike = Union[str, Path]

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_VERSION_RE = re.compile(r"^\s*([0-9][0-9A-Za-z.+!_-]*)\s*$")
_REQUIREMENT_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"\s*(?:\[[^\]]*\])?"  # optional extras
    r"\s*(?:\(\s*|\s*)"  # PEP 508 parenthesised or plain marker
    r"(?P<op>===|==|>=|<=|~=|!=|>|<)?\s*"
    r"(?P<version>[0-9][0-9A-Za-z.+!_-]*)?\s*\)?\s*$"
)


def _version_key(value: str) -> tuple:
    """Return a comparable key for a PEP 440-ish version string.

    Deliberately simple: split on dots/dashes and compare numerically where
    possible. A prerelease suffix sorts before the plain release so
    ``1.2.0rc1 < 1.2.0``, which is the only ordering property the advisory
    matcher needs.
    """
    text = str(value or "").strip().lower().lstrip("v")
    if not text:
        return ()
    head = re.split(r"[-+]", text, maxsplit=1)[0]
    parts: list[Any] = []
    for chunk in head.split("."):
        if chunk.isdigit():
            parts.append((1, int(chunk), ""))
        else:
            digits = re.match(r"^(\d+)(.*)$", chunk)
            if digits:
                parts.append((1, int(digits.group(1)), digits.group(2)))
            else:
                parts.append((0, 0, chunk))
    # A prerelease marker sorts BEFORE the release it precedes.
    prerelease = 0 if re.search(r"(a|b|rc|dev|alpha|beta|pre)", text) else 1
    return (prerelease, tuple(parts), text)


def _normalize_package(name: str) -> str:
    return re.sub(r"[-_.]+", "-", str(name or "").strip()).casefold()


@dataclass(frozen=True)
class DependencyFinding:
    """One dependency that matches a pinned advisory."""

    package: str
    ecosystem: str
    declared: str
    advisory_id: str
    severity: str
    fixed_in: str
    summary: str

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible finding record."""
        return {
            "package": self.package,
            "ecosystem": self.ecosystem,
            "declared": self.declared,
            "advisory_id": self.advisory_id,
            "severity": self.severity,
            "fixed_in": self.fixed_in,
            "summary": self.summary,
        }


def _match_advisory(
    package: str, declared: str, advisory: Advisory
) -> Optional[DependencyFinding]:
    if advisory.ecosystem not in ("pypi", "npm"):
        return None
    if _normalize_package(advisory.package) != _normalize_package(package):
        return None
    found = _version_key(declared)
    if not found:
        return None
    introduced = _version_key(advisory.introduced)
    fixed = _version_key(advisory.fixed)
    if found < introduced:
        return None
    if found >= fixed:
        return None
    return DependencyFinding(
        package=_normalize_package(package),
        ecosystem=advisory.ecosystem,
        declared=declared,
        advisory_id=advisory.advisory_id,
        severity=advisory.severity,
        fixed_in=advisory.fixed,
        summary=advisory.summary,
    )


# ---------------------------------------------------------------------------
# manifest parsing
# ---------------------------------------------------------------------------


def _parse_pyproject(text: str) -> list[tuple[str, str, str]]:
    """Return ``(ecosystem, package, version)`` triples from a pyproject.

    Uses ``tomllib`` on 3.11+ and ``tomli`` when available. When neither
    parser exists, it degrades to a bounded reader that collects every quoted
    string in the file which parses as a PEP 508 requirement. That reader is a
    deliberate over-approximation — it can pick up a build-time requirement
    such as ``requires = ["setuptools==84.0.0"]`` — because an approximate
    answer that fails closed on unpinned entries is safer than refusing to
    scan a release at all.
    """
    data: Optional[Mapping[str, Any]] = None
    for module_name in ("tomllib", "tomli"):
        try:
            module = __import__(module_name)
        except ImportError:
            continue
        try:
            data = module.loads(text)
        except Exception:
            data = None
        break
    if isinstance(data, Mapping):
        rows: list[tuple[str, str, str]] = []
        project = data.get("project")
        if isinstance(project, Mapping):
            rows.extend(_pyproject_dependency_list(project.get("dependencies")))
            optional = project.get("optional-dependencies")
            if isinstance(optional, Mapping):
                for group in optional.values():
                    rows.extend(_pyproject_dependency_list(group))
        build_system = data.get("build-system")
        if isinstance(build_system, Mapping):
            rows.extend(_pyproject_dependency_list(build_system.get("requires")))
        return rows
    rows = []
    seen: set[tuple[str, str]] = set()
    for match in re.finditer(r"""["']([^"'\n]{2,200})["']""", text):
        for ecosystem, name, version in _parse_requirement(match.group(1)):
            key = (ecosystem, name, version)
            if key in seen:
                continue
            seen.add(key)
            rows.append((ecosystem, name, version))
    return rows


def _pyproject_dependency_list(value: Any) -> list[tuple[str, str, str]]:
    if not isinstance(value, (list, tuple)):
        return []
    rows: list[tuple[str, str, str]] = []
    for item in value:
        if isinstance(item, str):
            rows.extend(_parse_requirement(item))
    return rows


def _parse_requirement(requirement: str) -> list[tuple[str, str, str]]:
    """Return ``(ecosystem, name, version)`` for one requirement string.

    A requirement with no operator (``"requests"``) or one pinned by a direct
    URL/VCS ref (``"pkg @ git+https://..."``) yields an empty version, which
    the scan reports as unpinned. That is deliberate: the advisory matcher
    cannot reason about a version it does not know, and pretending otherwise
    would turn an unreviewable dependency into a silent pass.
    """
    text = str(requirement or "").split(";")[0].strip().strip(",").strip()
    if not text or text.startswith("#") or text.startswith("-"):
        return []
    if "@" in text:
        name = text.split("@", 1)[0].strip()
        return [("pypi", name, "")] if _NAME_RE.match(name) else []
    match = _REQUIREMENT_RE.match(text)
    if match is None:
        return []
    name = match.group("name")
    if not _NAME_RE.match(name):
        return []
    operator = match.group("op") or ""
    version = (match.group("version") or "").strip()
    if operator and operator not in {"==", "==="} and not version:
        # A range like ">=2" bounds a floor; report the floor as the declared
        # version so the matcher still refuses anything below it.
        return [("pypi", name, version)]
    return [("pypi", name, version)]


def _parse_requirements_txt(text: str) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped or stripped.startswith("-"):
            continue
        rows.extend(_parse_requirement(stripped))
    return rows


def _parse_package_json(text: str) -> list[tuple[str, str, str]]:
    try:
        payload = json.loads(text)
    except ValueError:
        return []
    if not isinstance(payload, Mapping):
        return []
    rows: list[tuple[str, str, str]] = []
    for section in (
        "dependencies",
        "devDependencies",
        "peerDependencies",
        "optionalDependencies",
    ):
        block = payload.get(section)
        if not isinstance(block, Mapping):
            continue
        for name, spec in block.items():
            if not isinstance(name, str):
                continue
            version = str(spec or "").strip().lstrip("^~>=< ")
            if version and not _VERSION_RE.match(version):
                version = ""
            rows.append(("npm", name, version))
    return rows


_MANIFEST_READERS = {
    "pyproject.toml": _parse_pyproject,
    "package.json": _parse_package_json,
}


def _read_text(path: Path, limit: int = 2 * 1024 * 1024) -> Optional[str]:
    try:
        if path.stat().st_size > limit:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def _collect_dependencies(root: Path) -> list[tuple[str, str, str, str]]:
    """Return ``(ecosystem, package, version, manifest)`` for one tree."""
    rows: list[tuple[str, str, str, str]] = []
    for name, reader in _MANIFEST_READERS.items():
        candidate = root / name
        if candidate.is_file():
            text = _read_text(candidate)
            if text is None:
                continue
            for ecosystem, package, version in reader(text):
                rows.append((ecosystem, package, version, name))
    for candidate in sorted(root.glob("requirements*.txt")):
        text = _read_text(candidate)
        if text is None:
            continue
        for ecosystem, package, version in _parse_requirements_txt(text):
            rows.append((ecosystem, package, version, candidate.name))
    return rows


# ---------------------------------------------------------------------------
# the scan
# ---------------------------------------------------------------------------


def scan_dependencies(
    root: PathLike = ".",
    *,
    advisories: Sequence[Advisory] = ADVISORIES,
    fail_on: Sequence[str] = ("critical", "high"),
) -> dict[str, Any]:
    """Scan a dependency tree against the pinned advisory database.

    Returns a report dict with ``findings``, the thresholds, the advisory DB
    version, and an ``ok`` boolean. A dependency with no exact version pin is
    reported as an ``unpinned`` finding, because an unpinned dependency is
    unreviewable: today's answer says nothing about the release artifact.

    Never raises for a missing or unreadable manifest: an absent tree is
    reported honestly as ``no_manifests`` with ``ok`` decided by ``fail_on``
    only when there is something to fail on.
    """
    base = Path(root).expanduser()
    rows = _collect_dependencies(base)
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for ecosystem, package, version, manifest in rows:
        if not version:
            key = (ecosystem, package)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                {
                    "package": _normalize_package(package),
                    "ecosystem": ecosystem,
                    "declared": version,
                    "advisory_id": "UNPINNED",
                    "severity": "medium",
                    "fixed_in": "",
                    "summary": f"dependency is not version-pinned in {manifest}",
                }
            )
            continue
        for advisory in advisories:
            match = _match_advisory(package, version, advisory)
            if match is None:
                continue
            key = (match.ecosystem, match.package, match.advisory_id)
            if key in seen:
                continue
            seen.add(key)
            findings.append(match.as_dict())
    findings.sort(
        key=lambda item: (item["severity"], item["package"], item["advisory_id"])
    )
    blocking = [item for item in findings if item["severity"] in set(fail_on)]
    return {
        "root": str(base),
        "advisory_db_version": ADVISORY_DB_VERSION,
        "dependencies_scanned": len(rows),
        "manifests": sorted({row[3] for row in rows}),
        "findings": findings,
        "blocking": len(blocking),
        "fail_on": list(fail_on),
        "ok": not blocking,
    }


def scan_manifest_file(path: PathLike, **kwargs: Any) -> dict[str, Any]:
    """Scan a single manifest file (or the directory that contains it)."""
    target = Path(path).expanduser()
    return scan_dependencies(target if target.is_dir() else target.parent, **kwargs)


# ---------------------------------------------------------------------------
# signer identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignerPolicy:
    """The identity a release artifact must have been signed by.

    Every populated field is a required match. An empty policy matches
    everything, so :func:`verify_signer_identity` refuses an empty policy
    rather than treating "no expectation" as "verified" — otherwise a
    misconfigured gate would silently pass, which is the exact failure this
    control exists to prevent.
    """

    subject: str = ""
    issuer: str = ""
    repository: str = ""
    workflow: str = ""
    environment: str = ""
    ref: str = ""
    audience: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible policy record."""
        return {
            "subject": self.subject,
            "issuer": self.issuer,
            "repository": self.repository,
            "workflow": self.workflow,
            "environment": self.environment,
            "ref": self.ref,
            "audience": self.audience,
        }

    def is_empty(self) -> bool:
        """Return whether the policy expresses no expectation at all."""
        return not any(
            str(getattr(self, name) or "").strip()
            for name in (
                "subject",
                "issuer",
                "repository",
                "workflow",
                "environment",
                "ref",
                "audience",
            )
        )


@dataclass(frozen=True)
class SignerCheck:
    """The result of verifying a signature's *identity*."""

    ok: bool
    reason: str
    mismatches: tuple[str, ...] = ()
    signer_subject: str = ""
    signature_present: bool = False
    policy: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible check record."""
        return {
            "ok": self.ok,
            "reason": self.reason,
            "mismatches": list(self.mismatches),
            "signer_subject": self.signer_subject,
            "signature_present": self.signature_present,
            "policy": dict(self.policy),
        }


# ``TrustPolicy`` is the name used by the release gate; kept as an alias so
# callers can spell the intent either way.
TrustPolicy = SignerPolicy


def _claim(claims: Mapping[str, Any], *names: str) -> str:
    for name in names:
        if name in claims:
            value = claims[name]
            if isinstance(value, (str, int, float, bool)):
                return str(value)
            if isinstance(value, Mapping):
                for key in ("slug", "name", "login", "identity", "value"):
                    if key in value and isinstance(value[key], str):
                        return value[key]
    return ""


def verify_signer_identity(
    claims: Optional[Mapping[str, Any]],
    policy: Union[SignerPolicy, Mapping[str, Any]],
    *,
    signature_present: Optional[bool] = None,
) -> SignerCheck:
    """Verify that the SIGNER IDENTITY matches the trust policy.

    ``claims`` is the verified attestation/provenance payload (an OIDC-style
    claim set, a Sigstore certificate subject, or an equivalent). The check
    is identity-first: a missing signature, an empty policy, or any single
    mismatched dimension all fail. It never returns ``ok`` on "a signature
    existed", which is the presence-only check the ceiling prompt calls out.
    """
    expectation = (
        policy
        if isinstance(policy, SignerPolicy)
        else SignerPolicy(
            **{
                key: str(value)
                for key, value in dict(policy or {}).items()
                if key in SignerPolicy.__dataclass_fields__
            }
        )
    )
    if expectation.is_empty():
        return SignerCheck(
            False,
            "trust policy is empty; an empty policy cannot verify an identity",
            policy=expectation.as_dict(),
        )
    present = bool(claims) if signature_present is None else bool(signature_present)
    if not present:
        return SignerCheck(
            False,
            "no signature or attestation is present",
            policy=expectation.as_dict(),
        )
    values = dict(claims or {})

    observed = {
        "subject": _claim(values, "subject", "sub", "signer", "email", "identity"),
        "issuer": _claim(values, "issuer", "iss"),
        "repository": _claim(values, "repository", "repository_owner_id", "repo"),
        "workflow": _claim(values, "workflow", "workflow_ref", "job_workflow_ref"),
        "environment": _claim(values, "environment", "deployment_environment"),
        "ref": _claim(values, "ref", "git_ref", "tag"),
        "audience": _claim(values, "audience", "aud", "job_workflow_ref"),
    }
    mismatches: list[str] = []
    for name, expected_value in expectation.as_dict().items():
        wanted = str(expected_value or "").strip()
        if not wanted:
            continue
        if _identity_mismatch(name, wanted, observed.get(name, "")):
            mismatches.append(name)
    if mismatches:
        return SignerCheck(
            False,
            "signer identity does not match the trust policy: " + ", ".join(mismatches),
            tuple(mismatches),
            observed["subject"],
            True,
            expectation.as_dict(),
        )
    return SignerCheck(True, "", (), observed["subject"], True, expectation.as_dict())


def _identity_mismatch(dimension: str, expected: str, observed: str) -> bool:
    """Compare one identity dimension, tolerating a value-shaped expectation.

    A repository expectation may be given as an id or a slug, and a subject as
    a full email or its local part; the check is a case-insensitive exact or
    suffix match rather than a substring, so ``attacker-repo`` cannot satisfy
    an expectation of ``neo-agent-cli``.
    """
    want = str(expected or "").strip().casefold()
    got = str(observed or "").strip().casefold()
    if not want:
        return False
    if not got:
        return True
    if got == want:
        return False
    if dimension in {"repository", "workflow", "environment", "ref"}:
        return not (got.endswith("/" + want) or got.endswith(":" + want))
    if dimension == "subject":
        return not got.endswith("@" + want) and want not in got.split("/")[-1]
    return True


# ---------------------------------------------------------------------------
# dependency update PRs (human review required)
# ---------------------------------------------------------------------------


def dependency_update_plan(
    root: PathLike = ".",
    *,
    advisories: Sequence[Advisory] = ADVISORIES,
) -> dict[str, Any]:
    """Produce a reviewable dependency-update plan for the release build.

    The plan is data, never an action: it names the packages to move, the
    advisory each move closes, and the exact version to move to. It always
    sets ``human_review_required`` and ``auto_merge`` to False. Automated
    update PRs are a *request to a human*, not a self-approving bot; a tool
    that could merge its own bumps would be a supply-chain hole, not a
    feature.
    """
    report = scan_dependencies(root, advisories=advisories, fail_on=())
    updates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for finding in report["findings"]:
        if finding["advisory_id"] == "UNPINNED":
            continue
        key = (finding["package"], finding["fixed_in"])
        if key in seen:
            continue
        seen.add(key)
        updates.append(
            {
                "package": finding["package"],
                "ecosystem": finding["ecosystem"],
                "from": finding["declared"],
                "to": finding["fixed_in"],
                "closes": [finding["advisory_id"]],
                "severity": finding["severity"],
            }
        )
    return {
        "root": str(Path(root).expanduser()),
        "advisory_db_version": ADVISORY_DB_VERSION,
        "updates": updates,
        "update_count": len(updates),
        "human_review_required": True,
        "auto_merge": False,
        "note": (
            "Bumps are proposals for a reviewed pull request. A machine that "
            "can approve its own dependency updates is a supply-chain hole."
        ),
    }


# ---------------------------------------------------------------------------
# CLI — the release-build gate
# ---------------------------------------------------------------------------


def _load_policy_file(path: Optional[str]) -> SignerPolicy:
    if not path:
        return SignerPolicy()
    try:
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"signer policy file is unreadable: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise SystemExit("signer policy file must contain a JSON object")
    known = set(SignerPolicy.__dataclass_fields__)
    return SignerPolicy(
        **{key: str(value) for key, value in payload.items() if key in known}
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the supply-chain gate; non-zero exit means the build must fail."""
    parser = argparse.ArgumentParser(
        prog="python -m shared.supply_chain",
        description="dependency vulnerability scan + signer identity verification",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser(
        "scan", help="scan dependencies against the pinned advisories"
    )
    scan.add_argument("--root", default=".")
    scan.add_argument(
        "--fail-on",
        default="critical,high",
        help="comma-separated severities that fail the gate (default critical,high)",
    )
    scan.add_argument("--json", action="store_true")

    plan = subparsers.add_parser(
        "update-plan", help="print a human-reviewed dependency update plan"
    )
    plan.add_argument("--root", default=".")
    plan.add_argument("--json", action="store_true")

    signer = subparsers.add_parser(
        "verify-signer", help="verify the signer identity of a release artifact"
    )
    signer.add_argument(
        "--claims", required=True, help="path to the verified claims JSON"
    )
    signer.add_argument("--policy", default="", help="path to the trust policy JSON")
    signer.add_argument(
        "--no-signature",
        action="store_true",
        help="assert that no signature is present (proves presence-only gates fail)",
    )
    signer.add_argument("--json", action="store_true")

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "scan":
        report = scan_dependencies(
            args.root,
            fail_on=tuple(
                item.strip() for item in args.fail_on.split(",") if item.strip()
            ),
        )
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(
                f"scanned {report['dependencies_scanned']} dependencies in "
                f"{len(report['manifests'])} manifest(s) against advisories "
                f"{report['advisory_db_version']}"
            )
            for finding in report["findings"]:
                print(
                    f"  [{finding['severity']}] {finding['package']}=={finding['declared']} "
                    f"{finding['advisory_id']} fixed in {finding['fixed_in'] or 'n/a'}"
                )
            print("PASS" if report["ok"] else f"FAIL ({report['blocking']} blocking)")
        return 0 if report["ok"] else 2

    if args.command == "update-plan":
        result = dependency_update_plan(args.root)
        print(
            json.dumps(result, indent=2, sort_keys=True)
            if args.json
            else _render_plan(result)
        )
        return 0

    if args.command == "verify-signer":
        claims: Optional[Mapping[str, Any]] = None
        if not args.no_signature:
            try:
                payload = json.loads(
                    Path(args.claims).expanduser().read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                print(f"signer claims are unreadable: {exc}", file=sys.stderr)
                return 2
            claims = payload if isinstance(payload, Mapping) else None
        policy = _load_policy_file(args.policy)
        result = verify_signer_identity(
            claims, policy, signature_present=False if args.no_signature else None
        )
        if args.json:
            print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
        else:
            print(
                f"signer={redact_text(result.signer_subject) or '<none>'} "
                f"result={'PASS' if result.ok else 'FAIL'}"
            )
            if not result.ok:
                print(f"  reason: {result.reason}")
        return 0 if result.ok else 2

    parser.error(f"unknown command: {args.command}")
    return 2


def _render_plan(plan: Mapping[str, Any]) -> str:
    lines = [
        f"{plan['update_count']} dependency update(s) proposed "
        f"(human review required: {plan['human_review_required']}, "
        f"auto-merge: {plan['auto_merge']})"
    ]
    for update in plan["updates"]:
        lines.append(
            f"  {update['package']}: {update['from']} -> {update['to']} "
            f"({', '.join(update['closes'])})"
        )
    if not plan["updates"]:
        lines.append("  (no advisory-matching upgrades found)")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
