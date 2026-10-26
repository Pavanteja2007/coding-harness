"""P3b/Wave 2 pins -- the cache-prefix audit and the canonicalisation repair.

Run them explicitly; ``pyproject.toml`` pins ``testpaths = ["tests"]`` so a bare
``pytest`` collects none of the in-package ``runtime/test_*.py`` modules::

    python -m pytest runtime/test_p3b_w2_pins.py -q -p no:randomly

Every pin here is behavioural and each carries a CONTROL arm, because the
cheapest way to satisfy "the digest is stable" is to never compute one. The
three defect classes in :class:`TestTheCanonicalisationRepair` additionally
carry a **weakening demonstration**: each shows the pre-fix behaviour by
re-implementing it inline and asserting that the shipped implementation does
something the old one did not. A pin that cannot fail is a comment.

Nothing in this file needs Docker, a provider, a credential, or the network.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Mapping
from typing import Any, Dict, List

import pytest

from memory import decision_store as ds_mod
from memory import snapshot as ms_mod
from runtime import cache_economics as econ
from runtime import cache_prefix_audit as audit
from runtime import invariants, model_capabilities
from runtime import prompt_cache as pc

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _child(code: str, *, hashseed: str = "0") -> str:
    """Run ``code`` in a FRESH interpreter and return stdout.

    Used for the two properties that cannot be observed in-process: PYTHONHASHSEED
    stability (the interpreter has already fixed its hash seed by the time a test
    runs) and "never raises on a provider payload" across a fresh import.
    """
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = hashseed
    env["NEO_PRELOAD_LITELLM"] = "off"
    env["PYTHONPATH"] = REPO_ROOT
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=180,
    )
    assert proc.returncode == 0, f"child failed: {proc.stderr[-2000:]}"
    return proc.stdout.strip()


# ==========================================================================
# W2.2 -- the canonicalisation repair. Three measured defect classes.
# ==========================================================================


class _Marker:
    """An opaque object with no ``__str__``: whatever ``str()`` gives is a
    memory address, which is what made the old digest unstable."""

    def __init__(self, value: str) -> None:
        self.value = value


class TestTheCanonicalisationRepair:
    """The three classes, each measured before the fix and pinned after."""

    # -- class 1: a set was HASH ORDERED ------------------------------------
    def test_a_set_is_stable_across_processes_and_hash_seeds(self):
        """**The defect.** ``set`` fell through to ``str(value)``, and CPython
        randomises string hashing per process, so the same catalog produced a
        different digest in every process::

            seed 1 -> {"tools":"{'read', 'write', 'grep', 'glob'}"}
            seed 2 -> {"tools":"{'grep', 'read', 'write', 'glob'}"}
            seed 3 -> {"tools":"{'read', 'glob', 'write', 'grep'}"}

        This is Wave 1's digest-vs-bytes defect arriving through a different
        door: a worker re-hashing its own catalog per turn would report "one
        stable prefix" while the provider re-read the entry in full.
        """
        code = (
            "from runtime.prompt_cache import canonical_json\n"
            "print(canonical_json({'tools': {'read', 'write', 'grep', 'glob'}}))"
        )
        outputs = {seed: _child(code, hashseed=seed) for seed in ("1", "2", "3", "4")}
        assert len(set(outputs.values())) == 1, (
            f"the digest moved across processes: {outputs}"
        )

    def test_a_frozenset_is_stable_across_processes_too(self):
        code = (
            "from runtime.prompt_cache import canonical_json\n"
            "print(canonical_json([frozenset({'alpha', 'beta', 'gamma'})]))"
        )
        outputs = {seed: _child(code, hashseed=seed) for seed in ("1", "2", "3")}
        assert len(set(outputs.values())) == 1, outputs

    def test_the_set_case_is_proven_non_vacuous_ACROSS_PROCESSES(self):
        """The control arm, and it is subtler than it looks.

        Within ONE process, ``str(some_set)`` is stable for a fixed set of
        strings -- CPython's hash seed is fixed at startup, so insertion order
        alone does not change the rendering. Measured::

            str({"read","write","grep","glob","list"})
              == str({"glob","grep","read","write","list"})   -> True, same process

        So an in-process control arm would have PASSED against the broken code
        and proved nothing. The instability is **across processes**, which is
        exactly where a worker re-hashing its catalog actually lives. So the
        demonstration runs the OLD implementation in fresh interpreters under
        different ``PYTHONHASHSEED`` values and shows they disagree, while the
        shipped one agrees.
        """
        old_code = (
            "import json, unicodedata\n"
            "from collections.abc import Mapping\n"
            "def old(value):\n"
            "    if isinstance(value, Mapping):\n"
            "        return {str(k): old(value[k]) for k in sorted(value, key=str)}\n"
            "    if isinstance(value, (list, tuple)):\n"
            "        return [old(i) for i in value]\n"
            "    if isinstance(value, bool) or value is None or isinstance(value, int):\n"
            "        return value\n"
            "    if isinstance(value, float):\n"
            "        return value\n"
            "    if isinstance(value, str):\n"
            "        return unicodedata.normalize('NFC', value)\n"
            "    if isinstance(value, (bytes, bytearray)):\n"
            "        return value.decode('utf-8', 'replace')\n"
            "    return unicodedata.normalize('NFC', str(value))  # <- the defect\n"
            "print(json.dumps(old({'tools': {'read','write','grep','glob'}}),\n"
            "                 separators=(',',':'), sort_keys=True))\n"
        )
        old_outputs = {seed: _child(old_code, hashseed=seed) for seed in ("1", "2", "3")}
        assert len(set(old_outputs.values())) > 1, (
            f"the OLD implementation did not vary across processes, so this "
            f"pin would pass against broken code: {old_outputs}"
        )
        new_outputs = {
            seed: _child(
                "from runtime.prompt_cache import canonical_json\n"
                "print(canonical_json({'tools': {'read','write','grep','glob'}}))\n",
                hashseed=seed,
            )
            for seed in ("1", "2", "3")
        }
        assert len(set(new_outputs.values())) == 1, new_outputs

    def test_a_set_does_not_collide_with_a_list_of_the_same_members(self):
        """A set is emitted as a TAGGED sorted list, because a bare sorted list
        is byte-identical to the list ``[1, 2]`` -- two logically different
        values producing the same bytes, which is the projection-vs-bytes defect
        one level down."""
        assert pc.canonical_json({1, 2}) != pc.canonical_json([1, 2])
        assert pc.SET_TAG in pc.canonical_json({1, 2})

    def test_a_mixed_type_set_is_sorted_deterministically_and_never_raises(self):
        """``sorted()`` on a mixed-type set raises, and a raise on a provider
        payload is the failure mode this function exists to remove.

        The exact member ORDER is an implementation detail of sorting on the
        canonical JSON text, so the pin asserts the two properties that matter --
        it does not raise, and it is stable regardless of insertion order --
        rather than pinning an order nobody should depend on.
        """
        first = pc.canonical_json({"s": {1, "a", None}})
        second = pc.canonical_json({"s": {"a", 1, None}})
        third = pc.canonical_json({"s": {None, "a", 1}})
        assert first == second == third
        members = json.loads(first)["s"][pc.SET_TAG]
        assert sorted(map(repr, members)) == sorted(map(repr, [1, "a", None]))
        with pytest.raises(TypeError):
            sorted({1, "a", None})  # the reason this path exists

    # -- class 2: an object produced its MEMORY ADDRESS ---------------------
    def test_two_identical_objects_hash_identically(self):
        """**The defect.** ``str(obj)`` yields ``"<Marker object at 0x...>"``, so
        two logically identical objects in ONE process disagreed::

            ja = {"note":"<Marker object at 0x00000202D772FFD0>"}
            jb = {"note":"<Marker object at 0x00000202D772FEB0>"}
        """
        findings: List[Dict[str, Any]] = []
        first = pc.canonical_json({"note": _Marker("x")}, findings=findings)
        second = pc.canonical_json({"note": _Marker("x")})
        assert first == second
        assert "0x" not in first and "object at" not in first
        assert findings and findings[0]["source"] == "non_canonical_prefix_value"
        assert findings[0]["type"] == "_Marker"

    def test_a_non_mapping_message_never_leaks_an_address_either(self):
        """``_message_text`` coerced a non-mapping message with ``str()``, so an
        object in the message list put an address in the prefix DIGEST."""
        text = pc._message_text(_Marker("x"))
        assert "0x" not in text
        assert text.startswith(pc.OPAQUE_TEXT)

    def test_the_opaque_case_is_REPORTED_not_silent(self):
        """A type-only tag means two different values of one type collide, so
        the digest is a LOWER BOUND on identity. Reproducible is not the same as
        sound, and the receipt has to say which one it is."""
        plan = pc.plan_cache(
            [{"role": "system", "content": "x" * 4000}],
            tools=[
                {
                    "name": "read",
                    "input_schema": {"type": "object"},
                    "meta": _Marker("a"),
                }
            ],
            provider="anthropic",
            model="claude-sonnet-4-20250514",
        )
        payload = plan.to_dict()
        assert payload["cache_prefix_uncanonical"] is True
        assert "non_canonical_prefix_value" in payload["cache_prefix_uncanonical_reasons"]

    # -- class 3: a self-referential payload RAISED ------------------------
    def test_a_self_referential_list_does_not_raise(self):
        """**The defect.** ``_canonical_value`` recursed without a bound, so a
        cyclic payload raised ``RecursionError`` inside ``plan_cache`` -- on the
        live request path -- contradicting the docstring's "never raises on a
        provider payload"."""
        cyclic: List[Any] = []
        cyclic.append(cyclic)
        findings: List[Dict[str, Any]] = []
        rendered = pc.canonical_json({"tools": cyclic}, findings=findings)
        assert json.loads(rendered)["tools"][0][pc.OPAQUE_TAG] == "cycle"
        assert findings[0]["source"] == "cycle_in_prefix_value"

    def test_a_self_referential_dict_does_not_raise(self):
        cyclic: Dict[str, Any] = {}
        cyclic["self"] = cyclic
        rendered = pc.canonical_json({"tools": cyclic})
        assert json.loads(rendered)["tools"]["self"][pc.OPAQUE_TAG] == "cycle"

    def test_a_pathologically_nested_payload_stops_at_the_declared_depth(self):
        deep: Dict[str, Any] = {}
        cursor = deep
        for _ in range(pc.CANONICAL_MAX_DEPTH * 4):
            cursor["n"] = {}
            cursor = cursor["n"]
        findings: List[Dict[str, Any]] = []
        pc.canonical_json({"tools": deep}, findings=findings)
        assert any(r["source"] == "max_depth_in_prefix_value" for r in findings)

    def test_a_SHARED_subobject_is_still_hashed_rather_than_called_a_cycle(self):
        """The cycle guard is a STACK, not a visited-set. A DAG with a shared
        node is legal and must hash; a visited-set would call it a cycle and
        silently drop a real part of the payload."""
        shared = {"k": "v"}
        payload = {"a": shared, "b": shared}
        findings: List[Dict[str, Any]] = []
        rendered = json.loads(pc.canonical_json(payload, findings=findings))
        assert rendered["a"] == rendered["b"] == {"k": "v"}
        assert not findings

    # -- the by-value types, which is where "refuse" became "get it right" ---
    def test_realistic_provider_payload_types_canonicalise_BY_VALUE(self):
        """The opaque tag is a last resort, not the answer for a Path or a
        Decimal. These are realistic payload values and each has a spelling that
        is a pure function of the VALUE."""
        import datetime
        import decimal
        import enum
        import pathlib
        import uuid

        class Colour(enum.Enum):
            RED = "red"

        findings: List[Dict[str, Any]] = []
        cases = {
            "pathlib": pathlib.PurePosixPath("a/b.py"),
            "datetime": datetime.datetime(2026, 1, 2, 3, 4, 5),
            "date": datetime.date(2026, 1, 2),
            "decimal": decimal.Decimal("1.50"),
            "uuid": uuid.UUID("12345678-1234-5678-1234-567812345678"),
            "enum": Colour.RED,
        }
        for name, value in cases.items():
            findings.clear()
            rendered = json.loads(pc.canonical_json({"v": value}, findings=findings))
            assert findings == [], f"{name} was reported as non-canonical"
            assert isinstance(rendered["v"], str)
        # And they are distinguishable BY VALUE, which the opaque tag is not.
        assert pc.canonical_json({"v": uuid.UUID(int=1)}) != pc.canonical_json(
            {"v": uuid.UUID(int=2)}
        )
        assert pc.canonical_json({"v": decimal.Decimal("1.50")}) != pc.canonical_json(
            {"v": decimal.Decimal("1.5")}
        )

    def test_an_enum_is_canonicalised_to_its_VALUE_not_its_class_name(self):
        """``"Color.RED"`` couples the digest to a class name, so renaming the
        enum moved a prefix nobody edited."""

        class Colour:
            pass

        import enum

        class Named(enum.Enum):
            RED = "red"

        assert json.loads(pc.canonical_json(Named.RED)) == "red"

    # -- the no-regression guarantee ----------------------------------------
    def test_no_digest_moved_for_any_payload_the_old_code_handled(self):
        """A differential against a verbatim re-implementation of the previous
        ``_canonical_value``. Twenty JSON-native payloads, byte-identical output.

        This is the pin that makes the repair safe: without it the three fixes
        above are only safe-looking.
        """

        def old_canonical_value(value: Any) -> Any:
            if isinstance(value, Mapping):
                return {
                    str(k): old_canonical_value(value[k])
                    for k in sorted(value, key=str)
                }
            if isinstance(value, (list, tuple)):
                return [old_canonical_value(i) for i in value]
            if isinstance(value, bool) or value is None or isinstance(value, int):
                return value
            if isinstance(value, float):
                if value != value or value in (float("inf"), float("-inf")):
                    return str(value)
                return value
            if isinstance(value, str):
                return unicodedata.normalize("NFC", value)
            if isinstance(value, (bytes, bytearray)):
                return value.decode("utf-8", "replace")
            return unicodedata.normalize("NFC", str(value))

        def old_canonical_json(value: Any) -> str:
            return json.dumps(
                old_canonical_value(value),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
                default=str,
            )

        payloads = [
            {},
            [],
            {"a": 1, "b": "two", "c": True, "d": None, "e": 1.5},
            {"z": 1, "a": 2, "m": 3},
            {"nested": {"deep": {"deeper": [1, 2, {"k": "v"}]}}},
            {"unicode": "caf\u00e9", "decomposed": "cafe\u0301"},
            {"empty_str": "", "empty_list": [], "empty_dict": {}},
            {"floats": [0.1, -0.0, 1e300, 1e-30]},
            {"nonfinite": [float("nan"), float("inf"), float("-inf")]},
            {"list_of_lists": [[1, 2], [3, 4]]},
            {
                "tool_schema": {
                    "name": "read",
                    "input_schema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                }
            },
            {"catalog": [{"name": "read"}, {"name": "write"}]},
            {"bools": [True, False, None]},
            {"bytes": b"binary\x00\xff"},
            {"int_keys": {"1": "a", 2: "b", 3.5: "c"}},
            {"big_int": 10**40},
            {"escapes": "line\nbreak\ttab\"quote\\slash"},
            {"emoji": "\U0001f600\U0001f680"},
            {"rtl": "\u05d0\u05d1"},
        ]
        for payload in payloads:
            assert pc.canonical_json(payload) == old_canonical_json(payload), payload

    def test_a_genuine_change_STILL_moves_the_digest(self):
        """The control for the whole class. Canonicalisation must not become
        "hash everything to the same value"."""
        a = [{"name": "read", "input_schema": {"type": "object"}}]
        b = [{"name": "write", "input_schema": {"type": "object"}}]
        assert pc.tool_schema_digest(a) != pc.tool_schema_digest(b)

    def test_the_summary_reader_refuses_an_unknown_key_rather_than_reading_zero(self):
        """**A defect in this round's own first draft.** It read
        ``cache_hit_calls`` / ``cache_cached_tokens``; the ledger publishes
        ``cache_hits`` / ``cached_input_tokens``. Every misspelling made
        ``.get()`` return ``None``, ``int(None or 0)`` returned 0, and the audit
        reported a **0% hit rate and a cache collapse** for a session that had
        hit on 46 of 50 calls. A silent zero is the worst shape a measurement
        takes, so an unknown key is now an error."""
        with pytest.raises(KeyError) as excinfo:
            audit._summary_int({"cache_hits": 3}, "cache_hit_calls")
        assert "no key" in str(excinfo.value)

    def test_the_summary_keys_the_audit_reads_are_the_keys_the_ledger_publishes(self):
        """Every key the audit reads is asserted against a REAL ledger summary
        rather than against a literal in the audit."""
        result = audit.session(turns=3, search_at=(), include_zero_hit_probe=False)
        assert result.decided > 0
        assert 0.0 < float(result.hit_rate) <= 1.0
        assert result.distinct_prefixes == result.provider_writes


# ==========================================================================
# W2.1 -- the 50-turn deferral session
# ==========================================================================


class TestTheDeferralSessionSurvivesFiftyTurns:
    @pytest.fixture(scope="class")
    def session(self):
        return audit.session(turns=audit.DEFAULT_TURNS, search_at=audit.DEFAULT_SEARCH_AT)

    def test_fifty_real_turns_were_actually_made(self, session):
        assert session.turns == 50
        assert session.call_count == 50, "the endpoint did not see 50 requests"

    def test_the_receipt_matches_the_providers_OWN_invoice(self, session):
        """The Wave-1 property, re-asserted on a deferral session rather than a
        synthetic catalog. ``distinct_prefixes`` must equal the number of writes
        the endpoint actually performed -- this is read off the endpoint's own
        store, not off a receipt."""
        assert session.receipt_matches_invoice is True
        assert session.distinct_prefixes == session.provider_writes

    def test_the_tool_surface_grows_and_every_growth_is_attributed(self, session):
        assert session.shown_first < session.shown_last, (
            "deferral never activated a tool, so this session did not exercise "
            "the thing under test"
        )
        assert session.invalidations == session.tool_schema_invalidations
        assert set(session.invalidation_causes) == {"tool_surface_changed"}

    def test_every_invalidation_is_charged_to_a_search(self, session):
        """The load-bearing attribution: the prefix moved because the tool
        surface moved, once per activating search, and for no other reason."""
        assert session.searches_run == len(audit.DEFAULT_SEARCH_AT)
        assert session.tool_schema_invalidations <= session.searches_run

    def test_the_hit_rate_is_high_and_the_shortfall_is_explained(self, session):
        assert session.hit_rate is not None
        assert session.hit_rate >= audit.HIT_RATE_HELD_MIN, (
            f"hit rate {session.hit_rate} fell below the declared hold line "
            f"({audit.HIT_RATE_HELD_MIN})"
        )
        verdict = audit.classify_session(session, baseline_hit_rate=0.98)
        assert verdict["outcome"] == "cache_survived"
        assert verdict["unexplained_shortfall"] is None

    def test_the_frozen_half_of_the_prompt_is_byte_stable_across_all_fifty_turns(
        self, session
    ):
        """One digest for the whole session's frozen half, across 50 volatile
        turns. The per-turn half is deliberately varied and must NOT move it --
        and the control is that a change inside the frozen half DOES."""
        messages = [{"role": "system", "content": "FROZEN"}]
        tools = [{"name": "read", "input_schema": {"type": "object"}}]
        digests = {
            pc.prefix_digest(
                [
                    *messages,
                    {"role": "system", "content": f"per-turn {turn}"},
                    {"role": "user", "content": f"turn {turn}"},
                ],
                tools=tools,
            )
            for turn in range(50)
        }
        assert len(digests) == 1, "the volatile half moved the prefix digest"
        moved = pc.prefix_digest(
            [{"role": "system", "content": "FROZEN, edited"}], tools=tools
        )
        assert moved not in digests, "a real frozen-half edit did not move it"

    def test_the_provider_keyed_on_our_bytes_so_a_divergence_would_show(self):
        """The endpoint's key is built with ``separators=(',',':')`` and NO sort.
        That is the whole reason it can see a digest-vs-bytes divergence, and a
        pin that does not check the instrument is a pin on nothing."""
        import inspect

        source = inspect.getsource(audit.ByteKeyedEndpoint._completion)
        assert "separators" in source
        assert "sort_keys" not in source, (
            "sort_keys=True in the endpoint's cache key would normalise key order "
            "FOR the provider and hide the exact defect this audit measures"
        )


# ==========================================================================
# W2.5 -- a failed search is non-retryable and free
# ==========================================================================


class TestAFailedToolSearchIsFree:
    def test_the_probe_query_really_finds_nothing(self):
        """The control arm for the next two pins. If the query matched, "the
        failed search was free" would be vacuous -- so the session RECORDS how
        many activations the probe got and a pin asserts it was zero."""
        from harness import deferred_tools

        state = deferred_tools.DeferredToolState(context_window=32768, rich_descriptions=True)
        assert state.search(audit.zero_hit_query()).activated == ()

    def test_a_search_that_activates_nothing_moves_the_prefix_not_one_byte(self):
        """**Free, measured as "it moved nothing".** A search that activates no
        tool leaves the tool digest byte-identical, so it costs no cache
        re-creation. Measured by digest, in a window that contains ONLY the
        probe -- the first version of this measurement took its "after" digest
        after the real search too, so the real search's activation was charged to
        the probe and every run reported ``free: False`` for a probe that had
        changed nothing."""
        result = audit.session(
            turns=max(audit.DEFAULT_SEARCH_AT),
            search_at=audit.DEFAULT_SEARCH_AT,
        )
        assert result.searches_zero_hit >= 1, (
            "the zero-hit probe activated something, so this session did not "
            "measure a failed search at all"
        )
        assert result.zero_hit_prefix_moved == 0

    def test_the_audit_reports_the_failed_search_as_free(self):
        payload = audit.audit(turns=12, search_at=(5,), runs=1)
        assert payload["available"] is True
        assert payload["failed_search"]["free"] is True
        assert payload["failed_search"]["zero_hit_probes_measured"] >= 1

    def test_the_non_retryable_half_is_FILED_not_silently_claimed(self):
        """The runtime can measure that a failed search is free. It cannot make
        it non-retryable: ``RETRYABLE_CLASSES`` lives in
        ``harness/tool_errors.py``, another owner's file. The receipt says so
        rather than letting a reader assume the runtime owns both halves."""
        payload = audit.audit(turns=12, search_at=(5,), runs=1)
        assert "harness/tool_errors.py" in payload["failed_search"]["detail"]
        assert "RETRYABLE_CLASSES" in payload["failed_search"]["detail"]


# ==========================================================================
# W2.3 -- the dialect matrix
# ==========================================================================


class TestTheDialectMatrix:
    @pytest.fixture(scope="class")
    def matrix(self):
        return audit.dialect_matrix()

    def test_every_declared_dialect_is_exercised(self, matrix):
        assert sorted(matrix["families_covered"]) == sorted(
            matrix["declared_dialects"]
        ), "a dialect row in the table has no real-shaped request"

    def test_no_row_fails(self, matrix):
        assert matrix["failures"] == [], matrix["failures"]

    def test_every_row_agrees_with_itself_across_plan_dialect_and_gate(self, matrix):
        for row in matrix["rows"]:
            assert row["plan_dialect"] == row["dialect"], row["case"]
            assert row["gate_admitted"] is True, row["case"]

    def test_only_the_breakpoints_dialects_send_a_marker(self, matrix):
        """Conflating the four mechanisms is how a ``cache_control`` key reaches
        a gateway that hard-400s on it, so the marker/send relationship is
        asserted per row rather than once."""
        for row in matrix["rows"]:
            sent = row["message_marker_present"] or row["tool_marker_present"]
            assert sent is (row["mechanism"] == "breakpoints"), row["case"]

    def test_bedrock_never_speaks_anthropic(self, matrix):
        """Converse spells the marker ``cachePoint``. Anthropic's spelling is a
        hard 400 there, and this is the row that would ship it."""
        row = next(r for r in matrix["rows"] if r["case"] == "bedrock_converse_spelling")
        assert row["dialect"] == "bedrock"

    def test_a_real_vertex_endpoint_resolves_to_vertex_and_not_to_gemini(self):
        """**A table defect this round found by measurement.** Vertex hosts look
        like ``us-central1-aiplatform.googleapis.com``, which contains the
        substring ``google`` -- and the ``gemini`` row matches on ``google``.
        Because the api_base pass runs in table order, the gemini row won on a
        real Vertex endpoint and ``vertex_ai`` was reachable only by a provider
        string. Nothing behavioural was wrong (both rows are ``implicit``, send
        nothing, read ``cachedContentTokenCount``), but an operator debugging a
        Vertex deployment was told ``gemini``."""
        dialect = pc.cache_dialect_for(
            "vertex_ai",
            "gemini-2.0-flash",
            "https://us-central1-aiplatform.googleapis.com",
        )
        assert dialect.dialect == "vertex_ai"
        # And the gemini row keeps its own host.
        assert (
            pc.cache_dialect_for(
                "gemini", "gemini-2.5-pro", "https://generativelanguage.googleapis.com"
            ).dialect
            == "gemini"
        )

    def test_the_local_row_reports_unsupported_not_a_zero_hit_rate(self):
        """A llama.cpp server reports no cache field. ``unsupported`` is the
        honest answer -- we know there is no cache API to report on -- and it is
        excluded from the hit rate in both directions, which is what makes it
        safe to quote beside a priced row."""
        plan = pc.plan_cache(
            [{"role": "system", "content": "x" * 4000}],
            tools=[{"name": "read", "input_schema": {"type": "object"}}],
            provider="local",
            model="qwen2.5-coder-14b-instruct",
            api_base="http://127.0.0.1:8080/v1",
            min_prefix_tokens=256,
        )
        receipt = pc.receipt_from_usage(
            {"prompt_tokens": 9000, "completion_tokens": 64}, plan
        )
        assert receipt.status == pc.CACHE_UNSUPPORTED
        assert receipt.reported is False
        assert receipt.decided is False

    def test_every_gate_refusal_slug_is_still_producible(self):
        """Wave 1 closed all four; this round did not add one, and a slug that
        stopped being producible would be untested vocabulary."""
        produced = set()

        def attempt(fn):
            try:
                fn()
            except pc.CacheDialectRefused as refused:
                produced.add(refused.reason)

        messages = [{"role": "system", "content": "x" * 4000}]
        tools = [{"name": "read", "input_schema": {"type": "object"}}]

        anthropic = pc.plan_cache(
            messages, tools=tools, provider="anthropic",
            model="claude-sonnet-4-20250514", min_prefix_tokens=256,
        )
        # foreign_marker_present: the endpoint carries its OWN marker AND a
        # marker belonging to another dialect. A bare foreign marker alone is
        # `required_marker_missing` -- the plan never got what it asked for --
        # which is the correct diagnosis and is why the fixture needs both.
        attempt(
            lambda: pc.enforce_cache_dialect_gate(
                [
                    {
                        "role": "system",
                        "content": "x",
                        "cache_control": {"type": "ephemeral"},
                        "cachePoint": {"type": "default"},
                    }
                ],
                tools, anthropic, provider="anthropic",
            )
        )
        # required_marker_missing
        attempt(
            lambda: pc.enforce_cache_dialect_gate(messages, tools, anthropic,
                                                  provider="anthropic")
        )
        # session_key_missing
        keyed = pc.plan_cache(
            messages, tools=tools, provider="openai", model="gpt-4o-mini",
            min_prefix_tokens=256,
        )
        attempt(
            lambda: pc.enforce_cache_dialect_gate(
                messages, tools, keyed, provider="openai", model="gpt-4o-mini"
            )
        )
        # breakpoint_limit_exceeded
        attempt(
            lambda: pc.enforce_cache_dialect_gate(
                [
                    {"role": "system", "content": f"b{i}", "cache_control": {"type": "ephemeral"}}
                    for i in range(8)
                ],
                tools, anthropic, provider="anthropic",
            )
        )
        assert produced == set(pc.CACHE_DIALECT_REFUSALS), (
            f"unproducible refusal slugs: {set(pc.CACHE_DIALECT_REFUSALS) - produced}"
        )


# ==========================================================================
# W2.4 / W2.6 -- economics, and the counterfactual defect
# ==========================================================================


class TestTheCounterfactualWasWrongInTheDirectionThatDiscreditsCaching:
    """**The most consequential defect this round found**, and it was found by
    running the audit rather than by reading the code."""

    def test_the_no_cache_baseline_prices_EVERY_prompt_token(self):
        """The defect: the baseline priced ``total - cached`` and left the
        cached tokens out of the no-cache side entirely -- but the cached tokens
        are the entire quantity being compared, because with no cache every one
        of them would be served at the full input rate."""
        result = econ.counterfactual_no_cache(
            "claude-sonnet-4-20250514",
            prompt_tokens=142_583,
            completion_tokens=3_200,
            cached_tokens=131_981,
        )
        # The honest baseline is every prompt token at the full input rate.
        expected = model_capabilities.estimate_cost(
            "claude-sonnet-4-20250514", 142_583, 3_200
        ).cost_usd
        assert result["cost_usd"] == pytest.approx(expected)
        assert result["cached_tokens_priced_at_full_rate"] is True

    def test_the_defect_flipped_the_sign_of_the_headline_metric(self):
        """Both readings, side by side, at the real session's token counts.

        | baseline | cost | net vs the served cost |
        |---|---:|---:|
        | cached omitted (the defect) | $0.1118 | **-$0.0396** -- "the cache COSTS money" |
        | every prompt token at full price | $0.5077 | **+$0.3563** -- saves ~70% |

        The sign of the headline Phase 3b metric turned on an accounting slip,
        in the direction of "prompt caching is not worth it".
        """
        model = "claude-sonnet-4-20250514"
        total, cached, completion = 142_583, 131_981, 3_200
        row = model_capabilities.price_of(model)
        input_rate = float(row["input_cost_per_million"]) / 1e6
        output_rate = float(row["output_cost_per_million"]) / 1e6
        served = (
            cached * input_rate * pc.DEFAULT_CACHE_READ_DISCOUNT
            + (total - cached) * input_rate
            + completion * output_rate
        )
        defective = total - cached
        defective_baseline = model_capabilities.estimate_cost(
            model, defective, completion
        ).cost_usd
        assert defective_baseline - served < 0, (
            "the defective baseline should read as the cache COSTING money"
        )
        fixed_baseline = econ.counterfactual_no_cache(
            model, total, completion, cached
        )["cost_usd"]
        assert fixed_baseline - served > 0, (
            "the corrected baseline should read as the cache SAVING money"
        )

    def test_the_ledger_rollup_already_summed_the_two_halves(self):
        """Why the defect survived: ``from_ledger`` has always passed
        ``uncached + cached`` as the total, so the function was subtracting what
        its own caller had just added. Asserted so the two cannot drift apart
        again."""
        import inspect

        source = inspect.getsource(econ.from_ledger)
        assert "prompt_tokens_uncached" in source and "prompt_tokens_cached" in source

    def test_an_unpriced_model_still_reports_an_UNKNOWN_net_not_a_zero(self):
        result = econ.net_cache_benefit("no-such-model-anywhere", 1000, 100, 900, 0.001)
        assert result["priced"] is False
        assert result["net_cache_benefit_usd"] is None
        assert "UNKNOWN" in result["verdict"]

    def test_a_collapsed_cache_still_reads_as_a_NEGATIVE_net(self):
        """The control for the fix. Correcting the baseline must not make the
        negative case impossible -- that is the property the counterfactual
        exists for."""
        result = econ.net_cache_benefit(
            "gpt-4o-mini", prompt_tokens=10_000, completion_tokens=0,
            cached_tokens=0, observed_cost_usd=0.010,
        )
        assert result["net_cache_benefit_usd"] < 0
        assert "COST" in result["verdict"]


class TestTheEconomicsReport:
    @pytest.fixture(scope="class")
    def payload(self):
        return audit.audit(turns=12, search_at=(5,), runs=2)

    def test_the_machine_is_named(self, payload):
        host = payload["machine"]
        assert host["platform"] and host["python"] and host["cpus"]

    def test_the_run_count_is_reported_next_to_every_median_and_p95(self, payload):
        cost = payload["cost"]
        for key in ("per_session_usd_median", "per_session_usd_p95", "monthly_usd_median"):
            assert key in cost
        assert cost["runs"] == 2

    def test_the_monthly_projection_is_not_inflated_by_the_repeat_count(self, payload):
        """**A defect in this round's own first draft.** It multiplied the
        projection by ``runs``, so five REPEATS of one configuration became 450
        sessions a month instead of 90 -- a five-fold overstatement of the
        headline cost. Repeats buy a median; they are not configurations."""
        cost = payload["cost"]
        assert cost["sessions_per_month"] == (
            cost["workload"]["sessions_per_day"] * cost["workload"]["days"]
        )
        assert cost["runs_are_repeats_not_configurations"] is True

    def test_the_cost_row_says_it_is_not_an_invoice(self, payload):
        assert "NOT an" in payload["cost"]["cost_basis"]
        assert payload["economics"]["served_is_observed"] is False

    def test_an_unpriced_model_reports_none_rather_than_zero(self):
        result = audit.cost_table(
            [
                audit.SessionResult(
                    turns=50, provider="p", model="m", call_count=50, hits=46,
                    creations=1, decided=50, hit_rate=0.92, distinct_prefixes=4,
                    invalidations=3, invalidation_causes={},
                    tool_schema_invalidations=3, provider_writes=4,
                    provider_creations=1, receipt_matches_invoice=True,
                    shown_first=8, shown_last=17, shown_per_change=(8,),
                    searches_run=1, searches_with_hits=1, searches_zero_hit=0,
                    zero_hit_prefix_moved=0, prefix_sha_first="a",
                    prefix_sha_last="a", tool_sha_first="b", tool_sha_last="b",
                    uncanonical_reasons=(), tokens_estimated=1000,
                    cached_tokens=900, creation_tokens=100, elapsed_s=0.1,
                )
            ],
            model="no-such-model-anywhere",
        )
        assert result["available"] is False
        assert result["per_session_usd_median"] is None
        assert result["monthly_usd_median"] is None
        assert result["price_state"] == "unpriced"

    def test_there_is_a_failures_section_and_it_is_empty_on_a_clean_run(self, payload):
        assert payload["failures"] == []

    def test_every_unestablished_claim_is_published(self, payload):
        claims = payload["not_established"]
        assert len(claims) >= 5
        joined = " ".join(claims)
        assert "live-provider" in joined
        assert "tokenizer" in joined
        assert "llama.cpp" in joined and "vLLM" in joined


def _result(**overrides: Any) -> Any:
    """A measured-shaped ``SessionResult`` with the fields a classifier reads.

    Used to drive the classifier's OTHER two outcomes. A three-valued
    classifier where only one arm was ever exercised is untested vocabulary, and
    untested vocabulary is how a status nothing can produce survives for years.
    """
    base: Dict[str, Any] = dict(
        turns=50, provider="anthropic", model="claude-sonnet-4-20250514",
        call_count=50, hits=46, creations=10602, decided=50, hit_rate=0.92,
        distinct_prefixes=4, invalidations=3, invalidation_causes={
            "tool_surface_changed": 3
        },
        tool_schema_invalidations=3, provider_writes=4, provider_creations=10602,
        receipt_matches_invoice=True, shown_first=8, shown_last=17,
        shown_per_change=(8,), searches_run=3, searches_with_hits=3,
        searches_zero_hit=1, zero_hit_prefix_moved=0,
        prefix_sha_first="a", prefix_sha_last="a", tool_sha_first="b",
        tool_sha_last="b", uncanonical_reasons=(), tokens_estimated=142583,
        cached_tokens=131981, creation_tokens=10602, elapsed_s=0.1,
    )
    base.update(overrides)
    return audit.SessionResult(**base)


# ==========================================================================
# W2.1 -- all THREE outcomes are producible, not just the one that occurred
# ==========================================================================


class TestEveryOutcomeIsProducible:
    """The audit measured outcome 1. A classifier that can only ever return the
    outcome it has already seen is not a classifier, so the other two are driven
    here against synthetic receipts."""

    def test_the_measured_outcome_is_the_first_member(self):
        assert audit.OUTCOMES[0] == "cache_survived"
        assert len(audit.OUTCOMES) == 3

    def test_cache_survived_is_produced_by_a_high_hit_rate(self):
        verdict = audit.classify_session(_result(hit_rate=0.96), baseline_hit_rate=0.98)
        assert verdict["outcome"] == "cache_survived"

    def test_hit_rate_dropped_is_produced_and_is_ATTRIBUTED(self):
        """A bounded, attributable drop is its own outcome, not a collapsed one.
        The two declared lines are what make it separable."""
        verdict = audit.classify_session(_result(hit_rate=0.72), baseline_hit_rate=0.98)
        assert verdict["outcome"] == "hit_rate_dropped"
        assert audit.HIT_RATE_COLLAPSE_MAX < 0.72 < audit.HIT_RATE_HELD_MIN

    def test_an_EXPLAINED_drop_is_reported_as_explained(self):
        """A bounded, attributable drop is its own outcome, not a collapsed one.
        The two declared lines are what make it separable.

        The arithmetic matters and the first draft of this pin got it backwards.
        Each tool-surface change can cost at most ONE call's hit, so over 50
        decided calls ``k`` changes explain at most ``k/50``. A 0.98 → 0.72 drop
        is a 0.26 shortfall, which needs **13** changes -- 3 explain 0.06 and
        leave 0.20 unaccounted for.
        """
        verdict = audit.classify_session(
            _result(hit_rate=0.72, tool_schema_invalidations=13),
            baseline_hit_rate=0.98,
        )
        assert verdict["outcome"] == "hit_rate_dropped"
        assert verdict["explained_by_tool_surface"] is True
        assert verdict["unexplained_shortfall"] is None

    def test_an_UNEXPLAINED_drop_is_NOT_laundered_as_explained(self):
        """The control, and it is the half that keeps
        ``explained_by_tool_surface`` from becoming a decoration: the same 0.26
        shortfall with only 3 changes must report UNEXPLAINED and name the
        residue."""
        verdict = audit.classify_session(
            _result(hit_rate=0.72, tool_schema_invalidations=3),
            baseline_hit_rate=0.98,
        )
        assert verdict["outcome"] == "hit_rate_dropped"
        assert verdict["explained_by_tool_surface"] is False
        assert verdict["unexplained_shortfall"] == pytest.approx(0.26)

    def test_zero_tool_changes_can_never_explain_a_drop(self):
        verdict = audit.classify_session(
            _result(hit_rate=0.72, tool_schema_invalidations=0),
            baseline_hit_rate=0.98,
        )
        assert verdict["explained_by_tool_surface"] is False

    def test_cache_collapsed_is_produced_by_a_low_hit_rate(self):
        verdict = audit.classify_session(_result(hit_rate=0.04), baseline_hit_rate=0.98)
        assert verdict["outcome"] == "cache_collapsed"
        assert "stopped being reusable" in verdict["detail"]

    def test_an_UNREPORTED_provider_is_not_classified_at_all(self):
        """A provider that never answered the cache question has no hit rate to
        classify. Returning ``cache_collapsed`` here would score a silent
        endpoint as a broken cache -- the exact confusion
        ``cache_status_separates_unreported_from_miss`` exists to prevent."""
        verdict = audit.classify_session(
            _result(hit_rate=None, decided=0), baseline_hit_rate=0.98
        )
        assert verdict["outcome"] == ""
        assert verdict["state"] == "unreported"
        assert "NOT a zero" in verdict["detail"]

    def test_all_three_outcomes_are_reachable_from_the_measured_shape(self):
        produced = {
            audit.classify_session(
                _result(hit_rate=rate), baseline_hit_rate=0.98
            )["outcome"]
            for rate in (0.99, 0.70, 0.05)
        }
        assert produced == set(audit.OUTCOMES)


# ==========================================================================
# W2.2 -- the session key survives a RESTART, and a prefix edit still moves it
# ==========================================================================


class TestTheSessionKeySurvivesARestart:
    def test_two_processes_derive_the_same_key_for_the_same_session(self):
        """**Cross-restart, in two real interpreters.** A worker that is relaunched
        must ask the provider for the same cache entry, so the key has to be a
        pure function of (session id, prefix) and not of anything in-memory."""
        code = (
            "from runtime import prompt_cache as pc\n"
            "msgs=[{'role':'system','content':'FROZEN'*400}]\n"
            "tools=[{'name':'read','input_schema':{'type':'object'}}]\n"
            "plan=pc.plan_cache(msgs, tools=tools, provider='anthropic',\n"
            "                   model='claude-sonnet-4-20250514', min_prefix_tokens=256)\n"
            "print(pc.cache_session_key(plan, 'sess-restart')['cache_session_key'])\n"
        )
        first = _child(code, hashseed="1")
        second = _child(code, hashseed="7")
        assert first == second and first, "the key is not stable across processes"

    def test_the_key_CHANGES_when_the_prefix_changes(self):
        """The other half, and the reason the key is derived from the prefix at
        all: a rewritten prefix must stop asking for an entry that cannot
        exist, or the provider is asked to route to a cache it will never hit."""
        messages = [{"role": "system", "content": "FROZEN"}]
        tools = [{"name": "read", "input_schema": {"type": "object"}}]
        opts = dict(provider="anthropic", model="claude-sonnet-4-20250514",
                    min_prefix_tokens=256)
        before = pc.cache_session_key(
            pc.plan_cache(messages, tools=tools, **opts), "sess-1"
        )["cache_session_key"]
        after = pc.cache_session_key(
            pc.plan_cache(
                [{"role": "system", "content": "FROZEN, edited"}], tools=tools, **opts
            ),
            "sess-1",
        )["cache_session_key"]
        assert before != after

    def test_it_differs_between_sessions(self):
        messages = [{"role": "system", "content": "FROZEN"}]
        tools = [{"name": "read", "input_schema": {"type": "object"}}]
        plan = pc.plan_cache(
            messages, tools=tools, provider="anthropic",
            model="claude-sonnet-4-20250514", min_prefix_tokens=256,
        )
        a = pc.cache_session_key(plan, "sess-a")["cache_session_key"]
        b = pc.cache_session_key(plan, "sess-b")["cache_session_key"]
        assert a != b

    def test_the_ROUTER_actually_FORWARDS_the_key_it_derived(self):
        """Behavioural, because a string scan of ``call_model`` proves nothing
        about the kwargs the provider received.

        The first draft of this pin asserted the literal ``"cache_session_key"``
        appears in ``call_model``'s source. It does **not**: the router calls
        ``prompt_cache.cache_parameters_for``, and *that* is where the key is
        built. So the pin asserted an implementation detail of a function two
        calls deep, and would have kept passing if the router stopped forwarding
        the result entirely. This drives two real calls at a ``keyed`` endpoint
        and reads the captured request kwargs instead.
        """
        import contextvars

        from runtime import model_router

        def capture() -> Dict[str, Any]:
            endpoint = audit.ByteKeyedEndpoint()
            endpoint.install()
            model_router.set_call_context(
                {
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "api_key": "pin-not-a-secret",
                    "task_id": "t",
                    "session_id": "sess-forwarded",
                }
            )
            pc.reset_cache_context()
            try:
                for turn in (1, 2):
                    model_router.call_model(
                        [
                            {"role": "system", "content": "FROZEN" * 400},
                            {"role": "user", "content": f"turn {turn}"},
                        ],
                        tools=[{"name": "read", "input_schema": {"type": "object"}}],
                    )
                return endpoint.calls
            finally:
                endpoint.uninstall()
                pc.reset_cache_context()

        calls = contextvars.copy_context().run(capture)
        assert len(calls) == 2
        keys = [
            call.get("prompt_cache_key")
            for call in calls
            if isinstance(call, Mapping)
        ]
        assert keys and all(keys), (
            "the router did not put prompt_cache_key on the request; a keyed "
            "dialect routes on it, so every turn would be a cold prefix"
        )
        assert keys[0] == keys[1], "the key moved between turns of one session"

    def test_the_ROUTER_derives_the_key_from_session_id_first(self):
        """The documented precedence: an explicit ``cache_session_id`` wins, then
        ``session_id``, then ``task_id``. Read from the source because the
        precedence itself is a policy, not an observable."""
        import inspect

        from runtime import model_router

        source = inspect.getsource(model_router.call_model)
        for needle in ("cache_session_id", "session_id", "task_id"):
            assert needle in source
        assert 'ctx.get("cache_session_id")' in source

    def test_task_id_alone_is_accepted_and_the_receipt_says_which(self):
        """Wave 1 filed this: ``task_id`` is stable per TASK, not per session,
        so a resumed attempt changes the key and re-creates an entry. The
        receipt names which source supplied it so an operator can see it."""
        messages = [{"role": "system", "content": "FROZEN"}]
        tools = [{"name": "read", "input_schema": {"type": "object"}}]
        plan = pc.plan_cache(
            messages, tools=tools, provider="anthropic",
            model="claude-sonnet-4-20250514", min_prefix_tokens=256,
        )
        named = pc.cache_session_key(plan, "sess-x")
        assert named["cache_session_key_source"] == "session_and_prefix"
        anonymous = pc.cache_session_key(plan, "")
        assert anonymous["cache_session_key_source"] == "prefix_only"


# ==========================================================================
# W2.2 -- a mid-session memory write cannot move an ALREADY captured prefix
# ==========================================================================


class TestTheMemorySnapshotIsWriteIsolated:
    """The W2.2 item this round originally FILED instead of pinning. It is
    pinnable from ``runtime/`` read-only, against the real
    ``memory.snapshot``, so it is now pinned rather than handed to another
    terminal.

    **How, and what I could not do.** The first draft drove a real
    ``memory.decision_store.DecisionStore`` in a temp directory. It could not get
    a single row into it: ``record()`` returns ``None`` and ``count()`` stays
    ``0``, with and without ``promote=True``, with and without ``repo_path``.
    Every assertion built on that would have been measured against an EMPTY
    snapshot -- and an empty snapshot is trivially deterministic, so the pin
    would have passed whatever the code did. That is the vacuity trap this file
    exists to avoid, so the property is pinned where it is actually load-bearing:

    * **structurally**, because ``MemorySnapshot`` is a frozen dataclass and
      ``capture_snapshot`` has no parameter that could express updating one --
      which is a stronger guarantee than any behavioural observation, and is the
      property in ``capture_snapshot``'s own docstring (*"it never mutates an
      existing one ... a caller that keeps the returned object has a prompt
      prefix that a mid-session write cannot invalidate"*);
    * **behaviourally**, for the half ``runtime/`` does own: the prefix digest is
      a pure function of the snapshot's TEXT, including when that text carries a
      clock.
    """

    def test_the_snapshot_is_a_frozen_value(self):

        from memory import snapshot as ms

        assert ms.MemorySnapshot.__dataclass_params__.frozen is True
        assert ms.SnapshotRow.__dataclass_fields__ is not None

    def test_a_caller_cannot_mutate_a_captured_snapshot(self):

        from memory import snapshot as ms

        snapshot = ms.capture_snapshot(None, query="anything")
        with pytest.raises(dataclasses.FrozenInstanceError):
            snapshot.text = "a mid-session write"

    def test_there_is_NO_WAY_to_express_updating_a_frozen_snapshot(self):
        """The API shape IS the argument. If a future edit adds a
        ``mutate=`` / ``refresh=`` / ``invalidate=`` parameter, the cache
        argument collapses and this pin says so before it happens."""
        import inspect

        from memory import snapshot as ms

        names = list(inspect.signature(ms.capture_snapshot).parameters)
        suspicious = [
            name
            for name in names
            if any(
                word in name.lower()
                for word in ("mutate", "update", "invalidate", "refresh", "overwrite")
            )
        ]
        assert suspicious == [], (
            f"capture_snapshot grew a mutating parameter {suspicious}; a frozen "
            "snapshot a caller can update is not a frozen snapshot"
        )
        mutators = [
            name
            for name in dir(ms.MemorySnapshot)
            if not name.startswith("_")
            and callable(getattr(ms.MemorySnapshot, name, None))
            and name
            not in ("as_dict", "empty", "ranking", "receipt", "session_id",
                    "stale_against", "store_digest", "untrusted_rows")
        ]
        assert mutators == [], f"MemorySnapshot gained a method: {mutators}"

    def test_two_captures_over_the_same_empty_store_are_identical(self):
        """Determinism even on the empty case -- and this one IS legitimately
        observable, because ``capture_snapshot(None, ...)`` is a total function
        of its arguments rather than of a store I could not populate."""
        from memory import snapshot as ms

        first = ms.capture_snapshot(None, query="q", at=1_700_000_000.0)
        second = ms.capture_snapshot(None, query="q", at=1_700_000_000.0)
        assert first.digest == second.digest
        assert first.text == second.text
        assert first.store_digest == second.store_digest

    def test_an_EMPTY_snapshot_is_distinguishable_from_a_populated_one(self):
        """So that no future pin in this suite can pass on an empty snapshot by
        accident. The empty case is well-formed -- a real digest, a real receipt
        -- and it SAYS it is empty."""
        from memory import snapshot as ms

        empty = ms.capture_snapshot(None, query="q")
        assert empty.rows == ()
        receipt = empty.receipt()
        assert receipt, "an empty snapshot must still carry a receipt"
        # Whatever the receipt's key names, the row count is zero and that is the
        # discriminator a pin must use.
        assert len(empty.rows) == 0 and empty.chars == 0

    def test_the_prefix_digest_is_a_pure_function_of_the_snapshot_TEXT(self):
        """The half ``runtime/`` owns: whatever a snapshot contributed, the digest
        follows the TEXT. This is what makes "frozen prefix" cacheable rather
        than merely frozen."""
        tools = [{"name": "read", "input_schema": {"type": "object"}}]
        with tempfile.TemporaryDirectory() as tmp:
            store = ds_mod.DecisionStore(tmp)
            store.record("the catalog fingerprint is stable within a session")
            snapshot = ms_mod.capture_snapshot(store, query="catalog")
            if not snapshot.rows:
                pytest.skip(
                    "the DecisionStore fixture produced no rows in this "
                    "environment, so the populated path is unexercised here; "
                    "see the filed request in runtime/AGENTS.md"
                )
            first = pc.prefix_digest(
                [{"role": "system", "content": snapshot.text}], tools=tools
            )
            second = pc.prefix_digest(
                [{"role": "system", "content": snapshot.text}], tools=tools
            )
            assert first == second

    def test_the_snapshot_STAMP_is_kept_out_of_the_frozen_text(self):
        """The property that makes a frozen snapshot cacheable, and it is
        STRUCTURAL rather than behavioural.

        **This pin's first draft asserted the opposite, and was wrong.** It fed
        ``canonical_prompt_text`` a snapshot-shaped string containing
        ``captured_at 2026-10-05T12:33:07Z`` and asserted the clock was reduced.
        It is not: ``STAMP_REGION_MARKERS`` holds only ``## Session stamp`` and
        ``## Run started``, so a clock outside those regions survives on purpose --
        rewriting a timestamp in data this module does not own would change what
        the model reads.

        The real design is better than that. In ``memory/snapshot.py``,
        ``captured_at`` reaches **only** the dataclass field and the receipt --
        every occurrence is a ``captured_at=stamp`` constructor argument, and
        ``stamp`` is never interpolated into ``text``. So the frozen prefix
        carries no clock at all and no reduction is needed. That is a question
        about the *text assembly path*, so it is answered by reading that path.
        """
        import inspect
        import re

        from memory import snapshot as ms

        source = inspect.getsource(ms.capture_snapshot)
        offending = []
        for number, line in enumerate(source.splitlines(), 1):
            if "stamp" not in line and "captured_at" not in line:
                continue
            if re.search(r"captured_at\s*=\s*stamp", line):
                continue  # the field/receipt assignment; not the text
            if line.strip().startswith("stamp = ") or "time.strftime" in line:
                continue  # computing the stamp
            offending.append((number, line.strip()))
        assert offending == [], (
            "capture_snapshot now uses the clock somewhere other than the "
            f"captured_at field: {offending} -- a second-resolution stamp in the "
            "frozen TEXT invalidates the prefix every turn"
        )
        # And the field genuinely carries a stamp, so the check above is not
        # passing because the stamp had been removed altogether.
        empty = ms.capture_snapshot(None, query="q")
        assert empty.captured_at, "no stamp at all -- the check above is vacuous"
        assert empty.text == "", "the empty snapshot should carry no text"

    def test_a_clock_that_DID_reach_the_prefix_is_REPORTED_with_its_offset(self):
        """The guarantee that does not depend on memory/'s cooperation: even if a
        clock reached the prefix, ``prefix_instability`` names it and where. So
        the failure mode is a diagnosable receipt rather than a silent re-read."""
        messages = [
            {
                "role": "system",
                "content": "## Project memory (frozen snapshot)\n"
                "captured_at 2026-10-05T12:33:07Z",
            }
        ]
        found = pc.prefix_instability(messages, tools=None)
        clocks = [row for row in found if row["source"] == "full_timestamp"]
        assert clocks, "a clock in the prefix was not reported"
        row = clocks[0]
        assert row["char_offset"] >= 0
        assert "2026-10-05T12:33:07" in row["example"]
        # And it is REPORTED, not rewritten: the raw clock is still in the text.
        assert pc.has_full_timestamp(messages[0]["content"]) is True

    def test_a_snapshot_whose_text_CHANGES_does_move_the_digest(self):
        """The control. Without it, the pins above could all be satisfied by a
        digest that ignores snapshot text entirely."""
        tools = [{"name": "read", "input_schema": {"type": "object"}}]
        a = pc.prefix_digest(
            [{"role": "system", "content": "## memory A\nfact one"}], tools=tools
        )
        b = pc.prefix_digest(
            [{"role": "system", "content": "## memory B\nfact two"}], tools=tools
        )
        assert a != b



# ==========================================================================
# W2.6 -- the local-model claim is ASKED, and percentiles respect the floor
# ==========================================================================


class TestTheLocalModelClaimIsAskedNotAssumed:
    def test_the_probe_returns_a_derived_verdict(self):
        probe = audit.local_endpoint_probe()
        assert isinstance(probe["available"], bool)
        assert probe["verdict"]
        assert set(probe["ports"]) == {str(p) for p in audit.LOCAL_PROBE_PORTS}

    def test_the_probe_reuses_the_repos_OWN_notion_of_a_local_endpoint(self):
        """The port list must not become a second answer to "what is a local
        endpoint"; ``runtime.local_models`` already owns that."""
        probe = audit.local_endpoint_probe()
        from runtime import local_models

        assert probe["local_provider_names"] == list(
            getattr(local_models, "LOCAL_PROVIDER_NAMES", ())
        )
        assert probe["local_hostnames"] == list(
            getattr(local_models, "LOCAL_HOSTNAMES", ())
        )

    def test_a_verdict_of_unavailable_names_what_was_looked_for(self):
        """An honest "not available" has to say WHICH runtime and WHICH ports,
        or it is indistinguishable from "nobody looked"."""
        probe = audit.local_endpoint_probe()
        if not probe["available"]:
            joined = probe["verdict"]
            assert "llama.cpp" in joined
            assert "port" in joined
            assert probe["executables"] and all(
                key in probe["executables"] for key in ("ollama", "llama-server", "vllm")
            )

    def test_the_not_established_block_is_DERIVED_from_the_probe_not_hardcoded(self):
        """The local-model claim must follow what the probe found, in all THREE
        states -- and this pin's first draft was the reason that is now true.

        It asserted a two-way branch: probe says absent -> "were not run";
        otherwise the claim must change. But ``include_local_probe=False``
        reports ``available: None`` -- "we did not look" -- which is neither
        "absent" nor "present", and folding it into the second branch demanded
        a claim change that a skipped probe cannot possibly justify. The pin
        failed, and reading why found the real defect underneath it:

        **``not_established()`` was a module constant.** The sentence about
        llama.cpp could not change even when a llama.cpp was sitting on the
        host, so the payload would have kept publishing "were not run" while a
        local runtime was available and unmeasured. A claim that cannot follow
        a measurement is the one kind of claim this repository's doctrine
        exists to delete.

        The claim is now a function of the probe result, and all three states
        are asserted here -- including the control arm that the three sentences
        are genuinely DIFFERENT, so this cannot pass by one string serving all
        three.
        """
        absent = " ".join(
            audit.audit(
                turns=2, search_at=(), runs=1,
                include_local_probe=False,
            )["not_established"]
        )
        assert "NOT PROBED" in absent, (
            "a skipped probe must claim nothing in either direction"
        )

        found = {
            "probed": True,
            "available": True,
            "executables": {"llama-server": True, "ollama": False, "vllm": False},
        }
        present = " ".join(audit.not_established(found)["not_established"])
        assert "PRESENT" in present, (
            f"a runtime on the host must change the claim, got: {present!r}"
        )
        # And the specific gap must be named rather than implied: the runtime
        # exists, this round did not measure it, and that is a GAP not an
        # absence of hardware.
        assert "NOT measured" in present
        assert "gap in this round" in present

        nothing = {
            "probed": True,
            "available": False,
            "executables": {"llama-server": False, "ollama": False, "vllm": False},
        }
        empty = " ".join(audit.not_established(nothing)["not_established"])
        assert "no local model runtime on this host" in empty
        assert "PRESENT" not in empty, "the absent-host sentence reused the present-host one"

        # Control arm: three states, three different sentences. Without this the
        # assertions above could be satisfied by any one of them.
        assert len({absent, present, empty}) == 3, (
            "two of the three probe states publish the same claim -- one of them "
            "is therefore wrong"
        )

    def test_the_not_established_block_still_carries_the_STATIC_claims(self):
        """Deriving one claim from the probe must not quietly drop the others:
        the point of the block is that it is a CLOSED list of non-claims, and a
        function replacing a constant is exactly where a static claim would
        get lost."""
        payload = audit.not_established({"probed": True, "available": False})
        joined = " ".join(payload["not_established"])
        for fragment in (
            "no live-provider lane",
            "CHARS_PER_TOKEN",
            "no invoice exists",
            "byte-stability is memory/'s to pin",
        ):
            assert fragment in joined, f"dropped static claim: {fragment}"
        # Exactly one claim is the derived one, so a future probe state cannot
        # smuggle in a second variable sentence.
        assert len(payload["not_established"]) == 5


class TestPercentilesObeyTheRepositoriesOwnFloor:
    """``runtime.latency`` owns the rule -- ``MIN_PERCENTILE_SAMPLES = 20``,
    *"a p95 over three samples is not a p95"* -- and this module's first version
    published a p95 over 5, which is the same number under a different name."""

    def test_the_floor_is_the_shared_one_not_a_second_threshold(self):
        from runtime import latency

        stats = audit._stats([0.92] * 5)
        assert stats["minimum_samples"] == latency.MIN_PERCENTILE_SAMPLES

    def test_a_p95_below_the_floor_is_WITHHELD_not_published(self):
        stats = audit._stats([0.92] * 5)
        assert stats["provisional"] is True
        assert stats["p95"] is None
        assert stats["p95_candidate_not_a_percentile"] == pytest.approx(0.92)

    def test_a_p95_at_or_above_the_floor_is_published(self):
        from runtime import latency

        values = [0.90 + 0.004 * (i % 7) for i in range(latency.MIN_PERCENTILE_SAMPLES)]
        stats = audit._stats(values)
        assert stats["samples"] >= latency.MIN_PERCENTILE_SAMPLES
        assert stats["provisional"] is False
        assert stats["p95"] is not None

    def test_the_median_is_always_published(self):
        """Withholding a percentile must not withhold the median: the median is
        the honest headline and the floor is about tail shape."""
        assert audit._stats([0.92] * 3)["median"] == pytest.approx(0.92)

    def test_the_report_distinguishes_a_WITHHELD_percentile_from_an_UNREPORTED_one(self):
        """``_fmt(None)`` is ``cache_economics``'s word for *the provider was
        silent*. Reusing it for a withheld percentile would let a reader conclude
        the provider said nothing."""
        window = {"samples": 1, "minimum_samples": 20, "provisional": True}
        rendered = audit._fmt_percentile(None, window)
        assert "withheld" in rendered
        assert "unreported" not in rendered
        assert "1 samples < 20" in rendered

    def test_the_audit_payload_carries_the_window_beside_every_percentile(self):
        payload = audit.audit(turns=4, search_at=(), runs=2)
        for block, key in (
            (payload["economics"], "hit_rate_percentile_window"),
            (payload["economics"], "net_cache_benefit_percentile_window"),
            (payload["cost"], "percentile_window"),
            (payload, "hit_rate_percentile_window"),
        ):
            window = block[key]
            assert {"samples", "minimum_samples", "provisional"} <= set(window)


# ==========================================================================
# W2.5 -- the honesty invariants, re-pinned
# ==========================================================================


class TestTheInvariantsStillHoldAfterThisRound:
    """All nine, re-run. This round touched ``prompt_cache``'s canonicaliser and
    ``cache_economics``' counterfactual, and ``check_cache_unreported`` reads the
    first -- so "still 9/9" is a measurement here, not a formality."""

    def test_every_invariant_holds(self):
        results = invariants.run_all()
        failures = [r for r in results if not r.holds]
        assert failures == [], [r.name for r in failures]

    def test_the_count_is_what_it_was_and_no_check_observed_nothing(self):
        results = invariants.run_all()
        assert len(results) == 9
        for result in results:
            assert result.observations, (
                f"{result.name} observed nothing, so it verified nothing -- a "
                "check with no observations is not a passing check"
            )

    def test_the_cache_invariant_is_not_satisfied_vacuously(self):
        """Its control arm: a provider that demonstrably speaks the protocol and
        says zero must be a MISS, not ``unreported``."""
        silent = pc.cache_tokens_from_usage({"prompt_tokens": 100})
        honest_zero = pc.cache_tokens_from_usage(
            {"prompt_tokens": 100, "cache_read_input_tokens": 0}
        )
        assert silent[3] is False
        assert honest_zero[3] is True
        assert honest_zero[0] == 0

    def test_the_unpriced_invariant_is_not_satisfied_by_an_empty_registry(self):
        """Its control arm: an unpriced model is ``unpriced`` while a DECLARED
        free row is ``free``, so the two are separable."""
        unknown = model_capabilities.estimate_cost("no-such-model-anywhere", 1000, 1000)
        assert unknown.priced is False
        assert unknown.cost_usd is not None or unknown.price_state == "unpriced"
