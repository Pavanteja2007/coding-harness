"""Phase 3b/W1 T3 -- cache economics: canonical bytes, dialects, cost per turn.

Why an in-package module: ``pyproject.toml`` pins ``testpaths = ["tests"]``, so
a bare ``python -m pytest`` collects NONE of this. Run it explicitly:

    python -m pytest runtime/test_p3b_w1_pins.py -q -p no:randomly

What each group is FOR, and the anti-vacuity rule it leans on
--------------------------------------------------------------
* **The byte/digest pin.** The defect this round was asked to look for is a
  receipt that disagrees with an invoice, so the pin drives a provider that
  keys its cache on RAW BYTES and compares the two. A test that only compared
  digests to digests would have passed against the defect.
* **Every dialect gets a REAL-SHAPED request.** Not a mock of the table: a
  hand-built Anthropic request, a hand-built OpenAI-shaped one, a Bedrock
  Converse one, a Gemini usage payload, a DeepSeek usage payload. Asserting
  against a mock asserts the mock.
* **The honest-absent pins each carry a CONTROL arm**, because "never reports
  $0" is trivially satisfiable by never reporting anything.
* **The deferral vector builds a real 50-turn session** with a tool surface
  that grows mid-session, and measures the hit rate rather than asserting one.

Six defects this round's own work produced, each pinned below so it cannot
come back: the digest hashing a projection instead of the bytes (Probe A), the
gate demanding a marker from a plan that never asked for one, the rollup
zero-initialising its sums so an empty session rendered $0.000000, the
``unpriced`` rule firing on a DECIDED status, the volatile-region scan reading
tool text as prompt text, and the ``unknown`` invalidation cause absorbing
cases that had a real name.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import types
from pathlib import Path
from typing import Any, Dict, List

import pytest

import runtime.cache_economics as econ
import runtime.model_capabilities as model_capabilities
import runtime.model_router as model_router
from runtime import prompt_cache

# ---------------------------------------------------------------------------
# A provider that keys its cache on RAW BYTES -- what Anthropic and OpenAI do.
# ---------------------------------------------------------------------------

BIG_SYSTEM = "You are the Neo coding agent. " + ("Rule. " * 900)


class _Message:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls = None


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Message(content)
        self.finish_reason = "stop"


class _Usage:
    def __init__(self, prompt_tokens: int, completion_tokens: int, cache: Dict[str, int]):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.prompt_tokens_details = types.SimpleNamespace(
            cached_tokens=cache.get("cached", 0),
            cache_creation_input_tokens=cache.get("creation", 0),
        )
        self.cache_read_input_tokens = cache.get("cached", 0)
        self.cache_creation_input_tokens = cache.get("creation", 0)


class _Response:
    def __init__(self, usage: _Usage, content: str = "ok") -> None:
        self.usage = usage
        self.choices = [_Choice(content)]
        self._hidden_params: Dict[str, Any] = {}


class ByteKeyedProvider:
    """Caches on the exact request bytes, so a byte difference IS a miss.

    The existing fixture in ``tests/test_prompt_cache_cost.py`` keys on
    ``json.dumps(..., sort_keys=True)``, which normalises key order FOR the
    provider. That flatters us: it cannot see a divergence between our digest
    and the provider's bytes, which is the whole defect. This one can.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.store: Dict[str, int] = {}

    def install(self) -> None:
        module = types.ModuleType("litellm")

        def completion(**kwargs: Any) -> _Response:
            self.calls.append(kwargs)
            messages = kwargs.get("messages") or []
            tools = kwargs.get("tools") or []
            prefix: List[Any] = []
            tools_in_prefix: List[Any] = []
            tokens = 0
            for message in messages:
                if not isinstance(message, dict):
                    continue
                content = str(message.get("content", ""))
                tokens += max(1, len(content) // 4)
                if message.get("cache_control") or message.get("cachePoint"):
                    prefix.append(content)
                    tools_in_prefix = list(tools)
                    tokens += prompt_cache.estimate_tokens(tools)
                    break
                prefix.append(content)
            key = json.dumps(
                {"prefix": prefix, "tools": tools_in_prefix},
                separators=(",", ":"),
                default=str,
            )
            cached = self.store.get(key, 0)
            creation = 0
            if not cached:
                self.store[key] = tokens
                creation = tokens
            return _Response(
                _Usage(tokens + 64, 64, {"cached": cached, "creation": creation})
            )

        module.completion = completion  # type: ignore[attr-defined]
        sys.modules["litellm"] = module

    def uninstall(self) -> None:
        sys.modules.pop("litellm", None)


def _drive(turns: int, tools_for, provider: str = "anthropic",
           model: str = "claude-sonnet-4-20250514", volatile: str = "stable"):
    """Run ``turns`` real calls through the real router and return the ledger."""
    fake = ByteKeyedProvider()
    fake.install()
    model_router.set_call_context(
        {
            "provider": provider,
            "model": model,
            "api_key": "pin-not-a-secret",
            "task_id": "p3b-w1",
            "session_id": "sess-p3b-w1",
        }
    )
    prompt_cache.reset_cache_context()
    model_capabilities.reset_context_window_cache()
    try:
        for turn in range(turns):
            model_router.call_model(
                [
                    {"role": "system", "content": BIG_SYSTEM},
                    {
                        "role": "system",
                        "content": f"## Session guidance\n{volatile} turn {turn}",
                    },
                    {"role": "user", "content": f"turn {turn}"},
                ],
                tools=tools_for(turn),
            )
        ledger = prompt_cache.current_cache_ledger()
        return ledger.summary(), list(ledger.recent), fake
    finally:
        fake.uninstall()
        model_router.set_call_context(None)
        prompt_cache.reset_cache_context()


def _tool(name: str, desc: str) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "p"}},
                "required": ["path"],
            },
        },
    }


def _tool_reordered(name: str, desc: str) -> Dict[str, Any]:
    """LOGICALLY IDENTICAL to :func:`_tool`; every level declared differently."""
    return {
        "function": {
            "parameters": {
                "required": ["path"],
                "properties": {"path": {"description": "p", "type": "string"}},
                "type": "object",
            },
            "description": desc,
            "name": name,
        },
        "type": "function",
    }


def _messages(turn: int = 0) -> List[Dict[str, str]]:
    return [
        {"role": "system", "content": BIG_SYSTEM},
        {"role": "user", "content": f"turn {turn}"},
    ]


# ===========================================================================
# 1. SHA-256 EQUALITY ON LOGICALLY IDENTICAL PREFIXES. That is the test.
# ===========================================================================


