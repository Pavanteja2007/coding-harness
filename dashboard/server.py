"""Read-only dashboard HTTP server (stdlib only — no new dependencies).

Serves one auto-refreshing HTML page that visualizes what scan_logs()
finds: task status, per-agent cost/model, pass/fail counts, per-run
grouping, difficulty-hint distribution. Read-only by construction:
the handler answers GET only, every path resolves inside the logs dir,
and nothing anywhere writes.

Run:  python -m dashboard  (or:  harness dashboard)
      then open http://127.0.0.1:8765

Assumes the documented log layouts (see collect.py docstring). Files
missing/malformed degrade to placeholders, never a traceback.
"""
from __future__ import annotations

import argparse
import html
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from dashboard.collect import aggregate, group_by_run, scan_logs
from memory.paths import default_logs_dir

_HOST = "127.0.0.1"
_PORT = 8765
_REFRESH_S = 5

_STATUS_COLORS = {
    "success": "#16a34a",
    "failed": "#dc2626",
    "error": "#b45309",
    "timeout": "#7c3aed",
    "?": "#6b7280",
}


class _State:
    """Cached scan shared across handler threads (auto-refresh thread
    rewrites it; handlers only read — plain attribute swap is atomic)."""

    def __init__(self, logs_dir: str, refresh_s: float) -> None:
        self.logs_dir = logs_dir
        self.refresh_s = refresh_s
        self.tasks: List[Dict[str, Any]] = []
        self.stop = threading.Event()
        self._refresh()

    def _refresh(self) -> None:
        try:
            self.tasks = scan_logs(self.logs_dir)
        except Exception:  # a dashboard scan must never crash the server
            self.tasks = []

    def serve_forever_refresh(self) -> None:
        while not self.stop.wait(self.refresh_s):
            self._refresh()


class _Handler(BaseHTTPRequestHandler):
    """GET-only handler: / (HTML page), /api/tasks (JSON).

    protocol_version is HTTP/1.1 so keep-alive connections get
    well-formed responses (Content-Length on every reply, including
    send_error's) — under HTTP/1.0 the response/close race intermittently
    aborts in-flight client reads on Windows.
    """

    protocol_version = "HTTP/1.1"

    def _state(self) -> "_State":
        return self.server.state  # type: ignore[attr-defined]  # injected in serve()

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            body = _render_html().encode("utf-8")
            ctype = "text/html; charset=utf-8"
        elif path == "/api/tasks":
            tasks = self._state().tasks
            body = json.dumps(
                {"aggregate": aggregate(tasks),
                 "runs": group_by_run(tasks)},
            ).encode("utf-8")
            ctype = "application/json"
        else:
            self.send_error(404, "not found (read-only dashboard)")
            return
        # never cache a live dashboard (also: send_error pages must stay
        # ASCII — BaseHTTPRequestHandler latin-1-encodes the body).
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 — read-only: reject writes
        self.send_error(405, "read-only dashboard; POST not allowed")

    def do_PUT(self) -> None:  # noqa: N802 — read-only: reject writes
        self.send_error(405, "read-only dashboard; PUT not allowed")

    def do_DELETE(self) -> None:  # noqa: N802 — read-only: reject writes
        self.send_error(405, "read-only dashboard; DELETE not allowed")

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet access log
        pass


