// crates/server/src/registry.rs
// Connection Registry - Manages agent connections with 1:1 enforcement

use std::sync::Arc;
use tokio::sync::RwLock;
use tracing::{info, warn};
use std::time::Instant;

/// Connected agent metadata
#[derive(Debug, Clone)]
pub struct ConnectedAgent {
    pub connection_id: String,
    pub agent_id: String,
    pub agent_hostname: String,
    pub agent_ip: String,
    pub agent_os: String,
    pub agent_version: String,
    pub connected_at: Instant,
    pub last_heartbeat: Instant,
    pub commands_executed: u64,
    pub state: ConnectionState,
    pub os_type: i32,
    pub os_version: String,
    pub capabilities: Vec<String>,
}

/// Connection state
#[derive(Debug, Clone, PartialEq)]
pub enum ConnectionState {
    Connecting,
    Connected,
    Active,
    Idle,
    Disconnecting,
    Disconnected,
}

impl ConnectionState {
    pub fn to_proto(&self) -> i32 {
        match self {
            ConnectionState::Connecting => 0,
            ConnectionState::Connected => 1,
            ConnectionState::Active => 2,
            ConnectionState::Idle => 3,
            ConnectionState::Disconnecting => 4,
            ConnectionState::Disconnected => 5,
        }
    }
}

/// Connection registry with 1:1 enforcement
pub struct ConnectionRegistry {
    current_connection: Arc<RwLock<Option<ConnectedAgent>>>,
    connection_history: Arc<RwLock<Vec<AgentMetadata>>>,
    max_history: usize,
    single_agent_mode: bool,
    /// Notify channel: when Some(reason) is set, the stream handler should
    /// gracefully close the agent stream.
    disconnect_signal: Arc<RwLock<Option<String>>>,
}

/// Historical connection metadata (maps 1:1 to HistoricalConnection proto)
#[derive(Debug, Clone)]
pub struct AgentMetadata {
    pub connection_id: String,
    pub agent_id: String,
    pub agent_hostname: String,
    pub agent_ip: String,
    pub os_type: i32,
    pub os_version: String,
    pub capabilities: Vec<String>,
    pub server_ip: String,
    pub connected_at: i64,
    pub disconnected_at: Option<i64>,
    pub commands_executed: u64,
    pub disconnect_reason: Option<String>,
}

impl ConnectionRegistry {
    /// Create new registry
    pub fn new(single_agent_mode: bool, max_history: usize) -> Self {
        info!("Initializing connection registry (single_agent_mode: {})", single_agent_mode);

        Self {
            current_connection: Arc::new(RwLock::new(None)),
            connection_history: Arc::new(RwLock::new(Vec::new())),
            max_history,
            single_agent_mode,
            disconnect_signal: Arc::new(RwLock::new(None)),
        }
    }

    /// Register a new agent (with 1:1 enforcement)
    pub async fn register_agent(
        &self,
        agent_identity: &crate::proto::AgentIdentity,
        connection_id: String,
        agent_ip: String,
    ) -> Result<(), String> {
        let mut connection_lock = self.current_connection.write().await;

        // 1:1 enforcement check
        if self.single_agent_mode {
            if let Some(existing) = connection_lock.as_ref() {
                let error_msg = format!(
                    "Server in single-agent mode. Agent {} (hostname: {}) is already connected. \
                     Disconnect existing agent or restart server to accept new connections.",
                    existing.agent_id,
                    existing.agent_hostname
                );

                warn!("Registration rejected: {}", error_msg);
                return Err(error_msg);
            }
        }

        // Clear any stale disconnect signal from a previous session
        {
            let mut signal = self.disconnect_signal.write().await;
            *signal = None;
        }

        // Create new connection
        let connected_agent = ConnectedAgent {
            connection_id: connection_id.clone(),
            agent_id: agent_identity.agent_id.clone(),
            agent_hostname: agent_identity.hostname.clone(),
            agent_ip: agent_ip.clone(),
            os_type: agent_identity.os_type,
            os_version: agent_identity.os_version.clone(),
            capabilities: agent_identity.capabilities.clone(),
            agent_os: format!("{:?}", crate::proto::OsType::try_from(agent_identity.os_type).ok()),
            agent_version: agent_identity.version.clone(),
            connected_at: Instant::now(),
            last_heartbeat: Instant::now(),
            commands_executed: 0,
            state: ConnectionState::Connected,
        };

        info!(
            "Agent registered: {} (ID: {}, Hostname: {}, IP: {})",
            connected_agent.agent_id,
            connection_id,
            connected_agent.agent_hostname,
            connected_agent.agent_ip
        );

        *connection_lock = Some(connected_agent);
        Ok(())
    }

