"""Context retrieval for the harness — two layers, one API.

Layer 1 (dumb grep, Phase 1): extract terms from the issue text, score
files by term hits in path + content. Always available; works on any
text-shaped repo.

Layer 2 (structural, Phase 2 — this module's headline upgrade): when the
repo is Python and Terminal 4's memory.code_graph is importable, the
issue is located through the repo's ACTUAL structure — imports and call
edges, not keyword proximity:

  1. ANCHOR: the target test (config["target_test"], a pytest node id)
     names the test that encodes the bug. Even when the issue text never
     mentions the relevant function/class, the TEST file does: its
     imports lead to the module under test, its calls lead to the
     buggy symbol.
  2. SYMBOL MATCH: issue terms are matched against the indexed symbol
     table (names, qualified names, docstrings) — "average is wrong"
     finds compute_monthly_average without a single grep hit.
  3. NEIGHBORHOOD: matched/anchored symbols are expanded along call
     edges (callers + callees) and import edges (the module's importers
     — for a bug, the test that fails IS an importer), so the fix
     surface and its direct dependents are surfaced together.

The two layers are merged into one ranking; structural hits outrank
plain-content hits because they carry the repo's real dependency
structure. Layer 2 is best-effort everywhere: any failure degrades to
Layer 1 with a note in the returned dict (never an exception — retrieval
must not kill a task run).

Spec items 1 (repo understanding) and 21 (structural code graph); the
graph itself is Terminal 4's memory.code_graph (tree-sitter), consumed
via harness.deps.get_code_graph_factory — one structural index for the
whole project instead of a harness-private duplicate.
"""

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Extensions considered "code" when ranking hits.
_CODE_EXTS = {".py", ".js", ".ts", ".go", ".rs", ".java", ".c", ".h", ".cpp", ".hpp"}

# Files never offered as context (build artifacts, env, logs, binaries).
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".tox",
    ".idea",
    ".vscode",
    ".harness",
}
_SKIP_FILE_PAT = re.compile(
    r"(\.log$|\.pyc$|\.lock$|package-lock\.json$|poetry\.lock$)"
)
_LARGE_FILE = 200_000  # bytes — same cap the editor enforces

# Words too generic to be worth grepping for.
_STOPWORDS = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "it",
    "its",
    "this",
    "that",
    "these",
    "those",
    "of",
    "in",
    "on",
    "at",
    "to",
    "for",
    "and",
    "or",
    "but",
    "if",
    "then",
    "else",
    "when",
    "with",
    "without",
    "not",
    "no",
    "yes",
    "do",
    "does",
    "did",
    "has",
    "have",
    "had",
    "should",
    "would",
    "could",
    "can",
    "will",
    "must",
    "may",
    "might",
    "shall",
    "from",
    "by",
    "as",
    "so",
    "than",
    "too",
    "very",
    "just",
    "about",
    "into",
    "over",
    "after",
    "before",
    "while",
    "during",
    "between",
    "under",
    "above",
    "out",
    "off",
    "up",
    "down",
    "again",
    "further",
    "once",
    "here",
    "there",
    "all",
    "any",
    "both",
    "each",
    "few",
    "more",
    "most",
    "other",
    "some",
    "such",
    "only",
    "own",
    "same",
    "s",
    "t",
    "don",
    "now",
    "bug",
    "issue",
    "error",
    "fix",
    "please",
    "fails",
    "failed",
    "fail",
    "test",
    "tests",
    "testing",
    "when",
    "returns",
    "return",
    "raise",
    "raises",
    "expected",
    "actual",
    "function",
    "method",
    "class",
    "module",
    "file",
    "line",
    "code",
    "python",
    "python3",
    "version",
    "traceback",
    "exception",
    "stack",
    "trace",
}


def _walk_code_files(repo_path: str) -> List[Path]:
    """All candidate files in the repo (skip junk dirs and huge files)."""
    root = Path(repo_path)
    out: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if _SKIP_FILE_PAT.search(name):
                continue
            p = Path(dirpath) / name
            try:
                if p.stat().st_size > _LARGE_FILE:
                    continue
            except OSError:
                continue
            out.append(p)
    return out


