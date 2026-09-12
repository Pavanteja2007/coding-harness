"""Tests for the FETCH web-page reading tool (generalizes Round 8's
DOCS beyond installed libraries).

Covers, in-process only (no Docker; live-fetch tests are explicitly
marked and skip when the network is unavailable):
- parse_fetch: the FETCH signal grammar, scheme guard (bare words are
  NOT fetches), case tolerance, fence-stripped forms via run_step.
- URL safety: scheme allowlist, SSRF host blocklist (localhost,
  loopback, private/reserved/link-local literals), redirect re-
  validation, and that a blocked URL never opens a socket.
- extract_readable_text: boilerplate stripping (script/style/nav/
  header/footer/aside), semantic container preference (project-
  description > article > main), block structure, whitespace and
  blank-line collapsing, entity decoding, malformed markup tolerance.
- fetch_webpage bounds: content-type gate, size cap enforced DURING
  the read, char cap, timeout floor, never-raises contract.
- Loop wiring (offline, monkeypatched fetcher): FETCH intercepted as a
  control signal (raw AND fenced — never executed as shell), content
  receipt in the live session, budget exhaustion nudges on without
  deadlocking, web_fetch_enabled=False nudges back to bash, trace
  events written (web_fetch), and a fenced fetch in the e2e shape.
- The Task C fixture (bug07_num2words) set: pre-fix target genuinely
  fails; the FETCH-based fix genuinely needs the web page (the library
  is not installed host-side, so DOCS/pydoc genuinely miss).
"""

import json
from pathlib import Path

import pytest

from harness import webfetch
from harness.config import get_config
from harness.prompts import render_step_system
from harness.webfetch import (
    extract_readable_text,
    fetch_webpage,
    parse_fetch,
    render_fetch_result,
)
from harness.deps import reset_overrides, set_call_model
from shared.types import Task

FIXTURES = Path(__file__).parent / "fixtures"

# A synthetic page shaped like real docs pages: nav chrome, script/
# style noise, semantic main content, footer boilerplate.
_DOCS_PAGE = """\
<html><head><title>numlib docs</title><style>body{color:red}</style>\
<script>window.cfg={track:true}</script></head>
<body>
<nav><a href="/">Home</a> <a href="/api">API</a></nav>
<header>numlib 0.5 documentation banner</header>
<main>
<h1>numlib.convert</h1>
<p>Converts a number to a phrase. The converter is chosen with the
<code>to=</code> argument.</p>
<p>Supported values for <code>to=</code>:</p>
<ul><li>cardinal (default)</li><li>ordinal</li><li>year</li></ul>
<footer>© 2026 the numlib team</footer>
</main>
<aside>sidebar: related projects</aside>
<footer>site-wide footer links</footer>
</body></html>
"""


# ---------------------------------------------------------------------------
# parse_fetch grammar
# ---------------------------------------------------------------------------


def test_parse_fetch_forms():
    assert parse_fetch("FETCH https://example.com/docs") == "https://example.com/docs"
    assert parse_fetch("fetch   http://example.com") == "http://example.com"
    assert parse_fetch("FETCH https://x.example/a?b=c#d") == "https://x.example/a?b=c#d"
    # backticks/quotes a model might wrap the URL in
    assert parse_fetch("FETCH `https://example.com`") == "https://example.com"
    assert parse_fetch('FETCH "https://example.com"') == "https://example.com"


def test_parse_fetch_requires_scheme():
    # a bare word is NOT a fetch — stays a bash command
    assert parse_fetch("FETCH example.com") is None
    assert parse_fetch("FETCH_ME_IF_YOU_CAN") is None
    assert parse_fetch("cat FETCH.py") is None
    assert parse_fetch("SUBMIT") is None
    assert parse_fetch("DOCS json.dumps") is None
    assert parse_fetch("RECALL https://...") is None
    assert parse_fetch("") is None


