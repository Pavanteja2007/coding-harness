"""Product brand names, and the ONE place the previous name still works.

The product was renamed from ``vex`` to ``neo``. A rename that breaks an
existing install's configuration is not a rename, it is an outage with a
changelog, so the old spellings are still ACCEPTED on the way in and are
never written on the way out.

What this module owns:

* the canonical names (:data:`COMMAND`, :data:`DISTRIBUTION`, :data:`ENV_PREFIX`,
  :data:`PROJECT_DIRNAME`, :data:`HOME_DIRNAME`, :data:`MARKUP_PREFIX`);
* :func:`apply_legacy_env`, the single point where a legacy environment
  variable is honoured — it is called once per process from the entry points
  rather than being threaded through every one of the ~30 call sites that
  read configuration from the environment;
* :func:`legacy_home_candidates` / :func:`legacy_project_dir`, the read-only
  fallbacks that let an install which has not migrated its on-disk state
  keep working;
* :func:`deprecation_notice`, the one sentence a surface prints at most once.

Design constraints this file exists to hold:

1. **New is written, old is read.** Every writer in the tree emits the
   canonical spelling. Nothing writes ``VEX_*``, ``.vex/`` or
   ``vex-harness`` any more.
2. **A legacy value never silently outranks an explicit new one.** If both
   are set, the new one wins; the legacy one is ignored, and the receipt
   says so. Honouring the old spelling when the user has set the new one is
   how a "which one do I edit" bug becomes permanent.
3. **The fallback is a READ path.** It never migrates, copies, renames or
   writes into a legacy location. Silently moving a user's configuration
   is how a tool loses it.
4. **Deprecation is visible but not noisy.** One line, once, naming the
   replacement — not a warning per call site.
"""

from __future__ import annotations

import os
from typing import Dict, Mapping, MutableMapping, Optional, Tuple

__all__ = [
    "COMMAND",
    "DISTRIBUTION",
    "ENV_PREFIX",
    "HOME_DIRNAME",
    "LEGACY_COMMAND",
    "LEGACY_DISTRIBUTIONS",
    "LEGACY_ENV_PREFIX",
    "MARKUP_PREFIX",
    "PROJECT_DIRNAME",
    "apply_legacy_env",
    "deprecation_notice",
    "legacy_home_candidates",
    "legacy_names_report",
    "legacy_project_dirname",
]

#: The installed console script.
COMMAND = "neo"
#: The distribution name on PyPI. The command and the distribution name are
#: deliberately different, the same way beautifulsoup4 ships as bs4.
DISTRIBUTION = "neo-agent-cli"
#: Prefix for every environment variable the product reads.
ENV_PREFIX = "NEO"
#: Prefix for a rich-markup style / Textual CSS variable.
MARKUP_PREFIX = "neo"
#: The per-repository state directory, ``<repo>/.neo/``.
PROJECT_DIRNAME = ".neo"
#: The user-owned state root, ``~/.neo`` (or the platform data dir).
HOME_DIRNAME = "neo"

#: The previous console script. Still installed, still works, removed in a
#: dedicated migration release.
LEGACY_COMMAND = "vex"
#: The previous distribution names, newest first. ``neo-harness`` is the
#: immediately-previous working name (never published to PyPI, but installable
#: from a local build, so a machine can still carry it). ``harness`` predates
#: ``vex``; both remain on PyPI under this project's account, so an uninstall
#: must be able to name either.
LEGACY_DISTRIBUTIONS: Tuple[str, ...] = ("neo-harness", "vex-harness")
#: The previous environment-variable prefix.
LEGACY_ENV_PREFIX = "VEX"
#: The previous state directory names, newest first.
LEGACY_HOME_DIRNAME = "vex"

#: Environment variables that must NEVER be carried across from the legacy
#: prefix, because honouring them would change behaviour rather than
#: preserve it.
_LEGACY_EXCLUDED = frozenset()

_notice_shown = False


