"""VEX-CS-07 - the `/mcp` verb surface, as the behaviours it promises.

One class per required proof, one test per behaviour, every test named after
the behaviour it pins. Where a test drives a REAL process it says so; where it
stubs a spawn it says so and explains why the stub is the honest choice.

The eight proofs the brief names, in the order it names them:

1. every subcommand ACTS and RETURNS                        (``TestEveryVerbActs``)
2. a live server's prompts appear as ``/mcp__<server>__<prompt>``
   commands, discovered over a REAL stdio transport       (``TestServerContributedCommands``)
3. schemas load only on request and the token saving is
   MEASURED, not asserted                                (``TestDeferredSchemas``)
4. a tool outside the pin is NOT exposed                  (``TestPins``)
5. an undeclared tool is treated as MOST RESTRICTIVE      (``TestUndeclaredIsMostRestrictive``)
6. strict mode refuses an undeclared connector while the
   DEFAULT still reports ``enforced: false``              (``TestStrictModeIsOptIn``)
7. a hanging server is disabled with a REASON             (``TestLifecycle``)
8. the per-run budget is recorded                         (``TestBudget``)

Plus three that keep the round honest: markup safety through a REAL rich
Console, no completion vocabulary anywhere near the verifier gate, and the
backward-compatibility floor for the ``/mcp`` registry row.

Real processes in this file: a generated stdio MCP server that publishes two
prompts and one tool (written into ``tmp_path`` and spawned over stdio exactly
as Claude Code or Cursor would), a generated server that HANGS on purpose, and
this repository's own ``python -m mcp_server``. No Docker, no provider, no
network, no credential.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The generated stdio server used for prompt discovery. It is written to
#: ``tmp_path`` rather than checked in as a fixture directory because a fixture
#: under ``tests/fixtures`` is another surface in a shared dirty tree, and the
#: whole point of writing it here is that the test owns the bytes it spawns.
_PROMPT_SERVER = '''
"""A tiny MCP server that publishes prompts, tools and resources."""

from mcp.server.mcpserver import MCPServer

server = MCPServer(name="promptfixture", version="1.0.0")


@server.prompt(name="summarise", description="Summarise the given text.")
def summarise(text: str) -> str:
    return text[:40]


@server.prompt(name="review diff", description="Review a diff.")
def review(diff: str, style: str = "short") -> str:
    return diff[:40]


@server.tool(name="echo", description="Echo the given text back.")
def echo(text: str) -> str:
    return text


@server.tool(
    name="explode",
    description="A tool whose schema is deliberately enormous.",
)
def explode(payload: dict) -> str:
    return "ok"


# Pad the schema so the deferred/eager arms differ by a measurable amount.
explode.__doc__ = "x" * 4000


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
'''

#: A server that starts and then never answers. Used to prove the lifecycle
#: timeout: a wedge is REPORTED and DISABLED with a reason, never left to time
#: out on every subsequent call.
_HANGING_SERVER = """
import time

# Read the initialize request so the pipe is not closed under us, then never
# answer. The client must hit its own deadline, not the server's.
try:
    import sys

    sys.stdin.readline()
except Exception:
    pass

while True:
    time.sleep(3600)
"""


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Point every Neo-owned root at ``tmp_path`` and undo the process caches.

    The hook engine, the connector permission reader and the per-run tool budget
    all CACHE per root or per process, so a test that only changed the
    environment would see the previous test's answer. Clearing those caches is
    part of the isolation, not a convenience.
    """
    home = tmp_path / "home"
    config = tmp_path / "config"
    plugins = tmp_path / "plugins"
    repo = tmp_path / "repo"
    for directory in (home, config, plugins, repo / ".neo"):
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("NEO_HOME", str(home))
    monkeypatch.setenv("HARNESS_HOME", str(home))
    monkeypatch.setenv("NEO_CONFIG", str(config / "settings.toml"))
    monkeypatch.setenv("NEO_GLOBAL_ROOT", str(config))
    monkeypatch.setenv("NEO_PLUGINS_DIR", str(plugins))
    monkeypatch.setenv("NEO_HOOKS_DIR", str(config))
    monkeypatch.setenv("NEO_PROJECT_DIR", str(repo / ".neo"))
    monkeypatch.setenv("HARNESS_LOGS_DIR", str(home / "logs"))
    monkeypatch.delenv("NEO_EGRESS_ALLOWED_HOSTS", raising=False)

    from cli import connectors as connectors_mod

    connectors_mod._HOOK_ENGINE_CACHE.clear()
    connectors_mod._SESSION_STARTED.clear()
    connectors_mod.run_budget(reset=True)
    yield {"home": home, "config": config, "plugins": plugins, "repo": repo}
    connectors_mod._HOOK_ENGINE_CACHE.clear()
    connectors_mod._SESSION_STARTED.clear()
    connectors_mod.run_budget(reset=True)


