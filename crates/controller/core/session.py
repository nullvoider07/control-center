"""Session management with VM shutdown detection"""

import time
from typing import Optional, List
from datetime import datetime
from enum import Enum
import logging

# Set up logging
logger = logging.getLogger(__name__)

# Session management with VM shutdown detection
class ConnectionState(Enum):
    """Connection state enumeration"""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    VM_SHUTDOWN = "vm_shutdown"
    FAILED = "failed"

# Session event for tracking
class SessionEvent:
    """Session event for tracking"""
    def __init__(self, event_type: str, message: str, timestamp: Optional[float] = None):
        self.event_type = event_type
        self.message = message
        self.timestamp = timestamp or time.time()
    
    # Convert event to dictionary for logging/export
    def to_dict(self):
        return {
            'event_type': self.event_type,
            'message': self.message,
            'timestamp': self.timestamp,
            'time': datetime.fromtimestamp(self.timestamp).isoformat(),
        }

# Session class with enhanced features
class Session:
    """Session management with VM shutdown detection"""
    
    def __init__(
        self,
        host: str,
        port: int,
        user_id: str,
        os_type: str,
        os_version: str
    ):
        self.host = host
        self.port = port
        self.user_id = user_id
        self.os_type = os_type
        self.os_version = os_version
        
        # Timestamps
        self.started_at = datetime.now()
        self.last_activity = time.time()
        self.last_health_check = time.time()
        
        # Connection state
        self.state = ConnectionState.CONNECTED
        self.active = True
        
        # Reconnection tracking
        self.reconnection_attempts = 0
        self.max_reconnection_attempts = 5
        self.last_reconnection_attempt = 0
        
        # Health tracking
        self.health_check_failures = 0
        self.max_health_failures = 3
        
        # Events log
        self.events: List[SessionEvent] = []
        self.max_events = 100
        
        self._log_event("session_start", f"Session started for user {user_id}")
    
    # Internal method to log session events
    def _log_event(self, event_type: str, message: str):
        """Log session event"""
        event = SessionEvent(event_type, message)
        self.events.append(event)
        
        if len(self.events) > self.max_events:
            self.events = self.events[-self.max_events:]
        
        logger.info(f"Session event: {event_type} - {message}")
    
    # Update activity timestamp
    def update_activity(self):
        """Update last activity timestamp"""
        self.last_activity = time.time()
        self.health_check_failures = 0
        
        if self.state == ConnectionState.RECONNECTING:
            self.state = ConnectionState.CONNECTED
    
    # End session
    def end(self, reason: str = "Normal disconnect"):
        """End the session"""
        self.active = False
        self.state = ConnectionState.DISCONNECTED
        self._log_event("session_end", reason)
        logger.info(f"Session ended: {reason} (duration: {self.get_duration():.2f}s)")
    
    # Mark VM shutdown
    def mark_vm_shutdown(self):
        """Mark session as VM/container shutdown"""
        self.active = False
        self.state = ConnectionState.VM_SHUTDOWN
        self._log_event("vm_shutdown", "VM/Container is no longer accessible")
        logger.error(f"VM/Container shutdown detected for session with {self.host}:{self.port}")
    
    # Check if session is active
    def is_active(self) -> bool:
        """Check if session is active"""
        return self.active and self.state not in [
            ConnectionState.DISCONNECTED,
            ConnectionState.VM_SHUTDOWN,
            ConnectionState.FAILED
        ]
    
    # Check if VM was shut down
    def is_vm_shutdown(self) -> bool:
        """Check if VM was shut down"""
        return self.state == ConnectionState.VM_SHUTDOWN
    
    # Record health check failure
    def record_health_check_failure(self) -> bool:
        """
        Record health check failure
        
        Returns:
            True if max failures reached (VM likely shutdown)
        """
        self.health_check_failures += 1
        self.last_health_check = time.time()
        
        logger.warning(
            f"Health check failure {self.health_check_failures}/{self.max_health_failures} "
            f"for {self.host}:{self.port}"
        )
        
        self._log_event(
            "health_check_failure",
            f"Health check failed ({self.health_check_failures}/{self.max_health_failures})"
        )
        
        if self.health_check_failures >= self.max_health_failures:
            self.mark_vm_shutdown()
            return True
        
        return False
    
    # Record successful health check
    def record_health_check_success(self):
        """Record successful health check"""
        if self.health_check_failures > 0:
            logger.info(f"Health check recovered after {self.health_check_failures} failures")
        
        self.health_check_failures = 0
        self.last_health_check = time.time()
    
    # Check if should attempt reconnection
    def should_attempt_reconnection(self) -> bool:
        """Check if should attempt reconnection"""
        if self.state == ConnectionState.VM_SHUTDOWN:
            return False
        
        if self.reconnection_attempts >= self.max_reconnection_attempts:
            return False
        
        if time.time() - self.last_reconnection_attempt < 5:
            return False
        
        return True
    
    # Record reconnection attempt
    def record_reconnection_attempt(self):
        """Record reconnection attempt"""
        self.reconnection_attempts += 1
        self.last_reconnection_attempt = time.time()
        self.state = ConnectionState.RECONNECTING
        self._log_event("reconnection_attempt", f"Attempt {self.reconnection_attempts}/{self.max_reconnection_attempts}")
    
    # Record successful reconnection
    def record_reconnection_success(self):
        """Record successful reconnection"""
        self.reconnection_attempts = 0
        self.state = ConnectionState.CONNECTED
        self.health_check_failures = 0
        self._log_event("reconnection_success", "Reconnected successfully")
    
    # Get session duration
    def get_duration(self) -> float:
        """Get session duration in seconds"""
        return (datetime.now() - self.started_at).total_seconds()
    
    # Get idle time
    def get_idle_time(self) -> float:
        """Get idle time since last activity"""
        return time.time() - self.last_activity
    
    # Convert session to dictionary for export/logging
    def to_dict(self):
        """Convert session to dictionary"""
        return {
            'host': self.host,
            'port': self.port,
            'user_id': self.user_id,
            'os_type': self.os_type,
            'os_version': self.os_version,
            'started_at': self.started_at.isoformat(),
            'duration_seconds': self.get_duration(),
            'idle_seconds': self.get_idle_time(),
            'active': self.active,
            'state': self.state.value,
            'reconnection_attempts': self.reconnection_attempts,
            'health_check_failures': self.health_check_failures,
            'vm_shutdown': self.is_vm_shutdown(),
            'events': [e.to_dict() for e in self.events[-20:]],
        }