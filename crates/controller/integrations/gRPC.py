"""gRPC client with token-based authentication"""

import grpc
from typing import Optional, Dict, Iterator, Any, List
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

# Dynamically import protobuf modules with fallback for PyInstaller bundles
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
            # Trust the server's CA (self-signed or private). CC_TLS_CA points at the
            # CA/server cert PEM; without it, system roots are used.
            ca_path = os.environ.get('CC_TLS_CA')
            root_certs = None
            if ca_path:
                try:
                    with open(ca_path, 'rb') as f:
                        root_certs = f.read()
                except OSError as e:
                    logger.warning(f"Could not read CC_TLS_CA '{ca_path}': {e}")
            # When connecting by IP but the cert has a DNS SAN, allow overriding the
            # name the client verifies against.
            server_name = os.environ.get('CC_TLS_SERVER_NAME')
            if server_name:
                options = options + [('grpc.ssl_target_name_override', server_name)]
            logger.info("Creating secure channel (TLS)")
            credentials = grpc.ssl_channel_credentials(root_certificates=root_certs)
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
        if not self._connected or not self.token or not self.stub or not self.channel:
            return False
        
        # Check if connection is still alive
        try:
            state = self.channel.get_state(try_to_connect=False)
            if state == grpc.ChannelConnectivity.SHUTDOWN:
                self.connected = False
                return False
            return True
        except:
            return self._connected
    
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
    def execute_command(
        self,
        argv: List[str],
        human_command: str,
    ) -> Dict:
        """
        Execute a single command.

        Args:
            argv: Structured argument vector executed directly by the agent (no shell).
            human_command: Human-readable command for recording (e.g. "type hi").

        Both are required: the server rejects a request without them. The legacy
        `CommandRequest.command` shell string is no longer accepted anywhere.

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
                argv=list(argv),
                human_command=human_command,
            )
            
            # Call with authentication metadata
            response = self.stub.ExecuteCommand(
                request,
                metadata=self._get_metadata(),
                timeout=self.timeout
            )
            
            self._last_activity = time.time()
            
            # Extract position data from response
            result = {
                'success': response.success,
                'message': response.message,
                'execution_time_ms': response.execution_time_ms,
                'mouse_x': response.mouse_x if response.HasField('mouse_x') else None,
                'mouse_y': response.mouse_y if response.HasField('mouse_y') else None,
                'position_captured': response.position_captured if response.HasField('position_captured') else False,
                # Agent-side ground truth: the executed argv and any mouse button
                # still held. Carried on the existing metadata map, so surfacing a
                # held button needs no new RPC and no new scope check.
                'metadata': dict(response.metadata),
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
                # Agent-side ground truth: the executed argv and any mouse button
                # still held. Carried on the existing metadata map, so surfacing a
                # held button needs no new RPC and no new scope check.
                'metadata': dict(response.metadata),
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
    
    # Monitoring RPCs (require a `monitor`-scoped token; Ping is the exception)
    def query_connections(
        self,
        server_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        network: Optional[str] = None,
    ) -> Optional[Dict]:
        """Query current connection metadata from the server.

        Requires a `monitor`-scoped token (set via set_token).

        Returns:
            Dict with keys: connections (list[dict]), total_count (int)
            Each connection dict: connection_id, server_id, agent_id,
            agent_hostname, agent_ip, server_ip, network, connected_at,
            last_heartbeat, commands_executed, state
        """
        if not self.stub:
            raise ConnectionError(message="Not connected to server.", host=self.host, port=self.port)
        try:
            request = control_center_pb2.ConnectionQuery(
                server_id=server_id or "",
                agent_id=agent_id or "",
                network=network or "",
            )
            response = self.stub.QueryConnections(request, metadata=self._get_metadata(), timeout=self.timeout)
            self._last_activity = time.time()
            return {
                'total_count': response.total_count,
                'connections': [self._connection_metadata_to_dict(c) for c in response.connections],
            }
        except grpc.RpcError as e:
            logger.error(f"QueryConnections failed: {e.code()}: {e.details() if hasattr(e, 'details') else e}")
            return None

    # Query server status (identity, uptime, connection state)
    def query_server_status(
        self,
        server_id: Optional[str] = None,
        network: Optional[str] = None,
    ) -> Optional[Dict]:
        """Query server status (identity, uptime, connection state).

        Requires a `monitor`-scoped token (set via set_token).

        Returns:
            Dict with keys: servers (list[dict]), total_count (int)
            Each server dict: identity (dict), status (dict),
            current_connection (dict|None), last_seen (int)
        """
        if not self.stub:
            raise ConnectionError(message="Not connected to server.", host=self.host, port=self.port)
        try:
            request = control_center_pb2.ServerStatusQuery(
                server_id=server_id or "",
                network=network or "",
            )
            response = self.stub.QueryServers(request, metadata=self._get_metadata(), timeout=self.timeout)
            self._last_activity = time.time()

            servers = []
            for srv in response.servers:
                ident = srv.identity
                stat = srv.status
                server_dict: Dict[str, Any] = {
                    'identity': {
                        'server_id': ident.server_id,
                        'hostname': ident.hostname,
                        'listen_address': ident.listen_address,
                        'version': ident.version,
                        'started_at': ident.started_at,
                        'network': ident.network,
                    },
                    'status': {
                        'accepting_connections': stat.accepting_connections,
                        'agent_connected': stat.agent_connected,
                        'total_commands_processed': stat.total_commands_processed,
                        'uptime_seconds': stat.uptime_seconds,
                    },
                    'current_connection': (
                        self._connection_metadata_to_dict(srv.current_connection)
                        if srv.HasField('current_connection') else None
                    ),
                    'last_seen': srv.last_seen,
                }
                servers.append(server_dict)

            return {'total_count': response.total_count, 'servers': servers}
        except grpc.RpcError as e:
            logger.error(f"QueryServers failed: {e.code()}: {e.details() if hasattr(e, 'details') else e}")
            return None

    # Query persistent server identity and metadata
    def get_server_identity(self) -> Optional[Dict]:
        """Get persistent server identity.

        Requires a `monitor`-scoped token (set via set_token).

        Returns:
            Dict with keys: server_id, hostname, listen_address, version,
            started_at (unix int), network
        """
        if not self.stub:
            raise ConnectionError(message="Not connected to server.", host=self.host, port=self.port)
        try:
            request = control_center_pb2.InfoRequest()
            response = self.stub.GetServerIdentity(request, metadata=self._get_metadata(), timeout=self.timeout)
            self._last_activity = time.time()
            return {
                'server_id': response.server_id,
                'hostname': response.hostname,
                'listen_address': response.listen_address,
                'version': response.version,
                'started_at': response.started_at,
                'network': response.network,
            }
        except grpc.RpcError as e:
            logger.error(f"GetServerIdentity failed: {e.code()}: {e.details() if hasattr(e, 'details') else e}")
            return None

    # Ping the server and measure round-trip time
    def ping(self) -> Optional[float]:
        """Ping the server and measure round-trip time.

        No authentication required.

        Returns:
            Round-trip time in milliseconds, or None if ping failed.
        """
        if not self.stub:
            raise ConnectionError(message="Not connected to server.", host=self.host, port=self.port)
        try:
            request = control_center_pb2.PingRequest()
            start = time.time()
            response = self.stub.Ping(request, timeout=self.timeout)
            rtt_ms = (time.time() - start) * 1000
            self._last_activity = time.time()
            return rtt_ms if response.alive else None
        except grpc.RpcError as e:
            logger.error(f"Ping failed: {e.code()}: {e.details() if hasattr(e, 'details') else e}")
            return None

    # Authenticated monitoring RPCs
    def get_metrics(self) -> Optional[Dict]:
        """Retrieve server Prometheus-format metrics.

        Requires a valid token with 'metrics' scope.

        Returns:
            Dict with keys: metrics (str, raw Prometheus text), timestamp (int)

        Raises:
            AuthenticationError: If token is missing, invalid, or lacks 'metrics' scope.
        """
        if not self.token:
            raise AuthenticationError(message="Not authenticated")
        if not self.stub:
            raise ConnectionError(message="Not connected to server.", host=self.host, port=self.port)
        try:
            request = control_center_pb2.MetricsRequest()
            response = self.stub.GetMetrics(
                request,
                metadata=self._get_metadata(),
                timeout=self.timeout,
            )
            self._last_activity = time.time()
            return {'metrics': response.metrics, 'timestamp': response.timestamp}
        except grpc.RpcError as e:
            logger.error(f"GetMetrics failed: {e.code()}: {e.details() if hasattr(e, 'details') else e}")
            if e.code() == grpc.StatusCode.PERMISSION_DENIED:
                raise AuthenticationError(
                    message="Token lacks 'metrics' scope. Regenerate with: generate_token <user> <hours> execute metrics"
                )
            if e.code() == grpc.StatusCode.UNAUTHENTICATED:
                raise AuthenticationError.invalid_token(reason=e.details() or "Token invalid or expired")
            return None

    # Agent management RPCs (require auth)
    def disconnect_agent(self, reason: str = "") -> Dict:
        """Send a graceful disconnect signal to the currently connected agent.

        The stream handler picks up the signal on its next heartbeat tick
        (within 30 s) and sends a DisconnectNotice to the agent.

        Requires a valid token.

        Returns:
            Dict with keys: success (bool), message (str),
            disconnected_connection_id (str)

        Raises:
            AuthenticationError: If token is missing or invalid.
        """
        if not self.token:
            raise AuthenticationError(message="Not authenticated")
        if not self.stub:
            raise ConnectionError(message="Not connected to server.", host=self.host, port=self.port)
        try:
            request = control_center_pb2.DisconnectAgentRequest(reason=reason)
            response = self.stub.DisconnectAgent(
                request,
                metadata=self._get_metadata(),
                timeout=self.timeout,
            )
            self._last_activity = time.time()
            return {
                'success': response.success,
                'message': response.message,
                'disconnected_connection_id': response.disconnected_connection_id,
            }
        except grpc.RpcError as e:
            logger.error(f"DisconnectAgent failed: {e.code()}: {e.details() if hasattr(e, 'details') else e}")
            if e.code() == grpc.StatusCode.UNAUTHENTICATED:
                raise AuthenticationError.invalid_token(reason=e.details() or "Token invalid or expired")
            return {
                'success': False,
                'message': f"gRPC error: {e.details() if hasattr(e, 'details') else e}",
                'disconnected_connection_id': '',
            }

    # Fetch historical agent connection records from the server registry
    def get_connection_history(self, limit: int = 50) -> Optional[List[Dict]]:
        """Fetch historical agent connection records from the server registry.

        Requires a `monitor`-scoped token (set via set_token).

        Args:
            limit: Maximum records to return (default 50, server-side max 500).

        Returns:
            List of dicts, each with: connection_id, agent_id, agent_hostname,
            agent_ip, os_type (int), os_version, capabilities (list[str]),
            server_ip, connected_at (unix int), disconnected_at (unix int|None),
            commands_executed (int), disconnect_reason (str|None)
        """
        if not self.stub:
            raise ConnectionError(message="Not connected to server.", host=self.host, port=self.port)
        try:
            request = control_center_pb2.ConnectionHistoryRequest(limit=limit)
            response = self.stub.GetConnectionHistory(request, metadata=self._get_metadata(), timeout=self.timeout)
            self._last_activity = time.time()
            records = []
            for h in response.connections:
                records.append({
                    'connection_id': h.connection_id,
                    'agent_id': h.agent_id,
                    'agent_hostname': h.agent_hostname,
                    'agent_ip': h.agent_ip,
                    'os_type': h.os_type,
                    'os_version': h.os_version,
                    'capabilities': list(h.capabilities),
                    'server_ip': h.server_ip,
                    'connected_at': h.connected_at,
                    'disconnected_at': h.disconnected_at if h.HasField('disconnected_at') else None,
                    'commands_executed': h.commands_executed,
                    'disconnect_reason': h.disconnect_reason if h.HasField('disconnect_reason') else None,
                })
            return records
        except grpc.RpcError as e:
            logger.error(f"GetConnectionHistory failed: {e.code()}: {e.details() if hasattr(e, 'details') else e}")
            return None

    # Real-time command event streaming   
    def watch_commands(self):
        """Stream live command events from the server (requires a `monitor` token).

        Yields dicts for each CommandEvent as they arrive:
            session_id, agent_id, agent_version, os_type,
            timestamp, raw_command, action_type, action_subtype,
            is_here_command, success, error_message, execution_time_ms,
            mouse_x, mouse_y, position_captured,
            is_heartbeat, agent_alive

        Heartbeat events fire every 5s when idle (is_heartbeat=True).
        Stream closes automatically when the agent disconnects.

        Raises:
            ConnectionError: if not connected to server.
        """
        if not self.stub:
            raise ConnectionError(message="Not connected to server.", host=self.host, port=self.port)

        request = control_center_pb2.WatchRequest()

        try:
            for event in self.stub.WatchCommands(request, metadata=self._get_metadata()):
                yield {
                    'session_id':        event.session_id,
                    'agent_id':          event.agent_id,
                    'agent_version':     event.agent_version,
                    'os_type':           event.os_type,
                    'timestamp':         event.timestamp,
                    'raw_command':       event.raw_command,
                    'action_type':       event.action_type,
                    'action_subtype':    event.action_subtype,
                    'is_here_command':   event.is_here_command,
                    'success':           event.success,
                    'error_message':     event.error_message,
                    'execution_time_ms': event.execution_time_ms,
                    'mouse_x':           event.mouse_x,
                    'mouse_y':           event.mouse_y,
                    'position_captured': event.position_captured,
                    'is_heartbeat':      event.is_heartbeat,
                    'agent_alive':       event.agent_alive,
                }
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.UNAVAILABLE:
                logger.info("WatchCommands stream closed — agent disconnected")
            else:
                logger.error(f"WatchCommands failed: {e.code()}: {e.details() if hasattr(e, 'details') else e}")

    # Internal helpers
    def _connection_metadata_to_dict(self, conn) -> Dict:
        """Convert a ConnectionMetadata proto message to a plain dict."""
        return {
            'connection_id': conn.connection_id,
            'server_id': conn.server_id,
            'agent_id': conn.agent_id,
            'agent_hostname': conn.agent_hostname,
            'agent_ip': conn.agent_ip,
            'server_ip': conn.server_ip,
            'network': conn.network,
            'connected_at': conn.connected_at,
            'last_heartbeat': conn.last_heartbeat,
            'commands_executed': conn.commands_executed,
            'state': conn.state,
        }

    # Context manager support for automatic connection management
    def __enter__(self):
        """Context manager entry"""
        return self
    
    # Context manager exit to ensure proper disconnection
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.disconnect()
        return False