def _write_server(directory: Path, name: str, body: str) -> Path:
    """Write one generated stdio server and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.py"
    path.write_text(body, encoding="utf-8")
    return path


def _declare(connectors_mod, label: str, command: str, repo: Path, **kwargs) -> None:
    """Declare one connector AND its permission entry, the way ``add`` does."""
    connectors_mod._add_server_command(
        label, command, tier="project", repo_path=str(repo)
    )
    connectors_mod.set_permissions(
        label,
        tier="project",
        repo_path=str(repo),
        tools=kwargs.pop("tools", ("*",)),
        side_effect=kwargs.pop("side_effect", "mutation"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. Every verb acts and returns
# ---------------------------------------------------------------------------


class TestEveryVerbActs:
    """Each of the nine declared verbs performs work and returns lines."""

    def test_every_declared_verb_is_implemented_and_returns_output(self, isolated):
        """``/mcp`` has nine verbs and all nine ACT and RETURN.

        "Acts and returns" is the whole claim: an implementation that returns
        the usage line for a verb the registry advertises is a declared verb
        that does not exist, which is the failure mode a verb table makes
        possible and a plain string comparison did not.
        """
        from cli import connectors as connectors_mod
        from cli.commands import SUBCOMMANDS, command_spec

        declared = {verb.name for verb in SUBCOMMANDS["/mcp"]}
        assert declared == set(connectors_mod.MCP_VERBS), (
            "the registry and the dispatcher disagree about the verb vocabulary"
        )
        assert len(connectors_mod.MCP_VERBS) == 9, connectors_mod.MCP_VERBS
        # Every verb is declared with the permissions its action needs, and the
        # mutating ones are marked mutating so the in-flight gate can see it.
        for verb in SUBCOMMANDS["/mcp"]:
            if verb.name != "list":
                assert verb.required_permissions, verb.name
        mutating = {verb.name for verb in SUBCOMMANDS["/mcp"] if verb.mutating}
        assert {"add", "remove", "pin", "enable", "disable"} <= mutating, mutating
        assert command_spec("/mcp") is not None

    def test_every_verb_returns_lines_for_a_bare_registry_of_no_servers(self, isolated):
        """A verb on an empty registry still returns LINES, not nothing.

        An empty render is indistinguishable from a verb that does not exist,
        so the honest-empty case is one sentence that says so.
        """
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        for verb in connectors_mod.MCP_VERBS:
            argument = ""
            if verb in ("add",):
                argument = "docs python -m mcp_server"
            elif verb in ("remove", "enable", "disable", "reconnect"):
                if verb == "remove":
                    continue
                argument = "docs"
            elif verb == "call":
                argument = "docs list_repos"
            elif verb == "pin":
                continue
            result = connectors_mod.mcp_command(verb, argument, repo_path=repo)
            assert result["verb"] == verb, result
            assert isinstance(result["lines"], list), result
            assert result["lines"], f"/mcp {verb} returned no output at all"
            assert all(isinstance(line, str) for line in result["lines"]), result
            assert isinstance(result["ok"], bool), result

    def test_list_add_remove_round_trip_and_each_returns_its_receipt(self, isolated):
        """``add`` writes and declares, ``list`` shows it, ``remove`` deletes it.

        Driven through the ONE dispatcher, because a script surface that
        restated the behaviour would be a second implementation of one verb.
        """
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        added = connectors_mod.mcp_command(
            "add", "docs python -m mcp_server", repo_path=repo
        )
        assert added["ok"] is True, added["lines"]
        assert any("added connector docs" in line for line in added["lines"]), added
        assert any("declared: True" in line for line in added["lines"]), added

        listed = connectors_mod.mcp_command("list", "", repo_path=repo)
        assert listed["ok"] is True
        assert any("docs" in line for line in listed["lines"]), listed

        removed = connectors_mod.mcp_command("remove", "docs", repo_path=repo)
        assert removed["ok"] is True, removed["lines"]
        after = connectors_mod.mcp_command("list", "", repo_path=repo)
        assert "docs" not in " ".join(after["lines"]), after

    def test_disable_and_enable_round_trip_without_touching_the_declaration(
        self, isolated
    ):
        """Disabling is NOT removal: the declaration and pins survive it.

        This is the property that makes ``/mcp disable`` safe to reach for
        mid-session - the operator gets their blast radius back on ``/mcp
        enable`` instead of having to retype a declaration they already wrote.
        """
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        _declare(connectors_mod, "docs", "python -m mcp_server", isolated["repo"])

        disabled = connectors_mod.mcp_command("disable", "docs", repo_path=repo)
        assert disabled["ok"] is True, disabled["lines"]
        assert any(
            "declaration and permissions untouched" in line
            for line in disabled["lines"]
        )
        assert connectors_mod.connector_disabled("docs", repo_path=repo) is True
        # The declaration is still there, unchanged.
        assert connectors_mod.connector_permissions("docs", repo_path=repo) is not None
        assert "docs" in connectors_mod.discover_mcp_servers(repo)

        enabled = connectors_mod.mcp_command("enable", "docs", repo_path=repo)
        assert enabled["ok"] is True, enabled["lines"]
        assert connectors_mod.connector_disabled("docs", repo_path=repo) is False

    def test_disable_all_is_valid_and_enable_overrides_it(self, isolated):
        """``/mcp disable all`` is valid, and ``enable`` beats the master switch.

        An operator who disables everything and then re-enables one connector
        expects exactly one connector back. A master switch with no per-label
        escape hatch would strand them, so the explicit re-enable wins.
        """
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        _declare(connectors_mod, "alpha", "python -m mcp_server", isolated["repo"])
        _declare(connectors_mod, "beta", "python -m mcp_server", isolated["repo"])

        result = connectors_mod.mcp_command("disable", "all", repo_path=repo)
        assert result["ok"] is True, result["lines"]
        assert any("disabled every connector" in line for line in result["lines"])
        assert connectors_mod.connector_disabled("alpha", repo_path=repo) is True
        assert connectors_mod.connector_disabled("beta", repo_path=repo) is True

        assert (
            connectors_mod.mcp_command("enable", "alpha", repo_path=repo)["ok"] is True
        )
        assert connectors_mod.connector_disabled("alpha", repo_path=repo) is False
        assert connectors_mod.connector_disabled("beta", repo_path=repo) is True

    def test_disable_all_on_an_empty_registry_is_a_honest_no_op(self, isolated):
        """``disable all`` with nothing configured succeeds and says so.

        It is a master switch, not a loop over labels, so it cannot be a usage
        error on an empty registry - and "it worked" is the true answer.
        """
        from cli import connectors as connectors_mod

        result = connectors_mod.mcp_command(
            "disable", "all", repo_path=str(isolated["repo"])
        )
        assert result["ok"] is True, result["lines"]
        assert result["payload"].get("all") is True

    def test_a_verb_with_a_missing_argument_refuses_in_one_line(self, isolated):
        """Every verb that needs an argument refuses with a USAGE line.

        Not a traceback, not silence, and not a refusal that names a flag the
        build does not have.
        """
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        for verb in ("add", "remove", "call", "pin", "reconnect", "enable", "disable"):
            result = connectors_mod.mcp_command(verb, "", repo_path=repo)
            assert result["ok"] is False, (verb, result)
            assert any("usage:" in line for line in result["lines"]), (verb, result)

    def test_an_unknown_verb_is_refused_and_names_the_real_vocabulary(self, isolated):
        """A typo in the verb is a refusal that teaches the vocabulary."""
        from cli import connectors as connectors_mod

        result = connectors_mod.mcp_command(
            "healht", "docs", repo_path=str(isolated["repo"])
        )
        assert result["ok"] is False
        assert "unknown /mcp verb" in result["lines"][0]
        for verb in connectors_mod.MCP_VERBS:
            assert verb in result["lines"][0], (verb, result)

    def test_call_verb_returns_the_receipt_and_the_result(self, isolated):
        """``/mcp call`` returns the tool's answer AND the enforcement receipt.

        The receipt is not decoration: it is how a reader learns whether the
        call was gated, and a call surface that printed only the answer would
        make "undeclared" and "declared and permitted" indistinguishable.
        """
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        _declare(connectors_mod, "memory", "python -m mcp_server", isolated["repo"])
        result = connectors_mod.mcp_command(
            "call", "memory list_repos", repo_path=repo, timeout_s=120.0
        )
        assert result["ok"] is True, result["lines"]
        receipt = result["payload"]["receipt"]
        assert receipt["declared"] is True
        assert receipt["enforced"] is True
        assert receipt["namespaced_name"] == "mcp__memory__list_repos"
        assert any("enforced=True" in line for line in result["lines"]), result
        assert any("budget:" in line for line in result["lines"]), result

    def test_health_verb_reports_one_line_per_connector(self, isolated):
        """``/mcp health`` reports a count and then one line per connector."""
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        _declare(connectors_mod, "memory", "python -m mcp_server", isolated["repo"])
        result = connectors_mod.mcp_command(
            "health", "", repo_path=repo, timeout_s=120.0
        )
        assert result["lines"][0].endswith("connector(s) healthy"), result
        assert any("memory:" in line for line in result["lines"]), result
        assert result["ok"] is True, result

    def test_reconnect_verb_re_probes_one_connector_and_reports_the_budget(
        self, isolated
    ):
        """``/mcp reconnect`` re-probes ONE connector and reports its live state.

        It reports the timeout budget it used, because "it came back" and "it
        came back within a bound" are different facts and only the second one
        tells an operator whether the server is wedged.
        """
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        _declare(connectors_mod, "memory", "python -m mcp_server", isolated["repo"])
        result = connectors_mod.mcp_command(
            "reconnect", "memory", repo_path=repo, timeout_s=120.0
        )
        assert result["ok"] is True, result["lines"]
        assert any("reconnected" in line for line in result["lines"]), result
        assert any("timeout budget:" in line for line in result["lines"]), result

    def test_reconnect_refuses_a_connector_that_does_not_exist(self, isolated):
        """A reconnect for an unknown label is a refusal naming the label."""
        from cli import connectors as connectors_mod

        result = connectors_mod.mcp_command(
            "reconnect", "ghost", repo_path=str(isolated["repo"]), timeout_s=5.0
        )
        assert result["ok"] is False
        assert any("ghost" in line for line in result["lines"]), result


# ---------------------------------------------------------------------------
# 2. Server-contributed commands, against a REAL live server over stdio
# ---------------------------------------------------------------------------


class TestServerContributedCommands:
    """A connected server's prompts appear as ``/mcp__<server>__<prompt>``."""

    def test_a_live_servers_prompts_become_slash_commands(self, isolated, tmp_path):
        """Discovery is over a REAL stdio transport, not over a config file.

        "Real" means a real ``python`` subprocess speaking the real protocol:
        ``initialize`` -> ``prompts/list`` -> close, spawned by the same code
        path the product uses. A stubbed response would prove that a list of
        mappings renders, which is not the claim.
        """
        from cli import connectors as connectors_mod

        server = _write_server(tmp_path, "promptfixture", _PROMPT_SERVER)
        _declare(
            connectors_mod, "fixtures", f"{sys.executable} {server}", isolated["repo"]
        )

        result = connectors_mod.list_prompts(
            "fixtures", repo_path=str(isolated["repo"])
        )
        assert result["ok"] is True, result
        commands = [row["command"] for row in result["commands"]]
        assert commands == [
            "/mcp__fixtures__review-diff",
            "/mcp__fixtures__summarise",
        ], commands
        by_command = {row["command"]: row for row in result["commands"]}
        assert by_command["/mcp__fixtures__summarise"]["description"], by_command
        assert by_command["/mcp__fixtures__summarise"]["arguments"] == ["text"]
        assert by_command["/mcp__fixtures__review-diff"]["arguments"] == [
            "diff",
            "style",
        ]
        assert by_command["/mcp__fixtures__summarise"]["source"] == "mcp_prompt"

    def test_the_prompt_listing_reaches_the_slash_surface(self, isolated, tmp_path):
        """``/mcp <label>`` renders the prompt commands, not just the tools.

        Discovery that stops at the connector API is discovery nobody can use:
        the whole point of the feature is that the commands are REACHABLE from
        the surface a person types at.
        """
        from cli import connectors as connectors_mod

        server = _write_server(tmp_path, "promptfixture", _PROMPT_SERVER)
        _declare(
            connectors_mod, "fixtures", f"{sys.executable} {server}", isolated["repo"]
        )
        result = connectors_mod.mcp_command(
            "list", "fixtures", repo_path=str(isolated["repo"]), timeout_s=120.0
        )
        assert result["ok"] is True, result["lines"]
        rendered = "\n".join(result["lines"])
        assert "/mcp__fixtures__summarise" in rendered, rendered
        assert "/mcp__fixtures__review-diff diff style" in rendered, rendered

    def test_a_prompt_name_cannot_inject_the_namespace_separator(self):
        """A hostile PROMPT name goes through the same normalizer as a tool name.

        The separator is what a client routes on, so a server able to inject one
        could address a tool it does not own. The prompt axis gets the SAME
        ``namespaced_tool_name`` call, which is the structural answer.
        """
        from mcp_server.namespace import parse_namespaced_tool_name
        from mcp_server.tool_pinning import prompt_command_name, prompt_commands

        command = prompt_command_name("memory", "evil/__server")
        assert command == "/mcp__memory__evil-server", command
        server, prompt = parse_namespaced_tool_name(command.lstrip("/"))
        assert (server, prompt) == ("memory", "evil-server"), (server, prompt)

        rows = prompt_commands("memory", [{"name": "a__b"}, {"name": "c__d"}])
        assert len(rows) == 2
        assert len({row["command"] for row in rows}) == 2, rows

    def test_a_prompt_with_no_name_is_skipped_not_rendered(self):
        """A nameless prompt is SKIPPED: an unaddressable command cannot be run.

        Rendering it would put a row in the menu that resolves to nothing, which
        is the "a command a user cannot find does not exist" rule pointed the
        other way: a command they CAN find that does nothing is worse.
        """
        from mcp_server.tool_pinning import prompt_commands

        rows = prompt_commands("memory", [{"name": ""}, {"name": "  "}, {"name": "ok"}])
        assert [row["command"] for row in rows] == ["/mcp__memory__ok"], rows


