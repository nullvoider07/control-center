"""gRPC client with token-based authentication"""

import grpc
from typing import Optional, Dict, Iterator, Any
from uuid import uuid4
import time
import logging
import sys
import os

# Import error classes
from .exceptions import (
    GRPCError,
    ConnectionError,
    AuthenticationError,
    RateLimitError,
    TimeoutError,
)

def _import_protobuf():
    """Import protobuf modules with fallback for PyInstaller bundles."""
    try:
        # Try package import first (development/normal install)
        from controller.integrations.proto import control_center_pb2
        from controller.integrations.proto import control_center_pb2_grpc
        return control_center_pb2, control_center_pb2_grpc
    except ImportError:
        # Fallback for PyInstaller bundle or direct import
        if getattr(sys, 'frozen', False):
            # Running in PyInstaller bundle
            bundle_dir = sys._MEIPASS # type: ignore
            proto_path = os.path.join(bundle_dir, 'controller', 'integrations', 'proto')
        else:
            # Running in development, try relative to this file
            proto_path = os.path.join(os.path.dirname(__file__), 'proto')
        
        if proto_path not in sys.path:
            sys.path.insert(0, proto_path)
        
        import control_center_pb2 # type: ignore
        import control_center_pb2_grpc # type: ignore
        
        return control_center_pb2, control_center_pb2_grpc

# Import protobuf modules
control_center_pb2, control_center_pb2_grpc = _import_protobuf()

logger = logging.getLogger(__name__)

