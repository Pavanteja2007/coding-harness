"""Effective test-configuration resolution and protection (Ceiling Round 2, R2-03).

**The gap this closes.** `harness.editor` refuses an agent edit to a file that
matches ``config["protected_paths"]`` (``tests/*``, ``test_*.py``,
``*_test.py``). A repository's test *configuration* was not in that set, so an
agent could add ``-k``, ``--ignore``, ``--deselect`` or ``-p no:cacheprovider``
to ``addopts``, switch the inifile, or drop a ``conftest.py`` in place, and the
regression gate would report a clean run. The verifier answers "did the suite
pass"; nothing asked "is this the same suite".

**Three layers, deliberately not one.**

1. ``classify_test_config_path`` / ``is_test_config_path`` — the protected
   *surface* set, per language, with each surface either whole-file scoped
   (``conftest.py``, ``pytest.ini``) or table scoped (``[tool.pytest.ini_options]``
   in ``pyproject.toml``, the ``jest`` key in ``package.json``). A table-scoped
   file is dual-purpose: adding a dependency to ``pyproject.toml`` is a normal
   edit, and only its pytest table is part of the test contract.
2. ``declared_test_config_change`` — the *declared intent*. A task whose
   declared purpose is to change test configuration says so in
   ``Task.config["test_config_change"]`` and the change proceeds. The
   declaration is a config key, not issue prose, and it is recorded in the
   receipt: a bypass with no receipt is the thing this exists to prevent.
3. ``resolve_effective_test_config`` — the *effect*. The resolved inifile, its
   normalized options, the ``addopts`` token list, the ``conftest`` chain that
   can reach the declared target test, and any config found at or above the
   root, digested into one value. ``test_config_guard`` resolves it for the
   pristine tree and the working tree and reports the difference as
   ``test_config_changed`` with the resolved before/after.

**What this resolver does NOT do, stated plainly.** It reads configuration; it
never runs the test runner, so it cannot observe what the runner would actually
collect. It is evidence about the declared configuration, not a substitute for
a real run, and the receipt records how far the resolution went
(``conftest_scope``) so a partial resolution can never read as a complete one.

**Cost.** Resolution is O(changed paths + directory depth), not O(repo files):
the ``conftest`` chain is walked from the declared target's directory up to the
root, and every other surface comes from the caller-supplied changed-file set
(which the edit gate already computed). Nothing here re-walks a large tree.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

__all__ = [
    "SCHEMA_VERSION",
    "TABLE_SCOPED",
    "TEST_CONFIG_CHANGED_EVENT",
    "TEST_CONFIG_CHANGE_KEY",
    "TEST_CONFIG_EVENT",
    "WHOLE_FILE",
    "ResolvedTestConfig",
    "TestConfigReceipt",
    "TestConfigSurface",
    "TestConfigVerdict",
    "classify_test_config_path",
    "declared_test_config_change",
    "diff_effective_test_config",
    "is_test_config_path",
    "language_surfaces",
    "register_language_surfaces",
    "relaxation_slugs",
    "resolve_effective_test_config",
    "test_config_guard",
    "test_config_patterns",
]

#: Receipt schema version, so a stored receipt is never read as a newer shape.
SCHEMA_VERSION = 1

#: The declared-intent config key. Deliberately **absent from
#: ``harness.config.DEFAULTS``**: the declaration is opt-in by key *presence*,
#: so an unset task cannot be switched into "declared" by a default landing in
#: the dict that is merged into every task. A ``None`` entry may be added there
#: for discoverability — ``declared_test_config_change`` treats ``None``,
#: ``False`` and ``""`` as "not declared", so that addition is behavior-neutral.
TEST_CONFIG_CHANGE_KEY = "test_config_change"

#: The trace/result event name for a changed effective test configuration.
TEST_CONFIG_CHANGED_EVENT = "test_config_changed"

#: The trace event carrying the receipt on EVERY gate call, changed or not, so a
#: run with no test-config change is distinguishable from a run that never
#: looked.
TEST_CONFIG_EVENT = "test_config"

#: Scope kinds: the whole file is the test contract, or only named tables in it.
WHOLE_FILE = "file"
TABLE_SCOPED = "table"

_MAX_READ_BYTES = 512_000
_MAX_VALUE_CHARS = 4_000
_MAX_LIST_ITEMS = 64
_MAX_OPTIONS = 200
_MAX_SECTIONS = 400
_MAX_SECTION_KEYS = 400
_MAX_EXTERNAL_LEVELS = 4
_MAX_CONFTEST_CHAIN = 32

# ---------------------------------------------------------------------------
# The per-language protected surface table.
#
# filename -> (kind, tables-in-scope).  An empty ``tables`` tuple means the whole
# file is the test contract.  R2-12 (polyglot ecosystems) extends this through
# ``register_language_surfaces`` rather than editing the logic below.
# ---------------------------------------------------------------------------
_PYTHON_SURFACES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "conftest.py": ("conftest", ()),
    "pytest.ini": ("inifile", ()),
    "tox.ini": ("tox_pytest", ("pytest",)),
    "setup.cfg": ("setup_cfg_pytest", ("tool:pytest",)),
    "pyproject.toml": ("pyproject_pytest", ("tool.pytest.ini_options",)),
    "sitecustomize.py": ("sitecustomize", ()),
    "usercustomize.py": ("sitecustomize", ()),
}

_JS_SURFACES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
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
}

_GO_SURFACES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "go.test.conf": ("runner_config", ()),
}

#: Cargo.toml is deliberately absent: its ``[dev-dependencies]`` / ``[[test]]``
#: tables are test configuration, but a whole-file lock on Rust's manifest
#: would block every legitimate dependency edit, and the ecosystem layer
#: (R2-12) owns that decision. Recorded here so the gap is a stated one.
_RUST_SURFACES: Dict[str, Tuple[str, Tuple[str, ...]]] = {}

_JAVA_SURFACES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "pom.xml": ("maven_test", ("test",)),
    "build.gradle": ("gradle_test", ("test",)),
    "build.gradle.kts": ("gradle_test", ("test",)),
}

_DOTNET_SURFACES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "xunit.runner.json": ("runner_config", ()),
    "nunit.config": ("runner_config", ()),
}

_RUBY_SURFACES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    ".rspec": ("runner_config", ()),
    "spec_helper.rb": ("conftest", ()),
    "rails_helper.rb": ("conftest", ()),
    ".rspec-status.yml": ("runner_config", ()),
}

_PHP_SURFACES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "phpunit.xml": ("runner_config", ()),
    "phpunit.xml.dist": ("runner_config", ()),
    "phpunit.dist.xml": ("runner_config", ()),
}

_DART_SURFACES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "dart_test.yaml": ("runner_config", ()),
}

#: The surface kinds that are protected for EVERY repository, whatever its
#: language: they configure a test runner (or the interpreter's import path)
#: in whichever language the repository turns out to be.  An unknown-language
#: repository still gets this fail-closed set.
UNIVERSAL_SURFACES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "conftest.py": ("conftest", ()),
    "pytest.ini": ("inifile", ()),
    "sitecustomize.py": ("sitecustomize", ()),
    "usercustomize.py": ("sitecustomize", ()),
}

#: Suffix-keyed surfaces (``.runsettings`` and friends).
_SUFFIX_SURFACES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    ".runsettings": ("runner_config", ()),
}

_LANGUAGE_SURFACES: Dict[str, Dict[str, Tuple[str, Tuple[str, ...]]]] = {
    "python": dict(_PYTHON_SURFACES),
    "js": dict(_JS_SURFACES),
    "go": dict(_GO_SURFACES),
    "rust": dict(_RUST_SURFACES),
    "java": dict(_JAVA_SURFACES),
    "dotnet": dict(_DOTNET_SURFACES),
    "ruby": dict(_RUBY_SURFACES),
    "php": dict(_PHP_SURFACES),
    "dart": dict(_DART_SURFACES),
}

#: pytest's own inifile precedence. The first match wins, exactly as pytest
#: resolves it, so "the effective configuration" is pytest's answer and not a
#: guess. ``pytest.ini`` is selected by EXISTENCE (an empty pytest.ini still
#: stops the search); the others require their pytest table to be present.
_INIFILE_PRECEDENCE: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("pytest.ini", "inifile", ()),
    ("pyproject.toml", "pyproject_pytest", ("tool.pytest.ini_options",)),
    ("tox.ini", "tox_pytest", ("pytest",)),
    ("setup.cfg", "setup_cfg_pytest", ("tool:pytest",)),
)

#: ``addopts`` tokens that narrow WHAT RUNS or WHICH PLUGINS LOAD. Each maps to
#: a stable slug used in the receipt and the trace. A change that ADDS one of
#: these is a relaxation, whether or not the run declares it.
_RELAXATION_TOKENS: Dict[str, str] = {
    "-k": "addopts_selector",
    "--deselect": "addopts_deselect",
    "--ignore": "addopts_ignore",
    "--ignore-glob": "addopts_ignore_glob",
    "-m": "addopts_marker_filter",
    "-p": "addopts_plugin",
    "-o": "addopts_ini_override",
    "--override-ini": "addopts_ini_override",
    "--co": "addopts_collect_only",
    "--collect-only": "addopts_collect_only",
    "-x": "addopts_fail_fast",
    "--maxfail": "addopts_maxfail",
    "--lf": "addopts_last_failed_only",
    "--last-failed": "addopts_last_failed_only",
    "--lfnf": "addopts_last_failed_only",
    "--last-failed-no-failures": "addopts_last_failed_only",
    "--stepwise": "addopts_last_failed_only",
    "--stepwise-skip": "addopts_last_failed_only",
    "--sw": "addopts_last_failed_only",
}

#: Options whose value defines WHICH PATHS ARE COLLECTED. A narrowed value is a
#: relaxation even though the key itself is unchanged.
_SCOPE_KEYS: Dict[str, str] = {
    "testpaths": "scope_narrowed",
    "norecursedirs": "scope_narrowed",
    "python_files": "scope_narrowed",
    "python_classes": "scope_narrowed",
    "python_functions": "scope_narrowed",
    "collect_ignore": "scope_narrowed",
    "test_ignore": "scope_narrowed",
}


@dataclass(frozen=True)
class TestConfigSurface:
    """One protected test-configuration surface.

    ``path`` is repo-relative posix, ``kind`` the stable slug (``conftest`` |
    ``inifile`` | ``pyproject_pytest`` | ``setup_cfg_pytest`` | ``tox_pytest`` |
    ``package_json`` | ``angular_test`` | ``runner_config`` | ``maven_test`` |
    ``gradle_test`` | ``sitecustomize``), ``language`` the language whose runner
    reads it, and ``scope``/``tables`` whether the whole file or only named
    tables are part of the test contract.
    """

    path: str
    kind: str
    language: str
    scope: str
    tables: Tuple[str, ...] = ()


def register_language_surfaces(
    language: str,
    surfaces: Mapping[str, Tuple[str, Tuple[str, ...]]],
    *,
    replace: bool = False,
) -> None:
    """Add or replace one language's protected surface table.

    Assumes ``language`` is a non-empty lower-case token and ``surfaces`` maps a
    file NAME (not a path) to ``(kind, tables)`` where an empty ``tables`` tuple
    means the whole file is protected. Intended for the ecosystem layer
    (R2-12) to register Go / Rust / Java / .NET runners without editing this
    module's logic. Process-global by design; call it at import time.
    """
    name = str(language or "").strip().lower()
    if not name:
        raise ValueError("language must be a non-empty token")
    if replace or name not in _LANGUAGE_SURFACES:
        _LANGUAGE_SURFACES[name] = {
            str(key): (str(value[0]), tuple(str(t) for t in (value[1] or ())))
            for key, value in surfaces.items()
        }
        return
    merged = dict(_LANGUAGE_SURFACES[name])
    for key, value in surfaces.items():
        merged[str(key)] = (str(value[0]), tuple(str(t) for t in (value[1] or ())))
    _LANGUAGE_SURFACES[name] = merged


def language_surfaces(
    language: Optional[str] = None,
) -> Dict[str, Tuple[str, Tuple[str, ...]]]:
    """Return the protected surface table for one language (or all of them).

    Assumes nothing. An unknown language yields only the universal fail-closed
    set, so an unrecognized repository is still protected rather than open.
    """
    if language is None:
        merged: Dict[str, Tuple[str, Tuple[str, ...]]] = dict(UNIVERSAL_SURFACES)
        for table in _LANGUAGE_SURFACES.values():
            merged.update(table)
        return merged
    merged = dict(UNIVERSAL_SURFACES)
    merged.update(_LANGUAGE_SURFACES.get(str(language).strip().lower(), {}))
    return merged


def test_config_patterns(language: Optional[str] = None) -> List[str]:
    """Glob patterns covering the whole-file protected test-config surfaces.

    Returned so a consumer can extend an existing ``config["protected_paths"]``
    list instead of forking a second policy. Table-scoped surfaces are not
    expressible as a glob and are enforced by ``test_config_guard``, which has
    the tree in hand.
    """
    table = language_surfaces(language)
    whole_file = sorted(name for name, (_, tables) in table.items() if not tables)
    return whole_file + [f"*{name}" for name in whole_file]


def _normalize_rel(rel_path: str) -> str:
    """Collapse separators and ``..`` out of a repo-relative path.

    Same normalization the editor's protected-path guard uses, so a
    traversal-shaped path cannot reach a surface check under another name.
    """
    parts: List[str] = []
    for part in str(rel_path or "").replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def classify_test_config_path(rel_path: str) -> Optional[TestConfigSurface]:
    """Classify a repo-relative path as a test-configuration surface.

    Assumes ``rel_path`` is repo-relative (or traversal-shaped; it is
    normalized). Returns ``None`` for an ordinary source file. Matching is by
    BASENAME, because that is how the runners find their configuration:
    ``conftest.py`` is directory-scoped, so a nested one changes the run exactly
    as a root one does.
    """
    rel = _normalize_rel(rel_path)
    if not rel:
        return None
    name = rel.rsplit("/", 1)[-1]
    for language, table in _LANGUAGE_SURFACES.items():
        if name in table:
            kind, tables = table[name]
            return TestConfigSurface(
                path=rel,
                kind=kind,
                language=language,
                scope=TABLE_SCOPED if tables else WHOLE_FILE,
                tables=tables,
            )
    if name in UNIVERSAL_SURFACES:
        kind, tables = UNIVERSAL_SURFACES[name]
        return TestConfigSurface(
            path=rel,
            kind=kind,
            language="any",
            scope=TABLE_SCOPED if tables else WHOLE_FILE,
            tables=tables,
        )
    for suffix, (kind, tables) in _SUFFIX_SURFACES.items():
        if name.endswith(suffix):
            return TestConfigSurface(
                path=rel,
                kind=kind,
                language="any",
                scope=TABLE_SCOPED if tables else WHOLE_FILE,
                tables=tables,
            )
    return None


def is_test_config_path(rel_path: str) -> bool:
    """True when ``rel_path`` is a protected test-configuration surface."""
    return classify_test_config_path(rel_path) is not None


def declared_test_config_change(config: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Return the declared reason for changing test configuration, or ``None``.

    Assumes ``config`` is a mapping (a merged ``Task.config``); anything else, an
    absent key, or a value of ``None`` / ``False`` / ``""`` is "not declared". A
    dict value is read through its ``reason`` entry. The issue text is
    deliberately NOT consulted: prose in the task description is reachable by
    the same agent the guard exists to constrain, so prose cannot be an
    authorization. (The pre-existing ``harness.core._authorized_test_target``
    matches issue prose for a different, narrower exception; this does not copy
    that part.)
    """
    if not isinstance(config, Mapping):
        return None
    if TEST_CONFIG_CHANGE_KEY not in config:
        return None
    value = config[TEST_CONFIG_CHANGE_KEY]
    if value is None or value is False or value == "":
        return None
    if isinstance(value, Mapping):
        reason = value.get("reason")
        return TEST_CONFIG_CHANGE_KEY if reason in (None, "") else str(reason)
    if value is True:
        return TEST_CONFIG_CHANGE_KEY
    return str(value)


