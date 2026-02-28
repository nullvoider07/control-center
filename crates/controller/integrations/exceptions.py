"""Error classes for gRPC client - TOKEN-BASED"""

from typing import Optional, Dict, Any
from datetime import datetime
import grpc

# Base exception for gRPC client errors with context and metadata
class GRPCError(Exception):
    """
    Base exception for gRPC client errors with context and metadata
    
    Attributes:
        message: Human-readable error message
        details: Additional error details
        timestamp: When the error occurred
        request_id: Associated request ID if available
        grpc_code: gRPC status code if applicable
        is_retryable: Whether this error can be retried
    """
    
    def __init__(
        self,
        message: str,
        details: Optional[str] = None,
        request_id: Optional[str] = None,
        grpc_code: Optional[grpc.StatusCode] = None,
        is_retryable: bool = False,
        **kwargs
    ):
        self.message = message
        self.details = details
        self.timestamp = datetime.utcnow()
        self.request_id = request_id
        self.grpc_code = grpc_code
        self.is_retryable = is_retryable
        self.context = kwargs
        
        # Build full error message
        full_message = self._build_message()
        super().__init__(full_message)
    
    # Build comprehensive error message with all available information
    def _build_message(self) -> str:
        """Build comprehensive error message"""
        parts = [self.message]
        
        if self.details:
            parts.append(f"Details: {self.details}")
        
        if self.request_id:
            parts.append(f"Request ID: {self.request_id}")
        
        if self.grpc_code:
            parts.append(f"gRPC Code: {self.grpc_code.name}")
        
        return " | ".join(parts)
    
    # Convert error to dictionary for logging/serialization
    def to_dict(self) -> Dict[str, Any]:
        """Convert error to dictionary for logging/serialization"""
        return {
            'error_type': self.__class__.__name__,
            'message': self.message,
            'details': self.details,
            'timestamp': self.timestamp.isoformat(),
            'request_id': self.request_id,
            'grpc_code': self.grpc_code.name if self.grpc_code else None,
            'is_retryable': self.is_retryable,
            'context': self.context,
        }
    
    # Developer-friendly representation
    def __repr__(self) -> str:
        """Developer-friendly representation"""
        return (
            f"{self.__class__.__name__}("
            f"message={self.message!r}, "
            f"grpc_code={self.grpc_code.name if self.grpc_code else None}, "
            f"is_retryable={self.is_retryable}"
            f")"
        )

