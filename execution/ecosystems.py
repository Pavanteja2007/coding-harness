"""The ONE per-language ecosystem registry (INTERFACES.md, Ceiling Round 2 / R2-12).

Everything language-shaped in this repository used to be an ``if lang == ...``
chain with the knowledge split across four files: which command runs the suite
lives in ``execution/verify.py``, which globs count as tests lives in
``harness/config.py::DEFAULTS["protected_paths"]`` (and was Python-only), which
runner configurations are protected lives in ``harness/test_config.py``, and
which base image a repository gets lives in ``execution/sandbox.py``. Adding a
language meant editing all four and hoping they agreed.

This module is the one table they now all consult. An :class:`Ecosystem` is
DATA: the suite command, the build/lint/format commands, the test-file globs,
the protected-path globs, the structured result format, the zero-test policy,
the toolchain binaries, the sandbox base image, the dependency manifests, the
target-command template, and the protected test-configuration surfaces.
Registering a new language is a data change; no consumer needs a new branch,
and ``tests/test_ceiling_r2_12_polyglot.py`` proves that by registering a
language that appears nowhere else in the tree.

Three properties are ENFORCED rather than documented, because each one is a
known way a coding agent manufactures a false success:

1. **A zero collected test count is never a pass, in any ecosystem.** The
   policy is a CLOSED set (:data:`ZERO_TEST_POLICIES`) and
   :func:`register_ecosystem` refuses anything outside it. There is no
   "allow an empty suite" value, so a future language cannot be registered
   into the vacuous-green class. ``fail_closed`` additionally requires
   POSITIVE evidence that a test ran, which is what catches Go's
   ``? pkg [no test files]`` -> exit 0 shape that the exit code alone calls a
   pass.
2. **A missing toolchain is a refusal, never a pass.** The binaries an
   ecosystem needs are declared, and :func:`toolchain_probe_command` builds the
   in-sandbox probe that turns "the runner is not installed" into the distinct
   :data:`OUTCOME_TOOLCHAIN_UNAVAILABLE` outcome rather than an exit code a
   naive reader could round to success.
3. **The structured-result channel is fed, not merely present.** For an
   ecosystem whose runner can emit a machine-readable report, the composed
   command asks for it and echoes it back inside a per-call sentinel block, and
   :func:`normalize_structured` hands the payload to
   ``execution.result_parsing.parse_test_run`` through the ``junit_xml=`` /
   ``json_report=`` keyword-only parameters that already existed and had NO
   production caller. ``source`` on the resulting report then reads
   ``"report"`` with ``confidence="high"`` instead of scraping prose.

Everything here is total and never raises for a hostile or absent repository:
detection returns ``None``, an unknown ecosystem name returns ``None``, and a
malformed report degrades to ``None`` so the caller falls back instead of
inventing counts.

Configuration discipline: this module owns NO ``harness/config.py::DEFAULTS``
key with a real value. ``DEFAULTS`` is merged into every task and every eval
arm, so a behaviour-changing default there switches all of them at once; the
keys this round adds are ``None``-valued (behaviour-neutral) or absent.
"""

import fnmatch
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from dataclasses import replace as _dataclass_replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

#: Bumped when a field's MEANING changes, not when a field is added.
SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# The closed vocabularies. Each is a policy boundary, not an enum convenience:
# ``register_ecosystem`` validates against them so an ecosystem cannot opt out
# of the honesty invariants by registering a value nobody thought to check.
# ---------------------------------------------------------------------------

#: Machine-readable result formats. ``prose`` means the runner has no built-in
#: structured report, which is a fact about the runner and is recorded as such
#: rather than papered over.
FORMAT_PROSE = "prose"
FORMAT_JUNIT_XML = "junit_xml"
FORMAT_PYTEST_JSON = "pytest_json"
FORMAT_GO_TEST_JSON = "go_test_json"
RESULT_FORMATS = frozenset(
    {FORMAT_PROSE, FORMAT_JUNIT_XML, FORMAT_PYTEST_JSON, FORMAT_GO_TEST_JSON}
)

#: How the structured payload is obtained from the runner.
#:
#: - ``none``     the runner has no structured report.
#: - ``sentinel`` the runner writes a report to a path we name; the command
#:   echoes that file back inside a per-call sentinel block and preserves the
#:   runner's own exit code.
#: - ``inline``   the runner's stdout IS the structured stream (``go test -json``).
CAPTURE_NONE = "none"
CAPTURE_SENTINEL = "sentinel"
CAPTURE_INLINE = "inline"
REPORT_CAPTURES = frozenset({CAPTURE_NONE, CAPTURE_SENTINEL, CAPTURE_INLINE})

#: Zero-test policies. BOTH members refuse an empty suite; they differ only in
#: how strict the evidence must be.
#:
#: - ``parser``      ``execution.result_parsing.parse_test_run`` decides, which
#:   already treats exit 5, a ``0 passed`` / ``0 tests`` count, a no-tests
#:   marker, an empty capture at exit 0, and ``expected_tests>=1`` with nothing
#:   collected as ``no_tests``.
#: - ``fail_closed`` the above PLUS the ecosystem's own ``zero_test_markers``
#:   and a requirement of POSITIVE collected-test evidence before a zero exit
#:   may be called a pass. This is what a runner needs when it can exit 0
#:   having collected nothing.
ZERO_TEST_PARSER = "parser"
ZERO_TEST_FAIL_CLOSED = "fail_closed"
ZERO_TEST_POLICIES = frozenset({ZERO_TEST_PARSER, ZERO_TEST_FAIL_CLOSED})

#: How the suite command for a repository is obtained.
#:
#: - ``pytest`` / ``jest`` delegate to the EXISTING detectors in
#:   ``execution/verify.py`` so there is never a second language detector in
#:   the tree. The registry still owns the DECISION of which family a
#:   repository belongs to; the detector owns the mechanics pytest and
#:   jest/vitest already needed.
#: - ``runner_template`` uses the registry's own ``test_command`` plus the
#:   target-command fields, which is how a language with no bespoke detector
#:   gets one for free.
COMMAND_PYTEST = "pytest"
COMMAND_JEST = "jest"
COMMAND_TEMPLATE = "runner_template"
COMMAND_FAMILIES = frozenset({COMMAND_PYTEST, COMMAND_JEST, COMMAND_TEMPLATE})

#: The families whose command is resolved by ``execution/verify.py``'s existing
#: per-runner detection rather than by this registry.
DELEGATED_FAMILIES = frozenset({COMMAND_PYTEST, COMMAND_JEST})

# --- the gate outcome vocabulary -------------------------------------------
#: A run that collected no tests. Distinct from "fail", from "skipped", and
#: from "flaky": it blocks a verified success and says why. The parser-level
#: spelling is ``execution.result_parsing.OUTCOME_NO_TESTS``; this is the
#: gate-level name the receipt and the trace carry, and :func:`gate_outcome` is
#: the only thing that translates between them.
OUTCOME_NO_TESTS_COLLECTED = "no_tests_collected"
#: The declared toolchain is not installed in the environment the run used.
OUTCOME_TOOLCHAIN_UNAVAILABLE = "toolchain_unavailable"
#: Re-exported so a consumer has ONE vocabulary to import.
OUTCOME_PASS = "pass"
OUTCOME_FAIL = "fail"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_ERROR = "error"
#: Every gate outcome this module can produce. A closed set so a consumer can
#: reject an unknown value instead of defaulting to "fine".
GATE_OUTCOMES = frozenset(
    {
        OUTCOME_PASS,
        OUTCOME_FAIL,
        OUTCOME_TIMEOUT,
        OUTCOME_ERROR,
        OUTCOME_NO_TESTS_COLLECTED,
        OUTCOME_TOOLCHAIN_UNAVAILABLE,
    }
)