def extract_terms(issue_text: str, extra_stop: Optional[set] = None) -> List[str]:
    """Extract likely file/symbol names from issue text.

    Assumes issue_text is a human-written bug report. Pulls (a) explicit
    file-ish tokens (contain a '/' or a '.' or snake/camel-case code
    identifiers), (b) code-extension file names, and (c) any non-stopword
    word of length >= 3 (short symbols like "pop" matter a lot). Returns a
    de-duplicated, order-preserving list.
    """
    stop = _STOPWORDS | (extra_stop or set())
    tokens: List[str] = []
    raw = re.findall(r"[A-Za-z_][A-Za-z0-9_./\-]*", issue_text or "")
    for tok in raw:
        low = tok.lower().strip("./-")
        if not low or low in stop or len(low) < 3:
            continue
        if tok in tokens:
            continue
        tokens.append(tok)
    return tokens


def rank_files(repo_path: str, terms: List[str], limit: int = 4) -> List[str]:
    """Return up to `limit` repo-relative file paths ranked by term overlap.

    Assumes `terms` came from extract_terms. Dumb grep-style scoring:
    - +2 per term appearing in the file's path (path or stem)
    - +min(count, 5) per term found in the file's CONTENT (the actual
      "grep for symbols mentioned in the issue" behavior)
    Ties broken by code-extension bonus then shorter path. Returns
    repo-relative posix paths.
    """
    files = _walk_code_files(repo_path)
    lows = [t.lower().strip("./-") for t in terms if len(t.strip("./-")) >= 3]
    scored: List[tuple] = []
    for f in files:
        rel = f.relative_to(repo_path).as_posix()
        rel_l = rel.lower()
        score = 0
        for tl in lows:
            if tl in rel_l or tl in f.stem.lower():
                score += 2
        try:
            text = f.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            text = ""
        for tl in lows:
            c = text.count(tl)
            if c:
                score += min(c, 5)
        if score > 0:
            bonus = 1 if f.suffix in _CODE_EXTS else 0
            scored.append((score + bonus, -len(rel), rel))
    scored.sort(reverse=True)
    return [rel for _, _, rel in scored[:limit]]


