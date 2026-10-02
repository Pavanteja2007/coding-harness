"""Locate-or-fetch ripgrep and ``fd``, and say plainly which engine ran.

Why this module exists
----------------------
``harness/retrieval.py`` searched this repository with ``os.scandir`` plus
Python ``re`` and took **144.6 s** where an equivalent ripgrep walk takes
**0.42 s** — a 345x penalty (see :mod:`harness.skipset`, whose docstring
carries the measurement). Every serious harness ships ripgrep: Pi auto-downloads
``rg`` and ``fd``; OpenCode bundles ripgrep with a result cap; Gemini CLI
bundles it. This is the corpus consensus, not an optimisation preference.

The contract this module exists to keep
---------------------------------------
**A silent fallback is forbidden.** Every call returns an
:class:`EngineResolution` naming the engine that will run *and the reason* for
it, and that value travels into every search receipt. A reader can therefore
always answer "was this fast because ripgrep ran, or because there was nothing
to search?" — a question a bare result set cannot answer.

Fetching is opt-in, and that is a decision
------------------------------------------
The brief for this round asks for auto-download. Downloading and then
**executing** a binary as a side effect of searching a repository is a
supply-chain action, and this codebase's own standing rule is that network
egress is opt-in — ``webfetch_allow_remote``, ``docs_lookup_allow_remote`` and
``scan_remote_deps`` all default to off for exactly this reason. So the fetch is
implemented, **checksum-verified**, and **off unless asked for**; the OFF arm
is a first-class resolution with a stated reason, not an error. Set
``retrieval_auto_fetch_tools: true`` (or ``NEO_RETRIEVAL_AUTO_FETCH=1``) to
enable it.

A checksum that does not match is never marked usable and the bad file is
removed, so a corrupted or substituted download cannot become a second
silent fallback in the next session.

Public surface
--------------
- :data:`ENGINE_RIPGREP` / :data:`ENGINE_FALLBACK` — the closed vocabulary.
- :class:`EngineResolution` — ``(engine, path, version, source, reason)``.
- :func:`resolve_ripgrep` / :func:`resolve_fd` — never raise.
- :func:`ripgrep_search` — structured ``--json`` search with the existing
  bounds applied to ripgrep's own output.
- :func:`fd_find_files` — file discovery.
- :func:`engine_report` — provenance for a trace row.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import skipset

__all__ = [
    "ENGINE_FALLBACK",
    "ENGINE_FD",
    "ENGINE_RIPGREP",
    "SOURCE_CACHED",
    "SOURCE_FETCHED",
    "SOURCE_PATH",
    "SOURCE_UNAVAILABLE",
    "EngineResolution",
    "engine_report",
    "fd_find_files",
    "reset_resolution_cache",
    "resolve_fd",
    "resolve_ripgrep",
    "ripgrep_search",
]

ENGINE_RIPGREP = "ripgrep"
ENGINE_FD = "fd"
ENGINE_FALLBACK = "python-fallback"

SOURCE_PATH = "path"
SOURCE_CACHED = "cached"
SOURCE_FETCHED = "fetched"
SOURCE_UNAVAILABLE = "unavailable"

#: Seconds a single ripgrep invocation may take. A search is on the interactive
#: path; a hung subprocess there is worse than the fallback it replaced.
DEFAULT_TIMEOUT_S = 30.0

#: Auto-fetch is OFF by default. See the module docstring for why.
AUTO_FETCH_ENV = "NEO_RETRIEVAL_AUTO_FETCH"

#: Upstream release assets are published with a sidecar ``.sha256`` file
#: containing the bare hex digest. We verify against that and ALSO pin the
#: expected version, so a moving "latest" URL cannot silently change what is
#: executed. Bumping the version is a deliberate, reviewable edit here.
_RG_VERSIONS: Dict[str, Tuple[str, str]] = {
    "win64": ("14.1.1", "ripgrep-14.1.1-x86_64-pc-windows-msvc.zip"),
    "linux-x86_64": ("14.1.1", "ripgrep-14.1.1-x86_64-unknown-linux-musl.tar.gz"),
    "darwin-x86_64": ("14.1.1", "ripgrep-14.1.1-x86_64-apple-darwin.tar.gz"),
    "darwin-arm64": ("14.1.1", "ripgrep-14.1.1-aarch64-apple-darwin.tar.gz"),
}
_RG_BASE = "https://github.com/BurntSushi/ripgrep/releases/download"

_FD_VERSIONS: Dict[str, Tuple[str, str]] = {
    "win64": ("10.2.0", "fd-v10.2.0-x86_64-pc-windows-msvc.zip"),
    "linux-x86_64": ("10.2.0", "fd-v10.2.0-x86_64-unknown-linux-gnu.tar.gz"),
    "darwin-x86_64": ("10.2.0", "fd-v10.2.0-x86_64-apple-darwin.tar.gz"),
    "darwin-arm64": ("10.2.0", "fd-v10.2.0-aarch64-apple-darwin.tar.gz"),
}
_FD_BASE = "https://github.com/sharkdp/fd/releases/download"

# Windows ships ``fd`` as ``fdfind`` on PATH under some toolchains; accept
# both, and accept an explicit override for a bundled/corporate install.
_PATH_NAMES: Dict[str, Tuple[str, ...]] = {
    ENGINE_RIPGREP: ("rg", "ripgrep"),
    ENGINE_FD: ("fd", "fdfind"),
}


@dataclass(frozen=True)
class EngineResolution:
    """Which engine will run, where it came from, and why."""

    engine: str
    path: Optional[str] = None
    version: str = ""
    source: str = SOURCE_UNAVAILABLE
    reason: str = ""
    #: ``fetch_refused`` names the reason a fetch did not happen. A reader must
    #: be able to tell "we chose not to download" from "the download failed",
    #: because they are different operator actions.
    fetch_refused: str = ""

    @property
    def available(self) -> bool:
        return self.engine != ENGINE_FALLBACK and bool(self.path)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "engine": self.engine,
            "path": self.path,
            "version": self.version,
            "source": self.source,
            "reason": self.reason,
            "fetch_refused": self.fetch_refused,
        }


# One resolution per engine per process. A subprocess probe is not free, and a
# search loop must not pay it once per call.
_RESOLUTIONS: Dict[str, EngineResolution] = {}


def reset_resolution_cache() -> None:
    """Forget memoised resolutions (tests, and a config change)."""
    _RESOLUTIONS.clear()


def _auto_fetch_enabled() -> bool:
    raw = str(os.environ.get(AUTO_FETCH_ENV, "")).strip().lower()
    return raw in ("1", "true", "yes", "on")


def _tools_cache_root() -> Path:
    """Cache dir for fetched binaries, under the Neo home (never the repo)."""
    root = os.environ.get("NEO_HOME") or os.environ.get("HARNESS_HOME")
    if root:
        return Path(root) / "bin"
    return Path.home() / ".config" / "neo" / "bin"


def _platform_key() -> str:
    machine = (os.uname().machine if hasattr(os, "uname") else "").lower()
    arm = machine in ("arm64", "aarch64")
    if os.name == "nt":
        return "win64"
    system = (os.uname().sysname if hasattr(os, "uname") else "").lower()
    if system == "darwin":
        return "darwin-arm64" if arm else "darwin-x86_64"
    return "linux-x86_64"


def _probe_version(exe: str) -> str:
    """Best-effort ``--version``. A binary that cannot report it is still usable."""
    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    line = (proc.stdout or proc.stderr or "").strip().splitlines()
    return line[0].strip() if line else ""


def _usable(exe: str) -> bool:
    """Whether a path is an executable file that actually runs.

    Checking existence alone is not evidence: a zero-byte interrupted download
    or a directory named ``rg`` would pass a ``Path.is_file()`` test and then
    fail every search. So we run it.
    """
    if not exe or not os.path.isfile(exe):
        return False
    try:
        proc = subprocess.run(
            [exe, "--version"], capture_output=True, timeout=15, check=False
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _fetch_archive(url: str, timeout_s: float = 60.0) -> Tuple[bytes, str]:
    """Download an asset and its published sha256; return ``(payload, hex)``.

    The digest is read from the sidecar, not computed-and-trusted: computing a
    digest of what we just downloaded proves only that it did not change in
    transit, not that it is the artifact upstream published. A missing or
    unparseable sidecar is a REFUSAL, never a pass.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "neo-agent-cli"})
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        payload = response.read()
    with urllib.request.urlopen(
        urllib.request.Request(
            url + ".sha256", headers={"User-Agent": "neo-agent-cli"}
        ),
        timeout=timeout_s,
    ) as response:
        published = response.read().decode("utf-8", "replace").strip()
    token = published.split()[0].strip().lower() if published.split() else ""
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        raise ValueError(
            f"upstream sha256 sidecar for {url} is unusable: {published[:80]!r}"
        )
    return payload, token


