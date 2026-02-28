// crates/agent/src/connection.rs
// Agent Connection Management - Connect to server, register, maintain connection

use tonic::transport::Channel;
use tonic::Request;
use tracing::{info, warn, error};
use tokio::time::{interval, Duration};
use std::sync::Arc;
use tokio::sync::RwLock;

use crate::proto::{
    control_service_client::ControlServiceClient,
    RegistrationRequest, RegistrationResponse,
    AgentMessage, AgentHeartbeat, AgentStatus,
    agent_message,
};

/// Connection state
#[derive(Debug, Clone, PartialEq)]
#[allow(dead_code)]
pub enum ConnectionStatus {
    Disconnected,
    Connecting,
    Registering,
    Connected,
    #[allow(dead_code)]
    Active,
    #[allow(dead_code)]
    Reconnecting,
    Error(String),
}

/// Agent connection manager
pub struct ConnectionManager {
    server_url: String,
    agent_identity: crate::proto::AgentIdentity,
    auth_token: Option<String>,
    connection_id: Arc<RwLock<Option<String>>>,
    status: Arc<RwLock<ConnectionStatus>>,
    #[allow(dead_code)]
    commands_executed: Arc<RwLock<u64>>,
    #[allow(dead_code)]
    commands_failed: Arc<RwLock<u64>>,
}

// Implement connection manager
impl ConnectionManager {
    /// Create new connection manager
    pub fn new(
        server_host: String,
        server_port: u16,
        agent_identity: crate::proto::AgentIdentity,
        auth_token: Option<String>,
    ) -> Self {
        let server_url = format!("http://{}:{}", server_host, server_port);
        
        info!("Connection manager initialized");
        info!("  Server URL: {}", server_url);
        info!("  Agent ID: {}", agent_identity.agent_id);
        
        Self {
            server_url,
            agent_identity,
            auth_token,
            connection_id: Arc::new(RwLock::new(None)),
            status: Arc::new(RwLock::new(ConnectionStatus::Disconnected)),
            commands_executed: Arc::new(RwLock::new(0)),
            commands_failed: Arc::new(RwLock::new(0)),
        }
    }
    
    /// Connect to server and register
    pub async fn connect_and_register(&self) -> Result<ControlServiceClient<Channel>, Box<dyn std::error::Error>> {
        info!("Connecting to server at {}...", self.server_url);
        *self.status.write().await = ConnectionStatus::Connecting;
        
        // Connect with retry logic
        let channel = self.connect_with_retry(5, 2).await?;
        let mut client = ControlServiceClient::new(channel);
        
        info!("✓ Connected to server");
        
        // Register with server
        info!("Registering with server...");
        *self.status.write().await = ConnectionStatus::Registering;
        
        let registration_request = RegistrationRequest {
            agent_identity: Some(self.agent_identity.clone()),
            auth_token: self.auth_token.clone().unwrap_or_default(),
            connection_params: std::collections::HashMap::new(),
        };
        
        // Explicitly type the response
        let response: tonic::Response<RegistrationResponse> = client
            .register_agent(Request::new(registration_request))
            .await?;
        let registration: RegistrationResponse = response.into_inner();
        
        if !registration.success {
            let error_msg = format!("Registration failed: {}", registration.message);
            error!("{}", error_msg);
            *self.status.write().await = ConnectionStatus::Error(error_msg.clone());
            return Err(error_msg.into());
        }
        
        // Store connection ID
        *self.connection_id.write().await = Some(registration.connection_id.clone());
        *self.status.write().await = ConnectionStatus::Connected;
        
        info!("✓ Registered with server");
        info!("  Connection ID: {}", registration.connection_id);
        
        if let Some(server_identity) = registration.server_identity {
            info!("  Server ID: {}", server_identity.server_id);
            info!("  Server Network: {}", server_identity.network);
        }
        
        Ok(client)
    }
    
    /// Connect with retry logic
    async fn connect_with_retry(
        &self,
        max_retries: usize,
        retry_delay_seconds: u64,
    ) -> Result<Channel, Box<dyn std::error::Error>> {
        let mut attempt = 0;
        
        loop {
            attempt += 1;
            
            match Channel::from_shared(self.server_url.clone())?
                .connect()
                .await
            {
                Ok(channel) => return Ok(channel),
                Err(e) => {
                    if attempt >= max_retries {
                        return Err(format!("Failed to connect after {} attempts: {}", max_retries, e).into());
                    }
                    
                    warn!("Connection attempt {} failed: {}", attempt, e);
                    warn!("Retrying in {} seconds...", retry_delay_seconds);
                    
                    tokio::time::sleep(Duration::from_secs(retry_delay_seconds)).await;
                }
            }
        }
    }
    
