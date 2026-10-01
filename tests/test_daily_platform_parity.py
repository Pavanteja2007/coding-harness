"""Windows and no-colour parity for the daily path, on REAL temp trees.

This suite answers one question with measurements rather than assumptions:
**does the same daily path behave correctly on Windows as on POSIX?** Every
filesystem claim here is made against a real temporary tree under the test
runner's own ``tmp_path``. There is no filesystem mock in this file, and
adding one would defeat it -- a mocked ``write_text`` cannot show that a CRLF
file survives an edit, and a mocked ``open`` cannot show that writing ``NUL``
reports success and reads back empty.

The four areas, matching the brief:

* :class:`TestTheDailyPathOnARealWindowsTree` -- CRLF preservation on edit,
  drive-letter paths, long paths, reserved device names, and
  case-insensitive filesystem behaviour, all measured.
* :class:`TestEveryViewportAndTerminalShape` -- 60/80/100/120/200 columns plus
  split and vertical terminals: no ragged edge, no overlap, no layout jump.
* :class:`TestEverySurfaceIsLegibleWithoutHue` -- colour off and ``TERM=dumb``
  across the surfaces Prompts 01-05 introduced.
* :class:`TestAPipeCarriesNoControlBytes` -- real child processes, byte-counted.

**Capability, not assumption.** Long-path support and case-insensitivity are
properties of the MACHINE. The suite probes them with
:func:`shared.platform.capability_report` and gates on the measurement, so a
host without long paths reads a skip reason rather than a false pass. The one
thing asserted unconditionally is that the probe itself worked and cleaned up
after itself.

**Two tests here characterise a measured DEFECT in code this round does not
own** rather than asserting correct behaviour that does not exist. They are
named after what they observe, they pass, and they are the tripwire that makes
a future fix loud. See ``TestTheDailyPathOnARealWindowsTree`` in
``shared/AGENTS.md`` and the ``not_edited`` section of
``logs/product-round/terminal-07.json`` for the owners.

Host-only: no Docker, no provider, no network, no credential. The only child
processes are this interpreter running ``python -m cli``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from cli import a11y, auth, commands, design, models, review
from cli import tui_components as tui
from shared import platform as pf

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The five viewports the brief names, plus the two terminal SHAPES the layout
#: authority has to recognise.  Both are needed: a narrow-but-wide terminal and
#: a wide-but-short one are different problems.
VIEWPORT_WIDTHS = (60, 80, 100, 120, 200)
VIEWPORT_HEIGHT = 36
SPLIT_VIEWPORT = (60, 24)
VERTICAL_VIEWPORT = (50, 160)

#: A byte pattern that must never reach a pipe.  C0 minus the three bytes a
#: text stream legitimately uses (LF, and the two we exclude explicitly).
CONTROL_BYTES = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[@-Z\\-_]")
BELL = "\a"


@pytest.fixture(autouse=True)
def _no_ambient_effort(monkeypatch):
    """Keep an ambient ``VEX_EFFORT`` out of this process.

    ``cli.commands.apply_effort`` writes ``os.environ["VEX_EFFORT"]`` and
    never restores it, so a test file that types ``/effort high`` poisons
    every later ``get_config(None)`` in the same process.  Recorded in
    VEX-PF-03 section 7; cheap to defend against here.
    """
    monkeypatch.delenv("VEX_EFFORT", raising=False)


@pytest.fixture(scope="module")
def capabilities(tmp_path_factory):
    """Measure this host's filesystem capabilities once, on a real tree."""
    return pf.capability_report(tmp_path_factory.mktemp("capability-probe"))


def _crlf(text: str) -> bytes:
    """Return ``text`` with CRLF line endings."""
    return text.replace("\n", "\r\n").encode("utf-8")


# ===========================================================================
# 1. Windows parity for the daily path, on a real temp tree
# ===========================================================================


