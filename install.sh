#!/usr/bin/env bash
# install.sh — Vex installer for macOS, Linux, and WSL.
#
# curl-able one-liner (Task C):
#   curl -fsSL https://raw.githubusercontent.com/Pavanteja2007/coding-harness/main/install.sh | bash
#
# What it does:
#   1. Finds a compatible Python (>= 3.10).
#   2. Installs Vex with pipx if available, else into a dedicated
#      virtual environment (~/.vex-venv) and exposes the `vex` command
#      on PATH via ~/.vex/bin.
#   3. Adds that bin dir to PATH in the user's shell profile when needed.
#   4. Verifies `vex` actually runs and prints the installed version.
#
# Overridable via environment (defaults in CAPS):
#   VEX_INSTALL_REPO    GitHub "owner/repo" slug  (PAVANTEJA2007/CODING-HARNESS)
#   VEX_INSTALL_REF     branch/tag/commit to pin (main)
#   VEX_INSTALL_SOURCE  full pip requirement     (default: built from the two above)
#   VEX_PYTHON          python interpreter to use (auto-detected)
#
# Read more: https://github.com/Pavanteja2007/coding-harness
#
# Safe to re-run; upgrades in place (pipx --force / pip reinstall).

set -euo pipefail

VEX_INSTALL_REPO_DEFAULT="Pavanteja2007/coding-harness"
VEX_INSTALL_REF_DEFAULT="main"

REPO="${VEX_INSTALL_REPO:-$VEX_INSTALL_REPO_DEFAULT}"
REF="${VEX_INSTALL_REF:-$VEX_INSTALL_REF_DEFAULT}"
# VEX_INSTALL_SOURCE: full pip requirement override (testing / mirrors).
if [ -n "${VEX_INSTALL_SOURCE:-}" ]; then
    SOURCE_URL="$VEX_INSTALL_SOURCE"
else
    SOURCE_URL="git+https://github.com/${REPO}.git@${REF}"
fi

VENV_DIR="${VEX_VENV_DIR:-$HOME/.vex-venv}"
BIN_DIR="$HOME/.vex/bin"

# --- output helpers --------------------------------------------------------

plain()   { printf '%s\n' "$*"; }
info()    { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
success() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn()    { printf '\033[1;33m==> WARNING:\033[0m %s\n' "$*" >&2; }
fail()    { printf '\033[1;31m==> ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# --- preflight -------------------------------------------------------------

plain ""
plain "Vex — the AI harness that fixes bugs."
plain "Installing from github.com/${REPO} (${REF})..."
plain ""

# 1. Find a compatible Python (>= 3.10).
#    Order: $VEX_PYTHON if set, then python3, then python. The exit-code
#    probe needs no text parsing, and self-eliminates broken aliases
#    (e.g. the Windows Store "python" stub).

python_is_compatible() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
        >/dev/null 2>&1
}

PY=""
for candidate in "${VEX_PYTHON:-}" python3 python; do
    [ -z "$candidate" ] && continue
    if python_is_compatible "$candidate"; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    found="$(python3 --version 2>&1 || echo none)"
    fail "Vex needs Python 3.10+ (found: ${found}).
Install it from https://www.python.org/downloads/ (or your package
manager, e.g. 'sudo apt install python3 python3-venv') and re-run:
  curl -fsSL https://raw.githubusercontent.com/${REPO}/${REF}/install.sh | bash"
fi

PY_DISPLAY="$("$PY" --version 2>&1 || echo 'Python 3.10+')"
info "Found ${PY_DISPLAY}: $PY"

# 2. Git is required (the install source is a git URL).
if ! command -v git >/dev/null 2>&1; then
    fail "git is required (Vex installs from a GitHub repository).
Install it from https://git-scm.com/downloads (or your package
manager, e.g. 'sudo apt install git') and re-run this installer."
fi

# 3. Windows Python under an MSYS shell (Git Bash / Cygwin): venv layout
#    is Scripts\, not bin/ — the PowerShell/CMD installers are the
#    supported path there. WSL runs real Linux Python and is unaffected.
if "$PY" -c 'import sys; sys.exit(0 if sys.platform == "win32" else 1)' >/dev/null 2>&1; then
    fail "Windows Python detected from a Git Bash / MSYS shell.
Use the Windows installers instead:

  PowerShell:  irm https://raw.githubusercontent.com/${REPO}/${REF}/install.ps1 | iex
  CMD:         curl -fsSL https://raw.githubusercontent.com/${REPO}/${REF}/install.cmd -o install.cmd && install.cmd

(or run this script from inside WSL, where Python is Linux-native)."
fi

# --- install ----------------------------------------------------------------

install_via_pipx() {
    info "Installing with pipx (isolated, keeps your system Python clean)..."
    # --force makes re-runs upgrades instead of "already installed" errors.
    pipx install --force "${SOURCE_URL}"
}

