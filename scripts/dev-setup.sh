#!/usr/bin/env bash
# Vex one-command dev environment setup.
#
# Installs dependencies (editable, with dev extras), installs the git
# pre-commit hook (format + lint on commit), and verifies Docker access
# (the sandbox needs it for real runs). Safe to re-run — every step is
# idempotent and skips itself when already satisfied.
#
# Usage:   ./scripts/dev-setup.sh
# Windows: works in Git Bash / WSL; from PowerShell use `make dev` or
#          `bash scripts/dev-setup.sh` if bash is on PATH.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

say()  { printf '\033[0;33m[vex]\033[0m %s\n' "$*"; }
ok()   { printf '\033[0;32m[vex]\033[0m %s\n' "$*"; }
fail() { printf '\033[0;31m[vex]\033[0m %s\n' "$*" >&2; }

# --- 1/5 Python ------------------------------------------------------------
# Preference: $PYTHON override > repo .venv > `python` > `python3`.
# (On Git Bash for Windows, `python` is the Windows install; `python3` can
# resolve to WSL's externally-managed interpreter — hence the order.)
pick_python() {
  if [ -n "${PYTHON:-}" ]; then
    command -v "$PYTHON" >/dev/null 2>&1 && { echo "$PYTHON"; return 0; }
  fi
  if [ -x ".venv/bin/python" ]; then echo ".venv/bin/python"; return 0; fi
  if [ -x ".venv/Scripts/python.exe" ]; then echo ".venv/Scripts/python.exe"; return 0; fi
  command -v python  >/dev/null 2>&1 && { echo python;  return 0; }
  command -v python3 >/dev/null 2>&1 && { echo python3; return 0; }
  return 1
}
if ! PY="$(pick_python)"; then
  fail "Python 3.10+ not found. Install Python (https://python.org) and re-run."
  exit 1
fi
PYVER="$("$PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
say "Python $PYVER at $PY"
"$PY" - <<'EOF'
import sys
if sys.version_info < (3, 10):
    print("error: Python 3.10+ required", file=sys.stderr)
    raise SystemExit(1)
EOF

# --- 2/5 Dependencies (idempotent; auto-venv on PEP 668 systems) -----------
install_deps() {
  "$PY" -m pip install -e ".[dev]" 2>&1 | tee /tmp/vex-dev-setup-pip.log
}
if ! install_deps >/dev/null; then
  if grep -q "externally-managed-environment" /tmp/vex-dev-setup-pip.log 2>/dev/null; then
    say "System Python refuses pip installs (PEP 668) — creating a repo venv (.venv) ..."
    "$PY" -m venv .venv
    if [ -x ".venv/Scripts/python.exe" ]; then PY=".venv/Scripts/python.exe";
    else PY=".venv/bin/python"; fi
    "$PY" -m pip install --quiet --upgrade pip
    install_deps >/dev/null || { fail "pip install failed inside .venv — see output above"; exit 1; }
    say "Re-run this script inside the venv next time, or activate it: source .venv/bin/activate"
  else
    fail "pip install failed — see output above"
    exit 1
  fi
fi
ok "Dependencies installed ($(ruff --version 2>/dev/null | awk '{print $2}' || echo "ruff?"), pytest $("$PY" -m pytest --version 2>/dev/null | head -1 | grep -o '[0-9.]*' || echo '?'))"

# --- 3/5 Pre-commit hook (idempotent) --------------------------------------
say "Installing the git pre-commit hook ..."
# NOTE: the hook file must be named plain `pre-commit` on every platform.
# Git for Windows cannot spawn a hook named `pre-commit.exe` ("Function
# not implemented"), and MSYS `uname` claims MINGW — so no .exe suffix,
# ever; `sh .git/hooks/pre-commit` works on all of them.
HOOK=".git/hooks/pre-commit"
if [ ! -d .git ]; then
  fail "not a git repository — clone the repo first (hook skipped)"
else
  if [ -f "$HOOK" ] && grep -q "vex pre-commit hook" "$HOOK" 2>/dev/null; then
    ok "Pre-commit hook already installed (scripts/hooks/pre-commit)"
  else
    if [ -f "$HOOK" ] && ! grep -q "vex pre-commit hook" "$HOOK" 2>/dev/null; then
      say "Existing custom hook preserved as ${HOOK}.user-*"
      mv "$HOOK" "$HOOK.user-$(date +%s)"
    fi
    cp scripts/hooks/pre-commit "$HOOK"
    chmod +x "$HOOK" 2>/dev/null || true
    ok "Pre-commit hook installed: ruff format + ruff check (ratchet) on every commit"
  fi
fi

# --- 4/5 Docker (verify, don't install) -------------------------------------
say "Verifying Docker access (the sandbox runs commands in containers) ..."
if command -v docker >/dev/null 2>&1; then
  if docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
    DOCKER_VER="$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '?')"
    ok "Docker daemon reachable (server v$DOCKER_VER)"
  else
    fail "Docker CLI found but the daemon is NOT reachable."
    fail "  -> Start Docker Desktop (or 'sudo service docker start' on Linux) and re-run."
    fail "  -> Without Docker, e2e tests and real fix runs cannot execute (unit tests still work)."
    exit 1
  fi
else
  fail "Docker not found. Install it: https://docs.docker.com/get-docker/"
  fail "  -> Without Docker, e2e tests and real fix runs cannot execute (unit tests still work)."
  exit 1
fi

# --- smoke: the CLI should run (5/5) -----------------------------------------
say "Smoke test: vex CLI ..."
# `status` on a nonexistent id exits 2 (usage) — what we test is that the
# CLI imports, parses and renders cleanly, i.e. exit code is 0-2 and NOT
# a traceback (101+ / segfault 139).
set +e
"$PY" -m cli status --task-id __dev_setup_smoke__ >/dev/null 2>&1
CLI_RC=$?
set -e
if [ "$CLI_RC" -le 2 ]; then
  ok "CLI importable and runnable"
else
  fail "CLI failed to run: $PY -m cli (exit $CLI_RC — check the install output above)"
fi

printf '\n'
ok "Dev environment ready. Next steps:"
printf '  %s\n' \
  "run one bug end-to-end (offline, no API key):  python demo/run_demo.py" \
  "run the tests:                                 make test" \
  "lint + format:                                 make lint && make fmt" \
  "full contributor guide:                        CONTRIBUTING.md"
