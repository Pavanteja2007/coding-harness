"""First-run model onboarding — `neo login` + the no-model hint.

Neo talks to models via litellm (any OpenAI-compatible endpoint):
settings keys model/provider/base_url(=api_base)/api_key, env
NEO_MODEL/NEO_PROVIDER/NEO_BASE_URL/NEO_API_BASE/NEO_API_KEY,
precedence flags > env > local > project > global > legacy.

**The first run does not prompt.** This module used to run a blocking wizard
at session start whenever no model was set. Measured consequence: a user with
no credential could not open the app to read its help, browse its history, or
work offline, and the one question it asked was answered by a live network
probe. :func:`maybe_onboard_repl` is now a single line — the app is fully
usable with no provider at all, and ``/connect`` (or ``neo connect``, both
backed by :mod:`cli.auth`) is the way to add one.

Entry points:
- needs_onboarding(flags) — detection over the effective resolution.
- maybe_onboard_repl(file_config) — session-start hook. NEVER prompts.
- run_repl_wizard(...) — the legacy inline text wizard (also `neo login`).
  Still test-first; `/connect` is the flow that saves first.
- cmd_login / cmd_logout — `neo login [--tier ...]` / `neo logout`.
- format_model_display(...) — `/model` output.
- The `/connect` flow is `cli/auth.py`; the TUI modal that predates it lives
  in cli/tui.py and is being retired by that file's owner.
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

_ENVIRONMENT_CREDENTIAL_NAMES = (
    "NEO_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "AGENTROUTER_API_KEY",
    "OPENROUTER_API_KEY",
    "TOKENROUTER_API_KEY",
    "AZURE_API_KEY",
    "MISTRAL_API_KEY",
    "GROQ_API_KEY",
    "TOGETHER_API_KEY",
    "DEEPSEEK_API_KEY",
    "FIREWORKS_API_KEY",
    "XAI_API_KEY",
    "COHERE_API_KEY",
    "PERPLEXITY_API_KEY",
    "OLLAMA_API_KEY",
)

# Preset (id -> pick). Official kinds use litellm's own default
# endpoint (no base_url saved); router kinds prefill an editable
# base_url and save provider=openai (any OpenAI-compatible endpoint
# works with any model name it serves — normalize_runtime_keys).
PRESETS: Dict[str, Dict[str, Any]] = {
    "openai": {
        "label": "OpenAI (official)",
        "kind": "official",
        "provider": "openai",
        "base_url": None,
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"],
        "key_hint": "sk-... (OpenAI API key)",
    },
    "anthropic": {
        "label": "Anthropic (official)",
        "kind": "official",
        "provider": "anthropic",
        "base_url": None,
        "models": [
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
            "claude-3-haiku-20240307",
        ],
        "key_hint": "sk-ant-... (Anthropic API key)",
    },
    "gemini": {
        "label": "Gemini (official)",
        "kind": "official",
        "provider": "gemini",
        "base_url": None,
        "models": ["gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash"],
        "key_hint": "AIza... (Google AI Studio key)",
    },
    "openrouter": {
        "label": "OpenRouter (router)",
        "kind": "router",
        "provider": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "models": [
            "openai/gpt-4o-mini",
            "anthropic/claude-3.5-sonnet",
            "google/gemini-flash-1.5",
        ],
        "key_hint": "sk-or-... (OpenRouter key)",
        "env_key": "OPENROUTER_API_KEY",
    },
    "tokenrouter": {
        "label": "TokenRouter (router)",
        "kind": "router",
        "provider": "openai",
        "base_url": "https://api.tokenrouter.com/v1",
        "models": ["z-ai/glm-5.3-free", "stepfun-3.7-flash"],
        "key_hint": "TokenRouter API key",
        "env_key": "TOKENROUTER_API_KEY",
    },
    "agentrouter": {
        "label": "AgentRouter (router)",
        "kind": "router",
        "provider": "openai",
        "base_url": "https://agentrouter.org/v1",
        "models": [],
        "key_hint": "AgentRouter API key",
        "env_key": "AGENTROUTER_API_KEY",
    },
    "ollama": {
        "label": "Ollama (local, no key)",
        "kind": "router",
        "provider": "openai",
        "base_url": "http://localhost:11434/v1",
        "models": ["qwen2.5", "llama3.1", "mistral"],
        "key_hint": "",
        "no_key": True,
    },
    "custom": {
        "label": "Custom base_url (any OpenAI-compatible router)",
        "kind": "router",
        "provider": "openai",
        "base_url": "",
        "models": [],
        "key_hint": "router API key",
    },
}

PRESET_ORDER: List[str] = [
    "openai",
    "anthropic",
    "gemini",
    "openrouter",
    "tokenrouter",
    "ollama",
    "custom",
    "agentrouter",
]

SKIP_WORDS = {"/skip", "skip", "/q", "q", "quit", "exit"}


def onboarding_disabled() -> bool:
    """True when the wizard must never trigger (env opt-out)."""
    return os.environ.get("NEO_NO_ONBOARD", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def effective_credentials(
    flags: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Model/auth keys as the run would see them: explicit flags over
    the whole settings chain (files + NEO_* env), then the credential
    connected through `/connect`.

    The overlay is last and only fills absent keys, so `cli/auth.py`'s store
    is a real source of truth for a connected provider without ever
    duplicating a key into a settings file. Assumes flags is a plain
    key->value dict (None values are unset)."""
    from cli.neoconfig import normalize_runtime_keys, resolve_provider_config

    clean = {k: v for k, v in (flags or {}).items() if v is not None}
    resolved = normalize_runtime_keys(resolve_provider_config(clean))
    try:
        from cli.auth import apply_active_credential

        resolved = apply_active_credential(resolved)
    except Exception:
        # An unreadable or absent credential store must never make the
        # effective-config read raise; the settings chain is still valid.
        pass
    return resolved