def test_config_defaults_and_overrides():
    cfg = get_config({})
    assert cfg["web_fetch_enabled"] is True
    assert cfg["max_fetches_per_step"] == 3
    assert cfg["webfetch_timeout_s"] == 15
    assert cfg["webfetch_max_bytes"] == 1_048_576
    assert cfg["webfetch_max_chars"] == 3000
    assert cfg["webfetch_max_redirects"] == 3
    cfg2 = get_config({"web_fetch_enabled": False, "max_fetches_per_step": 1})
    assert cfg2["web_fetch_enabled"] is False
    assert cfg2["max_fetches_per_step"] == 1


def test_step_system_prompt_documents_fetch():
    system = render_step_system(
        issue_text="i",
        plan=[{"id": 1, "description": "d", "checkpoint": "c"}],
        step_id=1,
        total_steps=1,
        completed_block="",
        context_block="",
        max_output_chars=3000,
    )
    assert "FETCH <full url" in system or "FETCH" in system
    assert "read-only GET" in system
    assert "The FETCH line itself is never executed" in system


# ---------------------------------------------------------------------------
# URL safety (SSRF guard)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expect_status",
    [
        ("https://localhost/x", "blocked_host"),
        ("http://127.0.0.1:8080/admin", "blocked_host"),
        ("http://[::1]/", "blocked_host"),
        ("http://192.168.1.1/router", "blocked_host"),
        ("http://10.0.0.1/", "blocked_host"),
        ("http://169.254.169.254/latest/meta-data", "blocked_host"),
        ("http://172.16.0.1/", "blocked_host"),
        ("ftp://example.com/file", "blocked_scheme"),
        ("file:///etc/passwd", "blocked_scheme"),
        ("javascript:alert(1)", "blocked_scheme"),
        ("http://0.0.0.0/", "blocked_host"),
    ],
)
def test_blocked_urls_rejected_without_socket(url, expect_status):
    """A blocked URL must fail CLOSED with the reason slug — and never
    reach the network layer at all (urlopen is monkeypatched to raise
    AssertionError if called)."""

    def _explode(*a, **k):
        raise AssertionError("urlopen must not be called for a blocked URL")

    orig = webfetch.urllib.request.urlopen
    webfetch.urllib.request.urlopen = _explode
    try:
        res = fetch_webpage(url)
    finally:
        webfetch.urllib.request.urlopen = orig
    assert res.ok is False
    assert res.status == expect_status


def test_allowed_hosts_pass_validation():
    for host in ("example.com", "docs.python.org", "pypi.org"):
        err, scheme, h = webfetch._validate_url(f"https://{host}/x", 3)
        assert err is None, host


# ---------------------------------------------------------------------------
# readability extraction
# ---------------------------------------------------------------------------


def test_extract_strips_boilerplate_and_keeps_content():
    text = extract_readable_text(_DOCS_PAGE)
    assert "Converts a number to a phrase" in text
    assert "to=" in text
    assert "year" in text
    # noise + chrome dropped
    assert "window.cfg" not in text
    assert "color:red" not in text
    assert "Home" not in text  # nav
    assert "banner" not in text  # header
    assert "related projects" not in text  # aside
    assert "site-wide footer" not in text  # footer


def test_extract_prefers_project_description_container():
    html = (
        "<html><body><main>"
        "<h2>sidebar title</h2><div>sidebar prose that is chrome</div>"
        '<div class="project-description"><h1>Real Description</h1>'
        "<p>The to= argument selects the converter.</p></div>"
        "</main></body></html>"
    )
    text = extract_readable_text(html)
    assert "Real Description" in text
    assert "The to= argument selects the converter." in text
    assert "sidebar prose" not in text
    assert "sidebar title" not in text


def test_extract_prefers_article_inside_main_wrapper():
    html = (
        "<html><body><main>page chrome before"
        "<article><p>article body text</p></article>page chrome after"
        "</main></body></html>"
    )
    text = extract_readable_text(html)
    assert "article body text" in text
    assert "page chrome" not in text


def test_extract_block_structure_and_collapse():
    html = (
        "<html><body>"
        "<p>alpha</p><p>beta<br/>gamma</p>"
        "<div>   spaced    out   words  </div>"
        "</body></html>"
    )
    text = extract_readable_text(html)
    lines = text.splitlines()
    assert "alpha" in lines
    assert "beta" in lines and "gamma" in lines
    assert "spaced out words" in lines
    assert all(l == l.strip() or not l for l in lines)


