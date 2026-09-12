"""Documentation / API lookup tool (Round 8, Task D).

Problem being fixed: the harness's only knowledge source was the repo
itself. When the agent meets an unfamiliar library API, it has no way
to check the correct usage — it either burns turns guessing or writes
plausibly-wrong code that verify then rejects expensively.

The mechanism mirrors RECALL: a step session outputs

    DOCS <library or symbol>[.member] [query words...]

in place of a bash command (a control signal to the HARNESS, never
executed as shell). The harness resolves the lookup and re-injects the
result into the live session as the next user message.

Resolution order (read-only, network optional):
1. LOCAL CACHE — logs/_docs-cache/<slug>.json (shared across tasks,
   same pattern as logs/_code-graph/). A hit costs zero network.
2. INTERPRETER DOC — the authoritative in-repo source: pydoc renders
   the docstring/signature of an importable module or object. Runs in
   a SUBPROCESS (importing arbitrary code in-process would be a
   side-channel; a subprocess dies cleanly). No network.
3. PYPI JSON API — https://pypi.org/pypi/<pkg>/json (stdlib urllib,
   short timeout). Gives the package summary/description so the agent
   at least knows WHAT the library is and which version is deployed.
   Fetched ONLY when steps 1+2 miss AND docs_lookup_enabled is True
   (network is opt-in per task config; the sandbox itself is
   networkless — this lookup happens harness-side, never in-sandbox).

Safety/scope (read-only by construction):
- The only side effects are writes to logs/_docs-cache/ (harness-owned,
  outside the repo — the never-mutate-original-repo guarantee holds).
- No general web browsing: the ONLY remote call is the fixed PyPI
  JSON endpoint, GET-only, for the parsed package name.
- Everything is capped: cache entries, output chars, timeout.

Budgets (config-driven, like RECALL):
  docs_lookup_enabled (bool, default True; remote fetch still
                        requires True — see docs_lookup_allow_remote)
  docs_lookup_allow_remote (bool, default False) — the PyPI fetch gate
  max_docs_per_step (int, default 3) — per step session
  docs_max_chars (int, default 3000) — cap on one lookup's result

Trace: each lookup logs a `docs_lookup` event
{step_id, turn, query, target, source, ok} so the run is auditable.
"""

import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional

__all__ = ["DocsResult", "lookup", "render_docs_result", "parse_docs", "cache_path_for"]

_TIMEOUT_S = 10  # remote fetch timeout (bounded, config-independent
# safety bound: a lookup must never stall a step)


class DocsResult(NamedTuple):
    """One resolved docs lookup.

    source: "cache" | "pydoc" | "pypi" | "none"
    text: the model-facing doc text (already capped)
    ok: True iff any source produced content."""

    source: str
    text: str
    ok: bool


# ---------------------------------------------------------------------------
# query parsing
# ---------------------------------------------------------------------------

_DOCS_PAT = re.compile(r"^\s*DOCS?\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)
# a lookup target: dotted path, optionally followed by free-text words
_TARGET_PAT = re.compile(r"^([A-Za-z_][\w.]*)(?:\s+(.+))?$", re.DOTALL)


def parse_docs(text: str) -> Optional[str]:
    """Return the lookup query of a DOCS request, or None if the message
    isn't one. Accepts `DOCS <terms>` (case-insensitive; terms may span
    lines). A DOCS is a control signal to the HARNESS, not a bash
    command — run_step checks this before extracting a command (on both
    the raw reply and its fence-stripped form), so the terms are never
    executed in the sandbox."""
    m = _DOCS_PAT.match((text or "").strip())
    return m.group(1).strip() if m else None


def _split_target(query: str) -> Dict[str, str]:
    """Split 'json.dumps pretty indent' style queries into the dotted
    target ('json.dumps') and free-text words ('pretty indent')."""
    m = _TARGET_PAT.match((query or "").strip())
    if not m:
        return {"target": "", "words": (query or "").strip()}
    return {"target": m.group(1).rstrip("."), "words": (m.group(2) or "").strip()}


# ---------------------------------------------------------------------------
# cache (shared, harness-owned, outside the repo)
# ---------------------------------------------------------------------------


def cache_path_for(index_root: Path, target: str) -> Path:
    """Cache file path for a lookup target. Assumes index_root is the
    shared docs-cache root (logs/_docs-cache/)."""
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", target.strip("."))[:80] or "empty"
    return index_root / f"{slug}.json"


def _cache_get(index_root: Path, target: str) -> Optional[DocsResult]:
    try:
        p = cache_path_for(index_root, target)
        if not p.is_file():
            return None
        entry = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(entry, dict) or "text" not in entry:
            return None
        return DocsResult("cache", str(entry.get("text", "")), True)
    except (OSError, ValueError):
        return None


def _cache_put(index_root: Path, target: str, source: str, text: str) -> None:
    """Best-effort cache write — a cache failure must never break the
    lookup (or the step)."""
    try:
        index_root.mkdir(parents=True, exist_ok=True)
        p = cache_path_for(index_root, target)
        entry = {"target": target, "source": source, "text": text, "version": 1}
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entry), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# resolution layers
# ---------------------------------------------------------------------------


