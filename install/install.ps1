# Control Center - Unified Windows Installation
# Installs: Rust binaries (server + agent + generate-token) + Python CLI

$ErrorActionPreference = "Stop"

# ============================================================================
# Configuration
# ============================================================================
$REPO = "nullvoider07/control-center"
$INSTALL_DIR = "$env:LOCALAPPDATA\Programs\ControlCenter\bin"
$SERVER_BINARY = "control-center-server.exe"
$AGENT_BINARY = "control-center-agent.exe"
$CLI_BINARY = "control-center.exe"
$TOKEN_BINARY = "generate-token.exe"

# ============================================================================
# Helper Functions
# ============================================================================
function Write-Header {
    Write-Host ""
    Write-Host "==========================================" -ForegroundColor Blue
    Write-Host "   Control Center - Unified Installation" -ForegroundColor Blue
    Write-Host "==========================================" -ForegroundColor Blue
    Write-Host ""
}

function Write-Success {
    param($Message)
    Write-Host "√ $Message" -ForegroundColor Green
}

function Write-ErrorMsg {
    param($Message)
    Write-Host "x $Message" -ForegroundColor Red
}

function Write-Warning {
    param($Message)
    Write-Host "⚠ $Message" -ForegroundColor Yellow
}

function Write-Info {
    param($Message)
    Write-Host "i $Message" -ForegroundColor Cyan
}

# ============================================================================
# Check Existing Installation
# ============================================================================
function Test-ExistingInstallation {
    if (Get-Command control-center -ErrorAction SilentlyContinue) {
        Write-Warning "Control Center is already installed."
        $version = (control-center version 2>$null | Select-String -Pattern '\d+\.\d+\.\d+').Matches.Value
        if ($version) {
            Write-Host "  Current version: $version"
        }
        Write-Host ""
        $response = Read-Host "Do you want to reinstall/upgrade? [y/N]"
        if ($response -notmatch '^[Yy]$') {
            Write-Info "Installation cancelled."
            exit 0
        }
        Write-Info "Proceeding with installation..."
    }
}

# ============================================================================
# Detect Architecture
# ============================================================================
function Get-Architecture {
    $arch = $env:PROCESSOR_ARCHITECTURE
    switch ($arch) {
        "AMD64" { return "x64" }
        "ARM64" { return "arm64" }
        default {
            Write-ErrorMsg "Unsupported architecture: $arch"
            exit 1
        }
    }
}

# ============================================================================
# Check Dependencies
# ============================================================================
function Test-Dependencies {
    Write-Info "Checking dependencies..."
    
    $missing = @()
    
    $requiredCmdlets = @('Expand-Archive', 'Invoke-RestMethod', 'Invoke-WebRequest')
    
    foreach ($cmdlet in $requiredCmdlets) {
        if (-not (Get-Command $cmdlet -ErrorAction SilentlyContinue)) {
            $missing += $cmdlet
        }
    }
    
    if ($missing.Count -gt 0) {
        Write-ErrorMsg "Missing PowerShell cmdlets: $($missing -join ', ')"
        Write-Host ""
        Write-Host "Please ensure you're running PowerShell 5.1 or later"
        exit 1
    }
    
    Write-Success "All dependencies found"
}

# ============================================================================
# Runtime Dependencies
#
# What cc needs to actuate once installed, as opposed to what this script needs
# to run. Checked before anything is downloaded, offered for installation, and
# never forced: a refusal, a missing package manager or a failed install all
# leave cc installed and print what is still outstanding.
# ============================================================================
function Test-Interactive {
    # A prompt is only meaningful with a real console. Under
    # `irm ... | iex` in a CI runner there is nobody to answer, and defaulting a
    # silent non-answer to "yes" would install packages nobody approved.
    if (-not [Environment]::UserInteractive) { return $false }
    try   { return -not [Console]::IsInputRedirected }
    catch { return $false }
}

