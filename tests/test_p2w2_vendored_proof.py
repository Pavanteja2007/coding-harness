"""P2/W2 T5.W2.3 - the vendored-code audit, pinned so it cannot rot quietly.

T1 vendored two third-party ALGORITHMS into `harness/third_party/` with a
four-field provenance header in the module DOCSTRING. This file audits that
arrangement against what `scripts/provenance_report.py` actually enforces, and
records the gap between the two.

The headline finding, measured rather than asserted:

    the scanner parses a `# vendored:` COMMENT block, and the vendored files
    carry their provenance in a module DOCSTRING under four different field
    names. So the scanner cannot see any of it, and the gate is RED for a
    reason that has nothing to do with licensing.

That distinction matters. A red gate is not automatically a licence violation,
and this file is careful not to let the two be confused - the risk is a
repository "fixing" a licence red by renaming a directory.

What is pinned here:

* every vendored file's real sha256, so an in-place edit is visible;
* the four header fields, and that the reason field is non-empty AND specific;
* that `NOTICE.md` covers every vendored file and both licences;
* that the licences are compatible with this repository's MIT;
* that the two fetched binaries are declared with a version, a source URL and
  an HONEST statement of what is and is not pinned.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = REPO_ROOT / "harness" / "third_party"
NOTICE = VENDOR_DIR / "NOTICE.md"
BINARY_MANIFEST = REPO_ROOT / "provenance" / "binaries.json"

#: The files that carry vendored material. `__init__.py` is included because
#: it is IN the directory and the scanner flags the whole directory.
VENDORED_FILES = ("opencode_replacer.py", "aider_bitap.py", "__init__.py")

#: Measured on this tree at 2026-10-03. A vendored file edited in place makes
#: the provenance claim FALSE, and a checksum nobody compares is a comment with
#: a filename - so the comparison is this test, not the header.
EXPECTED_SHA256: Mapping[str, str] = {
    "opencode_replacer.py": "d3087952ba477ef813fdef06c3aa83b4c7d2ebe426b1f5eddb87e721ce937e64",
    "aider_bitap.py": "452658babd6b7c84961f84ea0828173f548186bb36e27b3b4379a067b80ea600",
    "__init__.py": "f5e83a81217120876f5d7268b893ac93122d5ead5fa89d500df06ea772eb5fe7",
}

#: The directory's own NOTICE is pinned too. It is the Apache-2.0 attribution
#: obligation, so an edit to it is a licence-visible change.
EXPECTED_NOTICE_SHA256 = (
    "ee15aa061403eaeaac4ab9251cce2959e048d5ce44758eca2e5cdc0d4c161c5c"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _docstring(path: Path) -> str:
    """Return the module docstring, or '' when there is none.

    Read by PARSING rather than by line slicing: this repository's own notes
    record a line-keyed claim being silently invalidated by an insertion, and
    a vendored header is exactly the kind of claim that must not move.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    return ast.get_docstring(tree) or ""


# ---------------------------------------------------------------------------
# 1. Checksums
# ---------------------------------------------------------------------------


class TestTheVendoredFilesAreUnmodified:
    def test_every_vendored_file_exists(self) -> None:
        missing = [n for n in VENDORED_FILES if not (VENDOR_DIR / n).is_file()]
        assert not missing, f"vendored files missing from {VENDOR_DIR}: {missing}"

    def test_measured_digests_are_pinned_and_current(self) -> None:
        """A vendored file edited in place makes the provenance claim false.

        The pins are EXACT and CURRENT: a mismatch is either a legitimate local
        modification that must be moved into a wrapper, or a violation. Either
        way it is a conversation, which is the point of comparing.
        """
        drifted = []
        for name in VENDORED_FILES:
            expected = EXPECTED_SHA256.get(name, "")
            actual = _sha256(VENDOR_DIR / name)
            if not expected:
                drifted.append(f"{name}: NO PIN (actual {actual})")
            elif expected != actual:
                drifted.append(
                    f"{name}: pinned {expected[:16]}... actual {actual[:16]}..."
                )
        assert not drifted, (
            "vendored-file checksum drift:\n  "
            + "\n  ".join(drifted)
            + "\n\nA vendored file edited in place makes the provenance claim "
            "false. Move the change into a wrapper module, or re-declare the "
            "file (and re-check the licence) in the same change."
        )

    def test_the_notice_is_pinned(self) -> None:
        """The NOTICE is the Apache-2.0 attribution, so its bytes matter."""
        assert _sha256(NOTICE) == EXPECTED_NOTICE_SHA256, (
            "harness/third_party/NOTICE.md changed. That is a licence-visible "
            "change: it carries the Apache-2.0 attribution. Re-read it and "
            "re-pin deliberately."
        )

    def test_the_pins_are_not_placeholders(self) -> None:
        """A pin of all zeros is a pin that can never fail."""
        for name, digest in EXPECTED_SHA256.items():
            assert len(digest) == 64, f"{name} pin is not a sha256: {digest!r}"
            assert set(digest) <= set("0123456789abcdef"), f"{name} pin is not hex"

    def test_the_vendor_directory_is_not_a_silent_gitignore(self) -> None:
        """`provenance/` is gitignored, so the BINARY manifest is uncommitted.

        That is a provenance claim nobody can review in a diff, and it is
        asserted rather than left for a reader to discover.
        """
        gitignore = _read(REPO_ROOT / ".gitignore")
        assert re.search(r"^provenance/?\s*$", gitignore, re.MULTILINE), (
            "provenance/ is no longer gitignored. If that was deliberate, remove "
            "this test and say so in the change; otherwise the binary manifest "
            "is a claim no reviewer can see."
        )