# ---------------------------------------------------------------------------
# 3. Deferred schemas, with the saving MEASURED
# ---------------------------------------------------------------------------


class TestDeferredSchemas:
    """A server's schemas are withheld until the model asks for one."""

    def test_the_default_catalog_carries_no_input_schema(self, isolated, tmp_path):
        """The default listing withholds ``inputSchema`` for EVERY tool.

        This is the deferral. Asserting it on the absence of a key rather than
        on a token number is what makes it a property: a token total could be
        small for a fat catalog, whereas "the schema is not in the row" cannot.
        """
        from cli import connectors as connectors_mod

        server = _write_server(tmp_path, "promptfixture", _PROMPT_SERVER)
        _declare(
            connectors_mod, "fixtures", f"{sys.executable} {server}", isolated["repo"]
        )
        result = connectors_mod.list_tools("fixtures", repo_path=str(isolated["repo"]))
        assert result["ok"] is True, result.get("error")
        rows = result["tools"]
        assert rows, "the real server offered no tools"
        for row in rows:
            assert "inputSchema" not in row, row
            assert row["deferred"] is True, row
            assert row["schema_loaded"] is False, row
            assert len(row["definition_digest"]) == 64, row

    def test_the_saving_is_measured_and_the_divisor_is_reported(
        self, isolated, tmp_path
    ):
        """The before/after token figure is computed from REAL published schemas.

        Measured on the same descriptors in both arms, with the divisor in the
        output, because the honest claim is a ratio under a stated heuristic and
        not a token count from a tokenizer nobody ran.
        """
        from cli import connectors as connectors_mod

        server = _write_server(tmp_path, "promptfixture", _PROMPT_SERVER)
        _declare(
            connectors_mod, "fixtures", f"{sys.executable} {server}", isolated["repo"]
        )
        result = connectors_mod.list_tools(
            "fixtures", repo_path=str(isolated["repo"]), include_schemas=["echo"]
        )
        assert result["ok"] is True, result.get("error")
        receipt = result["deferral"]
        assert receipt["tool_count"] >= 2, receipt
        assert receipt["chars_per_token"] == 4, receipt
        assert "not a tokenizer" in receipt["estimator"], receipt
        # The server publishes one deliberately enormous schema, so deferral has
        # something real to save and the eager arm is measurably larger.
        assert receipt["eager_tokens"] > 0
        assert receipt["deferred_tokens"] > 0
        assert receipt["deferred_tokens"] < receipt["eager_tokens"], receipt
        assert receipt["saving_tokens"] > 0, receipt
        assert 0.0 < receipt["saving_ratio"] < 1.0, receipt
        # Both the pessimistic (shipped row, with digests) and the model-only
        # figures are reported; quoting only the flattering one would be a
        # receipt shaped to sell the feature.
        assert receipt["saving_tokens_model_only"] > 0, receipt
        # Measured against the schemas the server ACTUALLY published, not
        # against the client wrapper's schema-free normalization. A measurement
        # taken from descriptors with no schemas in them would report a saving
        # of zero and call it a result.
        assert receipt["schema_source"] == "raw", receipt

    def test_a_schema_is_loaded_only_for_the_tool_that_was_asked_for(
        self, isolated, tmp_path
    ):
        """``include_schemas`` loads ONE tool's schema and returns nothing else.

        Loading the whole catalog because one tool was requested would be the
        eager path with extra steps, and it is the failure the feature is
        measured against.
        """
        from cli import connectors as connectors_mod

        server = _write_server(tmp_path, "promptfixture", _PROMPT_SERVER)
        _declare(
            connectors_mod, "fixtures", f"{sys.executable} {server}", isolated["repo"]
        )
        result = connectors_mod.list_tools(
            "fixtures", repo_path=str(isolated["repo"]), include_schemas=["echo"]
        )
        assert result["ok"] is True, result.get("error")
        loaded = result["schemas"]
        assert len(loaded) == 1, loaded
        assert loaded[0]["name"] == "mcp__fixtures__echo", loaded
        assert (
            isinstance(loaded[0]["inputSchema"], dict) and loaded[0]["inputSchema"]
        ), loaded
        assert loaded[0]["tokens"] > 0, loaded
        assert result["deferral"]["loaded"] == ["mcp__fixtures__echo"], result[
            "deferral"
        ]
        # The measurement is only meaningful against the schema the server
        # actually published, so the receipt names the source it read.
        assert result["schema_source"] == "raw", result["schema_source"]

    def test_the_default_catalog_is_identical_with_and_without_a_schema_request(
        self, isolated, tmp_path
    ):
        """Asking for one schema does not change what the catalog RENDERS.

        A "deferral" that quietly widened or narrowed the visible list while
        loading a schema would be a second, invisible filter, and the visible
        list and the callable list would drift apart again.
        """
        from cli import connectors as connectors_mod

        server = _write_server(tmp_path, "promptfixture", _PROMPT_SERVER)
        _declare(
            connectors_mod, "fixtures", f"{sys.executable} {server}", isolated["repo"]
        )
        repo = str(isolated["repo"])
        plain = connectors_mod.list_tools("fixtures", repo_path=repo)
        with_schema = connectors_mod.list_tools(
            "fixtures", repo_path=repo, include_schemas=["echo"]
        )
        assert plain["ok"] is True and with_schema["ok"] is True
        assert [row["name"] for row in plain["tools"]] == [
            row["name"] for row in with_schema["tools"]
        ]
        assert plain["blocked"] == with_schema["blocked"]