#: Emitted by :func:`toolchain_probe_command` when a required binary is absent.
#: A stable token matched BY NAME, never by a human reading a line, so it can
#: neither be missed by a paraphrase nor triggered by one.
TOOLCHAIN_UNAVAILABLE_MARKER = "VEX-TOOLCHAIN-UNAVAILABLE"

#: Report payloads are read back through the sandbox's stdout, which is itself
#: bounded. A payload larger than this is dropped and recorded as absent rather
#: than parsed as if it were whole.
MAX_REPORT_BYTES = 4_000_000

#: How many per-test outcomes ride on a receipt. The full list stays available
#: from :func:`parse_cases`; the receipt carries a bounded head plus the true
#: total, so a truncated list is never mistaken for a complete one.
MAX_RECEIPT_CASES = 200

#: The report file is written INSIDE the container, never into the bind-mounted
#: repository. A JUnit file landing in the work tree would show up in
#: ``harness.editor.changed_files`` and become part of the delivered diff.
DEFAULT_REPORT_PATH = "/tmp/neo-structured-result.xml"

#: Characters that must never reach a composed shell command.
_UNSAFE_COMMAND_CHARS = re.compile(r"[\r\n\x00]")

_REGEX_METACHARS = re.compile(r"([\\^$.|?*+()\[\]{}])")


class EcosystemPolicyError(ValueError):
    """Raised when an ecosystem registration would weaken a policy boundary.

    Assumes nothing about the caller. The message always names the field and
    the closed set it violated, because the caller is a registry author and
    needs to know what to write instead.
    """


@dataclass(frozen=True)
class TestOutcome:
    """One test's structured outcome, as the receipt reports it.

    ``outcome`` is one of ``pass`` / ``fail`` / ``skip``. ``test_id`` is the
    runner's OWN identifier (a Go test name, a JUnit ``classname::name``), not
    a re-derivation, so a receipt row can be matched back to the runner's
    report.
    """

    test_id: str
    outcome: str
    file: str = ""
    line: Optional[int] = None
    message: str = ""
    duration_s: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe dict. Assumes the field values are primitives."""
        return {
            "test_id": self.test_id,
            "outcome": self.outcome,
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "duration_s": self.duration_s,
        }


@dataclass(frozen=True)
class Ecosystem:
    """One language's build/test/lint/format contract, as data.

    Assumes ``name`` is a lower-case token unique in the registry. Every
    tuple field is ordered and immutable so a receipt is reproducible.

    ``surfaces`` maps a runner-configuration FILE NAME to ``(kind, tables)``
    exactly as ``harness/test_config.py`` defines it, and is pushed there on
    registration through that module's documented
    ``register_language_surfaces`` seam rather than by editing its tables.

    The target-command fields are the whole of "scope a run to one test", also
    as data: ``test_command_base`` is the runner invocation without its
    trailing package pattern, ``package_flag`` is how a package/directory is
    passed (empty when the runner takes none), ``default_scope`` is the scope
    used when the target id names none, ``target_test_flag`` is the runner's
    test-name flag (empty means the name is positional), and
    ``target_test_template`` renders the name itself — which is where Go's
    ``^...$`` anchoring lives, because an unanchored ``-run`` also runs every
    longer test name that contains the target.
    """

    name: str
    language: str
    command_family: str
    test_command: str
    test_command_base: str = ""
    test_globs: Tuple[str, ...] = ()
    protected_globs: Tuple[str, ...] = ()
    build_command: Optional[str] = None
    lint_command: Optional[str] = None
    format_command: Optional[str] = None
    package_flag: str = ""
    default_scope: str = ""
    target_test_flag: str = ""
    target_test_template: str = "{name}"
    result_format: str = FORMAT_PROSE
    report_capture: str = CAPTURE_NONE
    report_arg: Optional[str] = None
    report_path: str = DEFAULT_REPORT_PATH
    zero_test_policy: str = ZERO_TEST_PARSER
    zero_test_markers: Tuple[str, ...] = ()
    toolchain: Tuple[str, ...] = ()
    sandbox_image: str = ""
    dep_manifests: Tuple[str, ...] = ()
    markers: Tuple[str, ...] = ()
    primary_markers: Tuple[str, ...] = ()
    surfaces: Mapping[str, Tuple[str, Tuple[str, ...]]] = field(default_factory=dict)
    notes: str = ""

    # -- derived ------------------------------------------------------------
    @property
    def command_base(self) -> str:
        """The runner invocation to scope, defaulting to the suite command."""
        return self.test_command_base or self.test_command

    @property
    def structured(self) -> bool:
        """True when this ecosystem can produce a machine-readable report."""
        return self.report_capture != CAPTURE_NONE

    @property
    def requires_positive_test_evidence(self) -> bool:
        """True when a zero exit needs positive evidence before it is a pass."""
        return self.zero_test_policy == ZERO_TEST_FAIL_CLOSED

    # -- validation ---------------------------------------------------------
    def validate(self) -> None:
        """Raise :class:`EcosystemPolicyError` if this entry is unusable.

        Checks the CLOSED sets and the cross-field agreements they imply, so a
        bad entry is refused at registration rather than producing a command
        that cannot run. Never returns a value; raises on the first problem.
        """
        if not str(self.name or "").strip():
            raise EcosystemPolicyError("ecosystem name must be a non-empty token")
        if not self.language:
            raise EcosystemPolicyError(
                f"{self.name}: language must be a non-empty token"
            )
        if self.command_family not in COMMAND_FAMILIES:
            raise EcosystemPolicyError(
                f"{self.name}: command_family {self.command_family!r} is not one of "
                f"{sorted(COMMAND_FAMILIES)}"
            )
        if not self.test_command and self.command_family != COMMAND_JEST:
            # The jest family is resolved per repository (jest OR vitest, never
            # guessed) by execution.verify's existing detector, so its entry
            # carries no suite command of its own.
            raise EcosystemPolicyError(
                f"{self.name}: test_command must be non-empty unless command_family "
                f"is {COMMAND_JEST!r}"
            )
        if self.result_format not in RESULT_FORMATS:
            raise EcosystemPolicyError(
                f"{self.name}: result_format {self.result_format!r} is not one of "
                f"{sorted(RESULT_FORMATS)}"
            )
        if self.report_capture not in REPORT_CAPTURES:
            raise EcosystemPolicyError(
                f"{self.name}: report_capture {self.report_capture!r} is not one of "
                f"{sorted(REPORT_CAPTURES)}"
            )
        if self.zero_test_policy not in ZERO_TEST_POLICIES:
            raise EcosystemPolicyError(
                f"{self.name}: zero_test_policy {self.zero_test_policy!r} is not one "
                f"of {sorted(ZERO_TEST_POLICIES)}; every policy in that set refuses "
                "an empty suite and there is deliberately no permissive value"
            )
        if self.report_capture == CAPTURE_SENTINEL:
            if not self.report_arg or "{path}" not in self.report_arg:
                raise EcosystemPolicyError(
                    f"{self.name}: report_capture='sentinel' requires report_arg "
                    "containing '{path}'"
                )
            if self.result_format not in (FORMAT_JUNIT_XML, FORMAT_PYTEST_JSON):
                raise EcosystemPolicyError(
                    f"{self.name}: a sentinel report must be junit_xml or "
                    f"pytest_json, not {self.result_format!r}"
                )
        if self.report_capture == CAPTURE_INLINE:
            if self.result_format == FORMAT_PROSE:
                raise EcosystemPolicyError(
                    f"{self.name}: report_capture='inline' needs a structured "
                    "result_format, not 'prose'"
                )
            if self.report_arg:
                raise EcosystemPolicyError(
                    f"{self.name}: report_capture='inline' must not declare a "
                    "report_arg; the runner's own stdout is the stream"
                )
        if self.report_capture == CAPTURE_NONE and self.report_arg:
            raise EcosystemPolicyError(
                f"{self.name}: report_arg is set but report_capture is 'none'"
            )
        if self.command_family == COMMAND_TEMPLATE and not self.test_command:
            raise EcosystemPolicyError(
                f"{self.name}: command_family='runner_template' requires a "
                "test_command this registry composes the target command from"
            )
        if not self.toolchain:
            raise EcosystemPolicyError(
                f"{self.name}: toolchain must name at least one binary; a language "
                "with no declared toolchain cannot report 'unavailable' honestly"
            )
        if not self.markers:
            raise EcosystemPolicyError(
                f"{self.name}: markers must name at least one identifying file, or "
                "detection would claim every repository"
            )
        for field_name, value in (
            ("test_command", self.test_command),
            ("test_command_base", self.test_command_base),
            ("package_flag", self.package_flag),
            ("target_test_flag", self.target_test_flag),
            ("target_test_template", self.target_test_template),
        ):
            if _UNSAFE_COMMAND_CHARS.search(value or ""):
                raise EcosystemPolicyError(
                    f"{self.name}: {field_name} contains a line break or NUL, which "
                    "must never reach a composed shell command"
                )

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection for a receipt or trace row."""
        return {
            "schema_version": SCHEMA_VERSION,
            "name": self.name,
            "language": self.language,
            "command_family": self.command_family,
            "test_command": self.test_command,
            "test_globs": list(self.test_globs),
            "protected_globs": list(self.protected_globs),
            "build_command": self.build_command,
            "lint_command": self.lint_command,
            "format_command": self.format_command,
            "result_format": self.result_format,
            "report_capture": self.report_capture,
            "zero_test_policy": self.zero_test_policy,
            "zero_test_markers": list(self.zero_test_markers),
            "toolchain": list(self.toolchain),
            "sandbox_image": self.sandbox_image,
            "dep_manifests": list(self.dep_manifests),
        }


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