# ---------------------------------------------------------------------------
# Tolerant table reading. tomllib/tomli when importable (fidelity), otherwise a
# line reader that understands sections, quoted values and multi-line arrays.
# Never raises: an unparseable file is data, not an exception.
# ---------------------------------------------------------------------------


def _load_toml(text: str) -> Optional[Dict[str, Any]]:
    for module_name in ("tomllib", "tomli"):
        try:
            module = __import__(module_name)
        except Exception:  # pragma: no cover - import guard
            continue
        try:
            return dict(module.loads(text))
        except Exception:
            return None
    return None


def _strip_comment(line: str) -> str:
    """Remove a trailing ``#`` comment that sits outside quotes."""
    out: List[str] = []
    quote: Optional[str] = None
    for index, char in enumerate(line):
        if quote:
            out.append(char)
            if char == quote and (index == 0 or line[index - 1] != "\\"):
                quote = None
            continue
        if char in "\"'":
            quote = char
            out.append(char)
            continue
        if char == "#":
            break
        out.append(char)
    return "".join(out)


def _strip_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _normalize_value(value: Any, depth: int = 0) -> Any:
    """Coerce a parsed value into a deterministic, bounded JSON shape."""
    if depth > 4:
        return "..."
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value[:_MAX_VALUE_CHARS]
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_value(value[key], depth + 1)
            for key in sorted(str(k) for k in value)[:_MAX_OPTIONS]
        }
    if isinstance(value, (list, tuple)):
        return [
            _normalize_value(item, depth + 1) for item in list(value)[:_MAX_LIST_ITEMS]
        ]
    return str(value)[:_MAX_VALUE_CHARS]


