#!/bin/bash
# Control Center - Installation Script for macOS and Linux
# Installs: control-center-server, control-center-agent, control-center CLI

set -e

# ============================================================================
# Configuration
# ============================================================================
REPO="nullvoider07/control-center"
INSTALL_DIR="$HOME/.local/bin"
SERVER_BINARY="control-center-server"
AGENT_BINARY="control-center-agent"
CLI_PACKAGE="control-center"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ============================================================================
# Helper Functions
# ============================================================================
print_header() {
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo -e "${BLUE}   Control Center Installation${NC}"
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
        CURRENT_VERSION=$(control-center --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || echo "unknown")
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
# Detect OS
# ============================================================================
detect_os() {
    OS="$(uname -s)"
    case "${OS}" in
        Linux*)     OS_TYPE="linux";;
        Darwin*)    OS_TYPE="macos";;
        *)          print_error "Unsupported OS: ${OS}"; exit 1;;
    esac
    print_success "Detected OS: ${OS_TYPE}"
}

# ============================================================================
# Detect Architecture
# ============================================================================
detect_arch() {
    ARCH="$(uname -m)"
    case "${ARCH}" in
        x86_64)    ARCH_TYPE="x64";;
        arm64)     ARCH_TYPE="arm64";;
        aarch64)   ARCH_TYPE="arm64";;
        *)         print_error "Unsupported Architecture: ${ARCH}"; exit 1;;
    esac
    print_success "Detected Architecture: ${ARCH_TYPE}"
}

