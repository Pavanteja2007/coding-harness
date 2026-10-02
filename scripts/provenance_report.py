"""T5.W2.5 — the vendored-code provenance spine.

WHY THIS EXISTS NOW, BEFORE ANY PORT HAS HAPPENED
-------------------------------------------------

Phase 7 needs a provenance report. Every port in Phase 2 and later has to be
able to declare itself from the moment it lands. This module is the SPINE that
report is built on, and it is deliberately seeded **empty**: today this
repository vendors no third-party code, so the correct seed is a clean report
and a gate that will fail the first time somebody copies something in without
declaring it.

Starting empty is the point. A provenance mechanism introduced alongside the
first port is a mechanism that has never been wrong yet, and this repository
has already shipped four mechanisms with no call site.

THE HEADER FORMAT (this is the contract; copy it verbatim)
----------------------------------------------------------

A vendored file declares itself with a contiguous comment block in its first
:data:`HEADER_SCAN_LINES` lines::

    # vendored: yes
    # upstream: https://github.com/owner/repo
    # upstream-commit: 0f1e2d3c4b5a69788796a5b4c3d2e1f009876543
    # upstream-path: src/thing.py
    # licence: MIT
    # beats-ours-because: <the reason, in one sentence>
    # modified: yes|no

Field rules, each of which exists because the alternative is a lie:

``vendored``
    Must be exactly ``yes``. A block with ``vendored: maybe`` is not a
    declaration.
``upstream``
    A URL. Not a project name, not "internal", not "-".
``upstream-commit``
    A 7-40 character hex SHA. **Required, and it is the field everyone skips.**
    A vendored file with no commit pin cannot be re-derived, cannot be
    diffed against its source, and cannot be audited when the upstream turns
    out to have had a vulnerability. A moving branch is not a pin.
``upstream-path``
    The path within the upstream project. Optional when the whole repository
    is taken, but then say so with ``upstream-path: .``.
``licence``
    The upstream licence identifier (MIT, Apache-2.0, BSD-3-Clause, ...).
``beats-ours-because``
    **The field that matters, and the one everyone skips.** It is not
    "mature", not "battle-tested", not "we needed it". It is the specific
    property that made copying better than writing: a benchmark we could not
    reproduce, a data structure with a published worst-case bound, an audit
    that took a year. A file whose only justification is "it worked" is a file
    nobody will defend at the next refactor, and it should be rewritten.
``modified``
    ``yes`` or ``no``. Required even when ``no``: an unstated modification is
    the case that makes a licence determination impossible.

HOW A FILE IS DECIDED TO BE VENDORED
------------------------------------

Three shapes, deliberately narrow so the gate does not cry wolf:

1. it carries an explicit ``vendored:`` block;
2. it lives under a vendor-shaped directory (``vendor/``, ``third_party/``, ...);
3. it carries a **third-party licence or copyright header** in its first
   :data:`HEADER_SCAN_LINES` lines.

Shape 3 is the one that catches an honest mistake, because somebody who copies
a file usually keeps the licence comment. It is matched against a closed table
of licence-header signatures; a project that has vendored nothing has **zero**
hits on it, which is the measured state of this tree.

WHAT THIS DELIBERATELY CANNOT SEE
---------------------------------

"Borrowed in spirit" - a reimplementation whose *approach* came from another
project - is **not mechanically detectable**, and this module does not pretend
otherwise. An attempt to detect it by keyword produces false positives on this
repository's own vocabulary: the word "derived" appears 135 files deep in the
sense of *computed from*, and a keyword scanner cannot tell that apart from
*ported from*. Prose attribution stays a reviewer's job; this gate covers only
what it can prove.

CLI:
    python scripts/provenance_report.py            # human report, exit 2 on gaps
    python scripts/provenance_report.py --json     # machine-readable
    python scripts/provenance_report.py --strict   # also fail on advisories
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]

#: How deep into a file the declaration block may start. A licence header can
#: legitimately follow a shebang and an encoding line, but not 400 lines of
#: code.
HEADER_SCAN_LINES = 40

#: Directories that mean "this subtree is not ours" by construction.
VENDOR_DIR_NAMES: Tuple[str, ...] = (
    "vendor",
    "vendored",
    "third_party",
    "3rdparty",
    "external",
    "_vendor",
)

#: File suffixes the scanner reads. Binary and asset types are excluded: a
#: vendored PNG has no header to fill in, and demanding one would produce a
#: gate everyone learns to disable.
SCANNED_SUFFIXES: Tuple[str, ...] = (
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".jsx",
    ".ts",
    ".tsx",
    ".sh",
    ".bash",
    ".ps1",
    ".cmd",
    ".go",
    ".rs",
    ".java",
    ".rb",
    ".php",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".hpp",
    ".css",
    ".scss",
    ".html",
    ".sql",
)

#: Trees that are generated, vendored wholesale by a package manager, or are
#: another terminal's scratch space. Each exclusion is here because including
#: it produced a false positive, not for convenience.
#:
#: ``.next`` and ``out`` were added by T5.W1.4 when the BINARY lane reported
#: two ``.wasm`` files under ``site/.next/server/edge-chunks/`` as undeclared
#: vendored binaries. They are Next.js build OUTPUT - the equivalent of the
#: ``dist``/``build`` entries already here - and a gate that flags a
#: framework's own compiler output as third-party code is a gate whose output
#: nobody reads. The exclusion is the fix; loosening the ``.wasm`` suffix
#: would have been the bug.
EXCLUDED_DIRS: Tuple[str, ...] = (
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "logs",
    "graphify-out",
    "Temp",
    "dist",
    "build",
    ".pytest_cache",
    ".ruff_cache",
    "probe_logs",
    ".qwen",
    ".shots",
    ".playwright-mcp",
    ".docker",
    ".harness",
    ".tox",
    ".mypy_cache",
    "site-v1-backup",
    ".next",
    "out",
    ".svelte-kit",
    ".nuxt",
    ".parcel-cache",
    ".turbo",
)

#: Third-party licence/copyright header signatures. Shape (3) of the detector.
#: Kept as a closed table so a new pattern is a reviewed edit rather than a
#: widening that starts flagging the project's own docs.
LICENCE_SIGNATURES: Tuple[Tuple[str, str], ...] = (
    ("mit", r"Permission is hereby granted, free of charge"),
    ("apache-2.0", r"Licensed under the Apache License, Version 2\.0"),
    ("bsd", r"Redistribution and use in source and binary forms"),
    ("mpl-2.0", r"Mozilla Public License Version 2\.0"),
    ("gpl", r"GNU GENERAL PUBLIC LICENSE\s+Version"),
    ("isc", r"Permission to use, copy, modify, and/or distribute this software"),
    ("cc0", r"CC0 1\.0 Universal|dedicates? all .* to the public domain"),
)

#: A copyright line naming somebody who is not this project's author. The
#: project author is excluded BY NAME, not by pattern, because "Copyright (c)
# 2026" appears in this repository's own files too.
PROJECT_AUTHOR = "Pavanteja2007"
COPYRIGHT_RX = re.compile(
    r"Copyright \(c\)\s*((?:19|20)\d\d)(?:\s*-\s*(?:19|20)\d\d)?,?\s*([^\n]+)"
)

#: The mandatory fields and whether each may be empty.
REQUIRED_FIELDS: Tuple[str, ...] = (
    "vendored",
    "upstream",
    "upstream-commit",
    "licence",
    "beats-ours-because",
    "modified",
)

#: Advisory (non-failing) fields: a declaration may legitimately omit them.
OPTIONAL_FIELDS: Tuple[str, ...] = ("upstream-path",)

FIELD_RX = re.compile(
    r"^[#/*;<!-]*\s*(?P<key>[a-z][a-z0-9-]*)\s*:\s*(?P<value>.*?)\s*$"
)
SHA_RX = re.compile(r"^[0-9a-f]{7,40}$")
URL_RX = re.compile(r"^(https?://|git@|ssh://)", re.IGNORECASE)


# --------------------------------------------------------------------------
# the declaration
# --------------------------------------------------------------------------


@dataclass
class Declaration:
    """A parsed ``vendored:`` block.

    :param path: repo-relative POSIX path of the file.
    :param fields: every ``key: value`` pair found in the block.
    :param line: the 1-indexed line the block starts on.
    :param how: which detector shape claimed the file, so a report can say
        WHY a file was considered at all.
    """

    path: str
    fields: Dict[str, str]
    line: int
    how: str
    licence_signature: str = ""

    def missing(self) -> List[str]:
        """Required fields that are absent OR blank."""
        out: List[str] = []
        for name in REQUIRED_FIELDS:
            if not str(self.fields.get(name, "")).strip():
                out.append(name)
        return out

    def invalid(self) -> List[str]:
        """Present fields whose VALUE does not satisfy its own rule.

        Separate from :meth:`missing` because "present but wrong" is a
        different defect: a commit pin of ``main`` is not a missing pin, it is
        a false one, and it is worse.
        """
        problems: List[str] = []
        vendored = str(self.fields.get("vendored", "")).strip().lower()
        if vendored and vendored != "yes":
            problems.append(f"vendored={vendored!r} (must be 'yes')")
        upstream = str(self.fields.get("upstream", "")).strip()
        if upstream and not URL_RX.match(upstream):
            problems.append(f"upstream={upstream!r} is not a URL")
        commit = str(self.fields.get("upstream-commit", "")).strip()
        if commit and not SHA_RX.match(commit.lower()):
            problems.append(
                f"upstream-commit={commit!r} is not a hex SHA -- a branch name "
                "or tag is not a pin, because it cannot be re-derived"
            )
        modified = str(self.fields.get("modified", "")).strip().lower()
        if modified and modified not in ("yes", "no"):
            problems.append(f"modified={modified!r} (must be 'yes' or 'no')")
        beats = str(self.fields.get("beats-ours-because", "")).strip()
        if beats and len(beats) < 15:
            problems.append(
                f"beats-ours-because is {beats!r} -- too short to be a reason"
            )
        return problems

    @property
    def complete(self) -> bool:
        """Whether this declaration would satisfy the gate."""
        return not self.missing() and not self.invalid()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "how": self.how,
            "licence_signature": self.licence_signature,
            "fields": dict(self.fields),
            "missing": self.missing(),
            "invalid": self.invalid(),
            "complete": self.complete,
        }


def _string_literal_lines(text: str) -> frozenset:
    """Return every line number covered by a string literal or docstring.

    Needed because a ``vendored:`` block *inside a docstring* is documentation,
    not a declaration. This module's own header-format example is exactly that
    case, and before this exclusion the scanner flagged ITSELF as an incomplete
    declaration - a self-referential false positive that would have taught
    everyone to ignore the report.
    """
    import ast

    covered: set = set()
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        # ValueError covers "source code string cannot contain null bytes",
        # which a real repository file can contain. An unparseable file simply
        # gets no string-literal exclusion; the comment scan still runs and a
        # genuine comment-block declaration is still found.
        return frozenset()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        end = getattr(node, "end_lineno", None)
        if end is None:
            continue
        covered.update(range(node.lineno, end + 1))
    return frozenset(covered)


def parse_declaration(text: str, path: str, how: str) -> Optional[Declaration]:
    """Return the first ``vendored:`` block in ``text``, or None.

    A block is a run of consecutive comment lines, so a stray
    ``beats-ours-because:`` mention inside prose cannot register as a
    declaration and a field from an unrelated block cannot be borrowed.

    Lines inside a string literal are skipped entirely, so this module's own
    documented header example is documentation rather than a declaration.
    """
    literal_lines: frozenset = frozenset()
    # Cheap pre-check before the expensive one. `ast.parse` over 741 files
    # costs minutes; a substring test costs nothing, and only a file that
    # actually mentions `vendored:` can produce a declaration.
    if "vendored:" in text:
        literal_lines = _string_literal_lines(text)
    lines = text.splitlines()[:HEADER_SCAN_LINES]
    fields: Dict[str, str] = {}
    start: Optional[int] = None
    for index, raw in enumerate(lines, start=1):
        if index in literal_lines:
            if start is not None:
                break
            continue
        stripped = raw.strip()
        if not stripped:
            continue
        is_comment = stripped.startswith(("#", "//", "*", "/*", "<!--", ";"))
        if not is_comment:
            # A blank line ends the block; a non-comment line ends it too.
            if start is not None:
                break
            continue
        match = FIELD_RX.match(stripped)
        if not match:
            if start is not None:
                break
            continue
        key = match.group("key")
        if start is None:
            if key != "vendored":
                continue
            start = index
        fields[key] = match.group("value")
    if start is None:
        return None
    return Declaration(path=path, fields=fields, line=start, how=how)


def licence_signature(text: str) -> str:
    """Return the third-party licence signature present in ``text``, or "".

    This project's own ``LICENSE`` and docs are excluded by the caller's file
    filter; here we only look at the header region.
    """
    head = "\n".join(text.splitlines()[:HEADER_SCAN_LINES])
    for name, pattern in LICENCE_SIGNATURES:
        if re.search(pattern, head, re.IGNORECASE):
            return name
    return ""


def third_party_copyright(text: str) -> str:
    """Return a copyright holder that is NOT this project's author, or "".

    The project author is excluded BY NAME. An earlier version also excluded
    any holder beginning with ``The `` -- which silently discarded the single
    most common real shape ("The Upstream Authors", "The Regents of ...") and
    would have let an Apache-headered copy through on that basis alone. Only
    the project's own author and an obvious placeholder are excluded now.
    """
    head = "\n".join(text.splitlines()[:HEADER_SCAN_LINES])
    for match in COPYRIGHT_RX.finditer(head):
        holder = match.group(2).strip()
        if not holder or PROJECT_AUTHOR.lower() in holder.lower():
            continue
        if holder.startswith("<") or holder.lower() in ("name", "author", "year"):
            continue
        return f"{match.group(1)} {holder}"
    return ""


# --------------------------------------------------------------------------
# THE BINARY PROVENANCE SHAPE  (T5.W1.4)
# --------------------------------------------------------------------------
#
# WHY A BINARY NEEDS A DIFFERENT SHAPE
# ------------------------------------
#
# A source file declares itself in a comment header, because a source file
# CAN carry a comment. A downloaded binary cannot: it is bytes, it has no
# header, and prepending a comment to an executable either corrupts it or
# produces a second file that is not the thing being shipped. So the binary
# case is not "the same shape with a different field list" -- it is a
# DIFFERENT SHAPE: a manifest entry, checked against the file on disk.
#
# The three fields a binary needs that a source file does not:
#
# ``version``
#     A source file is pinned by a COMMIT. A release archive of ripgrep is
#     not a git checkout you can diff against; it is a versioned artifact, and
#     "14.1.1" is its only identity. Without a version there is nothing to
#     upgrade, re-download, or report in a CVE response.
#
# ``sha256``
#     64 hex characters of the FILE, not of a source tree. This is the field
#     that makes the declaration verifiable: the scanner re-hashes the binary
#     on disk and fails if the recorded digest does not match, so a swapped
#     binary is a build failure rather than a supply-chain surprise. A source
#     declaration can be checked by reading the file; a binary's cannot.
#
# ``platform``
#     The same upstream version is a different artifact per OS/arch. Without
#     it, a macOS arm64 digest "covers" a Linux x86_64 file.
#
# And the two it shares with a source file, because they are the same
# obligations: ``upstream`` (a URL, so the archive is re-derivable) and
# ``licence`` (ripgrep is MIT; `fd` is MIT OR Apache-2.0, dual, and WHICH ONE
# was taken is a decision somebody has to write down - a dual-licensed
# dependency whose chosen licence is unstated is an unanswerable licence
# question).
#
# ``beats-ours-because`` is carried over UNCHANGED from the source shape. A
# downloaded binary is the most expensive kind of vendoring - it cannot be
# refactored, reviewed line by line, or fixed in place - so the justification
# bar is higher, not lower.

#: The manifest. A committed JSON document, because it must be machine-readable
#: and reviewable in a diff, and because a binary has nowhere to put a comment.
BINARY_MANIFEST_NAME = "provenance/binaries.json"

#: The shape's schema version, so a Phase 2 manifest that grows a field is
#: distinguishable from a Phase 1 one that is malformed.
BINARY_SCHEMA_VERSION = 1

#: Fields a binary entry MUST carry. ``upstream-commit`` is deliberately
#: ABSENT: a release archive has no commit, and requiring one would push
#: authors to write a fabricated SHA rather than to record the real version.
BINARY_REQUIRED_FIELDS: Tuple[str, ...] = (
    "path",
    "upstream",
    "version",
    "sha256",
    "platform",
    "licence",
    "beats-ours-because",
)

#: Advisory fields.
BINARY_OPTIONAL_FIELDS: Tuple[str, ...] = (
    "upstream-commit",
    "asset-name",
    "verified_on",
    "note",
)

SHA256_RX = re.compile(r"^[0-9a-f]{64}$")

#: Suffixes that mean "an executable artifact". A file with one of these that
#: has no manifest entry is reported as an UNDECLARED BINARY, which fails the
#: gate. Kept narrow on purpose: a repository full of ``.so``/``.dll`` build
#: residue would otherwise produce a wall of false positives, and a gate that
#: cries wolf is a gate people stop reading.
BINARY_SUFFIXES: Tuple[str, ...] = (
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".wasm",
    ".jar",
    ".a",
    ".lib",
    ".o",
    ".bin",
)


@dataclass
class BinaryDeclaration:
    """One entry of the binary manifest, checked against the file on disk.

    :param path: repo-relative POSIX path, as it appears in the manifest.
    :param fields: the manifest entry, verbatim.
    :param exists: whether the file is present in the tree.
    :param digest_matches: ``True``/``False``/``None`` when it could not be
        computed. ``None`` is a distinct answer from ``False``: "the digest
        could not be checked" must never read as "the digest is fine", and
        must never read as "the digest is wrong" either.
    """

    path: str
    fields: Dict[str, Any]
    exists: bool = False
    digest_matches: Optional[bool] = None
    measured_sha256: str = ""
    present_in_tree: bool = True

    def missing(self) -> List[str]:
        """Required keys that are absent or blank."""
        out: List[str] = []
        for name in BINARY_REQUIRED_FIELDS:
            value = self.fields.get(name)
            if value is None or not str(value).strip():
                out.append(name)
        return out

    def invalid(self) -> List[str]:
        """Present fields whose VALUE breaks its own rule."""
        problems: List[str] = []
        upstream = str(self.fields.get("upstream") or "").strip()
        if upstream and not URL_RX.match(upstream):
            problems.append(f"upstream={upstream!r} is not a URL")
        digest = str(self.fields.get("sha256") or "").strip().lower()
        if digest and not SHA256_RX.match(digest):
            problems.append(
                f"sha256={digest!r} is not 64 hex characters -- a binary's only "
                f"verifiable identity is its digest, so a short or symbolic one "
                f"makes the entry unverifiable"
            )
        platform = str(self.fields.get("platform") or "").strip()
        if platform and ("/" not in platform and "-" not in platform):
            problems.append(
                f"platform={platform!r} does not name an os/arch pair, so the "
                f"same entry would appear to cover every platform's artifact"
            )
        licence = str(self.fields.get("licence") or "").strip().lower()
        if licence and licence in ("mit or apache-2.0", "apache-2.0 or mit", "dual"):
            problems.append(
                f"licence={licence!r} does not say WHICH licence was taken; a "
                f"dual-licensed dependency with an unstated choice is an "
                f"unanswerable licence question"
            )
        beats = str(self.fields.get("beats-ours-because") or "").strip()
        if beats and len(beats) < 15:
            problems.append(
                f"beats-ours-because is {beats!r} -- too short to be a reason"
            )
        return problems

    @property
    def complete(self) -> bool:
        """Whether this entry would satisfy the gate."""
        return (
            bool(self.fields)
            and not self.missing()
            and not self.invalid()
            and self.exists
            and self.digest_matches is not False
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "fields": dict(self.fields),
            "exists_in_tree": self.exists,
            "digest_matches": self.digest_matches,
            "measured_sha256": self.measured_sha256,
            "missing": self.missing(),
            "invalid": self.invalid(),
            "complete": self.complete,
        }


def _sha256_file(path: Path) -> str:
    """SHA-256 of a file's bytes, read in chunks so a large binary is fine."""
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_binary_manifest(
    root: Path,
) -> Tuple[List[BinaryDeclaration], List[Dict[str, str]]]:
    """Load and CHECK the binary manifest.

    Returns ``(declarations, problems)``. ``problems`` carries a malformed or
    missing manifest rather than raising, because a broken manifest is a
    finding the gate should PRINT, and a gate that crashes on its own input
    teaches people to run it with output discarded.
    """
    path = Path(root) / BINARY_MANIFEST_NAME
    if not path.is_file():
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [], [
            {
                "path": BINARY_MANIFEST_NAME,
                "detail": f"unparseable: {type(exc).__name__}: {exc}",
            }
        ]
    if not isinstance(data, dict):
        return [], [
            {"path": BINARY_MANIFEST_NAME, "detail": "top level is not an object"}
        ]
    version = data.get("schema_version")
    if version != BINARY_SCHEMA_VERSION:
        return [], [
            {
                "path": BINARY_MANIFEST_NAME,
                "detail": f"schema_version={version!r}, expected "
                f"{BINARY_SCHEMA_VERSION}",
            }
        ]
    entries = data.get("binaries")
    if entries is None:
        return [], [{"path": BINARY_MANIFEST_NAME, "detail": "no `binaries` key"}]
    if not isinstance(entries, list):
        return [], [
            {"path": BINARY_MANIFEST_NAME, "detail": "`binaries` is not a list"}
        ]

    out: List[BinaryDeclaration] = []
    for raw in entries:
        if not isinstance(raw, dict):
            out.append(
                BinaryDeclaration(path=str(raw), fields={}, present_in_tree=False)
            )
            continue
        rel = str(raw.get("path") or "")
        target = Path(root) / rel
        exists = bool(rel) and target.is_file()
        measured = ""
        matches: Optional[bool] = None
        if exists:
            try:
                measured = _sha256_file(target)
                recorded = str(raw.get("sha256") or "").strip().lower()
                matches = bool(recorded) and measured == recorded
            except OSError:
                out.append(
                    BinaryDeclaration(
                        path=rel,
                        fields=raw,
                        exists=True,
                        digest_matches=None,
                        present_in_tree=False,
                    )
                )
                continue
        out.append(
            BinaryDeclaration(
                path=rel,
                fields=raw,
                exists=exists,
                digest_matches=matches,
                measured_sha256=measured,
            )
        )
    return out, []


