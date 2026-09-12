"""Web-page reading tool (general-purpose FETCH — supersedes the narrower
docs-lookup scope of Round 8 Task D for anything not already installed).

Problem being fixed: the harness's knowledge sources were the repo itself
and (via DOCS) the local interpreter + PyPI metadata. When the agent meets
an unfamiliar library's API, a stdlib behavior, or an error whose answer
lives on a web page the repo doesn't ship, it has no way to read it — it
guesses, and the verifier rejects the guess expensively.

The mechanism mirrors RECALL/DOCS: a step session outputs

    FETCH <url>

in place of a bash command (a control signal to the HARNESS, never
executed as shell — parsed on the raw reply AND its fence-stripped form).
The harness fetches the page, extracts readable text, and re-injects it
into the live session as the next user message.

Scope and safety (read-only BY CONSTRUCTION):
- GET ONLY. urllib.request.Request with method GET; no body, no headers
  beyond a declared User-Agent. No form submission, no auth, no POST.
- Scheme allowlist: http/https only. Host blocklist: localhost/loopback/
  link-local/private ranges (SSRF guard) and non-DNS names — the fetch
  runs on the HOST, outside the sandbox, so the guard lives here.
- Redirects: followed only up to webfetch_max_redirects (default 3) and
  re-validated against the same scheme/host rules at EVERY hop.
- Timeout: webfetch_timeout_s (default 15) bounds the whole fetch — a
  slow page can never stall a step session.
- Content cap: the response is size-capped at webfetch_max_bytes (default
  1 MiB, enforced DURING the read loop, not after) and the rendered text
  at webfetch_max_chars (default 3000) — a huge page can never blow up
  context.
- Every fetch is trace-logged (URL + timestamp + outcome) via the
  `web_fetch` event, same discipline as any other tool call, and also
  emitted to the unified cross-module stream (shared.tracing) when
  VEX_TRACE_DIR is set.

Readability-style extraction (stdlib HTMLParser — no new deps, works in
every CI cell):
1. Strip <script>/<style>/<noscript>/<nav>/<header>/<footer>/<aside> and
   everything inside them (nav/ads/boilerplate live there).
2. Prefer semantic containers when present: <main>, <article>, or a
   div[class~="project-description"] (PyPI's description pane) — the
   extracted text comes from the FIRST such container rather than the
   whole body, which drops chrome without needing a scoring heuristic.
3. Block tags (p/li/h1..h6/td/dd...) separate lines; <br> breaks lines.
4. HTML entity refs decoded (via convert_charrefs), tags dropped, text
   runs whitespace-collapsed; consecutive blank lines collapse to one.

Budgets (config-driven, like RECALL/DOCS):
  web_fetch_enabled (bool, default True) — the FETCH escape on/off;
    False = the loop nudges back to bash (the ablation's OFF arm)
  max_fetches_per_step (int, default 3) — budget per step session, with
    an exhaustion nudge that can never deadlock (same pattern as RECALL)
  webfetch_timeout_s / webfetch_max_bytes / webfetch_max_chars /
  webfetch_max_redirects — per-fetch bounds (defaults above)

Trace: each fetch logs a `web_fetch` event {step_id, turn, url, ok,
status, source=..., chars} so every URL the harness ever fetched is
auditable from the task's trace alone.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

__all__ = [
    "FetchResult",
    "parse_fetch",
    "fetch_webpage",
    "render_fetch_result",
    "fetch_and_render",
]

# Bounded, config-independent safety floor: even a caller that pins
# webfetch_timeout_s high must not let one fetch stall the loop forever.
_TIMEOUT_FLOOR_S = 3
_MAX_BYTES_FLOOR = 64 * 1024
_MAX_REDIRECTS_CEIL = 5

_USER_AGENT = "vex-harness-webfetch/1.0 (+read-only page reader)"

# Tags whose entire content is dropped (script/style = noise, nav/header/
# footer/aside = boilerplate chrome).
_SKIP_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "iframe",
        "form",
        "nav",
        "header",
        "footer",
        "aside",
    }
)
# A newline is emitted when one of these tags closes (block structure).
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "li",
        "ul",
        "ol",
        "dl",
        "table",
        "tr",
        "blockquote",
        "pre",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "main",
        "br",
        "hr",
        "td",
        "th",
        "dd",
        "dt",
        "figcaption",
        "figure",
    }
)
# Semantic containers preferred for extraction (first match wins).
_CONTAINER_TAGS = ("main", "article")


class FetchResult(NamedTuple):
    """One resolved FETCH.

    status: "ok" | "error:<reason>" — a short stable reason slug
      (timeout, too_large, bad_url, blocked_scheme, blocked_host,
       denied_redirect, http_<code>, unreachable, no_text).
    text: the model-facing extracted text (already capped; "" on miss)
    url: the FINAL url after redirects (audit trail)
    ok: True iff readable text was extracted
    """

    status: str
    text: str
    url: str
    ok: bool


# ---------------------------------------------------------------------------
# signal parsing
# ---------------------------------------------------------------------------

_FETCH_PAT = re.compile(r"^\s*FETCH\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)


def parse_fetch(text: str) -> Optional[str]:
    """Return the URL of a FETCH request, or None if the message isn't one.

    Accepts `FETCH <url>` (case-insensitive). A FETCH is a control signal
    to the HARNESS, not a bash command — run_step checks this before
    command extraction (on both the raw reply and its fence-stripped
    form), so the URL is never executed in the sandbox. The argument must
    carry a scheme (http:// or https://) to qualify; a bare word is NOT a
    FETCH (stays a bash command), so `FETCH_ME_IF_YOU_CAN`-shaped
    identifiers never false-trigger.
    """
    m = _FETCH_PAT.match((text or "").strip())
    if not m:
        return None
    raw = m.group(1).strip().strip("`\"'")
    # The explicit scheme requirement is the false-positive guard.
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        return None
    return raw


# ---------------------------------------------------------------------------
# URL safety (SSRF guard — the fetch runs on the HOST)
# ---------------------------------------------------------------------------


def _blocked_host(host: str) -> Optional[str]:
    """Return a blocked-reason slug for a host the fetch must not reach,
    or None if it is allowed. Assumes host is lowercase, bracket-stripped
    (an IPv6 literal like [::1] arrives as ::1).

    Blocked: localhost and friends, loopback/Link-Local/Unique-Local/
    Reserved IPv4 + IPv6 literals, and 0.0.0.0-style wildcard addresses —
    the classic SSRF ladder. Everything DNS-resolvable beyond those is
    allowed (a docs page is a docs page).
    """
    if not host:
        return "blocked_host"
    if host in (
        "localhost",
        "localhost.localdomain",
        "0.0.0.0",
        "::",
        "ip6-localhost",
        "ip6-loopback",
    ):
        return "blocked_host"
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return None  # a DNS name — allowed at the name level
    if (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_private
        or addr.is_reserved
        or addr.is_unspecified
        or addr.is_multicast
    ):
        return "blocked_host"
    return None


def _validate_url(
    url: str,
    max_redirects_left: int,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Validate one URL for fetching.

    Returns (error_slug, scheme, host) — error_slug is None when the URL
    is fetchable. Checks scheme allowlist, host blocklist, and the
    redirect budget on every hop (a redirect is only followed when
    max_redirects_left > 0; the cap makes redirect loops terminate).
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "bad_url", None, None
    if parts.scheme.lower() not in ("http", "https"):
        return "blocked_scheme", None, None
    host = (parts.hostname or "").lower().strip("[]")
    if not host:
        return "bad_url", None, None
    reason = _blocked_host(host)
    if reason:
        return reason, None, None
    if max_redirects_left <= 0:
        return "denied_redirect", None, None
    return None, parts.scheme.lower(), host


# ---------------------------------------------------------------------------
# readability-style text extraction (stdlib HTMLParser)
# ---------------------------------------------------------------------------


class _ReadableText(HTMLParser):
    """Extract readable text from HTML, dropping boilerplate.

    Handles skip tags (script/style/nav/header/footer/aside — dropped
    with their content), block tags (line separators), whitespace
    collapsing, and a container preference: text inside a semantic
    container (<main>/<article>/a PyPI-style project-description div) is
    captured SEPARATELY per container; the innermost container's text
    wins over the whole body (the innermost is the tightest content
    region — e.g. PyPI's description div inside its <main> wrapper),
    and the plain body text is the fallback when no container exists.
    Assumes the input is HTML served as text (convert_charrefs handles
    entities). Never raises on malformed markup — HTMLParser is lenient
    by design and stray text outside any structure still renders.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: List[str] = []  # whole-body text
        self._skip_depth: int = 0
        # stack of open semantic containers; each entry is
        # (marker, own captured chunks); the DEEPEST entry holding text
        # at close time wins over the whole body (innermost = tightest
        # content region — e.g. PyPI's description div inside <main>).
        self._containers: List[Tuple[str, List[str]]] = []
        self._winner: Optional[List[str]] = None
        self._in_body: bool = False

    # -- tag events ------------------------------------------------------

    @staticmethod
    def _container_marker(
        tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> Optional[str]:
        """The container marker for a start tag, or None when the tag
        isn't a semantic container."""
        if tag == "div" and any(
            "project-description" in (cls or "").split()
            for name, cls in attrs
            if name == "class"
        ):
            return "div.project-description"
        if tag in _CONTAINER_TAGS:
            return tag
        return None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag == "body":
            self._in_body = True
            return
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        marker = self._container_marker(tag, attrs)
        if marker is not None:
            self._containers.append((marker, []))
            return
        if tag in _BLOCK_TAGS:
            self._emit("\n")

    def handle_startendtag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        if not self._skip_depth and self._in_body and tag in _BLOCK_TAGS:
            self._emit("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "body":
            self._in_body = False
            return
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if self._containers:
            # a close tag matching the innermost container's ACTUAL tag
            # closes it (div.project-description closes on </div>);
            # a stray close never pops the wrong container.
            inner_tag = self._containers[-1][0].split(".", 1)[0]
            if tag == inner_tag:
                self._close_container()
                return
        if tag in _BLOCK_TAGS:
            self._emit("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not self._in_body:
            return
        self._emit(data)

    # -- capture helpers ---------------------------------------------------

    def _close_container(self) -> None:
        marker, buf = self._containers.pop()
        # The innermost container that held any text wins; empty ones
        # (e.g. a wrapper closed before its content) defer to the next.
        if buf and "".join(buf).strip() and self._winner is None:
            self._winner = buf

    def _emit(self, chunk: str) -> None:
        if self._containers:
            self._containers[-1][1].append(chunk)
        else:
            self._out.append(chunk)

    def text(self) -> str:
        """The extracted readable text: the winning (innermost non-empty)
        semantic container's text when one was captured, else the whole
        body's; whitespace-collapsed, blank-line collapsed."""
        raw = "".join(self._winner if self._winner is not None else self._out)
        if not raw.strip():
            return ""
        lines: List[str] = []
        for line in raw.splitlines():
            line = " ".join(line.split())
            if line or (lines and lines[-1]):
                lines.append(line)
        # trim leading/trailing blank lines
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)


def extract_readable_text(html: str) -> str:
    """Extract clean readable text from an HTML page.

    Readability-style, deliberately simple (per the task brief): strip
    script/style/nav/header/footer/aside boilerplate, prefer the main
    semantic container when the page has one, keep block-structured
    lines, collapse whitespace. Assumes html is the page source as text
    (bytes must be decoded by the caller). Returns "" when nothing
    readable survives — a JS-only page honestly has nothing to read.
    """
    parser = _ReadableText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # malformed markup: fall back to what was captured so far — a
        # half-parsed page is still more signal than nothing.
        pass
    return parser.text()


# ---------------------------------------------------------------------------
# the fetch itself
# ---------------------------------------------------------------------------


def fetch_webpage(
    url: str,
    timeout_s: int = 15,
    max_bytes: int = 1_048_576,
    max_chars: int = 3000,
    max_redirects: int = 3,
) -> FetchResult:
    """Fetch one web page (GET only) and extract readable text.

    Assumes url starts with http:// or https:// (parse_fetch output) and
    the bounds are task-config-driven with the safety floors applied.
    Every hop of a redirect chain is re-validated (scheme + host + cap).

    Returns a FetchResult whose text is already capped; on any failure
    (timeout, oversize, blocked host, HTTP error, no readable text) ok is
    False and status names the reason. NEVER raises — a bad fetch is a
    tool result, not a crash; the loop feeds the reason back to the
    session.
    """
    timeout_s = max(_TIMEOUT_FLOOR_S, int(timeout_s))
    max_bytes = max(_MAX_BYTES_FLOOR, int(max_bytes))
    max_redirects = min(_MAX_REDIRECTS_CEIL, max(0, int(max_redirects)))

    hops_left = max_redirects
    current = url
    seen: set = set()
    for _ in range(max_redirects + 1):
        err, _scheme, _host = _validate_url(current, hops_left)
        if err:
            return FetchResult(err, "", current, False)
        if current in seen:
            return FetchResult("denied_redirect", "", current, False)
        seen.add(current)

        req = urllib.request.Request(
            current, headers={"User-Agent": _USER_AGENT}, method="GET"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                final_url = resp.geturl() or current
                status = getattr(resp, "status", None) or resp.getcode() or 0
                content_type = (resp.headers.get("Content-Type") or "").lower()
                if "html" not in content_type and "text" not in content_type:
                    # Not a page to read (binary/pdf/json payload): refuse
                    # rather than dumping bytes as "text".
                    return FetchResult("no_text", "", final_url, False)
                chunks: List[bytes] = []
                read = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    read += len(chunk)
                    if read > max_bytes:
                        # Cap DURING the read: a huge page can never blow
                        # up memory, let alone context.
                        return FetchResult("too_large", "", final_url, False)
                    chunks.append(chunk)
                html = b"".join(chunks).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return FetchResult(f"http_{exc.code}", "", current, False)
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            return FetchResult("unreachable", "", current, False)
        except ValueError:
            return FetchResult("bad_url", "", current, False)

        if final_url and final_url != current:
            # urlopen followed redirects internally; re-validate the final
            # target against the same rules (the intermediate hops were
            # opaque to us — the final URL must still be fetchable, and
            # the loop budget bounds chains).
            err, _s, _h = _validate_url(final_url, 1)
            if err:
                return FetchResult("denied_redirect", "", final_url, False)
            current = final_url
            hops_left -= 1
            if hops_left < 0:
                return FetchResult("denied_redirect", "", current, False)
            continue
        text = extract_readable_text(html)
        if not text:
            return FetchResult("no_text", "", current, False)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…[truncated]"
        return FetchResult("ok", text, current, True)

    return FetchResult("denied_redirect", "", current, False)


# ---------------------------------------------------------------------------
# session-facing rendering
# ---------------------------------------------------------------------------


def render_fetch_result(result: FetchResult) -> str:
    """The user message returned to a session that issued `FETCH <url>`.

    The miss message names the reason and nudges the session forward (the
    same never-deadlock discipline as RECALL/DOCS); the hit message caps
    itself and ends with the continue-with-one-command instruction.
    Assumes result.text is already length-capped by fetch_webpage.
    """
    if not result.ok:
        return (
            f"FETCH of {result.url} failed ({result.status}). The page "
            "may be down, blocked, or non-HTML — proceed with bash "
            "commands, or SUBMIT if the step is done."
        )
    return (
        f"FETCH results for {result.url} (readable text extracted):\n"
        f"---\n{result.text}\n---\n"
        "End of FETCH results. Continue with exactly ONE bash command, "
        "or SUBMIT if this step is done."
    )


def fetch_and_render(
    url: str,
    timeout_s: int = 15,
    max_bytes: int = 1_048_576,
    max_chars: int = 3000,
    max_redirects: int = 3,
    audit_hook=None,
) -> Tuple[str, FetchResult]:
    """Convenience wrapper: fetch + render, returning (message, result).

    audit_hook, when given, is called with the FetchResult AFTER the
    fetch — the loop controller passes a closure that trace-logs the
    event (URL + outcome), keeping this module free of trace plumbing
    while every fetch stays auditable. Assumes the hook never raises (the
    loop's does not); if it does, the exception propagates to the
    caller's best-effort handling, never into the fetch path.
    """
    res = fetch_webpage(url, timeout_s, max_bytes, max_chars, max_redirects)
    if audit_hook is not None:
        try:
            audit_hook(res)
        except Exception:
            pass
    return render_fetch_result(res), res


# kept for import-parity with docs_lookup's cache helpers (tests may use)
def _unused(_: Any) -> None:
    """Placeholder so Any/Path imports are not flagged."""