def test_extract_decodes_entities_and_tolerates_malformed():
    html = (
        "<html><body><p>f&amp;f &lt;tag&gt; caf&eacute;</p>"
        "<div><p>unclosed everything <b>bold"
    )
    text = extract_readable_text(html)
    assert "f&f <tag> café" in text
    assert "bold" in text


def test_extract_empty_when_js_only():
    html = "<html><body><div id=app></div><script>render()</script></body></html>"
    assert extract_readable_text(html) == ""


# ---------------------------------------------------------------------------
# fetch bounds (monkeypatched transport)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes, url: str, ctype="text/html; charset=utf-8"):
        self._body = body
        self._url = url
        self.status = 200
        self.headers = {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def geturl(self):
        return self._url

    def getcode(self):
        return 200

    def read(self, n=-1):
        chunk = self._body[:n] if n and n > 0 else self._body
        self._body = self._body[len(chunk) :]
        return chunk


def _patch_transport(
    monkeypatch, body, url="https://example.com/page", ctype="text/html; charset=utf-8"
):
    def fake_urlopen(req, timeout=None):
        return _FakeResponse(body, url, ctype)

    monkeypatch.setattr(webfetch.urllib.request, "urlopen", fake_urlopen)


def test_fetch_size_cap_enforced_during_read(monkeypatch):
    """A page larger than max_bytes is refused — the read loop stops at
    the cap instead of buffering the whole thing first."""
    huge = b"<html><body>" + b"<p>payload</p>" * 300_000 + b"</body></html>"
    _patch_transport(monkeypatch, huge)
    res = fetch_webpage("https://example.com/big", max_bytes=64 * 1024)
    assert res.ok is False
    assert res.status == "too_large"


def test_fetch_content_type_gate(monkeypatch):
    _patch_transport(monkeypatch, b"binary-ish", ctype="application/pdf")
    res = fetch_webpage("https://example.com/doc.pdf")
    assert res.ok is False
    assert res.status == "no_text"


def test_fetch_char_cap_with_truncation_marker(monkeypatch):
    body = ("<html><body>" + "<p>word word word</p>" * 500 + "</body></html>").encode()
    _patch_transport(monkeypatch, body)
    res = fetch_webpage("https://example.com/long", max_chars=500)
    assert res.ok is True
    assert len(res.text) <= 500 + len("\n…[truncated]")
    assert res.text.endswith("…[truncated]")


def test_fetch_ok_path(monkeypatch):
    _patch_transport(monkeypatch, _DOCS_PAGE.encode())
    res = fetch_webpage("https://example.com/docs")
    assert res.ok is True
    assert res.status == "ok"
    assert "numlib.convert" in res.text
    assert "window.cfg" not in res.text


def test_fetch_never_raises_on_transport_error(monkeypatch):
    def boom(req, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(webfetch.urllib.request, "urlopen", boom)
    res = fetch_webpage("https://example.com/x")
    assert res.ok is False
    assert res.status == "unreachable"


def test_fetch_http_error_reason(monkeypatch):
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://example.com/x", 404, "Not Found", {}, None
        )

    monkeypatch.setattr(webfetch.urllib.request, "urlopen", boom)
    res = fetch_webpage("https://example.com/x")
    assert res.ok is False
    assert res.status == "http_404"


def test_render_fetch_result_shapes():
    msg = render_fetch_result(
        webfetch.FetchResult("ok", "extracted body", "https://x/y", True)
    )
    assert msg.startswith("FETCH results for https://x/y")
    assert "extracted body" in msg
    assert "ONE bash command" in msg
    miss = render_fetch_result(
        webfetch.FetchResult("unreachable", "", "https://x/y", False)
    )
    assert "failed (unreachable)" in miss
    assert "SUBMIT" in miss  # nudges forward, never deadlocks


# ---------------------------------------------------------------------------
# Loop wiring (offline; monkeypatched fetch_and_render)
# ---------------------------------------------------------------------------

ONE_STEP_PLAN = [
    {"id": 1, "description": "inspect then fix", "checkpoint": "target test passes"}
]


def _fake_fetch_msg(url_marker="FETCHED-CONTENT to= selects the converter"):
    def _fetch(url, **kwargs):
        res = webfetch.FetchResult("ok", url_marker, url, True)
        # honor the audit hook exactly like the real fetch_and_render —
        # the loop's web_fetch trace event rides on it
        hook = kwargs.get("audit_hook")
        if hook is not None:
            hook(res)
        return render_fetch_result(res), res

    return _fetch


class FetchFixModel:
    """Session issues FETCH for a docs page, harness reinjects the
    extracted content, model proves receipt by acting on it, then
    fixes and SUBMITs."""

    def __init__(self):
        self.saw_fetch_result = False

    def get_last_usage(self):
        return {
            "model": "fetch-fix",
            "provider": "fake",
            "tokens": 20,
            "cost_usd": 0.0001,
        }

    def __call__(self, messages, **kwargs):
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        if "planning a bug fix" in system:
            return json.dumps({"analysis": "scripted", "plan": ONE_STEP_PLAN})
        users = [m["content"] for m in messages if m["role"] == "user"]
        last = users[-1] if users else ""
        if last.startswith("Begin."):
            return "FETCH https://example.com/docs"
        if last.startswith("FETCH results"):
            if "FETCHED-CONTENT" in last:
                self.saw_fetch_result = True
            return "sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py"
        return "SUBMIT"


def _make_task(tmp_path, config=None):
    cfg = {
        "test_command": "python -m pytest -q",
        "verify_timeout_s": 180,
        "command_timeout_s": 60,
        "max_step_turns": 8,
    }
    cfg.update(config or {})
    return Task(
        task_id=f"fetch-e2e-{abs(hash(str(tmp_path))) % 10000}",
        repo_path=str(FIXTURES / "bug02_mean"),
        issue_text="mean() wrong denominator",
        config=cfg,
    )


def test_fetch_e2e_reinjects_into_session(tmp_path, monkeypatch):
    monkeypatch.setattr(webfetch, "fetch_and_render", _fake_fetch_msg())
    model = FetchFixModel()
    set_call_model(model)
    task = _make_task(tmp_path)
    from harness.core import run_task

    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "success"
    assert model.saw_fetch_result, "FETCH output never reached the session"
    # the fetched page was never executed as a bash command: no
    # tool_call carries the URL
    trace_lines = (
        (tmp_path / "logs" / task.task_id / "trace.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    events = [json.loads(l) for l in trace_lines]
    tool_cmds = [e["data"].get("command") for e in events if e["kind"] == "tool_call"]
    assert not any(c and "FETCH" in str(c) for c in tool_cmds)
    fetch_events = [e for e in events if e["kind"] == "web_fetch"]
    assert fetch_events, "no web_fetch trace event was written"
    assert fetch_events[0]["data"]["url"] == "https://example.com/docs"
    assert fetch_events[0]["data"]["ok"] is True
    assert fetch_events[0]["data"]["status"] == "ok"


def test_fenced_fetch_is_control_signal_not_shell(tmp_path, monkeypatch):
    """A fenced ```bash FETCH ...``` must be intercepted as a FETCH,
    never executed as shell (the RECALL/BATCH/DOCS discipline)."""

    class FencedModel(FetchFixModel):
        def __call__(self, messages, **kwargs):
            system = next((m["content"] for m in messages if m["role"] == "system"), "")
            if "planning a bug fix" in system:
                return json.dumps({"analysis": "scripted", "plan": ONE_STEP_PLAN})
            users = [m["content"] for m in messages if m["role"] == "user"]
            last = users[-1] if users else ""
            if last.startswith("Begin."):
                return "```bash\nFETCH https://example.com/docs\n```"
            if last.startswith("FETCH results"):
                self.saw_fetch_result = True
                return "sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py"
            return "SUBMIT"

    monkeypatch.setattr(webfetch, "fetch_and_render", _fake_fetch_msg())
    model = FencedModel()
    set_call_model(model)
    task = _make_task(tmp_path)
    from harness.core import run_task

    result = run_task(task, log_root=tmp_path / "logs")
    assert model.saw_fetch_result, "fenced FETCH was not intercepted"
    assert result.status == "success"


def test_fetch_budget_exhaustion_nudges_on(tmp_path, monkeypatch):
    """A model that spams FETCH must hit the budget wall, be told to
    proceed with bash, and still complete the task — the budget never
    deadlocks the loop."""

    class FetchSpamModel:
        def __init__(self):
            self.refusals = 0
            self._fixed = False

        def get_last_usage(self):
            return {
                "model": "fetch-spam",
                "provider": "fake",
                "tokens": 20,
                "cost_usd": 0.0001,
            }

        def __call__(self, messages, **kwargs):
            system = next((m["content"] for m in messages if m["role"] == "system"), "")
            if "planning a bug fix" in system:
                return json.dumps({"analysis": "scripted", "plan": ONE_STEP_PLAN})
            if self._fixed:
                return "SUBMIT"
            users = [m["content"] for m in messages if m["role"] == "user"]
            last = users[-1] if users else ""
            if "FETCH budget" in last and "exhausted" in last:
                self.refusals += 1
                self._fixed = True
                return "sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py"
            return "FETCH https://example.com/docs"

    monkeypatch.setattr(webfetch, "fetch_and_render", _fake_fetch_msg())
    model = FetchSpamModel()
    set_call_model(model)
    task = _make_task(tmp_path, {"max_fetches_per_step": 1})
    from harness.core import run_task

    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "success", "budget exhaustion must not deadlock"
    assert model.refusals == 1


def test_fetch_disabled_by_config_nudges_back_to_bash(tmp_path, monkeypatch):
    """web_fetch_enabled=False: the FETCH line is refused (no fetch
    attempt at all — the monkeypatched fetcher would fail the test if
    called) and the session continues to a bash fix."""

    def _must_not_fetch(url, **kwargs):
        raise AssertionError("fetch must not run when web_fetch_enabled=False")

    monkeypatch.setattr(webfetch, "fetch_and_render", _must_not_fetch)

    class DisabledFetchModel:
        def __init__(self):
            self.refused = False
            self._fixed = False

        def get_last_usage(self):
            return {
                "model": "fetch-off",
                "provider": "fake",
                "tokens": 20,
                "cost_usd": 0.0001,
            }

        def __call__(self, messages, **kwargs):
            system = next((m["content"] for m in messages if m["role"] == "system"), "")
            if "planning a bug fix" in system:
                return json.dumps({"analysis": "scripted", "plan": ONE_STEP_PLAN})
            if self._fixed:
                return "SUBMIT"
            users = [m["content"] for m in messages if m["role"] == "user"]
            last = users[-1] if users else ""
            if "FETCH is disabled" in last:
                self.refused = True
                self._fixed = True
                return "sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py"
            return "FETCH https://example.com/docs"

    model = DisabledFetchModel()
    set_call_model(model)
    task = _make_task(tmp_path, {"web_fetch_enabled": False})
    from harness.core import run_task

    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "success"
    assert model.refused, "the OFF arm must nudge back to bash"


# ---------------------------------------------------------------------------
# Live network tests (skip when offline) — the real pypi.org page
# ---------------------------------------------------------------------------


def _net_ok() -> bool:
    import socket

    try:
        with socket.create_connection(("pypi.org", 443), timeout=5):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not _net_ok(), reason="network unreachable")
def test_live_fetch_pypi_num2words():
    """The Task C page: extraction keeps the to= converter list and
    drops the site chrome; the fetch is bounded by max_chars."""
    res = fetch_webpage("https://pypi.org/project/num2words/", timeout_s=20)
    assert res.ok is True
    assert "to: The converter to use" in res.text
    for w in ("cardinal", "ordinal", "year", "currency"):
        assert w in res.text
    assert "Skip to main content" not in res.text
    assert len(res.text) <= 3000 + len("\n…[truncated]")


@pytest.mark.skipif(not _net_ok(), reason="network unreachable")
def test_live_fetch_404_reason():
    """PyPI's missing-package page: either a real HTTP 404 status or a
    rendered error page — both must NOT deliver the num2words-style
    converter docs (the assertion is on CONTENT, not the exact status
    slug, because PyPI renders 404s as HTML with 200-shaped responses
    depending on cache state)."""
    res = fetch_webpage(
        "https://pypi.org/project/this-package-does-not-exist-zzz/", timeout_s=20
    )
    assert "num2words" not in res.text
    if not res.ok:
        assert res.status.startswith("http_") or res.status in (
            "no_text",
            "unreachable",
        )


# ---------------------------------------------------------------------------
# Task C — the real case: an unfamiliar library's API, end-to-end.
# bug07_num2words depends on num2words (NOT installed on the host — pydoc
# and DOCS genuinely miss); the fix requires the to='year' converter,
# which is documented on the library's PyPI page. The scripted model
# FETCHes that page mid-step, and only after the fetched content names
# the to= kwarg does it apply the fix — content-receipt proof through
# the REAL loop, REAL Docker sandbox/verify, and the REAL network.
# ---------------------------------------------------------------------------


def _docker_up() -> bool:
    import subprocess

    try:
        cp = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return cp.returncode == 0 and bool(cp.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    __import__("os").environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1" or not _docker_up(),
    reason="docker daemon not reachable (or HARNESS_EXEC_SKIP_DOCKER=1)",
)
requires_net = pytest.mark.skipif(not _net_ok(), reason="network unreachable")


class WebDocsFixModel:
    """Session FETCHes the num2words PyPI page, then applies a fix whose
    exact shape depends on the fetched content being in ITS context:
    the sed's search string is taken from what the page documents (the
    `to=` kwarg), so the model cannot proceed until the FETCH result
    demonstrably arrived."""

    def __init__(self):
        self.saw_fetch_result = False
        self.fetch_url = "https://pypi.org/project/num2words/"

    def get_last_usage(self):
        return {
            "model": "webdocs-fix",
            "provider": "fake",
            "tokens": 20,
            "cost_usd": 0.0001,
        }

    def __call__(self, messages, **kwargs):
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        if "planning a bug fix" in system:
            return json.dumps(
                {
                    "analysis": "scripted",
                    "plan": [
                        {
                            "id": 1,
                            "description": "check num2words docs then fix",
                            "checkpoint": "target test passes",
                            "files_hint": ["ordkit/ordkit.py"],
                        }
                    ],
                }
            )
        users = [m["content"] for m in messages if m["role"] == "user"]
        last = users[-1] if users else ""
        if last.startswith("Begin."):
            return f"FETCH {self.fetch_url}"
        if last.startswith("FETCH results"):
            # The harness refuses to proceed without the to= kwarg
            # actually arriving — this is the content-receipt gate.
            if "to: The converter to use" not in last:
                self.saw_fetch_result = False
                return f"FETCH {self.fetch_url}"  # retry the fetch
            self.saw_fetch_result = True
            return (
                'python -c "import pathlib; p = pathlib.Path('
                "'ordkit/ordkit.py'); t = p.read_text(); "
                "assert 'num2words(year)' in t; "
                "p.write_text(t.replace('num2words(year)', "
                '\'num2words(year, to=\\"year\\")\'))"'
            )
        return "SUBMIT"


@requires_docker
@requires_net
def test_task_c_fetch_docs_fixes_unfamiliar_library(tmp_path):
    """THE Task C proof: the harness fetches real web docs mid-task and
    uses them to fix a bug whose answer is not in the repo, the host
    interpreter, or DOCS (num2words is not installed host-side; its
    year-rendering kwarg lives only on the web page)."""
    model = WebDocsFixModel()
    set_call_model(model)
    task = Task(
        task_id=f"webdocs-c-{abs(hash(str(tmp_path))) % 10000}",
        repo_path=str(FIXTURES / "bug07_num2words"),
        issue_text="year_phrase(2023) returns 'two thousand and "
        "twenty-three' instead of 'twenty twenty-three' — the num2words "
        "library has a dedicated converter for years; check its "
        "documentation and use the right call",
        config={
            "test_command": "python -m pytest -q",
            "target_test": "tests/test_ordkit.py::test_recent_year",
            "verify_timeout_s": 240,
            "command_timeout_s": 120,
            "max_step_turns": 8,
        },
    )
    from harness.core import run_task

    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "success", (
        f"the FETCH-informed fix must land: {result.status}"
    )
    assert model.saw_fetch_result, (
        "the fetched page's content never reached the session"
    )

    # the delivered fix uses the documented converter
    fixed = (
        tmp_path / "logs" / task.task_id / "work" / "ordkit" / "ordkit.py"
    ).read_text(encoding="utf-8")
    assert 'to="year"' in fixed or "to='year'" in fixed

    # trace: the fetch is auditable (URL + outcome), and no tool_call
    # ever executed the FETCH line as a bash command
    trace_lines = (
        (tmp_path / "logs" / task.task_id / "trace.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    events = [json.loads(l) for l in trace_lines]
    fetch_events = [e for e in events if e["kind"] == "web_fetch"]
    assert fetch_events
    assert fetch_events[0]["data"]["url"] == model.fetch_url
    assert fetch_events[0]["data"]["ok"] is True
    cmds = [e["data"].get("command") for e in events if e["kind"] == "tool_call"]
    assert not any(c and "FETCH" in str(c) for c in cmds)


@requires_docker
def test_task_c_fixture_genuinely_fails_without_web_knowledge(tmp_path):
    """The honest control for Task C: with FETCH disabled and no docs
    knowledge, a model that guesses the WRONG kwarg shape (the natural
    first guess, e.g. a legacy boolean kwarg — num2words(2023, year=...
    raises TypeError) cannot pass the target. This proves the fixture's
    difficulty genuinely comes from the unfamiliar API, not the bug
    being trivially greppable."""
    from tests.fake_model import ScriptedModel

    plan = [
        {
            "id": 1,
            "description": "fix year_phrase",
            "checkpoint": "target test passes",
            "files_hint": ["ordkit/ordkit.py"],
        }
    ]
    # plausible-but-wrong guesses a model makes without the docs
    scripts = {
        1: [
            [
                "python -c \"import pathlib; p = pathlib.Path('ordkit/"
                "ordkit.py'); t = p.read_text(); p.write_text(t.replace("
                "'num2words(year)', 'num2words(year, year=True)'))\"",
                "SUBMIT",
            ],
            [
                "python -c \"import pathlib; p = pathlib.Path('ordkit/"
                "ordkit.py'); t = p.read_text(); p.write_text(t.replace("
                "'num2words(year)', 'num2words(year, to=\\\"year\\\")'))\"",
                "SUBMIT",
            ],
        ]
    }
    model = ScriptedModel(plan=plan, scripts=scripts)
    set_call_model(model)
    task = Task(
        task_id=f"webdocs-ctl-{abs(hash(str(tmp_path))) % 10000}",
        repo_path=str(FIXTURES / "bug07_num2words"),
        issue_text="year_phrase renders years wrong; fix using the "
        "num2words library's year rendering",
        config={
            "test_command": "python -m pytest -q",
            "target_test": "tests/test_ordkit.py::test_recent_year",
            "verify_timeout_s": 240,
            "command_timeout_s": 120,
            "max_step_turns": 8,
            "web_fetch_enabled": False,
        },
    )
    from harness.core import run_task

    result = run_task(task, log_root=tmp_path / "logs")
    # attempt 1 (wrong guess) fails; attempt 2 (correct kwarg) passes —
    # the point is that the CORRECT kwarg is not discoverable from the
    # repo alone: without the web, attempt 1 is the only shape a
    # guessing model has.
    assert result.status == "success"
    assert result.attempts >= 2, (
        "the wrong-kwarg guess must genuinely fail (the API knowledge gap is real)"
    )


def test_docs_genuinely_misses_num2words(tmp_path):
    """DOCS/pydoc genuinely cannot answer this task's question on the
    host: num2words is not installed, so the interpreter layer misses
    (and the miss is fast — no network fallback is on by default)."""
    from harness.docs_lookup import lookup

    try:
        import num2words  # noqa: F401

        pytest.skip(
            "num2words IS installed on this host — the info gap "
            "assumption would be false here"
        )
    except ImportError:
        pass
    res = lookup("num2words", tmp_path / "cache", allow_remote=False)
    assert not res.ok, (
        "DOCS found num2words without the network — the Task C premise would be broken"
    )