# ---------------------------------------------------------------------------
# 2. The four header fields
# ---------------------------------------------------------------------------

#: The field names the vendored files actually use, in a module docstring.
DOCSTRING_FIELDS = (
    "Upstream:",
    "Commit pin:",
    "SPDX-License-Identifier:",
    "Why it beats ours:",
)

#: The names `scripts/provenance_report.py` parses. Compared against the above
#: so the two vocabularies cannot drift apart unnoticed.
SCANNER_FIELDS = (
    "vendored",
    "upstream",
    "upstream-commit",
    "upstream-path",
    "licence",
    "beats-ours-because",
    "modified",
)


class TestTheFourFieldHeaders:
    def test_both_algorithm_modules_carry_all_four_fields(self) -> None:
        for name in ("opencode_replacer.py", "aider_bitap.py"):
            doc = _docstring(VENDOR_DIR / name)
            for field in DOCSTRING_FIELDS:
                assert field in doc, f"{name} is missing the {field!r} field"

    def test_the_reason_field_is_non_empty_AND_specific(self) -> None:
        """ "Vendored" is not a reason.

        The gate's own bar is 15 characters, which is a floor and not a
        standard. These two reasons are required to clear a much higher one,
        because they are the only thing that makes the decision auditable
        later - and because a future reader deciding whether to keep the
        dependency needs to know WHICH property was worth the coupling.
        """
        for name in ("opencode_replacer.py", "aider_bitap.py"):
            doc = _docstring(VENDOR_DIR / name)
            _, _, rest = doc.partition("Why it beats ours:")
            reason = rest.strip()
            assert len(reason) >= 200, (
                f"{name}'s reason is {len(reason)} chars. That is too short to "
                f"be a reason: it will not say WHICH property was worth taking."
            )
            lowered = reason.lower()
            assert "we" in lowered, f"{name}'s reason never compares against ours"

    def test_what_was_NOT_vendored_is_documented_in_the_docstring(self) -> None:
        """OpenCode's cascade is large and T1 ported only the matching stages.

        A future reader must know the boundary, or they will assume the file
        IS OpenCode's replacer and reason about a different thing.
        """
        expectations = {
            "opencode_replacer.py": ("did NOT take", "LADDER AFTER"),
            "aider_bitap.py": ("WHAT THIS IS NOT", "not a copy of diff_match_patch"),
        }
        for name, needles in expectations.items():
            doc = _docstring(VENDOR_DIR / name)
            for needle in needles:
                assert needle in doc, (
                    f"{name} does not document its boundary: {needle!r} absent. "
                    f"The docstring must say what was NOT taken."
                )

    def test_the_two_vocabularies_are_reconciled(self) -> None:
        """The header-format gap is CLOSED (2026-10-03, P2/W2-T5, re-measured).

        An earlier version of this test asserted the MISMATCH was real - that
        no vendored file carried a `# vendored:` block - so the gate could not
        see the provenance. T1 reconciled the two vocabularies during this
        phase, `python scripts/provenance_report.py` now exits **0** with
        `declared: 3, undeclared: 0`, and this assertion is retired rather than
        left to fail.

        What replaced it is the *scanner's* required key set, so a future
        rename of a field is caught here rather than as a silently-incomplete
        declaration.
        """
        scanner = _read(REPO_ROOT / "scripts" / "provenance_report.py")
        for field in SCANNER_FIELDS:
            assert field in scanner, (
                f"provenance_report.py no longer mentions {field!r}; this "
                f"file's record of the header contract is stale."
            )
        for name in ("opencode_replacer.py", "aider_bitap.py"):
            text = _read(VENDOR_DIR / name)
            assert "# vendored: yes" in text, (
                f"{name} no longer carries a `# vendored: yes` block. The "
                f"scanner reads that block and nothing else, so without it the "
                f"gate reports this file as undeclared."
            )

    def test_every_declared_vendored_file_MUST_PARSE(self) -> None:
        """NEW FINDING 2026-10-03 (P2/W2-T5) — the gate does not parse.

        `scripts/provenance_report.py` reads the header as TEXT. It never
        compiles the file. So a file can carry a complete, valid, seven-field
        provenance declaration and still be a Python syntax error, and the
        provenance gate will report the tree COMPLETE.

        That is not hypothetical. While this test was being written, both
        vendored modules were mid-edit with the new header block inserted
        ABOVE the module docstring - which removes the docstring's opening
        the triple-quote opener and leaves the file unterminated. Measured at
        the time:

            harness/third_party/aider_bitap.py        SyntaxError line 10
            harness/third_party/opencode_replacer.py  SyntaxError line 17

        while `python scripts/provenance_report.py` exited **0** with
        `declared: 3, undeclared: 0`. **The gate went green on two files that
        could not be imported.**

        The consequence is larger than a broken import: the gate's own claim is
        that a vendored file is auditable, and an unparseable one is not
        auditable by anything. So the parse check lives here, next to the
        checksum pins, because this file is where the vendored tree's
        integrity is actually enforced.

        (Writing this docstring is where I made the same mistake once: a
        literal triple quote inside a triple-quoted docstring TERMINATES it,
        and the file stops parsing. The symptom is identical to the bug above,
        which is precisely why the check here is `ast.parse` and never a
        visual inspection.)
        """
        import ast

        for name in VENDORED_FILES:
            path = VENDOR_DIR / name
            source = path.read_text(encoding="utf-8")
            try:
                ast.parse(source)
            except SyntaxError as exc:
                raise AssertionError(
                    f"{name} does not PARSE: {exc.msg} at line {exc.lineno}.\n\n"
                    f"The provenance gate reports a file as declared on its "
                    f"TEXT alone and never compiles it, so a syntactically "
                    f"broken vendored file passes `provenance_report.py` with "
                    f"exit 0. This is a live example: a `# vendored:` block "
                    f"inserted above the module docstring removes the "
                    f'docstring\'s opening `"""` and leaves the file '
                    f"unterminated."
                ) from exc

    def test_the_module_docstring_is_still_THE_DOCSTRING(self) -> None:
        """The specific way the parse broke, pinned so it cannot recur.

        Inserting a `#` comment block ABOVE a module docstring is harmless. What
        broke it was the docstring's opening delimiter being consumed, which
        turns the docstring into bare expression statements - so the file still
        *looks* right to a text scanner and is not a docstring to `ast`.
        """
        for name in ("opencode_replacer.py", "aider_bitap.py"):
            doc = _docstring(VENDOR_DIR / name)
            assert doc.strip(), (
                f"{name} has no module docstring any more. Either the "
                f'`"""` opener was consumed by an edit, or the docstring was '
                f"replaced by bare expression statements. Both make the "
                f"provenance prose unreadable to `ast.get_docstring`, which is "
                f"what every other test in this file reads."
            )
            assert "Upstream:" in doc, f"{name}'s docstring lost its Upstream line"

    def test_no_vendored_file_claims_to_be_a_byte_copy(self) -> None:
        """The honest position, asserted so it cannot be quietly dropped.

        Each file's own text states it was WRITTEN AGAINST the published
        algorithm description rather than copied. That is a licence-clean
        position and a provenance-honest one, and it is also why the gate is
        red for a reason that is not a licence problem.
        """
        for name in ("opencode_replacer.py", "aider_bitap.py"):
            doc = _docstring(VENDOR_DIR / name)
            assert "not copied" in doc.lower() or "not a copy" in doc.lower(), (
                f"{name} no longer states that it was written against the "
                f"published description rather than copied. That sentence is "
                f"the provenance claim; do not drop it."
            )