class TestBitPerfectPrefixStability:
    """The corpus's most reusable technique, asserted on BYTES not on fields."""

    def test_two_logically_identical_catalogs_are_byte_identical(self):
        a = [_tool("read", "Read a file"), _tool("grep", "Search")]
        b = [_tool_reordered("read", "Read a file"), _tool_reordered("grep", "Search")]
        left = json.dumps(prompt_cache.canonical_tool_schemas(a), sort_keys=False)
        right = json.dumps(prompt_cache.canonical_tool_schemas(b), sort_keys=False)
        assert left == right
        # And the assertion is on the DIGEST, sha256, as the brief requires.
        assert prompt_cache.tool_schema_digest(a) == prompt_cache.tool_schema_digest(b)
        # Reversed array order too: the provider keys on the array order, so a
        # reordered catalog is a real byte change unless we sort it.
        assert prompt_cache.tool_schema_digest(
            list(reversed(a))
        ) == prompt_cache.tool_schema_digest(a)

    def test_the_digest_is_sha256_of_the_bytes_we_actually_send(self):
        """The load-bearing property: what is hashed IS what goes on the wire.

        This is the pin that the measured defect would fail. Before the fix the
        digest hashed a projection and the payload carried the caller's bytes.
        """
        tools = [_tool("read", "Read"), _tool("grep", "Search")]
        plan = prompt_cache.plan_cache(
            _messages(), tools, provider="anthropic", model="claude-sonnet-4"
        )
        _sent_messages, sent_tools = prompt_cache.apply_cache_parameters(
            _messages(), tools, plan
        )
        # Strip the marker we deliberately add, and hash what remains: that is
        # the catalog the provider keys on.
        stripped = [
            {k: v for k, v in dict(tool).items() if k != "cache_control"}
            for tool in (sent_tools or [])
        ]
        expected = hashlib.sha256(
            prompt_cache.canonical_json(stripped).encode("utf-8")
        ).hexdigest()
        assert expected == prompt_cache.tool_schema_digest(tools)

    def test_a_real_reordered_catalog_costs_exactly_one_provider_creation(self):
        """The measured defect, as a regression.

        Measured BEFORE the fix, with the same two schemas declared in two
        different orders on alternating turns: our receipt said
        ``cache_distinct_prefixes == 1`` and ``cache_invalidation_count == 0``
        while the provider performed **two** cache creations. A receipt
        claiming a never-changing prefix beside an invoice that paid for two
        writes is worse than no receipt.
        """
        stable = [_tool("read", "Read a file"), _tool("grep", "Search")]
        reordered = [
            _tool_reordered("read", "Read a file"),
            _tool_reordered("grep", "Search"),
        ]
        summary, rows, _fake = _drive(
            6, lambda turn: stable if turn % 2 == 0 else reordered
        )
        creations = [row for row in rows if row.get("cache_status") == "creation"]
        assert len(creations) == 1, (
            "a byte-keyed provider must see ONE prefix, not two: "
            f"statuses={[row.get('cache_status') for row in rows]}"
        )
        assert summary["cache_distinct_prefixes"] == 1
        assert summary["cache_invalidation_count"] == 0
        # And the two numbers agree, which is the whole point.
        assert len(creations) == summary["cache_distinct_prefixes"]

    def test_canonicalization_never_mutates_the_caller(self):
        tools = [_tool("read", "Read")]
        before = json.dumps(tools, sort_keys=True)
        messages = _messages()
        messages_before = json.dumps(messages, sort_keys=True)
        plan = prompt_cache.plan_cache(
            messages, tools, provider="anthropic", model="claude-sonnet-4"
        )
        prompt_cache.apply_cache_parameters(messages, tools, plan)
        assert json.dumps(tools, sort_keys=True) == before
        assert json.dumps(messages, sort_keys=True) == messages_before

    def test_unicode_composition_does_not_split_the_prefix(self):
        """NFC: two spellings of one accent are different BYTES and one string.

        Found while writing this: a tool description or a memory note that
        reaches the prefix through two different composition paths (a copy-paste
        on macOS, a Windows editor, a database round-trip) silently splits the
        cache, and nothing in the report would say why.
        """
        composed = "caf\u00e9"          # precomposed
        decomposed = "cafe\u0301"       # combining acute
        assert composed != decomposed
        left = prompt_cache.tool_schema_digest([_tool("read", composed)])
        right = prompt_cache.tool_schema_digest([_tool("read", decomposed)])
        assert left == right, "NFC must make one spelling of one string"

    def test_a_genuine_content_change_still_moves_the_digest(self):
        """The control arm: canonicalization must not flatten real differences."""
        assert prompt_cache.tool_schema_digest([_tool("read", "Read")]) != (
            prompt_cache.tool_schema_digest([_tool("read", "Write")])
        )
        assert prompt_cache.tool_schema_digest([_tool("read", "R")]) != (
            prompt_cache.tool_schema_digest([_tool("write", "R")])
        )

    def test_an_incidental_provider_field_now_moves_the_digest(self):
        """A field the old projection DISCARDED is a real byte change.

        ``"strict": true`` does not change what a tool means, so the previous
        projection threw it away -- and therefore reported a stable prefix while
        the provider re-read it. It is bytes now, and that is correct.
        """
        base = [_tool("read", "Read")]
        strict = json.loads(json.dumps(base))
        strict[0]["function"]["strict"] = True
        assert prompt_cache.tool_schema_digest(base) != (
            prompt_cache.tool_schema_digest(strict)
        )


# ===========================================================================
# 2. DATE-ONLY TIMESTAMPS. No clock may reach the prefix.
# ===========================================================================


class TestNoClockInThePrefix:
    def test_date_only_reduces_a_timestamp_to_its_date(self):
        assert prompt_cache.date_only("started 2026-10-04T20:51:00.123456Z ok") == (
            "started 2026-10-04 ok"
        )
        assert prompt_cache.date_only("at 2026-10-04 20:51:00+00:00") == (
            "at 2026-10-04"
        )

    def test_an_unrecognised_value_is_returned_unchanged_not_guessed(self):
        for raw in ("", "no clock here", "2026-13", "version 1.2.3"):
            assert prompt_cache.date_only(raw) == raw

    def test_has_full_timestamp_is_the_assertion_not_the_fix(self):
        assert prompt_cache.has_full_timestamp("t=2026-10-04T20:51:00Z") is True
        assert prompt_cache.has_full_timestamp("t=20:51:00.123") is True
        assert prompt_cache.has_full_timestamp("dated 2026-10-04") is False
        assert prompt_cache.has_full_timestamp("") is False

    def test_a_stamp_region_we_own_is_date_only_and_byte_stable(self):
        morning = "## Session stamp\nstarted 2026-10-04T09:00:00.000Z"
        evening = "## Session stamp\nstarted 2026-10-04T21:00:00.000Z"
        assert prompt_cache.has_full_timestamp(morning) is True, "the INPUT has one"
        left = prompt_cache.canonical_prompt_text(morning)
        right = prompt_cache.canonical_prompt_text(evening)
        assert left == right, "12 hours apart must not be a prefix change"
        assert prompt_cache.has_full_timestamp(left) is False, (
            "no full timestamp may REACH the prefix -- that is the brief's rule"
        )

    def test_the_next_day_IS_a_prefix_change_and_that_is_correct(self):
        """The control arm: date-only must not become blind.

        A digest that ignored timestamps entirely would look stable forever.
        A new day genuinely changes the prompt, so the digest must move.
        """
        day_one = "## Session stamp\n2026-10-04"
        day_two = "## Session stamp\n2026-10-05"
        assert prompt_cache.prefix_digest(
            [{"role": "system", "content": day_one}], None
        ) != prompt_cache.prefix_digest(
            [{"role": "system", "content": day_two}], None
        )

    def test_a_clock_in_user_text_is_left_alone_on_purpose(self):
        """Rewriting a user's issue text to flatter a cache number is backwards.

        It would change what the model reads. So the reduction is confined to
        regions this module generated, and a timestamp in user content is
        reported instead (next test) rather than silently rewritten.
        """
        issue = "the deploy failed at 2026-10-04T03:14:00Z with HTTP 502"
        assert prompt_cache.canonical_prompt_text(issue) == issue

    def test_a_clock_anywhere_in_the_prefix_is_reported_with_its_value(self):
        messages = [
            {
                "role": "system",
                "content": "You are Neo. Started 2026-10-04 at 03:14:07.",
            },
            {"role": "user", "content": "go"},
        ]
        found = prompt_cache.prefix_instability(messages)
        clocks = [row for row in found if row["source"] == "full_timestamp"]
        assert clocks, "a clock in the prefix must be named, not tolerated"
        # Actionable: it carries the value and where it is.
        assert clocks[0]["example"] == "03:14:07"
        assert clocks[0]["char_offset"] > 0
        assert "message_index" in clocks[0]

    def test_a_bare_clock_does_not_crash_the_scanner(self):
        """A defect this round's own pin found, on the LIVE call path.

        The first version reported ``_FULL_TIMESTAMP_RES[0].search(...).group(0)``
        unconditionally, so a system prompt carrying a bare ``HH:MM:SS`` -- a
        quoted log line, which is ordinary -- raised AttributeError inside
        ``plan_cache``, i.e. on every model call. The scanner must name whichever
        pattern matched.
        """
        for text in (
            "log line: 03:14:07 request failed",
            "iso: 2026-10-04T03:14:07Z",
            "both 2026-10-04T03:14:07Z and 03:14:07",
            "none here at all",
        ):
            messages = [
                {"role": "system", "content": f"You are Neo. {text}"},
                {"role": "user", "content": "go"},
            ]
            found = prompt_cache.prefix_instability(messages)
            clocks = [row for row in found if row["source"] == "full_timestamp"]
            assert bool(clocks) is prompt_cache.has_full_timestamp(text), text
            for row in clocks:
                assert row["example"]
                assert row["char_offset"] >= 0


