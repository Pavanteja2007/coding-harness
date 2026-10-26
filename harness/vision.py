"""Vision: a real multimodal content block, or an honest refusal.

The defect this closes, measured not described
----------------------------------------------
The ``image`` tool exists in the one canonical catalog
(:data:`harness.tools._TYPED_TOOL_SPECS`) and its handler produces a dict:

    execution/workspace.py::Workspace.read_image
        -> {"path", "media_type", "bytes", "revision", "data_base64": ...}

That dict is a **JSON tool result**, and a JSON tool result becomes a **text
string** in the model's message list. So the base64 blob reaches the model as a
wall of ~1.33x the image's bytes in ASCII characters. Measured before this
module existed::

    grep -rn "image_url|input_image|data_base64|multimodal|supports_vision" \\
         harness/*.py harness/agent_kernel/*.py runtime/*.py
    -> ZERO hits

**The model cannot see a screenshot.** A user attaches an image of an error and
the agent receives base64 it cannot read. This is the "7-line gap and a large
capability" the round brief named, and the 7 lines were the easy part: the hard
part is that a correct implementation has to refuse four different ways.

Why a content block and not better base64
-----------------------------------------
There is no textual encoding of an image a model can read. The fix is a
**structured content block** in the provider's own shape:

* OpenAI-compatible -> ``{"type": "image_url", "image_url": {"url": "data:...;base64,..."}}``
* Anthropic -> ``{"type": "image", "source": {"type": "base64", ...}}``
* Google -> ``{"type": "image_url", "image_url": {"url": "data:..."}}``
  (Gemini's REST passthrough; OpenAI-compat shims use the first shape)

A base64 blob wrapped in more base64 is not a small optimisation, it is the only
thing that works.

The four refusals, and why each one exists
------------------------------------------
This module deliberately mirrors :class:`runtime.model_capabilities.EffortPlan`
field for field, because it answers the same class of question - *"what was
actually sent?"* - and because the repository already has an established
discipline for exactly this: **never attempt the impossible, report why.** A
``vision_status`` of ``unsupported_model`` and a request that was never built
is the same shape as ``effort_status: unsupported_model`` and no parameter sent.

1. :data:`VISION_UNSUPPORTED_MODEL` - not every model is multimodal, and a
   vision block sent to a text-only model is a hard 400 from the provider. The
   gate is **fail-closed and conservative**: an unrecognised model is reported
   unsupported rather than assumed capable. Guessing "probably multimodal" turns
   a missing capability into a run-ending provider error.
2. :data:`VISION_UNSUPPORTED_MEDIA_TYPE` - a model can be multimodal and still
   reject the format. Sending a ``.tiff`` to a model that accepts only png/jpeg
   is the same class of hard error.
3. :data:`VISION_TOO_LARGE` - **a 50 MB screenshot must not reach the
   provider.** The bound is enforced on the DECODED bytes before encoding, and
   the receipt reports the cap that fired and the size that was refused.
4. :data:`VISION_TOO_MANY` - the count bound. Twenty images in one turn is
   ~20x the prompt cost of one, and the failure mode is a request the provider
   rejects for size with an error that names nothing useful.

Where the P0 sanitiser does and does not apply
----------------------------------------------
This is the part that is genuinely subtle, and the split is not negotiable:

* **Image BYTES are not text.** They are not scanned, not redacted, and not
  pattern-matched. Running a secret regex over a PNG's bytes is meaningless at
  best and quadratic at worst. They are base64'd and sent, and that is all.
* **The METADATA and the PATH are text, and they DO go through the boundary.**
  ``path``, ``media_type``, ``bytes``, ``revision.sha256`` and any model-facing
  caption are ordinary strings that can carry a credential - a repository path
  can be ``/home/alice/.aws/credentials.png``, and a filename can be an API key.
  So :func:`vision_blocks` routes every text field it emits through
  :func:`harness.redaction.redact_text_for_journal`, the module P0-W1-T1
  established as the ONE fail-closed journal boundary, and P0-W2-T1 pinned as
  one of only two legal call sites.

The capability gate, and the T3 request
----------------------------------------
Family resolution delegates to :func:`runtime.model_capabilities.effort_family_for`
rather than re-deriving a family from a model-name substring. That is the whole
point of T3's function: an ``openai``-compatible gateway fronts Anthropic and
Google models too, so the PROVIDER name cannot decide whether a block is
allowed - only the MODEL's family can. **T3 request:** ``vision`` needs a
``supports_vision`` field on ``ModelCapability`` and a public
``vision_family_for()``. Reading the private ``_MODEL_FAMILY_HINTS`` /
``_EFFORT_KNOBS`` tables from here would repeat the private-name coupling that
P0-W2-T1 recorded for ``shared.security``; this module therefore keeps its own
narrow, declared family table and delegates the RESOLUTION, so the coupling is
one documented import rather than three private reads. See the handoff.

Config
------
Four keys, read by KEY MEANING, with bounded internal defaults, and deliberately
NOT in :data:`harness.config.DEFAULTS` beyond what is already there - a default
in ``DEFAULTS`` is merged into every task and every eval arm, and "this run can
look at pictures" is not a fact that should be switched on project-wide by
adding a line. ``harness/config.py`` publishes ``vision_enabled: None`` for
discoverability only; ``None`` means "use the module default", which is ON.
"""

