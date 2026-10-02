"""Sanitized, committed release evidence bundles with a retention/purge path.

``logs/`` is gitignored, which is correct â€” it holds prompts, paths, and
provider output. But it also means a quality claim backed only by a log
line is unreviewable. This module closes that gap: it distills the real
artifacts of a run into a small, sanitized, *committed* bundle that a
reviewer can read and a machine can re-verify.

What a bundle contains:

    manifest.json        schema, label, source digests, per-file sha256
    slo_report.json      measured SLOs (see :mod:`evals.slos`)
    ci_truth.json        the CI-truth verdict
    quality/             prompt-regression + daily-driver verdicts
    spans/<task>.json    GenAI span lifecycle per task, shareable view
    spans/otlp.json      OTLP/JSON projection of the same spans

What it must never contain (checked by :func:`verify_bundle`):

    * any secret pattern (``shared.security.contains_secret``)
    * an absolute path, a drive letter, or a user home directory
    * prompt, answer, or model-output content
    * a file whose sha256 no longer matches the manifest

Commands:
    python -m evals.evidence build  --logs-root logs --out evidence/bundles
    python -m evals.evidence verify evidence/bundles
    python -m evals.evidence purge  --root evidence/bundles --keep-latest 5
    python -m evals.evidence list   --root evidence/bundles
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from evals import ci_truth
from evals.slos import evidence_summary
from shared.privacy import apply_privacy_mode
from shared.retention import apply_retention
from shared.security import (
    REDACTED_SECRET,
    contains_secret,
    is_sensitive_key,
    redact_secrets,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

BUNDLE_SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"

#: Keys whose values are model output, prompts, or user content. A bundle
#: is built from the ``shareable`` privacy view; this list is the deep-drop
#: set applied by the bundle sanitizer and re-checked structurally by
#: ``verify_bundle``. A committed bundle carries measurements, not prose.
_DROP_KEYS = frozenset(
    {
        "prompt",
        "prompts",
        "answer",
        "answers",
        "completion",
        "messages",
        "issue_text",
        "system_prompt",
        "user_content",
        "command",
        "commands",
        "stdout",
        "stderr",
        "output",
        "diff",
        "patch",
        "reasoning",
        "rationale",
        "raw",
        "body",
        "cwd",
        "path",
        "repo_path",
        "repo",
        "root",
        "workdir",
        "artifact_dir",
        "trace_path",
        "file",
        "files",
        "source",
        "tool_feedback",
        "tool_output",
        "traceback",
        "excerpt",
        "snippet",
        "preview",
        "text",
        "content",
        "reproducer",
    }
)

#: Free-text ceiling. A committed bundle keeps numbers, statuses, digests,
#: counts, and short labels; anything longer is model transcript, a
#: reproduction command, or an explanation the user wrote, none of which is
#: evidence and all of which may quote source or a prompt.
MAX_VALUE_CHARS = 300

#: Absolute path shapes that must never survive into a committed bundle.
_PATH_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"[A-Za-z]:[\\/]{1,2}[A-Za-z0-9_]"),
    re.compile(r"/(?:home|Users|root|var|private|tmp)/"),
    re.compile(r"\\\\[A-Za-z0-9_.-]+\\"),
)

#: Report basenames copied into ``quality/`` when present.
QUALITY_REPORTS: Tuple[str, ...] = (
    "eval_report.json",
    "daily_driver_report.json",
    "live_quality_report.json",
    "mined_failures.json",
)


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------


def _looks_like_path(value: str) -> bool:
    return any(pattern.search(value) for pattern in _PATH_PATTERNS)


def _scrub(value: Any) -> Any:
    """Recursively drop content/path keys and rewrite path-shaped strings.

    ``shared.privacy``'s shareable view removes a fixed key set; a real eval
    report nests arbitrary user content and host paths far below it, so the
    bundle sanitizer walks the whole document instead of trusting one level
    of key matching. Numbers, booleans, statuses, digests, and counts are
    preserved — that is the evidence.
    """
    if isinstance(value, Mapping):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name.casefold() in _DROP_KEYS:
                continue
            cleaned[name] = _scrub(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value]
    if isinstance(value, str):
        if _looks_like_path(value):
            return portable_label(Path(value))
        if len(value) > MAX_VALUE_CHARS:
            return f"[redacted free text: {len(value)} chars]"
        return value
    return value


def _sanitize(value: Any) -> Any:
    """Deep bundle sanitizer: privacy view, key dropping, path scrub, redaction."""
    return redact_secrets(_scrub(apply_privacy_mode(value, "shareable")))


def sanitize_document(document: Any) -> Any:
    """Public sanitizer: redact secrets, drop content, strip identities."""
    return _sanitize(document)


def portable_label(path: Path, segments: int = 2) -> str:
    """A redacted, portable label for a host path â€” never raises.

    ``shared.security.safe_relative_path`` deliberately rejects absolute
    paths, which is right for a containment check but wrong for an
    evidence label. A bundle records only the last few path segments so a
    reviewer learns the shape of the tree without learning the operator's
    home directory or drive layout.
    """
    try:
        parts = [
            part for part in Path(path).resolve().parts if part not in ("", ".", "\\")
        ]
    except (OSError, RuntimeError, ValueError):
        parts = []
    if not parts:
        return "<path>"
    return "/".join(parts[-segments:])


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, payload: Any) -> str:
    data = json.dumps(_sanitize(payload), indent=2, sort_keys=True, default=str) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")
    return _sha256_bytes(data.encode("utf-8"))


# ---------------------------------------------------------------------------
# Bundle construction
# ---------------------------------------------------------------------------


def _newest(root: Path, name: str) -> Optional[Path]:
    candidates = sorted(
        (path for path in root.rglob(name) if path.is_file() and not path.is_symlink()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _task_ids(root: Path) -> List[str]:
    if not root.is_dir():
        return []
    return sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir()
        and not entry.is_symlink()
        and not entry.name.startswith(("_", "."))
    )


def _span_documents(task_id: str, logs_root: Path) -> List[Dict[str, Any]]:
    """GenAI span lifecycle for one task, in the shareable privacy view."""
    from shared.traceview import reconstruct_spans, span_lifecycle

    try:
        spans = reconstruct_spans(
            task_id, logs_root=logs_root, privacy="shareable", include_derived=True
        )
    except Exception:
        return []
    lifecycle = span_lifecycle(spans)
    return [
        {
            "task_id": task_id,
            "lifecycle": lifecycle,
            "spans": spans,
            "summary": {
                "span_count": lifecycle.get("n_spans"),
                "explicit_span_count": lifecycle.get("explicit_span_count"),
                "derived_span_count": lifecycle.get("derived_span_count"),
                "by_kind": lifecycle.get("by_kind"),
                "reconstructable": lifecycle.get("reconstructable"),
            },
        }
    ]


def build_bundle(
    logs_root: Path,
    out_root: Path,
    *,
    label: str = "release",
    include_spans: bool = True,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Distill real artifacts from ``logs_root`` into one sanitized bundle.

    The bundle is written to ``out_root/<label>-<timestamp>/`` and a
    manifest describing every file is written alongside. The source
    artifacts are only read; nothing under ``logs_root`` is modified.
    """
    logs = Path(logs_root)
    stamp = time.strftime(
        "%Y%m%d-%H%M%S", time.gmtime(now if now is not None else time.time())
    )
    bundle = Path(out_root) / f"{label}-{stamp}"
    bundle.mkdir(parents=True, exist_ok=True)

    files: Dict[str, str] = {}
    errors: List[Dict[str, str]] = []

    files["slo_report.json"] = _write_json(
        bundle / "slo_report.json", evidence_summary(logs)
    )
    files["ci_truth.json"] = _write_json(
        bundle / "ci_truth.json", ci_truth.ci_truth_report(REPO_ROOT)
    )

    quality: List[str] = []
    for name in QUALITY_REPORTS:
        source = _newest(logs, name)
        if source is None:
            continue
        try:
            document = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append(
                {"code": "quality_report_unreadable", "message": f"{name}: {exc}"}
            )
            continue
        relative = f"quality/{name}"
        try:
            files[relative] = _write_json(bundle / relative, document)
            quality.append(relative)
        except OSError as exc:
            errors.append(
                {"code": "quality_report_unwritable", "message": f"{name}: {exc}"}
            )

    spans_written: List[str] = []
    if include_spans:
        from shared.otel import to_otlp

        collected: List[Dict[str, Any]] = []
        for task_id in _task_ids(logs):
            documents = _span_documents(task_id, logs)
            if not documents:
                continue
            collected.extend(documents)
            relative = f"spans/{task_id}.json"
            try:
                files[relative] = _write_json(bundle / relative, documents[0])
                spans_written.append(relative)
            except OSError as exc:
                errors.append(
                    {"code": "span_unwritable", "message": f"{task_id}: {exc}"}
                )
        if collected:
            try:
                files["spans/otlp.json"] = _write_json(
                    bundle / "spans" / "otlp.json",
                    to_otlp(
                        [
                            span
                            for document in collected
                            for span in document.get("spans", [])
                        ],
                        privacy="shareable",
                    ),
                )
            except OSError as exc:
                errors.append({"code": "otlp_unwritable", "message": str(exc)})

    manifest = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "label": label,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "privacy": "shareable",
        "source_logs_root": portable_label(logs),
        "entries": dict(sorted(files.items())),
        "quality_reports": quality,
        "span_documents": spans_written,
        "errors": errors,
    }
    _write_json(bundle / MANIFEST_NAME, manifest)
    return {
        "bundle_path": str(bundle),
        "manifest": manifest,
        "n_files": len(files) + 1,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Bundle verification
# ---------------------------------------------------------------------------


def _walk_values(
    value: Any, keys: Optional[List[Tuple[str, Any]]] = None
) -> List[Tuple[str, Any]]:
    """Flatten a JSON document into ``(key, value)`` pairs, descending objects."""
    pairs: List[Tuple[str, Any]] = [] if keys is None else keys
    if isinstance(value, Mapping):
        for key, item in value.items():
            pairs.append((str(key), item))
            _walk_values(item, pairs)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk_values(item, pairs)
    return pairs


def _violations(text: str) -> List[str]:
    """Structural leak scan of one bundle file.

    The scan runs on the *parsed* document, not the raw text: a raw-text scan
    cannot tell a Windows path from a JSON escape sequence (``k:\\nTOOL``
    looks like ``C:/x``), and it cannot tell a content KEY from a content
    VALUE. Both mistakes produce false alarms that train reviewers to ignore
    the gate, so the check is structural.
    """
    try:
        document = json.loads(text)
    except ValueError:
        return ["file_unparseable"]
    found: List[str] = []
    for key, value in _walk_values(document):
        name = key.casefold()
        if name in _DROP_KEYS:
            found.append(f"content_key_survived:{key}")
        # A sensitive KEY with a redacted placeholder is the correct result of
        # redaction: the structure stays reviewable, the value is gone. Only an
        # un-redacted value under a sensitive key is a leak.
        if is_sensitive_key(key) and not (
            value in (REDACTED_SECRET, None, "", [], {})
            or (isinstance(value, str) and not value.strip())
        ):
            found.append(f"secret_value_survived:{key}")
        elif isinstance(value, str):
            if contains_secret(value):
                # An unredacted token under an innocuous key is still a leak.
                found.append(f"secret_value_survived:{key}")
            if _looks_like_path(value):
                found.append(f"absolute_path_survived:{key}")
            if len(value) > MAX_VALUE_CHARS:
                found.append(f"free_text_survived:{key}")
    return found


def verify_bundle(path: Path) -> Dict[str, Any]:
    """Re-verify a bundle from disk: digests, secrets, paths, and content.

    A bundle that fails verification is not evidence. This reads the
    bundle fresh â€” it does not trust any in-memory result from ``build``.
    """
    root = Path(path)
    manifest_path = root if root.is_file() else root / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return {
            "ok": False,
            "bundle_path": str(root),
            "errors": [
                {"code": "manifest_missing", "message": f"no {MANIFEST_NAME} in {root}"}
            ],
            "checked_files": 0,
        }
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {
            "ok": False,
            "bundle_path": str(root),
            "errors": [{"code": "manifest_unreadable", "message": str(exc)}],
            "checked_files": 0,
        }
    base = manifest_path.parent
    errors: List[Dict[str, str]] = []
    checked = 0
    # `entries` (not `files`) names the manifest index: `files` is a dropped
    # content key, so using it here would sanitize the index away.
    entries = manifest.get("entries")
    entries = entries if isinstance(entries, Mapping) else {}
    for relative, expected in sorted(entries.items()):
        target = base / str(relative)
        if target.is_symlink() or not target.is_file():
            errors.append({"code": "file_missing", "message": str(relative)})
            continue
        raw = target.read_text(encoding="utf-8", errors="replace")
        digest = _sha256_bytes(raw.encode("utf-8"))
        if digest != str(expected):
            errors.append(
                {
                    "code": "digest_mismatch",
                    "message": f"{relative}: {digest} != {expected}",
                }
            )
        for violation in _violations(raw):
            errors.append({"code": violation, "message": str(relative)})
        checked += 1
    if not entries:
        errors.append(
            {"code": "manifest_empty", "message": "bundle contains no evidence files"}
        )
    for key in ("schema_version", "label", "created_at", "privacy"):
        if key not in manifest:
            errors.append({"code": "manifest_field_missing", "message": key})
    if manifest.get("privacy") != "shareable":
        errors.append(
            {
                "code": "manifest_privacy",
                "message": "bundle was not built in shareable mode",
            }
        )
    return {
        "ok": not errors,
        "bundle_path": str(base),
        "label": str(manifest.get("label") or ""),
        "created_at": str(manifest.get("created_at") or ""),
        "checked_files": checked,
        "errors": errors,
    }


def verify_tree(root: Path) -> Dict[str, Any]:
    """Verify every bundle under ``root``; one bad bundle fails the tree."""
    results = [verify_bundle(bundle) for bundle in _bundle_dirs(Path(root))]
    return {
        "ok": bool(results) and all(item["ok"] for item in results),
        "root": str(root),
        "n_bundles": len(results),
        "bundles": results,
        "errors": [error for item in results for error in item["errors"]],
    }


# ---------------------------------------------------------------------------
# Retention / purge
# ---------------------------------------------------------------------------


def _bundle_dirs(root: Path) -> List[Path]:
    if not root.is_dir():
        return []
    return sorted(
        path.parent
        for path in root.glob(f"*/{MANIFEST_NAME}")
        if path.is_file() and not path.is_symlink() and not path.parent.is_symlink()
    )


def _prune_empty_dirs(bundle: Path) -> List[Dict[str, str]]:
    """Remove emptied subdirectories then the bundle dir; report what stuck.

    A bundle is nested (``spans/``, ``quality/``), so removing the files is
    only half the deletion. Anything left behind is reported rather than
    ignored, so a purge never claims more than it removed.
    """
    errors: List[Dict[str, str]] = []
    for current, dirnames, filenames in os.walk(bundle, topdown=False):
        if filenames or (current == str(bundle) and dirnames):
            continue
        try:
            os.rmdir(current)
        except OSError:
            if current != str(bundle):
                errors.append(
                    {
                        "code": "dir_not_removed",
                        "message": f"{Path(current).name}: {len(filenames)} file(s) remain",
                    }
                )
    return errors


def purge(
    root: Path,
    *,
    keep_latest: Optional[int] = None,
    max_age_s: Optional[float] = None,
    dry_run: bool = False,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Delete expired evidence bundles under ``root``.

    Selection is bundle-level (a bundle is the unit a reviewer reads, not
    a file), and the actual deletion of each selected bundle's files is
    delegated to ``shared.retention.apply_retention`` so bundle purging
    keeps the project's symlink refusal, containment checks, and redacted
    deletion receipt instead of forking a second deleter.
    """
    base = Path(root)
    bundles = _bundle_dirs(base)
    timestamp = now if now is not None else time.time()
    ordered = sorted(bundles, key=lambda path: (path.stat().st_mtime, str(path)))
    keep = max(0, int(keep_latest)) if keep_latest is not None else 0
    protected = set(ordered[-keep:]) if keep else set()
    if max_age_s is not None:
        cutoff = timestamp - float(max_age_s)
        selected = [
            path
            for path in ordered
            if path.stat().st_mtime < cutoff and path not in protected
        ]
    elif keep_latest is not None:
        # "keep the newest N" means everything else is expired by definition.
        selected = [path for path in ordered if path not in protected]
    else:
        selected = list(ordered)
    errors: List[Dict[str, str]] = []
    deleted: List[str] = []
    bytes_deleted = 0
    for path in selected:
        try:
            # The per-bundle file deletion reuses the shared retention policy
            # (containment, symlink refusal, byte accounting). Its own
            # receipt is suppressed: this function writes one bundle-level
            # receipt so a purge leaves a single reviewable record.
            report = apply_retention(
                path, max_bytes=0, keep_latest=0, dry_run=dry_run, write_receipt=False
            )
            deleted.append(portable_label(path, segments=1))
            bytes_deleted += int(getattr(report, "bytes_deleted", 0) or 0)
            if not dry_run:
                errors.extend(_prune_empty_dirs(path))
        except Exception as exc:
            errors.append(
                {
                    "code": "purge_failed",
                    "message": f"{path.name}: {type(exc).__name__}",
                }
            )
    receipt = {
        "schema_version": 1,
        "action": "purge_dry_run" if dry_run else "purge",
        "root": portable_label(base),
        "keep_latest": keep_latest,
        "max_age_s": max_age_s,
        "n_bundles": len(bundles),
        "n_selected": len(selected),
        "deleted": deleted,
        "bytes_deleted": bytes_deleted,
        "errors": errors,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp)),
    }
    if not dry_run:
        receipt_path = base / "purge_receipts.jsonl"
        try:
            base.mkdir(parents=True, exist_ok=True)
            with open(receipt_path, "a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(redact_secrets(receipt), sort_keys=True, default=str)
                    + "\n"
                )
            receipt["receipt"] = portable_label(receipt_path, segments=1)
        except OSError as exc:
            errors.append({"code": "receipt_unwritable", "message": type(exc).__name__})
    return {**receipt, "dry_run": bool(dry_run)}


def list_bundles(root: Path) -> Dict[str, Any]:
    """Every bundle under ``root`` with its label, age, and verification."""
    base = Path(root)
    rows: List[Dict[str, Any]] = []
    if base.is_dir():
        for manifest_path in sorted(base.glob(f"*/{MANIFEST_NAME}")):
            if manifest_path.is_symlink() or not manifest_path.is_file():
                continue
            verification = verify_bundle(manifest_path.parent)
            rows.append(
                {
                    "bundle_path": str(manifest_path.parent),
                    "label": verification.get("label"),
                    "created_at": verification.get("created_at"),
                    "checked_files": verification.get("checked_files"),
                    "ok": verification.get("ok"),
                }
            )
    return {"root": str(base), "n_bundles": len(rows), "bundles": rows}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    """Build, verify, list, or purge evidence bundles. Exit 2 on failure."""
    parser = argparse.ArgumentParser(
        prog="python -m evals.evidence",
        description="Sanitized, committed release evidence bundles.",
    )
    parser.add_argument(
        "command", choices=("build", "verify", "list", "purge"), help="operation"
    )
    parser.add_argument("target", nargs="?", default=None, help="bundle or root path")
    parser.add_argument("--logs-root", default=str(REPO_ROOT / "logs"))
    parser.add_argument("--out", default=None, help="bundle output root (build)")
    parser.add_argument("--label", default="release", help="bundle label (build)")
    parser.add_argument("--root", default=None, help="root for verify/list/purge")
    parser.add_argument(
        "--keep-latest", type=int, default=None, help="purge: keep N newest"
    )
    parser.add_argument(
        "--max-age-s", type=float, default=None, help="purge: max age seconds"
    )
    parser.add_argument("--dry-run", action="store_true", help="purge: never delete")
    parser.add_argument(
        "--json", action="store_true", help="machine-readable output only"
    )
    args = parser.parse_args(argv)

    if args.command == "build":
        out_root = Path(args.out) if args.out else (REPO_ROOT / "evidence" / "bundles")
        report = build_bundle(Path(args.logs_root), out_root, label=args.label)
        verification = verify_bundle(Path(report["bundle_path"]))
        payload = {**report, "verification": verification}
        status = 0 if verification["ok"] else 2
    elif args.command == "verify":
        base = args.root or args.target or str(REPO_ROOT / "evidence" / "bundles")
        payload = verify_tree(Path(base))
        status = 0 if payload["ok"] else 2
    elif args.command == "list":
        base = args.root or args.target or str(REPO_ROOT / "evidence" / "bundles")
        payload = list_bundles(Path(base))
        status = 0
    else:
        base = args.root or args.target or str(REPO_ROOT / "evidence" / "bundles")
        payload = purge(
            Path(base),
            keep_latest=args.keep_latest,
            max_age_s=args.max_age_s,
            dry_run=args.dry_run,
        )
        status = 0

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        if args.command == "build":
            print(f"bundle: {payload['bundle_path']} ({payload['n_files']} files)")
            print(f"verification ok: {payload['verification']['ok']}")
            for error in payload["verification"]["errors"]:
                print(f"  {error['code']}: {error['message']}")
        elif args.command == "verify":
            print(f"bundles: {payload['n_bundles']} ok={payload['ok']}")
            for error in payload["errors"]:
                print(f"  {error['code']}: {error['message']}")
        elif args.command == "list":
            for row in payload["bundles"]:
                print(
                    f"  {'ok ' if row['ok'] else 'BAD'} {row['label']} {row['bundle_path']}"
                )
        else:
            print(f"purge {payload['root']} dry_run={payload['dry_run']}")
            for deleted in payload["deleted"]:
                print(f"  deleted {deleted}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