def _is_local_base(base: Any) -> bool:
    """True when base points at this machine (Ollama-style endpoint)."""
    if not isinstance(base, str) or not base.strip():
        return False
    try:
        host = (urlsplit(base.strip()).hostname or "").lower()
    except ValueError:
        return False
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def credentials_status(
    creds: Dict[str, Any],
) -> Tuple[bool, str]:
    """(usable, reason). A session is usable when a model is set and —
    except for local endpoints — an api_key is set. Never raises."""
    try:
        model = (creds or {}).get("model")
        if not isinstance(model, str) or not model.strip():
            return False, "no model configured"
        base = (creds or {}).get("api_base") or (creds or {}).get("base_url")
        if _is_local_base(base):
            return True, "ok (local endpoint, no key needed)"
        key = (creds or {}).get("api_key")
        if not isinstance(key, str) or not key.strip():
            return False, "no api_key configured"
        return True, "ok"
    except Exception:
        return False, "unreadable credentials"


def needs_onboarding(flags: Optional[Dict[str, Any]] = None) -> bool:
    """True when a wizard offer is due: credentials unusable and not
    opted out. Pure (no I/O beyond reading settings/env)."""
    if onboarding_disabled():
        return False
    ok, _ = credentials_status(effective_credentials(flags))
    return not ok


def prompt_allowed(as_json: bool = False) -> bool:
    """True when an interactive prompt is safe: a TTY on stdin, not
    --json, not opted out. Flag commands consult this before any
    wizard offer (pipes/CI/--json never hang on input)."""
    if as_json or onboarding_disabled():
        return False
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False


def litellm_model(provider: Optional[str], model: str) -> str:
    """Return an explicit LiteLLM provider/model route.

    Router model identifiers may contain their own slash, so the only safe
    test is whether the requested provider prefix is already present.
    """
    model_name = (model or "").strip()
    provider_name = (provider or "").strip()
    if provider_name and not model_name.startswith(f"{provider_name}/"):
        return f"{provider_name}/{model_name}"
    return model_name