from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .redaction import redact_text_for_journal

__all__ = [
    "DEFAULT_MAX_IMAGES_PER_TURN",
    "DEFAULT_MAX_IMAGE_BYTES",
    "MEDIA_TYPES",
    "TEXT_ONLY_FAMILIES",
    "VISION_BUDGET",
    "VISION_STATUSES",
    "VISION_TOO_LARGE",
    "VISION_TOO_MANY",
    "VISION_UNSUPPORTED_MEDIA_TYPE",
    "VISION_UNSUPPORTED_MODEL",
    "VisionBlock",
    "VisionPlan",
    "vision_blocks",
    "vision_families",
    "vision_plan_for",
]


# -- statuses ---------------------------------------------------------------
# A closed vocabulary, for the same reason `REFUSAL_REASONS` and `EFFORT_*`
# are closed sets: a caller matches on a slug instead of parsing prose, so
# adding a state later cannot silently change an existing one's meaning.

VISION_SENT = "sent"
VISION_UNSUPPORTED_MODEL = "unsupported_model"
VISION_UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
VISION_TOO_LARGE = "too_large"
VISION_TOO_MANY = "too_many"
VISION_DISABLED = "disabled"
VISION_NO_DATA = "no_image_data"
VISION_FAMILY_UNKNOWN = "family_unknown"

VISION_STATUSES: Tuple[str, ...] = (
    VISION_SENT,
    VISION_UNSUPPORTED_MODEL,
    VISION_UNSUPPORTED_MEDIA_TYPE,
    VISION_TOO_LARGE,
    VISION_TOO_MANY,
    VISION_DISABLED,
    VISION_NO_DATA,
    VISION_FAMILY_UNKNOWN,
)

# -- bounds -----------------------------------------------------------------

#: P1/W2-T1: one image's decoded byte ceiling. A 50 MB screenshot is ~66 MB of
#: base64 in the request, which is a provider 400 and a context-window event;
#: neither is a useful error message for a user who attached a screenshot.
DEFAULT_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MiB

#: How many images one turn may carry. The prompt cost is linear in this and
#: the provider's own limit is not something to discover by exceeding it.
DEFAULT_MAX_IMAGES_PER_TURN = 4

#: Media types we will encode. Deliberately the formats every mainstream
#: multimodal API accepts; anything else is REFUSED BY NAME rather than sent
#: and rejected by the provider.
MEDIA_TYPES: Tuple[str, ...] = (
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
)

#: Per-image character budget. A base64 payload costs ~4/3 of its bytes, so a
#: 5 MiB image is ~6.7M characters of request. This is the value the receipt
#: reports, so a reader can see the request's own size rather than trusting it.
VISION_BUDGET: Dict[str, Any] = {
    "max_image_bytes": DEFAULT_MAX_IMAGE_BYTES,
    "max_images_per_turn": DEFAULT_MAX_IMAGES_PER_TURN,
}


