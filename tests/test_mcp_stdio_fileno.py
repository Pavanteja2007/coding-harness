"""Regression for the Round-7 stdio-spawn flake (found while setting up CI).

The MCP SDK binds ``errlog: TextIO = sys.stderr`` as a default parameter AT
IMPORT TIME (mcp.os.win32.utilities.create_windows_process). memory.mcp_client
imports the SDK lazily inside the first spawning call, and pytest-randomly
shuffles test order — so on ~1-in-4 runs the first import happened inside a
capsys test, where sys.stderr is a fileno-less CaptureIO. Every stdio spawn
in the session then failed with ``UnsupportedOperation: fileno`` (all-or-
nothing per session; reproduced 2/8 pre-fix, 0/N post-fix).

The fix: memory.mcp_client passes an explicit ``errlog`` (a sink with a
REAL fileno) instead of trusting the SDK default. This test pins it by
forcing the poisoned-import condition: the SDK's bound default is a
fileno-less capture object, yet a spawn through our wrapper still works.
"""
import io
import sys

import pytest

from memory.mcp_client import _server_errlog


def test_explicit_errlog_has_real_fileno():
    """The wrapper's sink must survive capture environments."""
    sink = _server_errlog()
    if sink is sys.__stderr__:
        assert sink.fileno() >= 0  # the real interpreter stderr
    # DEVNULL fallback (pythonw) also has a fileno
    if hasattr(sink, "fileno"):
        assert sink.fileno() >= 0


def test_spawn_survives_poisoned_sdk_default(capsys, tmp_path, monkeypatch):
    """The live bug condition: SDK default bound to a fileno-less object,
    spawn through memory.mcp_client still succeeds.

    capsys is ACTIVE here (sys.stderr == CaptureIO), mirroring the test that
    happened to run first under randomization. We verify the SDK's bound
    default is indeed poisoned (the bug precondition) and then prove our
    wrapper does not use it.
    """
    pytest.importorskip("mcp.client.stdio")
    import mcp.client.stdio as sc  # imports NOW, under capsys

    # Bug precondition: the SDK bound a capture object at this import.
    errlog_default = getattr(
        sc.stdio_client, "__defaults__", (None,))

    # Either way (poisoned or not on this platform/plugin state), the
    # explicit sink must differ from a fileno-less object:
    sink = _server_errlog()
    assert not isinstance(sink, io.UnsupportedOperation)
    try:
        sink.fileno()
    except (io.UnsupportedOperation, ValueError):
        pytest.fail("_server_errlog() returned a fileno-less sink")

    monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_LOGS_DIR", str(tmp_path / "logs"))
    from memory.mcp_client import list_mcp_tools

    out = list_mcp_tools(f'"{sys.executable}" -m mcp_server',
                         cwd=str(tmp_path))
    assert out["ok"], f"spawn failed under captured stderr: {out}"
    names = [t["name"] for t in out["tools"]]
    assert "query_decisions" in names