install_via_venv() {
    if [ ! -f "$VENV_DIR/bin/activate" ]; then
        info "Creating an isolated virtual environment at $VENV_DIR..."
        "$PY" -m venv "$VENV_DIR" || fail "could not create the virtual environment.
On Debian/Ubuntu this usually means the 'python3-venv' package is
missing:  sudo apt install python3-venv  — then re-run this installer."
    else
        info "Reusing the existing virtual environment at $VENV_DIR..."
    fi
    info "Installing Vex (this may take a minute — dependencies are built on first install)..."
    # shellcheck disable=SC1091
    . "$VENV_DIR/bin/activate"
    # python -m pip: the vendored venv pip only upgrades itself via the
    # module form (bare bin/pip refuses: "To modify pip, please run
    # ... -m pip install --upgrade pip"). URL requirements re-resolve
    # on every run, so re-runs upgrade naturally.
    python -m pip install --quiet --upgrade pip
    python -m pip install --quiet "${SOURCE_URL}"
    deactivate 2>/dev/null || true
}

INSTALLED_WITH=""
if command -v pipx >/dev/null 2>&1; then
    if install_via_pipx; then
        INSTALLED_WITH="pipx"
    else
        warn "pipx install failed — falling back to a dedicated venv."
    fi
fi
if [ -z "$INSTALLED_WITH" ]; then
    install_via_venv
    INSTALLED_WITH="venv"
fi

# --- expose `vex` on PATH ----------------------------------------------------

VEX_BIN=""

if [ "$INSTALLED_WITH" = "pipx" ]; then
    # pipx stage: $PIPX_LOCAL_VENVS/vex/bin/vex, exposed via $PIPX_BIN_DIR.
    pipx_venvs="$(pipx environment --value PIPX_LOCAL_VENVS 2>/dev/null || true)"
    if [ -n "$pipx_venvs" ] && [ -x "$pipx_venvs/vex/bin/vex" ]; then
        VEX_BIN="$pipx_venvs/vex/bin/vex"
    else
        VEX_BIN="${PIPX_BIN_DIR:-$HOME/.local/bin}/vex"
    fi
else
    VEX_BIN="$VENV_DIR/bin/vex"
fi

[ -x "$VEX_BIN" ] || fail "installation finished but the 'vex' executable
was not found at the expected location ($VEX_BIN).
Please report this: https://github.com/${REPO}/issues"

# venv route: link into ~/.vex/bin so ~/.local/bin stays untouched and the
# uninstall story is "rm -rf ~/.vex-venv ~/.vex".
if [ "$INSTALLED_WITH" = "venv" ]; then
    mkdir -p "$BIN_DIR"
    ln -sf "$VEX_BIN" "$BIN_DIR/vex"
    VEX_BIN="$BIN_DIR/vex"
fi

# Append a PATH hint to the shell profile, idempotently. One line serves
# both routes (~/.vex/bin for venv installs, ~/.local/bin for pipx).
case "$(basename "${SHELL:-unknown}")" in
    zsh)  PROFILE_FILES="$HOME/.zshrc" ;;
    bash)  PROFILE_FILES="$HOME/.bashrc $HOME/.bash_profile" ;;
    fish)  PROFILE_FILES="" ;;            # fish users manage PATH themselves
    *)     PROFILE_FILES="$HOME/.profile $HOME/.bashrc $HOME/.zshrc" ;;
esac

PATH_LINE='export PATH="$HOME/.vex/bin:'"${PIPX_BIN_DIR:-\$HOME/.local/bin}"':$PATH"'

add_path_hint() {
    local f="$1"
    [ -f "$f" ] || touch "$f"
    grep -qF '.vex/bin' "$f" 2>/dev/null || \
        { printf '\n# Added by the Vex installer\n%s\n' "$PATH_LINE" >>"$f"; }
}

case "$INSTALLED_WITH" in
    pipx) NEEDS_PATH="${PIPX_BIN_DIR:-$HOME/.local/bin}" ;;
    venv) NEEDS_PATH="$BIN_DIR" ;;
esac

case ":$PATH:" in
    *":$NEEDS_PATH:"*) PATH_OK=1 ;;
    *) PATH_OK=0 ;;
esac

if [ "$PATH_OK" -eq 0 ]; then
    for f in $PROFILE_FILES; do
        add_path_hint "$f"
    done
fi

# For the CURRENT shell (curl|bash users), make `vex` runnable right away.
export PATH="$NEEDS_PATH:$PATH"

# --- verify + success banner ------------------------------------------------

VEX_VERSION="$("$VEX_BIN" --version 2>/dev/null || echo unknown)"
# `vex --version` prints "vex 0.1.0"; the banner adds its own prefix.
VEX_VERSION="${VEX_VERSION#vex }"
if [ "$VEX_VERSION" = "unknown" ]; then
    warn "installed, but 'vex --version' did not run cleanly.
The install itself is fine — try it:  vex --version"
fi

success "Vex ${VEX_VERSION} installed via ${INSTALLED_WITH}."
plain   "  location: $VEX_BIN"
if [ "$PATH_OK" -eq 0 ]; then
    warn "The install location is not on your PATH for FUTURE shells."
    plain "  Added a PATH line to: $(echo $PROFILE_FILES | tr '\n' ' ')"
    plain "  Start a new shell, or run:"
    plain "    export PATH=\"$NEEDS_PATH:\$PATH\""
else
    plain "  on PATH: yes"
fi
plain ""
plain "Run \`vex\` to get started."
plain "Docs: https://github.com/${REPO}#readme"
plain ""