# ===========================================================================
# 3. THE SEARCH-RESULT / VOLATILE-REGION EXCLUSION.
# ===========================================================================


class TestVolatileContentStaysOutOfTheCachedPrefix:
    def test_the_frozen_prefix_is_one_system_message_not_the_whole_run(self):
        """Over-including a volatile system message is the classic killer.

        It looks perfectly healthy in the receipts right up until the provider
        stops hitting, and under-including costs only a few tokens.
        """
        prefix, suffix, index = prompt_cache.split_at_breakpoint(
            [
                {"role": "system", "content": "frozen"},
                {"role": "system", "content": "per-turn"},
                {"role": "user", "content": "go"},
            ]
        )
        assert [m["content"] for m in prefix] == ["frozen"]
        assert [m["content"] for m in suffix] == ["per-turn", "go"]
        assert index == 0

    def test_a_tool_search_result_inside_the_prefix_is_named(self):
        messages = [
            {
                "role": "system",
                "content": "You are Neo.\n## Tool search results\nread, grep, edit",
            },
            {"role": "user", "content": "go"},
        ]
        found = prompt_cache.prefix_instability(messages)
        markers = [row["marker"] for row in found if row["source"] == "volatile_region"]
        assert "## Tool search results" in markers
        row = next(r for r in found if r["source"] == "volatile_region")
        assert row["char_offset"] > 0, "the offset is what makes it actionable"

    def test_every_volatile_marker_is_reachable_in_a_prefix(self):
        """A marker nothing can reach is untested vocabulary."""
        for marker in prompt_cache.VOLATILE_REGION_MARKERS:
            messages = [
                {"role": "system", "content": f"You are Neo.\n{marker}\nstuff"},
                {"role": "user", "content": "go"},
            ]
            found = prompt_cache.prefix_instability(messages)
            assert any(row.get("marker") == marker for row in found), marker

    def test_the_harness_step_marker_is_recognised_as_volatile(self):
        """It is the renderer's own header, so this is not a guess.

        ``harness.prompts.STEP_CACHE_BREAKPOINT`` is where per-turn data
        starts. If it ever appears inside a frozen prefix the next turn reads
        stale per-turn data as current.
        """
        from harness import prompts

        step = prompts.render_step_messages(
            issue_text="i",
            plan=[{"id": 1, "description": "d", "checkpoint": "c"}],
            step_id=2,
            total_steps=3,
            completed_block="(none yet)",
            context_block="(none)",
            max_output_chars=3000,
            first_user="Begin.",
        )
        # The shipped split is CORRECT: the marker is in the SECOND message.
        _prefix, suffix, index = prompt_cache.split_at_breakpoint(step)
        assert index == 0
        assert prompts.STEP_CACHE_BREAKPOINT not in _prefix[0]["content"]
        assert any(
            prompts.STEP_CACHE_BREAKPOINT in str(m.get("content", "")) for m in suffix
        )
        # And a caller who wrongly put the whole render in the frozen message is
        # told, rather than being left to discover it as a 0% hit rate.
        wrong = [{"role": "system", "content": step[0]["content"] + step[1]["content"]}]
        assert prompt_cache.prefix_instability(wrong)

    def test_the_scanner_does_not_read_tool_text_as_prompt_text(self):
        """A defect this round's own first draft had.

        ``prefix_instability`` scanned the TOOL CATALOG in one early version, so
        a tool whose description mentioned "## Retrieved context" was reported
        as an unstable PROMPT. The scanner's subject is the message prefix.
        """
        tools = [_tool("read", "reads files; see ## Retrieved context for more")]
        assert prompt_cache.prefix_instability(_messages(), tools) == []


# ===========================================================================
# 4. THE DIALECT TABLE. Data, and every row asserted on a real-shaped request.
# ===========================================================================