_REGISTRY: Dict[str, Ecosystem] = {}
#: Registration order IS detection precedence: the first ecosystem whose markers
#: are present wins unless a later candidate carries a primary marker present on
#: disk. Ordered most-specific first, so a repository carrying a ``go.mod`` AND
#: a ``package.json`` (a vendored web asset, a docs site) is a Go repository.
_ORDER: List[str] = []


def register_ecosystem(eco: Ecosystem, *, replace: bool = False) -> Ecosystem:
    """Add one ecosystem to the registry and publish its protected surfaces.

    Assumes ``eco`` is an :class:`Ecosystem`. Raises
    :class:`EcosystemPolicyError` when the entry would weaken a policy boundary,
    and ``ValueError`` when ``replace`` is False and the name is already
    registered — a silent overwrite would let one import change another
    module's verdicts.

    The protected test-configuration surfaces are forwarded to
    ``harness.test_config.register_language_surfaces`` (that module's
    documented extension seam) inside a guarded import, so this module does not
    make ``execution`` depend on ``harness`` at import time and a broken
    test-config module cannot stop the registry from loading. A failed
    forwarding is recorded on the returned entry's ``notes`` rather than
    raised: the surfaces are an ADDITIVE protection, and the registry is still
    correct without them.
    """
    if not isinstance(eco, Ecosystem):
        raise ValueError("register_ecosystem expects an Ecosystem")
    eco.validate()
    if eco.name in _REGISTRY and not replace:
        raise ValueError(
            f"ecosystem {eco.name!r} is already registered; pass replace=True to "
            "change an existing entry deliberately"
        )
    if eco.surfaces:
        # Aliased import: this function's own ``replace`` parameter shadows the
        # dataclasses helper of the same name, and calling the bool would be a
        # TypeError on a path that only runs when surfaces fail to publish.
        note = _publish_surfaces(eco)
        if note:
            eco = _dataclass_replace(
                eco, notes=f"{eco.notes}; {note}" if eco.notes else note
            )
    if eco.name not in _REGISTRY:
        _ORDER.append(eco.name)
    _REGISTRY[eco.name] = eco
    return eco


def _publish_surfaces(eco: Ecosystem) -> str:
    """Forward ``eco.surfaces`` into ``harness.test_config``. Never raises."""
    try:
        from harness.test_config import register_language_surfaces
    except Exception as exc:  # pragma: no cover - test_config is in-tree
        return (
            "protected surfaces not published: harness.test_config unavailable "
            f"({type(exc).__name__}: {exc})"
        )
    try:
        register_language_surfaces(eco.language, dict(eco.surfaces))
    except Exception as exc:  # pragma: no cover - defensive
        return f"protected surfaces not published: {type(exc).__name__}: {exc}"
    return ""


def ecosystem(name: str) -> Optional[Ecosystem]:
    """Return the registered ecosystem called ``name``, or None.

    Assumes nothing: an unknown, empty, or non-string name yields None so a
    caller can report "no ecosystem" instead of raising inside a verifier.
    """
    return _REGISTRY.get(str(name or "").strip().lower())


def ecosystems() -> Tuple[Ecosystem, ...]:
    """Return every registered ecosystem in detection-precedence order."""
    return tuple(_REGISTRY[name] for name in _ORDER if name in _REGISTRY)


def ecosystem_names() -> Tuple[str, ...]:
    """Return every registered ecosystem name, in detection-precedence order."""
    return tuple(name for name in _ORDER if name in _REGISTRY)


def _marker_present(present: Iterable[str], marker: str) -> bool:
    """True when ``marker`` names something in ``present`` (glob-aware)."""
    for entry in present:
        if entry == marker:
            return True
        if any(ch in marker for ch in "*?[") and fnmatch.fnmatch(entry, marker):
            return True
    return False


def detect_ecosystem(repo_path: str) -> Optional[Ecosystem]:
    """Classify ``repo_path`` against the registry, or return None.

    Assumes ``repo_path`` is a directory that may not exist, may not be
    readable, and may contain none of the markers; a marker may be an exact
    name or an fnmatch glob. A marker match makes the ecosystem a CANDIDATE;
    when several candidates match, one carrying a ``primary_marker`` present on
    disk wins, and otherwise registry precedence decides. Never raises: an
    unreadable root yields None.
    """
    if not repo_path or not os.path.isdir(str(repo_path)):
        return None
    try:
        present = os.listdir(str(repo_path))
    except OSError:
        return None
    candidates = [
        eco for eco in ecosystems() if _any_marker_present(present, eco.markers)
    ]
    if not candidates:
        return None
    for eco in candidates:
        if _any_marker_present(present, eco.primary_markers):
            return eco
    return candidates[0]


