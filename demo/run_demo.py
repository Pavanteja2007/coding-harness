"""One-command offline demo of the whole coding-harness system.

Run:  python demo/run_demo.py

No API key, no network, no Docker required (verification runs through
harness._stubs.sandbox — the local-subprocess fallback — so the demo is
deterministic and repeatable anywhere; the REAL Docker sandbox is what
`harness fix` uses in normal operation, see demo/README.md step 1 for
the with-a-real-model variant of this exact flow).

What it shows, in demo order (each step prints its own evidence):
  1. FIX       — `harness fix` on a real (fixture) bug: plan → edit →
                 verify → verifier-gated success, with the diff printed.
  2. GIT/PR    — the git-native output: branch, commit, PR description
                 (from logs/<task>/git.json) + the rationale paragraph
                 (logs/<task>/rationale.md).
  3. ROUTING   — the adaptive-routing ablation savings, read from the
                 real summary.json artifacts under logs/ablations/ (if
                 present; otherwise explains where they come from).
  4. MEMORY    — MCP-style memory queries against the demo run's own
                 just-written decision memory + a code-graph query.
  5. HINT      — how to show the live dashboard + concurrent runs.

Everything is isolated under demo/demo-work/ (deleted at start, kept at
exit so you can inspect the artifacts). Exit 0 = all demo steps passed.

Assumes: run from the repo root (paths resolve relative to this file).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEMO = ROOT / "demo" / "demo-work"
REPO_SRC = ROOT / "cli" / "fixtures" / "smoke_repo"
REPO = DEMO / "repo"


def _banner(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


def _step(msg: str) -> None:
    print(f"  {msg}")


# ---------------------------------------------------------------------------
# Step 0: isolated environment
# ---------------------------------------------------------------------------

def setup_env() -> None:
    """Fresh demo-work dir + isolated HARNESS_HOME/logs (never touches
    the user's real .harness/ or logs/)."""
    if DEMO.exists():
        shutil.rmtree(DEMO, onerror=lambda f, p, e: os.chmod(p, 0o700) or f(p))
    DEMO.mkdir(parents=True)
    shutil.copytree(REPO_SRC, REPO, ignore=shutil.ignore_patterns("__pycache__"))
    os.environ["HARNESS_HOME"] = str(DEMO / "home")
    os.environ["HARNESS_LOGS_DIR"] = str(DEMO / "logs")
    (DEMO / "home").mkdir()
    (DEMO / "logs").mkdir()
    _banner("STEP 0 — isolated demo workspace (demo/demo-work/)")
    _step(f"repo copy with a real bug:   {REPO}")
    _step("bug: mathutil.mean() returns the SUM instead of the mean")
    _step(f"logs root:                   {DEMO / 'logs'}")
    _step(f"memory home:                 {DEMO / 'home'}")


# ---------------------------------------------------------------------------
# Step 1: harness fix (offline scripted model — same loop as production)
# ---------------------------------------------------------------------------

class ScriptedDemoModel:
    """Deterministic offline model (the tests' ScriptedModel pattern):
    plans one step, applies the one-line fix via bash, SUBMITs. The
    harness loop around it — retrieval, working-copy isolation, verify,
    gating — is the REAL one."""

    def __init__(self) -> None:
        self.n = 0

    def get_last_usage(self) -> dict:
        return {"model": "scripted-demo", "provider": "demo",
                "prompt_tokens": 100, "completion_tokens": 50,
                "tokens": 150, "cost_usd": 0.0001}

    def __call__(self, messages, difficulty_hint=None, provider=None,
                 model=None, api_key=None):
        self.n += 1
        if any("planning a bug fix" in m.get("content", "") for m in messages):
            return json.dumps({
                "analysis": "mean() returns sum(values); must divide by len",
                "plan": [{
                    "id": 1,
                    "description": "fix mean() to divide by len(values)",
                    "checkpoint": "tests/test_mathutil.py::test_mean passes",
                    "files_hint": ["mathutil.py"],
                }],
            })
        first_user = messages[0].get("content", "") if messages else ""
        if self.n == 2 or (self.n > 2 and "Begin" in first_user):
            return (
                "python -c \"import pathlib; p = pathlib.Path('mathutil.py'); "
                "s = p.read_text(); s = s.replace('return sum(values)', "
                "'return sum(values) / len(values)'); p.write_text(s)\""
            )
        return "SUBMIT"


def step1_fix() -> Path:
    """Run the real harness loop on the buggy repo; return the task dir."""
    _banner("STEP 1 — harness fix: plan -> edit -> verify (verifier-gated)")
    import harness.deps as hdeps

    fake = ScriptedDemoModel()
    hdeps.set_call_model(fake)

    # The demo runs offline: swap the Docker sandbox for the local
    # subprocess stub (the REAL sandbox needs Docker; see README step 1
    # for the production path — identical loop, different executor).
    hdeps.set_execute_sandboxed(None)  # clear any prior override
    import harness._stubs.sandbox as stub_sandbox
    hdeps.set_execute_sandboxed(stub_sandbox.execute_sandboxed)

    from cli.main import main

    t0 = time.time()
    rc = main([
        "fix",
        "--repo", str(REPO),
        "--issue",
        "mean() in mathutil.py returns the sum of the values instead of "
        "the arithmetic mean. tests/test_mathutil.py::test_mean fails; "
        "fix mathutil.py so it passes.",
        "--target-test", "tests/test_mathutil.py::test_mean",
        "--task-id", "demo-fix-mean",
        # harness.core writes to ./logs under CWD unless given a log root;
        # point it at the demo's isolated tree.
        "--log-root", str(DEMO / "logs"),
    ])
    elapsed = time.time() - t0
    hdeps.set_call_model(None)
    hdeps.set_execute_sandboxed(None)

    task_dir = DEMO / "logs" / "demo-fix-mean"
    if rc != 0 or not (task_dir / "state.json").is_file():
        print(f"\nDEMO FAILED: fix rc={rc} (see output above)")
        raise SystemExit(1)
    _step(f"status: success (verifier-gated: target PASS + suite PASS + "
          f"not flaky) in {elapsed:.1f}s, {fake.n} model call(s)")
    state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    _step(f"files touched: {state.get('files_touched')}")
    _step(f"decisions recorded: {len(state.get('decisions') or [])}")
    return task_dir


# ---------------------------------------------------------------------------
# Step 2: git-native output + rationale
# ---------------------------------------------------------------------------

def step2_git_output(task_dir: Path) -> None:
    """Show branch/commit/PR + rationale produced for the verified fix."""
    _banner("STEP 2 — git-native output + rationale (product-grade finish)")
    git_json = task_dir / "git.json"
    rationale = task_dir / "rationale.md"
    if git_json.is_file():
        out = json.loads(git_json.read_text(encoding="utf-8"))
        _step(f"branch:         {out['branch']}")
        _step(f"fix commit:    {out['commit_sha'][:12]}  "
              f"(`git -C {task_dir / 'work'} show` prints exactly the fix)")
        _step("\n  commit message:")
        for line in out["commit_message"].splitlines()[:8]:
            print(f"    {line}")
        pr = out["pr_description"]
        sections = [s.strip() for s in pr.split("##") if s.strip()]
        _step(f"\n  PR description sections: "
              f"{[s.splitlines()[0] for s in sections]}")
        print("\n  PR preview (first section):")
        body = pr.split("## Changes")[0].strip()
        for line in body.splitlines()[:10]:
            print(f"    {line}")
    else:
        _step("(no git.json — the fix was already-passing / disabled)")
    if rationale.is_file():
        print("\n  rationale.md:")
        para = rationale.read_text(encoding="utf-8").strip()
        for line in para.splitlines():
            print(f"    {line}")
    # prove the git story: pristine commit -> fix commit
    import subprocess as sp
    log = sp.run(["git", "-C", str(task_dir / "work"), "log", "--oneline"],
                 capture_output=True, text=True)
    if log.returncode == 0:
        _step("\n  git log in the working copy (fix on top of pristine):")
        for line in log.stdout.strip().splitlines():
            print(f"    {line}")


# ---------------------------------------------------------------------------
# Step 3: adaptive routing savings (real ablation artifacts)
# ---------------------------------------------------------------------------

def step3_routing() -> None:
    """Summarize the real ablation artifacts, wherever they exist."""
    _banner("STEP 3 — adaptive model routing: measured cost savings")
    abl = ROOT / "logs" / "ablations"
    shown = False

    # v2 (5-task, canonical both-arms artifact)
    p = abl / "v2-heuristic-on-r2" / "summary.json"
    if p.is_file():
        try:
            on = json.loads(p.read_text(encoding="utf-8"))["arms"]["on"]
            off_p = abl / "v2-heuristic-off" / "summary.json"
            off = json.loads(off_p.read_text(encoding="utf-8"))["arms"]["off"]
            _step("5-task fixture ablation (logs/ablations/v2-heuristic-*):")
            _step(f"  OFF: ${off['total_cost_usd']:.4f}, success "
                  f"{off['success_rate']:.0%}  "
                  f"({off['statuses']['success']}/{sum(off['statuses'].values())})")
            _step(f"  ON : ${on['total_cost_usd']:.4f}, success "
                  f"{on['success_rate']:.0%}  "
                  f"({on['statuses']['success']}/{sum(on['statuses'].values())})")
            _step(f"  => routing ON cost: "
                  f"{on['total_cost_usd'] / off['total_cost_usd']:.0%} of OFF")
            shown = True
        except (OSError, ValueError, KeyError):
            pass
    # v4 (16-task expanded set, both arms in one canonical artifact; the
    # earlier v3-expanded run is INVALID — fixture-path bug, see
    # runtime/AGENTS.md — and v3-expanded-fixed is its corrected re-run)
    p4 = abl / "v4" / "summary.json"
    if p4.is_file():
        try:
            arms = json.loads(p4.read_text(encoding="utf-8"))["arms"]
            off, on = arms["off"], arms["on"]

            def _n(a: dict) -> int:
                st = a.get("statuses") or {}
                return sum(st.values())

            def _ok(a: dict) -> int:
                return (a.get("statuses") or {}).get("success", 0)

            _step("\n16-task expanded ablation (logs/ablations/v4/, "
                  "11 synthesized repos + 5 fixtures):")
            _step(f"  OFF: ${off['total_cost_usd']:.4f}, success "
                  f"{_ok(off)}/{_n(off)}")
            _step(f"  ON : ${on['total_cost_usd']:.4f}, success "
                  f"{_ok(on)}/{_n(on)}")
            _step(f"  => same success, routing ON at "
                  f"{on['total_cost_usd'] / off['total_cost_usd']:.0%} "
                  f"of baseline cost")
            _step("  honesty: costs use proxy price rates for free-tier "
                  "endpoints; deltas are price-model deltas")
            shown = True
        except (OSError, ValueError, KeyError):
            pass

    if not shown:
        _step("no ablation artifacts under logs/ablations/ — run "
              "`python -m runtime.ablation` (needs model endpoints) to "
              "produce them; see runtime/AGENTS.md")


# ---------------------------------------------------------------------------
# Step 4: memory queries (the MCP surface, demoed via the same calls)
# ---------------------------------------------------------------------------

def step4_memory(task_dir: Path) -> None:
    """Query the memory layer exactly as the MCP tools do (same functions
    the stdio server exposes) against the demo's own run + this repo."""
    _banner("STEP 4 — persistent memory: decisions + code graph (MCP surface)")
    from memory.decision_store import DecisionStore, format_decisions
    from memory.paths import decisions_db_path

    store = DecisionStore(str(decisions_db_path()))
    new = store.poll(str(DEMO / "logs"))
    _step(f"ingested {new} decision(s) from the demo run's state.json "
          f"(what MCP query_decisions does on every call)")
    results = store.search("fix", limit=5)
    _step("\n  query_decisions 'fix' ->")
    for r in results[:4]:
        print(f"    [{r.category}] {r.text[:100]}")

    from memory.code_graph import CodeGraph
    _step("\n  query_structure 'callers run_task' on this repo ->")
    g = CodeGraph(str(ROOT))
    out = g.query("callers run_task")
    for line in out.splitlines()[:6]:
        print(f"    {line}")
    _step("\n  (over MCP: `python -m mcp_server` + any MCP client — Claude "
          "Code/Cursor — exposes these plus task_status and list_repos)")


# ---------------------------------------------------------------------------
# Step 5: dashboard hint
# ---------------------------------------------------------------------------

def step5_dashboard() -> None:
    _banner("STEP 5 — live dashboard + concurrent runs (manual, 10s)")
    _step("start a concurrent run:   harness run-benchmark --subset <tasks.json> --concurrency 10")
    _step("then:                    harness dashboard   (http://127.0.0.1:8765)")
    _step("watch: statuses flip as the VERIFIER decides, cost + per-model")
    _step("       call counts accumulate per run, difficulty hints show routing")


def main() -> int:
    print("coding-harness — offline demo (no API key, no Docker, deterministic)")
    setup_env()
    task_dir = step1_fix()
    step2_git_output(task_dir)
    step3_routing()
    step4_memory(task_dir)
    step5_dashboard()
    _banner("DEMO COMPLETE")
    print(f"artifacts kept for inspection under: {DEMO}")
    print("with-a-real-model variant + talking points: demo/README.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