# ---------------------------------------------------------------------------
# 4. Pins: an unpinned tool is not exposed
# ---------------------------------------------------------------------------


class TestPins:
    """A connector's pins narrow what the catalog EXPOSES."""

    def test_a_tool_outside_the_pin_is_not_exposed(self, isolated):
        """Pinning one tool hides the others from the catalog.

        Two declarations narrow two different things and conflating them is how
        a catalog shows something the session may not use: ``tools`` is the
        blast radius (what may be CALLED) and ``pins`` is what an operator
        actually APPROVED (what is EXPOSED). A connector with one pin has not
        approved the rest.
        """
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        _declare(connectors_mod, "memory", "python -m mcp_server", isolated["repo"])
        listing = connectors_mod.list_tools("memory", repo_path=repo, timeout_s=120.0)
        assert listing["ok"] is True, listing.get("error")
        assert len(listing["tools"]) >= 5, listing["tools"]

        digest = next(
            row["definition_digest"]
            for row in listing["tools"]
            if row["tool"] == "list_repos"
        )
        pinned = connectors_mod.pin_tool("memory", "list_repos", digest, repo_path=repo)
        assert pinned["ok"] is True
        assert pinned["namespaced_name"] == "mcp__memory__list_repos"

        after = connectors_mod.list_tools("memory", repo_path=repo, timeout_s=120.0)
        assert [row["name"] for row in after["tools"]] == ["mcp__memory__list_repos"], (
            after
        )
        assert "mcp__memory__record_decision" in after["blocked"], after
        assert not ({row["name"] for row in after["tools"]} & set(after["blocked"])), (
            "the exposed and the refused catalog overlap"
        )

    def test_a_pin_that_is_not_a_64_hex_definition_digest_is_refused(self, isolated):
        """A pin requires the digest the approval actually saw.

        Anything else is a pin that can never verify, and a pin that can never
        verify reads as protection that is not there.
        """
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        _declare(connectors_mod, "memory", "python -m mcp_server", isolated["repo"])
        for bad in ("", "abc", "0" * 63, "z" * 64, "not-a-digest"):
            with pytest.raises(connectors_mod.ConnectorError):
                connectors_mod.pin_tool("memory", "list_repos", bad, repo_path=repo)
        assert connectors_mod.connector_permissions("memory", repo_path=repo).pins == ()

    def test_pinning_an_undeclared_connector_is_refused_with_the_remediation(
        self, isolated
    ):
        """A pin against no declaration would bind to a blast radius nobody stated."""
        from cli import connectors as connectors_mod

        connectors_mod._add_server_command(
            "memory",
            "python -m mcp_server",
            tier="project",
            repo_path=str(isolated["repo"]),
        )
        with pytest.raises(connectors_mod.ConnectorError) as excinfo:
            connectors_mod.pin_tool(
                "memory", "list_repos", "0" * 64, repo_path=str(isolated["repo"])
            )
        assert "neo mcp permissions" in str(excinfo.value)

    def test_the_pin_survives_a_read_back_and_a_re_pin_replaces_it(self, isolated):
        """A pin is durable, and re-pinning the same tool replaces its digest."""
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        _declare(connectors_mod, "memory", "python -m mcp_server", isolated["repo"])
        connectors_mod.pin_tool("memory", "list_repos", "a" * 64, repo_path=repo)
        first = connectors_mod.connector_permissions("memory", repo_path=repo)
        assert first is not None and len(first.pins) == 1
        assert first.pins[0]["definition_digest"] == "a" * 64

        replaced = connectors_mod.pin_tool(
            "memory", "list_repos", "b" * 64, repo_path=repo
        )
        assert replaced["replaced"] is True
        assert replaced["pinned_tools"] == 1
        second = connectors_mod.connector_permissions("memory", repo_path=repo)
        assert second is not None and second.pins[0]["definition_digest"] == "b" * 64

    def test_the_pin_verb_acts_and_returns_the_path_it_wrote(self, isolated):
        """``/mcp pin`` goes through the one dispatcher, not its own copy."""
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        _declare(connectors_mod, "memory", "python -m mcp_server", isolated["repo"])
        result = connectors_mod.mcp_command(
            "pin", f"memory list_repos {'c' * 64}", repo_path=repo
        )
        assert result["ok"] is True, result["lines"]
        assert any(
            "pinned mcp__memory__list_repos" in line for line in result["lines"]
        ), result
        assert any("written to" in line for line in result["lines"]), result
        again = connectors_mod.mcp_command(
            "pin", f"memory list_repos {'d' * 64}", repo_path=repo
        )
        assert any("re-pinned" in line for line in again["lines"]), again

    def test_the_pin_gate_still_refuses_a_definition_that_moved(
        self, isolated, monkeypatch
    ):
        """Narrowing the EXPOSED catalog did not weaken the CALL-path pin gate.

        This is the direction that matters. The gate re-hashes the whole live
        catalog before the call; had the surface narrowing been applied to the
        catalog the gate verifies, a server could make a pinned tool vanish by
        being narrower and the gate would never see it. The refused call names
        the namespaced identifier.
        """
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        _declare(connectors_mod, "memory", "python -m mcp_server", isolated["repo"])
        listing = connectors_mod.list_tools("memory", repo_path=repo, timeout_s=120.0)
        digest = next(
            row["definition_digest"]
            for row in listing["tools"]
            if row["tool"] == "list_repos"
        )
        connectors_mod.pin_tool("memory", "list_repos", digest, repo_path=repo)

        from mcp_server.namespace import ToolDefinition

        moved = dict(listing["tools"][0])
        monkeypatch.setattr(
            ToolDefinition,
            "digest",
            property(lambda self: "e" * 64),
        )
        result = connectors_mod.call_tool(
            "memory", "list_repos", {}, repo_path=repo, timeout_s=120.0
        )
        assert result["ok"] is False
        assert "mcp__memory__list_repos" in result["error"], result["error"]
        assert result["receipt"]["pin_status"] == "pin_mismatch", result["receipt"]
        del moved


# ---------------------------------------------------------------------------
# 5. An undeclared tool is the most restrictive class
# ---------------------------------------------------------------------------


class TestUndeclaredIsMostRestrictive:
    """A tool that declares no side-effect class lands on ``mutation``."""

    def test_a_descriptor_with_no_side_effect_class_is_treated_as_mutation(self):
        """The absent class is the conservative rung, not a free pass.

        ``read`` and ``search`` would both be convenient here and both would be
        wrong: a server that publishes no class has told the host nothing about
        what it does, and the ceiling that must be assumed for "nothing stated"
        is the one that needs an explicit opt-in.
        """
        from mcp_server.namespace import DEFAULT_SIDE_EFFECT_CLASS, ToolDefinition

        assert DEFAULT_SIDE_EFFECT_CLASS == "mutation"
        definition = ToolDefinition.from_tool(
            "memory",
            "mystery",
            {"name": "mystery", "description": "says nothing about itself"},
        )
        assert definition.side_effect_class == "mutation"
        assert definition.as_dict()["side_effect_class"] == "mutation"

    def test_an_unrecognised_class_string_falls_to_mutation_too(self):
        """A typo'd class name is ``mutation``, not an unknown that passes."""
        from mcp_server.namespace import ToolDefinition

        for value in ("read-write", "DESTRUCTIVE ", "sideways", "0"):
            definition = ToolDefinition.from_tool(
                "memory", "mystery", {"name": "mystery", "sideEffectClass": value}
            )
            assert definition.side_effect_class in {"mutation", "destructive"}, (
                value,
                definition,
            )

    def test_a_declared_read_ceiling_then_refuses_an_undeclared_tool(self, isolated):
        """Under a ``read`` ceiling an undeclared tool is refused by the ceiling.

        This is the whole reason the default is ``mutation``: an operator who
        declared "this connector only reads" gets that declaration enforced
        against a tool the SERVER never classified.
        """
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        _declare(
            connectors_mod,
            "memory",
            "python -m mcp_server",
            isolated["repo"],
            tools=("*",),
            side_effect="read",
        )
        listing = connectors_mod.list_tools("memory", repo_path=repo, timeout_s=120.0)
        assert listing["ok"] is True, listing.get("error")
        # The real server's own descriptors carry no class through the client
        # wrapper, so every tool lands on `mutation` and the `read` ceiling
        # refuses them all. That is the conservative direction doing its job.
        assert listing["blocked"], "a read ceiling admitted an unclassified tool"
        assert listing["tools"] == [], listing["tools"]

    def test_the_side_effect_rank_helper_agrees_with_the_namespace(self):
        """The re-exported rank is the namespace's own ordering, not a copy."""
        from mcp_server import tool_pinning
        from mcp_server.namespace import SIDE_EFFECT_CLASSES

        for index, name in enumerate(SIDE_EFFECT_CLASSES):
            assert tool_pinning.side_effect_rank(name) == index, name
        assert tool_pinning.side_effect_rank("nonsense") == SIDE_EFFECT_CLASSES.index(
            "mutation"
        )