class TestTheDailyPathOnARealWindowsTree:
    """The four filesystem hazards, measured against real files."""

    # -- CRLF ---------------------------------------------------------------

    def test_editing_a_crlf_file_leaves_every_untouched_line_byte_identical(
        self, tmp_path
    ):
        """The daily edit must not re-encode the lines it did not touch.

        This is the load-bearing CRLF claim and it is made through the REAL
        edit primitive -- ``WorkspaceJournal``, the kernel's own workspace
        guard -- against a real file whose bytes are CRLF.  Asserting the
        whole file equals the expectation is what makes it a real claim: a
        read-modify-write that transliterated line endings would still produce
        a readable file, it would just produce different bytes.
        """
        from harness.agent_kernel.workspace import WorkspaceJournal

        target = tmp_path / "sample.txt"
        expected = b"line one\r\nTARGET\r\nline three\r\n"
        target.write_bytes(expected)

        journal = WorkspaceJournal(tmp_path)
        journal.edit("sample.txt", "TARGET", "REPLACED")

        assert target.read_bytes() == b"line one\r\nREPLACED\r\nline three\r\n"
        assert journal.changed_files() == ["sample.txt"]

    def test_a_crlf_file_reports_its_line_endings_so_a_caller_can_act_on_them(
        self, tmp_path
    ):
        """A caller must be able to ask what a file's line endings are.

        Not decoration: the edit path can only choose between the plain
        text-mode write and the preserving one if it knows which case it is
        in, and a text-mode write is correct for CRLF and destructive for
        mixed.  The receipt is measured from the bytes on disk.
        """
        target = tmp_path / "sample.txt"
        target.write_bytes(b"a\r\nb\r\n")

        receipt = pf.newline_report(target)

        assert receipt["available"] is True
        assert receipt["style"] == "crlf"
        assert receipt["crlf"] == 2
        assert receipt["needs_preserving_write"] is False

    def test_the_preserving_primitive_round_trips_a_mixed_newline_file_unchanged(
        self, tmp_path
    ):
        """A file that mixes CRLF and LF must survive a read-modify-write.

        ``Path.read_text()`` applies universal newlines, so a mixed file reads
        back as all-``\\n`` and a text-mode write then re-emits
        ``os.linesep`` for every line.  The bytes are therefore not merely
        reformatted -- unrelated lines change.  This is the primitive that
        prevents it, asserted byte-for-byte on a real mixed file.
        """
        target = tmp_path / "mixed.txt"
        original = b"a\r\nb\nTARGET\nc\r\nd"
        target.write_bytes(original)

        text = pf.read_preserving(target)
        assert text == "a\r\nb\nTARGET\nc\r\nd", (
            "read_preserving must not translate; a translated read cannot be "
            "written back losslessly"
        )
        pf.write_preserving(target, text.replace("TARGET", "NEW"))

        assert target.read_bytes() == b"a\r\nb\nNEW\nc\r\nd"

    def test_the_daily_edit_homogenises_a_mixed_newline_file(self, tmp_path):
        """MEASURED DEFECT, owner: ``harness/agent_kernel/workspace.py``.

        The real workspace guard reads with universal newlines and writes with
        ``os.linesep``, so a mixed file is silently rewritten to one style and
        lines the run never touched change bytes.  This test pins the measured
        shape so the fix is loud: the day ``edit()`` routes through
        ``shared.platform.write_preserving``, this assertion fails and has to
        be updated deliberately rather than drifting.
        """
        from harness.agent_kernel.workspace import WorkspaceJournal

        target = tmp_path / "mixed.txt"
        target.write_bytes(b"a\r\nb\nTARGET\nc\r\n")

        WorkspaceJournal(tmp_path).edit("mixed.txt", "TARGET", "NEW")

        after = target.read_bytes()
        assert after == b"a\r\nb\r\nNEW\r\nc\r\n", (
            "the daily edit homogenised a mixed-newline file; "
            f"observed {after!r}"
        )
        # The untouched LF lines are the ones that changed, which is the part
        # that matters: the damage is to lines the run never read.
        assert b"b\n" not in after and b"c\n" not in after

    def test_the_daily_edit_converts_an_lf_file_to_crlf(self, tmp_path):
        """MEASURED DEFECT, owner: ``harness/agent_kernel/workspace.py``.

        The mirror image of the mixed case and the more common one: a file
        stored with LF endings is rewritten as CRLF in full, because
        ``read_text`` normalises to ``\\n`` and ``write_text`` re-emits
        ``os.linesep``.

        The consequence is not cosmetic and it is not hypothetical. With
        ``core.autocrlf=false`` -- what CI and every Linux checkout use -- a
        one-token edit to a two-line LF file makes ``git diff --numstat``
        report **2 additions and 2 deletions**: the entire file. The review
        surface, which is the product's central promise, is showing a
        whole-file rewrite for a one-token change.
        """
        from harness.agent_kernel.workspace import WorkspaceJournal

        target = tmp_path / "mod.py"
        original = b"def f():\n    return 1\n"
        target.write_bytes(original)

        WorkspaceJournal(tmp_path).edit("mod.py", "return 1", "return 2")

        after = target.read_bytes()
        assert after == b"def f():\r\n    return 2\r\n", (
            f"the daily edit converted an LF file to CRLF; observed {after!r}"
        )
        assert after != original.replace(b"return 1", b"return 2"), (
            "if a preserving write has landed, this assertion fails and the "
            "expected bytes become the original with only the token changed"
        )

    def test_a_preserving_edit_leaves_an_lf_file_byte_identical_outside_the_edit(
        self, tmp_path
    ):
        """The remedy, on the same file shape, so the two tests read as a pair."""
        target = tmp_path / "mod.py"
        target.write_bytes(b"def f():\n    return 1\n")

        text = pf.read_preserving(target)
        pf.write_preserving(target, text.replace("return 1", "return 2"))

        assert target.read_bytes() == b"def f():\n    return 2\n"

    # -- drive letters ------------------------------------------------------

    def test_a_drive_lettered_path_is_absolute_in_both_grammars(self, tmp_path):
        """``C:/x`` must be recognised as absolute by POSIX *and* Windows.

        ``PurePosixPath("C:/x").is_absolute()`` is ``False`` and
        ``PureWindowsPath("/etc/hosts").is_absolute()`` is ``True``, so a
        containment check written against the host's own ``Path`` mis-reads the
        other platform's absolute paths -- and a repository-relative check
        then treats ``/etc/hosts`` as relative to the repository.  That is the
        defect this predicate closes.
        """
        assert pf.is_absolute_anywhere("C:/Windows/System32") is True
        assert pf.is_absolute_anywhere("C:\\Windows\\System32") is True
        assert pf.is_absolute_anywhere("/etc/hosts") is True
        assert pf.is_absolute_anywhere("/tmp/x") is True
        assert pf.is_absolute_anywhere("src/relative.py") is False
        assert pf.is_absolute_anywhere("") is False
        assert pf.is_absolute_anywhere(None) is False

    def test_containment_is_case_insensitive_so_a_case_change_cannot_escape(
        self,
    ):
        """Changing a path's CASE must not make it escape the repository.

        The filesystem is case-insensitive and ``relative_to`` is not, so a
        containment check built on ``relative_to`` alone answers "not under"
        for a path that is, in fact, inside.  ``C:/Repo/src/a.py`` and
        ``c:/repo`` are one place.
        """
        assert pf.path_is_under("C:/Repo/src/a.py", "c:/repo") is True
        assert pf.path_is_under("c:/REPO/SRC/A.PY", "C:/Repo") is True

    def test_containment_refuses_a_different_drive_rather_than_raising(self):
        """A drive mismatch is a refusal, not an exception.

        ``Path("C:/a").relative_to("D:/")`` raises ``ValueError``.  A
        containment check that lets that escape is a containment check whose
        caller has to wrap every call in a try.
        """
        assert pf.path_is_under("D:/secret", "C:/repo") is False
        assert pf.path_is_under("C:/repo", "D:/repo") is False

    def test_containment_refuses_a_sibling_that_shares_a_name_prefix(self):
        """``C:/repository`` is NOT under ``C:/repo``.

        The naive ``startswith(root)`` form says it is, which is how a
        containment check becomes an escape hatch.  Pinned because the fix is
        one separator and the regression is silent.
        """
        assert pf.path_is_under("C:/repository/x.py", "C:/repo") is False
        assert pf.path_is_under("C:/repo-evil/x.py", "C:/repo") is False
        assert pf.path_is_under("C:/repo", "C:/repo", strict=True) is False
        assert pf.path_is_under("C:/repo", "C:/repo", strict=False) is True

    def test_containment_holds_for_a_real_tree_on_this_host(self, tmp_path):
        """The same predicate, on the real directory this test runs in."""
        nested = tmp_path / "pkg"
        nested.mkdir()
        (nested / "module.py").write_text("x = 1\n", encoding="utf-8")

        assert pf.path_is_under(nested / "module.py", tmp_path) is True
        assert pf.path_is_under(tmp_path / "elsewhere.py", tmp_path / "pkg") is False
        assert pf.path_is_under(nested, tmp_path) is True
        assert pf.path_is_under(tmp_path, tmp_path, strict=True) is False
        assert pf.path_is_under(tmp_path, tmp_path, strict=False) is True

    def test_two_spellings_of_one_filename_are_reported_as_equivalent(
        self, capabilities
    ):
        """Path identity must follow the FILESYSTEM, not the spelling.

        Gated on the measured answer rather than on ``os.name``, because the
        question is genuinely per-machine and a POSIX host must be able to say
        ``False`` without the suite going red.
        """
        upper = Path("C:/Repo/Module.PY")
        lower = Path("c:/repo/module.py")

        assert pf.path_equivalent(upper, lower) is bool(capabilities.case_insensitive)
        assert pf.path_equivalent("C:/a", "D:/a") is False
        assert pf.normalize_relative("C:/Repo\\Sub") == pf.normalize_relative(
            "c:/repo/sub"
        )

    # -- long paths ---------------------------------------------------------

    def test_a_long_path_is_written_and_read_back_on_this_host(
        self, capabilities, tmp_path
    ):
        """Create and read back a path past the 260-character legacy limit.

        Windows has shipped two answers to this -- the legacy ``MAX_PATH`` and
        the opt-in extended-length form -- and they are a property of the
        machine's policy, not of this source file.  The suite therefore
        asserts the PROBE ran and cleaned up, and asserts the create/read-back
        only where the probe says the host supports it.
        """
        deep = tmp_path
        created: list[Path] = []
        while len(str(deep)) < 380 and len(created) < 24:
            deep = deep / ("segment" + "y" * 14 + str(len(created)))
            deep.mkdir()
            created.append(deep)
        leaf = deep / ("file" + "z" * 30 + ".txt")

        if capabilities.long_paths:
            leaf.write_text("long-path-probe", encoding="utf-8")
            assert leaf.is_file()
            assert len(str(leaf)) > 260
            assert leaf.read_text(encoding="utf-8") == "long-path-probe"
        else:
            pytest.skip(
                "this host does not support paths past MAX_PATH: "
                f"{capabilities.long_path_reason}"
            )

    def test_the_long_path_probe_measures_rather_than_assumes(self, capabilities):
        """The capability receipt must carry a real measurement, not a default."""
        assert capabilities.available is True
        assert capabilities.probes == (
            "long_path",
            "case_insensitive",
            "reserved_device_liveness",
        )
        assert capabilities.os_name == os.name
        assert capabilities.linesep == os.linesep
        if capabilities.long_paths:
            assert capabilities.long_path_depth > 260
        else:
            assert capabilities.long_path_reason

    def test_the_capability_probes_leave_no_residue_in_a_caller_directory(
        self, tmp_path
    ):
        """A probe must not change the tree it measured.

        A leftover file or directory here would be reported by every snapshot
        diff as a spurious change, and a snapshot diff is what decides whether
        a run is reviewable.
        """
        before = sorted(entry.name for entry in tmp_path.iterdir())

        pf.capability_report(tmp_path)

        assert sorted(entry.name for entry in tmp_path.iterdir()) == before

    # -- reserved device names ----------------------------------------------

    def test_the_reserved_set_is_the_complete_twenty_two_and_excludes_com0(self):
        """The set must be complete and must not over-refuse.

        ``COM0`` and ``LPT0`` are NOT reserved devices.  A validator that
        refuses them teaches people to work around the validator, which is
        worse than the bug it was added to fix.
        """
        expected = {"CON", "PRN", "AUX", "NUL"}
        expected |= {f"COM{index}" for index in range(1, 10)}
        expected |= {f"LPT{index}" for index in range(1, 10)}

        assert expected == pf.WINDOWS_RESERVED_NAMES
        assert len(pf.WINDOWS_RESERVED_NAMES) == 22
        assert pf.is_reserved_device_name("COM0") is False
        assert pf.is_reserved_device_name("LPT0") is False

    def test_every_reserved_name_is_refused_with_a_sentence_naming_the_consequence(
        self,
    ):
        """All 22 must be refused, case-insensitively and extension-insensitively.

        ``nul.txt`` is the spelling a person actually types; ``NUL.`` is the
        same device because Windows discards the trailing dot.  A validator
        that only catches the bare stem misses both.
        """
        for name in sorted(pf.WINDOWS_RESERVED_NAMES):
            assert pf.is_reserved_device_name(name) is True, name
            assert pf.is_reserved_device_name(name.lower()) is True, name
            assert pf.is_reserved_device_name(f"{name}.txt") is True, name
            assert pf.is_reserved_device_name(f"{name}.") is True, name
            refusal = pf.reserved_name_refusal(name)
            assert refusal, name
            assert "reserved Windows device name" in refusal
            assert "read back empty" in refusal, (
                "the refusal must name the consequence, not restate the rule"
            )
        for lookalike in ("console.py", "nullable.txt", "COM10", "auxiliary.md"):
            assert pf.is_reserved_device_name(lookalike) is False, lookalike
            assert pf.reserved_name_refusal(lookalike) == ""

    def test_a_reserved_name_in_a_directory_position_is_refused_too(self):
        """Every path COMPONENT is checked, not just the leaf.

        A reserved name in a directory position is exactly as unroutable as
        one in a filename position, and a leaf-only validator misses it.
        """
        assert pf.first_reserved_segment("src/NUL/helper.py") == "NUL"
        assert pf.first_reserved_segment("CON/src/helper.py") == "CON"
        assert pf.first_reserved_segment("C:/src/ok.py") == ""
        assert pf.first_reserved_segment("src/ok.py") == ""
        assert pf.first_reserved_segment(None) == ""

    def test_writing_a_reserved_device_name_can_report_success_and_read_back_empty(
        self, tmp_path
    ):
        """MEASURED on this host: the reserved names that lose writes.

        ``open()`` returns success, there is no directory entry, and reading
        the path back yields nothing.  Both halves of the answer are true at
        once, which is the whole hazard: a tool that says "wrote NUL" and a
        reader that says "no such file" are each correct.

        Every member of the set is measured, so a host that resolves them to
        real files (some do) is reported as safe rather than assumed unsafe.
        The assertion is that write-success IMPLIES read-back, and the set of
        names that violates it is reported, not assumed to be all 22.
        """
        losers = []
        for name in sorted(pf.WINDOWS_RESERVED_NAMES):
            target = tmp_path / name
            try:
                with open(target, "w", encoding="utf-8") as handle:
                    handle.write(f"payload-{name}")
            except OSError:
                continue  # the host refuses the write outright: safe
            if not target.is_file():
                losers.append(name)
                continue
            try:
                if target.read_text(encoding="utf-8") != f"payload-{name}":
                    losers.append(name)
            except OSError:
                losers.append(name)

        # Whatever this host does, the measurement must be reproducible and
        # the probe must agree with the direct write.
        measured_loser, _ = pf.probe_reserved_device_liveness(tmp_path, probe="NUL")
        assert measured_loser is (("NUL" in losers) or not (tmp_path / "NUL").is_file())
        assert isinstance(losers, list)
        # The point of the module: the refusal covers every name, whether or
        # not this particular host loses it.
        for name in losers:
            assert pf.is_reserved_device_name(name), name

    def test_the_daily_write_tool_accepts_a_reserved_name_and_it_cannot_be_read_back(
        self, tmp_path
    ):
        """MEASURED DEFECT, owner: ``harness/agent_kernel/workspace.py``.

        ``safe_path("NUL")`` resolves and ``write("NUL", ...)`` RETURNS
        ``"NUL"`` -- the tool reports the write as done.  The immediate read
        then raises ``FileNotFoundError`` because nothing was created.  The
        shape matters more than the name: a write that reports success and
        cannot be read back is the worst of the four outcomes, because nothing
        downstream knows to look.

        Pinned so the day ``safe_path`` consults
        ``shared.platform.is_reserved_device_name``, this fails and the fix
        has to be made deliberately.
        """
        from harness.agent_kernel.workspace import WorkspaceJournal

        journal = WorkspaceJournal(tmp_path)
        resolved = journal.safe_path("NUL")

        assert resolved is not None, (
            "if the daily path refuses reserved device names this test must be "
            "updated: safe_path returned None and the defect is fixed"
        )
        reported = journal.write("NUL", "payload-for-nul")
        assert reported == "NUL"

        with pytest.raises(OSError):
            journal.read("NUL")

    def test_the_daily_read_never_loses_a_reserved_name_silently(self, tmp_path):
        """A read of a reserved name must fail LOUDLY, not return empty.

        The defect above is only dangerous because the write is silent.  This
        pins the half that is already correct, so a fix that makes ``write``
        refuse cannot regress the reader into returning ``""`` -- which would
        be a different and equally bad lie.
        """
        from harness.agent_kernel.workspace import WorkspaceJournal

        journal = WorkspaceJournal(tmp_path)

        with pytest.raises(OSError):
            journal.read("CON")

    def test_the_reserved_device_evidence_is_a_receipt_not_a_verdict(self, tmp_path):
        """A probe failure and a probe that found a hazard are different.

        ``reserved_device_reason`` is empty when the probe RAN, even when it
        found silent loss.  Collapsing the two would report a machine that
        correctly measured the hazard as a machine whose probe failed, which
        is the opposite of the truth.
        """
        lost, reason = pf.probe_reserved_device_liveness(tmp_path)

        assert lost is True, (
            "on this host writing NUL loses the write; if a future host "
            "resolves NUL to a real file this becomes a real finding about "
            "that host, not a defect in the probe"
        )
        assert reason == ""
        evidence = pf.reserved_device_evidence(tmp_path)
        assert "open() reported success" in evidence
        assert "directory_entry=False" in evidence

    # -- the daily path, all four at once -----------------------------------

    def test_a_real_repository_tree_survives_a_full_read_edit_verify_cycle(
        self, tmp_path
    ):
        """The whole daily shape on a real tree: CRLF source, drive-letter
        root, deep path, and a change that is reported honestly.

        One test because the hazards are not independent in practice -- a
        single real repository on a real drive letter containing a real CRLF
        source is the thing a Windows user actually has.
        """
        from harness.agent_kernel.workspace import WorkspaceJournal

        package = tmp_path / "src" / "pkg"
        package.mkdir(parents=True)
        source = package / "core.py"
        original = _crlf("def total(values):\n    return sum(values)\n")
        source.write_bytes(original)
        deep = tmp_path
        for index in range(6):
            deep = deep / f"nested{index}"
        deep.mkdir(parents=True)
        (deep / "notes.txt").write_text("deep\n", encoding="utf-8")

        journal = WorkspaceJournal(tmp_path)
        assert journal.read("src/pkg/core.py") == "def total(values):\n    return sum(values)\n"
        journal.edit("src/pkg/core.py", "sum(values)", "len(values)")

        assert source.read_bytes() == _crlf("def total(values):\n    return len(values)\n")
        assert sorted(journal.changed_files()) == ["src/pkg/core.py"]
        assert (deep / "notes.txt").read_text(encoding="utf-8") == "deep\n"
        assert pf.path_is_under(source, tmp_path) is True
        assert pf.first_reserved_segment(source) == ""