# ---------------------------------------------------------------------------
# 3. NOTICE
# ---------------------------------------------------------------------------


class TestNoticeIsCurrent:
    def test_notice_exists(self) -> None:
        assert NOTICE.is_file(), f"{NOTICE} is missing"

    def test_notice_names_every_algorithm_module(self) -> None:
        text = _read(NOTICE)
        for name in ("opencode_replacer.py", "aider_bitap.py"):
            assert name in text, f"NOTICE.md does not mention {name}"

    def test_notice_carries_both_licences(self) -> None:
        text = _read(NOTICE)
        assert "MIT" in text, "NOTICE.md carries no MIT attribution"
        assert "Apache-2.0" in text, (
            "NOTICE.md carries no Apache-2.0 attribution. Apache-2.0 4 requires "
            "the NOTICE to carry it; that is the whole reason this artefact was "
            "licensed Apache-2.0 rather than MIT."
        )

    def test_notice_states_the_deviation_rather_than_implying_a_copy(self) -> None:
        text = _read(NOTICE)
        lowered = text.lower()
        assert "no network fetch" in lowered or "not a byte copy" in lowered, (
            "NOTICE.md no longer states that no upstream bytes were fetched. An "
            "independent implementation is licence-clean; an unstated one is "
            "just an unverified claim."
        )

    def test_notice_does_not_claim_a_header_the_files_do_not_carry(self) -> None:
        """`NOTICE.md` says the header is on EVERY file. It is not on
        `__init__.py`, which carries none of the four fields.

        Asserted so the overclaim is visible rather than believed.
        """
        text = _read(NOTICE)
        assert "every file" in text.lower(), (
            "NOTICE.md no longer claims the header is on every file; if that "
            "sentence was corrected, this test is now stale and should go."
        )
        init_doc = _docstring(VENDOR_DIR / "__init__.py")
        assert "SPDX-License-Identifier:" not in init_doc, (
            "__init__.py now carries an SPDX field. If so it is no longer the "
            "file NOTICE.md's 'every file' overclaim is about."
        )

    def test_the_root_notice_does_not_contradict_the_vendor_directory(self) -> None:
        """RECORDED CONTRADICTION, asserted so it cannot be read as agreeing.

        The tracked root `NOTICE` still says "NOTHING FROM AIDER IS COPIED
        INTO THIS TREE" and reports `declared: 0, undeclared: 0`. The measured
        state is `declared: 0, undeclared: 3`. One of the two is a lie and the
        tracked file is the one a reader trusts.
        """
        root_notice = REPO_ROOT / "NOTICE"
        assert root_notice.is_file(), "the root NOTICE is missing"
        text = _read(root_notice)
        assert "NOTHING FROM AIDER IS COPIED" in text, (
            "the root NOTICE no longer contains the stale Aider sentence. If "
            "it was corrected, this test is stale - re-measure and remove it."
        )
        assert "undeclared: 0" in text, (
            "the root NOTICE no longer reports `undeclared: 0`. If it was "
            "corrected, this test is stale."
        )