def _normalize_table_name(name: str) -> str:
    return ".".join(
        part
        for part in str(name or "")
        .replace(":", ".")
        .replace(" ", "")
        .lower()
        .split(".")
        if part
    )


def _bracket_depth(text: str, *, start: int = 0, stop_at_comment: bool = False) -> int:
    depth = start
    quote: Optional[str] = None
    for index, char in enumerate(text):
        if quote:
            if char == quote and (index == 0 or text[index - 1] != "\\"):
                quote = None
            continue
        if char in "\"'":
            quote = char
            continue
        if char == "#" and stop_at_comment:
            break
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
    return depth


def _parse_sections(text: str) -> Dict[str, Dict[str, Any]]:
    """Parse every ``[section]`` of an INI/TOML-ish text into nested dicts.

    Assumes a small configuration file (callers cap the size). Handles quoted
    values and multi-line arrays. Returns ``{}`` for text with no parseable
    key/value pair. Never raises.
    """
    sections: Dict[str, Dict[str, Any]] = {"": {}}
    stack: List[str] = []
    key: Optional[str] = None
    buffer: List[str] = []
    depth = 0

    for raw in text.splitlines():
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if (
            depth == 0
            and line.startswith("[")
            and line.endswith("]")
            and "=" not in line
        ):
            if len(sections) >= _MAX_SECTIONS:
                break
            stack = [
                part for part in _normalize_table_name(line[1:-1]).split(".") if part
            ]
            sections.setdefault(".".join(stack), {})
            key = None
            buffer = []
            continue
        if depth == 0 and "=" in line:
            name, _, value = line.partition("=")
            name = name.strip().strip("\"'")
            value = value.strip()
            if not name:
                continue
            depth = _bracket_depth(value)
            if depth > 0:
                key, buffer = name, [value]
                continue
            sections[".".join(stack)][name] = _normalize_value(_strip_quotes(value))
            key, buffer = None, []
            continue
        if key is not None:
            buffer.append(line)
            depth = _bracket_depth(line, start=depth, stop_at_comment=True)
            if depth <= 0:
                sections[".".join(stack)][key] = _normalize_value(
                    _strip_quotes(" ".join(buffer))
                )
                key, buffer = None, []
    return sections