def vision_families() -> Dict[str, bool]:
    """Which provider families are known to accept a multimodal block.

    Deliberately a **narrow declared set**, not a guess. The cost of omitting a
    capable family is one honest ``unsupported_model`` and a user who names a
    different model; the cost of including an incapable one is a hard provider
    400 that kills the turn. This is the repo's standing ``unpriced`` /
    ``unsupported_level`` discipline applied to a capability instead of a price.
    """
    return {"anthropic": True, "google": True, "openai": True}


#: Families we RECOGNISE as text-only. These produce
#: :data:`VISION_UNSUPPORTED_MODEL` - a positive statement that the model cannot
#: see. Declared separately from :func:`vision_families` on purpose: an
#: unrecognised model is :data:`VISION_FAMILY_UNKNOWN` ("we do not know"), and
#: a recognised text-only model is :data:`VISION_UNSUPPORTED_MODEL` ("we know,
#: and the answer is no"). Collapsing the two would make an honest "we don't
#: know" indistinguishable from a confident "it cannot", which is the same
#: confusion this whole module exists to remove - and both fail closed, so the
#: distinction costs nothing in safety and everything in honesty.
TEXT_ONLY_FAMILIES: Tuple[str, ...] = ("llama", "mistral", "qwen", "deepseek")

#: Name fragments that positively identify a text-only family.
#:
#: **This is the ONE place in the module that reads a model-name substring, and
#: it is one-directional by construction.** A hit here can only ADD a refusal; it
#: can never grant capability, never select a content-block shape, and never
#: turn a refusal into a send. That asymmetry is the whole reason it is safe to
#: have: a false positive costs one honest ``unsupported_model`` line, while a
#: false negative in the opposite direction - a name rule that PROVED
#: multimodality - would be the unknowable-unknown class this module exists to
#: prevent.
#:
#: It exists because :func:`runtime.model_capabilities.effort_family_for`
#: answers a *different* question: "which EFFORT KNOB applies", and only three
#: families have one. So it returns ``""`` for a Llama model even though we
#: know perfectly well that it is text-only. Reusing it for family resolution is
#: still correct for the three families it knows (an ``openai``-named gateway
#: fronts Claude models, so only the MODEL decides); it simply cannot answer for
#: the rest. Filed to T3 as the ``vision_family_for()`` request - see the
#: handoff - at which point this table can be deleted.
_TEXT_ONLY_HINTS: Tuple[str, ...] = TEXT_ONLY_FAMILIES


def _family_for(model: Optional[str], provider: Optional[str] = None) -> str:
    """Resolve the model FAMILY that decides the content-block shape.

    Delegates to :func:`runtime.model_capabilities.effort_family_for`, which is
    T3's authority for "which family does this MODEL belong to" and which
    already handles the case the naive version gets wrong: an ``openai``-named
    gateway serving a Claude model.

    Falls back to an empty string when the module is unavailable or unrecognised,
    which the caller reports as :data:`VISION_FAMILY_UNKNOWN` rather than
    guessing. The harness must stay importable with no ``runtime`` package
    present, so the import is guarded.
    """
    try:
        from runtime.model_capabilities import effort_family_for
    except Exception:  # pragma: no cover - import guard, not a logic branch
        return ""
    try:
        return str(effort_family_for(model, provider) or "")
    except Exception:
        return ""


def _text_only_family(model: Optional[str], provider: Optional[str] = None) -> str:
    """Return a recognised text-only family for this model, or ``""``.

    **One-directional: a hit can only ever cause a refusal.** It never grants
    capability and never selects a block shape - see :data:`_TEXT_ONLY_HINTS` for
    why that asymmetry is what makes a name rule acceptable here.

    A provider-qualified id wins over a bare name fragment, exactly as T3's
    resolver does, so ``together/meta-llama/Llama-3-8b`` is recognised even
    though the head segment is a gateway.
    """
    name = str(model or "").strip().lower()
    declared = str(provider or "").strip().lower()
    if "/" in name:
        head = name.split("/", 1)[0]
        if head in TEXT_ONLY_FAMILIES:
            return head
    for fragment in _TEXT_ONLY_HINTS:
        if fragment in name:
            return fragment
    if declared in TEXT_ONLY_FAMILIES:
        return declared
    return ""


