"""R2-07: the AST codemod layer is a plan with a completeness receipt.

The four required proofs are here and are named after the behaviours:

* :meth:`TestCodemodChangesExactlyThose.test_a_rename_touches_the_definition_n_call_sites_and_the_import_and_nothing_else`
* :meth:`TestUnresolvedIsNamed.test_a_dynamic_reference_is_named_in_the_receipt_not_skipped_silently`
* :meth:`TestUnsupportedLanguageRefuses.test_a_rename_in_javascript_refuses_with_a_reason_and_changes_nothing`
* :meth:`TestOrdinaryEditPath.test_a_concurrent_write_after_planning_is_refused_by_the_stale_read_guard`
  and :meth:`TestCatalogParity.test_the_catalog_parity_report_includes_the_new_codemod_tools`

Host-only: no Docker, no model, no network. The edit path is the REAL
``execution.workspace.SafeToolBackend``, so the digest precondition, the
unique-match guard, the approval policy and the undo journal exercised here
are the production ones, not a double.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Dict

import pytest

from harness import codemod
from harness.codemod import (
    BLOCKING_UNRESOLVED_KINDS,
    OP_RENAME,
    OP_SIGNATURE,
    RELIABLE_LANGUAGES,
    UNRESOLVED_AMBIGUOUS_DEFINITION,
    UNRESOLVED_ATTRIBUTE_RECEIVER,
    UNRESOLVED_COMMENT,
    UNRESOLVED_DYNAMIC_LOOKUP,
    UNRESOLVED_OTHER_SYMBOL,
    UNRESOLVED_STRING_REFERENCE,
    UNRESOLVED_UNPARSED_FILE,
    CodemodConfig,
    CodemodPlan,
    apply_plan,
    config_from,
    language_of,
    language_support,
    plan_codemod,
    rename_symbol,
    render_plan,
    render_receipt,
    run_codemod,
    update_signature,
)
from harness.config import DEFAULTS
from harness.tools import (
    canonical_tool_names,
    catalog_fingerprint,
    catalog_parity_report,
    typed_tool_spec,
    typed_tool_specs,
)

# The definition this suite renames. The fixture deliberately declares a
# SECOND, unrelated function with the same name, so every rename is path
# scoped -- an unscoped rename of a name defined twice is a refusal that names
# both candidates, which is its own pinned behaviour below.
TARGET_PATH = "pkg/__init__.py"
OTHER_MODULE = "pkg/billing.py"


# ---------------------------------------------------------------------------
# fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every structural index this suite builds inside ``tmp_path``.

    ``memory.code_graph`` writes its index under the harness home, which is
    outside the repository; pointing it at the test's own temp dir keeps the
    suite from writing into a developer's real home and from ever reusing a
    real index.
    """
    home = tmp_path / "neo-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HARNESS_HOME", str(home))
    monkeypatch.setenv("NEO_HOME", str(home))


def _write(root: Path, rel: str, text: str) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _tree(root: Path) -> Dict[str, str]:
    """Return every ``.py`` file under ``root`` as ``rel -> text``."""
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.py"))
        if path.is_file()
    }


def _tree_any(root: Path) -> Dict[str, str]:
    """Return every file under ``root`` as ``rel -> text``, any extension."""
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _approve(tool: str, arguments) -> bool:
    """Approve every ordinary typed-tool call.

    Approval is the policy's job, not this module's, so the suite grants it
    once here and proves separately that the UNGRANTED path refuses.
    """
    return True


def _apply(operation: str, repo: str, symbol: str, new_name: str = "", **kwargs):
    """``run_codemod`` with the ordinary approval hook granted."""
    kwargs.setdefault("approve", _approve)
    return run_codemod(operation, repo, symbol, new_name, **kwargs)


def _backend(root: Path, *, approve=_approve):
    """A REAL ``SafeToolBackend`` over a real workspace for ``root``."""
    from execution.workspace import SafeToolBackend, open_workspace

    workspace = open_workspace(str(root))
    kwargs = {"approve": approve} if approve is not None else {}
    return SafeToolBackend(workspace, **kwargs), workspace


def _rename_repo(root: Path) -> Path:
    """A repo whose ``pkg.compute_total`` has three call sites and one import.

    It also contains the three things a naive rename corrupts: an UNRELATED
    same-named definition in another module, a comment mentioning the name,
    and a docstring mentioning it.

    Line numbers, asserted below, are part of the contract this suite pins::

         1  \"\"\"Package root.\"\"\"
         2
         3
         4  def compute_total(items, tax=0.0):
         5      \"\"\"Return the order total.\"\"\"
         6      return round(sum(items) + tax, 2)
    """
    _write(
        root,
        TARGET_PATH,
        '"""Package root."""\n'
        "\n"
        "\n"
        "def compute_total(items, tax=0.0):\n"
        '    """Return the order total."""\n'
        "    return round(sum(items) + tax, 2)\n",
    )
    _write(
        root,
        "pkg/app.py",
        '"""Application; uses compute_total for order totals."""\n'
        "\n"
        "from pkg import compute_total\n"
        "from pkg.billing import apply_tax\n"
        "\n"
        "\n"
        "def one(items):\n"
        "    # compute_total is called here\n"
        "    return compute_total(items)\n"
        "\n"
        "\n"
        "def two(items):\n"
        "    return apply_tax(compute_total(items, 0.1))\n"
        "\n"
        "\n"
        "def three(items):\n"
        "    return compute_total(items) + compute_total(items)\n",
    )
    _write(
        root,
        OTHER_MODULE,
        '"""Billing."""\n'
        "\n"
        "\n"
        "def compute_total(value):\n"
        '    """A DIFFERENT function that happens to share the name."""\n'
        "    return value\n"
        "\n"
        "\n"
        "def apply_tax(value):\n"
        "    return value * 1.1\n",
    )
    return root


def _signature_repo(root: Path) -> Path:
    """A repo with one function whose signature can be changed three ways."""
    _write(root, "pkg/__init__.py", "")
    _write(
        root,
        "pkg/core.py",
        "def total(\n"
        "    items,\n"
        "    tax,\n"
        "    rounding=2,\n"
        "):\n"
        '    """Compute a total."""\n'
        "    return round(sum(items) + tax, rounding)\n",
    )
    _write(
        root,
        "pkg/app.py",
        "from pkg.core import total\n"
        "\n"
        "\n"
        "def positional(items):\n"
        "    return total(items, 0.2)\n"
        "\n"
        "\n"
        "def keyword(items):\n"
        "    return total(items, tax=0.2)\n"
        "\n"
        "\n"
        "def splatted(items, *rest):\n"
        "    return total(items, *rest)\n",
    )
    return root


def _core_without_tax_read(root: Path) -> None:
    """Rewrite the signature fixture so the body does NOT read ``tax``.

    Removing a parameter the body still reads is a different (and refused)
    case; this variant isolates the call-site rewriting.
    """
    _write(
        root,
        "pkg/core.py",
        "def total(\n"
        "    items,\n"
        "    tax,\n"
        "    rounding=2,\n"
        "):\n"
        '    """Compute a total."""\n'
        "    return round(sum(items), rounding)\n",
    )


# ---------------------------------------------------------------------------
# required proof 1: a rename changes exactly the definition, its call sites
# and its import -- and nothing else
# ---------------------------------------------------------------------------


