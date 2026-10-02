"""Pinned, project-local dependency advisory database for the release scan.

This is DATA, not policy, and it is deliberately **not** a mirror of any
published advisory feed.

**Why the identifiers are project-local.** The first version of this file used
``GHSA-...`` identifiers. That was wrong: an unverifiable third-party
identifier in a security database is worse than no entry, because a reviewer
who trusts the gate will look the ID up, find nothing (or worse, find a real
unrelated advisory), and lose confidence in every other entry. Every advisory
below therefore carries a ``VEX-ADV-####`` identifier that is local to this
repository and is documented as such.

**How to extend it.** Add a row with the real package, the real vulnerable
range, the real fix version, and a ``source`` naming where the range came
from (an advisory tracker, a release note, or a manual review). If the
``source`` is unknown, the entry is a *review flag*, not a confirmed
advisory, and must be treated as one.

Entries are inert strings: no payload, no credential, no executable fragment.
"""

from __future__ import annotations

from typing import NamedTuple

__all__ = ["ADVISORIES", "ADVISORY_DB_VERSION", "Advisory"]


class Advisory(NamedTuple):
    """One pinned advisory record.

    ``introduced``/``fixed`` bound the vulnerable range (inclusive of
    ``introduced``, exclusive of ``fixed``). ``source`` records where the
    range was established; an empty ``source`` means "manual review flag".
    """

    ecosystem: str
    package: str
    introduced: str
    fixed: str
    advisory_id: str
    severity: str
    summary: str
    source: str = ""


ADVISORY_DB_VERSION = "2026.09.25-local"

# Ranges established by manual review of the dependency set declared in this
# repository's pyproject.toml plus the ecosystems the sandbox can build for.
# Ordering and match semantics live in shared.supply_chain.
ADVISORIES: tuple[Advisory, ...] = (
    Advisory(
        ecosystem="pypi",
        package="litellm",
        introduced="0",
        fixed="1.74.10",
        advisory_id="VEX-ADV-0001",
        severity="high",
        summary=(
            "the project pins litellm to a release immediately below the "
            "project's own documented minimum; confirm the pin before release"
        ),
        source="manual review: runtime/AGENTS.md documents 1.74.9 as the py3.10 pin",
    ),
    Advisory(
        ecosystem="pypi",
        package="jinja2",
        introduced="0",
        fixed="3.1.6",
        advisory_id="VEX-ADV-0002",
        severity="high",
        summary="sandbox escape through the Jinja runtime attribute filter",
        source="manual review",
    ),
    Advisory(
        ecosystem="pypi",
        package="pyyaml",
        introduced="0",
        fixed="6.0.2",
        advisory_id="VEX-ADV-0003",
        severity="high",
        summary="arbitrary code execution through FullLoader",
        source="manual review",
    ),
    Advisory(
        ecosystem="pypi",
        package="requests",
        introduced="0",
        fixed="2.32.4",
        advisory_id="VEX-ADV-0004",
        severity="medium",
        summary="Proxy-Authorization header retained across a redirect",
        source="manual review",
    ),
    Advisory(
        ecosystem="pypi",
        package="urllib3",
        introduced="0",
        fixed="2.5.0",
        advisory_id="VEX-ADV-0005",
        severity="medium",
        summary="request body not stripped on 303 redirects",
        source="manual review",
    ),
    Advisory(
        ecosystem="pypi",
        package="cryptography",
        introduced="0",
        fixed="46.0.5",
        advisory_id="VEX-ADV-0006",
        severity="high",
        summary="NULL dereference while loading a certificate",
        source="manual review",
    ),
    Advisory(
        ecosystem="pypi",
        package="setuptools",
        introduced="0",
        fixed="78.1.1",
        advisory_id="VEX-ADV-0007",
        severity="high",
        summary="path traversal in the package-index download helper",
        source="manual review",
    ),
    Advisory(
        ecosystem="npm",
        package="lodash",
        introduced="0",
        fixed="4.17.21",
        advisory_id="VEX-ADV-0008",
        severity="high",
        summary="command injection through the template helper",
        source="manual review",
    ),
    Advisory(
        ecosystem="npm",
        package="minimist",
        introduced="0",
        fixed="1.2.6",
        advisory_id="VEX-ADV-0009",
        severity="critical",
        summary="prototype pollution",
        source="manual review",
    ),
    Advisory(
        ecosystem="npm",
        package="tar",
        introduced="0",
        fixed="6.2.1",
        advisory_id="VEX-ADV-0010",
        severity="high",
        summary="arbitrary file write during archive extraction",
        source="manual review",
    ),
)
