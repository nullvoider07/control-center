"""CLI implementation with token-based authentication and persistent connections"""

import click
import sys
import os
import uuid
import signal
import shutil
import platform
import subprocess
from typing import Optional, Union
from pathlib import Path

from controller.management.config_manager import ConfigManager, ConfigurationError
from controller.integrations.gRPC import GRPCClient, AuthenticationError, ConnectionError, RateLimitError
from controller.integrations.exceptions import VMShutdownError
from controller.integrations.status import StatusReporter
from controller.core.session import Session
from controller.core.metrics import MetricsCollector
from controller.os_specific.windows_actuation import WindowsActuation
from controller.os_specific.macos_actuation import MacOSActuation
from controller.os_specific.linux_actuation import LinuxActuation
from controller.utils.logger import setup_logger
from controller.utils.validation import require_valid_host, require_valid_port, ValidationError

__version__ = "1.0.0"

# Setup logger
logger = setup_logger('control-center')

# ============================================================================
# CLI Context and State Management
# ============================================================================

class CLIContext:
    """Context object for CLI state"""
    
    def __init__(self):
        self.client: Optional[GRPCClient] = None
        self.controller: Optional[Union[WindowsActuation, MacOSActuation, LinuxActuation]] = None
        self.session: Optional[Session] = None
        self.metrics: Optional[MetricsCollector] = None
        self.config_manager = ConfigManager()
        self.interrupted = False
    
    def cleanup(self):
        """Cleanup resources"""
        if self.client:
            try:
                self.client.disconnect()
            except Exception as e:
                logger.warning(f"Error during cleanup: {e}")

# Global context
ctx = CLIContext()

# Signal Handling for Graceful Shutdown
def signal_handler(sig, frame):
    """Handle Ctrl+C gracefully"""
    logger.info("\nInterrupt received. Cleaning up...")
    ctx.interrupted = True
    ctx.cleanup()
    sys.exit(0)

# Register signal handler
signal.signal(signal.SIGINT, signal_handler)

# Main CLI Group
@click.group()
@click.version_option(version=__version__)
@click.option('--debug', is_flag=True, help='Enable debug logging')
def cli(debug):
    """Control Center - Multi-OS actuation CLI tool
    
    Remote control for Windows, macOS, and Linux systems.
    """
    if debug:
        import logging
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Debug logging enabled")

# ============================================================================
# Main Commands - PERSISTENT CONNECTION MODE
# ============================================================================

