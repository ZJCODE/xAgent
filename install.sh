#!/usr/bin/env bash
set -euo pipefail

# Install the current xAgent command as an isolated uv tool.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/ZJCODE/xagent/main/install.sh | bash

PACKAGE_NAME="${XAGENT_PACKAGE:-myxagent}"
COMMAND_NAME="${XAGENT_COMMAND:-xagent}"
PYTHON_VERSION="${XAGENT_PYTHON_VERSION:-3.12}"
BINDIR="${XAGENT_BINDIR:-$HOME/.local/bin}"
PYPI_INDEX="${XAGENT_PYPI_INDEX:-https://pypi.org/simple/}"

if [ -t 1 ]; then
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    CYAN='\033[0;36m'
    NC='\033[0m'
else
    GREEN=''
    YELLOW=''
    CYAN=''
    NC=''
fi

info() { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }

has_command() {
    command -v "$1" >/dev/null 2>&1
}

append_path() {
    local file="$1"
    local line="$2"
    [ -f "$file" ] || touch "$file"
    if ! grep -q "# xAgent PATH" "$file"; then
        {
            echo ""
            echo "# xAgent PATH"
            echo "$line"
        } >> "$file"
        info "Added $BINDIR to PATH in $file"
    fi
}

ensure_path() {
    export PATH="$BINDIR:$PATH"
    if [ "${XAGENT_NO_PATH_MODIFY:-0}" = "1" ]; then
        return
    fi
    case "${SHELL:-}" in
        */fish)
            mkdir -p "$HOME/.config/fish"
            append_path "$HOME/.config/fish/config.fish" "fish_add_path \"$BINDIR\""
            ;;
        */zsh)
            append_path "$HOME/.zshrc" "export PATH=\"$BINDIR:\$PATH\""
            ;;
        *)
            append_path "$HOME/.bashrc" "export PATH=\"$BINDIR:\$PATH\""
            ;;
    esac
}

install_uv() {
    if has_command uv; then
        return
    fi
    has_command curl || {
        echo "curl is required to install uv." >&2
        exit 1
    }
    info "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    has_command uv || {
        echo "uv installation failed." >&2
        exit 1
    }
}

main() {
    echo ""
    echo -e "${CYAN}xAgent installer${NC}"
    echo ""

    install_uv
    mkdir -p "$BINDIR"
    UV_TOOL_BIN_DIR="$BINDIR" uv tool install --force "$PACKAGE_NAME" \
        --python "$PYTHON_VERSION" \
        --default-index "$PYPI_INDEX"
    ensure_path

    if ! has_command "$COMMAND_NAME"; then
        warn "$COMMAND_NAME was installed but is not on PATH in this shell."
        echo "export PATH=\"$BINDIR:\$PATH\""
    fi

    echo ""
    echo -e "${GREEN}xAgent installed successfully.${NC}"
    echo ""
    echo "Desktop:"
    echo "  $COMMAND_NAME setup"
    echo "  $COMMAND_NAME web"
    echo ""
    echo "Headless or SSH:"
    echo "  $COMMAND_NAME setup"
    echo "  $COMMAND_NAME launcher"
    echo ""
    echo "Use direct commands when scripting:"
    echo "  $COMMAND_NAME start"
    echo "  $COMMAND_NAME status"
    echo "  $COMMAND_NAME chat"
    echo ""
    echo "See every command:"
    echo "  $COMMAND_NAME --help"
    echo ""
}

main "$@"
