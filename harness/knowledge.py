"""Run-scoped knowledge binding: compiled context, symbols, LSP, memory capture.

This module is the *wiring* layer between four capabilities that already exist
separately in this repository and the one agent path that has to use them:

- ``harness.context_compiler.ContextCompiler`` compiles instruction files,
  role policy, retrieval, citations and budgets into one cited bundle.
- ``harness.retrieval`` resolves symbols, definitions, references and blast
  radius from the tree-sitter index built by ``memory.code_graph``.
- ``harness.lsp.LspManager`` drives an optional language server for real
  diagnostics.
- ``memory.decision_store.DecisionStore`` is the durable, provenance-carrying
  memory that later sessions read back.

Nothing here duplicates any of those implementations. ``KnowledgeContext``
owns exactly one instance of each per run, compiles ONCE, and exposes the
result as a block that the caller injects into every model request.

Design rules that callers depend on:

1. **Compile once per run.** :meth:`KnowledgeContext.compile` is idempotent
   and cached by the compiler's own request digest. A later call with
   ``force=True`` recompiles; a normal call returns the cached bundle. The
   returned receipt carries the sources and the token cost, so a caller can
   emit a trace event without re-deriving anything.
2. **Degrade, never raise.** Every public method returns a structured value
   (or an empty one) instead of propagating. A missing language server, a
   missing memory module, a missing index, or a corrupt store degrades the
   capability and records a warning; it never fails a run.
3. **No writes outside the harness's own log/index roots.** The compiler,
   retrieval, and the memory store all resolve their own roots; this module
   only passes a repository path through. The original repository is never
   mutated by reading context, resolving symbols, or recording memory.
4. **Untrusted memory writes are gated, not sanitized into silence.** A record
   is routed through ``shared.security.authorize_memory_write`` first, so a
   row that claims system authority is quarantined, a secret is redacted, and
   a write with no provenance is refused.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from harness import retrieval
from harness.context_compiler import (
    ContextBundle,
    ContextCompiler,
    trace_receipt,
)

__all__ = [
    "KnowledgeContext",
    "knowledge_receipt",
    "memory_provenance",
    "render_diagnostics_note",
    "render_symbol_result",
    "render_truncation_note",
]

# The default text budget for a symbol-shaped tool result. A tool result is a
# model-facing message, so it is bounded; the caller can raise the cap through
# ``knowledge_tool_max_chars``.
_DEFAULT_TOOL_CHARS = 6000

# Provenance categories a record may declare. An unlisted category is coerced
# to "general" rather than rejected: the gate's job is to stop authority
# claims and secrets, not to police vocabulary.
_KNOWN_CATEGORIES = frozenset(
    {
        "convention",
        "gotcha",
        "general",
        "preference",
        "architecture",
        "environment",
        "gotchas",
        "conventions",
    }
)

# Claims that are not evidence. A record asserting an unverified result is
# downgraded to an observation, never stored as a durable decision, because a
# later session would read it back as a settled fact.
_UNVERIFIED_CLAIM_PATTERNS = (
    re.compile(
        r"(?i)\b(?:i\s+)?(?:verified|confirmed|proved|proven)\b[^.\n]{0,40}\b(?:all\s+)?tests?\b"
    ),
    re.compile(
        r"(?i)\b(?:tests?|suite|build|ci)\s+(?:now\s+)?(?:all\s+)?pass(?:es|ed|ing)?\b"
    ),
    re.compile(
        r"(?i)\b(?:this|the)\s+(?:fix|change|patch)\s+(?:is|was)\s+(?:correct|right|good)\b"
    ),
    re.compile(r"(?i)\bdefinitely\b|\bcertainly\b|\b100%\s+(?:sure|certain)\b"),
    re.compile(r"(?i)\bshould\s+(?:always|never)\s+work\b"),
    re.compile(r"(?i)\bno\s+longer\s+(?:broken|needed|required)\b"),
)

_IDENTIFIER_SPLIT = re.compile(r"[^0-9A-Za-z]+")


def _as_text(value: Any) -> str:
    """Return a trimmed, non-``None`` string for any input."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _bounded(value: Any, limit: int) -> str:
    """Bound a rendered string, marking the truncation explicitly."""
    text = _as_text(value)
    cap = max(0, int(limit or 0))
    if cap == 0 or len(text) <= cap:
        return text
    return text[: max(0, cap - 24)] + "\n...[truncated]"


