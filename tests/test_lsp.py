"""Offline regression tests for the optional LSP boundary."""

from __future__ import annotations

from harness.lsp import Diagnostic, LspManager, get_diagnostics


class FakeTransport:
    """Deterministic in-process LSP transport for lifecycle tests."""

    def __init__(self, response=None, timeout=False):
        self.response = response or {}
        self.timeout = timeout
        self.requests = []
        self.notifications = []
        self.closed = False

    def start(self):
        return None

    def request(self, method, params, timeout_s):
        self.requests.append((method, params, timeout_s))
        if self.timeout:
            raise TimeoutError("slow language server")
        if method == "initialize":
            return {"capabilities": {"diagnosticProvider": True}}
        return self.response.get(method)

    def notify(self, method, params):
        self.notifications.append((method, params))

    def close(self):
        self.closed = True


def test_lsp_lifecycle_diagnostics_hover_symbols_references(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    transport = FakeTransport(
        {
            "textDocument/diagnostic": {
                "items": [
                    {
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 5},
                        },
                        "severity": 2,
                        "message": "unused value",
                        "source": "pyright",
                    }
                ]
            },
            "textDocument/hover": {"contents": "int"},
            "textDocument/documentSymbol": [{"name": "value"}],
            "textDocument/references": [{"uri": "file:///app.py", "line": 1}],
            "shutdown": None,
        }
    )
    manager = LspManager(transport=transport, cwd=tmp_path)
    assert manager.start() is True
    assert manager.open_document(source) is True
    assert manager.update_document(source, "value = 2\n") is True
    assert any(
        method == "textDocument/didChange" for method, _ in transport.notifications
    )
    diagnostics = manager.get_diagnostics(source)
    assert diagnostics and diagnostics[0].severity == "warning"
    assert diagnostics[0].message == "unused value"
    assert manager.hover(source, 0, 0) == {"contents": "int"}
    assert manager.symbols(source) == [{"name": "value"}]
    assert manager.references(source, 0, 0)
    assert manager.shutdown() is True
    assert transport.closed is True
    assert ("initialized", {}) in transport.notifications
    assert ("exit", {}) in transport.notifications


def test_lsp_missing_and_slow_servers_degrade_without_raising(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    missing = LspManager(cwd=tmp_path)
    assert missing.start() is False
    assert missing.get_diagnostics(source) == []
    assert missing.last_error == "no LSP command configured"
    slow = LspManager(transport=FakeTransport(timeout=True), cwd=tmp_path)
    assert slow.get_diagnostics(source) == []
    assert "TimeoutError" in slow.last_error


def test_lsp_can_restart_and_malformed_diagnostics_are_safe(tmp_path):
    transport = FakeTransport({"initialize": {"capabilities": {}}})
    manager = LspManager(transport=transport, cwd=tmp_path)
    assert manager.start() is True
    manager.close()
    assert manager.start() is True
    assert manager.hover(tmp_path / "missing.py", "bad", "bad") is None
    manager.shutdown()


def test_lsp_rejects_paths_outside_workspace(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    outside = tmp_path.parent / "outside.py"
    outside.write_text("outside = True\n", encoding="utf-8")
    transport = FakeTransport({"textDocument/diagnostic": {"items": []}})
    manager = LspManager(transport=transport, cwd=tmp_path)
    assert manager.open_document(outside) is False
    assert manager.get_diagnostics(outside) == []
    assert manager.hover(outside, 0, 0) is None
    assert manager.symbols(outside) == []
    assert manager.references(outside, 0, 0) == []
    assert not any(
        method == "textDocument/diagnostic" for method, _, _ in transport.requests
    )
    assert "outside workspace" in manager.last_error


def test_lsp_explicit_empty_pull_diagnostics_clears_cached_values(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    transport = FakeTransport({"textDocument/diagnostic": {"items": []}})
    transport.notifications = [
        {
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": source.as_uri(),
                "diagnostics": [
                    {
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 1},
                        },
                        "message": "stale",
                    }
                ],
            },
        }
    ]
    manager = LspManager(transport=transport, cwd=tmp_path)
    assert manager.get_diagnostics(source) == []
    assert manager.get_diagnostics(source) == []


def test_lsp_public_exports_and_update_requires_open_document(tmp_path):
    import harness.lsp as lsp_module

    assert lsp_module.__all__ == [
        "Diagnostic",
        "LspManager",
        "diagnostics",
        "get_diagnostics",
    ]
    manager = LspManager(transport=FakeTransport(), cwd=tmp_path)
    assert manager.update_document(tmp_path / "app.py", "value = 1\n") is False
    assert manager.last_error == "document is not open in this workspace"
    manager.shutdown()


def test_diagnostic_normalization_and_module_helper():
    diagnostic = Diagnostic.from_payload(
        {
            "uri": "file:///tmp/app.py",
            "range": {
                "start": {"line": 3, "character": 2},
                "end": {"line": 3, "character": 8},
            },
            "severity": 1,
            "message": "bad",
        }
    )
    assert diagnostic.file.endswith("/tmp/app.py")
    assert diagnostic.path == diagnostic.file
    assert diagnostic.as_dict()["severity"] == "error"
    assert get_diagnostics(object()) == []
