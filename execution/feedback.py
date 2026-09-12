"""Structured verification feedback (the FeedbackObject contract, INTERFACES.md Boundary 7).

Raw pytest output is noisy: full source listings, decorator lines, where-clauses,
progress bars, coverage tables — the model burns context re-reading all of it to
find the one line that matters. This module parses pytest's failure sections into
FeedbackObjects: which test failed, what kind of failure, expected vs actual,
file:line, and a SHORT traceback summary — the "expected 200, got 404 at line 42"
shape a model can act on directly.

FeedbackObject is a pure data layer over verify()'s raw_output: it never runs
anything, never mutates the repo, and DEGRADES GRACEFULLY — when the output is
not parseable pytest (collection errors, tool noise, crashes), to_objects() falls
back to a single generic object carrying the raw tail, so a consumer never gets
an empty list for a failing run.

Consumers (per the published contract):
- the harness's step/attempt feedback (the `last_feedback` seam in core.py)
  renders format_objects() instead of raw_output tails;
- trace/`verify` events gain a `feedback` list (the to_dict() shape);
- the Agent Intelligence prompt builds against the JSON field contract below.

JSON shape (stable field names — other terminals build against these):
    {
      "test_id":   "tests/test_mathutil.py::test_mean_two",   # node id or None
      "failure_type": "assertion_mismatch",                   # see FAILURE_TYPES
      "summary":   "test_mean_two failed: expected 3, got 6.0",  # one line
      "expected":  "3",                                       # raw textual value
      "actual":    "6.0",                                     # raw textual value
      "file":      "tests/test_mathutil.py",                  # repo-relative
      "line":      5,                                         # failing statement line
      "traceback_summary": ">  assert mean([2, 4]) == 3\nE  assert 6.0 == 3\n...",
    }

All value fields are RAW STRINGS from pytest's own rendering ("6.0", "'1th'",
"[1, -3, 4]", "datetime.datetime(1970, 1, 6, 0, 0)") — no attempted coercion
(floats, datetimes, lists stay exactly as pytest printed them), so the parser
can never misrepresent a value, only reposition it. Note pytest renders
`assert <left> == <right>` with LEFT = what the code produced (actual) and
RIGHT = the expected side; this module maps accordingly.
"""

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "FeedbackObject",
    "parse_pytest_failures",
    "to_objects",
    "format_objects",
    "FAILURE_TYPES",
]

# The closed set of failure classifications (a shared vocabulary between the
# verifier and the model-facing feedback loops; stable names).
FAILURE_TYPES = (
    "assertion_mismatch",  # assert X == Y (or bare assert) failed
    "exception",  # an unexpected exception escaped the test
    "collection_error",  # pytest could not even import/collect the tests
    "timeout",  # the run was killed by the verify timeout
    "unparseable",  # output wasn't recognizable pytest — raw tail carried
)

# Short traceback cap: enough for the failing statements + E-lines, never a dump.
_TRACEBACK_MAX_LINES = 12
# Cap on any single value lifted out of hostile/absurd output.
_VALUE_MAX_CHARS = 300
# Raw-tail cap for fallback objects (diagnostic, not model-facing).
_TAIL_MAX_CHARS = 800


