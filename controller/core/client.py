"""Client functionality with token-based authentication"""

from typing import Optional, Dict
import logging
from ..integrations.gRPC import GRPCClient
from ..integrations.exceptions import AuthenticationError, ConnectionError, VMShutdownError
from .session import Session
from .metrics import MetricsCollector

# Set up logging
logger = logging.getLogger(__name__)

# Client class for managing connections and sessions with token-based authentication
class Client:
    """
    Client for managing connections and sessions
    
    Features:
    - Token-based authentication (OAuth2/JWT)
    - Automatic session management
    - Connection health monitoring
    - VM shutdown detection
    - Metrics collection
    """
    
    def __init__(
        self,
        host: str,
        port: int = 50051,
        timeout: int = 30,
        use_ssl: bool = False
    ):
        """
        Initialize client
        
        Args:
            host: Server hostname or IP
            port: gRPC server port
            timeout: Connection timeout in seconds
            use_ssl: Whether to use SSL/TLS
        """
        self.host = host
        self.port = port
        self.timeout = timeout
        self.use_ssl = use_ssl
        
        # Initialize gRPC client
        self.grpc_client = GRPCClient(
            host=host,
            port=port,
            timeout=timeout,
            use_ssl=use_ssl
        )
        
        # State
        self.session: Optional[Session] = None
        self.metrics = MetricsCollector()
        self._token: Optional[str] = None
    
    # Public API methods
    def connect(self, token: str) -> bool:
        """
        Connect to server with API token
        
        Args:
            token: JWT/OAuth2 API token
            
        Returns:
            True if connection successful
            
        Raises:
            AuthenticationError: If token is invalid
            ConnectionError: If connection fails
            VMShutdownError: If VM/Container is shut down
        """
        try:
            # Store token
            self._token = token
            
            # Set token in gRPC client
            self.grpc_client.set_token(token)
            
            # Connect to server
            if not self.grpc_client.connect():
                logger.error("Connection failed")
                return False
            
            # Validate token by getting agent info
            agent_info = self.grpc_client.get_agent_info()
            if not agent_info:
                logger.error("Failed to get agent info - token may be invalid")
                raise AuthenticationError("Failed to authenticate with provided token")
            
            # Extract user_id from JWT token (decode without verification for user_id)
            user_id = self._extract_user_id_from_token(token)
            
            # Create session
            self.session = Session(
                host=self.host,
                port=self.port,
                user_id=user_id,
                os_type=agent_info.get('os_type', 'UNKNOWN'),
                os_version=agent_info.get('os_version', 'UNKNOWN'),
            )
            
            logger.info(f"Connected successfully to {self.host}:{self.port}")
            return True
            
        except AuthenticationError:
            logger.error("Authentication failed")
            raise
        except ConnectionError:
            logger.error("Connection error")
            raise
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            return False
    
    # Helper method to extract user_id from JWT token without full validation
    def _extract_user_id_from_token(self, token: str) -> str:
        """
        Extract user_id from JWT token (without full validation)
        
        Args:
            token: JWT token string
            
        Returns:
            User ID from token claims, or 'unknown' if extraction fails
        """
        try:
            import json
            import base64
            
            # Split token
            parts = token.split('.')
            if len(parts) != 3:
                return 'unknown'
            
            # Decode payload (add padding if needed)
            payload = parts[1]
            padding = 4 - len(payload) % 4
            if padding != 4:
                payload += '=' * padding
            
            # Decode base64
            decoded = base64.urlsafe_b64decode(payload)
            claims = json.loads(decoded)
            
            # Extract user_id (could be 'sub', 'user_id', 'uid', etc.)
            return claims.get('sub') or claims.get('user_id') or claims.get('uid') or 'unknown'
            
        except Exception as e:
            logger.warning(f"Failed to extract user_id from token: {e}")
            return 'unknown'
    
    # Additional methods for session management, health monitoring, and metrics
    def disconnect(self):
        """Disconnect from server and cleanup session"""
        try:
            if self.session:
                self.session.end(reason="User disconnected")
                self.session = None
            
            if self.grpc_client:
                self.grpc_client.disconnect()
            
            self._token = None
            logger.info("Disconnected successfully")
            
        except Exception as e:
            logger.error(f"Error during disconnect: {e}")
    
    # Health monitoring and reconnection logic
    def is_connected(self) -> bool:
        """
        Check if client is connected
        
        Returns:
            True if connected and session is active
        """
        if not self.grpc_client:
            return False
        
        if not self.grpc_client.is_connected():
            # Check if VM shutdown
            if self.session and not self.session.is_vm_shutdown():
                # Record health check failure
                vm_down = self.session.record_health_check_failure()
                if vm_down:
                    logger.error("VM/Container appears to be shut down")
            return False
        
        # Connection is good - update health
        if self.session:
            self.session.record_health_check_success()
        
        return self.session is not None and self.session.is_active()
    
    # Reconnection logic
    def refresh_connection(self) -> bool:
        """
        Attempt to refresh/reconnect with existing token
        
        Returns:
            True if reconnection successful
        """
        if not self._token:
            logger.error("No token available for reconnection")
            return False
        
        if self.session and self.session.is_vm_shutdown():
            logger.error("Cannot reconnect - VM is shut down")
            return False
        
        try:
            # Record reconnection attempt
            if self.session:
                self.session.record_reconnection_attempt()
            
            # Try to reconnect
            if self.grpc_client.connect():
                if self.session:
                    self.session.record_reconnection_success()
                logger.info("Reconnection successful")
                return True
            else:
                logger.error("Reconnection failed")
                return False
                
        except Exception as e:
            logger.error(f"Reconnection error: {e}")
            return False
    
    # Session and metrics accessors
    def get_session_info(self) -> Optional[Dict]:
        """
        Get current session information
        
        Returns:
            Session info dict or None if no active session
        """
        if self.session:
            return self.session.to_dict()
        return None
    
    # Metrics collection and reporting
    def get_metrics(self) -> Dict:
        """
        Get current metrics
        
        Returns:
            Metrics dict with performance statistics
        """
        return self.metrics.get_stats()
    
    # Activity tracking
    def update_activity(self):
        """Update session activity timestamp"""
        if self.session:
            self.session.update_activity()
    
    # Context manager support for automatic cleanup
    def __enter__(self):
        """Context manager entry"""
        return self
    
    # Context manager support for automatic cleanup
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensure cleanup"""
        self.disconnect()
        return False