def search_file_for_lines(repo_path: str, rel_path: str, terms: List[str]) -> List[str]:
    """Grep one file for lines containing any term; returns up to N matching
    'file:lineno: line' strings (dumb but effective for locating symbols)."""
    import re as _re

    try:
        text = Path(repo_path, rel_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    hits = []
    lows = [t.lower() for t in terms if t]
    for i, line in enumerate(text.splitlines(), start=1):
        lline = line.lower()
        if any(t in lline for t in lows):
            hits.append(f"{rel_path}:{i}: {line.rstrip()}")
            if len(hits) >= 20:
                break
    return hits


def _subwords(name: str) -> Set[str]:
    """Split a snake_case/camelCase identifier into lowercase words.

    ``compute_monthly_average`` -> {compute, monthly, average}; ``HTTPServer``
    -> {http, server}. Used so an issue saying "monthly average is wrong"
    matches the symbol compute_monthly_average even though the full
    identifier never appears in the issue text.
    """
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", split)
    return {w.lower() for w in re.split(r"[_\s]+", split) if len(w) >= 3}


# ---------------------------------------------------------------------------
# Structural layer (Phase 2)
# ---------------------------------------------------------------------------


def _module_id_for(t_file: str) -> str:
    """Graph module node id for a target-test file path (language-aware).

    Python: tests/test_x.py -> module:tests.test_x (the dotted module
    name, matching memory.code_graph's Python indexer). JS/TS: the
    extension-stripped path dotted (src/util.test.js -> module:src.util
    .test), matching the JS indexer's path-based identity. Assumes
    t_file is a normalized repo-relative posix path.
    """
    parts = t_file.split("/")
    js_exts = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    elif parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    elif parts[-1].endswith(js_exts):
        for ext in js_exts:
            if parts[-1].endswith(ext) and parts[-1] != ext:
                parts[-1] = parts[-1][: -len(ext)]
                break
    return "module:" + ".".join(p for p in parts if p)


def _structural_scores(
    repo_path: str,
    terms: List[str],
    target_test: Optional[str],
    index_root: Optional[Path],
) -> Optional[Dict[str, Any]]:
    """Best-effort structural ranking via memory.code_graph.

    Returns {"file_scores": {rel: score}, "symbol_notes": [str], "graph":
    Graph} or None when the graph layer is unavailable/unusable (caller
    degrades to grep-only). NEVER raises.
    """
    factory = None
    try:
        from harness.deps import get_code_graph_factory

        factory = get_code_graph_factory()
    except Exception:
        factory = None
    if factory is None:
        return None
    try:
        if index_root is None:
            # CodeGraph's own default root lives INSIDE the repo — the
            # harness must never write there. Use a throwaway index dir.
            import tempfile

            with tempfile.TemporaryDirectory(prefix="harness-cg-") as tmp:
                graph_obj = factory(repo_path, root=tmp)
                graph = graph_obj.load_or_build()
        else:
            graph_obj = factory(repo_path, root=str(index_root))
            graph = graph_obj.load_or_build()
    except Exception:
        return None

    nodes = getattr(graph, "nodes", {}) or {}
    calls = getattr(graph, "calls", set()) or set()
    imports = getattr(graph, "imports", set()) or set()
    file_scores: Dict[str, float] = {}
    notes: List[str] = []

    # -- helpers ----------------------------------------------------------
    def bump(rel: str, pts: float, why: str) -> None:
        if rel:
            file_scores[rel] = file_scores.get(rel, 0.0) + pts

    def seed_files_of(node_id: str) -> List[str]:
        info = nodes.get(node_id)
        return [info.file] if info is not None else []

    # -- 1. ANCHOR: target test -> the symbols it exercises --------------
    anchor_ids: Set[str] = set()
    if target_test:
        t_file = (
            target_test.split("::")[0].split(" - ")[0].strip("/").replace("\\", "/")
        )
        # normalize the usual target-id forms per language:
        # pytest: tests/test_x.py or ./tests/test_x.py; also accept a bare
        # test name (assume under tests/). JS/TS (vitest/jest ids carry
        # '<file> - <name>' or '<file>::<name>'): any .js/.ts-family file
        # is used as-is.
        if "/" not in t_file and not t_file.endswith(
            (".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")
        ):
            t_file = f"tests/test_{t_file}.py"
        file_node = f"file:{t_file}"
        # module identity is language-specific: dotted for Python, the
        # path-based dotted name for JS/TS (both produce module:<name>
        # nodes — see memory.code_graph._js_module_name)
        t_mod = _module_id_for(t_file)
        if file_node in nodes:
            anchor_ids.add(file_node)
        if t_mod and t_mod in nodes:
            anchor_ids.add(t_mod)
        if not anchor_ids:
            # find a file whose rel path ends with the node-id's file part
            suffix = t_file.split("/")[-1]
            for nid, info in nodes.items():
                if nid.startswith("file:") and info.file.endswith(suffix):
                    anchor_ids.add(nid)
                    break
        if anchor_ids:
            for nid in anchor_ids:
                for rel in seed_files_of(nid):
                    bump(rel, 3.0, "target-test anchor")
            notes.append(f"anchored on target test {target_test}")

    # expand anchors: what the test module imports + calls
    expanded: Set[str] = set(anchor_ids)

    def expand(ids: Set[str], hops: int) -> None:
        """One BFS hop along import/call edges from the given node ids."""
        for _ in range(hops):
            frontier: Set[str] = set()
            for src, dst in imports:
                if src in ids and dst not in ids:
                    frontier.add(dst)
                if dst in ids and src not in ids:
                    frontier.add(src)
            for src, dst in calls:
                if src in ids and dst not in ids:
                    frontier.add(dst)
                if dst in ids and src not in ids:
                    frontier.add(src)
            ids |= frontier
            if not frontier:
                return

    if anchor_ids:
        # imports first (test -> module under test), then calls out of the
        # test module (test -> buggy symbol)
        expand(expanded, 2)

    # test-module-only guard: expansion from the TEST should reach the code
    # under test but must not flood the whole repo; only files reachable
    # within these hops score.
    for nid in expanded:
        for rel in seed_files_of(nid):
            if nodes.get(nid) is not None and nodes[nid].kind != "file":
                # symbol/module nodes carry their defining file; file nodes
                # score via the anchor bump above
                bump(rel, 1.5, "structurally adjacent (import/call edge)")

    # -- 2. SYMBOL MATCH: terms vs the symbol table ----------------------
    # Subword matching: "average" matches compute_monthly_average because
    # identifiers decompose into words. This is what lets the harness find
    # the right file when the issue names a CONCEPT, not the identifier.
    lows = [t.lower().strip("./-") for t in terms if len(t.strip("./-")) >= 3]
    term_words: Set[str] = set()
    for tl in lows:
        term_words |= _subwords(tl)
    matched: Set[str] = set()
    for nid, info in nodes.items():
        if info.kind not in ("func", "method", "class"):
            continue
        hay_name = info.name.lower()
        hay_qual = info.qualified.lower()
        hay_doc = (info.docstring or "").lower()
        sym_words = _subwords(info.name) | _subwords(info.qualified.split(".")[-1])
        for tl in lows:
            if tl == hay_name or tl in hay_qual or (len(tl) >= 4 and tl in hay_doc):
                matched.add(nid)
                break
        else:
            if sym_words and term_words and (sym_words & term_words):
                matched.add(nid)
    for nid in matched:
        for rel in seed_files_of(nid):
            bump(rel, 4.0, "symbol-name match")

    # -- 3. NEIGHBORHOOD: call-graph expansion of matched symbols --------
    if matched:
        expanded_m: Set[str] = set(matched)
        # one hop is deliberate: the buggy symbol + its direct callers
        # (often the failing test) + direct callees (often the true defect
        # when the matched name is a wrapper)
        for src, dst in calls:
            if src in matched and dst not in matched:
                expanded_m.add(dst)
            if dst in matched and src not in matched:
                expanded_m.add(src)
        for nid in expanded_m:
            for rel in seed_files_of(nid):
                if nid in matched:
                    continue  # already scored at full weight
                bump(rel, 1.0, "call-graph neighbor of matched symbol")
        notes.append(f"{len(matched)} symbol(s) matched issue terms")

    return {"file_scores": file_scores, "symbol_notes": notes, "graph": graph}


def retrieve_context(
    repo_path: str,
    issue_text: str,
    max_files: int = 4,
    max_grep_lines: int = 20,
    target_test: Optional[str] = None,
    index_root: Optional[Path] = None,
) -> dict:
    """Two-layer retrieval: structural (imports/call graph) + grep fallback.

    Assumes repo_path exists and issue_text is the bug report; target_test
    (a pytest node id "file.py::test_name" for Python repos, or a
    "<file> - <name>" / "<file>::<name>" id for JS/TS vitest/jest repos —
    from task.config, when known) is the strongest
    anchor: the test that encodes the bug leads to the code that has it,
    even when the issue text never names the relevant symbol. Returns
    {"terms", "files", "greps", "strategy"} — files are repo-relative
    posix paths ranked best-first; strategy records which layer(s) ran
    (for the trace + tests). Never raises: structural failure degrades to
    grep-only with strategy="grep (structural layer unavailable)".
    """
    terms = extract_terms(issue_text)

    structural: Optional[Dict[str, Any]] = None
    if target_test or terms:
        structural = _structural_scores(repo_path, terms, target_test, index_root)

    if structural is not None and structural["file_scores"]:
        # merge: structural score + grep score (grep alone ranks poorly on
        # repos where the issue text names nothing)
        grep_scores = _grep_scores(repo_path, terms)
        merged: Dict[str, float] = {}
        for rel, s in structural["file_scores"].items():
            merged[rel] = s * 2.0  # structural carries the repo's real edges
        for rel, s in grep_scores.items():
            merged[rel] = merged.get(rel, 0.0) + s
        ranked = sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))
        files = [rel for rel, _ in ranked[:max_files]]
        strategy = "structural+grep"
        if structural["symbol_notes"]:
            strategy += f" ({'; '.join(structural['symbol_notes'])})"
    else:
        files = rank_files(repo_path, terms, limit=max_files)
        strategy = (
            "grep" if structural is None else "grep (structural layer found nothing)"
        )

    greps = {}
    for f in files:
        greps[f] = search_file_for_lines(repo_path, f, terms)[:max_grep_lines]

    return {"terms": terms, "files": files, "greps": greps, "strategy": strategy}


def _grep_scores(repo_path: str, terms: List[str]) -> Dict[str, float]:
    """Score every candidate file by term overlap (the rank_files scoring,
    returned as a dict instead of a truncated list)."""
    files = _walk_code_files(repo_path)
    lows = [t.lower().strip("./-") for t in terms if len(t.strip("./-")) >= 3]
    out: Dict[str, float] = {}
    for f in files:
        rel = f.relative_to(repo_path).as_posix()
        rel_l = rel.lower()
        score = 0.0
        for tl in lows:
            if tl in rel_l or tl in f.stem.lower():
                score += 2
        try:
            text = f.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            text = ""
        for tl in lows:
            c = text.count(tl)
            if c:
                score += min(c, 5)
        if score > 0:
            if f.suffix in _CODE_EXTS:
                score += 1
            out[rel] = score
    return out
