"""AST symbol scopes and claim resources for parallel editing units.

A claim is what makes parallel work safe: a unit declares the symbols (or the
whole file) it is allowed to touch *before* it edits anything, and a merge
refuses any edit that lands in an unclaimed symbol. Claims are plain strings so
they compose with :class:`runtime.orchestration.ClaimStore`:

``file:<repo-relative path>``            whole-file scope
``symbol:<repo-relative path>::<name>``  one AST symbol (function/class/method)

Symbol extraction is stdlib ``ast`` for Python. JavaScript/TypeScript get a
conservative top-level declaration scan; any other extension yields no symbols,
so a file in that language can only be claimed as a whole file — an honest
degradation rather than a guess.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

MAX_SOURCE_BYTES = 2 * 1024 * 1024

_JS_DECL = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?"
    r"(?:async\s+)?(?:function\s*(?P<fn>\w+)|class\s+(?P<cls>\w+)|"
    r"(?:const|let|var)\s+(?P<var>\w+)\s*=\s*(?:async\s*)?(?:function\b|\())",
    re.MULTILINE,
)
_TS_MEMBER = re.compile(
    r"^\s{2,}(?:public\s+|private\s+|protected\s+|readonly\s+|static\s+|async\s+)*"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*\(",
    re.MULTILINE,
)
_PY_SUFFIXES = {".py", ".pyi"}
_JS_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}


def normalize_repo_path(value: Any) -> str:
    """Return a repo-relative, forward-slash path for claim and patch use."""
    text = str(value or "").replace("\\", "/").strip()
    if not text:
        return ""
    for marker in ("a/", "b/"):
        if text.startswith(marker) and len(text) > 2:
            text = text[2:]
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def file_claim(path: str) -> str:
    """Return the whole-file claim resource for a repo-relative path."""
    normalized = normalize_repo_path(path)
    return f"file:{normalized}" if normalized else ""


def symbol_claim(path: str, symbol: str) -> str:
    """Return the claim resource for one AST symbol in a repo-relative path."""
    normalized = normalize_repo_path(path)
    name = str(symbol or "").strip()
    if not normalized or not name:
        return ""
    return f"symbol:{normalized}::{name}"


def claim_symbol_name(resource: str) -> str:
    """Return the symbol name carried by a claim resource, or an empty string."""
    text = str(resource or "")
    if not text.startswith("symbol:") or "::" not in text:
        return ""
    return text.split("::", 1)[1]


def claim_path(resource: str) -> str:
    """Return the repo-relative path a claim resource refers to."""
    text = str(resource or "")
    if text.startswith("file:"):
        return normalize_repo_path(text[5:])
    if text.startswith("symbol:") and "::" in text:
        return normalize_repo_path(text[7:].split("::", 1)[0])
    return ""


def claim_resources(
    files: Sequence[str],
    *,
    symbols: Optional[Mapping[str, Sequence[str]]] = None,
    whole_file: bool = False,
) -> Tuple[str, ...]:
    """Build the ordered claim resources for a set of files and symbols.

    ``whole_file=True`` claims each file as a unit (used by a unit that owns
    a file end to end). Otherwise, when a symbol map is supplied, only those
    symbols are claimed; a file with no symbol entry still gets a whole-file
    claim so its scope is never accidentally empty.
    """
    resources: List[str] = []
    for path in files or ():
        normalized = normalize_repo_path(path)
        if not normalized:
            continue
        names = list((symbols or {}).get(normalized, ()) or ())
        if not names or whole_file:
            candidate = file_claim(normalized)
            if candidate and candidate not in resources:
                resources.append(candidate)
            continue
        for name in names:
            candidate = symbol_claim(normalized, name)
            if candidate and candidate not in resources:
                resources.append(candidate)
    return tuple(resources)


@dataclass(frozen=True)
class SymbolSpan:
    """One source symbol with the line range it owns."""

    name: str
    kind: str
    start_line: int
    end_line: int

    def contains(self, line: int) -> bool:
        """Return whether a 1-based line falls inside this symbol's range."""
        return self.start_line <= int(line) <= self.end_line

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible span."""
        return {
            "name": self.name,
            "kind": self.kind,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }


def symbols_in_source(text: str, path: str = "") -> Tuple[SymbolSpan, ...]:
    """Return the symbols declared in one source file.

    Never raises: an unparseable file yields an empty tuple so the caller can
    fall back to a whole-file claim instead of guessing symbol boundaries.
    """
    source = str(text or "")
    if not source.strip():
        return ()
    suffix = Path(str(path or "")).suffix.lower()
    if suffix in _PY_SUFFIXES or (not suffix and _looks_like_python(source)):
        return _python_symbols(source)
    if suffix in _JS_SUFFIXES:
        return _javascript_symbols(source)
    return ()


def symbols_in_file(path: str | Path) -> Tuple[SymbolSpan, ...]:
    """Return the symbols declared in a file on disk, or an empty tuple."""
    target = Path(path)
    try:
        if not target.is_file() or target.stat().st_size > MAX_SOURCE_BYTES:
            return ()
        return symbols_in_source(
            target.read_text(encoding="utf-8", errors="replace"), str(target)
        )
    except OSError:
        return ()


def symbols_by_file(
    paths: Iterable[str], *, root: str | Path | None = None
) -> Dict[str, Tuple[str, ...]]:
    """Map repo-relative paths to their declared symbol names."""
    base = Path(root) if root is not None else None
    selected: Dict[str, Tuple[str, ...]] = {}
    for path in paths or ():
        normalized = normalize_repo_path(path)
        if not normalized or normalized in selected:
            continue
        full = (base / normalized) if base is not None else Path(normalized)
        selected[normalized] = tuple(span.name for span in symbols_in_file(full))
    return selected


def enclosing_symbol(spans: Sequence[SymbolSpan], line: int) -> str:
    """Return the innermost symbol containing a line, or an empty string.

    Ties break toward the narrowest span, so a method wins over its class.
    """
    best: Optional[SymbolSpan] = None
    for span in spans or ():
        if not span.contains(line):
            continue
        if best is None:
            best = span
            continue
        width = span.end_line - span.start_line
        best_width = best.end_line - best.start_line
        if width < best_width or (
            width == best_width and span.start_line > best.start_line
        ):
            best = span
    return best.name if best is not None else ""


def parse_patch_files(patch: str) -> Dict[str, Tuple[int, ...]]:
    """Return the new-side lines each file adds or removes in a unified diff.

    Keys are repo-relative paths; values are 1-based line numbers on the
    post-image side, which is what a symbol span is expressed in.
    """
    lines: Dict[str, List[int]] = {}
    current = ""
    new_line = 0
    for raw in str(patch or "").splitlines():
        if raw.startswith("diff --git "):
            current = _path_from_diff_header(raw)
            lines.setdefault(current, [])
            new_line = 0
            continue
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            if target == "/dev/null":
                current = ""
                continue
            current = normalize_repo_path(target)
            lines.setdefault(current, [])
            continue
        if raw.startswith("--- "):
            continue
        if raw.startswith("@@"):
            new_line = _hunk_new_start(raw)
            continue
        if not current:
            continue
        if raw.startswith("+"):
            lines[current].append(new_line)
            new_line += 1
        elif raw.startswith("-") or raw.startswith("\\"):
            continue
        else:
            new_line += 1
    return {key: tuple(sorted(set(value))) for key, value in lines.items() if key}


def unclaimed_symbol_edits(
    patch: str,
    claimed: Sequence[str],
    *,
    root: str | Path | None = None,
    spans_by_path: Optional[Mapping[str, Sequence[SymbolSpan]]] = None,
) -> List[Dict[str, Any]]:
    """Return every symbol (or file) a patch edits that no claim covers.

    A whole-file claim covers every line of that file. A symbol claim covers
    the lines inside that symbol only, so module-level code and sibling symbols
    in the same file are still violations. A file with no claim at all is a
    violation on its first changed line.
    """
    resources = {str(item) for item in claimed or ()}
    whole_files = {claim_path(item) for item in resources if item.startswith("file:")}
    symbol_map: Dict[str, set[str]] = {}
    for item in resources:
        name = claim_symbol_name(item)
        if name:
            symbol_map.setdefault(claim_path(item), set()).add(name)
    violations: List[Dict[str, Any]] = []
    for path, changed in sorted(parse_patch_files(patch).items()):
        if not changed:
            continue
        if path in whole_files:
            continue
        spans = (
            list((spans_by_path or {}).get(path, ()))
            if spans_by_path is not None
            else list(_spans_for_path(path, root))
        )
        allowed = symbol_map.get(path, set())
        for line in changed:
            symbol = enclosing_symbol(spans, line)
            if symbol and symbol in allowed:
                continue
            violations.append(
                {
                    "path": path,
                    "line": int(line),
                    "symbol": symbol,
                    "reason": "unclaimed_file" if not symbol else "unclaimed_symbol",
                }
            )
    return violations


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _python_symbols(source: str) -> Tuple[SymbolSpan, ...]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return ()
    spans: List[SymbolSpan] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                start = int(getattr(child, "lineno", 0))
                end = int(getattr(child, "end_lineno", start) or start)
                kind = "class" if isinstance(child, ast.ClassDef) else "function"
                spans.append(SymbolSpan(name, kind, start, end))
                visit(child, f"{name}.")
                continue
            if isinstance(child, (ast.If, ast.Try, ast.With)):
                visit(child, prefix)

    visit(tree, "")
    return tuple(sorted(spans, key=lambda item: (item.start_line, -(item.end_line))))


def _javascript_symbols(source: str) -> Tuple[SymbolSpan, ...]:
    spans: List[SymbolSpan] = []
    lines = source.splitlines()
    for index, line in enumerate(lines, start=1):
        match = _JS_DECL.match(line)
        if not match:
            continue
        name = match.group("fn") or match.group("cls") or match.group("var")
        if not name:
            continue
        kind = "class" if match.group("cls") else "function"
        spans.append(SymbolSpan(name, kind, index, _block_end(lines, index)))
    for match in _TS_MEMBER.finditer(source):
        line = source.count("\n", 0, match.start()) + 1
        name = match.group("name")
        if name in {"if", "for", "while", "switch", "catch", "return", "function"}:
            continue
        if any(
            span.name == name and span.start_line <= line <= span.end_line
            for span in spans
        ):
            continue
        spans.append(SymbolSpan(name, "method", line, _block_end(lines, line)))
    return tuple(sorted(spans, key=lambda item: (item.start_line, -(item.end_line))))


def _block_end(lines: Sequence[str], start: int) -> int:
    depth = 0
    opened = False
    for index in range(max(1, start), len(lines) + 1):
        line = lines[index - 1]
        depth += line.count("{") - line.count("}")
        if "{" in line:
            opened = True
        if opened and depth <= 0:
            return index
    return max(1, start)


def _looks_like_python(source: str) -> bool:
    head = "\n".join(source.splitlines()[:40])
    return bool(re.search(r"^\s*(def|class|import|from)\s", head, re.MULTILINE))


def _spans_for_path(path: str, root: Optional[str | Path]) -> Tuple[SymbolSpan, ...]:
    base = Path(root) if root is not None else None
    full = (base / path) if base is not None else Path(path)
    return symbols_in_file(full)


def _path_from_diff_header(line: str) -> str:
    parts = line.split()
    if len(parts) >= 4:
        return normalize_repo_path(parts[3])
    return ""


def _hunk_new_start(header: str) -> int:
    match = re.search(r"\+(\d+)", header)
    return int(match.group(1)) if match else 1


__all__ = [
    "MAX_SOURCE_BYTES",
    "SymbolSpan",
    "claim_path",
    "claim_resources",
    "claim_symbol_name",
    "enclosing_symbol",
    "file_claim",
    "normalize_repo_path",
    "parse_patch_files",
    "symbol_claim",
    "symbols_by_file",
    "symbols_in_file",
    "symbols_in_source",
    "unclaimed_symbol_edits",
]
