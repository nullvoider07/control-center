# Control Center - Desktop Actuation Tool

**Version:** 0.1.0  
**Last Updated:** February 2026  
**Developer:** Kartik (NullVoider)

---

## Table of Contents

1. [Overview](#overview)
2. [Key Features](#key-features)
3. [Capability Summary](#capability-summary)
   - [Server Capabilities](#server-capabilities)
   - [Agent Capabilities](#agent-capabilities)
   - [Controller Capabilities](#controller-capabilities)
4. [Technical Specifications](#technical-specifications)
   - [System Requirements](#system-requirements)
   - [Architecture](#architecture)
   - [Platform Backends](#platform-backends)
5. [Installation](#installation)
6. [Quick Start](#quick-start)
7. [Authentication](#authentication)
   - [JWT Tokens](#jwt-tokens)
   - [Token Scopes](#token-scopes)
   - [Environment Variables](#environment-variables)
8. [Command Syntax Reference](#command-syntax-reference)
   - [Mouse Commands](#mouse-commands)
   - [Keyboard Commands](#keyboard-commands)
   - [Position Tracking](#position-tracking)
   - [Platform Differences](#platform-differences)
9. [Usage Modes](#usage-modes)
   - [Interactive Mode](#interactive-mode)
   - [Single Execute Mode](#single-execute-mode)
   - [Batch Mode](#batch-mode)
   - [Watch Mode](#watch-mode)
10. [CLI Command Reference](#cli-command-reference)
    - [server](#server)
    - [agent](#agent)
    - [connect](#connect)
    - [execute](#execute)
    - [watch](#watch)
    - [batch](#batch)
    - [status](#status)
    - [session](#session)
    - [export](#export)
    - [audit](#audit)
    - [token](#token)
    - [config](#config)
    - [version / doctor / update / uninstall](#version--doctor--update--uninstall)
11. [Configuration](#configuration)
12. [Session Management](#session-management)
13. [Metrics and Monitoring](#metrics-and-monitoring)
14. [Export System](#export-system)
15. [Audit Logging](#audit-logging)
16. [WatchCommands Stream](#watchcommands-stream)
17. [Troubleshooting](#troubleshooting)
18. [About This Project](#about-this-project)

---

## Overview

**Control Center** is a desktop actuation tool designed for Computer Use Agent (CUA) workflows. It provides real-time mouse, keyboard, and OS-level control over remote machines with a client-server architecture built on gRPC.

Control Center consists of three components:

- **Server** (Rust): High-performance gRPC server that receives, validates, and routes actuation commands to the connected agent
- **Agent** (Rust): Cross-platform actuation client that runs on the target machine, translates commands into native OS actions, and reports results back to the server
- **Controller** (Python): CLI and programmatic interface used to send commands to the server, manage sessions, and observe live command events

### Use Cases

- **CUA Actuation**: Send mouse and keyboard commands to a remote desktop environment controlled by an AI agent
- **Automation Scripting**: Execute sequences of UI interactions from scripts or batch files
- **AI Training Data Collection**: Record command streams with full spatial and timing metadata for training computer use models
- **Remote Desktop Control**: Control a remote machine's UI without a traditional remote desktop client

---

## Key Features

- ✅ **Cross-Platform Actuation**: Windows, macOS, and Linux (X11) support from a single command interface
- ✅ **Three Usage Modes**: Interactive shell, single-command execution, and batch file execution
- ✅ **Live Command Streaming**: WatchCommands gRPC stream for real-time event observation by external tools
- ✅ **Auto OS Detection**: Server detects connected agent OS and dispatches commands to the correct backend automatically
- ✅ **Position Tracking**: All mouse commands automatically report final cursor coordinates after execution
- ✅ **JWT Authentication**: Scope-based token authentication for all privileged operations
- ✅ **Session Persistence**: Sessions are saved to disk and accessible after disconnect
- ✅ **Reconnection Logic**: Interactive mode automatically attempts reconnection on connection loss
- ✅ **Batch File Support**: Execute commands from txt, JSON, NDJSON, YAML, or CSV files
- ✅ **Full Export System**: Export command logs, metrics, session data, audit logs, and diagnostics
- ✅ **Structured Audit Logging**: Every auth event, session start/stop, and agent disconnect is logged as structured JSON
- ✅ **VM Shutdown Detection**: Detects and gracefully handles target machine shutdown mid-session
- ✅ **Heartbeat Monitoring**: Server emits heartbeat events every 5 seconds during idle periods to signal liveness

---

## Capability Summary

### Server Capabilities

The Control Center Server is a Rust-based gRPC service that acts as the command broker between the controller and the agent.

**Core Functions:**

- Accepts and validates incoming gRPC commands from the controller
- Proxies validated commands to the connected Rust agent
- Maintains a registry of connected agents with connection metadata
- Enforces JWT-based authentication and scope checks on all privileged RPCs
- Tracks command execution counts, uptime, and connection history
- Broadcasts command events to all active WatchCommands subscribers
- Operates in single-agent mode (default) or multi-agent mode

**gRPC Service Endpoints:**

| RPC | Auth Required | Description |
|-----|--------------|-------------|
| `ExecuteCommand` | Yes (`execute` scope) | Execute a single command on the agent |
| `ExecuteCommandStream` | Yes (`execute` scope) | Execute a command and stream partial responses |
| `GetAgentInfo` | Yes (`monitor` scope) | Get agent OS, version, and capability info |
| `QueryConnections` | No | List currently connected agents |
| `QueryServers` | No | List server identity and status |
| `GetServerIdentity` | No | Get server ID, hostname, version, and uptime |
| `Ping` | No | Round-trip liveness check |
| `GetMetrics` | Yes (`metrics` scope) | Prometheus-style performance metrics |
| `DisconnectAgent` | Yes (`admin` scope) | Send graceful disconnect signal to agent |
| `GetConnectionHistory` | No | Retrieve historical connection records |
| `WatchCommands` | No | Stream live command events (read-only) |

### Agent Capabilities

The Control Center Agent is a Rust binary that runs on the target machine and executes commands using platform-native tools.

**Platform Support:**

| Platform | Mouse Backend | Keyboard Backend |
|----------|--------------|-----------------|
| Windows | AutoHotkey v2 (AHK) | AutoHotkey v2 (AHK) |
| macOS | cliclick | osascript (AppleScript) |
| Linux | xdotool | xdotool |

**Core Functions:**

- Registers with the server on startup with OS type, version, and capabilities
- Receives and executes command strings from the server
- Captures mouse position after every mouse action
- Responds to the server's Ping RPC for liveness checks
- Reports execution success/failure and timing back with each command result
- Sends keepalive signals to maintain server connection
- Detects and reports capabilities available on the host (e.g., `cliclick`, `xdotool`)

### Controller Capabilities

The Python controller provides the CLI and `GRPCClient` class used to interact with the server.

**Core Functions:**

- Connects to the server over gRPC (plaintext or SSL)
- Manages JWT authentication and token resolution (flag → env var → config file)
- Auto-detects agent OS and initializes the appropriate actuation controller
- Provides three command execution modes: interactive, execute, and batch
- Streams live command events from WatchCommands
- Manages session lifecycle and persists session data between runs
- Exports session data, metrics, and audit logs in multiple formats
- Generates, inspects, and validates JWT tokens locally

---

## Technical Specifications

### System Requirements

#### Server

- **OS**: Linux, macOS, or Windows
- **RAM**: 64 MB minimum
- **Network**: TCP port 50051 (default, configurable)
- **Dependencies**: None (standalone Rust binary)
- **Required env**: `JWT_SECRET` (≥64 characters)

#### Agent

- **OS**: Windows 10+, macOS 10.13+, or Linux with X11
- **RAM**: 32 MB minimum
- **Dependencies**:
  - Windows: AutoHotkey v2 must be installed
  - macOS: `cliclick` must be installed (`brew install cliclick`)
  - Linux: `xdotool` must be installed (`apt install xdotool`), `DISPLAY` must be set

#### Controller (CLI)

- **OS**: Windows, macOS, Linux
- **Python**: 3.12 or higher
- **Required Python dependencies**:
  - `grpcio` — gRPC client
  - `grpcio-tools` — proto code generation
  - `click` — CLI framework
  - `PyJWT` — token generation and validation
  - `psutil` — system resource metrics
  - `pyyaml` — YAML batch file support (optional)

### Architecture

![Architecture Diagram](./assets/cc_architecture.png)

#### Data Flow

1. **Command Input**: User types a command in interactive mode, or `execute`/`batch` is called
2. **Actuation Layer**: Python controller translates the human command into the OS-specific format
3. **gRPC Call**: `ExecuteCommand` RPC is sent to the server with the translated command string
4. **Validation**: Server validates the JWT token and scope
5. **Dispatch**: Server forwards the command to the connected Rust agent
6. **Execution**: Agent runs the command via the platform backend (AHK / cliclick / xdotool)
7. **Position Capture**: Agent captures final mouse coordinates after mouse commands
8. **Response**: Result, position, and timing are returned up the chain
9. **Broadcast**: Server broadcasts a `CommandEvent` to all WatchCommands subscribers

### Platform Backends

**Windows — AutoHotkey v2**

Commands are written to `C:\mouse_cmd.txt` and `C:\keyboard_cmd.txt` and executed by a persistent AHK v2 script running on the agent machine. This approach avoids spawning a new AHK process per command, which gives lower latency.

**macOS — cliclick + osascript**

Mouse commands use `cliclick` (a command-line tool for simulating mouse events). Keyboard commands — including `type` and all modifier+key combinations — use `osascript` with AppleScript's `keystroke` and `key code` commands. Commands containing `&&` or `t:"` (cliclick type syntax) are run via `sh -c` to allow compound execution.

**Linux — xdotool**

Both mouse and keyboard commands are executed via `xdotool` through `sh -c`. The `DISPLAY` environment variable must be set. On headless systems, Xvfb can provide a virtual display.

---

## Installation

### macOS and Linux

```bash
curl -fsSL https://raw.githubusercontent.com/nullvoider07/control-center/master/install/install.sh | bash
```

This will:
- Download platform-specific binaries (`control-center`, `control-center-server`, `control-center-agent`)
- Install to `~/.local/bin`
- Install Python package and dependencies
- Update PATH in your shell profile

### Windows

Run in PowerShell (Administrator):

```powershell
irm https://raw.githubusercontent.com/nullvoider07/control-center/master/install/install.ps1 | iex
```

This will:
- Download Windows binaries
- Install to `%LOCALAPPDATA%\Programs\ControlCenter\bin`
- Add to system PATH
- Install Python package

### Windows Agent Setup

Windows actuation requires two AutoHotkey v2 watcher scripts to be installed and running on the target machine before the agent is started. These scripts run as background services and watch for command files written by the Rust agent.

#### Prerequisites

1. **Install AutoHotkey v2** from [https://www.autohotkey.com](https://www.autohotkey.com). The scripts require v2 — v1 is not compatible.

2. **Obtain the AHK scripts.** The two scripts (`mouse_control.ahk` and `keyboard_control.ahk`) are included in the Control Center release package.

#### Step 1: Copy Scripts to AutoHotkey Directory

Open PowerShell as Administrator and run:

```powershell
Copy-Item "mouse_control.ahk" "C:\Program Files\AutoHotkey\mouse_control.ahk"
Copy-Item "keyboard_control.ahk" "C:\Program Files\AutoHotkey\keyboard_control.ahk"
```

The Rust agent writes commands to `C:\mouse_cmd.txt` and `C:\keyboard_cmd.txt`. The watcher scripts poll these files continuously and execute the commands via AutoHotkey v2.

#### Step 2: Configure Auto-Start via Task Scheduler

The watchers must start automatically at login. Use Task Scheduler so they run with the correct user context and elevation level.

**Windows 10:**

```powershell
# Mouse watcher
$MouseArg = '/c start /min "" "C:\Program Files\AutoHotkey\v2\AutoHotkey.exe" "C:\Program Files\AutoHotkey\mouse_control.ahk" watcher'
$ActionMouse = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $MouseArg
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "AgentUser"
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -Hidden
$Principal = New-ScheduledTaskPrincipal -UserId "AgentUser" -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskName "MouseControlWatcher" -Action $ActionMouse -Trigger $Trigger -Settings $Settings -Principal $Principal

# Keyboard watcher
$KeyboardArg = '/c start /min "" "C:\Program Files\AutoHotkey\v2\AutoHotkey.exe" "C:\Program Files\AutoHotkey\keyboard_control.ahk" watcher'
$ActionKey = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $KeyboardArg
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "AgentUser"
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -Hidden
$Principal = New-ScheduledTaskPrincipal -UserId "AgentUser" -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskName "KeyboardControlWatcher" -Action $ActionKey -Trigger $Trigger -Settings $Settings -Principal $Principal
```

Replace `AgentUser` with the actual Windows username that will be logged in when the agent runs.

**Windows 11:**

```powershell
# Mouse watcher
$MouseArg = '/c start /min "" "C:\Program Files\AutoHotkey\v2\AutoHotkey.exe" "C:\Program Files\AutoHotkey\mouse_control.ahk" watcher'
$ActionMouse = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $MouseArg
$CurrentUser = "$env:USERDOMAIN\$env:USERNAME"
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -Hidden
$Principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskName "MouseControlWatcher" -Action $ActionMouse -Trigger $Trigger -Settings $Settings -Principal $Principal -Description "Monitors for remote mouse control commands"

# Keyboard watcher
$KeyboardArg = '/c start /min "" "C:\Program Files\AutoHotkey\v2\AutoHotkey.exe" "C:\Program Files\AutoHotkey\keyboard_control.ahk" watcher'
$ActionKey = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $KeyboardArg
$CurrentUser = "$env:USERDOMAIN\$env:USERNAME"
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -Hidden
$Principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskName "KeyboardControlWatcher" -Action $ActionKey -Trigger $Trigger -Settings $Settings -Principal $Principal -Description "Monitors for remote keyboard control commands"
```

#### Step 3: Start Watchers Immediately

There is no need to reboot to start the watchers for the first time. Run them directly:

```powershell
Start-ScheduledTask -TaskName "MouseControlWatcher"
Start-ScheduledTask -TaskName "KeyboardControlWatcher"

# Verify both are running — you should see 2 AutoHotkey processes
Get-Process | Where-Object {$_.ProcessName -eq "AutoHotkey"}
```

> **Note:** From the next login onward, the watchers will start automatically at logon via Task Scheduler. A system restart (or manual logoff and logon) is required to verify the auto-start is working correctly. The Control Center agent will not be able to execute mouse or keyboard commands on Windows if the watchers are not running.

#### Step 4: Start the Agent

Once the watchers are confirmed running, start the Control Center agent:

```powershell
control-center agent start --server-host 192.168.1.100
```

---

### Build from Source

```bash
# Clone the repository
git clone https://github.com/nullvoider07/control-center.git
cd control-center

# Build Rust binaries
cargo build --release

# Install Python controller
pip install -e crates/controller
```

---

## Quick Start

### 1. Set JWT Secret

```bash
export CC_JWT_SECRET='your-secret-at-least-64-characters-long'
```

### 2. Start the Server

```bash
control-center server start
```

### 3. Generate a Token

```bash
export CONTROL_CENTER_TOKEN=$(control-center token generate --user me --scopes execute monitor)
```

### 4. Start the Agent on the Target Machine

```bash
# On the target machine (or same machine for local testing)
control-center agent start --server-host 127.0.0.1
```

### 5. Connect and Start Controlling

```bash
control-center connect --host 127.0.0.1
```

You are now in interactive mode. Type commands to control the desktop:

```
control-center> 960 540 left
control-center> type Hello World
control-center> press ^c
control-center> exit
```

---

## Authentication

### JWT Tokens

Control Center uses JWT (JSON Web Token) for authentication. The server validates a token on every authenticated RPC call. Tokens are signed with HMAC (HS256 by default) using a shared secret.

Tokens contain:
- `sub` — the user identifier
- `scopes` — list of permitted operations
- `exp` — expiry timestamp (required; tokens without expiry are rejected)
- `aud` — audience claim (default: `control-center`)
- `iss` — issuer claim (default: `control-center-auth`)
- `jti` — unique token ID

The controller resolves the token in this priority order: `--token` flag → `CONTROL_CENTER_TOKEN` env var → config file.

### Token Scopes

| Scope | Permitted RPCs |
|-------|---------------|
| `execute` | `ExecuteCommand`, `ExecuteCommandStream` |
| `monitor` | `GetAgentInfo`, `QueryConnections`, `QueryServers` |
| `metrics` | `GetMetrics` |
| `admin` | `DisconnectAgent`, all admin operations |

RPCs marked "No auth required" (`QueryConnections`, `QueryServers`, `Ping`, `GetConnectionHistory`, `WatchCommands`) do not require a token.

### Environment Variables

| Variable | Used By | Description |
|----------|---------|-------------|
| `CC_JWT_SECRET` | Controller + Server | JWT signing secret (controller maps this to `JWT_SECRET` for the server) |
| `JWT_SECRET` | Server binary | JWT secret as read directly by the Rust server |
| `JWT_AUDIENCE` | Controller + Server | JWT audience claim (default: `control-center`) |
| `JWT_ISSUER` | Controller | JWT issuer claim (default: `control-center-auth`) |
| `CONTROL_CENTER_TOKEN` | Controller | API token for authenticated commands |
| `AGENT_SERVER_HOST` | Agent binary | Server host the agent connects to |
| `AGENT_SERVER_PORT` | Agent binary | Server port the agent connects to |
| `SERVER_ADDR` | Server binary | Bind address for the server (e.g., `0.0.0.0:50051`) |
| `SINGLE_AGENT_MODE` | Server binary | `true` = only one agent allowed (default), `false` = multi-agent |
| `CONTROL_CENTER_NETWORK` | Server binary | Network label for this server instance |
| `RUST_LOG` | Agent + Server | Log level for Rust binaries (e.g., `info`, `debug`) |

---

## Command Syntax Reference

The actuation command language is the same across all three platforms. The controller translates these commands into the appropriate OS-specific calls before sending them to the server.

### Mouse Commands

| Command | Description |
|---------|-------------|
| `<x> <y> move` | Move cursor to coordinates without clicking |
| `<x> <y> left` | Move to coordinates and left-click |
| `<x> <y> right` | Move to coordinates and right-click |
| `<x> <y> double` | Move to coordinates and double-click |
| `<x> <y> middle` | Move to coordinates and middle-click |
| `<x> <y> triple` | Move to coordinates and triple-click (macOS only) |
| `<x> <y> scroll_up [n]` | Move to coordinates and scroll up (optionally n times) |
| `<x> <y> scroll_down [n]` | Move to coordinates and scroll down (optionally n times) |
| `<x> <y> drag <x2> <y2>` | Click and drag from (x,y) to (x2,y2) |
| `here <action>` | Perform action at the current cursor position |
| `position` | Query and return the current mouse cursor position |

**Examples:**

```
960 540 left            → Left-click at screen center
1200 300 right          → Right-click at (1200, 300)
500 400 double          → Double-click at (500, 400)
100 100 drag 800 600    → Drag from (100,100) to (800,600)
here left               → Left-click at wherever cursor currently is
960 540 scroll_down 5   → Scroll down 5 notches at center
position                → Return current X and Y coordinates
```

### Keyboard Commands

| Command | Description |
|---------|-------------|
| `type <text>` | Type the given text literally |
| `press <keys>` | Press one or more keys or a shortcut |
| `{Enter}` | Press the Enter key (auto-detected without `press`) |
| `{Tab}` | Press the Tab key |
| `{Esc}` or `{Escape}` | Press the Escape key |
| `{Backspace}` or `{BS}` | Press Backspace |
| `{Delete}` or `{Del}` | Press Delete (forward delete) |
| `{Up}`, `{Down}`, `{Left}`, `{Right}` | Arrow keys |
| `{Home}`, `{End}` | Home / End keys |
| `{PgUp}`, `{PgDn}` | Page Up / Page Down |
| `{F1}` – `{F12}` | Function keys |
| `{Space}` | Space key |

**Modifier syntax** (same across all platforms):

| Symbol | Modifier |
|--------|---------|
| `^` | Ctrl |
| `+` | Shift |
| `!` | Alt / Option |
| `#` | Super / Win / Cmd |

**Examples:**

```
type Hello World        → Types the string "Hello World"
press ^c                → Ctrl+C (Copy)
press ^v                → Ctrl+V (Paste)
press ^z                → Ctrl+Z (Undo)
press ^a                → Ctrl+A (Select All)
{Enter}                 → Press Enter
press +{Tab}            → Shift+Tab
press ^!{Delete}        → Ctrl+Alt+Delete
press ^+{Esc}           → Ctrl+Shift+Esc (Task Manager on Windows)
#                       → Super key (opens Start / app launcher)
#r                      → Super+R (Run dialog on Windows/Linux)
```

**macOS Unicode modifier syntax** (also accepted):

```
⌘c                      → Cmd+C
⌃v                      → Ctrl+V
⇧{Tab}                  → Shift+Tab
⌥{Up}                   → Option+Up
```

**macOS media keys** (via cliclick kp:):

```
press {VolumeUp}
press {VolumeDown}
press {Mute}
press {BrightnessUp}
press {PlayPause}
```

### Position Tracking

Every mouse command automatically captures and reports the final cursor position after execution. You do not need to do anything extra — the output is returned with the command result:

```
[MOUSE] Left-clicked @(960,540) (42ms)
[MOUSE] Dragged to @(800,600) (118ms)
[MOUSE] Scrolled down at @(500,400) (23ms)
```

To explicitly query the position without performing an action:

```
control-center> position
Position: X=960, Y=540
```

### Platform Differences

| Feature | Windows | macOS | Linux |
|---------|---------|-------|-------|
| Mouse backend | AHK v2 | cliclick | xdotool |
| Keyboard backend | AHK v2 | osascript | xdotool |
| Triple-click | ❌ | ✅ | ❌ |
| Media keys | ✅ | ✅ | ❌ |
| Super key (`#`) | Win key | Cmd | Super |
| `{LWin}`, `{RWin}` | ✅ | ❌ | ❌ |
| Unicode modifier chars (⌘ ⌃ ⇧ ⌥) | ❌ | ✅ | ❌ |
| Requires display | ❌ | ❌ | ✅ (DISPLAY) |

---

## Usage Modes

### Interactive Mode

The default and most powerful mode. A persistent gRPC connection is established once and maintained for the duration of the session. Commands are entered one at a time at the `control-center>` prompt.

```bash
control-center connect --host 192.168.1.100 --token YOUR_TOKEN
```

**Interactive mode features:**

- Persistent connection — no per-command connection overhead
- Live reconnection: if the connection drops, the session automatically attempts to reconnect (up to the configured retry limit)
- VM shutdown detection: if the target machine powers off, the session is gracefully terminated with a clear notification
- Built-in commands: `help`, `status`, `clear`, `exit`, `quit`
- Session tracking: all commands, their results, and timing are recorded in memory and saved on exit

**Interactive mode built-in commands:**

| Command | Description |
|---------|-------------|
| `help` | Display the OS-specific command reference |
| `status` | Show live connection status, session stats, and system resources |
| `clear` | Clear the terminal |
| `exit` / `quit` / `q` | Disconnect and exit |

### Single Execute Mode

Connect, run one command, then immediately disconnect. Suitable for scripting.

```bash
control-center execute --host 192.168.1.100 --token YOUR_TOKEN -c "960 540 left"
control-center execute -c "type Hello World"   # Uses saved config
control-center execute -c "press ^a"
```

Exit codes: `0` = success, `1` = command failed, `2` = cannot connect to VM/container.

### Batch Mode

Execute a list of commands from a file. The server connection is established once for the entire batch.

```bash
control-center batch -f commands.txt
control-center batch -f commands.json --stop-on-error
control-center batch -f script.yaml --delay 0.5 -o results.json
```

**Supported file formats:**

| Format | Description |
|--------|-------------|
| `txt` | One command per line. Lines starting with `#` are ignored. |
| `json` | A JSON array of strings, or array of `{"command": "..."}` objects |
| `ndjson` | One `{"command": "..."}` JSON object per line |
| `yaml` | A YAML list of command strings (requires `pyyaml`) |
| `csv` | First column is the command. Header row is skipped if the first cell is `command`, `cmd`, or `commands` |

Format is auto-detected from the file extension when `--format auto` (default).

**Example `commands.txt`:**

```
# Click the start menu
960 50 left
# Open search
type notepad
{Enter}
# Wait and type
type Hello from batch mode
press ^s
```

**Example `commands.json`:**

```json
[
  "960 540 left",
  {"command": "type Hello World"},
  "press ^s"
]
```

Results can be saved to a JSON file with `--output results.json`:

```json
{
  "total": 3,
  "success": 3,
  "failed": 0,
  "results": [
    {"index": 1, "command": "960 540 left", "success": true, "error": null},
    {"index": 2, "command": "type Hello World", "success": true, "error": null}
  ]
}
```

### Watch Mode

Stream live command events from the server in real-time. No authentication required. The stream remains open until the agent disconnects or you press Ctrl+C.

```bash
control-center watch                        # Human-readable output
control-center watch --fmt json             # Machine-readable JSON
control-center watch --host 192.168.1.10
```

**Text output format:**

```
[✓] 2026-02-25T12:58:04.286Z | mouse:left | 960 540 left | 42ms
[✗] 2026-02-25T12:58:05.100Z | keyboard:type | type badcommand | 3ms | ERROR: execution failed
[heartbeat] 2026-02-25T12:58:10.000Z — agent alive (session: abc123)
```

**JSON output format** (one JSON object per line):

```json
{"session_id": "abc123", "agent_id": "...", "timestamp": "2026-02-25T12:58:04.286Z", "raw_command": "960 540 left", "action_type": "mouse", "action_subtype": "left", "success": true, "execution_time_ms": 42, "mouse_x": 960, "mouse_y": 540, "is_heartbeat": false, ...}
```

---

## CLI Command Reference

### server

Manage the Control Center server (Rust binary).

```bash
control-center server start [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--host` | `0.0.0.0` | Interface to bind to |
| `--port` | `50051` | gRPC port |
| `--single-agent` / `--multi-agent` | single | Allow one or multiple agents |
| `--network` | — | Network label for this server instance |
| `--auth-url` | — | OAuth2 authorization URL |
| `--token-url` | — | OAuth2 token URL |
| `--client-id` | — | OAuth2 client ID |

**Examples:**

```bash
control-center server start
control-center server start --port 8080
control-center server start --multi-agent --network datacenter-east
```

**Requires:** `CC_JWT_SECRET` (or `JWT_SECRET`) environment variable set before starting.

---

### agent

Query and manage agents connected to the server.

```bash
control-center agent <subcommand>
```

#### agent info

Show details of the currently connected agent.

```bash
control-center agent info [--host HOST] [--port PORT] [--format text|json]
```

Displays agent ID, hostname, IP, OS type and version, connection ID, connected-at timestamp, and total commands executed.

#### agent capabilities

List the command types supported by the connected agent.

```bash
control-center agent capabilities [--host HOST] [--port PORT] [--format text|json]
```

#### agent ping

Measure round-trip latency to the server.

```bash
control-center agent ping [--host HOST] [--port PORT] [--count N] [--format text|json]
```

Sends N pings (default: 3) and reports per-ping RTT and aggregate min/avg/max.

#### agent disconnect

Send a graceful disconnect signal to the connected agent.

```bash
control-center agent disconnect --token TOKEN [--reason REASON] [--yes]
```

Requires `admin` scope. Prompts for confirmation unless `--yes` is passed.

#### agent history

Show historical connection records from the server registry.

```bash
control-center agent history [--host HOST] [--port PORT] [--limit N] [--format text|json|csv]
```

Returns up to N records (default: 10, server-side max: 500) including connection ID, hostname, IP, OS, connected/disconnected timestamps, commands executed, and disconnect reason.

#### agent start

Launch the Rust agent binary on the current machine.

```bash
control-center agent start [--server-host HOST] [--server-port PORT] [--token TOKEN]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--server-host` | `127.0.0.1` | Server host to connect to |
| `--server-port` | `50051` | Server gRPC port |
| `--token` | env/config | Authentication token |

---

### connect

Connect to the server and enter interactive mode with a persistent connection.

```bash
control-center connect [--host HOST] [--port PORT] [--token TOKEN] [--ssl]
```

Token resolution order: `--token` flag → `CONTROL_CENTER_TOKEN` env var → config file.

On successful connection, a banner is displayed showing the connected OS, agent version, and the available interactive commands. The session is automatically saved on exit.

**Connection failure behavior:** If the connection times out (default 5s), a troubleshooting message is shown. If the connection drops mid-session, the CLI attempts automatic reconnection. If the VM/container has shut down (detected after 3 consecutive failures), the session terminates with a VM shutdown notice.

---

### execute

Execute a single command without a persistent connection.

```bash
control-center execute --command|-c "COMMAND" [--host HOST] [--port PORT] [--token TOKEN] [--ssl]
```

**Examples:**

```bash
control-center execute -c "960 540 left"
control-center execute --host 10.0.0.5 --token $TOKEN -c "type Hello"
control-center execute -c "{Enter}"
```

---

### watch

Stream live command events from the server. No token required.

```bash
control-center watch [--host HOST] [--port PORT] [--ssl] [--fmt text|json]
```

Press Ctrl+C to stop watching. The stream ends automatically when the agent disconnects.

---

### batch

Execute commands from a file.

```bash
control-center batch -f FILE [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `-f`, `--file` | required | Input file path |
| `--format` | `auto` | File format: `auto`, `txt`, `json`, `ndjson`, `yaml`, `csv` |
| `--delay` | `0.0` | Seconds to wait between commands |
| `--stop-on-error` | off | Stop on first failure |
| `-o`, `--output` | — | Write results to a JSON file |
| `--host` | config | Server host |
| `--port` | config | Server port |
| `--token` | env/config | Auth token |
| `--ssl` | off | Use SSL/TLS |

---

### status

Show connection and server status. Can be run bare for a combined overview, or with a subcommand for focused output.

```bash
control-center status [--host HOST] [--port PORT] [--format text|json]
control-center status connection
control-center status server
control-center status metrics
control-center status system
control-center status session
```

| Subcommand | Auth | Description |
|------------|------|-------------|
| *(bare)* | No | Combined live overview: server, connection, metrics, system |
| `connection` | No | Live agent/connection details |
| `server` | No | Server identity, version, uptime, command count |
| `metrics` | No* | Command performance stats for current/last session |
| `system` | No | Controller host CPU, memory, disk, network |
| `session` | No | Current or last session summary |

*Metrics are read from in-memory session data or the saved session file; no token needed.

---

### session

Inspect and replay the current or last session.

```bash
control-center session <subcommand>
```

| Subcommand | Description |
|------------|-------------|
| `events` | List session lifecycle events (connect, disconnect, reconnect) |
| `commands` | List commands executed during the session |
| `stats` | Aggregate performance statistics |
| `replay` | Re-execute commands from the last session |

#### session commands

```bash
control-center session commands [--failed] [--limit N] [--format text|json|csv]
```

#### session stats

```bash
control-center session stats [--format text|json]
```

Displays total commands, successful, failed, success rate, average/min/max/p95 execution time (ms), and session duration.

#### session replay

```bash
control-center session replay [--host HOST] [--port PORT] [--token TOKEN]
                               [--failed-only] [--delay SECS] [--dry-run]
```

Re-executes commands from the last saved session. `--failed-only` replays only commands that previously failed. `--dry-run` prints the command list without executing.

---

### export

Export session data in various formats. All exports are written to `./exports/` by default unless `--output` is specified.

```bash
control-center export <subcommand>
```

#### export commands

```bash
control-center export commands [--format csv|json|ndjson] [--type-filter PREFIX]
                                [--success-only] [--failed-only] [--last N] [-o FILE]
```

Exports the command execution log with full metadata (command, success, timing, error).

#### export metrics

```bash
control-center export metrics [--format json|csv] [-o FILE]
```

Exports performance metrics: total commands, success/fail counts, success rate, avg/min/max/p95 timing, session duration.

#### export session

```bash
control-center export session [--format json|csv] [-o FILE]
```

Exports the full session data bundle: commands + metrics + events.

#### export audit

```bash
control-center export audit [--log-dir DIR] [--format json|csv|ndjson]
                             [--since YYYY-MM-DD] [--event-type TYPE]
                             [--level INFO|WARNING|ERROR] [--last N] [-o FILE]
```

Exports structured audit log entries with optional filtering.

#### export diagnostics

```bash
control-center export diagnostics [-o OUTPUT_DIR] [--no-system] [--no-html]
```

Exports a full diagnostics bundle: logs, system info, and config snapshot. Optionally generates an HTML report.

#### export report

```bash
control-center export report [-o FILE] [--command-format csv|json|ndjson]
```

Exports a complete human-readable session report.

---

### audit

Query and tail the structured audit log.

```bash
control-center audit <subcommand>
```

#### audit show

```bash
control-center audit show [--log-dir DIR] [--since DATE] [--event TYPE]
                           [--level INFO|WARNING|ERROR] [--last N] [--format text|json]
```

#### audit tail

```bash
control-center audit tail [--log-dir DIR] [--lines N]
```

Follows the audit log in real-time (like `tail -f`). Shows the last N lines first, then follows new entries. Press Ctrl+C to stop.

#### audit search

```bash
control-center audit search [--log-dir DIR] [--event TYPE] [--user USER_ID]
                             [--level LEVEL] [--since DATE] [--keyword TEXT]
                             [--format text|json]
```

**Audit event types recorded:**

- `auth_attempt` — login/token validation attempts (success and failure)
- `session_start` — new interactive session began
- `session_end` — session ended (with duration)
- `reconnection_attempt` — mid-session reconnection triggered
- `agent_disconnect` — graceful disconnect signal sent
- `vm_shutdown` — VM/container shutdown detected

---

### token

Generate, inspect, and validate JWT API tokens. Requires `PyJWT` (`pip install PyJWT`).

```bash
control-center token <subcommand>
```

#### token generate

```bash
control-center token generate --user USER --scopes SCOPE [SCOPE ...] [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--user` | required | User identifier (JWT `sub` claim) |
| `--scopes` | `execute monitor` | Permission scopes (repeatable) |
| `--expires` | `24` | Token lifetime in hours |
| `--secret` | `CC_JWT_SECRET` | Signing secret |
| `--algorithm` | `HS256` | HMAC algorithm: `HS256`, `HS384`, `HS512` |
| `--audience` | `control-center` | JWT audience claim |
| `--issuer` | `control-center-auth` | JWT issuer claim |
| `-o`, `--output` | stdout | Write token to file instead of stdout |

**Examples:**

```bash
# Standard operator token
control-center token generate --user ops-bot --scopes execute monitor

# Admin token for agent disconnect
control-center token generate --user admin --scopes execute monitor admin --expires 1

# Short-lived CI token
control-center token generate --user ci-runner --scopes execute --expires 2

# Save to file (useful for piping)
export CONTROL_CENTER_TOKEN=$(control-center token generate --user me --scopes execute monitor)
```

#### token inspect

Decode and display a token's claims without verifying its signature.

```bash
control-center token inspect TOKEN_STRING [--format text|json]
```

Shows subject, scopes, issued-at, expiry (with `[VALID]`/`[EXPIRED]` status), and token ID (jti).

#### token validate

Verify a token's signature and expiry against your JWT secret.

```bash
control-center token validate TOKEN_STRING [--secret SECRET] [--audience AUD]
```

Exits with code 0 if valid, 1 if expired or invalid.

---

### config

Manage the local configuration file.

```bash
control-center config <subcommand>
```

| Subcommand | Description |
|------------|-------------|
| `show` | Display current configuration and config file path |
| `set-token TOKEN` | Save API token to config file |
| `clear-token` | Remove token from config |
| `set-server HOST [PORT]` | Set default server host and port (default port: 50051) |
| `set KEY VALUE` | Set an arbitrary config key (e.g., `jwt_secret`) |
| `validate` | Check configuration for errors |
| `reset` | Reset configuration to defaults (prompts for confirmation) |
| `init` | Create a default configuration file |

**Examples:**

```bash
control-center config init
control-center config set-server 192.168.1.100 50051
control-center config set-token eyJhbGci...
control-center config set jwt_secret my-signing-secret
control-center config show
control-center config validate
```

Storing server and token in config means you can run `control-center connect` without any flags.

---

### version / doctor / update / uninstall

```bash
control-center version      # Show version of CLI, server binary, and agent binary
control-center doctor       # Check system dependencies (gRPC, PyJWT, binaries, config)
control-center update       # Check GitHub for updates and install if available
control-center update --check-only   # Only check, do not install
control-center uninstall    # Remove binaries and optionally purge config/data
control-center uninstall --purge --yes  # Non-interactive full removal
```

---

## Configuration

Control Center reads configuration from a YAML file stored at a platform-appropriate location. Run `control-center config show` to see the exact path.

**Configurable values:**

| Key | Description |
|-----|-------------|
| `host` | Default server host |
| `port` | Default server port (default: 50051) |
| `token` | Saved API token |
| `jwt_secret` | JWT signing secret (used by `token generate` and server start) |
| `use_ssl` | Whether to use SSL/TLS by default |
| `timeout` | gRPC connection timeout in seconds |

All config values can be overridden at runtime via CLI flags or environment variables. The resolution order for each value is always: **CLI flag → environment variable → config file → built-in default**.

---

## Session Management

When you use `connect`, a session is created and tracked in memory. On exit, the session is saved to disk so that `session`, `export`, and `status` commands can access it without an active connection.

**Session data includes:**

- Session ID, user ID, host, port, OS type, OS version
- Start time, end time, duration
- Connection events (connect, disconnect, reconnect, VM shutdown)
- Full command history with index, command text, success flag, execution time, and error message
- Aggregate metrics (total, success, failed, success rate, avg/min/max/p95 timing)

**Reconnection behavior:**

If the connection drops during an interactive session, the CLI will:
1. Detect the loss via a failed ping check before the next prompt
2. Increment a failure counter
3. Attempt reconnection up to the configured maximum attempts
4. If reconnection succeeds, resume the session seamlessly
5. If the max attempts are reached or the failure looks like a VM shutdown (3+ consecutive failures), terminate the session with an appropriate message

---

## Metrics and Monitoring

The `MetricsCollector` tracks all command executions during a session.

**Tracked metrics:**

| Metric | Description |
|--------|-------------|
| `total_commands` | Total commands attempted |
| `successful_commands` | Commands that returned success |
| `failed_commands` | Commands that returned failure |
| `success_rate` | Percentage of successful commands |
| `avg_execution_time_ms` | Mean execution time across all commands |
| `min_execution_time_ms` | Fastest command execution |
| `max_execution_time_ms` | Slowest command execution |
| `p95_execution_time_ms` | 95th percentile execution time |
| `session_duration_seconds` | Total session wall-clock time |

View live metrics during a session with `status` in interactive mode, or via `control-center status metrics` from another terminal.

---

## Export System

The `Exporter` class handles all data export operations. Files are auto-named by type and timestamp when no `--output` path is given.

**Default output locations:**

```
exports/
  commands_<timestamp>.csv
  metrics_<timestamp>.json
  session_<timestamp>.json
  audit_<timestamp>.json
  report_<timestamp>.html
  diagnostics_<timestamp>/
```

Export formats by subcommand:

| Subcommand | Available Formats |
|------------|-----------------|
| `commands` | csv, json, ndjson |
| `metrics` | json, csv |
| `session` | json, csv |
| `audit` | json, csv, ndjson |
| `diagnostics` | directory bundle (JSON + optional HTML) |
| `report` | html (with embedded command log in csv/json/ndjson) |

---

## Audit Logging

Every security-relevant event is written to a structured JSON audit log in `./logs/audit/audit.log`.

**Audit log entry format:**

```json
{
  "timestamp": "2026-02-25T12:58:04.286Z",
  "level": "INFO",
  "event": "session_start",
  "session_id": "abc123-...",
  "user_id": "abc123-...",
  "ip_address": "192.168.1.100"
}
```

The audit log is append-only and rotated by date. Use `control-center audit tail` to follow it live during operations.

---

## WatchCommands Stream

`WatchCommands` is a server-side streaming gRPC RPC that broadcasts all command events in real-time. It requires no authentication, making it safe to expose to read-only observers.

**Key properties:**

- Opens immediately with an empty `WatchRequest`
- Emits one `CommandEvent` per command executed by the agent
- Emits a heartbeat event every 5 seconds when no commands are executing
- Stream closes automatically when the agent disconnects from the server
- Multiple subscribers can watch simultaneously

**CommandEvent fields:**

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | string | Active session UUID |
| `agent_id` | string | Agent machine UUID |
| `agent_version` | string | Agent version |
| `os_type` | string | `WINDOWS` / `MACOS` / `LINUX` |
| `timestamp` | string | ISO 8601 with milliseconds |
| `raw_command` | string | Exact command as entered (e.g., `^a`, `960 540 left`) |
| `action_type` | string | `mouse` / `keyboard` / `position` |
| `action_subtype` | string | `left`, `right`, `type`, `press`, etc. |
| `is_here_command` | bool | True if command used the `here` keyword |
| `success` | bool | Whether the command executed successfully |
| `error_message` | string | Error description if `success` is false |
| `execution_time_ms` | int32 | Wall-clock execution time in milliseconds |
| `mouse_x` | int32 | Final cursor X coordinate (mouse commands only) |
| `mouse_y` | int32 | Final cursor Y coordinate (mouse commands only) |
| `position_captured` | bool | Whether position was successfully captured |
| `is_heartbeat` | bool | True for keep-alive events, false for real commands |
| `agent_alive` | bool | Always true while the stream is open |

**Consuming the stream programmatically:**

```python
from controller.integrations.grpc_client import GRPCClient

client = GRPCClient(host="192.168.1.100", port=50051)
# No token needed for WatchCommands

for event in client.watch_commands():
    if event['is_heartbeat']:
        print(f"[alive] {event['timestamp']}")
    else:
        print(f"[{'+' if event['success'] else 'x'}] {event['raw_command']} ({event['execution_time_ms']}ms)")
```

**Using the CLI to pipe JSON to another process:**

```bash
control-center watch --fmt json | python memory_archive.py
```

---

## Troubleshooting

### Cannot connect to server

```
ERROR: Cannot connect to VM/Container
```

Check that:
1. The server is running: `control-center server start`
2. The host and port are correct: `control-center config show`
3. `JWT_SECRET` is set before starting the server
4. The port is not blocked by a firewall
5. The agent is running on the target machine: `control-center agent start`

### Token rejected / authentication failed

```
[x] Authentication failed — check your token
```

Check that:
1. The token has not expired: `control-center token inspect YOUR_TOKEN`
2. The token was signed with the same secret as the server: `control-center token validate YOUR_TOKEN`
3. The token has the required scope for the operation
4. `JWT_AUDIENCE` and `JWT_ISSUER` match between the token and server

### Commands fail silently on Linux

On Linux, xdotool requires a valid `DISPLAY` environment variable. If the session is headless:

```bash
# Install Xvfb for a virtual display
apt-get install xvfb
Xvfb :99 &
export DISPLAY=:99
```

Verify xdotool is installed:

```bash
which xdotool
# If not found:
apt-get install xdotool
```

### Commands fail on macOS

Verify cliclick is installed:

```bash
which cliclick
# If not found:
brew install cliclick
```

macOS may require accessibility permissions for cliclick. Go to **System Settings → Privacy & Security → Accessibility** and add your terminal application.

### Session data not available after disconnect

Session data is saved when you exit with `exit`/`quit`. If the process is killed (e.g., Ctrl+C during a command), data may not be saved. Use `exit` to disconnect cleanly whenever possible.

### "JWT_SECRET environment variable must be set" on server start

```bash
export CC_JWT_SECRET='your-secret-at-least-64-characters'
control-center server start
```

Or store it in config:

```bash
control-center config set jwt_secret your-secret-at-least-64-characters
control-center server start
```

### Debug logging

For detailed Rust-level logging:

```bash
export RUST_LOG=debug
control-center server start
# or
control-center agent start
```

For Python controller debug logs:

```bash
control-center --debug connect --host 192.168.1.100
```

---

# License

Copyright (C) 2026 Kartik (NullVoider)

This program is free software: you can redistribute it and/or modify it under the terms of the **GNU General Public License version 3** as published by the Free Software Foundation.

This program is distributed in the hope that it will be useful, but **without any warranty** — without even the implied warranty of merchantability or fitness for a particular purpose. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/>.

---

### What this means

- **Use freely** — run Control Center for any purpose, including commercial CUA workflows
- **Study and modify** — the full source is available and you are free to adapt it
- **Distribute** — you may share original or modified copies, provided they carry the same GPLv3 license
- **Contribute back** — modifications distributed to others must also be released under GPLv3

For the full license text, see the [`LICENSE`](./LICENSE) file in the root of the repository.

---

## Support

- **Issues:** [GitHub Issues](https://github.com/nullvoider07/control-center/issues)
- **Repository:** [GitHub Repository](https://github.com/nullvoider07/control-center)

---

**Last Updated:** February 2026  
**Developer:** Kartik (NullVoider)

---

## About This Project

Control Center was built from scratch as the actuation layer for Computer Use Agent (CUA) workflows. Every command format, every CLI flag, and every gRPC endpoint was designed around the real constraints of controlling desktop environments programmatically — across Windows, macOS, and Linux — in a way that an AI reasoning model can reliably drive.

The tool operates as one part of a three-component CUA stack alongside The Eyes (vision capture) and Memory Archive (Work In Progress), with each component designed to be independently deployable and observable.

**Control Center** — Desktop actuation for the AI age 🖱️