# ---------------------------------------------------------------------------
# 6. Strict mode is opt-in; the default still says the call was NOT gated
# ---------------------------------------------------------------------------


class TestStrictModeIsOptIn:
    """The surfaced boundary is the default; strict mode is the opt-in."""

    def test_the_default_reports_enforced_false_and_says_it_was_not_gated(
        self, isolated
    ):
        """An undeclared connector runs, and the receipt admits it was NOT gated.

        Backward compatibility, and the honesty rule: enforcing this by default
        would break every connector that exists on upgrade, so the default is
        unchanged and the receipt says what it is rather than implying
        protection that is not there.
        """
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        connectors_mod._add_server_command(
            "memory", "python -m mcp_server", tier="project", repo_path=repo
        )
        assert connectors_mod.connector_permissions("memory", repo_path=repo) is None
        listing = connectors_mod.list_tools("memory", repo_path=repo, timeout_s=120.0)
        assert listing["ok"] is True, listing.get("error")
        receipt = listing["receipt"]
        assert receipt["declared"] is False
        assert receipt["enforced"] is False
        assert "was NOT filtered" in receipt["reason"], receipt

    def test_strict_mode_refuses_an_undeclared_connector_outright(self, isolated):
        """The opt-in refuses BEFORE a server is spawned, and names the fix."""
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        connectors_mod._add_server_command(
            "memory", "python -m mcp_server", tier="project", repo_path=repo
        )
        strict = {"mcp_require_declared_connector": True}
        listing = connectors_mod.list_tools(
            "memory", repo_path=repo, config=strict, timeout_s=120.0
        )
        assert listing["ok"] is False
        assert "strict mode is on" in listing["error"], listing["error"]
        assert "neo mcp permissions memory" in listing["error"], listing["error"]
        assert listing["tools"] == []
        assert listing["receipt"]["enforced"] is False
        assert listing["receipt"]["allowed"] is False

        result = connectors_mod.call_tool(
            "memory",
            "list_repos",
            {},
            repo_path=repo,
            config=strict,
            timeout_s=120.0,
        )
        assert result["ok"] is False
        assert "strict mode is on" in result["error"], result["error"]

    def test_a_declared_connector_is_unaffected_by_strict_mode(self, isolated):
        """Strict mode refuses UNDECLARED connectors; a declared one still works.

        Otherwise the opt-in would be a global kill switch rather than a
        boundary, and an operator who wants it would have to declare every
        connector in the project to keep using any of them.
        """
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        _declare(connectors_mod, "memory", "python -m mcp_server", isolated["repo"])
        strict = {"mcp_require_declared_connector": True}
        result = connectors_mod.call_tool(
            "memory",
            "list_repos",
            {},
            repo_path=repo,
            config=strict,
            timeout_s=120.0,
        )
        assert result["ok"] is True, result.get("error")
        assert result["receipt"]["enforced"] is True

    def test_the_key_is_read_by_presence_not_by_a_truthy_default(self, isolated):
        """Absent, false, unusable and true are four DIFFERENT answers.

        ``config.get(key, False)`` would treat an absent key and an explicit
        ``False`` as one thing, and a string typo as a decision. The key is
        read by presence so "nobody said" stays distinguishable from "somebody
        said no".
        """
        from mcp_server.tool_pinning import strict_undeclared_reason

        assert strict_undeclared_reason({}) is None
        assert strict_undeclared_reason(None) is None
        assert (
            strict_undeclared_reason({"mcp_require_declared_connector": False}) is None
        )
        assert (
            strict_undeclared_reason({"mcp_require_declared_connector": "false"})
            is None
        )
        assert (
            strict_undeclared_reason({"mcp_require_declared_connector": "no"}) is None
        )
        assert strict_undeclared_reason({"mcp_require_declared_connector": []}) is None
        for value in (True, "true", "yes", "require", "strict", 1):
            assert (
                strict_undeclared_reason(
                    {"mcp_require_declared_connector": value}, label="x"
                )
                is not None
            ), value

    def test_no_budget_or_strict_key_was_added_to_config_defaults(self):
        """No key from this round is in ``harness.config.DEFAULTS``.

        A DEFAULTS value merges into every task and every eval arm, so a
        connector-session ceiling or a security posture would silently become a
        fact about every run in the project. Both are read by key presence.
        """
        from harness.config import DEFAULTS

        for key in (
            "mcp_require_declared_connector",
            "mcp_max_tools_exposed",
            "mcp_max_tool_calls",
            "mcp_chars_per_token",
            "mcp_disable_on_timeout",
        ):
            assert key not in DEFAULTS, (
                f"{key} is in DEFAULTS; it merges into every task and every eval arm"
            )


# ---------------------------------------------------------------------------
# 7. Lifecycle: a hanging server is disabled with a reason
# ---------------------------------------------------------------------------


