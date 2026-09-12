"""Quick endpoint health probe (the project convention before any
real-model run): one trivial call through the pinned tier; prints
latency + content presence. Writes probe_logs/atgate-endpoint.json."""

import json
import os
import time
from pathlib import Path

from runtime.ablation import EXPENSIVE_TIER, _tier

tier = _tier(EXPENSIVE_TIER)
from runtime.model_router import set_call_context, call_model

set_call_context(tier, ledger_dir="probe_logs")
set_call_context(
    {
        "provider": tier["provider"],
        "model": tier["model"],
        "api_key": tier["api_key"],
        "api_base": tier["api_base"],
    }
)
t0 = time.time()
out = call_model([{"role": "user", "content": "Reply with exactly: OK"}])
lat = round(time.time() - t0, 1)
rec = {"model": tier["model"], "latency_s": lat, "content": (out or "")[:80]}
Path("probe_logs").mkdir(exist_ok=True)
Path("probe_logs/atgate-endpoint.json").write_text(json.dumps(rec, indent=2))
print(json.dumps(rec, indent=2))