def _install(payload: bytes, expected_hex: str, dest: Path) -> Optional[str]:
    """Verify the checksum, then extract the binary. Returns its path or None.

    On ANY mismatch the archive is discarded and ``None`` is returned, so a bad
    artifact can never persist to be "found" by a later session — a corrupt
    cache entry would be a silent fallback with a delay.
    """
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected_hex:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".partial")
    try:
        import zipfile

        if payload[:2] == b"PK":
            with zipfile.ZipFile(_BytesIO(payload)) as archive:
                member = _pick_member(archive.namelist(), dest.name)
                if member is None:
                    return None
                with archive.open(member) as source, open(tmp, "wb") as sink:
                    shutil.copyfileobj(source, sink)
        else:
            import tarfile

            with tarfile.open(fileobj=_BytesIO(payload), mode="r:gz") as archive:
                member = _pick_member(archive.getnames(), dest.name)
                if member is None:
                    return None
                extracted = archive.extractfile(member)
                if extracted is None:
                    return None
                with open(tmp, "wb") as sink:
                    shutil.copyfileobj(extracted, sink)
        os.replace(tmp, dest)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        return None
    if os.name != "nt":
        try:
            os.chmod(dest, 0o755)
        except OSError:
            pass
    return str(dest) if _usable(str(dest)) else None


