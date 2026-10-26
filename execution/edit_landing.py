"""Where a write actually LANDED -- the half of a fuzzy edit that is not matching.

T1 owns MATCHING (which block a fuzzy cascade selects). This module owns
LANDING: what happens once a match has been chosen and a repository write is
about to be attempted.

The split exists because the two fail differently. A matching bug is an
accuracy bug -- the wrong block is chosen, and the journal says which block.
A landing bug is a CONTAINMENT bug: the write goes somewhere the task never
declared, and nothing downstream re-asks. This module is the choke point for
the second kind, and it is deliberately importable with no dependency on any
matcher, so a future 9-stage cascade routes through it without adopting it.

Two invariants, stated once:

1. **A write may not land outside the declared roots.** A cascade that
   searched the whole repository and matched in ``other/module.py`` when the
   edit was declared for ``src/`` is not an accuracy problem -- it is a write
   outside the boundary the task was given. :func:`assert_landing_within_roots`
   refuses it, and the refusal happens BEFORE any bytes are produced.

2. **A write that lands where it was NOT intended must be detectable and
   undoable.** A fuzzy match that succeeds in the wrong file is allowed to
   happen -- refusing every non-verbatim match would make the cascade useless
   -- so the compensating control is not prevention but RECORD: the landing is
   published with the path it intended, the path it hit, the byte offset, the
   before/after digests, and a verified undo. :func:`landing_receipt` is that
   record, and it is DERIVED from the journal record rather than from the
   caller's request, so a caller cannot describe a landing that did not occur.

Neither invariant depends on the other: the roots check is a refusal that
needs no journal, and the receipt is a record that needs no matcher.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from execution.workspace import WorkspaceEditError

#: How a write was matched. A CLOSED vocabulary, because the whole point of the
#: receipt is that a reader can tell a verbatim edit from a fuzzy one without
#: parsing prose, and an open set would make "fuzzy" a judgement call again.
LANDING_EXACT_VERBATIM = "exact_verbatim"
LANDING_EXACT_REPLACE_ALL = "exact_replace_all"
LANDING_FUZZY = "fuzzy"
LANDING_CLASSES: Tuple[str, ...] = (
    LANDING_EXACT_VERBATIM,
    LANDING_EXACT_REPLACE_ALL,
    LANDING_FUZZY,
)

#: Whether the landing receipt can be trusted to describe what happened. Four
#: values, and the ordering matters: a caller that checks only "did it land at
#: the intended path" learns nothing when the landing was WRONG, which is the
#: case the receipt exists to make loud.
LANDING_AS_INTENDED = "as_intended"
LANDING_WRONG_FILE = "wrong_file"
LANDING_CROSS_ROOT = "cross_root"
LANDING_UNDETECTABLE = "undetectable"
LANDING_VERDICTS: Tuple[str, ...] = (
    LANDING_AS_INTENDED,
    LANDING_WRONG_FILE,
    LANDING_CROSS_ROOT,
    LANDING_UNDETECTABLE,
)


class EditLandingRefused(WorkspaceEditError):
    """A write was refused because it would land outside the declared roots.

    A :class:`~execution.workspace.WorkspaceEditError` so every existing caller
    that already handles a refused edit handles this one too, and so nothing in
    the tree can mistake it for a matcher failure. Raised BEFORE any post-image
    is built, so a refusal means no bytes were produced anywhere.
    """


def normalize_roots(roots: Optional[Iterable[str]]) -> Tuple[str, ...]:
    """Return the declared roots as clean repository-relative POSIX prefixes.

    A root is a PREFIX, not a file: ``("src",)`` covers ``src/a/b.py``. Roots
    are normalized with the same rules a write path is normalized with, so a
    root cannot smuggle a traversal past the check that a path cannot get past
    it. A root that is absolute or traversing is DROPPED rather than clamped,
    for the reason :func:`execution.sandbox._normalize_repo_relative` gives: a
    clamped root would report a boundary different from the enforced one.

    An empty or absent declaration means THE WHOLE WORKSPACE, which is the
    historical behaviour and is reported by :func:`describe_roots` rather than
    being an implicit surprise.
    """
    values = roots if roots is not None else ()
    if isinstance(values, (str, bytes)):
        values = [values]
    cleaned: List[str] = []
    for raw in values:
        text = str(raw or "").strip().replace("\\", "/")
        if not text:
            continue
        if text.startswith("/") or ":" in text.split("/")[0]:
            continue
        parts = [part for part in text.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            continue
        if not parts:
            continue
        candidate = "/".join(parts)
        if candidate not in cleaned:
            cleaned.append(candidate)
    return tuple(cleaned)


def path_within_roots(
    relative: str, roots: Optional[Iterable[str]] = ()
) -> bool:
    """Return whether a repository-relative path is inside the declared roots.

    Boundary-safe by construction: a prefix test compares path SEGMENTS, not
    strings. ``"src-extra/x.py"`` is not inside ``"src"`` even though it starts
    with those three characters, and that is the shape a plain
    ``startswith`` gets wrong in exactly the case that matters.

    An empty root set means the whole workspace, so this returns True. Callers
    that want the roots to be mandatory should check
    :func:`describe_roots` -- or better, should pass them.
    """
    declared = normalize_roots(roots)
    if not declared:
        return True
    text = str(relative or "").replace("\\", "/").strip()
    if not text:
        return False
    return any(text == root or text.startswith(root + "/") for root in declared)


def describe_roots(roots: Optional[Iterable[str]] = ()) -> str:
    """Return the one-line human statement of what the declared roots MEAN.

    Absent roots render as ``"whole workspace (no roots declared)"`` rather
    than as nothing, because a receipt that omits its boundary is the same
    dishonesty as one that overstates it: a reader cannot tell "unrestricted"
    from "unreported".
    """
    declared = normalize_roots(roots)
    if not declared:
        return "whole workspace (no roots declared)"
    return "roots: " + ", ".join(declared)


def assert_landing_within_roots(
    relative: str,
    roots: Optional[Iterable[str]] = (),
    *,
    intended: str = "",
) -> str:
    """Refuse a landing outside the declared roots; return the normalized path.

    Call this immediately BEFORE the write is dispatched, with the path the
    write is about to target. Raises :class:`EditLandingRefused` -- which means
    nothing was written -- with a message naming the path, the roots, and the
    path the caller MEANT, so the difference is legible in the failure and not
    only in a post-hoc diff.

    ``intended`` is optional but should always be passed by a fuzzy cascade: a
    refusal that does not say what was wanted is a refusal the next turn
    cannot act on.
    """
    from execution.workspace import normalize_relative_path

    normalized = normalize_relative_path(relative)
    if path_within_roots(normalized, roots):
        return normalized
    wanted = f"; the edit was declared for {intended!r}" if intended else ""
    raise EditLandingRefused(
        f"refused a write to {normalized!r}: it is outside the declared "
        f"{describe_roots(roots)}{wanted}. A fuzzy match that lands outside "
        "the declared roots is a containment violation, not a near miss."
    )


@dataclass(frozen=True)
class LandingReceipt:
    """Where one write landed, derived from the journal record that made it.

    Every field here is read from the mutation the workspace actually
    performed. ``landed_path`` is the path on the record; ``pre_sha256`` /
    ``post_sha256`` are the digests the fenced replace bound; ``offset`` and
    ``replacements`` come from the effect the edit itself journalled. Nothing
    is taken from the caller's request, which is what makes the receipt
    evidence about the write rather than a description of the intention.

    ``undo_operation`` / ``undo_hunk`` name the handles that revert it. They
    are populated only from the record's own ``operation_id``/``hunk_id``; a
    receipt never claims an undo it cannot name.
    """

    landed_path: str
    intended_path: str
    landing_class: str
    verdict: str
    replacements: int
    offset: int
    pre_sha256: str
    post_sha256: str
    pre_size: int
    post_size: int
    created: bool
    undo_operation: str
    undo_hunk: str
    roots: Tuple[str, ...]
    roots_statement: str
    reversible: bool
    notes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-safe receipt.

        ``notes`` is a list rather than prose so a test can assert on a
        substring and a reviewer can read the whole thing; the two uses do not
        conflict here and a single string serves only the second.
        """
        return {
            "landed_path": self.landed_path,
            "intended_path": self.intended_path,
            "landing_class": self.landing_class,
            "verdict": self.verdict,
            "replacements": self.replacements,
            "offset": self.offset,
            "pre_sha256": self.pre_sha256,
            "post_sha256": self.post_sha256,
            "pre_size": self.pre_size,
            "post_size": self.post_size,
            "created": self.created,
            "undo_operation": self.undo_operation,
            "undo_hunk": self.undo_hunk,
            "roots": list(self.roots),
            "roots_statement": self.roots_statement,
            "reversible": self.reversible,
            "notes": list(self.notes),
        }


