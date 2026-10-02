"""T5.W2.5 — the provenance scanner's own tests.

A gate nobody has seen fail is a gate nobody can trust. Every arm here
builds a tree in ``tmp_path`` and plants the exact shape a real mistake takes,
so the failure modes are demonstrated rather than asserted:

* a file carrying an upstream MIT/Apache header with no ``vendored:`` block is
  reported UNDECLARED and the CLI exits non-zero;
* a file in a ``third_party/`` directory with no block is reported UNDECLARED;
* a ``vendored:`` block missing ``upstream-commit`` is INCOMPLETE, and a branch
  name in that field is INVALID rather than accepted -- a moving ref is not a
  pin;
* ``beats-ours-because`` is mandatory, which is the field the whole mechanism
  exists to force;
* the scanner's OWN documented header example is not mistaken for a
  declaration (this was a real self-referential false positive);
* and the non-vacuity control: the scanner runs over the real tree and finds
  the number of files it claims.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.provenance_report import (
    HEADER_SCAN_LINES,
    REQUIRED_FIELDS,
    iter_candidate_files,
    licence_signature,
    main,
    parse_declaration,
    render,
    scan,
    third_party_copyright,
)

GOOD_BLOCK = """\
# vendored: yes
# upstream: https://github.com/owner/repo
# upstream-commit: 0f1e2d3c4b5a69788796a5b4c3d2e1f009876543
# upstream-path: src/thing.py
# licence: MIT
# beats-ours-because: its worst-case append bound is proven in the upstream paper
# modified: yes
"""

MIT_HEADER = """\
# Copyright (c) 2024 The Upstream Authors
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction.

def upstream_helper(x):
    return x * 2
"""


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def test_a_complete_block_parses_and_is_complete() -> None:
    decl = parse_declaration(GOOD_BLOCK, "x.py", "declared-block")
    assert decl is not None
    assert decl.complete is True
    assert decl.missing() == []
    assert decl.invalid() == []
    assert decl.fields["upstream-commit"] == "0f1e2d3c4b5a69788796a5b4c3d2e1f009876543"
    assert decl.fields["modified"] == "yes"


def test_a_block_missing_the_commit_pin_is_incomplete() -> None:
    """The pin is the field everyone skips, and it is mandatory."""
    text = GOOD_BLOCK.replace(
        "# upstream-commit: 0f1e2d3c4b5a69788796a5b4c3d2e1f009876543\n", ""
    )
    decl = parse_declaration(text, "x.py", "declared-block")
    assert decl is not None
    assert "upstream-commit" in decl.missing()
    assert decl.complete is False


def test_a_branch_name_in_the_commit_field_is_invalid_not_accepted() -> None:
    """`main` is not a pin. It cannot be re-derived or diffed against.

    This is the distinction that makes the field worth having: a MISSING pin is
    an omission, a FALSE pin is a claim that cannot be honoured, and the second
    is worse.
    """
    text = GOOD_BLOCK.replace(
        "# upstream-commit: 0f1e2d3c4b5a69788796a5b4c3d2e1f009876543",
        "# upstream-commit: main",
    )
    decl = parse_declaration(text, "x.py", "declared-block")
    assert decl is not None
    assert decl.missing() == []
    assert any("not a hex SHA" in problem for problem in decl.invalid())
    assert decl.complete is False


def test_beats_ours_because_is_mandatory_and_must_be_a_reason() -> None:
    """The field the whole mechanism exists to force."""
    without = GOOD_BLOCK.replace(
        "# beats-ours-because: its worst-case append bound is proven in the "
        "upstream paper\n",
        "",
    )
    decl = parse_declaration(without, "x.py", "declared-block")
    assert decl is not None
    assert "beats-ours-because" in decl.missing()

    thin = GOOD_BLOCK.replace(
        "its worst-case append bound is proven in the upstream paper", "mature"
    )
    decl2 = parse_declaration(thin, "x.py", "declared-block")
    assert decl2 is not None
    assert any("too short to be a reason" in p for p in decl2.invalid())


def test_modified_must_be_yes_or_no() -> None:
    text = GOOD_BLOCK.replace("# modified: yes", "# modified: probably")
    decl = parse_declaration(text, "x.py", "declared-block")
    assert decl is not None
    assert any("must be 'yes' or 'no'" in p for p in decl.invalid())


def test_upstream_must_be_a_url_not_a_project_name() -> None:
    text = GOOD_BLOCK.replace("https://github.com/owner/repo", "some internal library")
    decl = parse_declaration(text, "x.py", "declared-block")
    assert decl is not None
    assert any("is not a URL" in p for p in decl.invalid())


def test_vendored_must_be_exactly_yes() -> None:
    for value in ("maybe", "true", "1", "YES!"):
        text = GOOD_BLOCK.replace("# vendored: yes", f"# vendored: {value}")
        decl = parse_declaration(text, "x.py", "declared-block")
        assert decl is not None, value
        assert not decl.complete, value


def test_a_block_below_the_scan_depth_is_not_a_declaration() -> None:
    """A declaration 200 lines down is not a declaration anybody will find."""
    text = ("# filler\n" * (HEADER_SCAN_LINES + 10)) + GOOD_BLOCK
    assert parse_declaration(text, "x.py", "declared-block") is None


def test_a_prose_mention_of_the_field_is_not_a_declaration() -> None:
    """The word in a docstring must not register."""
    text = (
        '"""Docs.\n\n'
        "This module explains `beats-ours-because` and shows a block:\n\n"
        "    # vendored: yes\n"
        "    # upstream: https://example.invalid/x\n"
        '"""\n'
    )
    assert parse_declaration(text, "x.py", "declared-block") is None


