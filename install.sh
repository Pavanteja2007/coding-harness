#!/usr/bin/env bash

set -euo pipefail

NEO_INSTALL_REPO_DEFAULT="Pavanteja2007/coding-harness"
NEO_INSTALL_REF_DEFAULT="main"
PYPI_SPEC="neo-agent-cli"

REPO="${NEO_INSTALL_REPO:-$NEO_INSTALL_REPO_DEFAULT}"
REF="${NEO_INSTALL_REF:-$NEO_INSTALL_REF_DEFAULT}"
if [ -n "${NEO_INSTALL_SOURCE:-}" ]; then
    SOURCE_URL="$NEO_INSTALL_SOURCE"
    SOURCE_DESC="explicit source: $SOURCE_URL"
elif [ -n "${NEO_INSTALL_REPO:-}" ] || [ -n "${NEO_INSTALL_REF:-}" ]; then
    SOURCE_URL="git+https://github.com/${REPO}.git@${REF}"
    SOURCE_DESC="github.com/${REPO} (${REF})"
else
    SOURCE_URL="$PYPI_SPEC"
    SOURCE_DESC="PyPI ($PYPI_SPEC, latest)"
fi

VENV_DIR="${NEO_VENV_DIR:-$HOME/.neo-venv}"
BIN_DIR="$HOME/.neo/bin"
ROUTE_MARKER_DIR="$HOME/.neo"
ROUTE_MARKER_FILE="$ROUTE_MARKER_DIR/install-route"
PATH_MARKER="NEO_INSTALLER_PATH"

plain() { printf '%s\n' "$*"; }
info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
success() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m==> WARNING:\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m==> ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

normalize_path_entry() {
    local entry="${1-}"
    entry="${entry#"${entry%%[![:space:]]*}"}"
    entry="${entry%"${entry##*[![:space:]]}"}"
    if [ -z "$entry" ]; then
        return 0
    fi
    while [ "$entry" != "/" ] && [ "${entry%/}" != "$entry" ]; do
        entry="${entry%/}"
    done
    printf '%s' "$entry"
}

path_in_list() {
    local wanted="$1"
    shift
    local item normalized
    for item in "$@"; do
        normalized="$(normalize_path_entry "$item")"
        if [ -n "$normalized" ] && [ "$wanted" = "$normalized" ]; then
            return 0
        fi
    done
    return 1
}

build_path() {
    local current="$1"
    local preferred="$2"
    shift 2
    local preferred_norm entry normalized existing duplicate
    local old_ifs
    local -a entries=()
    local -a result=()
    preferred_norm="$(normalize_path_entry "$preferred")"
    if [ -n "$preferred_norm" ]; then
        result+=("$preferred_norm")
    fi
    old_ifs="$IFS"
    IFS=':' read -r -a entries <<< "$current"
    IFS="$old_ifs"
    for entry in "${entries[@]}"; do
        normalized="$(normalize_path_entry "$entry")"
        [ -n "$normalized" ] || continue
        [ "$normalized" != "$preferred_norm" ] || continue
        path_in_list "$normalized" "$@" && continue
        duplicate=0
        for existing in "${result[@]}"; do
            if [ "$existing" = "$normalized" ]; then
                duplicate=1
                break
            fi
        done
        [ "$duplicate" -eq 0 ] || continue
        result+=("$normalized")
    done
    (IFS=':'; printf '%s' "${result[*]}")
}

python_is_compatible() {
    "$1" -c 'import sys; sys.exit(0 if (3, 10) <= sys.version_info < (3, 13) else 1)' >/dev/null 2>&1
}

plain ""
plain "Neo - the AI coding agent for your terminal."
plain "Installing from ${SOURCE_DESC}..."
plain ""

PY=""
for candidate in "${NEO_PYTHON:-}" python3 python; do
    [ -n "$candidate" ] || continue
    if python_is_compatible "$candidate"; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    found="$(python3 --version 2>&1 || echo none)"
    fail "Neo needs Python 3.10-3.12 (found: ${found}).
Install it from https://www.python.org/downloads/ (or your package
manager, e.g. 'sudo apt install python3 python3-venv') and re-run:
  curl -fsSL https://raw.githubusercontent.com/${REPO}/${REF}/install.sh | bash"
