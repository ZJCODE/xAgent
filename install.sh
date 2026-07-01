#!/usr/bin/env bash
set -euo pipefail

# xAgent install script
# One command to rule them all:
#   curl -fsSL https://raw.githubusercontent.com/ZJCODE/xagent/main/install.sh | bash

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
step()  { echo -e "${CYAN}[STEP]${NC}  $1"; }

echo ""
echo -e "${CYAN}╔══════════════════════════════════════╗${NC}"
echo -e "${CYAN}║           xAgent Installer           ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════╝${NC}"
echo ""

# --- Check Python 3.10+ ---
has_python310() {
    if command -v python3 &>/dev/null; then
        local ver
        ver=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null) || return 1
        if printf '%s\n' "3.10" "$ver" | sort -V | head -n1 | grep -q "3.10"; then
            return 0
        fi
    fi
    return 1
}

# --- Install via pip ---
install_via_pip() {
    # Ensure pip is available
    if ! command -v pip3 &>/dev/null; then
        step "pip not found, installing..."
        python3 -m ensurepip --upgrade 2>/dev/null || true
        if ! command -v pip3 &>/dev/null; then
            # Try to install pip via common methods
            if command -v apt-get &>/dev/null; then
                sudo apt-get update -qq && sudo apt-get install -y -qq python3-pip
            elif command -v brew &>/dev/null; then
                brew install python@3.12
            else
                error "Cannot install pip. Please install it manually and retry."
            fi
        fi
    fi
    step "Installing xAgent via pip..."
    pip3 install --user myxagent
    ensure_path
}

# --- Install via uv ---
install_via_uv() {
    # Install uv if not present
    if ! command -v uv &>/dev/null; then
        step "Installing uv (standalone Python manager)..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
        if ! command -v uv &>/dev/null; then
            error "uv installation failed. Please install Python 3.10+ manually: https://python.org"
        fi
        info "uv installed"
    fi

    step "Installing xAgent via uv (Python 3.12 will be set up automatically)..."
    uv tool install myxagent --python 3.12
    ensure_path
}

# --- Ensure ~/.local/bin is in PATH ---
ensure_path() {
    local bindir="$HOME/.local/bin"
    if ! echo "$PATH" | tr ':' '\n' | grep -q "$bindir"; then
        step "Adding $bindir to PATH"
        export PATH="$bindir:$PATH"

        # Add to shell profile for persistence
        for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.config/fish/config.fish"; do
            if [ -f "$rc" ]; then
                grep -q "$bindir" "$rc" 2>/dev/null || echo "export PATH=\"$bindir:\$PATH\"" >> "$rc"
            fi
        done
    fi
}

# --- Upgrade ---
upgrade_xagent() {
    step "Checking for updates..."
    if command -v uv &>/dev/null && uv tool list 2>/dev/null | grep -q myxagent; then
        uv tool upgrade myxagent
    else
        pip3 install --upgrade --user myxagent
    fi
}

# --- Main ---
if command -v xagent &>/dev/null; then
    upgrade_xagent
    echo ""
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}  xAgent is up to date!${NC}"
    echo ""
    echo -e "  Run   ${CYAN}xagent${NC}"
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    exit 0
fi

if has_python310; then
    info "Python 3.10+ detected → installing via pip"
    install_via_pip
else
    info "Python 3.10+ not found → installing via uv"
    install_via_uv
fi

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  xAgent installed successfully!${NC}"
echo ""
echo -e "  Run   ${CYAN}xagent${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