class TestCacheDialectTable:
    def test_the_table_is_data_not_code(self):
        rows = prompt_cache.cache_dialect_table()
        names = {row["dialect"] for row in rows}
        assert names == {
            "anthropic", "bedrock", "vertex_ai", "gemini", "deepseek",
            "openai", "local",
        }
        for row in rows:
            assert row["mechanism"] in {"breakpoints", "implicit", "keyed", "bytes"}
            assert row["note"], f"{row['dialect']} has no recorded reason"
        # Every mechanism is represented: a table of one shape is not a table.
        assert len({row["mechanism"] for row in rows}) == 4

    @pytest.mark.parametrize(
        "provider,model,api_base,expected",
        [
            ("anthropic", "claude-sonnet-4-20250514", None, "anthropic"),
            ("bedrock", "anthropic.claude-3-5-sonnet", None, "bedrock"),
            ("vertex_ai", "gemini-2.5-pro", None, "vertex_ai"),
            ("google", "gemini-2.5-pro", None, "gemini"),
            ("deepseek", "deepseek-chat", None, "deepseek"),
            ("openai", "gpt-4o-mini", None, "openai"),
            ("ollama", "qwen2.5:0.5b", None, "local"),
            ("openai", "local-model", "http://localhost:11434/v1", "local"),
            ("vllm", "some-model", "http://127.0.0.1:8000/v1", "local"),
            # An unrecognised endpoint must resolve to the SAFEST row (send
            # nothing, pass the session key), never to the Anthropic row.
            ("some-new-gateway", "some-model", "https://x.example/v1", "openai"),
        ],
    )
    def test_each_endpoint_resolves_to_exactly_one_row(
        self, provider, model, api_base, expected
    ):
        assert prompt_cache.cache_dialect_for(provider, model, api_base).dialect == expected

    def test_an_openai_shaped_url_naming_claude_still_resolves_to_openai(self):
        """The ordering that keeps a hard 400 away.

        OpenAI-compatible base URLs routinely contain the word "claude". Letting
        the Anthropic row win on ``api_base`` would put ``cache_control`` on a
        gateway that rejects it -- and the existing suite pins exactly that
        400.
        """
        dialect = prompt_cache.cache_dialect_for(
            "openai", "claude-sonnet-4", "https://gw.example/openai/v1"
        )
        assert dialect.dialect == "openai"
        assert dialect.message_parameter is None

    def test_anthropic_gets_cache_control_on_the_message_and_the_tool(self):
        """A REAL-SHAPED Anthropic request, not a mock of the table."""
        tools = [_tool("read", "Read")]
        plan = prompt_cache.plan_cache(
            _messages(), tools, provider="anthropic", model="claude-sonnet-4-20250514"
        )
        assert plan.dialect == "anthropic"
        assert plan.requested is True
        assert plan.breakpoints == 2, "one message breakpoint + one tool breakpoint"
        assert plan.breakpoints <= plan.breakpoint_limit
        messages, out_tools = prompt_cache.apply_cache_parameters(
            _messages(), tools, plan
        )
        assert messages[0]["cache_control"] == {"type": "ephemeral"}
        assert (out_tools or [])[-1]["cache_control"] == {"type": "ephemeral"}
        # The gate passes it.
        receipt = prompt_cache.enforce_cache_dialect_gate(
            messages, out_tools, plan,
            provider="anthropic", model="claude-sonnet-4-20250514",
        )
        assert receipt["cache_dialect"] == "anthropic"
        assert receipt["cache_breakpoints_placed"] <= receipt["cache_breakpoint_limit"]

    def test_openai_gets_prompt_cache_key_and_no_marker(self):
        """A REAL-SHAPED OpenAI-shaped request.

        OpenAI's own pin asserts ``cache_control`` must be absent here because
        it is a hard 400. This extends it: the OpenAI request is not merely
        marker-free, it carries the stable session key that makes the automatic
        cache land on the same host.
        """
        tools = [_tool("read", "Read")]
        plan = prompt_cache.plan_cache(
            _messages(), tools, provider="openai", model="gpt-4o-mini"
        )
        assert plan.dialect == "openai"
        assert plan.requested is False
        assert plan.skip_reason == "implicit_prefix"
        messages, out_tools = prompt_cache.apply_cache_parameters(
            _messages(), tools, plan
        )
        assert "cache_control" not in messages[0]
        for tool in out_tools or []:
            assert "cache_control" not in tool
        params = prompt_cache.cache_parameters_for(
            prompt_cache.cache_dialect_for("openai", "gpt-4o-mini"),
            plan,
            session_id="sess-1",
        )
        assert set(params) == {"prompt_cache_key"}
        assert len(params["prompt_cache_key"]) == 32
        receipt = prompt_cache.enforce_cache_dialect_gate(
            messages, out_tools, plan,
            provider="openai", model="gpt-4o-mini",
            session_id="sess-1", request_kwargs=params,
        )
        assert receipt["cache_dialect_mechanism"] == "keyed"
        assert receipt["cache_session_key_source"] == "session_and_prefix"

    def test_bedrock_gets_cachePoint_and_never_cache_control(self):
        """The Converse API variant, which is a different SPELLING not a subset.

        Bedrock has no implicit prefix caching at all, so an Anthropic spelling
        there is not merely redundant -- it fails.
        """
        tools = [_tool("read", "Read")]
        plan = prompt_cache.plan_cache(
            _messages(), tools, provider="bedrock", model="anthropic.claude-3-5-sonnet"
        )
        assert plan.dialect == "bedrock"
        assert plan.requested is True, "bedrock has no implicit prefix cache"
        messages, _out_tools = prompt_cache.apply_cache_parameters(
            _messages(), tools, plan
        )
        assert messages[0]["cachePoint"] == {"type": "default"}
        assert "cache_control" not in messages[0]

    def test_gemini_and_vertex_send_nothing_at_all(self):
        for provider, model in (
            ("google", "gemini-2.5-pro"),
            ("vertex_ai", "gemini-2.5-pro"),
        ):
            tools = [_tool("read", "Read")]
            plan = prompt_cache.plan_cache(
                _messages(), tools, provider=provider, model=model
            )
            assert plan.requested is False
            assert plan.mechanism == "implicit"
            messages, out_tools = prompt_cache.apply_cache_parameters(
                _messages(), tools, plan
            )
            assert "cache_control" not in messages[0]
            for tool in out_tools or []:
                assert "cache_control" not in tool
            assert prompt_cache.cache_parameters_for(
                prompt_cache.cache_dialect_for(provider, model), plan
            ) == {}

    def test_local_sends_nothing_because_there_is_nothing_to_send(self):
        plan = prompt_cache.plan_cache(
            _messages(), [_tool("read", "R")], provider="ollama", model="qwen2.5:0.5b"
        )
        assert plan.dialect == "local"
        assert plan.mechanism == "bytes"
        assert plan.requested is False

    @pytest.mark.parametrize(
        "dialect_name,usage,expect_reported",
        [
            (
                "deepseek",
                {"prompt_tokens": 900, "prompt_cache_hit_tokens": 0,
                 "prompt_cache_miss_tokens": 900},
                True,
            ),
            ("gemini", {"prompt_tokens": 900, "cachedContentTokenCount": 0}, True),
            ("bedrock", {"cacheReadInputTokens": 0, "inputTokens": 900}, True),
            ("openai", {"prompt_tokens": 900,
                        "prompt_tokens_details": {"cached_tokens": 0}}, True),
            ("anthropic", {"cache_read_input_tokens": 0, "input_tokens": 900}, True),
        ],
    )
    def test_each_dialect_reports_its_cache_usage_under_its_own_names(
        self, dialect_name, usage, expect_reported
    ):
        """A REAL-SHAPED usage payload per family, not a mock.

        DeepSeek, Gemini and Bedrock were all invisible to this module before
        this round: a perfectly working cache read as ``unreported`` on every
        turn, which on a cost dashboard is indistinguishable from "caching is
        broken here".
        """
        model = {
            "deepseek": ("deepseek", "deepseek-chat"),
            "gemini": ("google", "gemini-2.5-pro"),
            "bedrock": ("bedrock", "anthropic.claude-3-5-sonnet"),
            "openai": ("openai", "gpt-4o-mini"),
            "anthropic": ("anthropic", "claude-sonnet-4-20250514"),
        }[dialect_name]
        plan = prompt_cache.plan_cache(_messages(), [_tool("read", "R")], provider=model[0], model=model[1])
        assert plan.dialect == dialect_name
        _cached, _creation, _write, reported = prompt_cache.cache_tokens_from_usage(
            usage
        )
        assert reported is expect_reported, (
            f"{dialect_name} reported no recognisable cache field"
        )
        # And a zero there is a real MISS, not silence.
        receipt = prompt_cache.receipt_from_usage(usage, plan)
        assert receipt.reported is True
        assert receipt.status in (
            prompt_cache.CACHE_MISS,
            prompt_cache.CACHE_UNSUPPORTED,
        )

    def test_every_declared_cached_usage_key_is_actually_read(self):
        """The table and the reader must not drift, and drift is invisible.

        A dialect row listing a usage field the reader does not know is a row
        that will report ``unreported`` forever. Asserted per row.
        """
        known = {
            "cached_tokens", "cache_read_input_tokens", "cache_read_tokens",
            "cache_hit_tokens", "prompt_cache_hit_tokens",
            "cachedContentTokenCount", "cached_content_token_count",
            "cacheReadInputTokens",
        }
        for dialect in prompt_cache.CACHE_DIALECTS:
            for key in dialect.cached_usage_keys:
                assert key in known, f"{dialect.dialect} declares unread key {key}"
                _c, _cr, _w, reported = prompt_cache.cache_tokens_from_usage({key: 0})
                assert reported is True, f"{key} is declared but not read"


# ===========================================================================
# 5. THE GATE, mirroring enforce_stream_gate.
# ===========================================================================


