"""Real-provider smoke tests (skipped unless a working setup exists).

Verifies the Definition of Done: call_model correctly calls at least two
different providers/models with a user-supplied key, and logs cost/tokens
per call (get_last_usage + JSONL ledger).

Three tiers, each self-gating:
  - Cloud Anthropic: needs ANTHROPIC_API_KEY that is not a placeholder and
    no localhost ANTHROPIC_BASE_URL override.
  - Cloud OpenAI: needs OPENAI_API_KEY.
  - Local Ollama: needs `ollama` on PATH + two pulled models (qwen2.5:0.5b,
    smollm2:360m). Runs fully offline against localhost:11434.

Env note (this machine, 2026-09-07): ANTHROPIC_API_KEY held a Groq-format
placeholder ("gsk_YOUR_...") and ANTHROPIC_BASE_URL pointed at a local
Ollama with no models pulled — so the Ollama tier is the one that actually
exercises two real models here. The cloud tiers self-skip with reasons.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from runtime.model_router import call_model, get_last_usage, set_call_context

_tiny = [{"role": "user", "content": "Reply with exactly: ok"}]

OLLAMA_MODELS = ("qwen2.5:0.5b", "smollm2:360m")


def _placeholder(key: str) -> bool:
    # "gsk_" is a Groq key (wrong provider for the anthropic/openai call);
    # "YOUR_" marks example strings. Real "sk-..." OpenAI keys are valid.
    return (not key) or "YOUR_" in key.upper() or key.startswith("gsk_")


ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_USABLE = (
    not _placeholder(ANTHROPIC_KEY)
    and "localhost" not in os.environ.get("ANTHROPIC_BASE_URL", "")
)
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")


def _ollama_ready() -> bool:
    if not shutil.which("ollama"):
        return False
    try:
        out = subprocess.run(["ollama", "list"], capture_output=True, text=True,
                              timeout=30)
        if out.returncode != 0:
            return False
        return all(m in out.stdout for m in OLLAMA_MODELS)
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.fixture(autouse=True)
def _clean():
    set_call_context(None)
    yield
    set_call_context(None)


class _LedgerAssertions:
    @staticmethod
    def assert_logged(model: str, tmp_path: Path, provider: str = "") -> None:
        usage = get_last_usage()
        assert usage["model"] == model
        assert usage["prompt_tokens"] > 0
        assert usage["tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
        assert "cost_usd" in usage
        lines = (tmp_path / "ledger.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["model"] == model
        assert rec["tokens"] > 0
        if provider:
            assert usage["provider"] == provider


@pytest.mark.skipif(not ANTHROPIC_USABLE,
                    reason="ANTHROPIC_API_KEY is placeholder/absent or base URL is a local override")
class TestAnthropicSmoke(_LedgerAssertions):
    @pytest.mark.parametrize("model", [
        "claude-3-5-haiku-20241022",
        "claude-3-5-sonnet-20241022",
    ])
    def test_two_models_call_and_log(self, model, tmp_path):
        set_call_context({}, ledger_dir=str(tmp_path / "ledger.jsonl"))
        out = call_model(_tiny, provider="anthropic", model=model,
                         api_key=ANTHROPIC_KEY)
        assert isinstance(out, str) and out
        self.assert_logged(model, tmp_path, "anthropic")


@pytest.mark.skipif(_placeholder(OPENAI_KEY), reason="OPENAI_API_KEY not set")
class TestOpenAISmoke(_LedgerAssertions):
    def test_openai_call_and_log(self, tmp_path):
        set_call_context({}, ledger_dir=str(tmp_path / "ledger.jsonl"))
        out = call_model(_tiny, provider="openai", model="gpt-4o-mini",
                         api_key=OPENAI_KEY)
        assert isinstance(out, str) and out
        self.assert_logged("gpt-4o-mini", tmp_path, "openai")


@pytest.mark.skipif(not _ollama_ready(),
                    reason=f"ollama not ready (need models {OLLAMA_MODELS})")
class TestOllamaSmoke(_LedgerAssertions):
    @pytest.mark.parametrize("model", OLLAMA_MODELS)
    def test_local_models_call_and_log(self, model, tmp_path):
        """Two REAL models through litellm's ollama provider — user-supplied
        key not required (local server), tokens/cost still ledgered."""
        set_call_context({}, ledger_dir=str(tmp_path / "ledger.jsonl"))
        out = call_model(_tiny, provider="ollama", model=model)
        assert isinstance(out, str) and out
        self.assert_logged(model, tmp_path, "ollama")