def _resolve_table(
    sections: Mapping[str, Mapping[str, Any]], name: str
) -> Dict[str, Any]:
    """Resolve one requested table, allowing a trailing KEY segment.

    ``"tool:pytest"`` matches the section directly; ``"scripts.test"`` matches a
    ``test`` key inside the ``scripts`` section, which is how a JSON manifest
    spells it.
    """
    normalized = _normalize_table_name(name)
    if sections.get(normalized):
        return dict(sections[normalized])
    if "." in normalized:
        parent, _, leaf = normalized.rpartition(".")
        values = sections.get(parent) or {}
        if leaf in values:
            return {leaf: values[leaf]}
    return {}


def _read_tables(
    text: str, tables: Sequence[str], *, toml: bool
) -> Dict[str, Dict[str, Any]]:
    """Read the named tables out of a TOML or INI-shaped text.

    Assumes the text is a small configuration file. Only requested tables that
    resolved to a NON-EMPTY mapping are returned, so an absent pytest table is
    an empty result (the caller then knows this file is not the inifile) rather
    than an empty section that looks present.
    """
    if not tables:
        return {}
    if toml:
        parsed = _load_toml(text)
        if parsed is not None:
            out: Dict[str, Dict[str, Any]] = {}
            for name in tables:
                node: Any = parsed
                for part in _normalize_table_name(name).split("."):
                    if not isinstance(node, Mapping) or part not in node:
                        node = None
                        break
                    node = node[part]
                if isinstance(node, Mapping) and node:
                    out[name] = dict(node)
            return out
    sections = _parse_sections(text)
    out = {}
    for name in tables:
        resolved = _resolve_table(sections, name)
        if resolved:
            out[name] = resolved
    return out


