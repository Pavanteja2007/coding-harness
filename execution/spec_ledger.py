"""Machine-checkable feature/spec artifacts with immutable items (Ceiling 08 §1).

A *spec artifact* is a JSON document that states what a change was supposed to
deliver, as machine-checkable items. Each item carries:

- ``id``            stable, human-meaningful identity
- ``title``         one line
- ``acceptance``    the criteria a judge may check
- ``tests``         test references that give the item teeth
- ``passes``        the ONLY field the building agent may flip

The contract is deliberately narrow, because the failure mode this closes is
an agent quietly shrinking its own obligations:

    The agent may flip ``passes``. It may NOT add an item, delete an item,
    or edit an item's identity, title, acceptance criteria, or test
    references.

Immutability is enforced mechanically, not by convention. :func:`seal` writes
a separate ``spec.seal.json`` artifact holding a SHA-256 fingerprint over the
IMMUTABLE projection of every item (ids, titles, acceptance, tests) plus the
item count. :meth:`SpecLedger.guard_against` recomputes the fingerprint from
whatever the artifact says at verification time and compares:

- a removed item, an added item, or a mutated item all fail the guard;
- flipping ``passes`` changes nothing in the fingerprint, so honest progress
  reporting is free.

The seal lives in its own file precisely so that a "helpful" rewrite of the
spec cannot rewrite its own permission slip. Consumers of the artifact that
have no seal still get :meth:`SpecLedger.integrity_report`, which reports
``sealed=False`` honestly instead of pretending the item set is protected.

Failure mode: :class:`SpecViolation` is raised for structurally invalid
artifacts (duplicate ids, missing acceptance, missing test refs) so a
malformed spec can never load as "zero obligations met".
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

SPEC_SCHEMA_VERSION = 1
SPEC_ARTIFACT_NAME = "spec.json"
SPEC_SEAL_NAME = "spec.seal.json"

#: Item fields that define the obligation itself. ``passes`` is deliberately
#: absent: it is the agent's report, not part of the obligation.
IMMUTABLE_ITEM_FIELDS: Tuple[str, ...] = ("id", "title", "acceptance", "tests")


class SpecViolation(ValueError):
    """Raised when a spec artifact is structurally invalid or illegally edited."""


@dataclass(frozen=True)
class SpecItem:
    """One immutable machine-checkable spec item."""

    id: str
    title: str
    acceptance: Tuple[str, ...] = ()
    tests: Tuple[str, ...] = ()
    passes: bool = False

    def immutable_projection(self) -> Dict[str, Any]:
        """Return the obligation-defining projection used for fingerprinting."""
        return {
            "id": self.id,
            "title": self.title,
            "acceptance": list(self.acceptance),
            "tests": list(self.tests),
        }

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON document form, with stable key order."""
        return {
            "id": self.id,
            "title": self.title,
            "acceptance": list(self.acceptance),
            "tests": list(self.tests),
            "passes": bool(self.passes),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "SpecItem":
        """Build an item from a JSON mapping, rejecting malformed shapes."""
        if not isinstance(raw, Mapping):
            raise SpecViolation("spec item must be a JSON object")
        item_id = str(raw.get("id") or "").strip()
        if not item_id:
            raise SpecViolation("spec item is missing a non-empty 'id'")
        acceptance = _string_tuple(raw.get("acceptance"), "acceptance", item_id)
        tests = _string_tuple(raw.get("tests"), "tests", item_id)
        if not acceptance:
            raise SpecViolation(f"spec item {item_id!r} has no acceptance criteria")
        if not tests:
            raise SpecViolation(f"spec item {item_id!r} has no test references")
        return cls(
            id=item_id,
            title=str(raw.get("title") or item_id),
            acceptance=acceptance,
            tests=tests,
            passes=bool(raw.get("passes", False)),
        )


@dataclass(frozen=True)
class SpecArtifact:
    """A parsed spec document: identity plus an ordered item list."""

    spec_id: str
    title: str = ""
    items: Tuple[SpecItem, ...] = ()
    schema_version: int = SPEC_SCHEMA_VERSION

    def item(self, item_id: str) -> Optional[SpecItem]:
        """Return the item with ``item_id``, or None when absent."""
        for candidate in self.items:
            if candidate.id == item_id:
                return candidate
        return None

    @property
    def item_ids(self) -> Tuple[str, ...]:
        """Return item ids in document order."""
        return tuple(item.id for item in self.items)

    def immutable_projection(self) -> Dict[str, Any]:
        """Return the fingerprintable projection of the whole document."""
        return {
            "schema_version": int(self.schema_version),
            "spec_id": self.spec_id,
            "title": self.title,
            "items": [item.immutable_projection() for item in self.items],
        }

    def fingerprint(self) -> str:
        """Return the SHA-256 fingerprint over the immutable projection."""
        payload = json.dumps(
            self.immutable_projection(), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON document form, with stable key order."""
        return {
            "schema_version": int(self.schema_version),
            "spec_id": self.spec_id,
            "title": self.title,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "SpecArtifact":
        """Build an artifact from a JSON mapping, rejecting malformed shapes."""
        if not isinstance(raw, Mapping):
            raise SpecViolation("spec artifact must be a JSON object")
        spec_id = str(raw.get("spec_id") or "").strip()
        if not spec_id:
            raise SpecViolation("spec artifact is missing a non-empty 'spec_id'")
        raw_items = raw.get("items")
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
            raise SpecViolation("spec artifact 'items' must be a JSON array")
        if not raw_items:
            raise SpecViolation("spec artifact has no items")
        items = tuple(SpecItem.from_dict(entry) for entry in raw_items)
        seen: Dict[str, int] = {}
        for index, item in enumerate(items):
            if item.id in seen:
                raise SpecViolation(
                    f"duplicate spec item id {item.id!r} at positions "
                    f"{seen[item.id]} and {index}"
                )
            seen[item.id] = index
        version = raw.get("schema_version", SPEC_SCHEMA_VERSION)
        try:
            version_int = int(version)
        except (TypeError, ValueError) as exc:
            raise SpecViolation(
                "spec artifact 'schema_version' must be an integer"
            ) from exc
        return cls(
            spec_id=spec_id,
            title=str(raw.get("title") or spec_id),
            items=items,
            schema_version=version_int,
        )


@dataclass(frozen=True)
class SpecSeal:
    """An immutable fingerprint record for one spec artifact.

    ``item_projections`` stores each item's obligation-defining projection so
    the guard can NAME a mutated item instead of only reporting that some
    fingerprint changed. It is additive: a thin seal still guards correctly via
    ``fingerprint``, it just cannot attribute the difference.
    """

    spec_id: str
    fingerprint: str
    item_count: int
    item_ids: Tuple[str, ...] = ()
    sealed_at: str = ""
    item_projections: Tuple[Dict[str, Any], ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON document form of the seal."""
        return {
            "schema_version": SPEC_SCHEMA_VERSION,
            "spec_id": self.spec_id,
            "fingerprint": self.fingerprint,
            "item_count": int(self.item_count),
            "item_ids": list(self.item_ids),
            "item_projections": [dict(item) for item in self.item_projections],
            "sealed_at": self.sealed_at,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "SpecSeal":
        """Build a seal from a JSON mapping, rejecting malformed shapes."""
        if not isinstance(raw, Mapping):
            raise SpecViolation("spec seal must be a JSON object")
        fingerprint = str(raw.get("fingerprint") or "").strip()
        spec_id = str(raw.get("spec_id") or "").strip()
        if not fingerprint or not spec_id:
            raise SpecViolation("spec seal requires 'spec_id' and 'fingerprint'")
        try:
            item_count = int(raw.get("item_count", 0))
        except (TypeError, ValueError) as exc:
            raise SpecViolation("spec seal 'item_count' must be an integer") from exc
        ids = raw.get("item_ids") or []
        if not isinstance(ids, Sequence) or isinstance(ids, (str, bytes)):
            raise SpecViolation("spec seal 'item_ids' must be a JSON array")
        raw_projections = raw.get("item_projections") or []
        if not isinstance(raw_projections, Sequence) or isinstance(
            raw_projections, (str, bytes)
        ):
            raise SpecViolation("spec seal 'item_projections' must be a JSON array")
        projections = tuple(
            dict(entry) for entry in raw_projections if isinstance(entry, Mapping)
        )
        return cls(
            spec_id=spec_id,
            fingerprint=fingerprint,
            item_count=item_count,
            item_ids=tuple(str(value) for value in ids),
            sealed_at=str(raw.get("sealed_at") or ""),
            item_projections=projections,
        )


@dataclass(frozen=True)
class SpecGuardReport:
    """The diff-guard verdict for one spec artifact against a seal.

    ``ok`` is the only field a gate should branch on. ``removed`` /
    ``added`` / ``mutated`` name the offending items so a refusal is
    actionable rather than a bare boolean.
    """

    ok: bool
    sealed: bool
    spec_id: str = ""
    expected_fingerprint: str = ""
    actual_fingerprint: str = ""
    removed: Tuple[str, ...] = ()
    added: Tuple[str, ...] = ()
    mutated: Tuple[str, ...] = ()
    claimed: Tuple[str, ...] = ()
    unclaimed: Tuple[str, ...] = ()
    violations: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible view for trace/evidence records."""
        return {
            "ok": bool(self.ok),
            "sealed": bool(self.sealed),
            "spec_id": self.spec_id,
            "expected_fingerprint": self.expected_fingerprint,
            "actual_fingerprint": self.actual_fingerprint,
            "removed": list(self.removed),
            "added": list(self.added),
            "mutated": list(self.mutated),
            "claimed": list(self.claimed),
            "unclaimed": list(self.unclaimed),
            "violations": list(self.violations),
        }


@dataclass
class SpecLedger:
    """Read/write access to one spec artifact plus its integrity guard.

    The ledger is intentionally mutable in exactly one way: :meth:`apply_claims`
    flips ``passes`` on items that already exist. Structural edits go through
    :meth:`replace_items`, which exists so a HUMAN (or a spec-authoring
    step) can define obligations, and which is why every verification still
    re-checks the artifact against the seal rather than trusting this object.
    """

    artifact: SpecArtifact
    _original: SpecArtifact = field(default_factory=lambda: SpecArtifact(spec_id=""))
    _seal: Optional[SpecSeal] = None
    _path: Optional[str] = None

    # -- construction -----------------------------------------------------

    @classmethod
    def load(cls, path: str, *, seal_path: Optional[str] = None) -> "SpecLedger":
        """Load a spec artifact (and its seal when present) from ``path``.

        Raises :class:`SpecViolation` for a missing or malformed artifact. A
        missing SEAL is not an error: the ledger loads with ``sealed=False`` so
        the guard can report the missing seal honestly.
        """
        raw = _read_json(path, f"spec artifact {path!r}")
        artifact = SpecArtifact.from_dict(raw)
        resolved_seal_path = seal_path or _sibling(path, SPEC_SEAL_NAME)
        seal: Optional[SpecSeal] = None
        if resolved_seal_path and os.path.isfile(resolved_seal_path):
            seal = SpecSeal.from_dict(_read_json(resolved_seal_path, "spec seal"))
        return cls(artifact=artifact, _original=artifact, _seal=seal, _path=path)

    @classmethod
    def create(
        cls,
        spec_id: str,
        items: Iterable[Mapping[str, Any]],
        *,
        title: str = "",
    ) -> "SpecLedger":
        """Build a fresh ledger from item definitions and seal it immediately.

        Sealing at creation is what makes the guard meaningful: a spec that is
        never sealed reports ``sealed=False`` and a caller can require a seal.
        """
        document = {
            "schema_version": SPEC_SCHEMA_VERSION,
            "spec_id": spec_id,
            "title": title or spec_id,
            "items": [dict(item) for item in items],
        }
        artifact = SpecArtifact.from_dict(document)
        return cls(artifact=artifact, _original=artifact, _seal=seal_of(artifact))

    # -- inspection -------------------------------------------------------

    @property
    def sealed(self) -> bool:
        """Return whether a seal is attached."""
        return self._seal is not None

    @property
    def seal(self) -> Optional[SpecSeal]:
        """Return the attached seal, or None."""
        return self._seal

    @property
    def path(self) -> Optional[str]:
        """Return the on-disk artifact path, when the ledger was loaded/saved."""
        return self._path

    @property
    def items(self) -> Tuple[SpecItem, ...]:
        """Return the current item list."""
        return self.artifact.items

    def claimed(self) -> Tuple[str, ...]:
        """Return ids of items whose ``passes`` is True."""
        return tuple(item.id for item in self.artifact.items if item.passes)

    def unclaimed(self) -> Tuple[str, ...]:
        """Return ids of items whose ``passes`` is still False."""
        return tuple(item.id for item in self.artifact.items if not item.passes)

    def to_dict(self) -> Dict[str, Any]:
        """Return the artifact's JSON document form."""
        return self.artifact.to_dict()

    # -- mutation ---------------------------------------------------------

    def apply_claims(self, claims: Mapping[str, Any]) -> Dict[str, Any]:
        """Flip ``passes`` for existing items only.

        ``claims`` maps item id to a truthy/falsey claim. An id that is not in
        the artifact is a violation, not a no-op: inventing an obligation is as
        much a spec mutation as deleting one, and silently dropping it would let
        a caller believe a claim was recorded.

        Returns ``{"applied": [...ids...], "rejected": [...ids...]}``.
        Raises :class:`SpecViolation` when any id is unknown.
        """
        known = set(self.artifact.item_ids)
        unknown = sorted(str(key) for key in claims if str(key) not in known)
        if unknown:
            raise SpecViolation(
                "spec claims reference unknown item ids: " + ", ".join(unknown)
            )
        updated: List[SpecItem] = []
        applied: List[str] = []
        for item in self.artifact.items:
            if item.id in claims:
                updated.append(
                    SpecItem(
                        id=item.id,
                        title=item.title,
                        acceptance=item.acceptance,
                        tests=item.tests,
                        passes=bool(claims[item.id]),
                    )
                )
                applied.append(item.id)
            else:
                updated.append(item)
        self.artifact = SpecArtifact(
            spec_id=self.artifact.spec_id,
            title=self.artifact.title,
            items=tuple(updated),
            schema_version=self.artifact.schema_version,
        )
        return {"applied": applied, "rejected": []}

    def replace_items(
        self, items: Iterable[Mapping[str, Any]], *, title: str = ""
    ) -> None:
        """Replace the item list wholesale (spec authoring, not the agent).

        This is the only structural write path and it is documented as a
        human/spec-authoring operation: the guard re-run afterwards will report
        any difference from the seal, which is the point.
        """
        document = {
            "schema_version": SPEC_SCHEMA_VERSION,
            "spec_id": self.artifact.spec_id,
            "title": title or self.artifact.title,
            "items": [dict(item) for item in items],
        }
        self.artifact = SpecArtifact.from_dict(document)

    # -- persistence ------------------------------------------------------

    def save(self, path: Optional[str] = None, *, seal: bool = False) -> str:
        """Atomically write the artifact, optionally writing a fresh seal.

        Writing a new seal is a deliberate act (``seal=True``) so a routine
        save during a run can never quietly re-baseline the guard.
        """
        target = path or self._path
        if not target:
            raise SpecViolation("SpecLedger.save needs a path")
        _write_json(target, self.artifact.to_dict())
        self._path = target
        if seal:
            self.write_seal()
        return target

    def write_seal(self, path: Optional[str] = None, *, sealed_at: str = "") -> str:
        """Write the seal for the CURRENT artifact and attach it."""
        target = path or (self._path and _sibling(self._path, SPEC_SEAL_NAME))
        if not target:
            raise SpecViolation("SpecLedger.write_seal needs a path")
        seal = seal_of(self.artifact, sealed_at=sealed_at)
        _write_json(target, seal.to_dict())
        self._seal = seal
        return target

    # -- the guard --------------------------------------------------------

    def guard_against(self, seal: Optional[SpecSeal] = None) -> SpecGuardReport:
        """Diff the current artifact against a seal.

        A removed item, an added item, a mutated obligation, a spec-id swap, or
        an item-count change all fail. Flipping ``passes`` cannot fail, because
        ``passes`` is not part of the fingerprint.

        With no seal available the report is ``ok=False`` and ``sealed=False``:
        an unsealed spec is honestly reported as unprotected, not as passing.
        """
        effective = seal or self._seal
        actual = self.artifact.fingerprint()
        if effective is None:
            return SpecGuardReport(
                ok=False,
                sealed=False,
                spec_id=self.artifact.spec_id,
                actual_fingerprint=actual,
                violations=("spec is not sealed; item removal cannot be detected",),
                claimed=self.claimed(),
                unclaimed=self.unclaimed(),
            )
        violations: List[str] = []
        removed: List[str] = []
        added: List[str] = []
        mutated: List[str] = []
        if self.artifact.spec_id != effective.spec_id:
            violations.append(
                f"spec_id changed: {effective.spec_id!r} -> {self.artifact.spec_id!r}"
            )
        expected_ids = set(effective.item_ids)
        current_ids = set(self.artifact.item_ids)
        for item_id in sorted(expected_ids - current_ids):
            removed.append(item_id)
        for item_id in sorted(current_ids - expected_ids):
            added.append(item_id)
        if len(self.artifact.items) != int(effective.item_count):
            violations.append(
                "item count changed: "
                f"{effective.item_count} -> {len(self.artifact.items)}"
            )
        for item in self.artifact.items:
            if item.id not in expected_ids:
                continue
            if _expected_projection(effective, item.id) != item.immutable_projection():
                mutated.append(item.id)
        if removed:
            violations.append("spec items removed: " + ", ".join(removed))
        if added:
            violations.append("spec items added: " + ", ".join(added))
        if mutated:
            violations.append("spec items mutated: " + ", ".join(mutated))
        if actual != effective.fingerprint:
            violations.append("spec fingerprint changed")
        return SpecGuardReport(
            ok=not violations,
            sealed=True,
            spec_id=self.artifact.spec_id,
            expected_fingerprint=effective.fingerprint,
            actual_fingerprint=actual,
            removed=tuple(removed),
            added=tuple(added),
            mutated=tuple(mutated),
            claimed=self.claimed(),
            unclaimed=self.unclaimed(),
            violations=tuple(violations),
        )

    def integrity_report(self) -> Dict[str, Any]:
        """Return a machine-readable summary of the current spec state."""
        report = self.guard_against()
        return {
            **report.to_dict(),
            "items": len(self.artifact.items),
            "claimed_count": len(self.claimed()),
            "unclaimed_count": len(self.unclaimed()),
        }


# ---------------------------------------------------------------------------
# module helpers
# ---------------------------------------------------------------------------


def seal_of(artifact: SpecArtifact, *, sealed_at: str = "") -> SpecSeal:
    """Return a fresh :class:`SpecSeal` for ``artifact``."""
    return SpecSeal(
        spec_id=artifact.spec_id,
        fingerprint=artifact.fingerprint(),
        item_count=len(artifact.items),
        item_ids=artifact.item_ids,
        sealed_at=sealed_at,
        item_projections=tuple(item.immutable_projection() for item in artifact.items),
    )


def guard_paths(root: str) -> Tuple[str, str]:
    """Return the conventional (artifact, seal) paths under ``root``."""
    return os.path.join(root, SPEC_ARTIFACT_NAME), os.path.join(root, SPEC_SEAL_NAME)


def load_or_report(root: str) -> SpecGuardReport:
    """Load the spec under ``root`` and guard it; report, never raise.

    A missing, malformed, or unsealed spec produces a failing report rather
    than an exception, because "there is no machine-checkable spec" is a gate
    failure a caller must be able to record, not a crash.
    """
    artifact_path, seal_path = guard_paths(root)
    try:
        ledger = SpecLedger.load(artifact_path, seal_path=seal_path)
    except (SpecViolation, OSError, ValueError) as exc:
        return SpecGuardReport(
            ok=False, sealed=False, violations=(f"spec unreadable: {exc}",)
        )
    return ledger.guard_against()


def _expected_projection(seal: SpecSeal, item_id: str) -> Optional[Dict[str, Any]]:
    """Return the sealed projection for ``item_id`` when it is recoverable.

    Older seals may predate per-item projections; in that case ``None`` is
    returned and the fingerprint comparison still governs the verdict, so a
    thin seal degrades to "fingerprint mismatch" rather than a false pass.
    """
    for projection in seal.item_projections:
        if str(projection.get("id") or "") == item_id:
            return dict(projection)
    return None


def _string_tuple(value: Any, field_name: str, item_id: str) -> Tuple[str, ...]:
    """Coerce a JSON value into a tuple of non-empty strings."""
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise SpecViolation(
            f"spec item {item_id!r} field {field_name!r} must be a JSON array"
        )
    out: List[str] = []
    for entry in value:
        text = str(entry or "").strip()
        if not text:
            raise SpecViolation(
                f"spec item {item_id!r} has an empty {field_name!r} entry"
            )
        out.append(text)
    return tuple(out)


def _sibling(path: str, name: str) -> str:
    """Return ``name`` next to ``path``."""
    return os.path.join(os.path.dirname(os.path.abspath(path)), name)


def _read_json(path: str, label: str) -> Any:
    """Read a JSON document, converting IO/shape problems into SpecViolation."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise SpecViolation(f"{label} not found at {path!r}") from exc
    except (OSError, ValueError, UnicodeError) as exc:
        raise SpecViolation(f"{label} at {path!r} is unreadable: {exc}") from exc


def _write_json(path: str, value: Any) -> None:
    """Atomically write a JSON document (tmp + replace).

    The temporary handle is bound to a name rather than used as a context
    manager because the atomic replace has to happen AFTER the handle is
    closed; the tmp name is also what makes the cleanup path possible.
    """
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - see docstring
        "w",
        encoding="utf-8",
        dir=parent,
        prefix=".spec-",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            json.dump(value, handle, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
