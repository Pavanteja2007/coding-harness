# Release runbook — cutting 0.3.0

**Every step here is owner-only.** This round prepared the artifacts and the
evidence; it performed no commit, tag, push, upload, or publish. Nothing below
has been executed.

Read [`release-evidence.md`](release-evidence.md) first. It states which
lanes are currently green, which are blocked, and why. This runbook is the
procedure, not the status.

---

## 0. Preconditions — all must hold before step 1

| # | precondition | how to check |
|---|---|---|
| P1 | Every Round 2 lane has landed and its suite is green | `pytest tests/test_ceiling_r2_*.py` |
| P2 | The working tree is clean | `git status --short` prints nothing |
| P3 | R2-18's documentation is merged, including the site release-list fix | `python -m scripts.docs_truth` |
| P4 | SG-01 and SG-02 are closed, **or** accepted in writing in the release note | see `known-issues.md` |
| P5 | The lint ratchet is green, or its debt is written into the release note | `python scripts/lint_ratchet.py` |
| P6 | A live provider credential actually works | see the probe in §5 |

**P2 is the one people skip.** A release built from a dirty tree is not a
release, it is a snapshot of whatever four people were editing at that
moment. Two earlier attempts in this project's history produced artifact
pairs that differed *because a neighbouring file changed between build A and
build B*, and both had to be thrown away.

---

## 1. Verify the tree before building anything

```bash
python -m evals.run --suite prompt-regression --check --json
```

Expect `verdict: CLEAN`, `pass_count: 14`, `fail_count: 0`, `skip_count: 0`.
This needs no Docker and no model. If it is not clean, stop.

Then the real matrix, which does need Docker:

```bash
python -m evals.run --suite prompt-regression --json
```

Expect `verdict: CLEAN` with `regressions: []`. Anything else is a stop.

Then the full suite, **on the quiet tree**:

```bash
python -m pytest -q -p no:randomly
```

Record the exact number, including skips. "It was fine when I ran it" is not a
number.

---

## 2. Build exactly one candidate

Build into **fresh, empty** directories. Never append to an existing `dist/`:
a stale wheel from an earlier build is how a `twine upload dist/*` publishes
the wrong version.

```bash
rm -rf dist build   # only on a clean tree you own
python -m build
```

You should see exactly two artifacts:

```
dist/neo_agent_cli-0.3.0-py3-none-any.whl
dist/neo_agent_cli-0.3.0.tar.gz
```

Anything else in `dist/` means a stale file; delete it and rebuild.

---

## 3. Verify the artifacts

```bash
python -m scripts.verify_release \
  --dist dist \
  --require-clean \
  --require-tag v0.3.0 \
  --sbom dist/sbom.json \
  --checksums dist/SHA256SUMS
```

This checks exact filenames, project metadata, the configured package payload,
generated-bytecode exclusion, SHA-256 hashes, safe archive member names, and
sdist normalization. Exit 0 required.

**The tag must exist before this passes with `--require-tag`.** So the order is:
verify without the tag, tag, then re-verify with it.

```bash
# round 1: no tag requirement yet
python -m scripts/verify_release.py --dist dist --require-clean

# only after the tree is clean and reviewed:
git tag -a v0.3.0 -m "v0.3.0: daily engine by default, honest verification gates"
```

Create exactly one tag: `v0.3.0`.

```bash
python -m scripts/verify_release.py --dist dist --require-clean --require-tag v0.3.0
```

### Reproducibility, on a quiet tree

```bash
python -m build --outdir dist-b
python -m scripts/verify_release.py --dist dist --compare-dist dist-b
```

`reproducible: true` is the required result. **Do not** run the two builds
while anything else is editing the tree — you will measure the race, not
nondeterminism.

---

## 4. The release-evidence aggregate

```bash
python -m scripts.release_evidence --dist dist --report release-evidence.json --json
```

Nine lanes, all blocking:

`source_state`, `reproducibility`, `sbom`, `vulnerability_scan`,
`clean_room_install`, `installed_wheel_flow`, `full_test_suite`,
`docs_truth`, `capability_probe`.

