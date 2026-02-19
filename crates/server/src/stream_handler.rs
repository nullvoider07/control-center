// crates/server/src/stream_handler.rs
// Stream Handler - Manages bidirectional streaming with agents

use tokio::sync::{mpsc, oneshot, RwLock};
use tokio_stream::wrappers::ReceiverStream;
use tonic::{Status, Streaming};
use tracing::{info, warn, error, debug};
use std::collections::{HashMap, VecDeque};
use std::sync::Arc;
use std::time::{Duration, Instant};

use crate::proto::{
    AgentMessage, ServerMessage, CommandRequest, CommandResponse,
    agent_message, server_message, ServerHeartbeat, ServerStatus,
};
use crate::registry::ConnectionRegistry;

/// Queued command waiting to be sent to agent
struct QueuedCommand {
    request: CommandRequest,
    response_tx: oneshot::Sender<CommandResponse>,
    queued_at: Instant,
}

/// Pending command waiting for response from agent
struct PendingCommand {
    _request: CommandRequest,
    response_tx: oneshot::Sender<CommandResponse>,
    sent_at: Instant,
    timeout: Duration,
}

/// Stream handler manages bidirectional communication with agent
pub struct StreamHandler {
    /// Connection registry
    registry: Arc<ConnectionRegistry>,
    
    /// Server identity
    server_id: String,
    
    /// Command queue (commands waiting to be sent)
    command_queue: Arc<RwLock<VecDeque<QueuedCommand>>>,
    
    /// Pending commands (waiting for responses)
    pending_commands: Arc<RwLock<HashMap<String, PendingCommand>>>,
    
    /// Channel to notify about new commands
    new_command_notify: Arc<tokio::sync::Notify>,
    
    /// Command timeout duration
    command_timeout: Duration,
}

impl StreamHandler {
    /// Create new stream handler
    pub fn new(
        registry: Arc<ConnectionRegistry>,
        server_id: String,
    ) -> Self {
        Self {
            registry,
            server_id,
            command_queue: Arc::new(RwLock::new(VecDeque::new())),
            pending_commands: Arc::new(RwLock::new(HashMap::new())),
            new_command_notify: Arc::new(tokio::sync::Notify::new()),
            command_timeout: Duration::from_secs(30),
        }
    }
    
    /// Queue a command for execution
    pub async fn queue_command(
        &self,
        request: CommandRequest,
    ) -> Result<CommandResponse, Status> {
        let command_id = request.id.clone();
        
        // Create oneshot channel for response
        let (response_tx, response_rx) = oneshot::channel();
        
        // Queue the command
        {
            let mut queue = self.command_queue.write().await;
            queue.push_back(QueuedCommand {
                request,
                response_tx,
                queued_at: Instant::now(),
            });
            debug!("Command {} queued (queue size: {})", command_id, queue.len());
        }
        
        // Notify sender task
        self.new_command_notify.notify_one();
        
        // Wait for response with timeout
        match tokio::time::timeout(self.command_timeout, response_rx).await {
            Ok(Ok(response)) => {
                debug!("Command {} completed successfully", command_id);
                Ok(response)
            }
            Ok(Err(_)) => {
                error!("Command {} channel closed unexpectedly", command_id);
                Err(Status::internal("Command channel closed"))
            }
            Err(_) => {
                error!("Command {} timed out after {:?}", command_id, self.command_timeout);
                
                // Clean up pending command
                self.pending_commands.write().await.remove(&command_id);
                
                Err(Status::deadline_exceeded(format!(
                    "Command execution timed out after {:?}",
                    self.command_timeout
                )))
            }
        }
    }
    
    /// Handle agent stream (bidirectional)
    pub async fn handle_agent_stream(
        &self,
        connection_id: String,
        agent_stream: Streaming<AgentMessage>,
    ) -> Result<ReceiverStream<Result<ServerMessage, Status>>, Status> {
        info!("Starting bidirectional stream for connection: {}", connection_id);
        
        // Create channels for communication
        let (server_tx, server_rx) = mpsc::channel(100);
        
        // Clone Arcs for tasks
        let registry = self.registry.clone();
        let pending_commands = self.pending_commands.clone();
        let command_queue = self.command_queue.clone();
        let new_command_notify = self.new_command_notify.clone();
        let server_id = self.server_id.clone();
        
        // Spawn task to receive messages from agent
        let receiver_registry = registry.clone();
        let receiver_pending = pending_commands.clone();
        let receiver_conn_id = connection_id.clone(); // Clone for first task
        tokio::spawn(async move {
            if let Err(e) = Self::handle_incoming_messages(
                agent_stream,
                receiver_conn_id,
                receiver_registry,
                receiver_pending,
            ).await {
                error!("Error in incoming message handler: {}", e);
            }
        });
        
        // Spawn task to send messages to agent
        let sender_server_tx = server_tx.clone();
        tokio::spawn(async move {
            if let Err(e) = Self::handle_outgoing_messages(
                connection_id, // Use original for second task
                sender_server_tx,
                registry,
                command_queue,
                pending_commands,
                new_command_notify,
                server_id,
            ).await {
                error!("Error in outgoing message handler: {}", e);
            }
        });
        
        Ok(ReceiverStream::new(server_rx))
    }
    