def test_credentials(
    provider: Optional[str],
    model: str,
    api_key: str,
    api_base: Optional[str] = None,
    timeout_s: int = 60,
) -> Tuple[bool, str]:
    """One tiny live litellm call. Returns (True, "") when the
    endpoint answers, else (False, short error). Never raises and
    never saves anything (callers save only on True)."""
    try:
        import litellm  # lazy: offline paths work without it
    except ImportError:
        return False, "litellm is not installed (pip install neo-agent-cli)"
    kwargs: Dict[str, Any] = {
        "model": litellm_model(provider, model),
        "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
        "max_tokens": 16,
        "timeout": timeout_s,
    }
    request_key = api_key
    if not request_key and _is_local_base(api_base):
        request_key = "ollama"
    if request_key:
        kwargs["api_key"] = request_key
    if api_base:
        kwargs["api_base"] = api_base
    try:
        resp = litellm.completion(**kwargs)
        try:
            text = (resp.choices[0].message.content or "").strip()
        except Exception:
            text = ""
        if text:
            return True, ""
        return False, "endpoint answered but returned no content (retry)"
    except Exception as exc:  # auth/network/rate-limit: honest message
        from cli.neoconfig import redact_text

        return False, redact_text(str(exc) or type(exc).__name__, [api_key])


def health_check(
    provider: Optional[str],
    model: str,
    api_key: str,
    api_base: Optional[str] = None,
    timeout_s: int = 60,
) -> Dict[str, Any]:
    """Run a bounded credential check and return a non-secret result.

    The result contains ``ok`` and a redacted ``error`` only; it is safe
    for status/JSON surfaces and never includes the API key or endpoint
    credentials.
    """
    from cli.neoconfig import redact_text

    ok, error = test_credentials(provider, model, api_key, api_base, timeout_s)
    return {"ok": bool(ok), "error": None if ok else redact_text(error, [api_key])}


def save_credentials(
    provider: Optional[str],
    model: str,
    api_key: str,
    base_url: Optional[str] = None,
    model_tier: str = "global",
    display_label: Optional[str] = None,
    health_check_enabled: Optional[bool] = None,
    profile_name: Optional[str] = None,
) -> Dict[str, str]:
    """Persist a validated provider profile without exposing secrets.

    Project-tier profiles keep the API key and endpoint in the global
    file, because the project file is committable. Local profiles keep
    all four values in the ignored local file, which is useful for a
    per-repository secret. Official providers clear stale router
    endpoints in the selected writable tier.
    """
    from cli import neoconfig

    if model_tier not in ("global", "project", "local"):
        raise ValueError(f"unknown model tier: {model_tier!r}")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string")
    written: Dict[str, str] = {}
    secret_tier = model_tier if model_tier == "local" else "global"
    endpoint_tier = secret_tier
    if api_key:
        neoconfig.set_tier_key(secret_tier, "api_key", api_key)
        written["api_key"] = secret_tier
    else:
        for tier in dict.fromkeys(("global", secret_tier)):
            try:
                neoconfig.unset_tier_key(tier, "api_key")
            except ValueError:
                pass
    if base_url:
        neoconfig.set_tier_key(endpoint_tier, "base_url", base_url)
        try:
            neoconfig.unset_tier_key(endpoint_tier, "api_base")
        except ValueError:
            pass
        written["base_url"] = endpoint_tier
    else:
        for tier in dict.fromkeys(("global", endpoint_tier, model_tier)):
            for stale in ("base_url", "api_base"):
                try:
                    neoconfig.unset_tier_key(tier, stale)
                except ValueError:
                    pass
    if provider:
        neoconfig.set_tier_key(model_tier, "provider", provider)
        written["provider"] = model_tier
    neoconfig.set_tier_key(model_tier, "model", model)
    written["model"] = model_tier
    if display_label:
        neoconfig.set_tier_key(model_tier, "display_label", display_label)
        written["display_label"] = model_tier
    if health_check_enabled is not None:
        neoconfig.set_tier_key(model_tier, "health_check", bool(health_check_enabled))
        written["health_check"] = model_tier
    if profile_name:
        profile_values: Dict[str, Any] = {
            "provider": provider,
            "model": model,
        }
        if api_key:
            profile_values["api_key"] = api_key
        if base_url:
            profile_values["base_url"] = base_url
        if display_label:
            profile_values["display_label"] = display_label
        if health_check_enabled is not None:
            profile_values["health_check"] = bool(health_check_enabled)
        neoconfig.set_provider_profile(profile_name, profile_values, tier="global")
        neoconfig.select_provider_profile(profile_name, tier=model_tier)
        written["provider_profile"] = model_tier
        written["profile_definition"] = "global"
    return written


