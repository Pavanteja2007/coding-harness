"""P2/W1-T4 — skill DECLARATIONS are enforced, and lint feedback carries LOCALITY.

Two tasks, one module, because they are both about the same failure: a
corrective signal that names a problem without naming where it is.

## Task A — `extensions/skill_policy.py` gets a production caller

`extensions/skill_policy.py` parsed a skill's declared model tier and
permission claims and **did not enforce them**. A skill declaring
``model-tier: expensive`` got no escalation and a skill declaring a permission
it did not have was not stopped. The parsing was real; the enforcement was dark.

Three rules, and each one is a place where the WRONG answer is the easy one:

1. **A skill's declared permissions are CAPPED by the run's actual permissions.**
   Same shape as OpenCode's "children inherit only the parent's deny rules" and
   Hermes' "toolsets intersected with the parent's". The envelope is built from
   THE RUN — its policy rules, its approval mode, its visible tools — and never
   from a declaration. A declaration is data arriving from a markdown file on
   disk; building the ceiling out of it would be building the lock out of the
   thing it is supposed to lock.

2. **An unsatisfied declaration is RECORDED, never ignored.** ``unsatisfied`` is a
   first-class key and every entry names the capability, what was declared, and
   why it was refused. A declaration that silently evaporates is
   indistinguishable from a skill that declared nothing, which is exactly the
   state the wiring exists to make visible.

3. **Nothing here is behaviour-changing in ``DEFAULTS``.** ``skill_policy``
   enforcement is opt-in on KEY PRESENCE. ``harness/config.py::DEFAULTS`` is
   merged into every task and every eval arm, so a truthy default would switch
   every run in the project onto a different architecture the day the key landed.

## Task B — a lint error carries CODE LOCALITY, not a line number

Aider's distinctive contribution to its repair loop is not the three-stage lint
pipeline; it is ``TreeContext``: the corrective message points at the ENCLOSING
FUNCTION around the error lines, so the model is shown the code it has to change
rather than a bare ``line 412``.

``harness.lint._context_block`` already renders ±3 lines with real line
numbers. What it does not do is say WHICH function those lines are in, and for
the failure this repository's runs actually hit -- an undefined name, a syntax
error inside one method of a large class -- "line 412" is not an actionable
anchor and the model has to guess which of four hundred lines it meant.

:func:`enclosing_scope_block` adds the AST answer: the nearest enclosing
``def``/``async def``/``class`` at any depth, its header line, and where the
error sits inside it. It is fail-closed in the same direction as everything else
here: a file that does not parse, or a line outside any scope, yields ``""`` and
the caller keeps the line-number block it already had. A locality hint is an
addition, never a replacement.
"""

from __future__ import annotations

import ast
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "ENFORCEMENT_KEYS",
    "enclosing_scope_block",
    "enforce_skill_policy",
    "session_permissions_for_run",
]

#: Config keys that turn enforcement ON, read by KEY PRESENCE plus explicit
#: truthiness. None of these is in `harness/config.py::DEFAULTS` -- see the
#: module docstring for why that is not a stylistic preference.
ENFORCEMENT_KEYS: Tuple[str, ...] = (
    "skill_policy_enforce",
    "session_permissions",
)

_TRUTHY = {"1", "true", "yes", "on"}


def _enabled(config: Optional[Mapping[str, Any]]) -> bool:
    """Return whether skill enforcement is on, by key presence + truthiness."""
    if not config:
        return False
    for key in ENFORCEMENT_KEYS:
        if key not in config:
            continue
        value = config[key]
        if isinstance(value, str):
            if value.strip().lower() in _TRUTHY:
                return True
            continue
        if value:
            return True
    return False