    /// Handle incoming messages from agent
    async fn handle_incoming_messages(
        mut agent_stream: Streaming<AgentMessage>,
        connection_id: String,
        registry: Arc<ConnectionRegistry>,
        pending_commands: Arc<RwLock<HashMap<String, PendingCommand>>>,
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        info!("Incoming message handler started for {}", connection_id);
        
        while let Some(result) = agent_stream.message().await? {
            match result.payload {
                Some(agent_message::Payload::CommandResponse(response)) => {
                    let command_id = response.id.clone();
                    debug!("Received command response for: {}", command_id);
                    
                    // Find and remove pending command
                    let pending = {
                        let mut pending = pending_commands.write().await;
                        pending.remove(&command_id)
                    };
                    
                    if let Some(pending) = pending {
                        let execution_time = pending.sent_at.elapsed();
                        debug!(
                            "Command {} completed in {:?} (success: {})",
                            command_id,
                            execution_time,
                            response.success
                        );
                        
                        // Send response back to waiting execute_command
                        if pending.response_tx.send(response).is_err() {
                            warn!("Failed to send response for command {}", command_id);
                        }
                        
                        // Update registry
                        registry.increment_command_count(&connection_id).await;
                    } else {
                        warn!("Received response for unknown command: {}", command_id);
                    }
                }
                
                Some(agent_message::Payload::Heartbeat(heartbeat)) => {
                    debug!("Received heartbeat from agent");
                    
                    // Update registry
                    registry.update_heartbeat(&connection_id).await;
                    
                    // Log status
                    if let Some(status) = heartbeat.status {
                        debug!(
                            "Agent status - Ready: {}, Commands: {}, Uptime: {}s",
                            status.ready,
                            status.commands_executed,
                            status.uptime_seconds
                        );
                    }
                }
                
                Some(agent_message::Payload::StatusUpdate(status)) => {
                    debug!("Received status update from agent");
                    
                    // Update registry with latest agent status
                    registry.update_heartbeat(&connection_id).await;
                    
                    // Log detailed status
                    debug!(
                        "Agent status update - Ready: {}, Commands: {} (failed: {}), Uptime: {}s",
                        status.ready,
                        status.commands_executed,
                        status.commands_failed,
                        status.uptime_seconds
                    );
                }
                
                Some(agent_message::Payload::Disconnect(disconnect)) => {
                    info!(
                        "Agent requested disconnect: {}",
                        disconnect.reason
                    );
                    break;
                }
                
                None => {
                    warn!("Received message with no payload");
                }
            }
        }
        
        info!("Incoming message handler ended for {}", connection_id);
        Ok(())
    }
    
