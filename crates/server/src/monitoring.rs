// crates/server/src/monitoring.rs
// Monitoring API - Query connection status and server metrics

use tonic::{Request, Response, Status};
use tracing::info;
use std::sync::Arc;

use crate::proto::{
    ConnectionQuery, ConnectionStatusResponse, ConnectionMetadata,
    ServerStatusQuery, ServerStatusResponse, ServerInfo,
    ServerStatus, ConnectionState as ProtoConnectionState,
};
use crate::registry::{ConnectionRegistry, ConnectionState};
use crate::identity::ServerIdentityConfig;

/// Monitoring service handler
pub struct MonitoringHandler {
    registry: Arc<ConnectionRegistry>,
    server_identity: ServerIdentityConfig,
    server_version: String,
    started_at: i64,
}

/// Implementation of monitoring handler
impl MonitoringHandler {
    /// Create new monitoring handler
    pub fn new(
        registry: Arc<ConnectionRegistry>,
        server_identity: ServerIdentityConfig,
        server_version: String,
    ) -> Self {
        Self {
            registry,
            server_identity,
            server_version,
            started_at: chrono::Utc::now().timestamp(),
        }
    }
    
    /// Handle connection query
    pub async fn handle_connection_query(
        &self,
        _request: Request<ConnectionQuery>,
    ) -> Result<Response<ConnectionStatusResponse>, Status> {
        info!("Received connection query");
        
        // Get current connection
        let current = self.registry.get_current_connection().await;
        
        let mut connections = Vec::new();
        
        if let Some(agent) = current {
            connections.push(ConnectionMetadata {
                connection_id: agent.connection_id.clone(),
                server_id: self.server_identity.server_id.clone(),
                agent_id: agent.agent_id.clone(),
                agent_hostname: agent.agent_hostname.clone(),
                agent_ip: agent.agent_ip.clone(),
                server_ip: "0.0.0.0".to_string(), // TODO: Get actual server IP
                network: self.server_identity.network.clone(),
                connected_at: self.started_at + agent.connected_at.elapsed().as_secs() as i64,
                last_heartbeat: self.started_at + agent.last_heartbeat.elapsed().as_secs() as i64,
                commands_executed: agent.commands_executed,
                state: self.connection_state_to_proto(&agent.state),
            });
        }

        let total_count = connections.len() as i32;
        Ok(Response::new(ConnectionStatusResponse {
            connections,
            total_count,
        }))
    }
    
    /// Handle server status query
    pub async fn handle_server_status_query(
        &self,
        _request: Request<ServerStatusQuery>,
    ) -> Result<Response<ServerStatusResponse>, Status> {
        info!("Received server status query");
        
        let stats = self.registry.get_stats().await;
        let current_connection = self.registry.get_current_connection().await;
        
        // Build server status
        let status = ServerStatus {
            accepting_connections: true,
            agent_connected: stats.current_connection,
            total_commands_processed: stats.total_commands_executed,
            uptime_seconds: (chrono::Utc::now().timestamp() - self.started_at),
        };
        
        // Build server identity
        let identity = crate::proto::ServerIdentity {
            server_id: self.server_identity.server_id.clone(),
            hostname: crate::identity::get_hostname(),
            listen_address: "0.0.0.0:50051".to_string(), // TODO: Get actual listen address
            version: self.server_version.clone(),
            started_at: self.started_at,
            network: self.server_identity.network.clone(),
        };
        
        // Build connection metadata if agent is connected
        let current_connection_metadata = current_connection.map(|agent| ConnectionMetadata {
            connection_id: agent.connection_id.clone(),
            server_id: self.server_identity.server_id.clone(),
            agent_id: agent.agent_id.clone(),
            agent_hostname: agent.agent_hostname.clone(),
            agent_ip: agent.agent_ip.clone(),
            server_ip: "0.0.0.0".to_string(),
            network: self.server_identity.network.clone(),
            connected_at: self.started_at + agent.connected_at.elapsed().as_secs() as i64,
            last_heartbeat: self.started_at + agent.last_heartbeat.elapsed().as_secs() as i64,
            commands_executed: agent.commands_executed,
            state: self.connection_state_to_proto(&agent.state),
        });
        
        let server_info = ServerInfo {
            identity: Some(identity),
            status: Some(status),
            current_connection: current_connection_metadata,
            last_seen: chrono::Utc::now().timestamp(),
        };
        
        Ok(Response::new(ServerStatusResponse {
            servers: vec![server_info],
            total_count: 1,
        }))
    }
    