    /// Unregister agent (called on disconnect)
    pub async fn unregister_agent(&self, connection_id: &str, reason: Option<String>) {
        let mut connection_lock = self.current_connection.write().await;

        if let Some(agent) = connection_lock.take() {
            if agent.connection_id == connection_id {
                info!(
                    "Agent unregistered: {} (Reason: {})",
                    agent.agent_id,
                    reason.as_ref().unwrap_or(&"none".to_string())
                );

                // Add to history
                let metadata = AgentMetadata {
                    connection_id: agent.connection_id,
                    agent_id: agent.agent_id,
                    agent_hostname: agent.agent_hostname,
                    agent_ip: agent.agent_ip,
                    os_type: agent.os_type,
                    os_version: agent.os_version,
                    capabilities: agent.capabilities,
                    server_ip: "0.0.0.0".to_string(),
                    connected_at: chrono::Utc::now().timestamp()
                        - agent.connected_at.elapsed().as_secs() as i64,
                    disconnected_at: Some(chrono::Utc::now().timestamp()),
                    commands_executed: agent.commands_executed,
                    disconnect_reason: reason,
                };

                let mut history_lock = self.connection_history.write().await;
                history_lock.push(metadata);

                // Trim history if needed
                if history_lock.len() > self.max_history {
                    history_lock.remove(0);
                }
            }
        }
    }

    /// Signal the stream handler to disconnect the current agent gracefully.
    /// Returns (true, connection_id) if an agent was connected, (false, "") otherwise.
    pub async fn request_disconnect(&self, reason: String) -> (bool, String) {
        let connection_lock = self.current_connection.read().await;

        if let Some(agent) = connection_lock.as_ref() {
            let conn_id = agent.connection_id.clone();
            drop(connection_lock);

            // Write the signal — the stream handler polls this and will close the stream
            let mut signal = self.disconnect_signal.write().await;
            *signal = Some(reason);

            info!("Disconnect requested for connection: {}", conn_id);
            (true, conn_id)
        } else {
            (false, String::new())
        }
    }

    /// Check and consume the disconnect signal. Called by the stream handler on each loop tick.
    /// Returns Some(reason) if a disconnect was requested, None otherwise.
    pub async fn consume_disconnect_signal(&self) -> Option<String> {
        let mut signal = self.disconnect_signal.write().await;
        signal.take()
    }

    /// Check if agent is connected
    pub async fn is_agent_connected(&self) -> bool {
        self.current_connection.read().await.is_some()
    }

    /// Get current connection
    pub async fn get_current_connection(&self) -> Option<ConnectedAgent> {
        self.current_connection.read().await.clone()
    }

    /// Update heartbeat timestamp
    pub async fn update_heartbeat(&self, connection_id: &str) {
        let mut connection_lock = self.current_connection.write().await;

        if let Some(agent) = connection_lock.as_mut() {
            if agent.connection_id == connection_id {
                agent.last_heartbeat = Instant::now();

                // Update state to Idle if was Active
                if agent.state == ConnectionState::Active {
                    agent.state = ConnectionState::Idle;
                }
            }
        }
    }

    /// Increment command counter
    pub async fn increment_command_count(&self, connection_id: &str) {
        let mut connection_lock = self.current_connection.write().await;

        if let Some(agent) = connection_lock.as_mut() {
            if agent.connection_id == connection_id {
                agent.commands_executed += 1;
                agent.state = ConnectionState::Active;
            }
        }
    }

    /// Update connection state
    pub async fn update_state(&self, connection_id: &str, state: ConnectionState) {
        let mut connection_lock = self.current_connection.write().await;

        if let Some(agent) = connection_lock.as_mut() {
            if agent.connection_id == connection_id {
                agent.state = state;
            }
        }
    }

    /// Get connection history
    pub async fn get_history(&self, limit: Option<usize>) -> Vec<AgentMetadata> {
        let history_lock = self.connection_history.read().await;
        let history = history_lock.clone();

        if let Some(limit) = limit {
            let start = history.len().saturating_sub(limit);
            history[start..].to_vec()
        } else {
            history
        }
    }

    /// Get connection statistics
    pub async fn get_stats(&self) -> RegistryStats {
        let current = self.current_connection.read().await;
        let history = self.connection_history.read().await;

        RegistryStats {
            current_connection: current.is_some(),
            total_connections: history.len(),
            current_uptime_seconds: current.as_ref().map(|c| c.connected_at.elapsed().as_secs()),
            total_commands_executed: current
                .as_ref()
                .map(|c| c.commands_executed)
                .unwrap_or(0),
        }
    }