    /// Handle outgoing messages to agent
    async fn handle_outgoing_messages(
        connection_id: String,
        server_tx: mpsc::Sender<Result<ServerMessage, Status>>,
        _registry: Arc<ConnectionRegistry>,
        command_queue: Arc<RwLock<VecDeque<QueuedCommand>>>,
        pending_commands: Arc<RwLock<HashMap<String, PendingCommand>>>,
        new_command_notify: Arc<tokio::sync::Notify>,
        server_id: String,
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        info!("Outgoing message handler started for {}", connection_id);
        
        // Heartbeat interval
        let mut heartbeat_interval = tokio::time::interval(Duration::from_secs(30));
        
        loop {
            tokio::select! {
                // Send heartbeat
                _ = heartbeat_interval.tick() => {
                    let heartbeat = ServerHeartbeat {
                        server_id: server_id.clone(),
                        timestamp: chrono::Utc::now().timestamp(),
                        status: Some(ServerStatus {
                            accepting_connections: true,
                            agent_connected: true,
                            total_commands_processed: 0, // TODO: Track actual count
                            uptime_seconds: 0, // TODO: Track actual uptime
                        }),
                    };
                    
                    let server_msg = ServerMessage {
                        payload: Some(server_message::Payload::Heartbeat(heartbeat)),
                    };
                    
                    if server_tx.send(Ok(server_msg)).await.is_err() {
                        info!("Agent stream closed, stopping heartbeat");
                        break;
                    }
                    
                    debug!("Sent heartbeat to agent");
                }
                
                // Send commands
                _ = new_command_notify.notified() => {
                    // Process all queued commands
                    loop {
                        let queued = {
                            let mut queue = command_queue.write().await;
                            queue.pop_front()
                        };
                        
                        let queued = match queued {
                            Some(q) => q,
                            None => break, // No more commands
                        };
                        
                        let command_id = queued.request.id.clone();
                        let wait_time = queued.queued_at.elapsed();
                        
                        debug!(
                            "Sending command {} to agent (waited {:?})",
                            command_id,
                            wait_time
                        );
                        
                        // Build server message
                        let server_msg = ServerMessage {
                            payload: Some(server_message::Payload::CommandRequest(
                                queued.request.clone()
                            )),
                        };
                        
                        // Send to agent
                        if server_tx.send(Ok(server_msg)).await.is_err() {
                            error!("Failed to send command {}, agent stream closed", command_id);
                            
                            // Return error to waiting CLI
                            let _ = queued.response_tx.send(CommandResponse {
                                id: command_id.clone(),
                                success: false,
                                message: "Agent stream closed".to_string(),
                                execution_time_ms: 0,
                                mouse_x: None,
                                mouse_y: None,
                                position_captured: None,
                                metadata: std::collections::HashMap::new(),
                            });
                            
                            break;
                        }
                        
                        // Add to pending commands
                        {
                            let mut pending = pending_commands.write().await;
                            pending.insert(
                                command_id.clone(),
                                PendingCommand {
                                    _request: queued.request,
                                    response_tx: queued.response_tx,
                                    sent_at: Instant::now(),
                                    timeout: Duration::from_secs(30),
                                },
                            );
                        }
                        
                        debug!("Command {} sent and awaiting response", command_id);
                    }
                }
            }
        }
        
        info!("Outgoing message handler ended for {}", connection_id);
        Ok(())
    }
    
    /// Clean up expired pending commands
    pub async fn cleanup_expired_commands(&self) {
        let mut pending = self.pending_commands.write().await;
        let now = Instant::now();
        
        // Collect expired command IDs
        let expired: Vec<String> = pending
            .iter()
            .filter_map(|(command_id, pending_cmd)| {
                let elapsed = now.duration_since(pending_cmd.sent_at);
                if elapsed > pending_cmd.timeout {
                    Some(command_id.clone())
                } else {
                    None
                }
            })
            .collect();
        
        // Remove expired commands and send timeout responses
        for command_id in expired {
            if let Some(pending_cmd) = pending.remove(&command_id) {
                let elapsed = now.duration_since(pending_cmd.sent_at);
                
                warn!(
                    "Removing expired command {} (elapsed: {:?})",
                    command_id,
                    elapsed
                );
                
                // Send timeout error (can now move response_tx)
                let _ = pending_cmd.response_tx.send(CommandResponse {
                    id: command_id.clone(),
                    success: false,
                    message: format!("Command timed out after {:?}", elapsed),
                    execution_time_ms: elapsed.as_millis() as i64,
                    mouse_x: None,
                    mouse_y: None,
                    position_captured: None,
                    metadata: std::collections::HashMap::new(),
                });
            }
        }
    }
    
    /// Get queue statistics
    pub async fn get_queue_stats(&self) -> (usize, usize) {
        let queue_size = self.command_queue.read().await.len();
        let pending_size = self.pending_commands.read().await.len();
        (queue_size, pending_size)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    
    #[tokio::test]
    async fn test_queue_command() {
        let registry = Arc::new(ConnectionRegistry::new(true, 100));
        let handler = StreamHandler::new(registry, "srv-test".to_string());
        
        let request = CommandRequest {
            id: "cmd-test".to_string(),
            command: "960 540 left".to_string(),
            user_id: Some("test-user".to_string()),
            timestamp: chrono::Utc::now().timestamp(),
        };
        
        // Queue a command (will timeout since no agent)
        let result = handler.queue_command(request).await;
        
        // Should timeout since no agent to respond
        assert!(result.is_err());
    }
    
    #[tokio::test]
    async fn test_queue_stats() {
        let registry = Arc::new(ConnectionRegistry::new(true, 100));
        let handler = StreamHandler::new(registry, "srv-test".to_string());
        
        let (queued, pending) = handler.get_queue_stats().await;
        assert_eq!(queued, 0);
        assert_eq!(pending, 0);
    }
}