"""`/connect` — the opencode-style authentication flow.

Every test is named after the behaviour it checks, and every one of them is
named after something a person could have complained about. The eight the
brief requires are marked ``(brief)``; the rest are the contracts those eight
would silently break if they were implemented carelessly.

Nothing here contacts a provider, reads a real credential, or needs Docker.
The tests that exercise the real child-process probe install a FAKE
``litellm`` on the child's ``PYTHONPATH``, so they measure the mechanism (can
a banner escape? does the receipt come back?) rather than a network.

The autouse fixture isolates ``NEO_HOME`` and the whole settings chain into
``tmp_path``, so no test can read or write the developer's real store.
"""

from __future__ import annotations

import builtins
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from cli import auth as auth
from cli import commands as commands_mod
from cli import onboard as onboard

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Isolation. The store lives under NEO_HOME; the settings chain under tmp_path.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_auth_env(tmp_path, monkeypatch):
    """Point every credential and settings location at ``tmp_path``."""
    for name in tuple(os.environ):
        upper = name.upper()
        if (
            upper.startswith("NEO_")
            or upper.endswith("_API_KEY")
            or upper == "HARNESS_HOME"
        ):
            monkeypatch.delenv(name, raising=False)
    home = tmp_path / "neohome"
    appdata = tmp_path / "appdata"
    project = tmp_path / "repo" / ".neo"
    for path in (home, appdata, project):
        path.mkdir(parents=True, exist_ok=True)
    for name, value in {
        "NEO_HOME": str(home),
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "APPDATA": str(appdata),
        "XDG_CONFIG_HOME": str(appdata / "xdg"),
        "NEO_CONFIG": str(appdata / "neo" / "settings.toml"),
        "NEO_LEGACY_CONFIG": str(appdata / "neo" / "config.toml"),
        "NEO_PROJECT_DIR": str(project),
        "HARNESS_LOGS_DIR": str(tmp_path / "logs"),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)
    return {"home": home, "root": tmp_path, "store": home / auth.AUTH_FILENAME}


@pytest.fixture
def env(_isolated_auth_env):
    return _isolated_auth_env


def _store(env) -> Path:
    return Path(env["store"])


def _failing_probe(kind: str = "timeout", *, elapsed_ms: float = 42.0):
    """A probe double: never touches a network, records what it was asked."""

    def _probe(request: Dict[str, Any], timeout_s: int) -> Dict[str, Any]:
        _probe.calls.append(dict(request))
        return {"ok": False, "checked": True, "kind": kind, "elapsed_ms": elapsed_ms}

    _probe.calls = []
    return _probe


def _ok_probe(*, elapsed_ms: float = 7.0):
    def _probe(request: Dict[str, Any], timeout_s: int) -> Dict[str, Any]:
        _probe.calls.append(dict(request))
        return {"ok": True, "checked": True, "kind": "", "elapsed_ms": elapsed_ms}

    _probe.calls = []
    return _probe


def _declare_acme_two_methods(env, **overrides) -> None:
    """Add a provider that declares TWO auth methods, as a data edit."""
    row = {
        "id": "acme",
        "name": "ACME",
        "methods": ["api_key", "api_base"],
        "key_url": "https://acme.test/keys",
        "priority": 1,
        "default_model": "acme-m",
        "base_url": "https://acme.test/v1",
    }
    row.update(overrides)
    (env["home"] / auth.PROVIDERS_FILENAME).write_text(
        json.dumps({"providers": [row]}), encoding="utf-8"
    )


# Real provider failures, transcribed from the incident this round closes and
# from the shapes the library actually raises. Every one of these is input the
# old flow handed straight to the terminal.
PROVIDER_FAILURE_TEXTS = (
    "litellm.Timeout: APITimeoutError - Request timed out.",
    "litellm.AuthenticationError: AuthenticationError: OpenAIException - "
    "Incorrect API key provided: sk-or-v1-***",
    "litellm.RateLimitError: RateLimitError: OpenAIException - 429 Too Many Requests",
    "litellm.NotFoundError: NotFoundError: OpenAIException - model_not_found: openai/nope",
    "litellm.ServiceUnavailableError: 503 No available channel for model z-ai/glm",
    "litellm.exceptions.UnprocessableEntityError: OpenAIException - content policy",
    "httpx.ConnectTimeout: timed out",
    "openai.AuthenticationError: The api key you provided is invalid",
    "ConnectionError: [Errno -2] Name or service not known",
    'Traceback (most recent call last):\n  File "litellm/main.py", line 1, in completion',
    "Provider List: https://docs.litellm.ai/docs/providers\nLiteLLM.Info: Provider List",
    "",
)


# ---------------------------------------------------------------------------
# 1 (brief). First run must never block.
# ---------------------------------------------------------------------------


class TestFirstRunNeverBlocks:
    def test_session_start_never_reads_stdin_and_never_tests(
        self, env, monkeypatch, capsys
    ):
        """The session-start hook may not call input, getpass, or the wizard.

        The old hook ran a whole wizard here, so a machine with no credential
        could not open the app at all. Both console readers are booby-trapped:
        one call is a test failure, not a slow test.
        """
        called: List[str] = []

        def _boom_input(*a, **k):
            called.append("input")
            raise AssertionError("the first run must not read stdin")

        def _boom_getpass(*a, **k):
            called.append("getpass")
            raise AssertionError("the first run must not read a key")

        monkeypatch.setattr(builtins, "input", _boom_input)
        monkeypatch.setattr("getpass.getpass", _boom_getpass)
        monkeypatch.setattr(
            onboard, "run_repl_wizard", lambda *a, **k: called.append("wizard")
        )
        monkeypatch.setattr(
            onboard, "test_credentials", lambda *a, **k: called.append("probe")
        )
        monkeypatch.setattr(
            onboard, "prompt_allowed", lambda *a, **k: called.append("tty_probe")
        )

        assert onboard.needs_onboarding() is True
        returned = onboard.maybe_onboard_repl({"model": ""})

        assert called == []
        assert returned == {"model": ""}
        out = capsys.readouterr().out
        assert "/connect" in out
        assert len([line for line in out.splitlines() if line.strip()]) == 1

    def test_a_process_with_no_credentials_and_closed_stdin_starts(self, env):
        """A real child process, no TTY, stdin closed: it must return.

        The process-level twin of the unit test above. A blocking prompt would
        hang here or die on EOF; a one-line hint cannot do either.
        """
        script = textwrap.dedent(
            """
            import json
            from cli import onboard
            cfg = onboard.maybe_onboard_repl({"model": ""})
            print("RETURNED:" + json.dumps(cfg))
            """
        )
        started = time.monotonic()
        child = subprocess.run(
            [sys.executable, "-c", script],
            input="",
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO_ROOT),
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            check=False,
        )
        elapsed = time.monotonic() - started
        assert child.returncode == 0, child.stderr
        assert "RETURNED:" in child.stdout
        assert elapsed < 30.0, f"session start took {elapsed:.1f}s with stdin closed"

    def test_the_only_surviving_gate_is_on_work_that_spends_money(
        self, env, monkeypatch, capsys
    ):
        """No credential refuses a MODEL RUN and nothing else.

        The wizard was the wrong instrument: it made the whole app
        conditional on a network. The gate that survives is the one on work
        that spends money, and it must name the new flow.
        """
        from cli import main as cli_main

        monkeypatch.setattr(
            "cli.deps.get_run_task",
            lambda: (_ for _ in ()).throw(AssertionError("no run without credentials")),
        )
        code = cli_main.main(["fix", "--repo", str(env["root"]), "--issue", "broken"])
        err = capsys.readouterr().err
        assert code == 4
        assert "neo connect" in err

    def test_the_first_run_hint_names_the_command_and_offers_the_escape(self, env):
        hint = auth.first_run_hint()
        assert "/connect" in hint
        assert "offline" in hint.lower()
        assert not auth.looks_like_library_leak(hint)


# ---------------------------------------------------------------------------
# 2 (brief). Two providers coexist; writing one does not disturb the other.
# ---------------------------------------------------------------------------