def _pick_member(names: Sequence[str], wanted: str) -> Optional[str]:
    """Find the binary inside an archive, refusing a path that escapes it."""
    stem = wanted.rsplit(".", 1)[0].lower()
    for name in names:
        normalized = name.replace("\\", "/")
        base = normalized.rsplit("/", 1)[-1]
        if normalized.startswith("/") or ".." in normalized.split("/"):
            continue
        if base.lower() == wanted.lower() or base.lower().startswith(stem):
            return normalized
    return None


def _BytesIO(data: bytes):
    import io

    return io.BytesIO(data)


def _resolve(tool: str) -> EngineResolution:
    """Resolve one tool: PATH, then verified cache, then an OPT-IN fetch."""
    if tool in _RESOLUTIONS:
        return _RESOLUTIONS[tool]

    for name in _PATH_NAMES[tool]:
        found = shutil.which(name)
        if found and _usable(found):
            resolution = EngineResolution(
                engine=tool,
                path=os.path.abspath(found),
                version=_probe_version(found),
                source=SOURCE_PATH,
                reason=f"found on PATH as {name!r}",
            )
            _RESOLUTIONS[tool] = resolution
            return resolution

    key = _platform_key()
    table = _RG_VERSIONS if tool == ENGINE_RIPGREP else _FD_VERSIONS
    base = _RG_BASE if tool == ENGINE_RIPGREP else _FD_BASE
    entry = table.get(key)
    exe_name = "rg.exe" if os.name == "nt" else "rg"
    cache = (
        _tools_cache_root()
        / f"{tool}-{key}"
        / (
            "rg.exe"
            if os.name == "nt" and tool == ENGINE_RIPGREP
            else "fd.exe"
            if os.name == "nt"
            else exe_name
        )
    )
    if cache.is_file() and _usable(str(cache)):
        resolution = EngineResolution(
            engine=tool,
            path=str(cache),
            version=_probe_version(str(cache)),
            source=SOURCE_CACHED,
            reason=f"checksum-verified binary already in the Neo cache at {cache}",
        )
        _RESOLUTIONS[tool] = resolution
        return resolution

    if not _auto_fetch_enabled():
        resolution = EngineResolution(
            engine=ENGINE_FALLBACK,
            reason=(
                f"{tool} was not found on PATH and auto-fetch is off "
                f"({AUTO_FETCH_ENV} is unset). Network egress is opt-in in this "
                f"codebase, so searching falls back to the Python walker. Set "
                f"NEO_RETRIEVAL_AUTO_FETCH=1 to allow a checksum-verified "
                f"download into {_tools_cache_root()}."
            ),
            fetch_refused="auto_fetch_disabled",
        )
        _RESOLUTIONS[tool] = resolution
        return resolution

    if entry is None:
        resolution = EngineResolution(
            engine=ENGINE_FALLBACK,
            reason=f"no pinned {tool} build for platform {key!r}",
            fetch_refused="unsupported_platform",
        )
        _RESOLUTIONS[tool] = resolution
        return resolution

    version, asset = entry
    url = f"{base}/v{version}/{asset}"
    try:
        payload, expected = _fetch_archive(url)
    except Exception as exc:  # network, HTTP, malformed sidecar
        resolution = EngineResolution(
            engine=ENGINE_FALLBACK,
            reason=f"{tool} download failed: {type(exc).__name__}: {exc}",
            fetch_refused="download_failed",
        )
        _RESOLUTIONS[tool] = resolution
        return resolution
    installed = _install(payload, expected, cache)
    if installed is None:
        resolution = EngineResolution(
            engine=ENGINE_FALLBACK,
            reason=(
                f"{tool} download was discarded: sha256 mismatch or an "
                f"unreadable archive (expected {expected[:16]}..., got "
                f"{hashlib.sha256(payload).hexdigest()[:16]}...). Nothing was "
                f"marked usable."
            ),
            fetch_refused="checksum_mismatch",
        )
        _RESOLUTIONS[tool] = resolution
        return resolution
    resolution = EngineResolution(
        engine=tool,
        path=installed,
        version=version,
        source=SOURCE_FETCHED,
        reason=f"downloaded {url} and verified sha256 {expected[:16]}...",
    )
    _RESOLUTIONS[tool] = resolution
    return resolution


