"""Platform parity primitives: the filesystem hazards a daily run can hit.

A Windows user and a POSIX user run the SAME daily path, and four things
differ between them in ways that are silent rather than loud:

1. **Reserved device names.** ``NUL``, ``CON``, ``COM1`` and their siblings are
   not ordinary filenames.  A write to one can report success, create no
   directory entry, and read back empty -- so a tool that says "wrote NUL" and
   a reader that says "no such file" are both telling the truth.  The set was
   duplicated in three modules at two different sizes; it is declared ONCE
   here and :func:`is_reserved_device_name` is the one test.

2. **Newline style.** ``Path.read_text()`` applies universal newlines, so a
   CRLF file reads back as ``\\n``; ``Path.write_text()`` then re-emits
   ``os.linesep``.  A pure-CRLF file survives that round trip, and a **mixed**
   file does not -- it is silently homogenised.  :func:`read_preserving` and
   :func:`write_preserving` keep whatever was there, and
   :func:`newline_report` is the receipt that says which case a file is.

3. **Path identity and containment.** ``str(Path("C:/A")) == str(Path("c:/a"))``
   is ``False`` on Windows and the two names are the same file, so identity
   and containment both have to be case-folded and drive-letter aware.
   ``Path("/etc/hosts").is_absolute()`` is ``False`` on Windows, which is how a
   leading-slash absolute path gets mistaken for a repository-relative one.

4. **Capability.** Long paths, case-insensitive filesystems and the
   liveness of a reserved device are properties of the MACHINE, not of this
   source file.  :func:`capability_report` probes them on a real temporary
   tree and returns what it measured, so nothing has to assume.

Every function here is total: it never raises for hostile input, and where it
refuses it returns a sentence rather than an exception, because a refusal a
caller cannot render is a refusal that gets swallowed.

This module is CLI- and harness-agnostic and has no dependency on either.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any, Optional, Sequence, Union

__all__ = [
    "NEWLINE_STYLES",
    "RESERVED_NAME_REFUSAL",
    "WINDOWS_RESERVED_NAMES",
    "CapabilityReport",
    "NewlineStyle",
    "capability_report",
    "detect_newline",
    "first_reserved_segment",
    "is_absolute_anywhere",
    "is_reserved_device_name",
    "newline_report",
    "normalize_relative",
    "path_equivalent",
    "path_is_under",
    "probe_case_insensitive",
    "probe_long_path_support",
    "probe_reserved_device_liveness",
    "read_preserving",
    "reserved_device_evidence",
    "reserved_name_refusal",
    "write_preserving",
]

# ---------------------------------------------------------------------------
# 1. Reserved device names
# ---------------------------------------------------------------------------

#: The complete Windows reserved-device set, extension-insensitive.  ``COM1``
#: through ``COM9`` and ``LPT1`` through ``LPT9`` are the real set; ``COM0``
#: and ``LPT0`` are NOT reserved and are deliberately absent, because a
#: validator that over-refuses teaches people to work around the validator.
WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)

#: The one sentence a refusal is rendered from.  It names the CONCRETE
#: consequence rather than restating the rule, because "reserved name" tells a
#: person nothing about why their file vanished.
RESERVED_NAME_REFUSAL = (
    "{name!r} is a reserved Windows device name, not a file: writing it can "
    "report success, create no file, and read back empty. Rename it."
)


def is_reserved_device_name(segment: Any) -> bool:
    """Return whether one path segment is a Windows reserved device name.

    Judged on the STEM, so ``nul.txt`` and ``CON`` are both reserved while
    ``console.py`` and ``COM0`` are not.  Trailing dots and spaces are stripped
    first because Windows silently discards them, which makes ``nul.`` a
    spelling of ``nul``.  Case-folded, because the device namespace is.
    Never raises: a non-string is reported as not reserved.
    """
    text = str(segment if segment is not None else "")
    if not text:
        return False
    stem = text.rstrip(". ").split(".", 1)[0]
    if not stem:
        return False
    return stem.casefold() in {name.casefold() for name in WINDOWS_RESERVED_NAMES}


def reserved_name_refusal(segment: Any) -> str:
    """Return the refusal sentence for a reserved device name, else ``""``."""
    if not is_reserved_device_name(segment):
        return ""
    return RESERVED_NAME_REFUSAL.format(name=str(segment))


def first_reserved_segment(path: Any) -> str:
    """Return the first reserved device name in a path, or ``""``.

    Checks EVERY component, not just the leaf, because a reserved name in a
    directory position is exactly as unroutable as one in a filename position
    and a validator that only looks at the leaf misses it.
    """
    if path is None:
        return ""
    try:
        parts = PurePath(str(path)).parts
    except (TypeError, ValueError):
        parts = [str(path)]
    for part in parts:
        # A drive anchor is "C:"; it is not a device name.
        if part.endswith(":") and len(part) == 2:
            continue
        if is_reserved_device_name(part):
            return part
    return ""


# ---------------------------------------------------------------------------
# 2. Newline style
# ---------------------------------------------------------------------------

NEWLINE_STYLES = ("crlf", "lf", "mixed", "none")

PathLike = Union[str, "os.PathLike[str]"]


@dataclass(frozen=True)
class NewlineStyle:
    """The measured line-ending shape of a byte string.

    ``style`` is one of :data:`NEWLINE_STYLES`.  ``crlf`` and ``mixed`` are the
    two cases that a plain text-mode round trip damages differently: ``crlf``
    is usually preserved by accident, ``mixed`` is always destroyed.  A caller
    deciding whether it needs :func:`write_preserving` should branch on
    ``style == "mixed"``, not on "is this Windows".
    """

    style: str
    crlf: int = 0
    lf: int = 0
    bare_cr: int = 0
    ends_with_newline: bool = False

    @property
    def preserves_under_text_round_trip(self) -> bool:
        """Return whether ``read_text``/``write_text`` keeps this file intact.

        ``lf`` and ``none`` survive everywhere.  ``crlf`` survives on a host
        whose ``os.linesep`` is CRLF and is DESTROYED on a host whose is not --
        which is why the honest answer is a property of the pair, and why the
        test suite measures it rather than asserting it.
        """
        if self.style in {"lf", "none"}:
            return True
        if self.style == "crlf":
            return os.linesep == "\r\n"
        return False

    @property
    def needs_preserving_write(self) -> bool:
        """Return whether a read-modify-write must use the preserving path."""
        return self.style == "mixed"

    def to_dict(self) -> dict:
        """Return a JSON-friendly receipt."""
        return {
            "style": self.style,
            "crlf": self.crlf,
            "lf": self.lf,
            "bare_cr": self.bare_cr,
            "ends_with_newline": self.ends_with_newline,
            "needs_preserving_write": self.needs_preserving_write,
        }


def detect_newline(data: Any) -> NewlineStyle:
    """Measure the line-ending shape of ``data`` without decoding it.

    Takes BYTES, because that is the only level at which the question is
    answerable: ``\\r\\n`` and ``\\n`` are indistinguishable after universal
    newline translation.  A lone ``\\r`` (classic Mac) is counted separately
    rather than folded into either, so a file with one is not reported as
    clean.  Never raises.
    """
    if isinstance(data, str):
        data = data.encode("utf-8", errors="replace")
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return NewlineStyle("none")
    raw = bytes(data)
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    bare_cr = raw.count(b"\r") - crlf
    if crlf and lf == 0 and bare_cr == 0:
        style = "crlf"
    elif crlf == 0 and lf and bare_cr == 0:
        style = "lf"
    elif crlf == 0 and lf == 0 and bare_cr == 0:
        style = "none"
    else:
        style = "mixed"
    return NewlineStyle(
        style=style,
        crlf=crlf,
        lf=lf,
        bare_cr=bare_cr,
        ends_with_newline=raw.endswith((b"\n", b"\r")),
    )


def read_preserving(path: PathLike, *, encoding: str = "utf-8") -> str:
    """Read text with line endings left EXACTLY as they are on disk.

    This is the counterpart to :func:`write_preserving`.  The combination
    round-trips a mixed-newline file byte-for-byte, which
    ``read_text``/``write_text`` cannot do.
    """
    with open(path, "r", encoding=encoding, newline="") as handle:
        return handle.read()


def write_preserving(
    path: PathLike,
    text: Any,
    *,
    encoding: str = "utf-8",
    style: Optional[str] = None,
) -> NewlineStyle:
    """Write ``text`` without translating its line endings, and report the shape.

    When ``style`` is ``None`` the shape is measured from ``text`` itself, so a
    caller that read a CRLF file and edited one line writes back CRLF without
    having to remember to pass anything.  Returns the :class:`NewlineStyle`
    actually written, which is a receipt rather than a promise: a caller can
    assert the file it produced is the file it intended.
    """
    body = text if isinstance(text, str) else str(text if text is not None else "")
    measured = detect_newline(body)
    wanted = (style or measured.style).lower()
    if wanted == "crlf":
        payload = _to_crlf(body)
    elif wanted == "lf" or wanted == "none":
        payload = body.replace("\r\n", "\n").replace("\r", "\n")
    else:  # "mixed" and anything unrecognised: leave the text alone
        payload = body
    with open(path, "w", encoding=encoding, newline="") as handle:
        handle.write(payload)
    return detect_newline(payload)


def _to_crlf(text: str) -> str:
    """Return ``text`` with every line ending as CRLF."""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


def newline_report(path: PathLike, *, limit: Optional[int] = None) -> dict:
    """Return a receipt describing a real file's newline shape.

    Reads at most ``limit`` bytes when given, so this is safe to point at a
    large source file.  A missing or unreadable path is reported as
    ``unavailable`` with a reason rather than raised, so a receipt is always
    renderable.
    """
    target = Path(path)
    if not target.is_file():
        return {"available": False, "path": str(target), "reason": "not a file"}
    try:
        with open(target, "rb") as handle:
            data = handle.read() if limit is None else handle.read(int(limit))
    except OSError as exc:
        return {"available": False, "path": str(target), "reason": str(exc)}
    style = detect_newline(data)
    receipt = {"available": True, "path": str(target)}
    receipt.update(style.to_dict())
    return receipt


# ---------------------------------------------------------------------------
# 3. Path identity and containment
# ---------------------------------------------------------------------------


def normalize_relative(path: Any) -> str:
    """Return a portable, comparable spelling of a path.

    Backslashes become forward slashes, a trailing separator is dropped, and
    the whole thing is case-folded, so two spellings of one Windows path
    compare equal and one spelling cannot smuggle a case difference into an
    equality check.  Never raises.
    """
    try:
        text = str(path if path is not None else "")
    except Exception:  # pragma: no cover - str() of a hostile __str__
        return ""
    if not text:
        return ""
    # normcase FIRST, then the separator swap: on Windows normcase rewrites
    # "/" back to "\", so doing the swap first produces a string whose prefix
    # test silently never matches.  This ordering is load-bearing and was found
    # by measuring, not by reading.
    text = os.path.normcase(text).replace("\\", "/")
    while len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text


def path_equivalent(left: Any, right: Any) -> bool:
    """Return whether two paths name the same location on THIS host.

    Uses ``os.path.normcase`` rather than a hand-rolled fold because the answer
    is a property of the filesystem: on Windows ``C:/A`` and ``c:/a`` are one
    file, on POSIX they are two.  Falls back to the samefile probe when both
    names exist, so a symlink or a junction is still recognised as one target.
    Never raises.
    """
    first, second = Path(str(left)), Path(str(right))
    if os.path.normcase(str(first)) == os.path.normcase(str(second)):
        return True
    try:
        return first.exists() and second.exists() and os.path.samefile(first, second)
    except (OSError, ValueError):
        return False


def is_absolute_anywhere(path: Any) -> bool:
    """Return whether a path is absolute on EITHER POSIX or Windows.

    ``PurePosixPath("/etc/hosts").is_absolute()`` is ``True`` and
    ``PureWindowsPath("/etc/hosts").is_absolute()`` is also ``True``, but
    ``Path("/etc/hosts").is_absolute()`` on Windows is **False** -- and
    ``PureWindowsPath("C:/x").is_absolute()`` is ``True`` where
    ``PurePosixPath("C:/x").is_absolute()`` is ``False``.  A containment check
    written against the host's own ``Path`` therefore mis-classifies the other
    platform's absolute paths, and a repository-relative check then treats an
    absolute path as relative.  This asks both grammars and refuses when
    either says yes.
    """
    try:
        text = str(path if path is not None else "")
    except Exception:  # pragma: no cover
        return False
    if not text:
        return False
    windows_absolute = PurePath(text).is_absolute()
    try:
        from pathlib import PurePosixPath, PureWindowsPath

        windows_absolute = PureWindowsPath(text).is_absolute()
        posix_absolute = PurePosixPath(text).is_absolute()
    except (ImportError, ValueError, TypeError):  # pragma: no cover
        return bool(windows_absolute)
    return bool(windows_absolute or posix_absolute)


def path_is_under(child: Any, parent: Any, *, strict: bool = True) -> bool:
    """Return whether ``child`` is contained by ``parent``, on any platform.

    Case-folded and separator-normalised, so containment cannot be escaped by
    changing the case of a path segment on Windows -- a trick that defeats a
    plain ``relative_to`` because ``relative_to`` is case-sensitive even where
    the filesystem is not.  Both sides are made absolute first, because
    ``relative_to`` against a relative base answers a different question.
    A drive mismatch is a refusal, not a ``ValueError``: ``C:/a`` is never
    under ``D:/``.
    """
    try:
        child_text = _absolute_text(child)
        parent_text = _absolute_text(parent)
    except (OSError, ValueError):
        return False
    if not child_text or not parent_text:
        return False
    normalized_parent = normalize_relative(parent_text)
    normalized_child = normalize_relative(child_text)
    if _drive_of(normalized_child) != _drive_of(normalized_parent):
        return False
    if normalized_child == normalized_parent:
        return not strict
    return normalized_child.startswith(normalized_parent.rstrip("/") + "/")


def _absolute_text(path: Any) -> str:
    """Return an absolute string form of ``path`` without raising."""
    try:
        return str(Path(str(path)).expanduser().absolute())
    except (OSError, ValueError, RuntimeError):
        return str(path if path is not None else "")


def _drive_of(normalized: str) -> str:
    """Return the drive/anchor token of a normalized path, or ``""``."""
    head = normalized[:2]
    return head if len(head) == 2 and head[1] == ":" else ""


# ---------------------------------------------------------------------------
# 4. Measured capability
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityReport:
    """What this MACHINE measured about its own filesystem, not what we assume.

    Every field is the result of a real probe against a real temporary tree.
    ``available`` is ``False`` when a probe could not run, and the matching
    ``*_reason`` says why -- a probe that silently reported ``False`` would be
    indistinguishable from a machine that genuinely lacks the capability.
    """

    long_paths: bool = False
    long_path_depth: int = 0
    long_path_reason: str = ""
    case_insensitive: bool = False
    case_insensitive_reason: str = ""
    reserved_device_silent_loss: bool = False
    reserved_device_reason: str = ""
    linesep: str = "\n"
    os_name: str = ""
    platform: str = ""
    reserved_device_evidence: str = ""
    probes: tuple = field(default_factory=tuple)

    @property
    def available(self) -> bool:
        """Return whether every probe RAN.

        A ``*_reason`` field is non-empty only when a probe could not produce a
        measurement, never when a probe succeeded and found a hazard -- a
        hazard is the boolean's job.  Conflating the two would report a machine
        that correctly measured "reserved device names lose writes" as a probe
        that failed, which is the opposite of the truth and would hide the one
        finding this module exists to surface.
        """
        return not (
            self.long_path_reason
            or self.case_insensitive_reason
            or self.reserved_device_reason
        )

    def to_dict(self) -> dict:
        """Return a JSON-friendly receipt of the measured capabilities."""
        return {
            "available": self.available,
            "os_name": self.os_name,
            "platform": self.platform,
            "linesep_codepoints": [ord(ch) for ch in self.linesep],
            "long_paths": self.long_paths,
            "long_path_depth": self.long_path_depth,
            "long_path_reason": self.long_path_reason,
            "case_insensitive": self.case_insensitive,
            "case_insensitive_reason": self.case_insensitive_reason,
            "reserved_device_silent_loss": self.reserved_device_silent_loss,
            "reserved_device_reason": self.reserved_device_reason,
            "reserved_device_evidence": self.reserved_device_evidence,
            "probes": list(self.probes),
        }


def probe_long_path_support(
    root: Optional[PathLike] = None, *, target_length: int = 380
) -> tuple[bool, int, str]:
    """Measure whether this host can create a path of ``target_length``.

    Returns ``(supported, achieved_length, reason)``.  The tree is created
    under a real temporary directory and removed afterwards, so the answer is
    a fact about the filesystem rather than an inference from a registry key
    nobody can read the same way twice.
    """
    base = (
        Path(root)
        if root is not None
        else Path(tempfile.mkdtemp(prefix="neo-longpath-"))
    )
    owns = root is None
    deep = base
    made: list[Path] = []
    try:
        # Grow one segment at a time until the full path is at least
        # target_length or the OS refuses.  MAX_PATH is 260, so a host that
        # has the policy off fails somewhere below 260.
        while len(str(deep)) < target_length and len(made) < 24:
            segment = ("d" + "x" * 18)[:20] + str(len(made))
            deep = deep / segment
            deep.mkdir()
            made.append(deep)
        leaf = deep / ("f" + "y" * 40 + ".txt")
        leaf.write_text("long-path-probe", encoding="utf-8")
        achieved = len(str(leaf))
        if not leaf.is_file():
            return False, achieved, "wrote the file but it is not there"
        return True, achieved, ""
    except OSError as exc:
        return False, len(str(deep)), f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        return False, 0, f"{type(exc).__name__}: {exc}"
    finally:
        # Always clean up, scoped or not: a probe that leaves residue in a
        # caller's directory is a probe that has changed the thing it measured,
        # and the tree it just walked is exactly what a snapshot diff would
        # report as a spurious change.
        _remove_tree(base, made, owns)


def _remove_tree(base: Path, made: Sequence[Path], owns: bool) -> None:
    """Remove everything a probe created under ``base``, deepest first."""
    for path in reversed(list(made)):
        try:
            for child in path.iterdir():
                try:
                    child.unlink()
                except OSError:
                    pass
            path.rmdir()
        except OSError:
            pass
    if owns:
        try:
            for child in base.iterdir():
                try:
                    child.unlink()
                except OSError:
                    pass
            base.rmdir()
        except OSError:
            pass


def probe_case_insensitive(
    root: Optional[PathLike] = None,
) -> tuple[bool, str]:
    """Measure whether two spellings of one name reach the same file.

    Writes ``CaseProbe.TXT`` and asks whether ``caseprobe.txt`` sees it.  This
    is the question every path-identity comparison actually depends on, and the
    answer differs by host, so it is measured rather than assumed from
    ``os.name``.
    """
    base = (
        Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="neo-case-"))
    )
    owns = root is None
    upper = base / "CaseProbe.TXT"
    lower = base / "caseprobe.txt"
    try:
        upper.write_text("case-probe", encoding="utf-8")
        if not lower.is_file():
            return False, "two spellings reached different files"
        if lower.read_text(encoding="utf-8") != "case-probe":
            return False, "the second spelling saw different content"
        return True, ""
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        # Unconditional: the probe created these names, so it removes them
        # whether or not the caller owns the directory.
        for path in (upper, lower):
            try:
                path.unlink()
            except OSError:
                pass
        if owns:
            try:
                base.rmdir()
            except OSError:
                pass


def probe_reserved_device_liveness(
    root: Optional[PathLike] = None, *, probe: str = "NUL"
) -> tuple[bool, str]:
    """Measure whether a reserved device name loses a write silently.

    Writes ``probe`` with a known payload, then asks two questions a caller
    actually cares about: is there a directory entry, and does reading the
    path back return the payload.  A host where the write "succeeds", creates
    nothing, and reads back empty is exactly the silent-loss shape, and that is
    what the boolean means.

    The returned string is a PROBE-ERROR reason and is empty when the probe ran
    -- including when it ran and found silent loss.  The evidence for a
    positive finding goes in the caller's receipt via
    :func:`reserved_device_evidence`, so "the probe failed" and "the probe
    worked and here is what it saw" can never be read as the same thing.
    """
    base = (
        Path(root)
        if root is not None
        else Path(tempfile.mkdtemp(prefix="neo-reserved-"))
    )
    owns = root is None
    target = base / probe
    payload = f"reserved-probe-{probe}"
    try:
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(payload)
        has_entry = probe in {entry.name for entry in base.iterdir()}
        try:
            readback = target.read_text(encoding="utf-8")
        except OSError:
            readback = ""
        return (not has_entry) or readback != payload, ""
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        try:
            target.unlink()
        except OSError:
            pass
        if owns:
            try:
                base.rmdir()
            except OSError:
                pass


def reserved_device_evidence(root: PathLike, probe: str = "NUL") -> str:
    """Return what a reserved device name actually did on this host.

    Separated from :func:`probe_reserved_device_liveness` so the boolean stays
    a clean hazard signal and the narrative stays a receipt.  Returns ``""``
    when the probe could not run.
    """
    base = Path(root)
    target = base / probe
    payload = f"reserved-probe-{probe}"
    try:
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(payload)
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    try:
        has_entry = probe in {entry.name for entry in base.iterdir()}
        try:
            readback = target.read_text(encoding="utf-8")
            read_error = ""
        except OSError as exc:
            readback = ""
            read_error = type(exc).__name__
        parts = [
            f"wrote {probe!r} and open() reported success",
            f"directory_entry={has_entry}",
            f"readback={readback!r}",
        ]
        if read_error:
            parts.append(f"read_error={read_error}")
        return "; ".join(parts)
    finally:
        try:
            target.unlink()
        except OSError:
            pass


def capability_report(
    root: Optional[PathLike] = None, *, long_path_target: int = 380
) -> CapabilityReport:
    """Probe every platform capability on a real tree and return what happened.

    The point is that no caller has to guess: a Windows user without long-path
    support and one with it run the same source, and the report tells them
    apart.  ``root`` scopes the probes to an existing real directory (the suite
    passes its ``tmp_path``); omitted, a private temporary tree is used and
    removed.
    """
    long_ok, achieved, long_reason = probe_long_path_support(
        root, target_length=long_path_target
    )
    case_ok, case_reason = probe_case_insensitive(root)
    lost, reserved_reason = probe_reserved_device_liveness(root)
    if lost:
        evidence_root = (
            Path(root)
            if root is not None
            else Path(tempfile.mkdtemp(prefix="neo-resv-"))
        )
        try:
            evidence = reserved_device_evidence(evidence_root)
        finally:
            if root is None:
                try:
                    evidence_root.rmdir()
                except OSError:
                    pass
    else:
        evidence = ""
    return CapabilityReport(
        long_paths=long_ok,
        long_path_depth=achieved,
        long_path_reason=long_reason,
        case_insensitive=case_ok,
        case_insensitive_reason=case_reason,
        reserved_device_silent_loss=lost,
        reserved_device_reason=reserved_reason,
        linesep=os.linesep,
        os_name=os.name,
        platform=sys_platform(),
        reserved_device_evidence=evidence,
        probes=("long_path", "case_insensitive", "reserved_device_liveness"),
    )


def sys_platform() -> str:
    """Return ``sys.platform`` without importing ``sys`` at module scope."""
    import sys

    return sys.platform
