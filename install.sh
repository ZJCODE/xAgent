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

PATH_CONFIGURED=0

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

install_via_uv() {
    install_uv_if_needed
    step "Installing $PACKAGE_NAME via uv using Python $PYTHON_VERSION..."
    mkdir -p "$BINDIR"
    UV_TOOL_BIN_DIR="$BINDIR" uv tool install --force "$PACKAGE_NAME" --python "$PYTHON_VERSION"
    ensure_path
}

upgrade_xagent() {
    step "Checking for updates..."

    install_via_uv
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

    info "Installing isolated CLI tool via uv."
    install_via_uv

    verify_install
    success_message
}

main "$@"
