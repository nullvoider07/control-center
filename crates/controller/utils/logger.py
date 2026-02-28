"""Logging utilities with rotation and structured output - TOKEN-BASED"""

import logging
import sys
from pathlib import Path
from datetime import datetime
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from typing import Optional, Dict, Any
import json

# === Logging Utilities ===
class JSONFormatter(logging.Formatter):
    """JSON formatter for structured logging"""
    
    # Override format method to output JSON
    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON"""
        log_data = {
            'timestamp': datetime.fromtimestamp(record.created).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno,
        }
        
        # Add exception info if present
        if record.exc_info:
            log_data['exception'] = self.formatException(record.exc_info)
        
        # Pass through ALL extra fields added via the extra={} kwarg to logger calls.
        # Previously only user_id/session_id/command were captured; this meant that
        # AuditLogger's structured fields (event, success, ip_address, attempt, etc.)
        # were silently dropped from the JSON output.
        _STDLIB_ATTRS = frozenset({
            'name', 'msg', 'args', 'levelname', 'levelno', 'pathname', 'filename',
            'module', 'exc_info', 'exc_text', 'stack_info', 'lineno', 'funcName',
            'created', 'msecs', 'relativeCreated', 'thread', 'threadName',
            'processName', 'process', 'message', 'taskName',
        })
        for key, value in record.__dict__.items():
            if key not in _STDLIB_ATTRS and not key.startswith('_'):
                log_data[key] = value
        
        return json.dumps(log_data)

# === Colored Console Formatter ===
class ColoredFormatter(logging.Formatter):
    """Colored formatter for console output"""
    
    # ANSI color codes
    COLORS = {
        'DEBUG': '\033[36m',      # Cyan
        'INFO': '\033[32m',       # Green
        'WARNING': '\033[33m',    # Yellow
        'ERROR': '\033[31m',      # Red
        'CRITICAL': '\033[35m',   # Magenta
        'RESET': '\033[0m',       # Reset
    }
    
    # Override format method to add colors
    def format(self, record: logging.LogRecord) -> str:
        """Format log record with colors"""
        # Get color for level
        color = self.COLORS.get(record.levelname, self.COLORS['RESET'])
        reset = self.COLORS['RESET']
        
        # Format timestamp
        timestamp = datetime.fromtimestamp(record.created).strftime('%Y-%m-%d %H:%M:%S')
        
        # Format level with color
        level = f"{color}{record.levelname:<8}{reset}"
        
        # Format message
        message = record.getMessage()
        
        # Add exception if present
        if record.exc_info:
            message += '\n' + self.formatException(record.exc_info)
        
        return f"{timestamp} {level} {message}"

# === Logger Setup Function ===
def setup_logger(
    name: str,
    log_dir: str = "./logs",
    log_level: str = "INFO",
    max_bytes: int = 10 * 1024 * 1024,  # 10MB
    backup_count: int = 5,
    json_logs: bool = False,
    console_colors: bool = True,
) -> logging.Logger:
    """
    Setup logger with rotation
    
    Args:
        name: Logger name (typically module or application name)
        log_dir: Directory for log files
        log_level: Minimum log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        max_bytes: Maximum size per log file before rotation
        backup_count: Number of backup files to keep
        json_logs: Use JSON format for file logs
        console_colors: Use colors in console output
        
    Returns:
        Configured logger instance
        
    Example:
        >>> logger = setup_logger('control-center')
        >>> logger.info("Application started")
        >>> logger.error("Connection failed", exc_info=True)
    """
    # Create logger
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, log_level.upper()))
    logger.handlers.clear()  # Clear any existing handlers
    
    # Create logs directory
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    
    # === Console Handler ===
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    
    if console_colors and sys.stdout.isatty():
        console_format = ColoredFormatter()
    else:
        console_format = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
    
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)
    
    # === File Handler (Rotating) ===
    log_file = log_path / f"{name}.log"
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count
    )
    file_handler.setLevel(logging.DEBUG)  # Capture all levels to file
    
    if json_logs:
        file_format = JSONFormatter()
    else:
        file_format = logging.Formatter(
            '%(asctime)s [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
    
    file_handler.setFormatter(file_format)
    logger.addHandler(file_handler)
    
    # === Error File Handler (Separate file for errors) ===
    error_log_file = log_path / f"{name}_errors.log"
    error_handler = RotatingFileHandler(
        error_log_file,
        maxBytes=max_bytes,
        backupCount=backup_count
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(file_format)
    logger.addHandler(error_handler)
    
    logger.debug(f"Logger initialized: {name}")
    logger.debug(f"Log directory: {log_path.absolute()}")
    logger.debug(f"Log level: {log_level}")
    
    return logger

# === Debug Logger Setup (Console Only) ===
def setup_debug_logger(name: str) -> logging.Logger:
    """
    Setup debug logger (console only, maximum verbosity)
    
    Useful for development and troubleshooting
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    
    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] [%(name)s:%(funcName)s:%(lineno)d] %(message)s',
        datefmt='%H:%M:%S'
    )
    
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    
    return logger

