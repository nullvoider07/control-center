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
    DisconnectNotice,
};
use crate::registry::ConnectionRegistry;

/// How long a command may wait, both for an agent to take it and for that agent to
/// answer. The queue reaper and the per-call timeout must agree, so they read the
/// same constant rather than repeating the literal.
const COMMAND_TIMEOUT: Duration = Duration::from_secs(30);

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

/// Implementation of stream handler
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
            command_timeout: COMMAND_TIMEOUT,
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

                // Both structures, not just one. A command reaches `pending_commands`
                // only once it has been dispatched; if no agent was attached it is
                // still sitting in `command_queue`, where the next agent's drain loop
                // would pick it up and actuate it — minutes or hours after its caller
                // was told it timed out, and after the recorded event said it failed.
                self.pending_commands.write().await.remove(&command_id);
                self.command_queue
                    .write()
                    .await
                    .retain(|queued| queued.request.id != command_id);

                Err(Status::deadline_exceeded(format!(
                    "Command execution timed out after {:?}",
                    self.command_timeout
                )))
            }
        }
    }
    
    /// A failure response for a command that will never be delivered.
    fn undeliverable(id: String, reason: &str) -> CommandResponse {
        CommandResponse {
            id,
            success: false,
            message: reason.to_string(),
            execution_time_ms: 0,
            mouse_x: None,
            mouse_y: None,
            position_captured: None,
            metadata: std::collections::HashMap::new(),
        }
    }

    /// Fail and discard everything outstanding, so nothing survives to be delivered
    /// to a later agent.
    ///
    /// A command that outlives its caller is worse than a lost one: the operator has
    /// been told it failed, the recorded event says so, and then it actuates anyway
    /// against whatever happens to be on screen when the next agent attaches.
    async fn fail_outstanding(
        command_queue: &Arc<RwLock<VecDeque<QueuedCommand>>>,
        pending_commands: &Arc<RwLock<HashMap<String, PendingCommand>>>,
        reason: &str,
    ) {
        // Drained before sending so no lock is held across the notifications.
        let queued: Vec<QueuedCommand> = {
            let mut queue = command_queue.write().await;
            queue.drain(..).collect()
        };
        let pending: Vec<(String, PendingCommand)> = {
            let mut pending = pending_commands.write().await;
            pending.drain().collect()
        };

        if !queued.is_empty() || !pending.is_empty() {
            warn!(
                "Failing {} queued and {} in-flight command(s): {}",
                queued.len(),
                pending.len(),
                reason
            );
        }

        for command in queued {
            let id = command.request.id.clone();
            let _ = command.response_tx.send(Self::undeliverable(id, reason));
        }
        for (id, command) in pending {
            let _ = command.response_tx.send(Self::undeliverable(id, reason));
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
        let receiver_queue = command_queue.clone();
        let receiver_conn_id = connection_id.clone(); // Clone for first task
        tokio::spawn(async move {
            if let Err(e) = Self::handle_incoming_messages(
                agent_stream,
                receiver_conn_id,
                receiver_registry,
                receiver_pending.clone(),
            ).await {
                error!("Error in incoming message handler: {}", e);
            }
            // The agent is gone. Anything still outstanding has no way to reach it,
            // and must not be left for whichever agent attaches next.
            Self::fail_outstanding(
                &receiver_queue,
                &receiver_pending,
                "Agent disconnected before the command was delivered",
            ).await;
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
        // Ensure registry is cleaned up if the stream ended without an explicit
        // DisconnectNotice from the agent (e.g. network drop, process killed).
        registry.unregister_agent(&connection_id, Some("stream closed".to_string())).await;
        Ok(())
    }
    
    /// Handle outgoing messages to agent.
    /// On each heartbeat tick, also checks the registry disconnect_signal.
    /// If a signal is present (set by DisconnectAgent RPC), it drains the
    /// command queue, sends a graceful DisconnectNotice to the agent, and exits.
    async fn handle_outgoing_messages(
        connection_id: String,
        server_tx: mpsc::Sender<Result<ServerMessage, Status>>,
        registry: Arc<ConnectionRegistry>,
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
                    // --- Check for operator-requested disconnect first ---
                    if let Some(reason) = registry.consume_disconnect_signal().await {
                        info!(
                            "Disconnect signal received for connection {}: {}",
                            connection_id, reason
                        );

                        // Send graceful disconnect notice to agent
                        let notice = DisconnectNotice {
                            reason: reason.clone(),
                            timestamp: chrono::Utc::now().timestamp(),
                            graceful: true,
                        };
                        let _ = server_tx.send(Ok(ServerMessage {
                            payload: Some(server_message::Payload::Disconnect(notice)),
                        })).await;

                        // Fail queued and in-flight commands so CLI callers unblock
                        // immediately and nothing is left for the next agent. The
                        // in-flight arm used to report an empty command id, so a
                        // caller could not match the failure to its request.
                        Self::fail_outstanding(
                            &command_queue,
                            &pending_commands,
                            &format!("Agent disconnected by operator: {}", reason),
                        ).await;

                        registry.unregister_agent(&connection_id, Some(reason)).await;
                        break;
                    }

                    // Reap anything whose caller has stopped waiting. queue_command
                    // clears its own entry on timeout, but only while that future is
                    // alive; a cancelled RPC leaves the entry behind.
                    Self::reap_expired(&command_queue, &pending_commands, COMMAND_TIMEOUT).await;

                    // --- Regular heartbeat ---
                    let heartbeat = ServerHeartbeat {
                        server_id: server_id.clone(),
                        timestamp: chrono::Utc::now().timestamp(),
                        status: Some(ServerStatus {
                            accepting_connections: true,
                            agent_connected: true,
                            total_commands_processed: 0,
                            uptime_seconds: 0,
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
                                    timeout: COMMAND_TIMEOUT,
                                },
                            );
                        }
                        
                        debug!("Command {} sent and awaiting response", command_id);
                    }
                }
            }
        }
        
        info!("Outgoing message handler ended for {}", connection_id);
        // Nothing can be dispatched once this loop stops. Draining here covers the
        // exits the incoming handler does not see — a closed server_tx, or a send
        // failure mid-drain.
        Self::fail_outstanding(
            &command_queue,
            &pending_commands,
            "Agent stream ended before the command was delivered",
        ).await;
        Ok(())
    }
    
    /// Clean up expired commands, queued as well as in flight.
    pub async fn cleanup_expired_commands(&self) {
        Self::reap_expired(&self.command_queue, &self.pending_commands, self.command_timeout).await;
    }

    /// Drop commands whose caller is no longer waiting.
    ///
    /// `queue_command` clears its own entry on timeout, but only if that future is
    /// still running. When the caller's RPC is cancelled — client killed, channel
    /// dropped — nothing runs, and a queued entry would otherwise sit there until an
    /// agent attached and actuated it. Run from the heartbeat tick so the window is
    /// one heartbeat rather than unbounded.
    async fn reap_expired(
        command_queue: &Arc<RwLock<VecDeque<QueuedCommand>>>,
        pending_commands: &Arc<RwLock<HashMap<String, PendingCommand>>>,
        queue_timeout: Duration,
    ) {
        let now = Instant::now();

        let stale: Vec<QueuedCommand> = {
            let mut queue = command_queue.write().await;
            let mut stale = Vec::new();
            while let Some(front) = queue.front() {
                // The queue is in insertion order, so the first live entry ends it.
                if now.duration_since(front.queued_at) <= queue_timeout {
                    break;
                }
                stale.push(queue.pop_front().expect("front() was Some"));
            }
            stale
        };
        for command in stale {
            let waited = now.duration_since(command.queued_at);
            warn!(
                "Discarding command {} never delivered to an agent (queued {:?} ago)",
                command.request.id, waited
            );
            let id = command.request.id.clone();
            let _ = command.response_tx.send(Self::undeliverable(
                id,
                "Command expired before an agent was available",
            ));
        }

        let expired: Vec<(String, PendingCommand)> = {
            let mut pending = pending_commands.write().await;
            let ids: Vec<String> = pending
                .iter()
                .filter(|(_, cmd)| now.duration_since(cmd.sent_at) > cmd.timeout)
                .map(|(id, _)| id.clone())
                .collect();
            ids.into_iter()
                .filter_map(|id| pending.remove(&id).map(|cmd| (id, cmd)))
                .collect()
        };
        for (command_id, command) in expired {
            let elapsed = now.duration_since(command.sent_at);
            warn!("Removing expired command {} (elapsed: {:?})", command_id, elapsed);
            let mut response = Self::undeliverable(
                command_id,
                &format!("Command timed out after {:?}", elapsed),
            );
            response.execution_time_ms = elapsed.as_millis() as i64;
            let _ = command.response_tx.send(response);
        }
    }
    
    /// Get queue statistics
    pub async fn get_queue_stats(&self) -> (usize, usize) {
        let queue_size = self.command_queue.read().await.len();
        let pending_size = self.pending_commands.read().await.len();
        (queue_size, pending_size)
    }
}

