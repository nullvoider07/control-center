#!/bin/bash
# Control Center - Unified Installation Script
# Installs: Rust binaries (server + agent) + Python CLI
# Following the-eye pattern: One package, one command

set -e

# ============================================================================
# Configuration
# ============================================================================
REPO="nullvoider07/control-center"
INSTALL_DIR="$HOME/.local/bin"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# ============================================================================
# Helper Functions
# ============================================================================
print_header() {
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo -e "${BLUE}   Control Center - Unified Installation${NC}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

print_info() {
    echo -e "${BLUE}ℹ${NC} $1"
}

# ============================================================================
# Check Existing Installation
# ============================================================================
check_existing_installation() {
    if command -v control-center &> /dev/null; then
        print_warning "Control Center is already installed."
        CURRENT_VERSION=$(control-center version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || echo "unknown")
        echo "  Current version: $CURRENT_VERSION"
        echo ""
        read -p "Do you want to reinstall/upgrade? [y/N] " -n 1 -r
        echo ""
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            print_info "Installation cancelled."
            exit 0
        fi
        print_info "Proceeding with installation..."
    fi
}

# ============================================================================
# Detect OS and Architecture
# ============================================================================
detect_platform() {
    OS="$(uname -s)"
    case "${OS}" in
        Linux*)     OS_TYPE="linux";;
        Darwin*)    OS_TYPE="macos";;
        *)          print_error "Unsupported OS: ${OS}"; exit 1;;
    esac
    
    ARCH="$(uname -m)"
    case "${ARCH}" in
        x86_64)    ARCH_TYPE="x64";;
        arm64)     ARCH_TYPE="arm64";;
        aarch64)   ARCH_TYPE="arm64";;
        *)         print_error "Unsupported Architecture: ${ARCH}"; exit 1;;
    esac
    
    print_success "Detected: ${OS_TYPE}-${ARCH_TYPE}"
}

# ============================================================================
# Check Dependencies
# ============================================================================
check_dependencies() {
    print_info "Checking dependencies..."
    
    local missing_deps=()
    
    # Essential tools
    for dep in curl tar python3; do
        if ! command -v $dep &> /dev/null; then
            missing_deps+=("$dep")
        fi
    done
    
    # pip3 (can also use python3 -m pip)
    if ! command -v pip3 &> /dev/null && ! python3 -m pip --version &> /dev/null 2>&1; then
        missing_deps+=("python3-pip")
    fi
    
    if [ ${#missing_deps[@]} -ne 0 ]; then
        print_error "Missing dependencies: ${missing_deps[*]}"
        echo ""
        if [[ "$OS_TYPE" == "linux" ]]; then
            echo "Install with: sudo apt-get install ${missing_deps[*]}"
        else
            echo "Install with: brew install ${missing_deps[*]}"
        fi
        exit 1
    fi
    
    print_success "All dependencies found"
}

# ============================================================================
# Get Latest Release
# ============================================================================
get_latest_release() {
    print_info "Fetching latest version from GitHub..."
    LATEST_TAG=$(curl -s "https://api.github.com/repos/$REPO/releases/latest" | grep '"tag_name":' | sed -E 's/.*"([^"]+)".*/\1/')

    if [ -z "$LATEST_TAG" ]; then
        print_error "Could not find latest release."
        echo "  Check: https://github.com/$REPO/releases"
        exit 1
    fi

    VERSION=${LATEST_TAG#v}
    print_success "Latest version: v${VERSION}"
}

# ============================================================================
# Download and Extract Package
# ============================================================================
download_package() {
    FILE_NAME="control-center-${VERSION}-${OS_TYPE}-${ARCH_TYPE}.tar.gz"
    DOWNLOAD_URL="https://github.com/$REPO/releases/download/$LATEST_TAG/$FILE_NAME"

    print_info "Downloading Control Center v${VERSION}..."
    echo "  URL: $DOWNLOAD_URL"
    
    TMP_FILE="/tmp/control-center-install-$$.tar.gz"

    if ! curl -L -f -o "$TMP_FILE" "$DOWNLOAD_URL" --progress-bar; then
        print_error "Download failed."
        echo ""
        echo "  Please check:"
        echo "    1. Release exists: https://github.com/$REPO/releases/tag/$LATEST_TAG"
        echo "    2. Asset exists: $FILE_NAME"
        echo ""
        exit 1
    fi

    print_success "Downloaded successfully"
}

extract_package() {
    print_info "Extracting package..."
    TMP_DIR="/tmp/control-center-extract-$$"
    mkdir -p "$TMP_DIR"

    if ! tar -xzf "$TMP_FILE" -C "$TMP_DIR"; then
        print_error "Failed to extract package"
        exit 1
    fi

    # Verify contents
    if [ ! -f "$TMP_DIR/bin/control-center-server" ]; then
        print_error "Server binary not found in package"
        ls -la "$TMP_DIR/bin" 2>/dev/null || ls -la "$TMP_DIR"
        exit 1
    fi

    if [ ! -f "$TMP_DIR/bin/control-center-agent" ]; then
        print_error "Agent binary not found in package"
        exit 1
    fi

    WHEEL_FILE=$(find "$TMP_DIR/python" -name "control_center-*.whl" 2>/dev/null | head -1)
    if [ -z "$WHEEL_FILE" ]; then
        print_error "Python wheel not found in package"
        exit 1
    fi

    print_success "Package extracted and verified"
}

# ============================================================================
# Install Components
# ============================================================================
install_rust_binaries() {
    print_info "Installing Rust binaries..."
    mkdir -p "$INSTALL_DIR"

    # Copy binaries
    cp "$TMP_DIR/bin/control-center-server" "$INSTALL_DIR/"
    cp "$TMP_DIR/bin/control-center-agent" "$INSTALL_DIR/"

    # Make executable
    chmod +x "$INSTALL_DIR/control-center-server"
    chmod +x "$INSTALL_DIR/control-center-agent"

    # macOS: Remove quarantine
    if [[ "$OS_TYPE" == "macos" ]]; then
        xattr -d com.apple.quarantine "$INSTALL_DIR/control-center-server" 2>/dev/null || true
        xattr -d com.apple.quarantine "$INSTALL_DIR/control-center-agent" 2>/dev/null || true
    fi

    print_success "Rust binaries installed"
    echo "  • Server: $INSTALL_DIR/control-center-server"
    echo "  • Agent:  $INSTALL_DIR/control-center-agent"
}

install_python_cli() {
    print_info "Installing Python CLI..."
    
    # Determine pip command
    PIP_CMD="pip3"
    if ! command -v pip3 &> /dev/null; then
        PIP_CMD="python3 -m pip"
    fi
    
    # Install wheel
    if ! $PIP_CMD install --user "$WHEEL_FILE" --force-reinstall --no-deps 2>/dev/null; then
        # Try without --no-deps if it fails
        if ! $PIP_CMD install --user "$WHEEL_FILE" --force-reinstall; then
            print_error "Failed to install Python CLI"
            echo "  Try manually: $PIP_CMD install $WHEEL_FILE"
            exit 1
        fi
    fi
    
    print_success "Python CLI installed"
    echo "  • Command: control-center"
}

# ============================================================================
# Update PATH
# ============================================================================
update_path() {
    # Detect shell config file
    SHELL_CONFIG=""
    case "$SHELL" in
        */zsh)  SHELL_CONFIG="$HOME/.zshrc" ;;
        */bash) SHELL_CONFIG="$HOME/.bashrc" ;;
        *)      SHELL_CONFIG="$HOME/.profile" ;;
    esac

    # Check if PATH already includes install dir
    if [[ ":$PATH:" != *":$INSTALL_DIR:"* ]]; then
        print_info "Adding $INSTALL_DIR to PATH..."
        echo "" >> "$SHELL_CONFIG"
        echo "# Control Center" >> "$SHELL_CONFIG"
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_CONFIG"
        PATH_UPDATED=true
    else
        PATH_UPDATED=false
    fi
}