# gRPC client with token-based authentication
class GRPCClient:
    """gRPC client with token-based authentication"""
    
    def __init__(
        self, 
        host: str, 
        port: int = 50051,
        timeout: int = 30,
        use_ssl: bool = False
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.use_ssl = use_ssl
        
        self.channel: Optional[grpc.Channel] = None
        self.stub: Optional[Any] = None  # Type: ControlServiceStub
        self.token: Optional[str] = None
        self.agent_info: Optional[Dict] = None
        
        # Connection state
        self._connected = False
        self._last_activity = 0
        
        logger.info(f"Initialized gRPC client for {host}:{port}")
    
    # Private method to create gRPC channel with proper configuration
    def _create_channel(self) -> grpc.Channel:
        """Create gRPC channel with proper configuration"""
        target = f'{self.host}:{self.port}'
        
        # Channel options
        options = [
            ('grpc.max_receive_message_length', 100 * 1024 * 1024),  # 100MB
            ('grpc.max_send_message_length', 100 * 1024 * 1024),
            ('grpc.keepalive_time_ms', 30000),  # 30 seconds
            ('grpc.keepalive_timeout_ms', 10000),  # 10 seconds
            ('grpc.http2.max_pings_without_data', 0),
            ('grpc.keepalive_permit_without_calls', 1),
        ]
        
        if self.use_ssl:
            logger.info("Creating secure channel (SSL)")
            credentials = grpc.ssl_channel_credentials()
            return grpc.secure_channel(target, credentials, options=options)
        else:
            logger.info("Creating insecure channel")
            return grpc.insecure_channel(target, options=options)
    
    # Public method to set authentication token
    def set_token(self, token: str):
        """
        Set API token for authentication
        
        Args:
            token: JWT or API token for authentication
        """
        self.token = token
        logger.debug("API token set")
    
    # Private method to get gRPC metadata with authentication token
    def _get_metadata(self) -> list:
        """
        Get gRPC metadata with authentication token
        
        Returns:
            List of metadata tuples for gRPC call
        """
        if not self.token:
            raise AuthenticationError(message="No token set. Call set_token() first.")
        
        # Add token as Authorization header (Bearer scheme)
        return [
            ('authorization', f'Bearer {self.token}')
        ]
    
    # Public method to connect to server and validate connection
    def connect(self) -> bool:
        """
        Connect to server and validate connection
        
        Returns:
            True if connection successful
            
        Raises:
            ConnectionError: If connection fails
            AuthenticationError: If token validation fails
        """
        try:
            logger.info(f"Connecting to {self.host}:{self.port}...")
            
            if not self.token:
                raise AuthenticationError(
                    message="No token set. Use set_token() before connecting."
                )
            
            # Create channel
            self.channel = self._create_channel()
            self.stub = control_center_pb2_grpc.ControlServiceStub(self.channel)
            self.agent_info = None
            
            # Test connection with timeout
            try:
                grpc.channel_ready_future(self.channel).result(timeout=self.timeout)
            except grpc.FutureTimeoutError:
                raise ConnectionError(
                    message="Connection timeout",
                    host=self.host,
                    port=self.port,
                    timeout=self.timeout,
                )
            
            # Validate token by getting agent info
            try:
                agent_info = self.get_agent_info()
                if not agent_info:
                    raise AuthenticationError(message="Failed to authenticate with server")
            except grpc.RpcError as e:
                if e.code() == grpc.StatusCode.UNAUTHENTICATED:
                    raise AuthenticationError.invalid_token(
                        reason=e.details() or "Invalid or expired token"
                    )
                raise
            
            self._connected = True
            self._last_activity = time.time()
            
            logger.info("Connection and authentication successful")
            return True
            
        except grpc.RpcError as e:
            logger.error(f"gRPC error during connection: {e.code()}: {e.details() if hasattr(e, 'details') else e}")
            
            if e.code() == grpc.StatusCode.UNAVAILABLE:
                raise ConnectionError.from_grpc_error(
                    grpc_error=e,
                    host=self.host,
                    port=self.port,
                    timeout=self.timeout,
                )
            elif e.code() == grpc.StatusCode.UNAUTHENTICATED:
                raise AuthenticationError.invalid_token(
                    reason=e.details() or "Authentication failed"
                )
            else:
                raise ConnectionError(
                    message="Connection failed",
                    details=e.details() if hasattr(e, 'details') else str(e),
                    host=self.host,
                    port=self.port,
                    grpc_code=e.code() if hasattr(e, 'code') else None,
                )
                
        except (ConnectionError, AuthenticationError):
            # Re-raise our custom errors
            raise
        except Exception as e:
            logger.error(f"Unexpected error during connection: {e}")
            raise ConnectionError(
                message="Connection failed",
                details=str(e),
                host=self.host,
                port=self.port,
            )
    
    # Public method to disconnect from server and clean up resources
    def disconnect(self):
        """Disconnect from server and clean up resources"""
        logger.info("Disconnecting from server...")
        
        if self.channel:
            try:
                self.channel.close()
            except Exception as e:
                logger.warning(f"Error closing channel: {e}")
            
        self.channel = None
        self.stub = None
        self.agent_info = None
        self._connected = False
        
        logger.info("Disconnected")
    
    # Public method to check if client is connected and token is valid
    def is_connected(self) -> bool:
        """Check if client is connected"""
        if not self._connected or not self.token:
            return False
        
        # Check if connection is still alive
        try:
            if self.channel:
                state = self.channel.get_state(try_to_connect=True) # type: ignore[attr-defined]
                return state == grpc.ChannelConnectivity.READY
        except:
            return False
        
        return True
    
    # Public method to get agent information (OS, version, capabilities)
    def get_agent_info(self) -> Optional[Dict]:
        """Get agent information (OS, version, capabilities)"""
        if self.agent_info:
            return self.agent_info
        
        if not self.token:
            raise AuthenticationError(message="Not authenticated")
        
        if not self.stub:
            raise ConnectionError(
                message="Not connected to server.",
                host=self.host,
                port=self.port,
            )
        
        try:
            request = control_center_pb2.AgentInfoRequest()
            
            # Call with authentication metadata
            response = self.stub.GetAgentInfo(
                request,
                metadata=self._get_metadata(),
                timeout=self.timeout
            )
            
            self.agent_info = {
                'os_type': self._map_os_enum(response.os),
                'os_version': response.os_version,
                'capabilities': list(response.capabilities),
                'agent_version': response.agent_version,
            }
            
            self._last_activity = time.time()
            return self.agent_info
            
        except grpc.RpcError as e:
            logger.error(f"Failed to get agent info: {e.code()}: {e.details() if hasattr(e, 'details') else e}")
            
            if e.code() == grpc.StatusCode.UNAUTHENTICATED:
                self._connected = False
                raise AuthenticationError.invalid_token(
                    reason=e.details() or "Token expired or invalid"
                )
            elif e.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
                raise RateLimitError.from_grpc_error(e)
            
            return None
    
    # Public method to execute a single command and return result
    def execute_command(self, command: str) -> Dict:
        """
        Execute single command
        
        Args:
            command: Command to execute
            
        Returns:
            Dict with keys: success, message, execution_time_ms, mouse_x, mouse_y, position_captured
            
        Raises:
            AuthenticationError: If token invalid/expired
            RateLimitError: If rate limit exceeded
            ConnectionError: If connection failed
        """
        if not self.token:
            raise AuthenticationError(message="Not authenticated")
        
        request_id = str(uuid4())
        
        if not self.stub:
            raise ConnectionError(
                message="Not connected to server.",
                host=self.host,
                port=self.port,
            )

        try:
            request = control_center_pb2.CommandRequest(
                id=request_id,
                command=command
            )
            
            # Call with authentication metadata
            response = self.stub.ExecuteCommand(
                request,
                metadata=self._get_metadata(),
                timeout=self.timeout
            )
            
            self._last_activity = time.time()
            
            # ✅ Extract position data from response
            result = {
                'success': response.success,
                'message': response.message,
                'execution_time_ms': response.execution_time_ms,
                'mouse_x': response.mouse_x if response.HasField('mouse_x') else None,
                'mouse_y': response.mouse_y if response.HasField('mouse_y') else None,
                'position_captured': response.position_captured if response.HasField('position_captured') else False,
            }
            
            return result
            
        except grpc.RpcError as e:
            logger.error(f"Command execution failed: {e.code()}: {e.details() if hasattr(e, 'details') else e}")
            
            if e.code() == grpc.StatusCode.UNAUTHENTICATED:
                self._connected = False
                raise AuthenticationError.invalid_token(
                    reason=e.details() or "Token expired"
                )
            elif e.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
                raise RateLimitError.from_grpc_error(e)
            elif e.code() == grpc.StatusCode.INVALID_ARGUMENT:
                return {
                    'success': False,
                    'message': f"Invalid command: {e.details() if hasattr(e, 'details') else e}",
                    'execution_time_ms': 0,
                }
            elif e.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
                raise TimeoutError(
                    message="Command execution timed out",
                    timeout=self.timeout,
                    operation="execute_command",
                    request_id=request_id,
                )
            
            return {
                'success': False,
                'message': f"gRPC error: {e.details() if hasattr(e, 'details') else e}",
                'execution_time_ms': 0,
            }
    
    # Public method to execute a batch of commands via streaming
    def execute_batch(self, commands: Iterator[str]) -> Iterator[Dict]:
        """
        Execute batch of commands via streaming
        
        Args:
            commands: Iterator of commands to execute
            
        Yields:
            Dict with execution results for each command
        """
        if not self.token:
            raise AuthenticationError(message="Not authenticated")
        
        def request_generator():
            for cmd in commands:
                yield control_center_pb2.CommandRequest(
                    id=str(uuid4()),
                    command=cmd
                )
        
        if not self.stub:
            raise ConnectionError(
                message="Not connected to server.",
                host=self.host,
                port=self.port,
            )

        try:
            # Call with authentication metadata
            responses = self.stub.ExecuteCommandStream(
                request_generator(),
                metadata=self._get_metadata(),
                timeout=self.timeout * 10  # Longer timeout for batch
            )
            
            for response in responses:
                self._last_activity = time.time()
                yield {
                    'success': response.success,
                    'message': response.message,
                    'execution_time_ms': response.execution_time_ms,
                    'mouse_x': response.mouse_x if response.HasField('mouse_x') else None,
                    'mouse_y': response.mouse_y if response.HasField('mouse_y') else None,
                    'position_captured': response.position_captured if response.HasField('position_captured') else False,
                }
                
        except grpc.RpcError as e:
            logger.error(f"Batch execution failed: {e.code()}: {e.details() if hasattr(e, 'details') else e}")
            
            if e.code() == grpc.StatusCode.UNAUTHENTICATED:
                self._connected = False
                raise AuthenticationError.invalid_token(
                    reason=e.details() or "Token expired"
                )
            elif e.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
                raise RateLimitError.from_grpc_error(e)
            
            yield {
                'success': False,
                'message': f"gRPC error: {e.details() if hasattr(e, 'details') else e}",
                'execution_time_ms': 0,
            }
    
    # Public method to get timestamp of last activity (for idle timeout management)
    def get_last_activity(self) -> float:
        """Get timestamp of last activity"""
        return self._last_activity
    
    # Private method to map protobuf OS enum to string
    def _map_os_enum(self, os_enum: int) -> str:
        """Map protobuf OS enum to string"""
        os_map = {
            control_center_pb2.WINDOWS: "WINDOWS",
            control_center_pb2.MACOS: "MACOS",
            control_center_pb2.LINUX: "LINUX",
        }
        return os_map.get(os_enum, "UNKNOWN")
    
    # Context manager support for automatic connection management
    def __enter__(self):
        """Context manager entry"""
        return self
    
    # Context manager exit to ensure proper disconnection
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.disconnect()
        return False