@dataclass(frozen=True)
class VisionBlock:
    """One image, as a provider-shaped multimodal content block."""

    #: ``image_url`` (OpenAI-compatible + Google passthrough) or ``image`` (Anthropic).
    kind: str
    payload: Dict[str, Any]
    media_type: str
    path: str
    size_bytes: int
    family: str

    def to_dict(self) -> Dict[str, Any]:
        """The content block, exactly as it goes into a message's ``content`` list."""
        return dict(self.payload)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # NEVER let base64 reach a repr: a traceback that prints a block would
        # put 6.7M characters into a log line, and an exception is not a place
        # a payload belongs.
        return (
            f"VisionBlock(kind={self.kind!r}, media_type={self.media_type!r}, "
            f"path={self.path!r}, size_bytes={self.size_bytes}, "
            f"family={self.family!r}, payload=<{len(self.payload)} keys>)"
        )


@dataclass(frozen=True)
class VisionPlan:
    """The honest answer to "will a vision block be sent, and what did it cost?".

    Field-for-field the same shape as :class:`runtime.model_capabilities.EffortPlan`,
    on purpose. Both answer "what was actually sent?", both carry a closed
    status vocabulary, and both make ``sent`` the ONLY status that produces a
    request. A reader who has learned to read one has learned to read the other.
    """

    status: str
    model: str = ""
    provider: str = ""
    family: str = ""
    detail: str = ""
    blocks: Tuple[VisionBlock, ...] = field(default_factory=tuple)
    refused: Tuple[str, ...] = field(default_factory=tuple)
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES
    max_images_per_turn: int = DEFAULT_MAX_IMAGES_PER_TURN
    total_bytes: int = 0

    @property
    def sent(self) -> bool:
        """Whether a real multimodal block is on the request."""
        return self.status == VISION_SENT

    @property
    def blocks_payload(self) -> List[Dict[str, Any]]:
        """The content blocks, or an empty list when none will be sent.

        An empty list on every non-``sent`` status is the load-bearing property:
        a caller that forgets to check :attr:`sent` gets no image rather than an
        image the provider will reject.
        """
        return [block.to_dict() for block in self.blocks]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "vision_status": self.status,
            "vision_sent": self.sent,
            "vision_model": self.model,
            "vision_provider": self.provider,
            "vision_family": self.family,
            "vision_detail": self.detail,
            "vision_blocks": len(self.blocks),
            "vision_refused": list(self.refused),
            "vision_max_image_bytes": int(self.max_image_bytes),
            "vision_max_images_per_turn": int(self.max_images_per_turn),
            "vision_total_bytes": int(self.total_bytes),
        }


def _text(value: Any) -> str:
    """Route a text field through the ONE fail-closed journal boundary.

    Image bytes are exempt (they are not text); the PATH and the METADATA are
    ordinary strings and can absolutely carry a credential - see the module
    docstring. This is the P0-W1-T1 boundary, not a second redactor.
    """
    return str(redact_text_for_journal(str(value or "")))


def _media_type_of(record: Mapping[str, Any]) -> str:
    """Resolve the media type from the record, trusting the record first."""
    declared = str(record.get("media_type") or "").strip().lower()
    if declared:
        return declared
    guessed, _enc = mimetypes.guess_type(str(record.get("path") or ""))
    return str(guessed or "").strip().lower()


def _decode(data_base64: str) -> bytes:
    return base64.b64decode(str(data_base64), validate=False)


def _block_for(
    family: str, media_type: str, payload_b64: str, path: str
) -> Dict[str, Any]:
    """Build the family-native content block.

    Each shape is the provider's OWN, not an approximation of it. A block that
    is structurally wrong is rejected by the provider before it reaches the
    model, which is the same hard-error class the capability gate exists to
    avoid, so the shapes are exact.
    """
    data_url = f"data:{media_type};base64,{payload_b64}"
    if family == "anthropic":
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": payload_b64},
        }
    # OpenAI-compatible and Google's OpenAI-compat passthrough share one shape.
    return {"type": "image_url", "image_url": {"url": data_url}}


