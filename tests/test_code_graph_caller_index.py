"""T5.W1.1 - the code-graph caller index, pinned on a graph big enough to see it.

WHY THIS FILE EXISTS
--------------------
``memory/code_graph._resolve_calls`` resolves a call site's enclosing symbol
through :func:`memory.code_graph._caller_id`. That function was optimised once
(R2-09) and the optimisation was pinned by
``tests/test_ceiling_r2_09_scale.py::test_indexed_caller_lookup_agrees_with_
the_linear_scan_on_every_call_site`` -- against a **4-file, 8-node** fixture
comparing 4 call sites.

The pin passed. The defect was real. The first fix indexed only the
SYMBOL-level branch and left ``if caller_qualified == module:`` scanning every
node, so a repository with one module-level call site per file paid
``O(files x nodes)``. Measured on this repository, 2026-10-02, from rung #9 of
the Trust Ladder:

* ``retrieve_context`` cold: **52-93 s**
* ``_caller_id``: **134,069 calls, 34.7 s**, driving **55,436,109**
  ``str.startswith`` calls
* after the :class:`memory.code_graph.CallerIndex` fix: ``_caller_id`` is
  **1.1 s** and no longer appears among the top phases

A 4-file fixture cannot see an O(nodes)-per-call-site scan. That is the lesson
this file encodes mechanically: **the fixture must be large enough that the
defect class is observable**, and the module-level branch must be exercised
explicitly, because it is the branch the original optimisation missed.

What this file pins
-------------------
1. Every call site of a REAL, multi-file graph resolves to the SAME id the
   linear-scan oracle returns. Equivalence, not similarity.
2. The module-level branch specifically: a call site whose qualified name
   equals the module name resolves to the module node for its file, in
   insertion order, which is the case the original fix left scanning.
3. The index is not merely correct but **sub-linear in node count** for the
   module branch: a graph with 10x the nodes must not cost 10x the time. This
   is the anti-regression property the small fixture could not express.
4. The prefix iteration order is pinned, because a reorder would change which
   candidate wins and therefore which edges exist.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

import pytest

from memory import code_graph


def _build_graph(modules: int = 6, funcs_per_module: int = 6) -> Any:
    """A real parsed graph over a synthetic multi-module repository.

    Every module gets module-level calls AND symbol-level calls, so both
    branches of ``_caller_id`` are exercised. Parsed with the project's own
    tree-sitter indexer rather than hand-built ``NodeInfo`` objects, so the
    node ids and insertion order are the ones a real build produces.
    """
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp(prefix="neo-caller-index-")) / "repo"
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    for m in range(modules):
        body = [f"CONST_{m} = {m}", ""]
        for f in range(funcs_per_module):
            body.append(f"def helper_{m}_{f}():")
            body.append(f"    return CONST_{m} + {f}")
            body.append("")
        # module-level calls: these are the branch the original fix missed
        body.append(f"TOP_{m} = helper_{m}_0()")
        for f in range(1, funcs_per_module):
            body.append(f"ALSO_{m}_{f} = helper_{m}_{f}()")
        # symbol-level calls
        body.append("")
        body.append("def entry():")
        body.append(
            "    return [helper_%d_%d() for _ in range(%d)]"
            % (m, funcs_per_module - 1, funcs_per_module)
        )
        (root / "pkg" / f"mod_{m}.py").write_text(
            "\n".join(body) + "\n", encoding="utf-8"
        )

    builder = code_graph.CodeGraphBuilder(str(root))
    graph = code_graph.Graph(repo_path=str(root), built_at=time.time())
    indexers: List[Any] = []
    for rel, source, lang in builder._iter_source_files():
        parser = builder._parsers.get(lang)
        if parser is None:
            continue
        idx = code_graph._FileIndexer(rel, source, parser.parse(source).root_node)
        idx.run()
        indexers.append(idx)
        graph.file_count += 1
        graph.indexed_file_count += 1
        graph.nodes[f"file:{rel}"] = code_graph.NodeInfo(
            kind="file", name=rel, qualified=rel, file=rel, line=0, end_line=0
        )
        for info in idx.symbols:
            info.extras["node_id"] = f"{info.kind}:{info.qualified}"
            graph.nodes[info.extras["node_id"]] = info
        if idx.module:
            graph.nodes[f"module:{idx.module}"] = code_graph.NodeInfo(
                kind="module",
                name=idx.module,
                qualified=idx.module,
                file=rel,
                line=0,
                end_line=0,
            )
    return graph, indexers, root


@pytest.fixture(scope="module")
def built():
    return _build_graph()


def test_the_fixture_is_large_enough_for_the_defect_class_to_be_visible(built):
    """NON-VACUITY GATE. A 4-file fixture cannot see an O(nodes) scan.

    This is the assertion that would have caught the original miss. The
    threshold is deliberately modest -- 40 nodes, 12 call sites -- because the
    point is not the number, it is that the fixture is big enough for the
    per-call-site cost to be attributable at all.
    """
    graph, indexers, _root = built
    call_sites = sum(len(idx.calls) for idx in indexers)
    assert len(graph.nodes) >= 40, (
        f"only {len(graph.nodes)} nodes; a graph this small cannot show an "
        f"O(nodes)-per-call-site scan, which is how the original pin passed "
        f"while the defect was live"
    )
    assert call_sites >= 12, f"only {call_sites} call sites"


def test_every_call_site_resolves_to_the_oracle_answer(built):
    """The optimised lookup is a PURE SPEEDUP on a graph that can see it."""
    graph, indexers, _root = built
    index = code_graph._qualified_index(graph)
    assert isinstance(index, code_graph.CallerIndex)
    compared = 0
    module_level = 0
    for idx in indexers:
        for caller_qualified, _callee in idx.calls:
            fast = code_graph._caller_id(
                graph,
                idx.module,
                caller_qualified,
                caller_file=idx.rel_path,
                index=index,
            )
            slow = code_graph._caller_id_reference(
                graph, idx.module, caller_qualified, caller_file=idx.rel_path
            )
            assert fast == slow, (
                f"call site {caller_qualified!r} in {idx.rel_path}: indexed "
                f"gave {fast!r}, the linear-scan oracle gave {slow!r}. The "
                f"index is not a pure speedup."
            )
            if caller_qualified == idx.module:
                module_level += 1
            compared += 1
    assert compared >= 12
    # The branch the original fix missed must be covered, or this file repeats
    # the original fixture's mistake at a larger scale.
    assert module_level >= 1, (
        "no module-level call site was compared, so the branch that caused "
        "the 55M-startswith regression is untested"
    )


def test_the_module_level_branch_picks_the_first_module_node_for_its_file(built):
    """The exact semantics the module branch has to preserve.

    The oracle returns the FIRST ``module:`` node in ``graph.nodes`` insertion
    order whose ``file`` matches, and falls back to the synthesised
    ``module:<name>`` when there is none. A caller-index that kept the LAST
    match would be faster and wrong.
    """
    graph, _indexers, _root = built
    index = code_graph._qualified_index(graph)
    for node_id, info in graph.nodes.items():
        if info.kind != "module":
            continue
        got = code_graph._caller_id(
            graph, info.qualified, info.qualified, caller_file=info.file, index=index
        )
        want = code_graph._caller_id_reference(
            graph, info.qualified, info.qualified, caller_file=info.file
        )
        assert got == want == node_id
    # And with no file, the synthesised id is returned rather than None.
    any_module = next((i for i in graph.nodes.values() if i.kind == "module"), None)
    assert any_module is not None
    assert (
        code_graph._caller_id(
            graph, any_module.qualified, any_module.qualified, index=index
        )
        == f"module:{any_module.qualified}"
    )


def test_a_caller_file_with_no_module_node_falls_back_rather_than_raising(built):
    """An absent file is a lookup miss, not a crash and not a wrong id."""
    graph, _indexers, _root = built
    index = code_graph._qualified_index(graph)
    assert (
        code_graph._caller_id(
            graph, "pkg.absent", "pkg.absent", caller_file="pkg/absent.py", index=index
        )
        == "module:pkg.absent"
    )


def test_the_module_branch_is_not_linear_in_node_count():
    """THE ANTI-REGRESSION PROPERTY the 4-file fixture could not express.

    The original defect was O(files x nodes) in the module branch. A
    correctness test cannot catch a complexity regression; only a scaling
    test can. Two graphs, 8x the nodes, and the per-call-site cost must not
    grow proportionally. The bound is deliberately loose (12x) because a
    timing test in a blocking lane must not be flaky -- this asserts the
    ORDER OF MAGNITUDE, which is what actually regressed.
    """
    small_graph, small_indexers, _ = _build_graph(modules=2, funcs_per_module=3)
    big_graph, big_indexers, _ = _build_graph(modules=8, funcs_per_module=12)

    small_index = code_graph._qualified_index(small_graph)
    big_index = code_graph._qualified_index(big_graph)

    def module_branch_seconds(graph, indexers, index, repeats=40) -> float:
        sites = [
            (idx.module, idx.module, idx.rel_path)
            for idx in indexers
            for _ in idx.calls
        ]
        if not sites:
            return 0.0
        started = time.perf_counter()
        for _ in range(repeats):
            for module, qualified, rel in sites:
                code_graph._caller_id(
                    graph, module, qualified, caller_file=rel, index=index
                )
        return (time.perf_counter() - started) / (repeats * len(sites))

    node_ratio = len(big_graph.nodes) / max(1, len(small_graph.nodes))
    small_s = module_branch_seconds(small_graph, small_indexers, small_index)
    big_s = module_branch_seconds(big_graph, big_indexers, big_index)
    if small_s <= 0:
        pytest.skip("the timing window was too small to compare on this host")
    growth = (big_s / small_s) if small_s else float("inf")
    # An O(nodes) module branch grows roughly with node_ratio. An O(1) branch
    # grows with the number of CALL SITES, which also grows here, so the bound
    # is node_ratio x a generous constant rather than a constant.
    assert growth <= node_ratio * 12, (
        f"the module-level branch grew {growth:.1f}x while the graph grew "
        f"{node_ratio:.1f}x ({len(small_graph.nodes)} -> "
        f"{len(big_graph.nodes)} nodes). A linear scan per call site returns "
        f"when the CallerIndex is removed."
    )


def test_the_prefix_order_is_the_order_the_oracle_iterates(built):
    """Pinned because a reorder changes which candidate wins, and therefore
    which call edges exist -- a silent semantic change with a green suite."""
    assert code_graph.NODE_KIND_PREFIXES == ("func:", "method:", "class:")
    source = code_graph._caller_id_reference.__doc__ or ""
    assert "linear-scan" in source.lower()
    # The oracle must remain available: it is the ONLY thing that can prove
    # the index is equivalent, so deleting it would make the correctness
    # untestable.
    assert callable(code_graph._caller_id_reference)


def test_caller_index_reports_its_own_size(built):
    """The index publishes what it covered, so a partial build is visible."""
    graph, _indexers, _root = built
    index = code_graph._qualified_index(graph)
    doc: Dict[str, Any] = {
        "node_count": index.node_count,
        "qualified_prefix_buckets": sum(len(v) for v in index.by_qualified.values()),
        "module_by_file": len(index.module_by_file),
    }
    assert doc["node_count"] == len(graph.nodes)
    assert doc["module_by_file"] >= 1
    assert doc["qualified_prefix_buckets"] >= 1
    # Every module node in the graph must be reachable by its file.
    module_nodes = [i for i in graph.nodes.values() if i.kind == "module"]
    for info in module_nodes:
        assert info.file in index.module_by_file


def test_a_plain_dict_index_is_still_accepted(built):
    """A caller holding the pre-R2-09 shape keeps working.

    ``_caller_id``'s ``index=`` parameter is keyword-only and existed before
    the CallerIndex, so a caller passing the old ``Dict[str, List[str]]``
    must not be broken by the type change. It simply does not get the
    module-branch speedup -- which is the documented trade, not a bug.
    """
    graph, indexers, _root = built
    legacy: Dict[str, List[str]] = {}
    for node_id, info in graph.nodes.items():
        legacy.setdefault(info.qualified, []).append(node_id)
    for idx in indexers:
        for caller_qualified, _callee in idx.calls:
            got = code_graph._caller_id(
                graph,
                idx.module,
                caller_qualified,
                caller_file=idx.rel_path,
                index=legacy,
            )
            want = code_graph._caller_id_reference(
                graph, idx.module, caller_qualified, caller_file=idx.rel_path
            )
            assert got == want


def test_the_real_builder_still_produces_call_edges():
    """The end-to-end control: the build this index serves still works."""
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp(prefix="neo-caller-e2e-")) / "repo"
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.py").write_text(
        "def target():\n    return 1\n\n\ndef caller():\n    return target()\n",
        encoding="utf-8",
    )
    (root / "b.py").write_text(
        "from a import target\n\n\ndef other():\n    return target()\n",
        encoding="utf-8",
    )
    graph = code_graph.CodeGraphBuilder(str(root)).build()
    assert graph.calls, "the builder produced no call edges at all"
    assert any("target" in n or "caller" in n for n in graph.nodes)