def format_model_display(
    state: Optional[Dict[str, Any]] = None,
    file_config: Optional[Dict[str, Any]] = None,
    flags: Optional[Dict[str, Any]] = None,
) -> str:
    """One-line current-model summary with a redacted endpoint."""
    try:
        from cli import neoconfig

        explicit = dict(flags or {})
        for key in (
            "model",
            "provider",
            "api_base",
            "base_url",
            "api_key",
            "profile",
            "provider_profile",
            "display_label",
        ):
            if (state or {}).get(key) is not None:
                explicit[key] = state[key]
        resolved = neoconfig.resolve_provider_config(explicit)
        model = resolved.get("model") or "(none — run /connect or `neo connect`)"
        source = (
            "session"
            if (state or {}).get("model")
            else resolved.get("source_tiers", {}).get("model", "default")
        )
        provider = resolved.get("provider") or ""
        base = resolved.get("api_base") or resolved.get("base_url") or ""
        label = resolved.get("display_label") or resolved.get("label") or ""
        bits = f"model: {model} ({source})"
        if resolved.get("profile"):
            bits += f" · profile: {resolved['profile']}"
        if label:
            bits += f" · {label}"
        if provider:
            bits += f" · provider: {provider}"
        if base:
            bits += f" · base_url: {neoconfig.redact_url(base)}"
        return bits
    except Exception:
        return "model: (unknown)"


def _source_of(key: str, effective: Dict[str, Any]) -> str:
    """Return the display source for a setting without exposing values."""
    try:
        from cli import neoconfig

        return neoconfig.value_source(key, effective=effective)
    except Exception:
        return "default" if key in effective else "unknown"


# ---------------------------------------------------------------------------
# The REPL wizard
# ---------------------------------------------------------------------------


def _ask(
    prompt: str,
    default: str = "",
    mask: bool = False,
    input_fn: Optional[Callable[[str], str]] = None,
    getpass_fn: Optional[Callable[[str], str]] = None,
) -> Optional[str]:
    """One line of input. Returns None on /skip (or EOF)."""
    fn = input_fn or input
    gp = getpass_fn or getpass.getpass
    show = f"{prompt}"
    if default:
        show += f" [{default}]"
    show += ": "
    try:
        raw = (gp(show) if mask else fn(show)).strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if raw.lower() in SKIP_WORDS:
        return None
    return raw or default


