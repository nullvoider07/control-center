"""Utility helper functions"""

import re
from typing import Optional, Tuple, List
from datetime import datetime, timedelta
from pathlib import Path
import logging

# Set up logging
logger = logging.getLogger(__name__)

# Helper functions for parsing and formatting data, validating input, and other common tasks
def parse_coordinates(text: str) -> Optional[Tuple[int, int]]:
    """
    Parse coordinates from text with validation
    
    Args:
        text: Text containing coordinates (e.g., "960 540" or "960,540")
        
    Returns:
        Tuple of (x, y) coordinates or None if invalid
        
    Examples:
        >>> parse_coordinates("960 540")
        (960, 540)
        >>> parse_coordinates("1920, 1080")
        (1920, 1080)
        >>> parse_coordinates("invalid")
        None
    """
    # Try space-separated format first
    pattern_space = r'(\d+)\s+(\d+)'
    match = re.match(pattern_space, text.strip())
    
    if match:
        x, y = int(match.group(1)), int(match.group(2))
        
        # Validate ranges (support up to 8K resolution)
        if 0 <= x <= 7680 and 0 <= y <= 4320:
            return (x, y)
        else:
            logger.warning(f"Coordinates out of range: ({x}, {y})")
            return None
    
    # Try comma-separated format
    pattern_comma = r'(\d+),\s*(\d+)'
    match = re.match(pattern_comma, text.strip())
    
    if match:
        x, y = int(match.group(1)), int(match.group(2))
        
        if 0 <= x <= 7680 and 0 <= y <= 4320:
            return (x, y)
        else:
            logger.warning(f"Coordinates out of range: ({x}, {y})")
            return None
    
    return None

# Additional helper functions for formatting durations, timestamps, parsing batch files, and more
def format_duration(seconds: float) -> str:
    """
    Format duration in human-readable format
    
    Args:
        seconds: Duration in seconds
        
    Returns:
        Formatted duration string
        
    Examples:
        >>> format_duration(45)
        '45.0s'
        >>> format_duration(125)
        '2m 5s'
        >>> format_duration(3665)
        '1h 1m 5s'
    """
    if seconds < 60:
        return f"{seconds:.1f}s"
    
    minutes, secs = divmod(int(seconds), 60)
    
    if minutes < 60:
        return f"{minutes}m {secs}s"
    
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m {secs}s"

# Format Unix timestamp to human-readable string
def format_timestamp(timestamp: float, format_str: str = "%Y-%m-%d %H:%M:%S") -> str:
    """
    Format Unix timestamp to human-readable string
    
    Args:
        timestamp: Unix timestamp (seconds since epoch)
        format_str: strftime format string
        
    Returns:
        Formatted timestamp string
    """
    dt = datetime.fromtimestamp(timestamp)
    return dt.strftime(format_str)

# Parse batch command file with validation
def parse_batch_file(filepath: str) -> List[str]:
    """
    Parse batch command file with validation
    
    Args:
        filepath: Path to batch file
        
    Returns:
        List of commands (empty lines and comments removed)
        
    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If file is empty or too large
    """
    path = Path(filepath)
    
    if not path.exists():
        raise FileNotFoundError(f"Batch file not found: {filepath}")
    
    if not path.is_file():
        raise ValueError(f"Path is not a file: {filepath}")
    
    # Check file size (max 10MB)
    file_size = path.stat().st_size
    if file_size > 10 * 1024 * 1024:
        raise ValueError(f"Batch file too large: {file_size} bytes (max 10MB)")
    
    if file_size == 0:
        raise ValueError("Batch file is empty")
    
    # Read and parse
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # Filter and clean
    commands = []
    for i, line in enumerate(lines, 1):
        # Remove comments
        if '#' in line:
            line = line[:line.index('#')]
        
        # Strip whitespace
        line = line.strip()
        
        # Skip empty lines
        if not line:
            continue
        
        # Validate line length
        if len(line) > 1000:
            logger.warning(f"Line {i} exceeds 1000 characters, truncating")
            line = line[:1000]
        
        commands.append(line)
    
    if not commands:
        raise ValueError("No valid commands found in batch file")
    
    logger.info(f"Loaded {len(commands)} commands from {filepath}")
    return commands

# Chunk list into smaller sublists of specified size
def chunk_list(items: List, chunk_size: int) -> List[List]:
    """
    Split list into chunks
    
    Args:
        items: List to split
        chunk_size: Size of each chunk
        
    Returns:
        List of chunks
        
    Examples:
        >>> chunk_list([1, 2, 3, 4, 5], 2)
        [[1, 2], [3, 4], [5]]
    """
    if chunk_size <= 0:
        raise ValueError("Chunk size must be positive")
    
    return [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]