def iter_binary_candidates(
    root: Path, *, excluded: Iterable[str] = EXCLUDED_DIRS
) -> Iterable[Path]:
    """Yield every vendored-BINARY-shaped file in the tree.

    Pruned in the walk for the same reason :func:`iter_candidate_files` is:
    this repository's own ``DOCTRINE.md`` §8 records a 345x difference between
    a pruning walk and a filtering one on this very tree.
    """
    skip = set(excluded)
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = [d for d in dirnames if d not in skip]
        here = Path(dirpath)
        for name in filenames:
            if not name.endswith(BINARY_SUFFIXES):
                continue
            yield here / name


#: Fields a RUNTIME-FETCHED binary entry must carry. Deliberately does NOT
#: require ``sha256``, and that asymmetry is the point.
#:
#: A binary fetched at download time is not in the tree, so there are no bytes
#: on disk to re-hash and nothing for the manifest's digest check to verify.
#: Demanding ``sha256`` anyway would push the author to record a digest they
#: cannot check, which is the paperwork version of the same dishonesty. What
#: the entry DOES require is ``digest-source`` (where the expected digest comes
#: from) and, when that source is not a digest committed to THIS repository,
#: a ``digest-unpinned-reason`` saying why.
#:
#: That is a real, named gap rather than a silent one: ripgrep 14.1.1 and
#: fd 10.2.0 are fetched from their GitHub release URLs and verified against
#: the ``.sha256`` sidecar published beside each asset, and the VERSION is
#: pinned in ``harness/search_engine.py`` while the DIGEST is not committed
#: anywhere in this tree. A replaced release asset with a replaced sidecar
#: would be accepted. Committing the eight digests closes it, and P2 vendors
#: more binaries than P1 does.
RUNTIME_FETCH_REQUIRED_FIELDS: Tuple[str, ...] = (
    "name",
    "upstream",
    "version",
    "platforms",
    "licence",
    "beats-ours-because",
    "digest-source",
)