# ============================================================================
# Clean Up
# ============================================================================
cleanup() {
    print_info "Cleaning up..."
    rm -rf "$TMP_FILE" "$TMP_DIR"
}

# ============================================================================
# Print Success Message
# ============================================================================
print_success_message() {
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo -e "${GREEN}✓ Control Center v${VERSION} installed successfully!${NC}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "Installed components:"
    echo "  • Server:  $INSTALL_DIR/control-center-server"
    echo "  • Agent:   $INSTALL_DIR/control-center-agent"
    echo "  • CLI:     control-center (Python)"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Quick Start"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "1. Start the server:"
    echo "   control-center server start"
    echo ""
    echo "2. Start the agent (on VM/container):"
    echo "   control-center agent start"
    echo ""
    echo "3. Connect with CLI:"
    echo "   control-center connect --host <server-ip> --token <your-token>"
    echo ""
    echo "4. Configuration:"
    echo "   control-center config set-token <token>"
    echo "   control-center config set-server <host> <port>"
    echo "   control-center config show"
    echo ""
    echo "5. Execute commands:"
    echo "   # Interactive mode"
    echo "   control-center> 960 540 left"
    echo "   control-center> type Hello World"
    echo ""
    echo "   # Single command"
    echo "   control-center execute -c \"960 540 left\" --host X --token Y"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Help & Documentation"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "  • CLI help:     control-center --help"
    echo "  • Version:      control-center version"
    echo "  • System check: control-center doctor"
    echo "  • Docs:         https://github.com/$REPO"
    echo ""
    
    if [ "$PATH_UPDATED" = true ]; then
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        print_warning "PATH updated - Apply changes:"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo ""
        echo "  source $SHELL_CONFIG"
        echo ""
        echo "Or restart your terminal."
    else
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "Ready to use!"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo ""
        echo "  Try: control-center --help"
    fi
    
    echo ""
}

# ============================================================================
# Main Installation Flow
# ============================================================================
main() {
    print_header
    
    check_existing_installation
    detect_platform
    check_dependencies
    get_latest_release
    download_package
    extract_package
    install_rust_binaries
    install_python_cli
    update_path
    cleanup
    print_success_message
}

# Run main installation
main