def _any_marker_present(present: Iterable[str], markers: Sequence[str]) -> bool:
    """True when ANY of ``markers`` names something in ``present``."""
    return any(_marker_present(present, marker) for marker in markers)


# ---------------------------------------------------------------------------
# Command composition
# ---------------------------------------------------------------------------


def shell_quote(value: str) -> str:
    """Quote one shell word. Everything between the quotes is literal."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def regex_escape(value: str) -> str:
    """Escape regex metacharacters so a test name matches only itself."""
    return _REGEX_METACHARS.sub(r"\\\1", str(value))


def _join_flag(flag: str, value: str) -> str:
    """Join a runner flag and a quoted value.

    Assumes ``value`` is already shell-safe to quote. A flag that ends in ``=``
    (``-Dtest=``, ``--filter=``) carries its value in the same word, so a space
    between them would be a SECOND argument and the runner would see an empty
    filter; every other flag is space-separated. Never raises.
    """
    quoted = shell_quote(value)
    if not flag:
        return quoted
    return f"{flag}{quoted}" if flag.endswith("=") else f"{flag} {quoted}"


def split_target(target_test: str) -> Tuple[str, str]:
    """Split a target id into ``(scope, test_name)``.

    Accepts ``"<scope>::<name>"`` (the pytest-style habit) and a bare
    ``"<name>"`` with no scope. Never raises.
    """
    text = str(target_test or "").strip()
    if "::" in text:
        scope, _, name = text.partition("::")
        return scope.strip(), name.strip()
    return "", text


def suite_command(eco: Ecosystem) -> str:
    """Return the full-suite command for ``eco``.

    Assumes ``eco.test_command`` is non-empty; for the ``jest`` family the
    caller resolves the runner per repository first.
    """
    return eco.test_command


def target_command(eco: Ecosystem, target_test: Optional[str]) -> Optional[str]:
    """Return a command that runs only ``target_test``, or None.

    Returns ``eco.test_command`` when no target is named. For the ``pytest`` and
    ``jest`` families returns None, because those are composed by
    ``execution.verify``'s existing, test-pinned logic and a second
    implementation here would be a second language detector.

    For a ``runner_template`` ecosystem the registry's own target fields are
    filled in, and the result is refused (None) rather than composed when any
    rendered piece contains a line break or NUL.
    """
    if not target_test:
        return eco.test_command or None
    if eco.command_family != COMMAND_TEMPLATE:
        return None
    scope_raw, name = split_target(target_test)
    if not name:
        return eco.test_command or None
    parts = [eco.command_base]
    if eco.package_flag:
        scope = (
            eco.package_flag.replace("{scope}", scope_raw.strip("/"))
            if scope_raw
            else eco.default_scope
        )
        if scope:
            parts.append(scope)
    rendered = eco.target_test_template.replace(
        "{escaped}", regex_escape(name)
    ).replace("{name}", name)
    if rendered:
        parts.append(_join_flag(eco.target_test_flag, rendered))
    command = " ".join(part for part in parts if part)
    if _UNSAFE_COMMAND_CHARS.search(command) or _UNSAFE_COMMAND_CHARS.search(name):
        return None
    return command


def toolchain_probe_command(eco: Ecosystem) -> str:
    """Return the in-sandbox prefix that refuses a run with no toolchain.

    Assumes ``eco.toolchain`` is non-empty (validated at registration). The
    probe is a shell fragment PREPENDED to the runner command, so it costs no
    extra container and cannot change the runner's own exit code: a missing
    binary prints :data:`TOOLCHAIN_UNAVAILABLE_MARKER` and exits 127 — the
    shell's own "command not found" code, so a reader that ignores the marker
    still sees a non-zero exit — and a present binary falls through untouched.
    """
    checks = " && ".join(
        f"command -v {shell_quote(binary)} >/dev/null 2>&1" for binary in eco.toolchain
    )
    marker = shell_quote(f"{TOOLCHAIN_UNAVAILABLE_MARKER}: {','.join(eco.toolchain)}")
    return f"{checks} || {{ echo {marker}; exit 127; }}; "


def compose_run_command(eco: Ecosystem, command: str, *, token: str = "") -> str:
    """Return the command to dispatch for ``command`` under ``eco``.

    Assumes ``command`` is the composed suite or target command and contains no
    line break. When the ecosystem declares a sentinel report, the runner's
    report argument is appended, the runner's exit code is preserved ACROSS the
    report echo, and the report is echoed inside a sentinel block keyed by
    ``token``.

    The sentinel block is what makes the payload trustworthy: ``token`` is
    per-call and unguessable, so test OUTPUT cannot forge a report. The
    transcript is restored to exactly the runner's own bytes by
    :func:`split_report`, which is what keeps ``execution.feedback`` and
    ``execution.rationale`` parsers unaffected.
    """
    if not command:
        return command
    probe = toolchain_probe_command(eco)
    if eco.report_capture != CAPTURE_SENTINEL or not token:
        return probe + command
    report_path = eco.report_path
    start, end = f"{token}-START", f"{token}-END"
    report_arg = (eco.report_arg or "").replace("{path}", report_path)
    return (
        f"{probe}{{ {command} {report_arg}; }} ; __neo_rc=$? ; "
        f"printf '%s\\n' {shell_quote(start)} ; "
        f"cat {report_path} 2>/dev/null ; "
        f"printf '%s\\n' {shell_quote(end)} ; "
        f"exit $__neo_rc"
    )


def split_report(text: str, token: str) -> Tuple[str, Optional[str]]:
    """Split a capture into ``(runner_output, report_payload_or_None)``.

    Assumes ``token`` is the per-call token :func:`compose_run_command` used.
    The LAST start marker and the first end marker after it are honoured, so a
    runner that happens to print the token cannot pre-empt the real block, and
    the runner's own bytes are returned with the block removed. A missing start
    marker, a missing end marker, or an oversized payload all yield
    ``(text, None)`` so the caller falls back to the prose path instead of
    parsing half a report.
    """
    if not token or not text:
        return text, None
    start, end = f"{token}-START", f"{token}-END"
    start_at = text.rfind(start)
    if start_at < 0:
        return text, None
    end_at = text.find(end, start_at)
    if end_at < 0:
        return text, None
    payload = text[start_at + len(start) : end_at]
    remainder = (text[:start_at] + text[end_at + len(end) :]).strip("\r\n")
    if len(payload) > MAX_REPORT_BYTES:
        return remainder, None
    # Strip the newline the echo introduced. This is not cosmetic: an XML
    # declaration must be the document's FIRST byte, so a single leading "\n"
    # makes `ET.fromstring` raise ParseError and the whole structured channel
    # degrades to prose. Found by a real pytest --junitxml run, not by reading.
    return remainder, payload.strip()


def new_report_token() -> str:
    """Return a fresh, unguessable per-call sentinel token."""
    import uuid

    return f"neo-structured-{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------------------
# Structured results: the channel, fed
# ---------------------------------------------------------------------------


def parse_junit_cases(text: str) -> Tuple[TestOutcome, ...]:
    """Return per-test outcomes from a JUnit XML document.

    Assumes ``text`` is a JUnit document. Returns () for anything unparseable
    or case-free: the counts still come from
    ``execution.result_parsing.parse_junit_xml``, and a report with no
    ``<testcase>`` children legitimately has no per-test rows. A ``<failure>``
    or ``<error>`` child is a fail, ``<skipped>`` is a skip, and anything else
    is a pass — which is the JUnit convention.
    """
    if not text or "<" not in text:
        return ()
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return ()
    cases: List[TestOutcome] = []
    for element in root.iter("testcase"):
        name = str(element.get("name") or "").strip()
        if not name:
            continue
        classname = str(element.get("classname") or "").strip()
        outcome, message = "pass", ""
        for child in element:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag in ("failure", "error"):
                outcome = "fail"
                message = (child.get("message") or (child.text or "")).strip()[:2000]
                break
            if tag == "skipped":
                outcome = "skip"
                message = (child.get("message") or "").strip()[:2000]
                break
        cases.append(
            TestOutcome(
                test_id=f"{classname}::{name}" if classname else name,
                outcome=outcome,
                file=str(element.get("file") or ""),
                line=_coerce_int(element.get("line")),
                message=message,
                duration_s=_coerce_float(element.get("time")),
            )
        )
    return tuple(cases)


def _go_events(text: str) -> List[Dict[str, Any]]:
    """Return the JSON event objects from a ``go test -json`` capture.

    Assumes ``text`` is the runner's stdout. Every line is attempted as JSON; a
    line that is not JSON is a build diagnostic printed outside the event
    stream, so it is skipped rather than treated as a parse failure. Go emits
    one JSON object per line, so a line that parses is an event.
    """
    events: List[Dict[str, Any]] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] != "{":
            continue
        try:
            parsed = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _go_per_test(events: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    """Return ``{test_name: outcome}`` from Go events, first terminal wins.

    Assumes ``events`` are ``go test -json`` objects. A test's terminal action
    is its first ``pass`` / ``fail`` / ``skip``; a later duplicate (Go emits one
    per subtest roll-up) is ignored so the first decision stands. A test that
    only ever started and produced output never reached a terminal action, and
    is recorded as ``fail`` — an unfinished test is not a pass.
    """
    terminal: Dict[str, str] = {}
    started: Dict[str, bool] = {}
    for event in events:
        name = event.get("Test")
        if not name:
            continue
        key = str(name)
        action = str(event.get("Action") or "")
        if action in ("pass", "fail", "skip"):
            terminal.setdefault(key, action)
        else:
            started.setdefault(key, True)
    for key in started:
        terminal.setdefault(key, "fail")
    return terminal


def _go_package_failures(events: Sequence[Mapping[str, Any]]) -> bool:
    """True when a PACKAGE-level (test-less) ``fail`` event is present."""
    return any(
        not event.get("Test") and str(event.get("Action") or "") == "fail"
        for event in events
    )


def parse_go_test_cases(text: str) -> Tuple[TestOutcome, ...]:
    """Return per-test outcomes from a ``go test -json`` capture.

    Assumes ``text`` is the runner's stdout. Returns () when the stream carried
    no per-test events, which is the honest answer for a package with no test
    files and for a build that failed before any test ran — neither has a
    per-test row to report, and inventing one would be the exact failure this
    round exists to prevent.
    """
    events = _go_events(text)
    if not events:
        return ()
    terminal = _go_per_test(events)
    if not terminal:
        return ()
    durations: Dict[str, Optional[float]] = {}
    messages: Dict[str, str] = {}
    packages: Dict[str, str] = {}
    for event in events:
        name = event.get("Test")
        if not name:
            continue
        key = str(name)
        elapsed = _coerce_float(event.get("Elapsed"))
        if elapsed is not None:
            durations[key] = elapsed
        if event.get("Package"):
            packages.setdefault(key, str(event["Package"]))
        if str(event.get("Action")) == "output":
            output = event.get("Output")
            if isinstance(output, str) and output.strip() and not messages.get(key):
                messages[key] = output.strip()[:2000]
    return tuple(
        TestOutcome(
            test_id=name,
            outcome="skip" if terminal[name] == "skip" else terminal[name],
            file=packages.get(name, ""),
            message=messages.get(name, "") if terminal[name] == "fail" else "",
            duration_s=durations.get(name),
        )
        for name in sorted(terminal)
    )


def parse_go_test_json(text: str) -> Optional[Dict[str, int]]:
    """Return normalized counts for ``parse_test_run(json_report=...)``.

    Assumes ``text`` is a ``go test -json`` capture. Returns the same
    ``{"collected", "passed", "failed", "skipped"}`` shape
    ``execution.result_parsing.parse_pytest_json`` produces, so the EXISTING
    report channel consumes it unchanged.

    Returns None — degrade, never invent — in the three cases where counts
    would be a lie:

    - **a build failure with no test event.** ``go test`` reports a compile
      error as a package-level event. Reporting "0 collected" there would call
      a broken build a zero-test suite, which is the vacuous-green class.
    - **a package-level failure with no failing test.** This is a teardown or
      post-test panic. The per-test evidence says every test passed, so the
      counts would read as a pass; the exit code is the only honest signal, so
      the report is withheld and the caller falls through to it.
    - **no recognised event at all**, which is what a non-``-json`` invocation
      or a foreign runner produces.
    """
    events = _go_events(text)
    if not events:
        return None
    if any(str(event.get("Action") or "") == "build-fail" for event in events):
        return None
    package_actions = {
        str(event.get("Action") or "") for event in events if not event.get("Test")
    }
    terminal = _go_per_test(events)
    if not terminal:
        if "fail" in package_actions:
            return None
        if package_actions & {"pass", "skip"}:
            return {"collected": 0, "passed": 0, "failed": 0, "skipped": 0}
        return None
    failed = sum(1 for value in terminal.values() if value == "fail")
    skipped = sum(1 for value in terminal.values() if value == "skip")
    if failed == 0 and "fail" in package_actions:
        return None
    collected = len(terminal)
    return {
        "collected": collected,
        "passed": collected - failed - skipped,
        "failed": failed,
        "skipped": skipped,
    }


def normalize_structured(text: str, eco: Ecosystem) -> Optional[str]:
    """Return the payload to hand ``parse_test_run`` for ``eco``, or None.

    Assumes ``text`` is the extracted sentinel payload for a ``sentinel``
    ecosystem, or the runner's own stdout for an ``inline`` one. A JUnit
    document is passed through verbatim; a Go event stream is converted to the
    normalized JSON shape the existing channel already understands. An unusable
    payload is None so the caller falls back rather than inventing counts.
    """
    if not text or not eco.structured:
        return None
    if eco.result_format == FORMAT_JUNIT_XML:
        # Leading whitespace is stripped HERE as well as at the transport: the
        # document is untrusted input, and an XML declaration that is not the
        # first byte is a ParseError, i.e. a silent fall back to prose.
        body = text.strip()
        return body if "<testsuite" in body else None
    if eco.result_format == FORMAT_PYTEST_JSON:
        return text if text.lstrip()[:1] in ("{", "[") else None
    if eco.result_format == FORMAT_GO_TEST_JSON:
        counts = parse_go_test_json(text)
        return json.dumps(counts) if counts is not None else None
    return None


def parse_cases(text: str, eco: Ecosystem) -> Tuple[TestOutcome, ...]:
    """Return per-test outcomes from a structured payload, or ().

    Assumes ``text`` is the same payload :func:`normalize_structured` accepts.
    The per-test channel is strictly additional to the counts: a format with
    counts but no case elements legitimately yields ().
    """
    if not text:
        return ()
    if eco.result_format == FORMAT_JUNIT_XML:
        return parse_junit_cases(text)
    if eco.result_format == FORMAT_GO_TEST_JSON:
        return parse_go_test_cases(text)
    return ()


# ---------------------------------------------------------------------------
# The zero-test policy and the gate
# ---------------------------------------------------------------------------


def toolchain_unavailable_reason(output: str, eco: Ecosystem) -> Optional[str]:
    """Return why the toolchain was unavailable, or None if it was present.

    Assumes ``output`` is the runner capture. Matches the stable marker emitted
    by :func:`toolchain_probe_command`, so a paraphrase of the message cannot
    trigger it and a runner that merely printed a similar word cannot either.
    """
    if TOOLCHAIN_UNAVAILABLE_MARKER not in str(output or ""):
        return None
    return (
        f"{eco.name}: the toolchain this ecosystem requires "
        f"({', '.join(eco.toolchain)}) is not installed in the environment the run "
        "used; that is not a test result and not a pass"
    )


def zero_test_reason(report: Any, eco: Ecosystem, output: str) -> Optional[str]:
    """Return why this run collected no tests, or None if it collected some.

    Assumes ``report`` is an ``execution.result_parsing.TestRunReport`` and
    ``output`` the runner capture. The parser's own ``no_tests`` verdict is
    always honoured. Under ``fail_closed`` two more refusals apply:

    - one of the ecosystem's declared ``zero_test_markers`` appears in the
      capture, which is how Go's ``? pkg [no test files]`` is recognised at an
      exit code of 0;
    - a zero exit with NO positive collected-test evidence at all is refused,
      because a runner that reports nothing cannot be certified as having run
      anything.
    """
    outcome = str(getattr(report, "outcome", "") or "")
    if outcome == "no_tests":
        return (
            f"{eco.name}: the runner collected no tests; zero collected tests is "
            "never a pass, never 'flaky', and never 'skipped'"
        )
    if not eco.requires_positive_test_evidence:
        return None
    text = str(output or "")
    for marker in eco.zero_test_markers:
        if marker and marker in text:
            return (
                f"{eco.name}: the runner reported {marker!r} at a zero exit; a suite "
                "that ran nothing cannot certify a fix"
            )
    if outcome == "pass" and not getattr(report, "tests_collected", None):
        return (
            f"{eco.name}: exit 0 with no collected-test evidence under the "
            f"{eco.zero_test_policy!r} zero-test policy; a runner that reports "
            "nothing is not evidence that tests passed"
        )
    return None


def gate_outcome(report: Any, eco: Ecosystem, output: str) -> Tuple[str, str]:
    """Return ``(gate_outcome, reason)`` for one run under ``eco``.

    Assumes ``report`` is a ``TestRunReport`` and ``output`` the capture. The
    order is deliberate: a toolchain fault is reported as itself, because "the
    runner is not installed" and "the tests failed" are different facts and
    conflating them is how an environment problem becomes an edit instruction.
    ``no_tests`` is then promoted to the distinct
    :data:`OUTCOME_NO_TESTS_COLLECTED` so the receipt, the trace row, and any
    consumer all say the same loud thing.

    The returned outcome is always a member of :data:`GATE_OUTCOMES`; the
    reason is a human sentence naming the ecosystem and the evidence.
    """
    toolchain = toolchain_unavailable_reason(output, eco)
    if toolchain is not None:
        return OUTCOME_TOOLCHAIN_UNAVAILABLE, toolchain
    zero = zero_test_reason(report, eco, output)
    if zero is not None:
        return OUTCOME_NO_TESTS_COLLECTED, zero
    outcome = str(getattr(report, "outcome", "") or OUTCOME_ERROR)
    if outcome == "no_tests":  # pragma: no cover - zero_test_reason catches it
        return OUTCOME_NO_TESTS_COLLECTED, f"{eco.name}: no tests were collected"
    if outcome not in GATE_OUTCOMES:
        return OUTCOME_ERROR, f"{eco.name}: unrecognised run outcome {outcome!r}"
    return outcome, ""


def blocks_success(gate: str) -> bool:
    """True when ``gate`` may not be reported as a verified success.

    Assumes ``gate`` is a member of :data:`GATE_OUTCOMES`; an UNKNOWN value is
    treated as blocking, because a consumer that cannot classify a verdict must
    not pass it. This is the single place the rule is written, so the verifier
    and any future consumer cannot disagree about which outcomes mint success.
    """
    return gate != OUTCOME_PASS


def render_receipt(
    eco: Ecosystem,
    gate: str,
    reason: str,
    report: Any = None,
    cases: Sequence[TestOutcome] = (),
) -> str:
    """Render the ``## ecosystem`` block appended to ``raw_output``.

    Assumes the arguments are already validated. The block is grep-friendly
    (``## ecosystem`` / ``ecosystem=`` / ``gate=`` on their own lines) and
    ALWAYS names the ecosystem, so a reader of a trace can tell which contract
    produced a verdict without re-deriving it. Per-test outcomes are bounded to
    :data:`MAX_RECEIPT_CASES` with the true total reported beside them, so a
    truncated list is never mistaken for a complete one.
    """
    lines = [
        "## ecosystem",
        f"  ecosystem={eco.name} language={eco.language} "
        f"zero_test_policy={eco.zero_test_policy} gate={gate} "
        f"blocking={str(blocks_success(gate)).lower()}",
        f"  result_format={eco.result_format} capture={eco.report_capture} "
        f"toolchain={','.join(eco.toolchain)}",
    ]
    if reason:
        lines.append(f"  reason: {reason}")
    if report is not None:
        lines.append(
            "  report: outcome={} exit={} collected={} passed={} failed={} "
            "skipped={} source={} confidence={}".format(
                getattr(report, "outcome", "?"),
                getattr(report, "exit_code", "?"),
                getattr(report, "tests_collected", "?"),
                getattr(report, "tests_passed", "?"),
                getattr(report, "tests_failed", "?"),
                getattr(report, "tests_skipped", "?"),
                getattr(report, "source", "?"),
                getattr(report, "confidence", "?"),
            )
        )
    if cases:
        shown = list(cases)[:MAX_RECEIPT_CASES]
        lines.append(f"  test_outcomes: {len(cases)} total, {len(shown)} shown")
        for case in shown:
            location = f" {case.file}" if case.file else ""
            if case.line is not None:
                location += f":{case.line}"
            lines.append(f"    {case.outcome} {case.test_id}{location}")
    elif eco.structured:
        lines.append("  test_outcomes: 0 (the structured report carried no cases)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Protected paths
# ---------------------------------------------------------------------------


def effective_protected_paths(
    configured: Optional[Iterable[str]] = None,
    eco: Optional[Ecosystem] = None,
    *,
    include_test_config: bool = True,
) -> Tuple[str, ...]:
    """Return the glob list a repository in ``eco`` should be held to.

    Assumes ``configured`` is the caller's ``Task.config["protected_paths"]``
    (any iterable of str, possibly None) and ``eco`` is the detected ecosystem
    or None. The caller's globs are always KEPT — the operator's list is
    policy, not a default — and the ecosystem's own test globs are ADDED, which
    is what makes the set language-correct: a Java repository is held to
    ``src/test/*`` and a Go repository to ``*_test.go``, neither of which the
    Python-shaped default list would have caught.

    Order-stable and duplicate-free, so a caller can pass the result straight to
    ``harness.editor.is_protected`` without changing any decision the
    configured list already made.
    """
    out: List[str] = []
    for item in configured or ():
        text = str(item)
        if text and text not in out:
            out.append(text)
    for glob in eco.protected_globs if eco is not None else ():
        if glob and glob not in out:
            out.append(glob)
    if include_test_config and eco is not None:
        try:
            from harness.test_config import test_config_patterns
        except Exception:  # pragma: no cover - test_config is in-tree
            return tuple(out)
        for pattern in test_config_patterns(eco.language):
            if pattern not in out:
                out.append(pattern)
    return tuple(out)


def matches_any_glob(rel_path: str, patterns: Sequence[str]) -> bool:
    """True when ``rel_path`` matches any glob in ``patterns``.

    Assumes ``rel_path`` is repo-relative posix. Matching is fnmatch against
    the full posix path, the basename, and each directory component, mirroring
    ``harness.editor.is_protected`` so the two agree on what a path is. Never
    raises.
    """
    rel = str(rel_path or "").replace("\\", "/")
    parts = rel.split("/")
    lowered = [part.lower() for part in parts]
    for pattern in patterns or ():
        low = str(pattern).lower()
        if fnmatch.fnmatch(rel.lower(), low):
            return True
        if fnmatch.fnmatch(lowered[-1], low):
            return True
        for part in lowered[:-1]:
            if fnmatch.fnmatch(part, low):
                return True
    return False


def is_test_path(rel_path: str, eco: Optional[Ecosystem]) -> bool:
    """True when ``rel_path`` matches ``eco``'s test-file globs.

    Assumes ``rel_path`` is repo-relative posix and ``eco`` may be None, in
    which case the answer is False: an unknown ecosystem has no declared test
    shape, and inventing one would be a guess.
    """
    if eco is None or not eco.test_globs:
        return False
    return matches_any_glob(rel_path, eco.test_globs)


# ---------------------------------------------------------------------------
# The registered ecosystems
#
# python and javascript DELEGATE their command composition to the detectors
# execution/verify.py already has (one language detector in the tree, never two).
# They are registered anyway, because the registry is where the DECISION of
# which contract a repository is held to belongs, and their test globs,
# zero-test policy, toolchain, and image belong here too.
# ---------------------------------------------------------------------------

PYTHON = Ecosystem(
    name="python",
    language="python",
    command_family=COMMAND_PYTEST,
    test_command="python -m pytest -q",
    test_globs=("test_*.py", "*_test.py", "tests/*", "conftest.py"),
    protected_globs=("tests/*", "test_*.py", "*_test.py"),
    lint_command="python -m ruff check .",
    format_command="python -m ruff format .",
    result_format=FORMAT_JUNIT_XML,
    report_capture=CAPTURE_SENTINEL,
    report_arg="--junitxml={path}",
    zero_test_policy=ZERO_TEST_PARSER,
    toolchain=("python",),
    sandbox_image="python:3.10-slim",
    dep_manifests=("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg"),
    markers=(
        "pytest.ini",
        "pyproject.toml",
        "setup.cfg",
        "conftest.py",
        "tox.ini",
        "requirements.txt",
        "setup.py",
    ),
    primary_markers=(
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "pytest.ini",
        "tox.ini",
    ),
    surfaces={
        "conftest.py": ("conftest", ()),
        "pytest.ini": ("inifile", ()),
        "tox.ini": ("tox_pytest", ("pytest",)),
        "setup.cfg": ("setup_cfg_pytest", ("tool:pytest",)),
        "pyproject.toml": ("pyproject_pytest", ("tool.pytest.ini_options",)),
        "sitecustomize.py": ("sitecustomize", ()),
        "usercustomize.py": ("sitecustomize", ()),
    },
    notes="Command composition delegates to execution.verify's existing pytest "
    "detector; the registry owns the contract, not a second detector.",
)

JAVASCRIPT = Ecosystem(
    name="javascript",
    language="js",
    command_family=COMMAND_JEST,
    # Resolved per repository (jest OR vitest, never guessed) by the existing
    # detector; an empty suite command is legal only for this family.
    test_command="",
    test_globs=(
        "*.test.js",
        "*.test.jsx",
        "*.test.ts",
        "*.test.tsx",
        "*.spec.js",
        "*.spec.jsx",
        "*.spec.ts",
        "*.spec.tsx",
        "__tests__/*",
        "test/*",
        "tests/*",
    ),
    protected_globs=(
        "*.test.js",
        "*.test.jsx",
        "*.test.ts",
        "*.test.tsx",
        "*.spec.js",
        "*.spec.jsx",
        "*.spec.ts",
        "*.spec.tsx",
        "__tests__/*",
    ),
    lint_command="npx --no-install eslint .",
    format_command="npx --no-install prettier --write .",
    result_format=FORMAT_PROSE,
    report_capture=CAPTURE_NONE,
    zero_test_policy=ZERO_TEST_PARSER,
    toolchain=("node",),
    sandbox_image="node:22-slim",
    dep_manifests=(
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
    ),
    markers=("package.json", "tsconfig.json"),
    primary_markers=("package.json",),
    surfaces={
        "jest.config.js": ("runner_config", ()),
        "jest.config.cjs": ("runner_config", ()),
        "jest.config.mjs": ("runner_config", ()),
        "jest.config.ts": ("runner_config", ()),
        "jest.config.json": ("runner_config", ()),
        "vitest.config.js": ("runner_config", ()),
        "vitest.config.mjs": ("runner_config", ()),
        "vitest.config.ts": ("runner_config", ()),
        "vitest.config.mts": ("runner_config", ()),
        "karma.conf.js": ("runner_config", ()),
        "karma.conf.ts": ("runner_config", ()),
        "playwright.config.js": ("runner_config", ()),
        "playwright.config.ts": ("runner_config", ()),
        "cypress.config.js": ("runner_config", ()),
        "cypress.config.ts": ("runner_config", ()),
        "mocharc.js": ("runner_config", ()),
        "mocharc.cjs": ("runner_config", ()),
        "mocharc.json": ("runner_config", ()),
        ".mocharc.js": ("runner_config", ()),
        ".mocharc.cjs": ("runner_config", ()),
        ".mocharc.json": ("runner_config", ()),
        ".mocharc.yml": ("runner_config", ()),
        ".mocharc.yaml": ("runner_config", ()),
        "angular.json": ("angular_test", ("test",)),
        "package.json": ("package_json", ("jest", "scripts.test")),
    },
    notes="Jest and Vitest have no built-in machine-readable report: jest's "
    "--json is deprecated and vitest's junit reporter needs a dependency, so "
    "the format is honestly 'prose' and the structured channel is off.",
)

GO = Ecosystem(
    name="go",
    language="go",
    command_family=COMMAND_TEMPLATE,
    test_command="go test -json ./...",
    test_command_base="go test -json",
    test_globs=("*_test.go",),
    protected_globs=("*_test.go", "testdata/*", "go.test.conf", "go.mod"),
    build_command="go build ./...",
    lint_command="go vet ./...",
    format_command="gofmt -l -w .",
    package_flag="./{scope}",
    default_scope="./...",
    target_test_flag="-run",
    # Anchored, because `go test -run` matches a SUBSTRING unless anchored, so
    # an unanchored TestAdd would also run TestAddOverflow.
    target_test_template="^{escaped}$",
    result_format=FORMAT_GO_TEST_JSON,
    report_capture=CAPTURE_INLINE,
    zero_test_policy=ZERO_TEST_FAIL_CLOSED,
    zero_test_markers=(
        "[no test files]",
        "no test files",
        "no tests to run",
        "testing: warning: no tests to run",
    ),
    toolchain=("go",),
    sandbox_image="golang:1.23-slim",
    dep_manifests=("go.mod", "go.sum"),
    markers=("go.mod",),
    primary_markers=("go.mod",),
    surfaces={"go.test.conf": ("runner_config", ())},
    notes="`go test -json` is Go's own structured event stream, so the "
    "structured channel needs no extra tool. `[no test files]` exits 0, which "
    "is exactly why this entry's zero-test policy is fail_closed.",
)

RUST = Ecosystem(
    name="rust",
    language="rust",
    command_family=COMMAND_TEMPLATE,
    test_command="cargo test",
    test_globs=("tests/*.rs", "*_test.rs", "tests/*"),
    protected_globs=("tests/*", "benches/*", "Cargo.toml"),
    build_command="cargo build",
    lint_command="cargo clippy --all-targets",
    format_command="cargo fmt",
    result_format=FORMAT_PROSE,
    report_capture=CAPTURE_NONE,
    zero_test_policy=ZERO_TEST_FAIL_CLOSED,
    zero_test_markers=("running 0 tests", "0 passed;", "no test target"),
    toolchain=("cargo",),
    sandbox_image="rust:1-slim",
    dep_manifests=("Cargo.toml", "Cargo.lock"),
    markers=("Cargo.toml",),
    primary_markers=("Cargo.toml",),
    surfaces={},
    notes="Registered from DATA only, as the standing proof that a new "
    "language is a data change. Cargo's own output is prose; `cargo nextest "
    "--message-format junit-xml` would give a structured channel and is not "
    "built.",
)

JAVA = Ecosystem(
    name="java",
    language="java",
    command_family=COMMAND_TEMPLATE,
    test_command="mvn -B -q test",
    test_globs=("src/test/java/*", "*Test.java", "*Tests.java", "*IT.java"),
    protected_globs=("src/test/*", "src/it/*", "*Test.java", "*Tests.java"),
    build_command="mvn -B -q package -DskipTests",
    target_test_flag="-Dtest=",
    result_format=FORMAT_PROSE,
    report_capture=CAPTURE_NONE,
    zero_test_policy=ZERO_TEST_FAIL_CLOSED,
    zero_test_markers=("no tests to run", "no tests were executed", "Tests run: 0"),
    toolchain=("mvn",),
    sandbox_image="maven:3-eclipse-temurin-21",
    dep_manifests=("pom.xml",),
    markers=("pom.xml", "build.gradle", "build.gradle.kts"),
    primary_markers=("pom.xml", "build.gradle", "build.gradle.kts"),
    surfaces={
        "pom.xml": ("maven_test", ("test",)),
        "build.gradle": ("gradle_test", ("test",)),
        "build.gradle.kts": ("gradle_test", ("test",)),
    },
    notes="This is the concrete case the per-language protected globs exist "
    "for: a Java agent must not edit src/test/** while a Python agent is held "
    "to tests/*.",
)

DOTNET = Ecosystem(
    name="dotnet",
    language="dotnet",
    command_family=COMMAND_TEMPLATE,
    test_command="dotnet test --nologo",
    test_globs=("*.cs", "*Tests.cs", "*.Tests.cs"),
    protected_globs=("*.Tests.cs", "*.csproj", "*.runsettings", "*.sln"),
    build_command="dotnet build",
    lint_command="dotnet format --verify-no-changes",
    format_command="dotnet format",
    target_test_flag="--filter",
    result_format=FORMAT_PROSE,
    report_capture=CAPTURE_NONE,
    zero_test_policy=ZERO_TEST_FAIL_CLOSED,
    zero_test_markers=("no test is available", "No test matches", "Total tests: 0"),
    toolchain=("dotnet",),
    sandbox_image="mcr.microsoft.com/dotnet/sdk:8.0",
    dep_manifests=("*.csproj", "*.sln", "global.json"),
    markers=("*.csproj", "*.sln", "*.fsproj"),
    primary_markers=("*.csproj", "*.fsproj", "*.sln"),
    surfaces={
        "xunit.runner.json": ("runner_config", ()),
        "nunit.config": ("runner_config", ()),
    },
    notes="Registered from DATA only; not exercised by any test lane.",
)

RUBY = Ecosystem(
    name="ruby",
    language="ruby",
    command_family=COMMAND_TEMPLATE,
    test_command="bundle exec rspec",
    test_globs=("spec/**/*_spec.rb", "test/**/*_test.rb"),
    protected_globs=("spec/*", "test/*", "Gemfile", ".rspec"),
    build_command="bundle install",
    lint_command="bundle exec rubocop",
    format_command="bundle exec rubocop -a",
    result_format=FORMAT_PROSE,
    report_capture=CAPTURE_NONE,
    zero_test_policy=ZERO_TEST_FAIL_CLOSED,
    zero_test_markers=("0 examples, 0 failures", "examples, 0 failures"),
    toolchain=("bundle",),
    sandbox_image="ruby:3-slim",
    dep_manifests=("Gemfile", "Gemfile.lock"),
    markers=("Gemfile", "Rakefile", ".rspec"),
    primary_markers=("Gemfile", ".rspec"),
    surfaces={
        ".rspec": ("runner_config", ()),
        "spec_helper.rb": ("conftest", ()),
        "rails_helper.rb": ("conftest", ()),
        ".rspec-status.yml": ("runner_config", ()),
    },
    notes="Registered from DATA only; not exercised by any test lane.",
)

BUILTIN_ECOSYSTEMS: Tuple[Ecosystem, ...] = (
    GO,
    RUST,
    JAVA,
    DOTNET,
    RUBY,
    JAVASCRIPT,
    PYTHON,
)


def _register_builtins() -> None:
    """Register the built-in ecosystems, best-effort.

    A duplicate registration is tolerated (a reload); a genuine failure is
    swallowed so importing this module can never be the reason a verifier fails
    to start. The missing entry is then absent from the registry and every
    consumer degrades to "no ecosystem" rather than to a wrong one.
    """
    for eco in BUILTIN_ECOSYSTEMS:
        try:
            register_ecosystem(eco, replace=True)
        except Exception:  # pragma: no cover - defensive
            continue


_register_builtins()


def _coerce_int(value: Any) -> Optional[int]:
    """Return ``value`` as an int, or None. Never raises on a hostile value."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> Optional[float]:
    """Return ``value`` as a float, or None. Never raises on a hostile value."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