#: The reason required when the digest is not committed to this repository.
UNPINNED_REASON_FIELD = "digest-unpinned-reason"

#: A dual-licensed upstream must name the licence actually relied on. The
#: two live cases, verified 2026-10-02 against the projects' own files:
#: ripgrep is dual "MIT OR Unlicense" (NOT "MIT" alone -- the brief that
#: introduced this task said so, and it is an underspecification), and fd is
#: dual "MIT OR Apache-2.0".
KNOWN_DUAL_LICENCE_NOTES: Dict[str, str] = {
    "ripgrep": (
        "dual-licensed MIT OR Unlicense; the choice taken here is MIT, "
        "because MIT carries an attribution-and-notice obligation that this "
        "project's NOTICE file can discharge, and Unlicense would leave a "
        "downstream redistributor with nothing to reproduce"
    ),
    "fd": (
        "dual-licensed MIT OR Apache-2.0; the choice taken here is MIT, for "
        "the same NOTICE reason. Apache-2.0's patent grant was not needed, "
        "and taking MIT keeps one licence determination across all vendored "
        "artefacts"
    ),
}


#: The key a runtime-fetched entry sets to ``true`` when the expected digest
#: IS committed to this repository. Its absence plus a
#: :data:`UNPINNED_REASON_FIELD` is the honest "not yet" shape.
DIGEST_PINNED_FIELD = "digest-pinned"