The report exits **2** unless every one is `pass`. A `skipped` lane is not a
pass, and an `unevaluated` lane is not a pass. That is deliberate: the honest
way to run this without a candidate is to be told "NOT RELEASABLE" with a list
of what is missing, rather than to get a green report that quietly skipped the
expensive half.

If a lane cannot be run, say so in the release note. Do not work around it.

---

## 5. Live-provider lane (optional, and honestly labelled)

**This lane did not run for the 0.3.0 candidate**, and the release note says
so. If the owner wants it run, a credential must first be proven to work —
because the failure mode is misleading. A bad key surfaces through litellm as:

```
litellm.InternalServerError: ... [WinError 10061] No connection could be made
```

which reads like a network outage and is not one. Probe the layers separately:

```bash
python - <<'PY'
import json, os, socket, ssl, urllib.request, urllib.error
key = os.environ.get("ANTHROPIC_API_KEY") or ""
raw = socket.create_connection(("api.anthropic.com", 443), timeout=10)
with ssl.create_default_context().wrap_socket(raw, server_hostname="api.anthropic.com"):
    print("tls: OK")
if not key:
    print("api: SKIPPED (no key)"); raise SystemExit
try:
    r = urllib.request.Request("https://api.anthropic.com/v1/messages",
        data=json.dumps({"model": "claude-sonnet-4-5", "max_tokens": 8,
                         "messages": [{"role": "user", "content": "say ok"}]}).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(r, timeout=45) as resp:
        print("api: OK", json.loads(resp.read())["content"][0]["text"])
except urllib.error.HTTPError as exc:
    print("api: HTTP", exc.code, exc.read()[:200].decode("utf-8", "replace"))
PY
```

`401 authentication_error` means the credential, not the network. Anything
other than `OK` means the live-provider lane is **blocked**, and the release
note must say "live-provider lane: blocked", not "verified".

---

## 6. Clean-room install matrix

```bash
python -m scripts.clean_room_matrix \
  --wheel dist/neo_agent_cli-0.3.0-py3-none-any.whl \
  --sdist  dist/neo_agent_cli-0.3.0.tar.gz \
  --python py310=python \
  --python py311=python3.11 \
  --python py312=python3.12 \
  --output-root <an empty directory> \
  --report clean-room-0.3.0.json
```

One fresh venv per Python × artifact lane, with credentials and ambient pip
config stripped from the child environment. `blocked` is never `passed`: a
Docker-unavailable lane reports blocked.

---

## 7. Upload — the only irreversible step

```bash
python -m twine check dist/neo_agent_cli-0.3.0-py3-none-any.whl dist/neo_agent_cli-0.3.0.tar.gz
python -m twine upload dist/neo_agent_cli-0.3.0-py3-none-any.whl dist/neo_agent_cli-0.3.0.tar.gz
```

**Name both files exactly. Never `twine upload dist/*`.** A wildcard will
happily publish the stale 0.2.0 wheel that is sitting in `dist/` right now.

Then verify from outside the checkout, in a clean venv:

```bash
python -m pip install --upgrade neo-agent-cli
neo --version          # must print 0.3.0
```

If `--version` does not print 0.3.0, the upload did not take. Check the PyPI
JSON API; a fresh index cache can serve the old version for a few minutes.

---

## 8. GitHub release

Cut it from the `v0.3.0` tag. Paste the lane table from `CHANGELOG.md`
**unchanged** — including every "BLOCKED" and "not run" row.

A release note that overstates verification is the same defect as a lying UI.
If a lane was skipped, the reader of the release note is entitled to know.

Keep these two numbers in the note, because they are the two that are easy to
overstate:

- **No SWE-bench result is claimed.** See `benchmark.md` for what *is* measured.
- **The real-OSS success rate is 40–60%, not 100%.** The 100% figures are on
  fixture and synthesized tasks. `benchmark.md` §3 has both.

---

## 9. After publishing

- Verify the PyPI page renders the readme and the version.
- Verify `pip install neo-agent-cli` in a fresh venv outside the checkout.
- Record the two artifact SHA-256 values in the changelog.
- Close SG-04 in `known-issues.md`.