class TestCacheDialectGate:
    def test_a_foreign_marker_is_refused_with_a_closed_reason(self):
        tools = [_tool("read", "R")]
        plan = prompt_cache.plan_cache(
            _messages(), tools, provider="openai", model="gpt-4o-mini"
        )
        poisoned = [{"role": "system", "content": BIG_SYSTEM,
                     "cache_control": {"type": "ephemeral"}}]
        with pytest.raises(prompt_cache.CacheDialectRefused) as excinfo:
            prompt_cache.enforce_cache_dialect_gate(
                poisoned, tools, plan, provider="openai", model="gpt-4o-mini",
                request_kwargs=prompt_cache.cache_parameters_for(
                    prompt_cache.cache_dialect_for("openai", "gpt-4o-mini"),
                    plan, session_id="s",
                ),
            )
        assert excinfo.value.reason == "foreign_marker_present"
        assert excinfo.value.reason in prompt_cache.CACHE_DIALECT_REFUSALS
        assert "cache_control" in excinfo.value.detail

    def test_the_refusal_is_not_a_permission_error(self):
        """It must not be absorbed as "try the next candidate".

        ``provider_gateway`` treats ``PermissionError`` as its fail-closed path
        and moves on, which would turn a cache-dialect mistake into one more
        reason to spend money on a second provider. Same reasoning as
        ``runtime.hardline``'s refusal type.
        """
        assert not issubclass(prompt_cache.CacheDialectRefused, PermissionError)
        assert not issubclass(prompt_cache.CacheDialectRefused, OSError)

    def test_a_requested_plan_without_its_marker_is_refused(self):
        """The narrow, correct invariant: requested IMPLIES a marker present."""
        tools = [_tool("read", "R")]
        plan = prompt_cache.plan_cache(
            _messages(), tools, provider="anthropic", model="claude-sonnet-4"
        )
        assert plan.requested is True
        with pytest.raises(prompt_cache.CacheDialectRefused) as excinfo:
            prompt_cache.enforce_cache_dialect_gate(
                _messages(), tools, plan, provider="anthropic",
                model="claude-sonnet-4",
            )
        assert excinfo.value.reason == "required_marker_missing"

    def test_a_plan_that_declined_is_not_demanded_a_marker(self):
        """A defect this round's own first gate had.

        The first version refused whenever the endpoint SUPPORTS breakpoints,
        which turned every short-prefix call (``prefix_below_floor``) and every
        disabled-cache call into a refusal. Eight unrelated router tests went
        red. The rule is narrower: only a plan that asked gets the demand.
        """
        tools = [_tool("read", "R")]
        declined = prompt_cache.plan_cache(
            [{"role": "system", "content": "hi"}], tools,
            provider="anthropic", model="claude-sonnet-4",
        )
        assert declined.requested is False
        assert declined.skip_reason == "prefix_below_floor"
        receipt = prompt_cache.enforce_cache_dialect_gate(
            [{"role": "system", "content": "hi"}], tools, declined,
            provider="anthropic", model="claude-sonnet-4",
        )
        assert receipt["cache_breakpoints_placed"] == 0

    def test_a_keyed_dialect_without_its_session_key_is_refused(self):
        """Without a stable key every turn is a cold prefix, silently."""
        plan = prompt_cache.plan_cache(
            _messages(), [_tool("read", "R")], provider="openai", model="gpt-4o-mini"
        )
        with pytest.raises(prompt_cache.CacheDialectRefused) as excinfo:
            prompt_cache.enforce_cache_dialect_gate(
                _messages(), [_tool("read", "R")], plan,
                provider="openai", model="gpt-4o-mini", request_kwargs={},
            )
        assert excinfo.value.reason == "session_key_missing"

    def test_every_refusal_slug_is_producible(self):
        """A refusal nothing can raise is untested vocabulary."""
        tools = [_tool("read", "R")]
        anthropic = prompt_cache.plan_cache(
            _messages(), tools, provider="anthropic", model="claude-sonnet-4"
        )
        openai = prompt_cache.plan_cache(
            _messages(), tools, provider="openai", model="gpt-4o-mini"
        )
        produced = set()
        with pytest.raises(prompt_cache.CacheDialectRefused) as exc:
            prompt_cache.enforce_cache_dialect_gate(
                [{"role": "system", "content": BIG_SYSTEM,
                  "cache_control": {"type": "ephemeral"}}],
                tools, openai, provider="openai", model="gpt-4o-mini",
                request_kwargs={"prompt_cache_key": "k"},
            )
        produced.add(exc.value.reason)
        with pytest.raises(prompt_cache.CacheDialectRefused) as exc:
            prompt_cache.enforce_cache_dialect_gate(
                _messages(), tools, anthropic, provider="anthropic",
                model="claude-sonnet-4",
            )
        produced.add(exc.value.reason)
        with pytest.raises(prompt_cache.CacheDialectRefused) as exc:
            prompt_cache.enforce_cache_dialect_gate(
                _messages(), tools, openai, provider="openai", model="gpt-4o-mini",
                request_kwargs={},
            )
        produced.add(exc.value.reason)
        # The cap: five markers on a four-breakpoint family.
        over = [
            {**BIG_SYSTEM_MARKED(i), "cache_control": {"type": "ephemeral"}}
            for i in range(prompt_cache.CACHE_BREAKPOINT_LIMIT + 2)
        ]
        with pytest.raises(prompt_cache.CacheDialectRefused) as exc:
            prompt_cache.enforce_cache_dialect_gate(
                over, tools, anthropic, provider="anthropic",
                model="claude-sonnet-4",
            )
        produced.add(exc.value.reason)
        assert produced == set(prompt_cache.CACHE_DIALECT_REFUSALS)

    def test_the_breakpoint_cap_is_a_real_limit_not_a_formula(self):
        assert prompt_cache.CACHE_BREAKPOINT_LIMIT == 4
        for dialect in prompt_cache.CACHE_DIALECTS:
            assert 0 < dialect.breakpoint_limit <= 64

    def test_a_deferred_tool_search_cannot_silently_exceed_the_cap(self):
        """The plausible Phase 3b failure: one breakpoint per deferred tool.

        Each extra breakpoint is a cache entry the provider creates and a
        chance to pass the cap. Exceeding it is a hard 400 that kills the turn
        over an optimisation, so the plan counts what it is about to place.
        """
        tools = [_tool(f"t{i}", f"Tool {i}") for i in range(12)]
        plan = prompt_cache.plan_cache(
            _messages(), tools, provider="anthropic", model="claude-sonnet-4"
        )
        # This implementation places exactly two (message + tool catalog), and
        # that number is asserted rather than assumed, because it is the number
        # that must stay under the cap as the tool surface grows.
        assert plan.breakpoints == 2
        assert plan.breakpoints <= plan.breakpoint_limit
        messages, out_tools = prompt_cache.apply_cache_parameters(
            _messages(), tools, plan
        )
        markers = sum(
            1 for m in messages if isinstance(m, dict) and "cache_control" in m
        ) + sum(
            1 for t in (out_tools or []) if isinstance(t, dict) and "cache_control" in t
        )
        assert markers == 2, "a 12-tool catalog must still place exactly two markers"
        receipt = prompt_cache.enforce_cache_dialect_gate(
            messages, out_tools, plan, provider="anthropic", model="claude-sonnet-4"
        )
        assert receipt["cache_breakpoints_placed"] <= receipt["cache_breakpoint_limit"]


def BIG_SYSTEM_MARKED(index: int) -> Dict[str, Any]:
    return {"role": "system", "content": f"{BIG_SYSTEM}#{index}"}


# ===========================================================================
# 6. THE STABLE SESSION CACHE KEY.
# ===========================================================================


