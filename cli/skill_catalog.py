"""``/skills`` and ``/agents`` as verbs, and the keyboard list they open.

Two surfaces, one module, no Textual import: the KEYBOARD-DRIVEN skill list
and the agent cost roster. Both are pure state machines over data the rest of
the product already owns.

Why a list and not a filter box
-------------------------------

The reference interaction is right, and the reason is the substance rather
than the ergonomics: **a user managing skills is managing context budget**.
Every extra enabled skill adds its name and description to every model
request, forever, whether or not it is relevant. So the questions a person
actually has are "which of these am I paying for", "what does each one cost",
and "which do I want to stop paying for" - none of which a filter box
answers. This list answers all three on one screen:

* ``t`` sorts by token count, so the expensive ones float to the top rather
  than requiring a person to know which names are expensive;
* ``Space`` cycles a skill's VISIBILITY to the model, which is the cheap
  reversible action (a hidden skill is not discovered at all, so it costs
  nothing and can be re-shown in one keystroke);
* ``Enter`` saves, ``d`` disables (removes the file from discovery
  entirely), ``e`` re-enables.

The distinction between the three actions is deliberate and is the reason this
is not one "toggle": hiding is a session/preference-level act and is
reversible with no file write; disabling is a filesystem act that makes the
skill undiscoverable to every surface. A UI with one toggle for both cannot
say which one it did.

What this module does NOT do
----------------------------

* It does not import ``textual``. Every function here is pure or file-backed,
  so the whole surface is testable on a host with no terminal - which is what
  makes the keyboard proofs possible at all.
* It does not emit markup. Every line is PLAIN text and the two sanctioned
  exits are :func:`escape_lines` (rich's own escaper, so it cannot drift from
  the parser) and :func:`safe_lines` (``rich.text.Text``, which has no markup
  interpretation at all - the structural answer).
* It does not grant anything. A skill's declared tools are intersected with
  the ROLE PROFILE through :mod:`extensions.skill_policy`; declaring a tool
  is data, and the intersection is the only thing that is ever reported as
  available.

The receipt rules, which are not optional
------------------------------------------

* A receipt that reports a delivered skill must be the receipt derived from
  the bundle that was ACTUALLY injected. This module never re-scans to
  produce a delivery claim.
* ``(none matched)`` is a CONTRACT (``harness.skills.NONE_MATCHED``), not a
  string. A surface that checked only for a section's presence would claim a
  delivered skill for every run in every repository, so
  :func:`render_skill_receipt` reads the same constant and renders the
  placeholder honestly.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from harness import skills as skill_mod
from harness.skills import (
    NONE_MATCHED,
    attach_declarations,
    build_skill_receipt,
    command_sources,
    discover_skills,
    scan_skills_for_task,
    skill_catalog,
    skill_token_cost,
    supporting_files,
)

__all__ = [
    "AGENT_VERBS",
    "LIST_WIDGET_ID",
    "SKILL_KEYS",
    "SKILL_VERBS",
    "SKILL_VISIBILITIES",
    "VISIBILITY_STORE_VERSION",
    "AgentRoster",
    "SkillBrowser",
    "agents_command",
    "escape_lines",
    "load_visibility",
    "render_agent_lines",
    "render_skill_list_lines",
    "render_skill_receipt",
    "safe_lines",
    "save_visibility",
    "skill_receipt_for",
    "skills_command",
    "visibility_store_path",
]


#: The widget a shell mounts for the keyboard list. Declared beside the state
#: machine it renders so the two cannot drift, and named so a mount can find
#: it without reading this module.
LIST_WIDGET_ID = "neo-skills-list"

#: The closed vocabulary of the keyboard list's keys. A shell with an extra
#: binding must not crash the list, and a surface that binds a key the state
#: machine does not answer has a key that does nothing - so the set is
#: declared here and :meth:`SkillBrowser.key` returns ``"ignored"`` for
#: anything else rather than raising.
SKILL_KEYS: Tuple[str, ...] = (
    "up",
    "down",
    "t",
    "space",
    "enter",
    "d",
    "e",
    "escape",
    "q",
)

#: The visibility vocabulary, the store version and the read bound are owned by
#: `harness.skills` (assigned below, with the rest of the store) because the
#: ENFORCING reader is the scan path and `harness` may not import `cli`. A
#: second table here would be a second answer to "which words are legal".
#:
#: ``visible`` is the ordinary case. ``hidden`` is the reversible one: the
#: skill is not discovered, so it costs no context and nothing was written to
#: disk. It is deliberately NOT the same word as "disabled", because disabling
#: moves a file and hiding does not, and a receipt using one word for both
#: would describe an act the product did not perform.

_UNSAFE = re.compile(r"[^A-Za-z0-9._:-]+")


# ---------------------------------------------------------------------------
# Markup safety - the two sanctioned exits
# ---------------------------------------------------------------------------


def escape_lines(lines: Iterable[Any]) -> List[str]:
    """Escape each line for a markup renderer.

    Bracket escaping is delegated to ``rich.markup.escape`` when rich is
    importable, so the escaping is the RENDERER'S OWN and cannot drift from
    it. Escaping is not dropping: a skill named ``[bold]evil`` stays visible.
    """
    out: List[str] = []
    try:
        from rich.markup import escape as _rich_escape

        for line in lines or ():
            out.append(_rich_escape("" if line is None else str(line)))
        return out
    except Exception:  # pragma: no cover - a path with no rich
        for line in lines or ():
            out.append(str(line).replace("[", r"\["))
        return out


def safe_lines(lines: Iterable[Any]) -> List[Any]:
    """Return ``rich.text.Text`` objects, which no markup parser can eat.

    The structural answer and the one a shell should prefer: a ``Text`` has no
    markup interpretation at all. Falls back to escaped strings when rich is
    unavailable, so this helper never itself becomes the crash.
    """
    plain = ["" if line is None else str(line) for line in (lines or ())]
    try:
        from rich.text import Text

        return [Text(line) for line in plain]
    except Exception:  # pragma: no cover - a path with no rich
        return escape_lines(plain)


# ---------------------------------------------------------------------------
# Visibility store (owned by harness.skills - see the alias block below)
# ---------------------------------------------------------------------------


#: The visibility store is owned by `harness.skills`, not reimplemented here.
#: It lives there because `harness` may not import `cli` - so a store that
#: lived in `cli` could not be read by the scan path that has to ENFORCE the
#: ceiling. `harness.skills` can read it, and this module writes it.
load_visibility = skill_mod.load_visibility
visibility_store_path = skill_mod.visibility_store_path
SKILL_VISIBILITIES = skill_mod.SKILL_VISIBILITIES
VISIBILITY_STORE_VERSION = skill_mod.VISIBILITY_STORE_VERSION
MAX_STORED_VISIBILITIES = skill_mod.MAX_STORED_VISIBILITIES


def save_visibility(
    hidden: Iterable[str], repo_path: Any = None, *, home: Any = None
) -> Tuple[bool, str]:
    """Write the visibility document atomically. Returns ``(written, note)``.

    Values are FILTERED through :data:`SKILL_VISIBILITIES` here, not only on
    read, so the file on disk can never hold a state the reader would refuse.
    A filtered value is REPORTED in the note rather than dropped silently: a
    save that quietly discarded half its input looks exactly like a save that
    worked, which is the same defect class as a verification that looks
    passed and was not.

    A write failure is likewise reported - a preference that looks saved and
    was not is a different defect with the same shape.

    An EMPTY list is a valid, writable document: unhiding the last skill is a
    state the store has to be able to reach. Refusing to write it (because
    "there is nothing to hide") made visibility a one-way door - every save
    that removed a name was silently discarded, so a skill a user re-enabled
    came back hidden on the next session with no error anywhere.

    A save that consisted ONLY of refused values still writes nothing, and
    says why: that is a rejected input, not a request to hide nothing, and
    writing an empty document for it would report a refusal as a success.
    """
    clean: List[str] = []
    refused: List[str] = []
    for item in hidden or ():
        name = str(item or "").strip()
        if not name:
            continue
        if name in SKILL_VISIBILITIES:
            refused.append(name)
            continue
        clean.append(name)
    notes: List[str] = []
    if refused:
        notes.append("unusable value refused for: " + ", ".join(sorted(refused)))
    if refused and not clean:
        return (False, "; ".join(notes))
    target = visibility_store_path(repo_path, home=home)
    document = {
        "version": VISIBILITY_STORE_VERSION,
        "hidden": sorted(dict.fromkeys(clean)),
    }
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    temp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except OSError as exc:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:  # pragma: no cover - defensive
            pass
        return (False, f"could not write: {exc.__class__.__name__}")
    return (True, "; ".join(notes))


# ---------------------------------------------------------------------------
# The keyboard list
# ---------------------------------------------------------------------------


@dataclass
class SkillBrowser:
    """The keyboard-driven skill list: a state machine, not a dialog.

    Built from :func:`harness.skills.skill_catalog`, so every row already
    carries its origin, its merged-catalog kind, and its THREE token costs.
    Sorting by ``catalog_tokens`` is therefore meaningful on first press -
    the number a person is actually managing is the one they can sort by.

    Nothing here writes to disk except an explicit ``save()``. Every other
    key mutates in-memory state and reports what it did, so a surface can
    drive it without owning persistence.
    """

    rows: List[Dict[str, Any]] = field(default_factory=list)
    hidden: List[str] = field(default_factory=list)
    repo_path: Any = None
    sort_by: str = "name"
    cursor: int = 0
    message: str = ""
    dirty: bool = False
    receipt: Dict[str, Any] = field(default_factory=dict)

    # -- construction

    @classmethod
    def open(
        cls,
        repo_path: Any = None,
        *,
        extra_roots: Optional[Sequence[str]] = None,
        receipt: Optional[Mapping[str, Any]] = None,
        home: Any = None,
    ) -> "SkillBrowser":
        """Build the list, applying the persisted visibility ceiling.

        The ceiling is applied by DISCOVERY, not by row filtering: a hidden
        skill is not in the catalog at all, so this list cannot show a skill
        the model would not receive. It also carries them as a separate list
        so a person can see and undo what they hid.
        """
        hidden, note = load_visibility(repo_path, home=home)
        catalog = skill_catalog(repo_path, extra_roots=list(extra_roots or ()))
        browser = cls(
            rows=[dict(row) for row in catalog["rows"]],
            hidden=sorted(hidden),
            repo_path=repo_path,
            receipt=dict(receipt or {}),
        )
        browser.message = note or ""
        return browser

    # -- ordering

    def _sort_key(self, row: Mapping[str, Any]) -> tuple:
        if self.sort_by == "tokens":
            # Descending preloaded cost, then name for stability. Sorting by
            # CATALOG cost would rank nearly every skill equally, which is
            # the "the sort did nothing" failure.
            return (
                -int(row.get("preloaded_tokens", row.get("body_tokens", 0))),
                str(row.get("name", "")),
            )
        return (str(row.get("name", "")),)

    def sorted_rows(self) -> List[Dict[str, Any]]:
        """Return the rows in the CURRENT sort order."""
        return sorted((dict(row) for row in self.rows), key=self._sort_key)

    # -- mutation

    def move(self, delta: int) -> str:
        """Move the cursor, clamped to the list."""
        rows = self.sorted_rows()
        if not rows:
            return "empty"
        self.cursor = max(0, min(len(rows) - 1, self.cursor + int(delta or 0)))
        return "moved"

    def selected(self) -> Optional[Dict[str, Any]]:
        """Return the highlighted row, or ``None`` for an empty list."""
        rows = self.sorted_rows()
        if not rows:
            return None
        return rows[max(0, min(len(rows) - 1, self.cursor))]

    def toggle_sort(self) -> str:
        """Toggle the ``t`` sort between name order and token cost."""
        self.sort_by = "tokens" if self.sort_by == "name" else "name"
        self.cursor = 0
        self.message = (
            "sorted by token cost (largest first)"
            if self.sort_by == "tokens"
            else "sorted by name"
        )
        return "sorted"

    def cycle_visibility(self) -> str:
        """``Space``: cycle the selected skill's visibility to the model.

        Returns the NEW visibility word. A row whose kind is ``command_file``
        is refused with a reason rather than silently doing nothing: a flat
        markdown template has no skill directory to hide, and a keystroke
        that appears to work while changing nothing is worse than a refusal.
        """
        row = self.selected()
        if row is None:
            self.message = "nothing to hide"
            return "ignored"
        name = str(row.get("qualified_name") or row.get("name") or "")
        if str(row.get("kind")) == "command_file":
            self.message = (
                f"{name} is a flat command file; hide the skill with the same name, "
                "or remove the file"
            )
            return "refused"
        if name in self.hidden:
            self.hidden = [item for item in self.hidden if item != name]
            self.message = f"{name} is visible to the model again (unsaved)"
        else:
            self.hidden = sorted({*self.hidden, name})
            self.message = f"{name} is hidden from the model (unsaved)"
        self.dirty = True
        return "visibility"

    def disable_selected(self) -> str:
        """``d``: DISABLE the selected skill, through the one owner of the move.

        This key ACTS. The earlier version only recorded the intent and
        returned ``"disable <name>"`` for a caller to perform, which is a
        keystroke that looks like it did something and did not - the same
        defect class as a verification that renders `pass`. The file move
        itself still belongs to ``cli.plugins.disable_skill``: re-implementing
        the rename here is how two implementations of one behaviour appear.

        A skill whose ORIGIN is a plugin is refused with the reason, because a
        plugin's skills are switched off by the plugin's own marker, not by
        renaming a file inside a bundle.
        """
        row = self.selected()
        if row is None:
            self.message = "nothing to disable"
            return "ignored"
        name = str(row.get("name") or "")
        if str(row.get("kind")) == "command_file":
            self.message = (
                f"{name} is a flat command file; remove it rather than disabling it"
            )
            return "refused"
        origin = str(row.get("origin") or "")
        if origin and origin not in _MOVABLE_TIERS:
            self.message = (
                f"{name} comes from the {origin} origin; disable that instead of "
                "renaming a file this surface does not own"
            )
            return "refused"
        tier = _tier_for(name, row, self.repo_path)
        try:
            from cli import plugins

            plugins.disable_skill(name, tier=tier, repo_path=str(self.repo_path or ""))
        except Exception as exc:
            self.message = f"could not disable {name}: {exc.__class__.__name__}"
            return "refused"
        self.message = f"disabled {name} ({tier})"
        return "disabled"

    def enable_selected(self) -> str:
        """``e``: ENABLE the selected skill. Acts, like ``d``.

        A disabled skill is by definition absent from the catalogue, so the
        tier is resolved from disk through :func:`_tier_for` - the same helper
        ``skills_command("enable", ...)`` uses. Falling back to a fixed tier
        here would make the keystroke refuse exactly the skill that needs it.
        """
        row = self.selected()
        if row is None:
            self.message = "nothing to enable"
            return "ignored"
        name = str(row.get("name") or "")
        origin = str(row.get("origin") or "")
        if origin and origin not in _MOVABLE_TIERS:
            self.message = f"{name} comes from the {origin} origin; enable that instead"
            return "refused"
        tier = _tier_for(name, row, self.repo_path)
        try:
            from cli import plugins

            plugins.enable_skill(name, tier=tier, repo_path=str(self.repo_path or ""))
        except Exception as exc:
            self.message = f"could not enable {name}: {exc.__class__.__name__}"
            return "refused"
        self.message = f"enabled {name} ({tier})"
        return "enabled"

    def save(self) -> str:
        """``Enter``: persist the visibility changes."""
        if not self.dirty:
            self.message = "no changes to save"
            return "unchanged"
        wrote, note = save_visibility(self.hidden, self.repo_path)
        if not wrote:
            self.message = f"not saved: {note}"
            return "refused"
        self.dirty = False
        self.message = f"saved {len(self.hidden)} hidden skill(s)"
        return "saved"

    def close(self) -> str:
        """``escape``/``q``: leave the list. Any unsaved change is REPORTED."""
        self.message = (
            "closed with unsaved visibility changes" if self.dirty else "closed"
        )
        return "closed"

    def key(self, name: Any) -> str:
        """Answer one keystroke. Never raises.

        An unrecognised key returns ``"ignored"``: a surface with an extra
        binding must not crash the list it is decorating.
        """
        raw = str(name or "").strip().lower()
        # A literal space is the Space key. It cannot survive the strip above,
        # so it is recovered from the UNSTRIPPED name - otherwise the one key
        # that cycles visibility would be the one key the state machine
        # silently ignored.
        if str(name or "") == " " or raw == "space":
            return self.cycle_visibility()
        if raw == "up" or raw == "k":
            return self.move(-1)
        if raw == "down" or raw == "j":
            return self.move(1)
        if raw == "t":
            return self.toggle_sort()
        if raw == "enter":
            return self.save()
        if raw == "d":
            return self.disable_selected()
        if raw == "e":
            return self.enable_selected()
        if raw in ("escape", "q"):
            return self.close()
        return "ignored"

    # -- rendering

    def lines(self) -> List[str]:
        """Render the list as PLAIN lines. Never markup.

        The first line is a fixed heading authored here, so it can never open
        with ``[`` and be eaten by a markup parser. Every other line carries
        DATA (a skill name, an origin, a source path) and is the caller's to
        escape on the way out.
        """
        out: List[str] = ["skills - context budget"]
        report = dict(self.receipt.get("progressive_disclosure") or {})
        if report:
            out.append(
                "  catalogue: {skills} skill(s), {catalog} token(s) loaded at "
                "startup; {saved} token(s) saved by loading bodies on demand".format(
                    skills=int(report.get("skills", 0)),
                    catalog=int(report.get("catalog_tokens", 0)),
                    saved=int(report.get("saving_tokens", 0)),
                )
            )
        rows = self.sorted_rows()
        if not rows:
            out.append("  (no skills or commands discovered)")
        for index, row in enumerate(rows):
            marker = ">" if index == self.cursor else " "
            kind = str(row.get("kind") or "skill")
            origin = str(row.get("origin") or "?")
            name = str(row.get("qualified_name") or row.get("name") or "?")
            cost = (
                f"{int(row.get('preloaded_tokens', 0))} tok"
                if kind != "command_file"
                else "template"
            )
            support = int(row.get("supporting_files", 0) or 0)
            extra = f" +{support} file(s) on demand" if support else ""
            collision = (
                " [wins over a flat command file]" if row.get("wins_over") else ""
            )
            out.append(
                f"{marker} {index + 1:>2}. {name} ({kind}, {origin}) {cost}{extra}"
                f"{collision}"
            )
        if self.hidden:
            out.append("  hidden: " + ", ".join(self.hidden))
        out.append(
            "  keys: up/down move - t token sort - space visibility - enter save"
            " - d disable - e enable - esc close"
        )
        if self.message:
            out.append(f"  {self.message}")
        return out


def render_skill_list_lines(
    repo_path: Any = None,
    *,
    extra_roots: Optional[Sequence[str]] = None,
    receipt: Optional[Mapping[str, Any]] = None,
    home: Any = None,
) -> List[str]:
    """Render the no-argument ``/skills`` list. Pure; writes nothing."""
    return SkillBrowser.open(
        repo_path,
        extra_roots=extra_roots,
        receipt=receipt,
        home=home,
    ).lines()


# ---------------------------------------------------------------------------
# The skill receipt, rendered honestly
# ---------------------------------------------------------------------------


def render_skill_receipt(receipt: Optional[Mapping[str, Any]]) -> List[str]:
    """Render a skills receipt, honouring the ``(none matched)`` contract.

    The rule this exists to enforce: a receipt must not report a delivered
    skill for a run that delivered none. ``harness.skills.NONE_MATCHED`` is
    the authority and is read here rather than re-spelled, so a change to the
    placeholder cannot leave one surface reading the old word.

    Three cases, three answers:

    * an empty receipt (``{}``) says the run consulted no skills at all;
    * a receipt whose ``model_content`` is False renders the PLACEHOLDER
      verbatim plus the reason it was not matched - never a skill name;
    * a delivered receipt renders the names it says were rendered, and their
      declarations when the receipt carries them.
    """
    data = dict(receipt or {})
    if not data:
        return [
            "no skills receipt: this run compiled no skills section",
            f"  (an absent receipt means the scanner was not consulted, not that "
            f"nothing matched - that case reports {NONE_MATCHED!r})",
        ]
    lines: List[str] = []
    delivered = bool(data.get("model_content")) and bool(data.get("rendered") or [])
    names = [str(item) for item in (data.get("rendered") or data.get("matched") or [])]
    if delivered:
        lines.append("delivered to the model: " + ", ".join(names))
    else:
        # The placeholder IS the answer. Printing a skill name next to
        # "none matched" is the exact lie this function refuses.
        lines.append(f"delivered to the model: {NONE_MATCHED}")
        if data.get("skipped"):
            lines.append(f"  reason: {data['skipped']}")
    considered = int(data.get("considered", 0) or 0)
    lines.append(f"  considered: {considered}")
    section_chars = int(data.get("section_chars", 0) or 0)
    lines.append(f"  section chars: {section_chars}")
    declarations = dict(data.get("declarations") or {})
    if declarations:
        for name, record in sorted(declarations.items()):
            data_row = dict(record) if isinstance(record, Mapping) else {}
            tools = (
                ", ".join(str(item) for item in (data_row.get("tools") or ())) or "none"
            )
            tier = str(data_row.get("model_tier") or "")
            lines.append(
                f"  declaration {name}: v{data_row.get('version', 1)}"
                f" tier {tier or 'medium'} tools [{tools}] (declared, not granted)"
            )
    elif data.get("declarations_source"):
        lines.append(f"  declarations: none ({data['declarations_source']})")
    tainted = [str(item) for item in (data.get("tainted") or [])]
    if tainted:
        lines.append("  tainted: " + ", ".join(tainted))
    quarantined = [str(item) for item in (data.get("quarantined") or [])]
    if quarantined:
        lines.append("  quarantined: " + ", ".join(quarantined))
    if data.get("error"):
        lines.append(f"  error: {data['error']}")
    return lines


# ---------------------------------------------------------------------------
# /skills verbs
# ---------------------------------------------------------------------------

#: The verb vocabulary, declared once so the registry, the palette and this
#: dispatcher cannot disagree about what can be typed.
SKILL_VERBS: Tuple[str, ...] = ("list", "enable", "disable", "inspect", "create")

_USAGE = "usage: /skills " + " | ".join(SKILL_VERBS)

_NAME_OK = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def skill_receipt_for(
    repo_path: Any,
    issue_text: str = "",
    *,
    config: Optional[Mapping[str, Any]] = None,
    terms: Optional[Sequence[str]] = None,
    hidden_names: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Return the receipt a run WOULD carry, with declarations threaded on.

    Used by ``/skills inspect`` when no run journal is available, and by a
    surface that wants the receipt shape without running an agent.

    It is a SCAN receipt and never a delivery claim: ``model_content`` is
    DERIVED from the block the scan actually rendered, exactly as
    ``build_skill_receipt`` derives it for a real run. Forcing it True here was
    the same lie the receipt rules exist to prevent - `render_skill_receipt`
    reads `model_content` to decide whether to print "(none matched)", so a
    forced True told `/skills inspect` that a skill the model never received
    had been delivered. `provenance: "skills_command_scan"` is what tells a
    reader this is a projection.
    """
    settings = dict(config or {})
    scan = scan_skills_for_task(
        repo_path=str(repo_path or "."),
        issue_text=str(issue_text or ""),
        retrieval_terms=[str(item) for item in (terms or ())],
        extra_roots=[],
        max_skills=int(settings.get("skills_max", 3) or 3),
        max_chars=int(settings.get("skills_max_chars", 2500) or 2500),
        hidden_names=list(hidden_names or ()),
    )
    receipt = build_skill_receipt(scan, model_content=bool(scan.get("skills_block")))
    receipt["provenance"] = "skills_command_scan"
    return attach_declarations(
        receipt, discover_skills(repo_path=str(repo_path or "."))
    )


def skills_command(
    verb: str = "",
    rest: str = "",
    *,
    repo_path: Any = None,
    extra_roots: Optional[Sequence[str]] = None,
    receipt: Optional[Mapping[str, Any]] = None,
    home: Any = None,
    config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Run one ``/skills`` verb and RETURN a plain receipt.

    A no-argument call opens the keyboard list - which is a browser, not a
    verb, so it is reported as ``result_kind="browser"`` rather than a
    receipt a caller might mistake for a completed action.

    Every verb either acts and returns or names the next key. Nothing here
    prints: the caller owns rendering and escaping.
    """
    name = str(verb or "").strip().lower()
    argument = str(rest or "").strip()

    # A NO-ARGUMENT call is the browser, not the list. That asymmetry is the
    # reference interaction: `/skills` with nothing typed opens a
    # keyboard-driven list, and every TYPED verb acts and returns.
    if not name:
        name = "browser"

    if name == "list":
        hidden, note = load_visibility(repo_path, home=home)
        catalog = skill_catalog(
            repo_path,
            extra_roots=list(extra_roots or ()),
        )
        rows = [dict(row) for row in catalog["rows"]]
        if argument:
            needle = argument.casefold()
            rows = [
                row
                for row in rows
                if needle in str(row.get("name", "")).casefold()
                or needle in str(row.get("description", "")).casefold()
            ]
        report = catalog["progressive_disclosure"]
        lines = [
            f"{len(rows)} entr(ies); catalogue {report['catalog_tokens']} token(s) "
            f"at startup, {report['saving_tokens']} token(s) saved by on-demand "
            "bodies",
        ]
        for row in rows:
            kind = str(row.get("kind") or "skill")
            cost = (
                f"{int(row.get('preloaded_tokens', 0))} tok"
                f" (catalog {int(row.get('catalog_tokens', 0))})"
                if kind != "command_file"
                else "template"
            )
            lines.append(
                f"  /{row.get('qualified_name', row.get('name'))} "
                f"[{kind}, {row.get('origin')}] {cost}"
            )
            if row.get("wins_over"):
                lines.append(
                    "    wins over a flat command file of the same name "
                    "(the skill carries the declaration)"
                )
        collisions = catalog.get("collisions") or []
        if collisions:
            lines.append(
                f"{len(collisions)} name(s) claimed by both a skill and a flat "
                "command file; the skill wins"
            )
        if hidden:
            lines.append("hidden from the model: " + ", ".join(sorted(hidden)))
        if note:
            lines.append(f"visibility store: {note}")
        if not rows:
            lines.append("  (nothing matched)")
        return {
            "verb": "list",
            "ok": True,
            "lines": lines,
            "payload": {
                "count": len(rows),
                "rows": rows,
                "progressive_disclosure": report,
                "hidden": sorted(hidden),
                "collisions": collisions,
                "filter": argument,
            },
        }

    if name == "browser":
        browser = SkillBrowser.open(
            repo_path, extra_roots=extra_roots, receipt=receipt, home=home
        )
        return {
            "verb": "browser",
            "ok": True,
            "result_kind": "browser",
            "widget_id": LIST_WIDGET_ID,
            "keys": list(SKILL_KEYS),
            "visibilities": list(SKILL_VISIBILITIES),
            "hidden": list(browser.hidden),
            "lines": browser.lines(),
            # The state machine travels with the receipt. A shell that
            # rebuilt the list from the rendered lines would lose the cursor
            # and the hidden set, and a `space` press would then hide nothing.
            "browser": browser,
            "payload": {},
        }

    if not argument:
        return {"verb": name, "ok": False, "lines": [_USAGE], "payload": {}}

    if name == "inspect":
        catalog = skill_catalog(repo_path, extra_roots=list(extra_roots or ()))
        entry = next(
            (
                row
                for row in catalog["rows"]
                if argument
                in (
                    str(row.get("name") or ""),
                    str(row.get("qualified_name") or ""),
                )
            ),
            None,
        )
        if entry is None:
            return {
                "verb": name,
                "ok": False,
                "lines": [f"no skill or command named {argument}"],
                "payload": {},
            }
        lines = [
            f"{entry.get('qualified_name', entry.get('name'))} "
            f"[{entry.get('kind')}, {entry.get('origin')}]",
            f"  source: {entry.get('source')}",
            (
                f"  catalogue tokens: {entry.get('catalog_tokens')} "
                f"(what the model sees at startup)"
            ),
            f"  body tokens: {entry.get('body_tokens')} (paid only on invocation)",
            (
                f"  supporting files: {entry.get('supporting_files')} "
                f"({entry.get('supporting_tokens')} tokens, loaded on demand)"
            ),
            f"  version: {entry.get('version')}",
            f"  declared model tier: {entry.get('model_tier') or 'medium'}",
        ]
        declared = [str(item) for item in (entry.get("declared_tools") or ())]
        lines.append(
            f"  declared tools: {', '.join(declared) or 'none'} "
            "(declared data; never granted)"
        )
        if entry.get("wins_over"):
            lines.append("  wins over a flat command file of the same name")
        lines.extend(_supporting_lines(repo_path, argument, entry))
        payload = dict(entry)
        # The receipt, when the caller supplied one, is rendered through the
        # honest renderer - never interleaved into this listing, so a
        # `(none matched)` answer cannot sit next to a skill name and read as
        # a delivery.
        supplied = dict(receipt or {})
        if supplied:
            lines.append("")
            lines.append("last skills receipt:")
            lines.extend(f"  {line}" for line in render_skill_receipt(supplied))
            payload["receipt"] = supplied
        elif str(entry.get("kind")) != "command_file":
            lines.append("")
            lines.append("no skills receipt supplied for this run")
            lines.append(
                f"  an absent receipt means no run consulted skills; a run that "
                f"consulted them and matched nothing reports {NONE_MATCHED!r}"
            )
        return {"verb": name, "ok": True, "lines": lines, "payload": payload}

    if name in ("enable", "disable"):
        from cli import plugins as plugins_mod

        catalog = skill_catalog(repo_path, extra_roots=list(extra_roots or ()))
        entry = next(
            (
                row
                for row in catalog["rows"]
                if argument
                in (str(row.get("name") or ""), str(row.get("qualified_name") or ""))
            ),
            None,
        )
        # The TIER is chosen from where the skill actually lives, not assumed.
        # `cli.plugins.enable_skill` takes a tier and its default is "global",
        # so a project skill asked for without one fails with "no standalone
        # skill named X in global tier" - a refusal that names the wrong tier
        # reads as "that skill does not exist", which is a lie about a file
        # sitting in the repository.
        tier = _tier_for(argument, entry, repo_path)
        try:
            if name == "enable":
                plugins_mod.enable_skill(argument, tier=tier, repo_path=repo_path)
            else:
                plugins_mod.disable_skill(argument, tier=tier, repo_path=repo_path)
        except Exception as exc:
            return {
                "verb": name,
                "ok": False,
                "lines": [f"{name} failed: {exc}"],
                "payload": {"name": argument, "tier": tier},
            }
        verb_note = "is enabled" if name == "enable" else "is disabled"
        lines = [f"skill {argument} {verb_note}"]
        if entry is not None and entry.get("wins_over"):
            lines.append(
                "  the flat command file of the same name is still discoverable "
                "until it is removed"
            )
        if entry is None:
            lines.append("  (not found in the catalogue; the file was still moved)")
        return {
            "verb": name,
            "ok": True,
            "lines": lines,
            "payload": {"name": argument, "tier": tier},
        }

    if name == "create":
        if not _NAME_OK.match(argument):
            return {
                "verb": name,
                "ok": False,
                "lines": [
                    "a skill name must be lowercase letters, digits, dot, dash "
                    "or underscore and start with a letter or digit"
                ],
                "payload": {},
            }
        root = Path(str(repo_path or ".")) / ".neo" / "skills" / argument
        if root.exists():
            return {
                "verb": name,
                "ok": False,
                "lines": [f"{root} already exists"],
                "payload": {},
            }
        try:
            root.mkdir(parents=True)
            (root / "SKILL.md").write_text(
                f"---\nname: {argument}\ndescription: what this skill is for\n"
                "---\n\nDescribe the task this skill covers.\n",
                encoding="utf-8",
            )
        except OSError as exc:
            return {
                "verb": name,
                "ok": False,
                "lines": [f"create failed: {exc}"],
                "payload": {},
            }
        return {
            "verb": name,
            "ok": True,
            "lines": [
                f"created {root / 'SKILL.md'}",
                "  only the name and description load at startup; the body loads "
                "when the skill is invoked",
            ],
            "payload": {"path": str(root / "SKILL.md")},
        }

    return {"verb": name, "ok": False, "lines": [_USAGE], "payload": {}}


#: Origins `cli.plugins` knows how to move a skill within. A `plugin` origin
#: is deliberately absent: a plugin skill is disabled by the PLUGIN's marker,
#: not by renaming a file inside the bundle, and pretending otherwise would let
#: a `/skills disable` half-disable an installed plugin.
_MOVABLE_TIERS = ("project", "global")


def _tier_for(
    name: str,
    entry: Optional[Mapping[str, Any]],
    repo_path: Any = None,
) -> str:
    """Return the tier a skill should be enabled/disabled in.

    Derived from the catalogue row's own origin, and - when the row is absent
    because the skill is ALREADY disabled and therefore not discovered - by
    looking on disk. That second step is load-bearing: `enable` is exactly the
    call made when discovery cannot see the skill, so a resolver that trusted
    only the catalogue would fall back to "global" and refuse to re-enable a
    project skill that exists.

    Project before global, matching the discovery precedence, so a name in
    both tiers resolves the same way twice.
    """
    origin = str((entry or {}).get("origin") or "")
    if origin in _MOVABLE_TIERS:
        return origin
    roots: List[Tuple[str, Any]] = []
    if repo_path:
        roots.append(("project", Path(str(repo_path)) / ".neo" / "skills"))
    roots.append(("global", _global_skills_root()))
    for tier, root in roots:
        try:
            if not root.is_dir():
                continue
            for candidate in sorted(root.iterdir()):
                if candidate.name != str(name or ""):
                    continue
                if (candidate / "SKILL.md").is_file() or (
                    candidate / "SKILL.md.disabled"
                ).is_file():
                    return tier
        except OSError:
            continue
    return "global"


def _global_skills_root() -> Path:
    """Return the global skills root, reading the same env the scanner does."""
    override = os.environ.get("NEO_GLOBAL_ROOT")
    if override:
        return Path(override).expanduser() / "skills"
    from harness.skills import _global_config_root as _root

    return _root() / "skills"


def _supporting_lines(
    repo_path: Any, argument: str, entry: Mapping[str, Any]
) -> List[str]:
    """Return the supporting-file listing for one catalogue entry."""
    if str(entry.get("kind")) == "command_file":
        return []
    skill = next(
        (
            item
            for item in discover_skills(repo_path=repo_path)
            if str(getattr(item, "name", "")) == argument
        ),
        None,
    )
    if skill is None:
        return []
    rows = supporting_files(skill)
    if not rows:
        return ["  supporting files: none (nothing to preload)"]
    out = [
        f"  supporting files: {len(rows)} (none are loaded until the skill reads one)"
    ]
    for row in rows:
        out.append(
            f"    {row['relative']} - {row['bytes']} bytes, ~{row['tokens']} tokens"
        )
    return out


# ---------------------------------------------------------------------------
# /agents verbs
# ---------------------------------------------------------------------------

#: The verb vocabulary for ``/agents``. Declared here for the same reason
#: :data:`SKILL_VERBS` is: one table, so the registry and the dispatcher
#: cannot disagree about what can be typed.
AGENT_VERBS: Tuple[str, ...] = (
    "list",
    "show",
    "enable",
    "disable",
    "create",
    "inspect",
)

_AGENT_USAGE = "usage: /agents " + " | ".join(AGENT_VERBS)


@dataclass
class AgentRoster:
    """Every loadable agent with its cost facts, resolved lazily per verb.

    The roster is a projection of ``runtime.subagents.AgentRegistry`` and
    nothing else: an agent a runtime would refuse to construct is still
    LISTED (the loader records it as a diagnostic), because an invisible
    agent is indistinguishable from one nobody wrote.
    """

    repo_path: Any = None
    model: Optional[str] = None
    roots: Optional[Sequence[Any]] = None
    data: Dict[str, Any] = field(default_factory=dict)

    def load(self) -> Dict[str, Any]:
        """Load the roster once and cache it."""
        if not self.data:
            from runtime.subagents import agent_roster

            self.data = agent_roster(self.repo_path, roots=self.roots, model=self.model)
        return self.data

    def rows(self) -> List[Dict[str, Any]]:
        """Return the agent rows, sorted by name."""
        return [dict(item) for item in self.load().get("agents", [])]

    def lines(self) -> List[str]:
        """Render the roster as PLAIN lines.

        Each row carries the four things that cost money - model tier, effort
        level, tool count, max turns - and the effort line states whether a
        real parameter is actually sent. ``level`` and ``honoured`` are on
        SEPARATE lines; one line reading "effort: high (honoured)" is the
        conflation this surface exists to avoid.
        """
        rows = self.rows()
        out = [f"{len(rows)} agent(s)"]
        for row in rows:
            tier = str(row.get("model_tier") or "") or "session default"
            honoured = bool(row.get("effort_honoured", False))
            status = str(row.get("effort_status") or "unreported")
            parameter = str(row.get("effort_parameter") or "")
            out.append(
                f"  {row.get('name')} (role {row.get('role')}, v{row.get('version')})"
            )
            out.append(f"    model tier: {tier}")
            out.append(f"    effort level: {row.get('effort_level') or 'auto'}")
            out.append(
                f"    effort sent: {parameter or 'nothing'} "
                f"({'sent' if honoured else status})"
            )
            tools = [str(item) for item in (row.get("tools") or ())]
            out.append(f"    tools ({len(tools)}): {', '.join(tools) or 'none'}")
            out.append(f"    max turns: {row.get('max_turns') or 'session default'}")
        for diagnostic in self.load().get("diagnostics", []):
            out.append(
                f"  refused: {diagnostic.get('path', '?')} - "
                f"{diagnostic.get('error', 'unreadable')}"
            )
        if not rows:
            out.append("  (no agent definitions found)")
        return out


def render_agent_lines(
    repo_path: Any = None,
    *,
    model: Optional[str] = None,
    roots: Optional[Sequence[Any]] = None,
) -> List[str]:
    """Render ``/agents list``. Pure; writes nothing."""
    return AgentRoster(repo_path=repo_path, model=model, roots=roots).lines()


def agent_effort_for(tier: Any, model: Optional[str] = None) -> Dict[str, Any]:
    """Resolve a declared TIER into the ladder level a model would send.

    A thin DELEGATION to `runtime.subagents.agent_effort`, which is the one
    translation. It used to carry its own copy, which is how a renderer and
    the runtime's own view of the same agent could answer "what effort does
    this ask for" two different ways - the exact "two implementations of one
    behaviour" failure. The tier-to-level rule (a class of model never
    implies a level above `medium`) lives in `tier_requested_effort` and is
    stated there once.
    """
    from runtime.subagents import agent_effort

    return agent_effort(model, str(tier or ""))


def _agent_lines_for(row: Mapping[str, Any], model: Optional[str] = None) -> List[str]:
    """Render one agent's cost lines through the runtime's own view."""
    from runtime.subagents import AgentCostView

    view = AgentCostView(
        name=str(row.get("name") or ""),
        role=str(row.get("role") or ""),
        model_tier=str(row.get("model_tier") or ""),
        tools=tuple(str(item) for item in (row.get("tools") or ())),
        max_turns=int(row.get("max_turns") or 0),
        max_children=int(row.get("max_children") or 0),
        max_cost_usd=float(row.get("max_cost_usd") or 0.0),
        effort=agent_effort_for(row.get("model_tier"), model),
        version=str(row.get("version") or ""),
        source=str(row.get("source") or ""),
        can_spawn=bool(row.get("can_spawn", False)),
        diagnostics=tuple(str(item) for item in (row.get("diagnostics") or ())),
    )
    return view.lines()


def agents_command(
    verb: str = "",
    rest: str = "",
    *,
    repo_path: Any = None,
    model: Optional[str] = None,
    roots: Optional[Sequence[Any]] = None,
) -> Dict[str, Any]:
    """Run one ``/agents`` verb and RETURN a plain receipt.

    ``list`` and ``show`` are read-only. ``enable``/``disable``/
    ``create``/``inspect`` are declared because the product should not show a
    verb it cannot honour - each one either acts or names the reason it did
    not, and none of them silently does nothing.
    """
    name = str(verb or "").strip().lower() or "list"
    argument = str(rest or "").strip()
    roster = AgentRoster(repo_path=repo_path, model=model, roots=roots)

    if name == "list":
        rows = roster.rows()
        return {
            "verb": "list",
            "ok": True,
            "lines": roster.lines(),
            "payload": {
                "count": len(rows),
                "agents": rows,
                "diagnostics": roster.load().get("diagnostics", []),
            },
        }

    if not argument:
        return {"verb": name, "ok": False, "lines": [_AGENT_USAGE], "payload": {}}

    row = next(
        (item for item in roster.rows() if str(item.get("name") or "") == argument),
        None,
    )

    if name in ("enable", "disable") and row is None:
        # A disabled agent is BY DEFINITION not loadable, so the roster cannot
        # name it - which would make `enable` refuse the exact agent it exists
        # to re-enable. The lookup therefore falls back to the marker file on
        # disk, and the record says which file it found rather than inventing
        # a name from the argument.
        marker = None
        repo_root = Path(str(roster.repo_path or "")).resolve()
        for candidate in sorted((repo_root / ".neo" / "agents").glob("*.md.disabled")):
            if candidate.name[: -len(".disabled")] == f"{argument}.md":
                marker = candidate
                break
        row = {"name": argument, "source": str(marker)} if marker is not None else None

    if name == "show":
        if row is None:
            return {
                "verb": name,
                "ok": False,
                "lines": [f"no agent named {argument}"],
                "payload": {},
            }
        lines = _agent_lines_for(row, model)
        lines.append(
            "  declared tools are intersected with the role profile; nothing a "
            "definition declares is granted"
        )
        return {"verb": name, "ok": True, "lines": lines, "payload": dict(row)}

    if name == "inspect":
        if row is None:
            return {
                "verb": name,
                "ok": False,
                "lines": [f"no agent named {argument}"],
                "payload": {},
            }
        # The declaration, explained against the ROLE profile: the two
        # ceilings stay apart in the record so a reader can see which one
        # refused a tool.
        try:
            from extensions.skill_policy import explain_declaration_for_role

            policy = explain_declaration_for_role(row, row.get("role"))
        except Exception as exc:  # pragma: no cover - defensive
            policy = {"error": f"the policy layer is unavailable: {exc}"}
        return {
            "verb": name,
            "ok": True,
            "lines": _agent_lines_for(row, model)
            + [
                "  role profile tools: "
                + (", ".join(policy.get("role_tools") or ()) or "none"),
                "  effective tools: "
                + (", ".join(policy.get("effective_tools") or ()) or "none"),
                # The sentence `show` carries belongs on `inspect` too: this
                # is the verb a reader opens precisely to find out whether a
                # declared tool is really available, so it is the one place
                # "nothing a definition declares is granted" must be said.
                "  nothing a definition declares is granted: these are "
                "declared data intersected with the role profile",
            ]
            + [
                f"  refused: {item.get('tool') or item.get('claim')} - "
                f"{item.get('reason')}"
                for item in (policy.get("refused") or ())
            ],
            "payload": {"agent": dict(row), "policy": policy},
        }

    if name in ("enable", "disable"):
        if row is None:
            return {
                "verb": name,
                "ok": False,
                "lines": [
                    f"no agent named {argument}; an agent is enabled by being "
                    "loadable, so enable/disable is a filesystem move"
                ],
                "payload": {},
            }
        # An agent definition is one file, so the marker is a suffix on the
        # FILE (`scout.md.disabled`) - which is also the convention the skill
        # layer already uses. Deriving it from the source path rather than
        # stripping the extension keeps `.md` in the name, so a definition is
        # renamed to something the loader does not read rather than to a
        # second agent file.
        source = Path(str(row.get("source") or ""))
        if not source.is_file():
            return {
                "verb": name,
                "ok": False,
                "lines": [f"the definition for {argument} is not on disk"],
                "payload": {"name": argument},
            }
        if name == "disable":
            target = Path(str(source) + ".disabled")
        else:
            # `source` may already BE the marker, because the marker is the
            # only thing on disk for a disabled agent and that is where the
            # lookup above found it. Stripping the suffix here is what makes
            # enable round-trip; appending to a name that already ends in
            # `.disabled` is how a second `enable` would look for
            # `scout.md.disabled.disabled` and report "already enabled" about
            # a file that is still switched off.
            if source.name.endswith(".disabled"):
                target = source.with_name(source.name[: -len(".disabled")])
            elif Path(str(source) + ".disabled").is_file():
                target = source
            else:
                return {
                    "verb": name,
                    "ok": False,
                    "lines": [f"agent {argument} is already enabled"],
                    "payload": {"name": argument},
                }
        try:
            source.rename(target)
        except OSError as exc:
            return {
                "verb": name,
                "ok": False,
                "lines": [f"{name} failed: {exc}"],
                "payload": {"name": argument},
            }
        return {
            "verb": name,
            "ok": True,
            "lines": [f"agent {argument} is {name}d"],
            "payload": {"name": argument, "path": str(target)},
        }

    if name == "create":
        if not _NAME_OK.match(argument):
            return {
                "verb": name,
                "ok": False,
                "lines": [
                    "an agent name must be lowercase letters, digits, dot, dash "
                    "or underscore and start with a letter or digit"
                ],
                "payload": {},
            }
        root = Path(str(repo_path or ".")) / ".neo" / "agents"
        target = root / f"{argument}.md"
        if target.exists():
            return {
                "verb": name,
                "ok": False,
                "lines": [f"{target} already exists"],
                "payload": {},
            }
        try:
            root.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "---\n"
                f"name: {argument}\n"
                "role: implementer\n"
                "version: 1.0.0\n"
                "model-tier: medium\n"
                "tools: [read, edit, test]\n"
                "---\n\n"
                "Describe what this agent does and when it should be used.\n",
                encoding="utf-8",
            )
        except OSError as exc:
            return {
                "verb": name,
                "ok": False,
                "lines": [f"create failed: {exc}"],
                "payload": {},
            }
        return {
            "verb": name,
            "ok": True,
            "lines": [
                f"created {target}",
                "  a declared tool outside the role profile is refused at load "
                "time, not granted",
            ],
            "payload": {"path": str(target)},
        }

    return {"verb": name, "ok": False, "lines": [_AGENT_USAGE], "payload": {}}


# Re-exported so a caller reading this module's surface knows where the cost
# numbers came from without importing `harness.skills` as well.
_TOKEN_COST = skill_token_cost
_COMMAND_SOURCES = command_sources