# ===========================================================================
# 2. Every viewport and terminal shape
# ===========================================================================


class TestEveryViewportAndTerminalShape:
    """60/80/100/120/200 plus split and vertical: no ragged edge, no overlap."""

    @pytest.mark.parametrize("width", VIEWPORT_WIDTHS)
    @pytest.mark.parametrize("mode", ("auto", "show", "hide"))
    def test_every_declared_region_is_inside_the_viewport(self, width, mode):
        """All six regions are on screen and in bounds at every width and mode.

        ``out_of_bounds()`` empty is the pass condition.  The sidebar mode is
        parameterised because it is a user preference, not a fact about the
        terminal, and a layout that only works in the default mode is a layout
        that breaks the moment somebody presses ``ctrl+5``.
        """
        spec = design.resolve_layout(
            width, VIEWPORT_HEIGHT, sidebar=mode, statusline_rows=1
        )

        assert spec.out_of_bounds() == ()
        assert set(spec.regions) == {
            design.Region(name=name, widget_id=widget, x=0, y=0, width=0, height=0)
            for name, widget in design.REGION_IDS.items()
        } or len(spec.regions) == len(design.REGION_IDS)
        for name in design.REGION_IDS:
            region = spec.region(name)
            assert region.within(width, VIEWPORT_HEIGHT), (
                f"{name} at {width} columns is off screen: "
                f"x={region.x} y={region.y} w={region.width} h={region.height}"
            )

    @pytest.mark.parametrize("width", VIEWPORT_WIDTHS)
    @pytest.mark.parametrize("mode", ("auto", "show", "hide"))
    def test_no_two_regions_occupy_the_same_cell(self, width, mode):
        """No two regions collide, and a pinned band is not a collision.

        Overlap, not just out-of-bounds, is what makes a layout unreadable: two
        regions sharing cells means one is painted over the other and whichever
        is mounted later silently wins.  A bounds check cannot see it, because
        both rectangles can be entirely on screen and still collide.

        One shape is deliberately NOT a collision. A one-row band whose rows
        fall inside a larger region is a band PINNED to that region's edge --
        ``#vex-runline`` and ``#vex-announce`` are ``height: 1`` siblings of
        ``#vex-body`` inside a ``Screen { layout: vertical }``, so they are
        carved out of the content area rather than laid over it. Treating
        that as a collision would be a test that punishes correct behaviour,
        so the band relationship is DERIVED from the geometry (a region whose
        whole extent lies inside another's) rather than from a hardcoded name
        list, which means a genuinely new collision is still caught.

        The bands must not collide with each other, and each must be inside the
        content area it is pinned to.
        """
        spec = design.resolve_layout(
            width, VIEWPORT_HEIGHT, sidebar=mode, statusline_rows=1
        )
        visible = [region for region in spec.regions if region.visible]

        def inside(inner, outer):
            return (
                inner.x >= outer.x
                and inner.right <= outer.right
                and inner.y >= outer.y
                and inner.bottom <= outer.bottom
            )

        def band_of(candidate, host):
            """Return whether ``candidate`` is a band carved from ``host``.

            A band is strictly SHORTER than the thing it sits in, and its rows
            fall inside that thing's rows.  Both halves matter: without the
            first, two side-by-side peers of equal height that happen to share
            a row would be excused; without the second, a short region in a
            different column would be excused.

            Full width is allowed and is the normal case -- the run line and
            the announcement span the whole terminal and sit below both the
            transcript and the context rail, because the real app is one
            ``Screen { layout: vertical }``.  So the rule is about HEIGHT and
            rows, never about width.
            """
            return (
                candidate.height < host.height
                and candidate.y >= host.y
                and candidate.bottom <= host.bottom
            )

        bands: list[tuple[str, str]] = []
        collisions = []
        for index, first in enumerate(visible):
            for second in visible[index + 1 :]:
                overlap_x = min(first.right, second.right) - max(first.x, second.x)
                overlap_y = min(first.bottom, second.bottom) - max(first.y, second.y)
                if overlap_x <= 0 or overlap_y <= 0:
                    continue
                if band_of(first, second):
                    bands.append((first.name, second.name))
                elif band_of(second, first):
                    bands.append((second.name, first.name))
                else:
                    collisions.append(
                        f"{first.name}/{second.name} {overlap_x}x{overlap_y}"
                    )
        assert not collisions, (
            f"regions collide at {width} columns ({mode}): {collisions}"
        )

        # A band is one row tall, and it really is inside the region it is
        # carved from -- a band that grows is a region that overlaps.
        for band_name, host_name in bands:
            band = spec.region(band_name)
            host = spec.region(host_name)
            assert band.height == 1, (
                f"{band_name} is carved from {host_name} at height "
                f"{band.height}; a band is one row and a taller one is a region "
                "that overlaps its host"
            )
            assert band.y >= host.y and band.bottom <= host.bottom, (
                f"{band_name} is a band of {host_name} but its rows fall outside"
            )

    def test_a_pinned_band_is_carved_out_of_the_content_area_not_laid_over_it(
        self,
    ):
        """The one place the layout model and the app's own CSS can disagree.

        The real app is ``Screen { layout: vertical }`` with ``#vex-body
        { width: 1fr }`` and ``#vex-runline`` / ``#vex-announce`` at ``height:
        1``, so those three stack and do not overlap; the transcript gets
        whatever is left.  The authority therefore has to say what is left.

        As measured, ``design.resolve_layout(60, 36)`` reports the transcript
        as ``y=1 h=31`` -- rows 1 through 31 -- with the run line on row 30 and
        the announcement on row 31.  Both bands are inside the transcript's own
        span, so the transcript rectangle is the content AREA and the bands are
        carved from its bottom edge.  That is a coherent convention, and this
        test pins it: the bands are contained, they do not touch each other,
        and the scrollable height is the area minus the bands.

        The trap, and the request, is that ``Region.height`` is the AREA.  A
        consumer that reads it as the scroll viewport over-reports by the
        number of bands.  Recorded here so the convention is a decision
        somebody made rather than an accident.  Owner: ``cli/design.py``.
        """
        spec = design.resolve_layout(60, VIEWPORT_HEIGHT, sidebar="auto")
        transcript = spec.region("transcript")
        runline = spec.region("runline")
        announce = spec.region("announce")

        # Both bands are inside the transcript's rows -- that is the convention.
        assert runline.y >= transcript.y
        assert runline.bottom <= transcript.bottom
        assert announce.y >= transcript.y
        assert announce.bottom <= transcript.bottom
        # They stack on distinct rows and do not touch.
        assert runline.bottom <= announce.y, "the run line and announcement collide"
        # The scrollable height is the area less the bands, and the bands start
        # exactly where the scrollable area ends.
        bands = runline.height + announce.height
        scrollable = transcript.height - bands
        assert transcript.y + scrollable == runline.y, (
            "the transcript's rows below the bands are not accounted for: "
            f"transcript y={transcript.y} h={transcript.height} "
            f"bands={bands} runline y={runline.y} (scrollable={scrollable})"
        )
        # A statusline that speaks must be carved out of the same area, and
        # must not push the transcript off its own rows.
        speaking = design.resolve_layout(
            60, VIEWPORT_HEIGHT, sidebar="auto", statusline_rows=1
        )
        assert speaking.region("transcript").height < transcript.height, (
            "a statusline with a fact to state must cost the content area a row"
        )
        assert speaking.out_of_bounds() == ()

    @pytest.mark.parametrize("width", VIEWPORT_WIDTHS)
    def test_no_rendered_line_exceeds_the_viewport_width(self, width):
        """The ragged edge: every rendered row fits the terminal it renders in.

        Driven through the real renderers rather than through
        ``resolve_layout`` alone, because a region can be correctly placed and
        still publish a line longer than the region.
        """
        shell = tui.resolve_shell_layout(width, VIEWPORT_HEIGHT)
        header = tui.HeaderModel(
            version="0.3.0",
            model="nvidia/nemotron-3.5-lightning:free",
            repo="C:/Users/pavan/Desktop/projects/coding-harness",
            mode="daily",
            task_id="fix-abc123def456",
            layout=shell,
            status="running",
        )
        fit = tui.fit_header(header, "running")

        rendered = {
            "header": fit.columns,
            "composer hints": len(tui.contextual_hints(shell)),
            "command hints": len(commands.contextual_command_hints(width=width)),
            "statusline": len(
                design.fit_statusline(
                    {"queue": 3, "density": "compact", "sidebar": "auto"}, width
                ).text
            ),
            "sidebar heading": len(
                design.SidebarSection("mcp", "MCP", ("a", "b", "c")).heading
            ),
            "announcement": len(a11y.announce_run_started("fix-abc123def456", "daily")),
            "legend": max(len(line) for line in a11y.legend_lines()),
        }

        overflowing = {name: size for name, size in rendered.items() if size > width}
        assert not overflowing, (
            f"at {width} columns these rows are wider than the terminal: "
            f"{overflowing}"
        )

    def test_split_and_vertical_terminals_collapse_the_rails_rather_than_squeezing_them(
        self,
    ):
        """A split or vertical terminal loses the rails, it does not crush them.

        Squeezing a 42-column rail into 8 columns produces an unreadable rail;
        collapsing it produces a readable transcript.  The layout authority
        chooses collapse, and this pins that choice at the two shapes the brief
        names.
        """
        split_width, split_height = SPLIT_VIEWPORT
        split = design.resolve_layout(split_width, split_height, sidebar="show")
        assert design.split_terminal(split_width) is True
        assert split.split is True
        assert split.sidebar_cols == 0
        assert split.context_cols == 0
        assert split.out_of_bounds() == ()

        vert_width, vert_height = VERTICAL_VIEWPORT
        vertical = design.resolve_layout(vert_width, vert_height, sidebar="show")
        assert design.vertical_terminal(vert_width, vert_height) is True
        assert vertical.vertical is True
        assert vertical.sidebar_cols == 0
        assert vertical.context_cols == 0
        assert vertical.out_of_bounds() == ()

    def test_the_transcript_keeps_a_usable_width_at_every_viewport(self):
        """The conversation is the product; it must never be squeezed to nothing.

        Measured values, not a guessed constant, so a change to the rail
        arithmetic shows up as a diff in this test rather than as a surprise on
        a 120-column screen.

        The transcript is legitimately much narrower at 120 and 200 than at 100
        in ``show`` mode: both rails are pinned open, which is what ``show``
        means.  That is a declared trade, not a squeeze, and the rail columns
        are asserted against the authority's own arithmetic so the trade is
        visible rather than inferred.
        """
        measured = {}
        for width in VIEWPORT_WIDTHS:
            spec = design.resolve_layout(width, VIEWPORT_HEIGHT, sidebar="show")
            transcript = spec.region("transcript")
            measured[width] = transcript.width

            assert transcript.width > 0, f"the transcript vanished at {width}"
            assert transcript.width <= width, f"the transcript exceeds {width}"
            # The sidebar is exactly the declared width, and the content
            # arithmetic is the authority's, not a restatement of it:
            # content is the viewport less the sidebar and the gutter, less
            # the context rail's own columns, because the two rails do not
            # overlap.
            assert spec.sidebar_cols == (design.SIDEBAR_WIDTH if spec.sidebar_shown else 0)
            assert spec.content_cols == (
                design.content_width(width, spec.sidebar_shown) - spec.context_cols
            )
            assert spec.context_cols <= design.CONTEXT_MAX_WIDTH

        assert measured == {60: 60, 80: 80, 100: 58, 120: 48, 200: 124}, (
            f"transcript widths moved: {measured}"
        )

    def test_the_reflow_slope_changes_only_at_a_declared_breakpoint(self):
        """A layout jump is an UNEXPLAINED kink, not a rail appearing.

        The transcript is a proportional region, so its width changes on every
        column and a "does it change" test would be vacuous.  What must not
        happen is the rate of change moving at a width no constant declares:
        that is content the reader is looking at reflowing for no stated
        reason, and it is invisible in a screenshot.

        So this asserts the layout is piecewise-linear with kinks only at
        declared breakpoints, in both sidebar modes, over 200 columns.
        """
        declared = {
            design.SPLIT_MIN_COLUMNS,
            design.PLAN_MIN_COLUMNS,
            design.SIDEBAR_BREAKPOINT,
            design.SIDEBAR_BREAKPOINT + 1,
            design.CONTEXT_MIN_COLUMNS,
            design.ULTRAWIDE_COLUMNS,
        }
        # A rail taking columns is a two-column settle: the breakpoint column
        # and the one after it.
        allowed = declared | {value + 1 for value in declared} | {value - 1 for value in declared}

        measured = {}
        for mode in ("auto", "show"):
            widths = list(range(40, 241))
            transcript = {
                width: design.resolve_layout(width, VIEWPORT_HEIGHT, sidebar=mode)
                .region("transcript")
                .width
                for width in widths
            }
            slope = {
                widths[index]: transcript[widths[index]] - transcript[widths[index - 1]]
                for index in range(1, len(widths))
            }
            kinks = sorted(
                {
                    widths[index]
                    for index in range(2, len(widths))
                    if slope[widths[index]] != slope[widths[index - 1]]
                }
            )
            measured[mode] = kinks
            undeclared = [kink for kink in kinks if kink not in allowed]
            assert not undeclared, (
                f"the {mode} layout's reflow rate changes at undeclared widths: "
                f"{undeclared} (declared: {sorted(declared)})"
            )

        assert measured["auto"] == [120, 121, 122, 160, 161], (
            f"the auto layout's kinks moved: {measured['auto']}"
        )
        assert measured["show"] == [96, 97, 120, 121, 160, 161], (
            f"the show layout's kinks moved: {measured['show']}"
        )

    def test_a_rail_never_appears_and_then_disappears_as_the_terminal_grows(self):
        """A rail that comes and goes is the layout jumping under the reader.

        Both rails are monotone in width under ``auto``: once one has room it
        keeps it.  Asserted column by column over 200 columns rather than at
        the five sampled widths, because a rail that flickers between two
        sampled widths would pass a sampled test.
        """
        widths = list(range(40, 241))
        predicates = (
            ("sidebar", lambda width: design.sidebar_visible(width, VIEWPORT_HEIGHT, "auto")),
            ("context", lambda width: design.context_visible(width, VIEWPORT_HEIGHT)),
        )
        for name, predicate in predicates:
            flags = [predicate(width) for width in widths]
            on = [index for index, flag in enumerate(flags) if flag]
            assert on, f"the {name} rail is never visible in auto mode"
            assert all(flags[index:] for index in on), (
                f"the {name} rail appeared, disappeared, and reappeared as the "
                "terminal grew"
            )

    def test_the_sidebar_appears_at_exactly_its_measured_breakpoint(self):
        """The declared 120-column auto breakpoint, pinned to the column.

        Terminal 01 measured "visible at 121, hidden at 120".  A breakpoint
        that drifts by a column is invisible in a screenshot and obvious to a
        person resizing a window.
        """
        flags = [design.sidebar_visible(w, VIEWPORT_HEIGHT, "auto") for w in range(100, 141)]

        assert design.SIDEBAR_BREAKPOINT == 120
        assert flags[design.SIDEBAR_BREAKPOINT - 100] is False, (
            "the sidebar is visible at the declared breakpoint, not above it"
        )
        assert flags[design.SIDEBAR_BREAKPOINT + 1 - 100] is True, (
            "the sidebar is not visible one column above the breakpoint"
        )
        assert flags.index(True) + 100 == design.SIDEBAR_BREAKPOINT + 1

    def test_the_density_changes_rows_without_moving_a_region_off_screen(self):
        """Compact density spends rows; it must not spend a region off the edge."""
        for width in VIEWPORT_WIDTHS:
            comfortable = design.resolve_layout(
                width, VIEWPORT_HEIGHT, density="comfortable"
            )
            compact = design.resolve_layout(width, VIEWPORT_HEIGHT, density="compact")
            assert comfortable.out_of_bounds() == ()
            assert compact.out_of_bounds() == ()
            assert compact.composer_height < comfortable.composer_height, (
                f"compact density did not spend a composer row at {width}"
            )