fi

PY_DISPLAY="$("$PY" --version 2>&1 || echo 'Python 3.10-3.12')"
info "Found ${PY_DISPLAY}: $PY"

case "$SOURCE_URL" in
    git+*)
        if ! command -v git >/dev/null 2>&1; then
            fail "git is required for git-URL installs (source: ${SOURCE_URL}).
Install it from https://git-scm.com/downloads (or your package
manager, e.g. 'sudo apt install git') and re-run this installer."
        fi
        ;;
    *)
        if ! command -v git >/dev/null 2>&1; then
            warn "git is not installed - fine for PyPI installs, but needed
if you ever pin NEO_INSTALL_REPO/_REF to a git checkout."
        fi
        ;;
esac

if ! command -v docker >/dev/null 2>&1; then
    warn "Docker not found - install it for real bug-fixing (the sandbox
+ verifier). See https://docs.docker.com/get-docker/"
elif ! docker info >/dev/null 2>&1; then
    warn "Docker is installed but the daemon is not reachable - start it
for real bug-fixing (the sandbox + verifier)."
fi

if "$PY" -c 'import sys; sys.exit(0 if sys.platform == "win32" else 1)' >/dev/null 2>&1; then
    fail "Windows Python detected from a Git Bash / MSYS shell.
Use the Windows installers instead:

  PowerShell:  irm https://raw.githubusercontent.com/${REPO}/${REF}/install.ps1 | iex
  CMD:         curl -fsSL https://raw.githubusercontent.com/${REPO}/${REF}/install.cmd -o install.cmd && install.cmd

(or run this script from inside WSL, where Python is Linux-native)."
fi

PIPX_CMD="$(command -v pipx || true)"
if [ "${NEO_FORCE_VENV:-0}" = "1" ]; then
    PIPX_CMD=""
fi
PIPX_HOME_DIR="${PIPX_HOME:-$HOME/.local/pipx}"
PIPX_BIN_DIR_VALUE="${PIPX_BIN_DIR:-$HOME/.local/bin}"
if [ -n "$PIPX_CMD" ]; then
    pipx_query="$("$PIPX_CMD" environment --value PIPX_BIN_DIR 2>/dev/null || true)"
    pipx_query="${pipx_query##*$'\n'}"
    pipx_query="${pipx_query//$'\r'/}"
    if [ -n "$pipx_query" ]; then
        PIPX_BIN_DIR_VALUE="$pipx_query"
    fi
