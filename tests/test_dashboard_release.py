import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from dashboard.collect import aggregate, scan_logs
from dashboard.server import _Handler, _render_html, _State


def _write_state(root: Path, task_id: str, payload) -> Path:
    task_dir = root / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    state_file = task_dir / "state.json"
    if isinstance(payload, str):
        state_file.write_text(payload, encoding="utf-8")
    else:
        state_file.write_text(json.dumps(payload), encoding="utf-8")
    return state_file


def _write_successful_task(root: Path, task_id: str) -> Path:
    state_file = _write_state(
        root,
        task_id,
        {
            "task_id": task_id,
            "plan": ["fix", "verify"],
            "completed_steps": ["fix", "verify"],
            "files_touched": ["mod.py"],
            "decisions": ["kept the patch focused"],
            "remaining_plan": [],
        },
    )
    task_dir = state_file.parent
    rows = [
        {"ts": 10.0, "kind": "task_start", "data": {"issue_text": "broken"}},
        {
            "ts": 20.0,
            "kind": "result",
            "data": {"status": "success", "attempts": 1, "cost_usd": 0.25},
        },
    ]
    (task_dir / "trace.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows),
        encoding="utf-8",
    )
    return state_file


def _request(url: str, method: str):
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_rendered_html_has_no_uninterpolated_placeholders():
    page = _render_html(2.5)
    assert "%STATUS_COLORS%" not in page
    assert "%REFRESH_S%" not in page
    assert "const STATUS_COLORS = {" in page
    assert '"success": "#16a34a"' in page
    assert "auto-refresh 2.5s" in page
    assert "setInterval(poll, 2500)" in page


def test_server_interpolates_page_and_remains_get_only(tmp_path):
    root = tmp_path / "logs"
    root.mkdir()
    state = _State(str(root), refresh_s=1.25)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        status, page = _request(url, "GET")
        assert status == 200
        assert b"%STATUS_COLORS%" not in page
        assert b"%REFRESH_S%" not in page
        assert b"setInterval(poll, 1250)" in page
        assert _request(url, "POST")[0] == 405
        assert _request(url, "PUT")[0] == 405
        assert _request(url, "DELETE")[0] == 405
        assert _request(url, "GET")[0] == 200
        assert list(root.iterdir()) == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_malformed_task_becomes_placeholder_without_hiding_valid_task(tmp_path):
    root = tmp_path / "logs"
    root.mkdir()
    _write_successful_task(root, "valid")
    _write_state(root, "malformed", ["not", "an", "object"])

    tasks = scan_logs(str(root))
    by_id = {task["task_id"]: task for task in tasks}
    assert set(by_id) == {"valid", "malformed"}
    assert by_id["valid"]["status"] == "success"
    assert by_id["valid"]["cost_usd"] == pytest.approx(0.25)
    assert by_id["malformed"]["status"] == "?"
    assert by_id["malformed"]["cost_usd"] == 0.0
    assert by_id["malformed"]["plan_steps"] == 0

    state = _State(str(root), refresh_s=3600)
    assert aggregate(state.tasks)["total"] == 2
    assert aggregate(state.tasks)["counts"]["?"] == 1


def test_dashboard_scan_skips_state_symlink_escape(tmp_path):
    root = tmp_path / "logs"
    root.mkdir()
    _write_successful_task(root, "valid")
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = "OUTSIDE-DASHBOARD-CANARY"
    (outside / "state.json").write_text(
        json.dumps({"task_id": "outside", "decisions": [canary]}),
        encoding="utf-8",
    )
    escape = root / "escape"
    escape.mkdir()
    try:
        (escape / "state.json").symlink_to(outside / "state.json")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"state-file symlinks unavailable: {exc}")

    tasks = scan_logs(str(root))
    assert [task["task_id"] for task in tasks] == ["valid"]
    assert all(canary not in json.dumps(task) for task in tasks)