class TestStableSessionCacheKey:
    def _plan(self):
        return prompt_cache.plan_cache(
            _messages(), [_tool("read", "R")], provider="openai", model="gpt-4o-mini"
        )

    def test_the_key_is_stable_across_turns_of_one_session(self):
        """The property that makes an OpenAI cache hit at all."""
        plan = self._plan()
        keys = {
            prompt_cache.cache_session_key(plan, "sess-abc")["cache_session_key"]
            for turn in range(10)
        }
        assert len(keys) == 1

    def test_it_differs_between_sessions(self):
        """Two runs must never share a routing key."""
        plan = self._plan()
        one = prompt_cache.cache_session_key(plan, "sess-1")["cache_session_key"]
        two = prompt_cache.cache_session_key(plan, "sess-2")["cache_session_key"]
        assert one != two

    def test_it_changes_when_the_prefix_changes(self):
        """Otherwise a rewritten prefix keeps asking for an entry that cannot exist.

        Note what varies here is the SYSTEM message. Varying only the user turn
        -- the first draft of this test -- leaves the prefix identical by
        construction, so the key correctly does NOT move, and the test was
        asserting a change that the cache contract says should not happen.
        """
        before = prompt_cache.plan_cache(
            [{"role": "system", "content": BIG_SYSTEM}],
            [_tool("read", "R")], provider="openai", model="gpt-4o-mini",
        )
        after = prompt_cache.plan_cache(
            [{"role": "system", "content": BIG_SYSTEM + " now with a new rule."}],
            [_tool("read", "R")], provider="openai", model="gpt-4o-mini",
        )
        assert before.prefix_sha256 != after.prefix_sha256, "the fixture must differ"
        assert prompt_cache.cache_session_key(
            before, "s"
        )["cache_session_key"] != prompt_cache.cache_session_key(
            after, "s"
        )["cache_session_key"]

    def test_a_volatile_turn_does_not_change_the_key(self):
        """The control arm for the test above, and the property that matters.

        A per-turn change OUTSIDE the prefix must leave the routing key alone,
        or every turn would ask for a different cache host and the automatic
        cache would never hit at all.
        """
        keys = set()
        for turn in range(20):
            plan = prompt_cache.plan_cache(
                [
                    {"role": "system", "content": BIG_SYSTEM},
                    {"role": "user", "content": f"turn {turn}"},
                ],
                [_tool("read", "R")], provider="openai", model="gpt-4o-mini",
            )
            keys.add(
                prompt_cache.cache_session_key(plan, "s")["cache_session_key"]
            )
        assert len(keys) == 1, "20 volatile turns must share ONE session key"

    def test_no_session_id_degrades_to_prefix_only_and_says_so(self):
        receipt = prompt_cache.cache_session_key(self._plan(), "")
        assert receipt["cache_session_key_source"] == "prefix_only"
        assert len(receipt["cache_session_key"]) == 32

    def test_the_router_actually_passes_it(self):
        """Not "the helper works" -- that the real call path sends it.

        CONTROL ARM: the captured kwargs are inspected, so a router that stopped
        forwarding the key cannot pass this by still building one correctly.
        """
        fake = ByteKeyedProvider()
        fake.install()
        model_router.set_call_context(
            {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "api_key": "pin-not-a-secret",
                "session_id": "sess-router",
            }
        )
        prompt_cache.reset_cache_context()
        try:
            model_router.call_model(_messages(), tools=[_tool("read", "R")])
        finally:
            fake.uninstall()
            model_router.set_call_context(None)
            prompt_cache.reset_cache_context()
        assert len(fake.calls) == 1, "the control arm: a call really was made"
        sent = fake.calls[0]
        assert sent.get("prompt_cache_key"), "no session key reached the wire"
        assert "cache_control" not in json.dumps(sent.get("messages"))


# ===========================================================================
# 7. THE SIX COST METRICS, AND THE FOUR HONEST-ABSENT RULES.
# ===========================================================================


def _ledger_row(**overrides: Any) -> Dict[str, Any]:
    """A realistic router ledger row. Anything not named is a working call."""
    row: Dict[str, Any] = {
        "ts": "2026-10-04T20:51:00Z",
        "call_id": "c1",
        "attempt": 1,
        "outcome": "ok",
        "model": "gpt-4o-mini",
        "provider": "openai",
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "tokens": 1100,
        "cost_usd": 0.000_250,
        "cost_source": "estimate_cost",
        "price_state": model_capabilities.PRICE_PRICED,
        "cost_priced": True,
        "cache_status": prompt_cache.CACHE_HIT,
        "cached_input_tokens": 900,
        "cache_creation_input_tokens": 0,
        "cache_reported": True,
        "cache_dialect": "openai",
    }
    row.update(overrides)
    return row


class TestTheSixCostMetrics:
    def test_the_metric_set_is_closed_and_six_wide(self):
        assert len(econ.CACHE_METRICS) == 6
        assert set(econ.CACHE_METRICS) == {
            "prompt_tokens_uncached",
            "prompt_tokens_cached",
            "cache_hit_rate",
            "cache_status",
            "cost_usd",
            "cost_per_turn",
        }
        rollup = econ.session_cost([]).to_dict()
        for metric in econ.CACHE_METRICS:
            assert metric in rollup, metric

    def test_a_healthy_session_reports_all_six(self):
        rows = [
            _ledger_row(
                cache_status=prompt_cache.CACHE_CREATION,
                cached_input_tokens=0,
                cache_creation_input_tokens=1000,
                cache_reported=True,
            )
        ] + [_ledger_row() for _ in range(9)]
        rollup = econ.session_cost(econ.turn_cost_rows(rows))
        assert rollup.calls == 10
        assert rollup.cache_hit_rate == 0.9
        assert rollup.decided_calls == 10
        assert rollup.status_counts == {prompt_cache.CACHE_HIT: 9,
                                        prompt_cache.CACHE_CREATION: 1}
        assert rollup.dominant_status() == prompt_cache.CACHE_HIT
        assert rollup.prompt_tokens_cached == 9 * 900
        assert rollup.prompt_tokens_uncached == 9 * 100 + 1000
        assert rollup.cost_usd is not None
        assert rollup.cost_per_turn == round(rollup.cost_usd / 10, 8)

    def test_the_hit_rate_always_carries_its_window(self):
        """A rate without its denominator is a rate nobody can act on.

        100% of 4 calls is not 100% of 400.
        """
        rollup = econ.session_cost(econ.turn_cost_rows([_ledger_row()])).to_dict()
        assert rollup["cache_hit_rate_window"] == rollup["cache_decided_calls"]
        assert rollup["cache_hit_rate_window"] == 1
        assert econ.HIT_RATE_WINDOW_NOTE in rollup["cache_hit_rate_note"]
        assert "window" in econ.render_report(rollup)