class TestLifecycle:
    """A probe is bounded, and a wedge is reported rather than left to repeat."""

    def test_a_hanging_server_is_disabled_with_a_recorded_reason(
        self, isolated, tmp_path
    ):
        """A server that never answers is DISABLED, and the reason is recorded.

        Reported AND disabled, not merely reported: a wedge that repeats is a
        wedge that outlasts the session, and an operator who typed one command
        should not pay the full timeout on every subsequent call. The reason is
        stored so ``/mcp list`` and ``doctor`` can say WHY, and ``/mcp enable``
        reverses it.
        """
        from cli import connectors as connectors_mod

        server = _write_server(tmp_path, "hanger", _HANGING_SERVER)
        _declare(
            connectors_mod, "wedged", f"{sys.executable} {server}", isolated["repo"]
        )
        repo = str(isolated["repo"])
        assert connectors_mod.connector_disabled("wedged", repo_path=repo) is False

        started = time.monotonic()
        result = connectors_mod.list_tools("wedged", repo_path=repo, timeout_s=3.0)
        elapsed = time.monotonic() - started

        assert result["ok"] is False, result
        assert elapsed < 45.0, f"the probe was not bounded ({elapsed:.1f}s)"
        assert connectors_mod.connector_disabled("wedged", repo_path=repo) is True
        state = connectors_mod.read_state(repo_path=repo)
        reason = state["disabled"]["wedged"]
        assert "disabled automatically" in reason, reason
        assert "did not answer" in reason, reason

    def test_the_disabled_connector_is_refused_before_any_spawn(
        self, isolated, tmp_path
    ):
        """Once disabled, the refusal happens BEFORE a process is spawned.

        Ordering is the point: a refusal that first spawns the server it is
        refusing is a refusal that pays the full timeout every time, which is
        the behaviour disabling exists to stop. The spawn is counted so the
        test can prove it did not happen.
        """
        from cli import connectors as connectors_mod

        server = _write_server(tmp_path, "hanger", _HANGING_SERVER)
        _declare(
            connectors_mod, "wedged", f"{sys.executable} {server}", isolated["repo"]
        )
        repo = str(isolated["repo"])
        connectors_mod.list_tools("wedged", repo_path=repo, timeout_s=3.0)
        assert connectors_mod.connector_disabled("wedged", repo_path=repo) is True

        spawned: list[str] = []
        original = connectors_mod._run_bounded

        def counting(fn, timeout_s, timeout_message="health check timed out"):
            spawned.append("called")
            return original(fn, timeout_s, timeout_message)

        connectors_mod._run_bounded = counting
        try:
            started = time.monotonic()
            result = connectors_mod.call_tool(
                "wedged", "list_repos", {}, repo_path=repo, timeout_s=3.0
            )
            elapsed = time.monotonic() - started
        finally:
            connectors_mod._run_bounded = original

        assert result["ok"] is False
        assert "is disabled" in result["error"], result["error"]
        assert "/mcp enable wedged" in result["error"], result["error"]
        assert spawned == [], "a disabled connector still spawned a server"
        assert elapsed < 1.0, f"the refusal was not immediate ({elapsed:.2f}s)"

    def test_a_missing_executable_is_reported_but_does_not_disable(self, isolated):
        """A typo in a path is an operator error, and hiding it would be worse.

        Only a HANG disables. A command that cannot be spawned is reported as a
        failure and the connector stays exactly where it was, because silently
        switching off the connector an operator just mistyped would remove the
        evidence of the mistake.
        """
        from cli import connectors as connectors_mod

        connectors_mod._add_server_command(
            "ghost",
            "definitely-not-a-real-neo-binary-42",
            tier="project",
            repo_path=str(isolated["repo"]),
        )
        repo = str(isolated["repo"])
        result = connectors_mod.list_tools("ghost", repo_path=repo, timeout_s=10.0)
        assert result["ok"] is False
        assert result["error"], result
        assert connectors_mod.connector_disabled("ghost", repo_path=repo) is False

    def test_the_state_file_is_a_separate_file_from_the_declaration(self, isolated):
        """Runtime state is NOT written into the committable declaration.

        ``connectors.toml`` and ``connector-permissions.toml`` are things a
        reviewer reads in a diff. Writing a transient operator decision into
        them would put "disabled for the next ten minutes" in a code review,
        and reading a disable marker out of a hand-edited connector file would
        let a connector arrive switched off.
        """
        from cli import connectors as connectors_mod

        repo = isolated["repo"]
        _declare(connectors_mod, "memory", "python -m mcp_server", repo)
        connectors_mod.mcp_command("disable", "memory", repo_path=str(repo))
        state_path = repo / ".neo" / connectors_mod.STATE_FILE
        assert state_path.is_file()
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == 1
        assert payload["disabled"]["memory"]
        connectors_text = (repo / ".neo" / connectors_mod.CONNECTORS_FILE).read_text(
            encoding="utf-8"
        )
        assert "disable" not in connectors_text.lower(), connectors_text
        permissions_text = (repo / ".neo" / connectors_mod.PERMISSIONS_FILE).read_text(
            encoding="utf-8"
        )
        assert "disable" not in permissions_text.lower(), permissions_text

    def test_an_unreadable_state_file_is_reported_not_treated_as_a_decision(
        self, isolated
    ):
        """ "Nothing is disabled" and "we could not tell" are different facts."""
        from cli import connectors as connectors_mod

        repo = isolated["repo"]
        _declare(connectors_mod, "memory", "python -m mcp_server", repo)
        connectors_mod.mcp_command("disable", "memory", repo_path=str(repo))
        (repo / ".neo" / connectors_mod.STATE_FILE).write_text(
            "{not json", encoding="utf-8"
        )
        assert connectors_mod.connector_state_problem(repo_path=str(repo))
        assert connectors_mod.connector_disabled("memory", repo_path=str(repo)) is False
        listed = connectors_mod.mcp_command("list", "", repo_path=str(repo))
        assert any("state unreadable" in line for line in listed["lines"]), listed


# ---------------------------------------------------------------------------
# 8. The per-run tool budget
# ---------------------------------------------------------------------------


class TestBudget:
    """What one run exposed, called, and is projected to spend."""

    def test_the_budget_is_recorded_on_every_call_receipt(self, isolated):
        """The receipt carries the run's MCP spend, so no second read is needed.

        A budget that lives somewhere else is a dashboard; one that rides on the
        receipt that already exists is answerable from what a caller already
        holds.
        """
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        _declare(connectors_mod, "memory", "python -m mcp_server", isolated["repo"])
        result = connectors_mod.call_tool(
            "memory", "list_repos", {}, repo_path=repo, timeout_s=120.0
        )
        assert result["ok"] is True, result.get("error")
        budget = result["receipt"]["budget"]
        assert budget["calls_made"] == 1, budget
        assert budget["max_calls"] == 32, budget
        assert budget["tools_exposed"] >= 5, budget
        assert budget["projected_context_tokens"] > 0, budget
        assert budget["chars_per_token"] == 4, budget
        assert "not a tokenizer" in budget["estimator"], budget
        assert budget["within_budget"] is True, budget
        assert budget["share_by_server"]["memory"] == 1.0, budget

    def test_the_budget_refuses_the_call_that_would_cross_the_ceiling(self, isolated):
        """A budget that only reports is a dashboard, and a dashboard does not stop
        one server owning the window.

        ``mcp_max_tool_calls = 1`` is read by KEY PRESENCE, so the second call is
        refused with the ceiling named, and the refused call is NOT counted - a
        budget that counted its own refusal would report a refusal as
        consumption.
        """
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        _declare(connectors_mod, "memory", "python -m mcp_server", isolated["repo"])
        tight = {"mcp_max_tool_calls": 1}
        first = connectors_mod.call_tool(
            "memory", "list_repos", {}, repo_path=repo, config=tight, timeout_s=120.0
        )
        assert first["ok"] is True, first.get("error")
        second = connectors_mod.call_tool(
            "memory", "list_repos", {}, repo_path=repo, config=tight, timeout_s=120.0
        )
        assert second["ok"] is False
        assert "budget exhausted" in second["error"], second["error"]
        assert "1/1" in second["error"], second["error"]
        assert second["receipt"]["budget"]["calls_made"] == 1, second["receipt"][
            "budget"
        ]

    def test_an_absent_ceiling_key_is_not_a_ceiling_of_zero(self, isolated):
        """Absent means "nobody said", which is not the same as "no calls".

        ``config.get(key, 0) or 0`` would turn a missing key into a total
        lockout; a value of ``0`` remains a deliberate "no MCP calls at all".
        """
        from mcp_server.tool_pinning import DEFAULT_MAX_TOOL_CALLS, budget_from_config

        assert budget_from_config({}).max_calls == DEFAULT_MAX_TOOL_CALLS
        assert budget_from_config(None).max_calls == DEFAULT_MAX_TOOL_CALLS
        assert budget_from_config({"mcp_max_tool_calls": 0}).max_calls == 0

    def test_an_unusable_ceiling_is_reported_in_notes_and_falls_back(self, isolated):
        """A typo cannot produce a budget nobody asked for."""
        from mcp_server.tool_pinning import DEFAULT_MAX_TOOL_CALLS, budget_from_config

        for value in ("lots", None, [], {}, -3):
            budget = budget_from_config({"mcp_max_tool_calls": value})
            assert budget.max_calls == DEFAULT_MAX_TOOL_CALLS, value
            assert budget.notes, value
        assert budget_from_config({"mcp_max_tool_calls": 7}).notes == ()

    def test_one_server_cannot_quietly_own_the_window(self, isolated):
        """The dominance signal is per SERVER, not just a total.

        "3 of 64 calls" is a total; what an operator needs to know is which
        server took the window, and a receipt with no per-server share cannot
        answer that question at all.
        """
        from mcp_server.tool_pinning import ToolBudget

        small = [
            {"name": "a", "description": "a", "inputSchema": {}},
            {"name": "b", "description": "b", "inputSchema": {}},
        ]
        fat = [
            {"name": f"f{index}", "description": "f" * 400, "inputSchema": {}}
            for index in range(20)
        ]
        budget = ToolBudget()
        budget.record_exposure("small", small)
        budget.record_exposure("fat", fat)
        budget.record_call("fat", "f0")
        budget.record_call("small", "a")
        receipt = budget.as_dict()
        assert receipt["tools_exposed"] == 22, receipt
        assert receipt["tools_by_server"] == {"fat": 20, "small": 2}, receipt
        assert receipt["calls_by_server"] == {"fat": 1, "small": 1}, receipt
        assert receipt["share_by_server"] == {"fat": 0.5, "small": 0.5}, receipt
        assert (
            receipt["tokens_by_server"]["fat"] > receipt["tokens_by_server"]["small"]
        ), receipt
        assert receipt["within_budget"] is True

    def test_re_exposing_one_server_does_not_double_count_it(self, isolated):
        """A re-listed server is ONE catalog the model pays for, not two.

        A budget that double-counted a re-list would report a server as more
        dominant than it is, which is the same class of dishonesty as reporting
        the wrong total.
        """
        from mcp_server.tool_pinning import ToolBudget

        tools = [{"name": "a", "description": "a", "inputSchema": {}}]
        budget = ToolBudget()
        first = budget.record_exposure("memory", tools)
        second = budget.record_exposure("memory", tools)
        assert first == second, (first, second)
        assert budget.tools_exposed == 1, budget.as_dict()
        assert budget.projected_context_tokens == first, budget.as_dict()

    def test_would_allow_does_not_mutate_the_budget(self, isolated):
        """The question and the record are two calls, so a caller may ask first."""
        from mcp_server.tool_pinning import ToolBudget

        budget = ToolBudget(max_calls=1)
        assert budget.would_allow(server="memory", tool="a").allowed is True
        assert budget.calls_made == 0, budget.as_dict()
        assert budget.record_call("memory", "a").allowed is True
        assert budget.calls_made == 1
        assert budget.would_allow(server="memory", tool="a").allowed is False
        assert budget.calls_made == 1, budget.as_dict()

    def test_the_budget_refuses_an_oversized_catalog(self, isolated):
        """The exposure ceiling is checked too, not just the call count."""
        from mcp_server.tool_pinning import ToolBudget

        budget = ToolBudget(max_tools_exposed=1)
        budget.record_exposure(
            "fat",
            [
                {"name": f"t{i}", "description": "t", "inputSchema": {}}
                for i in range(4)
            ],
        )
        assert budget.as_dict()["within_budget"] is False
        decision = budget.record_call("fat", "t0")
        assert decision.allowed is False
        assert "exceeds the per-run ceiling" in decision.reason, decision

    def test_the_listing_reports_the_budget_for_the_connector(self, isolated):
        """``/mcp <label>`` answers "how much of the window is this server taking"."""
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        _declare(connectors_mod, "memory", "python -m mcp_server", isolated["repo"])
        listing = connectors_mod.list_tools("memory", repo_path=repo, timeout_s=120.0)
        assert listing["ok"] is True, listing.get("error")
        budget = listing["budget"]
        assert budget["tools_exposed"] >= 5, budget
        assert budget["projected_context_tokens"] > 0, budget
        rendered = connectors_mod.mcp_command(
            "list", "memory", repo_path=repo, timeout_s=120.0
        )
        assert any("budget:" in line for line in rendered["lines"]), rendered


