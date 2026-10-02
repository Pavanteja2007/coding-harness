"""Focused regression tests for semantic self-update release handling."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from cli import selfupdate, ui


class _Recorder:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, value: object) -> None:
        self.lines.append(str(value))


class _Response:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


@pytest.fixture(autouse=True)
def _forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make accidental network access fail the focused test immediately."""

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("self-update tests must not access the network")

    monkeypatch.setattr(selfupdate.urllib.request, "urlopen", blocked)


def _run_check(
    monkeypatch: pytest.MonkeyPatch,
    current: str,
    latest: Optional[str],
) -> tuple[int, str, str]:
    """Run the check command with deterministic versions and output."""
    output = _Recorder()
    errors = _Recorder()
    monkeypatch.setattr(selfupdate, "installed_version", lambda: current)
    monkeypatch.setattr(
        selfupdate, "latest_available_version", lambda timeout_s=5.0: latest
    )
    monkeypatch.setattr(selfupdate, "detect_install_method", lambda: ("pip", "pip"))
    monkeypatch.setattr(ui, "console", lambda: output)
    monkeypatch.setattr(ui, "err_console", lambda: errors)
    result = selfupdate.cmd_update(SimpleNamespace(check=True))
    return result, "\n".join(output.lines), "\n".join(errors.lines)


def _github_tags(monkeypatch: pytest.MonkeyPatch, tags: list[dict[str, Any]]) -> None:
    """Install a JSON response fake for the GitHub fallback."""
    response = _Response(tags)

    def urlopen(request: Any, timeout: float) -> _Response:
        return response

    monkeypatch.setattr(selfupdate.urllib.request, "urlopen", urlopen)


