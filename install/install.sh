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
# Runtime Dependencies
#
# These are what cc needs to actuate once installed, as opposed to what this
# script needs to run. They are checked before anything is downloaded, offered
# for installation, and never forced: a refusal, a missing package manager or a
# failed install all leave cc installed and print what is still outstanding.
# Nothing here can abort the installation.
# ============================================================================

# Prompt on the terminal rather than on stdin.
#
# The documented install is `curl ... | bash`, where stdin is the SCRIPT, not the
# operator - a plain `read` there consumes the script's own remaining bytes and
# returns garbage without anyone typing. /dev/tty is the controlling terminal
# whatever stdin happens to be. With no terminal at all (CI, a Dockerfile) there
# is nobody to ask, so the answer is no and the packages are printed instead.
ask_yes_no() {
    local prompt="$1" reply=""

    case "${CC_INSTALL_DEPS:-}" in
        yes|YES|1|true) print_info "$prompt -> yes (CC_INSTALL_DEPS)"; return 0 ;;
        no|NO|0|false)  print_info "$prompt -> no (CC_INSTALL_DEPS)"; return 1 ;;
    esac

    # `[ -r /dev/tty ]` is not sufficient and testing it was a real defect: with no
    # controlling terminal the device node still exists and still tests readable,
    # and only the open fails, with ENXIO. The read then failed silently, left the
    # reply empty, and empty means "yes" here - so a `curl | bash` in CI answered
    # its own prompt and went on to run sudo unattended. Open it instead of
    # predicting that the open would work.
    # The redirect is on a group, not on the exec: redirections apply left to
    # right, so `exec 3< /dev/tty 2>/dev/null` attempts the open before the
    # silencing takes effect and leaks "No such device or address" to the
    # operator's terminal. Redirecting the group covers the open itself, and the
    # descriptor still belongs to this shell afterwards because `{ }` does not fork.
    if ! { exec 3< /dev/tty; } 2>/dev/null; then
        print_warning "No terminal available to ask; skipping."
        echo "    Set CC_INSTALL_DEPS=yes to install them without being asked."
        return 1
    fi

    printf '%s [Y/n] ' "$prompt" > /dev/tty
    # A failed read is not an empty answer. Only a reply that was actually typed
    # may be defaulted to yes.
    if ! read -r reply <&3; then
        exec 3<&-
        print_warning "Could not read a reply; skipping."
        return 1
    fi
    exec 3<&-

    case "$reply" in
        ""|y|Y|yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

detect_package_manager() {
    PKG_MGR=""
    if [[ "$OS_TYPE" == "macos" ]]; then
        command -v brew &> /dev/null && PKG_MGR="brew"
        return 0
    fi
    if command -v apt-get &> /dev/null; then PKG_MGR="apt"
    elif command -v dnf &> /dev/null; then PKG_MGR="dnf"
    elif command -v pacman &> /dev/null; then PKG_MGR="pacman"
    elif command -v zypper &> /dev/null; then PKG_MGR="zypper"
    fi
    return 0
}

# The same dependency is packaged under different names, so the mapping is per
# manager rather than one list of "the" package names.
pkg_name_for() {
    local dep="$1"
    case "$PKG_MGR:$dep" in
        apt:xdotool|dnf:xdotool|pacman:xdotool|zypper:xdotool) echo "xdotool" ;;
        brew:cliclick)      echo "cliclick" ;;
        apt:compiler)       echo "build-essential" ;;
        dnf:compiler)       echo "gcc" ;;
        pacman:compiler)    echo "base-devel" ;;
        zypper:compiler)    echo "gcc" ;;
        apt:pkgconfig)      echo "pkg-config" ;;
        dnf:pkgconfig)      echo "pkgconf-pkg-config" ;;
        pacman:pkgconfig)   echo "pkgconf" ;;
        zypper:pkgconfig)   echo "pkg-config" ;;
        apt:pipewire)       echo "libpipewire-0.3-dev" ;;
        dnf:pipewire)       echo "pipewire-devel" ;;
        pacman:pipewire)    echo "libpipewire" ;;
        zypper:pipewire)    echo "pipewire-devel" ;;
        *) echo "" ;;
    esac
}

install_cmd_prefix() {
    case "$PKG_MGR" in
        apt)    echo "sudo apt-get install -y" ;;
        dnf)    echo "sudo dnf install -y" ;;
        pacman) echo "sudo pacman -S --needed --noconfirm" ;;
        zypper) echo "sudo zypper install -y" ;;
        brew)   echo "brew install" ;;
        *)      echo "" ;;
    esac
}