class TestTheStoreIsAdditive:
    def test_two_providers_coexist_and_are_both_readable(self, env):
        """Two providers, one store, two entries."""
        first = auth.connect(
            "openrouter", "sk-or-first-1111", store=_store(env), verify=False
        )
        second = auth.connect(
            "local",
            "",
            store=_store(env),
            base_url="http://localhost:11434/v1",
            model="qwen2.5",
        )

        assert first.saved is True and second.saved is True
        report = auth.load_credentials(path=_store(env))
        assert report.ok is True and report.corrupt is False
        assert set(report.ids()) == {"openrouter", "local"}
        assert report.by_id("openrouter").resolved_secret() == "sk-or-first-1111"
        assert report.by_id("local").method == "none"
        assert report.active == "local"

    def test_rewriting_one_provider_leaves_the_others_byte_identical(self, env):
        """The additive property, asserted on the FILE, not on the report.

        A round-trip through a report could normalise a timestamp or reorder
        a key and still pass. Only the bytes on disk answer whether providers
        1..N-1 were disturbed.
        """
        auth.connect("openrouter", "sk-or-first-1111", store=_store(env), verify=False)
        auth.connect("local", "", store=_store(env), model="qwen2.5", verify=False)
        auth.connect("anthropic", "sk-ant-2222", store=_store(env), verify=False)
        before = json.loads(_store(env).read_text(encoding="utf-8"))["providers"]

        auth.connect("openrouter", "sk-or-REPLACED", store=_store(env), verify=False)

        after = json.loads(_store(env).read_text(encoding="utf-8"))["providers"]
        assert after["openrouter"]["secret"] == "sk-or-REPLACED"
        assert after["anthropic"] == before["anthropic"]
        assert after["local"] == before["local"]

    def test_a_trailing_slash_is_the_same_key_not_a_second_credential(self, env):
        """`gw.example///` and `gw.example` are one provider, written twice."""
        first = auth.connect(
            "other",
            "sk-a",
            store=_store(env),
            base_url="https://gw.example/v1",
            model="m",
            verify=False,
        )
        second = auth.connect(
            "other/",
            "sk-b",
            store=_store(env),
            base_url="https://gw.example/v1",
            model="m",
            verify=False,
        )
        assert auth.normalise_provider_id("gw.example///") == "gw.example"
        assert first.provider == second.provider
        report = auth.load_credentials(path=_store(env))
        assert report.ids() == (first.provider,)
        assert report.by_id(first.provider).resolved_secret() == "sk-b"

    def test_the_store_file_is_owner_only(self, env):
        """The store asks for 0600 and REPORTS the mode it got.

        POSIX-only for the equality: on Windows ``os.chmod`` only toggles the
        read-only bit, so asserting 0600 there would be a fake pass.
        """
        auth.connect("openrouter", "sk-or-mode-3333", store=_store(env), verify=False)
        report = auth.load_credentials(path=_store(env))
        assert report.mode != -1
        if os.name == "nt":
            pytest.skip(
                "POSIX-only file-mode equality; the observed mode is reported instead"
            )
        assert (_store(env).stat().st_mode & 0o777) == auth.AUTH_FILE_MODE

    def test_no_temp_file_is_left_behind_by_a_write(self, env):
        auth.connect("openrouter", "sk-or-tmp-4444", store=_store(env), verify=False)
        leftovers = [p.name for p in _store(env).parent.iterdir() if ".tmp-" in p.name]
        assert leftovers == []

    def test_four_concurrent_writers_do_not_lose_a_key(self, env):
        """Four writers on four providers: no lost update, no crash.

        The store is read-modify-write per key, so a lost update is a deleted
        credential. The credential each writer wrote must exist afterwards.
        """
        errors: List[str] = []
        names = ("openrouter", "anthropic", "gemini", "local")

        def _writer(name: str) -> None:
            try:
                auth.connect(
                    name,
                    f"sk-{name}-race",
                    store=_store(env),
                    verify=False,
                    model=f"{name}-model",
                )
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=_writer, args=(n,)) for n in names]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert errors == []
        report = auth.load_credentials(path=_store(env))
        for name in names:
            assert report.by_id(name) is not None, (
                f"{name} was lost to a concurrent write"
            )


# ---------------------------------------------------------------------------
# 3 (brief). A failing verification keeps the key.
# ---------------------------------------------------------------------------


class TestSaveFirstThenCheck:
    def test_a_failing_check_keeps_exactly_what_the_user_typed(self, env):
        """THE core inversion: a dead probe must not delete the credential."""
        result = auth.connect(
            "openrouter",
            "sk-or-typed-by-hand-5555",
            store=_store(env),
            probe=_failing_probe("timeout"),
            background=False,
        )

        assert result.saved is True
        assert result.verification is not None
        assert result.verification.ok is False
        stored = auth.load_credentials(path=_store(env)).by_id("openrouter")
        assert stored is not None, "the failing check deleted the credential"
        assert stored.resolved_secret() == "sk-or-typed-by-hand-5555"
        assert stored.model == "openai/gpt-4o-mini"

    def test_a_failing_check_never_prints_a_class_name(self, env):
        result = auth.connect(
            "openrouter",
            "sk-or-typed-6666",
            store=_store(env),
            probe=_failing_probe("timeout"),
            background=False,
        )
        receipt = "\n".join(auth.render_connect_result(result))
        assert "litellm" not in receipt.lower()
        assert "Traceback" not in receipt
        assert "Timeout" not in receipt
        assert "saved" in receipt

    def test_the_receipt_leads_with_saved_not_with_failure(self, env):
        """Ordering is the fix, so the ordering is asserted."""
        result = auth.connect(
            "openrouter",
            "sk-or-order-7777",
            store=_store(env),
            probe=_failing_probe("auth"),
            background=False,
        )
        lines = auth.render_connect_result(result)
        assert lines[0].lstrip().startswith("saved:")
        assert "check failed" in "\n".join(lines)

    def test_a_successful_check_is_recorded_on_the_entry(self, env):
        result = auth.connect(
            "openrouter",
            "sk-or-good-8888",
            store=_store(env),
            probe=_ok_probe(),
            background=False,
        )
        assert result.verification.ok is True
        assert (
            auth.load_credentials(path=_store(env)).by_id("openrouter").verified is True
        )

    def test_a_background_check_never_blocks_the_caller(self, env):
        """The receipt comes back before the check has finished."""
        released = threading.Event()

        def _slow(request, timeout_s):
            released.wait(20.0)
            return {"ok": True, "checked": True, "kind": "", "elapsed_ms": 1.0}

        try:
            started = time.monotonic()
            result = auth.connect(
                "openrouter",
                "sk-or-bg-9999",
                store=_store(env),
                probe=_slow,
                background=True,
            )
            elapsed = time.monotonic() - started
            assert result.saved is True
            assert result.check is not None
            assert result.verification is None
            assert elapsed < 5.0, (
                f"the connect receipt waited {elapsed:.1f}s for the check"
            )
            # The key is already on disk even though the check has not answered.
            assert (
                auth.load_credentials(path=_store(env)).by_id("openrouter") is not None
            )
        finally:
            released.set()

    def test_a_background_check_that_never_answers_reports_nothing_rather_than_lying(
        self, env
    ):
        """`None` is the honest answer for "still checking"."""
        credential = auth.Credential(
            provider="openrouter",
            method="api_key",
            secret="sk-x",
            model="m",
            route="openai",
        )
        released = threading.Event()
        handle = auth.BackgroundCheck(
            credential, probe=lambda r, t: released.wait(10.0) or {}
        )
        try:
            assert handle.done() is False
            assert handle.result(timeout=0.2) is None
        finally:
            released.set()
        handle.result(timeout=10.0)

    def test_the_check_runs_in_a_child_process_so_it_cannot_print_into_the_app(
        self, env, tmp_path
    ):
        """A banner must not reach the terminal, and the flow must not wait.

        A fake ``litellm`` is installed on the child's ``PYTHONPATH``. It
        prints a banner, then raises the real library's timeout. The parent's
        captured stdout must be EMPTY, and the receipt must come back through
        the pipe as one JSON line.
        """
        fake = tmp_path / "fakevendor"
        fake.mkdir()
        (fake / "litellm.py").write_text(
            textwrap.dedent(
                """
                class Timeout(Exception):
                    def __init__(self, message="Request timed out.", model="m", llm_provider="p"):
                        super().__init__(message)
                        self.model = model
                        self.llm_provider = llm_provider


                def completion(**kwargs):
                    print("Provider List: https://docs.litellm.ai/docs/providers")
                    print("LiteLLM.Info: this banner is not a receipt", file=__import__("sys").stderr)
                    raise Timeout("Request timed out.")


                suppress_debug_info = False
                set_verbose = True
                """
            ),
            encoding="utf-8",
        )
        credential = auth.Credential(
            provider="openrouter",
            method="api_key",
            secret="sk-banner-test",
            model="openai/gpt-4o-mini",
            route="openai",
        )
        request = auth._probe_request(credential, timeout_s=5)
        root = str(REPO_ROOT)
        saved_pythonpath = os.environ.get("PYTHONPATH", "")
        os.environ["PYTHONPATH"] = (
            f"{root}{os.pathsep}{fake}{os.pathsep}{saved_pythonpath}"
        )
        try:
            import contextlib as _contextlib
            import io as _io

            captured = _io.StringIO()
            with _contextlib.redirect_stdout(captured):
                raw = auth._subprocess_probe(request, 5)
        finally:
            os.environ["PYTHONPATH"] = saved_pythonpath

        assert captured.getvalue() == "", (
            "the provider banner reached the parent's stdout"
        )
        assert raw["ok"] is False
        assert raw["kind"] == "error"
        # The child's own exception crossed the pipe as its repr; the parent
        # re-classifies it and never shows it.
        assert "Timeout" in repr(raw.get("error"))

    def test_a_banner_printed_by_a_provider_never_reaches_the_receipt(
        self, env, monkeypatch
    ):
        """The in-process probe discards what a provider printed."""
        import litellm

        def _banner_then_fail(**kwargs):
            print("Provider List: https://docs.litellm.ai/docs/providers")
            raise litellm.Timeout(
                message="Request timed out.", model="m", llm_provider="p"
            )

        monkeypatch.setattr(litellm, "completion", _banner_then_fail)
        credential = auth.Credential(
            provider="openrouter",
            method="api_key",
            secret="sk-x",
            model="m",
            route="openai",
        )
        captured = []
        real_stdout = sys.stdout

        class _Recorder:
            def write(self, _text):
                captured.append("wrote-to-stdout")

            def flush(self):
                return None

        monkeypatch.setattr(auth.sys, "__stdout__", _Recorder())
        monkeypatch.setattr(sys, "stdout", real_stdout)
        receipt = auth._in_process_probe(auth._probe_request(credential, timeout_s=1))
        line = "".join(str(v) for v in captured)
        assert "Provider List" not in line
        assert receipt["ok"] is False
        assert receipt["kind"] == "error"