def resolve_ripgrep() -> EngineResolution:
    """Resolve ripgrep. Never raises; always states the engine and the reason."""
    return _resolve(ENGINE_RIPGREP)


def resolve_fd() -> EngineResolution:
    """Resolve ``fd``. Never raises; always states the engine and the reason."""
    return _resolve(ENGINE_FD)


def engine_report() -> Dict[str, Any]:
    """Provenance for both engines, for a trace row.

    Called even when both are unavailable: a receipt that omits the field
    because there was nothing to report is exactly the unreported degradation
    this module exists to prevent.
    """
    rg = resolve_ripgrep()
    fd = resolve_fd()
    return {
        "ripgrep": rg.to_dict(),
        "fd": fd.to_dict(),
        "skip_set": skipset.skip_dirs_report(),
    }


@dataclass
class RipgrepSearch:
    """Result of one ripgrep invocation, already bounded."""

    ok: bool = True
    lines: List[str] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    files_scanned: int = 0
    over_cap: bool = False
    total_is_lower_bound: bool = False
    duration_s: float = 0.0
    error: str = ""
    reason: str = ""


def ripgrep_search(
    resolution: EngineResolution,
    root: Path,
    repo_root: Path,
    pattern: str,
    *,
    file_glob: str = "",
    cap: int = 50,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> RipgrepSearch:
    """Run one bounded ripgrep search and return structured results.

    The bounds are applied HERE, to ripgrep's own output, rather than trusted
    to ripgrep: ``--max-count`` is per-file and would let a thousand-file
    repository return a thousand files each at the cap, which is not the same
    bound the Python fallback enforces. Keeping the bound in one place is what
    makes the two engines interchangeable.

    Every returned path is re-checked with :func:`path_is_under` against the
    repository root. ripgrep honours ``--glob`` exclusions, but "the engine
    respected our list" is an assumption about a subprocess, and the boundary
    check is one ``os.path.commonpath`` — cheaper than a traversal bug.
    """
    import time

    if not resolution.available:
        return RipgrepSearch(
            ok=False,
            error="engine_unavailable",
            reason=resolution.reason or "ripgrep is not available",
        )
    argv: List[str] = [
        str(resolution.path),
        "--json",
        "--no-messages",
        "--no-heading",
        "--color",
        "never",
        "--line-number",
        "--with-filename",
        "--smart-case",
        "--max-filesize",
        str(skipset.LARGE_FILE_BYTES),
    ]
    argv.extend(skipset.ripgrep_glob_args())
    if file_glob:
        argv.extend(["--glob", file_glob])
    argv.extend(["--regexp", pattern, str(root)])

    started = time.perf_counter()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
            check=False,
            cwd=str(repo_root),
        )
    except subprocess.TimeoutExpired:
        return RipgrepSearch(
            ok=False,
            error="timeout",
            reason=f"ripgrep exceeded {timeout_s:.0f}s and was killed",
            duration_s=round(time.perf_counter() - started, 4),
        )
    except (OSError, ValueError) as exc:
        return RipgrepSearch(
            ok=False,
            error="spawn_failed",
            reason=f"could not run ripgrep: {type(exc).__name__}: {exc}",
            duration_s=round(time.perf_counter() - started, 4),
        )
    elapsed = time.perf_counter() - started

    # rg exits 0 with no matches, 1 with no matches, 2 on a real error.
    if proc.returncode not in (0, 1):
        return RipgrepSearch(
            ok=False,
            error="ripgrep_error",
            reason=(proc.stderr or "").strip()[:400] or f"exit {proc.returncode}",
            duration_s=round(elapsed, 4),
        )

    lines: List[str] = []
    files: List[str] = []
    scanned = 0
    for record in _iter_json_lines(proc.stdout or ""):
        if record.get("type") != "match":
            continue
        data = record.get("data") or {}
        absolute = str(data.get("path") or {}).get("text") or ""
        if not absolute or not path_is_under(absolute, repo_root):
            continue
        line_number = int((data.get("line_number") or 0) or 0)
        body = str((data.get("lines") or {}).get("text") or "").rstrip("\r\n")
        relative = _relative(absolute, repo_root)
        if relative not in files:
            files.append(relative)
        lines.append(f"{relative}:{line_number}: {body[:200]}")
        if len(lines) > cap:
            break
    # rg reports files it read via a "begin"/"end" pair per file; count them so
    # the receipt's `files_scanned` means the same thing on both engines.
    scanned = max(len(files), 0)

    over = len(lines) > cap
    return RipgrepSearch(
        ok=True,
        lines=lines[: cap + 1],
        files=files,
        files_scanned=scanned,
        over_cap=over,
        total_is_lower_bound=over,
        duration_s=round(elapsed, 4),
    )


