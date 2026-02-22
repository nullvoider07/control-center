"""CLI implementation with token-based authentication and persistent connections"""

import click
import sys
import os
import uuid
import signal
import shutil
import platform
import subprocess
import json
import time
import csv
import io
from typing import Optional, Union, List, Dict
from pathlib import Path

from controller.management.config_manager import ConfigManager, ConfigurationError
from controller.integrations.gRPC import GRPCClient, AuthenticationError, ConnectionError, RateLimitError
from controller.integrations.exceptions import VMShutdownError
from controller.integrations.status import StatusReporter
from controller.integrations.export import Exporter
from controller.core.session import Session
from controller.core.metrics import MetricsCollector
from controller.os_specific.windows_actuation import WindowsActuation
from controller.os_specific.macos_actuation import MacOSActuation
from controller.os_specific.linux_actuation import LinuxActuation
from controller.management.monitoring import monitoring
from controller.management.agent import AgentManager, AgentInfo
from controller.utils.logger import setup_logger, get_audit_logger
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
        # True only while _interactive_mode() is running.
        # The signal handler checks this to decide whether to exit the process
        # outright (non-interactive) or merely raise KeyboardInterrupt so the
        # interactive loop can handle Ctrl+C gracefully (BUG-011 fix).
        self.in_interactive_mode = False
    
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
    """Handle Ctrl+C gracefully.

    BUG-011 FIX — Two distinct behaviours depending on whether the process is
    currently inside the interactive REPL or running a non-interactive command
    (batch, execute, server start, etc.):

    Interactive mode  (ctx.in_interactive_mode is True):
        - Do NOT set ctx.interrupted — that flag is the while-loop exit guard.
          Setting it here would cause the session to terminate on the very next
          loop iteration after the KeyboardInterrupt is handled.
        - Do NOT call ctx.cleanup() — that destroys the live gRPC session.
        - Do NOT call sys.exit() — that kills the whole process immediately,
          which was the exact symptom reported in BUG-011.
        - Instead, raise KeyboardInterrupt so it propagates into the interactive
          loop's  except KeyboardInterrupt  handler, which prints
          "[*] Interrupted. Type 'exit' to disconnect."  and  continue s.
          Raising from a signal handler is the standard Python idiom for
          delegating SIGINT handling back to normal exception-flow logic.

    Non-interactive mode  (ctx.in_interactive_mode is False):
        - Original behaviour preserved: set the interrupt flag, disconnect, and
          exit.  Batch jobs and one-shot execute commands terminate cleanly.
    """
    if ctx.in_interactive_mode:
        # Delegate to the interactive loop's except KeyboardInterrupt handler.
        # Do not touch ctx.interrupted, cleanup(), or sys.exit() here.
        raise KeyboardInterrupt
    else:
        logger.info("\nInterrupt received. Cleaning up...")
        ctx.interrupted = True
        ctx.cleanup()
        sys.exit(0)

# Register signal handler
signal.signal(signal.SIGINT, signal_handler)

# ============================================================================
# Helper Utilities
# ============================================================================

# Last-session persistence path (written on disconnect, read by `session` commands)
_SESSION_DATA_PATH = Path.home() / '.config' / 'control-center' / 'last_session.json'

def _get_server_config(host=None, port=None, use_ssl=False):
    """Resolve host/port from CLI flags or saved config."""
    return ctx.config_manager.get_server_config(host=host, port=port, use_ssl=use_ssl)

def _resolve_host_port(host, port, use_ssl=False):
    """Resolve host/port: CLI flags > saved config. Exits if host not found."""
    server_config = _get_server_config(host=host, port=port, use_ssl=use_ssl)
    if not server_config['host']:
        click.echo("Error: No server host. Use --host or run 'config set-server HOST'", err=True)
        sys.exit(1)
    return server_config['host'], server_config['port']

def _resolve_token(token):
    """Resolve token: CLI flag > CONTROL_CENTER_TOKEN env > config file. Returns None if absent."""
    return ctx.config_manager.get_token(cli_token=token)

def _get_no_auth_client(host: str, port: int, timeout: int = 10) -> GRPCClient:
    """Build a GRPCClient with channel + stub but NO authentication call.

    Used for no-auth RPCs: QueryConnections, QueryServers, GetServerIdentity,
    Ping, GetConnectionHistory.  Skips connect() so it works without a token.
    """
    from controller.integrations.proto import control_center_pb2_grpc
    client = GRPCClient(host=host, port=port, timeout=timeout)
    client.channel = client._create_channel()
    client.stub = control_center_pb2_grpc.ControlServiceStub(client.channel)
    return client

def _get_auth_client(host: str, port: int, token: str, timeout: int = 10) -> GRPCClient:
    """Build a fully-authenticated GRPCClient (calls connect() + validates token)."""
    client = GRPCClient(host=host, port=port, timeout=timeout)
    client.set_token(token)
    client.connect()
    return client

def _save_session_data():
    """Persist current session + metrics to disk so 'session' commands can read it after disconnect."""
    if not ctx.session or not ctx.metrics:
        return
    try:
        _SESSION_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {
            'session':  ctx.session.to_dict(),
            'metrics':  ctx.metrics.get_stats(),
            'commands': [dict(c) for c in getattr(ctx.metrics, 'command_history', [])],
            'saved_at': time.time(),
        }
        _SESSION_DATA_PATH.write_text(json.dumps(data, indent=2, default=str))
    except Exception as e:
        logger.debug(f"Could not save session data: {e}")

def _load_session_data() -> Optional[Dict]:
    """Load last-session data saved by _save_session_data()."""
    if not _SESSION_DATA_PATH.exists():
        return None
    try:
        return json.loads(_SESSION_DATA_PATH.read_text())
    except Exception:
        return None