def vision_plan_for(
    records: Sequence[Mapping[str, Any]],
    *,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    enabled: bool = True,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    max_images_per_turn: int = DEFAULT_MAX_IMAGES_PER_TURN,
) -> VisionPlan:
    """Turn ``image`` tool results into multimodal blocks, or refuse honestly.

    ``records`` are the dicts an ``image`` tool handler returned - the shape
    :meth:`execution.workspace.Workspace.read_image` produces. Each is checked
    independently and the plan reports BOTH what was sent and what was refused,
    because a turn that carried three readable screenshots and one 50 MB
    screenshot is a useful outcome and a silent whole-turn refusal is not.

    Never raises. Every rejection is a status on the returned plan.
    """
    rows = [r for r in (records or []) if isinstance(r, Mapping)]
    try:
        byte_cap = max(1, int(max_image_bytes))
    except (TypeError, ValueError):
        byte_cap = DEFAULT_MAX_IMAGE_BYTES
    try:
        count_cap = max(1, int(max_images_per_turn))
    except (TypeError, ValueError):
        count_cap = DEFAULT_MAX_IMAGES_PER_TURN

    if not enabled:
        return VisionPlan(
            status=VISION_DISABLED,
            model=str(model or ""),
            provider=str(provider or ""),
            detail="vision is disabled for this run (vision_enabled is false)",
            max_image_bytes=byte_cap,
            max_images_per_turn=count_cap,
        )

    family = _family_for(model, provider)
    base = {
        "model": str(model or ""),
        "provider": str(provider or ""),
        "family": family,
        "max_image_bytes": byte_cap,
        "max_images_per_turn": count_cap,
    }
    # The Phase-1 capability gate is MANDATORY. T3's declared
    # ``supports_vision`` is checked FIRST: an authoritative ``False`` refuses
    # even when the family heuristic would have allowed it (a declared fact
    # outranks a name guess), and an authoritative ``True`` lets a
    # model-without-a-family-hint through only when the family resolver can
    # still give it a block shape - otherwise it is family_unknown. An
    # undeclared ``None`` FALLS THROUGH to the family gate rather than
    # guessing; an unknown capability is not a licence to send.
    try:
        from runtime.model_capabilities import vision_capability_of

        _sv, _sv_source = vision_capability_of(provider, model)
    except Exception:  # pragma: no cover - defend import/attr
        _sv, _sv_source = None, "unavailable"
    if _sv is False:
        return VisionPlan(
            status=VISION_UNSUPPORTED_MODEL,
            detail=(
                f"{base['model'] or 'this run'!r} is declared "
                f"supports_vision=False ({_sv_source}); a vision block was NOT "
                f"sent and nothing was requested. Use a text description, or "
                f"pin a multimodal model."
            ),
            **base,
        )
    if _sv is True and family not in vision_families():
        # Declared capable, but we have no recognised block shape for it.
        return VisionPlan(
            status=VISION_FAMILY_UNKNOWN,
            detail=(
                f"{model or 'this run'!r} declares supports_vision=True but "
                f"has no recognised multimodal family (family={family or 'unrecognised'}); "
                f"a vision block was NOT sent. Pin a known multimodal model."
            ),
            **base,
        )
    if family not in vision_families():
        # Fail CLOSED. An unrecognised model is reported unsupported rather than
        # assumed capable: guessing costs a provider 400 that kills the turn,
        # while refusing costs one honest line the user can act on.
        #
        # The two refusals are kept apart because they mean different things to
        # the person reading them. A recognised text-only family is
        # `unsupported_model`; an unrecognised model is `family_unknown`. Both
        # send nothing - the distinction is about what we KNOW, not what we do.
        text_only = _text_only_family(base["model"], provider)
        if text_only:
            return VisionPlan(
                status=VISION_UNSUPPORTED_MODEL,
                detail=(
                    f"{base['model'] or 'this run'!r} resolves to the "
                    f"{text_only!r} family, which is text-only; a vision block "
                    f"was NOT sent and nothing was requested. Use a text "
                    f"description, or pin a multimodal model."
                ),
                **base,
            )
        return VisionPlan(
            status=VISION_FAMILY_UNKNOWN,
            detail=(
                f"{model or 'this run'!r} has no recognised multimodal family "
                f"(family={family or 'unrecognised'}); a vision block was NOT "
                f"sent and nothing was requested. This is a refusal to guess, "
                f"not a claim that the model is text-only."
            ),
            **base,
        )

    if not rows:
        return VisionPlan(
            status=VISION_NO_DATA,
            detail="no image records were supplied",
            **base,
        )

    if len(rows) > count_cap:
        return VisionPlan(
            status=VISION_TOO_MANY,
            detail=(
                f"{len(rows)} image(s) in one turn exceeds the cap of "
                f"{count_cap}; NOTHING was sent. Reference the images "
                f"individually."
            ),
            refused=tuple(_text(r.get("path")) for r in rows),
            **base,
        )

    blocks: List[VisionBlock] = []
    refused: List[str] = []
    total = 0
    first_detail = ""
    status = VISION_SENT

    for record in rows:
        path = _text(record.get("path"))
        media = _media_type_of(record)
        if media not in MEDIA_TYPES:
            refused.append(path)
            if not first_detail:
                first_detail = (
                    f"{media or 'unrecognised type'} for {path!r} is not one of "
                    f"{', '.join(MEDIA_TYPES)}; that image was NOT sent."
                )
            status = VISION_UNSUPPORTED_MEDIA_TYPE
            continue
        payload = str(record.get("data_base64") or "")
        if not payload:
            refused.append(path)
            if not first_detail:
                first_detail = (
                    f"{path!r} carried no image data (include_data was false or "
                    f"the read returned nothing); it was NOT sent."
                )
            status = VISION_NO_DATA
            continue
        try:
            raw = _decode(payload)
        except Exception:
            refused.append(path)
            if not first_detail:
                first_detail = f"{path!r} carried undecodable image data; NOT sent."
            status = VISION_UNSUPPORTED_MEDIA_TYPE
            continue
        # The bound is on the DECODED bytes, checked BEFORE the payload is put
        # into a request. A 50 MB screenshot must never become 66 MB of base64
        # in an outgoing message, even transiently.
        if len(raw) > byte_cap:
            refused.append(path)
            if not first_detail:
                first_detail = (
                    f"{path!r} is {len(raw)} bytes, over the {byte_cap}-byte "
                    f"vision cap; it was NOT sent. Downscale it or pin "
                    f"vision_max_image_bytes."
                )
            status = VISION_TOO_LARGE
            continue
        total += len(raw)
        blocks.append(
            VisionBlock(
                kind=("image" if family == "anthropic" else "image_url"),
                payload=_block_for(family, media, payload, path),
                media_type=media,
                path=path,
                size_bytes=len(raw),
                family=family,
            )
        )

    if not blocks:
        # Every row was refused. Reporting `sent` with zero blocks would be the
        # exact "partial list that reads as complete" shape this round exists to
        # stop, so the status is the FIRST refusal and the detail names it.
        return VisionPlan(
            status=status if status != VISION_SENT else VISION_NO_DATA,
            detail=first_detail or "no image could be encoded for this request",
            refused=tuple(refused),
            total_bytes=total,
            **base,
        )
    return VisionPlan(
        status=VISION_SENT,
        detail=first_detail,
        blocks=tuple(blocks),
        refused=tuple(refused),
        total_bytes=total,
        **base,
    )


def vision_blocks(
    records: Sequence[Mapping[str, Any]], **kwargs: Any
) -> List[Dict[str, Any]]:
    """The content blocks to splice into a message, or ``[]``.

    The thin convenience over :func:`vision_plan_for` for the call site that
    only wants the payload. It is NOT the reporting surface: a caller that needs
    to know WHY there are no blocks must call :func:`vision_plan_for` and read
    ``status``, because an empty list is indistinguishable from "the model has
    no image" and that is precisely the confusion this round removes.
    """
    return vision_plan_for(records, **kwargs).blocks_payload