# Specific error types for common gRPC error scenarios
class ConnectionError(GRPCError):
    """
    Connection-related errors with retry logic and diagnostics
    
    Raised when:
    - Server is unreachable
    - Connection timeout
    - Network failures
    - Channel closure
    """
    
    def __init__(
        self,
        message: str,
        host: Optional[str] = None,
        port: Optional[int] = None,
        timeout: Optional[int] = None,
        attempts: int = 1,
        **kwargs
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.attempts = attempts
        
        # Connection errors are generally retryable
        super().__init__(
            message=message,
            is_retryable=True,
            **kwargs
        )
    
    # Build connection-specific error message with diagnostics
    def _build_message(self) -> str:
        """Build connection-specific error message"""
        parts = [self.message]
        
        if self.host and self.port:
            parts.append(f"Target: {self.host}:{self.port}")
        
        if self.timeout:
            parts.append(f"Timeout: {self.timeout}s")
        
        if self.attempts > 1:
            parts.append(f"Failed after {self.attempts} attempts")
        
        if self.details:
            parts.append(f"Details: {self.details}")
        
        return " | ".join(parts)
    
    # Factory method to create ConnectionError from gRPC error with context
    @classmethod
    def from_grpc_error(
        cls,
        grpc_error: grpc.RpcError,
        host: str,
        port: int,
        timeout: int,
        attempts: int = 1
    ) -> 'ConnectionError':
        """Create ConnectionError from gRPC error"""
        return cls(
            message="Connection to server failed",
            details=grpc_error.details() if hasattr(grpc_error, 'details') else str(grpc_error),
            host=host,
            port=port,
            timeout=timeout,
            attempts=attempts,
            grpc_code=grpc_error.code() if hasattr(grpc_error, 'code') else None,
        )
    
    # Suggest action to user based on error details
    def suggest_action(self) -> str:
        """Suggest action to user based on error details"""
        if self.timeout and self.attempts > 1:
            return (
                f"Connection failed after {self.attempts} attempts. "
                f"Please check:\n"
                f"  1. Server is running at {self.host}:{self.port}\n"
                f"  2. VM/Container is powered on\n"
                f"  3. Network connectivity\n"
                f"  4. Firewall settings\n"
                f"  5. Increase timeout (current: {self.timeout}s)"
            )
        
        if self.grpc_code == grpc.StatusCode.UNAVAILABLE:
            return (
                "Server is unavailable. Please check:\n"
                "  1. Server/Agent is running\n"
                "  2. VM/Container is powered on and accessible\n"
                "  3. Correct host and port\n"
                "  4. No firewall blocking connection"
            )
        
        return "Check server connectivity and try again"

# Authentication errors with security context and retry logic
class AuthenticationError(GRPCError):
    """
    Authentication failures with security context
    
    Raised when:
    - Invalid credentials
    - Session expired
    - Token invalid/malformed
    - Insufficient permissions
    """
    
    def __init__(
        self,
        message: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        token_expired: bool = False,
        **kwargs
    ):
        self.user_id = user_id
        self.session_id = session_id
        self.token_expired = token_expired
        
        # Auth errors are NOT retryable (unless token expired)
        super().__init__(
            message=message,
            is_retryable=token_expired,
            **kwargs
        )
    
    # Build auth-specific error message with security context
    def _build_message(self) -> str:
        """Build auth-specific error message"""
        parts = [self.message]
        
        if self.user_id:
            # Don't log full user_id for security, just hint
            masked_user = f"{self.user_id[:2]}***" if len(self.user_id) > 2 else "***"
            parts.append(f"User: {masked_user}")
        
        if self.token_expired:
            parts.append("Reason: Session expired")
        
        if self.details:
            parts.append(f"Details: {self.details}")
        
        return " | ".join(parts)
    
    # Factory methods for common auth error scenarios
    @classmethod
    def invalid_credentials(cls, user_id: str) -> 'AuthenticationError':
        """Create error for invalid credentials"""
        return cls(
            message="Authentication failed: Invalid credentials",
            user_id=user_id,
        )
    
    # Factory method for session expiration with retry logic
    @classmethod
    def session_expired(cls, session_id: str, user_id: Optional[str] = None) -> 'AuthenticationError':
        """Create error for expired session"""
        return cls(
            message="Session has expired",
            session_id=session_id,
            user_id=user_id,
            token_expired=True,
        )
    
    # Factory method for invalid token with security context
    @classmethod
    def invalid_token(cls, reason: str, user_id: Optional[str] = None) -> 'AuthenticationError':
        """Create error for invalid token"""
        return cls(
            message="Invalid session token",
            details=reason,
            user_id=user_id,
        )
    
    # Suggest action to user based on error details
    def suggest_action(self) -> str:
        """Suggest action to user"""
        if self.token_expired:
            return "Token expired. Please reconnect with a valid token."
        
        return "Authentication failed. Please check your API token."

# Rate limit errors with retry timing and quota context
class RateLimitError(GRPCError):
    """
    Rate limit exceeded with retry timing
    
    Raised when:
    - Too many requests in time window
    - Server throttling
    - Quota exceeded
    """
    
    def __init__(
        self,
        message: str,
        limit: Optional[int] = None,
        window_seconds: Optional[int] = None,
        retry_after: Optional[int] = None,
        current_count: Optional[int] = None,
        **kwargs
    ):
        self.limit = limit
        self.window_seconds = window_seconds
        self.retry_after = retry_after
        self.current_count = current_count
        
        # Rate limit errors are retryable after waiting
        super().__init__(
            message=message,
            is_retryable=True,
            **kwargs
        )
    
    # Build rate-limit-specific error message with quota context
    def _build_message(self) -> str:
        """Build rate-limit-specific error message"""
        parts = [self.message]
        
        if self.limit and self.window_seconds:
            parts.append(f"Limit: {self.limit} requests per {self.window_seconds}s")
        
        if self.current_count and self.limit:
            parts.append(f"Current: {self.current_count}/{self.limit}")
        
        if self.retry_after:
            parts.append(f"Retry after: {self.retry_after}s")
        
        if self.details:
            parts.append(f"Details: {self.details}")
        
        return " | ".join(parts)
    
    # Factory method to create RateLimitError from gRPC error with retry timing
    @classmethod
    def from_grpc_error(
        cls,
        grpc_error: grpc.RpcError,
        limit: int = 100,
        window_seconds: int = 60
    ) -> 'RateLimitError':
        """Create RateLimitError from gRPC error"""
        # Try to extract retry-after from metadata
        retry_after = None
        if hasattr(grpc_error, 'trailing_metadata'):
            metadata = {}
            for metadatum in grpc_error.trailing_metadata():
                if isinstance(metadatum, (list, tuple)) and len(metadatum) == 2:
                    key, value = metadatum
                elif hasattr(metadatum, 'key') and hasattr(metadatum, 'value'):
                    key, value = metadatum.key, metadatum.value
                else:
                    continue

                if isinstance(key, bytes):
                    key = key.decode('utf-8', errors='ignore')
                metadata[key] = value
                
            retry_after_val = metadata.get('retry-after')
            if retry_after_val:
                try:
                    retry_after = int(retry_after_val)
                except ValueError:
                    retry_after = None
        
        return cls(
            message="Rate limit exceeded",
            details=grpc_error.details() if hasattr(grpc_error, 'details') else None,
            limit=limit,
            window_seconds=window_seconds,
            retry_after=retry_after or window_seconds,
            grpc_code=grpc.StatusCode.RESOURCE_EXHAUSTED,
        )
    
    # Get recommended wait time based on error details
    def get_wait_time(self) -> int:
        """Get recommended wait time in seconds"""
        if self.retry_after:
            return self.retry_after
        
        if self.window_seconds:
            return self.window_seconds
        
        # Default: wait 60 seconds
        return 60
    
    # Suggest action to user based on error details
    def suggest_action(self) -> str:
        """Suggest action to user"""
        wait_time = self.get_wait_time()
        return (
            f"Rate limit exceeded. Please wait {wait_time} seconds before retrying.\n"
            f"Limit: {self.limit or 'unknown'} requests per {self.window_seconds or 60}s"
        )

# Validation errors with field context and expected format
class ValidationError(GRPCError):
    """
    Input validation errors
    
    Raised when:
    - Invalid command format
    - Invalid coordinates
    - Invalid parameters
    """
    
    def __init__(
        self,
        message: str,
        field: Optional[str] = None,
        value: Optional[Any] = None,
        expected: Optional[str] = None,
        **kwargs
    ):
        self.field = field
        self.value = value
        self.expected = expected
        
        # Validation errors are NOT retryable (input must be fixed)
        super().__init__(
            message=message,
            is_retryable=False,
            **kwargs
        )
    
    # Build validation-specific error message with field context
    def _build_message(self) -> str:
        """Build validation-specific error message"""
        parts = [self.message]
        
        if self.field:
            parts.append(f"Field: {self.field}")
        
        if self.value is not None:
            # Truncate long values
            value_str = str(self.value)
            if len(value_str) > 50:
                value_str = value_str[:47] + "..."
            parts.append(f"Value: {value_str}")
        
        if self.expected:
            parts.append(f"Expected: {self.expected}")
        
        return " | ".join(parts)

# Timeout errors with operation context and retry logic
class TimeoutError(GRPCError):
    """
    Request timeout errors
    
    Raised when:
    - Command execution takes too long
    - Server doesn't respond in time
    """
    
    def __init__(
        self,
        message: str,
        timeout: int,
        operation: Optional[str] = None,
        **kwargs
    ):
        self.timeout = timeout
        self.operation = operation
        
        # Timeouts may be retryable
        super().__init__(
            message=message,
            is_retryable=True,
            **kwargs
        )
    
    # Build timeout-specific error message with operation context
    def _build_message(self) -> str:
        """Build timeout-specific error message"""
        parts = [self.message]
        
        if self.operation:
            parts.append(f"Operation: {self.operation}")
        
        parts.append(f"Timeout: {self.timeout}s")
        
        return " | ".join(parts)
    
    # Suggest action to user based on error details
    def suggest_action(self) -> str:
        """Suggest action to user"""
        return (
            f"Operation timed out after {self.timeout}s. "
            f"Consider:\n"
            f"  1. Increasing timeout value\n"
            f"  2. Checking server performance\n"
            f"  3. Retrying the operation"
        )

# VM shutdown errors with detection logic and user guidance
class VMShutdownError(ConnectionError):
    """
    VM/Container shutdown detected
    
    Raised when:
    - Multiple consecutive connection failures
    - Server becomes unavailable
    - Agent stops responding
    """
    
    def __init__(
        self,
        message: str = "VM/Container is no longer accessible",
        host: Optional[str] = None,
        port: Optional[int] = None,
        **kwargs
    ):
        super().__init__(
            message=message,
            host=host,
            port=port,
            is_retryable=False,
            **kwargs
        )
    
    def suggest_action(self) -> str:
        """Suggest action for VM shutdown"""
        return (
            "VM/Container has been shut down or is no longer accessible.\n"
            "Please check:\n"
            "  1. VM/Container is powered on\n"
            "  2. Agent service is running inside the container\n"
            "  3. Network connectivity to the VM\n"
            "  4. Restart VM/Container if needed\n"
            "  5. Reconnect after VM is available"
        )