    /// Convert internal connection state to protobuf enum
    fn connection_state_to_proto(&self, state: &ConnectionState) -> i32 {
        match state {
            ConnectionState::Connecting => ProtoConnectionState::Connecting as i32,
            ConnectionState::Connected => ProtoConnectionState::Connected as i32,
            ConnectionState::Active => ProtoConnectionState::Active as i32,
            ConnectionState::Idle => ProtoConnectionState::Idle as i32,
            ConnectionState::Disconnecting => ProtoConnectionState::Disconnecting as i32,
            ConnectionState::Disconnected => ProtoConnectionState::Disconnected as i32,
        }
    }
    
    /// Get connection history
    pub async fn get_connection_history(&self, limit: Option<usize>) -> Vec<crate::registry::AgentMetadata> {
        self.registry.get_history(limit).await
    }
    
    /// Export metrics in Prometheus format (optional feature)
    #[cfg(feature = "prometheus")]
    pub fn export_prometheus_metrics(&self) -> String {
        let stats = tokio::runtime::Runtime::new()
            .unwrap()
            .block_on(self.registry.get_stats());
        
        format!(
            "# HELP control_center_agent_connected Whether an agent is currently connected\n\
             # TYPE control_center_agent_connected gauge\n\
             control_center_agent_connected {}\n\
             \n\
             # HELP control_center_total_connections Total number of connections in history\n\
             # TYPE control_center_total_connections counter\n\
             control_center_total_connections {}\n\
             \n\
             # HELP control_center_commands_executed Total commands executed\n\
             # TYPE control_center_commands_executed counter\n\
             control_center_commands_executed {}\n\
             \n\
             # HELP control_center_uptime_seconds Server uptime in seconds\n\
             # TYPE control_center_uptime_seconds gauge\n\
             control_center_uptime_seconds {}\n",
            if stats.current_connection { 1 } else { 0 },
            stats.total_connections,
            stats.total_commands_executed,
            chrono::Utc::now().timestamp() - self.started_at
        )
    }
}

/// Helper function to format connection status for CLI display
/// This is a utility function that can be used by CLI tools
#[allow(dead_code)]
pub fn format_connection_status(metadata: &ConnectionMetadata) -> String {
    format!(
        "Connection ID: {}\n\
         Server ID: {}\n\
         Agent ID: {}\n\
         Agent Hostname: {}\n\
         Agent IP: {}\n\
         Network: {}\n\
         Connected At: {}\n\
         Commands Executed: {}\n\
         State: {:?}",
        metadata.connection_id,
        metadata.server_id,
        metadata.agent_id,
        metadata.agent_hostname,
        metadata.agent_ip,
        metadata.network,
        chrono::DateTime::from_timestamp(metadata.connected_at, 0)
            .map(|dt| dt.to_rfc3339())
            .unwrap_or_else(|| "Unknown".to_string()),
        metadata.commands_executed,
        metadata.state
    )
}

// Unit tests for monitoring module
#[cfg(test)]
mod tests {
    use super::*;
    
    #[test]
    fn test_format_connection_status() {
        let metadata = ConnectionMetadata {
            connection_id: "conn-123".to_string(),
            server_id: "srv-456".to_string(),
            agent_id: "agent-789".to_string(),
            agent_hostname: "windows-vm".to_string(),
            agent_ip: "192.168.1.100".to_string(),
            server_ip: "192.168.1.1".to_string(),
            network: "test-network".to_string(),
            connected_at: 1234567890,
            last_heartbeat: 1234567900,
            commands_executed: 42,
            state: ProtoConnectionState::Active as i32,
        };
        
        let formatted = format_connection_status(&metadata);
        assert!(formatted.contains("conn-123"));
        assert!(formatted.contains("agent-789"));
        assert!(formatted.contains("192.168.1.100"));
    }
}