class TestASectionWithTwoEntriesIsNotRendered:
    """The anti-clutter rule, obeyed in every panel this round drove."""

    @pytest.mark.parametrize("count", (0, 1, 2))
    def test_a_section_with_at_most_two_entries_is_not_rendered(self, count):
        """Two or fewer entries publishes nothing at all -- not a heading.

        A heading with one row under it costs more space than the row and
        teaches the reader nothing.
        """
        assert design.section_is_rendered(count) is False
        assert design.section_is_collapsible(count) is False
        section = design.SidebarSection("mcp", "MCP", tuple(f"e{i}" for i in range(count)))
        assert section.rendered is False
        assert section.indicator == ""

    @pytest.mark.parametrize("count", (3, 4, 9))
    def test_a_section_with_three_or_more_entries_is_rendered(self, count):
        section = design.SidebarSection(
            "mcp", "MCP", tuple(f"e{i}" for i in range(count))
        )

        assert section.rendered is True
        assert section.heading.endswith("MCP")
        assert section.indicator == design.TRIANGLE_EXPANDED

    def test_the_threshold_is_one_number_across_every_surface(self):
        """Three means three everywhere, read from the authority.

        The rule was duplicated into ``cli.toggles`` and ``cli.review``; if any
        of them drifted, a section that the sidebar hides would appear in the
        review panel and the reader would see two different answers to "how
        many things do I need for this to be worth a heading".
        """
        from cli import review, toggles

        assert toggles.MIN_SECTION_ENTRIES == design.ANTI_CLUTTER_MIN_ENTRIES
        assert tui.MIN_SECTION_ENTRIES == design.ANTI_CLUTTER_MIN_ENTRIES
        assert review.MIN_REVIEW_ROWS == design.ANTI_CLUTTER_MIN_ENTRIES
        assert design.ANTI_CLUTTER_MIN_ENTRIES == 3

    def test_the_statusline_renders_nothing_when_it_has_nothing_true_to_say(self):
        """A zero count produces NO section, and an absent value costs no row.

        ``StatusSection.render(0)`` returning ``""`` is the declared contract
        and the load-bearing half: the count is the CALLER's decision, and
        ``fit_statusline`` documents that it receives already-rendered text.
        So a zero reaches the fitter as an absent key, never as the string
        ``"0"``.  Asserting the fitter re-derives truthiness from a raw count
        would be asserting a second vocabulary that does not exist.
        """
        section = design.StatusSection("queue", "queued", 0, "ctrl+g")

        assert section.render(0) == ""
        assert section.render(None) == ""
        assert section.render(3) == "3 queued (ctrl+g)"

        assert design.fit_statusline({}, 120).text == ""
        assert design.fit_statusline({"queue": ""}, 120).text == ""
        assert design.fit_statusline({"queue": ""}, 120).kept == ()
        assert design.fit_statusline({"density": "compact"}, 120).text == "compact"

    def test_the_statusline_costs_nothing_when_silent_and_rows_when_not(self):
        """The statusline's row count is an INPUT to the layout, not a cost.

        This is the anti-clutter rule applied to a whole region rather than a
        section: an idle shell must not carry a blank band, and a shell with
        three true facts must.
        """
        silent = design.resolve_layout(120, VIEWPORT_HEIGHT, statusline_rows=0)
        speaking = design.resolve_layout(120, VIEWPORT_HEIGHT, statusline_rows=1)

        assert silent.region("statusline").height == 0
        assert silent.out_of_bounds() == ()
        assert speaking.region("statusline").height == 1
        assert speaking.out_of_bounds() == ()


# ===========================================================================
# 3. Legible without colour
# ===========================================================================