function Get-Consent {
    param([string]$Prompt)

    switch ("$env:CC_INSTALL_DEPS".ToLower()) {
        { $_ -in 'yes', '1', 'true' } { Write-Info "$Prompt -> yes (CC_INSTALL_DEPS)"; return $true }
        { $_ -in 'no', '0', 'false' } { Write-Info "$Prompt -> no (CC_INSTALL_DEPS)";  return $false }
    }

    if (-not (Test-Interactive)) {
        Write-Warning "No console available to ask; skipping."
        Write-Host "    Set CC_INSTALL_DEPS=yes to install without being asked."
        return $false
    }

    $reply = Read-Host "$Prompt [Y/n]"
    return ($reply -eq '' -or $reply -match '^(y|yes)$')
}

function Get-AutoHotkeyV2 {
    # v2 specifically. The watcher scripts are v2 syntax, so a v1 install is not a
    # substitute — it would load them and fail on the first line. So this looks for
    # the versioned path rather than for the name.
    #
    # Roots are filtered before use: Join-Path throws on a null Path, and
    # ProgramFiles(x86) is absent on a 32-bit host. Registry first, because an
    # install to a non-default location is invisible to a path guess.
    $roots = @()
    try {
        $reg = Get-ItemProperty -Path 'HKLM:\SOFTWARE\AutoHotkey' -ErrorAction Stop
        if ($reg.InstallDir) { $roots += $reg.InstallDir }
    } catch { }
    $roots += @($env:ProgramFiles, ${env:ProgramFiles(x86)}) |
        Where-Object { $_ } |
        ForEach-Object { Join-Path $_ 'AutoHotkey' }

    foreach ($root in ($roots | Where-Object { $_ })) {
        foreach ($exe in 'AutoHotkey64.exe', 'AutoHotkey32.exe', 'AutoHotkey.exe') {
            $p = Join-Path $root (Join-Path 'v2' $exe)
            if (Test-Path $p) { return $p }
        }
    }
    return $null
}

function Get-WindowsPackageManager {
    if (Get-Command winget -ErrorAction SilentlyContinue) { return 'winget' }
    if (Get-Command choco  -ErrorAction SilentlyContinue) { return 'choco' }
    return $null
}

function Test-RuntimeDependencies {
    Write-Info "Checking actuation dependencies..."

    $ahk = Get-AutoHotkeyV2
    if ($ahk) {
        Write-Success "AutoHotkey v2 found: $ahk"
        return
    }

    Write-Host ""
    Write-Warning "AutoHotkey v2 is required for actuation and was not found."
    Write-Host "    The agent drives the mouse and keyboard through the v2 watcher"
    Write-Host "    scripts, so without it cc installs but cannot click or type."
    Write-Host "    A v1 installation does not substitute: the watchers are v2 syntax."

    $mgr = Get-WindowsPackageManager
    if (-not $mgr) {
        Write-Host ""
        Write-Warning "Neither winget nor choco found, so it cannot be installed automatically."
        Write-Host "    Install it from https://www.autohotkey.com/ (choose v2)."
        return
    }

    $cmd = if ($mgr -eq 'winget') {
        'winget install --id AutoHotkey.AutoHotkey --accept-source-agreements --accept-package-agreements'
    } else {
        'choco install autohotkey -y'
    }

    Write-Host ""
    Write-Host "  Would run: $cmd"
    Write-Host ""

    if (-not (Get-Consent "Install AutoHotkey v2 now?")) {
        Write-Info "Skipping. Install it later with:"
        Write-Host "    $cmd"
        return
    }

    # A failed dependency install must not abort an installation that is otherwise
    # fine, so this does not throw — cc still installs and the operator is told
    # what is outstanding.
    try {
        Invoke-Expression $cmd
        if (Get-AutoHotkeyV2) {
            Write-Success "AutoHotkey v2 installed"
        } else {
            Write-Warning "Install reported success but AutoHotkey v2 was still not found."
            Write-Host "    A new shell may be needed for PATH changes, or install it by hand:"
            Write-Host "    https://www.autohotkey.com/"
        }
    } catch {
        Write-Warning "Dependency installation did not complete: $_"
        Write-Host "    cc is still being installed. Finish it with:"
        Write-Host "    $cmd"
    }
}

