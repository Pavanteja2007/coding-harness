"""T5.W1.3 — an honest diff for a human reviewer.

Every fact a diff renderer hides is a place a user can approve something
they did not review. This module makes the hidden parts explicit and
names the state the diff is about:

* binary files are called **binary**, never rendered as mojibake;
* mode changes (``chmod +x``) are shown;
* renames are shown as renames, not delete+add;
* whitespace-only changes are marked;
* line-ending changes are marked — a CRLF→LF pass is not "every line
  changed";
* truncation states its bound;
* the whole thing carries a digest of the exact tree state it was computed
  against — a **stale** diff is refused by ``assert_fresh``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "MAX_DIFF_CHARS",
    "DiffEntry",
    "HumanDiff",
    "assert_fresh",
    "digest_of_diff",
    "render_human_diff",
]

MAX_DIFF_CHARS = 200_000

# When git describes one path twice, the more specific kind is the true one:
# a mode change is not a modification, and a rename is not a delete plus an add.
_ENTRY_RANK = {
    "mode_changed": 4,
    "renamed": 3,
    "binary": 2,
    "added": 1,
    "deleted": 1,
    "modified": 0,
}


@dataclass(frozen=True)
class DiffEntry:
    path: str
    kind: str  # modified | added | deleted | renamed | mode_changed | binary
    old_mode: str = ""
    new_mode: str = ""
    is_binary: bool = False
    whitespace_only: bool = False
    line_ending_change: bool = False
    additions: int = 0
    deletions: int = 0
    note: str = ""


@dataclass(frozen=True)
class HumanDiff:
    entries: Tuple[DiffEntry, ...] = ()
    text: str = ""
    truncated: bool = False
    truncation_bound: int = MAX_DIFF_CHARS
    digest: str = ""
    head: str = ""  # the tree state this was computed against

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entries": [dataclasses.asdict(e) for e in self.entries],
            "truncated": self.truncated,
            "truncation_bound": self.truncation_bound,
            "digest": self.digest,
            "head": self.head,
            "text": self.text,
        }


def _git(work_dir: str, *args: str) -> str:
    cp = subprocess.run(
        ["git", *args],
        cwd=work_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    return cp.stdout


def _git_bytes(work_dir: str, *args: str) -> bytes:
    """Raw git stdout.

    Needed by the line-ending check and nowhere else. ``_git`` decodes with
    universal newlines, which rewrites CRLF to LF before a caller can count it
    -- so a HEAD blob that is genuinely CRLF is indistinguishable from an LF
    one, and the comparison reports a change that did not happen. **Counting
    bytes is the only way this question has an honest answer.**
    """
    cp = subprocess.run(
        ["git", *args],
        cwd=work_dir,
        capture_output=True,
        timeout=120,
    )
    return cp.stdout


def _is_binary_numstat(added: str, deleted: str) -> bool:
    return added == "-" and deleted == "-"


def digest_of_diff(entries: Tuple[DiffEntry, ...], text: str, head: str) -> str:
    """The digest a caller asserts against: entries + text + the tree state."""
    h = hashlib.sha256()
    for e in entries:
        for part in (
            e.path, e.kind, e.old_mode, e.new_mode, str(e.is_binary),
            str(e.whitespace_only), str(e.line_ending_change),
            str(e.additions), str(e.deletions),
        ):
            h.update(part.encode("utf-8", "replace"))
            h.update(b"\x00")
    h.update(text.encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update(head.encode("utf-8", "replace"))
    return h.hexdigest()


def render_human_diff(
    work_dir: str,
    *,
    paths: Optional[List[str]] = None,
    max_chars: int = MAX_DIFF_CHARS,
) -> HumanDiff:
    """Compute the diff of ``work_dir`` against its own ``HEAD``.

    Works with both a plain diff (uncommitted changes) and, when the repo
    has a pristine baseline commit, the fix diff. Never raises on missing
    pieces: no HEAD yields an empty, digest-stable result."""
    head = ""
    try:
        head = _git(work_dir, "rev-parse", "--verify", "HEAD").strip()
    except Exception:
        head = ""

    try:
        head = _git(work_dir, "rev-parse", "--verify", "HEAD").strip()
    except Exception:
        head = ""

    def _diff(*args: str) -> str:
        # Prefer the diff against HEAD so staged fixes are rendered too.
        if head:
            out = _git(work_dir, "diff", *args, "HEAD")
            if out.strip() or not args:
                return out
        return _git(work_dir, "diff", *args)

    name_status = _diff("--name-status")
    numstat = _diff("--numstat")
    summary = _diff("--summary")
    whitespace = _diff("-w", "--numstat")

    ns_parts = [p for p in name_status.splitlines() if p]
    entries: List[DiffEntry] = []
    for line in summary.splitlines():
        m2 = re.search(r"mode change (\d+) => (\d+) (.+)", line)
        if m2:
            entries.append(DiffEntry(path=m2.group(3).strip(), kind="mode_changed",
                                     old_mode=m2.group(1), new_mode=m2.group(2)))
    ns_iter = iter(ns_parts)
    for status in ns_iter:
        parts = status.split("\t")
        code = parts[0]
        if code.startswith("R") and len(parts) >= 3:
            entries.append(DiffEntry(path=parts[2], kind="renamed", note=f"from {parts[1]}"))
        elif len(parts) >= 2:
            kind = {"A": "added", "D": "deleted"}.get(code, "modified")
            entries.append(DiffEntry(path=parts[1], kind=kind))

    # git reports a mode change twice: once in --summary and once as an
    # ordinary M in --name-status. One path is one entry, and the specific
    # kind wins over the generic one.
    by_path: Dict[str, DiffEntry] = {}
    for e in entries:
        prior = by_path.get(e.path)
        if prior is None:
            by_path[e.path] = e
            continue
        if _ENTRY_RANK.get(e.kind, 0) > _ENTRY_RANK.get(prior.kind, 0):
            by_path[e.path] = dataclasses.replace(
                e, old_mode=e.old_mode or prior.old_mode,
                new_mode=e.new_mode or prior.new_mode,
            )
        else:
            by_path[e.path] = dataclasses.replace(
                prior, old_mode=prior.old_mode or e.old_mode,
                new_mode=prior.new_mode or e.new_mode,
            )
    entries = [by_path[p] for p in sorted(by_path)]

    num_pairs: Dict[str, Tuple[str, str]] = {}
    for block in numstat.splitlines():
        tabs = block.split("\t")
        if len(tabs) >= 3:
            num_pairs[tabs[2]] = (tabs[0], tabs[1])

    ws_pairs: Dict[str, Tuple[str, str]] = {}
    for block in whitespace.splitlines():
        tabs = block.split("\t")
        if len(tabs) >= 3:
            ws_pairs[tabs[2]] = (tabs[0], tabs[1])

    final: List[DiffEntry] = []
    for e in entries:
        adds, dels = "0", "0"
        for p, (a, d) in num_pairs.items():
            if p == e.path or p.endswith("/" + e.path):
                adds, dels = a, d
                break
        is_bin = _is_binary_numstat(adds, dels)
        ws_a, ws_d = "0", "0"
        for p, (a, d) in ws_pairs.items():
            if p == e.path or p.endswith("/" + e.path):
                ws_a, ws_d = a, d
                break
        whitespace_only = (not is_bin) and adds.lstrip("-").isdigit() and dels.lstrip("-").isdigit() \
            and int(ws_a or 0) == 0 and int(ws_d or 0) == 0
        note = e.note
        if is_bin:
            kind = "binary"
            note = "binary file: not shown"
        elif e.kind == "mode_changed":
            kind = "mode_changed"
        else:
            kind = e.kind
        final.append(
            DiffEntry(
                path=e.path,
                kind=kind,
                old_mode=e.old_mode,
                new_mode=e.new_mode,
                is_binary=is_bin,
                whitespace_only=whitespace_only and not is_bin and e.kind == "modified",
                line_ending_change=_has_eol_change(work_dir, e.path),
                additions=0 if is_bin else _to_int(adds),
                deletions=0 if is_bin else _to_int(dels),
                note=note,
            )
        )

    # If git produced no entries but the work dir is dirty, say so honestly.
    # The rendered text carries the caveats the diff itself cannot state. A
    # reviewer reads "+400/-400" long before they read an entry list, so a fact
    # that changes how the hunks should be read belongs in the text.
    caveats: List[str] = []
    eol_paths = [e.path for e in final if e.line_ending_change]
    if eol_paths:
        caveats.append(
            "line ending change (CRLF<->LF) in: " + ", ".join(sorted(eol_paths))
        )
    binary_paths = [e.path for e in final if e.is_binary]
    if binary_paths:
        caveats.append(
            "binary file(s), content not shown: " + ", ".join(sorted(binary_paths))
        )
    mode_paths = [e.path for e in final if e.kind == "mode_changed"]
    if mode_paths:
        caveats.append("mode change in: " + ", ".join(sorted(mode_paths)))
    ws_paths = [e.path for e in final if e.whitespace_only]
    if ws_paths:
        caveats.append(
            "whitespace-only change in: " + ", ".join(sorted(ws_paths))
        )
    text = _diff() if head else _git(work_dir, "diff")
    if caveats:
        text = "[!] " + "\n[!] ".join(caveats) + "\n\n" + text
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + f"\n[... diff truncated at {max_chars} chars ...]"
    digest = digest_of_diff(tuple(final), text, head)
    return HumanDiff(
        entries=tuple(final),
        text=text,
        truncated=truncated,
        truncation_bound=max_chars,
        digest=digest,
        head=head,
    )


def _to_int(value: str) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def _has_eol_change(work_dir: str, path: str) -> bool:
    """True when the working-tree line endings differ from HEAD's."""
    try:
        head_bytes = _git_bytes(work_dir, "show", f"HEAD:{path}")
    except Exception:
        return False
    try:
        disk = Path(work_dir, path).read_bytes()
    except OSError:
        return False
    head_crlf = head_bytes.count(b"\r\n")
    disk_crlf = disk.count(b"\r\n")
    head_bare_lf = head_bytes.count(b"\n") - head_crlf
    disk_bare_lf = disk.count(b"\n") - disk_crlf
    return (head_crlf > 0) != (disk_crlf > 0) or (head_bare_lf > 0) != (disk_bare_lf > 0)


def assert_fresh(diff: HumanDiff, work_dir: str) -> None:
    """Refuse a stale diff: the digest must match the current tree state."""
    current_head = ""
    try:
        current_head = _git(work_dir, "rev-parse", "--verify", "HEAD").strip()
    except Exception:
        current_head = ""
    if diff.head != current_head:
        raise ValueError(
            f"diff is stale: it was computed against {diff.head!r}, "
            f"current HEAD is {current_head!r}"
        )
    if digest_of_diff(diff.entries, diff.text, diff.head) != diff.digest:
        raise ValueError("diff is stale: its content no longer matches its digest")
    # The tree state is what makes a diff current; a byte-identical HEAD with
    # a mutated working copy is still stale, and only a re-render tells us that.
    current = render_human_diff(
        work_dir, max_chars=diff.truncation_bound
    )
    if current.digest != diff.digest:
        raise ValueError(
            "diff is stale: the working tree has moved on since it was computed"
        )