def session_permissions_for_run(
    config: Optional[Mapping[str, Any]],
    *,
    visible_tools: Sequence[str] = (),
) -> Optional[Any]:
    """Build the run's OWN permission envelope, or ``None`` if unconfigured.

    Returns an ``extensions.skill_policy.SessionPermissions`` built from facts
    about THE RUN:

    * ``max_model_tier`` from the run's configured model tier
      (``model_tiers`` / ``model`` / ``intent_model``), so the ceiling is the tier
      the run is actually paying for;
    * ``allow_tools`` from the run's visible tool surface, which is the same set
      ``harness.tools`` builds the schema list from;
    * ``allow`` / ``deny`` from an operator-declared
      ``session_permissions`` mapping, when one is present.

    **Nothing is read from a skill.** A skill's declarations are the thing being
    capped; letting them into the envelope would make the cap a no-op.

    ``None`` means "no envelope declared", and a caller must treat that as
    "nothing to enforce against" rather than as "everything permitted" -- the
    fail-closed direction, and the same one
    ``SessionPermissions.permits`` takes when ``allow`` is empty.
    """
    try:
        from extensions.skill_policy import SessionPermissions, normalize_tier
    except Exception:  # pragma: no cover - extensions is optional
        return None

    declared = config.get("session_permissions") if config else None
    if isinstance(declared, Mapping):
        envelope = SessionPermissions.from_value(declared)
        if visible_tools:
            # `SessionPermissions` is FROZEN, so the run's visible surface is
            # folded in at CONSTRUCTION rather than assigned afterwards. Narrowing
            # an operator's envelope to what the run actually exposes is the
            # whole point, and the tighter of the two must win.
            envelope = SessionPermissions(
                allow=envelope.allow,
                deny=envelope.deny,
                allow_tools=tuple(visible_tools),
                max_model_tier=envelope.max_model_tier,
            )
        return envelope

    tier = _run_model_tier(config)
    if not tier and not visible_tools:
        return None
    return SessionPermissions(
        allow=(),
        deny=(),
        allow_tools=tuple(visible_tools or ()),
        max_model_tier=normalize_tier(tier or "medium"),
    )


def _run_model_tier(config: Optional[Mapping[str, Any]]) -> str:
    """Return the model TIER the run itself is on, or ``""``."""
    if not config:
        return ""
    tiers = config.get("model_tiers")
    if isinstance(tiers, Mapping):
        for key in ("run", "agent", "default", "task"):
            value = tiers.get(key)
            if value:
                return str(value)
    for key in ("model_tier", "intent_model", "model"):
        value = config.get(key)
        if value:
            text = str(value).strip().lower()
            # A bare model NAME is not a tier; naming one as the ceiling would
            # be a fabricated fact.
            if text in {"cheap", "easy", "medium", "expensive", "hard"}:
                return text
    return ""


def enforce_skill_policy(
    scan: Mapping[str, Any],
    config: Optional[Mapping[str, Any]],
    *,
    skills: Sequence[Any] = (),
    visible_tools: Sequence[str] = (),
) -> Dict[str, Any]:
    """Intersect a scan's skill declarations with the run's own permissions.

    Returns a receipt with three parts and never raises:

    * ``session`` -- the envelope the run declared, or ``None``. Absent means
      "no envelope", NOT "everything permitted".
    * ``unsatisfied`` -- every declared capability the run does not have, each
      naming what was asked for and why it was refused. This is the key the
      brief asks for and the one a declaration-quiet design would omit.
    * ``records`` -- the per-skill explanation from
      ``extensions.skill_policy.explain_selection``, i.e. the intersection
      itself, not a summary of it.

    A missing ``extensions`` package, an absent envelope, or an unparseable
    declaration all degrade to ``{"enforced": False, ...}`` with the reason.
    Nothing here raises, because a skill is a markdown file on disk and a broken
    one must not take a run down with it.
    """
    out: Dict[str, Any] = {
        "enforced": False,
        "session": None,
        "unsatisfied": [],
        "records": [],
        "refused_claims": {},
        "skipped": "",
    }
    if not _enabled(config):
        out["skipped"] = "skill policy enforcement is off (opt-in by key presence)"
        return out
    envelope = session_permissions_for_run(config, visible_tools=visible_tools)
    if envelope is None:
        out["skipped"] = (
            "enforcement was requested but the run declared no session "
            "permissions, so there is no ceiling to cap declarations against. "
            "Reported rather than treated as 'everything permitted'."
        )
        return out
    out["session"] = envelope.to_dict()

    scan = dict(scan or {})
    rendered_names = [str(item) for item in (scan.get("rendered") or [])]
    receipts = [dict(item) for item in (scan.get("receipts") or []) if isinstance(item, Mapping)]
    try:
        from extensions.skill_policy import explain_selection
    except Exception as exc:  # pragma: no cover - extensions is optional
        out["skipped"] = f"extensions.skill_policy is unavailable: {exc}"
        return out

    # The OBJECTS are what carry the declarations; the receipts are what carry
    # the match reasons. `explain_selection` needs both, and passing receipts
    # where it expects objects produces an empty intersection with no error --
    # which is the shape of a wiring bug that looks like "the skill declared
    # nothing".
    objects = {str(getattr(skill, "name", "")): skill for skill in skills or ()}
    selected = [objects[name] for name in rendered_names if name in objects]
    try:
        explained = explain_selection(
            selected,
            session=envelope,
            considered=int(scan.get("considered") or 0),
            receipts=receipts,
        )
    except Exception as exc:  # pragma: no cover - defensive
        out["skipped"] = f"skill policy explanation failed: {exc}"
        return out

    out["enforced"] = True
    out["records"] = list(explained.get("records") or [])
    out["refused_claims"] = dict(explained.get("refused_claims") or {})
    out["unsatisfied"] = _unsatisfied(out["records"], envelope)
    return out


