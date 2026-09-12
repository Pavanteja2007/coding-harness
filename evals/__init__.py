"""Internal prompt-regression eval harness (Observability round, Task B).

One discipline: whenever a prompt changes (planner, repair, step
session, self-critique), run `python -m evals.run` BEFORE shipping —
a fixed task set through the REAL harness loop (scripted model, REAL
Docker sandbox/verify), scored on outcomes + loop-integrity checks.
This is the ablation discipline applied to prompt engineering: fixed
tasks, paired arms, honest numbers. See evals/run.py for arms and
evals/tasks.py for the task set; results land under
logs/evals/<run-ts>/eval_report.json.
"""