# ============================================================================
# Get Latest Release
# ============================================================================
function Get-LatestRelease {
    Write-Info "Fetching latest version from GitHub..."

    # An anonymous GitHub API request is charged against a 60/hour quota keyed on the
    # exit IP, shared with every other client leaving through it — behind a VPN or
    # corporate NAT, strangers spend it. A token moves the quota onto the account, so
    # the lookup stops depending on the network path.
    $headers = @{
        'Accept'     = 'application/vnd.github+json'
        'User-Agent' = 'control-center-install'
    }
    $token = if ($env:GITHUB_TOKEN) { $env:GITHUB_TOKEN } else { $env:GH_TOKEN }
    if ($token) { $headers['Authorization'] = "Bearer $token" }

    try {
        $response = Invoke-RestMethod -Uri "https://api.github.com/repos/$REPO/releases/latest" -Headers $headers
        $script:LATEST_TAG = $response.tag_name
        $script:VERSION = $LATEST_TAG -replace '^v', ''
        Write-Success "Latest version: v$VERSION"
    } catch {
        $status = $null
        if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }

        if ($status -eq 403 -or $status -eq 429) {
            Write-ErrorMsg "GitHub API quota exhausted (HTTP $status)."
            if (-not $token) {
                Write-Host "  The anonymous quota is 60/hour and is keyed on your public IP, which"
                Write-Host "  a VPN or corporate NAT shares with everyone else behind it."
                Write-Host "  Set GITHUB_TOKEN to raise it to 5000/hour tied to your account."
            }
        } elseif ($status) {
            Write-ErrorMsg "GitHub returned HTTP $status while looking up the latest release."
        } else {
            Write-ErrorMsg "Could not reach GitHub: $($_.Exception.Message)"
        }
        Write-Host "  Release downloads are not rate limited, so you can also install a"
        Write-Host "  specific version by hand: https://github.com/$REPO/releases"
        exit 1
    }
}

# ============================================================================
# Download Package
# ============================================================================
function Get-Package {
    param($Arch)
    
    $FILE_NAME = "control-center-$VERSION-windows-$Arch.zip"
    $DOWNLOAD_URL = "https://github.com/$REPO/releases/download/$LATEST_TAG/$FILE_NAME"
    
    Write-Info "Downloading Control Center v$VERSION..."
    Write-Host "  URL: $DOWNLOAD_URL" -ForegroundColor Gray
    
    $TMP_FILE = "$env:TEMP\control-center-install-$PID.zip"
    
    try {
        # Download with progress
        $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -Uri $DOWNLOAD_URL -OutFile $TMP_FILE -UseBasicParsing
        $ProgressPreference = 'Continue'
        
        Write-Success "Downloaded successfully"
    } catch {
        Write-ErrorMsg "Download failed."
        Write-Host ""
        Write-Host "  Please check:"
        Write-Host "    1. Release exists: https://github.com/$REPO/releases/tag/$LATEST_TAG"
        Write-Host "    2. Asset exists: $FILE_NAME"
        Write-Host ""
        exit 1
    }

    Test-PackageChecksum -File $TMP_FILE -Name $FILE_NAME
    return $TMP_FILE
}