class TestTheFourHonestAbsentRules:
    def test_rule_1_unreported_is_never_a_zero_hit_rate(self):
        assert econ.hit_rate_of(0, 0) is None
        assert econ.hit_rate_of(0, 5) == 0.0
        rows = [
            _ledger_row(
                cache_status=prompt_cache.CACHE_UNREPORTED,
                cached_input_tokens=None,
                cache_reported=False,
                prompt_tokens=None,
                cost_usd=None,
                cost_priced=False,
            )
            for _ in range(4)
        ]
        rollup = econ.session_cost(econ.turn_cost_rows(rows))
        assert rollup.cache_hit_rate is None, "an all-unreported session is UNKNOWN"
        assert rollup.decided_calls == 0
        assert rollup.unreported_calls == 4
        rendered = econ.render_report(rollup.to_dict())
        assert "NOT 0%" in rendered

    def test_rule_1_the_denominator_excludes_unreported_in_BOTH_directions(self):
        """Counting them either way would flatter one side of the ledger.

        As a hit, the rate overstates efficiency; as a miss, the saving looks
        bigger than it is. Both are the error the brief calls out.
        """
        rows = [_ledger_row() for _ in range(9)] + [
            _ledger_row(
                cache_status=prompt_cache.CACHE_UNREPORTED,
                cached_input_tokens=None,
                cache_reported=False,
                prompt_tokens=None,
            )
        ]
        rollup = econ.session_cost(econ.turn_cost_rows(rows))
        assert rollup.decided_calls == 9, "the unreported row left the denominator"
        assert rollup.cache_hit_rate == 1.0, "9/9 over DECIDED calls only"
        assert rollup.calls == 10, "and the call count still counts it"

    def test_rule_1_the_control_arm_a_real_zero_hit_rate_is_still_zero(self):
        """Otherwise "never 0%" is satisfied by never reporting anything."""
        rollup = econ.session_cost(
            econ.turn_cost_rows(
                [
                    _ledger_row(
                        cache_status=prompt_cache.CACHE_MISS,
                        cached_input_tokens=0,
                        cache_reported=True,
                    )
                    for _ in range(4)
                ]
            )
        )
        assert rollup.cache_hit_rate == 0.0
        assert rollup.decided_calls == 4

    def test_rule_2_no_sum_without_the_call_count(self):
        rollup = econ.session_cost(econ.turn_cost_rows([_ledger_row()] * 7)).to_dict()
        assert rollup["calls"] == 7
        assert rollup["turns"] == 7
        assert "unreceipted_calls" in rollup
        # A missing ledger row is REPORTED, never summed as zero.
        rows = [_ledger_row() for _ in range(3)] + [
            _ledger_row(cost_usd=None, cost_priced=False)
        ]
        rollup = econ.session_cost(econ.turn_cost_rows(rows))
        assert rollup.calls == 4
        assert rollup.unreceipted_calls == 1
        assert "never summed as zero" in econ.render_report(rollup.to_dict())

    def test_rule_2_an_empty_session_reports_no_total_rather_than_zero(self):
        """A defect this round's own first rollup had.

        It zero-initialised the sums, so a session that made NO calls rendered
        ``cost: $0.000000`` and ``prompt tokens: 0``. A reader scanning a cost
        dashboard reads both as measurements. The sums start as ``None``.
        """
        rollup = econ.session_cost([]).to_dict()
        assert rollup["calls"] == 0
        assert rollup["cost_usd"] is None
        assert rollup["cost_per_turn"] is None
        assert rollup["prompt_tokens_cached"] is None
        assert rollup["prompt_tokens_uncached"] is None
        rendered = econ.render_report(rollup)
        assert "NOT $0" in rendered
        assert "NOT a measurement of zero tokens" in rendered

    def test_rule_3_unpriced_is_never_zero_dollars(self):
        unpriced = _ledger_row(
            model="no-such-model-anywhere",
            price_state=model_capabilities.PRICE_UNPRICED,
            cost_priced=False,
            cost_usd=None,
        )
        rollup = econ.session_cost(econ.turn_cost_rows([unpriced] * 3))
        assert rollup.cost_usd is None
        assert rollup.cost_per_turn is None
        assert rollup.price_state == model_capabilities.PRICE_UNPRICED
        assert rollup.unpriced_calls == 3
        rendered = econ.render_report(rollup.to_dict())
        assert "NOT $0" in rendered
        assert "unpriced" in rendered

    def test_rule_3_one_unpriced_call_makes_the_SESSION_total_unknown(self):
        """A partial sum of the priced calls is not the cost of the session.

        Reporting it would under-report, which is the direction that spends
        money: a session whose cost reads low gets run again.
        """
        rows = [_ledger_row() for _ in range(3)] + [
            _ledger_row(
                model="no-such-model-anywhere",
                price_state=model_capabilities.PRICE_UNPRICED,
                cost_priced=False,
                cost_usd=None,
            )
        ]
        rollup = econ.session_cost(econ.turn_cost_rows(rows))
        assert rollup.cost_usd is None
        assert rollup.calls == 4
        assert rollup.unpriced_calls == 1

    def test_rule_3_the_control_arm_a_priced_session_is_not_refused(self):
        rollup = econ.session_cost(econ.turn_cost_rows([_ledger_row()] * 2))
        assert rollup.cost_usd == 0.0005
        assert rollup.price_state == model_capabilities.PRICE_PRICED

    def test_rule_4_spent_is_a_maximum_never_a_sum(self):
        from runtime import budget_governor

        # Two views of the SAME money: the governor's charges and a
        # `spend_source` that already includes them.
        governor = budget_governor.BudgetGovernor(
            cap_usd=1.0, spend_source=lambda: 3.00
        )
        for _ in range(4):
            governor.commit(0.50, model="m")
        assert governor.spent_usd() == 3.00, "a sum would read 5.00 and fire late"

        # And with only its own charges it still SUMS them -- "maximum" is a
        # statement about two views of one money, not about refusing to count.
        alone = budget_governor.BudgetGovernor(cap_usd=1.0)
        for _ in range(4):
            alone.commit(0.50, model="m")
        assert alone.spent_usd() == 2.00

        # A raising spend_source is ignored, never allowed to disable the cap.
        def raising() -> float:
            raise RuntimeError("no bound available")

        guarded = budget_governor.BudgetGovernor(cap_usd=1.0, spend_source=raising)
        guarded.commit(0.50, model="m")
        assert guarded.spent_usd() == 0.50

    def test_all_four_rules_hold_when_asked(self):
        report = econ.honest_absent_report()
        assert report["all_hold"] is True, report["holds"]
        assert set(report["holds"]) == {
            rule["rule"] for rule in report["rules"]
        }
        for name, holds in report["holds"].items():
            assert holds is True, name
        assert "unpriced" in report["cost_sources"]