def _read_flat(text: str) -> Dict[str, Any]:
    """Read the key/value pairs of a section-less config (pytest.ini).

    Every section is merged, because ``pytest.ini`` has no section of its own
    but a stray ``[pytest]`` header is tolerated by the runner and must not
    silently drop the options it wraps.
    """
    merged: Dict[str, Any] = {}
    for values in _parse_sections(text).values():
        merged.update(values)
    return merged


def _read_text(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """Read a config file as text. Returns ``(text, error)``; never raises."""
    try:
        if path.is_symlink() or not path.is_file():
            return None, None
        size = path.stat().st_size
        if size > _MAX_READ_BYTES:
            return None, f"too large to read ({size} bytes)"
        return path.read_text(encoding="utf-8", errors="replace"), None
    except OSError as exc:
        return None, f"unreadable: {exc}"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, default=str, ensure_ascii=True)


def _addopts_tokens(options: Mapping[str, Any]) -> List[str]:
    """Tokenize the resolved ``addopts`` option, string or list shaped."""
    raw = options.get("addopts")
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw]
    text = str(raw).strip().strip("[]").replace(",", " ")
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def relaxation_slugs(tokens: Sequence[str]) -> List[str]:
    """Stable slugs for the narrowing / plug-in-disabling tokens in ``tokens``.

    Assumes a token list (typically ``shlex.split(addopts)``). A token that
    disables a plug-in (``-p no:randomly``) reports the ``addopts_plugin`` slug
    with the plug-in name as its detail, so a reader can tell it from a benign
    ``-p`` without a new slug per plug-in. ``-p`` with a value that does not
    start with ``no:`` is not a relaxation and is not reported.
    """
    out: List[str] = []
    tokens = [str(token) for token in tokens or []]
    for index, token in enumerate(tokens):
        slug = _RELAXATION_TOKENS.get(token.split("=", 1)[0])
        if slug is None:
            continue
        if slug == "addopts_plugin":
            value = token.split("=", 1)[1] if "=" in token else ""
            if not value and index + 1 < len(tokens):
                value = tokens[index + 1]
            if not value.lower().startswith("no:"):
                continue
        out.append(slug)
    return out


@dataclass
class ResolvedTestConfig:
    """The test configuration actually in force for one tree.

    ``resolved`` is False when the tree could not be read at all; the receipt
    then carries ``error`` and every field is empty. ``conftest_scope`` records
    HOW the ``conftest`` chain was resolved (``target_chain`` when a declared
    target test was supplied, ``root_only`` otherwise) so a partial resolution
    can never be read as a complete one.
    """

    root: str
    resolved: bool
    error: Optional[str] = None
    inifile: Optional[str] = None
    inifile_kind: Optional[str] = None
    options: Dict[str, Any] = field(default_factory=dict)
    addopts: List[str] = field(default_factory=list)
    conftests: Dict[str, str] = field(default_factory=dict)
    surfaces: Dict[str, Dict[str, str]] = field(default_factory=dict)
    external: Tuple[Dict[str, str], ...] = ()
    conftest_scope: str = "root_only"
    digest: str = ""
    test_command: Optional[str] = None

    def to_record(self) -> Dict[str, Any]:
        """JSON-safe projection for a trace row or a task receipt."""
        return {
            "schema_version": SCHEMA_VERSION,
            "root": self.root,
            "resolved": self.resolved,
            "error": self.error,
            "inifile": self.inifile,
            "inifile_kind": self.inifile_kind,
            "options": self.options,
            "addopts": self.addopts,
            "conftests": self.conftests,
            "conftest_scope": self.conftest_scope,
            "surfaces": {key: dict(value) for key, value in self.surfaces.items()},
            "external": [dict(row) for row in self.external],
            "test_command": self.test_command,
            "digest": self.digest,
        }

    def identity(self) -> Dict[str, Any]:
        """The part of the resolution a change comparison is made over.

        ``root`` is excluded on purpose: the pristine and working trees live at
        different paths by construction, so including it would report a change
        on every single run.
        """
        return {
            "inifile": self.inifile,
            "inifile_kind": self.inifile_kind,
            "options": self.options,
            "addopts": self.addopts,
            "conftests": self.conftests,
            "surfaces": {
                key: value.get("scoped") for key, value in sorted(self.surfaces.items())
            },
            "external": [dict(row) for row in self.external],
        }


def _inifile_of(identity: Mapping[str, Any]) -> Optional[str]:
    """The inifile name inside a resolution identity (small shared accessor)."""
    value = identity.get("inifile")
    return str(value) if value else None


def _surface_candidates(extra_paths: Sequence[str]) -> List[str]:
    """Repo-relative paths worth resolving, without walking the whole tree.

    The Python surfaces are always inspected (a repo's inifile chain is a fixed
    short list) and every other language's surface is inspected when the
    caller's changed set names it, which is the only way it could have changed.
    """
    candidates: List[str] = []
    seen: Set[str] = set()

    def add(rel: str) -> None:
        normalized = _normalize_rel(rel)
        if normalized and normalized not in seen:
            seen.add(normalized)
            candidates.append(normalized)

    for name, _kind, _tables in _INIFILE_PRECEDENCE:
        add(name)
    for name in _PYTHON_SURFACES:
        add(name)
    for rel in extra_paths or []:
        if is_test_config_path(rel):
            add(rel)
    return candidates


def _conftest_digest(root: Path, rel: str, errors: List[str]) -> Optional[str]:
    data, error = _read_text(root / rel)
    if error:
        errors.append(f"{rel}: {error}")
        return None
    if data is None:
        return None
    return _sha(data.encode("utf-8", "replace"))