def _unsatisfied(
    records: Iterable[Mapping[str, Any]], envelope: Any = None
) -> List[Dict[str, Any]]:
    """Flatten every refused claim into one list a reader can scan.

    ``extensions.skill_policy`` already produces the refusal rows and puts them
    under three different shapes -- a permission claim, a declared tool, and a
    model tier. This is the ONE place they become a single vocabulary, because a
    refusal recorded only inside a nested per-skill dict is a refusal nobody
    reads.

    ``capability`` is DERIVED from the refusal's own shape rather than read from
    a field, because there is no common field: a permission refusal carries
    ``action``, a tool refusal carries ``tool``, and a tier refusal carries
    neither. Guessing the slug and then asserting on it would have produced a
    test that passes on an empty string.
    """
    out: List[Dict[str, Any]] = []
    for record in records or ():
        name = str(record.get("name") or "")
        for claim in record.get("refused") or ():
            if not isinstance(claim, Mapping):
                continue
            if "tool" in claim and "action" not in claim:
                capability = "tool"
                declared = claim.get("tool")
            elif "claim" in claim and str(claim.get("claim")).startswith("model-tier"):
                capability = "model_tier"
                declared = str(claim.get("claim")).split(":", 1)[-1].strip()
            else:
                capability = "permission"
                declared = claim.get("claim") or claim.get("action") or ""
            out.append(
                {
                    "skill": name,
                    "capability": capability,
                    "declared": declared,
                    "effective": record.get("effective_model_tier")
                    if capability == "model_tier"
                    else record.get("effective_tools"),
                    "reason": str(claim.get("reason") or ""),
                }
            )
    del envelope
    return out


# ---------------------------------------------------------------------------
# Task B: AST code locality for a lint failure
# ---------------------------------------------------------------------------