fi
case "$PIPX_BIN_DIR_VALUE" in
    /*) ;;
    "~/"*) PIPX_BIN_DIR_VALUE="$HOME/${PIPX_BIN_DIR_VALUE#\~/}" ;;
    *) PIPX_BIN_DIR_VALUE="$PWD/$PIPX_BIN_DIR_VALUE" ;;
esac

PREVIOUS_ROUTE=""
PREVIOUS_ROUTE_PATH=""
if [ -f "$ROUTE_MARKER_FILE" ]; then
    while IFS='=' read -r marker_key marker_value; do
        case "$marker_key" in
            route) PREVIOUS_ROUTE="$marker_value" ;;
            path) PREVIOUS_ROUTE_PATH="$marker_value" ;;
        esac
    done < "$ROUTE_MARKER_FILE"
fi

SYSTEM_PYTHON="$("$PY" -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
if [ -n "$SYSTEM_PYTHON" ] && "$SYSTEM_PYTHON" -m pip show "$PYPI_SPEC" >/dev/null 2>&1; then
    case "$SYSTEM_PYTHON" in
        "$VENV_DIR"/*|"$PIPX_HOME_DIR"/*) ;;
        *)
            info "Removing the existing Neo pip distribution before migrating to the dedicated install..."
            "$SYSTEM_PYTHON" -m pip uninstall -y "$PYPI_SPEC"
            ;;
    esac
fi

PERSIST_REMOVE_PATHS=("$BIN_DIR" "$VENV_DIR/bin" "$VENV_DIR/Scripts")
STALE_PATHS=("${PERSIST_REMOVE_PATHS[@]}" "$PIPX_BIN_DIR_VALUE")
if [ -n "$PREVIOUS_ROUTE_PATH" ]; then
    previous_normalized="$(normalize_path_entry "$PREVIOUS_ROUTE_PATH")"
    for known_path in "$BIN_DIR" "$VENV_DIR/bin" "$VENV_DIR/Scripts"; do
        known_normalized="$(normalize_path_entry "$known_path")"
        if [ "$previous_normalized" = "$known_normalized" ]; then
            PERSIST_REMOVE_PATHS+=("$PREVIOUS_ROUTE_PATH")
            STALE_PATHS+=("$PREVIOUS_ROUTE_PATH")
            break
        fi
    done
    previous_pipx_normalized="$(normalize_path_entry "$PIPX_BIN_DIR_VALUE")"
    if [ "$previous_normalized" = "$previous_pipx_normalized" ]; then
        STALE_PATHS+=("$PREVIOUS_ROUTE_PATH")
    fi
fi

install_via_pipx() {
    info "Installing with pipx (isolated, keeps your system Python clean)..."
    "$PIPX_CMD" install --force "$SOURCE_URL"
}

install_via_venv() {
    if [ ! -f "$VENV_DIR/bin/python" ]; then
        info "Creating an isolated virtual environment at $VENV_DIR..."
        "$PY" -m venv "$VENV_DIR" || fail "could not create the virtual environment.
On Debian/Ubuntu this usually means the 'python3-venv' package is
missing:  sudo apt install python3-venv  - then re-run this installer."
    else
        info "Reusing the existing virtual environment at $VENV_DIR..."
    fi
    info "Installing Neo (this may take a minute - dependencies build on first install)..."
    "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
    "$VENV_DIR/bin/python" -m pip install --quiet --upgrade "$SOURCE_URL"
}

INSTALLED_WITH=""
if [ -n "$PIPX_CMD" ]; then
    if install_via_pipx; then
        INSTALLED_WITH="pipx"
    else
        warn "pipx install failed - falling back to a dedicated venv."
    fi
fi
if [ -z "$INSTALLED_WITH" ]; then
    install_via_venv
    INSTALLED_WITH="venv"
fi

NEO_BIN=""
HARNESS_BIN=""
if [ "$INSTALLED_WITH" = "pipx" ]; then
    NEO_BIN="$PIPX_BIN_DIR_VALUE/neo"
    HARNESS_BIN="$PIPX_BIN_DIR_VALUE/harness"
else
    NEO_BIN="$VENV_DIR/bin/neo"
    HARNESS_BIN="$VENV_DIR/bin/harness"
    mkdir -p "$BIN_DIR"
    ln -sf "$VENV_DIR/bin/neo" "$BIN_DIR/neo"
    ln -sf "$VENV_DIR/bin/harness" "$BIN_DIR/harness"
    NEO_BIN="$BIN_DIR/neo"
    HARNESS_BIN="$BIN_DIR/harness"
fi

[ -x "$NEO_BIN" ] || fail "installation finished but the 'neo' executable
was not found at the expected location ($NEO_BIN)."
[ -x "$HARNESS_BIN" ] || fail "installation finished but the 'harness' executable
was not found at the expected location ($HARNESS_BIN)."

case "$INSTALLED_WITH" in
    pipx) NEEDS_PATH="$PIPX_BIN_DIR_VALUE" ;;
    venv) NEEDS_PATH="$BIN_DIR" ;;
esac

case "$(basename "${SHELL:-unknown}")" in
    zsh) PROFILE_FILES=("$HOME/.zshrc") ;;
    bash) PROFILE_FILES=("$HOME/.bashrc" "$HOME/.bash_profile") ;;
    fish)
        PROFILE_FILES=("${XDG_CONFIG_HOME:-$HOME/.config}/fish/config.fish")
        ;;
    *) PROFILE_FILES=("$HOME/.profile" "$HOME/.bashrc" "$HOME/.zshrc") ;;
esac

printf -v NEEDS_PATH_Q '%q' "$NEEDS_PATH"
PATH_COMMENT="$(printf '\043 Added by the Neo installer')"
if [ "${PROFILE_FILES[0]}" = "${XDG_CONFIG_HOME:-$HOME/.config}/fish/config.fish" ]; then
    FISH_PATH_Q="'$(printf '%s' "$NEEDS_PATH" | sed "s/'/\\\\'/g")'"
    PATH_LINE="set -gx NEO_INSTALLER_PATH 1; set -gx PATH $FISH_PATH_Q \$PATH"
else
    PATH_LINE="export NEO_INSTALLER_PATH=1; PATH=${NEEDS_PATH_Q}:\$PATH"
fi

update_profile() {
    local file="$1"
    local tmp
    mkdir -p "$(dirname "$file")"
    [ -f "$file" ] || : > "$file"
    if grep -F -x -q "$PATH_LINE" "$file" && \
        [ "$(grep -F -c "$PATH_MARKER" "$file" 2>/dev/null || true)" -eq 1 ] && \
        [ "$(grep -F -c "$PATH_COMMENT" "$file" 2>/dev/null || true)" -eq 0 ] && \
        [ "$(grep -F -c "$PATH_COMMENT"$'\r' "$file" 2>/dev/null || true)" -eq 0 ]; then
        return 0
    fi
    tmp="${file}.neo-install.$$"
    awk -v marker="$PATH_MARKER" -v legacy="$PATH_COMMENT" '
        $0 == legacy || $0 == legacy "\r" { skip = 1; next }
        skip {
            if ($0 ~ /PATH=/ || $0 ~ /set -gx PATH/) { skip = 0; next }
            skip = 0
        }
        index($0, marker) == 0 { print }
    ' "$file" > "$tmp"
    printf '\n%s\n' "$PATH_LINE" >> "$tmp"
    if cmp -s "$file" "$tmp"; then
        rm -f "$tmp"
    else
        cat "$tmp" > "$file"
        rm -f "$tmp"
    fi
}

PERSISTED=0
for profile in "${PROFILE_FILES[@]}"; do
    if update_profile "$profile"; then
        PERSISTED=1
    else
        fail "could not update the shell profile at $profile"
    fi
done

CURRENT_PATH="$(build_path "$PATH" "$NEEDS_PATH" "${STALE_PATHS[@]}")"
export PATH="$CURRENT_PATH"
PATH_OK=0
current_first="${CURRENT_PATH%%:*}"
current_first="$(normalize_path_entry "$current_first")"
needs_first="$(normalize_path_entry "$NEEDS_PATH")"
if [ "$current_first" = "$needs_first" ]; then
    PATH_OK=1
fi

ROUTE_SHELL="${BASH:-$(command -v bash)}"
LOGIN_SHELL="${SHELL:-$ROUTE_SHELL}"
[ -n "$ROUTE_SHELL" ] || fail "could not locate a shell for route verification"
[ -x "$LOGIN_SHELL" ] || fail "configured login shell is unavailable: $LOGIN_SHELL"

resolve_route_command() {
    PATH="$CURRENT_PATH" "$ROUTE_SHELL" --noprofile --norc -c 'type -P "$1"' _ "$1"
}

run_route_command() {
    local name="$1"
    shift
    PATH="$CURRENT_PATH" "$ROUTE_SHELL" --noprofile --norc -c '
        set -eu
        type -P "$1" >/dev/null
        exec "$1" "${@:2}"
    ' neo "$name" "$@"
}

if ! RESOLVED_NEO="$(resolve_route_command neo)"; then
    fail "could not resolve neo through the constructed installation PATH"
fi
if ! RESOLVED_HARNESS="$(resolve_route_command harness)"; then
    fail "could not resolve harness through the constructed installation PATH"
fi
if [ "$(normalize_path_entry "$RESOLVED_NEO")" != "$(normalize_path_entry "$NEO_BIN")" ]; then
    fail "neo resolved to $RESOLVED_NEO instead of the intended $NEO_BIN"
fi
if [ "$(normalize_path_entry "$RESOLVED_HARNESS")" != "$(normalize_path_entry "$HARNESS_BIN")" ]; then
    fail "harness resolved to $RESOLVED_HARNESS instead of the intended $HARNESS_BIN"
fi

if ! NEO_VERSION="$(run_route_command neo --version)"; then
    fail "neo --version failed through the constructed installation PATH"
fi
NEO_VERSION="${NEO_VERSION%%$'\n'*}"
NEO_VERSION="${NEO_VERSION#neo }"
[ -n "$NEO_VERSION" ] || fail "neo --version returned no version"
if ! run_route_command harness --version >/dev/null; then
    fail "harness --version failed through the constructed installation PATH"
fi

FRESH_BASE_PATH="$(build_path "$CURRENT_PATH" '' "${PERSIST_REMOVE_PATHS[@]}" "$NEEDS_PATH")"
FRESH_SHELL_NAME="$(basename "$LOGIN_SHELL")"
case "$FRESH_SHELL_NAME" in
    fish)
        FRESH_SCRIPT='set neo_path (command -s neo); set harness_path (command -s harness); string equal -- "$neo_path" "$NEO_EXPECTED_NEO_BIN" || exit 41; string equal -- "$harness_path" "$NEO_EXPECTED_HARNESS_BIN" || exit 42; neo --version >/dev/null; or exit 43; harness --version >/dev/null; or exit 44'
        FRESH_ARGS=(-lc)
        ;;
    zsh)
        FRESH_SCRIPT='set -e; neo_path="$(command -v neo 2>/dev/null || true)"; harness_path="$(command -v harness 2>/dev/null || true)"; [ "$neo_path" = "$NEO_EXPECTED_NEO_BIN" ]; [ "$harness_path" = "$NEO_EXPECTED_HARNESS_BIN" ]; neo --version >/dev/null; harness --version >/dev/null'
        FRESH_ARGS=(-lic)
        ;;
    *)
        FRESH_SCRIPT='set -e; neo_path="$(command -v neo 2>/dev/null || true)"; harness_path="$(command -v harness 2>/dev/null || true)"; [ "$neo_path" = "$NEO_EXPECTED_NEO_BIN" ]; [ "$harness_path" = "$NEO_EXPECTED_HARNESS_BIN" ]; neo --version >/dev/null; harness --version >/dev/null'
        FRESH_ARGS=(-lc)
        ;;
esac
if ! env -i \
    HOME="$HOME" \
    USERPROFILE="${USERPROFILE:-}" \
    XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}" \
    PATH="$FRESH_BASE_PATH" \
    SHELL="$LOGIN_SHELL" \
    NEO_EXPECTED_NEO_BIN="$NEO_BIN" \
    NEO_EXPECTED_HARNESS_BIN="$HARNESS_BIN" \
    "$LOGIN_SHELL" "${FRESH_ARGS[@]}" -c "$FRESH_SCRIPT" </dev/null; then
    fail "a fresh login shell did not resolve both commands to the intended Neo installation"
fi

if [ "${NEO_SKIP_UPDATE_CHECK:-0}" = "1" ]; then
    warn "skipping neo update --check because NEO_SKIP_UPDATE_CHECK=1"
elif run_route_command neo update --check; then
    :
elif [ "${NEO_REQUIRE_UPDATE_CHECK:-0}" = "1" ]; then
    fail "neo update --check failed through the constructed installation PATH"
else
    warn "neo update --check failed; the verified local installation is still complete"
fi

write_route_marker() {
    local tmp
    mkdir -p "$ROUTE_MARKER_DIR"
    tmp="${ROUTE_MARKER_FILE}.tmp.$$"
    printf 'route=%s\npath=%s\nversion=1\n' "$INSTALLED_WITH" "$NEEDS_PATH" > "$tmp"
    mv -f "$tmp" "$ROUTE_MARKER_FILE"
}
write_route_marker

success "Neo ${NEO_VERSION} installed via ${INSTALLED_WITH}."
plain "  location: $NEO_BIN"
if [ "$PATH_OK" -eq 1 ]; then
    plain "  installer process PATH: intended route first"
else
    warn "the installer process PATH does not have the intended route first"
fi
if [ "$PERSISTED" -eq 1 ]; then
    plain "  fresh login shell: both neo and harness resolve to the installed Neo route"
else
    warn "fresh login shell PATH was not verified"
fi
if [ -n "$PREVIOUS_ROUTE" ] && [ "$PREVIOUS_ROUTE" != "$INSTALLED_WITH" ]; then
    plain "  route transition: $PREVIOUS_ROUTE -> $INSTALLED_WITH"
fi
plain ""
plain "Run neo to get started."
plain "Docs: https://github.com/${REPO}#readme"
plain ""