/// Unit tests for stream handler
#[cfg(test)]
mod tests {
    use super::*;
    
    #[tokio::test]
    async fn test_queue_command() {
        let registry = Arc::new(ConnectionRegistry::new(true, 100, "127.0.0.1".to_string()));
        let handler = StreamHandler::new(registry, "srv-test".to_string());
        
        let request = CommandRequest {
            id: "cmd-test".to_string(),
            command: "960 540 left".to_string(),
            user_id: Some("test-user".to_string()),
            timestamp: chrono::Utc::now().timestamp(),
            ..Default::default()
        };
        
        // Queue a command (will timeout since no agent)
        let result = handler.queue_command(request).await;

        // Should timeout since no agent to respond
        assert!(result.is_err());

        // And it must not still be queued. It used to be: the timeout path cleared
        // `pending_commands`, which a command that was never dispatched had never
        // entered, leaving it in `command_queue` for the next agent to actuate long
        // after the caller was told it timed out.
        let (queued, pending) = handler.get_queue_stats().await;
        assert_eq!((queued, pending), (0, 0), "the timed-out command is still queued");
    }

    fn queued(id: &str, at: Instant) -> (QueuedCommand, oneshot::Receiver<CommandResponse>) {
        let (response_tx, response_rx) = oneshot::channel();
        (
            QueuedCommand {
                request: CommandRequest {
                    id: id.to_string(),
                    human_command: "press #r".to_string(),
                    argv: vec!["__write__".to_string()],
                    ..Default::default()
                },
                response_tx,
                queued_at: at,
            },
            response_rx,
        )
    }