# ---------------------------------------------------------------------------
# 4 (brief). A timeout produces a plain sentence containing no library name.
# ---------------------------------------------------------------------------


class TestPlainLanguageOnly:
    def test_a_real_timeout_exception_becomes_one_sentence(self, env):
        litellm = pytest.importorskip("litellm")
        exc = litellm.Timeout(message="Request timed out.", model="m", llm_provider="p")
        got = auth.classify_failure(exc, "openrouter", timeout_s=60)
        assert got.kind == "timeout"
        assert "60s" in got.sentence
        assert "OpenRouter" in got.sentence
        for banned in (
            "litellm",
            "Traceback",
            "Timeout",
            "APITimeoutError",
            "Exception",
        ):
            assert banned not in got.sentence
        assert not auth.looks_like_library_leak(got.sentence)

    def test_the_documented_timeout_sentence_is_the_one_a_user_reads(self, env):
        got = auth.classify_failure(
            "litellm.Timeout: APITimeoutError", "openrouter", timeout_s=60
        )
        assert got.sentence == (
            "OpenRouter didn't answer in 60s. Free-tier models are often busy "
            "— try again or pick another model."
        )

    def test_every_library_failure_maps_to_a_kind_and_a_usable_sentence(self, env):
        for text in PROVIDER_FAILURE_TEXTS:
            got = auth.classify_failure(text, "openrouter", timeout_s=60)
            assert got.kind in auth.FAILURE_KINDS, text
            assert got.sentence.endswith(".") or got.sentence.endswith("]"), text
            assert not auth.looks_like_library_leak(got.sentence), (text, got.sentence)

    def test_every_kind_for_every_shipped_provider_is_plain(self, env):
        checked = 0
        for provider in auth.provider_list():
            for kind in auth.FAILURE_KINDS:
                sentence = auth.classify_failure(kind, provider).sentence
                assert not auth.looks_like_library_leak(sentence), (provider.id, kind)
                assert "{" not in sentence, (provider.id, kind)
                checked += 1
        assert checked == len(auth.provider_list()) * len(auth.FAILURE_KINDS)

    def test_the_leak_detector_catches_the_shapes_it_claims_to(self, env):
        for leak in PROVIDER_FAILURE_TEXTS:
            if not leak:
                continue
            if "Provider List" in leak:
                continue  # a banner is not classified text; it is captured
            assert auth.looks_like_library_leak(leak), leak
        for clean in (
            "OpenRouter didn't answer in 60s.",
            "Check you copied the whole key.",
            "Could not reach that provider.",
            "The model library is not installed, so no request was made.",
        ):
            assert not auth.looks_like_library_leak(clean), clean

    def test_no_user_facing_string_ever_names_a_library_or_a_class(self, env):
        """Every renderer, over every failure, is swept for the leak.

        This is the assertion the brief asks for, applied to OUTPUT rather
        than to a source grep: a source grep cannot tell a forbidden word in
        a sentence from the same word in the table that detects it.
        """
        for text in PROVIDER_FAILURE_TEXTS:
            for provider in auth.provider_list():
                classification = auth.classify_failure(text, provider, timeout_s=60)
                surfaces = (
                    auth.render_verification(
                        auth.Verification(
                            ok=False,
                            checked=True,
                            kind=classification.kind,
                            sentence=classification.sentence,
                            provider=provider.id,
                        )
                    ),
                    auth.render_connect_result(
                        auth.ConnectResult(
                            provider=provider.id,
                            provider_name=provider.name,
                            method="api_key",
                            saved=False,
                            path=Path("auth.json"),
                            store_error=classification.sentence,
                        )
                    ),
                    [
                        auth.status_line(
                            auth.Credentials(
                                providers=(
                                    auth.Credential(
                                        provider=provider.id,
                                        method="api_key",
                                        secret="sk-" + classification.kind,
                                        model=classification.sentence[:20],
                                        route="openai",
                                    ),
                                ),
                                active=provider.id,
                                path=Path("auth.json"),
                            )
                        )
                    ],
                    auth.render_list(
                        auth.Credentials(
                            providers=(
                                auth.Credential(
                                    provider=provider.id,
                                    method="api_key",
                                    secret="sk-" + classification.kind,
                                    model="m",
                                    route="openai",
                                ),
                            ),
                            active=provider.id,
                            path=Path("auth.json"),
                        )
                    ),
                )
                for lines in surfaces:
                    for line in lines:
                        assert "litellm" not in line.lower(), line
                        assert "Traceback" not in line, line
                        assert not auth._EXCEPTION_CLASS_RE.search(line), line

    def test_classification_is_deterministic_for_one_input(self, env):
        first = auth.classify_failure(PROVIDER_FAILURE_TEXTS[0], "openrouter")
        for _ in range(5):
            again = auth.classify_failure(PROVIDER_FAILURE_TEXTS[0], "openrouter")
            assert (again.kind, again.sentence) == (first.kind, first.sentence)

    def test_a_classifier_that_gets_nothing_usable_still_answers(self, env):
        for value in (None, "", 0, [], {}, object()):
            got = auth.classify_failure(value, "openrouter")
            assert got.kind in auth.FAILURE_KINDS
            assert not auth.looks_like_library_leak(got.sentence)

    def test_an_auth_failure_is_never_told_to_try_again(self, env):
        """A rejected key and a busy free tier need OPPOSITE advice.

        Telling a user to retry a 401 is how an afternoon disappears, so the
        ordering of the hint table is itself pinned.
        """
        auth_failure = auth.classify_failure(
            "litellm.AuthenticationError: 401", "openrouter"
        )
        timeout = auth.classify_failure(
            "litellm.Timeout: APITimeoutError", "openrouter"
        )
        assert auth_failure.kind == "auth"
        assert timeout.kind == "timeout"
        assert "copied the whole key" in auth_failure.sentence
        assert "try again" not in auth_failure.sentence.lower()