def resolve_effective_test_config(
    root: str,
    *,
    target: Optional[str] = None,
    extra_paths: Sequence[str] = (),
    test_command: Optional[str] = None,
) -> ResolvedTestConfig:
    """Resolve the test configuration in force for the tree at ``root``.

    Assumes ``root`` is a directory; a missing directory yields
    ``resolved=False`` with an error rather than an exception. ``target`` is the
    declared target test (repo-relative) and selects the ``conftest`` chain that
    can actually reach it. ``extra_paths`` is the caller's changed-file set:
    supplying it is what lets a ``conftest.py`` added OUTSIDE the target's
    directory chain be detected without a repository-wide walk.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        return ResolvedTestConfig(
            root=str(root),
            resolved=False,
            error=f"not a directory: {root}",
            test_command=test_command,
        )

    errors: List[str] = []
    inifile: Optional[str] = None
    inifile_kind: Optional[str] = None
    options: Dict[str, Any] = {}
    for name, kind, tables in _INIFILE_PRECEDENCE:
        text, error = _read_text(root_path / name)
        if error:
            errors.append(f"{name}: {error}")
            continue
        if text is None:
            continue
        if tables:
            found = _read_tables(text, tables, toml=name.endswith(".toml"))
            if not found:
                continue
            merged: Dict[str, Any] = {}
            for value in found.values():
                for key, item in value.items():
                    merged[key] = _normalize_value(item)
            options = dict(sorted(merged.items())[:_MAX_OPTIONS])
        else:
            options = dict(sorted(_read_flat(text).items())[:_MAX_OPTIONS])
        inifile, inifile_kind = name, kind
        break

    conftests: Dict[str, str] = {}
    conftest_scope = "root_only"
    if target:
        conftest_scope = "target_chain"
        rel_target = _normalize_rel(target)
        directory = rel_target.rsplit("/", 1)[0] if "/" in rel_target else ""
        parts = [part for part in directory.split("/") if part] if directory else []
        for index in range(len(parts) + 1):
            prefix = "/".join(parts[:index])
            rel = f"{prefix}/conftest.py" if prefix else "conftest.py"
            if rel in conftests:
                continue
            digest = _conftest_digest(root_path, rel, errors)
            if digest is not None:
                conftests[rel] = digest
            if len(conftests) >= _MAX_CONFTEST_CHAIN:
                break
    if "conftest.py" not in conftests:
        digest = _conftest_digest(root_path, "conftest.py", errors)
        if digest is not None:
            conftests["conftest.py"] = digest
    for rel in extra_paths or []:
        normalized = _normalize_rel(rel)
        surface = classify_test_config_path(normalized)
        if surface is None or surface.kind != "conftest" or normalized in conftests:
            continue
        digest = _conftest_digest(root_path, normalized, errors)
        if digest is not None:
            conftests[normalized] = digest

    surfaces: Dict[str, Dict[str, str]] = {}
    for rel in _surface_candidates(extra_paths):
        surface = classify_test_config_path(rel)
        if surface is None:
            continue
        path = root_path / rel
        try:
            raw = (
                path.read_bytes()
                if (path.is_file() and not path.is_symlink())
                else None
            )
        except OSError as exc:
            errors.append(f"{rel}: unreadable: {exc}")
            raw = None
        if raw is None:
            continue
        if len(raw) > _MAX_READ_BYTES:
            errors.append(f"{rel}: too large to read")
            continue
        entry: Dict[str, str] = {"file": _sha(raw), "kind": surface.kind}
        if surface.scope == TABLE_SCOPED:
            found = _read_tables(
                raw.decode("utf-8", "replace"),
                surface.tables,
                toml=rel.endswith(".toml"),
            )
            entry["scoped"] = _sha(
                _canonical(
                    {name: found.get(name, {}) for name in surface.tables}
                ).encode()
            )
        else:
            entry["scoped"] = entry["file"]
        surfaces[rel] = entry

    external_rows: List[Dict[str, str]] = []
    for level in range(1, _MAX_EXTERNAL_LEVELS + 1):
        parent = root_path
        for _ in range(level):
            parent = parent.parent
        if not parent.is_dir():
            break
        for name, kind, _tables in _INIFILE_PRECEDENCE:
            data, error = _read_text(parent / name)
            if error:
                errors.append(f"../{name}: {error}")
                continue
            if data is None:
                continue
            row = {
                "level": str(level),
                "name": name,
                "kind": kind,
                "sha256": _sha(data.encode("utf-8", "replace")),
            }
            if row not in external_rows:
                external_rows.append(row)

    resolved = ResolvedTestConfig(
        root=str(root),
        resolved=True,
        error="; ".join(errors) if errors else None,
        inifile=inifile,
        inifile_kind=inifile_kind,
        options=options,
        addopts=_addopts_tokens(options),
        conftests=dict(sorted(conftests.items())),
        surfaces=dict(sorted(surfaces.items())),
        external=tuple(external_rows),
        conftest_scope=conftest_scope,
        test_command=test_command,
    )
    resolved.digest = _sha(_canonical(resolved.identity()).encode())
    return resolved


def diff_effective_test_config(
    baseline: ResolvedTestConfig, work: ResolvedTestConfig
) -> Dict[str, Any]:
    """Compare two resolutions and describe what changed, if anything.

    Assumes both were produced by ``resolve_effective_test_config`` over the
    pristine and working trees of the same repository. Returns a JSON-safe dict
    with ``changed``, ``changed_fields``, ``changed_paths``, ``relaxations`` and
    the full ``before``/``after`` records. ``changed`` is False whenever either
    side failed to resolve, because an unresolved comparison is not evidence of
    a change — the receipt says so rather than inventing a diff.
    """
    if not (baseline.resolved and work.resolved):
        return {
            "changed": False,
            "resolved": False,
            "error": "; ".join(part for part in (baseline.error, work.error) if part)
            or "configuration could not be resolved",
            "changed_fields": [],
            "changed_paths": [],
            "relaxations": [],
            "before": baseline.to_record(),
            "after": work.to_record(),
        }
    before, after = baseline.identity(), work.identity()
    changed_fields = sorted(key for key in before if before[key] != after.get(key))

    changed_paths: List[str] = []
    for rel in sorted(set(before["surfaces"]) | set(after["surfaces"])):
        if before["surfaces"].get(rel) != after["surfaces"].get(rel):
            changed_paths.append(rel)
    for rel in sorted(set(before["conftests"]) | set(after["conftests"])):
        if (
            before["conftests"].get(rel) != after["conftests"].get(rel)
            and rel not in changed_paths
        ):
            changed_paths.append(rel)

    relaxations: List[Dict[str, str]] = []
    before_tokens = [str(token) for token in before["addopts"]]
    after_tokens = [str(token) for token in after["addopts"]]
    if before_tokens == after_tokens:
        added: List[str] = []
    elif after_tokens[: len(before_tokens)] == before_tokens:
        added = after_tokens[len(before_tokens) :]
    else:
        added = after_tokens
    inifile_path = _inifile_of(after) or _inifile_of(before) or ""
    for slug in relaxation_slugs(added):
        relaxations.append({"slug": slug, "detail": "addopts", "path": inifile_path})
    if before_tokens != after_tokens:
        relaxations.append(
            {
                "slug": "addopts_changed",
                "detail": f"{_canonical(before_tokens)} -> {_canonical(after_tokens)}",
                "path": inifile_path,
            }
        )
    for key in sorted(set(before["options"]) | set(after["options"])):
        old_value, new_value = before["options"].get(key), after["options"].get(key)
        if old_value == new_value:
            continue
        slug = _SCOPE_KEYS.get(key)
        if slug:
            relaxations.append(
                {
                    "slug": slug,
                    "detail": f"{key}: {_canonical(old_value)} -> {_canonical(new_value)}",
                    "path": inifile_path,
                }
            )
    for rel in changed_paths:
        surface = classify_test_config_path(rel)
        kind = surface.kind if surface else "conftest"
        existed_before = rel in before["surfaces"] or rel in before["conftests"]
        existed_after = rel in after["surfaces"] or rel in after["conftests"]
        if not existed_before:
            slug = f"{kind}_added"
        elif not existed_after:
            slug = f"{kind}_removed"
        else:
            slug = f"{kind}_modified"
        relaxations.append({"slug": slug, "detail": rel, "path": rel})
    if before["inifile"] != after["inifile"]:
        relaxations.append(
            {
                "slug": "inifile_changed",
                "detail": f"{before['inifile']} -> {after['inifile']}",
                "path": after["inifile"] or before["inifile"] or "",
            }
        )

    seen: Set[Tuple[str, str, str]] = set()
    unique: List[Dict[str, str]] = []
    for row in relaxations:
        key = (row["slug"], row["detail"], row["path"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)

    return {
        "changed": bool(changed_fields),
        "resolved": True,
        "error": None,
        "changed_fields": changed_fields,
        "changed_paths": changed_paths,
        "relaxations": unique,
        "before": baseline.to_record(),
        "after": work.to_record(),
    }


@dataclass
class TestConfigReceipt:
    """What the guard found, in the shape the trace and the result both carry."""

    declared: bool = False
    declared_reason: Optional[str] = None
    changed: bool = False
    resolved: bool = True
    violations: List[Dict[str, str]] = field(default_factory=list)
    changed_paths: List[str] = field(default_factory=list)
    changed_fields: List[str] = field(default_factory=list)
    relaxations: List[Dict[str, str]] = field(default_factory=list)
    before: Dict[str, Any] = field(default_factory=dict)
    after: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    test_command: Optional[str] = None

    def to_record(self) -> Dict[str, Any]:
        """JSON-safe projection, carrying the event name it is logged under."""
        return {
            "schema_version": SCHEMA_VERSION,
            "event": TEST_CONFIG_CHANGED_EVENT,
            "declared": self.declared,
            "declared_reason": self.declared_reason,
            "declared_key": TEST_CONFIG_CHANGE_KEY if self.declared else None,
            "changed": self.changed,
            "resolved": self.resolved,
            "violations": list(self.violations),
            "changed_paths": list(self.changed_paths),
            "changed_fields": list(self.changed_fields),
            "relaxations": list(self.relaxations),
            "before": self.before,
            "after": self.after,
            "error": self.error,
            "test_command": self.test_command,
        }


@dataclass
class TestConfigVerdict:
    """The guard's answer: ``ok``, the model-facing message, and the receipt.

    ``allowed_paths`` lists the changed test-config surface paths a DECLARED
    change cleared. The edit gate uses it to skip the generic protected-path
    rule for exactly those files, so a declaration is honored once and only
    once, and only for test configuration. (A declaration therefore also lifts
    the configured glob for those specific files — the same trade the
    pre-existing `harness.core._authorized_test_target` makes when it strips
    the test globs. The declaration is a recorded, run-owner-level intent, not
    something the agent can write for itself.)
    """

    ok: bool
    message: str
    receipt: TestConfigReceipt
    violations: List[Dict[str, str]] = field(default_factory=list)
    allowed_paths: List[str] = field(default_factory=list)


def _violations(
    baseline: ResolvedTestConfig,
    work: ResolvedTestConfig,
    extra_paths: Sequence[str],
) -> List[Dict[str, str]]:
    """Per-surface file-level verdicts, independent of any resolution failure.

    A whole-file surface is a violation when its bytes differ (or it exists on
    only one side). A table-scoped surface is a violation when only the tables in
    scope differ, so editing ``[project] dependencies`` in ``pyproject.toml``
    stays a normal edit while editing ``[tool.pytest.ini_options]`` is refused.
    """
    out: List[Dict[str, str]] = []
    candidates: Set[str] = set(baseline.surfaces) | set(work.surfaces)
    for rel in extra_paths or []:
        if is_test_config_path(rel):
            candidates.add(_normalize_rel(rel))
    for rel in sorted(candidates):
        surface = classify_test_config_path(rel)
        if surface is None:
            continue
        old = baseline.surfaces.get(rel) or {}
        new = work.surfaces.get(rel) or {}
        if surface.scope == WHOLE_FILE:
            differs = old.get("file") != new.get("file")
            scope_label = "whole file"
        else:
            differs = old.get("scoped") != new.get("scoped")
            scope_label = "table " + ", ".join(surface.tables)
        if differs:
            out.append(
                {
                    "path": rel,
                    "kind": surface.kind,
                    "language": surface.language,
                    "scope": scope_label,
                    "reason": (
                        f"test configuration surface modified: {rel} "
                        f"({surface.kind}, {scope_label})"
                    ),
                }
            )
    return out


def test_config_guard(
    pristine_dir: str,
    work_dir: str,
    *,
    config: Optional[Mapping[str, Any]] = None,
    changed_paths: Sequence[str] = (),
    target_test: Optional[str] = None,
) -> TestConfigVerdict:
    """Refuse an undeclared change to a repository's test configuration.

    Assumes ``pristine_dir`` and ``work_dir`` are the two trees the edit gate
    compares; ``changed_paths`` is that gate's already-computed diff (supplying
    it is what keeps this O(changed) rather than a repository walk). ``config``
    is the merged ``Task.config``: it is the ONLY place a legitimate test-config
    change can be declared (``TEST_CONFIG_CHANGE_KEY``), and a declaration is
    recorded in the receipt rather than silently honored.

    Returns ``ok=False`` with a model-facing message when a surface changed and
    no declaration was made, or when the effective configuration differs for a
    reason no surface explains. A declared change proceeds AND is still
    reported, with the relaxations it introduced, so a declaration is a receipt,
    not a bypass.
    """
    reason = declared_test_config_change(config)
    command: Optional[str] = None
    target: Optional[str] = target_test
    if isinstance(config, Mapping):
        raw_command = config.get("test_command")
        command = str(raw_command) if raw_command else None
        target = target_test or (
            str(config.get("target_test")) if config.get("target_test") else None
        )
    target = str(target) if target else None

    candidates = [rel for rel in (changed_paths or []) if is_test_config_path(rel)]
    baseline = resolve_effective_test_config(
        pristine_dir, target=target, extra_paths=candidates, test_command=command
    )
    work = resolve_effective_test_config(
        work_dir, target=target, extra_paths=candidates, test_command=command
    )
    diff = diff_effective_test_config(baseline, work)
    violations = _violations(baseline, work, candidates)

    receipt = TestConfigReceipt(
        declared=reason is not None,
        declared_reason=reason,
        changed=bool(diff.get("changed")),
        resolved=bool(diff.get("resolved")),
        violations=violations,
        changed_paths=list(diff.get("changed_paths") or []),
        changed_fields=list(diff.get("changed_fields") or []),
        relaxations=list(diff.get("relaxations") or []),
        before=diff.get("before") or {},
        after=diff.get("after") or {},
        error=diff.get("error"),
        test_command=command,
    )

    if not violations and not receipt.changed:
        return TestConfigVerdict(ok=True, message="ok", receipt=receipt)

    if reason is not None:
        allowed = sorted(
            {
                _normalize_rel(rel)
                for rel in (changed_paths or [])
                if is_test_config_path(rel)
            }
        )
        return TestConfigVerdict(
            ok=True, message="ok", receipt=receipt, allowed_paths=allowed
        )

    if violations:
        first = violations[0]
        slugs = (
            ", ".join(sorted({row["slug"] for row in receipt.relaxations})) or "none"
        )
        message = (
            f"protected path modified: {first['path']} ({first['reason']}). "
            "This is a TEST CONFIGURATION surface: relaxing it makes a suite "
            "pass without fixing anything. Do not modify it. If changing test "
            "configuration is this task's declared purpose, the run owner must "
            f"declare it in Task.config['{TEST_CONFIG_CHANGE_KEY}'] with a reason. "
            f"Resolved relaxations: {slugs}."
        )
        return TestConfigVerdict(
            ok=False, message=message, receipt=receipt, violations=violations
        )

    message = (
        "test configuration changed: "
        f"{', '.join(receipt.changed_fields) or 'effective resolution'} differs "
        f"from the baseline ({', '.join(receipt.changed_paths) or 'no surface'}). "
        "A test run whose effective configuration differs from the baseline is "
        "not evidence about the same suite. If changing test configuration is "
        f"this task's declared purpose, the run owner must declare it in "
        f"Task.config['{TEST_CONFIG_CHANGE_KEY}'] with a reason."
    )
    return TestConfigVerdict(
        ok=False, message=message, receipt=receipt, violations=violations
    )