def _as_mapping(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            built = to_dict()
        except Exception:  # pragma: no cover - a receipt must never crash a run
            return {}
        if not isinstance(built, dict):
            return {}
        # `EditResult.to_dict()` omits `hunk_id` and `kind`, both of which the
        # receipt needs. Falling back to the ATTRIBUTE keeps the receipt
        # complete whether it is built from the live object or from the
        # serialised form a journal replay would hand it -- and a receipt that
        # silently loses the hunk handle in one of the two is a receipt that
        # cannot answer "how do I undo just this hunk" half the time.
        for extra in ("hunk_id", "kind", "operation_id", "created"):
            if extra not in built:
                attribute = getattr(value, extra, None)
                if attribute is not None:
                    built[extra] = attribute
        return built
    return {}


def landing_receipt(
    result: Any,
    *,
    intended_path: str,
    roots: Optional[Iterable[str]] = (),
    landing_class: str = LANDING_EXACT_VERBATIM,
) -> LandingReceipt:
    """Build the landing receipt for one applied write.

    ``result`` is the :class:`~execution.workspace.EditResult` (or any object
    exposing the same ``to_dict``), i.e. the value the workspace returned AFTER
    the fenced replace. It is the only input that carries what happened.

    The verdict is computed here rather than passed in, because a caller that
    supplied its own verdict would be describing the write rather than
    reporting it:

    * ``cross_root`` when the landed path is outside the declared roots. This
      should be unreachable through
      :func:`assert_landing_within_roots`, so reaching it means a call site
      bypassed the check -- the receipt says so in ``notes`` rather than
      reporting the write as merely misfiled.
    * ``wrong_file`` when a fuzzy landing landed somewhere other than the
      intended file. This is the allowed-but-recorded case: the write is
      reversible and the receipt names both paths, which is what makes it
      detectable and undoable rather than a silent edit to the wrong module.
    * ``undetectable`` when the record carries no before-image digest. Without
      it there is no way to prove what the file held, so the receipt refuses to
      claim the landing is reversible. A missing measurement is not a passing
      measurement (``phases/DOCTRINE.md`` §1).
    * ``as_intended`` otherwise.

    Raises :class:`ValueError` for an unknown ``landing_class`` rather than
    defaulting, so a new cascade stage cannot be recorded as a verbatim edit.
    """
    if landing_class not in LANDING_CLASSES:
        raise ValueError(
            f"unknown landing class {landing_class!r}; expected one of "
            f"{LANDING_CLASSES!r}"
        )
    data = _as_mapping(result)
    landed = str(data.get("path") or "")
    intended = str(intended_path or "").replace("\\", "/").strip()
    effect = data.get("effect")
    effect_map = _as_mapping(effect) if effect is not None else {}
    before = _as_mapping(data.get("before"))
    after = _as_mapping(data.get("after"))
    created = bool(data.get("created"))

    notes: List[str] = []
    declared = normalize_roots(roots)
    pre_sha = str(before.get("sha256") or "")
    post_sha = str(after.get("sha256") or "")
    if not landed:
        notes.append(
            "the journal record carries no path; this receipt cannot say where "
            "the write landed"
        )

    if landed and not path_within_roots(landed, declared):
        verdict = LANDING_CROSS_ROOT
        notes.append(
            "this write landed OUTSIDE the declared roots, which means the "
            "pre-write check was bypassed rather than passed"
        )
    elif created:
        # A file the write CREATED has no pre-image, and that is not a missing
        # measurement -- it is the fact that makes the undo's delete
        # verifiable. It is still detected (the record says `created`) and still
        # reversible, so calling it `undetectable` would be the dishonest
        # direction.
        if intended and landed and landed != intended:
            verdict = LANDING_WRONG_FILE
            notes.append(
                f"the write CREATED {landed!r} rather than replacing the declared "
                f"{intended!r}; undo deletes it, and its absence is the proof"
            )
        else:
            verdict = LANDING_AS_INTENDED
            notes.append(
                "the write created the file; undo removes it, and the proof is "
                "that the path is absent afterwards"
            )
    elif not pre_sha:
        verdict = LANDING_UNDETECTABLE
        notes.append(
            "no pre-image digest on the record: the previous content cannot be "
            "proven, so this landing is not claimed to be reversible"
        )
    elif intended and landed and landed != intended:
        verdict = LANDING_WRONG_FILE
        notes.append(
            f"the write landed in {landed!r} rather than the declared "
            f"{intended!r}; it is journalled and reversible, not silent"
        )
    else:
        verdict = LANDING_AS_INTENDED

    operation_id = str(data.get("operation_id") or "")
    hunk_id = str(data.get("hunk_id") or "")
    reversible = bool(operation_id) and verdict != LANDING_UNDETECTABLE
    if not operation_id:
        notes.append(
            "no operation id on the record, so no undo handle can be named"
        )
    if landing_class == LANDING_FUZZY and verdict == LANDING_AS_INTENDED:
        notes.append(
            "fuzzy landing at the intended path; the match quality is the "
            "matcher's receipt, not this module's claim"
        )

    return LandingReceipt(
        landed_path=landed,
        intended_path=intended,
        landing_class=landing_class,
        verdict=verdict,
        replacements=int(data.get("replacements") or effect_map.get("replacements") or 0),
        offset=int(effect_map.get("offset") or -1),
        pre_sha256=pre_sha,
        post_sha256=post_sha,
        pre_size=int(before.get("size") or 0),
        post_size=int(after.get("size") or 0),
        created=created,
        undo_operation=operation_id,
        undo_hunk=hunk_id,
        roots=declared,
        roots_statement=describe_roots(declared),
        reversible=reversible,
        notes=tuple(notes),
    )


def ws_workspace_error() -> type:
    """Return the workspace's base error class, for a precise except clause.

    A local import inside a function is the established pattern in this
    package (``execution/sandbox.py`` does the same for ``ContainmentPolicy``),
    and it keeps this module importable with no workspace import at module
    scope -- which is what keeps it out of ``execution.sandbox``'s import graph.
    """
    from execution.workspace import WorkspaceError

    return WorkspaceError


def verify_undo(
    workspace: Any,
    receipt: LandingReceipt,
    *,
    operation_id: str = "",
) -> Dict[str, Any]:
    """Revert a landing through the workspace's own undo, and report the proof.

    Takes the receipt rather than the result so the thing being undone is the
    thing that was recorded. The journal restores the pre-image and DELETES a
    file the write created; afterwards this reads the file back and reports
    whether the restored bytes hash to the receipt's ``pre_sha256``.

    That read-back is the part that makes the claim evidence. "Undo returned
    ok" is a report about an undo; "the file now hashes to the digest the
    receipt recorded before the write" is a report about the FILE. A created
    file is verified by its ABSENCE, which is the correct check for a file
    that did not exist before.

    Returns a dict with ``attempted``, ``undone``, ``verified``, ``verified_by``
    (``"absent"`` for a created file, ``"sha256"`` otherwise), ``reason`` when
    it could not verify, and the undo result. Never raises: an undo that fails
    must be REPORTED, because the alternative is a caller that catches nothing
    and believes a wrong write is gone.
    """
    target = operation_id or receipt.undo_operation
    report: Dict[str, Any] = {
        "attempted": bool(target),
        "undone": False,
        "verified": False,
        "verified_by": "",
        "path": receipt.landed_path,
        "undo_result": None,
    }
    if not target:
        report["reason"] = "the receipt names no undo handle"
        return report
    try:
        undone = workspace.undo(target)
    except Exception as exc:
        report["reason"] = f"undo raised: {exc!r}"
        return report
    report["undo_result"] = _as_mapping(undone)
    report["undone"] = bool(
        report["undo_result"].get("ok", report["undo_result"].get("undone", False))
    )
    if not receipt.landed_path:
        report["reason"] = "the receipt names no path to verify"
        return report
    try:
        current = workspace.read_bytes(receipt.landed_path)
    except (FileNotFoundError, ws_workspace_error()):
        # A deleted file is the EXPECTED outcome for a created file, and this
        # workspace raises a refusal (not FileNotFoundError) for a path that is
        # no longer a regular file -- so both mean "absent".
        current = None
    except Exception as exc:
        report["reason"] = f"could not read the path back: {exc!r}"
        return report
    if current is None:
        if receipt.created:
            report["verified"] = True
            report["verified_by"] = "absent"
            return report
        report["reason"] = "the path is absent but the write did not create it"
        return report
    import hashlib

    digest = hashlib.sha256(current).hexdigest()
    if receipt.pre_sha256 and digest == receipt.pre_sha256:
        report["verified"] = True
        report["verified_by"] = "sha256"
        return report
    report["reason"] = (
        f"the restored bytes hash to {digest[:12]}, not the recorded "
        f"pre-image {receipt.pre_sha256[:12]}"
    )
    return report


def landing_summary(receipts: Sequence[LandingReceipt]) -> Dict[str, Any]:
    """Fold several landing receipts into one countable summary.

    A cascade writes many files, and a reader asking "did anything land in the
    wrong place" wants a count, not a list they have to scan. The counts are
    derived from the receipts' own verdicts, and ``clean`` is
    ``not any(wrong)`` so it cannot be True while a bad landing is present.
    """
    items = list(receipts or ())
    by_verdict: Dict[str, int] = {name: 0 for name in LANDING_VERDICTS}
    for receipt in items:
        by_verdict[receipt.verdict] = by_verdict.get(receipt.verdict, 0) + 1
    off_path = [
        receipt.landed_path
        for receipt in items
        if receipt.verdict in (LANDING_WRONG_FILE, LANDING_CROSS_ROOT)
    ]
    unrecoverable = [
        receipt.landed_path for receipt in items if not receipt.reversible
    ]
    return {
        "count": len(items),
        "by_verdict": by_verdict,
        "off_path": off_path,
        "not_reversible": unrecoverable,
        "clean": not off_path and not unrecoverable,
        "paths": [posixpath.join(*receipt.landed_path.split("/")[-1:]) for receipt in items],
    }