def run_repl_wizard(
    model_tier: str = "global",
    input_fn: Optional[Callable[[str], str]] = None,
    getpass_fn: Optional[Callable[[str], str]] = None,
    test_fn: Optional[Callable[..., Tuple[bool, str]]] = None,
    print_fn: Optional[Callable[[str], None]] = None,
    profile_name: Optional[str] = None,
) -> bool:
    """The inline first-run wizard. Pick -> base_url -> model ->
    api_key (masked) -> live TEST -> save. A failed test shows the
    error and offers retry — bad creds are NEVER saved. Returns True
    when credentials were saved, False on skip/abort. The fns are
    injectable for tests (defaults are the real console)."""
    say = print_fn or (lambda s: print(s))
    test = test_fn or test_credentials

    say("No model configured yet — let's add one (once; `neo login` re-runs this).")
    say("  [1] OpenAI (official)      [4] OpenRouter (router)")
    say("  [2] Anthropic (official)   [5] TokenRouter (router)")
    say("  [3] Gemini (official)      [6] Ollama (local, no key)")
    say(
        "                             [7] Custom base_url (any OpenAI-compatible router)"
    )
    say("                             [8] AgentRouter (router)")
    say("Type /skip to skip (neo keeps working offline; model runs will fail).")
    choice = _ask("Pick [1-8]", default="", input_fn=input_fn)
    if choice is None:
        say("Skipped — run `neo login` any time to configure a model.")
        return False
    idx_map = {str(i + 1): pid for i, pid in enumerate(PRESET_ORDER)}
    pid = idx_map.get(choice.strip(), choice.strip().lower())
    if pid not in PRESETS:
        # free-text preset id or model shortcut: fall back to custom
        pid = "custom" if pid not in PRESETS else pid
        if choice.strip().lower() not in PRESETS and choice.strip() not in idx_map:
            say("Unknown pick — using Custom base_url.")
            pid = "custom"
    preset = PRESETS[pid]

    # base_url (prefilled per pick, editable; official = litellm default)
    base_default = str(preset.get("base_url") or "")
    if preset["kind"] == "official":
        base = _ask(
            "base_url (empty = litellm default for " + str(preset["provider"]) + ")",
            default="",
            input_fn=input_fn,
        )
        if base is None:
            say("Skipped — run `neo login` any time to configure a model.")
            return False
        base = base.strip() or None
    else:
        base = _ask("base_url", default=base_default, input_fn=input_fn)
        if base is None:
            say("Skipped — run `neo login` any time to configure a model.")
            return False
        base = base.strip()
        if pid == "custom" and not base:
            say("Custom needs a base_url — skipped (`neo login` to retry).")
            return False
        base = base or None

    # model name (suggest 2-3 per pick + free text)
    models: List[str] = list(preset.get("models") or [])
    if models:
        say("Suggestions: " + ", ".join(models))
    m_default = models[0] if models else ""
    model = _ask("model", default=m_default, input_fn=input_fn)
    if model is None or not model.strip():
        say("No model given — skipped (`neo login` to retry).")
        return False
    model = model.strip()

    # api_key (masked; Ollama-local may be empty)
    no_key_ok = bool(preset.get("no_key")) or _is_local_base(base)
    key_hint = str(preset.get("key_hint") or "api_key")
    while True:
        key = _ask(
            f"api_key ({key_hint})",
            default="",
            mask=True,
            input_fn=input_fn,
            getpass_fn=getpass_fn,
        )
        if key is None:
            say("Skipped — run `neo login` any time to configure a model.")
            return False
        if key.strip() or no_key_ok:
            break
        env_key_name = str(preset.get("env_key") or "")
        env_value = os.environ.get(env_key_name, "") if env_key_name else ""
        if env_value:
            key = env_value
            break
        say("api_key is required for this endpoint (or /skip).")
    api_key = key.strip()

    # TEST with one tiny live call — fail = error + retry, never save
    provider = str(preset.get("provider") or "openai")
    while True:
        say(f"Testing {litellm_model(provider, model)} ...")
        ok, err = test(provider, model, api_key, base)
        if ok:
            break
        # The literal key the user just typed is scrubbed first (this is a
        # secret this process HOLDS, not display sanitisation), then the
        # display pipeline strips escapes and redacts again, so a provider
        # banner carrying an ANSI-split credential cannot reassemble one.
        from cli import ui as _ui
        from cli.neoconfig import redact_text

        say(f"Test failed: {_ui.sanitize_text(redact_text(err, [api_key]))}")
        say("Nothing saved. Check the key / base_url / model and retry.")
        again = _ask("Retry? [Y/n]", default="y", input_fn=input_fn)
        if again is None or again.strip().lower() not in ("y", "yes", ""):
            return False
        # let the user fix the key (the usual failure) before re-testing
        key2 = _ask(
            f"api_key ({key_hint})",
            default="",
            mask=True,
            input_fn=input_fn,
            getpass_fn=getpass_fn,
        )
        if key2 is None:
            return False
        if key2.strip():
            api_key = key2.strip()

    written = save_credentials(
        provider,
        model,
        api_key,
        base,
        model_tier,
        profile_name=profile_name,
    )
    where = ", ".join(f"{k}->{v}" for k, v in written.items())
    say(f"Saved ({where}). Second `neo` starts with no prompt.")
    return True