def _int(value: Any, default: int, *, low: int = 0, high: int = 10_000) -> int:
    """Coerce an argument to a bounded integer without raising."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return int(default)
    return max(int(low), min(int(high), number))


def _normalize_category(value: Any) -> str:
    """Return a known memory category, coercing anything unknown to general."""
    text = _as_text(value).casefold().replace("-", "_")
    if text in _KNOWN_CATEGORIES:
        return text
    singular = (
        text[:-1] if text.endswith("s") and text[:-1] in _KNOWN_CATEGORIES else text
    )
    return singular if singular in _KNOWN_CATEGORIES else "general"


def _has_unverified_claim(text: str) -> bool:
    """Return whether text asserts an outcome instead of reporting one.

    Only certainty language counts. A row that *reports* "the suite failed on
    ``test_x``" is evidence; a row that *claims* "the tests pass now" is a
    claim no later session can check, and is stored as an observation instead.
    """
    return any(pattern.search(text) for pattern in _UNVERIFIED_CLAIM_PATTERNS)


def memory_provenance(
    *,
    repo_path: Any = "",
    session_id: Any = "",
    run_id: Any = "",
    task_id: Any = "",
    source: Any = "agent",
    model: Any = "",
    provider: Any = "",
    verified: Optional[bool] = None,
) -> Dict[str, Any]:
    """Build the provenance block every memory record must carry.

    The block names where the row came from (repository, session, run, task),
    who produced it (model/provider, or an explicit source label), when it was
    captured, and whether it is evidence or an observation. ``timestamp`` is
    epoch seconds; ``iso_timestamp`` is the same instant in UTC so a row read
    by a human or an external client is self-describing.
    """
    captured = time.time()
    provenance: Dict[str, Any] = {
        "kind": "agent-tool",
        "source": _as_text(source) or "agent",
        "repo_path": _as_text(repo_path),
        "session_id": _as_text(session_id),
        "run_id": _as_text(run_id),
        "task_id": _as_text(task_id),
        "model": _as_text(model),
        "provider": _as_text(provider),
        "timestamp": captured,
        "iso_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(captured)),
        "explicit": True,
    }
    provenance["verified"] = bool(verified) if verified is not None else None
    return provenance


def render_diagnostics_note(
    diagnostics: Sequence[Mapping[str, Any]],
    *,
    title: str = "LSP diagnostics (language server, live)",
    max_chars: int = 2000,
) -> str:
    """Render language-server diagnostics as one bounded model-facing block.

    Empty input renders as an explicit "cleared" line rather than an empty
    string, so the model can tell the difference between "the server reported
    nothing" and "nothing was ever checked".
    """
    items = [dict(item) for item in diagnostics or [] if isinstance(item, Mapping)]
    if not items:
        return f"## {title}\n(none reported; the language server reported no findings)"
    lines = [f"## {title}"]
    for item in items[:20]:
        path = _as_text(item.get("path") or item.get("file"))
        line = _int(item.get("line"), 0)
        column = _int(item.get("column"), 0)
        severity = _as_text(item.get("severity")) or "unknown"
        message = _as_text(item.get("message")) or "(no message)"
        source = _as_text(item.get("source"))
        location = path or "(unknown file)"
        if line:
            location = f"{location}:{line}" + (f":{column}" if column else "")
        suffix = f" [{source}]" if source else ""
        lines.append(f"- {severity}{suffix} {location}: {message}")
    if len(items) > 20:
        lines.append(f"- ... and {len(items) - 20} more")
    return _bounded("\n".join(lines), max_chars)


def render_symbol_result(
    records: Sequence[Mapping[str, Any]],
    *,
    header: str,
    empty: str,
    max_chars: int = _DEFAULT_TOOL_CHARS,
) -> str:
    """Render symbol-shaped records as a bounded, cited, model-facing block.

    Every record contributes a ``[cite]`` line carrying its path, line range
    and content digest, so a symbol the model reads is attributable to the
    exact bytes it came from.
    """
    items = [dict(item) for item in records or [] if isinstance(item, Mapping)]
    if not items:
        return empty
    lines = [f"## {header}"]
    for item in items:
        citation = (
            item.get("citation") if isinstance(item.get("citation"), Mapping) else {}
        )
        path = _as_text(item.get("path") or item.get("file") or citation.get("path"))
        line = _int(item.get("line") or citation.get("line"), 0)
        end = _int(item.get("end_line") or citation.get("end_line"), 0)
        digest = _as_text(item.get("digest") or citation.get("digest"))
        name = _as_text(item.get("qualified") or item.get("name"))
        where = f"{path}:{line}" if line else (path or "(unknown file)")
        if end and end != line:
            where = f"{path}:{line}-{end}"
        cite = f"[cite:{digest[:12]}]" if digest else "[cite:unavailable]"
        body = _as_text(item.get("text") or item.get("content"))
        header_line = f"### {name or '(unnamed)'} at {where} {cite}"
        if body:
            lines.append(header_line)
            lines.append("```")
            lines.append(body)
            lines.append("```")
        else:
            lines.append(header_line)
            extra = _as_text(item.get("kind") or item.get("relation"))
            if extra:
                lines.append(f"({extra})")
    return _bounded("\n".join(lines), max_chars)


def _citation_line(citation: Mapping[str, Any]) -> str:
    """Return a compact one-line citation label for a record."""
    if not isinstance(citation, Mapping):
        return "[cite:unavailable]"
    path = _as_text(citation.get("path"))
    line = _int(citation.get("line"), 0)
    digest = _as_text(citation.get("digest"))
    where = f"{path}:{line}" if path and line else (path or "unknown")
    return f"{where} [cite:{digest[:12] if digest else 'unavailable'}]"


class KnowledgeContext:
    """Bind compiled context, symbols, an optional LSP, and memory capture.

    One instance per run. Construct it with the run's repository, config and
    identity; call :meth:`compile` once; inject :meth:`context_block` into
    every model request; call :meth:`note_edit` after each mutation so live
    diagnostics reach the next turn; and close it when the run ends.
    """

    def __init__(
        self,
        repo_path: Any = None,
        config: Optional[Mapping[str, Any]] = None,
        *,
        run_id: Any = "",
        session_id: Any = "",
        task_id: Any = "",
        model: Any = "",
        provider: Any = "",
        issue_text: Any = "",
        target_test: Any = "",
        compiler: Any = None,
        lsp_manager: Any = None,
        event_hook: Any = None,
    ) -> None:
        self.repo_path = _as_text(repo_path)
        self.config = dict(config or {})
        self.run_id = _as_text(run_id)
        self.session_id = _as_text(session_id)
        self.task_id = _as_text(task_id)
        self.model = _as_text(model)
        self.provider = _as_text(provider)
        self.issue_text = _as_text(issue_text)
        self.target_test = _as_text(target_test)
        self.warnings: List[str] = []
        self._compiler = compiler
        self._lsp = lsp_manager
        self._owns_lsp = lsp_manager is None
        self._event_hook = event_hook if callable(event_hook) else None
        self._bundle: Optional[ContextBundle] = None
        self._receipt: Dict[str, Any] = {}
        self._compile_attempts = 0
        self._lsp_started = False
        self._open_documents: Dict[str, int] = {}
        self._pending_diagnostics: List[Dict[str, Any]] = []
        self._store: Any = None
        self._store_attempted = False
        self._recorded_ids: List[int] = []
        self._retrieval_receipt: Dict[str, Any] = {}
        self.stats: Dict[str, int] = {
            "compiles": 0,
            "cache_hits": 0,
            "symbol_lookups": 0,
            "lsp_opens": 0,
            "lsp_updates": 0,
            "diagnostics_seen": 0,
            "memory_records": 0,
            "memory_refusals": 0,
            "retrievals": 0,
            "retrievals_truncated": 0,
        }

    # ------------------------------------------------------------------
    # capability flags
    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        """Whether compiled context should be injected at all.

        The OFF arm is a single config key, so the ablation that measures
        knowledge injection differs from the ON arm by exactly that key.
        """
        return bool(self.config.get("knowledge_enabled", True))

    @property
    def lsp_enabled(self) -> bool:
        """Whether a language server may be started for this run."""
        return bool(self.config.get("lsp_enabled", False))

    @property
    def memory_enabled(self) -> bool:
        """Whether the run may record durable memory."""
        return bool(self.config.get("memory_record_enabled", True))

    @property
    def bundle(self) -> Optional[ContextBundle]:
        """Return the compiled bundle, or ``None`` before :meth:`compile`."""
        return self._bundle

    @property
    def receipt(self) -> Dict[str, Any]:
        """Return the context trace receipt for the last compile."""
        return dict(self._receipt)

    def _warn(self, message: str) -> str:
        text = _as_text(message)
        if text and text not in self.warnings:
            self.warnings.append(text)
        return text

    def _emit(self, event: str, payload: Mapping[str, Any]) -> None:
        """Send an observation to the caller's event sink; never raise."""
        if self._event_hook is None:
            return
        try:
            self._event_hook(event, dict(payload))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 1. compiled context
    # ------------------------------------------------------------------
    def _compiler_instance(self) -> Optional[ContextCompiler]:
        if self._compiler is None:
            try:
                self._compiler = ContextCompiler(
                    self.repo_path,
                    config=self.config,
                    lsp_manager=self._lsp,
                )
            except Exception as exc:
                self._warn(f"context compiler unavailable: {type(exc).__name__}")
                return None
        return self._compiler

    def compile(
        self,
        *,
        issue_text: Any = "",
        target_test: Any = "",
        force: bool = False,
    ) -> Dict[str, Any]:
        """Compile the run's context bundle once and return its receipt.

        The receipt carries the sources that were included or omitted and the
        token cost of the bundle, which is exactly what a ``context`` trace
        event needs. It is safe to call every turn: the compiler's own request
        digest makes the second call a cache hit and the compiled text is
        identical, so the injected block never churns between turns.

        A compile failure is not a run failure. The receipt then reports
        ``compiled: False`` with the reason, and :meth:`context_block` returns
        an empty string.
        """
        if not self.enabled:
            self._receipt = {
                "compiled": False,
                "skipped": "knowledge_enabled=False",
                "sources": [],
                "estimated_tokens": 0,
            }
            return dict(self._receipt)
        if self._bundle is not None and not force:
            self.stats["cache_hits"] += 1
            memo = dict(self._receipt)
            memo["memo_hit"] = True
            return memo
        compiler = self._compiler_instance()
        if compiler is None:
            self._receipt = {
                "compiled": False,
                "skipped": "compiler unavailable",
                "sources": [],
                "estimated_tokens": 0,
            }
            return dict(self._receipt)
        self._compile_attempts += 1
        issue = _as_text(issue_text) or self.issue_text
        target = _as_text(target_test) or self.target_test
        try:
            bundle = compiler.compile(
                issue_text=issue,
                target_test=target or None,
                lsp_manager=self._lsp,
                use_cache=True,
            )
        except Exception as exc:
            reason = f"context compile failed: {type(exc).__name__}"
            self._warn(reason)
            self._bundle = None
            self._receipt = {
                "compiled": False,
                "skipped": reason,
                "sources": [],
                "estimated_tokens": 0,
            }
            self._emit("context_compile_failed", {"reason": reason})
            return dict(self._receipt)
        self._bundle = bundle
        self.stats["compiles"] += 1
        receipt = trace_receipt(bundle)
        receipt["compiled"] = True
        receipt["memo_hit"] = False
        receipt["sources"] = [
            {
                "source": section.get("source") or section.get("name"),
                "included": bool(section.get("included")),
                "truncated": bool(section.get("truncated")),
                "reason": _as_text(section.get("reason")),
                "citation_ids": list(section.get("citation_ids") or []),
            }
            for section in receipt.get("sections") or []
        ]
        receipt["tokens"] = receipt.get("estimated_tokens", 0)
        receipt["chars"] = len(bundle.text)
        receipt["cache_hit"] = bool(bundle.cache_hit)
        receipt["compacted"] = bool(bundle.compacted)
        receipt["compaction_metadata"] = {
            "compacted": bool(bundle.compacted),
            "omitted": list(bundle.omitted or []),
            "source_reference_count": len(bundle.source_references or []),
            "citation_count": len(bundle.citations or []),
            "cache_key": bundle.cache_key,
            "source_digest": bundle.source_digest,
            "index_digest": bundle.index_digest,
        }
        self._receipt = receipt
        for warning in bundle.warnings or []:
            self._warn(f"compiler: {warning}")
        self._emit("context_compiled", receipt)
        return dict(receipt)

    def context_block(self, *, title: str = "Compiled repository context") -> str:
        """Return the compiled bundle as one bounded block for the model.

        The block is the same string for every request in the run: it is
        compiled once and injected into the base frame, so later turns inherit
        it as a prefix rather than re-deriving it. It renders empty (not an
        error) when compilation is disabled or unavailable.
        """
        if not self.enabled or self._bundle is None:
            return ""
        text = _as_text(self._bundle.text)
        if not text:
            return ""
        limit = _int(
            self.config.get("knowledge_block_max_chars"),
            24_000,
            low=200,
            high=200_000,
        )
        return _bounded(f"## {title}\n{text}", limit)

    def citation_index(self) -> List[str]:
        """Return one compact label per citation in the compiled bundle."""
        if self._bundle is None:
            return []
        return [
            _citation_line(citation)
            for citation in (self._bundle.citations or [])
            if isinstance(citation, Mapping)
        ]

    def skill_receipt(self) -> Dict[str, Any]:
        """Return the skills receipt for the COMPILED bundle, or ``{}``.

        The daily path delivers skills through the context compiler, not
        through a caller's pre-built receipt, so a caller-supplied receipt
        cannot describe what this run actually used. This derives the receipt
        from the SAME bundle whose text was injected, which is the only
        version of the claim that cannot drift from the delivery.

        ``model_content`` is set only when a skill section was INCLUDED in the
        compiled text. A discovered-but-unincluded skill reports
        ``discovered`` with ``model_content: false`` — "we looked" and "the
        model saw it" are different facts and the receipt keeps them apart.
        Returns ``{}`` when nothing was compiled, so a caller that gates on
        truthiness cannot publish a receipt for a run that injected no skills.
        """
        bundle = self._bundle
        if bundle is None:
            return {}
        from harness.skills import NONE_MATCHED

        section = next(
            (
                item
                for item in (bundle.sections or [])
                if isinstance(item, Mapping) and item.get("name") == "skills"
            ),
            None,
        )
        section_text = _as_text(section.get("text")) if section else ""
        # `## Applicable skills\n(none matched)` is the compiler's way of
        # saying the scan found nothing. A receipt that read it as a delivered
        # skill would claim model content that does not exist, which is the
        # exact defect class this receipt exists to prevent — so the
        # placeholder is the same authority `harness.skills` treats it as.
        delivered = bool(section_text) and NONE_MATCHED not in section_text
        references = (
            [
                reference
                for reference in (bundle.source_references or [])
                if isinstance(reference, Mapping) and reference.get("source") == "skill"
            ]
            if delivered
            else []
        )
        if section is None and not references:
            return {}
        source_row = next(
            (
                row
                for row in (self._receipt.get("sources") or [])
                if isinstance(row, Mapping) and row.get("source") == "skills"
            ),
            {},
        )
        receipts = [
            {
                "name": str(reference.get("name") or "skill"),
                "source": str(reference.get("path") or "skills"),
                "digest": str(reference.get("digest") or ""),
                "included": True,
                "truncated": bool(source_row.get("truncated")),
            }
            for reference in references
        ]
        return {
            "matched": [row["name"] for row in receipts],
            "considered": len(receipts),
            "receipts": receipts,
            "rendered": [row["name"] for row in receipts],
            "omitted": [],
            "section_chars": len(section_text),
            "skipped": None if delivered else "no skill matched",
            "error": None,
            "tainted": [],
            "quarantined": [],
            "model_content": delivered,
            "declarations": {},
            "provenance": "compiled_context_bundle",
            "included": delivered,
            "reason": str(source_row.get("reason") or ""),
        }

    # ------------------------------------------------------------------
    # 2. symbol-level tools
    # ------------------------------------------------------------------
    @property
    def _index_root(self) -> Optional[Path]:
        value = self.config.get("index_root") or self.config.get("_code_graph_root")
        return Path(value) if value else None

    def _tool_chars(self) -> int:
        return _int(
            self.config.get("knowledge_tool_max_chars"),
            _DEFAULT_TOOL_CHARS,
            low=200,
            high=200_000,
        )

    def read_symbol(
        self,
        symbol: Any,
        path: Any = "",
        *,
        max_lines: Any = 400,
    ) -> str:
        """Return the source of one symbol with its citation and digest.

        Resolves through the tree-sitter index first, so a symbol in a file the
        session has never opened is reachable in one call. An index miss falls
        back to a bounded text search so the tool is not a hard dependency on a
        warm index.
        """
        name = _as_text(symbol)
        if not name:
            return "READ_SYMBOL requires a symbol name."
        self.stats["symbol_lookups"] += 1
        target = _as_text(path)
        found = retrieval.find_definitions(
            self.repo_path,
            name,
            target_file=target or None,
            index_root=self._index_root,
        )
        records = retrieval.read_symbol_records(
            self.repo_path,
            name,
            target_file=target or None,
            index_root=self._index_root,
            limit=_int(max_lines, 400, low=1, high=5000),
        )
        if records:
            return render_symbol_result(
                records,
                header=f"Symbol {name}",
                empty=f"symbol {name!r} not found in the repository index",
                max_chars=self._tool_chars(),
            )
        if not found.get("available"):
            # Honest absence: a cold or unreadable index cannot distinguish
            # "does not exist" from "not indexed yet", so say which one it is.
            return (
                f"symbol {name!r} could not be resolved: the repository index "
                "is unavailable, so this is NOT a claim that the symbol does "
                "not exist. Use grep to confirm."
            )
        fallback = retrieval.read_symbol_by_search(
            self.repo_path, name, index_root=self._index_root
        )
        return render_symbol_result(
            fallback,
            header=f"Symbol {name} (text-search fallback; no index match)",
            empty=(
                f"symbol {name!r} not found by the index or by a text search. "
                "It does not exist under that name in this repository."
            ),
            max_chars=self._tool_chars(),
        )

    def find_definition(self, symbol: Any, path: Any = "") -> str:
        """Return where a symbol is defined, with its citation and digest."""
        name = _as_text(symbol)
        if not name:
            return "FIND_DEFINITION requires a symbol name."
        self.stats["symbol_lookups"] += 1
        found = retrieval.find_definitions(
            self.repo_path,
            name,
            target_file=_as_text(path) or None,
            index_root=self._index_root,
        )
        body = render_symbol_result(
            found["definitions"],
            header=f"Definitions of {name}",
            empty=(
                f"no definition of {name!r} exists in this repository"
                if found.get("available")
                else f"the repository index is unavailable, so the definition "
                f"status of {name!r} is unknown (this is not a claim that it "
                "does not exist)"
            ),
            max_chars=self._tool_chars(),
        )
        if found["ambiguous"]:
            body = (
                f"NOTE: {name!r} is defined in "
                f"{len(found['definitions'])} places. A call site does not tell "
                "you which one applies; read the call site or use "
                "FIND_REFERENCES to disambiguate.\n" + body
            )
        return body

    def find_references(
        self,
        symbol: Any,
        path: Any = "",
        *,
        max_results: Any = 60,
    ) -> str:
        """Return the call sites and importers of a symbol.

        References are the model's substitute for re-reading whole files: a
        symbol's callers tell it which files must be checked, and the
        ``find_references`` -> ``read_symbol`` pair reaches a symbol outside
        the initial file window without a repository-wide scan.
        """
        name = _as_text(symbol)
        if not name:
            return "FIND_REFERENCES requires a symbol name."
        self.stats["symbol_lookups"] += 1
        result = retrieval.find_references(
            self.repo_path,
            name,
            target_file=_as_text(path) or None,
            index_root=self._index_root,
            limit=_int(max_results, 60, low=1, high=500),
        )
        if not result.get("available"):
            return (
                f"reference status for {name!r} is unknown: the repository "
                "index is unavailable. This is not a claim that nothing "
                "references it."
            )
        if not result["definitions"] and not result["references"]:
            return f"no references to {name!r} exist in this repository index."
        lines = []
        if result["definitions"]:
            lines.append(
                render_symbol_result(
                    result["definitions"],
                    header=f"Definition of {name}",
                    empty="",
                    max_chars=self._tool_chars(),
                )
            )
        lines.append(
            render_symbol_result(
                result["references"],
                header=f"References to {name} ({len(result['references'])} shown)",
                empty=f"no call site or importer of {name!r} is indexed",
                max_chars=self._tool_chars(),
            )
        )
        if result["definition_count"] > 1:
            lines.append(
                f"NOTE: {name!r} has {result['definition_count']} definitions; "
                "these references are the union of all of them."
            )
        return _bounded("\n".join(lines), self._tool_chars() * 2)

    def blast_radius(
        self,
        symbol: Any = "",
        *,
        paths: Sequence[Any] = (),
        depth: Any = 1,
        max_files: Any = 25,
    ) -> str:
        """Return the files and symbols a change to ``symbol``/``paths`` can break.

        The answer is read from the index, so it is an over-approximation
        (name-based call resolution) and is labelled as such: it is a list of
        files to check, not a proof of breakage.
        """
        name = _as_text(symbol)
        targets = [_as_text(item) for item in paths or [] if _as_text(item)]
        if not name and not targets:
            return (
                "BLAST_RADIUS requires a symbol name or a paths list. "
                "Call it before renaming or changing a shared signature."
            )
        self.stats["symbol_lookups"] += 1
        result = retrieval.blast_radius_for(
            self.repo_path,
            changed_symbols=[name] if name else [],
            changed_files=targets,
            depth=_int(depth, 1, low=1, high=4),
            limit=_int(max_files, 25, low=1, high=200),
            index_root=self._index_root,
        )
        if not result.get("available"):
            return (
                "blast radius is UNKNOWN: the repository index is unavailable, "
                "so this is not a claim that nothing depends on this target. "
                "Use grep before changing a shared signature."
            )
        if not result["files"] and not result["symbols"]:
            return (
                "no structural dependents are indexed for this target "
                "(an empty result is a real answer only when the index is "
                "warm; otherwise say the index is unavailable)."
            )
        lines = [
            "## Blast radius",
            f"index_digest: {result['index_digest']}",
            f"depth: {result['depth']} (name-based resolution is an "
            "over-approximation: these are files to CHECK, not proven breakage)",
        ]
        for entry in result["symbols"]:
            relation = entry.get("relation", "dependent")
            lines.append(
                f"- {relation}: {entry.get('qualified') or entry.get('name')} "
                f"at {_citation_line(entry.get('citation') or {})}"
            )
        lines.append("### Files to check")
        for item in result["files"]:
            relations = ", ".join(item.get("relations") or []) or "dependent"
            lines.append(f"- {item.get('file')} ({relations})")
        return _bounded("\n".join(lines), self._tool_chars())

    # ------------------------------------------------------------------
    # 3. language server feedback
    # ------------------------------------------------------------------
    def lsp_manager(self) -> Any:
        """Return the run's language-server manager, or ``None``.

        A manager is created lazily so a run that never edits a file never
        pays for a process, and a host with no configured server reports
        ``None`` instead of raising.
        """
        if self._lsp is not None:
            return self._lsp
        if not self.lsp_enabled:
            return None
        try:
            from harness.lsp import LspManager

            self._lsp = LspManager.from_config(
                self.config, repo_path=self.repo_path or "."
            )
        except Exception as exc:
            self._warn(f"language server unavailable: {type(exc).__name__}: {exc}")
            self._lsp = None
        return self._lsp

    def start_lsp(self) -> bool:
        """Start the configured language server; return whether it is up.

        ``False`` is a normal outcome: no server configured, the binary is
        missing, or the handshake timed out. The reason is recorded in
        ``lsp_status`` and the run continues with no diagnostics rather than
        failing.
        """
        manager = self.lsp_manager()
        if manager is None:
            self._lsp_started = False
            return False
        if self._lsp_started:
            return True
        try:
            self._lsp_started = bool(manager.start())
        except Exception as exc:
            self._warn(f"language server start failed: {type(exc).__name__}")
            self._lsp_started = False
        if not self._lsp_started:
            self._warn("language server did not start; diagnostics disabled")
        self._emit("lsp_status", self.lsp_status())
        return self._lsp_started

    def lsp_status(self) -> Dict[str, Any]:
        """Return the honest state of the language-server boundary."""
        manager = self._lsp
        return {
            "enabled": self.lsp_enabled,
            "started": bool(self._lsp_started),
            "configured": bool(manager is not None),
            "open_documents": sorted(self._open_documents),
            "last_error": _as_text(getattr(manager, "last_error", "")),
            "pending_diagnostics": len(self._pending_diagnostics),
        }

    def sync_document(self, relative_path: Any, text: Any = None) -> bool:
        """Open or update one document after an edit.

        The first sync for a path sends ``didOpen``; later syncs send
        ``didChange`` with an incremented version. Returns whether the
        document is open in the server; ``False`` means the boundary is
        unavailable and the caller carries on unchanged.
        """
        manager = self.lsp_manager()
        if manager is None or not self.start_lsp():
            return False
        relative = _as_text(relative_path).replace("\\", "/").strip()
        if not relative:
            return False
        version = self._open_documents.get(relative, 0) + 1
        try:
            if version == 1:
                opened = bool(
                    manager.open_document(relative, text=text, version=version)
                )
            else:
                opened = bool(
                    manager.update_document(relative, text=text, version=version)
                )
        except Exception as exc:
            self._warn(f"lsp document sync failed: {type(exc).__name__}")
            return False
        if not opened:
            return False
        self._open_documents[relative] = version
        self.stats["lsp_opens" if version == 1 else "lsp_updates"] += 1
        return True

    def note_edit(self, relative_path: Any, text: Any = None) -> List[Dict[str, Any]]:
        """Push an edited file to the server and return its fresh diagnostics.

        Called after a mutation tool succeeds. The returned list is the
        server's answer for the NEW content, so the caller can feed genuinely
        new findings into the next model turn and observe them clearing after
        a repair.
        """
        if not self.note_edit_enabled:
            return []
        if not self.sync_document(relative_path, text=text):
            return []
        found = self.collect_diagnostics(relative_path)
        if found:
            self._pending_diagnostics = list(found)
        self._emit(
            "lsp_diagnostics",
            {
                "path": _as_text(relative_path).replace("\\", "/"),
                "count": len(found),
                "diagnostics": found[:10],
            },
        )
        return found

    @property
    def note_edit_enabled(self) -> bool:
        """Whether edits should be pushed to the language server."""
        return bool(self.config.get("lsp_enabled", False))

    def collect_diagnostics(self, relative_path: Any = "") -> List[Dict[str, Any]]:
        """Read diagnostics for one path (or the whole workspace)."""
        manager = self.lsp_manager()
        if manager is None or not self._lsp_started:
            return []
        relative = _as_text(relative_path).replace("\\", "/").strip()
        try:
            found = manager.get_diagnostics(relative or None)
        except Exception as exc:
            self._warn(f"lsp diagnostics unavailable: {type(exc).__name__}")
            return []
        records = [
            item.as_dict() if hasattr(item, "as_dict") else dict(item)
            for item in found or []
        ]
        for record in records:
            record["path"] = _as_text(record.get("path") or record.get("file"))
        self.stats["diagnostics_seen"] += len(records)
        return records

    def diagnostics_note(self, relative_path: Any = "", *, clear: bool = False) -> str:
        """Return the diagnostics block for the next model turn.

        ``clear=True`` reports the CURRENT state for a path, which is how a
        repaired file demonstrates that its finding cleared.
        """
        if clear:
            self._pending_diagnostics = []
        found = self._pending_diagnostics or self.collect_diagnostics(relative_path)
        if not found and clear:
            return render_diagnostics_note([])
        return render_diagnostics_note(
            found,
            max_chars=_int(
                self.config.get("knowledge_diagnostics_max_chars"),
                2000,
                low=200,
                high=20_000,
            ),
        )

    def take_pending_diagnostics(self) -> List[Dict[str, Any]]:
        """Return and clear the diagnostics queued for the next turn."""
        pending = list(self._pending_diagnostics)
        self._pending_diagnostics = []
        return pending

    # ------------------------------------------------------------------
    # 4. memory capture
    # ------------------------------------------------------------------
    def _decision_store(self) -> Any:
        if self._store is not None:
            return self._store
        if self._store_attempted:
            return None
        self._store_attempted = True
        try:
            from memory.decision_store import open_default_store

            self._store = open_default_store()
        except Exception as exc:
            self._warn(f"decision store unavailable: {type(exc).__name__}")
            self._store = None
        return self._store

    def record_memory(
        self,
        text: Any,
        *,
        category: Any = "convention",
        verified: Optional[bool] = None,
        source: Any = "agent",
    ) -> Dict[str, Any]:
        """Record one durable convention, with provenance and honest status.

        The write passes three gates in order:

        1. **Untrusted-content review** (``shared.security``) — a row that
           claims system authority is quarantined, a secret is redacted, and a
           write with no provenance is refused. The gate's decision is
           returned verbatim; it is never downgraded to a warning.
        2. **Claim check** — text asserting an unverified outcome is stored as
           an observation (``category`` is rewritten and ``verified`` is set to
           ``False``) so a later session reads it as a report, not a fact.
        3. **Store record with dedupe** — an identical normalized
           ``(repo, category, text)`` row already present is a no-op and is
           reported as ``deduplicated`` rather than silently doubling.

        Returns a receipt dict. A refusal is a receipt with ``recorded: False``
        and a reason, never an exception.
        """
        raw = _as_text(text)
        receipt: Dict[str, Any] = {
            "recorded": False,
            "text": raw,
            "category": _normalize_category(category),
            "source": _as_text(source) or "agent",
            "repo_path": self.repo_path,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "model": self.model,
            "quarantined": False,
            "deduplicated": False,
            "verified": verified,
        }
        if not self.memory_enabled:
            receipt["reason"] = "memory_record_enabled=False"
            self.stats["memory_refusals"] += 1
            return receipt
        if not raw:
            receipt["reason"] = "empty record"
            self.stats["memory_refusals"] += 1
            return receipt
        if not self.repo_path:
            receipt["reason"] = "no repository identity for this record"
            self.stats["memory_refusals"] += 1
            return receipt
        store = self._decision_store()
        if store is None:
            receipt["reason"] = "decision store unavailable"
            self.stats["memory_refusals"] += 1
            return receipt
        provenance = memory_provenance(
            repo_path=self.repo_path,
            session_id=self.session_id,
            run_id=self.run_id,
            task_id=self.task_id,
            source=receipt["source"],
            model=self.model,
            provider=self.provider,
            verified=verified,
        )
        claim = _has_unverified_claim(raw)
        try:
            from shared.security import authorize_memory_write, contains_secret
        except Exception as exc:  # pragma: no cover - shared is a hard dep
            receipt["reason"] = f"memory gate unavailable: {type(exc).__name__}"
            self.stats["memory_refusals"] += 1
            return receipt
        # The store has its own secret check and returns None for a
        # credential-shaped row. Detecting it here means the refusal NAMES the
        # reason instead of reporting a generic rejection.
        secret = bool(contains_secret(raw))
        receipt["secret"] = secret
        decision = authorize_memory_write(
            raw,
            source=receipt["source"],
            actor="agent",
            provenance=provenance,
        )
        receipt["findings"] = [dict(item) for item in (decision.findings or ())]
        receipt["severity"] = decision.severity
        if decision.quarantined or not decision.allowed:
            receipt["quarantined"] = bool(decision.quarantined)
            receipt["reason"] = decision.reason or "memory write refused"
            self.stats["memory_refusals"] += 1
            self._emit("memory_record_refused", receipt)
            return receipt
        stored_text = _as_text(decision.text) or raw
        if stored_text != raw:
            receipt["redacted"] = True
        if claim and verified is not True:
            receipt["category"] = "observation"
            receipt["claim_downgraded"] = True
        receipt["text"] = stored_text
        # Deduplicate obvious repeats. The store's own dedupe is scoped to one
        # task id, so a convention recorded by an earlier SESSION would still
        # land a second row. The check here is repository- and category-scoped
        # over normalized text, which is what "obvious repeat" means for a fact,
        # and returning the FIRST record's id is what lets a caller prove the
        # earlier record is being reused instead of duplicated.
        existing = self.find_recorded(stored_text, category=receipt["category"])
        if existing is not None:
            receipt["deduplicated"] = True
            receipt["existing_id"] = existing
            receipt["reason"] = f"identical record already present as #{existing}"
            self._emit("memory_record_deduplicated", receipt)
            return receipt
        try:
            row_id = store.record(
                stored_text,
                category=receipt["category"],
                source=receipt["source"],
                task_id=self.task_id or None,
                repo_path=self.repo_path,
                dedupe=True,
                provenance=provenance,
                metadata={
                    "run_id": self.run_id,
                    "session_id": self.session_id,
                    "model": self.model,
                    "claim": bool(claim),
                    "verified": bool(verified) if verified is not None else False,
                },
            )
        except Exception as exc:
            receipt["reason"] = f"record failed: {type(exc).__name__}"
            self.stats["memory_refusals"] += 1
            return receipt
        if row_id is None:
            # The repository/category dedupe above already ran, so a None here
            # is the store refusing the row itself: a credential-shaped text, an
            # unusable field, or a within-task duplicate the store caught first.
            # It is an honest non-record, reported as a refusal that names the
            # reason when the reason is knowable.
            receipt["reason"] = (
                "record carries credential-shaped text and was refused"
                if secret
                else "record rejected by the store"
            )
            self.stats["memory_refusals"] += 1
            self._emit("memory_record_refused", receipt)
            return receipt
        receipt["recorded"] = True
        receipt["id"] = int(row_id)
        receipt["reason"] = ""
        self._recorded_ids.append(int(row_id))
        self.stats["memory_records"] += 1
        self._emit("memory_recorded", receipt)
        return receipt

    def find_recorded(self, text: Any, *, category: Any = "") -> Optional[int]:
        """Return the id of an identical stored record, or ``None``.

        This is the reuse proof: a later session that records the same
        convention gets the FIRST record's id back instead of a duplicate row.
        Matching is normalized-text plus category, so the same sentence stored
        as a convention and as a gotcha stays two distinct records.
        """
        needle = _as_text(text)
        store = self._decision_store()
        if not needle or store is None:
            return None
        wanted_category = _as_text(category)
        normalized = " ".join(needle.split()).casefold()
        probe = normalized[:200]
        try:
            rows = store.search(probe, limit=100, repo_path=self.repo_path or None)
        except Exception:
            return None
        for row in rows or []:
            candidate = " ".join(str(getattr(row, "text", "") or "").split()).casefold()
            if candidate != normalized:
                continue
            if wanted_category and str(getattr(row, "category", "")) != wanted_category:
                continue
            row_id = int(getattr(row, "id", 0) or 0)
            if row_id:
                return row_id
        return None

    def recalled(self, limit: int = 6) -> List[Dict[str, Any]]:
        """Return stored records for this repository, newest first.

        A later session calls this to demonstrate reuse: the record written by
        an earlier run comes back with its provenance intact.
        """
        store = self._decision_store()
        if store is None or not self.repo_path:
            return []
        try:
            rows = store.search(
                "", limit=_int(limit, 6, low=1, high=200), repo_path=self.repo_path
            )
        except Exception:
            return []
        records: List[Dict[str, Any]] = []
        for row in rows or []:
            payload = row.as_dict() if hasattr(row, "as_dict") else dict(row)
            records.append(
                {
                    "id": payload.get("id"),
                    "text": payload.get("text"),
                    "category": payload.get("category"),
                    "source": payload.get("source"),
                    "created_at": payload.get("created_at"),
                    "provenance_kind": payload.get("provenance_kind"),
                }
            )
        return records

    # ------------------------------------------------------------------
    # 1b. budgeted retrieval (R2-09)
    # ------------------------------------------------------------------
    @property
    def retrieval_budget_s(self) -> Optional[float]:
        """Return this run's retrieval wall-clock budget in seconds.

        Read by KEY MEANING, not truthiness: an absent key means "no budget"
        (the historical unbounded behaviour), and an explicit ``0`` means "no
        time at all", which is a measurable OFF arm rather than a silent one.
        The key is deliberately NOT in ``harness/config.py`` ``DEFAULTS`` — a
        default there is merged into every task and every eval arm, so adding
        one would silently switch all of them.

        Assumes the configured value is a number in seconds; a value that
        cannot be read as one degrades to "no budget" and records a warning
        rather than inventing one.
        """
        if "retrieval_budget_s" not in self.config:
            return None
        raw = self.config.get("retrieval_budget_s")
        if raw is None or raw is False:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            self._warn(f"retrieval_budget_s is not usable ({raw!r}); no budget applied")
            return None
        return max(0.0, value)

    @property
    def retrieval_receipt(self) -> Dict[str, Any]:
        """Return the completeness receipt of the last :meth:`retrieve` call.

        Empty before the first call. ``truncated`` is the field a reader must
        check before presenting a retrieval as the whole answer; the rest
        (``truncation``, ``not_searched``, ``not_searched_files``, ``elapsed_s``,
        ``stages``, ``cache_status``) says why and what was skipped.
        """
        return dict(self._retrieval_receipt)

    def retrieve(
        self,
        issue_text: Any = "",
        *,
        target_test: Any = "",
        max_files: Any = 4,
        include_citations: Any = False,
    ) -> Any:
        """Run one budgeted retrieval and return its ``RetrievalOutcome``.

        The budget comes from :attr:`retrieval_budget_s`; an exhausted budget
        returns the best-ranked results computed so far with
        ``truncated: True`` and the reason, and this method NEVER raises — a
        broken retrieval module, an unreadable repository, or an exhausted
        budget all produce a labelled answer.

        Assumes ``self.repo_path`` is the repository to search. Returns
        ``harness.retrieval.RetrievalOutcome``; when the retrieval module
        cannot be imported at all, a synthetic outcome with
        ``truncated: True`` and ``error`` set is returned instead, so a caller
        never has to branch on an import failure.
        """
        text = _as_text(issue_text) or self.issue_text
        target = _as_text(target_test) or self.target_test
        try:
            files = max(1, min(20, int(max_files)))
        except (TypeError, ValueError):
            files = 4
        self.stats["retrievals"] = int(self.stats.get("retrievals", 0)) + 1
        try:
            from harness import retrieval as _retrieval
        except Exception as exc:  # pragma: no cover - import guard
            reason = f"retrieval unavailable: {type(exc).__name__}"
            self._warn(reason)
            self._retrieval_receipt = {
                "truncated": True,
                "truncation": _retrieval_error_label(),
                "not_searched": 0,
                "not_searched_files": [],
                "elapsed_s": 0.0,
                "stages": {},
                "error": reason,
            }
            self._emit("retrieval_truncated", dict(self._retrieval_receipt))
            return _empty_outcome(reason)
        try:
            outcome = _retrieval.retrieve_context_budgeted(
                self.repo_path,
                text,
                max_files=files,
                target_test=target or None,
                include_citations=bool(include_citations),
                budget_s=self.retrieval_budget_s,
            )
        except Exception as exc:
            reason = f"retrieval failed: {type(exc).__name__}: {exc}"
            self._warn(reason)
            self._retrieval_receipt = {
                "truncated": True,
                "truncation": _retrieval_error_label(),
                "not_searched": 0,
                "not_searched_files": [],
                "elapsed_s": 0.0,
                "stages": {},
                "error": reason,
            }
            self._emit("retrieval_truncated", dict(self._retrieval_receipt))
            return _empty_outcome(reason)
        self._retrieval_receipt = dict(outcome.receipt)
        if outcome.truncated:
            self.stats["retrievals_truncated"] = (
                int(self.stats.get("retrievals_truncated", 0)) + 1
            )
            self._emit("retrieval_truncated", dict(outcome.receipt))
        else:
            self._emit("retrieval", dict(outcome.receipt))
        return outcome

    # ------------------------------------------------------------------
    # teardown
    # ------------------------------------------------------------------
    def close(self) -> Dict[str, Any]:
        """Shut the language server and the store down; return a receipt.

        A close failure is reported, never raised: teardown must not change a
        run's outcome. The receipt carries the retrieval completeness label so
        a run that ended on a partial retrieval is visible at teardown.
        """
        receipt: Dict[str, Any] = {
            "lsp_shutdown": False,
            "store_closed": False,
            "warnings": list(self.warnings),
            "stats": dict(self.stats),
            "retrieval": self.retrieval_receipt,
            "retrieval_truncated": bool(self._retrieval_receipt.get("truncated")),
        }
        manager = self._lsp
        if manager is not None and self._owns_lsp:
            try:
                receipt["lsp_shutdown"] = bool(manager.shutdown())
            except Exception as exc:
                self._warn(f"language server shutdown failed: {type(exc).__name__}")
        elif manager is not None:
            receipt["lsp_shutdown"] = False
            receipt["lsp_note"] = "caller-owned language server left running"
        if self._store is not None:
            try:
                self._store.close()
                receipt["store_closed"] = True
            except Exception as exc:
                self._warn(f"decision store close failed: {type(exc).__name__}")
        self._lsp_started = False
        self._open_documents = {}
        return receipt

    def as_dict(self) -> Dict[str, Any]:
        """Return a serializable, secret-free description of this binding."""
        return {
            "repo_path": self.repo_path,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "model": self.model,
            "enabled": self.enabled,
            "lsp": self.lsp_status(),
            "warnings": list(self.warnings),
            "stats": dict(self.stats),
            "receipt": self.receipt,
            "compiled": self._bundle is not None,
            "retrieval": self.retrieval_receipt,
        }


