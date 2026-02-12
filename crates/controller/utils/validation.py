"""Input validation utilities - TOKEN-BASED"""

import re
import ipaddress
from typing import Tuple, List, Optional
import logging

# Set up logging
logger = logging.getLogger(__name__)

# Exception classes
class ValidationError(Exception):
    """Validation error exception"""
    pass

# Validation functions
def validate_host(host: str) -> Tuple[bool, str]:
    """
    Validate host IP or hostname
    
    Args:
        host: IP address or hostname to validate
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not host or not host.strip():
        return False, "Host cannot be empty"
    
    host = host.strip()
    
    if len(host) > 253:
        return False, "Host name too long (max 253 characters)"
    
    # Try IP address validation
    try:
        ip = ipaddress.ip_address(host)
        
        if ip.is_multicast:
            return False, "Multicast address not allowed"
        
        if ip.is_reserved:
            return False, "Reserved IP address"
        
        if ip.is_unspecified:
            return False, "Unspecified address (0.0.0.0 or ::)"
        
        if ip.is_private:
            logger.debug(f"Private IP address: {host}")
        
        return True, ""
        
    except ValueError:
        pass
    
    # Try hostname validation (RFC 1123)
    hostname_pattern = r'^([a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)*[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?$'
    
    if re.match(hostname_pattern, host):
        if host.startswith('-') or host.endswith('-'):
            return False, "Hostname cannot start or end with hyphen"
        
        if '..' in host:
            return False, "Hostname cannot contain consecutive dots"
        
        labels = host.split('.')
        for label in labels:
            if len(label) > 63:
                return False, f"Hostname label too long: {label} (max 63)"
            if not label:
                return False, "Empty hostname label"
        
        return True, ""
    
    return False, "Invalid IP address or hostname format"

# Validate port number
def validate_port(port: int) -> Tuple[bool, str]:
    """Validate port number"""
    if not isinstance(port, int):
        return False, "Port must be an integer"
    
    if port < 1:
        return False, "Port must be >= 1"
    
    if port > 65535:
        return False, "Port must be <= 65535"
    
    if port < 1024:
        logger.warning(f"Using privileged port: {port}")
    
    return True, ""

# Validate screen coordinates
def validate_coordinates(x: int, y: int, max_x: int = 7680, max_y: int = 4320) -> Tuple[bool, str]:
    """Validate screen coordinates (up to 8K resolution)"""
    if not isinstance(x, int) or not isinstance(y, int):
        return False, "Coordinates must be integers"
    
    if x < 0 or y < 0:
        return False, f"Coordinates cannot be negative: ({x}, {y})"
    
    if x > max_x or y > max_y:
        return False, f"Coordinates exceed screen bounds: ({x}, {y}) > ({max_x}, {max_y})"
    
    return True, ""

# Validate command input
def validate_command(command: str, max_length: int = 1000) -> Tuple[bool, str]:
    """Validate command input with security checks"""
    if not command or not command.strip():
        return False, "Command cannot be empty"
    
    if len(command) > max_length:
        return False, f"Command too long: {len(command)} > {max_length} characters"
    
    if '\x00' in command:
        return False, "Command contains null bytes"
    
    # Check for control characters (except newline and tab)
    control_chars = [chr(i) for i in range(32) if i not in (9, 10, 13)]
    for char in control_chars:
        if char in command:
            return False, f"Command contains control character: {repr(char)}"
    
    return True, ""

# Sanitize command input and provide warnings
def sanitize_command(command: str) -> Tuple[str, List[str]]:
    """Sanitize command input and provide warnings"""
    warnings = []
    original = command
    
    # Remove null bytes
    if '\x00' in command:
        command = command.replace('\x00', '')
        warnings.append("Removed null bytes")
    
    # Remove control characters (keep tab, newline)
    cleaned = []
    for char in command:
        code = ord(char)
        if code < 32 and code not in (9, 10, 13):
            warnings.append(f"Removed control character: {repr(char)}")
        else:
            cleaned.append(char)
    
    command = ''.join(cleaned)
    command = command.strip()
    
    if command != original.strip():
        warnings.append("Command was sanitized")
    
    return command, warnings

# Validate JWT session token
def validate_session_token(token: str) -> Tuple[bool, str]:
    """
    Validate JWT session token format
    
    Args:
        token: JWT token to validate
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not token or not token.strip():
        return False, "Token cannot be empty"
    
    token = token.strip()
    
    # JWT tokens are typically 100-500 chars
    if len(token) < 20:
        return False, "Token too short"
    
    if len(token) > 2000:
        return False, "Token too long"
    
    # JWT has 3 parts separated by dots
    parts = token.split('.')
    if len(parts) != 3:
        return False, "Invalid token format (expected JWT with 3 parts)"
    
    # Check each part is base64-like
    for i, part in enumerate(parts):
        if not part:
            return False, f"Token part {i+1} is empty"
        
        if not re.match(r'^[A-Za-z0-9_-]+$', part):
            return False, f"Token part {i+1} contains invalid characters"
    
    return True, ""