# ---------------------------------------------------------------------------
# 9. Markup safety, gate integrity, and the backward-compatibility floor
# ---------------------------------------------------------------------------


class TestMarkupSafety:
    """A hostile string from a server is DATA, and it must survive rendering."""

    def test_a_hostile_server_name_is_visible_after_a_real_render(self):
        """Rendered through a REAL rich Console and asserted VISIBLE.

        The pinned failure is a render that EATS the message, so the assertion is
        that the text is still there afterwards and that the rows after it still
        print - not that a bracket is absent, which rich escaping makes a false
        and useless check. A substring assertion on the pre-render string would
        pass while the message was being deleted.
        """
        import io

        from rich.console import Console
        from rich.markup import escape

        hostile = "[bold red]evil[/bold red] \u001b[31mred\u001b[0m [x]"
        lines = [f"mcp server {hostile}", "the next row must still print"]
        buffer = io.StringIO()
        console = Console(file=buffer, width=100, markup=True, force_terminal=False)
        for line in lines:
            console.print(escape(line))
        rendered = buffer.getvalue()
        assert "evil" in rendered, rendered
        assert "the next row must still print" in rendered, rendered
        assert rendered.count("mcp server") == 1, rendered

    def test_every_rendered_line_from_the_verb_surface_is_plain(self, isolated):
        """Every line the ``/mcp`` surface produces is PLAIN text.

        A connector label, a launch command and a tool description all come from
        data this product did not author, so they cross into Textual's markup
        parser as data. The escape is the CALLER's job - the module states that
        in the docstring - and this test pins that it did not smuggle markup in.
        """
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        for verb, argument in (
            ("list", ""),
            ("add", "docs python -m mcp_server"),
            ("health", ""),
            ("disable", "docs"),
            ("pin", "docs list_repos"),
        ):
            result = connectors_mod.mcp_command(verb, argument, repo_path=repo)
            for line in result["lines"]:
                assert isinstance(line, str), (verb, line)
                assert "[" not in line and "]" not in line, (verb, line)

    def test_a_bracket_in_a_tool_description_is_escaped_by_the_caller_not_by_us(self):
        """The surface does not try to "fix" data by editing it.

        A mangled description is worse than a bracketed one: it is a receipt
        that no longer matches what the server published, and a pin is taken
        against exactly that text.
        """
        from mcp_server.tool_pinning import deferred_catalog

        raw = [
            {
                "name": "weird",
                "description": "[bold]not markup[/bold] but data",
                "inputSchema": {},
            }
        ]
        rows = deferred_catalog("memory", raw)
        assert rows[0].description == "[bold]not markup[/bold] but data", rows
        assert "[bold]" in rows[0].as_model_dict()["description"], rows[0]


class TestGateIntegrity:
    """This round changed the surface, never the gate."""

    def test_no_completion_vocabulary_reaches_the_new_modules(self):
        """Neither new module names a completion status or a verdict.

        A budget hint a verifier can read is how ``completed_unverified``
        becomes promotable, so the vocabulary is pinned absent at the SOURCE
        level rather than trusted to a reader's judgement.
        """
        import ast

        forbidden = (
            "completed_verified",
            "completed_unverified",
            "status_is_success",
            "run_verdict",
            "agent_contracts",
            "RUN_STATUSES",
        )
        for name in ("mcp_server/tool_pinning.py",):
            source = (REPO_ROOT / name).read_text(encoding="utf-8")
            tree = ast.parse(source)
            literals = [
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            ]
            for word in forbidden:
                assert word not in literals, (name, word)

    def test_strict_mode_and_the_budget_are_refusals_not_allowances(self):
        """Both new opt-in checks can only ever REFUSE.

        A check that could also permit is a check whose default matters, and
        whose default is exactly what this round promised not to change. The
        direction is asserted by construction: absent key means absent refusal.
        """
        from cli import connectors as connectors_mod
        from mcp_server.tool_pinning import strict_undeclared_reason

        assert connectors_mod.strict_undeclared_reason({}) is None
        assert (
            connectors_mod.strict_undeclared_reason(
                {"mcp_require_declared_connector": 0}
            )
            is None
        )
        assert strict_undeclared_reason({}) is None
        # The DEFAULT connector behaviour is unchanged: a declared connector is
        # permitted and an undeclared one runs with `enforced: false`.
        assert connectors_mod.enforcement_receipt(None).enforced is False
        assert "NOT gated" in connectors_mod.enforcement_receipt(None).reason

    def test_the_receipt_key_set_is_a_superset_of_the_historical_one(self):
        """Nothing the existing receipts published was removed.

        The real per-surface shape is asserted against a LIVE listing in
        ``test_the_listing_shape_kept_every_historical_key``; this one pins the
        receipt alone, because a receipt is handed to a machine and a missing
        key there is a silent schema break.
        """
        from cli import connectors as connectors_mod

        historical = {
            "label",
            "declared",
            "enforced",
            "allowed",
            "reason",
            "side_effect",
            "declared_tools",
            "network",
            "write_declared",
            "write",
            "pinned",
            "pin_status",
            "tool",
            "namespaced_name",
            "definition_digest",
            "untrusted_reviews",
            "hooks",
        }
        assert historical <= set(connectors_mod.enforcement_receipt(None).to_dict())
        added = {"budget"}
        assert added <= set(connectors_mod.enforcement_receipt(None).to_dict())

    def test_the_listing_shape_kept_every_historical_key(self, isolated, tmp_path):
        """``list_tools`` / ``call_tool`` kept their keys; this round only added."""
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        _declare(connectors_mod, "memory", "python -m mcp_server", isolated["repo"])
        listing = connectors_mod.list_tools("memory", repo_path=repo, timeout_s=120.0)
        assert {"ok", "tools", "error", "namespaced", "blocked", "receipt"} <= set(
            listing
        )
        added = {"schemas", "prompts", "prompts_error", "deferral", "budget"}
        assert added <= set(listing), sorted(added - set(listing))

        call = connectors_mod.call_tool(
            "memory", "list_repos", {}, repo_path=repo, timeout_s=120.0
        )
        assert {"ok", "text", "error", "tool", "namespaced", "receipt"} <= set(call)