def _fmt_ts(unix_ts) -> str:
    """Format a Unix timestamp to a human-readable string."""
    if not unix_ts:
        return "N/A"
    try:
        from datetime import datetime
        return datetime.fromtimestamp(float(unix_ts)).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return str(unix_ts)


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
    audit = get_audit_logger()
    session_id = str(uuid.uuid4())

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

        import signal as _signal

        def timeout_handler(signum, frame):
            raise TimeoutError("Connection timed out")
        
        old_handler = _signal.signal(_signal.SIGALRM, timeout_handler)
        _signal.alarm(connection_timeout)

        try:
            if not ctx.client.connect():
                logger.error("Connection failed")
                audit.log_auth_attempt(session_id, success=False,
                                       ip_address=server_config['host'],
                                       reason="connect() returned False")
                sys.exit(1)
        except TimeoutError:
            click.echo(f"\nError: Connection timed out after {connection_timeout}s", err=True)
            click.echo("\nTroubleshooting:", err=True)
            click.echo("  1. Check if server is running: control-center server start", err=True)
            click.echo(f"  2. Verify host/port: {server_config['host']}:{server_config['port']}", err=True)
            click.echo("  3. Check network connectivity", err=True)
            sys.exit(1)
        finally:
            _signal.alarm(0)
            _signal.signal(_signal.SIGALRM, old_handler)
        
        logger.info("Connected and authenticated")
        audit.log_auth_attempt(session_id, success=True, ip_address=server_config['host'])
        
        # Get agent info
        agent_info = ctx.client.get_agent_info()
        if not agent_info:
            logger.error("Failed to get agent information")
            sys.exit(1)
        
        # Initialize session and metrics
        ctx.session = Session(
            user_id=session_id,
            host=server_config['host'],
            port=server_config['port'],
            os_type=agent_info['os_type'],
            os_version=agent_info['os_version']
        )
        ctx.metrics = MetricsCollector()
        audit.log_session_start(session_id, session_id)
        
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
        
        # Enter interactive mode with PERSISTENT connection.
        # BUG-011 FIX: Raise the in_interactive_mode flag so signal_handler
        # knows to raise KeyboardInterrupt instead of calling sys.exit(0).
        # The try/finally guarantees the flag is always cleared, even if
        # _interactive_mode exits via an unexpected exception.
        ctx.in_interactive_mode = True
        try:
            _interactive_mode(ctx.controller)
        finally:
            ctx.in_interactive_mode = False
        
    except ValidationError as e:
        logger.error(f"Validation error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("\nInterrupted by user")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        # Persist session data before cleanup so 'session' commands can read it
        _save_session_data()
        if ctx.session:
            duration = ctx.session.duration_seconds if hasattr(ctx.session, 'duration_seconds') else 0
            audit.log_session_end(session_id, duration)
        ctx.cleanup()

# ============================================================================
# Interactive Mode Implementation
# ============================================================================
def _print_banner(agent_info: dict):
    """Print connection banner"""
    banner = f"""
\u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557
\u2551                   Control Center - Interactive Mode                  \u2551
\u2560\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2563
\u2551 Connected to: {agent_info['os_type']} {agent_info['os_version']:<38}           \u2551
\u2551 Agent Version: {agent_info['agent_version']:<43}           \u2551
\u2560\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2563
\u2551 Commands:                                                            \u2551
\u2551   help                  - Show available commands                    \u2551
\u2551   status                - Show connection status                     \u2551
\u2551   exit, quit            - Disconnect and exit                        \u2551
\u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d
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
                    click.echo("\u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557", err=True)
                    click.echo("\u2551                  VM/CONTAINER HAS BEEN SHUT DOWN                 \u2551", err=True)
                    click.echo("\u2560\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2563", err=True)
                    click.echo("\u2551 The target VM/Container is no longer accessible.                 \u2551", err=True)
                    click.echo("\u2551 Connection cannot be restored.                                   \u2551", err=True)
                    click.echo("\u2551                                                                  \u2551", err=True)
                    click.echo("\u2551 Session will be terminated.                                      \u2551", err=True)
                    click.echo("\u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d", err=True)
                    click.echo("="*70 + "\n", err=True)

                    if ctx.session:
                        ctx.session.mark_vm_shutdown()
                        audit = get_audit_logger()
                        audit.log_vm_shutdown(
                            ctx.session.user_id,
                            ctx.session.user_id,
                            ctx.session.host
                        )

                    logger.error("Session terminated due to VM shutdown")
                    break
            
                if ctx.session and ctx.session.should_attempt_reconnection():
                    click.echo(f"\n[!] Connection lost. Reconnection attempt "
                               f"{ctx.session.reconnection_attempts + 1}/"
                               f"{ctx.session.max_reconnection_attempts}...", err=True)
                    ctx.session.record_reconnection_attempt()
                    audit = get_audit_logger()
                    audit.log_reconnection_attempt(
                        ctx.session.user_id,
                        ctx.session.user_id,
                        ctx.session.reconnection_attempts
                    )
                    
                    try:
                        if ctx.client.connect():
                            ctx.session.record_reconnection_success()
                            consecutive_failures = 0
                            click.echo("[+] Reconnected successfully!\n")
                            continue
                    except Exception as e:
                        logger.warning(f"Reconnection failed: {e}")
                    continue
                elif ctx.session and not ctx.session.should_attempt_reconnection():
                    if ctx.session.reconnection_attempts >= ctx.session.max_reconnection_attempts:
                        click.echo("\n[!] Max reconnection attempts reached.", err=True)
                    else:
                        time.sleep(1)
                        continue
                elif not ctx.session:
                    click.echo("\n[!] Connection lost. Attempting to reconnect...", err=True)
                    try:
                        if ctx.client.connect():
                            consecutive_failures = 0
                            click.echo("[+] Reconnected successfully!\n")
                            continue
                    except Exception:
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
                # BUG-010 FIX: Time the execution so we can pass execution_time_ms
                # to record_command(). The controllers return only a bool, so we
                # measure wall-clock time around the call. This is the minimal
                # surgical change — no controller method signatures are altered.
                _cmd_start = time.time()
                success = controller.execute_command(user_input)
                _exec_ms = int((time.time() - _cmd_start) * 1000)

                # Record the command in the MetricsCollector so that session stats,
                # export, and the interactive `status` report all show real data
                # instead of perpetual zeroes (which was the observable symptom).
                if ctx.metrics:
                    ctx.metrics.record_command(user_input, success, _exec_ms)

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
                time.sleep(wait_time)
            except AuthenticationError:
                logger.error("Session expired")
                click.echo("[!] Session expired. Please reconnect.", err=True)
                break
            except ConnectionError as e:
                if isinstance(e, VMShutdownError):
                    click.echo(f"\n[x] {e.suggest_action()}", err=True)
                    if ctx.session:
                        ctx.session.mark_vm_shutdown()
                    break
                else:
                    logger.error(f"Connection error: {e}")
                    click.echo(f"[x] Connection Error: {e}", err=True)
                    consecutive_failures += 1
            except Exception as e:
                logger.error(f"Command execution error: {e}")
                click.echo(f"[x] Error: {e}", err=True)
        
        except KeyboardInterrupt:
            click.echo("\n[*] Interrupted. Type 'exit' to disconnect.")
            continue
        except EOFError:
            logger.info("EOF detected. Disconnecting...")
            break
        except Exception as e:
            logger.error(f"Error in interactive mode: {e}", exc_info=True)
            click.echo(f"[x] Error: {e}", err=True)

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
        click.echo("  - Shut down or powered off", err=True)
        click.echo("  - Not running", err=True)
        click.echo("  - Network unreachable", err=True)
        click.echo("  - Agent service not started\n", err=True)
        click.echo("Please verify VM/Container status and try again.\n", err=True)
        logger.error(f"VM/Container unreachable: {e}")
        sys.exit(2)

    except Exception as e:
        error_str = str(e).lower()
        if 'unavailable' in error_str or 'failed to connect' in error_str:
            click.echo("\n" + "="*70, err=True)
            click.echo("ERROR: Cannot connect to VM/Container", err=True)
            click.echo("="*70, err=True)
            click.echo("\nPossible causes:", err=True)
            click.echo("  - VM/Container is shut down", err=True)
            click.echo("  - Server/Agent not running", err=True)
            click.echo("  - Network connectivity issues", err=True)
            click.echo("  - Incorrect host/port\n", err=True)
            logger.error(f"Connection failed: {e}")
            sys.exit(2)
        else:
            logger.error(f"Execution failed: {e}")
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
    finally:
        ctx.cleanup()

# ============================================================================
# Batch Execution Command
# ============================================================================

@cli.command()
@click.option('--host', help='Server host')
@click.option('--port', type=int, help='Server port')
@click.option('--token', envvar='CONTROL_CENTER_TOKEN', help='API token')
@click.option('--ssl', is_flag=True, help='Use SSL/TLS')
@click.option('--file', '-f', 'input_file', required=True,
              type=click.Path(exists=True), help='Input file with commands')
@click.option('--format', 'fmt', default='auto',
              type=click.Choice(['auto', 'txt', 'json', 'ndjson', 'yaml', 'csv']),
              help='Input file format (default: auto-detect from extension)')
@click.option('--delay', default=0.0, type=float,
              help='Delay in seconds between commands (default: 0)')
@click.option('--stop-on-error', is_flag=True,
              help='Stop batch execution on first failed command')
@click.option('--output', '-o', default=None,
              help='Write results to this JSON file')
def batch(host, port, token, ssl, input_file, fmt, delay, stop_on_error, output):
    """Execute a batch of commands from a file
    
    Supported file formats:
      txt    - one command per line (# comments ignored)
      json   - list of strings, or list of {"command": "..."} objects
      ndjson - one JSON object per line: {"command": "..."}
      yaml   - list of command strings (requires PyYAML)
      csv    - first column is the command (header row skipped if text)
    
    Examples:
        control-center batch -f commands.txt
        control-center batch -f commands.json --stop-on-error
        control-center batch -f script.yaml --delay 0.5 -o results.json
    """
    try:
        api_token = _resolve_token(token)
        if not api_token:
            click.echo("Error: No API token provided", err=True)
            sys.exit(1)
        
        h, p = _resolve_host_port(host, port, use_ssl=ssl)
        
        ctx.client = GRPCClient(host=h, port=p, timeout=10,
                                use_ssl=ctx.config_manager.get_server_config(use_ssl=ssl)['use_ssl'])
        ctx.client.set_token(api_token)
        if not ctx.client.connect():
            sys.exit(1)
        
        agent_info = ctx.client.get_agent_info()
        if not agent_info:
            sys.exit(1)
        
        os_type = agent_info['os_type']
        if os_type == 'WINDOWS':
            controller = WindowsActuation(ctx.client)
        elif os_type == 'MACOS':
            controller = MacOSActuation(ctx.client)
        else:
            controller = LinuxActuation(ctx.client)
        
        # Detect format from extension if 'auto'
        ext = Path(input_file).suffix.lower().lstrip('.')
        resolved_fmt = ext if fmt == 'auto' else fmt
        if resolved_fmt not in ('txt', 'json', 'ndjson', 'yaml', 'csv'):
            resolved_fmt = 'txt'
        
        # Load commands
        commands: List[str] = []
        raw = Path(input_file).read_text(encoding='utf-8')
        
        if resolved_fmt == 'txt':
            for line in raw.splitlines():
                line = line.strip()
                if line and not line.startswith('#'):
                    commands.append(line)
        
        elif resolved_fmt == 'json':
            data = json.loads(raw)
            if not isinstance(data, list):
                click.echo("Error: JSON file must contain a list", err=True)
                sys.exit(1)
            for item in data:
                if isinstance(item, str):
                    commands.append(item)
                elif isinstance(item, dict):
                    commands.append(item.get('command', ''))
        
        elif resolved_fmt == 'ndjson':
            for line in raw.splitlines():
                line = line.strip()
                if line:
                    try:
                        obj = json.loads(line)
                        commands.append(obj.get('command', ''))
                    except json.JSONDecodeError:
                        pass
        
        elif resolved_fmt == 'yaml':
            try:
                import yaml
                data = yaml.safe_load(raw)
                if isinstance(data, list):
                    commands = [str(c) for c in data]
                else:
                    click.echo("Error: YAML file must contain a list of commands", err=True)
                    sys.exit(1)
            except ImportError:
                click.echo("Error: PyYAML not installed. Run: pip install pyyaml", err=True)
                sys.exit(1)
        
        elif resolved_fmt == 'csv':
            reader = csv.reader(io.StringIO(raw))
            first = True
            for row in reader:
                if first:
                    first = False
                    # Skip header if first cell looks like a label
                    if row and row[0].lower() in ('command', 'cmd', 'commands'):
                        continue
                if row:
                    commands.append(row[0].strip())
        
        commands = [c for c in commands if c]
        if not commands:
            click.echo("No commands found in file.", err=True)
            sys.exit(1)
        
        click.echo(f"[*] Executing {len(commands)} command(s) from {input_file}")
        click.echo("")
        
        results = []
        success_count = 0
        fail_count = 0
        
        for i, cmd in enumerate(commands, 1):
            click.echo(f"[{i}/{len(commands)}] {cmd}")
            try:
                ok = controller.execute_command(cmd)
                status = "OK" if ok else "FAIL"
                results.append({'index': i, 'command': cmd, 'success': ok, 'error': None})
                if ok:
                    success_count += 1
                    click.echo(f"         -> {status}")
                else:
                    fail_count += 1
                    click.echo(f"         -> {status}", err=True)
                    if stop_on_error:
                        click.echo("[!] Stopping on first error (--stop-on-error)", err=True)
                        break
            except RateLimitError as e:
                wait = e.get_wait_time()
                click.echo(f"         -> RATE LIMITED (waiting {wait}s)", err=True)
                time.sleep(wait)
                results.append({'index': i, 'command': cmd, 'success': False, 'error': 'rate_limited'})
                fail_count += 1
            except Exception as e:
                click.echo(f"         -> ERROR: {e}", err=True)
                results.append({'index': i, 'command': cmd, 'success': False, 'error': str(e)})
                fail_count += 1
                if stop_on_error:
                    break
            
            if delay > 0 and i < len(commands):
                time.sleep(delay)
        
        click.echo("")
        click.echo(f"[*] Batch complete: {success_count} succeeded, {fail_count} failed")
        
        if output:
            summary = {
                'total': len(results),
                'success': success_count,
                'failed': fail_count,
                'results': results,
            }
            Path(output).write_text(json.dumps(summary, indent=2))
            click.echo(f"[*] Results written to {output}")
        
        sys.exit(0 if fail_count == 0 else 1)
    
    except Exception as e:
        logger.error(f"Batch execution failed: {e}")
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    finally:
        ctx.cleanup()

# ============================================================================
# Status Command Group
# ============================================================================

@cli.group(invoke_without_command=True)
@click.option('--host', help='Server host')
@click.option('--port', type=int, help='Server port')
@click.option('--token', envvar='CONTROL_CENTER_TOKEN', help='API token')
@click.option('--format', 'fmt', default='text', type=click.Choice(['text', 'json']),
              help='Output format')
@click.pass_context
def status(click_ctx, host, port, token, fmt):
    """Show connection and server status (subcommands) or live summary (bare)
    
    Run bare with no subcommand for a combined live overview.
    
    Examples:
        control-center status                    # Combined overview
        control-center status connection         # Agent/connection details
        control-center status server             # Server identity + uptime
        control-center status metrics            # Command performance stats
        control-center status system             # Controller host resources
        control-center status session            # Current/last session info
    """
    # BUG-012 FIX: Previously there were TWO registrations under the name
    # "status" in Click's command registry: first @cli.group() (which attached
    # all the subcommands), then @cli.command(name='status') (the bare overview).
    # Click's registry is a dict so the second registration silently overwrote
    # the first, discarding the entire subcommand tree. The fix is to use a
    # single group decorated with invoke_without_command=True and run the
    # overview logic here when no subcommand is provided.
    if click_ctx.invoked_subcommand is not None:
        # A subcommand was given — let Click dispatch to it; do nothing here.
        return

    # ── Bare invocation: run the combined live status overview ────────────────
    h, p = _resolve_host_port(host, port)

    # Try to get live connection data (no auth needed)
    client = _get_no_auth_client(h, p)
    conn_data = None
    server_data = None
    try:
        srv = client.query_server_status()
        if srv and srv.get('servers'):
            server_data = srv['servers'][0]
            conn_data = server_data.get('current_connection')
    except Exception:
        pass
    finally:
        if client.channel:
            client.channel.close()

    # Always show local session/metrics if available from ctx
    if fmt == 'json':
        out: Dict = {
            'server': server_data['identity'] if server_data else None,
            'connection': conn_data,
            'session': ctx.session.to_dict() if ctx.session else _load_session_data(),
            'metrics': ctx.metrics.get_stats() if ctx.metrics else None,
            'system': StatusReporter.get_system_status(),
        }
        click.echo(json.dumps(out, indent=2, default=str))
        return

    click.echo("\n=== Control Center Status ===\n")

    if server_data:
        ident = server_data['identity']
        st = server_data['status']
        click.echo(f"Server:   {ident.get('hostname')}  ({ident.get('server_id', '')[:8]}...)")
        click.echo(f"Network:  {ident.get('network', 'default')}")
        click.echo(f"Version:  {ident.get('version', 'N/A')}")
        click.echo(f"Uptime:   {st.get('uptime_seconds', 0)}s")
        click.echo(f"Commands: {st.get('total_commands_processed', 0)} processed")
        click.echo("")

    if conn_data:
        click.echo(f"Agent:    {conn_data.get('agent_hostname', 'N/A')} ({conn_data.get('agent_ip', '')})")
        click.echo(f"OS:       {conn_data.get('os_type', 'N/A')}")
        click.echo(f"ConnID:   {conn_data.get('connection_id', 'N/A')[:8]}...")
        click.echo(f"Since:    {_fmt_ts(conn_data.get('connected_at'))}")
        click.echo(f"Commands: {conn_data.get('commands_executed', 0)} executed")
        click.echo("")
    else:
        click.echo("Agent:    No agent connected")
        click.echo("")

    if ctx.metrics:
        stats = ctx.metrics.get_stats()
        click.echo(f"Session commands: {stats.get('total_commands', 0)}  "
                   f"success rate: {stats.get('success_rate', 0):.1f}%  "
                   f"avg: {stats.get('avg_execution_time_ms', 0):.1f}ms")
        click.echo("")

    sys_info = StatusReporter.get_system_status()
    click.echo(f"CPU: {sys_info['cpu_percent']}%  Memory: {sys_info['memory_percent']}%  Disk: {sys_info['disk_percent']}%")
    click.echo("")


@status.command(name='connection')
@click.option('--host', help='Server host')
@click.option('--port', type=int, help='Server port')
@click.option('--format', 'fmt', default='text', type=click.Choice(['text', 'json']))
def status_connection(host, port, fmt):
    """Show live connection and agent details"""
    h, p = _resolve_host_port(host, port)
    client = _get_no_auth_client(h, p)
    try:
        data = client.query_server_status()
    finally:
        if client.channel:
            client.channel.close()
    
    if not data or not data.get('servers'):
        click.echo("No server data available.", err=True)
        sys.exit(1)
    
    srv = data['servers'][0]
    conn = srv.get('current_connection')
    
    if fmt == 'json':
        click.echo(json.dumps({'connected': srv['status']['agent_connected'],
                               'connection': conn}, indent=2, default=str))
        return
    
    StatusReporter.print_connection_detail(conn or {})


@status.command(name='server')
@click.option('--host', help='Server host')
@click.option('--port', type=int, help='Server port')
@click.option('--format', 'fmt', default='text', type=click.Choice(['text', 'json']))
def status_server(host, port, fmt):
    """Show server identity and uptime"""
    h, p = _resolve_host_port(host, port)
    client = _get_no_auth_client(h, p)
    try:
        ident = client.get_server_identity()
        srv_data = client.query_server_status()
    finally:
        if client.channel:
            client.channel.close()
    
    if fmt == 'json':
        click.echo(json.dumps({
            'identity': ident,
            'status': srv_data['servers'][0]['status'] if srv_data and srv_data.get('servers') else None,
        }, indent=2, default=str))
        return
    
    if ident:
        click.echo("\n=== Server Identity ===")
        click.echo(f"  ID:            {ident.get('server_id', 'N/A')}")
        click.echo(f"  Hostname:      {ident.get('hostname', 'N/A')}")
        click.echo(f"  Listen:        {ident.get('listen_address', 'N/A')}")
        click.echo(f"  Network:       {ident.get('network', 'default')}")
        click.echo(f"  Version:       {ident.get('version', 'N/A')}")
        click.echo(f"  Started:       {_fmt_ts(ident.get('started_at'))}")
    if srv_data and srv_data.get('servers'):
        st = srv_data['servers'][0]['status']
        click.echo(f"  Uptime:        {st.get('uptime_seconds', 0)}s")
        click.echo(f"  Agent online:  {st.get('agent_connected', False)}")
        click.echo(f"  Cmds processed:{st.get('total_commands_processed', 0)}")
    click.echo("")


@status.command(name='metrics')
@click.option('--host', help='Server host')
@click.option('--port', type=int, help='Server port')
@click.option('--token', envvar='CONTROL_CENTER_TOKEN', help='API token (metrics scope)')
@click.option('--format', 'fmt', default='text', type=click.Choice(['text', 'json']))
def status_metrics(host, port, token, fmt):
    """Show command performance metrics for the current/last session"""
    # Prefer live in-memory metrics, fall back to saved session
    if ctx.metrics:
        stats = ctx.metrics.get_stats()
    else:
        saved = _load_session_data()
        stats = saved['metrics'] if saved else None
    
    if not stats:
        click.echo("No metrics available. Run 'connect' first or check last session.", err=True)
        sys.exit(1)
    
    if fmt == 'json':
        click.echo(json.dumps(stats, indent=2, default=str))
        return
    
    StatusReporter.print_metrics_detail(stats)


@status.command(name='system')
@click.option('--format', 'fmt', default='text', type=click.Choice(['text', 'json']))
def status_system(fmt):
    """Show controller host system resources (CPU, memory, disk, network)"""
    info = StatusReporter.get_system_status()
    if fmt == 'json':
        click.echo(json.dumps(info, indent=2, default=str))
        return
    StatusReporter.print_system_detail(info)


@status.command(name='session')
@click.option('--format', 'fmt', default='text', type=click.Choice(['text', 'json']))
def status_session(fmt):
    """Show current or last session summary"""
    if ctx.session:
        data = ctx.session.to_dict()
    else:
        saved = _load_session_data()
        data = saved['session'] if saved else None
    
    if not data:
        click.echo("No session data available.", err=True)
        sys.exit(1)
    
    if fmt == 'json':
        click.echo(json.dumps(data, indent=2, default=str))
        return
    
    click.echo("\n=== Session ===")
    click.echo(f"  Host:     {data.get('host', 'N/A')}:{data.get('port', '')}")
    click.echo(f"  OS:       {data.get('os_type', 'N/A')} {data.get('os_version', '')}")
    click.echo(f"  Duration: {data.get('duration_seconds', 0):.1f}s")
    click.echo(f"  Idle:     {data.get('idle_seconds', 0):.1f}s")
    click.echo(f"  State:    {data.get('state', 'unknown')}")
    click.echo("")

# ============================================================================
# Session History Group
# ============================================================================

@cli.group()
def session():
    """Inspect current or last session history
    
    Examples:
        control-center session events          # Timeline of session lifecycle events
        control-center session commands        # All commands executed this session
        control-center session stats           # Aggregate performance statistics
        control-center session replay          # Re-run commands from last session
    """
    pass


@session.command(name='events')
@click.option('--format', 'fmt', default='text', type=click.Choice(['text', 'json']))
def session_events(fmt):
    """Show session lifecycle events (connect, reconnect, disconnect, VM shutdown)"""
    data = _load_session_data()
    if not data:
        click.echo("No session data found. Connect first.", err=True)
        sys.exit(1)

    events = data.get('session', {}).get('events', [])
    if fmt == 'json':
        click.echo(json.dumps(events, indent=2, default=str))
        return

    click.echo(f"\n=== Session Events ({len(events)}) ===\n")
    if not events:
        click.echo("  (no events recorded)")
    for ev in events:
        ts = _fmt_ts(ev.get('timestamp'))
        click.echo(f"  {ts}  [{ev.get('type', 'event').upper():<20}]  {ev.get('detail', '')}")
    click.echo("")


@session.command(name='commands')
@click.option('--failed', is_flag=True, help='Show only failed commands')
@click.option('--limit', default=50, show_default=True, help='Maximum rows to show')
@click.option('--format', 'fmt', default='text', type=click.Choice(['text', 'json', 'csv']))
def session_commands(failed, limit, fmt):
    """Show commands executed during the current or last session"""
    # Prefer live history from MetricsCollector
    if ctx.metrics and hasattr(ctx.metrics, 'command_history'):
        history = [dict(c) for c in ctx.metrics.command_history]
    else:
        saved = _load_session_data()
        history = saved.get('commands', []) if saved else []

    if failed:
        history = [c for c in history if not c.get('success', True)]

    history = history[-limit:]

    if fmt == 'json':
        click.echo(json.dumps(history, indent=2, default=str))
        return

    if fmt == 'csv':
        writer = csv.DictWriter(sys.stdout,
                                fieldnames=['index', 'command', 'success',
                                            'execution_time_ms', 'timestamp', 'error'],
                                extrasaction='ignore')
        writer.writeheader()
        writer.writerows(history)
        return

    click.echo(f"\n=== Session Commands (showing {len(history)}) ===\n")
    click.echo(f"  {'#':<5} {'OK':<4} {'ms':>6}  {'Command'}")
    click.echo("  " + "-"*70)
    for i, cmd in enumerate(history, 1):
        ok = '+' if cmd.get('success', True) else 'x'
        ms = cmd.get('execution_time_ms', '-')
        click.echo(f"  {i:<5} {ok:<4} {str(ms):>6}  {cmd.get('command', '')}")
    click.echo("")


@session.command(name='stats')
@click.option('--format', 'fmt', default='text', type=click.Choice(['text', 'json']))
def session_stats(fmt):
    """Aggregate performance statistics for current or last session"""
    if ctx.metrics:
        stats = ctx.metrics.get_stats()
    else:
        saved = _load_session_data()
        stats = saved.get('metrics') if saved else None

    if not stats:
        click.echo("No stats available.", err=True)
        sys.exit(1)

    if fmt == 'json':
        click.echo(json.dumps(stats, indent=2, default=str))
        return

    click.echo("\n=== Session Statistics ===\n")
    click.echo(f"  Total commands:   {stats.get('total_commands', 0)}")
    click.echo(f"  Successful:       {stats.get('successful_commands', 0)}")
    click.echo(f"  Failed:           {stats.get('failed_commands', 0)}")
    click.echo(f"  Success rate:     {stats.get('success_rate', 0):.1f}%")
    click.echo(f"  Avg time (ms):    {stats.get('avg_execution_time_ms', 0):.1f}")
    click.echo(f"  Min time (ms):    {stats.get('min_execution_time_ms', 0):.1f}")
    click.echo(f"  Max time (ms):    {stats.get('max_execution_time_ms', 0):.1f}")
    click.echo(f"  p95 time (ms):    {stats.get('p95_execution_time_ms', 0):.1f}")
    click.echo(f"  Session duration: {stats.get('session_duration_seconds', 0):.1f}s")
    click.echo("")


@session.command(name='replay')
@click.option('--host', help='Server host')
@click.option('--port', type=int, help='Server port')
@click.option('--token', envvar='CONTROL_CENTER_TOKEN', help='API token')
@click.option('--failed-only', is_flag=True, help='Only replay failed commands')
@click.option('--delay', default=0.5, show_default=True, help='Delay between commands (s)')
@click.option('--dry-run', is_flag=True, help='Print commands without executing')
def session_replay(host, port, token, failed_only, delay, dry_run):
    """Re-execute commands from the last saved session"""
    saved = _load_session_data()
    if not saved:
        click.echo("No saved session found.", err=True)
        sys.exit(1)

    history = saved.get('commands', [])
    if failed_only:
        history = [c for c in history if not c.get('success', True)]

    if not history:
        click.echo("No commands to replay.", err=True)
        sys.exit(1)

    click.echo(f"[*] Replaying {len(history)} command(s) from last session")
    if dry_run:
        click.echo("[*] DRY RUN — commands will not be executed\n")
        for i, cmd in enumerate(history, 1):
            click.echo(f"  [{i}] {cmd.get('command', '')}")
        return

    try:
        api_token = _resolve_token(token)
        if not api_token:
            click.echo("Error: No API token.", err=True)
            sys.exit(1)
        h, p = _resolve_host_port(host, port)
        ctx.client = _get_auth_client(h, p, api_token)
        agent_info = ctx.client.get_agent_info()
        if not agent_info:
            sys.exit(1)
        os_type = agent_info['os_type']
        if os_type == 'WINDOWS':
            controller = WindowsActuation(ctx.client)
        elif os_type == 'MACOS':
            controller = MacOSActuation(ctx.client)
        else:
            controller = LinuxActuation(ctx.client)

        for i, cmd in enumerate(history, 1):
            command_str = cmd.get('command', '')
            click.echo(f"[{i}/{len(history)}] {command_str}")
            try:
                ok = controller.execute_command(command_str)
                click.echo(f"       -> {'OK' if ok else 'FAIL'}")
            except Exception as e:
                click.echo(f"       -> ERROR: {e}", err=True)
            if delay > 0 and i < len(history):
                time.sleep(delay)
    finally:
        ctx.cleanup()

# ============================================================================
# Agent Command Group  (info / capabilities / ping / disconnect / history)
# NOTE: agent start (Rust binary launcher) is defined further below.
# ============================================================================

@cli.group()
def agent():
    """Query and manage agents connected to the server
    
    Subcommands for live agents require no auth (QueryConnections).
    disconnect requires a token.
    
    Examples:
        control-center agent info                     # Details of connected agent
        control-center agent capabilities             # Supported command types
        control-center agent ping                     # Round-trip latency
        control-center agent disconnect --reason done # Graceful disconnect
        control-center agent history --limit 20       # Past connection records
        control-center agent start                    # Launch Rust agent binary
    """
    pass


@agent.command(name='info')
@click.option('--host', help='Server host')
@click.option('--port', type=int, help='Server port')
@click.option('--format', 'fmt', default='text', type=click.Choice(['text', 'json']))
def agent_info(host, port, fmt):
    """Show info about the currently-connected agent"""
    h, p = _resolve_host_port(host, port)
    client = _get_no_auth_client(h, p)
    try:
        data = client.query_connections()
    finally:
        if client.channel:
            client.channel.close()

    if not data or data.get('total_count', 0) == 0:
        click.echo("No agent currently connected.", err=True)
        sys.exit(1)

    conn = data['connections'][0]
    info = AgentInfo.from_connection_dict(conn)

    if fmt == 'json':
        click.echo(json.dumps(info.to_dict(), indent=2, default=str))
        return

    click.echo("\n=== Agent Information ===\n")
    click.echo(f"  Agent ID:        {info.agent_id}")
    click.echo(f"  Hostname:        {info.agent_hostname or 'N/A'}")
    click.echo(f"  IP Address:      {info.agent_ip or 'N/A'}")
    click.echo(f"  OS:              {info.os_type} {info.os_version}")
    click.echo(f"  Connection ID:   {info.connection_id or 'N/A'}")
    click.echo(f"  Connected At:    {_fmt_ts(info.connected_at)}")
    click.echo(f"  Commands Run:    {info.commands_executed}")
    click.echo("")


@agent.command(name='capabilities')
@click.option('--host', help='Server host')
@click.option('--port', type=int, help='Server port')
@click.option('--format', 'fmt', default='text', type=click.Choice(['text', 'json']))
def agent_capabilities(host, port, fmt):
    """List command types supported by the connected agent"""
    h, p = _resolve_host_port(host, port)
    client = _get_no_auth_client(h, p)
    try:
        data = client.query_connections()
    finally:
        if client.channel:
            client.channel.close()

    if not data or data.get('total_count', 0) == 0:
        click.echo("No agent connected.", err=True)
        sys.exit(1)

    conn = data['connections'][0]
    caps = list(conn.get('capabilities', []))
    os_type = conn.get('os_type', 'N/A')

    if fmt == 'json':
        click.echo(json.dumps({'os_type': os_type, 'capabilities': caps}, indent=2))
        return

    click.echo(f"\n=== Agent Capabilities (OS: {os_type}) ===\n")
    if caps:
        for cap in caps:
            click.echo(f"  + {cap}")
    else:
        # Fall back to OS-implied capabilities
        defaults = {
            'WINDOWS': ['mouse_click', 'mouse_move', 'keyboard_type', 'keyboard_shortcut',
                        'screenshot', 'run_program'],
            'MACOS':   ['mouse_click', 'mouse_move', 'keyboard_type', 'keyboard_shortcut',
                        'screenshot', 'run_program'],
            'LINUX':   ['mouse_click', 'mouse_move', 'keyboard_type', 'keyboard_shortcut',
                        'screenshot', 'run_program'],
        }
        for cap in defaults.get(os_type, ['command_execution']):
            click.echo(f"  + {cap}")
        click.echo("\n  (capabilities list inferred from OS type)")
    click.echo("")


@agent.command(name='ping')
@click.option('--host', help='Server host')
@click.option('--port', type=int, help='Server port')
@click.option('--count', default=3, show_default=True, help='Number of pings')
@click.option('--format', 'fmt', default='text', type=click.Choice(['text', 'json']))
def agent_ping(host, port, count, fmt):
    """Measure round-trip latency to the server (no auth required)"""
    h, p = _resolve_host_port(host, port)

    results = []
    for i in range(count):
        t0 = time.monotonic()
        client = _get_no_auth_client(h, p)
        try:
            ok = client.ping()
            rtt = (time.monotonic() - t0) * 1000
            results.append({'seq': i + 1, 'rtt_ms': round(rtt, 2), 'success': ok})
        except Exception as e:
            results.append({'seq': i + 1, 'rtt_ms': None, 'success': False, 'error': str(e)})
        finally:
            if client.channel:
                client.channel.close()
        if i < count - 1:
            time.sleep(0.2)

    if fmt == 'json':
        click.echo(json.dumps(results, indent=2))
        return

    click.echo(f"\nPING {h}:{p}")
    ok_results = [r['rtt_ms'] for r in results if r['success'] and r['rtt_ms'] is not None]
    for r in results:
        if r['success']:
            click.echo(f"  seq={r['seq']}  rtt={r['rtt_ms']:.2f}ms")
        else:
            click.echo(f"  seq={r['seq']}  FAILED  {r.get('error', '')}")
    if ok_results:
        click.echo(f"\n  min={min(ok_results):.2f}ms  "
                   f"avg={sum(ok_results)/len(ok_results):.2f}ms  "
                   f"max={max(ok_results):.2f}ms")
    click.echo("")


@agent.command(name='disconnect')
@click.option('--host', help='Server host')
@click.option('--port', type=int, help='Server port')
@click.option('--token', envvar='CONTROL_CENTER_TOKEN', required=True, help='Auth token')
@click.option('--reason', default='operator_request',
              help='Disconnect reason (default: operator_request)')
@click.option('--yes', '-y', is_flag=True, help='Skip confirmation')
def agent_disconnect(host, port, token, reason, yes):
    """Send a graceful disconnect signal to the connected agent (requires auth)"""
    if not yes:
        if not click.confirm(f"Send disconnect signal to agent with reason '{reason}'?"):
            click.echo("Cancelled.")
            return

    h, p = _resolve_host_port(host, port)
    api_token = _resolve_token(token)
    if not api_token:
        click.echo("Error: Token required for disconnect.", err=True)
        sys.exit(1)

    client = _get_no_auth_client(h, p)
    client.set_token(api_token)
    try:
        ok = client.disconnect_agent(reason=reason)
        if ok:
            click.echo(f"[+] Disconnect signal sent (reason: {reason})")
            audit = get_audit_logger()
            audit.log_agent_disconnect(
                session_id=str(uuid.uuid4()),
                user_id='cli-operator',
                reason=reason,
            )
        else:
            click.echo("[x] Server returned failure for disconnect request.", err=True)
            sys.exit(1)
    except AuthenticationError:
        click.echo("[x] Authentication failed — check your token.", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"[x] Error: {e}", err=True)
        sys.exit(1)
    finally:
        if client.channel:
            client.channel.close()


@agent.command(name='history')
@click.option('--host', help='Server host')
@click.option('--port', type=int, help='Server port')
@click.option('--limit', default=10, show_default=True, help='Max records to show')
@click.option('--format', 'fmt', default='text', type=click.Choice(['text', 'json', 'csv']))
def agent_history(host, port, limit, fmt):
    """Show historical connection records from the server registry (no auth)"""
    h, p = _resolve_host_port(host, port)
    client = _get_no_auth_client(h, p)
    try:
        data = client.get_connection_history(limit=limit)
    finally:
        if client.channel:
            client.channel.close()

    # BUG-013 FIX: get_connection_history() returns a plain List[Dict], not a
    # wrapper dict {"records": [...]}. Calling .get('records', []) on a list
    # raised: AttributeError: 'list' object has no attribute 'get'.
    # The fix is a single line: use the list directly.
    records = data if data is not None else []

    if fmt == 'json':
        click.echo(json.dumps(records, indent=2, default=str))
        return

    if fmt == 'csv':
        if records:
            writer = csv.DictWriter(sys.stdout, fieldnames=records[0].keys(),
                                    extrasaction='ignore')
            writer.writeheader()
            writer.writerows(records)
        return

    click.echo(f"\n=== Connection History ({len(records)} records) ===\n")
    for i, rec in enumerate(records, 1):
        info = AgentInfo.from_history_dict(rec)
        click.echo(f"  [{i}] {info.agent_hostname or info.agent_id}  "
                   f"({info.os_type})  "
                   f"connected: {_fmt_ts(info.connected_at)}  "
                   f"cmds: {info.commands_executed}  "
                   f"reason: {info.disconnect_reason or 'N/A'}")
    if not records:
        click.echo("  (no history records)")
    click.echo("")


@agent.command(name='start')
@click.option('--server-host', default='127.0.0.1', help='Server host to connect to')
@click.option('--server-port', default=50051, help='Server gRPC port')
@click.option('--token', envvar='CONTROL_CENTER_TOKEN', help='Authentication token')
def agent_start(server_host, server_port, token):
    """Start the Rust agent on this machine"""
    click.echo(f"[START] Starting Control Center Agent (Rust)")
    click.echo(f"   Connecting to: {server_host}:{server_port}")

    env = os.environ.copy()
    env['AGENT_SERVER_HOST'] = server_host
    env['AGENT_SERVER_PORT'] = str(server_port)
    if token:
        env['CONTROL_CENTER_TOKEN'] = token

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

# ============================================================================
# Export Command Group
# ============================================================================

@cli.group()
def export():
    """Export data in various formats
    
    Examples:
        control-center export commands --format json
        control-center export commands --success-only --last 100
        control-center export metrics
        control-center export session
        control-center export audit --since 2025-01-01
        control-center export diagnostics
        control-center export report
    """
    pass


def _require_exporter() -> 'Exporter':
    """Return an Exporter instance wired to the current session's metrics."""
    exporter = Exporter()
    if ctx.metrics:
        exporter.metrics = ctx.metrics
    elif _load_session_data():
        pass  # Exporter reads log files; session data used separately
    return exporter


@export.command(name='commands')
@click.option('--format', 'fmt', default='csv',
              type=click.Choice(['csv', 'json', 'ndjson']),
              help='Output format (default: csv)')
@click.option('--type-filter', default=None,
              help='Only include commands matching this type/prefix')
@click.option('--success-only', is_flag=True, help='Only successful commands')
@click.option('--failed-only', is_flag=True, help='Only failed commands')
@click.option('--last', 'last_n', default=None, type=int,
              help='Only export last N commands')
@click.option('--output', '-o', default=None,
              help='Output file (default: auto-named in ./exports/)')
def export_commands(fmt, type_filter, success_only, failed_only, last_n, output):
    """Export command execution log"""
    exporter = _require_exporter()
    try:
        commands = (
            list(getattr(exporter.metrics, 'command_history', []))
            if exporter.metrics else []
        )
        path = exporter.export_command_log(
            commands=commands,
            fmt=fmt,
            command_type_filter=type_filter,
            success_only=success_only,
            failed_only=failed_only,
            last_n=last_n,
            filename=output,
        )
        click.echo(f"[+] Exported commands -> {path}")
    except Exception as e:
        click.echo(f"[x] Export failed: {e}", err=True)
        sys.exit(1)


@export.command(name='metrics')
@click.option('--format', 'fmt', default='json',
              type=click.Choice(['json', 'csv']),
              help='Output format (default: json)')
@click.option('--output', '-o', default=None, help='Output file path')
def export_metrics(fmt, output):
    """Export session performance metrics"""
    stats = ctx.metrics.get_stats() if ctx.metrics else None
    if not stats:
        saved = _load_session_data()
        stats = saved.get('metrics') if saved else None
    if not stats:
        click.echo("No metrics available. Run 'connect' first.", err=True)
        sys.exit(1)

    out_path = Path(output) if output else Path(f"exports/metrics_{int(time.time())}.{fmt}")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == 'json':
        out_path.write_text(json.dumps(stats, indent=2, default=str))
    else:
        with open(out_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['metric', 'value'])
            for k, v in stats.items():
                writer.writerow([k, v])

    click.echo(f"[+] Exported metrics -> {out_path}")


@export.command(name='session')
@click.option('--format', 'fmt', default='json',
              type=click.Choice(['json', 'csv']),
              help='Output format (default: json)')
@click.option('--output', '-o', default=None, help='Output file path')
def export_session(fmt, output):
    """Export last session data (commands + metrics + events)"""
    data = _load_session_data()
    if not data:
        click.echo("No session data found. Connect first.", err=True)
        sys.exit(1)

    out_path = Path(output) if output else Path(f"exports/session_{int(time.time())}.{fmt}")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == 'json':
        out_path.write_text(json.dumps(data, indent=2, default=str))
    else:
        # Flatten: write command history as CSV
        commands = data.get('commands', [])
        with open(out_path, 'w', newline='') as f:
            fields = ['index', 'command', 'success', 'execution_time_ms', 'error']
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(commands)

    click.echo(f"[+] Exported session -> {out_path}")


@export.command(name='audit')
@click.option('--log-dir', default='./logs/audit', show_default=True,
              help='Audit log directory')
@click.option('--format', 'fmt', default='json',
              type=click.Choice(['json', 'csv', 'ndjson']),
              help='Output format (default: json)')
@click.option('--since', default=None, help='Start date filter (YYYY-MM-DD)')
@click.option('--event-type', default=None,
              help='Filter by event type (auth_attempt, session_start, ...)')
@click.option('--level', default=None,
              type=click.Choice(['INFO', 'WARNING', 'ERROR']),
              help='Filter by log level')
@click.option('--last', 'last_n', default=None, type=int,
              help='Only export last N entries')
@click.option('--output', '-o', default=None, help='Output file path')
def export_audit(log_dir, fmt, since, event_type, level, last_n, output):
    """Export structured audit logs"""
    exporter = _require_exporter()
    try:
        path = exporter.export_audit_logs(
            log_dir=log_dir,
            fmt=fmt,
            since=since,
            event_type_filter=event_type,
            level_filter=level,
            last_n=last_n,
            filename=output,
        )
        click.echo(f"[+] Exported audit logs -> {path}")
    except Exception as e:
        click.echo(f"[x] Export failed: {e}", err=True)
        sys.exit(1)


@export.command(name='diagnostics')
@click.option('--output', '-o', default=None, help='Output directory path')
@click.option('--no-system', 'include_system', is_flag=True, default=True,
              help='Exclude system info from diagnostics')
@click.option('--no-html', 'include_html', is_flag=True, default=True,
              help='Skip HTML report generation')
def export_diagnostics(output, include_system, include_html):
    """Export full diagnostics bundle (logs + system + config snapshot)"""
    exporter = _require_exporter()
    try:
        _session_data = _load_session_data()
        path = exporter.export_full_diagnostics(
            session_info=ctx.session.to_dict() if ctx.session else (_session_data.get('session') if _session_data else {}) or {},
            metrics=ctx.metrics.get_stats() if ctx.metrics else (_session_data.get('metrics') if _session_data else {}) or {},
            include_system=include_system,
            include_html=include_html,
        )
        click.echo(f"[+] Diagnostics bundle -> {path}")
    except Exception as e:
        click.echo(f"[x] Export failed: {e}", err=True)
        sys.exit(1)


@export.command(name='report')
@click.option('--output', '-o', default=None, help='Output file path')
@click.option('--command-format', 'command_fmt', default='csv',
              type=click.Choice(['csv', 'json', 'ndjson']),
              help='Format for embedded command log (default: csv)')
def export_report(output, command_fmt):
    """Export a full human-readable session report"""
    exporter = _require_exporter()
    try:
        _session_data = _load_session_data()
        path = exporter.export_full_report(
            session_info=ctx.session.to_dict() if ctx.session else (_session_data.get('session') if _session_data else {}) or {},
            metrics=ctx.metrics.get_stats() if ctx.metrics else (_session_data.get('metrics') if _session_data else {}) or {},
            command_fmt=command_fmt,
        )
        click.echo(f"[+] Report -> {path}")
    except Exception as e:
        click.echo(f"[x] Export failed: {e}", err=True)
        sys.exit(1)

# ============================================================================
# Audit Command Group
# ============================================================================

@cli.group()
def audit():
    """Query and tail structured audit logs
    
    Examples:
        control-center audit show                 # Print recent audit events
        control-center audit tail                 # Follow log in real-time
        control-center audit search --event auth_attempt --level WARNING
    """
    pass


def _parse_audit_line(line: str) -> Optional[Dict]:
    """Try to parse a JSON audit log line; return None on failure."""
    try:
        return json.loads(line.strip())
    except Exception:
        return None


def _load_audit_entries(log_dir: str, since: Optional[str] = None,
                        event_type: Optional[str] = None,
                        level: Optional[str] = None,
                        last_n: Optional[int] = None) -> List[Dict]:
    """Read + filter all entries from the audit log directory."""
    log_path = Path(log_dir)
    entries: List[Dict] = []

    if not log_path.exists():
        return entries

    # Read all audit.log* files in date order
    log_files = sorted(log_path.glob('audit.log*'))
    for lf in log_files:
        try:
            for line in lf.read_text(encoding='utf-8', errors='ignore').splitlines():
                entry = _parse_audit_line(line)
                if entry:
                    entries.append(entry)
        except Exception:
            pass

    # Filters
    if since:
        entries = [e for e in entries
                   if e.get('timestamp', '') >= since]
    if event_type:
        entries = [e for e in entries
                   if e.get('event') == event_type]
    if level:
        entries = [e for e in entries
                   if e.get('level', '').upper() == level.upper()]
    if last_n:
        entries = entries[-last_n:]

    return entries


@audit.command(name='show')
@click.option('--log-dir', default='./logs/audit', show_default=True)
@click.option('--since', default=None, help='Start timestamp (YYYY-MM-DD or ISO)')
@click.option('--event', 'event_type', default=None,
              help='Filter by event type (auth_attempt, session_start, ...)')
@click.option('--level', default=None, type=click.Choice(['INFO', 'WARNING', 'ERROR']))
@click.option('--last', 'last_n', default=50, show_default=True,
              help='Number of recent entries to show')
@click.option('--format', 'fmt', default='text', type=click.Choice(['text', 'json']))
def audit_show(log_dir, since, event_type, level, last_n, fmt):
    """Print recent audit log entries"""
    entries = _load_audit_entries(log_dir, since=since, event_type=event_type,
                                  level=level, last_n=last_n)

    if fmt == 'json':
        click.echo(json.dumps(entries, indent=2))
        return

    if not entries:
        click.echo("No audit entries found.")
        return

    click.echo(f"\n=== Audit Log ({len(entries)} entries) ===\n")
    for e in entries:
        lvl = e.get('level', 'INFO')
        ts = e.get('timestamp', 'N/A')[:19]
        event = e.get('event', e.get('message', ''))
        uid = e.get('user_id', '')
        extra = f"  user={uid}" if uid else ""
        click.echo(f"  {ts}  [{lvl:<7}]  {event}{extra}")
    click.echo("")


@audit.command(name='tail')
@click.option('--log-dir', default='./logs/audit', show_default=True)
@click.option('--lines', default=20, show_default=True,
              help='Initial number of lines to show before following')
def audit_tail(log_dir, lines):
    """Follow audit log in real-time (like tail -f)"""
    log_file = Path(log_dir) / 'audit.log'
    if not log_file.exists():
        click.echo(f"Audit log not found: {log_file}", err=True)
        sys.exit(1)

    click.echo(f"==> {log_file} <==")
    try:
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            # Seek to near end, show last N lines
            all_lines = f.readlines()
            for line in all_lines[-lines:]:
                entry = _parse_audit_line(line)
                if entry:
                    click.echo(f"  {entry.get('timestamp','')[:19]}  "
                               f"[{entry.get('level','INFO'):<7}]  "
                               f"{entry.get('event', entry.get('message',''))}")
                else:
                    click.echo(f"  {line.rstrip()}")

            click.echo("\n[Following log — Ctrl+C to stop]\n")
            while True:
                line = f.readline()
                if line:
                    entry = _parse_audit_line(line)
                    if entry:
                        click.echo(f"  {entry.get('timestamp','')[:19]}  "
                                   f"[{entry.get('level','INFO'):<7}]  "
                                   f"{entry.get('event', entry.get('message',''))}")
                    else:
                        click.echo(f"  {line.rstrip()}")
                else:
                    time.sleep(0.3)
    except KeyboardInterrupt:
        click.echo("\n[*] Stopped.")


@audit.command(name='search')
@click.option('--log-dir', default='./logs/audit', show_default=True)
@click.option('--event', 'event_type', default=None, help='Event type to match exactly')
@click.option('--user', default=None, help='Filter by user_id')
@click.option('--level', default=None, type=click.Choice(['INFO', 'WARNING', 'ERROR']))
@click.option('--since', default=None, help='Start date (YYYY-MM-DD)')
@click.option('--keyword', default=None, help='Free-text keyword in the JSON line')
@click.option('--format', 'fmt', default='text', type=click.Choice(['text', 'json']))
def audit_search(log_dir, event_type, user, level, since, keyword, fmt):
    """Search audit logs with filters"""
    entries = _load_audit_entries(log_dir, since=since, event_type=event_type, level=level)

    if user:
        entries = [e for e in entries if e.get('user_id') == user]
    if keyword:
        kw = keyword.lower()
        entries = [e for e in entries if kw in json.dumps(e).lower()]

    if fmt == 'json':
        click.echo(json.dumps(entries, indent=2))
        return

    click.echo(f"\n=== Audit Search Results ({len(entries)} matches) ===\n")
    for e in entries:
        ts = e.get('timestamp', '')[:19]
        lvl = e.get('level', 'INFO')
        ev = e.get('event', e.get('message', ''))
        uid = e.get('user_id', '')
        click.echo(f"  {ts}  [{lvl:<7}]  {ev}"
                   + (f"  [user={uid}]" if uid else ""))
    if not entries:
        click.echo("  (no matches)")
    click.echo("")

# ============================================================================
# Token Command Group
# ============================================================================

@cli.group()
def token():
    """Generate, inspect, and revoke API tokens (PyJWT)
    
    Examples:
        control-center token generate --user alice --scopes execute monitor
        control-center token inspect eyJhbGci...
        control-center token validate eyJhbGci...
    """
    pass


def _jwt_secret() -> str:
    """Resolve the JWT signing secret: CC_JWT_SECRET env > config file > error."""
    secret = os.environ.get('CC_JWT_SECRET')
    if not secret:
        try:
            secret = ctx.config_manager.get('jwt_secret')
        except Exception:
            pass
    if not secret:
        click.echo(
            "Error: No JWT signing secret found.\n"
            "Set CC_JWT_SECRET environment variable or run:\n"
            "  control-center config set jwt_secret YOUR_SECRET",
            err=True
        )
        sys.exit(1)
    return secret


@token.command(name='generate')
@click.option('--user', required=True, help='User identifier (sub claim)')
@click.option('--scopes', multiple=True, default=('execute', 'monitor'),
              help='Permission scopes (repeatable). '
                   'Options: execute monitor metrics admin  '
                   'Example: --scopes execute --scopes monitor')
@click.option('--expires', default=24, type=float, show_default=True,
              help='Token lifetime in hours, fractions allowed (0 = no expiry)')
@click.option('--secret', 'secret_override', default=None, envvar='CC_JWT_SECRET',
              help='JWT signing secret (or set CC_JWT_SECRET)')
@click.option('--algorithm', default='HS256',
              type=click.Choice(['HS256', 'HS384', 'HS512']),
              help='HMAC algorithm (default: HS256)')
@click.option('--audience', default=None, envvar='JWT_AUDIENCE',
              help='JWT audience claim (default: control-center)')
@click.option('--issuer', default=None, envvar='JWT_ISSUER',
              help='JWT issuer claim (default: control-center-auth)')
@click.option('--output', '-o', default=None,
              help='Write token to this file instead of stdout')
def token_generate(user, scopes, expires, secret_override, algorithm, audience, issuer, output):
    """Generate a signed JWT API token

    The generated token is validated by the server on every authenticated RPC.
    Scopes control which RPCs the token may call:
      execute  - ExecuteCommand, execute_batch
      monitor  - GetAgentInfo, QueryConnections, QueryServers
      metrics  - GetMetrics (Prometheus data)
      admin    - DisconnectAgent, all admin operations

    Examples:
        control-center token generate --user ops-bot --scopes execute --scopes monitor
        control-center token generate --user ci --scopes execute --expires 1
    """
    try:
        import jwt
    except ImportError:
        click.echo("Error: PyJWT not installed. Run: pip install PyJWT", err=True)
        sys.exit(1)

    secret = secret_override or _jwt_secret()

    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)

    # BUG-006 FIX: include aud and iss — server requires both claims
    aud = audience or os.environ.get('JWT_AUDIENCE', 'control-center')
    iss = issuer  or os.environ.get('JWT_ISSUER',   'control-center-auth')

    payload = {
        'sub':    user,
        'aud':    aud,
        'iss':    iss,
        'iat':    now,
        'scopes': list(scopes),
        'jti':    str(uuid.uuid4()),
    }
    # BUG-003 FIX: expires is now float so sub-hour values work
    if expires and expires > 0:
        payload['exp'] = now + dt.timedelta(hours=expires)

    try:
        tok = jwt.encode(payload, secret, algorithm=algorithm)
    except Exception as e:
        click.echo(f"Error generating token: {e}", err=True)
        sys.exit(1)

    # jwt.encode returns bytes in PyJWT<2 and str in PyJWT>=2
    if isinstance(tok, bytes):
        tok = tok.decode('utf-8')

    if output:
        Path(output).write_text(tok + '\n')
        click.echo(f"[+] Token written to {output}")
    else:
        click.echo(tok)

    # Print metadata to stderr so stdout stays clean when piping
    exp_str = (f"expires in {expires}h" if expires else "no expiry")
    click.echo(
        f"\n  user={user}  scopes={','.join(scopes)}  {exp_str}  alg={algorithm}"
        f"  aud={aud}  iss={iss}",
        err=True
    )


@token.command(name='inspect')
@click.argument('token_string')
@click.option('--format', 'fmt', default='text', type=click.Choice(['text', 'json']))
def token_inspect(token_string, fmt):
    """Decode and display a JWT token's claims (does NOT verify signature)"""
    try:
        import jwt
    except ImportError:
        click.echo("Error: PyJWT not installed. Run: pip install PyJWT", err=True)
        sys.exit(1)

    try:
        # Decode without verification to inspect any token
        payload = jwt.decode(
            token_string,
            options={"verify_signature": False},
            algorithms=["HS256", "HS384", "HS512"],
        )
    except Exception as e:
        click.echo(f"Error decoding token: {e}", err=True)
        sys.exit(1)

    if fmt == 'json':
        click.echo(json.dumps(payload, indent=2, default=str))
        return

    import datetime as dt
    click.echo("\n=== Token Claims ===\n")
    click.echo(f"  Subject (user):  {payload.get('sub', 'N/A')}")
    click.echo(f"  Scopes:          {', '.join(payload.get('scopes', []))}")
    click.echo(f"  Issued at:       {_fmt_ts(payload.get('iat'))}")
    exp = payload.get('exp')
    if exp:
        exp_dt = dt.datetime.utcfromtimestamp(exp)
        now = dt.datetime.utcnow()
        expired = now > exp_dt
        click.echo(f"  Expires at:      {_fmt_ts(exp)}  {'[EXPIRED]' if expired else '[VALID]'}")
    else:
        click.echo("  Expires at:      never")
    click.echo(f"  Token ID (jti):  {payload.get('jti', 'N/A')}")
    click.echo("")


@token.command(name='validate')
@click.argument('token_string')
@click.option('--secret', 'secret_override', default=None, envvar='CC_JWT_SECRET',
              help='JWT signing secret (or set CC_JWT_SECRET)')
@click.option('--audience', default=None, envvar='JWT_AUDIENCE',
              help='Expected audience claim (default: control-center)')
def token_validate(token_string, secret_override, audience):
    """Verify a JWT token's signature and expiry against your secret"""
    try:
        import jwt
    except ImportError:
        click.echo("Error: PyJWT not installed. Run: pip install PyJWT", err=True)
        sys.exit(1)

    secret = secret_override or _jwt_secret()
    # BUG-006 FIX: PyJWT>=2 raises InvalidAudienceError if aud is present but
    # audience= is not passed to decode().  Match the server default.
    aud = audience or os.environ.get('JWT_AUDIENCE', 'control-center')

    try:
        payload = jwt.decode(
            token_string, secret,
            algorithms=["HS256", "HS384", "HS512"],
            audience=aud,
        )
        click.echo("[+] Token is VALID")
        click.echo(f"    user={payload.get('sub')}  "
                   f"scopes={','.join(payload.get('scopes', []))}")
    except jwt.ExpiredSignatureError:
        click.echo("[x] Token is EXPIRED", err=True)
        sys.exit(1)
    except jwt.InvalidTokenError as e:
        click.echo(f"[x] Token is INVALID: {e}", err=True)
        sys.exit(1)

# ============================================================================
# Update and Uninstall Commands  (verbatim from original)
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
    
    import platform as _platform
    import tempfile
    import stat as _stat
    
    click.echo("Checking for updates...")
    click.echo(f"Current version: v{__version__}")
    
    GITHUB_REPO = "nullvoider07/control-center"  # TODO: Update this!
    API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    
    try:
        try:
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
        
        latest_tag = release_data['tag_name']
        latest_version = latest_tag.lstrip('v')
        
        click.echo(f"Latest version: v{latest_version}")
        
        current_version = __version__
        if latest_version == current_version:
            click.echo(click.style("Check: You already have the latest version!", fg='green'))
            return
        
        click.echo(click.style(f"-> New version available: v{latest_version}", fg='yellow'))
        
        if check_only:
            click.echo("\nTo install the update, run:")
            click.echo("  control-center update")
            return
        
        if not click.confirm('\nDo you want to update now?'):
            click.echo("Update cancelled.")
            return
        
        os_type = _platform.system().lower()
        machine = _platform.machine().lower()
        
        if os_type == 'darwin':
            os_name = 'macos'
        elif os_type == 'linux':
            os_name = 'linux'
        elif os_type == 'windows':
            os_name = 'windows'
        else:
            click.echo(click.style(f"[ERROR] Unsupported OS: {os_type}", fg='red'), err=True)
            sys.exit(1)
        
        if machine in ['x86_64', 'amd64']:
            arch = 'x64'
        elif machine in ['arm64', 'aarch64']:
            arch = 'arm64'
        elif machine in ['i386', 'i686']:
            arch = 'x86'
        else:
            click.echo(click.style(f"[ERROR] Unsupported architecture: {machine}", fg='red'), err=True)
            sys.exit(1)
        
        platform_suffix = f"{os_name}-{arch}"
        
        download_url = None
        asset_name = None
        for asset in release_data['assets']:
            if platform_suffix in asset['name']:
                download_url = asset['browser_download_url']
                asset_name = asset['name']
                break
        
        if not download_url or not asset_name:
            click.echo(click.style(f"[ERROR] No release found for {os_name} {arch}", fg='red'), err=True)
            sys.exit(1)
        
        click.echo(f"\nDownloading {asset_name}...")
        
        temp_dir = tempfile.mkdtemp()
        temp_file = os.path.join(temp_dir, asset_name)
        
        try:
            try:
                import requests
                download_response = requests.get(download_url, stream=True, timeout=30)
                download_response.raise_for_status()
                with open(temp_file, 'wb') as f:
                    for chunk in download_response.iter_content(chunk_size=8192):
                        f.write(chunk)
            except ImportError:
                import urllib.request
                urllib.request.urlretrieve(download_url, temp_file)
            
            click.echo(click.style("Download complete", fg='green'))
            click.echo("Extracting archive...")
            
            if os_name == 'windows':
                import zipfile
                with zipfile.ZipFile(temp_file, 'r') as zip_ref:
                    zip_ref.extractall(temp_dir)
            else:
                import tarfile
                with tarfile.open(temp_file, 'r:gz') as tar:
                    tar.extractall(temp_dir)
            
            if os_name == 'windows':
                install_dir = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'ControlCenter' / 'bin'
            else:
                current_binary_path = shutil.which('control-center')
                if current_binary_path:
                    install_dir = Path(current_binary_path).parent
                else:
                    install_dir = Path.home() / '.local' / 'bin'
            
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
                shutil.rmtree(temp_dir)
                sys.exit(1)
            
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
                    if os_name == 'windows' and dst.exists():
                        try:
                            old_binary = install_dir / f"{binary}.old"
                            if old_binary.exists():
                                try: old_binary.unlink()
                                except: pass
                            dst.rename(old_binary)
                        except Exception as e:
                            click.echo(click.style(f"[WARNING] Could not replace {binary}: {e}", fg='yellow'), err=True)
                            continue
                    
                    shutil.copy2(src, dst)
                    
                    if os_name != 'windows':
                        os.chmod(dst, os.stat(dst).st_mode | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)
                    
                    click.echo(click.style(f"  Updated {binary}", fg='green'))
                    installed_count += 1
                else:
                    click.echo(click.style(f"  - {binary} not found in archive (optional)", fg='yellow'))
            
            shutil.rmtree(temp_dir)
            
            if os_name == 'windows':
                for binary in binaries:
                    old_binary = install_dir / f"{binary}.old"
                    if old_binary.exists():
                        try: old_binary.unlink()
                        except: pass
            
            if installed_count == 0:
                click.echo(click.style("\n[ERROR] No binaries were installed", fg='red'), err=True)
                sys.exit(1)
            
            click.echo("\n" + "="*60)
            click.echo(click.style(f"Successfully updated to v{latest_version}!", fg='green', bold=True))
            click.echo("="*60)
            click.echo("\nRestart any running Control Center processes to use the new version.")
            
        except Exception as e:
            if Path(temp_dir).exists():
                shutil.rmtree(temp_dir)
            raise
    
    except Exception as e:
        logger.error(f"Update failed: {e}")
        click.echo(click.style(f"\n[ERROR] Update failed: {e}", fg='red'), err=True)
        click.echo(f"\nManually download from: https://github.com/{GITHUB_REPO}/releases/latest")
        sys.exit(1)


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
    
    os_type = platform.system().lower()
    
    paths_to_remove = []
    config_paths = []
    
    click.echo("Scanning for installed components...")
    
    if os_type == 'windows':
        binary_locations = [
            Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'ControlCenter' / 'bin',
            Path.home() / '.local' / 'bin',
        ]
        binary_names = ['control-center.exe', 'control-center-server.exe', 'control-center-agent.exe', 'generate-token.exe']
    else:
        binary_locations = [
            Path('/usr/local/bin'),
            Path.home() / '.local' / 'bin',
        ]
        binary_names = ['control-center', 'control-center-server', 'control-center-agent', 'generate-token']
    
    found_binaries = []
    for location in binary_locations:
        if location.exists():
            for binary in binary_names:
                binary_path = location / binary
                if binary_path.exists():
                    found_binaries.append(binary_path)
                    paths_to_remove.append(binary_path)
                old_binary = location / f"{binary}.old"
                if old_binary.exists():
                    paths_to_remove.append(old_binary)
    
    if purge:
        config_dir = ConfigManager.CONFIG_DIR
        if config_dir.exists():
            config_paths.append(config_dir)
        
        log_locations = [
            Path.home() / '.local' / 'share' / 'control-center' / 'logs',
            Path.home() / '.control-center' / 'logs',
            Path('/var/log/control-center') if os_type != 'windows' else None,
        ]
        for log_dir in [l for l in log_locations if l]:
            if log_dir and log_dir.exists():
                config_paths.append(log_dir)
    
    click.echo("")
    click.echo("The following components will be removed:")
    click.echo("")
    
    click.echo(click.style("Binaries:", fg='yellow', bold=True))
    if found_binaries:
        for binary in found_binaries:
            click.echo(f"  - {binary}")
    else:
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
    
    if not found_binaries and not config_paths:
        click.echo(click.style("Control Center is not installed on this system", fg='green'))
        return
    
    if not yes:
        click.echo(click.style("This action cannot be undone!", fg='red', bold=True))
        if not click.confirm('Do you want to continue?'):
            click.echo("\nUninstall cancelled.")
            return
    
    click.echo("")
    click.echo("Uninstalling...")
    click.echo("")
    
    removed = []
    failed = []
    
    for binary_path in paths_to_remove:
        try:
            if binary_path.exists():
                binary_path.unlink()
                removed.append(str(binary_path))
                click.echo(click.style(f"  Removed: {binary_path}", fg='green'))
        except PermissionError:
            if os_type == 'windows':
                try:
                    temp_path = binary_path.with_suffix('.delete_me')
                    if temp_path.exists():
                        try: temp_path.unlink()
                        except: pass
                    binary_path.rename(temp_path)
                    cmd = f'cmd /c ping 127.0.0.1 -n 3 > nul & del "{temp_path}"'
                    subprocess.Popen(cmd, shell=True,
                                     creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
                    removed.append(str(binary_path))
                    click.echo(click.style(f"  Scheduled for deletion: {binary_path}", fg='green'))
                    continue
                except Exception:
                    pass
            failed.append((str(binary_path), "Permission denied (File in use)"))
            click.echo(click.style(f"  Failed: {binary_path} (File in use)", fg='red'))
        except Exception as e:
            failed.append((str(binary_path), str(e)))
            click.echo(click.style(f"  Failed: {binary_path} ({e})", fg='red'))
    
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
                    click.echo(click.style(f"  Removed: {config_path}", fg='green'))
            except Exception as e:
                failed.append((str(config_path), str(e)))
                click.echo(click.style(f"  Failed: {config_path} ({e})", fg='red'))
    
    if os_type == 'windows':
        parent_dir = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'ControlCenter'
        try:
            if parent_dir.exists() and not any(parent_dir.iterdir()):
                parent_dir.rmdir()
                click.echo(click.style(f"  Removed empty directory: {parent_dir}", fg='green'))
        except Exception:
            pass
        
        # Clean PATH
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment', 0, winreg.KEY_ALL_ACCESS)
            try:
                path_value, _ = winreg.QueryValueEx(key, 'Path')
            except FileNotFoundError:
                path_value = ""
            control_center_fragment = str(Path('Programs/ControlCenter/bin')).lower()
            new_paths = []
            changed = False
            if path_value:
                for part in path_value.split(';'):
                    if control_center_fragment in part.lower().replace('/', '\\'):
                        changed = True
                    elif part.strip():
                        new_paths.append(part)
            if changed:
                winreg.SetValueEx(key, 'Path', 0, winreg.REG_EXPAND_SZ, ';'.join(new_paths))
                click.echo(click.style("  Updated Windows PATH variable", fg='green'))
            winreg.CloseKey(key)
        except Exception as e:
            click.echo(click.style(f"  Warning: Could not remove from PATH: {e}", fg='yellow'))
    else:
        for parent_dir in [Path.home() / '.local' / 'share' / 'control-center',
                           Path.home() / '.control-center']:
            try:
                if parent_dir.exists() and not any(parent_dir.iterdir()):
                    parent_dir.rmdir()
                    click.echo(click.style(f"  Removed empty directory: {parent_dir}", fg='green'))
            except Exception:
                pass
    
    click.echo("")
    click.echo("="*60)
    
    if removed and not failed:
        click.echo(click.style("Uninstall completed successfully!", fg='green', bold=True))
        click.echo(f"\nRemoved {len(removed)} item(s).")
    elif removed and failed:
        click.echo(click.style("Uninstall partially completed", fg='yellow', bold=True))
        click.echo(f"\nSuccessfully removed: {len(removed)}, Failed: {len(failed)}")
        for path, error in failed:
            click.echo(f"  - {path}: {error}")
        if os_type != 'windows':
            click.echo("\nTip: Try with sudo for system-wide installations.")
    elif not removed and failed:
        click.echo(click.style("Uninstall failed", fg='red', bold=True))
        for path, error in failed:
            click.echo(f"  - {path}: {error}")
        sys.exit(1)
    
    click.echo("")
    click.echo("Thank you for using Control Center!")
    click.echo("Feedback: https://github.com/nullvoider07/control-center/issues")

# ============================================================================
# Config Commands  (verbatim from original)
# ============================================================================

@cli.group()
def config():
    """Manage configuration"""
    pass

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

@config.command('set-token')
@click.argument('token')
def config_set_token(token: str):
    """Set API token in config file"""
    try:
        ctx.config_manager.set_token(token)
        click.echo("API token saved to config")
    except ConfigurationError as e:
        click.echo(f"Configuration error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

@config.command('set-server')
@click.argument('host')
@click.argument('port', type=int, default=50051)
def config_set_server(host: str, port: int):
    """Set default server host and port"""
    try:
        require_valid_host(host)
        require_valid_port(port)
        ctx.config_manager.set_server(host, port)
        click.echo(f"Default server set to {host}:{port}")
    except ConfigurationError as e:
        click.echo(f"Configuration error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

@config.command('set')
@click.argument('key')
@click.argument('value')
def config_set(key: str, value: str):
    """Set an arbitrary config key (e.g. jwt_secret)"""
    try:
        ctx.config_manager.set(key, value)
        click.echo(f"Set {key}")
    except ConfigurationError as e:
        click.echo(f"Configuration error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

@config.command('clear-token')
def config_clear_token():
    """Remove API token from config"""
    try:
        ctx.config_manager.clear_token()
        click.echo("API token cleared from config")
    except ConfigurationError as e:
        click.echo(f"Configuration error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

@config.command('validate')
def config_validate():
    """Validate current configuration"""
    try:
        is_valid, errors = ctx.config_manager.validate()
        if is_valid:
            click.echo("Configuration is valid")
        else:
            click.echo("Configuration is invalid", err=True)
            for error in errors:
                click.echo(f"  - {error}", err=True)
            sys.exit(1)
    except ConfigurationError as e:
        click.echo(f"Configuration error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

@config.command('reset')
@click.confirmation_option(prompt='Reset configuration to defaults?')
def config_reset():
    """Reset configuration to defaults"""
    try:
        ctx.config_manager.reset()
        click.echo("Configuration reset to defaults")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

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
# Info Commands  (verbatim from original)
# ============================================================================

@cli.command()
def version():
    """Show version information"""
    click.echo(f"Control Center v{__version__}")
    click.echo("")
    click.echo("Components:")
    server_bin = _find_binary('control-center-server')
    click.echo(f"  Server: {server_bin}" if server_bin else "  Server: Not found")
    agent_bin = _find_binary('control-center-agent')
    click.echo(f"  Agent:  {agent_bin}" if agent_bin else "  Agent:  Not found")
    click.echo(f"  CLI:    Python v{__version__}")

@cli.command()
def doctor():
    """Check system configuration and dependencies"""
    click.echo("=== Control Center System Check ===\n")
    click.echo(f"Python: {sys.version.split()[0]} OK")
    server_bin = _find_binary('control-center-server')
    click.echo(f"Server binary: {server_bin} OK" if server_bin else "Server binary: Not found")
    agent_bin = _find_binary('control-center-agent')
    click.echo(f"Agent binary: {agent_bin} OK" if agent_bin else "Agent binary: Not found")
    if ctx.config_manager.CONFIG_FILE.exists():
        click.echo(f"Config file: {ctx.config_manager.CONFIG_FILE} OK")
    else:
        click.echo(f"Config file: Not found (run 'control-center config init')")
    try:
        import grpc
        click.echo("gRPC: Installed OK")
    except ImportError:
        click.echo("gRPC: Not installed")
    try:
        import jwt
        click.echo("PyJWT: Installed OK")
    except ImportError:
        click.echo("PyJWT: Not installed  (pip install PyJWT)")
    click.echo("\n=== System Check Complete ===")


def _find_binary(binary_name: str) -> Optional[str]:
    """Find binary in common locations"""
    possible_locations = [
        f"./{binary_name}",
        f"./bin/{binary_name}",
        f"./target/release/{binary_name}",
        str(Path.home() / ".local" / "bin" / binary_name),
        f"/usr/local/bin/{binary_name}",
        binary_name,
    ]
    for location in possible_locations:
        if os.path.exists(location):
            return location
    return shutil.which(binary_name)

# ============================================================================
# Server Commands  (verbatim from original)
# ============================================================================

@cli.group()
def server():
    """Manage Control Center server (Rust binary)"""
    pass

@server.command(name='start')
@click.option('--host', default='0.0.0.0', help='Host to bind to')
@click.option('--port', default=50051, help='gRPC port')
@click.option('--single-agent/--multi-agent', default=True,
              help='Only allow one agent connection (default: single-agent)')
@click.option('--network', help='Network identifier for this server')
@click.option('--auth-url', help='OAuth2 authorization URL')
@click.option('--token-url', help='OAuth2 token URL')
@click.option('--client-id', help='OAuth2 client ID')
def server_start(host, port, single_agent, network, auth_url, token_url, client_id):
    """Start the Rust gRPC server
    
    Examples:
        control-center server start
        control-center server start --network datacenter-east
        control-center server start --multi-agent
        control-center server start --host 0.0.0.0 --port 8080
    """
    click.echo(f"[START] Starting Control Center Server (Rust) on {host}:{port}")
    click.echo(f"[INFO] Single-agent mode: {single_agent}")
    if network:
        click.echo(f"[INFO] Network: {network}")
    click.echo(f"[INFO] Ready to accept agent connections")
    
    env = os.environ.copy()
    env['GRPC_HOST'] = host
    env['GRPC_PORT'] = str(port)
    env['SINGLE_AGENT_MODE'] = 'true' if single_agent else 'false'
    if network:
        env['CONTROL_CENTER_NETWORK'] = network
    if auth_url:
        env['OAUTH_AUTH_URL'] = auth_url
    if token_url:
        env['OAUTH_TOKEN_URL'] = token_url
    if client_id:
        env['OAUTH_CLIENT_ID'] = client_id

    # BUG-004 FIX: The Rust binary reads JWT_SECRET (not CC_JWT_SECRET).
    # Map CC_JWT_SECRET → JWT_SECRET so users only need to export one variable.
    # Config file is the fallback if the env var isn't set.
    if 'JWT_SECRET' not in env:
        cc_secret = os.environ.get('CC_JWT_SECRET') or ctx.config_manager.get('jwt_secret')
        if cc_secret:
            env['JWT_SECRET'] = cc_secret
        else:
            click.echo(
                "[ERROR] JWT_SECRET is required by the server but is not set.\n"
                "  Export CC_JWT_SECRET (≥32 chars) before running server start:\n"
                "    export CC_JWT_SECRET='your-secret-at-least-32-chars'\n"
                "  Or store it: control-center config set jwt_secret YOUR_SECRET",
                err=True
            )
            sys.exit(1)

    # Pass audience and issuer so the server uses the same values as the CLI.
    # These default to the same values the server uses when the vars are absent,
    # so this is a no-op unless the user has overridden them.
    env.setdefault('JWT_AUDIENCE', os.environ.get('JWT_AUDIENCE', 'control-center'))
    env.setdefault('JWT_ISSUER',   os.environ.get('JWT_ISSUER',   'control-center-auth'))
    
    server_bin = _find_binary('control-center-server')
    if not server_bin:
        click.echo("[ERROR] 'control-center-server' binary not found!", err=True)
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

# ============================================================================
# Register monitoring group + entry point  (verbatim from original)
# ============================================================================

cli.add_command(monitoring, name='monitor')

def main():
    """Main entry point"""
    try:
        cli(prog_name='control-center')
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)

if __name__ == '__main__':
    main()