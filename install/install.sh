#!/bin/bash
# Control Center - Unified Installation Script
# Installs: Rust binaries (server + agent + generate-token) + Python CLI
# Following the-eye pattern: One package, one command

set -e

# ============================================================================
# Configuration
# ============================================================================
REPO="nullvoider07/control-center"
INSTALL_DIR="$HOME/.local/bin"
SERVER_BINARY="control-center-server"
AGENT_BINARY="control-center-agent"
CLI_BINARY="control-center"
TOKEN_BINARY="generate-token"
# Linux-only, and only in releases from 2.0.0. The agent spawns it by name for
# Wayland actuation and for the position readback, so on a Wayland session the
# whole backend fails without it.
WAYLAND_BINARY="cc-wayland-actuate"

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
    
    # Check for curl
    if ! command -v curl &> /dev/null; then
        missing_deps+=("curl")
    fi
    
    # Check for tar
    if ! command -v tar &> /dev/null; then
        missing_deps+=("tar")
    fi

    if [ ${#missing_deps[@]} -ne 0 ]; then
        print_error "Missing dependencies: ${missing_deps[*]}"
        exit 1
    fi

    print_success "All dependencies found"
}

# ============================================================================
# Get Latest Release
# ============================================================================
get_latest_release() {
    print_info "Fetching latest version from GitHub..."

    # An anonymous GitHub API request is charged against a 60/hour quota keyed on the
    # exit IP, shared with every other client leaving through it — behind a VPN or
    # carrier NAT, strangers spend it. A token moves the quota onto the account, so
    # the lookup stops depending on the network path.
    local auth=()
    local token="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
    # `gh auth login` stores its token in the OS keyring, not in the environment, so
    # a logged-in `gh` did nothing for the request below — the two never met. That
    # produced the worst version of this failure: the operator is authenticated to
    # GitHub, is told the quota is exhausted, logs out and back in, and nothing
    # changes, because the only thing that would have helped is an environment
    # variable nobody mentioned. Ask gh for the token it already holds instead.
    if [ -z "$token" ] && command -v gh &> /dev/null; then
        token=$(gh auth token 2>/dev/null || true)
    fi
    if [ -n "$token" ]; then
        auth=(-H "Authorization: Bearer $token")
    fi

    local body status
    body=$(mktemp)
    status=$(curl -sS -o "$body" -w '%{http_code}' \
        -H "Accept: application/vnd.github+json" \
        -H "User-Agent: control-center-install" \
        "${auth[@]}" \
        "https://api.github.com/repos/$REPO/releases/latest") || status="000"

    if [ "$status" = "403" ] || [ "$status" = "429" ]; then
        rm -f "$body"
        print_error "GitHub API quota exhausted (HTTP $status)."
        if [ -z "$token" ]; then
            echo "  The anonymous quota is 60/hour and is keyed on your public IP, which"
            echo "  a VPN or carrier NAT shares with everyone else behind it."
            if command -v gh &> /dev/null; then
                echo "  gh is installed but holds no token — run 'gh auth login', or set"
                echo "  GITHUB_TOKEN, to raise the limit to 5000/hour on your account."
            else
                echo "  Set GITHUB_TOKEN to raise it to 5000/hour tied to your account."
            fi
        else
            # A token was sent and still refused: not the anonymous bucket, so the
            # advice above would be wrong. This is the account's own limit or a
            # secondary limit, and waiting is the only fix.
            echo "  A token was used, so this is your account's own limit rather than"
            echo "  the shared anonymous one. Retry after the reset."
        fi
        echo "  Release downloads are not rate limited, so you can also install a"
        echo "  specific version by hand: https://github.com/$REPO/releases"
        exit 1
    fi

    if [ "$status" != "200" ]; then
        rm -f "$body"
        print_error "GitHub returned HTTP $status while looking up the latest release."
        echo "  Check: https://github.com/$REPO/releases"
        exit 1
    fi

    # The response is a single line, so a greedy `.*"([^"]+)".*` captures the last
    # quoted string in the whole document — the tail of the release notes — rather
    # than the tag. Extract the field itself, then take its value.
    LATEST_TAG=$(grep -o '"tag_name"[[:space:]]*:[[:space:]]*"[^"]*"' "$body" \
        | head -n1 | sed -E 's/.*"([^"]*)"$/\1/')
    rm -f "$body"

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

    verify_package
}

# Check the download against the digest the release publishes. TLS to GitHub is
# otherwise the only integrity control on a payload this script unpacks and puts on
# PATH. Missing or mismatched digests abort rather than warn: a release that cannot
# be verified is not one to install unattended.
verify_package() {
    local sums_url="https://github.com/$REPO/releases/download/$LATEST_TAG/SHA256SUMS"
    local sums_file="/tmp/control-center-sums-$$"

    print_info "Verifying checksum..."

    if ! curl -sSL -f -o "$sums_file" "$sums_url"; then
        rm -f "$sums_file" "$TMP_FILE"
        print_error "Release $LATEST_TAG publishes no SHA256SUMS; cannot verify the download."
        echo ""
        echo "  Releases before v1.2.1 predate checksum publishing. Install one by"
        echo "  downloading and checking it yourself:"
        echo "    https://github.com/$REPO/releases/tag/$LATEST_TAG"
        echo ""
        exit 1
    fi

    local expected
    expected=$(awk -v name="$FILE_NAME" '$2 == name || $2 == "*" name {print $1; exit}' "$sums_file")
    if [ -z "$expected" ]; then
        rm -f "$sums_file" "$TMP_FILE"
        print_error "SHA256SUMS lists no digest for $FILE_NAME"
        exit 1
    fi

    local actual
    if command -v sha256sum &> /dev/null; then
        actual=$(sha256sum "$TMP_FILE" | awk '{print $1}')
    elif command -v shasum &> /dev/null; then
        actual=$(shasum -a 256 "$TMP_FILE" | awk '{print $1}')
    else
        rm -f "$sums_file" "$TMP_FILE"
        print_error "Neither sha256sum nor shasum is available; cannot verify the download."
        exit 1
    fi

    rm -f "$sums_file"

    if [ "$actual" != "$expected" ]; then
        rm -f "$TMP_FILE"
        print_error "Checksum mismatch for $FILE_NAME — refusing to install."
        echo "    expected $expected"
        echo "    got      $actual"
        exit 1
    fi

    print_success "Checksum verified"
}

extract_package() {
    print_info "Extracting package..."
    TMP_DIR="/tmp/control-center-extract-$$"
    mkdir -p "$TMP_DIR"

    if ! tar -xzf "$TMP_FILE" -C "$TMP_DIR"; then
        print_error "Failed to extract package"
        exit 1
    fi

    # Verify all binaries exist
    if [ ! -f "$TMP_DIR/bin/$SERVER_BINARY" ]; then
        print_error "$SERVER_BINARY not found in package"
        exit 1
    fi

    if [ ! -f "$TMP_DIR/bin/$AGENT_BINARY" ]; then
        print_error "$AGENT_BINARY not found in package"
        exit 1
    fi

    if [ ! -f "$TMP_DIR/bin/$CLI_BINARY" ]; then
        print_error "$CLI_BINARY not found in package"
        ls -la "$TMP_DIR/bin" 2>/dev/null
        exit 1
    fi

    if [ ! -f "$TMP_DIR/bin/$TOKEN_BINARY" ]; then
        print_error "$TOKEN_BINARY not found in package"
        ls -la "$TMP_DIR/bin" 2>/dev/null
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
    cp "$TMP_DIR/bin/$SERVER_BINARY" "$INSTALL_DIR/"
    cp "$TMP_DIR/bin/$AGENT_BINARY" "$INSTALL_DIR/"
    cp "$TMP_DIR/bin/$TOKEN_BINARY" "$INSTALL_DIR/"

    # Make executable
    chmod +x "$INSTALL_DIR/$SERVER_BINARY"
    chmod +x "$INSTALL_DIR/$AGENT_BINARY"
    chmod +x "$INSTALL_DIR/$TOKEN_BINARY"

    # macOS: Remove quarantine
    if [[ "$OS_TYPE" == "macos" ]]; then
        xattr -d com.apple.quarantine "$INSTALL_DIR/$SERVER_BINARY" 2>/dev/null || true
        xattr -d com.apple.quarantine "$INSTALL_DIR/$AGENT_BINARY" 2>/dev/null || true
        xattr -d com.apple.quarantine "$INSTALL_DIR/$TOKEN_BINARY" 2>/dev/null || true
    fi

    # The Wayland actuation helper, when the archive carries one. Absent from
    # releases before 2.0.0 and from the macOS and Windows archives, so a missing
    # file is a normal outcome rather than a failure.
    #
    # Removed before it is copied, unlike the binaries above. `cp` onto a path that
    # is a symlink follows it and writes through to the target, and a developer
    # machine may well have this name pointing into a source checkout — in which
    # case a plain `cp` would overwrite a source file somewhere else entirely
    # instead of installing anything here.
    if [ -f "$TMP_DIR/bin/$WAYLAND_BINARY" ]; then
        rm -f "$INSTALL_DIR/$WAYLAND_BINARY"
        cp "$TMP_DIR/bin/$WAYLAND_BINARY" "$INSTALL_DIR/"
        chmod +x "$INSTALL_DIR/$WAYLAND_BINARY"
    fi

    print_success "Rust binaries installed"
    echo "  • Server:         $INSTALL_DIR/$SERVER_BINARY"
    echo "  • Agent:          $INSTALL_DIR/$AGENT_BINARY"
    echo "  • Token generator:$INSTALL_DIR/$TOKEN_BINARY"
    if [ -f "$INSTALL_DIR/$WAYLAND_BINARY" ]; then
        echo "  • Wayland helper: $INSTALL_DIR/$WAYLAND_BINARY"
    fi
}

install_cli_binary() {
    print_info "Installing CLI binary..."
    
    # Copy the PyInstaller-built binary
    cp "$TMP_DIR/bin/$CLI_BINARY" "$INSTALL_DIR/"
    chmod +x "$INSTALL_DIR/$CLI_BINARY"
    
    if [[ "$OS_TYPE" == "macos" ]]; then
        xattr -d com.apple.quarantine "$INSTALL_DIR/$CLI_BINARY" 2>/dev/null || true
    fi
    
    print_success "CLI binary installed"
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
    echo "  • Server:          $INSTALL_DIR/$SERVER_BINARY"
    echo "  • Agent:           $INSTALL_DIR/$AGENT_BINARY"
    echo "  • CLI:             $INSTALL_DIR/$CLI_BINARY"
    echo "  • Token generator: $INSTALL_DIR/$TOKEN_BINARY"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Quick Start"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "1. Generate an auth token:"
    echo "   export JWT_SECRET=\"your-secret-32-chars-minimum\""
    echo "   generate-token admin"
    echo "   generate-token user123 24 execute monitor"
    echo ""
    echo "2. Start the server:"
    echo "   control-center server start"
    echo ""
    echo "3. Start the agent (on VM/container):"
    echo "   control-center agent start"
    echo ""
    echo "4. Connect with CLI:"
    echo "   control-center connect --host <server-ip> --token <your-token>"
    echo ""
    echo "5. Configuration:"
    echo "   control-center config set-token <token>"
    echo "   control-center config set-server <host> <port>"
    echo "   control-center config show"
    echo ""
    echo "6. Execute commands:"
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
    echo "  • Token help:   generate-token --help"
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
    install_cli_binary
    update_path
    cleanup
    print_success_message
}

# Run main installation
main