    #[tokio::test]
    async fn the_reaper_discards_commands_whose_caller_stopped_waiting() {
        // A cancelled RPC runs no timeout branch, so nothing would clear the entry.
        let registry = Arc::new(ConnectionRegistry::new(true, 100, "127.0.0.1".to_string()));
        let handler = StreamHandler::new(registry, "srv-test".to_string());

        let (command, response_rx) = queued("cmd-abandoned", Instant::now());
        handler.command_queue.write().await.push_back(command);

        StreamHandler::reap_expired(
            &handler.command_queue,
            &handler.pending_commands,
            Duration::ZERO,
        ).await;

        assert_eq!(handler.get_queue_stats().await.0, 0, "the stale command survived");
        let response = response_rx.await.expect("the caller was never answered");
        assert!(!response.success);
        assert_eq!(response.id, "cmd-abandoned", "the failure must name its request");
    }

    #[tokio::test]
    async fn a_live_command_is_not_reaped() {
        let registry = Arc::new(ConnectionRegistry::new(true, 100, "127.0.0.1".to_string()));
        let handler = StreamHandler::new(registry, "srv-test".to_string());

        let (command, _rx) = queued("cmd-fresh", Instant::now());
        handler.command_queue.write().await.push_back(command);

        StreamHandler::reap_expired(
            &handler.command_queue,
            &handler.pending_commands,
            COMMAND_TIMEOUT,
        ).await;

        assert_eq!(handler.get_queue_stats().await.0, 1, "a live command was discarded");
    }

    #[tokio::test]
    async fn a_disconnect_leaves_nothing_for_the_next_agent() {
        let registry = Arc::new(ConnectionRegistry::new(true, 100, "127.0.0.1".to_string()));
        let handler = StreamHandler::new(registry, "srv-test".to_string());

        let (first, first_rx) = queued("cmd-1", Instant::now());
        let (second, second_rx) = queued("cmd-2", Instant::now());
        handler.command_queue.write().await.push_back(first);
        handler.command_queue.write().await.push_back(second);

        StreamHandler::fail_outstanding(
            &handler.command_queue,
            &handler.pending_commands,
            "Agent disconnected before the command was delivered",
        ).await;

        assert_eq!(handler.get_queue_stats().await, (0, 0));
        for (rx, id) in [(first_rx, "cmd-1"), (second_rx, "cmd-2")] {
            let response = rx.await.expect("caller was never answered");
            assert!(!response.success);
            assert_eq!(response.id, id, "the failure must name its request");
        }
    }
    
    #[tokio::test]
    async fn test_queue_stats() {
        let registry = Arc::new(ConnectionRegistry::new(true, 100, "127.0.0.1".to_string()));
        let handler = StreamHandler::new(registry, "srv-test".to_string());
        
        let (queued, pending) = handler.get_queue_stats().await;
        assert_eq!(queued, 0);
        assert_eq!(pending, 0);
    }
}