@dataclass
class FetchedBinary:
    """A binary fetched at download time rather than committed to the tree.

    Reported as a FAILURE of the gate only when the entry is malformed or
    missing; the *unpinned digest* itself is reported as a published advisory,
    because the artefact is not in the tree and there is nothing for a build
    to be wrong about. The gap is named rather than silent.
    """

    fields: Dict[str, Any]

    def missing(self) -> List[str]:
        return [
            name
            for name in RUNTIME_FETCH_REQUIRED_FIELDS
            if not str(self.fields.get(name) or "").strip()
        ]

    def invalid(self) -> List[str]:
        problems: List[str] = []
        upstream = str(self.fields.get("upstream") or "")
        if upstream and not URL_RX.match(upstream):
            problems.append(f"upstream={upstream!r} is not a URL")
        licence = str(self.fields.get("licence") or "").strip()
        if licence and re.search(r"\bor\b", licence, re.IGNORECASE):
            problems.append(
                f"licence={licence!r} names two licences without choosing; "
                f"state the one relied on"
            )
        platforms = self.fields.get("platforms")
        if platforms is not None and not isinstance(platforms, list):
            problems.append("platforms must be a list")
        pinned = bool(self.fields.get(DIGEST_PINNED_FIELD))
        if not pinned and not str(self.fields.get(UNPINNED_REASON_FIELD) or "").strip():
            problems.append(
                f"neither {DIGEST_PINNED_FIELD!r}=true nor "
                f"{UNPINNED_REASON_FIELD!r} is present: say whether the digest "
                f"is committed here, and if not, why not"
            )
        return problems

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fields": dict(self.fields),
            "missing": self.missing(),
            "invalid": self.invalid(),
        }


