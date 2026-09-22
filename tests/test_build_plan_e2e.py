"""Task C proof — a real multi-sub-task feature across >=2 sessions.

Docker-gated e2e (tests/test_build_plan.py holds the offline units).

The feature request for the feat02_shop fixture genuinely requires
several DISTINCT changes across the codebase (not a single-file
toggle):

  "Build out shoplib's checkout and reporting capabilities: (1) a
  receipt renderer that produces a plain-text line bundle for a cart
  with the subtotal, volume discount, and amount due; (2) loyalty
  point accrual — 1 point per whole dollar spent after discounts; and
  (3) CSV export of carts with one row per line."

- Sub-task 1 (receipts) touches shoplib/receipts.py (new module) —
  volume discount + amount due.
- Sub-task 2 (loyalty) touches shoplib/pricing.py (points accrual).
- Sub-task 3 (csv) touches shoplib/serializers.py (export shape).

The scripted model serves every stage deterministically: criteria
extraction, project decomposition, then per-sub-task acceptance-test
authoring + planner + implementing step sessions. Session 1 completes
sub-task 1 and CHECKPOINTS (budget 1/session); session 2 RESUMES from
project.json, skips sub-task 1 (never re-runs), builds sub-tasks 2 and
3 from the ACCUMULATED tree, and the project finishes with criteria
coverage + the final full-suite verify green — all through the REAL
Docker sandbox/verify.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures"


def _docker_up() -> bool:
    try:
        out = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=10,
        )
        return out.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    __import__("os").environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1" or not _docker_up(),
    reason="docker daemon not reachable (or HARNESS_EXEC_SKIP_DOCKER=1)",
)


# ---------------------------------------------------------------------------
# The scripted model: dispatch by system-prompt shape
# ---------------------------------------------------------------------------

REQUEST = (
    "Build out shoplib's checkout and reporting capabilities: (1) a "
    "receipt renderer that produces a plain-text line bundle for a "
    "cart with the subtotal, volume discount, and amount due; (2) "
    "loyalty point accrual - 1 point per whole dollar spent after "
    "discounts; and (3) CSV export of carts with one row per line."
)

CRITERIA = {
    "criteria": [
        {
            "id": "receipts_render",
            "description": "A cart's receipt renders as a plain-text "
            "line bundle showing subtotal, volume discount, and "
            "amount due.",
        },
        {
            "id": "loyalty_points",
            "description": "A cart earns 1 loyalty point per whole "
            "dollar spent after discounts.",
        },
        {
            "id": "csv_export",
            "description": "A cart exports to CSV with one row per cart line.",
        },
    ]
}

SUB_TASKS = {
    "analysis": "three distinct capabilities, one sub-task each",
    "sub_tasks": [
        {
            "id": 1,
            "description": "add a receipt renderer module to shoplib",
            "criteria": ["receipts_render"],
            "files_hint": ["shoplib/receipts.py"],
        },
        {
            "id": 2,
            "description": "add loyalty point accrual to the pricing rules",
            "criteria": ["loyalty_points"],
            "files_hint": ["shoplib/pricing.py"],
        },
        {
            "id": 3,
            "description": "add CSV export of carts to the serializers",
            "criteria": ["csv_export"],
            "files_hint": ["shoplib/serializers.py"],
        },
    ],
}


def _json_reply(obj):
    return json.dumps(obj)


def _build_tests_reply(module, import_line, test_body):
    """The per-sub-task acceptance-test authoring reply."""
    return _json_reply(
        {
            "tests": [
                {
                    "filename": f"test_{module}.py",
                    "content": (f"import pytest\n\n{import_line}\n\n{test_body}"),
                }
            ]
        }
    )


# Receipts: tests import a NEW module (fails on pristine -> honest
# baseline-fail), implemented by writing shoplib/receipts.py.
RECEIPTS_TESTS = _build_tests_reply(
    "receipts",
    "from shoplib.model import Cart, Product\nfrom shoplib.receipts import render_receipt",
    "def _cart():\n"
    "    c = Cart()\n"
    "    c.add(Product(sku='MUG-1', price_cents=1200), qty=2)\n"
    "    c.add(Product(sku='PEN-1', price_cents=300), qty=1)\n"
    "    return c\n\n"
    "def test_receipt_shows_amounts():\n"
    "    text = render_receipt(_cart())\n"
    "    assert 'Subtotal: 2700' in text\n"
    "    assert 'Discount: 0' in text\n"
    "    assert 'Amount due: 2700' in text\n\n"
    "def test_receipt_applies_volume_discount():\n"
    "    c = Cart()\n"
    "    c.add(Product(sku='DESK', price_cents=6000), qty=1)\n"
    "    text = render_receipt(c)\n"
    "    assert 'Subtotal: 6000' in text\n"
    "    assert 'Discount: 600' in text\n"
    "    assert 'Amount due: 5400' in text\n",
)

RECEIPTS_IMPL = (
    "cat > shoplib/receipts.py <<'EOF'\n"
    '"""Plain-text receipt rendering."""\n\n'
    "from shoplib.model import Cart\n"
    "from shoplib.pricing import apply_volume_discount\n\n\n"
    "def render_receipt(cart: Cart) -> str:\n"
    '    """A plain-text line bundle: subtotal, discount, amount due."""\n'
    "    subtotal = cart.subtotal_cents()\n"
    "    discounted = apply_volume_discount(subtotal, len(cart.lines))\n"
    "    discount = subtotal - discounted\n"
    "    lines = [\n"
    "        f'Subtotal: {subtotal}',\n"
    "        f'Discount: {discount}',\n"
    "        f'Amount due: {discounted}',\n"
    "    ]\n"
    "    return '\\n'.join(lines)\n"
    "EOF"
)

# Loyalty: tests extend pricing (fail pre-fix: no points function),
# implemented by appending to shoplib/pricing.py.
LOYALTY_TESTS = _build_tests_reply(
    "loyalty",
    "from shoplib.pricing import loyalty_points",
    "def test_points_per_whole_dollar():\n"
    "    # 5400 cents = $54 -> 54 points\n"
    "    assert loyalty_points(5400) == 54\n\n"
    "def test_points_floor_partial_dollars():\n"
    "    # 2699 cents = $26.99 -> 26 points\n"
    "    assert loyalty_points(2699) == 26\n\n"
    "def test_zero_spend_zero_points():\n"
    "    assert loyalty_points(0) == 0\n",
)

LOYALTY_IMPL = (
    "cat >> shoplib/pricing.py <<'EOF'\n\n\n"
    "def loyalty_points(amount_due_cents: int) -> int:\n"
    '    """1 loyalty point per whole dollar (100c) spent."""\n'
    "    return amount_due_cents // 100\n"
    "EOF"
)

# CSV export: tests extend serializers, implemented by appending.
CSV_TESTS = _build_tests_reply(
    "csv_export",
    "from shoplib.model import Cart, Product\nfrom shoplib.serializers import to_csv",
    "def _cart():\n"
    "    c = Cart()\n"
    "    c.add(Product(sku='MUG-1', price_cents=1200), qty=2)\n"
    "    c.add(Product(sku='PEN-1', price_cents=300), qty=1)\n"
    "    return c\n\n"
    "def test_csv_header_and_rows():\n"
    "    text = to_csv(_cart())\n"
    "    rows = text.strip().splitlines()\n"
    "    assert rows[0] == 'sku,qty,unit_price_cents'\n"
    "    assert rows[1] == 'MUG-1,2,1200'\n"
    "    assert rows[2] == 'PEN-1,1,300'\n",
)

CSV_IMPL = (
    "cat >> shoplib/serializers.py <<'EOF'\n\n\n"
    "def to_csv(cart: Cart) -> str:\n"
    '    """CSV export: header row + one row per cart line."""\n'
    "    rows = ['sku,qty,unit_price_cents']\n"
    "    for sku, qty, price in cart_lines(cart):\n"
    "        rows.append(f'{sku},{qty},{price}')\n"
    "    return '\\n'.join(rows) + '\\n'\n"
    "EOF"
)


class ProjectScriptedModel:
    """Deterministic model for the whole multi-session project run.

    Dispatch by system-prompt shape, in arrival order:
    - criteria extraction   -> CRITERIA
    - project decomposition -> SUB_TASKS
    - sub-task N's acceptance-test authoring -> that sub-task's tests
    - sub-task N's planner                  -> a 1-step plan
    - sub-task N's step session             -> the implementing command
    A `_gate` (int|None) simulates the SESSION boundary: when set to
    sub-task id N, the model raises SystemExit AFTER sub-task N's
    authoring succeeds but before its planner call — no, the cleaner
    session cut is handled by the harness's session budget itself; the
    gate is only used for the interrupted-session variant.
    """

    def __init__(self):
        self._step_queues = {}
        self._seen_sub_planners = []

    def get_last_usage(self):
        return {
            "model": "project-scripted",
            "provider": "fake",
            "tokens": 20,
            "cost_usd": 0.0001,
        }

    def __call__(self, messages, **kw):
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        # --- project-layer calls ----------------------------------------
        if "ACCEPTANCE-CRITERIA" in system:
            return _json_reply(CRITERIA)
        if "span SEVERAL work sessions" in system:
            return _json_reply(SUB_TASKS)
        # --- per-sub-task build-mode calls ------------------------------
        if "ACCEPTANCE TESTS" in system:
            user = next((m["content"] for m in messages if m["role"] == "user"), "")
            if "receipt renderer" in user:
                return RECEIPTS_TESTS
            if "loyalty point accrual" in user:
                return LOYALTY_TESTS
            if "CSV export" in user:
                return CSV_TESTS
            raise AssertionError(f"unknown sub-request: {user[:200]}")
        if "planning a bug fix" in system:
            user = next((m["content"] for m in messages if m["role"] == "user"), "")
            if "receipt renderer" in user:
                plan = [
                    {
                        "id": 1,
                        "description": "write shoplib/receipts.py",
                        "checkpoint": "receipt acceptance test passes",
                        "files_hint": ["shoplib/receipts.py"],
                    }
                ]
            elif "loyalty point accrual" in user:
                plan = [
                    {
                        "id": 1,
                        "description": "add loyalty_points to pricing",
                        "checkpoint": "loyalty acceptance test passes",
                        "files_hint": ["shoplib/pricing.py"],
                    }
                ]
            elif "CSV export" in user:
                plan = [
                    {
                        "id": 1,
                        "description": "add to_csv to serializers",
                        "checkpoint": "csv acceptance test passes",
                        "files_hint": ["shoplib/serializers.py"],
                    }
                ]
            else:
                raise AssertionError(f"unknown planner request: {user[:200]}")
            self._seen_sub_planners.append(user[:60])
            return _json_reply({"analysis": "scripted", "plan": plan})
        # --- step session ------------------------------------------------
        # The sub-request text rides the step system prompt's
        # "## Overall issue" block — NOT the first user message (which
        # is the curated context block). Dispatch on the system prompt.
        import re as _re

        mm = _re.search(r"your step is #(\d+) of", system)
        step_id = int(mm.group(1)) if mm else 0
        if "receipt renderer" in system:
            queue = self._step_queues.setdefault("receipts", [RECEIPTS_IMPL, "SUBMIT"])
        elif "loyalty point accrual" in system:
            queue = self._step_queues.setdefault("loyalty", [LOYALTY_IMPL, "SUBMIT"])
        elif "CSV export" in system:
            queue = self._step_queues.setdefault("csv", [CSV_IMPL, "SUBMIT"])
        else:
            raise AssertionError(f"unknown step session: {system[:200]}")
        if step_id == 1 and not getattr(self, "_started", None):
            self._started = {}
        if queue:
            return queue.pop(0)
        return "SUBMIT"


# ---------------------------------------------------------------------------
# The proof
# ---------------------------------------------------------------------------


@requires_docker
class TestMultiSessionProjectE2E:
    def test_two_real_sessions_with_checkpoint_resume(self, tmp_path):
        """The full Task C proof: 3 distinct module changes, executed
        across 2 real sessions with a genuine checkpoint between them,
        through the REAL Docker sandbox/verify at every gate."""
        from harness import deps
        from harness.build_plan import run_project
        from harness.config import get_config

        # isolate the fixture repo (never-mutate guarantee pinned at end)
        repo = tmp_path / "feat02"
        shutil.copytree(FIXTURES / "feat02_shop", repo)

        logs = tmp_path / "logs"
        cfg = get_config(
            {
                "agent_tests": False,  # keep the e2e focused on the
                "self_critique": False,  # multi-session machinery
                "build_project": True,
                "project_sub_tasks_per_session": 1,  # session 1 = sub-task 1
                "max_wallclock_s": 1200,
                "verify_timeout_s": 300,
            }
        )

        deps.set_call_model(ProjectScriptedModel())
        try:
            # -- SESSION 1: plan + sub-task 1, then the checkpoint pause -
            out1 = run_project(
                request_text=REQUEST,
                repo_path=str(repo),
                config=cfg,
                log_root=logs,
                project_id="proj-e2e",
            )
        finally:
            deps.reset_overrides()

        assert out1["status"] == "checkpointed", out1.get("note")
        assert out1["completed"] == [1]
        assert len(out1["sub_tasks"]) == 3
        assert out1["sessions"] == 1

        # the persisted project plan is on disk and resumable
        proj_file = logs / "proj-e2e" / "project.json"
        proj = json.loads(proj_file.read_text(encoding="utf-8"))
        assert proj["status"] == "checkpointed"
        assert proj["completed"] == [1]
        assert proj["current_tree"] and Path(proj["current_tree"]).is_dir()
        # sub-task 1's verified work REALLY accumulated: the pinned tree
        # contains the receipts module, the original does not
        pinned1 = Path(proj["current_tree"])
        assert (pinned1 / "shoplib" / "receipts.py").is_file()
        assert not (repo / "shoplib" / "receipts.py").exists()
        # the pinned tree carries sub-task 1's acceptance tests too
        acc = list((pinned1 / "tests" / "_build_acceptance").glob("*.py"))
        assert acc

        deps.set_call_model(ProjectScriptedModel())
        try:
            # -- SESSION 2: resume — sub-tasks 2 and 3, then completion --
            # (this session's budget is 2: it finishes the plan)
            out2 = run_project(
                request_text=REQUEST,
                repo_path=str(repo),
                config=dict(cfg, project_resume=True, project_sub_tasks_per_session=2),
                log_root=logs,
                project_id="proj-e2e",
            )
        finally:
            deps.reset_overrides()

        assert out2["status"] == "success", out2.get("note")
        assert out2["completed"] == [1, 2, 3]
        assert out2["sessions"] == 2
        assert [c["id"] for c in out2["criteria"]] == [
            "receipts_render",
            "loyalty_points",
            "csv_export",
        ]

        # the accumulated final tree has ALL THREE capabilities and none
        # of them ever touched the original repo
        final_tree = Path(
            json.loads(proj_file.read_text(encoding="utf-8"))["current_tree"]
        )
        assert (final_tree / "shoplib" / "receipts.py").is_file()
        pricing = (final_tree / "shoplib" / "pricing.py").read_text(encoding="utf-8")
        assert "def loyalty_points" in pricing
        serializers = (final_tree / "shoplib" / "serializers.py").read_text(
            encoding="utf-8"
        )
        assert "def to_csv" in serializers
        # original untouched (never-mutate across the whole project)
        assert not (repo / "shoplib" / "receipts.py").exists()
        assert "def loyalty_points" not in (repo / "shoplib" / "pricing.py").read_text(
            encoding="utf-8"
        )
        assert "def to_csv" not in (repo / "shoplib" / "serializers.py").read_text(
            encoding="utf-8"
        )

        # -- trace + project-file evidence ------------------------------
        kinds = [
            json.loads(l)["kind"]
            for l in (logs / "proj-e2e" / "trace.jsonl")
            .read_text(encoding="utf-8")
            .strip()
            .splitlines()
        ]
        for k in (
            "project_start",
            "project_criteria_extracted",
            "project_plan_generated",
            "project_sub_task_start",
            "project_checkpoint",
            "project_final_verify",
            "project_end",
        ):
            assert k in kinds, k
        # two sessions = two project_start events; the resume carried a
        # fresh model but never re-ran sub-task 1's planner
        assert kinds.count("project_start") == 2
        assert kinds.count("project_sub_task_start") == 3  # 1,2,3 exactly

        # sub-task 2's build REALLY started from the accumulated tree
        # (its base copy contained receipts.py)
        s2_base = logs / "proj-e2e-s2.base"
        assert (s2_base / "shoplib" / "receipts.py").is_file()

        # resuming a FINISHED project is an honest no-op
        deps.set_call_model(ProjectScriptedModel())
        try:
            out3 = run_project(
                request_text=REQUEST,
                repo_path=str(repo),
                config=dict(cfg, project_resume=True),
                log_root=logs,
                project_id="proj-e2e",
            )
        finally:
            deps.reset_overrides()
        assert out3["status"] == "success"
        assert out3["completed"] == [1, 2, 3]
        assert "already complete" in out3["note"]

    def test_failed_sub_task_checkpoints_for_retry(self, tmp_path):
        """A sub-task failure (its build can't pass) ends the session
        honestly with the project state checkpointed — a LATER session
        resumes and retries that sub-task from the same point."""
        from harness import deps
        from harness.build_plan import run_project
        from harness.config import get_config

        repo = tmp_path / "feat02"
        shutil.copytree(FIXTURES / "feat02_shop", repo)

        class FailSubTask2(ProjectScriptedModel):
            def __init__(self):
                super().__init__()
                self._poisoned = False

            def __call__(self, messages, **kw):
                system = next(
                    (m["content"] for m in messages if m["role"] == "system"), ""
                )
                users = [m["content"] for m in messages if m["role"] == "user"]
                if "ACCEPTANCE TESTS" in system and any(
                    "loyalty point accrual" in u for u in users
                ):
                    # author a test that CANNOT pass with the scripted
                    # implementation (wrong expected value) -> the sub-
                    # task's loop exhausts retries and fails honestly
                    return _json_reply(
                        {
                            "tests": [
                                {
                                    "filename": "test_loyalty.py",
                                    "content": (
                                        "from shoplib.pricing import "
                                        "loyalty_points\n\n"
                                        "def test_impossible():\n"
                                        "    assert loyalty_points(5400) == "
                                        "99999\n"
                                    ),
                                }
                            ]
                        }
                    )
                return super().__call__(messages, **kw)

        logs = tmp_path / "logs"
        cfg = get_config(
            {
                "agent_tests": False,
                "self_critique": False,
                "max_wallclock_s": 1200,
                "verify_timeout_s": 300,
                "max_retries": 2,
                # let session 1 RUN sub-task 2 (and watch it fail) —
                # budget 1 would checkpoint after sub-task 1 instead
                "project_sub_tasks_per_session": 2,
            }
        )
        deps.set_call_model(FailSubTask2())
        try:
            out1 = run_project(
                request_text=REQUEST,
                repo_path=str(repo),
                config=cfg,
                log_root=logs,
                project_id="proj-fail",
            )
        finally:
            deps.reset_overrides()
        assert out1["status"] in ("failed", "timeout")
        assert out1["completed"] == [1]
        proj = json.loads(
            (logs / "proj-fail" / "project.json").read_text(encoding="utf-8")
        )
        assert proj["completed"] == [1]
        assert proj["status"] in ("failed", "timeout")

        # a LATER session retries sub-task 2 with a HEALTHY model and
        # the project completes — the checkpoint held the progress
        deps.set_call_model(ProjectScriptedModel())
        try:
            out2 = run_project(
                request_text=REQUEST,
                repo_path=str(repo),
                config=dict(cfg, project_resume=True),
                log_root=logs,
                project_id="proj-fail",
            )
        finally:
            deps.reset_overrides()
        assert out2["status"] == "success"
        assert out2["completed"] == [1, 2, 3]

    def test_coverage_gap_aborts_planning_honestly(self, tmp_path):
        """A decomposition that leaves criteria uncovered is a hard
        planning error — the project never starts on a plan that can't
        complete the contract."""
        from harness import deps
        from harness.build_plan import run_project
        from harness.config import get_config

        repo = tmp_path / "feat02"
        shutil.copytree(FIXTURES / "feat02_shop", repo)

        class GappedModel(ProjectScriptedModel):
            def __call__(self, messages, **kw):
                system = next(
                    (m["content"] for m in messages if m["role"] == "system"), ""
                )
                if "span SEVERAL work sessions" in system:
                    plan = {
                        "analysis": "missing csv",
                        "sub_tasks": SUB_TASKS["sub_tasks"][:2],  # drops csv
                    }
                    return _json_reply(plan)
                return super().__call__(messages, **kw)

        deps.set_call_model(GappedModel())
        try:
            out = run_project(
                request_text=REQUEST,
                repo_path=str(repo),
                config=get_config({"build_project": True}),
                log_root=tmp_path / "logs",
                project_id="proj-gap",
            )
        finally:
            deps.reset_overrides()
        assert out["status"] == "error"
        assert "csv_export" in out["note"]
