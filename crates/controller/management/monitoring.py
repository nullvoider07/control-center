# crates/controller/management/monitoring.py
# Monitoring CLI Commands - Query and display connection status

import click
import grpc
import sys
import time
from datetime import datetime
from typing import Optional

# Import protobuf definitions
try:
    from controller.integrations.proto import control_center_pb2 as cc_pb2
    from controller.integrations.proto import control_center_pb2_grpc as cc_grpc
except ImportError:
    print("[ERROR] Failed to import protobuf definitions", file=sys.stderr)
    print("[ERROR] Run: python -m grpc_tools.protoc -Iproto --python_out=. --grpc_python_out=. proto/control_center.proto", file=sys.stderr)
    sys.exit(1)


class MonitoringClient:
    """Client for monitoring server and agent status"""
    
    def __init__(self, host, port, token=None):
        self.host = host
        self.port = port
        self.token = token
        self.channel: Optional[grpc.Channel] = None
        self.stub: Optional[cc_grpc.ControlServiceStub] = None
    
    def connect(self):
        """Establish connection to server"""
        try:
            self.channel = grpc.insecure_channel(f'{self.host}:{self.port}')
            self.stub = cc_grpc.ControlServiceStub(self.channel)
            return True
        except Exception as e:
            click.echo(f"[✗] Connection failed: {e}", err=True)
            return False
    
    def close(self):
        """Close connection"""
        if self.channel:
            self.channel.close()
    
    def query_connections(self, server_id=None, agent_id=None, network=None):
        """Query connection status"""
        if not self.connect():
            return None
        
        assert self.stub is not None, "Stub should be initialized after successful connection"

        try:
            request = cc_pb2.ConnectionQuery(
                server_id=server_id or "",
                agent_id=agent_id or "",
                network=network or ""
            )
            
            response = self.stub.QueryConnections(request)
            return response
        except grpc.RpcError as e:
            click.echo(f"[✗] Query failed: {e.details()}", err=True)
            return None
        finally:
            self.close()
    
    def query_server_status(self, server_id=None, network=None):
        """Query server status"""
        if not self.connect():
            return None
        
        assert self.stub is not None, "Stub should be initialized after successful connection"
        
        try:
            request = cc_pb2.ServerStatusQuery(
                server_id=server_id or "",
                network=network or ""
            )
            
            response = self.stub.QueryServers(request)
            return response
        except grpc.RpcError as e:
            click.echo(f"[✗] Query failed: {e.details()}", err=True)
            return None
        finally:
            self.close()
    
    def get_server_identity(self):
        """Get server identity"""
        if not self.connect():
            return None
        
        assert self.stub is not None, "Stub should be initialized after successful connection"
        
        try:
            request = cc_pb2.InfoRequest()
            response = self.stub.GetServerIdentity(request)
            return response
        except grpc.RpcError as e:
            click.echo(f"[✗] Query failed: {e.details()}", err=True)
            return None
        finally:
            self.close()


def format_timestamp(timestamp):
    """Format Unix timestamp to human-readable string"""
    try:
        dt = datetime.fromtimestamp(timestamp)
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except:
        return "Unknown"


def format_duration(seconds):
    """Format duration in seconds to human-readable string"""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes}m {secs}s"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m"


def format_connection_state(state):
    """Format connection state enum to string"""
    states = {
        0: "CONNECTING",
        1: "CONNECTED",
        2: "ACTIVE",
        3: "IDLE",
        4: "DISCONNECTING",
        5: "DISCONNECTED",
        6: "ERROR"
    }
    return states.get(state, "UNKNOWN")


@click.group()
def monitoring():
    """Monitoring commands"""
    pass