    /// Start heartbeat sender
    #[allow(dead_code)]
    pub async fn start_heartbeat(
        &self,
        _client: &mut ControlServiceClient<Channel>,
        interval_seconds: u64,
    ) {
        let connection_id = self.connection_id.read().await.clone();
        
        if connection_id.is_none() {
            warn!("Cannot start heartbeat: no connection ID");
            return;
        }
        
        let connection_id = connection_id.unwrap();
        info!("Starting heartbeat (interval: {}s)", interval_seconds);
        
        let mut heartbeat_interval = interval(Duration::from_secs(interval_seconds));
        
        loop {
            heartbeat_interval.tick().await;
            
            let commands_executed = *self.commands_executed.read().await;
            let commands_failed = *self.commands_failed.read().await;
            
            let heartbeat = AgentHeartbeat {
                connection_id: connection_id.clone(),
                timestamp: chrono::Utc::now().timestamp(),
                status: Some(AgentStatus {
                    ready: true,
                    commands_executed,
                    commands_failed,
                    uptime_seconds: 0,
                    system_info: std::collections::HashMap::new(),
                }),
                current_command_id: None,
            };
            
            let _agent_message = AgentMessage {
                connection_id: connection_id.clone(),
                payload: Some(agent_message::Payload::Heartbeat(heartbeat)),
            };
            
            if *self.status.read().await == ConnectionStatus::Connected {
                *self.status.write().await = ConnectionStatus::Active;
            }
        }
    }
    
    /// Get current connection status
    #[allow(dead_code)]
    pub async fn get_status(&self) -> ConnectionStatus {
        self.status.read().await.clone()
    }
    
    /// Get connection ID
    pub async fn get_connection_id(&self) -> Option<String> {
        self.connection_id.read().await.clone()
    }
    
    /// Increment command counter
    #[allow(dead_code)]
    pub async fn increment_command_executed(&self) {
        *self.commands_executed.write().await += 1;
    }
    
    /// Increment failed command counter
    #[allow(dead_code)]
    pub async fn increment_command_failed(&self) {
        *self.commands_failed.write().await += 1;
    }
    
    /// Handle graceful disconnect
    #[allow(dead_code)]
    pub async fn disconnect(&self, reason: String) {
        info!("Disconnecting: {}", reason);
        *self.status.write().await = ConnectionStatus::Disconnected;
        *self.connection_id.write().await = None;
    }
}

/// Reconnection manager with exponential backoff
#[allow(dead_code)]
pub struct ReconnectionManager {
    initial_delay_seconds: u64,
    max_delay_seconds: u64,
    current_attempt: usize,
}

// Implement reconnection manager
#[allow(dead_code)]
impl ReconnectionManager {
    pub fn new(initial_delay_seconds: u64, max_delay_seconds: u64) -> Self {
        Self {
            initial_delay_seconds,
            max_delay_seconds,
            current_attempt: 0,
        }
    }
    
    /// Calculate next retry delay with exponential backoff
    pub fn next_delay(&mut self) -> Duration {
        self.current_attempt += 1;
        
        let delay = self.initial_delay_seconds * 2_u64.pow(self.current_attempt as u32 - 1);
        let delay = delay.min(self.max_delay_seconds);
        
        Duration::from_secs(delay)
    }
    
    /// Reset attempt counter
    pub fn reset(&mut self) {
        self.current_attempt = 0;
    }
    
    /// Get current attempt number
    pub fn attempts(&self) -> usize {
        self.current_attempt
    }
}

// Unit tests for connection manager
#[cfg(test)]
mod tests {
    use super::*;
    
    #[test]
    fn test_reconnection_backoff() {
        let mut manager = ReconnectionManager::new(1, 60);
        
        assert_eq!(manager.next_delay().as_secs(), 1);
        assert_eq!(manager.next_delay().as_secs(), 2);
        assert_eq!(manager.next_delay().as_secs(), 4);
        assert_eq!(manager.next_delay().as_secs(), 8);
        assert_eq!(manager.next_delay().as_secs(), 16);
        assert_eq!(manager.next_delay().as_secs(), 32);
        assert_eq!(manager.next_delay().as_secs(), 60); // Capped at max
        
        manager.reset();
        assert_eq!(manager.next_delay().as_secs(), 1); // Back to initial
    }
    
    #[tokio::test]
    async fn test_connection_status() {
        let identity = crate::proto::AgentIdentity {
            agent_id: "agent-test".to_string(),
            hostname: "test-host".to_string(),
            os_type: 0,
            os_version: "Test OS".to_string(),
            ip_address: "127.0.0.1".to_string(),
            version: "1.0.0".to_string(),
            capabilities: vec![],
            metadata: std::collections::HashMap::new(),
        };
        
        let manager = ConnectionManager::new(
            "localhost".to_string(),
            50051,
            identity,
            None,
        );
        
        assert_eq!(manager.get_status().await, ConnectionStatus::Disconnected);
        
        *manager.status.write().await = ConnectionStatus::Connected;
        assert_eq!(manager.get_status().await, ConnectionStatus::Connected);
    }
}