def apply_legacy_env(
    env: Optional[MutableMapping[str, str]] = None,
) -> Dict[str, str]:
    """Copy every legacy ``VEX_*`` variable onto its ``NEO_*`` counterpart.

    Returns the mapping that was applied, so a caller can report exactly
    which names were honoured rather than saying "your configuration was
    migrated".

    Precedence: **the new name always wins.** A ``NEO_HOME`` that is set
    while a stale ``VEX_HOME`` is also exported must resolve to
    ``NEO_HOME``; otherwise a user who renamed the variable in their profile
    and still has the old one exported somewhere would silently keep the old
    root, and would have no way to tell which one the product used.

    Total by contract: a non-mapping ``env`` raises ``TypeError`` rather than
    quietly doing nothing, and a value that is not a string is passed through
    untouched rather than stringified into a path.
    """
    target: MutableMapping[str, str] = os.environ if env is None else env
    if not hasattr(target, "get") or not hasattr(target, "__setitem__"):
        raise TypeError(f"env must be a mutable mapping, got {type(env).__name__}")

    applied: Dict[str, str] = {}
    for raw in list(target.keys()):
        if not isinstance(raw, str) or not raw.startswith(LEGACY_ENV_PREFIX + "_"):
            continue
        suffix = raw[len(LEGACY_ENV_PREFIX) + 1 :]
        if not suffix or suffix in _LEGACY_EXCLUDED:
            continue
        new_name = f"{ENV_PREFIX}_{suffix}"
        existing = target.get(new_name)
        if existing is not None and str(existing).strip():
            # The new spelling is explicitly set; the legacy one loses.
            continue
        value = target.get(raw)
        if value is None or not str(value).strip():
            continue
        target[new_name] = value
        applied[new_name] = str(value)
    return applied


def legacy_home_candidates() -> Tuple[str, ...]:
    """Previous state-root directory names, newest first.

    Used to locate an install's existing state on disk. Returned as names
    rather than paths because the platform data directory is resolved by
    :mod:`memory.paths`, which is the single location authority; this module
    must not become a second one.
    """
    return (LEGACY_HOME_DIRNAME,)


def legacy_project_dirname() -> str:
    """The previous per-repository state directory name, ``.vex``."""
    return f".{LEGACY_HOME_DIRNAME}"


def deprecation_notice(*, force: bool = False) -> str:
    """The one line a surface prints when it honoured a legacy name.

    Empties itself after the first call so a per-call-site notice cannot
    turn into a warning storm; ``force=True`` re-arms it for tests. The text
    names the replacement, because a deprecation notice that does not say
    what to type is a notice nobody can act on.
    """
    global _notice_shown
    if _notice_shown and not force:
        return ""
    _notice_shown = True
    return (
        f"note: `{LEGACY_ENV_PREFIX}_*` and `{LEGACY_COMMAND}` are the previous "
        f"names and still work. Rename them to `{ENV_PREFIX}_*` and `{COMMAND}`; "
        f"the old spellings are removed in a later release."
    )


def legacy_names_report(env: Optional[Mapping[str, str]] = None) -> dict:
    """What legacy spelling is currently live, as data.

    Read-only and total: a surface that wants to warn (or a test that wants
    to assert the compat path is reachable) reads this instead of reaching
    into ``os.environ`` and re-deriving the prefix rules. ``honoured`` lists
    only the names that ``apply_legacy_env`` would actually carry across, so
    a report cannot claim a legacy variable is in use when a new one
    overrides it.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    present: list[str] = []
    for raw in sorted(source.keys()):
        if not isinstance(raw, str):
            continue
        if not raw.startswith(LEGACY_ENV_PREFIX + "_"):
            continue
        if not str(source.get(raw) or "").strip():
            continue
        present.append(raw)

    carried: list[str] = []
    shadowed: list[str] = []
    for raw in present:
        suffix = raw[len(LEGACY_ENV_PREFIX) + 1 :]
        if not suffix or suffix in _LEGACY_EXCLUDED:
            continue
        new_name = f"{ENV_PREFIX}_{suffix}"
        if str(source.get(new_name) or "").strip():
            shadowed.append(raw)
        else:
            carried.append(new_name)

    return {
        "command": COMMAND,
        "distribution": DISTRIBUTION,
        "legacy_command": LEGACY_COMMAND,
        "legacy_distributions": list(LEGACY_DISTRIBUTIONS),
        "legacy_env_prefix": LEGACY_ENV_PREFIX,
        "legacy_home_dirname": LEGACY_HOME_DIRNAME,
        "project_dirname": PROJECT_DIRNAME,
        "legacy_project_dirname": legacy_project_dirname(),
        "legacy_env_present": present,
        "legacy_env_honoured": carried,
        "legacy_env_shadowed": shadowed,
        "notice": deprecation_notice(force=True),
    }