class TestEverySurfaceIsLegibleWithoutHue:
    """Colour off and ``TERM=dumb`` across the surfaces Prompts 01-05 added."""

    def test_every_state_is_distinguishable_with_no_colour_at_all(self):
        """No state may be readable only as a hue.

        ``NO_COLOR`` removes hue entirely, so the ``(marker, label)`` pair is
        the only channel left.  All 15 must be unique, and ``verified`` /
        ``unverified`` must NOT collapse -- they share a hue token by design,
        so a shared marker would make "verified" and "not verified" the same
        character.
        """
        tokens = pf_capability_free_theme()

        pairs = {
            (pf_marker(name), pf_label(name)): name for name in pf_state_names()
        }
        assert len(pairs) == len(pf_state_names()) == 15, (
            "two states are identical without hue"
        )
        assert tokens.depth.value == "none"
        assert tokens.color_enabled is False
        assert pairs[("✔", "verified")] == "verified"
        assert pairs[("◇", "unverified")] == "unverified"

    def test_the_channel_report_says_hue_is_off_rather_than_claiming_it(self):
        """A receipt that reports hue as ON with no hue is a lie a caller acts on.

        ``channel_report()`` is what a renderer asks before it decides whether
        it needs a second channel, so a wrong answer here propagates.

        The collision COUNT is deliberately not asserted to be zero:
        ``cli/theme.py`` gates ``hue_collisions`` on ``hue_on`` but computes
        ``hue_collision_count`` unconditionally, so with hue off the report
        carries an empty list beside a non-zero count.  That is a real
        inconsistency in another terminal's file; the honest assertion is that
        the LIST is empty -- nothing is being painted a colour -- and the
        finding is filed rather than papered over.
        """
        from cli import theme

        tokens = theme.resolve_theme(depth=theme.ColorDepth.NONE, env={"NO_COLOR": "1"})
        report = theme.channel_report(tokens)

        assert report["hue"] is False
        assert report["hue_collisions"] == []
        assert report["marker"] is True
        assert report["text"] is True
        assert tokens.color_enabled is False
        # A caller that asks "would anything collide if I had hue?" still gets
        # a usable answer, which is why the count is computed at all.
        assert report["hue_collision_count"] >= 0

    def test_a_dumb_terminal_degrades_to_text_rather_than_to_nothing(self):
        """``TERM=dumb`` is a refusal, so the marker channel must fall all the
        way through to the label and still say something.

        Measured on this host: the ASCII marker is skipped too, because the
        encoding probe is also refused, so the LABEL is what survives.  That is
        verbose but it is legible, and ``state_marker`` promising a non-empty
        string is the contract.
        """
        from cli import theme

        for name in ("verified", "unverified", "error", "success", "approval"):
            with_dumb = _with_term_dumb(name)
            assert with_dumb, f"{name} rendered nothing under TERM=dumb"
            assert with_dumb.strip(), name
            # Whatever form it takes, it must contain the state's own words or
            # a marker a reader can map -- never an empty or blank cell.
            assert with_dumb != theme.state_label(name)[:0]

    def test_prompt_01_surfaces_render_with_colour_off(self, monkeypatch):
        """The layout authority's own surfaces: statusline, sidebar, footer."""
        monkeypatch.setenv("NO_COLOR", "1")
        from cli import ui

        tokens = ui.active_tokens()
        assert tokens.color_enabled in (True, False)  # import-time, not our concern

        statusline = design.fit_statusline(
            {"queue": 3, "density": "compact"}, 120
        )
        section = design.SidebarSection("mcp", "MCP", ("a", "b", "c"))
        footer = design.sidebar_footer("C:/Users/pavan/repo", "0.3.0")

        assert statusline.text, "the statusline must say something without hue"
        assert section.heading, "the sidebar heading must survive without hue"
        assert footer.lines, "the sidebar footer must survive without hue"
        assert design.ANTI_CLUTTER_EXEMPT, "the exemptions must be declared"

    def test_prompt_02_and_03_surfaces_render_with_colour_off(self, monkeypatch):
        """``/connect`` and the model picker, both of which carry provider data.

        These are the two surfaces where the content is a provider name or a
        model id, i.e. data a user typed into a config file.  A hostile one must
        be escaped rather than eaten -- the failure mode being a message that
        silently disappears, which is worse than a loud error.
        """
        monkeypatch.setenv("NO_COLOR", "1")

        assert auth.render_provider_menu(), "the provider menu rendered nothing"
        assert auth.render_status(), "the auth status rendered nothing"
        assert auth.status_line(), "the auth status line was empty"

        catalog = models.model_catalog({"model": "gpt-4o"})
        picker = models.ModelPicker(catalog, current_model="gpt-4o")
        assert picker.lines(), "the model picker rendered nothing"
        assert picker.lines()[0] == "model picker"

    def test_prompt_04_and_05_surfaces_render_with_colour_off(self, monkeypatch):
        """Role hierarchy and the diff review, with and without a path that
        contains a bracket.

        A repository path may contain ``[``.  Rich escapes only the OPENING
        bracket, so the characters remain but a line carrying an unbalanced
        ``[/]`` raises.  The property that matters is not "the bracket is
        gone" -- it is that the text is still VISIBLE and the render did not
        raise, which is what a render failure that deletes a message looks
        like.
        """
        monkeypatch.setenv("NO_COLOR", "1")

        for hostile in ("[bold red]evil[/bold red]", "cli/we[i]rd.py", "[/]"):
            escaped = models.escape_lines([hostile])
            assert hostile.replace("[", "\\[") in escaped[0] or hostile in escaped[0]
            safe = models.safe_lines([hostile])
            assert safe[0].plain == hostile, "a render failure must not delete text"
            assert auth.markup_safe(hostile)

        assert a11y.announce_finished("completed_unverified", verified=False), (
            "an unverified finish must still announce"
        )
        assert "not verified" in a11y.announce_finished(
            "completed_unverified", verified=False
        )
        assert review.resolve_diff_style("auto", 80)[0] == "stacked"
        assert review.resolve_diff_style("auto", 120)[0] == "split"

    def test_an_unverified_result_is_never_dressed_as_verified_without_hue(self):
        """The honesty rule must hold with the colour layer entirely gone.

        With hue there is at least a tint to distinguish; without it the only
        thing left is the word.  If the word can be wrong, no-colour is where
        it shows.

        The substring check is done on a WORD BOUNDARY because the status word
        ``completed_unverified`` contains ``verified`` as a substring -- a naive
        containment assertion here would either fail on correct behaviour or
        (worse) be written as ``"verified" in text`` and taught to be
        satisfied by the unverified case.
        """
        import re

        unverified = a11y.announce_finished("completed_unverified", verified=False)
        verified = a11y.announce_finished("completed_verified", verified=True)

        assert "not verified" in unverified
        bare = re.sub(r"\bnot verified\b", "", unverified)
        assert not re.search(r"\bverified\b", bare), (
            f"an unverified result still says 'verified': {unverified!r}"
        )
        assert re.search(r"\bverified\b", verified)
        assert verified != unverified
        assert a11y.announce_finished("completed", verified=False) != verified, (
            "a bare completion word must never read as verified"
        )

    def test_a_role_token_with_a_character_outside_the_rgx_charset_reaches_the_parser(
        self,
    ):
        """MEASURED DEFECT, owner: ``cli/tui.py``.

        ``_m()`` is the guard that keeps a ``[vex.*]`` role from reaching
        Textual's markup parser, and it maps an unmapped role to ``none``.  But
        ``_ROLE_RE``'s character class is ``[a-z0-9.]`` -- it excludes the
        hyphen -- so a role-shaped token containing one is not matched at all,
        passes through ``_m`` unchanged, and Textual then raises
        ``MarkupError: auto closing tag ('[/]') has nothing to close``: the
        exact crash the guard was written to prevent, reached through a
        different door.

        The transcript seam is defended (it falls back to escaped plain text,
        so no message is deleted -- that property is asserted below and holds).
        The other ``_m`` call sites update widgets directly and have no such
        fallback.  The numbers are counted from the source so the scope of the
        request is a measurement, not an impression.
        """
        from textual.content import Content

        from cli.tui import _ROLE_MAP, _ROLE_RE, _m

        # The guard works for a role name inside the regex charset.
        assert _m("[vex.notarealrole]text[/]") == "[none]text[/]"
        assert _m("[none]text[/]") == "[none]text[/]"

        # A hyphen falls outside it, and the tag survives verbatim.
        assert not _ROLE_RE.search("[vex.some-role]"), (
            "the defect this test pins has been fixed: _ROLE_RE now matches "
            "hyphenated roles and this assertion must be replaced with a "
            "positive check that _m maps one to 'none'"
        )
        assert _m("[vex.some-role]text[/]") == "[vex.some-role]text[/]"
        with pytest.raises(Exception, match="nothing to close"):
            Content.from_markup(_m("[vex.some-role]text[/]"))

        # Scope: one of the _m call sites has a rendering fallback, the rest do not.
        source = (REPO_ROOT / "cli" / "tui.py").read_text(encoding="utf-8")
        call_sites = len(re.findall(r"(?<!def )\b_m\(", source))
        assert call_sites >= 19, (
            f"expected at least 19 _m call sites, found {call_sites}; the call "
            "sites moved and this scope figure is stale"
        )
        assert len(_ROLE_MAP) >= 20, "the role map shrank; re-measure the scope"

    def test_a_role_token_that_reaches_the_parser_never_deletes_the_message(
        self, tmp_path
    ):
        """The property that matters most, and it HOLDS today.

        Even for the token shape that ``_m`` does not map, ``VexApp.transcript``
        catches the render failure and writes the escaped text instead.  A
        render failure that DELETES a message is the failure mode this project
        treats as unrecoverable, so the defence is asserted here even though the
        triggering token is a separate finding.
        """
        from rich.markup import escape
        from rich.text import Text
        from textual.content import Content

        hostile = "[vex.some-role]the answer the model gave[/]"

        with pytest.raises(Exception):
            Content.from_markup(hostile)  # the unguarded path does fail

        # The guarded path: escape and the text survives, fully.
        recovered = str(Content.from_markup(escape(hostile)))
        assert "the answer the model gave" in recovered
        assert Text(escape(hostile)).plain == hostile.replace("[", "\\[")


# ===========================================================================
# 4. A pipe carries no control bytes
# ===========================================================================