# Alias for backward compatibility
setup_production_logger = setup_logger

# === Specialized Loggers for Commands and Auditing ===
class CommandLogger:
    """Specialized logger for command execution tracking"""
    
    def __init__(self, base_logger: logging.Logger):
        self.logger = base_logger
        self.command_count = 0
    
    # Log command execution with structured data
    def log_command(
        self,
        command: str,
        success: bool,
        execution_time_ms: int,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        error: Optional[str] = None
    ):
        """
        Log command execution with structured data
        
        Args:
            command: Command that was executed
            success: Whether execution succeeded
            execution_time_ms: Execution time in milliseconds
            session_id: Session identifier
            user_id: User identifier from JWT token
            error: Error message if failed
        """
        self.command_count += 1
        
        # Create log record with extra fields
        extra = {
            'command': command,
            'success': success,
            'execution_time_ms': execution_time_ms,
            'command_number': self.command_count,
        }
        
        if session_id:
            extra['session_id'] = session_id
        
        if user_id:
            extra['user_id'] = user_id
        
        # Log message
        if success:
            self.logger.info(
                f"Command #{self.command_count} executed in {execution_time_ms}ms: {command}",
                extra=extra
            )
        else:
            error_msg = error or "Unknown error"
            self.logger.error(
                f"Command #{self.command_count} failed ({execution_time_ms}ms): {command} - {error_msg}",
                extra=extra
            )
    
    # Log batch execution summary
    def log_batch_start(self, batch_size: int, batch_id: Optional[str] = None):
        """Log start of batch execution"""
        extra: Dict[str, Any] = {'batch_size': batch_size}
        if batch_id:
            extra['batch_id'] = batch_id
        
        self.logger.info(
            f"Starting batch execution: {batch_size} commands",
            extra=extra
        )
    
    # Log batch completion with summary
    def log_batch_complete(
        self,
        total: int,
        successful: int,
        failed: int,
        total_time_ms: int,
        batch_id: Optional[str] = None
    ):
        """Log completion of batch execution"""
        success_rate = (successful / total * 100) if total > 0 else 0
        
        extra = {
            'total': total,
            'successful': successful,
            'failed': failed,
            'success_rate': success_rate,
            'total_time_ms': total_time_ms,
        }
        
        if batch_id:
            extra['batch_id'] = batch_id
        
        self.logger.info(
            f"Batch complete: {successful}/{total} successful ({success_rate:.1f}%) in {total_time_ms}ms",
            extra=extra
        )

