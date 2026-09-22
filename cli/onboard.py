"""First-run model onboarding — `vex login` + the no-model wizard.

Vex talks to models via litellm (any OpenAI-compatible endpoint):
settings keys model/provider/base_url(=api_base)/api_key, env
VEX_MODEL/VEX_PROVIDER/VEX_BASE_URL/VEX_API_BASE/VEX_API_KEY,
precedence flags > env > local > project > global > legacy. Before
this module, a fresh machine with no model set got a litellm auth
error mid-run. Now the session offers to add one up front, saves it,
and never asks again (Claude Code's `/login` + OpenCode's wizard,
adapted: free-text base_url + model name, never a hardcoded-only
provider list — any connector/router works).

Entry points:
- needs_onboarding(flags) — detection over the effective resolution.
- maybe_onboard_repl(file_config) — REPL session-start hook (once).
- run_repl_wizard(...) — the inline text wizard (also `vex login`).
- cmd_login / cmd_logout — `vex login [--tier ...]` / `vex logout`.
- format_model_display(...) — `/model` output.
- TUI modal lives in cli/tui.py (_OnboardScreen) and drives the
  PRESETS + test_credentials + save_credentials below (one source).
"""

from __future__ import annotations

import getpass
import os
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

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
    },
    "tokenrouter": {
        "label": "TokenRouter (router)",
        "kind": "router",
        "provider": "openai",
        "base_url": "https://api.tokenrouter.com/v1",
        "models": ["z-ai/glm-5.3-free", "stepfun-3.7-flash"],
        "key_hint": "TokenRouter API key",
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
]

SKIP_WORDS = {"/skip", "skip", "/q", "q", "quit", "exit"}