    /// Check for stale connections (heartbeat timeout)
    pub async fn check_heartbeat_timeout(&self, timeout_seconds: u64) -> Option<String> {
        let connection_lock = self.current_connection.read().await;

        if let Some(agent) = connection_lock.as_ref() {
            let elapsed = agent.last_heartbeat.elapsed().as_secs();
            if elapsed > timeout_seconds {
                return Some(format!(
                    "Heartbeat timeout: {} seconds since last heartbeat",
                    elapsed
                ));
            }
        }

        None
    }
}

/// Registry statistics
#[derive(Debug, Clone)]
pub struct RegistryStats {
    pub current_connection: bool,
    pub total_connections: usize,
    pub current_uptime_seconds: Option<u64>,
    pub total_commands_executed: u64,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_single_agent_enforcement() {
        let registry = ConnectionRegistry::new(true, 10);

        let agent1 = crate::proto::AgentIdentity {
            agent_id: "agent-1".to_string(),
            hostname: "host-1".to_string(),
            os_type: 0,
            os_version: "Windows 10".to_string(),
            ip_address: "192.168.1.100".to_string(),
            version: "1.0.0".to_string(),
            capabilities: vec![],
            metadata: std::collections::HashMap::new(),
        };

        // First agent should register successfully
        let result1 = registry
            .register_agent(&agent1, "conn-1".to_string(), "192.168.1.100".to_string())
            .await;
        assert!(result1.is_ok());

        // Second agent should be rejected
        let agent2 = crate::proto::AgentIdentity {
            agent_id: "agent-2".to_string(),
            hostname: "host-2".to_string(),
            os_type: 0,
            os_version: "Windows 10".to_string(),
            ip_address: "192.168.1.101".to_string(),
            version: "1.0.0".to_string(),
            capabilities: vec![],
            metadata: std::collections::HashMap::new(),
        };

        let result2 = registry
            .register_agent(&agent2, "conn-2".to_string(), "192.168.1.101".to_string())
            .await;
        assert!(result2.is_err());
        assert!(result2.unwrap_err().contains("single-agent mode"));
    }

    #[tokio::test]
    async fn test_unregister_and_history() {
        let registry = ConnectionRegistry::new(true, 10);

        let agent = crate::proto::AgentIdentity {
            agent_id: "agent-1".to_string(),
            hostname: "host-1".to_string(),
            os_type: 0,
            os_version: "Windows 10".to_string(),
            ip_address: "192.168.1.100".to_string(),
            version: "1.0.0".to_string(),
            capabilities: vec![],
            metadata: std::collections::HashMap::new(),
        };

        registry
            .register_agent(&agent, "conn-1".to_string(), "192.168.1.100".to_string())
            .await
            .unwrap();
        assert!(registry.is_agent_connected().await);

        registry
            .unregister_agent("conn-1", Some("test disconnect".to_string()))
            .await;
        assert!(!registry.is_agent_connected().await);

        let history = registry.get_history(None).await;
        assert_eq!(history.len(), 1);
        assert_eq!(history[0].agent_id, "agent-1");
        assert!(history[0].disconnect_reason.is_some());
    }

    #[tokio::test]
    async fn test_disconnect_signal() {
        let registry = ConnectionRegistry::new(true, 10);

        // No agent connected — request_disconnect should return false
        let (disconnected, _) = registry
            .request_disconnect("admin request".to_string())
            .await;
        assert!(!disconnected);

        // Register agent then request disconnect
        let agent = crate::proto::AgentIdentity {
            agent_id: "agent-1".to_string(),
            hostname: "host-1".to_string(),
            os_type: 0,
            os_version: "Windows 10".to_string(),
            ip_address: "192.168.1.100".to_string(),
            version: "1.0.0".to_string(),
            capabilities: vec![],
            metadata: std::collections::HashMap::new(),
        };
        registry
            .register_agent(&agent, "conn-1".to_string(), "192.168.1.100".to_string())
            .await
            .unwrap();

        let (disconnected, conn_id) = registry
            .request_disconnect("test reason".to_string())
            .await;
        assert!(disconnected);
        assert_eq!(conn_id, "conn-1");

        // Signal should be consumable once
        let signal = registry.consume_disconnect_signal().await;
        assert_eq!(signal, Some("test reason".to_string()));

        // Second consume returns None (already consumed)
        let signal2 = registry.consume_disconnect_signal().await;
        assert_eq!(signal2, None);
    }
}