# === Audit Logger for Security Events ===
class AuditLogger:
    """Specialized logger for security auditing"""
    
    def __init__(self, log_dir: str = "./logs/audit"):
        self.log_path = Path(log_dir)
        self.log_path.mkdir(parents=True, exist_ok=True)
        
        # Create audit logger with daily rotation
        self.logger = logging.getLogger('audit')
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()
        
        # Daily rotating file handler
        handler = TimedRotatingFileHandler(
            self.log_path / 'audit.log',
            when='midnight',
            interval=1,
            backupCount=365,  # Keep 1 year of audit logs
        )
        
        # JSON format for easy parsing
        handler.setFormatter(JSONFormatter())
        self.logger.addHandler(handler)
    
    # Log authentication attempts with structured data
    def log_auth_attempt(
        self,
        user_id: str,
        success: bool,
        ip_address: Optional[str] = None,
        reason: Optional[str] = None
    ):
        """Log authentication attempt"""
        extra = {
            'event': 'auth_attempt',
            'user_id': user_id,
            'success': success,
        }
        
        if ip_address:
            extra['ip_address'] = ip_address
        if reason:
            extra['reason'] = reason
        
        if success:
            self.logger.info(f"Successful authentication: {user_id}", extra=extra)
        else:
            self.logger.warning(f"Failed authentication: {user_id}", extra=extra)
    
    # Log session start and end with duration
    def log_session_start(self, session_id: str, user_id: str):
        """Log session start"""
        self.logger.info(
            f"Session started: {session_id}",
            extra={'event': 'session_start', 'session_id': session_id, 'user_id': user_id}
        )
    
    # Log session end with duration
    def log_session_end(self, session_id: str, duration_seconds: float):
        """Log session end"""
        self.logger.info(
            f"Session ended: {session_id}",
            extra={'event': 'session_end', 'session_id': session_id, 'duration_seconds': duration_seconds}
        )
    
    # Log VM shutdown detection
    def log_vm_shutdown(self, session_id: str, user_id: str, host: str):
        """Log VM/Container shutdown detection"""
        self.logger.error(
            f"VM shutdown detected: {session_id} for {user_id} at {host}",
            extra={
                'event': 'vm_shutdown',
                'session_id': session_id,
                'user_id': user_id,
                'host': host
            }
        )
    
    # Log reconnection attempt
    def log_reconnection_attempt(self, session_id: str, user_id: str, attempt: int):
        """Log reconnection attempt"""
        self.logger.warning(
            f"Reconnection attempt #{attempt}: {session_id}",
            extra={
                'event': 'reconnection_attempt',
                'session_id': session_id,
                'user_id': user_id,
                'attempt': attempt
            }
        )
    
    # Log permission denial with details
    def log_permission_denied(self, user_id: str, action: str, resource: str):
        """Log permission denial"""
        self.logger.warning(
            f"Permission denied: {user_id} attempted {action} on {resource}",
            extra={'event': 'permission_denied', 'user_id': user_id, 'action': action, 'resource': resource}
        )

    # Log agent disconnect event
    def log_agent_disconnect(self, session_id: str, user_id: str, reason: str):
        """Log operator-initiated agent disconnect"""
        self.logger.info(
            f"Agent disconnected: {session_id} by {user_id}",
            extra={
                'event': 'agent_disconnect',
                'session_id': session_id,
                'user_id': user_id,
                'reason': reason,
            }
        )

# Global logger instance (can be imported)
logger = None

# Global audit logger singleton — lazy-initialised on first call
_audit_logger: Optional[AuditLogger] = None

# Function to get or create logger instance
def get_logger(name: Optional[str] = None) -> logging.Logger:
    """
    Get or create logger instance
    
    Args:
        name: Logger name (defaults to 'control-center')
        
    Returns:
        Logger instance
    """
    global logger
    
    if logger is None:
        logger = setup_logger(name or 'control-center')
    
    return logger

# Function to get or create audit logger singleton
def get_audit_logger(log_dir: str = "./logs/audit") -> AuditLogger:
    """Return the process-wide AuditLogger singleton.

    Creates the logger on first call (with the supplied log_dir), then returns
    the same instance on every subsequent call regardless of log_dir — matching
    the behaviour of get_logger().

    Usage in cli.py::

        from controller.utils.logger import get_audit_logger
        audit = get_audit_logger()
        audit.log_session_start(session_id, user_id)
        audit.log_auth_attempt(user_id, success=True, ip_address=host)
        ...
        audit.log_session_end(session_id, duration_seconds)
    """
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = AuditLogger(log_dir=log_dir)
    return _audit_logger