# ---------------------------------------------------------------------------
# 5 (brief). Logout removes exactly one provider.
# ---------------------------------------------------------------------------


class TestLogoutRemovesExactlyOne:
    def test_logout_removes_one_and_reports_what_remains(self, env):
        auth.connect("openrouter", "sk-or-out-1111", store=_store(env), verify=False)
        auth.connect("anthropic", "sk-ant-out-2222", store=_store(env), verify=False)
        auth.connect("gemini", "gemini-out-3333", store=_store(env), verify=False)

        report = auth.remove_credential("anthropic", path=_store(env))

        assert set(report.ids()) == {"openrouter", "gemini"}
        assert report.by_id("anthropic") is None
        assert report.by_id("openrouter").resolved_secret() == "sk-or-out-1111"
        assert report.by_id("gemini").resolved_secret() == "gemini-out-3333"
        assert report.active in {"openrouter", "gemini"}

    def test_logout_moves_the_active_marker_off_the_removed_provider(self, env):
        auth.connect("openrouter", "sk-active-1111", store=_store(env), verify=False)
        assert auth.load_credentials(path=_store(env)).active == "openrouter"
        report = auth.remove_credential("openrouter/", path=_store(env))
        assert report.active is None
        assert report.ids() == ()

    def test_logout_of_something_not_connected_removes_nothing(self, env):
        auth.connect("openrouter", "sk-keep-1111", store=_store(env), verify=False)
        before = _store(env).read_text(encoding="utf-8")
        args = SimpleNamespace(provider="nope", json=False)
        assert auth.cmd_auth_logout(args) == 2
        assert _store(env).read_text(encoding="utf-8") == before

    def test_logout_with_no_provider_named_is_a_usage_error(self, env):
        assert auth.cmd_auth_logout(SimpleNamespace(provider="", json=False)) == 2

    def test_the_logout_receipt_names_the_remainder(self, env, capsys):
        auth.connect("openrouter", "sk-a-1111", store=_store(env), verify=False)
        auth.connect("anthropic", "sk-b-2222", store=_store(env), verify=False)
        code = auth.cmd_auth_logout(SimpleNamespace(provider="openrouter", json=False))
        out = capsys.readouterr().out
        assert code == 0
        assert "removed openrouter" in out
        assert "still connected: anthropic" in out
        assert "sk-a-1111" not in out
        assert "sk-b-2222" not in out

    def test_emptying_the_store_says_offline_still_works(self, env, capsys):
        auth.connect("openrouter", "sk-only-1111", store=_store(env), verify=False)
        auth.cmd_auth_logout(SimpleNamespace(provider="openrouter", json=False))
        assert "offline" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 6 (brief). A custom OpenAI-compatible endpoint with {env:VAR}.
# ---------------------------------------------------------------------------


