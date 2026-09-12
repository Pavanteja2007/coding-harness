"""IR2 three-arm verification: re-derive every headline number from the
on-disk ledgers + results (not from summary.json's own arithmetic)."""

import json
from pathlib import Path

OUT = Path("logs/ablations/ir2-final")
LOGS = OUT / "tasklogs"

s = json.loads((OUT / "summary.json").read_text(encoding="utf-8"))
bugs = None
# recover the bug list order from any arm's per_task keys
key_arms = {a: list(v["per_task"].keys()) for a, v in s["arms"].items()}
print("arm per-task key samples:")
for a, keys in key_arms.items():
    print(f"  {a}: n={len(keys)} first={keys[0]} last={keys[-1]}")


def ledger(tid):
    p = LOGS / f"{tid}.runtime" / "model_ledger.jsonl"
    if not p.exists():
        return []
    return [
        json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()
    ]


print("\n--- per-task statuses ---")
for arm in ("off", "on", "ensemble"):
    a = s["arms"][arm]
    rows = []
    for tid, t in a["per_task"].items():
        rows.append(f"{tid.replace('abl-' + arm + '-', ''):18s}:{t['status']:8s}")
    print(f"[{arm}] " + " ".join(rows))

print("\n--- ledger-derived arm totals (recomputed) ---")
for arm in ("off", "on", "ensemble"):
    a = s["arms"][arm]
    total_calls = 0
    total_cost = 0.0
    total_tokens = 0
    models = {}
    for tid, t in a["per_task"].items():
        # ensemble per_task entries aggregate sub-tasks; for off/on the
        # ledger lives under abl-{arm}-{slug}.runtime
        if arm == "ensemble":
            continue
        recs = ledger(tid)
        total_calls += len(recs)
        total_cost += sum(r.get("cost_usd", 0.0) for r in recs)
        total_tokens += sum(r.get("tokens", 0) for r in recs)
        for r in recs:
            models[r["model"]] = models.get(r["model"], 0) + 1
    print(
        f"[{arm}] recomputed calls={total_calls} cost=${total_cost:.4f} "
        f"tokens={total_tokens} models={models}"
    )
    print(
        f"       summary says calls={a['total_calls']} "
        f"cost=${a['total_cost_usd']:.4f} tokens={a['total_tokens']} "
        f"models={a['model_mix']}"
    )

# ensemble: aggregate from its sub-task ledgers
print("\n--- ensemble sub-task ledgers ---")
ens_calls = 0
ens_cost = 0.0
ens_models = {}
ens_pt = s["arms"]["ensemble"]["per_task"]
for tid, t in ens_pt.items():
    for sub in t["sub_tasks"]:
        recs = ledger(sub["task_id"])
        ens_calls += len(recs)
        ens_cost += sum(r.get("cost_usd", 0.0) for r in recs)
        for r in recs:
            ens_models[r["model"]] = ens_models.get(r["model"], 0) + 1
print(
    f"[ensemble] recomputed calls={ens_calls} cost=${ens_cost:.4f} models={ens_models}"
)
print(
    f"           summary says calls={s['arms']['ensemble']['total_calls']} "
    f"cost=${s['arms']['ensemble']['total_cost_usd']:.4f} "
    f"models={s['arms']['ensemble']['model_mix']}"
)

print("\n--- hard tasks' ensemble detail ---")
for tid, t in ens_pt.items():
    if t["strategy"] != "single-adaptive":
        print(
            f"{tid}: hint={t['hint']} strategy={t['strategy']} "
            f"status={t['status']} candidate_win={t['candidate_win']}"
        )
        for sub in t["sub_tasks"]:
            print(
                f"    {sub['task_id']}: {sub['status']} "
                f"attempts={sub['attempts']} calls={sub['calls']} "
                f"${sub['cost_usd']}"
            )

print("\n--- ON arm expensive calls (which tasks/why) ---")
for tid, t in s["arms"]["on"]["per_task"].items():
    recs = ledger(tid)
    exp = [r for r in recs if r["model"] == "z-ai/glm-5.3-free"]
    if exp:
        hints = [r.get("difficulty_hint") for r in exp]
        print(
            f"{tid}: {len(exp)} expensive calls, hints={hints}, "
            f"task status={t['status']}"
        )

print("\n--- OFF arm failures ---")
for tid, t in s["arms"]["off"]["per_task"].items():
    if t["status"] != "success":
        recs = ledger(tid)
        print(
            f"{tid}: status={t['status']} ledger_calls={len(recs)} "
            f"attempts={t.get('attempts')}"
        )

print("\n--- three-arm delta block ---")
print(json.dumps(s.get("delta_three_arm", {}), indent=2)[:1200])