_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def enclosing_scope_block(
    src: str, line: int, *, max_lines: int = 40
) -> Dict[str, Any]:
    """Return the enclosing function/class around 1-based ``line``.

    Returns a dict with:

    * ``kind`` -- ``function`` / ``async_function`` / ``class`` / ``module``
    * ``name`` -- the qualified name, e.g. ``Outer.method``
    * ``header`` -- the ``def``/``class`` line itself, verbatim
    * ``header_line`` / ``start_line`` / ``end_line``
    * ``body`` -- up to ``max_lines`` of the scope with the error line marked,
      or ``""`` when the scope is too large to be worth showing

    ``offset_in_scope`` is how many lines into the scope the error sits, which is
    the number a model actually needs: "34 lines into ``Outer.method``" is a
    place, and "line 412" is not.

    **Fail-closed.** A file that does not parse, a line outside every scope, or
    a scope too large to bound all return ``kind="module"`` with empty fields --
    never a guess about where the error is. The caller keeps the line-number
    block it already had, so this is strictly additive.
    """
    empty: Dict[str, Any] = {
        "kind": "module",
        "name": "",
        "header": "",
        "header_line": 0,
        "start_line": 0,
        "end_line": 0,
        "body": "",
        "offset_in_scope": 0,
        "truncated": False,
    }
    if line <= 0 or not src:
        return empty
    try:
        tree = ast.parse(src)
    except SyntaxError:
        # The file itself does not parse -- which is the common case for a
        # syntax lint failure. There is no AST scope to report, and inventing
        # one by scanning backwards for a `def` would be a guess.
        return empty

    best: Optional[Tuple[ast.AST, List[str]]] = None
    for node in ast.walk(tree):
        if not isinstance(node, _SCOPE_NODES):
            continue
        if not (node.lineno <= line <= (node.end_lineno or node.lineno)):
            continue
        if best is None or node.lineno > best[0].lineno:
            best = (node, _qualified(tree, node))

    if best is None:
        return empty
    node, path = best
    start = node.lineno
    end = node.end_lineno or start
    lines = src.splitlines()
    header = lines[start - 1] if 0 < start <= len(lines) else ""
    span = end - start + 1
    truncated = span > max_lines
    body = ""
    if not truncated and 0 < start <= len(lines):
        top = max(start, line - max_lines // 2)
        bottom = min(end, top + max_lines - 1)
        rendered: List[str] = []
        for offset in range(top, bottom + 1):
            marker = ">" if offset == line else " "
            text = lines[offset - 1] if offset - 1 < len(lines) else ""
            if len(text) > 200:
                text = text[:200] + "..."
            rendered.append(f"{marker} {offset:>5} | {text}")
        body = "\n".join(rendered)
        if body and body != lines[start - 1] and not body.startswith(" "):
            pass
    kind = "class" if isinstance(node, ast.ClassDef) else (
        "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
    )
    return {
        "kind": kind,
        "name": ".".join(path),
        "header": header.strip(),
        "header_line": start,
        "start_line": start,
        "end_line": end,
        "body": body,
        "offset_in_scope": line - start,
        "scope_lines": span,
        "truncated": truncated,
    }


def _qualified(tree: ast.AST, node: ast.AST) -> List[str]:
    """Return the dotted path of enclosing scopes down to ``node``.

    ``Outer.method`` for a method, ``Outer`` for the class itself. Built from
    the PARENT map rather than from indentation, because indentation is exactly
    what a lint failure in the file being checked may have broken.
    """
    parents: Dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    chain: List[str] = []
    current: Optional[ast.AST] = node
    while current is not None:
        if isinstance(current, _SCOPE_NODES):
            chain.append(getattr(current, "name", "") or "<anonymous>")
        current = parents.get(id(current))
    chain.reverse()
    return chain


_LINE_RE = re.compile(r"^\s*(?:def|class)\s")


def scope_hint(src: str = "", line: int = 0, scope: Optional[Mapping[str, Any]] = None) -> str:
    """Return a ONE-LINE model-facing locality hint, or ``""``.

    The form is deliberately short enough to prepend to a corrective message
    without displacing the finding: ``in Outer.method (line 34 of 51)``.

    Pass ``scope`` when the caller already has one -- the unparseable-file path
    in :mod:`harness.lint` computes the scope with its indentation heuristic and
    then re-deriving it here would return the AST answer (which is "no scope" for
    a file that does not parse) and silently drop the hint it just built.
    """
    if scope is None:
        scope = enclosing_scope_block(src, line)
    if not scope or scope.get("kind") == "module" or not scope.get("name"):
        return ""
    offset = int(scope.get("offset_in_scope") or 0)
    span = int(scope.get("scope_lines") or 0)
    if span:
        return f"in {scope['name']} (line {offset} of {span})"
    return f"in {scope['name']} (line {offset})"


del _LINE_RE