def load_fetched_manifest(
    root: Path,
) -> Tuple[List[FetchedBinary], List[Dict[str, str]]]:
    """Load the ``fetched_binaries`` section of the manifest.

    Kept in the SAME document as the committed binaries on purpose: one file
    to read, one file to review, and a reader cannot conclude "nothing is
    fetched at runtime" by looking at a file that simply has no such section.
    A missing section is reported, not treated as "there are none".
    """
    path = Path(root) / BINARY_MANIFEST_NAME
    if not path.is_file():
        # An ABSENT manifest means "this tree declares no binaries", which is
        # the honest seed the source shape shipped with -- not a defect. A
        # manifest that EXISTS but omits the `fetched_binaries` key is a
        # different thing and IS reported, because then a reader cannot tell
        # "fetches nothing" from "did not write the section down".
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return [], []  # already reported by load_binary_manifest
    if not isinstance(data, dict):
        return [], []  # already reported by load_binary_manifest
    entries = data.get("fetched_binaries")
    if entries is None:
        return [], [
            {
                "path": BINARY_MANIFEST_NAME,
                "detail": "no `fetched_binaries` key, so a binary downloaded at "
                "run time would be invisible to this gate",
            }
        ]
    if not isinstance(entries, list):
        return [], [
            {"path": BINARY_MANIFEST_NAME, "detail": "`fetched_binaries` is not a list"}
        ]
    return (
        [FetchedBinary(fields=e if isinstance(e, dict) else {}) for e in entries],
        [],
    )


