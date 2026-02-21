"""Agent management utilities

Note: In current architecture, agent management is not needed
because agents run independently in containers. This module
is reserved for future features like:
- Remote agent deployment
- Agent health monitoring from CLI
- Agent update management
"""

from typing import Dict, List, Optional
from datetime import datetime


# Map OsType proto integer to readable string (mirrors the server-side enum)
_OS_TYPE_MAP = {0: "WINDOWS", 1: "MACOS", 2: "LINUX"}


class AgentManager:
    """Manage agent information and health"""
    
    def __init__(self):
        self.agents: Dict[str, 'AgentInfo'] = {}
    
    # Agent registration and management methods
    def register_agent(self, agent_id: str, host: str, port: int, 
                      os_type: str, os_version: str):
        """Register a new agent"""
        self.agents[agent_id] = AgentInfo(
            agent_id=agent_id,
            host=host,
            port=port,
            os_type=os_type,
            os_version=os_version,
        )
    
    # Agent health monitoring methods
    def unregister_agent(self, agent_id: str):
        """Unregister an agent"""
        if agent_id in self.agents:
            del self.agents[agent_id]
    
    # Agent information retrieval methods
    def get_agent(self, agent_id: str) -> Optional['AgentInfo']:
        """Get agent information"""
        return self.agents.get(agent_id)
    
    # List all registered agents
    def list_agents(self) -> List['AgentInfo']:
        """List all registered agents"""
        return list(self.agents.values())

    # ------------------------------------------------------------------ #
    # Live-data refresh (wires to gRPC client)
    # ------------------------------------------------------------------ #

    def refresh(self, grpc_client) -> bool:
        """Refresh agent registry from the live server connection.

        Calls query_connections() on the gRPC client, clears the local
        agents dict, and repopulates it from the server's live data.
        Returns True if at least one agent was found.

        Called by CLI commands that need real connection metadata rather
        than the in-process stubs from register_agent().
        """
        try:
            if not hasattr(grpc_client, 'query_connections'):
                return False
            data = grpc_client.query_connections()
            if not data or data.get('total_count', 0) == 0:
                return False

            self.agents.clear()
            for conn in data.get('connections', []):
                info = AgentInfo.from_connection_dict(conn)
                self.agents[info.agent_id] = info

            return bool(self.agents)
        except Exception:
            return False


# Agent information data class
class AgentInfo:
    """Agent information"""
    
    def __init__(self, agent_id: str, host: str, port: int,
                 os_type: str, os_version: str):
        self.agent_id = agent_id
        self.host = host
        self.port = port
        self.os_type = os_type
        self.os_version = os_version
        self.registered_at = datetime.now()
        self.last_seen = datetime.now()
        # Extended fields -- populated by factory classmethods, None when
        # created via the legacy register_agent() path
        self.agent_hostname: Optional[str] = None
        self.agent_ip: Optional[str] = None
        self.connection_id: Optional[str] = None
        self.capabilities: List[str] = []
        self.commands_executed: int = 0
        self.connected_at: Optional[int] = None        # Unix timestamp from server
        self.disconnect_reason: Optional[str] = None   # Only set for historical records
    
    # Update last seen timestamp
    def update_last_seen(self):
        """Update last seen timestamp"""
        self.last_seen = datetime.now()
    
    # Convert to dictionary for serialization
    def to_dict(self) -> Dict:
        """Convert to dictionary"""
        return {
            'agent_id':        self.agent_id,
            'host':            self.host,
            'port':            self.port,
            'os_type':         self.os_type,
            'os_version':      self.os_version,
            'registered_at':   self.registered_at.isoformat(),
            'last_seen':       self.last_seen.isoformat(),
            # extended fields (None for legacy-created instances)
            'agent_hostname':  self.agent_hostname,
            'agent_ip':        self.agent_ip,
            'connection_id':   self.connection_id,
            'capabilities':    self.capabilities,
            'commands_executed': self.commands_executed,
            'connected_at':    self.connected_at,
            'disconnect_reason': self.disconnect_reason,
        }

    # ------------------------------------------------------------------ #
    # Factory classmethods
    # ------------------------------------------------------------------ #

    @classmethod
    def from_connection_dict(cls, conn: Dict) -> 'AgentInfo':
        """Build an AgentInfo from a live connection dict.

        The dict comes from GRPCClient._connection_metadata_to_dict(), which
        maps the server's ConnectionMetadata proto fields:
          connection_id, server_id, agent_id, agent_hostname, agent_ip,
          server_ip, network, connected_at, last_heartbeat,
          commands_executed, state.
        """
        os_int = conn.get('os_type', -1)
        os_str = _OS_TYPE_MAP.get(os_int, f"UNKNOWN({os_int})")

        info = cls(
            agent_id=conn.get('agent_id', ''),
            host=conn.get('agent_ip', ''),
            port=0,
            os_type=os_str,
            os_version=conn.get('os_version', ''),
        )
        info.agent_hostname    = conn.get('agent_hostname')
        info.agent_ip          = conn.get('agent_ip')
        info.connection_id     = conn.get('connection_id')
        info.capabilities      = list(conn.get('capabilities', []))
        info.commands_executed = int(conn.get('commands_executed', 0))
        info.connected_at      = conn.get('connected_at')
        return info

    @classmethod
    def from_history_dict(cls, hist: Dict) -> 'AgentInfo':
        """Build an AgentInfo from a historical connection dict.

        The dict comes from GRPCClient.get_connection_history(), which maps
        the server's HistoricalConnection proto fields:
          connection_id, agent_id, agent_hostname, agent_ip, os_type,
          os_version, capabilities, server_ip, connected_at,
          disconnected_at, commands_executed, disconnect_reason.
        """
        os_int = hist.get('os_type', -1)
        os_str = _OS_TYPE_MAP.get(os_int, f"UNKNOWN({os_int})")

        info = cls(
            agent_id=hist.get('agent_id', ''),
            host=hist.get('agent_ip', ''),
            port=0,
            os_type=os_str,
            os_version=hist.get('os_version', ''),
        )
        info.agent_hostname    = hist.get('agent_hostname')
        info.agent_ip          = hist.get('agent_ip')
        info.connection_id     = hist.get('connection_id')
        info.capabilities      = list(hist.get('capabilities', []))
        info.commands_executed = int(hist.get('commands_executed', 0))
        info.connected_at      = hist.get('connected_at')
        info.disconnect_reason = hist.get('disconnect_reason')
        return info