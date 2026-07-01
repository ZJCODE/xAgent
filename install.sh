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

has_python310() {
    has_command python3 || return 1
    python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
}

ensure_pip() {
    if python3 -m pip --version >/dev/null 2>&1; then
        return 0
    fi

    step "pip not found, trying ensurepip..."
    python3 -m ensurepip --upgrade >/dev/null 2>&1 || true

    if python3 -m pip --version >/dev/null 2>&1; then
        return 0
    fi

    if has_command apt-get; then
        if has_command sudo; then
            step "Installing python3-pip via apt..."
            sudo apt-get update -qq
            sudo apt-get install -y -qq python3-pip
        else
            error "pip is missing and sudo is unavailable. Please install python3-pip manually."
        fi
    elif has_command brew; then
        step "Installing Python via Homebrew..."
        brew install python
    else
        error "Cannot install pip automatically. Please install pip manually and retry."
    fi

    python3 -m pip --version >/dev/null 2>&1 || error "pip installation failed."
}

ensure_path() {
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

    local bashrc="$HOME/.bashrc"
    local zshrc="$HOME/.zshrc"
    local fishrc="$HOME/.config/fish/config.fish"

    if [ -f "$bashrc" ] && ! grep -q "# xAgent PATH" "$bashrc"; then
        {
            echo ""
            echo "# xAgent PATH"
            echo "export PATH=\"$BINDIR:\$PATH\""
        } >> "$bashrc"
    fi

    if [ -f "$zshrc" ] && ! grep -q "# xAgent PATH" "$zshrc"; then
        {
            echo ""
            echo "# xAgent PATH"
            echo "export PATH=\"$BINDIR:\$PATH\""
        } >> "$zshrc"
    fi

    if [ -f "$fishrc" ] && ! grep -q "# xAgent PATH" "$fishrc"; then
        {
            echo ""
            echo "# xAgent PATH"
            echo "fish_add_path \"$BINDIR\""
        } >> "$fishrc"
    fi
}

install_via_pip() {
    ensure_pip
    step "Installing $PACKAGE_NAME via pip..."
    python3 -m pip install --upgrade --user "$PACKAGE_NAME"
    ensure_path
}

install_uv_if_needed() {
    if has_command uv; then
        return 0
    fi

    has_command curl || error "curl is required to install uv."

    step "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"

    has_command uv || error "uv installation failed. Please install Python 3.10+ manually."
}

install_via_uv() {
    install_uv_if_needed
    step "Installing $PACKAGE_NAME via uv using Python $PYTHON_VERSION..."
    uv tool install --force "$PACKAGE_NAME" --python "$PYTHON_VERSION"
    ensure_path
}

upgrade_xagent() {
    step "Checking for updates..."

    if has_command uv && uv tool list 2>/dev/null | grep -q "$PACKAGE_NAME"; then
        uv tool install --force "$PACKAGE_NAME" --python "$PYTHON_VERSION"
    elif has_python310; then
        ensure_pip
        python3 -m pip install --upgrade --user "$PACKAGE_NAME"
    else
        install_via_uv
    fi
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

success_message() {
    echo ""
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}  xAgent installed successfully!${NC}"
    echo ""
    echo -e "  Run   ${CYAN}${COMMAND_NAME}${NC}"
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
}

main() {
    banner

    if has_command "$COMMAND_NAME"; then
        upgrade_xagent
        verify_install
        success_message
        exit 0
    fi

    if has_python310; then
        info "Python 3.10+ detected; installing via pip."
        install_via_pip
    else
        info "Python 3.10+ not found; installing via uv."
        install_via_uv
    fi

    verify_install
    success_message
}

main "$@"