def scan_binaries(root: Optional[Path] = None) -> Dict[str, Any]:
    """Check every binary-shaped file against the manifest.

    Three outcomes, kept separate because they are different problems:

    ``declared``
        Present in the manifest, every required field present and valid, and
        the recorded SHA-256 matches the bytes on disk.
    ``undeclared``
        A binary-shaped file with NO manifest entry. Fails the gate. This is
        the honest mistake: somebody downloaded a binary into the tree and did
        not record where it came from.
    ``mismatched``
        Declared, but the digest on disk is not the digest recorded. This is
        the SUPPLY-CHAIN case and it is called out separately from
        incomplete, because a digest that does not match is not a paperwork
        problem.

    A tree with no vendored binaries and no manifest is ``PROVENANCE_COMPLETE``
    with ``declared: 0`` - the same honest seed the source shape shipped with.
    """
    root = Path(root) if root is not None else REPO_ROOT
    declarations, manifest_problems = load_binary_manifest(root)
    by_path = {d.path.replace("\\", "/"): d for d in declarations}

    undeclared: List[Dict[str, Any]] = []
    for path in iter_binary_candidates(root):
        rel = path.relative_to(root).as_posix()
        if rel in by_path:
            continue
        undeclared.append(
            {
                "path": rel,
                "why": "binary-shape",
                "detail": "an executable artifact is present in the tree with "
                f"no entry in {BINARY_MANIFEST_NAME}. A binary has no header, "
                "so the manifest is the only place its provenance can live.",
            }
        )

    incomplete = [d for d in declarations if not d.complete]
    mismatched = [d for d in declarations if d.digest_matches is False]
    fetched, fetched_problems = load_fetched_manifest(root)
    fetched_bad = [f for f in fetched if f.missing() or f.invalid()]
    # `fetched_problems` is PUBLISHED but does not make the lane red. A
    # manifest with no `fetched_binaries` key and a manifest whose
    # `fetched_binaries` is empty are indistinguishable to this gate, so
    # failing on the first one would be punishing the second one on a guess.
    # What DOES fail the lane is a fetched entry that is present and malformed:
    # that is a claim somebody made and did not fill in.
    ok = not undeclared and not incomplete and not manifest_problems and not fetched_bad
    return {
        "schema_version": BINARY_SCHEMA_VERSION,
        "manifest": BINARY_MANIFEST_NAME,
        "manifest_present": (Path(root) / BINARY_MANIFEST_NAME).is_file(),
        "manifest_problems": manifest_problems,
        "ok": ok,
        "verdict": "BINARY_PROVENANCE_COMPLETE" if ok else "BINARY_PROVENANCE_GAPS",
        "declared": [d.to_dict() for d in declarations if d.complete],
        "incomplete": [
            d.to_dict() for d in incomplete if d.digest_matches is not False
        ],
        "mismatched": [d.to_dict() for d in mismatched],
        "undeclared": sorted(undeclared, key=lambda d: d["path"]),
        "fetched_binaries": [
            f.to_dict() for f in fetched if not (f.missing() or f.invalid())
        ],
        "fetched_incomplete": [f.to_dict() for f in fetched_bad],
        "fetched_problems": fetched_problems,
        "known_dual_licence_notes": dict(KNOWN_DUAL_LICENCE_NOTES),
        "required_fields": list(BINARY_REQUIRED_FIELDS),
        "optional_fields": list(BINARY_OPTIONAL_FIELDS),
        # Published so a reader can see WHERE the lane looked. A lane that
        # searches a pruned set and says nothing about it reads as a lane that
        # searched everything.
        "searched_suffixes": list(BINARY_SUFFIXES),
        "excluded_dirs": list(EXCLUDED_DIRS),
        "example": {
            "schema_version": BINARY_SCHEMA_VERSION,
            "binaries": [
                {
                    "path": "third_party/bin/rg",
                    "upstream": "https://github.com/BurntSushi/ripgrep",
                    "version": "14.1.1",
                    "asset-name": "ripgrep-14.1.1-x86_64-unknown-linux-musl.tar.gz",
                    "sha256": "0" * 64,
                    "platform": "linux-x86_64",
                    "licence": "MIT",
                    "beats-ours-because": "SIMD literal + memory-map search; a "
                    "published benchmark we could not reproduce in pure Python",
                    "modified": "no",
                }
            ],
        },
    }


# --------------------------------------------------------------------------
# the scan
# --------------------------------------------------------------------------


def iter_candidate_files(
    root: Path, *, excluded: Iterable[str] = EXCLUDED_DIRS
) -> Iterable[Path]:
    """Yield every scannable source file, pruning generated trees IN the walk.

    **Vendor-shaped directories are INCLUDED on purpose.** They are one of the
    three vendored-code shapes, so excluding them here would make that
    detector unreachable -- a check that cannot fire is not a check. An
    earlier version of this function skipped them and a planted
    ``third_party/lib.py`` was reported as clean.

    The walk PRUNES rather than filtering after descending. This repository's
    own recorded lesson (``phases/DOCTRINE.md`` §8, the 345x case) is that
    ``rglob``/``os.walk`` plus a later ``parts & SKIP`` test costs a full
    traversal of ``.venv``, ``logs``, ``site/node_modules`` and ``Temp``: the
    measured scan time was **127 s**, of which essentially all was walking
    directories this function was about to discard. Pruning in place is the
    difference between a gate that runs in CI and one nobody runs.
    """
    skip = set(excluded)
    for dirpath, dirnames, filenames in os.walk(str(root)):
        # Prune in place: this is the whole optimisation.
        dirnames[:] = [d for d in dirnames if d not in skip]
        here = Path(dirpath)
        for name in filenames:
            if not name.endswith(SCANNED_SUFFIXES):
                continue
            yield here / name