@cli.command()
@click.option('--host', help='Server host IP/hostname')
@click.option('--port', type=int, help='Server gRPC port (default: 50051)')
@click.option('--token', envvar='CONTROL_CENTER_TOKEN', help='API token')
@click.option('--ssl', is_flag=True, help='Use SSL/TLS connection')
def connect(host: Optional[str], port: Optional[int], token: Optional[str], ssl: bool):
    """Connect to server with PERSISTENT connection and enter interactive mode
    
    This establishes a persistent connection that stays active until you
    exit with 'exit' or 'quit' command. The connection and OS-specific
    actuation logic are initialized once at the start.
    
    Examples:
        control-center connect --host 192.168.1.100 --token abc123
        export CONTROL_CENTER_TOKEN=abc123
        control-center connect --host 192.168.1.100
    """
    try:
        # Get token (CLI flag > env var > config file)
        api_token = ctx.config_manager.get_token(cli_token=token)
        
        if not api_token:
            click.echo("Error: No API token provided", err=True)
            click.echo("\nSet token via:")
            click.echo("  1. --token flag:  control-center connect --token YOUR_TOKEN")
            click.echo("  2. Environment:   export CONTROL_CENTER_TOKEN=YOUR_TOKEN")
            click.echo("  3. Config file:   control-center config set-token YOUR_TOKEN")
            sys.exit(1)
        
        # Get server configuration
        server_config = ctx.config_manager.get_server_config(
            host=host,
            port=port,
            use_ssl=ssl,
        )
        
        if not server_config['host']:
            click.echo("Error: No server host specified", err=True)
            click.echo("\nSpecify host via:")
            click.echo("  1. --host flag:   control-center connect --host 192.168.1.100")
            click.echo("  2. Config file:   control-center config set-server 192.168.1.100")
            sys.exit(1)
        
        # Validate inputs
        require_valid_host(server_config['host'])
        require_valid_port(server_config['port'])
        
        logger.info(f"Connecting to {server_config['host']}:{server_config['port']}...")

        connection_timeout = min(server_config['timeout'], 5)
        
        # Create gRPC client with token
        ctx.client = GRPCClient(
            host=server_config['host'],
            port=server_config['port'],
            timeout=connection_timeout,
            use_ssl=server_config['use_ssl']
        )
        
        # Set token for authentication
        ctx.client.set_token(api_token)

        import signal

        def timeout_handler(signum, frame):
            raise TimeoutError("Connection timed out")
        
        old_handler = signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(connection_timeout)

        try:
            if not ctx.client.connect():
                logger.error("Connection failed")
                sys.exit(1)
        except TimeoutError:
            click.echo(f"\nError: Connection timed out after {connection_timeout}s", err=True)
            click.echo("\nTroubleshooting:", err=True)
            click.echo("  1. Check if server is running: control-center server start", err=True)
            click.echo(f"  2. Verify host/port: {server_config['host']}:{server_config['port']}", err=True)
            click.echo("  3. Check network connectivity", err=True)
            sys.exit(1)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
        
        # Connect and get agent info
        try:
            if not ctx.client.connect():
                logger.error("Connection failed")
                sys.exit(1)
        except AuthenticationError as e:
            logger.error(f"Authentication failed: {e}")
            click.echo("\nAuthentication failed. Please check your API token.", err=True)
            sys.exit(1)
        except ConnectionError as e:
            logger.error(f"Connection error: {e}")
            click.echo(f"\n{e.suggest_action()}", err=True)
            sys.exit(1)
        
        logger.info("✓ Connected and authenticated")
        
        # Get agent info
        agent_info = ctx.client.get_agent_info()
        if not agent_info:
            logger.error("Failed to get agent information")
            sys.exit(1)
        
        # Initialize session and metrics
        session_id = str(uuid.uuid4())
        ctx.session = Session(
            user_id=session_id,
            host=server_config['host'],
            port=server_config['port'],
            os_type=agent_info['os_type'],
            os_version=agent_info['os_version']
        )
        ctx.metrics = MetricsCollector()
        
        # Initialize appropriate controller based on detected OS
        os_type = agent_info['os_type']
        logger.info(f"Detected OS: {os_type}")
        
        if os_type == 'WINDOWS':
            ctx.controller = WindowsActuation(ctx.client)
        elif os_type == 'MACOS':
            ctx.controller = MacOSActuation(ctx.client)
        elif os_type == 'LINUX':
            ctx.controller = LinuxActuation(ctx.client)
        else:
            logger.error(f"Unsupported OS type: {os_type}")
            sys.exit(1)
        
        logger.info(f"Initialized {os_type} actuation controller")
        
        # Print banner
        _print_banner(agent_info)
        
        # Enter interactive mode with PERSISTENT connection
        _interactive_mode(ctx.controller)
        
    except ValidationError as e:
        logger.error(f"Validation error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("\nInterrupted by user")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        ctx.cleanup()

# ============================================================================
# Interactive Mode Implementation
# ============================================================================
def _print_banner(agent_info: dict):
    """Print connection banner"""
    banner = f"""
╔══════════════════════════════════════════════════════════════════════╗
║          Control Center - Interactive Mode                           ║
╠══════════════════════════════════════════════════════════════════════╣
║ Connected to: {agent_info['os_type']} {agent_info['os_version']:<38} ║
║ Agent Version: {agent_info['agent_version']:<43}                     ║
╠══════════════════════════════════════════════════════════════════════╣
║ Commands:                                                            ║
║   help                  - Show available commands                    ║
║   status                - Show connection status                     ║
║   exit, quit            - Disconnect and exit                        ║
╚══════════════════════════════════════════════════════════════════════╝
"""
    click.echo(banner)

# ============================================================================
# Interactive Command Loop with Persistent Connection
# =============================================================================

def _interactive_mode(controller):
    """Interactive command loop with PERSISTENT connection"""
    command_count = 0
    consecutive_failures = 0
    max_failures = 3
    
    while not ctx.interrupted:
        try:
            if ctx.client and not ctx.client.is_connected():
                consecutive_failures += 1
                logger.warning(f"Connection lost (failure {consecutive_failures}/{max_failures})")
                
                if consecutive_failures >= max_failures:
                    logger.error("Multiple connection failures - VM likely shutdown")
                
                    click.echo("\n" + "="*70, err=True)
                    click.echo("╔══════════════════════════════════════════════════════════════════╗", err=True)
                    click.echo("║                  VM/CONTAINER HAS BEEN SHUT DOWN                 ║", err=True)
                    click.echo("╠══════════════════════════════════════════════════════════════════╣", err=True)
                    click.echo("║ The target VM/Container is no longer accessible.                 ║", err=True)
                    click.echo("║ Connection cannot be restored.                                   ║", err=True)
                    click.echo("║                                                                  ║", err=True)
                    click.echo("║ Session will be terminated.                                      ║", err=True)
                    click.echo("╚══════════════════════════════════════════════════════════════════╝", err=True)
                    click.echo("="*70 + "\n", err=True)

                    if ctx.session:
                        ctx.session.mark_vm_shutdown()

                    logger.error("Session terminated due to VM shutdown")
                    break
            
                if ctx.session and ctx.session.should_attempt_reconnection():
                    click.echo(f"\n[!] Connection lost. Reconnection attempt {ctx.session.reconnection_attempts + 1}/{ctx.session.max_reconnection_attempts}...", err=True)
                    ctx.session.record_reconnection_attempt()
                    
                    try:
                        if ctx.client.connect():
                            ctx.session.record_reconnection_success()
                            consecutive_failures = 0
                            click.echo("[✓] Reconnected successfully!\n")
                            continue
                    except Exception as e:
                        logger.warning(f"Reconnection failed: {e}")
                elif not ctx.session:
                    # No session tracking - show simple message and try once
                    click.echo("\n[!] Connection lost. Attempting to reconnect...", err=True)
                    try:
                        if ctx.client.connect():
                            consecutive_failures = 0
                            click.echo("[✓] Reconnected successfully!\n")
                            continue
                    except:
                        pass
                
                # If we got here, reconnection failed
                click.echo("\n[!] Connection to server lost and reconnection failed", err=True)
                break
            else:
                consecutive_failures = 0
            
            # Get user input
            user_input = click.prompt("control-center>", prompt_suffix=" ", default="", show_default=False)
            user_input = user_input.strip()
            
            if not user_input:
                continue
            
            # Handle special commands
            if user_input.lower() in ['exit', 'quit', 'q']:
                logger.info("Exiting...")
                break
            
            if user_input.lower() == 'help':
                controller.show_help()
                continue
            
            if user_input.lower() == 'status':
                if ctx.client and ctx.session and ctx.metrics:
                    report = StatusReporter.generate_status_report(
                        ctx.session,
                        ctx.metrics,
                        ctx.client
                    )
                    StatusReporter.print_status_report(report)
                else:
                    click.echo("Status information not available", err=True)
                continue
            
            if user_input.lower() == 'clear':
                os.system('clear' if os.name != 'nt' else 'cls')
                continue
            
            # Execute command
            command_count += 1
            try:
                success = controller.execute_command(user_input)
                
                if success and ctx.session:
                    ctx.session.update_activity()
                    consecutive_failures = 0

                if success:
                    logger.debug(f"Command {command_count} executed successfully")
                else:
                    logger.debug(f"Command {command_count} failed")
                    
            except RateLimitError as e:
                wait_time = e.get_wait_time()
                click.echo(f"[!] {e.suggest_action()}", err=True)
                import time
                time.sleep(wait_time)
            except AuthenticationError:
                logger.error("Session expired")
                click.echo("[!] Session expired. Please reconnect.", err=True)
                break
            except ConnectionError as e:
                if isinstance(e, VMShutdownError):
                    click.echo(f"\n[✗] {e.suggest_action()}", err=True)
                    if ctx.session:
                        ctx.session.mark_vm_shutdown()
                    break
                else:
                    logger.error(f"Connection error: {e}")
                    click.echo(f"[✗] Connection Error: {e}", err=True)
                    consecutive_failures += 1
            except Exception as e:
                logger.error(f"Command execution error: {e}")
                click.echo(f"[✗] Error: {e}", err=True)
        
        except KeyboardInterrupt:
            click.echo("\n[*] Interrupted. Type 'exit' to disconnect.")
            continue
        except EOFError:
            logger.info("EOF detected. Disconnecting...")
            break
        except Exception as e:
            logger.error(f"Error in interactive mode: {e}", exc_info=True)
            click.echo(f"[✗] Error: {e}", err=True)

    if ctx.session and ctx.session.is_vm_shutdown():
        click.echo("\n[*] Session ended due to VM shutdown")
    else:
        click.echo("\n[*] Disconnecting...")

# ============================================================================
# One-Time Command Execution (No persistent connection)
# ============================================================================

@cli.command()
@click.option('--host', help='Server host')
@click.option('--port', type=int, help='Server port')
@click.option('--token', envvar='CONTROL_CENTER_TOKEN', help='API token')
@click.option('--command', '-c', required=True, help='Single command to execute')
@click.option('--ssl', is_flag=True, help='Use SSL/TLS')
def execute(host: Optional[str], port: Optional[int], token: Optional[str], command: str, ssl: bool):
    """Execute a single command WITHOUT persistent connection (one-off execution)
    
    This connects, executes one command, and immediately disconnects.
    Use this for scripting or one-time commands.
    
    Examples:
        control-center execute --host 192.168.1.100 --token abc123 -c "960 540 left"
        control-center execute -c "type Hello World"  # Uses config
    """
    try:
        # Get token
        api_token = ctx.config_manager.get_token(cli_token=token)
        if not api_token:
            click.echo("Error: No API token provided", err=True)
            sys.exit(1)
        
        # Get server config
        server_config = ctx.config_manager.get_server_config(host=host, port=port, use_ssl=ssl)
        if not server_config['host']:
            click.echo("Error: No server host specified", err=True)
            sys.exit(1)
        
        # Connect
        ctx.client = GRPCClient(
            host=server_config['host'],
            port=server_config['port'],
            timeout=5,
            use_ssl=server_config['use_ssl']
        )
        ctx.client.set_token(api_token)
        
        if not ctx.client.connect():
            sys.exit(1)
        
        # Get agent info and create controller
        agent_info = ctx.client.get_agent_info()
        if not agent_info:
            logger.error("Failed to get agent information")
            sys.exit(1)
        
        os_type = agent_info['os_type']
        
        if os_type == 'WINDOWS':
            controller = WindowsActuation(ctx.client)
        elif os_type == 'MACOS':
            controller = MacOSActuation(ctx.client)
        elif os_type == 'LINUX':
            controller = LinuxActuation(ctx.client)
        else:
            sys.exit(1)
        
        # Execute single command
        success = controller.execute_command(command)
        sys.exit(0 if success else 1)
    
    except VMShutdownError as e:
        click.echo("\n" + "="*70, err=True)
        click.echo("ERROR: VM/Container is not accessible", err=True)
        click.echo("="*70, err=True)
        click.echo("\nThe target VM/Container may be:", err=True)
        click.echo("  • Shut down or powered off", err=True)
        click.echo("  • Not running", err=True)
        click.echo("  • Network unreachable", err=True)
        click.echo("  • Agent service not started\n", err=True)
        click.echo("Please verify VM/Container status and try again.\n", err=True)
        logger.error(f"VM/Container unreachable: {e}")
        sys.exit(2)

    except Exception as e:
        # Check if it's a gRPC unavailable error
        error_str = str(e).lower()
        if 'unavailable' in error_str or 'failed to connect' in error_str:
            click.echo("\n" + "="*70, err=True)
            click.echo("ERROR: Cannot connect to VM/Container", err=True)
            click.echo("="*70, err=True)
            click.echo("\nPossible causes:", err=True)
            click.echo("  • VM/Container is shut down", err=True)
            click.echo("  • Server/Agent not running", err=True)
            click.echo("  • Network connectivity issues", err=True)
            click.echo("  • Incorrect host/port\n", err=True)
            logger.error(f"Connection failed: {e}")
            sys.exit(2)
        else:
            logger.error(f"Execution failed: {e}")
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
    finally:
        ctx.cleanup()

# ============================================================================
# Update and Uninstall Commands
# ============================================================================
@cli.command()
@click.option('--check-only', is_flag=True, help='Only check for updates without installing')
def update(check_only):
    """Check for updates and install the latest version
    
    Options:
        --check-only: Only check for updates without installing
    
    Examples:
        control-center update              # Check and install updates
        control-center update --check-only # Just check for updates
    """
    
    import platform
    import tempfile
    import stat
    import json
    
    click.echo("Checking for updates...")
    click.echo(f"Current version: v{__version__}")
    
    # GitHub repository info - defined here to be accessible in except blocks
    GITHUB_REPO = "nullvoider07/control-center"  # TODO: Update this!
    API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    
    try:
        # Get latest release info
        try:
            # Try using requests if available (better), fallback to urllib
            try:
                import requests
                response = requests.get(API_URL, timeout=10)
                response.raise_for_status()
                release_data = response.json()
            except ImportError:
                import urllib.request
                with urllib.request.urlopen(API_URL) as response:
                    release_data = json.loads(response.read().decode())
        except Exception as e:
            click.echo(click.style(f"[ERROR] Failed to check for updates: {e}", fg='red'), err=True)
            click.echo("Please check your internet connection and try again.", err=True)
            click.echo(f"You can manually check: https://github.com/{GITHUB_REPO}/releases")
            sys.exit(1)
        
        # Parse version info
        latest_tag = release_data['tag_name']
        latest_version = latest_tag.lstrip('v')
        
        click.echo(f"Latest version: v{latest_version}")
        
        # Compare versions
        current_version = __version__
        if latest_version == current_version:
            click.echo(click.style("✓ You already have the latest version!", fg='green'))
            return
        
        click.echo(click.style(f"→ New version available: v{latest_version}", fg='yellow'))
        
        if check_only:
            click.echo("\nTo install the update, run:")
            click.echo("  control-center update")
            return
        
        # Confirm update
        if not click.confirm('\nDo you want to update now?'):
            click.echo("Update cancelled.")
            return
        
        # Detect platform and architecture
        os_type = platform.system().lower()
        machine = platform.machine().lower()
        
        # Map platform names
        if os_type == 'darwin':
            os_name = 'macos'
        elif os_type == 'linux':
            os_name = 'linux'
        elif os_type == 'windows':
            os_name = 'windows'
        else:
            click.echo(click.style(f"[ERROR] Unsupported OS: {os_type}", fg='red'), err=True)
            click.echo("Supported platforms: Linux, macOS, Windows")
            sys.exit(1)
        
        # Map architecture
        if machine in ['x86_64', 'amd64']:
            arch = 'x64'
        elif machine in ['arm64', 'aarch64']:
            arch = 'arm64'
        elif machine in ['i386', 'i686']:
            arch = 'x86'
        else:
            click.echo(click.style(f"[ERROR] Unsupported architecture: {machine}", fg='red'), err=True)
            click.echo(f"Supported architectures: x64, arm64, x86")
            sys.exit(1)
        
        # Construct download filename
        platform_suffix = f"{os_name}-{arch}"
        
        # Find the download URL
        download_url = None
        asset_name = None
        for asset in release_data['assets']:
            if platform_suffix in asset['name']:
                download_url = asset['browser_download_url']
                asset_name = asset['name']
                break
        
        if not download_url or not asset_name:
            click.echo(click.style(f"[ERROR] No release found for {os_name} {arch}", fg='red'), err=True)
            click.echo(f"Available assets:")
            for asset in release_data['assets']:
                click.echo(f"  - {asset['name']}")
            sys.exit(1)
        
        click.echo(f"\nDownloading {asset_name}...")
        
        # Create temp directory
        temp_dir = tempfile.mkdtemp()
        temp_file = os.path.join(temp_dir, asset_name)
        
        try:
            # Download the release
            try:
                import requests
                download_response = requests.get(download_url, stream=True, timeout=30)
                download_response.raise_for_status()
                
                # Save with progress
                with open(temp_file, 'wb') as f:
                    for chunk in download_response.iter_content(chunk_size=8192):
                        f.write(chunk)
            except ImportError:
                import urllib.request
                urllib.request.urlretrieve(download_url, temp_file)
            
            click.echo(click.style("✓ Download complete", fg='green'))
            
            # Extract archive
            click.echo("Extracting archive...")
            
            if os_name == 'windows':
                import zipfile
                with zipfile.ZipFile(temp_file, 'r') as zip_ref:
                    zip_ref.extractall(temp_dir)
            else:
                import tarfile
                with tarfile.open(temp_file, 'r:gz') as tar:
                    tar.extractall(temp_dir)
            
            # Determine installation directory
            if os_name == 'windows':
                install_dir = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'ControlCenter' / 'bin'
            else:
                # Check where current binary is installed
                current_binary_path = shutil.which('control-center')
                if current_binary_path:
                    install_dir = Path(current_binary_path).parent
                else:
                    install_dir = Path.home() / '.local' / 'bin'
            
            # Find the extracted binaries
            # Try multiple possible locations
            possible_bin_dirs = [
                Path(temp_dir) / 'bin',
                Path(temp_dir) / 'package' / 'bin',
                Path(temp_dir),
            ]
            
            extracted_bin_dir = None
            for bin_dir in possible_bin_dirs:
                if bin_dir.exists():
                    extracted_bin_dir = bin_dir
                    break
            
            if not extracted_bin_dir:
                click.echo(click.style("[ERROR] Binary directory not found in archive", fg='red'), err=True)
                click.echo("Archive contents:")
                for item in Path(temp_dir).rglob('*'):
                    click.echo(f"  {item.relative_to(temp_dir)}")
                shutil.rmtree(temp_dir)
                sys.exit(1)
            
            # Install binaries
            click.echo(f"Installing to {install_dir}...")
            install_dir.mkdir(parents=True, exist_ok=True)
            
            if os_name == 'windows':
                binaries = ['control-center.exe', 'control-center-server.exe', 'control-center-agent.exe']
            else:
                binaries = ['control-center', 'control-center-server', 'control-center-agent']
            
            installed_count = 0
            for binary in binaries:
                src = extracted_bin_dir / binary
                dst = install_dir / binary
                
                if src.exists():
                    # Handle Windows file-in-use issues
                    if os_name == 'windows' and dst.exists():
                        try:
                            # Rename old binary
                            old_binary = install_dir / f"{binary}.old"
                            if old_binary.exists():
                                try:
                                    old_binary.unlink()
                                except:
                                    pass
                            dst.rename(old_binary)
                        except Exception as e:
                            click.echo(click.style(f"[WARNING] Could not replace {binary}: {e}", fg='yellow'), err=True)
                            click.echo("The binary might be in use. Please close all Control Center processes and try again.", err=True)
                            continue
                    
                    # Copy new binary
                    shutil.copy2(src, dst)
                    
                    # Make executable on Unix-like systems
                    if os_name != 'windows':
                        os.chmod(dst, os.stat(dst).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                    
                    click.echo(click.style(f"  ✓ Updated {binary}", fg='green'))
                    installed_count += 1
                else:
                    click.echo(click.style(f"  - {binary} not found in archive (optional)", fg='yellow'))
            
            # Clean up
            shutil.rmtree(temp_dir)
            
            # Remove old binaries on Windows
            if os_name == 'windows':
                for binary in binaries:
                    old_binary = install_dir / f"{binary}.old"
                    if old_binary.exists():
                        try:
                            old_binary.unlink()
                        except:
                            pass
            
            if installed_count == 0:
                click.echo(click.style("\n[ERROR] No binaries were installed", fg='red'), err=True)
                sys.exit(1)
            
            click.echo("\n" + "="*60)
            click.echo(click.style(f"✓ Successfully updated to v{latest_version}!", fg='green', bold=True))
            click.echo("="*60)
            click.echo("\nRestart any running Control Center processes to use the new version.")
            click.echo("Run 'control-center version' to verify the update.")
            
        except Exception as e:
            # Clean up on error
            if Path(temp_dir).exists():
                shutil.rmtree(temp_dir)
            raise
    
    except Exception as e:
        logger.error(f"Update failed: {e}")
        click.echo(click.style(f"\n[ERROR] Update failed: {e}", fg='red'), err=True)
        click.echo("\nIf the problem persists, you can:")
        click.echo(f"1. Manually download from: https://github.com/{GITHUB_REPO}/releases/latest")
        click.echo("2. Report the issue: https://github.com/{GITHUB_REPO}/issues")
        sys.exit(1)

# Uninstall command with confirmation
@cli.command()
@click.option('--purge', is_flag=True, help='Also remove configuration files and data')
@click.option('--yes', '-y', is_flag=True, help='Skip confirmation prompts')
def uninstall(purge, yes):
    """Uninstall Control Center from your system
    
    Options:
        --purge: Also remove configuration files and logs
        --yes, -y: Skip confirmation prompts
    
    Examples:
        control-center uninstall           # Remove binaries only
        control-center uninstall --purge   # Remove everything
        control-center uninstall -y        # Skip confirmation
    """
    
    click.echo("="*60)
    click.echo("Control Center - Uninstall")
    click.echo("="*60)
    click.echo("")
    
    # Detect OS
    os_type = platform.system().lower()
    
    # Track what will be removed
    paths_to_remove = []
    config_paths = []
    
    # 1. Find installed binaries
    click.echo("Scanning for installed components...")
    
    if os_type == 'windows':
        binary_locations = [
            Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'ControlCenter' / 'bin',
            Path.home() / '.local' / 'bin',
        ]
        binary_names = ['control-center.exe', 'control-center-server.exe', 'control-center-agent.exe']
    else:
        binary_locations = [
            Path('/usr/local/bin'),
            Path.home() / '.local' / 'bin',
        ]
        binary_names = ['control-center', 'control-center-server', 'control-center-agent']
    
    # Find installed binaries
    found_binaries = []
    for location in binary_locations:
        if location.exists():
            for binary in binary_names:
                binary_path = location / binary
                if binary_path.exists():
                    found_binaries.append(binary_path)
                    paths_to_remove.append(binary_path)
                
                # Also check for .old versions
                old_binary = location / f"{binary}.old"
                if old_binary.exists():
                    paths_to_remove.append(old_binary)
    
    # 2. Configuration files (only if --purge)
    if purge:
        config_dir = ConfigManager.CONFIG_DIR
        if config_dir.exists():
            config_paths.append(config_dir)
        
        # Also check for logs
        log_locations = [
            Path.home() / '.local' / 'share' / 'control-center' / 'logs',
            Path.home() / '.control-center' / 'logs',
            Path('/var/log/control-center') if os_type != 'windows' else None,
        ]
        
        for log_dir in [l for l in log_locations if l]:
            if log_dir and log_dir.exists():
                config_paths.append(log_dir)
    
    # Display what will be removed
    click.echo("")
    click.echo("The following components will be removed:")
    click.echo("")
    
    if found_binaries:
        click.echo(click.style("Binaries:", fg='yellow', bold=True))
        for binary in found_binaries:
            click.echo(f"  - {binary}")
        click.echo("")
    else:
        click.echo(click.style("Binaries:", fg='yellow', bold=True))
        click.echo("  - None found")
        click.echo("")
    
    if config_paths:
        click.echo(click.style("Configuration & Data:", fg='yellow', bold=True))
        for path in config_paths:
            click.echo(f"  - {path}")
        click.echo("")
    elif purge:
        click.echo(click.style("Configuration & Data:", fg='yellow', bold=True))
        click.echo("  - None found")
        click.echo("")
    
    # Calculate total size
    total_size = 0
    for path in paths_to_remove + config_paths:
        if path.exists():
            if path.is_file():
                total_size += path.stat().st_size
            elif path.is_dir():
                total_size += sum(f.stat().st_size for f in path.rglob('*') if f.is_file())
    
    if total_size > 0:
        size_mb = total_size / (1024 * 1024)
        click.echo(f"Total disk space to be freed: {size_mb:.2f} MB")
        click.echo("")
    
    # Nothing to remove
    if not found_binaries and not config_paths:
        click.echo(click.style("✓ Control Center is not installed on this system", fg='green'))
        return
    
    # Confirm removal
    if not yes:
        click.echo(click.style("⚠ This action cannot be undone!", fg='red', bold=True))
        if not click.confirm('Do you want to continue?'):
            click.echo("\nUninstall cancelled.")
            return
    
    click.echo("")
    click.echo("Uninstalling...")
    click.echo("")
    
    # Track results
    removed = []
    failed = []
    
    # 1. Remove binaries
    for binary_path in paths_to_remove:
        try:
            if binary_path.exists():
                binary_path.unlink()
                removed.append(str(binary_path))
                click.echo(click.style(f"  ✓ Removed: {binary_path}", fg='green'))
        except PermissionError:
            # Handle Windows file-in-use
            if os_type == 'windows':
                try:
                    # Rename and schedule for deletion
                    temp_path = binary_path.with_suffix('.delete_me')
                    if temp_path.exists():
                        try:
                            temp_path.unlink()
                        except:
                            pass
                    
                    binary_path.rename(temp_path)
                    
                    # Schedule deletion after reboot
                    import subprocess
                    cmd = f'cmd /c ping 127.0.0.1 -n 3 > nul & del "{temp_path}"'
                    subprocess.Popen(
                        cmd,
                        shell=True,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                    )
                    
                    removed.append(str(binary_path))
                    click.echo(click.style(f"  ✓ Scheduled for deletion: {binary_path}", fg='green'))
                    continue
                except Exception:
                    pass
            
            failed.append((str(binary_path), "Permission denied (File in use)"))
            click.echo(click.style(f"  ✗ Failed: {binary_path} (File in use)", fg='red'))
            
        except Exception as e:
            failed.append((str(binary_path), str(e)))
            click.echo(click.style(f"  ✗ Failed: {binary_path} ({e})", fg='red'))
    
    # 2. Remove configuration (if --purge)
    if config_paths:
        click.echo("")
        for config_path in config_paths:
            try:
                if config_path.exists():
                    if config_path.is_dir():
                        shutil.rmtree(config_path)
                    else:
                        config_path.unlink()
                    removed.append(str(config_path))
                    click.echo(click.style(f"  ✓ Removed: {config_path}", fg='green'))
            except Exception as e:
                failed.append((str(config_path), str(e)))
                click.echo(click.style(f"  ✗ Failed: {config_path} ({e})", fg='red'))
    
    # 3. Remove empty parent directories
    if os_type == 'windows':
        parent_dir = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'ControlCenter'
        try:
            if parent_dir.exists() and not any(parent_dir.iterdir()):
                parent_dir.rmdir()
                click.echo(click.style(f"  ✓ Removed empty directory: {parent_dir}", fg='green'))
        except Exception:
            pass
    else:
        parent_dirs = [
            Path.home() / '.local' / 'share' / 'control-center',
            Path.home() / '.control-center',
        ]
        for parent_dir in parent_dirs:
            try:
                if parent_dir.exists() and not any(parent_dir.iterdir()):
                    parent_dir.rmdir()
                    click.echo(click.style(f"  ✓ Removed empty directory: {parent_dir}", fg='green'))
            except Exception:
                pass
    
    # 4. Clean up PATH environment variable (Windows Only)
    if os_type == 'windows':
        try:
            import winreg
            
            # Open the User Environment Key
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                'Environment',
                0,
                winreg.KEY_ALL_ACCESS
            )
            
            # Read the current PATH
            try:
                path_value, _ = winreg.QueryValueEx(key, 'Path')
            except FileNotFoundError:
                path_value = ""
            
            # Define the path fragment to look for
            control_center_fragment = str(Path('Programs/ControlCenter/bin')).lower()
            
            # Filter the paths
            new_paths = []
            changed = False
            
            if path_value:
                for part in path_value.split(';'):
                    if control_center_fragment in part.lower().replace('/', '\\'):
                        click.echo(click.style(f"  ✓ Removing from PATH: {part}", fg='green'))
                        changed = True
                    elif part.strip():
                        new_paths.append(part)
            
            # Save back if changed
            if changed:
                new_path_str = ';'.join(new_paths)
                winreg.SetValueEx(key, 'Path', 0, winreg.REG_EXPAND_SZ, new_path_str)
                click.echo(click.style("  ✓ Updated Windows PATH variable", fg='green'))
                
                # Notify the system about the environment variable change
                try:
                    import ctypes
                    HWND_BROADCAST = 0xFFFF
                    WM_SETTINGCHANGE = 0x001A
                    SMTO_ABORTIFHUNG = 0x0002
                    result = ctypes.c_long()
                    ctypes.windll.user32.SendMessageTimeoutW(
                        HWND_BROADCAST, WM_SETTINGCHANGE, 0, 'Environment',
                        SMTO_ABORTIFHUNG, 5000, ctypes.byref(result)
                    )
                except:
                    pass
            
            winreg.CloseKey(key)
            
        except Exception as e:
            click.echo(click.style(f"  ! Warning: Could not remove from PATH: {e}", fg='yellow'))
    
    # Summary
    click.echo("")
    click.echo("="*60)
    
    if removed and not failed:
        click.echo(click.style("✓ Uninstall completed successfully!", fg='green', bold=True))
        click.echo("")
        click.echo(f"Removed {len(removed)} item(s):")
        # Show first 5, truncate if more
        for item in removed[:5]:
            click.echo(f"  - {item}")
        if len(removed) > 5:
            click.echo(f"  ... and {len(removed) - 5} more")
    
    elif removed and failed:
        click.echo(click.style("⚠ Uninstall partially completed", fg='yellow', bold=True))
        click.echo("")
        click.echo(f"Successfully removed: {len(removed)} item(s)")
        click.echo(f"Failed to remove: {len(failed)} item(s)")
        click.echo("")
        click.echo("Failed items:")
        for path, error in failed:
            click.echo(f"  - {path}: {error}")
        click.echo("")
        if os_type != 'windows':
            click.echo("Tip: Try running with sudo for system-wide installations:")
            click.echo("  sudo control-center uninstall -y")
    
    elif not removed and failed:
        click.echo(click.style("✗ Uninstall failed", fg='red', bold=True))
        click.echo("")
        click.echo("Failed to remove:")
        for path, error in failed:
            click.echo(f"  - {path}: {error}")
        sys.exit(1)
    
    click.echo("")
    click.echo("Thank you for using Control Center!")
    click.echo("We'd appreciate your feedback: https://github.com/your-username/control-center/issues")

# Config Commands
@cli.group()
def config():
    """Manage configuration"""
    pass

# Config subcommands
@config.command('show')
def config_show():
    """Show current configuration"""
    try:
        config_data = ctx.config_manager.get_all()
        
        click.echo("\n=== Control Center Configuration ===\n")
        click.echo(f"Config file: {ctx.config_manager.CONFIG_FILE}\n")
        
        for key, value in config_data.items():
            click.echo(f"{key}: {value}")
        
        click.echo()
    
    except ConfigurationError as e:
        click.echo(f"Configuration error: {e}", err=True)
        sys.exit(1)
        
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

# Set token command
@config.command('set-token')
@click.argument('token')
def config_set_token(token: str):
    """Set API token in config file"""
    try:
        ctx.config_manager.set_token(token)
        click.echo("✓ API token saved to config")
    except ConfigurationError as e:
        click.echo(f"Configuration error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

# Set server command
@config.command('set-server')
@click.argument('host')
@click.argument('port', type=int, default=50051)
def config_set_server(host: str, port: int):
    """Set default server host and port"""
    try:
        require_valid_host(host)
        require_valid_port(port)
        ctx.config_manager.set_server(host, port)
        click.echo(f"✓ Default server set to {host}:{port}")
    except ConfigurationError as e:
        click.echo(f"Configuration error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

# Clear token command
@config.command('clear-token')
def config_clear_token():
    """Remove API token from config"""
    try:
        ctx.config_manager.clear_token()
        click.echo("✓ API token cleared from config")
    except ConfigurationError as e:
        click.echo(f"Configuration error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

# Validate config command
@config.command('validate')
def config_validate():
    """Validate current configuration"""
    try:
        is_valid, errors = ctx.config_manager.validate()
        
        if is_valid:
            click.echo("✓ Configuration is valid")
        else:
            click.echo("✗ Configuration is invalid", err=True)
            for error in errors:
                click.echo(f"  - {error}", err=True)
            sys.exit(1)
            
    except ConfigurationError as e:
        click.echo(f"Configuration error: {e}", err=True)
        sys.exit(1)

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

# Reset config command
@config.command('reset')
@click.confirmation_option(prompt='Reset configuration to defaults?')
def config_reset():
    """Reset configuration to defaults"""
    try:
        ctx.config_manager.reset()
        click.echo("✓ Configuration reset to defaults")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

# Create default config command
@config.command('init')
def config_init():
    """Create default configuration file"""
    try:
        from controller.management.config_manager import create_default_config
        create_default_config()
    except ConfigurationError as e:
        click.echo(f"Configuration error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

# ============================================================================
# Info Commands
# ============================================================================

@cli.command()
def version():
    """Show version information"""
    click.echo(f"Control Center v{__version__}")
    click.echo("")
    click.echo("Components:")
    
    # Check for server binary
    server_bin = _find_binary('control-center-server')
    if server_bin:
        click.echo(f"  Server: {server_bin}")
    else:
        click.echo("  Server: Not found")
    
    # Check for agent binary
    agent_bin = _find_binary('control-center-agent')
    if agent_bin:
        click.echo(f"  Agent:  {agent_bin}")
    else:
        click.echo("  Agent:  Not found")
    
    click.echo(f"  CLI:    Python v{__version__}")

# Diagnostics command
@cli.command()
def doctor():
    """Check system configuration and dependencies"""
    click.echo("=== Control Center System Check ===\n")
    
    # Check Python version
    import sys
    click.echo(f"Python: {sys.version.split()[0]} ✓")
    
    # Check for server binary
    server_bin = _find_binary('control-center-server')
    if server_bin:
        click.echo(f"Server binary: {server_bin} ✓")
    else:
        click.echo("Server binary: Not found ✗")
    
    # Check for agent binary
    agent_bin = _find_binary('control-center-agent')
    if agent_bin:
        click.echo(f"Agent binary: {agent_bin} ✓")
    else:
        click.echo("Agent binary: Not found ✗")
    
    # Check config file
    if ctx.config_manager.CONFIG_FILE.exists():
        click.echo(f"Config file: {ctx.config_manager.CONFIG_FILE} ✓")
    else:
        click.echo(f"Config file: Not found (run 'control-center config init')")
    
    # Check gRPC
    try:
        import grpc
        click.echo("gRPC: Installed ✓")
    except ImportError:
        click.echo("gRPC: Not installed ✗")
    
    click.echo("\n=== System Check Complete ===")

# Helper Functions - Binary Discovery
def _find_binary(binary_name: str) -> Optional[str]:
    """Find binary in common locations (following the-eye pattern)"""
    possible_locations = [
        # Current directory
        f"./{binary_name}",
        f"./bin/{binary_name}",
        
        # Build output
        f"./target/release/{binary_name}",
        
        # User local
        str(Path.home() / ".local" / "bin" / binary_name),
        
        # System
        f"/usr/local/bin/{binary_name}",
        
        # In PATH
        binary_name,
    ]
    
    for location in possible_locations:
        if os.path.exists(location):
            return location
    
    # Try to find in PATH
    path_binary = shutil.which(binary_name)
    if path_binary:
        return path_binary
    
    return None

# Server commands - Calls Rust Binary
@cli.group()
def server():
    """Manage Control Center server (Rust binary)"""
    pass

@server.command(name='start')
@click.option('--host', default='0.0.0.0', help='Host to bind to')
@click.option('--port', default=50051, help='gRPC port')
@click.option('--agent-host', default='127.0.0.1', help='Agent host address (IP of machine running agent)')
@click.option('--agent-port', default=50052, type=int, help='Agent port (default: 50052)')
@click.option('--auth-url', help='OAuth2 authorization URL')
@click.option('--token-url', help='OAuth2 token URL')
@click.option('--client-id', help='OAuth2 client ID')
def server_start(host, port, agent_host, agent_port, auth_url, token_url, client_id):
    """Start the Rust gRPC server
    
    The server listens for CLI connections on --host:--port and connects
    to the agent at --agent-host:--agent-port.
    
    Examples:
        # Local agent (same machine)
        control-center server start
        
        # Remote agent (e.g., Windows VM)
        control-center server start --agent-host 192.168.1.100
        
        # Custom ports
        control-center server start --port 8080 --agent-host 192.168.1.100 --agent-port 9090
    """
    click.echo(f"[START] Starting Control Center Server (Rust) on {host}:{port}")
    click.echo(f"[INFO] Will connect to agent at {agent_host}:{agent_port}")
    
    # Build environment variables
    env = os.environ.copy()
    env['GRPC_HOST'] = host
    env['GRPC_PORT'] = str(port)
    env['AGENT_HOST'] = agent_host
    env['AGENT_PORT'] = str(agent_port)
    
    if auth_url:
        env['OAUTH_AUTH_URL'] = auth_url
    if token_url:
        env['OAUTH_TOKEN_URL'] = token_url
    if client_id:
        env['OAUTH_CLIENT_ID'] = client_id
    
    # Find server binary
    server_bin = _find_binary('control-center-server')
    
    if not server_bin:
        click.echo("[ERROR] 'control-center-server' binary not found!", err=True)
        click.echo("", err=True)
        click.echo("The server binary should be installed alongside this CLI.", err=True)
        click.echo("Please reinstall Control Center or build from source:", err=True)
        click.echo("  cargo build --release -p control-center-server", err=True)
        sys.exit(1)
    
    try:
        click.echo(f"[INFO] Starting server: {server_bin}")
        subprocess.run([server_bin], env=env, check=True)
    except KeyboardInterrupt:
        click.echo("\n[INFO] Server stopped")
    except Exception as e:
        click.echo(f"[ERROR] Failed to start server: {e}", err=True)
        sys.exit(1)

# Agent commands - Calls Rust Binary (the-eye pattern)
@cli.group()
def agent():
    """Manage Control Center agent (Rust binary)"""
    pass

@agent.command(name='start')
@click.option('--server-host', default='127.0.0.1', help='Server host to connect to')
@click.option('--server-port', default=50051, help='Server gRPC port')
@click.option('--token', envvar='CONTROL_CENTER_TOKEN', help='Authentication token')
def agent_start(server_host, server_port, token):
    """Start the Rust agent on this machine"""
    click.echo(f"[START] Starting Control Center Agent (Rust)")
    click.echo(f"   Connecting to: {server_host}:{server_port}")
    
    # Build environment variables
    env = os.environ.copy()
    env['AGENT_SERVER_HOST'] = server_host
    env['AGENT_SERVER_PORT'] = str(server_port)
    
    if token:
        env['CONTROL_CENTER_TOKEN'] = token
    
    # Find agent binary
    agent_bin = _find_binary('control-center-agent')
    
    if not agent_bin:
        click.echo("[ERROR] 'control-center-agent' binary not found!", err=True)
        click.echo("", err=True)
        click.echo("The agent binary should be installed alongside this CLI.", err=True)
        click.echo("Please reinstall Control Center or build from source:", err=True)
        click.echo("  cargo build --release -p control-center-agent", err=True)
        sys.exit(1)
    
    try:
        click.echo(f"[INFO] Starting agent: {agent_bin}")
        subprocess.run([agent_bin], env=env, check=True)
    except KeyboardInterrupt:
        click.echo("\n[INFO] Agent stopped")
    except Exception as e:
        click.echo(f"[ERROR] Failed to start agent: {e}", err=True)
        sys.exit(1)

# Main entry point
def main():
    """Main entry point"""
    try:
        cli(prog_name='control-center')
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)

# Entry point for script execution
if __name__ == '__main__':
    main()