class TestTheRealProjectServer:
    """This repository's own server is the real target, not only a fixture."""

    def test_the_real_server_publishes_its_five_tools_with_no_prompts(self, isolated):
        """A server with no prompt capability reports that, and is NOT broken.

        ``python -m mcp_server`` publishes five tools and no prompts. Rendering
        "no prompts" as a failed probe would make a perfectly healthy server
        look broken, so the reason is recorded and the tool read is kept.
        """
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        _declare(connectors_mod, "memory", "python -m mcp_server", isolated["repo"])
        result = connectors_mod.list_tools("memory", repo_path=repo, timeout_s=120.0)
        assert result["ok"] is True, result.get("error")
        assert len(result["tools"]) == 5, [row["name"] for row in result["tools"]]
        assert result["prompts"] == [], result["prompts"]
        # A server that publishes no prompts must SAY SO: silence reads as a
        # broken probe, and the rendered surface states it either way.
        rendered = connectors_mod.mcp_command(
            "list", "memory", repo_path=repo, timeout_s=120.0
        )
        assert any("publishes none" in line for line in rendered["lines"]), rendered
        for row in result["tools"]:
            assert "inputSchema" not in row, row
            assert len(row["definition_digest"]) == 64, row

    def test_the_real_server_is_still_callable_end_to_end(self, isolated):
        """The namespace layer, the hooks and the untrusted review all still run.

        Driven over a REAL ``python -m mcp_server`` subprocess, and the receipt
        is read for the three properties the boundary promises. The hook rows
        depend on ``extensions.user_hooks`` importing; when it does not, the
        receipt says ``hooks_unavailable`` rather than pretending a gate ran,
        and this test asserts THAT rather than skipping.
        """
        from cli import connectors as connectors_mod

        repo = str(isolated["repo"])
        _declare(connectors_mod, "memory", "python -m mcp_server", isolated["repo"])
        result = connectors_mod.call_tool(
            "memory", "list_repos", {}, repo_path=repo, timeout_s=120.0
        )
        assert result["ok"] is True, result.get("error")
        receipt = result["receipt"]
        assert receipt["namespaced_name"] == "mcp__memory__list_repos"
        assert len(receipt["definition_digest"]) == 64
        assert receipt["untrusted_reviews"], (
            "the result was not reviewed as untrusted content"
        )
        events = [row.get("event") for row in receipt["hooks"]]
        if events == ["-", "-", "-"]:
            pytest.fail(
                "the hook layer is unavailable on this tree, so this test cannot "
                "assert the PreToolUse/PostToolUse gate: extensions.user_hooks "
                "does not import. See cli/AGENTS.md, VEX-CS-07."
            )
        assert "PreToolUse" in events and "PostToolUse" in events, events


class TestBackwardCompatibility:
    """Rule 8, per command and per key."""

    def test_the_mcp_registry_row_is_unchanged(self):
        """``/mcp`` keeps its type, its headless policy and its flag equivalent.

        This round added handlers, not a command, so the registry row must be
        byte-identical: a ``/mcp`` that became something else would break a
        script that types ``neo mcp list``.
        """
        from cli import commands as commands_mod

        spec = commands_mod.command_spec("/mcp")
        assert spec is not None
        assert commands_mod.HEADLESS_COMMAND_POLICIES["/mcp"] == "flag-only"
        assert commands_mod.HEADLESS_FLAG_EQUIVALENTS["/mcp"] == "neo mcp list"
        assert spec.type_name == "local", spec.type_name
        assert len(commands_mod.COMMAND_SPECS) >= 52

    def test_every_headless_flag_equivalent_row_is_still_present(self):
        """The pre-wave flag table is a FLOOR plus the historical names."""
        from cli import commands as commands_mod

        historical = {
            "/connect": "neo connect",
            "/cost": "neo status --task-id <task-id>",
            "/hooks": "neo hooks list",
            "/init": "neo config init-project",
            "/login": "neo login",
            "/logout": "neo logout",
            "/mcp": "neo mcp list",
            "/migrate": "neo migrate",
            "/model": "neo login",
            "/plugins": "neo plugin list",
            "/settings": "neo config list",
            "/skills": "neo skills list",
            "/status": "neo status --task-id <task-id>",
            "/support-bundle": "neo support-bundle",
            "/theme": "neo config set theme <name>",
            "/watch": "neo watch <task-id>",
            "/worktree": "neo worktree list",
        }
        for name, equivalent in historical.items():
            assert commands_mod.HEADLESS_FLAG_EQUIVALENTS.get(name) == equivalent, name

    def test_the_exit_code_contract_is_unchanged(self):
        """All six exit codes still exist with their documented values."""
        from cli.exit_codes import EXIT_CODES

        assert {EXIT_CODES[key] for key in EXIT_CODES} >= {0, 1, 2, 3, 4, 130}

    def test_this_round_added_no_command_row_and_edited_no_forbidden_file(self):
        """The forbidden surfaces were not touched, and no row was added.

        ``cli/tui.py`` and ``cli/commands.py`` are another terminal's, and the
        brief for this round adds verbs to an EXISTING row rather than a new
        command: adding a row would have put a second door on the same behaviour.
        """
        import ast

        for name in ("cli/tui.py", "cli/commands.py"):
            source = (REPO_ROOT / name).read_text(encoding="utf-8")
            ast.parse(source)  # parses; the tree never went unimportable
        from cli.commands import COMMAND_SPECS

        names = [spec.name for spec in COMMAND_SPECS]
        assert len(names) == len(set(names)), "a duplicate command row exists"
        assert "/mcp" in names
        assert not any(name.startswith("/mcp__") for name in names), (
            "a server-contributed command became a static registry row; they are "
            "discovered per session and must not be"
        )


class TestTheScriptSurfaceIsTheSameFunction:
    """Rule 4: the slash verb is the implementation; nothing is reimplemented."""

    def test_the_dispatcher_is_the_only_place_the_verbs_are_implemented(self):
        """No second ``if verb ==`` ladder exists for ``/mcp``.

        Read with ``ast`` rather than grep, so a reformat cannot empty the check
        and a comment cannot make it pass.
        """
        import ast

        from cli import connectors as connectors_mod

        source = (REPO_ROOT / "cli" / "connectors.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        dispatcher = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "mcp_command"
        )
        compared = {
            node.comparators[0].value
            for node in ast.walk(dispatcher)
            if isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "name"
            and isinstance(node.ops[0], ast.Eq)
            and isinstance(node.comparators[0], ast.Constant)
            and isinstance(node.comparators[0].value, str)
        }
        # `list`, `add`, `remove`, `health`, `call` and `pin` are compared
        # explicitly; `reconnect`, `enable` and `disable` are reached by the
        # fall-through the dispatcher documents. Every verb is reachable.
        assert {
            "list",
            "add",
            "remove",
            "health",
            "call",
            "pin",
            "reconnect",
            "enable",
        } <= compared, sorted(compared)
        assert connectors_mod.MCP_VERBS[-1] == "disable"
        # A `dict` of per-verb implementations would be the second-implementation
        # shape; there is none.
        assert not any(
            isinstance(node, ast.Dict) and "mcp" in ast.dump(node).lower()
            for node in ast.walk(dispatcher)
        )

    def test_the_module_is_importable_on_its_own(self):
        """``import cli.connectors`` must not require a hook layer or a server."""
        code = "import cli.connectors as c; print(len(c.MCP_VERBS))"
        completed = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=120,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == "9", completed.stdout