@dataclass
class FeedbackObject:
    """One structured test failure, parsed from pytest output (Boundary 7).

    Assumes fields come from parse_pytest_failures() or a hand-built fallback:
    test_id is a pytest node id or None; failure_type is one of FAILURE_TYPES;
    expected/actual are raw pytest-rendered strings or None when the failure
    has no such pair (a bare assert has neither; a KeyError has an actual but
    no expected); file is repo-relative with no anchor decoration; line is
    the failing statement's line number or None; traceback_summary keeps only
    the '>' statement lines, 'E' explanation lines, and location lines.
    """

    test_id: Optional[str] = None
    failure_type: str = "unparseable"
    summary: str = ""
    expected: Optional[str] = None
    actual: Optional[str] = None
    file: Optional[str] = None
    line: Optional[int] = None
    traceback_summary: str = ""
    raw_tail: str = field(default="", repr=False, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-ready dict (raw_tail omitted — it is diagnostic, not contract)."""
        d = asdict(self)
        d.pop("raw_tail", None)
        return d


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _shorten(value: Optional[str]) -> Optional[str]:
    """Cap a lifted value at _VALUE_MAX_CHARS with a truncation marker."""
    if value is None:
        return None
    value = value.strip()
    if len(value) > _VALUE_MAX_CHARS:
        return value[:_VALUE_MAX_CHARS] + "...(truncated)"
    return value


def _e_body(e_line: str) -> str:
    """Strip the 'E ' prefix (and any run of leading spaces) from an E-line."""
    body = e_line.strip()
    if body.startswith("E"):
        body = body[1:].lstrip()
    return body


def _is_summary_divider(line: str) -> bool:
    """True for pytest's '==== short test summary info ====' divider."""
    s = line.strip()
    return s.startswith("=") and "short test summary" in s


def _parse_assert_values(e_line: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract (expected, actual) from an E-line carrying 'assert L == R'.

    Assumes e_line is one of pytest's explanation lines. pytest renders the
    failing comparison as `assert <left> == <right>` where LEFT is what the
    code actually produced and RIGHT is the expected side — so this returns
    (right, left) as (expected, actual). Peels 'E ', 'AssertionError:' and
    'assert ' prefixes; splits on the LAST ' == ' (chained comparisons are
    right-associative in pytest's rendering). Returns (None, None) when the
    line has no '==' (bare assert / custom-message assert) — never guesses.
    """
    body = _e_body(e_line)
    if body.startswith("AssertionError:"):
        body = body[len("AssertionError:") :].strip()
    if body.startswith("assert "):
        body = body[len("assert ") :]
    if " == " not in body:
        return (None, None)
    left, right = body.rsplit(" == ", 1)
    return (_shorten(right), _shorten(left))


def _exception_name(e_line: str) -> str:
    """Exception name from an E-line like 'E   ZeroDivisionError: division by zero'."""
    body = _e_body(e_line)
    name = body.split(":", 1)[0].split("(", 1)[0].strip()
    return name or "Exception"


def _exception_message(e_line: str) -> str:
    """Message after the colon of an E-line exception ('' when there is none)."""
    body = _e_body(e_line)
    _, _, rest = body.partition(":")
    return _shorten(rest) or ""


def _location_from_line(line: str) -> Tuple[Optional[str], Optional[int]]:
    """(file, line) from a 'path/to/test.py:12: ErrorName' frame/location line.

    Assumes pytest's convention of terminating failure sections with the final
    frame 'file:line: ErrorName'. Returns (None, None) when the line doesn't
    match (e.g. an E-line or source context).
    """
    core = line.strip()
    if core.startswith("E") or core.startswith(">"):
        return (None, None)
    parts = core.split(":")
    if len(parts) >= 3 and parts[0].endswith(".py"):
        try:
            return (parts[0], int(parts[1]))
        except ValueError:
            return (None, None)
    return (None, None)


def _summarize_assertion(
    bare_name: str, core_e: str, expected: Optional[str], actual: Optional[str]
) -> str:
    """One-line assertion summary with values inline ('expected X, got Y')."""
    if expected is not None and actual is not None:
        return f"{bare_name} failed: expected {expected}, got {actual}"
    # No value pair (bare assert / custom-message assert): keep pytest's own
    # core line, capped — better than dropping the only signal there is.
    body = _e_body(core_e)
    if body.startswith("AssertionError:"):
        body = body[len("AssertionError:") :].strip()
    return f"{bare_name} failed: {body[:_VALUE_MAX_CHARS] or 'assertion failed'}"


def _traceback_tail(section_lines: List[str]) -> str:
    """Concise traceback: '>' statement lines, 'E' explanation lines, and
    'file:line: Error' location lines only — no source context, no docstrings,
    no progress bars. Capped at _TRACEBACK_MAX_LINES.
    """
    keep: List[str] = []
    for ln in section_lines:
        s = ln.rstrip()
        st = s.strip()
        if not st:
            continue
        # pytest's failing-statement marker is '>' + whitespace; doctest lines
        # ('>>> mean(...)') start with '>>' and must not be kept.
        if st.startswith(">") and not st.startswith(">>"):
            keep.append(st)
        elif st.startswith("E"):
            keep.append(st)
        else:
            # Location lines are kept only when they carry the exception name
            # ('numlib/mathutil.py:13: ZeroDivisionError') — bare mid-traceback
            # markers ('tests/test_mathutil.py:13:') are navigational noise.
            loc = _location_from_line(st)
            if loc[0] is not None and ":" in st.split(":", 2)[2]:
                keep.append(st)
    if not keep:
        return ""
    return "\n".join(keep[-_TRACEBACK_MAX_LINES:])


def _node_ids_from_summary(lines: List[str]) -> Dict[str, str]:
    """Map bare test name -> full node id from 'FAILED <node id> - <msg>' lines.

    Assumes pytest -q's short summary block format. The map keys on the node
    id's LAST '::' segment so failure sections (which carry only the bare
    name) can be upgraded to full node ids.
    """
    mapping: Dict[str, str] = {}
    for ln in lines:
        s = ln.strip()
        if not s.startswith("FAILED "):
            continue
        node = s[len("FAILED ") :].split(" - ")[0].split(" -")[0].strip()
        if not node:
            continue
        bare = node.split("::")[-1].split("[")[0].strip()
        if bare:
            mapping.setdefault(bare, node)
    return mapping


# ---------------------------------------------------------------------------
# Section parsing
# ---------------------------------------------------------------------------


def _is_section_header(line: str) -> bool:
    """True for '____ test_name ____' failure-section headers (not dividers)."""
    s = line.strip()
    return (
        s.startswith("___")
        and s.endswith("___")
        and len(s) > 8
        and s.count("_") < len(s)  # a pure '='/'_' divider has no name inside
    )


def _parse_chunk(chunk: str) -> List[FeedbackObject]:
    """Parse one pytest run's output ('FAILURES' block) into FeedbackObjects.

    Assumes chunk is a single run's text (verify() splits joined raw_output
    on '$ ' command headers). Returns [] when the chunk has no FAILURES block
    (passes, collection errors — the caller handles those via fallbacks).
    """
    if "FAILURES" not in chunk:
        return []
    lines = chunk.splitlines()
    node_map = _node_ids_from_summary(lines)

    objects: List[FeedbackObject] = []
    current: Optional[List[str]] = None
    for ln in lines:
        if _is_section_header(ln):
            if current is not None:
                obj = _build_object(current, node_map)
                if obj is not None:
                    objects.append(obj)
            current = [ln]
        elif current is not None:
            if _is_summary_divider(ln):
                break
            current.append(ln)
    if current is not None:
        obj = _build_object(current, node_map)
        if obj is not None:
            objects.append(obj)
    return objects


def _build_object(
    section_lines: List[str], node_map: Dict[str, str]
) -> Optional[FeedbackObject]:
    """Build one FeedbackObject from a single failure section's lines.

    Assumes section_lines[0] is the '___ name ___' header. Returns None for
    sections without a usable name (hostile output must never crash parsing).
    """
    header = section_lines[0].strip().strip("_").strip()
    if not header:
        return None
    bare_name = header.split("::")[-1].split(" ")[0] or header
    # Node-id resolution: class sections are headed 'Class.test_x' while the
    # short summary carries '...::Class::test_x' — try the exact header,
    # then the header's last dot-segment, then the header itself if it
    # already looks like a path/id.
    test_id: Optional[str] = None
    for key in (bare_name, bare_name.split(".")[-1]):
        if key in node_map:
            test_id = node_map[key]
            break
    if test_id is None and ("::" in header or "/" in header):
        test_id = header

    e_lines = [ln for ln in section_lines if ln.strip().startswith("E")]
    location: Tuple[Optional[str], Optional[int]] = (None, None)
    for ln in reversed(section_lines):
        loc = _location_from_line(ln)
        if loc[0] is not None:
            location = loc
            break

    # ---- assertion failures -------------------------------------------------
    # pytest emits either 'E   AssertionError: ...' (message/custom) or a bare
    # 'E   assert L == R' (or both, message first, comparison second).
    assert_e: Optional[str] = None
    for ln in e_lines:
        body = _e_body(ln)
        if body.startswith("assert ") or body.startswith("AssertionError:"):
            assert_e = ln
            break
    if assert_e is not None:
        expected: Optional[str] = None
        actual: Optional[str] = None
        # First E-line that actually carries an '== ' pair wins; a custom-
        # message assert ('AssertionError: ordinal(111)') often has the pair
        # on a FOLLOWING E-line.
        for ln in e_lines:
            expected, actual = _parse_assert_values(ln)
            if expected is not None and actual is not None:
                break
        if (expected is None or actual is None) and len(e_lines) > 1:
            # Bare assert with a 'where' decoration: 'E + where 6.0 = mean(...)'
            # completes the actual value.
            for ln in e_lines:
                if " where " in ln:
                    w = ln.split(" where ", 1)[1].strip()
                    w = w.split(" = ", 1)[-1].strip() if " = " in w else w
                    if actual is None:
                        actual = _shorten(w)
                    break
        return FeedbackObject(
            test_id=test_id or bare_name,
            failure_type="assertion_mismatch",
            summary=_summarize_assertion(bare_name, assert_e, expected, actual),
            expected=expected,
            actual=actual,
            file=location[0],
            line=location[1],
            traceback_summary=_traceback_tail(section_lines),
        )

    # ---- unexpected exceptions ----------------------------------------------
    if e_lines:
        core = e_lines[0]
        name = _exception_name(core)
        msg = _exception_message(core)
        return FeedbackObject(
            test_id=test_id or bare_name,
            failure_type="exception"
            if name != "AssertionError"
            else "assertion_mismatch",
            summary=f"{bare_name} raised {name}" + (f": {msg}" if msg else ""),
            actual=_shorten(msg) or None,
            file=location[0],
            line=location[1],
            traceback_summary=_traceback_tail(section_lines),
        )

    # ---- exception traceback without E-lines (rare; e.g. -q with --tb=short)
    exc_line = ""
    for ln in reversed(section_lines):
        if _location_from_line(ln)[0] is not None:
            exc_line = ln.strip()
            break
    name = _exception_name(exc_line) if exc_line else "Exception"
    return FeedbackObject(
        test_id=test_id or bare_name,
        failure_type="exception",
        summary=f"{bare_name} raised {name}",
        file=location[0],
        line=location[1],
        traceback_summary=_traceback_tail(section_lines),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_pytest_failures(raw_output: str) -> List[FeedbackObject]:
    """Parse pytest console output into FeedbackObjects (pure; never raises).

    Assumes raw_output is verify()'s combined output — possibly several runs
    joined (each '$ <cmd>' starts a new run). Returns one FeedbackObject per
    FAILED test, in order, deduplicated across the target/suite runs (the
    same failure typically appears in both). Empty when nothing parseable —
    callers wanting graceful fallbacks use to_objects().
    """
    objects: List[FeedbackObject] = []
    seen = set()
    for chunk in raw_output.split("$ "):
        if "FAILURES" not in chunk:
            continue
        for obj in _parse_chunk(chunk):
            key = (obj.test_id, obj.file, obj.line)
            if key in seen:
                continue
            seen.add(key)
            objects.append(obj)
    return objects


def _chunk_timed_out(chunk: str) -> bool:
    """True when one run-chunk of verify()'s raw_output was a timeout run.

    Assumes chunk text between '$ ' command headers. verify()'s _format_run
    annotates timed-out runs as 'exit=124 ... TIMEOUT' on the exit line —
    detectable from raw_output alone, so a consumer holding only a
    VerificationResult (e.g. one replayed from trace.jsonl) still gets
    correct timeout classification.
    """
    for ln in chunk.splitlines():
        s = ln.strip()
        if s.startswith("exit="):
            code = s.split()[0][len("exit=") :]
            return s.endswith("TIMEOUT") or code == "124"
    return False


def to_objects(
    raw_output: str,
    *,
    target_test: Optional[str] = None,
    timed_out: bool = False,
    exit_code: int = 0,
) -> List[FeedbackObject]:
    """Parse verification output into FeedbackObjects, with graceful fallbacks.

    Assumes raw_output is a verify() raw_output string (possibly several runs
    joined — each '$ <cmd>' header starts a new run). Order of precedence:
    1. explicit timed_out/exit_code args (the caller knows the run died) ->
       ONE timeout object (a killed run's partial output would mislead);
    2. per-chunk parsing: timeout-marked chunks ('exit=124 ... TIMEOUT')
       yield timeout objects, FAILURES blocks yield per-test objects,
       deduplicated (the same failure typically appears in target AND suite
       runs); target_test annotates objects still missing a node id;
    3. nothing parseable (collection error, crash, empty) -> ONE fallback
       object so a consumer never gets [] for a failing run.
    """
    if timed_out or exit_code == 124:
        return [
            FeedbackObject(
                test_id=target_test,
                failure_type="timeout",
                summary=f"test run exceeded its time budget (target: {target_test or 'full suite'})",
                traceback_summary="Killed by the harness verify timeout.",
            )
        ]

    objects: List[FeedbackObject] = []
    seen = set()
    for chunk in raw_output.split("$ ")[1:]:
        if _chunk_timed_out(chunk):
            key = ("__timeout__",)
            if key not in seen:
                seen.add(key)
                objects.append(
                    FeedbackObject(
                        test_id=target_test,
                        failure_type="timeout",
                        summary="test run exceeded its time budget and was killed",
                        traceback_summary="Killed by the harness verify timeout.",
                    )
                )
            continue
        for obj in _parse_chunk(chunk):
            key = (obj.test_id, obj.file, obj.line)
            if key in seen:
                continue
            seen.add(key)
            if target_test and obj.test_id is None:
                obj.test_id = target_test
            objects.append(obj)

    if objects:
        if target_test:
            for o in objects:
                if o.test_id is None:
                    o.test_id = target_test
        return objects

    stripped = raw_output.strip()
    if any(
        marker in stripped
        for marker in (
            "ERROR collecting",
            "no tests ran",
            "ImportError",
            "ModuleNotFoundError",
            "SyntaxError",
            "errors during collection",
            "Interrupted",
        )
    ):
        first = ""
        for ln in stripped.splitlines():
            s = ln.strip()
            if s and not s.startswith("===") and not s.startswith("$"):
                first = s[:_VALUE_MAX_CHARS]
                break
        return [
            FeedbackObject(
                test_id=target_test,
                failure_type="collection_error",
                summary="pytest could not collect the tests (import/collection error)"
                + (f": {first}" if first else ""),
                traceback_summary=stripped[-_TAIL_MAX_CHARS:],
                raw_tail=stripped[-_TAIL_MAX_CHARS:],
            )
        ]
    tail = stripped[-_TAIL_MAX_CHARS:] if stripped else "(no output)"
    first = ""
    for ln in stripped.splitlines():
        s = ln.strip()
        if s and not s.startswith("===") and not s.startswith("$"):
            first = s[:_VALUE_MAX_CHARS]
            break
    return [
        FeedbackObject(
            test_id=target_test,
            failure_type="unparseable",
            summary="verification failed with unrecognized output"
            + (f": {first}" if first else ""),
            traceback_summary=tail,
            raw_tail=tail,
        )
    ]


def feedback_from_result(v, target_test: Optional[str] = None) -> List[FeedbackObject]:
    """Structured feedback from any VerificationResult (Boundary 7 convenience).

    Assumes v is a shared.types.VerificationResult as returned by verify()
    (real or stub — both write the same '$ cmd / exit=N [TIMEOUT]' raw shape).
    Timeout classification is auto-detected from the raw_output's own
    markers, so historical results replayed from trace.jsonl work too.
    """
    return to_objects(v.raw_output, target_test=target_test)


def format_objects(objects: List[FeedbackObject], max_objects: int = 3) -> str:
    """Render FeedbackObjects as the model-facing feedback text (compact).

    Assumes objects is a to_objects() result. Produces one line per failure
    plus at most two traceback lines each, capped at max_objects with an
    explicit '+N more' marker — the replacement for raw_output[-1500:] tails
    in the harness's step/attempt feedback. E.g.::

        FAILED tests/test_mathutil.py::test_mean_two - assertion_mismatch:
        test_mean_two failed: expected 3, got 6.0 (tests/test_mathutil.py:5)
          E assert 6.0 == 3
    """
    if not objects:
        return "All checks passed."
    out: List[str] = []
    for o in objects[:max_objects]:
        line = f"FAILED {o.test_id or '(unknown test)'} - {o.failure_type}: {o.summary}"
        if o.file:
            where = f"{o.file}:{o.line}" if o.line is not None else o.file
            line += f" ({where})"
        out.append(line)
        if o.traceback_summary:
            tb_lines = [t for t in o.traceback_summary.splitlines() if t.strip()]
            # The core signal: the failing statement and the FIRST E-line
            # (the comparison/exception head) — later E-lines are diff/where
            # decoration pytest adds around it.
            for tb_line in tb_lines[:2]:
                if tb_line not in line:
                    out.append(f"  {tb_line}")
    if len(objects) > max_objects:
        out.append(f"  (+{len(objects) - max_objects} more failure(s))")
    return "\n".join(out)