def _retrieval_error_label() -> str:
    """Return the truncation label for a retrieval that could not run at all."""
    try:
        from harness.retrieval import TRUNCATION_ERROR

        return TRUNCATION_ERROR
    except Exception:  # pragma: no cover - defensive
        return "error"


def _empty_outcome(reason: str) -> Any:
    """Return a labelled empty outcome without importing harness.retrieval.

    Used only when the retrieval module itself is unusable, so that a broken
    retrieval still produces a typed, honest answer for the caller to render.
    """
    try:
        from harness.retrieval import RetrievalOutcome
    except Exception:  # pragma: no cover - defensive
        return {
            "terms": [],
            "files": [],
            "greps": [],
            "strategy": "unavailable",
            "truncated": True,
            "receipt": {"truncated": True, "error": reason},
        }
    return RetrievalOutcome(
        result={"terms": [], "files": [], "greps": [], "strategy": "unavailable"},
        receipt={"truncated": True, "truncation": "error", "error": reason},
    )


def render_truncation_note(outcome: Any, *, title: str = "RETRIEVAL") -> str:
    """Render the model-facing note for a partial retrieval, or '' when complete.

    The point of this function is that a bounded retrieval must never be
    rendered as a whole answer. It accepts a ``RetrievalOutcome``, a plain
    mapping with a ``truncated`` key, or ``None``, and never raises, so a
    caller can pipe whatever it has through it.

    The text names the cause and the scope: which stage stopped, how many
    candidates were never searched, and up to five of their paths. A caller
    that renders only the files would otherwise be presenting a partial
    ranking as a complete one.
    """
    receipt: Mapping[str, Any] = {}
    if outcome is None:
        return ""
    if isinstance(outcome, Mapping):
        inner = outcome.get("receipt")
        receipt = inner if isinstance(inner, Mapping) else outcome
    else:
        inner = getattr(outcome, "receipt", None)
        receipt = inner if isinstance(inner, Mapping) else {}
    if not receipt.get("truncated"):
        return ""
    truncation = _as_text(receipt.get("truncation")) or "budget"
    not_searched = int(receipt.get("not_searched") or 0)
    files = [str(value) for value in (receipt.get("not_searched_files") or [])][:5]
    parts = [
        f"[{title} TRUNCATED: {truncation}]",
        f"{not_searched} candidate(s) were not searched",
    ]
    if files:
        parts.append("not searched: " + ", ".join(files))
    elapsed = receipt.get("elapsed_s")
    if isinstance(elapsed, (int, float)) and elapsed > 0:
        parts.append(f"elapsed {float(elapsed):.2f}s")
    parts.append(
        "treat the files above as the best available, not the whole repository"
    )
    return " ".join(parts)


def knowledge_receipt(context: Optional[KnowledgeContext]) -> Dict[str, Any]:
    """Return a JSON-serializable receipt for a possibly-absent context."""
    if context is None:
        return {"compiled": False, "skipped": "no knowledge context"}
    try:
        return json.loads(json.dumps(context.as_dict(), default=str))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return {"compiled": context.bundle is not None}