class TestCostPerTurn:
    def test_cost_per_turn_is_none_for_every_absent_input(self):
        assert econ.cost_per_turn(None, 5) is None
        assert econ.cost_per_turn(1.0, None) is None
        assert econ.cost_per_turn(1.0, 0) is None, "a zero denominator is not $inf"
        assert econ.cost_per_turn(1.0, -3) is None
        assert econ.cost_per_turn(1.0, 4) == 0.25

    def test_the_net_number_and_its_counterfactual(self):
        result = econ.net_cache_benefit(
            "gpt-4o-mini",
            prompt_tokens=10_000,
            completion_tokens=1_000,
            cached_tokens=9_000,
            observed_cost_usd=0.000_400,
        )
        assert result["priced"] is True
        assert result["served_cost_usd"] == 0.0004
        assert result["no_cache_cost_usd"] > result["served_cost_usd"]
        assert result["net_cache_benefit_usd"] > 0
        assert "saved" in result["verdict"]

    def test_a_collapsed_cache_shows_up_as_a_NEGATIVE_net(self):
        """The whole point of the counterfactual.

        Phase 3b's claim is "the prompt got smaller". If the cache collapsed,
        the smaller prompt is multiplied by a full re-read every turn and the
        bill goes UP. A report with only the served cost cannot tell those two
        worlds apart.
        """
        result = econ.net_cache_benefit(
            "gpt-4o-mini",
            prompt_tokens=10_000,
            completion_tokens=0,
            cached_tokens=0,
            observed_cost_usd=0.010,
        )
        assert result["net_cache_benefit_usd"] < 0
        assert "COST" in result["verdict"]
        assert "re-reads" in result["verdict"]

    def test_an_unpriced_model_yields_an_unknown_net_not_a_zero_one(self):
        result = econ.net_cache_benefit(
            "no-such-model-anywhere", 1000, 100, 900, 0.001
        )
        assert result["priced"] is False
        assert result["net_cache_benefit_usd"] is None
        assert "UNKNOWN" in result["verdict"]
        assert "not zero" in result["verdict"]

    def test_the_counterfactual_is_labelled_as_arithmetic_not_a_measurement(self):
        """So nobody quotes it as "what a different prompt would have cost"."""
        result = econ.counterfactual_no_cache("gpt-4o-mini", 1000, 100, 900)
        assert "arithmetic on the observed token counts" in result["note"]

    def test_from_ledger_reads_a_real_jsonl_and_reports_a_torn_last_line(self):
        rows = [_ledger_row(call_id=f"c{i}") for i in range(5)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model_ledger.jsonl"
            path.write_text(
                "\n".join(json.dumps(row) for row in rows) + '\n{"partial": tr',
                encoding="utf-8",
            )
            rollup = econ.from_ledger(path)
        assert rollup["calls"] == 5
        assert rollup["ledger_rows"] == 5
        assert rollup["malformed_lines"] == 1, "a torn line is counted, not eaten"
        assert rollup["cache_hit_rate"] == 1.0
        assert rollup["cache_hit_rate_window"] == 5
        assert "net_cache_benefit" in rollup


# ===========================================================================
# 8. THE 50-TURN DEFERRAL VECTOR, AND THE INVALIDATION CAUSES.
# ===========================================================================


class TestFiftyTurnSessionWithDeferralActive:
    TURNS = 50

    def _grow(self):
        """A tool surface that GROWS mid-session: T1's deferred loading.

        The catalog widens once, at turn 10, the way a tool-search result would
        widen it. One widening, not one per turn -- a deferral that reshuffled
        the catalog every turn would be a different (and much worse) finding.
        """
        base = [_tool("read", "Read"), _tool("grep", "Search"), _tool("edit", "Edit")]
        extra = [_tool("web", "Fetch"), _tool("task", "Spawn")]
        return lambda turn: [*base, *extra] if turn >= 10 else base

    def test_the_hit_rate_holds_across_fifty_turns_with_deferral_active(self):
        summary, _rows, _fake = _drive(self.TURNS, self._grow())
        decided = summary["cache_decided_calls"]
        assert decided == self.TURNS, "every call must be DECIDED, none unreported"
        assert summary["cache_hit_rate"] > 0.90, (
            f"hit rate {summary['cache_hit_rate']} is not above the 0.90 gate"
        )
        # And the rate must be measured over a real window, not a sample of 3.
        assert decided == self.TURNS

    def test_the_tool_widening_costs_exactly_one_invalidation_and_names_itself(self):
        summary, rows, _fake = _drive(self.TURNS, self._grow())
        assert summary["cache_distinct_prefixes"] == 2, (
            "one prefix before the widening and one after"
        )
        assert summary["cache_invalidation_count"] == 1
        # Rule from W1.5: a per-turn surface change must not silently disable
        # caching -- the receipt NAMES it.
        assert summary["cache_invalidation_causes"] == {"tool_surface_changed": 1}
        # And the cause is on the row that changed, not only in the summary.
        changed = [row for row in rows if row.get("invalidated")]
        assert len(changed) == 1
        assert changed[0]["invalidation_cause"] == "tool_surface_changed"

    def test_the_digest_and_the_provider_agree_across_the_whole_session(self):
        """50 turns, byte-keyed provider: creations must equal distinct prefixes."""
        summary, rows, _fake = _drive(self.TURNS, self._grow())
        creations = sum(
            1 for row in rows if row.get("cache_status") == "creation"
        )
        assert creations == summary["cache_distinct_prefixes"], (
            f"our receipt says {summary['cache_distinct_prefixes']} prefix(es) "
            f"and the provider made {creations} cache write(s) -- these must "
            f"be the same number or one of them is lying"
        )

    def test_a_tool_surface_that_changes_EVERY_turn_is_reported_not_hidden(self):
        """The pathological case, and the one the brief says must not hide.

        If deferral reshuffled the catalog every turn, caching genuinely cannot
        work -- and the honest answer is a named cause on every turn, not a
        quietly collapsed hit rate that looks like a provider problem.
        """
        summary, _rows, _fake = _drive(
            12, lambda turn: [_tool(f"t{turn}", f"Tool {turn}")]
        )
        assert summary["cache_distinct_prefixes"] == 12
        assert summary["cache_invalidation_count"] == 11
        assert summary["cache_invalidation_causes"] == {"tool_surface_changed": 11}
        assert summary["cache_hit_rate"] < 0.10, (
            "a per-turn catalog must visibly collapse the hit rate rather than "
            "hide behind a healthy-looking number"
        )

    def test_every_invalidation_cause_is_producible_and_in_the_closed_set(self):
        """A cause nothing can produce is untested vocabulary."""
        produced = set()

        # 1. tool_surface_changed
        summary, _rows, _fake = _drive(4, lambda turn: [_tool(f"t{turn}", "R")])
        produced.update(summary["cache_invalidation_causes"])

        # 2. clock_in_prefix -- a system prompt whose clock moves each turn.
        fake = ByteKeyedProvider()
        fake.install()
        model_router.set_call_context(
            {"provider": "anthropic", "model": "claude-sonnet-4-20250514",
             "api_key": "pin-not-a-secret"}
        )
        prompt_cache.reset_cache_context()
        model_capabilities.reset_context_window_cache()
        try:
            for turn in range(4):
                model_router.call_model(
                    [
                        {"role": "system",
                         "content": f"{BIG_SYSTEM}\nlog at 03:14:{turn:02d}"},
                        {"role": "user", "content": "go"},
                    ]
                )
            # Read the summary INSIDE the context: `reset_cache_context` in the
            # `finally` installs a FRESH ledger, so reading it afterwards would
            # inspect an empty one. The first draft of this test did exactly
            # that and asserted against nothing.
            clock_causes = dict(
                prompt_cache.current_cache_ledger().summary()[
                    "cache_invalidation_causes"
                ]
            )
        finally:
            fake.uninstall()
            model_router.set_call_context(None)
            prompt_cache.reset_cache_context()
        assert clock_causes.get("clock_in_prefix") == 3, clock_causes
        produced.update(clock_causes)
        assert produced <= set(prompt_cache.INVALIDATION_CAUSES)

    def test_the_prefix_digest_covers_the_STABLE_region_only(self):
        """The property W1.1 asks for: hash the stable prefix, nothing else.

        A digest over the whole prompt would change on every turn for reasons
        that have nothing to do with the cache boundary, and the receipt would
        show a per-turn invalidation nobody caused.
        """
        stable = {"role": "system", "content": BIG_SYSTEM}
        turns = []
        for turn in range(25):
            turns.append(
                prompt_cache.prefix_digest(
                    [
                        stable,
                        {"role": "system", "content": f"## Session guidance\n{turn}"},
                        {"role": "user", "content": f"memory note {turn}"},
                        {"role": "assistant", "content": f"answer {turn}"},
                    ],
                    [_tool("read", "Read")],
                )
            )
        assert len(set(turns)) == 1, (
            "25 volatile turns must produce ONE stable-prefix digest"
        )
        # The CONTROL arm, and it has to vary a message that is actually IN the
        # prefix. Varying only the trailing user turn -- the first draft -- left
        # the prefix byte-identical by construction, so the assertion was
        # backwards: it demanded a change the cache contract forbids.
        moved = prompt_cache.prefix_digest(
            [{"role": "system", "content": BIG_SYSTEM + " one new stable rule."}],
            [_tool("read", "Read")],
        )
        assert moved != turns[0]