class TestCustomEndpointAndEnvIndirection:
    def test_a_custom_endpoint_works_through_an_env_reference(self, env, monkeypatch):
        """The reference is stored; the value is resolved at RUN time."""
        monkeypatch.setenv("ACME_GATEWAY_KEY", "sk-acme-live-7777")
        result = auth.connect(
            "other",
            "{env:ACME_GATEWAY_KEY}",
            store=_store(env),
            base_url="https://gateway.acme.test/v1",
            model="acme-large",
            probe=_ok_probe(),
            background=False,
        )

        assert result.saved is True
        assert result.provider == "gateway.acme.test"
        assert result.base_url == "https://gateway.acme.test/v1"
        assert result.secret_source == "env:ACME_GATEWAY_KEY"
        raw = _store(env).read_text(encoding="utf-8")
        assert "sk-acme-live-7777" not in raw, "the literal key reached the store"
        assert "{env:ACME_GATEWAY_KEY}" in raw
        overlay = auth.apply_active_credential({})
        assert overlay["api_key"] == "sk-acme-live-7777"
        assert overlay["api_base"] == "https://gateway.acme.test/v1"
        assert overlay["model"] == "acme-large"

    def test_the_env_method_accepts_a_bare_variable_name(self, env, monkeypatch):
        """`--method env ACME_TWO` is stored as a reference, not a literal."""
        monkeypatch.setenv("ACME_TWO", "sk-acme-two-8888")
        (env["home"] / auth.PROVIDERS_FILENAME).write_text(
            json.dumps(
                {
                    "providers": [
                        {
                            "id": "acme",
                            "name": "ACME",
                            "methods": ["api_key", "env"],
                            "key_url": "https://acme.test/keys",
                            "priority": 1,
                            "default_model": "acme-m",
                            "base_url": "https://acme.test/v1",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        result = auth.connect(
            "acme",
            "ACME_TWO",
            method="env",
            store=_store(env),
            verify=False,
        )
        assert result.saved is True
        assert result.method == "env"
        assert result.secret_source == "env:ACME_TWO"
        assert "sk-acme-two-8888" not in _store(env).read_text(encoding="utf-8")
        assert auth.apply_active_credential({})["api_key"] == "sk-acme-two-8888"

    def test_a_method_the_provider_does_not_declare_is_refused(self, env):
        result = auth.connect(
            "openrouter", "sk-wrong-method-1", method="env", store=_store(env)
        )
        assert result.saved is False
        assert "does not use" in result.store_error
        assert auth.load_credentials(path=_store(env)).ids() == ()

    def test_an_unset_reference_resolves_to_nothing_not_to_the_reference_text(
        self, env, monkeypatch
    ):
        """The literal text `{env:X}` must never be sent to a provider as a key."""
        monkeypatch.delenv("ACME_ABSENT", raising=False)
        auth.connect(
            "other",
            "{env:ACME_ABSENT}",
            store=_store(env),
            base_url="https://absent.acme.test/v1",
            model="m",
            verify=False,
        )
        overlay = auth.apply_active_credential({})
        assert "api_key" not in overlay
        assert "{env:" not in json.dumps(overlay)

    def test_no_literal_key_is_ever_written_into_a_settings_file(self, env, capsys):
        auth.connect("openrouter", "sk-or-config-9999", store=_store(env), verify=False)
        secret = "sk-or-config-9999"
        for path in Path(env["root"]).rglob("settings*.toml"):
            assert secret not in path.read_text(encoding="utf-8"), path
        for path in Path(env["root"]).rglob("config.toml"):
            assert secret not in path.read_text(encoding="utf-8"), path
        assert "openai/gpt-4o-mini" in auth.apply_active_credential({})["model"]

    def test_the_settings_mirror_records_the_endpoint_and_model_only(self, env):
        from cli import neoconfig

        auth.connect(
            "other",
            "sk-mirror-1234",
            store=_store(env),
            base_url="https://mirror.acme.test/v1",
            model="mirror-model",
            verify=False,
        )
        merged = neoconfig.merged_settings() or {}
        assert merged.get("base_url") == "https://mirror.acme.test/v1"
        assert merged.get("model") == "mirror-model"
        assert "api_key" not in merged
        assert "sk-mirror-1234" not in json.dumps(merged, default=str)

    def test_the_overlay_never_overrides_what_the_caller_asked_for(
        self, env, monkeypatch
    ):
        monkeypatch.setenv("ACME_OVERRIDE", "sk-store-1111")
        auth.connect(
            "other",
            "{env:ACME_OVERRIDE}",
            store=_store(env),
            base_url="https://store.acme.test/v1",
            model="store-model",
            verify=False,
        )
        overlay = auth.apply_active_credential(
            {
                "api_key": "sk-explicit",
                "model": "explicit-model",
                "api_base": "https://flag",
            }
        )
        assert overlay["api_key"] == "sk-explicit"
        assert overlay["model"] == "explicit-model"
        assert overlay["api_base"] == "https://flag"

    def test_the_overlay_returns_a_new_dict_and_never_mutates_the_caller(self, env):
        auth.connect("openrouter", "sk-immutable-1", store=_store(env), verify=False)
        original = {"budget_cap_usd": 5.0}
        overlay = auth.apply_active_credential(original)
        assert original == {"budget_cap_usd": 5.0}
        assert overlay is not original

    def test_a_local_endpoint_needs_no_key_and_says_so(self, env):
        result = auth.connect(
            "local",
            "",
            store=_store(env),
            base_url="http://localhost:11434/v1",
            model="qwen2.5",
        )
        assert result.saved is True
        assert result.method == "none"
        assert result.check is None
        receipt = "\n".join(auth.render_connect_result(result))
        assert "local endpoint" in receipt
        assert "nothing to check over a network" in receipt

    def test_a_key_pasted_to_a_keyless_endpoint_is_said_to_be_discarded(self, env):
        """A silently discarded paste is the same bug as a discarded key."""
        result = auth.connect(
            "local",
            "sk-pasted-1",
            store=_store(env),
            base_url="http://localhost:11434/v1",
            model="qwen2.5",
        )
        stored = auth.load_credentials(path=_store(env)).by_id("local")
        assert stored.secret == ""
        assert "nothing was stored for the key" in "\n".join(
            auth.render_connect_result(result)
        )

    def test_a_remote_provider_with_no_key_is_refused_with_a_sentence(self, env):
        result = auth.connect("openrouter", "", store=_store(env))
        assert result.saved is False
        assert "needs a key" in result.store_error
        assert auth.load_credentials(path=_store(env)).ids() == ()


# ---------------------------------------------------------------------------
# 7 (brief). A corrupt store is reported rather than silently reset.
# ---------------------------------------------------------------------------


CORRUPT_STORES = {
    "not_json": "{ this is not json at all",
    "not_an_object": '["openrouter", "anthropic"]',
    "wrong_version": json.dumps({"schema_version": 99, "providers": {}}),
    "no_providers": json.dumps({"schema_version": 1, "active": None}),
    "providers_not_a_dict": json.dumps({"schema_version": 1, "providers": []}),
    "empty_file": "",
}


class TestACorruptStoreIsReported:
    @pytest.mark.parametrize("name", sorted(CORRUPT_STORES))
    def test_each_corrupt_shape_is_reported_and_the_bytes_are_untouched(
        self, env, name
    ):
        payload = CORRUPT_STORES[name]
        _store(env).write_text(payload, encoding="utf-8")

        report = auth.load_credentials(path=_store(env))

        assert report.ok is False
        assert report.corrupt is True
        assert report.error, "a corrupt store must carry a reason"
        assert not auth.looks_like_library_leak(report.error)
        assert _store(env).read_text(encoding="utf-8") == payload, "the store was reset"

    @pytest.mark.parametrize("name", sorted(CORRUPT_STORES))
    def test_connecting_onto_a_corrupt_store_refuses_instead_of_clobbering(
        self, env, name
    ):
        payload = CORRUPT_STORES[name]
        _store(env).write_text(payload, encoding="utf-8")

        result = auth.connect(
            "openrouter", "sk-on-corrupt", store=_store(env), verify=False
        )

        assert result.saved is False
        assert "nothing was changed" in result.store_error.lower()
        assert _store(env).read_text(encoding="utf-8") == payload

    def test_logout_onto_a_corrupt_store_refuses_instead_of_clobbering(
        self, env, capsys
    ):
        payload = CORRUPT_STORES["not_json"]
        _store(env).write_text(payload, encoding="utf-8")
        code = auth.cmd_auth_logout(SimpleNamespace(provider="openrouter", json=False))
        assert code == 2
        assert _store(env).read_text(encoding="utf-8") == payload
        assert "litellm" not in capsys.readouterr().out.lower()

    def test_the_corrupt_message_names_the_file_and_the_way_out(self, env):
        _store(env).write_text(CORRUPT_STORES["not_json"], encoding="utf-8")
        report = auth.load_credentials(path=_store(env))
        assert "auth.json" in report.error
        assert "Move it aside" in report.error

    def test_a_missing_store_is_first_run_not_corruption(self, env):
        report = auth.load_credentials(path=_store(env))
        assert report.ok is True
        assert report.corrupt is False
        assert report.providers == ()

    def test_one_damaged_entry_is_skipped_and_the_rest_still_work(self, env):
        auth.connect("openrouter", "sk-good-1111", store=_store(env), verify=False)
        auth.connect("anthropic", "sk-good-2222", store=_store(env), verify=False)
        data = json.loads(_store(env).read_text(encoding="utf-8"))
        data["providers"]["broken"] = {"method": "carrier-pigeon", "secret": "x"}
        data["providers"]["also-broken"] = "not even an object"
        _store(env).write_text(json.dumps(data), encoding="utf-8")

        report = auth.load_credentials(path=_store(env))

        assert report.ok is True
        assert set(report.ids()) == {"openrouter", "anthropic"}
        assert len(report.problems) == 2
        assert any("carrier-pigeon" in problem for problem in report.problems)
        rendered = "\n".join(auth.render_status(report))
        assert "note:" in rendered

    def test_an_active_pointer_at_a_missing_provider_is_reported_not_silently_accepted(
        self, env
    ):
        auth.connect("openrouter", "sk-one-1111", store=_store(env), verify=False)
        data = json.loads(_store(env).read_text(encoding="utf-8"))
        data["active"] = "vanished"
        _store(env).write_text(json.dumps(data), encoding="utf-8")
        report = auth.load_credentials(path=_store(env))
        assert report.active is None
        assert any("vanished" in problem for problem in report.problems)

    def test_a_corrupt_user_provider_list_does_not_take_the_menu_away(self, env):
        (env["home"] / auth.PROVIDERS_FILENAME).write_text("{ nope", encoding="utf-8")
        rows = auth.provider_list(home=env["home"])
        assert len(rows) == len(auth.PROVIDERS)
        _, problems = auth._user_provider_rows(env["home"])
        assert problems and "providers.json" in problems[0]


# ---------------------------------------------------------------------------
# 8 (brief). No user-facing string names a library.
#   (the swept-output version of this lives in TestPlainLanguageOnly)
# ---------------------------------------------------------------------------


class TestTheModuleItselfIsHonest:
    def test_the_store_never_prints_a_key_where_a_person_reads(self, env, capsys):
        secret = "sk-or-never-printed-4242"
        auth.connect("openrouter", secret, store=_store(env), verify=False)
        auth.cmd_auth_list(SimpleNamespace(json=False))
        auth.cmd_auth_list(SimpleNamespace(json=True))
        auth.cmd_auth_logout(SimpleNamespace(provider="openrouter", json=True))
        captured = capsys.readouterr()
        assert secret not in captured.out
        assert secret not in captured.err

    def test_the_json_document_carries_a_mask_not_a_secret(self, env, capsys):
        secret = "sk-json-masked-5252"
        auth.connect("openrouter", secret, store=_store(env), verify=False)
        assert auth.cmd_auth_list(SimpleNamespace(json=True)) == 0
        out = capsys.readouterr().out
        assert secret not in out
        document = json.loads(out)
        assert document["providers"][0]["secret_source"] == "literal"
        assert document["providers"][0]["key"] != secret
        assert "secret" not in document["providers"][0]

    def test_the_status_line_names_the_credential_the_model_and_a_masked_key(self, env):
        secret = "sk-status-6262"
        auth.connect("openrouter", secret, store=_store(env), verify=False)
        line = auth.status_line()
        assert "OpenRouter" in line
        assert "openai/gpt-4o-mini" in line
        assert secret not in line
        assert secret[-4:] in line

    def test_a_mask_never_reveals_a_short_key(self, env):
        assert auth.mask_key("abcd") == "****"
        assert auth.mask_key("") == "(not set)"
        assert auth.mask_key("{env:SOME_VAR}") == "{env:SOME_VAR}"

    def test_the_module_never_imports_the_tui_it_does_not_own(self, env):
        """`cli/auth.py` must stay importable without a Textual install.

        This terminal does not own `cli/tui.py`. An import here would make
        every credential surface depend on a module another terminal is
        rewriting, for no reason.
        """
        source = (REPO_ROOT / "cli" / "auth.py").read_text(encoding="utf-8")
        assert "import cli.tui" not in source
        assert "from cli import tui" not in source
        assert "from cli.tui" not in source

    def test_the_module_carries_no_verification_vocabulary(self, env):
        """Nothing here may claim a run succeeded, in any surface.

        `completed_unverified` is never success, and a credential receipt is
        not a place to mint a verdict about somebody's code.
        """
        source = (REPO_ROOT / "cli" / "auth.py").read_text(encoding="utf-8").lower()
        for word in ("completed_verified", "completed_unverified", "verifier"):
            assert word not in source, word

    def test_no_configuration_default_was_added_for_this_flow(self, env):
        """A value in DEFAULTS merges into every task and every eval arm.

        The verification budget is read from the environment and the settings
        chain instead, precisely so this round cannot switch every run in the
        project onto a 60-second network probe.
        """
        from harness import config as harness_config

        assert "auth_verify_timeout_s" not in harness_config.DEFAULTS
        assert "connect_timeout_s" not in harness_config.DEFAULTS

    def test_the_verification_budget_is_read_not_hardcoded(self, env, monkeypatch):
        from cli import neoconfig

        monkeypatch.setenv("NEO_CONNECT_PROBE_TIMEOUT_S", "7")
        assert auth._resolve_timeout_s() == 7
        monkeypatch.setenv("NEO_CONNECT_PROBE_TIMEOUT_S", "not-a-number")
        monkeypatch.setattr(
            neoconfig, "merged_settings", lambda *a, **k: {"auth_verify_timeout_s": 11}
        )
        assert auth._resolve_timeout_s() == 11
        monkeypatch.setattr(
            neoconfig,
            "merged_settings",
            lambda *a, **k: {"auth_verify_timeout_s": 9999},
        )
        assert auth._resolve_timeout_s() == 600  # clamped, not trusted
        monkeypatch.setattr(neoconfig, "merged_settings", lambda *a, **k: {})
        assert auth._resolve_timeout_s() == 60

    def test_the_budget_the_user_sees_is_the_budget_the_probe_gets(
        self, env, monkeypatch
    ):
        monkeypatch.setenv("NEO_CONNECT_PROBE_TIMEOUT_S", "9")
        probe = _failing_probe("timeout")
        result = auth.connect(
            "openrouter",
            "sk-budget-7272",
            store=_store(env),
            probe=probe,
            background=False,
        )
        assert probe.calls[0]["timeout_s"] == 9
        assert "9s" in result.verification.sentence


# ---------------------------------------------------------------------------
# The provider list is DATA; the menu obeys the cap and the anti-clutter rule.
# ---------------------------------------------------------------------------


class TestTheProviderTableIsData:
    def test_every_row_declares_the_fields_the_brief_names(self, env):
        for provider in auth.provider_list():
            assert provider.id
            assert provider.name
            assert provider.methods, provider.id
            assert set(provider.methods) <= set(auth.AUTH_METHODS)
            assert isinstance(provider.priority, int)
            if provider.id != auth.OTHER_PROVIDER_ID:
                assert provider.key_url, f"{provider.id} has no get-a-key URL"

    def test_the_menu_is_sorted_by_priority_then_name(self, env):
        rows = [p for p in auth.provider_prompt() if p.id != auth.OTHER_PROVIDER_ID]
        assert rows == sorted(rows, key=lambda p: (p.priority, p.name))

    def test_the_menu_shows_at_most_eight_and_other_is_last(self, env):
        rows = auth.provider_prompt()
        assert len(rows) <= auth.MAX_PROVIDERS_SHOWN == 8
        assert rows[-1].id == auth.OTHER_PROVIDER_ID
        assert len(auth.provider_list()) > auth.MAX_PROVIDERS_SHOWN, (
            "the shipped table must exceed the cap, or the cap is untested"
        )

    def test_a_hidden_provider_is_still_connectable_by_name(self, env):
        shown = {p.id for p in auth.provider_prompt()}
        hidden = [p.id for p in auth.provider_list() if p.id not in shown]
        assert hidden, "no provider is hidden, so the cap is not doing anything"
        for provider_id in hidden:
            row = auth.provider_by_id(provider_id)
            assert row is not None and row.id == provider_id
            result = auth.connect(
                provider_id, "sk-hidden-8282", store=_store(env), verify=False
            )
            assert result.saved is True
            assert (
                auth.load_credentials(path=_store(env)).by_id(result.provider)
                is not None
            )

    def test_adding_a_provider_is_a_data_edit(self, env):
        """A row in providers.json becomes a menu row. No code, no restart."""
        (env["home"] / auth.PROVIDERS_FILENAME).write_text(
            json.dumps(
                {
                    "providers": [
                        {
                            "id": "acme",
                            "name": "ACME Gateway",
                            "methods": ["api_key"],
                            "key_url": "https://acme.test/keys",
                            "priority": 5,
                            "default_model": "acme-large",
                            "env_key": "ACME_API_KEY",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        rows = auth.provider_prompt(home=env["home"])
        assert rows[0].id == "acme", "priority 5 must sort above priority 10"
        result = auth.connect("acme", "sk-acme-9393", store=_store(env), verify=False)
        assert result.saved is True
        assert result.model == "acme-large"
        assert "/connect" in auth.status_line() or result.provider == "acme"

    def test_a_user_row_replaces_a_built_in_row_of_the_same_id(self, env):
        (env["home"] / auth.PROVIDERS_FILENAME).write_text(
            json.dumps(
                {
                    "providers": [
                        {
                            "id": "openai",
                            "name": "Our OpenAI proxy",
                            "methods": ["api_key"],
                            "priority": 10,
                            "default_model": "house-model",
                            "base_url": "https://proxy.test/v1",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        row = auth.provider_by_id("openai", home=env["home"])
        assert row.name == "Our OpenAI proxy"
        assert row.default_model == "house-model"
        assert (
            len([p for p in auth.provider_list(home=env["home"]) if p.id == "openai"])
            == 1
        )

    def test_a_row_without_an_id_is_dropped_with_a_reason_not_raised(self, env):
        _extra, problems = auth._user_provider_rows_text(
            json.dumps({"providers": [{"name": "nameless"}, "not a row"]})
        )
        assert len(problems) == 2
        assert all("row" in problem for problem in problems)

    def test_a_method_the_build_does_not_know_is_ignored_not_fatal(self, env):
        _extra, problems = auth._user_provider_rows_text(
            json.dumps(
                {"providers": [{"id": "x", "methods": ["api_key", "telepathy"]}]}
            )
        )
        assert problems == []
        row = auth.provider_by_id("x")
        assert row is None  # not merged: the test only proves parsing is total


# ---------------------------------------------------------------------------
# The method step is conditional.
# ---------------------------------------------------------------------------


class TestTheAuthMethodStepIsConditional:
    def test_a_single_method_provider_never_shows_the_method_step(self, env):
        for provider in auth.provider_list():
            if len(provider.methods) == 1:
                assert auth.method_prompt(provider) is None
                assert auth.render_method_prompt(provider) == []
                assert auth.needs_method_choice(provider) is False

    def test_a_multi_method_provider_shows_the_method_step(self, env):
        _declare_acme_two_methods(env)
        provider = auth.provider_by_id("acme", home=env["home"])
        assert auth.needs_method_choice(provider) is True
        assert auth.method_prompt(provider) == ("api_key", "api_base")
        lines = auth.render_method_prompt(provider)
        assert "ACME" in lines[0]
        assert "api_key" in "\n".join(lines)
        assert "api_base" in "\n".join(lines)

    def test_a_multi_method_provider_refuses_to_guess(self, env):
        """Choosing nothing is a question, not a default.

        Guessing here would write the wrong credential shape and the user
        would find out at the first model call.
        """
        _declare_acme_two_methods(env)
        provider = auth.provider_by_id("acme", home=env["home"])
        assert auth._pick_method(provider, None) is None
        assert auth._pick_method(provider, "api_base") == "api_base"
        with pytest.raises(ValueError):
            auth._pick_method(provider, "telepathy")
        result = auth.connect("acme", "sk-guess-1", store=_store(env))
        assert result.saved is False
        assert "more than one way" in result.store_error
        assert auth.load_credentials(path=_store(env)).ids() == ()

    def test_a_single_method_provider_never_asks(self, env):
        provider = auth.provider_by_id("openrouter")
        assert auth._pick_method(provider, None) == "api_key"


# ---------------------------------------------------------------------------
# The anti-clutter rule.
# ---------------------------------------------------------------------------


class TestTheAntiClutterRule:
    def test_a_section_with_two_or_fewer_entries_renders_nothing(self, env):
        assert auth.section_lines([], min_entries=3) == []
        assert auth.section_lines(["one"], min_entries=3) == []
        assert auth.section_lines(["one", "two"], min_entries=3) == []

    def test_a_section_with_three_entries_renders(self, env):
        lines = auth.section_lines(["one", "two", "three"], min_entries=3, title="also")
        assert lines[0].startswith("also")
        assert len(lines) == 4

    def test_the_status_block_omits_the_also_connected_section_below_three(self, env):
        """The active one is always a fact; the OTHERS need three to be a list."""
        for name, secret in (("openrouter", "sk-1"), ("anthropic", "sk-2")):
            auth.connect(name, secret, store=_store(env), verify=False)
        report = auth.load_credentials(path=_store(env))
        rendered = "\n".join(auth.render_status(report))
        assert "also connected" not in rendered
        active = report.active_credential()
        assert auth.provider_by_id(active.provider).name in rendered
        assert "connected:" in rendered
        # Three total = two others, still not a list.
        auth.connect("gemini", "sk-3", store=_store(env), verify=False)
        rendered = "\n".join(
            auth.render_status(auth.load_credentials(path=_store(env)))
        )
        assert "also connected" not in rendered
        # Four total = three others, which is a list.
        auth.connect("groq", "sk-4", store=_store(env), verify=False)
        rendered = "\n".join(
            auth.render_status(auth.load_credentials(path=_store(env)))
        )
        assert "also connected" in rendered

    def test_an_empty_store_gets_one_honest_line_not_a_panel(self, env):
        rendered = auth.render_status(auth.load_credentials(path=_store(env)))
        assert len(rendered) == 1
        assert "/connect" in rendered[0]

    def test_a_corrupt_store_gets_a_reason_and_no_fake_list(self, env):
        _store(env).write_text("{ broken", encoding="utf-8")
        rendered = auth.render_status(auth.load_credentials(path=_store(env)))
        assert len(rendered) == 1
        assert "unreadable" in rendered[0]


# ---------------------------------------------------------------------------
# Markup safety: every untrusted string is escaped at the boundary.
# ---------------------------------------------------------------------------


class TestUntrustedStringsNeverBecomeMarkup:
    def test_markup_safe_neutralises_a_hostile_provider_name(self, env):
        """rich escapes the OPENING bracket; that is what stops the parse.

        The test is on the real parser's contract, not on a substring count:
        an escaped `[red]` still contains the characters `[red]`, so asserting
        their absence would be asserting that escaping removes text — which is
        the opposite of what it must do.
        """
        from rich.console import Console
        from rich.text import Text

        escaped = auth.markup_safe("[red]pwned[/red] provider")
        assert "\\[red]" in escaped
        assert "pwned" in escaped, "escaping must not delete the message"
        console = Console(record=True, width=100, no_color=True)
        with console.capture() as capture:
            console.print(escaped, highlight=False)
        assert "pwned" in capture.get()
        assert Text.from_markup(escaped).plain == "[red]pwned[/red] provider"

    def test_a_hostile_provider_name_reaches_a_rich_console_as_text(self, env):
        """The real parser, and the rest of the panel must survive.

        A render failure that DELETES a message is the failure mode this
        guards. So the assertions are: rich does not raise, the hostile text
        is VISIBLE (not swallowed), and the seven rows after it still print —
        because a MarkupError on row 1 would empty the panel.
        """
        from rich.console import Console

        (env["home"] / auth.PROVIDERS_FILENAME).write_text(
            json.dumps(
                {
                    "providers": [
                        {
                            "id": "evil",
                            "name": "[/] evil [red]name",
                            "methods": ["api_key"],
                            "key_url": "https://evil.test/keys",
                            "priority": 1,
                            "default_model": "[bold]m[/bold]",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        console = Console(record=True, width=100, no_color=True)
        with console.capture() as capture:
            for line in auth.render_provider_menu(home=env["home"]):
                console.print(auth.markup_safe(line), highlight=False)
            for line in auth.render_connect_result(
                auth.ConnectResult(
                    provider="evil",
                    provider_name="[/] evil [red]name",
                    method="api_key",
                    saved=True,
                    path=Path("auth.json"),
                    model="[bold]m[/bold]",
                    masked="sk-…0000",
                )
            ):
                console.print(auth.markup_safe(line), highlight=False)
        text = capture.get()
        assert "evil" in text, "the hostile name was swallowed instead of shown"
        assert "OpenAI" in text, "a MarkupError emptied the rest of the panel"
        assert "Anthropic" in text
        assert "saved" in text

    def test_a_hostile_model_id_survives_the_status_line(self, env):
        auth.connect(
            "other",
            "sk-hostile-1",
            store=_store(env),
            base_url="https://hostile.test/v1",
            model="[red]m[/red]",
        )
        line = auth.status_line()
        assert "[red]" in line  # plain text, not markup
        assert auth.markup_safe(line) != line


# ---------------------------------------------------------------------------
# One backend, two surfaces.
# ---------------------------------------------------------------------------


class TestBothSurfacesShareOneBackend:
    def test_the_slash_command_is_registered_with_a_headless_equivalent(self, env):
        spec = commands_mod.command_spec("/connect")
        assert spec is not None
        assert spec.aliases == ("/auth",)
        assert commands_mod.command_spec("/auth").name == "/connect"
        assert commands_mod.headless_policy("/connect") == "flag-only"
        assert commands_mod.headless_equivalent("/connect") == "neo connect"
        assert "/connect" in commands_mod.BUILTIN_SLASH_COMMANDS

    def test_connect_is_allowed_while_a_run_is_live(self, env):
        """Auth is the control a person reaches for mid-run.

        Refusing it in flight is the same mistake as refusing `/effort`: the
        failure it fixes is usually WHY the run is failing.
        """
        availability = commands_mod.command_availability(
            commands_mod.command_spec("/connect"),
            commands_mod.CommandContext(surface="tui", in_flight=True),
        )
        assert availability.available is True

    def test_the_registry_validation_still_passes_with_the_new_row(self, env):
        commands_mod._validate_headless_tables()

    def test_the_cli_reaches_the_same_backend(self, env, monkeypatch, capsys):
        from cli import main as cli_main

        code = cli_main.main(
            [
                "connect",
                "openrouter",
                "--api-key",
                "sk-cli-shared-1",
                "--model",
                "openai/gpt-4o-mini",
                "--no-verify",
            ]
        )
        assert code == 0
        stored = auth.load_credentials(path=_store(env)).by_id("openrouter")
        assert stored is not None
        assert stored.resolved_secret() == "sk-cli-shared-1"
        assert "saved" in capsys.readouterr().out

    def test_the_cli_and_the_in_session_flow_write_the_same_shape(
        self, env, monkeypatch
    ):
        # pick row 1 (OpenAI), paste a key, accept the default model
        answers = iter(["1", "sk-interactive-1", ""])
        auth.connect_interactive(
            store=_store(env),
            input_fn=lambda prompt="": next(answers),
            getpass_fn=lambda prompt="": next(answers),
            say=lambda line: None,
            probe=_failing_probe("timeout"),
            background=False,
        )
        interactive_shape = json.loads(_store(env).read_text(encoding="utf-8"))[
            "providers"
        ]

        other = _store(env).parent / "other.json"
        auth.connect("openai", "sk-cli-shape-1", store=other, verify=False)
        cli_shape = json.loads(other.read_text(encoding="utf-8"))["providers"]

        assert set(interactive_shape) == set(cli_shape) == {"openai"}
        assert set(interactive_shape["openai"]) == set(cli_shape["openai"])
        assert interactive_shape["openai"]["method"] == "api_key"

    def test_a_failed_check_through_the_cli_still_exits_with_the_key_saved(
        self, env, monkeypatch
    ):
        from cli import main as cli_main

        monkeypatch.setattr(
            auth,
            "_subprocess_probe",
            lambda request, timeout_s: {
                "ok": False,
                "checked": True,
                "kind": "timeout",
            },
        )
        code = cli_main.main(
            [
                "connect",
                "openrouter",
                "--api-key",
                "sk-cli-fail-1",
                "--model",
                "openai/gpt-4o-mini",
            ]
        )
        assert code == 4
        assert auth.load_credentials(path=_store(env)).by_id("openrouter") is not None

    def test_neo_connect_with_no_arguments_and_no_tty_is_a_clean_usage_error(
        self, env, monkeypatch
    ):
        from cli import main as cli_main

        class _Stdin:
            def isatty(self):
                return False

        monkeypatch.setattr(sys, "stdin", _Stdin())
        assert cli_main.main(["connect"]) == 2


# ---------------------------------------------------------------------------
# The interactive flow's step count.
# ---------------------------------------------------------------------------


class TestTheInteractiveFlowAsksOnlyWhatItMust:
    def _run(self, env, answers, **kwargs):
        asked: List[str] = []
        lines: List[str] = []
        queue = list(answers)

        def _input(prompt: str = "") -> str:
            asked.append(prompt)
            return queue.pop(0) if queue else ""

        result = auth.connect_interactive(
            store=_store(env),
            input_fn=_input,
            getpass_fn=_input,
            say=lines.append,
            verify=False,
            **kwargs,
        )
        return result, asked, lines

    def test_a_single_method_provider_is_never_asked_for_a_method(self, env):
        result, asked, _lines = self._run(env, ["4", "sk-flow-1", ""])
        assert result is not None
        assert not any("Auth method" in prompt for prompt in asked)

    def test_a_multi_method_provider_is_asked_once(self, env):
        _declare_acme_two_methods(env)
        result, asked, _lines = self._run(
            env, ["1", "api_base", "https://acme.test/v1", "sk-acme-1", ""]
        )
        assert result is not None
        assert sum("Auth method" in prompt for prompt in asked) == 1
        assert result.method == "api_base"
        assert result.base_url == "https://acme.test/v1"

    def test_skipping_changes_nothing_and_says_so(self, env):
        result, _asked, lines = self._run(env, ["/skip"])
        assert result is None
        assert not _store(env).exists()
        assert "nothing was changed" in "\n".join(lines).lower()

    def test_the_prompt_says_skipping_is_allowed(self, env):
        _result, _asked, lines = self._run(env, ["4", "sk-flow-2", ""])
        menu = "\n".join(lines)
        assert "/skip" in menu
        assert "offline" in menu.lower()

    def test_a_number_or_a_name_both_pick_a_provider(self, env):
        by_number, _a, _l = self._run(env, ["4", "sk-num-1", ""])
        assert by_number.provider == "openrouter"
        by_name, _a, _l = self._run(env, ["openrouter", "sk-name-1", ""])
        assert by_name.provider == "openrouter"
        assert by_name.saved is True

    def test_an_unknown_pick_falls_through_to_a_custom_endpoint(self, env):
        """The `other` row is a MENU ENTRY; the store key comes from the host.

        Two gateways must be two credentials, so a custom endpoint is keyed
        by its host rather than all landing on one `other` row.
        """
        result, _asked, lines = self._run(
            env, ["not-a-provider", "https://x.test/v1", "x-model", "sk-unknown-1", ""]
        )
        assert result is not None
        assert result.provider == "x.test"
        assert result.base_url == "https://x.test/v1"
        assert "not on the list" in "\n".join(lines)
        assert auth.load_credentials(path=_store(env)).by_id("x.test") is not None

    def test_two_custom_endpoints_are_two_credentials(self, env):
        first, _a, _l = self._run(
            env, ["other", "https://one.test/v1", "m1", "sk-one-1", ""]
        )
        second, _a, _l = self._run(
            env, ["other", "https://two.test/v1", "m2", "sk-two-1", ""]
        )
        assert first.provider == "one.test"
        assert second.provider == "two.test"
        assert set(auth.load_credentials(path=_store(env)).ids()) == {
            "one.test",
            "two.test",
        }

    def test_the_custom_entry_without_an_address_refuses_with_a_sentence(self, env):
        result = auth.connect("other", "sk-no-address-1", store=_store(env))
        assert result.saved is False
        assert "needs its address" in result.store_error
        assert auth.load_credentials(path=_store(env)).ids() == ()


# ---------------------------------------------------------------------------
# Placement: the store, the parser, and the active pointer.
# ---------------------------------------------------------------------------


class TestPlacement:
    def test_a_receipt_line_that_can_be_short_is_short(self, env):
        """The `auth` sentence and every pointer line fit 80 columns.

        Measured, not eyeballed: the receipt prefix is 16 columns, so a
        sentence longer than 64 wraps and a URL appended to a sentence wraps
        the sentence itself. The two-sentence explanations DO wrap, which is
        acceptable; what is not acceptable is a half-URL on its own line, so
        the key pointer is its own row.
        """
        from rich.console import Console

        console = Console(width=80, no_color=True)
        for provider in auth.provider_list():
            for kind in ("auth",):
                verification = auth.Verification(
                    ok=False,
                    checked=True,
                    kind=kind,
                    sentence=auth.classify_failure(kind, provider).sentence,
                    provider=provider.id,
                )
                for line in auth.render_verification(verification):
                    assert len(line) <= 80, (provider.id, kind, len(line), line)
            for line in auth.render_connect_result(
                auth.ConnectResult(
                    provider=provider.id,
                    provider_name=provider.name,
                    method="api_key",
                    saved=True,
                    path=Path("auth.json"),
                    model="m",
                    masked="sk-…0000",
                )
            ):
                with console.capture() as capture:
                    console.print(auth.markup_safe(line), highlight=False)
                assert capture.get().rstrip("\n").count("\n") == 0, (provider.id, line)

    def test_the_store_lives_under_neo_home_and_nothing_else(self, env):
        path = auth.auth_path()
        assert path.name == "auth.json"
        assert path.parent == env["home"]
        assert path.parent == Path(os.environ["NEO_HOME"])

    def test_the_parser_knows_both_command_surfaces(self, env):
        import argparse

        from cli import main as cli_main

        parser = cli_main.build_parser()
        subcommands = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        assert "connect" in subcommands.choices
        assert "auth" in subcommands.choices
        for argv in (
            ["connect", "openrouter", "--no-verify"],
            ["auth", "list"],
            ["auth", "logout", "openrouter"],
        ):
            assert parser.parse_args(argv).func is not None

    def test_switching_the_active_credential_moves_only_the_pointer(self, env):
        auth.connect("openrouter", "sk-a-1", store=_store(env), verify=False)
        auth.connect("anthropic", "sk-b-1", store=_store(env), verify=False)
        report = auth.set_active("openrouter", path=_store(env))
        assert report.active == "openrouter"
        assert set(report.ids()) == {"openrouter", "anthropic"}
        assert auth.apply_active_credential({})["api_key"] == "sk-a-1"
        with pytest.raises(KeyError):
            auth.set_active("not-connected", path=_store(env))

    def test_list_credentials_is_the_same_read_as_load_credentials(self, env):
        auth.connect("openrouter", "sk-a-1", store=_store(env), verify=False)
        assert (
            auth.list_credentials(path=_store(env)).ids()
            == auth.load_credentials(path=_store(env)).ids()
        )
