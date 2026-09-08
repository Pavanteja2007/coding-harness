"""Tests for dashboard/ — the read-only visualization layer (stretch
item 40). Collector: correct summaries from the REAL log layouts
(Boundary-4 state.json/trace.jsonl + Terminal 3's .runtime sibling),
nested ablation layouts included. Server: GET-only, read-only, live
JSON API shape."""
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from dashboard.collect import aggregate, group_by_run, scan_logs
from dashboard.server import _Handler, _State, _render_html


@pytest.fixture
def logs_root(tmp_path: Path) -> Path:
    root = tmp_path / "logs"
    root.mkdir()
    return root


def _write_task(
    logs_root: Path,
    task_id: str,
    status: str = "success",
    cost: float = 0.01,
    models=None,
    hints=None,
    attempts: int = 1,
    issue: str = "something is broken",
    no_trace: bool = False,
    runtime_result=None,
) -> Path:
    """Lay down one task dir in the REAL combined format: state.json +
    trace.jsonl + sibling {task_id}.runtime/{model_ledger.jsonl,
    checkpoint.json}."""
    d = logs_root / task_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(
        json.dumps({
            "task_id": task_id,
            "plan": ["1. fix", "2. verify"],
            "completed_steps": ["1. fix", "2. verify"] if status == "success" else [],
            "files_touched": ["src/a.py"],
            "decisions": ["kept tests as-is"],
            "remaining_plan": [],
        }),
        encoding="utf-8",
    )
    if not no_trace:
        lines = [
            {"ts": 1000.0, "kind": "task_start", "data": {
                "task_id": task_id, "issue_text": issue, "config": {}}},
            {"ts": 1010.5, "kind": "task_end", "data": {"status": status,
                                                       "attempt": attempts}},
            {"ts": 1010.5, "kind": "result", "data": {
                "status": status, "attempts": attempts, "cost_usd": cost}},
        ]
        (d / "trace.jsonl").write_text(
            "\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    rt = logs_root / f"{task_id}.runtime"
    rt.mkdir(parents=True, exist_ok=True)
    ledger = [
        {"model": m, "difficulty_hint": h, "cost_usd": c}
        for (m, h), c in zip(models or [], hints or [])
    ] or [
        {"model": "m", "provider": "openai", "tokens": 10, "cost_usd": 0.0,
         "elapsed_s": 1.0, "routed_via_hint": "easy", "difficulty_hint": "easy"},
    ]
    (rt / "model_ledger.jsonl").write_text(
        "\n".join(json.dumps(r) for r in ledger), encoding="utf-8")
    (rt / "checkpoint.json").write_text(
        json.dumps({"task_id": task_id, "attempt": attempts - 1,
                    "result": runtime_result or {
                        "task_id": task_id, "status": status,
                        "cost_usd": cost}}),
        encoding="utf-8",
    )
    return d


# --------------------------------------------------------------------------
# collector
# --------------------------------------------------------------------------

def test_scan_top_level_and_nested_tasks(logs_root):
    _write_task(logs_root, "adhoc-1", cost=0.01)
    nested = logs_root / "ablations" / "v3-run" / "tasklogs"
    nested.mkdir(parents=True)
    _write_task(nested, "abl-on-bug01", cost=0.02)

    tasks = scan_logs(str(logs_root))
    by_id = {t["task_id"]: t for t in tasks}
    assert set(by_id) == {"adhoc-1", "abl-on-bug01"}
    assert by_id["adhoc-1"]["run"] == "adhoc"
    assert by_id["abl-on-bug01"]["run"] == "ablations/v3-run"
    assert by_id["adhoc-1"]["status"] == "success"
    assert by_id["adhoc-1"]["cost_usd"] == pytest.approx(0.01)
    assert by_id["adhoc-1"]["attempts"] == 1
    assert by_id["adhoc-1"]["elapsed_s"] == pytest.approx(10.5)
    assert by_id["adhoc-1"]["issue"] == "something is broken"


def test_scan_reads_ledger_models_hints_cost(logs_root):
    _write_task(
        logs_root, "routed-1",
        models=[("cheap-m", "easy"), ("cheap-m", "easy"), ("big-m", "hard")],
        hints=["easy", "easy", "hard"],
    )
    t = scan_logs(str(logs_root))[0]
    assert t["model_calls"] == 3
    assert t["models"] == {"cheap-m": 2, "big-m": 1}
    assert t["hints"] == {"easy": 2, "hard": 1}


def test_scan_failed_task_and_missing_logs(logs_root):
    _write_task(logs_root, "sad-1", status="failed", cost=0.5)
    t = scan_logs(str(logs_root))[0]
    assert t["status"] == "failed"
    assert t["cost_usd"] == pytest.approx(0.5)
    # empty / nonexistent roots -> [], never raises
    assert scan_logs(str(logs_root / "nope")) == []
    (logs_root / "not-a-task").mkdir()
    assert scan_logs(str(logs_root))[0]["task_id"] == "sad-1"  # non-task dir skipped


def test_scan_crashed_task_without_trace_uses_checkpoint(logs_root):
    """A worker killed before task_start still has .runtime/ state —
    the checkpoint result is the fallback source."""
    _write_task(logs_root, "crashed-1", no_trace=True,
                runtime_result={"task_id": "crashed-1", "status": "error",
                                "cost_usd": 0.0})
    t = scan_logs(str(logs_root))[0]
    assert t["status"] == "error"
    assert t["issue"] == ""


def test_group_and_aggregate(logs_root):
    _write_task(logs_root, "a-1", status="success", cost=0.1)
    _write_task(logs_root, "a-2", status="failed", cost=0.2)
    _write_task(logs_root, "a-3", status="error", cost=0.0)
    nested = logs_root / "ablations" / "run-1" / "tasklogs"
    nested.mkdir(parents=True)
    _write_task(nested, "b-1", status="success", cost=0.3)

    tasks = scan_logs(str(logs_root))
    runs = group_by_run(tasks)
    assert set(runs) == {"adhoc", "ablations/run-1"}
    assert len(runs["adhoc"]) == 3
    agg = aggregate(tasks)
    assert agg["total"] == 4
    assert agg["counts"]["success"] == 2
    assert agg["counts"]["failed"] == 1
    assert agg["counts"]["error"] == 1
    assert agg["cost_usd"] == pytest.approx(0.6)
    assert agg["model_calls"] == 4
    # the err/t/o card in the UI = error+timeout+"?" — same shape here
    assert (agg["counts"]["error"] + agg["counts"]["timeout"]
            + agg["counts"]["?"]) == 1


# --------------------------------------------------------------------------
# server (real HTTP round-trip, read-only checks)
# --------------------------------------------------------------------------

@pytest.fixture
def server(logs_root):
    _write_task(logs_root, "live-1")
    state = _State(str(logs_root), refresh_s=3600)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.state = state
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


def test_server_serves_page_and_api(server):
    port = server.server_address[1]
    status, page = _get(f"http://127.0.0.1:{port}/")
    assert status == 200
    assert b"coding-harness" in page
    status, body = _get(f"http://127.0.0.1:{port}/api/tasks")
    data = json.loads(body)
    assert data["aggregate"]["total"] == 1
    assert data["aggregate"]["counts"]["success"] == 1
    assert "adhoc" in data["runs"]
    assert data["runs"]["adhoc"][0]["task_id"] == "live-1"


def test_server_is_read_only(server):
    port = server.server_address[1]
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/tasks", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 405
    with pytest.raises(urllib.error.HTTPError) as exc2:
        _get(f"http://127.0.0.1:{port}/../etc/passwd")
    assert exc2.value.code in (400, 404)


def test_state_refresh_picks_up_new_tasks(server, logs_root):
    port = server.server_address[1]
    _, before = _get(f"http://127.0.0.1:{port}/api/tasks")
    assert json.loads(before)["aggregate"]["total"] == 1
    _write_task(logs_root, "live-2")
    server.state._refresh()
    deadline = time.time() + 5
    seen = 0
    while time.time() < deadline:
        _, after = _get(f"http://127.0.0.1:{port}/api/tasks")
        seen = json.loads(after)["aggregate"]["total"]
        if seen == 2:
            break
        time.sleep(0.1)
    assert seen == 2


def test_html_render_contains_live_elements():
    page = _render_html()
    assert "auto-refresh" in page
    assert "/api/tasks" in page
    assert "STATUS_COLORS" in page