def _pydoc_lookup(target: str, max_chars: int) -> Optional[str]:
    """Render the docstring + signature of an importable module/object
    via pydoc in a SUBPROCESS (importing agent-adjacent code in-process
    would be a side-channel; a subprocess dies cleanly). Returns None
    when the target isn't importable. Never raises."""
    if not target:
        return None
    code = (
        "import sys, pydoc\n"
        f"sys.argv = ['pydoc', {target!r}]\n"
        "try:\n"
        f"    pydoc.doc({target!r}, output=sys.stdout)\n"
        "except Exception:\n"
        "    sys.exit(1)\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    if not out:
        return None
    # pydoc prints a miss message (not an exit code) for unimportable
    # targets — case varies by version ("No Python documentation found")
    if "no python documentation found" in out.lower():
        return None
    # cap: keep the head (module/class docstring + signatures live there)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n…[truncated]"
    return out


def _pypi_lookup(pkg: str, max_chars: int) -> Optional[str]:
    """Fetch the PyPI JSON metadata for `pkg` (GET-only, fixed endpoint,
    stdlib urllib, short timeout). Returns None on any failure (network
    absent, unknown package, timeout) — never raises. The PyPI summary
    text is capped; this is a docs lookup, not a mirror."""
    if not pkg or not re.match(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$", pkg):
        return None
    url = f"https://pypi.org/pypi/{pkg}/json"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "vex-harness-docs-lookup/1.0"}
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None
    info = (data or {}).get("info") or {}
    name = info.get("name") or pkg
    summary = info.get("summary") or ""
    version = info.get("version") or "?"
    # description can be huge; take the leading prose only
    desc = info.get("description") or ""
    desc_head = desc[:max_chars]
    if len(desc) > max_chars:
        desc_head += "\n…[truncated]"
    lines = [
        f"{name} (PyPI, latest {version})",
        f"summary: {summary}" if summary else "summary: (none)",
    ]
    if desc_head:
        lines.append("description (head):")
        lines.append(desc_head)
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n…[truncated]"
    return out


# ---------------------------------------------------------------------------
# public entry
# ---------------------------------------------------------------------------


def lookup(
    query: str,
    index_root: Path,
    max_chars: int = 3000,
    allow_remote: bool = False,
) -> DocsResult:
    """Resolve one DOCS lookup: cache -> pydoc -> (opt-in) PyPI.

    Assumes query is the raw text after 'DOCS ' (parse_docs output),
    index_root is the shared cache root (created on demand), and
    max_chars caps the returned text. allow_remote=False keeps the
    lookup fully offline (cache + local interpreter docs only). Never
    raises — a miss returns source="none", ok=False.
    """
    target = _split_target(query)["target"]
    root_module = target.split(".")[0] if target else ""
    words = _split_target(query)["words"]

    # 1. cache (exact dotted target)
    hit = _cache_get(index_root, target or (query or "empty"))
    if hit is not None and hit.ok:
        return hit

    # 2. local interpreter docs (pydoc, subprocess-isolated)
    text = _pydoc_lookup(target or root_module, max_chars)
    if text:
        res = DocsResult("pydoc", text, True)
        _cache_put(index_root, target or root_module, "pydoc", text)
        return res

    # 3. remote PyPI metadata (opt-in)
    if allow_remote and root_module:
        text = _pypi_lookup(root_module, max_chars)
        if text:
            res = DocsResult("pypi", text, True)
            _cache_put(index_root, target or root_module, "pypi", text)
            return res

    return DocsResult("none", "", False)


def render_docs_result(query: str, result: DocsResult) -> str:
    """The user message returned to a session that issued `DOCS <query>`.
    Assumes result.text is already length-capped by lookup()."""
    if not result.ok:
        return (
            f"DOCS lookup for '{query}' found nothing (cache, local "
            "interpreter docs"
            + (", and PyPI" if False else "")
            + "). The module may not be installed or the name is wrong — "
            "proceed with bash commands, or SUBMIT if the step is done."
        )
    header = (
        f"DOCS results for '{query}' (source: {result.source}):\n"
        f"---\n{result.text}\n---"
    )
    return header + (
        "\nEnd of DOCS results. Continue with exactly ONE bash command, "
        "or SUBMIT if this step is done."
    )


def lookup_and_render(
    query: str,
    index_root: Path,
    max_chars: int = 3000,
    allow_remote: bool = False,
) -> tuple:
    """Convenience wrapper: lookup + render, returning
    (rendered_message, DocsResult) so the caller can trace the source."""
    res = lookup(query, index_root, max_chars=max_chars, allow_remote=allow_remote)
    return render_docs_result(query, res), res