def _child_env(extra: dict | None = None) -> dict:
    """Return a child environment isolated from the developer's real one.

    ``HARNESS_HOME`` / ``HARNESS_LOGS_DIR`` are redirected so a test run never
    reads or writes the machine's real session store, and no provider key is
    forwarded, so a surface that tried to call out would fail loudly rather
    than quietly succeed against a live account.
    """
    import tempfile

    scratch = tempfile.mkdtemp(prefix="vex-pf07-pipe-")
    env = dict(os.environ)
    env.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "VEX_HOME": scratch,
            "HARNESS_HOME": scratch,
            "HARNESS_LOGS_DIR": os.path.join(scratch, "logs"),
            "VEX_NO_RELEASE_NOTICE": "1",
        }
    )
    for key in ("VEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        env.pop(key, None)
    if extra:
        env.update(extra)
    return env


def _run_cli(*argv: str, env: dict | None = None, timeout: int = 240):
    """Run ``python -m cli`` as a real child with a real pipe on stdout."""
    return subprocess.run(
        [sys.executable, "-m", "cli", *argv],
        capture_output=True,
        text=True,
        env=env or _child_env(),
        timeout=timeout,
        cwd=str(REPO_ROOT),
    )


def _assert_byte_clean(label: str, output: str) -> None:
    """Fail with the offending offsets named, not just the count."""
    escapes = ANSI_ESCAPE.findall(output)
    controls = CONTROL_BYTES.findall(output)
    assert not escapes, f"{label}: {len(escapes)} raw ANSI escape(s): {escapes[:3]}"
    assert not controls, (
        f"{label}: {len(controls)} C0 control byte(s): "
        f"{sorted({ord(c) for c in controls})}"
    )
    assert BELL not in output, f"{label}: a terminal bell reached a pipe"


class TestAPipeCarriesNoControlBytes:
    """Real child processes, byte-counted. A pipe is not a terminal."""

    def test_the_version_surface_through_a_real_pipe_is_byte_clean(self):
        result = _run_cli("--version")

        assert result.returncode == 0
        _assert_byte_clean("--version", result.stdout)
        _assert_byte_clean("--version (stderr)", result.stderr)
        assert result.stdout.strip().startswith("vex")

    def test_the_help_surface_through_a_real_pipe_is_byte_clean(self):
        """``--help`` is the first thing a CI job or a script ever runs."""
        result = _run_cli("--help")

        assert result.returncode == 0
        _assert_byte_clean("--help", result.stdout)
        assert "usage" in result.stdout.lower()

    def test_a_json_surface_through_a_real_pipe_parses_and_is_byte_clean(self):
        """A ``--json`` document that carries an escape is not a document."""
        result = _run_cli("capabilities", "--json")

        assert result.returncode == 0
        _assert_byte_clean("capabilities --json", result.stdout)
        document = json.loads(result.stdout)
        assert isinstance(document, dict)
        assert document, "an empty --json document is not a receipt"

    def test_a_refusing_json_surface_still_carries_no_control_bytes(self):
        """A refusal is the case most likely to carry an exception's own bytes.

        A traceback on stdout is both a control-byte problem and a leak; the
        exit code is the machine contract and it must not be 0 for an unknown
        command, because ``/help`` exiting 0 must not read as a verification
        nobody ran.
        """
        result = _run_cli("run", "/definitely-not-a-command", "--json")

        assert result.returncode != 0
        _assert_byte_clean("unknown command --json", result.stdout)
        document = json.loads(result.stdout)
        assert document.get("exit_code") not in (0, None)
        assert "definitely-not-a-command" in result.stdout

    def test_a_hostile_command_name_survives_into_the_json_document(self):
        """Hostile input must be carried, not eaten and not executed.

        The document has to keep the text the user typed -- that is what makes
        it diagnosable -- while carrying no control byte.  Both halves are the
        requirement; either alone is a bug.
        """
        result = _run_cli("run", "/[bold red]x", "--json")

        _assert_byte_clean("hostile --json", result.stdout)
        document = json.loads(result.stdout)
        assert "[bold" in str(document.get("command", "")) or "[bold" in str(
            document.get("args", "")
        )

    def test_no_color_and_dumb_terminal_produce_the_same_clean_bytes(self):
        """``NO_COLOR`` and ``TERM=dumb`` must not change the BYTES a pipe gets.

        Measured, not assumed: both child environments are byte-counted and
        compared, because a colour-stripping path that leaves a stray escape
        is the classic version of this bug.
        """
        plain = _run_cli("--version", env=_child_env({"NO_COLOR": "1"}))
        dumb = _run_cli("--version", env=_child_env({"TERM": "dumb", "NO_COLOR": "1"}))

        _assert_byte_clean("NO_COLOR --version", plain.stdout)
        _assert_byte_clean("TERM=dumb --version", dumb.stdout)
        assert plain.stdout == dumb.stdout, (
            "the two environments produced different bytes for the same answer"
        )

    def test_a_pipe_does_not_get_a_bell_even_while_a_run_finishes(self):
        """A ``\\a`` byte in a pipe is literal garbage in whatever reads it.

        The bell is TTY-gated in the product; this pins that a real child
        process with a real pipe on stdout produces none, which is the only
        way to prove the gate is on the STREAM and not on a flag.
        """
        result = _run_cli("run", "/definitely-not-a-command")

        _assert_byte_clean("non-tty run", result.stdout)
        _assert_byte_clean("non-tty run (stderr)", result.stderr)


# ---------------------------------------------------------------------------
# Small indirections so the theme assertions read as the contract they check.
# ---------------------------------------------------------------------------


def pf_capability_free_theme():
    """Return a token set resolved with hue entirely unavailable."""
    from cli import theme

    return theme.resolve_theme(depth=theme.ColorDepth.NONE, env={"NO_COLOR": "1"})


def pf_marker(name: str) -> str:
    """Return the non-colour marker for a state."""
    from cli import theme

    return theme.state_marker(name)


def pf_label(name: str) -> str:
    """Return the text label for a state."""
    from cli import theme

    return theme.state_label(name)


def pf_state_names() -> tuple:
    """Return every state that carries a non-colour channel."""
    from cli import theme

    return theme.state_names()


def _with_term_dumb(state: str) -> str:
    """Return a state's marker as rendered with ``TERM=dumb`` set."""
    from cli import theme

    previous = os.environ.get("TERM")
    os.environ["TERM"] = "dumb"
    try:
        return theme.state_marker(state)
    finally:
        if previous is None:
            os.environ.pop("TERM", None)
        else:
            os.environ["TERM"] = previous

# ===========================================================================
# VEX-PF-08 -- multi-instance, offline, and long sessions
#
# APPENDED, not merged. VEX-PF-07 owns the Windows / no-colour parity content
# above; this section is VEX-PF-08's. Every helper below is prefixed
# ``_pf08_`` so neither round can collide with the other's names, and the
# section marker is the seam: whoever lands second APPENDS.
#
# Prompt 08: "Stop the ways a long or interrupted session loses the user's
# work or hangs." Four behaviours, one class each, one test per behaviour,
# named after the behaviour rather than the function.
#
# No model, no Docker, no network, and no mocked filesystem. The two real
# processes are this interpreter: one holds a repository guard in its OWN
# process, and one hard-kills itself mid-run so nothing is unwound.
# ===========================================================================

import time  # noqa: E402  (local to this appended section)

from cli import session as _pf08_session  # noqa: E402
from shared import availability as _pf08_avail  # noqa: E402
from shared import instance_guard as _pf08_guard  # noqa: E402

_PF08_KILL_DRIVER = Path(__file__).resolve().parent / "kernel_kill_driver.py"
_PF08_LOCK_DRIVER = Path(__file__).resolve().parent / "platform_lock_driver.py"


@pytest.fixture(autouse=True)
def _pf08_isolated_env(tmp_path, monkeypatch):
    """Isolated home, harness home, and a THROWAWAY lock directory.

    ``VEX_HOME`` is pointed at this test's own ``tmp_path`` so the instance
    guard never writes into the developer's real ``~/.vex``. It is set
    separately from ``HARNESS_HOME`` on purpose: the guard reads it first, and
    a test that pointed only ``HARNESS_HOME`` would let one failed run leave a
    real lock behind on a real machine.
    """
    home = tmp_path / "pf08-home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE" if os.name == "nt" else "HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    hhome = tmp_path / "pf08-harness-home"
    hhome.mkdir()
    monkeypatch.setenv("HARNESS_HOME", str(hhome))
    monkeypatch.setenv("HARNESS_DECISIONS_DB", str(hhome / "decisions.db"))
    monkeypatch.setenv("VEX_HOME", str(tmp_path / "pf08-vex-home"))
    monkeypatch.delenv(_pf08_avail.OFFLINE_ENV, raising=False)
    yield tmp_path


def _pf08_repo(tmp_path: Path, name: str = "repo") -> Path:
    """A real repository directory with real files under ``tmp_path``."""
    repo = tmp_path / name
    repo.mkdir(exist_ok=True)
    (repo / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    (repo / "sub").mkdir(exist_ok=True)
    (repo / "sub" / "b.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    return repo


def _pf08_live_peer(tmp_path: Path, repo: Path, *, hold_s: float = 60.0):
    """Start a REAL second process that holds the guard for ``repo``.

    A child process, not a same-process lease. ``acquire_repository_lock`` is
    deliberately re-entrant per process, so a same-process "peer" is not a
    peer: a test colliding with one would prove only that the guard does not
    fire on its own author, which is the bug this section exists to catch. The
    child answers the operating system's liveness question, so the guard is
    measured against the real thing rather than against a stub.
    """
    if not _PF08_LOCK_DRIVER.is_file():
        pytest.skip(f"the peer driver is missing: {_PF08_LOCK_DRIVER}")
    ready = tmp_path / f"pf08-peer-{repo.name}.ready"
    child = subprocess.Popen(
        [
            sys.executable,
            str(_PF08_LOCK_DRIVER),
            str(repo),
            str(tmp_path / "pf08-vex-home"),
            str(ready),
            str(hold_s),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if ready.is_file():
            return child
        if child.poll() is not None:
            _out, err = child.communicate()
            pytest.fail(f"the peer driver exited before holding the guard: {err}")
        time.sleep(0.05)
    child.kill()
    pytest.fail("the peer driver never reported that it held the guard")


# == multi-instance ===


class TestTwoInstancesOnOneRepository:
    """A second writer on one work tree must be detected and refused."""

    def test_a_free_repository_reports_free_and_creates_nothing(self, tmp_path):
        repo = _pf08_repo(tmp_path)
        report = _pf08_guard.instance_guard_report(repo)
        assert report["state"] == "free"
        assert report["free"] is True
        assert report["refuse"] is False
        assert not Path(report["lock_path"]).exists()

    def test_the_second_instance_is_refused_and_names_the_first(self, tmp_path):
        repo = _pf08_repo(tmp_path)
        peer = _pf08_live_peer(tmp_path, repo)
        try:
            with pytest.raises(_pf08_guard.ConcurrentInstanceError) as raised:
                with _pf08_session.open_session(tmp_path / "logs", repo, command="vex (second)"):
                    pytest.fail("the second instance was not refused")
            text = "\n".join(raised.value.lines())
            assert str(peer.pid) in text
            assert "vex (a second session)" in text
            assert "sess-peer001" in text
            # The refusal must not have started a conversation.
            assert not (tmp_path / "logs" / "_conversations").exists()
        finally:
            peer.kill()
            peer.wait(timeout=30)

    def test_the_guard_lives_outside_the_work_tree_so_it_cannot_dirty_it(self, tmp_path):
        repo = _pf08_repo(tmp_path)
        with _pf08_session.open_session(tmp_path / "logs", repo) as session:
            lock_path = Path(session["instance_guard"]["lock_path"])
            assert lock_path.exists() is True
        assert lock_path.exists() is False  # released on the way out
        assert repo not in lock_path.parents
        assert "locks" in lock_path.parts
        # Nothing the guard does may create a file inside the repository.
        assert sorted(item.name for item in repo.iterdir()) == ["a.py", "sub"]

    def test_a_crashed_owner_is_taken_over_without_asking_a_human(self, tmp_path):
        repo = _pf08_repo(tmp_path)
        peer = _pf08_live_peer(tmp_path, repo)
        dead = peer.pid
        peer.kill()
        peer.wait(timeout=30)
        assert _pf08_guard.pid_alive(dead) is False
        with _pf08_session.open_session(tmp_path / "logs", repo) as session:
            assert session["instance_guard"]["held"] is True

    def test_an_unreadable_lock_is_refused_and_never_stolen_silently(self, tmp_path):
        repo = _pf08_repo(tmp_path)
        peer = _pf08_live_peer(tmp_path, repo)
        ready = json.loads((tmp_path / f"pf08-peer-{repo.name}.ready").read_text(encoding="utf-8"))
        Path(ready["lock_path"]).write_text("this is not a lock record", encoding="utf-8")
        try:
            info = _pf08_guard.probe_repository_lock(repo)
            assert info.state == "held_unreadable"
            assert info.takeable is False
            with pytest.raises(_pf08_guard.ConcurrentInstanceError):
                with _pf08_session.open_session(tmp_path / "logs", repo):
                    pytest.fail("an unreadable lock was stolen")
        finally:
            peer.kill()
            peer.wait(timeout=30)

    def test_re_entering_in_one_process_is_allowed_and_only_the_outer_exit_frees_it(
        self, tmp_path
    ):
        repo = _pf08_repo(tmp_path)
        with _pf08_session.open_session(tmp_path / "logs", repo) as outer:
            assert outer["instance_guard"]["reentrant"] is False
            with _pf08_session.open_session(tmp_path / "logs", repo) as inner:
                assert inner["instance_guard"]["reentrant"] is True
            # The inner block released its depth, not the lock.
            assert Path(outer["instance_guard"]["lock_path"]).exists() is True
        assert Path(outer["instance_guard"]["lock_path"]).exists() is False

    def test_a_listing_still_works_while_another_instance_holds_the_repository(self, tmp_path):
        """The guard gates the WRITER. ``load_or_create`` is also the reader
        behind ``/sessions`` and ``resume_session``, and a listing must not
        fail because someone else is working."""
        repo = _pf08_repo(tmp_path)
        peer = _pf08_live_peer(tmp_path, repo)
        try:
            session = _pf08_session.load_or_create(tmp_path / "logs", repo, "sess-read001")
            assert session["session_id"] == "sess-read001"
        finally:
            peer.kill()
            peer.wait(timeout=30)

    def test_warn_mode_records_the_conflict_instead_of_refusing(self, tmp_path):
        repo = _pf08_repo(tmp_path)
        peer = _pf08_live_peer(tmp_path, repo)
        try:
            with _pf08_session.open_session(
                tmp_path / "logs", repo, config={"session_instance_guard": "warn"}
            ) as session:
                assert session["instance_guard"]["held"] is False
                conflict = session["instance_guard"]["conflict"]
                assert conflict["error"] == "concurrent_instance"
                assert any("'warn'" in line for line in session["instance_guard"]["lines"])
        finally:
            peer.kill()
            peer.wait(timeout=30)

    def test_the_refusal_is_plain_text_and_survives_a_real_rich_console(self, tmp_path):
        """A repository path may contain ``[``. A line that carries one into
        Textual's markup parser deletes the message instead of printing it, so
        the property under test is that every line still renders, from a
        directory whose own name is hostile."""
        from rich.console import Console

        repo = tmp_path / "weird[name]"
        repo.mkdir()
        (repo / "weird[name].py").write_text("x = 1\n", encoding="utf-8")
        peer = _pf08_live_peer(tmp_path, repo)
        with open(os.devnull, "w", encoding="utf-8") as sink:
            console = Console(width=100, file=sink)
            try:
                with pytest.raises(_pf08_guard.ConcurrentInstanceError) as raised:
                    with _pf08_session.open_session(tmp_path / "logs", repo):
                        pytest.fail("the refusal never fired")
                lines = raised.value.lines()
                assert lines
                for line in lines:
                    console.print(line, markup=True)
                # Nothing is invented and nothing carries a markup delimiter
                # the renderer had to escape: the lines are plain.
                assert not any("[" in line and "]" in line for line in lines)
            finally:
                peer.kill()
                peer.wait(timeout=30)

    def test_the_state_vocabulary_is_closed(self):
        assert "free" in _pf08_guard.INSTANCE_STATES
        assert "held_unreadable" in _pf08_guard.INSTANCE_STATES
        # Every state other than "free" means "not writable by a second writer".
        for state in _pf08_guard.INSTANCE_STATES:
            info = _pf08_guard.InstanceInfo(state=state)
            assert (not info.is_free) or state == "free"

    def test_liveness_is_answerable_and_an_unanswerable_question_is_not_a_yes(self):
        """``pid_alive`` returns ``None`` when it cannot tell. ``None`` must
        never become "the holder is gone", because that is permission to steal
        a repository somebody is using."""
        assert _pf08_guard.pid_alive(0) is None
        assert _pf08_guard.pid_alive(-1) is None
        assert _pf08_guard.pid_alive("not a pid") is None
        assert _pf08_guard.pid_alive(os.getpid()) is True
        assert _pf08_guard.pid_alive(0x7FFFFFFF) is False


# == offline ===


class TestOfflineDegradesHonestly:
    """No network must say so, stay bounded, and never fake a result."""

    def test_an_offline_run_names_what_is_unavailable_and_why(self):
        availability = _pf08_avail.availability_for(
            "https://docs.python.org/3/library/os.html", config={"offline": True}, kind="web fetch"
        )
        assert availability.available is False
        assert availability.category == "offline"
        assert "offline" in availability.sentence
        assert "web fetch" in availability.sentence
        assert availability.local_alternatives

    def test_a_policy_denial_is_a_policy_answer_and_not_a_network_one(self):
        availability = _pf08_avail.availability_for(
            "https://not-on-the-allowlist.invalid/x", config={}
        )
        assert availability.available is False
        assert availability.category == "policy"
        assert "allowlist" in availability.sentence

    def test_a_blocked_call_is_never_a_result(self):
        availability = _pf08_avail.availability_for(
            "https://example.com", config={"offline": True}
        )
        assert availability.is_result is False
        with pytest.raises(_pf08_avail.BlockedCallReportedAsResult) as raised:
            _pf08_avail.require_available(availability)
        assert raised.value.availability.category == "offline"
        assert raised.value.as_dict()["error"] == "blocked_call_reported_as_result"

    def test_a_result_is_returned_unchanged_when_the_call_is_allowed(self):
        availability = _pf08_avail.availability_for("https://example.com", config={})
        assert availability.available is True
        assert availability.is_result is True
        assert _pf08_avail.require_available(availability) is availability

    def test_the_dial_is_bounded_for_every_value_a_caller_can_supply(self):
        for value in (None, 0, -1, float("nan"), float("inf"), "not a number", object()):
            deadline = _pf08_avail.dial_deadline_s({"net_dial_deadline_s": value})
            assert _pf08_avail.MIN_DIAL_DEADLINE_S <= deadline <= _pf08_avail.MAX_DIAL_DEADLINE_S
            assert deadline == deadline  # not NaN

    def test_offline_is_read_by_key_presence_not_truthiness(self):
        # Absent means the operator never said; that is not the same as False.
        assert _pf08_avail.offline_requested({})[0] is False
        assert _pf08_avail.offline_requested({"offline": False})[0] is False
        assert _pf08_avail.offline_requested({"offline": True})[0] is True
        assert _pf08_avail.offline_requested({"offline": "yes"})[0] is True
        # A typo must not silently keep the network on, and must not kill it
        # either: the source label says the value was unusable.
        offline, source = _pf08_avail.offline_requested({"offline": "maybe"})
        assert offline is False
        assert "unusable" in source

    def test_the_offline_sentence_is_empty_when_the_run_is_online(self):
        assert _pf08_avail.offline_sentence({}) == ""
        assert "offline" in _pf08_avail.offline_sentence({"offline": True})

    def test_the_unavailability_lines_name_an_offline_usable_alternative(self):
        availability = _pf08_avail.availability_for(
            "https://docs.python.org/3/", config={"offline": True}
        )
        lines = _pf08_avail.unavailable_lines(availability)
        assert len(lines) >= 2
        assert "available instead" in lines[1]

    def test_an_unknown_category_cannot_be_constructed(self):
        with pytest.raises(ValueError):
            _pf08_avail.Availability(available=False, category="gremlins")

    def test_the_sandbox_receipt_separates_a_bridge_from_usable_network(self):
        from execution import sandbox

        networkless = sandbox.sandbox_network_availability(False)
        assert networkless["container_network"] is False
        assert networkless["available"] is False
        assert "by design" in networkless["detail"]

        offline_but_bridged = sandbox.sandbox_network_availability(
            True, config={"offline": True}, declared_hosts=("pypi.org",)
        )
        assert offline_but_bridged["container_network"] is True
        assert offline_but_bridged["available"] is False
        assert offline_but_bridged["category"] == "offline"
        assert offline_but_bridged["declared_hosts"] == ["pypi.org"]

    def test_no_configuration_default_was_added_for_this_flow(self):
        """A value in ``DEFAULTS`` merges into every task and every eval arm.
        These keys are opt-in by presence, so none of them may appear there."""
        from harness import config as harness_config

        forbidden = (
            "offline",
            "no_network",
            "airgap",
            "net_dial_deadline_s",
            "session_instance_guard",
        )
        present = [key for key in forbidden if key in harness_config.DEFAULTS]
        assert present == []


# == long sessions ==


class TestSixtyTurnSessionStaysCoherent:
    """A 60-turn session must stay coherent, priced, and readable."""

    @staticmethod
    def _sixty_turn_session(tmp_path, repo):
        session = _pf08_session.load_or_create(tmp_path / "logs", repo, "sess-long0001")
        for index in range(60):
            session_mod_turn = f"question {index} about module_{index % 5}"
            session_mod_reply = f"answer {index} about module_{index % 5}"
            _pf08_session.append_turn(session, "user", session_mod_turn)
            _pf08_session.append_turn(session, "assistant", session_mod_reply)
        _pf08_session.compact_session(session, tmp_path / "logs", keep_last=12)
        return session

    def test_sixty_turns_survive_compaction_with_every_turn_still_retrievable(self, tmp_path):
        repo = _pf08_repo(tmp_path)
        session = self._sixty_turn_session(tmp_path, repo)
        assert len(session["raw_turns"]) == 120
        assert session["turns"], "compaction must leave recent turns active"
        assert session["compacted_turns"], "compaction must retain what it summarised"
        assert len(session["turns"]) + len(session["compacted_turns"]) == 120
        retrieved = _pf08_session.retrieve_session_turns(session, query="", limit=200)
        assert len(retrieved) == 120

    def test_the_pulse_keeps_cost_and_context_visible_and_honest(self, tmp_path):
        repo = _pf08_repo(tmp_path)
        session = self._sixty_turn_session(tmp_path, repo)
        pulse = _pf08_session.session_pulse(
            session,
            config={"context_window_tokens": 32000},
            log_root=tmp_path / "logs",
            task_id="none",
        )
        assert pulse["readable"] is True
        assert pulse["conversation"]["turns_total"] == 120
        assert pulse["conversation"]["compactions"] >= 1
        assert pulse["context"]["window"] == 32000
        assert pulse["context"]["used"] > 0
        assert 0 < pulse["context"]["fraction"] < 1
        # No priced call exists, so the cost is UNKNOWN -- never zero.
        assert pulse["cost"]["usd"] is None
        assert pulse["cost"]["priced"] is False
        assert any("unknown rather than zero" in gap for gap in pulse["gaps"])

    def test_a_context_window_nobody_configured_is_a_gap_not_a_default(self, tmp_path):
        repo = _pf08_repo(tmp_path)
        session = self._sixty_turn_session(tmp_path, repo)
        pulse = _pf08_session.session_pulse(session, config={}, log_root=tmp_path / "logs")
        assert pulse["context"]["window"] is None
        assert pulse["context"]["source"] == "absent"
        assert any("context_window_tokens" in gap for gap in pulse["gaps"])

    def test_the_pulse_never_renders_a_zero_for_something_it_could_not_measure(self, tmp_path):
        repo = _pf08_repo(tmp_path)
        session = self._sixty_turn_session(tmp_path, repo)
        lines = _pf08_session.session_pulse_lines(
            _pf08_session.session_pulse(session, config={}, log_root=tmp_path / "logs")
        )
        rendered = "\n".join(lines)
        assert "$0.0000" not in rendered
        assert "unknown" in rendered
        assert "window not configured" in rendered

    def test_a_section_of_one_or_two_entries_does_not_earn_a_heading(self):
        """The anti-clutter rule, applied to a real pulse and measured."""
        pulse = {
            "schema_version": 1,
            "readable": True,
            "session_id": "sess-x",
            "conversation": {
                "turns_total": 1,
                "turns_active": 1,
                "turns_compacted": 0,
                "compactions": 0,
                "session_id": "sess-x",
            },
            "context": {"window": None, "used": 0, "fraction": None},
            "cost": {"usd": None, "priced": False, "calls": None, "tokens": None},
            "pressure": "fresh",
            "attention": ["only one thing needs you"],
            "gaps": ["only one thing could not be measured"],
        }
        lines = _pf08_session.session_pulse_lines(pulse)
        threshold = _pf08_session._min_section_entries()
        assert len(pulse["attention"]) < threshold
        assert not any(line.strip().startswith("attention (") for line in lines)
        assert not any(line.strip().startswith("gaps (") for line in lines)
        assert any("only one thing needs you" in line for line in lines)

        pulse["gaps"] = ["gap one", "gap two", "gap three"]
        assert len(pulse["gaps"]) >= threshold
        lines = _pf08_session.session_pulse_lines(pulse)
        assert any(line.strip().startswith("gaps (3):") for line in lines)

    def test_the_exempt_sections_are_declared_rather_than_inferred(self):
        assert "session" in _pf08_session.PULSE_ANTI_CLUTTER_EXEMPT
        assert "spend" in _pf08_session.PULSE_ANTI_CLUTTER_EXEMPT
        assert "attention" not in _pf08_session.PULSE_ANTI_CLUTTER_EXEMPT
        assert "gaps" not in _pf08_session.PULSE_ANTI_CLUTTER_EXEMPT
        assert set(_pf08_session.PULSE_SECTIONS) == {"session", "spend", "attention", "gaps"}

    def test_a_sixty_turn_transcript_is_segmented_and_the_omission_is_stated(self, tmp_path):
        repo = _pf08_repo(tmp_path)
        session = self._sixty_turn_session(tmp_path, repo)
        segments = _pf08_session.transcript_segments(session, max_segments=4)
        assert len(segments) == 5  # four kept plus the omission marker
        assert "omitted" in segments[-1]
        lines = _pf08_session.transcript_segment_lines(segments)
        assert any("not shown" in line for line in lines)

    def test_a_repeated_run_is_collapsed_but_a_single_turn_is_not(self):
        session = {
            "raw_turns": [
                {"role": "user", "text": "run the tests", "turn_id": "t1"},
                {"role": "user", "text": "run the tests", "turn_id": "t2"},
                {"role": "user", "text": "run the tests", "turn_id": "t3"},
                {"role": "assistant", "text": "all green", "turn_id": "t4"},
            ]
        }
        segments = _pf08_session.transcript_segments(session)
        assert segments[0]["count"] == 3
        assert segments[0]["repeated"] is True
        assert segments[1]["count"] == 1
        assert segments[1]["repeated"] is False
        lines = _pf08_session.transcript_segment_lines(segments)
        assert lines[0].startswith("user x3:")
        assert lines[1].startswith("assistant:")

    def test_the_session_pulse_is_a_projection_and_writes_nothing(self, tmp_path):
        repo = _pf08_repo(tmp_path)
        session = self._sixty_turn_session(tmp_path, repo)
        before = {key: repr(session.get(key)) for key in sorted(session)}
        _pf08_session.session_pulse(
            session, config={"context_window_tokens": 8000}, log_root=tmp_path / "logs"
        )
        after = {key: repr(session.get(key)) for key in sorted(session)}
        assert before == after


# == kill and restart ==


class TestKillAndRestartResumesHonestly:
    """A real hard kill, then an exact account of what survived."""

    @staticmethod
    def _kill_a_run(repo: Path, log_root: Path, run_id: str, replies: list) -> int:
        if not _PF08_KILL_DRIVER.is_file():
            pytest.skip(f"the kill driver is missing: {_PF08_KILL_DRIVER}")
        completed = subprocess.run(
            [
                sys.executable,
                str(_PF08_KILL_DRIVER),
                str(repo),
                str(log_root),
                run_id,
                "2",
                json.dumps(replies),
            ],
            capture_output=True,
            text=True,
            timeout=240,
        )
        return completed.returncode

    def test_a_hard_kill_survives_what_it_flushed_and_the_report_says_so(self, tmp_path):
        repo = _pf08_repo(tmp_path)
        log_root = tmp_path / "logs"
        returncode = self._kill_a_run(
            repo,
            log_root,
            "kill-run",
            [
                json.dumps({"tool": "write", "path": "notes.md", "content": "pre-kill fact\n"}),
                json.dumps({"tool": "read", "path": "notes.md"}),
            ],
        )
        assert returncode == 70, "the driver must hard-kill itself, not exit cleanly"
        assert (repo / "notes.md").read_text(encoding="utf-8") == "pre-kill fact\n"

        report = _pf08_session.session_survival_report(log_root, "kill-run", repo=repo)
        assert report["turns_durable"] >= 1
        assert report["turn_numbers"] == list(range(1, report["turns_durable"] + 1))
        assert report["looked_finished"] is False
        assert report["changed_files"], "the run recorded a file and it must be reported"
        assert report["files_lost"] == []
        # A verdict that could not explain a gap must not call the resume
        # clean, and a clean resume must have no gaps at all.
        assert (report["verdict"] == "clean_resume") == (report["resumable"] and not report["gaps"])

    def test_a_run_that_recorded_a_terminal_event_is_not_resumable(self, tmp_path):
        log_root = tmp_path / "logs"
        task_dir = log_root / "done-run"
        task_dir.mkdir(parents=True)
        (task_dir / "trace.jsonl").write_text(
            json.dumps({"event": "task_start", "sequence": 1})
            + "\n"
            + json.dumps({"event": "task_end", "sequence": 2})
            + "\n",
            encoding="utf-8",
        )
        (task_dir / "turns.jsonl").write_text(
            json.dumps({"turn": 1, "changed_files": []}) + "\n", encoding="utf-8"
        )
        report = _pf08_session.session_survival_report(log_root, "done-run")
        assert report["looked_finished"] is True
        assert report["resumable"] is False
        assert report["verdict"] == "not_resumable"
        assert any("no checkpoint" in gap for gap in report["gaps"])

    def test_a_file_the_run_changed_but_which_is_gone_is_reported_not_hidden(self, tmp_path):
        log_root = tmp_path / "logs"
        task_dir = log_root / "lost-run"
        task_dir.mkdir(parents=True)
        (task_dir / "turns.jsonl").write_text(
            json.dumps({"turn": 1, "changed_files": ["gone.py"]}) + "\n", encoding="utf-8"
        )
        report = _pf08_session.session_survival_report(log_root, "lost-run", repo=tmp_path)
        assert report["files_lost"] == ["gone.py"]
        assert any("no longer on disk" in gap for gap in report["gaps"])
        assert "gone.py" in "\n".join(report["gaps"])

    def test_a_gap_in_the_turn_ledger_is_reported_rather_than_smoothed(self, tmp_path):
        log_root = tmp_path / "logs"
        task_dir = log_root / "gap-run"
        task_dir.mkdir(parents=True)
        (task_dir / "turns.jsonl").write_text(
            json.dumps({"turn": 1}) + "\n" + json.dumps({"turn": 3}) + "\n", encoding="utf-8"
        )
        report = _pf08_session.session_survival_report(log_root, "gap-run")
        assert report["turns_missing"] == [2]
        assert any("skips turn" in gap for gap in report["gaps"])
        assert report["verdict"] != "clean_resume"

    def test_a_nothing_found_run_is_its_own_verdict(self, tmp_path):
        report = _pf08_session.session_survival_report(tmp_path / "logs", "never-ran")
        assert report["verdict"] == "nothing_found"
        assert report["resumable"] is False
        assert report["gaps"]

    def test_a_torn_final_journal_line_does_not_stop_the_reader(self, tmp_path):
        """A hard kill can land mid-write. A reader that refuses to read past
        a half line cannot answer the question it exists to answer."""
        log_root = tmp_path / "logs"
        task_dir = log_root / "torn-run"
        task_dir.mkdir(parents=True)
        (task_dir / "turns.jsonl").write_text(
            json.dumps({"turn": 1})
            + "\n"
            + json.dumps({"turn": 2})
            + "\n"
            + '{"turn": 3, "tool_ca',  # no closing brace, no newline
            encoding="utf-8",
        )
        report = _pf08_session.session_survival_report(log_root, "torn-run")
        assert report["turn_numbers"] == [1, 2]
        assert report["turns_durable"] == 2

    def test_the_survival_receipt_renders_every_gap(self, tmp_path):
        log_root = tmp_path / "logs"
        task_dir = log_root / "done-run"
        task_dir.mkdir(parents=True)
        (task_dir / "trace.jsonl").write_text(
            json.dumps({"event": "task_end", "sequence": 7}) + "\n", encoding="utf-8"
        )
        report = _pf08_session.session_survival_report(log_root, "done-run")
        report["lines"] = _pf08_session.session_survival_lines(report)
        rendered = "\n".join(report["lines"])
        assert report["gaps"], "the fixture must produce at least one gap"
        for gap in report["gaps"]:
            assert gap in rendered, "a dropped gap is the worst receipt this module could emit"
        assert "looks finished: yes" in rendered
        assert "resumable: no" in rendered

    def test_the_resume_contract_is_not_weakened_by_this_report(self, tmp_path):
        """The report is a READER. It must not write, repair, quarantine, or
        resume anything, and the files it read must be byte-identical after."""
        repo = _pf08_repo(tmp_path)
        log_root = tmp_path / "logs"
        returncode = self._kill_a_run(
            repo,
            log_root,
            "readonly-run",
            [
                json.dumps({"tool": "write", "path": "keep.md", "content": "unchanged\n"}),
                json.dumps({"tool": "read", "path": "keep.md"}),
            ],
        )
        assert returncode == 70
        task_dir = log_root / "readonly-run"
        before = {path.name: path.read_bytes() for path in sorted(task_dir.iterdir()) if path.is_file()}
        _pf08_session.session_survival_report(log_root, "readonly-run", repo=repo)
        after = {path.name: path.read_bytes() for path in sorted(task_dir.iterdir()) if path.is_file()}
        assert before == after


# == handoff discipline ==


class TestWhatThisRoundDidAndDidNotWire:
    """The state of the mount, pinned so it cannot be claimed as live by
    accident. A dead guard is worse than an absent one because it reads as
    protection that is not there."""

    def test_the_shells_now_hold_the_single_writer_guard(self):
        """FLIPPED INVERTED PIN — T4/W1 (2026-10-02).

        This test used to assert ``"session.open_session(" not in source``
        for both shells, and its own docstring said: *"When Prompt 01 wires
        it, this test is the one to update -- and it must be updated in the
        same change, not deleted."* It is updated in the same change.

        The state it used to record was true and it was a **trust hole**:
        ``cli.session.open_session`` is a unit-proven single-writer guard
        and nothing in the product called it, so two ``vex`` instances on
        one repository were not refused, and two agents mutating one
        worktree is how work is lost.

        The assertion is INVERTED rather than deleted, and deliberately so.
        An inverted pin that is deleted stops pinning; one that is inverted
        now fails if the mount is ever removed — which is the whole reason
        the construct exists. The comment on each side is kept because a
        future reader who finds only the new text cannot tell that there was
        once a period in which the answer was "no".

        Where the mount lives, and why it is not uniform:

        * ``cli/headless.py::_resolve_session`` goes through
          ``cli.session.open_session`` — it needs a conversation BY ID and a
          lease, which is exactly that function.
        * ``cli/tui.py`` takes ``session_instance_guard`` for the read side
          and ``shared.instance_guard.acquire_repository_lock`` for the
          hold, because a TUI loads ``load_latest_session`` (the newest
          resumable conversation) and calling ``open_session`` to get the
          lease would load the WRONG one. Neither half is re-implemented:
          both are the primitives ``open_session`` itself uses.

        Both are asserted here, so a future round cannot quietly drop one.
        """
        import ast

        import cli.interactive as interactive_mod
        import cli.tui as tui_mod

        headless = Path(interactive_mod.__file__).parent / "headless.py"
        headless_source = headless.read_text(encoding="utf-8")
        assert "open_session" in headless_source, (
            "cli/headless.py no longer takes the single-writer guard; a "
            "headless turn is as much a writer as a TUI one"
        )
        assert "load_or_create(log_root, repo, session_id=session_id)" not in (
            headless_source
        ), "the headless path went back to the UNGUARDED loader"

        tui_source = Path(tui_mod.__file__).read_text(encoding="utf-8")
        assert "acquire_repository_lock" in tui_source, (
            "cli/tui.py no longer takes a real lease; two vex instances on one "
            "repository would not be refused"
        )
        assert "ConcurrentInstanceError" in tui_source, (
            "cli/tui.py no longer handles a refusal, so a busy repository "
            "raises out of on_mount instead of showing an answer"
        )

        # And the refusal is a SCREEN, not a crash: asserted by finding the
        # handler rather than by counting call sites, so a refactor that
        # moves it cannot empty the check.
        tree = ast.parse(tui_source)
        methods = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        assert "_render_refusal" in methods, (
            "the TUI refuses a busy repository without a screen to show it on"
        )

    def test_the_session_guard_reader_is_not_a_writer(self):
        """``session_instance_guard`` is a reader, and a reader must not take."""
        import inspect

        signature = inspect.signature(_pf08_session.session_instance_guard)
        assert "command" not in signature.parameters