def scan(root: Optional[Path] = None) -> Dict[str, Any]:
    """Scan the tree and return the provenance report.

    The report separates three things that a single verdict would blur:
    ``declared`` (has a complete block), ``undeclared`` (looks vendored and
    has no block -- these FAIL the gate), and ``advisory`` (looks vendored,
    has a block, but the block is incomplete or wrong).
    """
    root = Path(root) if root is not None else REPO_ROOT
    declared: List[Declaration] = []
    undeclared: List[Dict[str, Any]] = []
    advisory: List[Declaration] = []
    scanned = 0
    unreadable: List[Dict[str, str]] = []

    for path in iter_candidate_files(root):
        scanned += 1
        rel = path.relative_to(root).as_posix()
        try:
            raw = path.read_bytes()
        except OSError as exc:
            unreadable.append({"path": rel, "detail": f"{type(exc).__name__}: {exc}"})
            continue
        if b"\x00" in raw[:65536]:
            # A NUL in the header region means this is not a text source file
            # whatever its suffix claims. Recorded, not guessed about.
            unreadable.append(
                {"path": rel, "detail": "contains a NUL byte; not a text file"}
            )
            continue
        text = raw.decode("utf-8", errors="replace")

        in_vendor_dir = bool(set(path.relative_to(root).parts) & set(VENDOR_DIR_NAMES))
        decl = parse_declaration(text, rel, "declared-block")
        signature = licence_signature(text)
        holder = third_party_copyright(text)

        if decl is not None:
            decl.licence_signature = signature
            (declared if decl.complete else advisory).append(decl)
            continue

        # No declaration. Is this file vendored by shape?
        if in_vendor_dir:
            undeclared.append(
                {
                    "path": rel,
                    "why": "vendor-directory",
                    "detail": "lives under a vendor-shaped directory with no "
                    "`vendored:` block",
                }
            )
        elif signature:
            undeclared.append(
                {
                    "path": rel,
                    "why": f"licence-header:{signature}",
                    "detail": "carries a third-party licence header but no "
                    "`vendored:` block",
                }
            )
        elif holder:
            undeclared.append(
                {
                    "path": rel,
                    "why": f"third-party-copyright:{holder}",
                    "detail": "carries a copyright line naming somebody who is "
                    "not this project's author",
                }
            )

    ok = not undeclared and not advisory
    return {
        "schema_version": 1,
        "root": str(root),
        "scanned_files": scanned,
        "ok": ok,
        "verdict": "PROVENANCE_COMPLETE" if ok else "PROVENANCE_GAPS",
        "unreadable": unreadable,
        "header_format": {
            "scan_lines": HEADER_SCAN_LINES,
            "required_fields": list(REQUIRED_FIELDS),
            "optional_fields": list(OPTIONAL_FIELDS),
            "example": [
                "# vendored: yes",
                "# upstream: https://github.com/owner/repo",
                "# upstream-commit: 0f1e2d3c4b5a69788796a5b4c3d2e1f009876543",
                "# upstream-path: src/thing.py",
                "# licence: MIT",
                "# beats-ours-because: <the specific property that made copying "
                "better than writing>",
                "# modified: yes",
            ],
        },
        "declared": [d.to_dict() for d in sorted(declared, key=lambda d: d.path)],
        "undeclared": sorted(undeclared, key=lambda d: d["path"]),
        "advisory": [d.to_dict() for d in sorted(advisory, key=lambda d: d.path)],
        "binaries": scan_binaries(root),
        "not_established": [
            "This gate detects a vendored file by SHAPE: an explicit "
            "`vendored:` block, a vendor-shaped directory, a third-party licence "
            "header, or a third-party copyright line. It cannot detect code "
            "borrowed in SPIRIT -- a reimplementation whose approach came from "
            "elsewhere -- because this repository uses the word 'derived' in "
            "the sense of *computed from* in 135 files, and a keyword scanner "
            "cannot separate the two senses. Prose attribution remains a "
            "reviewer's job.",
            "A clean report means nothing was found by those four shapes. It "
            "is not a claim that the tree contains no borrowed ideas.",
            "The BINARY lane checks the SHA-256 recorded in "
            f"{BINARY_MANIFEST_NAME} against the bytes on disk. That proves "
            "the file is the one that was declared. It does NOT prove the "
            "declared archive was the authentic upstream release: verifying "
            "that means checking the project's published checksum file, which "
            "this gate does not fetch (it is offline by design).",
            "A binary is matched by SUFFIX and manifest path. A vendored "
            "executable committed under a source suffix, or fetched at "
            "runtime rather than committed, is not visible to this lane.",
        ],
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def render(report: Dict[str, Any]) -> str:
    lines = [
        f"provenance: {report['verdict']}",
        f"  scanned {report['scanned_files']} source files under {report['root']}",
        f"  declared:    {len(report['declared'])}",
        f"  undeclared:  {len(report['undeclared'])}",
        f"  advisory:    {len(report['advisory'])}",
        "",
        "DECLARED VENDORED FILES (each one is auditable):",
    ]
    if report["declared"]:
        for item in report["declared"]:
            f = item["fields"]
            lines.append(f"  {item['path']}  (line {item['line']}, {item['how']})")
            lines.append(f"      upstream   {f.get('upstream', '?')}")
            lines.append(f"      commit     {f.get('upstream-commit', '?')}")
            lines.append(f"      licence    {f.get('licence', '?')}")
            lines.append(f"      beats ours {f.get('beats-ours-because', '?')}")
            lines.append(f"      modified   {f.get('modified', '?')}")
    else:
        lines.append(
            "  none. This tree vendors no third-party code today, which is the "
            "measured state and the correct seed."
        )

    lines += ["", "UNDECLARED FILES THAT LOOK VENDORED (these FAIL the gate):"]
    if report["undeclared"]:
        for item in report["undeclared"]:
            lines.append(f"  {item['path']}")
            lines.append(f"      why    {item['why']}")
            lines.append(f"      detail {item['detail']}")
            lines.append(
                "      fix    add a `vendored:` block; see the header format below"
            )
    else:
        lines.append("  none")

    lines += ["", "INCOMPLETE DECLARATIONS (advisory unless --strict):"]
    if report["advisory"]:
        for item in report["advisory"]:
            lines.append(f"  {item['path']} (line {item['line']})")
            for name in item["missing"]:
                lines.append(f"      MISSING  {name}")
            for problem in item["invalid"]:
                lines.append(f"      INVALID  {problem}")
    else:
        lines.append("  none")

    lines += ["", "THE HEADER FORMAT (copy verbatim):"]
    for line in report["header_format"]["example"]:
        lines.append(f"  {line}")

    binaries = report.get("binaries") or {}
    lines += [
        "",
        f"BINARY PROVENANCE ({binaries.get('verdict', 'not run')}):",
        f"  manifest: {binaries.get('manifest', BINARY_MANIFEST_NAME)}"
        + ("" if binaries.get("manifest_present") else "  (ABSENT)"),
        f"  declared:    {len(binaries.get('declared') or [])}",
        f"  undeclared:  {len(binaries.get('undeclared') or [])}",
        f"  incomplete:  {len(binaries.get('incomplete') or [])}",
        f"  mismatched:  {len(binaries.get('mismatched') or [])}",
        "",
        "  A BINARY DECLARES ITSELF IN THE MANIFEST, not in a header: it has no",
        "  header. A binary is pinned by a VERSION and a SHA-256 OF THE FILE, not",
        "  by a commit, and the digest is re-checked against the bytes on disk.",
    ]
    for item in binaries.get("mismatched") or []:
        lines.append(f"  MISMATCHED {item['path']}")
        lines.append(f"      recorded  {item['fields'].get('sha256')}")
        lines.append(f"      on disk   {item['measured_sha256']}")
        lines.append(
            "      this is a SUPPLY-CHAIN finding, not a paperwork one: the file "
            "in the tree is not the file that was declared"
        )
    for item in binaries.get("incomplete") or []:
        lines.append(f"  INCOMPLETE {item['path']}")
        for name in item.get("missing") or []:
            lines.append(f"      MISSING  {name}")
        for problem in item.get("invalid") or []:
            lines.append(f"      INVALID  {problem}")
        if not item.get("exists_in_tree"):
            lines.append("      ABSENT   the declared binary is not in the tree")
    for item in binaries.get("undeclared") or []:
        lines.append(f"  UNDECLARED {item['path']}")
        lines.append(f"      why    {item['why']}")
        lines.append(f"      detail {item['detail']}")
    for problem in binaries.get("manifest_problems") or []:
        lines.append(f"  MANIFEST PROBLEM {problem['path']}: {problem['detail']}")

    fetched = binaries.get("fetched_binaries") or []
    fetched_bad = binaries.get("fetched_incomplete") or []
    lines += [
        "",
        f"RUNTIME-FETCHED BINARIES ({len(fetched)} declared, {len(fetched_bad)} "
        f"incomplete):",
        "  These are NOT in the tree, so there are no bytes to re-hash and no",
        "  sha256 is required - requiring one would mean recording a digest",
        "  nobody can verify. What IS required is `digest-source`, and a",
        "  `digest-unpinned-reason` when that source is not a digest committed",
        "  to this repository.",
    ]
    for item in fetched + fetched_bad:
        f = item["fields"]
        lines.append(f"  {f.get('name', '?')} {f.get('version', '?')}")
        lines.append(f"      upstream     {f.get('upstream', '?')}")
        lines.append(f"      licence      {f.get('licence', '?')}")
        lines.append(f"      platforms    {f.get('platforms', '?')}")
        lines.append(f"      beats ours   {f.get('beats-ours-because', '?')}")
        lines.append(f"      digest from  {f.get('digest-source', '?')}")
        if f.get("licence-note"):
            lines.append(f"      licence note {f['licence-note']}")
        if not f.get("digest-pinned") and f.get("digest-unpinned-reason"):
            lines.append(f"      OPEN GAP     {f['digest-unpinned-reason']}")
        for name in item.get("missing") or []:
            lines.append(f"      MISSING      {name}")
        for problem in item.get("invalid") or []:
            lines.append(f"      INVALID      {problem}")
    for problem in binaries.get("fetched_problems") or []:
        lines.append(
            f"  FETCHED-MANIFEST PROBLEM {problem['path']}: {problem['detail']}"
        )
    if not fetched and not fetched_bad:
        lines.append(
            "  none declared. If this project downloads a binary at run time, it"
        )
        lines.append(
            "  must appear here; a binary the gate cannot see is a binary whose"
        )
        lines.append("  provenance nobody has written down.")

    if not (
        (binaries.get("declared") or [])
        or (binaries.get("undeclared") or [])
        or (binaries.get("incomplete") or [])
        or (binaries.get("mismatched") or [])
        or (binaries.get("fetched_binaries") or [])
        or (binaries.get("fetched_incomplete") or [])
    ):
        lines.append(
            "  none. This tree vendors no downloaded binary today, which is the "
            "measured state."
        )

    lines += ["", "THE BINARY MANIFEST FORMAT (copy verbatim):"]
    lines.append(
        "  " + json.dumps(binaries.get("example") or {}, indent=2).replace("\n", "\n  ")
    )

    lines += ["", "WHAT THIS DOES NOT ESTABLISH:"]
    for item in report["not_established"]:
        lines.append(f"  * {item}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the provenance report. Exit 2 when an undeclared file is found."""
    parser = argparse.ArgumentParser(
        prog="python scripts/provenance_report.py",
        description="Report vendored code and fail on anything undeclared.",
    )
    parser.add_argument("--root", default=str(REPO_ROOT))
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="also fail on an incomplete/incorrect declaration",
    )
    args = parser.parse_args(argv)

    report = scan(Path(args.root))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render(report))

    binaries = report.get("binaries") or {}
    if report["undeclared"]:
        return 2
    if binaries.get("undeclared") or binaries.get("mismatched"):
        # An undeclared binary and a digest that does not match are both
        # BUILD FAILURES, not advisories. A digest mismatch is the
        # supply-chain case: the file in the tree is not the declared file.
        return 2
    if binaries.get("manifest_problems"):
        return 2
    if binaries.get("fetched_incomplete"):
        # A fetched entry that is present and malformed is a claim somebody
        # made and did not fill in. That fails; an ABSENT `fetched_binaries`
        # key does not, and is published instead.
        return 2
    if args.strict and (report["advisory"] or binaries.get("incomplete")):
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
