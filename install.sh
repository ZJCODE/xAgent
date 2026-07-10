#!/usr/bin/env bash
set -euo pipefail

# xAgent install script
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/ZJCODE/xagent/main/install.sh | bash

if [ -t 1 ]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    CYAN='\033[0;36m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    CYAN=''
    NC=''
fi

PACKAGE_NAME="${XAGENT_PACKAGE:-myxagent}"
COMMAND_NAME="${XAGENT_COMMAND:-xagent}"
PYTHON_VERSION="${XAGENT_PYTHON_VERSION:-3.12}"
BINDIR="${XAGENT_BINDIR:-$HOME/.local/bin}"
PYPI_INDEX="${XAGENT_PYPI_INDEX:-https://pypi.org/simple/}"
PYPI_JSON_URL="https://pypi.org/pypi/${PACKAGE_NAME}/json"

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
step()  { echo -e "${CYAN}[STEP]${NC}  $*"; }

trap 'echo -e "${RED}[ERROR]${NC} Installation failed at line $LINENO." >&2' ERR

banner() {
    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║           xAgent Installer           ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════╝${NC}"
    echo ""
}

has_command() {
    command -v "$1" >/dev/null 2>&1
}

PATH_CONFIGURED=0
UPGRADED=0
SHOW_RESTART_REMINDER=0

append_path_block() {
    local file="$1"
    local create_if_missing="$2"
    shift 2

    [ -n "$file" ] || return 0
    if [ ! -f "$file" ]; then
        if [ "$create_if_missing" != "1" ]; then
            return 0
        fi
        mkdir -p "$(dirname "$file")"
        touch "$file"
    fi

    if grep -q "# xAgent PATH" "$file"; then
        return 0
    fi

    {
        echo ""
        echo "# xAgent PATH"
        printf '%s\n' "$@"
    } >> "$file"
    PATH_CONFIGURED=1
    info "Added $BINDIR to PATH in $file"
}

ensure_path() {
    PATH_CONFIGURED=0

    case ":$PATH:" in
        *":$BINDIR:"*) ;;
        *)
            step "Adding $BINDIR to PATH for this session"
            export PATH="$BINDIR:$PATH"
            ;;
    esac

    if [ "${XAGENT_NO_PATH_MODIFY:-0}" = "1" ]; then
        warn "Skipping shell profile modification because XAGENT_NO_PATH_MODIFY=1"
        return 0
    fi

    append_path_block "$HOME/.bashrc" 0 "export PATH=\"$BINDIR:\$PATH\""
    append_path_block "$HOME/.bash_profile" 0 "export PATH=\"$BINDIR:\$PATH\""
    append_path_block "$HOME/.profile" 0 "export PATH=\"$BINDIR:\$PATH\""
    append_path_block "$HOME/.zshrc" 0 "export PATH=\"$BINDIR:\$PATH\""
    append_path_block "$HOME/.zprofile" 0 "export PATH=\"$BINDIR:\$PATH\""
    append_path_block "$HOME/.config/fish/config.fish" 0 "fish_add_path \"$BINDIR\""

    if [ "$PATH_CONFIGURED" -eq 0 ]; then
        case "${SHELL:-}" in
            */fish)
                append_path_block "$HOME/.config/fish/config.fish" 1 "fish_add_path \"$BINDIR\""
                ;;
            */zsh)
                append_path_block "$HOME/.zshrc" 1 "export PATH=\"$BINDIR:\$PATH\""
                ;;
            *)
                append_path_block "$HOME/.bashrc" 1 "export PATH=\"$BINDIR:\$PATH\""
                ;;
        esac
    fi
}

install_uv_if_needed() {
    if has_command uv; then
        return 0
    fi

    has_command curl || error "curl is required to install uv."

    step "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"

    has_command uv || error "uv installation failed. Please install uv manually and retry."
}

get_local_version() {
    if ! has_command "$COMMAND_NAME"; then
        echo ""
        return 0
    fi

    ensure_path

    local version=""
    version=$("$COMMAND_NAME" version 2>/dev/null | awk 'NR == 1 { print $2; exit }' || true)
    if [ -n "$version" ]; then
        echo "$version"
        return 0
    fi

    if has_command uv; then
        version=$(uv tool list 2>/dev/null | awk -v pkg="$PACKAGE_NAME" '$1 == pkg { sub(/^v/, "", $2); print $2; exit }' || true)
        if [ -n "$version" ]; then
            echo "$version"
            return 0
        fi
    fi

    echo ""
}

get_remote_version() {
    has_command curl || error "curl is required to check for updates."

    if has_command python3; then
        curl -fsSL "$PYPI_JSON_URL" \
            | python3 -c "import json,sys; print(json.load(sys.stdin)['info']['version'])" \
            || error "Failed to fetch latest version from PyPI."
        return 0
    fi

    curl -fsSL "$PYPI_JSON_URL" \
        | sed -n 's/.*"info"[[:space:]]*:[[:space:]]*{.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
        | head -1 \
        || error "Failed to fetch latest version from PyPI."
}

run_uv_tool_install() {
    local package_spec="$1"

    install_uv_if_needed
    mkdir -p "$BINDIR"
    UV_TOOL_BIN_DIR="$BINDIR" uv tool install --force "$package_spec" \
        --python "$PYTHON_VERSION" \
        --default-index "$PYPI_INDEX"
}