def onboarding_disabled() -> bool:
    """True when the wizard must never trigger (env opt-out)."""
    return os.environ.get("VEX_NO_ONBOARD", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def effective_credentials(
    flags: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Model/auth keys as the run would see them: explicit flags over
    the whole settings chain (files + VEX_* env). Assumes flags is a
    plain key->value dict (None values are unset)."""
    from cli.vexconfig import apply_config_defaults, normalize_runtime_keys

    clean = {k: v for k, v in (flags or {}).items() if v is not None}
    return normalize_runtime_keys(apply_config_defaults(clean))


def _is_local_base(base: Any) -> bool:
    """True when base points at this machine (Ollama-style local
    endpoint — no api_key required to be usable)."""
    if not isinstance(base, str) or not base.strip():
        return False
    low = base.strip().lower()
    return any(h in low for h in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"))


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
    """The litellm model string: provider/model unless the name
    already carries a provider prefix (router model ids do)."""
    m = (model or "").strip()
    p = (provider or "").strip()
    if p and "/" not in m:
        return f"{p}/{m}"
    return m


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
        return False, "litellm is not installed (pip install vex-harness)"
    kwargs: Dict[str, Any] = {
        "model": litellm_model(provider, model),
        "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
        "max_tokens": 16,
        "timeout": timeout_s,
    }
    if api_key:
        kwargs["api_key"] = api_key
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
        return False, str(exc)[:300] or type(exc).__name__


def save_credentials(
    provider: Optional[str],
    model: str,
    api_key: str,
    base_url: Optional[str] = None,
    model_tier: str = "global",
) -> Dict[str, str]:
    """Persist wizard results. Secrets (api_key) and base_url ALWAYS
    go to the GLOBAL tier (never the project file); the model (+ its
    provider) goes to model_tier ("global" unless --tier project was
    passed). Official presets (base_url None) clear any stale global
    base so a previous router URL can't hijack litellm defaults.
    Returns {key: tier-written}. Raises ValueError/OSError on failure
    (callers report it; nothing partial is hidden — keys are written
    secret-first so a mid-write failure never leaves a keyless model
    pin pointing at a dead endpoint... actually model-last: a crash
    leaves the old model, never a half-migrated one)."""
    from cli import vexconfig

    if model_tier not in ("global", "project"):
        raise ValueError(f"unknown model tier: {model_tier!r}")
    written: Dict[str, str] = {}
    if api_key:
        vexconfig.set_tier_key("global", "api_key", api_key)
        written["api_key"] = "global"
    if base_url:
        vexconfig.set_tier_key("global", "base_url", base_url)
        written["base_url"] = "global"
    else:
        for stale in ("base_url", "api_base"):
            try:
                vexconfig.unset_tier_key("global", stale)
            except ValueError:
                pass
    if provider:
        vexconfig.set_tier_key(model_tier, "provider", provider)
        written["provider"] = model_tier
    vexconfig.set_tier_key(model_tier, "model", model)
    written["model"] = model_tier
    return written


def format_model_display(
    state: Optional[Dict[str, Any]] = None,
    file_config: Optional[Dict[str, Any]] = None,
    flags: Optional[Dict[str, Any]] = None,
) -> str:
    """One-line current-model summary for `/model` (effective value +
    where it came from). Never raises."""
    try:
        from cli import vexconfig

        merged = dict(file_config or {})
        merged.update(vexconfig.env_overrides())
        merged.update({k: v for k, v in (flags or {}).items() if v is not None})
        if state is not None:
            for k in ("model", "provider"):
                if state.get(k):
                    merged[k] = state[k]
        model = merged.get("model") or "(none — run `vex login`)"
        eff = vexconfig.effective_settings()
        src = "session" if (state or {}).get("model") else _source_of("model", eff)
        prov = merged.get("provider") or ""
        base = merged.get("api_base") or merged.get("base_url") or ""
        bits = f"model: {model} ({src})"
        if prov:
            bits += f" · provider: {prov}"
        if base:
            bits += f" · base_url: {base}"
        return bits
    except Exception:
        return "model: (unknown)"


def _source_of(key: str, effective: Dict[str, Any]) -> str:
    """Which tier last set `key` (small local copy of main's helper —
    no import cycle with cli.main)."""
    try:
        from cli import vexconfig

        for var, k in vexconfig._ENV_KEYS.items():
            if k == key and os.environ.get(var):
                return f"env:{var}"
        pd_ = vexconfig.project_settings_dir()
        if pd_ is not None:
            for label, fname in (
                ("project-local", "settings.local.toml"),
                ("project", "settings.toml"),
            ):
                if key in vexconfig.load_vex_config(pd_ / fname):
                    return label
        if key in vexconfig.load_vex_config(vexconfig.global_settings_path()):
            return "global"
        if vexconfig.legacy_settings_path().is_file() and key in (
            vexconfig.load_vex_config(vexconfig.legacy_settings_path())
        ):
            return "legacy"
    except Exception:
        pass
    return "default" if key not in effective else "unknown"


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
) -> bool:
    """The inline first-run wizard. Pick -> base_url -> model ->
    api_key (masked) -> live TEST -> save. A failed test shows the
    error and offers retry — bad creds are NEVER saved. Returns True
    when credentials were saved, False on skip/abort. The fns are
    injectable for tests (defaults are the real console)."""
    say = print_fn or (lambda s: print(s))
    test = test_fn or test_credentials

    say("No model configured yet — let's add one (once; `vex login` re-runs this).")
    say("  [1] OpenAI (official)      [4] OpenRouter (router)")
    say("  [2] Anthropic (official)   [5] TokenRouter (router)")
    say("  [3] Gemini (official)      [6] Ollama (local, no key)")
    say(
        "                             [7] Custom base_url (any OpenAI-compatible router)"
    )
    say("Type /skip to skip (vex keeps working offline; model runs will fail).")
    choice = _ask("Pick [1-7]", default="", input_fn=input_fn)
    if choice is None:
        say("Skipped — run `vex login` any time to configure a model.")
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
            say("Skipped — run `vex login` any time to configure a model.")
            return False
        base = base.strip() or None
    else:
        base = _ask("base_url", default=base_default, input_fn=input_fn)
        if base is None:
            say("Skipped — run `vex login` any time to configure a model.")
            return False
        base = base.strip()
        if pid == "custom" and not base:
            say("Custom needs a base_url — skipped (`vex login` to retry).")
            return False
        base = base or None

    # model name (suggest 2-3 per pick + free text)
    models: List[str] = list(preset.get("models") or [])
    if models:
        say("Suggestions: " + ", ".join(models))
    m_default = models[0] if models else ""
    model = _ask("model", default=m_default, input_fn=input_fn)
    if model is None or not model.strip():
        say("No model given — skipped (`vex login` to retry).")
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
            say("Skipped — run `vex login` any time to configure a model.")
            return False
        if key.strip() or no_key_ok:
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
        say(f"Test failed: {err}")
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

    written = save_credentials(provider, model, api_key, base, model_tier)
    where = ", ".join(f"{k}->{v}" for k, v in written.items())
    say(f"Saved ({where}). Second `vex` starts with no prompt.")
    return True


def maybe_onboard_repl(
    file_config: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Session-start hook for the REPL: if no usable model is set and
    prompts are safe, offer the wizard ONCE inline. Returns the
    (possibly reloaded) file_config. Never raises, never prompts when
    stdin isn't a TTY / opted out (pipe-safe)."""
    try:
        if not needs_onboarding():
            return file_config
        if not prompt_allowed():
            return file_config
        from cli import ui

        con = ui.console()
        con.print(
            "[vex.warn]no model configured[/] [vex.muted]— "
            "vex needs one model endpoint (or VEX_NO_ONBOARD=1 to silence)[/]"
        )
        saved = run_repl_wizard()
        if saved:
            from cli.vexconfig import merged_settings

            return merged_settings()
        return file_config
    except Exception:
        return file_config


# ---------------------------------------------------------------------------
# vex login / vex logout
# ---------------------------------------------------------------------------


def cmd_login(args: Any) -> int:
    """`vex login` — run the wizard on demand (even when creds exist).
    --tier project pins the MODEL to the project file (secrets still
    go global). Pipe-safe: refuses to prompt without a TTY (exit 2)."""
    from cli import ui

    tier = getattr(args, "tier", "global") or "global"
    if tier not in ("global", "project"):
        ui.err_console().print(f"[vex.error]error: unknown tier {tier!r}[/]")
        return 2
    try:
        stdin_tty = bool(sys.stdin.isatty())
    except Exception:
        stdin_tty = False
    if not stdin_tty:
        ui.err_console().print(
            "[vex.error]error: `vex login` needs an interactive terminal "
            "(stdin is not a TTY)[/]"
        )
        return 2
    try:
        ok = run_repl_wizard(model_tier=tier)
    except (ValueError, OSError) as exc:
        ui.err_console().print(f"[vex.error]error: could not save settings: {exc}[/]")
        return 2
    return 0 if ok else 1


def cmd_logout(args: Any) -> int:
    """`vex logout` — strip the stored api_key (global tier). Keeps
    model/base_url so a re-login only asks for the key."""
    from cli import ui, vexconfig

    con = ui.console()
    try:
        outcome, path = vexconfig.unset_tier_key("global", "api_key")
    except (ValueError, OSError) as exc:
        ui.err_console().print(f"[vex.error]error: {exc}[/]")
        return 2
    if outcome == "removed":
        con.print(f"[vex.ok]logged out[/] [vex.muted](api_key removed from {path})[/]")
        return 0
    con.print("[vex.muted]no api_key stored (nothing to remove)[/]")
    return 1


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
    if onboarding_disabled():
        return None
    if _has_offline_model(task_config):
        return None
    creds = effective_credentials(task_config)
    ok, reason = credentials_status(creds)
    if ok:
        return None
    try:
        from cli import ui

        ui.err_console().print(
            f"[vex.error]error: {reason} — run `vex login` to configure "
            "a model (or set VEX_MODEL/VEX_API_KEY)[/]"
        )
    except Exception:
        pass
    return 4
