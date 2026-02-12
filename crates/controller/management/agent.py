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


class AgentManager:
    """Manage agent information and health"""
    
    def __init__(self):
        self.agents: Dict[str, AgentInfo] = {}
    
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
    
    # Update last seen timestamp
    def update_last_seen(self):
        """Update last seen timestamp"""
        self.last_seen = datetime.now()
    
    # Convert to dictionary for serialization
    def to_dict(self) -> Dict:
        """Convert to dictionary"""
        return {
            'agent_id': self.agent_id,
            'host': self.host,
            'port': self.port,
            'os_type': self.os_type,
            'os_version': self.os_version,
            'registered_at': self.registered_at.isoformat(),
            'last_seen': self.last_seen.isoformat(),
        }