def _pypi_version(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    """Install a JSON response fake for the PyPI release source."""
    response = _Response({"info": {"version": version}})

    def urlopen(request: Any, timeout: float) -> _Response:
        return response

    monkeypatch.setattr(selfupdate.urllib.request, "urlopen", urlopen)


def test_zero_ten_is_newer_than_zero_nine(monkeypatch: pytest.MonkeyPatch) -> None:
    """PEP 440 compares 0.10 above 0.9 and reports an available update."""
    result, output, errors = _run_check(monkeypatch, "0.9", "0.10")
    assert result == 0
    assert "update available" in output
    assert errors == ""


def test_zero_ten_equals_zero_ten_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Equivalent padded versions are equal rather than string-different."""
    result, output, errors = _run_check(monkeypatch, "0.10", "0.10.0")
    assert result == 0
    assert "up to date" in output
    assert "update available" not in output
    assert errors == ""


def test_newer_installed_version_never_asks_for_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A locally newer release is reported as up to date, not downgraded."""
    result, output, errors = _run_check(monkeypatch, "1.0.0", "0.9.0")
    assert result == 0
    assert "up to date" in output
    assert "newer" in output
    assert "update available" not in output
    assert errors == ""


def test_newer_remote_version_reports_update(monkeypatch: pytest.MonkeyPatch) -> None:
    """A semantically newer remote release reports an update."""
    result, output, errors = _run_check(monkeypatch, "0.9.0", "0.10.0")
    assert result == 0
    assert "update available" in output
    assert errors == ""


def test_invalid_installed_version_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed local metadata cannot be treated as an update prompt."""
    result, output, errors = _run_check(monkeypatch, "not-a-version", "0.10.0")
    assert result != 0
    assert "invalid installed version" in errors
    assert "update available" not in output


def test_invalid_remote_version_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed remote metadata cannot be accepted as a release."""
    result, output, errors = _run_check(monkeypatch, "0.10.0", "not-a-version")
    assert result != 0
    assert "invalid latest release version" in errors
    assert "up to date" not in output
    assert "update available" not in output


def test_github_sorts_all_returned_tags_semantically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub fallback maxes every returned tag instead of trusting order."""
    tags = [
        {"name": "v0.9.0"},
        {"name": "v0.10.0"},
        {"name": "v0.8.0"},
        {"name": "v0.7.0"},
        {"name": "v0.6.0"},
        {"name": "v0.5.0"},
        {"name": "v0.4.0"},
        {"name": "v0.3.0"},
        {"name": "v0.2.0"},
        {"name": "v0.1.0"},
        {"name": "v0.11.0"},
        {"name": "not-a-version"},
    ]
    _github_tags(monkeypatch, tags)
    assert selfupdate._latest_from_github(0.1) == "0.11.0"


def test_github_prefers_final_over_prerelease(monkeypatch: pytest.MonkeyPatch) -> None:
    """A higher-base prerelease does not displace the latest stable tag."""
    tags = [{"name": "v2.0.0rc1"}, {"name": "v1.9.0"}]
    _github_tags(monkeypatch, tags)
    assert selfupdate._latest_from_github(0.1) == "1.9.0"


def test_github_filters_prereleases_when_no_stable_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stable-tag fallback returns no version when every tag is prerelease."""
    tags = [{"name": "v2.0.0rc1"}, {"name": "v1.9.0b1"}]
    _github_tags(monkeypatch, tags)
    assert selfupdate._latest_from_github(0.1) is None


def test_github_ignores_invalid_tags_and_accepts_one_v_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only one v/V prefix is stripped and malformed tags are ignored."""
    tags = [
        {"name": "vv0.12.0"},
        {"name": "V0.10.0"},
        {"name": "v0.9.0"},
        {"name": "not-a-version"},
        {"name": None},
    ]
    _github_tags(monkeypatch, tags)
    assert selfupdate._latest_from_github(0.1) == "0.10.0"


def test_latest_available_validates_and_canonicalizes_pypi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid PyPI value is canonicalized and prevents the fallback call."""
    calls: list[float] = []
    monkeypatch.setattr(
        selfupdate,
        "_latest_from_pypi",
        lambda timeout: "v0.10.0",
    )

    def github(timeout: float) -> str:
        calls.append(timeout)
        return "9.0.0"

    monkeypatch.setattr(selfupdate, "_latest_from_github", github)
    assert selfupdate.latest_available_version(0.2) == "0.10.0"
    assert calls == []


def test_latest_available_falls_back_when_pypi_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed PyPI metadata is not returned, but a valid fallback is used."""
    monkeypatch.setattr(
        selfupdate,
        "_latest_from_pypi",
        lambda timeout: "not-a-version",
    )
    monkeypatch.setattr(
        selfupdate,
        "_latest_from_github",
        lambda timeout: "v0.10.0",
    )
    assert selfupdate.latest_available_version(0.2) == "0.10.0"


def test_latest_available_rejects_malformed_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed GitHub fallback data is not exposed as a release version."""
    monkeypatch.setattr(selfupdate, "_latest_from_pypi", lambda timeout: None)
    monkeypatch.setattr(
        selfupdate,
        "_latest_from_github",
        lambda timeout: "not-a-version",
    )
    assert selfupdate.latest_available_version(0.2) is None


def test_pypi_prerelease_is_not_offered_as_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prerelease on PyPI falls back instead of becoming the stable update."""
    _pypi_version(monkeypatch, "2.0.0rc1")
    monkeypatch.setattr(selfupdate, "_latest_from_github", lambda timeout: "1.9.0")

    assert selfupdate.latest_available_version(0.2) == "1.9.0"


def test_github_paginates_before_choosing_semantic_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stable tag on a later page is not hidden by a full prerelease first page."""
    first = [{"name": f"v3.0.0rc{index}"} for index in range(100)]
    second = [{"name": "v0.10.0"}]
    requested: list[str] = []

    def urlopen(request: Any, timeout: float) -> _Response:
        requested.append(request.full_url)
        return _Response(first if request.full_url.endswith("page=1") else second)

    monkeypatch.setattr(selfupdate.urllib.request, "urlopen", urlopen)

    assert selfupdate._latest_from_github(0.1) == "0.10.0"
    assert len(requested) == 2
    assert all("per_page=100" in url for url in requested)


def test_update_never_claims_a_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful package command that lowers the version is still a failure."""
    output = _Recorder()
    errors = _Recorder()
    versions = iter(("1.0.0", "0.9.0"))
    monkeypatch.setattr(selfupdate, "detect_install_method", lambda: ("pip", "pip"))
    monkeypatch.setattr(selfupdate, "installed_version", lambda: next(versions))
    monkeypatch.setattr(selfupdate, "_run", lambda command, **kwargs: 0)
    monkeypatch.setattr(ui, "console", lambda: output)
    monkeypatch.setattr(ui, "err_console", lambda: errors)

    result = selfupdate.cmd_update(SimpleNamespace(check=False))

    assert result == 1
    assert "selected a downgrade" in "\n".join(errors.lines)
    assert "updated to" not in "\n".join(output.lines)


def test_update_reports_equal_version_without_false_upgrade_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unchanged post-update version is reported truthfully as already current."""
    output = _Recorder()
    errors = _Recorder()
    versions = iter(("0.10.0", "0.10.0"))
    monkeypatch.setattr(selfupdate, "detect_install_method", lambda: ("pip", "pip"))
    monkeypatch.setattr(selfupdate, "installed_version", lambda: next(versions))
    monkeypatch.setattr(selfupdate, "_run", lambda command, **kwargs: 0)
    monkeypatch.setattr(ui, "console", lambda: output)
    monkeypatch.setattr(ui, "err_console", lambda: errors)

    result = selfupdate.cmd_update(SimpleNamespace(check=False))

    assert result == 0
    assert "already at 0.10.0" in "\n".join(output.lines)
    assert errors.lines == []