class TestCodemodChangesExactlyThose:
    def test_a_rename_touches_the_definition_n_call_sites_and_the_import_and_nothing_else(
        self, tmp_path: Path
    ) -> None:
        """The prompt's first required test, asserted on real file bytes.

        The change set is exactly the definition, the four call sites, and the
        one import. The unrelated same-named function in ``pkg/billing.py``,
        the comment, and the docstring are all left byte-identical, and the
        receipt states how many files were CONSIDERED versus changed.
        """
        root = _rename_repo(tmp_path / "repo")
        before = _tree(root)

        plan = rename_symbol(
            str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        assert plan.refused is False
        assert plan.complete is True, render_receipt(plan.receipt)

        assert sorted((site.path, site.line, site.kind) for site in plan.sites) == [
            (TARGET_PATH, 4, "definition"),
            ("pkg/app.py", 3, "import"),
            ("pkg/app.py", 9, "call"),
            ("pkg/app.py", 13, "call"),
            ("pkg/app.py", 17, "call"),
            ("pkg/app.py", 17, "call"),
        ]
        assert plan.receipt.files_considered == 3
        assert plan.receipt.files_changed == 2
        assert plan.receipt.sites_planned == 6

        outcome = _apply(
            OP_RENAME, str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        assert outcome.applied is not None
        assert outcome.applied.ok is True, outcome.applied.error
        assert outcome.applied.files_changed == 2

        after = _tree(root)
        # exactly the definition ...
        assert "def compute_amount(items, tax=0.0):" in after[TARGET_PATH]
        # ... the import ...
        assert "from pkg import compute_amount" in after["pkg/app.py"]
        # ... and all four call sites, with nothing else on those lines moved.
        assert after["pkg/app.py"].count("compute_amount") == 5
        assert "apply_tax" in after["pkg/app.py"]
        # The UNRELATED same-named definition is byte-identical.
        assert after[OTHER_MODULE] == before[OTHER_MODULE]
        # The comment and the docstring were named, not rewritten.
        assert "# compute_total is called here" in after["pkg/app.py"]
        assert '"""Return the order total."""' in after[TARGET_PATH]
        for text in after.values():
            ast.parse(text)

    def test_planning_a_codemod_never_writes_a_file(self, tmp_path: Path) -> None:
        """A codemod is a PLAN. ``rename_symbol``/``update_signature`` are
        pure readers, which is what makes the receipt reviewable before
        anything is at stake."""
        root = _rename_repo(tmp_path / "repo")
        before = _tree(root)
        rename_symbol(str(root), "compute_total", "compute_amount", path=TARGET_PATH)
        update_signature(str(root), "compute_total", path=TARGET_PATH, removed=["tax"])
        assert _tree(root) == before

    def test_an_ambiguous_definition_is_a_refusal_that_names_the_candidates(
        self, tmp_path: Path
    ) -> None:
        """Renaming "one of these two" is not a plan, so it refuses."""
        root = _rename_repo(tmp_path / "repo")
        before = _tree(root)
        plan = rename_symbol(str(root), "compute_total", "compute_amount")
        assert plan.refused is True
        assert plan.edits == ()
        assert "2 places" in plan.receipt.reason
        assert f"{TARGET_PATH}:pkg.compute_total" in plan.receipt.reason
        assert f"{OTHER_MODULE}:pkg.billing.compute_total" in plan.receipt.reason
        assert all(
            item.kind == UNRESOLVED_AMBIGUOUS_DEFINITION
            for item in plan.receipt.unresolved
        )
        assert _tree(root) == before

    def test_a_scoped_rename_leaves_the_other_module_alone(
        self, tmp_path: Path
    ) -> None:
        root = _rename_repo(tmp_path / "repo")
        plan = rename_symbol(
            str(root), "compute_total", "compute_amount", path=OTHER_MODULE
        )
        assert plan.refused is False
        assert sorted({site.path for site in plan.sites}) == [OTHER_MODULE]


# ---------------------------------------------------------------------------
# required proof 2: an unresolvable reference is NAMED, never silently skipped
# ---------------------------------------------------------------------------


class TestUnresolvedIsNamed:
    def test_a_dynamic_reference_is_named_in_the_receipt_not_skipped_silently(
        self, tmp_path: Path
    ) -> None:
        """The prompt's second required test.

        A ``globals()["compute_total"]`` lookup is a call site no static
        resolver can follow. It is NAMED with a path, a line, a kind and a
        reason, the plan reports itself INCOMPLETE, and the apply refuses by
        default with nothing changed -- because a rename that missed it would
        leave code that looks renamed and is not.
        """
        root = _rename_repo(tmp_path / "repo")
        _write(
            root,
            "pkg/dynamic.py",
            "import pkg\n"
            "\n"
            "\n"
            "def dispatch(order):\n"
            '    handler = globals()["compute_total"]\n'
            "    return pkg.compute_total(order)\n",
        )
        before = _tree(root)

        plan = rename_symbol(
            str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        assert plan.refused is False
        assert plan.complete is False

        named = [
            item
            for item in plan.receipt.unresolved
            if item.kind == UNRESOLVED_DYNAMIC_LOOKUP
        ]
        assert len(named) == 1
        assert named[0].path == "pkg/dynamic.py"
        assert named[0].line == 5
        assert "compute_total" in named[0].detail
        assert named[0].blocking is True
        assert UNRESOLVED_DYNAMIC_LOOKUP in BLOCKING_UNRESOLVED_KINDS

        # The resolvable site in the SAME file is still planned: the miss is
        # reported beside the work, not instead of it.
        assert ("pkg/dynamic.py", 6, "attribute_reference") in [
            (site.path, site.line, site.kind) for site in plan.sites
        ]

        # The receipt is rendered, not summarised away.
        text = render_plan(plan)
        assert "pkg/dynamic.py:5 [dynamic_symbol_lookup]" in text
        assert "complete: False" in text

        outcome = _apply(
            OP_RENAME, str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        assert outcome.applied is not None
        assert outcome.applied.ok is False
        assert outcome.applied.error_kind == "incomplete_receipt"
        assert "pkg/dynamic.py:5" in outcome.applied.error
        assert _tree(root) == before  # nothing changed

    def test_the_override_is_explicit_and_still_names_the_site(
        self, tmp_path: Path
    ) -> None:
        """``allow_incomplete`` applies the resolvable sites, and the receipt
        still carries the named miss so the reader knows what is unverified."""
        root = _rename_repo(tmp_path / "repo")
        _write(
            root,
            "pkg/dynamic.py",
            "import pkg\n"
            "\n"
            "\n"
            "def dispatch(order):\n"
            '    handler = globals()["compute_total"]\n'
            "    return pkg.compute_total(order)\n",
        )
        outcome = _apply(
            OP_RENAME,
            str(root),
            "compute_total",
            "compute_amount",
            path=TARGET_PATH,
            allow_incomplete=True,
        )
        assert outcome.applied is not None
        assert outcome.applied.ok is True, outcome.applied.error
        assert any(
            item.kind == UNRESOLVED_DYNAMIC_LOOKUP
            for item in outcome.plan.receipt.unresolved
        )
        assert "pkg.compute_amount" in (root / "pkg" / "dynamic.py").read_text(
            encoding="utf-8"
        )

    def test_an_attribute_on_an_unrelated_receiver_is_named_and_blocks(
        self, tmp_path: Path
    ) -> None:
        root = _rename_repo(tmp_path / "repo")
        _write(
            root,
            "pkg/unrelated.py",
            "class Registry:\n"
            "    def compute_total(self):\n"
            "        return 0\n"
            "\n"
            "\n"
            "def use(registry):\n"
            "    return registry.compute_total()\n",
        )
        plan = rename_symbol(
            str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        named = [
            item
            for item in plan.receipt.unresolved
            if item.kind == UNRESOLVED_ATTRIBUTE_RECEIVER
        ]
        assert named, "an attribute on an unrelated receiver must be named"
        assert any(item.path == "pkg/unrelated.py" for item in named)
        assert plan.complete is False

    def test_an_unrelated_same_named_symbol_is_named_but_does_not_block(
        self, tmp_path: Path
    ) -> None:
        """A same-named symbol in a DIFFERENT module is provably not the
        target: naming it is information, not a blocker. Blocking on it would
        make every common name unrenameable."""
        root = _rename_repo(tmp_path / "repo")
        plan = rename_symbol(
            str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        advisory = [
            item
            for item in plan.receipt.unresolved
            if item.kind == UNRESOLVED_OTHER_SYMBOL
        ]
        assert advisory, "the unrelated definition must still be named"
        assert all(not item.blocking for item in advisory)
        assert UNRESOLVED_OTHER_SYMBOL not in BLOCKING_UNRESOLVED_KINDS
        assert plan.complete is True, render_receipt(plan.receipt)

    def test_a_comment_and_a_docstring_mention_are_named_as_advisory(
        self, tmp_path: Path
    ) -> None:
        root = _rename_repo(tmp_path / "repo")
        plan = rename_symbol(
            str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        kinds = {item.kind for item in plan.receipt.advisory_unresolved}
        assert UNRESOLVED_COMMENT in kinds
        assert UNRESOLVED_STRING_REFERENCE in kinds
        assert plan.complete is True, render_receipt(plan.receipt)
        text = render_receipt(plan.receipt)
        assert "unresolved blocking: 0" in text
        assert "[comment_reference]" in text
        assert "[string_reference]" in text

    def test_a_star_import_from_the_target_module_is_named_and_blocks(
        self, tmp_path: Path
    ) -> None:
        root = _rename_repo(tmp_path / "repo")
        _write(
            root, "pkg/wild.py", "from pkg import *\n\n\nvalue = compute_total([])\n"
        )
        plan = rename_symbol(
            str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        assert any("import *" in item.detail for item in plan.receipt.unresolved)
        assert plan.complete is False

    def test_a_file_that_does_not_parse_is_named_and_blocks(
        self, tmp_path: Path
    ) -> None:
        """The structural index SKIPS an unparseable file, which is exactly
        where a call site would hide. The codemod's own scan finds it and
        names it, rather than trusting the index's silence."""
        root = _rename_repo(tmp_path / "repo")
        _write(root, "pkg/broken.py", "def oops(:\n    return compute_total(1)\n")
        before = _tree(root)
        plan = rename_symbol(
            str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        named = [
            item
            for item in plan.receipt.unresolved
            if item.kind == UNRESOLVED_UNPARSED_FILE and item.path == "pkg/broken.py"
        ]
        assert named, "an unparseable file must be named, not skipped"
        assert plan.complete is False
        outcome = _apply(
            OP_RENAME, str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        assert outcome.applied is not None and outcome.applied.ok is False
        assert _tree(root) == before


# ---------------------------------------------------------------------------
# required proof 3: an unsupported language refuses with a reason
# ---------------------------------------------------------------------------


class TestUnsupportedLanguageRefuses:
    def _repo(self, tmp_path: Path, name: str) -> Path:
        root = tmp_path / "polyglot"
        root.mkdir(parents=True, exist_ok=True)
        _write(
            root, name, "function computeTotal(items) {\n  return items.length;\n}\n"
        )
        return root

    def test_a_rename_in_javascript_refuses_with_a_reason_and_changes_nothing(
        self, tmp_path: Path
    ) -> None:
        """The prompt's third required test.

        JavaScript has a conservative top-level declaration scan, not
        identifier-level extraction, so a rename could not be shown to be
        complete. It refuses, names the language and the missing extraction,
        produces NO edits, and the file is byte-identical afterwards.
        """
        root = self._repo(tmp_path, "app.js")
        before = _tree_any(root)
        plan = rename_symbol(str(root), "computeTotal", "computeSum")
        assert plan.refused is True
        assert plan.edits == ()
        assert plan.sites == ()
        assert "javascript" in plan.receipt.reason
        assert "runtime.symbols" in plan.receipt.reason
        assert "call sites" in plan.receipt.reason
        assert plan.language == codemod.LANGUAGE_JAVASCRIPT
        assert any(
            item.kind == codemod.UNRESOLVED_UNSUPPORTED_LANGUAGE
            for item in plan.receipt.unresolved
        )
        assert _tree_any(root) == before

    def test_a_rename_in_typescript_refuses_with_a_reason_and_changes_nothing(
        self, tmp_path: Path
    ) -> None:
        root = self._repo(tmp_path, "app.ts")
        before = _tree_any(root)
        plan = rename_symbol(str(root), "computeTotal", "computeSum")
        assert plan.refused is True
        assert plan.edits == ()
        assert "typescript" in plan.receipt.reason
        assert _tree_any(root) == before

    def test_a_signature_change_in_javascript_also_refuses(
        self, tmp_path: Path
    ) -> None:
        root = self._repo(tmp_path, "app.js")
        before = _tree_any(root)
        plan = update_signature(str(root), "computeTotal", removed=["items"])
        assert plan.refused is True
        assert "javascript" in plan.receipt.reason
        assert _tree_any(root) == before

    def test_only_python_is_a_reliable_language(self) -> None:
        assert RELIABLE_LANGUAGES == (codemod.LANGUAGE_PYTHON,)
        assert language_support("pkg/core.py") == ""
        assert language_support("pkg/core.pyi") == ""
        for refused in ("a.js", "a.tsx", "a.rb", "a.go", "a.txt", "Makefile"):
            assert language_support(refused), refused
        assert language_of("a.py") == codemod.LANGUAGE_PYTHON
        assert language_of("a.ts") == codemod.LANGUAGE_TYPESCRIPT
        assert language_of("a.bin") == codemod.LANGUAGE_UNKNOWN

    def test_an_unknown_operation_is_a_refusal_not_a_default(
        self, tmp_path: Path
    ) -> None:
        root = _rename_repo(tmp_path / "repo")
        plan = plan_codemod("refactor_everything", str(root), "compute_total")
        assert plan.refused is True
        assert "unknown codemod operation" in plan.receipt.reason
        assert OP_RENAME in plan.receipt.reason
        assert OP_SIGNATURE in plan.receipt.reason

    def test_a_missing_repository_is_a_refusal(self, tmp_path: Path) -> None:
        plan = rename_symbol(str(tmp_path / "nope"), "a", "b")
        assert plan.refused is True
        assert "repository not found" in plan.receipt.reason

    def test_a_non_identifier_name_is_refused(self, tmp_path: Path) -> None:
        root = _rename_repo(tmp_path / "repo")
        for bad in ("", "  ", "2bad", "has space", "pkg.mod"):
            plan = rename_symbol(str(root), bad, "y")
            assert plan.refused is True
            assert "valid Python identifiers" in plan.receipt.reason

    def test_renaming_a_name_to_itself_is_refused(self, tmp_path: Path) -> None:
        root = _rename_repo(tmp_path / "repo")
        plan = rename_symbol(str(root), "compute_total", "compute_total")
        assert plan.refused is True
        assert "the new name is the old name" in plan.receipt.reason


# ---------------------------------------------------------------------------
# required proof 4: every change goes through the ordinary edit path
# ---------------------------------------------------------------------------


class TestOrdinaryEditPath:
    def test_a_concurrent_write_after_planning_is_refused_by_the_stale_read_guard(
        self, tmp_path: Path
    ) -> None:
        """The prompt's fourth required test.

        The plan binds the digest it read. A file that changed underneath it
        is refused by the ORDINARY precondition in
        ``Workspace.apply_exact_edit`` -- this module does not implement,
        weaken, or bypass a stale-read check.
        """
        root = _rename_repo(tmp_path / "repo")
        before = _tree(root)
        plan = rename_symbol(
            str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        assert plan.complete is True

        backend, workspace = _backend(root)
        try:
            # A concurrent writer touches one file after the plan was built.
            _write(
                root,
                "pkg/app.py",
                (root / "pkg" / "app.py").read_text(encoding="utf-8")
                + "\n\ndef late(items):\n    return compute_total(items)\n",
            )
            result = apply_plan(plan, backend)
        finally:
            workspace.close()

        assert result.ok is False
        assert result.error_kind == "edit_refused"
        assert "stale edit precondition" in result.error
        # All-or-nothing: the edits that DID land before the refusal were undone.
        assert result.rolled_back is True
        assert result.rollback_failures == ()
        assert (root / TARGET_PATH).read_text(encoding="utf-8") == before[TARGET_PATH]
        # The concurrent writer's edit is preserved, not clobbered.
        assert "def late(items):" in (root / "pkg" / "app.py").read_text(
            encoding="utf-8"
        )

    def test_every_planned_edit_carries_the_digest_the_plan_read(
        self, tmp_path: Path
    ) -> None:
        """The precondition handed to the edit path is the file's own
        SHA-256, which is exactly what ``FileRevision.sha256`` carries."""
        root = _rename_repo(tmp_path / "repo")
        plan = rename_symbol(
            str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        for edit in plan.edits:
            expected = hashlib.sha256((root / edit.path).read_bytes()).hexdigest()
            assert edit.expected_sha256 == expected, edit.path

    def test_a_matching_digest_makes_a_span_refusal_impossible(
        self, tmp_path: Path
    ) -> None:
        """The precondition binds the exact bytes, so once the digest matches
        every planned span is still present. A rename therefore cannot be
        half-right: the only two failure modes are a stale digest (refused) or
        a policy refusal (rolled back)."""
        root = _rename_repo(tmp_path / "repo")
        plan = rename_symbol(
            str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        for edit in plan.edits:
            # read_bytes, not read_text: the edit path sees the file's own
            # line terminators, and read_text would translate them away.
            raw = (root / edit.path).read_bytes().decode("utf-8")
            assert edit.old_string in raw, edit.path

    def test_a_refused_edit_rolls_every_already_applied_edit_back(
        self, tmp_path: Path
    ) -> None:
        """A partially applied rename is the state this module exists to avoid,
        so the apply is all-or-nothing. The refusal under test is the ordinary
        APPROVAL policy, which lets the first file's edit land and refuses the
        second -- exactly the shape a partially applied rename would have."""
        root = _rename_repo(tmp_path / "repo")
        before = _tree(root)
        plan = rename_symbol(
            str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        assert next(edit.path for edit in plan.edits) == TARGET_PATH
        assert "pkg/app.py" in {edit.path for edit in plan.edits}

        def approve(tool, arguments):
            return str(arguments.get("path") or "") != "pkg/app.py"

        backend, workspace = _backend(root, approve=approve)
        try:
            result = apply_plan(plan, backend)
        finally:
            workspace.close()

        assert result.ok is False
        assert result.error_kind == "edit_refused"
        assert "pkg/app.py" in result.error
        assert result.applied, "the first file's edit must have landed"
        assert result.rolled_back is True
        assert result.rollback_failures == ()
        # Every file is byte-identical to its pre-apply state.
        assert _tree(root) == before

    def test_the_ordinary_approval_policy_still_runs(self, tmp_path: Path) -> None:
        """A codemod is not a way around the approval decision: with no
        approval hook the ordinary policy refuses the first edit and nothing
        changes."""
        root = _rename_repo(tmp_path / "repo")
        before = _tree(root)
        outcome = run_codemod(
            OP_RENAME, str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        assert outcome.applied is not None
        assert outcome.applied.ok is False
        assert "approval required" in outcome.applied.error
        assert _tree(root) == before

    def test_a_plan_with_blocking_sites_is_refused_before_any_edit(
        self, tmp_path: Path
    ) -> None:
        root = _rename_repo(tmp_path / "repo")
        _write(
            root,
            "pkg/dynamic.py",
            'def dispatch(order):\n    return globals()["compute_total"](order)\n',
        )
        before = _tree(root)
        plan = rename_symbol(
            str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        backend, workspace = _backend(root)
        try:
            result = apply_plan(plan, backend)
        finally:
            workspace.close()
        assert result.ok is False
        assert result.error_kind == "incomplete_receipt"
        assert result.applied == ()
        assert _tree(root) == before

    def test_no_backend_is_an_honest_refusal_not_a_direct_write(
        self, tmp_path: Path
    ) -> None:
        root = _rename_repo(tmp_path / "repo")
        before = _tree(root)
        result = apply_plan(
            rename_symbol(
                str(root), "compute_total", "compute_amount", path=TARGET_PATH
            ),
            None,
        )
        assert result.ok is False
        assert result.error_kind == "no_runtime"
        assert _tree(root) == before

    def test_a_refused_plan_is_never_applied(self, tmp_path: Path) -> None:
        root = tmp_path / "js"
        root.mkdir()
        _write(root, "a.js", "function alpha(x) { return x; }\n")
        before = _tree_any(root)
        outcome = _apply(OP_RENAME, str(root), "alpha", "beta")
        assert outcome.applied is None
        assert outcome.plan.refused is True
        assert _tree_any(root) == before

    def test_a_plan_with_no_edits_is_an_honest_no_match(self, tmp_path: Path) -> None:
        root = tmp_path / "none"
        root.mkdir()
        _write(root, "pkg.py", "def lonely(x):\n    return x\n")
        plan = rename_symbol(str(root), "lonely", "solo")
        empty = CodemodPlan(
            operation=plan.operation,
            repo_path=plan.repo_path,
            symbol=plan.symbol,
            new_name=plan.new_name,
            language=plan.language,
            edits=(),
            sites=plan.sites,
            receipt=plan.receipt,
            definition=plan.definition,
        )
        result = apply_plan(empty, object())
        assert result.ok is False
        assert result.error_kind == "no_match"

    def test_the_applied_result_records_every_operation_it_journaled(
        self, tmp_path: Path
    ) -> None:
        root = _rename_repo(tmp_path / "repo")
        outcome = _apply(
            OP_RENAME, str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        applied = outcome.applied
        assert applied is not None and applied.ok is True
        assert applied.undo_ids, "every applied edit must be undoable"
        assert len(applied.undo_ids) == len(applied.applied)
        for item in applied.applied:
            assert item["operation_id"]
            assert item["post_hash"]


# ---------------------------------------------------------------------------
# required proof 5: the canonical catalog
# ---------------------------------------------------------------------------


class TestCatalogParity:
    def test_the_catalog_parity_report_includes_the_new_codemod_tools(self) -> None:
        """The prompt's fifth required test: the ONE catalog carries both
        tools, and the machine-checkable parity report agrees."""
        from harness.agent_kernel.tools import builtin_tool_specs, catalog_parity

        names = canonical_tool_names()
        assert "rename_symbol" in names
        assert "update_signature" in names

        derived = builtin_tool_specs()
        assert [spec.name for spec in derived] == list(names)
        report = catalog_parity_report(derived)
        assert report["identical"] is True
        assert report["differences"] == []
        assert "rename_symbol" in report["canonical_names"]
        assert "update_signature" in report["canonical_names"]
        assert catalog_parity()["identical"] is True
        # The kernel really derives them, rather than declaring its own.
        assert catalog_fingerprint(derived) == catalog_fingerprint(
            list(typed_tool_specs())
        )
        assert len(names) == len(set(names))

    def test_the_codemod_tools_are_workspace_write_and_approval_gated(self) -> None:
        """They mutate several files at once, so they carry the same effect
        class and the same approval requirement as an ordinary edit."""
        for name in ("rename_symbol", "update_signature"):
            spec = typed_tool_spec(name)
            assert spec is not None, name
            assert spec.side_effect_class == "workspace_write", name
            assert spec.requires_approval is True, name

    def test_the_rename_schema_requires_both_names(self) -> None:
        spec = typed_tool_spec("rename_symbol")
        assert spec is not None
        assert set(spec.required) == {"symbol", "new_name"}
        assert "path" in spec.optional
        assert "apply" in spec.optional
        assert "allow_incomplete" in spec.optional
        assert spec.types["new_name"] is str
        assert spec.types["apply"] is bool
        assert spec.types["allow_incomplete"] is bool

    def test_the_signature_schema_declares_the_declaration_lists(self) -> None:
        spec = typed_tool_spec("update_signature")
        assert spec is not None
        assert set(spec.required) == {"symbol"}
        for field in ("added", "removed", "renamed", "retyped", "path"):
            assert field in spec.optional, field
        assert spec.types["added"] is list
        assert spec.types["removed"] is list
        assert spec.types["renamed"] is dict
        assert spec.types["retyped"] is dict

    def test_every_catalog_tool_still_has_a_kernel_handler(self) -> None:
        """The catalog must not advertise a tool the kernel cannot run. The
        two new entries are installed additively, and a bare argument set
        produces an honest refusal rather than "no handler registered"."""
        from harness.agent_kernel import build_default_handlers
        from harness.agent_kernel.tools import ToolRegistry

        registry = ToolRegistry()
        build_default_handlers(registry, repo_path=".", config={})
        assert registry.canonical_name("rename_symbol") == "rename_symbol"
        assert registry.canonical_name("update_signature") == "update_signature"
        minimal = {
            "rename_symbol": {"symbol": "x", "new_name": "y"},
            "update_signature": {"symbol": "x"},
        }
        for name, arguments in minimal.items():
            call = {"tool": name, "arguments": arguments}
            result = registry.execute(call, {})
            assert "no handler registered" not in str(result.output), name
            # The handler really ran: a plan or a named refusal, not a stub.
            assert "codemod" in str(result.output), name

    def test_typed_argument_validation_is_strict(self) -> None:
        from harness.tools import TypedToolValidationError, validate_typed_arguments

        name, values = validate_typed_arguments(
            "rename_symbol", {"symbol": "a", "new_name": "b", "apply": False}
        )
        assert name == "rename_symbol"
        assert values["apply"] is False
        with pytest.raises(TypedToolValidationError):
            validate_typed_arguments(
                "rename_symbol", {"symbol": "a", "new_name": "b", "nope": 1}
            )
        with pytest.raises(TypedToolValidationError):
            validate_typed_arguments("rename_symbol", {"symbol": "a"})
        with pytest.raises(TypedToolValidationError):
            validate_typed_arguments(
                "rename_symbol", {"symbol": "a", "new_name": "b", "apply": "yes"}
            )


# ---------------------------------------------------------------------------
# update_signature
# ---------------------------------------------------------------------------


class TestUpdateSignature:
    def test_a_parameter_rename_rewrites_the_declaration_the_body_and_the_call_sites(
        self, tmp_path: Path
    ) -> None:
        """A parameter rename that missed the body would be a guaranteed
        ``NameError`` the moment the change landed, so the body's own reads
        are rewritten too."""
        root = _signature_repo(tmp_path / "repo")
        outcome = _apply(
            OP_SIGNATURE, str(root), "total", renamed={"rounding": "digits"}
        )
        assert outcome.applied is not None, "the plan must not be incomplete"
        assert outcome.applied.ok is True, outcome.applied.error
        core = (root / "pkg" / "core.py").read_text(encoding="utf-8")
        assert "digits=2" in core
        assert "rounding" not in core
        ast.parse(core)
        # A parameter rename needs no call-site change, so the caller is
        # byte-identical.
        assert (root / "pkg" / "app.py").read_text(encoding="utf-8") == (
            tmp_path / "repo" / "pkg" / "app.py"
        ).read_text(encoding="utf-8")

    def test_removing_a_parameter_the_body_reads_is_named_and_blocks(
        self, tmp_path: Path
    ) -> None:
        """There is no correct mechanical rewrite for "the body still reads
        the parameter you removed" -- whether the argument should be dropped,
        defaulted, or replaced is a semantic decision. It is NAMED and the
        change refuses by default."""
        root = _signature_repo(tmp_path / "repo")
        before = _tree(root)
        plan = update_signature(str(root), "total", removed=["tax"])
        named = [
            item
            for item in plan.receipt.blocking_unresolved
            if item.path == "pkg/core.py"
        ]
        assert named, "the body's read of the removed parameter must be named"
        assert any("still reads parameter" in item.detail for item in named)
        assert plan.complete is False
        outcome = _apply(OP_SIGNATURE, str(root), "total", removed=["tax"])
        assert outcome.applied is not None and outcome.applied.ok is False
        assert outcome.applied.error_kind == "incomplete_receipt"
        assert _tree(root) == before

    def test_removing_a_parameter_the_body_does_not_read_rewrites_the_calls(
        self, tmp_path: Path
    ) -> None:
        root = _signature_repo(tmp_path / "repo")
        _core_without_tax_read(root)
        # The splat call cannot be mapped, so it is NAMED and the default
        # refuses -- naming it is the point.
        outcome = _apply(OP_SIGNATURE, str(root), "total", removed=["tax"])
        assert outcome.applied is not None
        assert outcome.applied.ok is False
        assert any(
            item.kind == codemod.UNRESOLVED_SPLAT_ARGUMENTS
            for item in outcome.plan.receipt.blocking_unresolved
        )
        outcome = _apply(
            OP_SIGNATURE, str(root), "total", removed=["tax"], allow_incomplete=True
        )
        assert outcome.applied is not None and outcome.applied.ok is True
        app = (root / "pkg" / "app.py").read_text(encoding="utf-8")
        assert app.count("return total(items)") == 2  # positional AND keyword
        assert "0.2" not in app
        core = (root / "pkg" / "core.py").read_text(encoding="utf-8")
        assert "tax" not in core.split('"""')[0]
        ast.parse(core)
        ast.parse(app)

    def test_a_splat_call_is_named_and_never_guessed_at(self, tmp_path: Path) -> None:
        root = _signature_repo(tmp_path / "repo")
        _core_without_tax_read(root)
        plan = update_signature(str(root), "total", removed=["tax"])
        splats = [
            item
            for item in plan.receipt.blocking_unresolved
            if item.kind == codemod.UNRESOLVED_SPLAT_ARGUMENTS
        ]
        assert splats
        assert splats[0].path == "pkg/app.py"
        assert "splat" in splats[0].detail

    def test_a_multi_line_call_that_cannot_be_rewritten_is_named(
        self, tmp_path: Path
    ) -> None:
        root = _signature_repo(tmp_path / "repo")
        _core_without_tax_read(root)
        _write(
            root,
            "pkg/app.py",
            "from pkg.core import total\n"
            "\n"
            "\n"
            "def spread(items):\n"
            "    return total(\n"
            "        items,\n"
            "        0.2,\n"
            "    )\n",
        )
        plan = update_signature(str(root), "total", removed=["tax"])
        assert any(
            "more than one source line" in item.detail
            for item in plan.receipt.blocking_unresolved
        )

    def test_an_added_parameter_without_a_default_after_a_defaulted_one_refuses(
        self, tmp_path: Path
    ) -> None:
        """A codemod must never leave a file that does not parse. This is a
        SyntaxError, and where an added parameter goes is the caller's
        decision, so it is a refusal that names the reason."""
        root = _signature_repo(tmp_path / "repo")
        before = _tree(root)
        plan = update_signature(str(root), "total", added=["currency:str"])
        assert plan.refused is True
        assert "not valid Python" in plan.receipt.reason
        assert "no default cannot follow" in plan.receipt.reason
        assert _tree(root) == before

    def test_an_added_parameter_with_a_default_is_appended_and_the_result_parses(
        self, tmp_path: Path
    ) -> None:
        root = _signature_repo(tmp_path / "repo")
        outcome = _apply(
            OP_SIGNATURE,
            str(root),
            "total",
            added=["currency:str=usd"],
            renamed={"rounding": "digits"},
        )
        assert outcome.applied is not None and outcome.applied.ok is True
        core = (root / "pkg" / "core.py").read_text(encoding="utf-8")
        assert 'currency: str = "usd"' in core
        assert "digits=2" in core
        assert "tax," in core  # kept: the body reads it, and nothing removed it
        ast.parse(core)

    def test_a_retyped_parameter_rewrites_only_the_declaration(
        self, tmp_path: Path
    ) -> None:
        root = _signature_repo(tmp_path / "repo")
        outcome = _apply(OP_SIGNATURE, str(root), "total", retyped={"tax": "Decimal"})
        assert outcome.applied is not None and outcome.applied.ok is True
        core = (root / "pkg" / "core.py").read_text(encoding="utf-8")
        assert "tax: Decimal" in core
        ast.parse(core)

    def test_a_signature_change_for_a_class_refuses_with_a_reason(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "repo"
        _write(root, "pkg/__init__.py", "")
        _write(
            root,
            "pkg/models.py",
            "class Widget:\n    def render(self, scale):\n        return scale\n",
        )
        before = _tree(root)
        plan = update_signature(
            str(root), "Widget", path="pkg/models.py", removed=["scale"]
        )
        assert plan.refused is True
        assert "class, not a function" in plan.receipt.reason
        assert _tree(root) == before

    def test_an_unknown_parameter_name_refuses_with_the_declared_ones(
        self, tmp_path: Path
    ) -> None:
        root = _signature_repo(tmp_path / "repo")
        plan = update_signature(str(root), "total", removed=["nope"])
        assert plan.refused is True
        assert "declares no parameter(s) nope" in plan.receipt.reason
        assert "declared parameters are items, tax, rounding" in plan.receipt.reason

    def test_an_empty_signature_change_refuses(self, tmp_path: Path) -> None:
        root = _signature_repo(tmp_path / "repo")
        plan = update_signature(str(root), "total")
        assert plan.refused is True
        assert "nothing to change" in plan.receipt.reason

    def test_a_body_that_rebinds_a_renamed_parameter_is_named_not_rewritten(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "repo"
        _write(root, "pkg/__init__.py", "")
        _write(
            root,
            "pkg/core.py",
            "def total(items, tax, rounding=2):\n"
            "    rounding = 3\n"
            "    return round(sum(items) + tax, rounding)\n",
        )
        plan = update_signature(str(root), "total", renamed={"rounding": "digits"})
        assert any(
            "rebinds" in item.detail for item in plan.receipt.blocking_unresolved
        )


# ---------------------------------------------------------------------------
# configuration discipline
# ---------------------------------------------------------------------------


class TestConfigDiscipline:
    def test_no_codemod_key_is_in_the_harness_defaults(self) -> None:
        """A value in ``DEFAULTS`` is merged into EVERY task and every eval
        arm, so one would silently switch every run. These keys are read from
        ``Task.config`` only, and this test fails if one is published."""
        offenders = sorted(key for key in DEFAULTS if key.startswith("codemod_"))
        assert offenders == [], (
            "codemod keys are read from Task.config and must not be defaulted: "
            f"{offenders}"
        )

    def test_every_codemod_knob_is_read_from_task_config(self) -> None:
        settings, notes = config_from(
            {
                "codemod_scan_max_files": 5,
                "codemod_max_index_files": 9,
                "codemod_max_sites": 7,
                "codemod_edit_context_lines": 1,
                "codemod_include_paths": ["pkg"],
                "codemod_follow_string_references": True,
            }
        )
        assert settings == CodemodConfig(
            scan_max_files=5,
            max_index_files=9,
            max_sites=7,
            edit_context_lines=1,
            include_paths=("pkg",),
            follow_string_references=True,
        )
        assert notes == []

    def test_an_absent_key_means_the_module_default(self) -> None:
        settings, notes = config_from({})
        assert settings == CodemodConfig()
        assert notes == []
        settings, notes = config_from(None)
        assert settings == CodemodConfig()
        assert notes == []

    def test_an_out_of_range_bound_is_clamped_and_the_clamp_is_reported(self) -> None:
        settings, notes = config_from({"codemod_scan_max_files": 10**9})
        assert settings.scan_max_files == 200_000
        assert any("codemod_scan_max_files" in note for note in notes)
        settings, notes = config_from({"codemod_max_index_files": 10**9})
        assert settings.max_index_files == 50_000
        assert any("codemod_max_index_files" in note for note in notes)

    def test_a_non_integer_bound_is_refused_rather_than_coerced(self) -> None:
        for bad in ("many", True, None, [1]):
            with pytest.raises(TypeError):
                config_from({"codemod_max_sites": bad})

    def test_an_unusable_bound_makes_the_plan_refuse_not_crash(
        self, tmp_path: Path
    ) -> None:
        root = _rename_repo(tmp_path / "repo")
        plan = rename_symbol(
            str(root),
            "compute_total",
            "compute_amount",
            path=TARGET_PATH,
            config={"codemod_max_sites": "loads"},
        )
        assert plan.refused is True
        assert "codemod configuration unusable" in plan.receipt.reason

    def test_the_index_scope_ceiling_refuses_before_the_index_is_built(
        self, tmp_path: Path
    ) -> None:
        """A clipped rename would LOOK complete and would not be, so the
        ceiling is a refusal that names the count.

        It is also checked BEFORE the structural index is touched, which is the
        only place a bound means anything: building the tree-sitter index for a
        large repository costs minutes, so a limit enforced afterwards would be
        no limit at all.
        """
        root = _rename_repo(tmp_path / "repo")
        plan = rename_symbol(
            str(root),
            "compute_total",
            "compute_amount",
            path=TARGET_PATH,
            config={"codemod_max_index_files": 1},
        )
        assert plan.refused is True
        assert "3 source file(s)" in plan.receipt.reason
        assert "above the bounded index scope of 1" in plan.receipt.reason
        assert "codemod_include_paths" in plan.receipt.reason
        assert "codemod_max_index_files" in plan.receipt.reason
        assert any("candidate_files=3" in note for note in plan.receipt.notes)
        # The index was never built, which is why the receipt has no index source.
        assert plan.receipt.index_source == ""

    def test_the_handler_refuses_an_oversized_repository_promptly(
        self, tmp_path: Path
    ) -> None:
        """A handler must not do unbounded work before it can answer. On a
        repository past the bound it refuses, and the refusal is fast."""
        import time

        root = _rename_repo(tmp_path / "repo")
        started = time.monotonic()
        plan = rename_symbol(
            str(root),
            "compute_total",
            "compute_amount",
            path=TARGET_PATH,
            config={"codemod_max_index_files": 1},
        )
        elapsed = time.monotonic() - started
        assert plan.refused is True
        assert elapsed < 5.0, f"the bounded refusal took {elapsed:.1f}s"

    def test_the_site_ceiling_refuses_rather_than_clipping(
        self, tmp_path: Path
    ) -> None:
        root = _rename_repo(tmp_path / "repo")
        plan = rename_symbol(
            str(root),
            "compute_total",
            "compute_amount",
            path=TARGET_PATH,
            config={"codemod_max_sites": 2},
        )
        assert plan.refused is True
        assert "above the bounded maximum of 2" in plan.receipt.reason

    def test_the_include_path_narrows_the_scan(self, tmp_path: Path) -> None:
        root = _rename_repo(tmp_path / "repo")
        plan = rename_symbol(
            str(root),
            "compute_total",
            "compute_amount",
            path=OTHER_MODULE,
            config={"codemod_include_paths": [OTHER_MODULE]},
        )
        assert plan.receipt.files_considered == 1
        assert all(site.path == OTHER_MODULE for site in plan.sites)


# ---------------------------------------------------------------------------
# the kernel handler
# ---------------------------------------------------------------------------


class TestKernelHandler:
    def _registry(self, root: Path):
        from harness.agent_kernel import build_default_handlers
        from harness.agent_kernel.tools import ToolRegistry

        registry = ToolRegistry()
        build_default_handlers(registry, repo_path=str(root), config={})
        return registry

    def _dispatch(self, registry, tool: str, arguments: Dict[str, object], context):
        return registry.execute({"tool": tool, "arguments": dict(arguments)}, context)

    def test_the_handler_returns_the_receipt_and_applies_through_the_backend(
        self, tmp_path: Path
    ) -> None:
        root = _rename_repo(tmp_path / "repo")
        registry = self._registry(root)
        backend, workspace = _backend(root)
        try:
            result = self._dispatch(
                registry,
                "rename_symbol",
                {
                    "symbol": "compute_total",
                    "new_name": "compute_amount",
                    "path": TARGET_PATH,
                },
                {"execution_backend": backend},
            )
        finally:
            workspace.close()
        assert result.ok is True, result.output
        assert "codemod receipt" in str(result.output)
        assert "APPLIED through the ordinary edit path" in str(result.output)
        assert "files changed: 2" in str(result.output)
        assert "def compute_amount" in (root / TARGET_PATH).read_text(encoding="utf-8")
        assert result.reference.startswith("codemod_applied:")

    def test_the_handler_can_plan_without_applying(self, tmp_path: Path) -> None:
        root = _rename_repo(tmp_path / "repo")
        before = _tree(root)
        result = self._dispatch(
            self._registry(root),
            "rename_symbol",
            {
                "symbol": "compute_total",
                "new_name": "compute_amount",
                "path": TARGET_PATH,
                "apply": False,
            },
            {},
        )
        assert result.ok is True, result.output
        assert "change set:" in str(result.output)
        assert _tree(root) == before

    def test_the_handler_refuses_an_unsupported_language_with_a_reason(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "js"
        root.mkdir()
        _write(root, "a.js", "function alpha(x) { return x; }\n")
        before = _tree_any(root)
        result = self._dispatch(
            self._registry(root),
            "rename_symbol",
            {"symbol": "alpha", "new_name": "beta"},
            {},
        )
        assert result.ok is False
        assert "javascript" in str(result.output)
        assert result.error_kind == "validation_error"
        assert _tree_any(root) == before

    def test_the_handler_is_honest_when_no_backend_is_bound(
        self, tmp_path: Path
    ) -> None:
        root = _rename_repo(tmp_path / "repo")
        before = _tree(root)
        result = self._dispatch(
            self._registry(root),
            "rename_symbol",
            {"symbol": "compute_total", "new_name": "x", "path": TARGET_PATH},
            {},
        )
        assert result.ok is False
        assert result.error_kind == "no_runtime"
        assert "no execution backend is bound" in str(result.output)
        assert _tree(root) == before

    def test_the_handler_routes_update_signature_to_the_same_implementation(
        self, tmp_path: Path
    ) -> None:
        root = _signature_repo(tmp_path / "repo")
        before = _tree(root)
        result = self._dispatch(
            self._registry(root),
            "update_signature",
            {"symbol": "total", "removed": ["tax"], "apply": False},
            {},
        )
        assert result.ok is True, result.output
        assert "codemod update_signature" in str(result.output)
        assert "complete: False" in str(result.output)
        assert _tree(root) == before

    def test_the_handler_surfaces_a_stale_read_refusal_with_its_reason(
        self, tmp_path: Path
    ) -> None:
        root = _rename_repo(tmp_path / "repo")
        backend, workspace = _backend(root)
        try:
            # Plan only, break the file, then apply the stale plan.
            plan_only = codemod.rename_symbol(
                str(root), "compute_total", "compute_amount", path=TARGET_PATH
            )
            _write(
                root,
                "pkg/app.py",
                (root / "pkg" / "app.py").read_text(encoding="utf-8") + "\n# later\n",
            )
            result = apply_plan(plan_only, backend)
        finally:
            workspace.close()
        assert result.ok is False
        assert "stale edit precondition" in result.error
        assert result.rolled_back is True


# ---------------------------------------------------------------------------
# receipt shape
# ---------------------------------------------------------------------------


class TestReceiptShape:
    def test_the_receipt_states_the_denominator_not_just_the_numerator(
        self, tmp_path: Path
    ) -> None:
        root = _rename_repo(tmp_path / "repo")
        plan = rename_symbol(
            str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        receipt = plan.receipt
        assert receipt.files_considered == 3
        assert receipt.files_changed == 2
        assert receipt.sites_planned == 6
        payload = receipt.to_dict()
        assert payload["schema_version"] == codemod.SCHEMA_VERSION
        # The denominator is every file the scan looked at, not just the ones
        # that changed, and it is strictly larger than the numerator.
        assert payload["files_considered"] > payload["files_changed"]
        assert payload["complete"] is True
        assert payload["unresolved_count"] == len(payload["unresolved"])
        assert payload["blocking_unresolved_count"] == 0
        json.dumps(payload)  # the receipt is serializable

    def test_the_applied_counts_are_measured_not_asserted(self, tmp_path: Path) -> None:
        root = _rename_repo(tmp_path / "repo")
        plan = rename_symbol(
            str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        assert plan.receipt.sites_changed == 0
        outcome = _apply(
            OP_RENAME, str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        applied = outcome.applied
        assert applied is not None and applied.ok is True
        measured = outcome.plan.with_applied(applied)
        assert measured.receipt.sites_changed == applied.sites_changed
        assert measured.receipt.files_changed == applied.files_changed
        assert measured.receipt.sites_changed == len(plan.edits)
        assert "sites changed: 5" in render_receipt(measured.receipt)

    def test_the_plan_serializes_without_file_contents(self, tmp_path: Path) -> None:
        root = _rename_repo(tmp_path / "repo")
        plan = rename_symbol(
            str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        document = plan.to_dict()
        # The CHANGE SET is addressed, not reproduced: a reviewable plan must
        # not become a second copy of the repository.
        for edit in document["edits"]:
            assert set(edit) == {
                "path",
                "line",
                "kind",
                "expected_sha256",
                "site_lines",
                "old_chars",
                "new_chars",
            }
        payload = json.dumps(document["edits"]) + json.dumps(document["sites"])
        assert "compute_total(items, tax=0.0)" not in payload
        assert "def compute_total" not in payload
        # A NAMED unresolved site does carry one bounded source line, because
        # "here is what we did not change" is the point of naming it.
        for item in document["receipt"]["unresolved"]:
            assert len(item["excerpt"]) <= 200
        assert json.loads(json.dumps(plan.to_dict()))["receipt"]["complete"] is True

    def test_the_receipt_renders_an_explicit_none_for_no_unresolved_sites(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "clean"
        root.mkdir()
        _write(root, "pkg.py", "def only(x):\n    return x\n")
        plan = rename_symbol(str(root), "only", "solo")
        text = render_receipt(plan.receipt)
        assert "(no unresolved sites)" in text
        assert "unresolved blocking: 0" in text

    def test_every_unresolved_kind_is_in_the_closed_set(self) -> None:
        assert set(codemod.UNRESOLVED_KINDS) == {
            codemod.UNRESOLVED_OTHER_REFERENCE,
            codemod.UNRESOLVED_ATTRIBUTE_RECEIVER,
            codemod.UNRESOLVED_DYNAMIC_LOOKUP,
            codemod.UNRESOLVED_OTHER_SYMBOL,
            codemod.UNRESOLVED_COMMENT,
            codemod.UNRESOLVED_STRING_REFERENCE,
            codemod.UNRESOLVED_UNPARSED_FILE,
            codemod.UNRESOLVED_AMBIGUOUS_SPAN,
            codemod.UNRESOLVED_AMBIGUOUS_DEFINITION,
            codemod.UNRESOLVED_SPLAT_ARGUMENTS,
            codemod.UNRESOLVED_UNSUPPORTED_LANGUAGE,
        }
        assert set(BLOCKING_UNRESOLVED_KINDS) <= set(codemod.UNRESOLVED_KINDS)
        assert set(codemod.SITE_KINDS) == {
            codemod.SITE_DEFINITION,
            codemod.SITE_CALL,
            codemod.SITE_REFERENCE,
            codemod.SITE_ASSIGNMENT,
            codemod.SITE_ATTRIBUTE,
            codemod.SITE_IMPORT,
            codemod.SITE_KEYWORD,
            codemod.SITE_PARAMETER,
            codemod.SITE_SIGNATURE,
        }

    def test_every_public_function_has_a_docstring_naming_its_assumptions(
        self,
    ) -> None:
        """The module convention: a docstring that says what it assumes."""
        public = [
            name
            for name in codemod.__all__
            if callable(getattr(codemod, name, None))
            and not isinstance(getattr(codemod, name), type)
        ]
        assert len(public) >= 10
        for name in public:
            doc = (getattr(codemod, name).__doc__ or "").strip()
            assert doc, f"{name} has no docstring"
            assert "Assumes" in doc, f"{name} does not state what it assumes"

    def test_every_public_type_has_a_docstring(self) -> None:
        for name in codemod.__all__:
            value = getattr(codemod, name, None)
            if isinstance(value, type):
                assert (value.__doc__ or "").strip(), name

    def test_the_scan_bounds_are_read_from_config_not_hardcoded_at_a_use_site(
        self,
    ) -> None:
        source = Path(codemod.__file__).read_text(encoding="utf-8")
        assert "settings.max_index_files" in source
        assert "settings.max_sites" in source
        assert "settings.edit_context_lines" in source


# ---------------------------------------------------------------------------
# precision regressions found while building this
# ---------------------------------------------------------------------------


class TestPrecisionRegressions:
    def test_a_site_reports_the_enclosing_symbol_from_the_existing_extraction(
        self, tmp_path: Path
    ) -> None:
        """``scope`` comes from ``runtime.symbols``, the extraction the brief
        names, not from a second walk here -- so a reader of the receipt sees
        that a call site lives inside ``three`` rather than at module level."""
        root = _rename_repo(tmp_path / "repo")
        plan = rename_symbol(
            str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        by_line = {(site.path, site.line): site for site in plan.sites}
        assert by_line[("pkg/app.py", 9)].scope == "one"
        assert by_line[("pkg/app.py", 13)].scope == "two"
        assert by_line[("pkg/app.py", 17)].scope == "three"
        assert by_line[(TARGET_PATH, 4)].scope == "compute_total"
        # Module-level code has no enclosing symbol, and that is reported
        # rather than guessed at.
        assert by_line[("pkg/app.py", 3)].scope == ""
        assert all("scope" in site.to_dict() for site in plan.sites)

    def test_a_signature_site_names_the_function_it_belongs_to(
        self, tmp_path: Path
    ) -> None:
        root = _signature_repo(tmp_path / "repo")
        plan = update_signature(str(root), "total", renamed={"rounding": "digits"})
        header = [site for site in plan.sites if site.kind == codemod.SITE_SIGNATURE]
        assert header and header[0].scope == "total"
        body = [
            site
            for site in plan.sites
            if site.path == "pkg/core.py" and site.kind == codemod.SITE_REFERENCE
        ]
        assert body and all(site.scope == "total" for site in body)

    def test_the_scope_index_never_raises(self, tmp_path: Path) -> None:
        """The scope is a reporting convenience: a broken import or an
        unparseable file degrades to an empty scope, never to a failure."""
        assert codemod._scope_index("def oops(:\n", "a.py") == {}
        assert codemod._scope_index("", "a.py") == {}

    def test_a_same_named_definition_in_another_module_is_never_renamed(
        self, tmp_path: Path
    ) -> None:
        """A name-based over-approximation would have rewritten this. It is
        the defect that makes a common name unsafe to rename, and the reason
        this module resolves each file's relationship to the target instead."""
        root = _rename_repo(tmp_path / "repo")
        outcome = _apply(
            OP_RENAME, str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        assert outcome.applied is not None and outcome.applied.ok is True
        billing = (root / OTHER_MODULE).read_text(encoding="utf-8")
        assert "def compute_total(value):" in billing
        assert "compute_amount" not in billing

    def test_a_qualified_attribute_on_the_target_module_is_renamed(
        self, tmp_path: Path
    ) -> None:
        root = _rename_repo(tmp_path / "repo")
        _write(
            root,
            "pkg/qualified.py",
            "import pkg\n\n\ndef use(items):\n    return pkg.compute_total(items)\n",
        )
        outcome = _apply(
            OP_RENAME, str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        assert outcome.applied is not None and outcome.applied.ok is True
        assert "pkg.compute_amount(items)" in (root / "pkg" / "qualified.py").read_text(
            encoding="utf-8"
        )

    def test_a_relative_import_of_the_target_is_recognised(
        self, tmp_path: Path
    ) -> None:
        root = _rename_repo(tmp_path / "repo")
        _write(
            root,
            "pkg/relative.py",
            "from . import compute_total\n"
            "\n"
            "\n"
            "def use(items):\n"
            "    return compute_total(items)\n",
        )
        outcome = _apply(
            OP_RENAME, str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        assert outcome.applied is not None and outcome.applied.ok is True
        text = (root / "pkg" / "relative.py").read_text(encoding="utf-8")
        assert "from . import compute_amount" in text
        assert "return compute_amount(items)" in text

    def test_an_aliased_import_renames_the_member_and_keeps_the_alias(
        self, tmp_path: Path
    ) -> None:
        root = _rename_repo(tmp_path / "repo")
        _write(
            root,
            "pkg/aliased.py",
            "from pkg import compute_total as total_of\n"
            "\n"
            "\n"
            "def use(items):\n"
            "    return total_of(items)\n",
        )
        outcome = _apply(
            OP_RENAME, str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        assert outcome.applied is not None and outcome.applied.ok is True
        text = (root / "pkg" / "aliased.py").read_text(encoding="utf-8")
        assert "from pkg import compute_amount as total_of" in text
        assert "return total_of(items)" in text
        ast.parse(text)

    def test_the_definition_keyword_is_never_renamed(self, tmp_path: Path) -> None:
        """A ``FunctionDef`` node's column points at ``def``, not the name. The
        first version of this scan renamed the keyword; this pins the fix."""
        root = tmp_path / "kw"
        root.mkdir()
        _write(
            root,
            "pkg.py",
            "def thing(value):\n    return value\n\n\nclass thing:\n    pass\n",
        )
        outcome = _apply(OP_RENAME, str(root), "thing", "widget")
        assert outcome.applied is not None and outcome.applied.ok is True
        text = (root / "pkg.py").read_text(encoding="utf-8")
        assert "def widget(value):" in text
        assert "class widget:" in text
        assert "def dewidget" not in text
        assert "class dewidget" not in text
        ast.parse(text)

    def test_an_async_definition_is_renamed_at_the_name(self, tmp_path: Path) -> None:
        root = tmp_path / "kw"
        root.mkdir()
        _write(root, "pkg.py", "async def thing(v):\n    return v\n")
        outcome = _apply(OP_RENAME, str(root), "thing", "widget")
        assert outcome.applied is not None and outcome.applied.ok is True
        text = (root / "pkg.py").read_text(encoding="utf-8")
        assert "async def widget(v):" in text
        ast.parse(text)

    def test_a_renamed_symbol_is_never_reported_twice(self, tmp_path: Path) -> None:
        """The completeness pass must not re-report an occurrence the
        role-aware scan already NAMED, or the receipt double-counts it."""
        root = _rename_repo(tmp_path / "repo")
        plan = rename_symbol(
            str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        locations = [
            (item.path, item.line, item.kind) for item in plan.receipt.unresolved
        ]
        assert len(locations) == len(set(locations)), sorted(locations)

    def test_a_bare_reference_from_a_file_that_never_imported_the_target_blocks(
        self, tmp_path: Path
    ) -> None:
        """A re-export or a namespace injection could make a bare reference
        the same symbol, so it is NAMED and blocks rather than assumed away."""
        root = _rename_repo(tmp_path / "repo")
        _write(
            root,
            "pkg/indirect.py",
            "def build(items):\n    return compute_total(items)\n",
        )
        plan = rename_symbol(
            str(root), "compute_total", "compute_amount", path=TARGET_PATH
        )
        named = [
            item
            for item in plan.receipt.blocking_unresolved
            if item.path == "pkg/indirect.py"
        ]
        assert named
        assert "does not import it from the defining module" in named[0].detail
        assert plan.complete is False

    def test_a_decorated_definition_is_renamed_at_the_name(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "dec"
        root.mkdir()
        _write(
            root,
            "pkg.py",
            "def deco(fn):\n    return fn\n"
            "\n"
            "\n"
            "@deco\n"
            "def thing(value):\n    return value\n",
        )
        outcome = _apply(OP_RENAME, str(root), "thing", "widget")
        assert outcome.applied is not None and outcome.applied.ok is True
        text = (root / "pkg.py").read_text(encoding="utf-8")
        assert "@deco\ndef widget(value):" in text
        ast.parse(text)
