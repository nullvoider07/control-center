# Control Center - Unified Windows Installation
# Installs: Rust binaries (server + agent) + Python CLI

$ErrorActionPreference = "Stop"

# ============================================================================
# Configuration
# ============================================================================
$REPO = "nullvoider07/control-center"
$INSTALL_DIR = "$env:LOCALAPPDATA\Programs\ControlCenter"

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
    Write-Success "All dependencies found"
}

# ============================================================================
# Get Latest Release
# ============================================================================
function Get-LatestRelease {
    Write-Info "Fetching latest version from GitHub..."
    
    try {
        $response = Invoke-RestMethod -Uri "https://api.github.com/repos/$REPO/releases/latest"
        $script:LATEST_TAG = $response.tag_name
        $script:VERSION = $LATEST_TAG -replace '^v', ''
        Write-Success "Latest version: v$VERSION"
    } catch {
        Write-ErrorMsg "Could not find latest release."
        Write-Host "  Check: https://github.com/$REPO/releases"
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
        return $TMP_FILE
    } catch {
        Write-ErrorMsg "Download failed."
        Write-Host ""
        Write-Host "  Please check:"
        Write-Host "    1. Release exists: https://github.com/$REPO/releases/tag/$LATEST_TAG"
        Write-Host "    2. Asset exists: $FILE_NAME"
        Write-Host ""
        exit 1
    }
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
        
        # Verify contents
        if (-not (Test-Path "$TMP_DIR\bin\control-center-server.exe")) {
            Write-ErrorMsg "Server binary not found in package"
            Get-ChildItem "$TMP_DIR\bin" -ErrorAction SilentlyContinue
            exit 1
        }
        
        if (-not (Test-Path "$TMP_DIR\bin\control-center-agent.exe")) {
            Write-ErrorMsg "Agent binary not found in package"
            exit 1
        }
        
        if (-not (Test-Path "$TMP_DIR\bin\control-center.exe")) {
            Write-ErrorMsg "CLI binary not found in package"
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
    Copy-Item "$SourceDir\bin\control-center-server.exe" $INSTALL_DIR -Force
    Copy-Item "$SourceDir\bin\control-center-agent.exe" $INSTALL_DIR -Force
    
    Write-Success "Rust binaries installed"
    Write-Host "  • Server: $INSTALL_DIR\control-center-server.exe"
    Write-Host "  • Agent:  $INSTALL_DIR\control-center-agent.exe"
}

function Install-CLIBinary {
    param($SourceDir)
    
    Write-Info "Installing CLI binary..."
    
    # Copy PyInstaller-built binary
    Copy-Item "$SourceDir\bin\control-center.exe" $INSTALL_DIR -Force
    
    Write-Success "CLI binary installed"
    Write-Host "  • CLI: $INSTALL_DIR\control-center.exe"
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
    Write-Host "  • Server:  $INSTALL_DIR\control-center-server.exe"
    Write-Host "  • Agent:   $INSTALL_DIR\control-center-agent.exe"
    Write-Host "  • CLI:     $INSTALL_DIR\control-center.exe"
    Write-Host ""
    Write-Host "==========================================" -ForegroundColor Cyan
    Write-Host "  Quick Start" -ForegroundColor Cyan
    Write-Host "==========================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "1. Start the server:"
    Write-Host "   control-center server start" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "2. Start the agent (on VM/container):"
    Write-Host "   control-center agent start" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "3. Connect with CLI:"
    Write-Host "   control-center connect --host <server-ip> --token <token>" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "4. Configuration:"
    Write-Host "   control-center config set-token <token>" -ForegroundColor Yellow
    Write-Host "   control-center config set-server <host> <port>" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "5. Execute commands:"
    Write-Host "   control-center execute -c `"960 540 left`" --host X --token Y" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "==========================================" -ForegroundColor Cyan
    Write-Host "  Help & Documentation" -ForegroundColor Cyan
    Write-Host "==========================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  • CLI help:     control-center --help"
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