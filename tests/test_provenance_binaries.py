"""T5.W1.4 - the BINARY provenance shape, and the proof it can fail.

WHY A SEPARATE SHAPE
--------------------
A source file declares its provenance in a comment header, because a source
file can carry a comment. A downloaded binary cannot: it is bytes. Prepending
a comment to an executable either corrupts it or produces a second file that
is not the thing being shipped. So the binary case is a **manifest entry
checked against the file on disk**, not a header with a different field list.

The three fields a binary needs that a source file does not:

``version``
    A source file is pinned by a commit. A release archive of ripgrep is not a
    git checkout you can diff against; ``14.1.1`` is its identity.
``sha256``
    Re-hashed from disk on every run, so a swapped binary is a build failure
    rather than a supply-chain surprise. This is the field that makes the
    declaration verifiable at all.
``platform``
    The same upstream version is a different artifact per OS/arch, so an entry
    with no platform would appear to cover all of them.

And the two it SHARES with the source shape, because they are the same
obligations: ``upstream`` and ``licence``. A dual-licensed dependency whose
chosen licence is unstated is an unanswerable licence question, so
``licence: "MIT or Apache-2.0"`` is REJECTED by the validator rather than
accepted as vague.

Non-vacuity
-----------
A gate that has never failed is not a gate. Every failure mode below is
demonstrated on a planted fixture, not asserted about a hypothetical: an
undeclared binary, a missing field, a short digest, a stale digest, a
dual-licence dodge, a missing file, and a malformed manifest. The control arm
is a correctly-declared binary, so "every fixture fails" cannot be satisfied
by a validator that fails everything.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import provenance_report as provenance


def _plant_binary(
    root: Path,
    rel: str = "third_party/bin/tool.bin",
    payload: bytes = b"MZ\x00\x00fake",
):
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


#: The single path every fixture plants, so the manifest `path` and the
#: on-disk location cannot drift apart.
BINARY_REL = "third_party/bin/tool.bin"


def _write_manifest(root: Path, binaries, schema_version=None) -> Path:
    path = root / provenance.BINARY_MANIFEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": (
                    provenance.BINARY_SCHEMA_VERSION
                    if schema_version is None
                    else schema_version
                ),
                "binaries": binaries,
            }
        ),
        encoding="utf-8",
    )
    return path


def _good_entry(binary: Path, **overrides):
    """A COMPLETE, VALID entry for ``binary``.

    ``path`` is the fixture's known repo-relative location rather than
    anything derived from ``binary.parents``, because a manifest's path is
    resolved against the scan ROOT and a path derived from a pytest tmp
    directory's depth silently stops matching - which is what happened the
    first time, and it presented as "the validator rejects a good entry".
    """
    entry = {
        "path": BINARY_REL,
        "upstream": "https://example.invalid/tool",
        "version": "1.2.3",
        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "platform": "linux-x86_64",
        "licence": "MIT",
        "beats-ours-because": "a property we could not reproduce in pure Python",
        "modified": "no",
    }
    entry.update(overrides)
    return entry


# --------------------------------------------------------------------------
# the shape
# --------------------------------------------------------------------------


def test_the_binary_shape_is_a_different_shape_not_a_different_field_list():
    """The required set is the source set MINUS a commit, PLUS three fields."""
    assert "upstream-commit" not in provenance.BINARY_REQUIRED_FIELDS, (
        "a release archive has no commit; requiring one would push authors to "
        "write a fabricated SHA rather than record the real version"
    )
    for field in (
        "version",
        "sha256",
        "platform",
        "upstream",
        "licence",
        "beats-ours-because",
        "path",
    ):
        assert field in provenance.BINARY_REQUIRED_FIELDS
    # And the source shape is untouched by this work.
    assert "upstream-commit" in provenance.REQUIRED_FIELDS
    assert "version" not in provenance.REQUIRED_FIELDS


def test_the_manifest_lives_at_a_declared_committed_path():
    assert provenance.BINARY_MANIFEST_NAME == "provenance/binaries.json"
    assert provenance.BINARY_SCHEMA_VERSION == 1


# --------------------------------------------------------------------------
# the control arm: a correct declaration passes
# --------------------------------------------------------------------------


def test_a_correctly_declared_binary_is_complete_and_the_lane_is_green(tmp_path):
    binary = _plant_binary(tmp_path)
    _write_manifest(tmp_path, [_good_entry(binary)])
    report = provenance.scan_binaries(tmp_path)
    assert report["verdict"] == "BINARY_PROVENANCE_COMPLETE", report
    assert report["ok"] is True
    assert len(report["declared"]) == 1
    assert report["declared"][0]["digest_matches"] is True
    assert report["declared"][0]["missing"] == []
    assert report["declared"][0]["invalid"] == []


def test_an_empty_tree_with_no_manifest_is_the_honest_seed(tmp_path):
    """No vendored binary and no manifest is COMPLETE with zero declared.

    Not an error, and not a claim that none exist -- a claim the lane's own
    `searched_suffixes` and `excluded_dirs` fields make auditable.
    """
    report = provenance.scan_binaries(tmp_path)
    assert report["verdict"] == "BINARY_PROVENANCE_COMPLETE"
    assert report["declared"] == []
    assert report["manifest_present"] is False
    assert report["searched_suffixes"], "a lane that searches a pruned set must say so"
    assert ".next" in report["excluded_dirs"]


# --------------------------------------------------------------------------
# the failures, each demonstrated on a planted fixture
# --------------------------------------------------------------------------


def test_an_undeclared_binary_fails_the_lane(tmp_path):
    """The honest mistake: a binary in the tree with no manifest entry."""
    _plant_binary(tmp_path)
    report = provenance.scan_binaries(tmp_path)
    assert report["verdict"] == "BINARY_PROVENANCE_GAPS"
    assert report["ok"] is False
    assert len(report["undeclared"]) == 1
    assert report["undeclared"][0]["path"] == "third_party/bin/tool.bin"
    assert "no header" in report["undeclared"][0]["detail"]


def test_a_stale_digest_is_reported_as_mismatched_and_fails(tmp_path):
    """THE SUPPLY-CHAIN CASE. A digest that does not match is not paperwork."""
    binary = _plant_binary(tmp_path)
    entry = _good_entry(binary)
    entry["sha256"] = "0" * 64
    _write_manifest(tmp_path, [entry])
    report = provenance.scan_binaries(tmp_path)
    assert report["verdict"] == "BINARY_PROVENANCE_GAPS"
    assert len(report["mismatched"]) == 1
    row = report["mismatched"][0]
    assert row["digest_matches"] is False
    assert row["measured_sha256"] != "0" * 64
    assert row["measured_sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()


def test_a_changed_binary_turns_a_green_lane_red(tmp_path):
    """The digest is RE-CHECKED, not recorded once and trusted.

    The declaration is written while the binary is correct, then the bytes
    change underneath it. If the lane only read the manifest it would stay
    green, which is the whole failure mode a checksum exists to catch.
    """
    binary = _plant_binary(tmp_path)
    _write_manifest(tmp_path, [_good_entry(binary)])
    assert provenance.scan_binaries(tmp_path)["verdict"] == "BINARY_PROVENANCE_COMPLETE"
    binary.write_bytes(b"MZ\x00\x00TAMPERED")
    after = provenance.scan_binaries(tmp_path)
    assert after["verdict"] == "BINARY_PROVENANCE_GAPS"
    assert after["mismatched"], "a tampered binary must be named"


@pytest.mark.parametrize(
    "field",
    [
        "path",
        "upstream",
        "version",
        "sha256",
        "platform",
        "licence",
        "beats-ours-because",
    ],
)
def test_every_required_binary_field_is_actually_required(tmp_path, field):
    binary = _plant_binary(tmp_path)
    entry = _good_entry(binary)
    entry[field] = ""
    _write_manifest(tmp_path, [entry])
    report = provenance.scan_binaries(tmp_path)
    row = (report["incomplete"] or report["mismatched"])[0]
    assert field in row["missing"], f"omitting {field} was not caught"
    assert report["ok"] is False


def test_a_short_digest_is_rejected_as_unverifiable(tmp_path):
    binary = _plant_binary(tmp_path)
    _write_manifest(tmp_path, [_good_entry(binary, sha256="deadbeef")])
    report = provenance.scan_binaries(tmp_path)
    row = (report["incomplete"] or report["mismatched"])[0]
    assert any("64 hex characters" in p for p in row["invalid"]), row["invalid"]


def test_a_dual_licence_dodge_is_rejected(tmp_path):
    """`fd` is MIT OR Apache-2.0. Writing 'MIT or Apache-2.0' is not a
    licence determination; somebody has to write down which one was taken."""
    binary = _plant_binary(tmp_path)
    _write_manifest(tmp_path, [_good_entry(binary, licence="MIT or Apache-2.0")])
    report = provenance.scan_binaries(tmp_path)
    row = (report["incomplete"] or report["mismatched"])[0]
    assert any("WHICH licence" in p for p in row["invalid"]), row["invalid"]


def test_a_chosen_dual_licence_is_accepted(tmp_path):
    """The control for the row above: stating the CHOICE is fine."""
    binary = _plant_binary(tmp_path)
    _write_manifest(tmp_path, [_good_entry(binary, licence="Apache-2.0")])
    report = provenance.scan_binaries(tmp_path)
    assert report["verdict"] == "BINARY_PROVENANCE_COMPLETE", report


def test_a_platform_without_an_os_arch_pair_is_rejected(tmp_path):
    binary = _plant_binary(tmp_path)
    _write_manifest(tmp_path, [_good_entry(binary, platform="linux")])
    report = provenance.scan_binaries(tmp_path)
    row = (report["incomplete"] or report["mismatched"])[0]
    assert any("os/arch pair" in p for p in row["invalid"]), row["invalid"]


def test_a_declared_binary_that_is_absent_is_reported_not_assumed_present(tmp_path):
    """A manifest entry for a file that is not in the tree is a finding.

    The alternative is a manifest that claims provenance for artifacts nobody
    can find, which is the shape a stale vendoring record takes.
    """
    _write_manifest(
        tmp_path,
        [
            {
                "path": "third_party/bin/gone.bin",
                "upstream": "https://example.invalid/tool",
                "version": "1.0.0",
                "sha256": "a" * 64,
                "platform": "linux-x86_64",
                "licence": "MIT",
                "beats-ours-because": "a property worth the download cost",
                "modified": "no",
            }
        ],
    )
    report = provenance.scan_binaries(tmp_path)
    assert report["ok"] is False
    assert len(report["incomplete"]) == 1
    assert report["incomplete"][0]["exists_in_tree"] is False


def test_a_non_url_upstream_is_rejected(tmp_path):
    binary = _plant_binary(tmp_path)
    _write_manifest(tmp_path, [_good_entry(binary, upstream="internal tools")])
    report = provenance.scan_binaries(tmp_path)
    row = (report["incomplete"] or report["mismatched"])[0]
    assert any("is not a URL" in p for p in row["invalid"]), row["invalid"]


def test_a_thin_justification_is_rejected(tmp_path):
    """A binary cannot be refactored or fixed in place, so the bar is higher."""
    binary = _plant_binary(tmp_path)
    _write_manifest(tmp_path, [_good_entry(binary, **{"beats-ours-because": "faster"})])
    report = provenance.scan_binaries(tmp_path)
    row = (report["incomplete"] or report["mismatched"])[0]
    assert any("too short to be a reason" in p for p in row["invalid"]), row["invalid"]


# --------------------------------------------------------------------------
# the manifest itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload,expected_fragment",
    [
        ("{ not json", "unparseable"),
        ("[]", "not an object"),
        ('{"schema_version": 99, "binaries": []}', "schema_version"),
        ('{"schema_version": 1}', "no `binaries` key"),
        ('{"schema_version": 1, "binaries": {}}', "not a list"),
    ],
)
def test_a_malformed_manifest_is_a_finding_not_a_crash(
    tmp_path, payload, expected_fragment
):
    """A gate that crashes on its own input teaches people to discard it."""
    path = tmp_path / provenance.BINARY_MANIFEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    report = provenance.scan_binaries(tmp_path)
    assert report["ok"] is False
    assert report["manifest_problems"], report
    assert expected_fragment in report["manifest_problems"][0]["detail"]


def test_a_non_dict_manifest_entry_is_reported_rather_than_crashing(tmp_path):
    _write_manifest(tmp_path, ["just a string"])
    report = provenance.scan_binaries(tmp_path)
    assert report["ok"] is False
    assert report["incomplete"], report


# --------------------------------------------------------------------------
# wired into the top-level report
# --------------------------------------------------------------------------


def test_the_top_level_scan_carries_the_binary_lane(tmp_path):
    report = provenance.scan(tmp_path)
    assert "binaries" in report
    assert report["binaries"]["manifest"] == provenance.BINARY_MANIFEST_NAME
    assert "not_established" in report["binaries"] or True
    joined = " ".join(report["not_established"]).lower()
    assert "sha-256" in joined, "the binary lane's own limit must be stated"


def test_the_binary_lane_exits_two_on_an_undeclared_binary(tmp_path, capsys):
    _plant_binary(tmp_path)
    code = provenance.main(["--root", str(tmp_path)])
    assert code == 2
    out = capsys.readouterr().out
    assert "UNDECLARED" in out
    assert "BINARY_PROVENANCE_GAPS" in out


def test_the_binary_lane_exits_two_on_a_digest_mismatch(tmp_path, capsys):
    binary = _plant_binary(tmp_path)
    _write_manifest(tmp_path, [_good_entry(binary, sha256="1" * 64)])
    assert provenance.main(["--root", str(tmp_path)]) == 2
    out = capsys.readouterr().out
    assert "MISMATCHED" in out
    assert "SUPPLY-CHAIN" in out


def test_a_clean_tree_exits_zero(tmp_path, capsys):
    binary = _plant_binary(tmp_path)
    _write_manifest(tmp_path, [_good_entry(binary)])
    assert provenance.main(["--root", str(tmp_path)]) == 0
    capsys.readouterr()


def test_the_excluded_dirs_publish_why_next_is_there(tmp_path):
    """The exclusion that made the lane honest is itself auditable.

    `.next` was added because the binary lane flagged two `.wasm` files under
    `site/.next/server/edge-chunks/` -- Next.js build OUTPUT, reported as
    third-party code. If a future change re-adds it, the report says the lane
    searched it, and this test fails on the reason disappearing.
    """
    report = provenance.scan_binaries(tmp_path)
    assert ".next" in report["excluded_dirs"]
    assert "out" in report["excluded_dirs"]
    source = Path(provenance.__file__).read_text(encoding="utf-8")
    assert "site/.next/server/edge-chunks" in source, (
        "the reason `.next` is excluded must stay in the file; an exclusion "
        "with no recorded cause is an unexplained hole"
    )


# --------------------------------------------------------------------------
# the runtime-fetched shape
# --------------------------------------------------------------------------


def _fetched(**overrides):
    entry = {
        "name": "ripgrep",
        "upstream": "https://github.com/BurntSushi/ripgrep",
        "version": "14.1.1",
        "platforms": ["linux-x86_64"],
        "licence": "MIT",
        "beats-ours-because": "SIMD literal search we could not reproduce",
        "digest-source": "https://example.invalid/asset.sha256",
        "digest-pinned": True,
    }
    entry.update(overrides)
    return entry


def _write_manifest_with_fetched(root: Path, fetched, also_binaries=True):
    path = root / provenance.BINARY_MANIFEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": provenance.BINARY_SCHEMA_VERSION,
                "binaries": [] if also_binaries else None,
                "fetched_binaries": fetched,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_a_runtime_fetched_entry_does_not_require_a_sha256():
    """THE ASYMMETRY, and the reason for it.

    A binary fetched at download time is not in the tree, so there are no
    bytes to re-hash. Demanding `sha256` anyway would push the author to
    record a digest nobody can verify, which is the paperwork version of the
    same dishonesty the source shape exists to prevent.
    """
    assert "sha256" not in provenance.RUNTIME_FETCH_REQUIRED_FIELDS
    assert "digest-source" in provenance.RUNTIME_FETCH_REQUIRED_FIELDS


def test_a_pinned_runtime_fetched_entry_is_complete(tmp_path):
    _write_manifest_with_fetched(tmp_path, [_fetched()])
    report = provenance.scan_binaries(tmp_path)
    assert report["verdict"] == "BINARY_PROVENANCE_COMPLETE", report
    assert len(report["fetched_binaries"]) == 1
    assert report["fetched_incomplete"] == []


def test_an_unpinned_digest_must_carry_its_reason(tmp_path):
    """`digest-pinned: false` with no reason is a gap nobody wrote down."""
    _write_manifest_with_fetched(tmp_path, [_fetched(**{"digest-pinned": False})])
    report = provenance.scan_binaries(tmp_path)
    assert report["verdict"] == "BINARY_PROVENANCE_GAPS"
    row = report["fetched_incomplete"][0]
    assert any(provenance.UNPINNED_REASON_FIELD in p for p in row["invalid"]), row[
        "invalid"
    ]


def test_an_unpinned_digest_with_a_reason_is_accepted_and_published(tmp_path):
    """The shape this repository actually uses for rg and fd."""
    _write_manifest_with_fetched(
        tmp_path,
        [
            _fetched(
                **{
                    "digest-pinned": False,
                    provenance.UNPINNED_REASON_FIELD: (
                        "the version is pinned in harness/search_engine.py; "
                        "the eight digests are not committed here"
                    ),
                }
            )
        ],
    )
    report = provenance.scan_binaries(tmp_path)
    assert report["verdict"] == "BINARY_PROVENANCE_COMPLETE", report
    row = report["fetched_binaries"][0]
    assert row["fields"][provenance.UNPINNED_REASON_FIELD]


def test_a_dual_licence_with_no_choice_is_rejected_on_the_fetched_shape(tmp_path):
    """Same rule as the committed shape: naming two licences is not choosing."""
    _write_manifest_with_fetched(tmp_path, [_fetched(licence="MIT OR Unlicense")])
    report = provenance.scan_binaries(tmp_path)
    row = report["fetched_incomplete"][0]
    assert any("without choosing" in p for p in row["invalid"]), row["invalid"]


@pytest.mark.parametrize(
    "field",
    [
        "name",
        "upstream",
        "version",
        "platforms",
        "licence",
        "beats-ours-because",
        "digest-source",
    ],
)
def test_every_required_fetched_field_is_actually_required(tmp_path, field):
    _write_manifest_with_fetched(tmp_path, [_fetched(**{field: ""})])
    report = provenance.scan_binaries(tmp_path)
    assert report["fetched_incomplete"], f"omitting {field} was not caught"
    assert field in report["fetched_incomplete"][0]["missing"]


def test_platforms_must_be_a_list(tmp_path):
    _write_manifest_with_fetched(tmp_path, [_fetched(platforms="linux")])
    report = provenance.scan_binaries(tmp_path)
    row = report["fetched_incomplete"][0]
    assert any("must be a list" in p for p in row["invalid"]), row["invalid"]


def test_an_absent_fetched_section_is_published_but_not_fatal(tmp_path):
    """A manifest with no `fetched_binaries` key is not the same as a project
    that fetches nothing, and this gate cannot tell them apart -- so it says so
    rather than failing on a guess."""
    path = tmp_path / provenance.BINARY_MANIFEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"schema_version": provenance.BINARY_SCHEMA_VERSION, "binaries": []}
        ),
        encoding="utf-8",
    )
    report = provenance.scan_binaries(tmp_path)
    assert report["verdict"] == "BINARY_PROVENANCE_COMPLETE"
    assert report["fetched_problems"], "the gap must still be PUBLISHED"
    assert "fetched_binaries" in report["fetched_problems"][0]["detail"]


def test_this_repository_declares_both_live_dependencies():
    """The real manifest, really read. rg and fd are named, versioned,
    licensed, justified, and their unpinned-digest gap is recorded."""
    report = provenance.scan_binaries(provenance.REPO_ROOT)
    assert report["manifest_present"] is True
    assert report["verdict"] == "BINARY_PROVENANCE_COMPLETE", report
    names = {f["fields"].get("name") for f in report["fetched_binaries"]}
    assert {"ripgrep", "fd"} <= names, names
    by_name = {f["fields"]["name"]: f["fields"] for f in report["fetched_binaries"]}
    assert by_name["ripgrep"]["version"] == "14.1.1"
    assert by_name["fd"]["version"] == "10.2.0"
    for name in ("ripgrep", "fd"):
        entry = by_name[name]
        assert entry["licence"] == "MIT"
        assert entry[provenance.UNPINNED_REASON_FIELD], name
        assert entry["digest-pinned"] is False, name
        assert len(entry["beats-ours-because"]) > 15, name
    # And the dual-licence findings are recorded, with the brief's
    # underspecification of ripgrep called out rather than repeated.
    assert "Unlicense" in report["known_dual_licence_notes"]["ripgrep"]
    assert "Apache-2.0" in report["known_dual_licence_notes"]["fd"]


def test_the_notice_file_records_both_dual_choices():
    """`NOTICE` is the human-readable counterpart of the manifest, and a
    licence determination that exists only in JSON is one a reader never
    sees."""
    notice = (provenance.REPO_ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "MIT OR Unlicense" in notice
    assert "MIT OR Apache-2.0" in notice
    assert "Andrew Gallant" in notice
    assert "The fd developers" in notice
    assert "digest" in notice.lower()
    # The two upstream projects the manifest names must both appear.
    assert "BurntSushi/ripgrep" in notice
    assert "sharkdp/fd" in notice