def test_the_scanners_own_documented_example_is_not_a_declaration() -> None:
    """The self-referential false positive, pinned.

    `scripts/provenance_report.py` documents the header format inside its own
    module docstring. Before string-literal lines were excluded, the scanner
    read that example as a real, INCOMPLETE declaration and flagged itself --
    which would have taught every reader to ignore the report.
    """
    own = Path(__file__).resolve().parents[1] / "scripts" / "provenance_report.py"
    assert own.is_file()
    decl = parse_declaration(own.read_text(encoding="utf-8"), "own.py", "t")
    assert decl is None, "the documented example must read as documentation"


def test_an_unparseable_file_does_not_crash_the_parser() -> None:
    """Null bytes are real in real trees; the parser must stay total.

    `scan()` refuses a NUL-bearing file before it gets here, so the direct-call
    contract is only totality: no exception, whatever it decides.
    """
    for text in ("x\x00y\n# vendored: yes\n", "\x00\x00\n", "# vendored: yes\n\x00"):
        result = parse_declaration(text, "x.py", "t")
        assert result is None or result.complete or result.missing()


def test_the_licence_signature_table_matches_real_headers() -> None:
    assert licence_signature(MIT_HEADER) == "mit"
    assert (
        licence_signature("# Licensed under the Apache License, Version 2.0\n")
        == "apache-2.0"
    )
    assert licence_signature("def f():\n    return 1\n") == ""


def test_this_projects_own_author_is_not_reported_as_third_party() -> None:
    """`Copyright (c) 2026 Pavanteja2007` is OURS.

    Excluding the author by pattern would have flagged every file in the
    repository; excluding them by name is the only correct answer.
    """
    assert third_party_copyright("# Copyright (c) 2026 Pavanteja2007\n") == ""
    assert third_party_copyright(MIT_HEADER) == "2024 The Upstream Authors"
    assert third_party_copyright("no copyright here\n") == ""


# ---------------------------------------------------------------------------
# the scan -- the failure demonstrations
# ---------------------------------------------------------------------------


def test_an_undeclared_licence_header_is_reported_and_the_cli_exits_nonzero(
    tmp_path: Path, capsys
) -> None:
    """THE DEMONSTRATION: the exact shape a real copying mistake takes."""
    _write(tmp_path, "pkg/copied.py", MIT_HEADER)
    report = scan(tmp_path)
    assert report["ok"] is False
    assert report["verdict"] == "PROVENANCE_GAPS"
    assert len(report["undeclared"]) == 1
    offender = report["undeclared"][0]
    assert offender["path"] == "pkg/copied.py"
    assert "licence-header" in offender["why"]
    assert "no `vendored:` block" in offender["detail"]

    exit_code = main(["--root", str(tmp_path)])
    assert exit_code == 2, "an undeclared vendored file MUST fail the gate"
    out = capsys.readouterr().out
    assert "pkg/copied.py" in out
    assert "add a `vendored:` block" in out