@monitoring.command(name='status')
@click.option('--host', default='localhost', help='Server host')
@click.option('--port', default=50051, help='Server port')
@click.option('--watch', is_flag=True, help='Watch mode (refresh every 5s)')
def status_command(host, port, watch):
    """Show server and connection status"""
    
    client = MonitoringClient(host, port)
    
    def display_status():
        # Clear screen if in watch mode
        if watch:
            click.clear()
        
        # Query server status
        click.echo("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        click.echo("  Control Center - Status")
        click.echo("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        click.echo("")
        
        server_response = client.query_server_status()
        
        if not server_response or server_response.total_count == 0:
            click.echo("[✗] Server not responding or not found")
            return
        
        server_info = server_response.servers[0]
        identity = server_info.identity
        status = server_info.status
        
        # Display server info
        click.echo("📡 SERVER INFORMATION")
        click.echo(f"  Server ID:       {identity.server_id}")
        click.echo(f"  Hostname:        {identity.hostname}")
        click.echo(f"  Listen Address:  {identity.listen_address}")
        click.echo(f"  Network:         {identity.network}")
        click.echo(f"  Version:         {identity.version}")
        click.echo(f"  Started:         {format_timestamp(identity.started_at)}")
        click.echo(f"  Uptime:          {format_duration(status.uptime_seconds)}")
        click.echo("")
        
        # Display connection status
        click.echo("🔌 CONNECTION STATUS")
        
        if status.agent_connected:
            click.echo("  Status: ✓ AGENT CONNECTED")
            click.echo("")
            
            if server_info.current_connection:
                conn = server_info.current_connection
                click.echo("  Agent Information:")
                click.echo(f"    Agent ID:        {conn.agent_id}")
                click.echo(f"    Hostname:        {conn.agent_hostname}")
                click.echo(f"    IP Address:      {conn.agent_ip}")
                click.echo(f"    Connection ID:   {conn.connection_id}")
                click.echo(f"    Connected At:    {format_timestamp(conn.connected_at)}")
                click.echo(f"    Last Heartbeat:  {format_timestamp(conn.last_heartbeat)}")
                click.echo(f"    State:           {format_connection_state(conn.state)}")
                click.echo(f"    Commands:        {conn.commands_executed}")
        else:
            click.echo("  Status: ✗ NO AGENT CONNECTED")
            click.echo("  Waiting for agent to connect...")
        
        click.echo("")
        click.echo("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        
        if watch:
            click.echo("")
            click.echo("Refreshing in 5 seconds... (Press Ctrl+C to stop)")
    
    try:
        if watch:
            while True:
                display_status()
                time.sleep(5)
        else:
            display_status()
    except KeyboardInterrupt:
        click.echo("\n\n[INFO] Stopped monitoring")


@monitoring.command(name='connections')
@click.option('--host', default='localhost', help='Server host')
@click.option('--port', default=50051, help='Server port')
@click.option('--server-id', help='Filter by server ID')
@click.option('--agent-id', help='Filter by agent ID')
@click.option('--network', help='Filter by network')
def connections_command(host, port, server_id, agent_id, network):
    """Query connection information"""
    
    client = MonitoringClient(host, port)
    response = client.query_connections(server_id, agent_id, network)
    
    if not response:
        sys.exit(1)
    
    click.echo("")
    click.echo(f"Found {response.total_count} connection(s)")
    click.echo("")
    
    for i, conn in enumerate(response.connections, 1):
        click.echo(f"[{i}] Connection")
        click.echo(f"  Connection ID:   {conn.connection_id}")
        click.echo(f"  Server ID:       {conn.server_id}")
        click.echo(f"  Agent ID:        {conn.agent_id}")
        click.echo(f"  Agent Hostname:  {conn.agent_hostname}")
        click.echo(f"  Agent IP:        {conn.agent_ip}")
        click.echo(f"  Network:         {conn.network}")
        click.echo(f"  Connected At:    {format_timestamp(conn.connected_at)}")
        click.echo(f"  Last Heartbeat:  {format_timestamp(conn.last_heartbeat)}")
        click.echo(f"  State:           {format_connection_state(conn.state)}")
        click.echo(f"  Commands:        {conn.commands_executed}")
        click.echo("")


@monitoring.command(name='identity')
@click.option('--host', default='localhost', help='Server host')
@click.option('--port', default=50051, help='Server port')
def identity_command(host, port):
    """Show server identity"""
    
    client = MonitoringClient(host, port)
    identity = client.get_server_identity()
    
    if not identity:
        sys.exit(1)
    
    click.echo("")
    click.echo("SERVER IDENTITY")
    click.echo("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    click.echo(f"  Server ID:       {identity.server_id}")
    click.echo(f"  Hostname:        {identity.hostname}")
    click.echo(f"  Listen Address:  {identity.listen_address}")
    click.echo(f"  Network:         {identity.network}")
    click.echo(f"  Version:         {identity.version}")
    click.echo(f"  Started At:      {format_timestamp(identity.started_at)}")
    click.echo("")


if __name__ == '__main__':
    monitoring()