# Sanitize filename to be filesystem-safe
def sanitize_filename(filename: str) -> str:
    """
    Sanitize filename to be filesystem-safe
    
    Args:
        filename: Original filename
        
    Returns:
        Sanitized filename
        
    Examples:
        >>> sanitize_filename("my file/name?.txt")
        'my_file_name.txt'
    """
    # Remove or replace invalid characters
    invalid_chars = r'[<>:"/\\|?*]'
    sanitized = re.sub(invalid_chars, '_', filename)
    
    # Remove leading/trailing whitespace and dots
    sanitized = sanitized.strip('. ')
    
    # Limit length (255 chars is common filesystem limit)
    if len(sanitized) > 255:
        name, ext = sanitized.rsplit('.', 1) if '.' in sanitized else (sanitized, '')
        max_name_len = 255 - len(ext) - 1 if ext else 255
        sanitized = f"{name[:max_name_len]}.{ext}" if ext else name[:255]
    
    return sanitized

# Truncate string to maximum length
def truncate_string(text: str, max_length: int, suffix: str = "...") -> str:
    """
    Truncate string to maximum length
    
    Args:
        text: String to truncate
        max_length: Maximum length (including suffix)
        suffix: Suffix to add if truncated
        
    Returns:
        Truncated string
    """
    if len(text) <= max_length:
        return text
    
    return text[:max_length - len(suffix)] + suffix

# Extract all numbers from text
def extract_numbers(text: str) -> List[int]:
    """
    Extract all numbers from text
    
    Args:
        text: Text containing numbers
        
    Returns:
        List of integers found in text
        
    Examples:
        >>> extract_numbers("Move to 960 540 and click")
        [960, 540]
    """
    pattern = r'\d+'
    matches = re.findall(pattern, text)
    return [int(m) for m in matches]

# Validate if string is a valid IPv4 address
def is_valid_ipv4(ip: str) -> bool:
    """
    Check if string is valid IPv4 address
    
    Args:
        ip: IP address string
        
    Returns:
        True if valid IPv4
    """
    pattern = r'^(\d{1,3}\.){3}\d{1,3}$'
    
    if not re.match(pattern, ip):
        return False
    
    # Check each octet is 0-255
    octets = ip.split('.')
    return all(0 <= int(octet) <= 255 for octet in octets)

# Validate if string is a valid hostname
def is_valid_hostname(hostname: str) -> bool:
    """
    Check if string is valid hostname
    
    Args:
        hostname: Hostname string
        
    Returns:
        True if valid hostname
    """
    if len(hostname) > 253:
        return False
    
    # RFC 1123 compliant
    pattern = r'^([a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)*[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?$'
    
    return bool(re.match(pattern, hostname))

# Calculate checksum of data
def calculate_checksum(data: bytes, algorithm: str = 'sha256') -> str:
    """
    Calculate checksum of data
    
    Args:
        data: Data to checksum
        algorithm: Hash algorithm (md5, sha1, sha256)
        
    Returns:
        Hex digest of checksum
    """
    import hashlib
    
    if algorithm == 'md5':
        h = hashlib.md5()
    elif algorithm == 'sha1':
        h = hashlib.sha1()
    elif algorithm == 'sha256':
        h = hashlib.sha256()
    else:
        raise ValueError(f"Unsupported algorithm: {algorithm}")
    
    h.update(data)
    return h.hexdigest()

# Retry function with exponential backoff
def retry_with_backoff(
    func,
    max_attempts: int = 3,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
    exceptions: tuple = (Exception,)
):
    """
    Retry function with exponential backoff
    
    Args:
        func: Function to retry
        max_attempts: Maximum number of attempts
        initial_delay: Initial delay in seconds
        backoff_factor: Multiplier for each retry
        exceptions: Tuple of exceptions to catch
        
    Returns:
        Function result
        
    Raises:
        Last exception if all retries fail
    """
    import time
    
    delay = initial_delay
    last_exception = Exception("No attempts made")
    
    for attempt in range(max_attempts):
        try:
            return func()
        except exceptions as e:
            last_exception = e
            
            if attempt < max_attempts - 1:
                logger.warning(
                    f"Attempt {attempt + 1}/{max_attempts} failed: {e}. "
                    f"Retrying in {delay}s..."
                )
                time.sleep(delay)
                delay *= backoff_factor
            else:
                logger.error(f"All {max_attempts} attempts failed")
    
    raise last_exception

# Get system information
def get_system_info() -> dict:
    """
    Get system information
    
    Returns:
        Dictionary with system details
    """
    import platform
    import sys
    
    return {
        'os': platform.system(),
        'os_release': platform.release(),
        'os_version': platform.version(),
        'architecture': platform.machine(),
        'processor': platform.processor(),
        'python_version': sys.version,
        'python_implementation': platform.python_implementation(),
    }

# Create ASCII progress bar
def create_progress_bar(
    current: int,
    total: int,
    width: int = 50,
    prefix: str = "",
    suffix: str = ""
) -> str:
    """
    Create ASCII progress bar
    
    Args:
        current: Current progress value
        total: Total value
        width: Width of progress bar in characters
        prefix: Text before bar
        suffix: Text after bar
        
    Returns:
        Formatted progress bar string
        
    Examples:
        >>> create_progress_bar(50, 100, width=20, prefix="Progress:")
        'Progress: [==========          ] 50%'
    """
    if total == 0:
        percent = 0
    else:
        percent = int((current / total) * 100)
    
    filled = int((current / total) * width) if total > 0 else 0
    bar = '=' * filled + ' ' * (width - filled)
    
    return f"{prefix} [{bar}] {percent}% {suffix}".strip()