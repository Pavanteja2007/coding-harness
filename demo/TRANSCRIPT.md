# Neo demo transcript

This is the stable, evidence-labeled transcript for the deterministic demo. The generated `neo-demo-transcript.txt` and `neo-demo.gif` are recording artifacts and may contain machine-specific paths, elapsed times, and commit hashes.

## Offline fix demo

Command:

```text
python demo/run_demo.py
```

Observed result:

```text
STEP 0 — isolated demo workspace (demo/demo-work/)
STEP 1 — neo fix: plan -> edit -> verify (verifier-gated)
task:   demo-fix-mean
run 31 events · 5 model calls · 750 tokens · $0.000500
attempts:     1
target test:  PASS
regression:   PASS
flaky:        False
-    return sum(values)
+    return sum(values) / len(values)
STEP 2 — git-native output + rationale
pristine state -> [fix] commit on a private work repository
STEP 3 — historical routing summaries, when present
STEP 4 — decision memory query + isolated repository graph query
STEP 5 — dashboard/concurrency hint
DEMO COMPLETE
```

The model is `ScriptedDemoModel`; the executor is the explicitly injected local subprocess sandbox fallback. The harness loop, verifier, git output, rationale, memory ingestion, and graph query are real product code. This transcript is deterministic evidence, not Docker or live-provider evidence.

## Offline agent demo

Command:

```text
python demo/agent_demo.py
```

Observed result:

```text
[OK] question
[OK] mention
[OK] plan
[OK] approval
[OK] undo
[OK] resume
[OK] compact
DEMO COMPLETE
```

The agent model is scripted and the demo uses local tools only. It proves the interaction receipts and recovery behavior, not provider quality.

## Real-model variant

Use the commands in [`README.md`](README.md) with Docker and a configured endpoint. Keep the resulting `state.json`, `trace.jsonl`, `git.json`, and `rationale.md` as the evidence for that run. A provider failure is recorded as blocked/failed; it is never replaced with this transcript.