# Check the download against the digest the release publishes. TLS to GitHub is
# otherwise the only integrity control on a payload this script unpacks and puts on
# PATH. Missing or mismatched digests abort rather than warn.
function Test-PackageChecksum {
    param($File, $Name)

    $sumsUrl = "https://github.com/$REPO/releases/download/$LATEST_TAG/SHA256SUMS"

    Write-Info "Verifying checksum..."

    try {
        $ProgressPreference = 'SilentlyContinue'
        $sums = (Invoke-WebRequest -Uri $sumsUrl -UseBasicParsing).Content
        $ProgressPreference = 'Continue'
    } catch {
        Remove-Item $File -Force -ErrorAction SilentlyContinue
        Write-ErrorMsg "Release $LATEST_TAG publishes no SHA256SUMS; cannot verify the download."
        Write-Host ""
        Write-Host "  Releases before v1.2.1 predate checksum publishing. Install one by"
        Write-Host "  downloading and checking it yourself:"
        Write-Host "    https://github.com/$REPO/releases/tag/$LATEST_TAG"
        Write-Host ""
        exit 1
    }

    $expected = $null
    foreach ($line in $sums -split "`n") {
        $parts = $line.Trim() -split '\s+'
        if ($parts.Count -eq 2 -and $parts[1].TrimStart('*') -eq $Name) {
            $expected = $parts[0].ToLower()
            break
        }
    }

    if (-not $expected) {
        Remove-Item $File -Force -ErrorAction SilentlyContinue
        Write-ErrorMsg "SHA256SUMS lists no digest for $Name"
        exit 1
    }

    $actual = (Get-FileHash -Path $File -Algorithm SHA256).Hash.ToLower()

    if ($actual -ne $expected) {
        Remove-Item $File -Force -ErrorAction SilentlyContinue
        Write-ErrorMsg "Checksum mismatch for $Name - refusing to install."
        Write-Host "    expected $expected"
        Write-Host "    got      $actual"
        exit 1
    }

    Write-Success "Checksum verified"
}

# ============================================================================
# Extract Package
# ============================================================================
function Expand-Package {
    param($ZipFile)
    
    Write-Info "Extracting package..."
    
    $TMP_DIR = "$env:TEMP\control-center-extract-$PID"
    
    try {
        Expand-Archive -Path $ZipFile -DestinationPath $TMP_DIR -Force
        
        # Verify all binaries exist
        if (-not (Test-Path "$TMP_DIR\bin\$SERVER_BINARY")) {
            Write-ErrorMsg "$SERVER_BINARY not found in package"
            Get-ChildItem "$TMP_DIR\bin" -ErrorAction SilentlyContinue
            exit 1
        }
        
        if (-not (Test-Path "$TMP_DIR\bin\$AGENT_BINARY")) {
            Write-ErrorMsg "$AGENT_BINARY not found in package"
            exit 1
        }
        
        if (-not (Test-Path "$TMP_DIR\bin\$CLI_BINARY")) {
            Write-ErrorMsg "$CLI_BINARY not found in package"
            Get-ChildItem "$TMP_DIR\bin" -ErrorAction SilentlyContinue
            exit 1
        }

        if (-not (Test-Path "$TMP_DIR\bin\$TOKEN_BINARY")) {
            Write-ErrorMsg "$TOKEN_BINARY not found in package"
            Get-ChildItem "$TMP_DIR\bin" -ErrorAction SilentlyContinue
            exit 1
        }
        
        Write-Success "Package extracted and verified"
        return @{
            Dir = $TMP_DIR
        }
    } catch {
        Write-ErrorMsg "Failed to extract package: $_"
        exit 1
    }
}

# ============================================================================
# Install Components
# ============================================================================
function Install-RustBinaries {
    param($SourceDir)
    
    Write-Info "Installing Rust binaries..."
    
    # Create install directory
    if (-not (Test-Path $INSTALL_DIR)) {
        New-Item -ItemType Directory -Path $INSTALL_DIR -Force | Out-Null
    }
    
    # Copy binaries
    Copy-Item "$SourceDir\bin\$SERVER_BINARY" $INSTALL_DIR -Force
    Copy-Item "$SourceDir\bin\$AGENT_BINARY" $INSTALL_DIR -Force
    Copy-Item "$SourceDir\bin\$TOKEN_BINARY" $INSTALL_DIR -Force
    
    Write-Success "Rust binaries installed"
    Write-Host "  • Server:          $INSTALL_DIR\$SERVER_BINARY"
    Write-Host "  • Agent:           $INSTALL_DIR\$AGENT_BINARY"
    Write-Host "  • Token generator: $INSTALL_DIR\$TOKEN_BINARY"
}

function Install-CLIBinary {
    param($SourceDir)
    
    Write-Info "Installing CLI binary..."
    
    # Copy PyInstaller-built binary
    Copy-Item "$SourceDir\bin\$CLI_BINARY" $INSTALL_DIR -Force
    
    Write-Success "CLI binary installed"
    Write-Host "  • CLI: $INSTALL_DIR\$CLI_BINARY"
}