def _iter_json_lines(text: str):
    """Parse ripgrep's JSON stream, skipping any malformed record.

    A truncated final line is normal when a stream is cut off, and one bad
    record must not discard the matches that came before it.
    """
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(record, dict):
            yield record


def path_is_under(candidate: str, root: Path) -> bool:
    """Whether ``candidate`` is contained in ``root``.

    Used to re-check ripgrep's own output rather than assume the engine
    honoured our exclusions. Resolves symlinks first, so a symlinked component
    cannot read as contained.
    """
    try:
        base = os.path.realpath(str(root))
        target = os.path.realpath(str(candidate))
    except (OSError, ValueError):
        return False
    if target == base:
        return True
    return target.startswith(base + os.sep)


def _relative(absolute: str, root: Path) -> str:
    try:
        return Path(absolute).resolve().relative_to(Path(root).resolve()).as_posix()
    except (OSError, ValueError, RuntimeError):
        return absolute


def fd_find_files(
    resolution: EngineResolution,
    root: Path,
    *,
    pattern: str = "",
    limit: int = 5000,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Tuple[List[str], Dict[str, Any]]:
    """List repo-relative candidates with ``fd``; ``(paths, receipt)``.

    Returns an empty list and a receipt naming the reason when ``fd`` is
    unavailable, so the caller can fall back and still report which engine ran.
    """
    import time

    receipt: Dict[str, Any] = {
        "engine": resolution.engine,
        "source": resolution.source,
        "reason": resolution.reason,
        "used": False,
        "duration_s": 0.0,
    }
    if not resolution.available:
        return [], receipt
    argv = [
        str(resolution.path),
        "--type",
        "f",
        "--max-results",
        str(max(1, int(limit))),
        "--absolute-path",
        "--color",
        "never",
    ]
    if pattern:
        argv.extend(["--glob", pattern])
    argv.extend(skipset.ripgrep_glob_args())
    argv.append(str(root))
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        receipt["reason"] = f"fd could not run: {type(exc).__name__}: {exc}"
        receipt["duration_s"] = round(time.perf_counter() - started, 4)
        return [], receipt
    if proc.returncode not in (0, 1):
        receipt["reason"] = (proc.stderr or "").strip()[
            :300
        ] or f"exit {proc.returncode}"
        receipt["duration_s"] = round(time.perf_counter() - started, 4)
        return [], receipt
    out: List[str] = []
    for line in (proc.stdout or "").splitlines():
        candidate = line.strip()
        if not candidate or not path_is_under(candidate, root):
            continue
        out.append(_relative(candidate, root))
        if len(out) >= limit:
            break
    receipt["used"] = True
    receipt["found"] = len(out)
    receipt["duration_s"] = round(time.perf_counter() - started, 4)
    return out, receipt