# ---------------------------------------------------------------------------
# 4. Licence compatibility
# ---------------------------------------------------------------------------


class TestLicenceCompatibility:
    def test_this_repository_is_mit(self) -> None:
        pyproject = _read(REPO_ROOT / "pyproject.toml")
        assert re.search(r'license\s*=\s*"MIT"', pyproject) or (
            "License-Expression" in pyproject and "MIT" in pyproject
        ), "this repository no longer declares MIT; the compatibility argument changes"

    def test_both_upstream_licences_are_permissive_and_compatible(self) -> None:
        """MIT and Apache-2.0 are both compatible with distributing under MIT.

        The one obligation that is NOT discharged by picking MIT is Apache-2.0
        section 4: the NOTICE must carry the attribution, which is why
        `harness/third_party/NOTICE.md` exists and why the Apache-derived file
        must never be re-declared `licence: MIT`.
        """
        assert "Apache-2.0" in _read(NOTICE)
        assert "MIT" in _read(NOTICE)
        # A file that copies Apache-2.0 code and declares MIT is a LICENCE
        # ERROR, not a preference.
        aider_doc = _docstring(VENDOR_DIR / "aider_bitap.py")
        assert "Apache-2.0" in aider_doc
        assert not re.search(r"SPDX-License-Identifier:\s*MIT", aider_doc), (
            "aider_bitap.py declares MIT. It is derived from Apache-2.0 work "
            "(Aider and Google diff-match-patch); declaring MIT would be a "
            "licence error."
        )
        opencode_doc = _docstring(VENDOR_DIR / "opencode_replacer.py")
        assert "SPDX-License-Identifier: MIT" in opencode_doc


# ---------------------------------------------------------------------------
# 5. The binaries
# ---------------------------------------------------------------------------


