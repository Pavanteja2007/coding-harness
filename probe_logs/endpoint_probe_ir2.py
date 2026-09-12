"""One-shot endpoint health probe (cheap + expensive tiers), 2026-09-10.

Improvement Round 2 pre-run check: both ablation tiers must answer with
parseable, non-empty content before the three-arm run burns wall clock.
Writes its verdict to probe_logs/endpoint-ir2.json.
"""

import json
import os
import time
from pathlib import Path

import litellm

TIERS = {
    "cheap": {
        "model": "stepfun-3.7-flash",
        "key_env": "NARAROUTER_API_KEY",
        "base": "https://router.bynara.id/v1",
    },
    "expensive": {
        "model": "z-ai/glm-5.3-free",
        "key_env": "TOKENROUTER_API_KEY",
        "base": "https://api.tokenrouter.com/v1",
    },
}

out = {}
for name, t in TIERS.items():
    key = os.environ.get(t["key_env"], "")
    t0 = time.time()
    try:
        r = litellm.completion(
            model=f"openai/{t['model']}",
            messages=[{"role": "user", "content": "Reply with exactly: ok"}],
            api_key=key,
            api_base=t["base"],
            timeout=180,
            max_tokens=2000,
        )
        content = r.choices[0].message.content or ""
        out[name] = {
            "ok": bool(content.strip()),
            "content": content.strip()[:80],
            "elapsed_s": round(time.time() - t0, 1),
        }
    except Exception as exc:  # noqa: BLE001 — probe reports, never raises
        out[name] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}"[:200],
            "elapsed_s": round(time.time() - t0, 1),
        }

Path("probe_logs").mkdir(exist_ok=True)
Path("probe_logs/endpoint-ir2.json").write_text(
    json.dumps(out, indent=2), encoding="utf-8"
)
print(json.dumps(out, indent=2))
raise SystemExit(0 if all(v["ok"] for v in out.values()) else 1)