# ============================================================================
# Update PATH
# ============================================================================
function Update-Path {
    # Check if directory is in PATH
    $currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
    
    if ($currentPath -notlike "*$INSTALL_DIR*") {
        Write-Info "Adding $INSTALL_DIR to PATH..."
        
        $newPath = "$currentPath;$INSTALL_DIR"
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        
        # Update current session
        $env:Path = "$env:Path;$INSTALL_DIR"
        
        $script:PATH_UPDATED = $true
    } else {
        $script:PATH_UPDATED = $false
    }
}

# ============================================================================
# Clean Up
# ============================================================================
function Remove-TempFiles {
    param($ZipFile, $ExtractDir)
    
    Write-Info "Cleaning up..."
    
    if (Test-Path $ZipFile) {
        Remove-Item $ZipFile -Force
    }
    
    if (Test-Path $ExtractDir) {
        Remove-Item $ExtractDir -Recurse -Force
    }
}

# ============================================================================
# Print Success Message
# ============================================================================
function Write-SuccessMessage {
    Write-Host ""
    Write-Host "==========================================" -ForegroundColor Green
    Write-Host "  √ Control Center v$VERSION installed!" -ForegroundColor Green
    Write-Host "==========================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "Installed components:"
    Write-Host "  • Server:          $INSTALL_DIR\$SERVER_BINARY"
    Write-Host "  • Agent:           $INSTALL_DIR\$AGENT_BINARY"
    Write-Host "  • CLI:             $INSTALL_DIR\$CLI_BINARY"
    Write-Host "  • Token generator: $INSTALL_DIR\$TOKEN_BINARY"
    Write-Host ""
    Write-Host "==========================================" -ForegroundColor Cyan
    Write-Host "  Quick Start" -ForegroundColor Cyan
    Write-Host "==========================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "1. Generate an auth token:"
    Write-Host "   `$env:JWT_SECRET = 'your-secret-32-chars-minimum'" -ForegroundColor Yellow
    Write-Host "   generate-token admin" -ForegroundColor Yellow
    Write-Host "   generate-token user123 24 execute monitor" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "2. Start the server:"
    Write-Host "   control-center server start" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "3. Start the agent (on VM/container):"
    Write-Host "   control-center agent start" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "4. Connect with CLI:"
    Write-Host "   control-center connect --host <server-ip> --token <token>" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "5. Configuration:"
    Write-Host "   control-center config set-token <token>" -ForegroundColor Yellow
    Write-Host "   control-center config set-server <host> <port>" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "6. Execute commands:"
    Write-Host "   control-center execute -c `"960 540 left`" --host X --token Y" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "==========================================" -ForegroundColor Cyan
    Write-Host "  Help & Documentation" -ForegroundColor Cyan
    Write-Host "==========================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  • CLI help:     control-center --help"
    Write-Host "  • Token help:   generate-token --help"
    Write-Host "  • Version:      control-center version"
    Write-Host "  • System check: control-center doctor"
    Write-Host "  • Docs:         https://github.com/$REPO"
    Write-Host ""
    
    if ($PATH_UPDATED) {
        Write-Warning "PATH updated - Restart your terminal to apply changes"
        Write-Host ""
    } else {
        Write-Host "Ready to use!" -ForegroundColor Green
        Write-Host ""
    }
}

# ============================================================================
# Main Installation Flow
# ============================================================================
function Main {
    Write-Header
    
    Test-ExistingInstallation
    
    $arch = Get-Architecture
    Write-Success "Detected: Windows-$arch"
    
    Test-Dependencies
    Test-RuntimeDependencies
    Get-LatestRelease
    
    $zipFile = Get-Package -Arch $arch
    $extracted = Expand-Package -ZipFile $zipFile
    
    Install-RustBinaries -SourceDir $extracted.Dir
    Install-CLIBinary -SourceDir $extracted.Dir
    
    Update-Path
    
    Remove-TempFiles -ZipFile $zipFile -ExtractDir $extracted.Dir
    
    Write-SuccessMessage
}

# Run installation
Main