def maybe_onboard_repl(
    file_config: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Session-start hook for the REPL. It NEVER prompts and never tests.

    This function used to run the whole inline wizard here, which meant the
    app could not be opened without a credential and without a network. The
    measured cost of that was a first-run question whose answer was a
    60-second probe that usually failed. The app is fully usable offline —
    help, history, diff, review — so the entire first-run surface is now one
    line, and the way to add a provider is ``/connect`` when the user wants
    one.

    Returns the (possibly unchanged) file_config. Never raises, and never
    reads stdin, so it is pipe-safe by construction rather than by probe.
    """
    try:
        if not needs_onboarding():
            return file_config
        from cli import auth, ui

        ui.console().print(f"[neo.muted]{auth.markup_safe(auth.first_run_hint())}[/]")
    except Exception:
        pass
    return file_config


# ---------------------------------------------------------------------------
# neo login / neo logout
# ---------------------------------------------------------------------------


def _noninteractive_login(args: Any, tier: str) -> Optional[int]:
    """Handle explicit login fields without reading stdin.

    Returns an exit code when a non-interactive request was detected, or
    ``None`` when the caller should use the interactive wizard.
    """
    from cli import ui

    fields = (
        "provider",
        "model",
        "base_url",
        "api_key",
        "label",
        "profile",
        "no_health_check",
    )
    if not any(getattr(args, field, None) not in (None, "", False) for field in fields):
        return None
    provider = str(getattr(args, "provider", None) or "").strip() or None
    model = str(getattr(args, "model", None) or "").strip()
    base = getattr(args, "base_url", None)
    base = str(base).strip() if base is not None else None
    if not model:
        ui.err_console().print(
            "[neo.error]error: --model is required for non-interactive login[/]"
        )
        return 2
    if not provider and not base:
        ui.err_console().print(
            "[neo.error]error: --provider or --base-url is required for non-interactive login[/]"
        )
        return 2
    if base and not provider:
        provider = "openai"
    key = str(getattr(args, "api_key", None) or "").strip()
    if not key:
        key = os.environ.get("NEO_API_KEY", "")
    if not key:
        from cli.neoconfig import _provider_env_name

        env_name = _provider_env_name(provider, base)
        if env_name:
            key = os.environ.get(env_name, "")
    skip_test = bool(getattr(args, "no_health_check", False))
    if skip_test and not _is_local_base(base):
        ui.err_console().print(
            "[neo.error]error: --no-health-check cannot save an unverified "
            "remote credential[/]"
        )
        return 2
    if not skip_test:
        ok, error = test_credentials(provider, model, key, base)
        if not ok:
            from cli.neoconfig import redact_text

            ui.err_console().print(
                f"[neo.error]error: health check failed: {ui.sanitize_text(redact_text(error, [key]))}[/]"
            )
            return 4
    try:
        written = save_credentials(
            provider,
            model,
            key,
            base,
            tier,
            display_label=getattr(args, "label", None),
            health_check_enabled=not skip_test,
            profile_name=str(getattr(args, "profile", None) or "").strip() or None,
        )
    except (ValueError, OSError) as exc:
        ui.err_console().print(f"[neo.error]error: could not save settings: {exc}[/]")
        return 2
    safe = ", ".join(f"{key_name}->{value}" for key_name, value in written.items())
    ui.console().print(f"[neo.ok]login saved[/] [neo.muted]({safe})[/]")
    return 0


def cmd_login(args: Any) -> int:
    """Run login interactively or validate explicit non-interactive fields."""
    from cli import ui

    tier = getattr(args, "tier", "global") or "global"
    if tier not in ("global", "project", "local"):
        ui.err_console().print(f"[neo.error]error: unknown tier {tier!r}[/]")
        return 2
    noninteractive = _noninteractive_login(args, tier)
    if noninteractive is not None:
        return noninteractive
    try:
        stdin_tty = bool(sys.stdin.isatty())
    except Exception:
        stdin_tty = False
    if not stdin_tty:
        ui.err_console().print(
            "[neo.error]error: `neo login` needs an interactive terminal "
            "or explicit --provider/--model/--base-url/--api-key arguments[/]"
        )
        return 2
    try:
        ok = run_repl_wizard(
            model_tier=tier,
            profile_name=str(getattr(args, "profile", None) or "").strip() or None,
        )
    except (ValueError, OSError) as exc:
        ui.err_console().print(f"[neo.error]error: could not save settings: {exc}[/]")
        return 2
    return 0 if ok else 1


def _environment_credential_names() -> List[str]:
    """Return configured credential variable names without reading values."""
    return [name for name in _ENVIRONMENT_CREDENTIAL_NAMES if name in os.environ]


def logout_result(args: Any = None, start: Optional[Path] = None) -> Dict[str, Any]:
    """Return persisted-removal and environment-only logout state.

    Assumes no environment variable is modified. The result exposes
    ``state`` as persisted_removed, env_only, not_found, or error and
    never includes credential values.
    """
    from cli import neoconfig

    try:
        persisted = neoconfig.remove_persisted_api_keys(start)
    except Exception as exc:
        persisted = {
            "removed": [],
            "removed_paths": [],
            "absent": [],
            "errors": [str(exc)],
        }
    environment_credentials = _environment_credential_names()
    env_active = bool(environment_credentials)
    if persisted.get("errors"):
        state = "error"
    elif persisted.get("removed"):
        state = "persisted_removed"
    elif env_active:
        state = "env_only"
    else:
        state = "not_found"
    persisted["env_active"] = env_active
    persisted["environment_credentials"] = environment_credentials
    persisted["persisted_removed"] = bool(persisted.get("removed"))
    persisted["env_only"] = state == "env_only"
    persisted["state"] = state
    return persisted


def cmd_logout(args: Any) -> int:
    """`neo logout` — remove persisted api_key values and report env-only state."""
    from cli import ui

    result = logout_result(args, start=getattr(args, "start", None))
    con = ui.console()
    errors = list(result.get("errors") or [])
    if errors:
        ui.err_console().print("[neo.error]logout incomplete[/]")
        for error in errors:
            ui.err_console().print(f"[neo.muted]{error}[/]")
    removed = list(result.get("removed") or [])
    if removed:
        con.print(
            "[neo.ok]logged out[/] [neo.muted](persisted api_key removed from "
            + ", ".join(removed)
            + ")[/]"
        )
    else:
        con.print("[neo.muted]no persisted api_key found (nothing to remove)[/]")
    environment_credentials = list(result.get("environment_credentials") or [])
    if environment_credentials:
        con.print(
            "[neo.warn]environment credentials remain active[/] "
            "[neo.muted](" + ", ".join(environment_credentials) + "; a child "
            "process cannot unset the parent environment; unset them in the "
            "parent shell)[/]"
        )
    if errors:
        return 2
    return 0 if removed else 1


def _has_offline_model(task_config: Optional[Dict[str, Any]] = None) -> bool:
    """True when the run answers without credentials: fake/mock harness
    keys, the cross-process scripted-model env hook, or an in-process
    injected model (tests, demos). Best-effort probes, never raises —
    anything unclear means 'not offline' (gate stays honest)."""
    cfg = task_config or {}
    try:
        if cfg.get("use_fake_harness") or cfg.get("use_mock_provider"):
            return True
        if cfg.get("mock_script") or os.environ.get("HARNESS_SCRIPTED_MODEL"):
            return True
        from harness import deps as _hdeps

        if getattr(_hdeps, "_call_model_override", None) is not None:
            return True
    except Exception:
        pass
    return False


def missing_credentials_exit(
    task_config: Optional[Dict[str, Any]] = None, as_json: bool = False
) -> Optional[int]:
    """Flag-command gate: when the effective config has no usable
    model/auth, return exit 4 (model_error) after one honest stderr
    line — never prompt (scriptable paths must not hang on input).
    Offline runs (fake/mock/scripted models) are exempt — they answer
    without credentials. Returns None when credentials are usable (proceed)."""
    if _has_offline_model(task_config):
        return None
    creds = effective_credentials(task_config)
    ok, reason = credentials_status(creds)
    if ok:
        return None
    try:
        from cli import ui

        ui.err_console().print(
            f"[neo.error]error: {reason} — run `neo connect` (or `neo login`) "
            "to configure a model (or set NEO_MODEL/NEO_API_KEY)[/]"
        )
    except Exception:
        pass
    return 4