# ============================================================================
# Check Dependencies
# ============================================================================
check_dependencies() {
    print_info "Checking dependencies..."
    
    local missing_deps=()
    
    # Check for curl
    if ! command -v curl &> /dev/null; then
        missing_deps+=("curl")
    fi
    
    # Check for tar
    if ! command -v tar &> /dev/null; then
        missing_deps+=("tar")
    fi
    
    # Check for Python 3
    if ! command -v python3 &> /dev/null; then
        missing_deps+=("python3")
    fi
    
    # Check for pip
    if ! command -v pip3 &> /dev/null && ! python3 -m pip --version &> /dev/null 2>&1; then
        missing_deps+=("python3-pip")
    fi
    
    if [ ${#missing_deps[@]} -ne 0 ]; then
        print_error "Missing required dependencies: ${missing_deps[*]}"
        echo ""
        if [[ "$OS_TYPE" == "linux" ]]; then
            echo "Install with: sudo apt-get install ${missing_deps[*]}"
        else
            echo "Install with: brew install ${missing_deps[*]}"
        fi
        exit 1
    fi
    
    # OS-specific dependencies
    if [[ "$OS_TYPE" == "linux" ]]; then
        if ! command -v xdotool &> /dev/null; then
            print_warning "xdotool not found (required for agent)"
            echo "  Install: sudo apt-get install xdotool"
            echo ""
            read -p "Continue without xdotool? [y/N] " -n 1 -r
            echo ""
            if [[ ! $REPLY =~ ^[Yy]$ ]]; then
                exit 1
            fi
        else
            print_success "xdotool found"
        fi
    elif [[ "$OS_TYPE" == "macos" ]]; then
        if ! command -v cliclick &> /dev/null; then
            print_warning "cliclick not found (required for agent)"
            echo "  Install: brew install cliclick"
            echo ""
            read -p "Continue without cliclick? [y/N] " -n 1 -r
            echo ""
            if [[ ! $REPLY =~ ^[Yy]$ ]]; then
                exit 1
            fi
        else
            print_success "cliclick found"
        fi
    fi
    
    print_success "All required dependencies found"
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

    # Extract version (remove 'v' prefix if present)
    VERSION=${LATEST_TAG#v}
    print_success "Latest version: v${VERSION}"
}

# ============================================================================
# Download Release Package
# ============================================================================
download_release() {
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

# ============================================================================
# Extract and Verify Package
# ============================================================================
extract_package() {
    print_info "Extracting package..."
    TMP_DIR="/tmp/control-center-extract-$$"
    mkdir -p "$TMP_DIR"

    if ! tar -xzf "$TMP_FILE" -C "$TMP_DIR"; then
        print_error "Failed to extract package"
        exit 1
    fi

    # Verify binaries exist
    if [ ! -f "$TMP_DIR/bin/$SERVER_BINARY" ]; then
        print_error "$SERVER_BINARY not found in package"
        ls -la "$TMP_DIR/bin" 2>/dev/null || ls -la "$TMP_DIR"
        exit 1
    fi

    if [ ! -f "$TMP_DIR/bin/$AGENT_BINARY" ]; then
        print_error "$AGENT_BINARY not found in package"
        ls -la "$TMP_DIR/bin" 2>/dev/null || ls -la "$TMP_DIR"
        exit 1
    fi

    # Check for Python wheel
    WHEEL_FILE=$(find "$TMP_DIR/python" -name "control_center-*.whl" 2>/dev/null | head -1)
    if [ -z "$WHEEL_FILE" ]; then
        print_error "Python wheel not found in package"
        ls -la "$TMP_DIR/python" 2>/dev/null || echo "  python/ directory not found"
        exit 1
    fi

    print_success "Package extracted and verified"
}

# ============================================================================
# Install Binaries
# ============================================================================
install_binaries() {
    print_info "Installing binaries to $INSTALL_DIR..."
    mkdir -p "$INSTALL_DIR"

    # Copy binaries
    cp "$TMP_DIR/bin/$SERVER_BINARY" "$INSTALL_DIR/"
    cp "$TMP_DIR/bin/$AGENT_BINARY" "$INSTALL_DIR/"

    # Make executable
    chmod +x "$INSTALL_DIR/$SERVER_BINARY"
    chmod +x "$INSTALL_DIR/$AGENT_BINARY"

    # macOS: Remove quarantine
    if [[ "$OS_TYPE" == "macos" ]]; then
        xattr -d com.apple.quarantine "$INSTALL_DIR/$SERVER_BINARY" 2>/dev/null || true
        xattr -d com.apple.quarantine "$INSTALL_DIR/$AGENT_BINARY" 2>/dev/null || true
    fi

    print_success "Binaries installed"
}

# ============================================================================
# Install Python CLI
# ============================================================================
install_python_cli() {
    print_info "Installing Python CLI..."
    
    # Determine pip command
    PIP_CMD="pip3"
    if ! command -v pip3 &> /dev/null; then
        PIP_CMD="python3 -m pip"
    fi
    
    # Install wheel
    if ! $PIP_CMD install --user "$WHEEL_FILE" --force-reinstall; then
        print_error "Failed to install Python CLI"
        echo "  Try manually: $PIP_CMD install $WHEEL_FILE"
        exit 1
    fi
    
    print_success "Python CLI installed"
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
        print_info "Adding $INSTALL_DIR to PATH in $SHELL_CONFIG..."
        echo "" >> "$SHELL_CONFIG"
        echo "# Control Center" >> "$SHELL_CONFIG"
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_CONFIG"
        PATH_UPDATED=true
    else
        PATH_UPDATED=false
    fi
    
    # Python user bin directory
    PYTHON_USER_BIN="$HOME/.local/bin"
    if [[ ":$PATH:" != *":$PYTHON_USER_BIN:"* ]]; then
        if [ "$PATH_UPDATED" = false ]; then
            echo "" >> "$SHELL_CONFIG"
            echo "# Control Center" >> "$SHELL_CONFIG"
        fi
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_CONFIG"
        PATH_UPDATED=true
    fi
}

# ============================================================================
# Clean Up
# ============================================================================
cleanup() {
    print_info "Cleaning up temporary files..."
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
    echo "  • Server:  $INSTALL_DIR/$SERVER_BINARY"
    echo "  • Agent:   $INSTALL_DIR/$AGENT_BINARY"
    echo "  • CLI:     control-center (Python)"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Quick Start Guide"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "1. Start the server:"
    echo "   control-center-server"
    echo ""
    echo "2. Start the agent (in VM/container):"
    echo "   control-center-agent"
    echo ""
    echo "3. Connect with CLI:"
    echo "   control-center connect --host <server-host> --token <your-token>"
    echo ""
    echo "4. Execute commands:"
    echo "   control-center> 960 540 left"
    echo "   control-center> type Hello World"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Documentation & Help"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "  • Full docs:    https://github.com/$REPO"
    echo "  • CLI help:     control-center --help"
    echo "  • Server help:  control-center-server --help"
    echo "  • Agent help:   control-center-agent --help"
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
    detect_os
    detect_arch
    check_dependencies
    get_latest_release
    download_release
    extract_package
    install_binaries
    install_python_cli
    update_path
    cleanup
    print_success_message
}

# Run main installation
main