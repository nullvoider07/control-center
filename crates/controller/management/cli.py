"""CLI implementation with token-based authentication and persistent connections"""

import click
import sys
import os
import uuid
import signal
import shutil
import subprocess
from typing import Optional, Union
from pathlib import Path

from .config_manager import ConfigManager, ConfigurationError
from ..integrations.gRPC import GRPCClient, AuthenticationError, ConnectionError, RateLimitError
from ..integrations.exceptions import VMShutdownError
from ..integrations.status import StatusReporter
from ..core.session import Session
from ..core.metrics import MetricsCollector
from ..os_specific.windows_actuation import WindowsActuation
from ..os_specific.macos_actuation import MacOSActuation
from ..os_specific.linux_actuation import LinuxActuation
from ..utils.logger import setup_logger
from ..utils.validation import require_valid_host, require_valid_port, ValidationError

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
@click.option('--token', envvar='CONTROL_CENTER_TOKEN', help='API token (or set CONTROL_CENTER_TOKEN)')
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
        
        # Create gRPC client with token
        ctx.client = GRPCClient(
            host=server_config['host'],
            port=server_config['port'],
            timeout=server_config['timeout'],
            use_ssl=server_config['use_ssl']
        )
        
        # Set token for authentication
        ctx.client.set_token(api_token)
        
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
            timeout=server_config['timeout'],
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
def update():
    """Update Control Center to latest version
    
    Pulls latest changes from git and reinstalls the package.
    """
    try:
        click.echo("Updating Control Center...")
        
        # Get the package root directory
        pkg_dir = Path(__file__).parent.parent.parent
        
        # Pull latest changes
        click.echo("\n1. Pulling latest changes from git...")
        result = subprocess.run(
            ['git', 'pull', 'origin', 'main'],
            cwd=pkg_dir,
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0:
            click.echo(f"Git pull failed: {result.stderr}", err=True)
            sys.exit(1)
        
        click.echo(result.stdout)
        
        # Reinstall package
        click.echo("\n2. Reinstalling package...")
        result = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '-e', '.', '--upgrade'],
            cwd=pkg_dir,
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0:
            click.echo(f"Installation failed: {result.stderr}", err=True)
            sys.exit(1)
        
        click.echo(result.stdout)
        click.echo("\n✓ Update complete!")
        
    except Exception as e:
        logger.error(f"Update failed: {e}")
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# Uninstall command with confirmation
@cli.command()
@click.confirmation_option(prompt='Are you sure you want to uninstall Control Center?')
def uninstall():
    """Uninstall Control Center
    
    Removes the package and cleans up configuration files.
    """
    try:
        click.echo("Uninstalling Control Center...")
        
        # Remove config directory
        config_dir = ConfigManager.CONFIG_DIR
        if config_dir.exists():
            click.echo(f"\n1. Removing configuration directory: {config_dir}")
            import shutil
            shutil.rmtree(config_dir)
            click.echo("✓ Configuration removed")
        
        # Uninstall package
        click.echo("\n2. Uninstalling package...")
        result = subprocess.run(
            [sys.executable, '-m', 'pip', 'uninstall', 'control-center', '-y'],
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0:
            click.echo(f"Uninstall failed: {result.stderr}", err=True)
            sys.exit(1)
        
        click.echo(result.stdout)
        click.echo("\n✓ Uninstall complete!")
        click.echo("Goodbye! 👋")
        
    except Exception as e:
        logger.error(f"Uninstall failed: {e}")
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

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
            click.echo("✗ Configuration errors found:\n", err=True)
            for error in errors:
                click.echo(f"  - {error}", err=True)
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
        from .config_manager import create_default_config
        create_default_config()
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
@click.option('--host', default='0.0.0.0', help='Server host')
@click.option('--port', default=50051, help='gRPC port')
@click.option('--auth-url', help='OAuth2 authorization URL')
@click.option('--token-url', help='OAuth2 token URL')
@click.option('--client-id', help='OAuth2 client ID')
def server_start(host, port, auth_url, token_url, client_id):
    """Start the Rust gRPC server"""
    click.echo(f"[START] Starting Control Center Server (Rust) on {host}:{port}")
    
    # Build environment variables
    env = os.environ.copy()
    env['GRPC_HOST'] = host
    env['GRPC_PORT'] = str(port)
    
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