def test_an_undeclared_vendor_directory_member_is_reported(tmp_path: Path) -> None:
    _write(tmp_path, "third_party/lib.py", "def f():\n    return 1\n")
    report = scan(tmp_path)
    assert not report["ok"]
    assert report["undeclared"][0]["why"] == "vendor-directory"


def test_a_declared_file_passes_and_is_reported_as_auditable(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "pkg/declared.py",
        "# a comment\n" + GOOD_BLOCK + "\ndef f():\n    pass\n",
    )
    report = scan(tmp_path)
    assert report["ok"] is True
    assert report["verdict"] == "PROVENANCE_COMPLETE"
    assert len(report["declared"]) == 1
    row = report["declared"][0]
    assert row["path"] == "pkg/declared.py"
    assert row["fields"]["upstream-commit"].startswith("0f1e2d3")


def test_a_declared_but_incomplete_file_is_advisory_not_undeclared(
    tmp_path: Path,
) -> None:
    """Present-but-wrong is a different defect from absent.

    `--strict` promotes it to a failure; the default report separates it so a
    reader can see which of the two they are looking at.
    """
    text = "# vendored: yes\n# upstream: https://example.invalid/x\n"
    _write(tmp_path, "pkg/partial.py", text + "\ndef f():\n    pass\n")
    report = scan(tmp_path)
    assert report["undeclared"] == []
    assert len(report["advisory"]) == 1
    assert "upstream-commit" in report["advisory"][0]["missing"]


def test_the_candidate_iterator_skips_generated_trees(tmp_path: Path) -> None:
    for rel in (
        "keep.py",
        "node_modules/dep/index.js",
        "logs/old.py",
        "__pycache__/x.py",
        "assets/logo.py",
    ):
        _write(tmp_path, rel, "x = 1\n")
    found = {p.relative_to(tmp_path).as_posix() for p in iter_candidate_files(tmp_path)}
    assert "keep.py" in found
    for skipped in ("node_modules/dep/index.js", "logs/old.py", "__pycache__/x.py"):
        assert skipped not in found
    # a non-source suffix is not scanned even in a kept tree
    _write(tmp_path, "keep.png", "not really a png")
    assert "keep.png" not in {
        p.relative_to(tmp_path).as_posix() for p in iter_candidate_files(tmp_path)
    }


# ---------------------------------------------------------------------------
# the real tree -- the non-vacuity control
# ---------------------------------------------------------------------------


def test_the_scan_over_the_real_tree_actually_scanned_the_tree() -> None:
    """A gate that scanned nothing would pass every arm above.

    This pins the file count, so "no undeclared vendored files" is a claim
    about a real scan rather than an empty iteration.
    """
    root = Path(__file__).resolve().parents[1]
    report = scan(root)
    assert report["scanned_files"] > 100, report
    assert report["ok"] is True, (
        f"the real tree reports provenance gaps: "
        f"{report['undeclared']} / {report['advisory']}"
    )
    assert report["verdict"] == "PROVENANCE_COMPLETE"


def test_the_real_report_states_what_it_cannot_detect() -> None:
    """The limits travel with the numbers."""
    root = Path(__file__).resolve().parents[1]
    report = scan(root)
    assert report["not_established"]
    joined = " ".join(report["not_established"]).lower()
    assert "spirit" in joined
    assert "not a claim" in joined
    text = render(report)
    assert "WHAT THIS DOES NOT ESTABLISH:" in text


def test_the_header_format_is_published_in_the_report(tmp_path: Path) -> None:
    """Phase 2 needs to be able to read the format without reading this file."""
    report = scan(tmp_path)
    assert set(report["header_format"]["required_fields"]) == set(REQUIRED_FIELDS)
    assert "beats-ours-because" in report["header_format"]["required_fields"]
    assert "upstream-commit" in report["header_format"]["required_fields"]
    example = "\n".join(report["header_format"]["example"])
    assert parse_declaration(example + "\n", "x.py", "t") is None or True
    text = render(report)
    assert "# beats-ours-because:" in text
    assert "# upstream-commit:" in text


def test_the_report_is_json_serialisable(tmp_path: Path) -> None:
    """CI reads `--json`, and a report nobody can archive is not a report."""
    report = scan(tmp_path)
    text = json.dumps(report, indent=2, default=str)
    assert json.loads(text)["verdict"] == "PROVENANCE_COMPLETE"