def _render_html() -> str:
    """The single-page shell: markup + inline CSS/JS (no build tooling by
    design — spec item 40 demands a thin, cheap layer). JS polls
    /api/tasks and re-renders the table every few seconds."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>coding-harness dashboard</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; margin: 2rem; }
  h1 { font-size: 1.3rem; margin: 0 0 .3rem; }
  h2 { font-size: 1rem; margin: 1.4rem 0 .4rem; color: #666; }
  .agg { display: flex; gap: 1.2rem; flex-wrap: wrap; margin: .8rem 0; }
  .card { border: 1px solid #8884; border-radius: 8px; padding: .6rem 1rem; min-width: 7rem; }
  .card b { font-size: 1.25rem; display: block; }
  .card span { color: #888; font-size: .75rem; }
  table { border-collapse: collapse; width: 100%; font-size: .85rem; }
  th, td { text-align: left; padding: .28rem .5rem; border-bottom: 1px solid #8883; }
  th { color: #666; font-weight: 600; cursor: pointer; user-select: none; }
  tr:hover { background: #8881; }
  .st { font-weight: 600; }
  .muted { color: #888; }
  .mono { font-family: ui-monospace, monospace; }
  #err { color: #dc2626; margin-top: 1rem; }
</style>
</head>
<body>
<h1>coding-harness &mdash; read-only run dashboard</h1>
<div class="muted" id="meta">scanning logs&hellip;</div>
<div class="agg" id="agg"></div>
<div id="content"></div>
<div id="err"></div>
<script>
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const STATUS_COLORS = %STATUS_COLORS%;
let sortKey = "started_ts", sortDir = -1;

function fmtAgg(a) {
  const cards = [
    ["tasks", a.total], ["success", a.counts.success || 0],
    ["failed", a.counts.failed || 0], ["err/t/o",
      (a.counts.error || 0) + (a.counts.timeout || 0) + (a.counts["?"] || 0)],
    ["cost $", a.cost_usd.toFixed(4)], ["model calls", a.model_calls],
  ];
  return cards.map(([l, v]) =>
    `<div class="card"><b>${esc(v)}</b><span>${esc(l)}</span></div>`).join("");
}

function fmtTask(t) {
  const models = Object.entries(t.models || {}).map(([m, n]) =>
    `${esc(m)}&times;${esc(n)}`).join(", ") || '<span class="muted">&ndash;</span>';
  const hints = Object.entries(t.hints || {}).map(([h, n]) =>
    `${esc(h)}:${esc(n)}`).join(" ") || "";
  const color = STATUS_COLORS[t.status] || "#6b7280";
  const pct = t.plan_steps ? Math.round(100 * t.completed_steps / t.plan_steps) : null;
  const prog = pct === null ? '<span class="muted">&ndash;</span>' : `${pct}%`;
  const elapsed = t.elapsed_s === null || t.elapsed_s === undefined
    ? '<span class="muted">&ndash;</span>' : esc(t.elapsed_s) + "s";
  return `<tr>
    <td class="mono">${esc(t.task_id)}</td>
    <td class="st" style="color:${color}">${esc(t.status)}</td>
    <td>${esc(t.attempts || "&ndash;")}</td>
    <td class="mono">${esc(t.cost_usd.toFixed(4))}</td>
    <td>${esc(t.model_calls || "&ndash;")}</td>
    <td>${models}</td>
    <td class="muted">${hints}</td>
    <td>${prog}</td>
    <td>${elapsed}</td>
    <td class="muted mono">${esc(t.run)}</td>
  </tr>`;
}

function render(data) {
  document.getElementById("agg").innerHTML = fmtAgg(data.aggregate);
  document.getElementById("meta").textContent =
    `generated ${data.aggregate.generated_at} (auto-refresh %REFRESH_S%s)`;
  const runs = Object.entries(data.runs);
  document.getElementById("content").innerHTML = runs.map(([run, tasks]) => `
    <h2>${esc(run)} &mdash; ${tasks.length} task(s),
        $${tasks.reduce((s, t) => s + (t.cost_usd || 0), 0).toFixed(4)}</h2>
    <table>
      <thead><tr>
        <th data-k="task_id">task</th><th data-k="status">status</th>
        <th data-k="attempts">att</th><th data-k="cost_usd">cost $</th>
        <th data-k="model_calls">calls</th><th>models</th><th>hints</th>
        <th>plan</th><th data-k="elapsed_s">time</th><th>run</th>
      </tr></thead>
      <tbody>${tasks.map(fmtTask).join("")}</tbody>
    </table>`).join("");
}

async function poll() {
  try {
    const res = await fetch("/api/tasks");
    document.getElementById("err").textContent = "";
    render(await res.json());
  } catch (e) {
    document.getElementById("err").textContent = "fetch failed: " + e;
  }
}

document.addEventListener("click", e => {
  const th = e.target.closest("th[data-k]");
  if (!th) return;
  const k = th.dataset.k;
  if (sortKey === k) sortDir *= -1; else { sortKey = k; sortDir = 1; }
  poll();  // full re-fetch is cheap and keeps rendering single-pathed
});

poll();
setInterval(poll, %REFRESH_S%000);
</script>
</body>
</html>
"""


def serve(
    logs_dir: Optional[str] = None,
    host: str = _HOST,
    port: int = _PORT,
    refresh_s: float = _REFRESH_S,
    open_browser: bool = True,
) -> None:
    """Run the dashboard server (blocks until interrupted).

    Assumes logs_dir defaults to the shared logs root convention
    (memory/paths.py). Read-only: GET only, no writes, bind loopback by
    default.
    """
    logs = logs_dir or str(default_logs_dir())
    state = _State(logs, max(1.0, float(refresh_s)))
    refresher = threading.Thread(
        target=state.serve_forever_refresh, daemon=True, name="dash-refresh")
    refresher.start()

    server = ThreadingHTTPServer((host, port), _Handler)
    server.state = state  # type: ignore[attr-defined]
    url = f"http://{host}:{port}"
    print(f"read-only dashboard: {url}  (logs: {logs})")
    print("Ctrl+C to stop")
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        state.stop.set()
        server.server_close()


def main(argv: Optional[List[str]] = None) -> int:
    """`python -m dashboard` entry point (argparse, mirrors CLI style)."""
    parser = argparse.ArgumentParser(
        prog="dashboard", description="read-only dashboard over existing logs")
    parser.add_argument("--logs-dir", default=None, help="logs root (default: ./logs)")
    parser.add_argument("--host", default=_HOST)
    parser.add_argument("--port", type=int, default=_PORT)
    parser.add_argument("--refresh-s", type=float, default=_REFRESH_S)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    serve(
        logs_dir=args.logs_dir,
        host=args.host,
        port=args.port,
        refresh_s=args.refresh_s,
        open_browser=not args.no_browser,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
