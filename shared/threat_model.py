"""Machine-readable threat model for the shared trust boundary."""

from __future__ import annotations

import copy
from typing import Any, Mapping

__all__ = [
    "THREAT_MODEL",
    "THREAT_MODEL_VERSION",
    "get_threat_model",
    "validate_threat_model",
]

THREAT_MODEL_VERSION = "1"

THREAT_MODEL: Mapping[str, Any] = {
    "version": THREAT_MODEL_VERSION,
    "scope": "shared trust boundary for prompts, repositories, tools, extensions, traces, and exports",
    "assets": [
        {
            "id": "credentials",
            "description": "API keys, tokens, cookies, private keys, and environment secrets",
        },
        {
            "id": "workspace",
            "description": "Repository files, VCS metadata, and host filesystem",
        },
        {
            "id": "authority",
            "description": "Approval decisions, verifier results, and tool permissions",
        },
        {
            "id": "privacy",
            "description": "Prompts, responses, source paths, logs, and user-provided data",
        },
        {
            "id": "integrity",
            "description": "Authoritative traces, state, telemetry, and exported evidence",
        },
    ],
    "trust_boundaries": [
        {
            "id": "untrusted_input",
            "from": "issue, repository, web, MCP, plugin, skill",
            "to": "model context and tools",
        },
        {
            "id": "child_process",
            "from": "harness or tool",
            "to": "sandbox or local process",
        },
        {
            "id": "persistence",
            "from": "runtime event or status surface",
            "to": "logs, traces, reports, and exports",
        },
        {
            "id": "extension",
            "from": "operator-installed extension",
            "to": "instructions, commands, and connectors",
        },
    ],
    "threats": [
        {
            "id": "prompt_injection",
            "category": "prompt_injection",
            "severity": "critical",
            "description": "Untrusted text impersonates a system instruction or redirects the agent's authority.",
            "mitigations": [
                "shared.security.detect_prompt_injection",
                "adversary_review",
                "sandbox and approval gates",
            ],
            "failure_gate": "block",
        },
        {
            "id": "hostile_repository",
            "category": "hostile_repo",
            "severity": "high",
            "description": "Repository instructions, tests, symlinks, or files attempt to steer or escape the workspace.",
            "mitigations": [
                "path containment",
                "symlink rejection",
                "protected paths",
                "verifier gate",
            ],
            "failure_gate": "block",
        },
        {
            "id": "mcp_boundary",
            "category": "mcp",
            "severity": "high",
            "description": "An MCP server or result returns hostile instructions, oversized data, or secret-bearing output.",
            "mitigations": [
                "source labelling",
                "adversary review",
                "bounded output",
                "secret redaction",
            ],
            "failure_gate": "block",
        },
        {
            "id": "plugin_boundary",
            "category": "plugin",
            "severity": "high",
            "description": "A plugin extends tools, commands, skills, or connectors beyond the operator's intent.",
            "mitigations": [
                "manifest validation",
                "explicit allowlists",
                "approval audit",
                "disabled markers",
            ],
            "failure_gate": "block",
        },
        {
            "id": "skill_boundary",
            "category": "skill",
            "severity": "high",
            "description": "A skill body or metadata contains prompt injection, secret requests, or unsafe commands.",
            "mitigations": [
                "bounded reads",
                "origin labels",
                "adversary review",
                "no implicit authority",
            ],
            "failure_gate": "block",
        },
        {
            "id": "symlink_and_traversal",
            "category": "containment",
            "severity": "critical",
            "description": "Traversal, drive-relative paths, or symlink components redirect reads or writes outside the root.",
            "mitigations": [
                "safe_relative_path",
                "require_contained",
                "no symlink components",
                "no-follow reads",
            ],
            "failure_gate": "block",
        },
        {
            "id": "secret_exposure",
            "category": "secrets",
            "severity": "critical",
            "description": "Credentials enter prompts, traces, events, logs, status output, reports, or exports.",
            "mitigations": [
                "recursive redaction",
                "privacy modes",
                "secret-safe environment scrubbing",
            ],
            "failure_gate": "block",
        },
        {
            "id": "supply_chain",
            "category": "supply_chain",
            "severity": "high",
            "description": "A dependency, build hook, plugin package, or remote extension executes unreviewed code.",
            "mitigations": [
                "dependency manifests as policy input",
                "network and process boundaries",
                "health metrics",
            ],
            "failure_gate": "block",
        },
        {
            "id": "egress_exfiltration",
            "category": "egress",
            "severity": "critical",
            "description": (
                "The harness opens an outbound connection to a host nobody "
                "allowlisted, so untrusted content can be shipped off the host "
                "and the agent can be steered into reaching attacker infrastructure."
            ),
            "mitigations": [
                "shared.egress.EgressPolicy deny-by-default allowlist",
                "host blocklist re-applied after the allowlist",
                "egress decision recorded on every fetch and networked sandbox call",
                "pre-spawn sandbox argv isolation gate",
            ],
            "failure_gate": "block",
        },
        {
            "id": "approval_integrity",
            "category": "approval_integrity",
            "severity": "critical",
            "description": (
                "An approval is granted for one effect and then executed for a "
                "different one, so the command a human reviewed is not the "
                "command that ran."
            ),
            "mitigations": [
                "shared.approval.canonical_effect derived from the executor's own values",
                "approval bound to a digest of the canonical effect",
                "reconstruct-and-recheck immediately before execution",
                "material change raises ApprovalStale instead of executing",
            ],
            "failure_gate": "block",
        },
        {
            "id": "memory_poisoning",
            "category": "provenance",
            "severity": "critical",
            "description": (
                "A stored memory row, an ingested state file, or an MCP result "
                "reads as a system instruction, turning durable storage into a "
                "persistent prompt-injection channel."
            ),
            "mitigations": [
                "memory writes require explicit provenance",
                "system-authority claims are quarantined for every actor",
                "MCP results are reviewed before they reach a client",
                "run receipts record what a run actually used",
            ],
            "failure_gate": "block",
        },
        {
            "id": "sandbox_escape",
            "category": "containment",
            "severity": "critical",
            "description": (
                "A container reaches the host Docker socket, a host secret "
                "directory, or an added capability, which converts a "
                "sandboxed command into host takeover."
            ),
            "mitigations": [
                "assert_sandbox_argv_isolated pre-spawn static gate",
                "no runtime socket or host secret mount is ever assembled",
                "cap-drop ALL and no-new-privileges are fixed by policy",
                "image digest pinning records the artifact source",
            ],
            "failure_gate": "block",
        },
    ],
    "invariants": [
        "Observability never changes a task outcome.",
        "Security violations are failed gates rather than warnings.",
        "Authoritative trace.jsonl is read-only for derived views and exporters.",
        "Derived traces disclose no more than their selected privacy mode allows.",
        "No credential value is printed, persisted, or included in provider fingerprints.",
        "One redaction implementation serves trace, overlay, diff, memory, and errors.",
        "Untrusted content is labelled and policy-checked before entering trusted context.",
        "Egress is denied unless a host is explicitly allowlisted.",
        "An approval is bound to a digest of the canonical effect and re-checked before execution.",
        "Memory cannot override the operator's instructions, whatever wrote it.",
        "A skipped environment lane is reported blocked, never passed.",
    ],
    "untrusted_sources": [
        "issue",
        "repository_instructions",
        "web",
        "skill",
        "plugin",
        "mcp",
        "memory",
    ],
}


def get_threat_model() -> dict[str, Any]:
    """Return an isolated copy of the machine-readable threat model."""
    return copy.deepcopy(dict(THREAT_MODEL))


def validate_threat_model(model: Mapping[str, Any] | None = None) -> list[str]:
    """Return human-readable schema errors; an empty list means valid."""
    value = dict(model or THREAT_MODEL)
    errors: list[str] = []
    for key in (
        "version",
        "scope",
        "assets",
        "trust_boundaries",
        "threats",
        "invariants",
    ):
        if key not in value:
            errors.append(f"missing {key}")
    threats = value.get("threats")
    if not isinstance(threats, list) or not threats:
        errors.append("threats must be a non-empty list")
    else:
        categories = {
            str(item.get("category", ""))
            for item in threats
            if isinstance(item, Mapping)
        }
        required = {
            "prompt_injection",
            "hostile_repo",
            "mcp",
            "plugin",
            "skill",
            "containment",
            "secrets",
            "supply_chain",
            "egress",
            "approval_integrity",
            "provenance",
        }
        missing = sorted(required - categories)
        if missing:
            errors.append("missing threat categories: " + ", ".join(missing))
    return errors