class TestTheFetchedBinariesAreDeclaredHonestly:
    def test_the_manifest_exists(self) -> None:
        assert BINARY_MANIFEST.is_file(), (
            f"{BINARY_MANIFEST} is missing. The binary lane has nothing to read "
            f"without it, and 'no manifest' must not read as 'nothing to check'."
        )

    def _manifest(self) -> dict:
        return json.loads(_read(BINARY_MANIFEST))

    def test_both_binaries_are_declared_with_version_and_source(self) -> None:
        doc = self._manifest()
        fetched = {row["name"]: row for row in doc.get("fetched_binaries", ())}
        for name, version in (("ripgrep", "14.1.1"), ("fd", "10.2.0")):
            assert name in fetched, f"{name} is not declared in the manifest"
            row = fetched[name]
            assert row["version"] == version, (
                f"{name} version drifted from the pinned {version} to "
                f"{row.get('version')!r}. The pin lives in "
                f"harness/search_engine.py; the two must agree."
            )
            assert str(row.get("upstream") or "").startswith("https://"), (
                f"{name} has no upstream URL"
            )
            assert str(row.get("digest-source") or "").strip(), (
                f"{name} does not say where its expected digest comes from"
            )

    def test_the_version_pins_agree_with_the_search_engine(self) -> None:
        """Two places pinning a version is how they drift."""
        engine = _read(REPO_ROOT / "harness" / "search_engine.py")
        for version in ("14.1.1", "10.2.0"):
            assert version in engine, (
                f"harness/search_engine.py no longer pins {version}. The binary "
                f"manifest and the downloader must agree, or one of them is "
                f"fetching a version nobody audited."
            )

    def test_the_unpinned_digest_gap_is_declared_not_hidden(self) -> None:
        """RECORDED GAP: no `committed_digests`, and that is stated.

        A checksum-verified download whose checksum is not committed is the
        PAPERWORK version of the honesty problem: the expected digest travels
        with the payload, so a replaced asset with a replaced sidecar is
        accepted. The manifest says so; this test makes sure it keeps saying so.
        """
        doc = self._manifest()
        committed = doc.get("committed_digests")
        assert not committed, (
            "provenance/binaries.json now carries `committed_digests`. That "
            "CLOSES the gap this test records - re-measure, then DELETE this "
            "assertion and publish the finding as closed."
        )
        for row in doc.get("fetched_binaries", ()):
            assert row.get("digest-pinned") is False, (
                f"{row['name']} claims a pinned digest; the manifest carries no "
                f"sha256 field for it."
            )
            reason = str(row.get("digest-unpinned-reason") or "")
            assert len(reason) >= 60, (
                f"{row['name']}'s digest-unpinned-reason is too short to be a "
                f"reason: {reason!r}"
            )

    def test_the_licence_facts_are_recorded_accurately(self) -> None:
        """Both binaries are DUAL licensed; the manifest must not understate.

        The brief that introduced this work recorded ripgrep as "MIT". It is
        `MIT OR Unlicense`. Recording only the half that is relied on is
        defensible; recording it as if it were the whole licence is not, and
        the difference matters to anyone re-licensing downstream.
        """
        doc = self._manifest()
        fetched = {row["name"]: row for row in doc.get("fetched_binaries", ())}
        ripgrep = str(fetched["ripgrep"].get("licence-note") or "")
        assert "MIT OR Unlicense" in ripgrep, (
            "ripgrep's dual licence is no longer recorded. ripgrep is "
            "'MIT OR Unlicense', NOT 'MIT' alone."
        )
        fd = str(fetched["fd"].get("licence-note") or "")
        assert "MIT OR Apache-2.0" in fd, (
            "fd's dual licence is no longer recorded. fd is 'MIT OR Apache-2.0'."
        )
        # Both are relied on under MIT, and the manifest must say WHY.
        for name in ("ripgrep", "fd"):
            assert fetched[name].get("licence") == "MIT"
            assert "NOTICE" in str(fetched[name].get("licence-note") or ""), (
                f"{name} must say MIT was chosen because it carries an "
                f"attribution-and-notice obligation the root NOTICE discharges."
            )

    def test_the_committed_binary_section_is_empty_and_says_why(self) -> None:
        doc = self._manifest()
        assert doc.get("binaries") == [], (
            "provenance/binaries.json now declares a COMMITTED binary. That is "
            "a real change: a committed binary is re-hashed from disk on every "
            "run, so this test's premise no longer holds."
        )