# A portal offering RemoteDesktop is what makes Wayland actuation possible at
# all, and it is not something this script can install sensibly: the right
# backend depends on the desktop, and installing the wrong one does nothing.
# So it is reported, never offered.
report_portal_backend() {
    local found=""
    for f in /usr/share/xdg-desktop-portal/portals/*.portal; do
        [ -e "$f" ] || continue
        if grep -qi "RemoteDesktop" "$f" 2>/dev/null; then
            found="$found $(basename "$f" .portal)"
        fi
    done
    if [ -n "$found" ]; then
        print_success "Portal backend with RemoteDesktop:$found"
    else
        print_warning "No installed portal backend declares RemoteDesktop."
        echo "    Wayland actuation needs one. GNOME uses xdg-desktop-portal-gnome,"
        echo "    KDE uses xdg-desktop-portal-kde. wlroots compositors (Hyprland, Sway)"
        echo "    ship no RemoteDesktop interface at all and cannot be driven this way."
    fi
}

check_runtime_dependencies() {
    print_info "Checking actuation dependencies..."

    MISSING_REQUIRED=()   # cc cannot actuate without these
    MISSING_OPTIONAL=()   # cc runs in a documented degraded mode without these
    detect_package_manager

    if [[ "$OS_TYPE" == "macos" ]]; then
        if command -v cliclick &> /dev/null; then
            print_success "cliclick found"
        else
            MISSING_REQUIRED+=("cliclick")
        fi
    else
        local session="x11"
        if [ -n "${WAYLAND_DISPLAY:-}" ] || [ "${XDG_SESSION_TYPE:-}" = "wayland" ]; then
            session="wayland"
        fi
        print_info "Session type: $session"

        if command -v xdotool &> /dev/null; then
            print_success "xdotool found"
        elif [ "$session" = "x11" ]; then
            MISSING_REQUIRED+=("xdotool")
        else
            # On Wayland the portal drives actuation, but xdotool still reaches
            # XWayland clients and is the fallback position reader.
            MISSING_OPTIONAL+=("xdotool")
        fi

        if [ "$session" = "wayland" ]; then
            report_portal_backend
            # The cursor helper is compiled on first use. Without these cc still
            # actuates; only compositor-sourced position readback is lost, and it
            # says so through `cc-wayland-actuate --status`.
            command -v cc &> /dev/null || command -v gcc &> /dev/null \
                || MISSING_OPTIONAL+=("compiler")
            command -v pkg-config &> /dev/null || MISSING_OPTIONAL+=("pkgconfig")
            if command -v pkg-config &> /dev/null; then
                pkg-config --exists libpipewire-0.3 2>/dev/null \
                    || MISSING_OPTIONAL+=("pipewire")
            else
                MISSING_OPTIONAL+=("pipewire")
            fi
        fi
    fi

    if [ ${#MISSING_REQUIRED[@]} -eq 0 ] && [ ${#MISSING_OPTIONAL[@]} -eq 0 ]; then
        print_success "All actuation dependencies present"
        return 0
    fi

    echo ""
    if [ ${#MISSING_REQUIRED[@]} -ne 0 ]; then
        print_warning "Required for actuation, not installed: ${MISSING_REQUIRED[*]}"
        echo "    Without these cc installs but cannot move the pointer or type."
    fi
    if [ ${#MISSING_OPTIONAL[@]} -ne 0 ]; then
        print_warning "Optional, not installed: ${MISSING_OPTIONAL[*]}"
        echo "    cc works without them. What is lost: compositor-sourced pointer"
        echo "    position on Wayland, which falls back to XWayland and reports"
        echo "    position_captured=false over native-Wayland windows."
    fi

    offer_dependency_install
}

offer_dependency_install() {
    local to_install=("${MISSING_REQUIRED[@]}" "${MISSING_OPTIONAL[@]}")
    local pkgs=() dep pkg

    if [ -z "$PKG_MGR" ]; then
        echo ""
        if [[ "$OS_TYPE" == "macos" ]]; then
            print_warning "Homebrew not found, so these cannot be installed automatically."
            echo "    Install Homebrew (https://brew.sh), then: brew install cliclick"
        else
            print_warning "No supported package manager found (apt, dnf, pacman, zypper)."
            echo "    Install the packages above with your distribution's tools."
        fi
        return 0
    fi

    for dep in "${to_install[@]}"; do
        pkg="$(pkg_name_for "$dep")"
        [ -n "$pkg" ] && pkgs+=("$pkg")
    done

    if [ ${#pkgs[@]} -eq 0 ]; then
        return 0
    fi

    local cmd
    cmd="$(install_cmd_prefix)"
    echo ""
    echo "  Would run: $cmd ${pkgs[*]}"
    echo ""

    if ! ask_yes_no "Install these now?"; then
        print_info "Skipping. Install them later with:"
        echo "    $cmd ${pkgs[*]}"
        return 0
    fi

    # Deliberately not guarded by `set -e`: a failed dependency install must not
    # abort an installation that is otherwise fine. cc is still usable, and the
    # operator is told exactly what is still missing.
    if $cmd "${pkgs[@]}"; then
        print_success "Dependencies installed"
    else
        print_warning "Dependency installation did not complete."
        echo "    cc is still being installed. Finish the dependencies with:"
        echo "    $cmd ${pkgs[*]}"
    fi
    return 0
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
    check_runtime_dependencies
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