install_via_uv() {
    step "Installing $PACKAGE_NAME via uv using Python $PYTHON_VERSION..."
    run_uv_tool_install "$PACKAGE_NAME"
    ensure_path
}

upgrade_xagent() {
    step "Checking for updates..."

    local local_ver remote_ver new_ver
    local_ver=$(get_local_version)
    remote_ver=$(get_remote_version)

    if [ -z "$remote_ver" ]; then
        error "Could not determine the latest $PACKAGE_NAME version from PyPI."
    fi

    if [ -n "$local_ver" ]; then
        info "Installed: $local_ver | Latest: $remote_ver"
    else
        warn "Could not detect the local version; reinstalling latest ($remote_ver)..."
        run_uv_tool_install "${PACKAGE_NAME}@latest"
        ensure_path
        UPGRADED=1
        return 0
    fi

    if [ "$local_ver" = "$remote_ver" ]; then
        info "Already up to date ($local_ver)."
        ensure_path
        return 0
    fi

    step "Upgrading $PACKAGE_NAME $local_ver → $remote_ver..."
    run_uv_tool_install "${PACKAGE_NAME}@latest"
    ensure_path
    UPGRADED=1

    new_ver=$(get_local_version)
    if [ -n "$new_ver" ] && [ "$new_ver" = "$remote_ver" ]; then
        info "Upgraded to $new_ver."
        return 0
    fi

    if [ -n "$new_ver" ] && [ "$new_ver" != "$local_ver" ]; then
        info "Upgraded to $new_ver (PyPI latest is $remote_ver)."
        warn "Installed version differs from PyPI. Your package index may be out of sync."
        warn "Retry with: XAGENT_PYPI_INDEX=https://pypi.org/simple/ curl -fsSL ... | bash"
        return 0
    fi

    warn "Upgrade finished but the installed version could not be verified."
}

verify_install() {
    ensure_path

    if has_command "$COMMAND_NAME"; then
        info "$COMMAND_NAME is available."
    else
        warn "$PACKAGE_NAME was installed, but '$COMMAND_NAME' is not available on PATH yet."
        warn "Run this command, then try again:"
        echo ""
        echo "  export PATH=\"$BINDIR:\$PATH\""
        echo ""
    fi
}

count_running_processes() {
  local status_json running_count

  if ! has_command "$COMMAND_NAME"; then
    echo 0
    return 0
  fi

  status_json=$("$COMMAND_NAME" processes status --json 2>/dev/null || true)
  if [ -z "$status_json" ]; then
    echo 0
    return 0
  fi

  if has_command python3; then
    running_count=$(printf '%s' "$status_json" | python3 -c "import json,sys; data=json.load(sys.stdin); print(int(data.get('running_count', 0)))" 2>/dev/null || echo 0)
  else
    running_count=$(printf '%s' "$status_json" | sed -n 's/.*"running_count"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -1)
    running_count=${running_count:-0}
  fi

  echo "$running_count"
}

post_upgrade_running_services() {
  local running_count answer

  [ "$UPGRADED" = "1" ] || return 0
  has_command "$COMMAND_NAME" || return 0

  running_count=$(count_running_processes)
  [ "${running_count:-0}" -gt 0 ] || return 0

  echo ""
  echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  warn "xAgent was upgraded while background services are still running."
  echo ""

  if [ "${XAGENT_AUTO_RESTART:-0}" = "1" ]; then
    step "Restarting running services (XAGENT_AUTO_RESTART=1)..."
    "$COMMAND_NAME" processes restart || warn "Some services failed to restart. Run: $COMMAND_NAME processes restart"
    return 0
  fi

  if [ -t 0 ] && [ -t 1 ]; then
    printf "Restart running services now? [y/N] "
    IFS= read -r answer || answer=""
    case "$answer" in
      y|Y|yes|YES)
        step "Restarting running services..."
        "$COMMAND_NAME" processes restart || warn "Some services failed to restart. Run: $COMMAND_NAME processes restart"
        return 0
        ;;
    esac
  fi

  echo -e "  Run: ${CYAN}${COMMAND_NAME} processes restart${NC}"
  echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo ""
  SHOW_RESTART_REMINDER=1
}

success_message() {
    echo ""
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}  xAgent installed successfully!${NC}"
    echo ""
    echo -e "  Get started:"
    echo -e "    ${CYAN}${COMMAND_NAME}${NC}"
    echo -e "      Open xAgent's interactive menu to set up and manage your agent."
    echo ""
    echo -e "  Want a visual interface?"
    echo -e "    ${CYAN}${COMMAND_NAME} web start --open${NC}"
    echo -e "      Start the xAgent Web UI and open it in your browser."
    echo ""
    echo -e "  More commands:"
    echo -e "    ${CYAN}${COMMAND_NAME} --help${NC}"
    if [ "$SHOW_RESTART_REMINDER" = "1" ]; then
        echo ""
        echo -e "  After upgrading, restart running services:"
        echo -e "    ${CYAN}${COMMAND_NAME} processes restart${NC}"
    fi
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
}

main() {
    banner

    if has_command "$COMMAND_NAME"; then
        upgrade_xagent
        verify_install
        post_upgrade_running_services
        success_message
        exit 0
    fi

    info "Installing isolated CLI tool via uv."
    install_via_uv

    verify_install
    success_message
}

main "$@"
