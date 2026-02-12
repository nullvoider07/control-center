# Control Center - Windows Installation Script
# Installs: control-center-server, control-center-agent, and Python CLI

$ErrorActionPreference = "Stop"

# ============================================================================
# Configuration
# ============================================================================
$REPO = "your-org/control-center"  # TODO: Update with actual GitHub repo
$SERVER_BINARY = "control-center-server.exe"
$AGENT_BINARY = "control-center-agent.exe"

# Colors for better readability
function Write-Success { param($Message) Write-Host "✓ $Message" -ForegroundColor Green }
function Write-Error-Message { param($Message) Write-Host "✗ $Message" -ForegroundColor Red }
function Write-Warning-Message { param($Message) Write-Host "⚠ $Message" -ForegroundColor Yellow }
function Write-Info { param($Message) Write-Host "→ $Message" -ForegroundColor Cyan }

# ============================================================================
# Print Header
# ============================================================================
function Write-Header {
    Write-Host ""
    Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Cyan
    Write-Host "   Control Center Installation" -ForegroundColor Cyan
    Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Cyan
    Write-Host ""
}

# ============================================================================
# Check Existing Installation
# ============================================================================
function Test-ExistingInstallation {
    if (Get-Command "control-center" -ErrorAction SilentlyContinue) {
        Write-Warning-Message "Control Center is already installed."
        
        try {
            $CurrentVersion = & control-center --version 2>$null | Select-String -Pattern '\d+\.\d+\.\d+' | ForEach-Object { $_.Matches.Value }
            Write-Host "  Current version: $CurrentVersion" -ForegroundColor Gray
        } catch {
            Write-Host "  Current version: unknown" -ForegroundColor Gray
        }
        
        Write-Host ""
        $Confirmation = Read-Host "Do you want to reinstall/upgrade? [y/N]"
        if ($Confirmation -notmatch "^[Yy]$") {
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
    $ARCH_TYPE = if ([Environment]::Is64BitOperatingSystem) { "x64" } else { "x86" }
    Write-Success "Detected: Windows ($ARCH_TYPE)"
    return $ARCH_TYPE
}

# ============================================================================
# Check Dependencies
# ============================================================================
function Test-Dependencies {
    Write-Info "Checking dependencies..."
    
    # Check for Python 3
    if (-not (Get-Command "python" -ErrorAction SilentlyContinue) -and 
        -not (Get-Command "python3" -ErrorAction SilentlyContinue)) {
        Write-Error-Message "Python 3 is required but not installed."
        Write-Host "  Download: https://www.python.org/downloads/" -ForegroundColor Yellow
        exit 1
    }
    
    # Check for pip
    $PythonCmd = if (Get-Command "python" -ErrorAction SilentlyContinue) { "python" } else { "python3" }
    $PipCheck = & $PythonCmd -m pip --version 2>$null
    if (-not $PipCheck) {
        Write-Error-Message "pip is required but not installed."
        Write-Host "  Install: $PythonCmd -m ensurepip" -ForegroundColor Yellow
        exit 1
    }
    
    # Check for AutoHotkey (optional but recommended for agent)
    if (-not (Test-Path "C:\Program Files\AutoHotkey\AutoHotkey.exe") -and
        -not (Test-Path "C:\Program Files (x86)\AutoHotkey\AutoHotkey.exe")) {
        Write-Warning-Message "AutoHotkey not found (required for agent functionality)"
        Write-Host "  Download: https://www.autohotkey.com/" -ForegroundColor Yellow
        Write-Host ""
        $Confirmation = Read-Host "Continue without AutoHotkey? [y/N]"
        if ($Confirmation -notmatch "^[Yy]$") {
            exit 1
        }
    } else {
        Write-Success "AutoHotkey found"
    }
    
    Write-Success "All required dependencies found"
}

# ============================================================================
# Get Latest Release
# ============================================================================
function Get-LatestRelease {
    Write-Info "Fetching latest version from GitHub..."
    
    try {
        $ReleaseUrl = "https://api.github.com/repos/$REPO/releases/latest"
        $LatestRelease = Invoke-RestMethod -Uri $ReleaseUrl
        $LatestTag = $LatestRelease.tag_name
        $Version = $LatestTag.TrimStart('v')
        
        Write-Success "Latest version: v$Version"
        return @{
            Tag = $LatestTag
            Version = $Version
        }
    } catch {
        Write-Error-Message "Could not fetch latest release"
        Write-Host "  Check: https://github.com/$REPO/releases" -ForegroundColor Yellow
        Write-Host "  Error: $_" -ForegroundColor Red
        exit 1
    }
}

# ============================================================================
# Download Release Package
# ============================================================================
function Invoke-DownloadRelease {
    param(
        [string]$Version,
        [string]$Tag,
        [string]$Arch
    )
    
    $FileName = "control-center-$Version-windows-$Arch.zip"
    $DownloadUrl = "https://github.com/$REPO/releases/download/$Tag/$FileName"
    
    Write-Info "Downloading Control Center v$Version..."
    Write-Host "  URL: $DownloadUrl" -ForegroundColor Gray
    
    $TempZip = "$env:TEMP\control-center-$Version-windows.zip"
    
    try {
        # Use BITS transfer for better reliability and progress
        if (Get-Command "Start-BitsTransfer" -ErrorAction SilentlyContinue) {
            Start-BitsTransfer -Source $DownloadUrl -Destination $TempZip -DisplayName "Downloading Control Center"
        } else {
            Invoke-WebRequest -Uri $DownloadUrl -OutFile $TempZip -UseBasicParsing
        }
        
        Write-Success "Downloaded successfully"
        return $TempZip
    } catch {
        Write-Error-Message "Download failed"
        Write-Host ""
        Write-Host "  Please check:" -ForegroundColor Yellow
        Write-Host "    1. Release exists: https://github.com/$REPO/releases/tag/$Tag"
        Write-Host "    2. Asset exists: $FileName"
        Write-Host ""
        Write-Host "  Error: $_" -ForegroundColor Red
        
        if (Test-Path $TempZip) { Remove-Item $TempZip -Force }
        exit 1
    }
}

# ============================================================================
# Extract and Verify Package
# ============================================================================
function Expand-Package {
    param([string]$ZipFile)
    
    Write-Info "Extracting package..."
    
    $TempDir = "$env:TEMP\control-center-extract-$PID"
    
    try {
        # Create temp extraction directory
        if (Test-Path $TempDir) {
            Remove-Item $TempDir -Recurse -Force
        }
        New-Item -ItemType Directory -Path $TempDir | Out-Null
        
        # Extract
        Expand-Archive -Path $ZipFile -DestinationPath $TempDir -Force
        
        # Verify binaries
        $ServerPath = Join-Path $TempDir "bin\$SERVER_BINARY"
        $AgentPath = Join-Path $TempDir "bin\$AGENT_BINARY"
        
        if (-not (Test-Path $ServerPath)) {
            Write-Error-Message "$SERVER_BINARY not found in package"
            Write-Host "  Expected: $ServerPath" -ForegroundColor Yellow
            Write-Host "  Contents:" -ForegroundColor Yellow
            Get-ChildItem $TempDir -Recurse | Select-Object FullName | Format-Table
            throw "Binary verification failed"
        }
        
        if (-not (Test-Path $AgentPath)) {
            Write-Error-Message "$AGENT_BINARY not found in package"
            Write-Host "  Expected: $AgentPath" -ForegroundColor Yellow
            throw "Binary verification failed"
        }
        
        Write-Success "Package verified"
        
        # Find .whl file
        $WheelFile = Get-ChildItem -Path $TempDir -Filter "*.whl" -Recurse | Select-Object -First 1
        
        if (-not $WheelFile) {
            Write-Error-Message "Python wheel (.whl) file not found in package"
            throw "Python CLI not found"
        }
        
        Write-Success "Found Python CLI: $($WheelFile.Name)"
        
        return @{
            TempDir = $TempDir
            WheelPath = $WheelFile.FullName
        }
    } catch {
        Write-Error-Message "Package extraction failed: $_"
        if (Test-Path $TempDir) {
            Remove-Item $TempDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        exit 1
    }
}

# ============================================================================
# Install Binaries
# ============================================================================
function Install-Binaries {
    param(
        [string]$SourceDir
    )
    
    $InstallDir = "$env:LOCALAPPDATA\Programs\ControlCenter"
    
    Write-Info "Installing binaries to $InstallDir..."
    
    # Create installation directory
    if (-not (Test-Path $InstallDir)) {
        New-Item -ItemType Directory -Path $InstallDir | Out-Null
    }
    
    # Create bin subdirectory
    $BinDir = Join-Path $InstallDir "bin"
    if (-not (Test-Path $BinDir)) {
        New-Item -ItemType Directory -Path $BinDir | Out-Null
    }
    
    # Copy binaries
    $SourceBinDir = Join-Path $SourceDir "bin"
    Copy-Item (Join-Path $SourceBinDir $SERVER_BINARY) -Destination $BinDir -Force
    Copy-Item (Join-Path $SourceBinDir $AGENT_BINARY) -Destination $BinDir -Force
    
    Write-Success "Binaries installed"
    
    return @{
        InstallDir = $InstallDir
        BinDir = $BinDir
    }
}

# ============================================================================
# Install Python CLI
# ============================================================================
function Install-PythonCLI {
    param([string]$WheelPath)
    
    Write-Info "Installing Python CLI..."
    
    # Determine Python command
    $PythonCmd = if (Get-Command "python" -ErrorAction SilentlyContinue) { "python" } else { "python3" }
    
    try {
        # Install wheel
        $InstallOutput = & $PythonCmd -m pip install --user $WheelPath --force-reinstall 2>&1
        
        if ($LASTEXITCODE -ne 0) {
            Write-Error-Message "Failed to install Python CLI"
            Write-Host "  Output: $InstallOutput" -ForegroundColor Yellow
            Write-Host "  Try manually: $PythonCmd -m pip install $WheelPath" -ForegroundColor Yellow
            throw "Python CLI installation failed"
        }
        
        Write-Success "Python CLI installed"
    } catch {
        Write-Error-Message "Python CLI installation failed: $_"
        exit 1
    }
}

# ============================================================================
# Update PATH
# ============================================================================
function Update-Path {
    param([string]$BinDir)
    
    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    
    if ($UserPath -notlike "*$BinDir*") {
        Write-Info "Adding to PATH..."
        [Environment]::SetEnvironmentVariable("Path", "$UserPath;$BinDir", "User")
        $env:Path += ";$BinDir"
        
        Write-Success "PATH updated"
        return $true
    } else {
        Write-Success "PATH already configured"
        return $false
    }
}

# ============================================================================
# Clean Up Temporary Files
# ============================================================================
function Remove-TemporaryFiles {
    param(
        [string]$ZipFile,
        [string]$TempDir
    )
    
    Write-Info "Cleaning up temporary files..."
    
    if (Test-Path $ZipFile) {
        Remove-Item $ZipFile -Force -ErrorAction SilentlyContinue
    }
    
    if (Test-Path $TempDir) {
        Remove-Item $TempDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# ============================================================================
# Print Success Message
# ============================================================================
function Write-SuccessMessage {
    param(
        [string]$Version,
        [string]$InstallDir,
        [bool]$PathUpdated
    )
    
    Write-Host ""
    Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Green
    Write-Host "✓ Control Center v$Version installed successfully!" -ForegroundColor Green
    Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Green
    Write-Host ""
    Write-Host "Installed to: $InstallDir" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Components:" -ForegroundColor White
    Write-Host "  • Server:  control-center-server.exe" -ForegroundColor Cyan
    Write-Host "  • Agent:   control-center-agent.exe" -ForegroundColor Cyan
    Write-Host "  • CLI:     control-center (Python)" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Cyan
    Write-Host "Quick Start Guide" -ForegroundColor Cyan
    Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "1. Start the server:" -ForegroundColor White
    Write-Host "   control-center-server" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "2. Start the agent (in VM/container):" -ForegroundColor White
    Write-Host "   control-center-agent" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "3. Connect with CLI:" -ForegroundColor White
    Write-Host "   control-center connect --host <server-host> --token <your-token>" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "4. Execute commands:" -ForegroundColor White
    Write-Host "   control-center> 960 540 left" -ForegroundColor Yellow
    Write-Host "   control-center> type Hello World" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Cyan
    Write-Host "Documentation & Help" -ForegroundColor Cyan
    Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  • Full docs:    https://github.com/$REPO" -ForegroundColor White
    Write-Host "  • CLI help:     control-center --help" -ForegroundColor White
    Write-Host "  • Server help:  control-center-server --help" -ForegroundColor White
    Write-Host "  • Agent help:   control-center-agent --help" -ForegroundColor White
    Write-Host ""
    
    if ($PathUpdated) {
        Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Yellow
        Write-Warning-Message "PATH updated - Restart your terminal or run:"
        Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Yellow
        Write-Host ""
        Write-Host '  $env:Path = [Environment]::GetEnvironmentVariable("Path", "User")' -ForegroundColor Cyan
    } else {
        Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Green
        Write-Host "Ready to use!" -ForegroundColor Green
        Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Green
        Write-Host ""
        Write-Host "  Try: control-center --help" -ForegroundColor White
    }
    
    Write-Host ""
}

# ============================================================================
# Main Installation Flow
# ============================================================================
function Invoke-Main {
    Write-Header
    
    Test-ExistingInstallation
    $Arch = Get-Architecture
    Test-Dependencies
    
    $Release = Get-LatestRelease
    $ZipFile = Invoke-DownloadRelease -Version $Release.Version -Tag $Release.Tag -Arch $Arch
    $Package = Expand-Package -ZipFile $ZipFile
    $Install = Install-Binaries -SourceDir $Package.TempDir
    Install-PythonCLI -WheelPath $Package.WheelPath
    $PathUpdated = Update-Path -BinDir $Install.BinDir
    Remove-TemporaryFiles -ZipFile $ZipFile -TempDir $Package.TempDir
    
    Write-SuccessMessage -Version $Release.Version -InstallDir $Install.InstallDir -PathUpdated $PathUpdated
}

# Run main installation
try {
    Invoke-Main
} catch {
    Write-Error-Message "Installation failed: $_"
    Write-Host ""
    Write-Host "Stack trace:" -ForegroundColor Yellow
    Write-Host $_.ScriptStackTrace -ForegroundColor Gray
    exit 1
}