# Validate file path for batch command files
def validate_file_path(path: str, allowed_extensions: Optional[List[str]] = None) -> Tuple[bool, str]:
    """Validate file path for batch command files"""
    import os
    
    if not path or not path.strip():
        return False, "File path cannot be empty"
    
    path = path.strip()
    
    # Check for path traversal
    if '..' in path:
        return False, "Path traversal detected (..) not allowed"
    
    if not os.path.exists(path):
        return False, f"File does not exist: {path}"
    
    if not os.path.isfile(path):
        return False, f"Path is not a file: {path}"
    
    # Check file extension
    if allowed_extensions:
        ext = os.path.splitext(path)[1].lower()
        if ext not in allowed_extensions:
            return False, f"File extension {ext} not allowed. Allowed: {allowed_extensions}"
    
    # Check file size (max 10MB)
    try:
        size = os.path.getsize(path)
        if size > 10 * 1024 * 1024:
            return False, f"File too large: {size} bytes (max 10MB)"
        
        if size == 0:
            return False, "File is empty"
            
    except OSError as e:
        return False, f"Cannot access file: {e}"
    
    return True, ""

# Validate user ID from JWT token claims
def validate_user_id(user_id: str) -> Tuple[bool, str]:
    """
    Validate user ID from JWT token claims
    
    Args:
        user_id: User identifier from token 'sub' claim
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not user_id or not user_id.strip():
        return False, "User ID cannot be empty"
    
    user_id = user_id.strip()
    
    # Check length
    if len(user_id) < 1:
        return False, "User ID too short"
    
    if len(user_id) > 256:
        return False, "User ID too long (max 256 characters)"
    
    return True, ""

# Convenience functions that raise exceptions
def require_valid_host(host: str) -> str:
    """Validate host or raise ValidationError"""
    is_valid, error = validate_host(host)
    if not is_valid:
        raise ValidationError(error)
    return host.strip()

# Convenience functions that raise exceptions
def require_valid_port(port: int) -> int:
    """Validate port or raise ValidationError"""
    is_valid, error = validate_port(port)
    if not is_valid:
        raise ValidationError(error)
    return port

# Convenience functions that raise exceptions
def require_valid_coordinates(x: int, y: int, max_x: int = 7680, max_y: int = 4320) -> Tuple[int, int]:
    """Validate coordinates or raise ValidationError"""
    is_valid, error = validate_coordinates(x, y, max_x, max_y)
    if not is_valid:
        raise ValidationError(error)
    return (x, y)

# Convenience functions that raise exceptions
def require_valid_command(command: str, max_length: int = 1000) -> str:
    """Validate command or raise ValidationError"""
    is_valid, error = validate_command(command, max_length)
    if not is_valid:
        raise ValidationError(error)
    return command.strip()

# Convenience functions that raise exceptions
def require_valid_token(token: str) -> str:
    """Validate token or raise ValidationError"""
    is_valid, error = validate_session_token(token)
    if not is_valid:
        raise ValidationError(error)
    return token.strip()