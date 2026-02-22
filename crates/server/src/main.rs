// crates/server/src/main.rs
// gRPC Server with JWT Token Validation

use tonic::{transport::Server, Request, Response, Status, metadata::MetadataMap};
use std::sync::Arc;
use tracing::{info, warn, debug};
use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};
use jsonwebtoken::{decode, DecodingKey, Validation, Algorithm};
mod identity;
mod registry;
mod monitoring;
mod stream_handler;

// Protobuf generated code
mod proto {
    tonic::include_proto!("control_center");
}

// Re-exporting for easier access
use proto::{
    control_service_server::{ControlService, ControlServiceServer},
};

// Single canonical Claims definition — shared across all crates.
// Do NOT define a local Claims struct here.
use control_center_common::Claims;

/// Rate limiter for preventing abuse
struct RateLimiter {
    requests: HashMap<String, Vec<u64>>,
    max_requests: usize,
    window_secs: u64,
}

// Simple in-memory rate limiter implementation
impl RateLimiter {
    fn new(max_requests: usize, window_secs: u64) -> Self {
        Self {
            requests: HashMap::new(),
            max_requests,
            window_secs,
        }
    }

    // Check if the user has exceeded the rate limit
    fn check_rate_limit(&mut self, user_id: &str) -> bool {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs();

        let requests = self.requests.entry(user_id.to_string()).or_insert_with(Vec::new);
        
        // Remove old requests outside the window
        requests.retain(|&timestamp| now - timestamp < self.window_secs);

        if requests.len() >= self.max_requests {
            return false;
        }

        requests.push(now);
        true
    }

    // Cleanup old entries to prevent memory bloat
    #[allow(dead_code)]
    fn cleanup_old_entries(&mut self) {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs();

        self.requests.retain(|_, timestamps| {
            timestamps.retain(|&t| now - t < self.window_secs);
            !timestamps.is_empty()
        });
    }
}

/// Main service implementation
pub struct ControlCenterService {
    jwt_secret: String,
    jwt_audience: String,
    jwt_issuer: String,
    registry: Arc<registry::ConnectionRegistry>,
    server_identity: identity::ServerIdentityConfig,
    monitoring: Arc<monitoring::MonitoringHandler>,
    stream_handler: Arc<stream_handler::StreamHandler>,
    listen_address: String,
    rate_limiter: Arc<tokio::sync::RwLock<RateLimiter>>,
    metrics: Arc<tokio::sync::RwLock<ServerMetrics>>,
}

// Metrics structure for monitoring server performance and security
#[derive(Default, Clone)]
struct ServerMetrics {
    total_requests: u64,
    successful_requests: u64,
    failed_requests: u64,
    auth_failures: u64,
    rate_limit_hits: u64,
}

// Associated functions for ControlCenterService
impl ControlCenterService {
    pub fn new(
        jwt_secret: String,
        jwt_audience: String,
        jwt_issuer: String,
        registry: Arc<registry::ConnectionRegistry>,
        server_identity: identity::ServerIdentityConfig,
        monitoring: Arc<monitoring::MonitoringHandler>,
        stream_handler: Arc<stream_handler::StreamHandler>,
        listen_address: String,
    ) -> Self {
        Self {
            jwt_secret,
            jwt_audience,
            jwt_issuer,
            registry,
            server_identity,
            monitoring,
            stream_handler,
            listen_address,
            rate_limiter: Arc::new(tokio::sync::RwLock::new(RateLimiter::new(100, 60))),
            metrics: Arc::new(tokio::sync::RwLock::new(ServerMetrics::default())),
        }
    }
    
    /// Validate JWT token from metadata
    async fn validate_token(&self, metadata: &MetadataMap) -> Result<Claims, Status> {
        // Extract Authorization header
        let auth_header = metadata
            .get("authorization")
            .ok_or_else(|| {
                warn!("Missing authorization header");
                Status::unauthenticated("Missing authorization token")
            })?
            .to_str()
            .map_err(|_| {
                warn!("Invalid authorization header format");
                Status::unauthenticated("Invalid authorization header")
            })?;

        // Extract Bearer token
        let token = auth_header
            .strip_prefix("Bearer ")
            .ok_or_else(|| {
                warn!("Authorization header not in Bearer format");
                Status::unauthenticated("Invalid authorization format. Expected: Bearer <token>")
            })?;

        // Validate JWT
        let mut validation = Validation::new(Algorithm::HS256);
        validation.set_audience(&[&self.jwt_audience]);
        validation.set_issuer(&[&self.jwt_issuer]);

        let token_data = match decode::<Claims>(
            token,
            &DecodingKey::from_secret(self.jwt_secret.as_bytes()),
            &validation,
        ) {
            Ok(data) => data,
            Err(e) => {
                warn!("JWT validation failed: {}", e);
                return Err(Status::unauthenticated(format!("Invalid token: {}", e)));
            }
        };

        debug!("Token validated for user: {}", token_data.claims.sub);
        Ok(token_data.claims)
    }

    async fn check_rate_limit(&self, user_id: &str) -> Result<(), Status> {
        let mut limiter = self.rate_limiter.write().await;
        
        if !limiter.check_rate_limit(user_id) {
            let mut metrics = self.metrics.write().await;
            metrics.rate_limit_hits += 1;
            
            warn!("Rate limit exceeded for user: {}", user_id);
            return Err(Status::new(
                tonic::Code::ResourceExhausted,
                "Rate limit exceeded. Please try again later.",
            ));
        }

        Ok(())
    }
}

// Implementation of the ControlService trait
#[tonic::async_trait]
impl ControlService for ControlCenterService {
    async fn register_agent(
        &self,
        request: Request<proto::RegistrationRequest>,
    ) -> Result<Response<proto::RegistrationResponse>, Status> {
        let registration_req = request.into_inner();
        
        let agent_identity = registration_req.agent_identity
            .ok_or_else(|| Status::invalid_argument("Agent identity required"))?;
        
        info!(
            "Agent registration request: {} (Hostname: {}, IP: {})",
            agent_identity.agent_id,
            agent_identity.hostname,
            agent_identity.ip_address
        );
        
        // Generate connection ID
        let connection_id = format!("conn-{}", uuid::Uuid::new_v4());
        
        // Register in registry (1:1 enforcement here)
        match self.registry.register_agent(
            &agent_identity,
            connection_id.clone(),
            agent_identity.ip_address.clone()
        ).await {
            Ok(_) => {
                let server_identity = identity::build_server_identity(
                    &self.server_identity,
                    self.listen_address.clone(),
                    env!("CARGO_PKG_VERSION").to_string(),
                );
                
                Ok(Response::new(proto::RegistrationResponse {
                    success: true,
                    message: "Agent registered successfully".to_string(),
                    connection_id,
                    server_identity: Some(server_identity),
                    connection_metadata: None,
                }))
            }
            Err(e) => {
                Err(Status::resource_exhausted(e))
            }
        }
    }
    
    /// Agent stream (bidirectional)
    type AgentStreamStream = tokio_stream::wrappers::ReceiverStream<Result<proto::ServerMessage, Status>>;
    
    async fn agent_stream(
        &self,
        request: Request<tonic::Streaming<proto::AgentMessage>>,
    ) -> Result<Response<Self::AgentStreamStream>, Status> {
        info!("Agent stream connection received");
        
        // Get agent stream from request
        let agent_stream = request.into_inner();
        
        // Get the current connection (we're in single-agent mode)
        let connection_id = {
            let current = self.registry.get_current_connection().await;
            match current {
                Some(agent) => agent.connection_id.clone(),
                None => {
                    return Err(Status::failed_precondition("No agent registered"));
                }
            }
        };
        
        info!("Starting bidirectional stream for connection: {}", connection_id);
        
        // Handle the stream
        let response_stream = self.stream_handler
            .handle_agent_stream(connection_id, agent_stream)
            .await?;
        
        info!("Bidirectional stream established successfully");
        
        Ok(Response::new(response_stream))
    }
    
    /// Query connections (monitoring API)
    async fn query_connections(
        &self,
        request: Request<proto::ConnectionQuery>,
    ) -> Result<Response<proto::ConnectionStatusResponse>, Status> {
        self.monitoring.handle_connection_query(request).await
    }
    
    /// Query servers (monitoring API)
    async fn query_servers(
        &self,
        request: Request<proto::ServerStatusQuery>,
    ) -> Result<Response<proto::ServerStatusResponse>, Status> {
        self.monitoring.handle_server_status_query(request).await
    }
    
    /// Get server identity
    async fn get_server_identity(
        &self,
        _request: Request<proto::InfoRequest>,
    ) -> Result<Response<proto::ServerIdentity>, Status> {
        let server_identity = identity::build_server_identity(
            &self.server_identity,
            "0.0.0.0:50051".to_string(), // TODO: Get actual listen address
            env!("CARGO_PKG_VERSION").to_string(),
        );
        
        Ok(Response::new(server_identity))
    }
    
    /// Legacy execute (compatibility)
    async fn execute(
        &self,
        request: Request<proto::ExecuteRequest>,
    ) -> Result<Response<proto::ExecuteResponse>, Status> {
        // Clone metadata before consuming request
        let metadata = request.metadata().clone();
        let claims = self.validate_token(&metadata).await?;
        self.check_rate_limit(&claims.sub).await?;
        
        // Convert ExecuteRequest to CommandRequest and call execute_command
        let exec_req = request.into_inner();
        
        let cmd_request = proto::CommandRequest {
            id: exec_req.id.clone(),
            command: exec_req.command,
            user_id: Some(exec_req.user_id.clone().unwrap_or_else(|| claims.sub.clone())),
            timestamp: chrono::Utc::now().timestamp(),
        };

        let mut new_request = Request::new(cmd_request);
        *new_request.metadata_mut() = metadata;
        
        let cmd_response = self.execute_command(new_request).await?;
        let response = cmd_response.into_inner();
        
        Ok(Response::new(proto::ExecuteResponse {
            id: response.id,
            success: response.success,
            message: response.message,
            execution_time_ms: response.execution_time_ms,
            mouse_x: response.mouse_x,
            mouse_y: response.mouse_y,
            position_captured: response.position_captured,
        }))
    }
    
    /// Ping (health check)
    async fn ping(
        &self,
        _request: Request<proto::PingRequest>,
    ) -> Result<Response<proto::PongResponse>, Status> {
        Ok(Response::new(proto::PongResponse {
            alive: true,
        }))
    }
    
    /// Get agent info
    async fn get_agent_info(
        &self,
        request: Request<proto::AgentInfoRequest>,
    ) -> Result<Response<proto::AgentInfo>, Status> {
        let claims = self.validate_token(request.metadata()).await?;
        self.check_rate_limit(&claims.sub).await?;
        let agent = self.registry.get_current_connection().await
            .ok_or_else(|| Status::unavailable("No agent connected"))?;
        let mut metrics = self.metrics.write().await;
        metrics.total_requests += 1;
        metrics.successful_requests += 1;
        
        info!("Agent info requested by user: {}", claims.sub);
        
        Ok(Response::new(proto::AgentInfo {
            os: agent.os_type,
            os_version: agent.os_version.clone(),
            capabilities: agent.capabilities.clone(),
            agent_version: agent.agent_version.clone(),
        }))
    }
    
    /// Execute command - forward to agent via bidirectional stream
    async fn execute_command(
        &self,
        request: Request<proto::CommandRequest>,
    ) -> Result<Response<proto::CommandResponse>, Status> {
        
        // Validate JWT token
        let start_time = std::time::Instant::now();
        let claims = self.validate_token(request.metadata()).await?;

        self.check_rate_limit(&claims.sub).await?;
        
        // Check if agent is connected
        if !self.registry.is_agent_connected().await {
            let mut metrics = self.metrics.write().await;
            metrics.total_requests += 1;
            metrics.failed_requests += 1;
            return Err(Status::unavailable("No agent connected to server"));
        }
        
        let cmd_req = request.into_inner();
        let command_id = cmd_req.id.clone();
        let command = cmd_req.command.clone();
        
        debug!(
            "Executing command {} via agent: {}",
            command_id,
            command
        );
        
        // Queue command and wait for response
        let response = self.stream_handler.queue_command(cmd_req).await;
        let execution_time = start_time.elapsed();
        let mut metrics = self.metrics.write().await;
        metrics.total_requests += 1;

        match &response {
            Ok(resp) => {
                if resp.success {
                    metrics.successful_requests += 1;
                    // Single clean log line matching the agent's JSON format
                    info!(
                        "{{\"action\": \"{}\", \"time_taken\": \"{}ms\"}}",
                        resp.message,
                        execution_time.as_millis()
                    );
                } else {
                    metrics.failed_requests += 1;
                }
            }
            Err(e) => {
                metrics.failed_requests += 1;
                warn!(
                    "Command {} failed (error: {}, time: {:?}, user: {})",
                    command_id,
                    e,
                    execution_time,
                    claims.sub
                );
            }
        }
        
        Ok(Response::new(response?))
    }
    
    /// Monitor connection status stream
    type MonitorConnectionStream = tokio_stream::wrappers::ReceiverStream<
        Result<proto::ConnectionStatus, Status>
    >;
    
    async fn monitor_connection(
        &self,
        request: Request<proto::MonitorRequest>,
    ) -> Result<Response<Self::MonitorConnectionStream>, Status> {
        let claims = self.validate_token(request.metadata()).await?;
        self.check_rate_limit(&claims.sub).await?;
        let registry = self.registry.clone();
        info!("Connection monitoring started by user: {}", claims.sub);
        
        info!("Connection monitoring started");
        
        let (tx, rx) = tokio::sync::mpsc::channel(32);
        
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(tokio::time::Duration::from_secs(5)).await;
                
                let connected = registry.is_agent_connected().await;
                
                let status = proto::ConnectionStatus {
                    connected,
                    message: if connected {
                        "Connected to agent".to_string()
                    } else {
                        "No agent connected".to_string()
                    },
                    timestamp: chrono::Utc::now().timestamp(),
                };
                
                if tx.send(Ok(status)).await.is_err() {
                    break;
                }
            }
        });
        
        Ok(Response::new(tokio_stream::wrappers::ReceiverStream::new(rx)))
    }

    async fn get_metrics(
        &self,
        request: Request<proto::MetricsRequest>,
    ) -> Result<Response<proto::MetricsResponse>, Status> {
        // Validate JWT (metrics scope required)
        let claims = self.validate_token(request.metadata()).await?;
        
        // Check if user has metrics scope
        if !claims.scopes.contains(&"metrics".to_string()) {
            return Err(Status::permission_denied(
                "User does not have permission to view metrics"
            ));
        }
        
        info!("Metrics requested by user: {}", claims.sub);
        
        let metrics = self.metrics.read().await.clone();
        
        // Build Prometheus-style metrics response
        let metrics_text = format!(
            "# HELP control_center_requests_total Total number of requests\n\
             # TYPE control_center_requests_total counter\n\
             control_center_requests_total {}\n\
             \n\
             # HELP control_center_requests_success Successful requests\n\
             # TYPE control_center_requests_success counter\n\
             control_center_requests_success {}\n\
             \n\
             # HELP control_center_requests_failed Failed requests\n\
             # TYPE control_center_requests_failed counter\n\
             control_center_requests_failed {}\n\
             \n\
             # HELP control_center_auth_failures Authentication failures\n\
             # TYPE control_center_auth_failures counter\n\
             control_center_auth_failures {}\n\
             \n\
             # HELP control_center_rate_limit_hits Rate limit violations\n\
             # TYPE control_center_rate_limit_hits counter\n\
             control_center_rate_limit_hits {}\n",
            metrics.total_requests,
            metrics.successful_requests,
            metrics.failed_requests,
            metrics.auth_failures,
            metrics.rate_limit_hits,
        );
        
        Ok(Response::new(proto::MetricsResponse {
            metrics: metrics_text,
            timestamp: chrono::Utc::now().timestamp(),
        }))
    }

    /// Forcefully disconnect the currently connected agent.
    /// Sets a signal that the stream handler picks up on its next heartbeat
    /// tick (within 30 s), sends a graceful DisconnectNotice, then cleans up.
    /// Requires a valid JWT token; any authenticated user may call this.
    async fn disconnect_agent(
        &self,
        request: Request<proto::DisconnectAgentRequest>,
    ) -> Result<Response<proto::DisconnectAgentResponse>, Status> {
        let claims = self.validate_token(request.metadata()).await?;
        self.check_rate_limit(&claims.sub).await?;

        let req = request.into_inner();
        let reason = if req.reason.is_empty() {
            format!("Disconnected by operator: {}", claims.sub)
        } else {
            req.reason.clone()
        };

        info!(
            "DisconnectAgent requested by user '{}' — reason: {}",
            claims.sub, reason
        );

        let (found, conn_id) = self.registry.request_disconnect(reason.clone()).await;

        if found {
            info!("Disconnect signal set for connection: {}", conn_id);
            Ok(Response::new(proto::DisconnectAgentResponse {
                success: true,
                message: format!("Disconnect signal sent to agent (connection: {})", conn_id),
                disconnected_connection_id: conn_id,
            }))
        } else {
            Ok(Response::new(proto::DisconnectAgentResponse {
                success: false,
                message: "No agent is currently connected".to_string(),
                disconnected_connection_id: String::new(),
            }))
        }
    }

    /// Return connection history stored in the registry.
    /// No JWT required — this is read-only operational metadata.
    async fn get_connection_history(
        &self,
        request: Request<proto::ConnectionHistoryRequest>,
    ) -> Result<Response<proto::ConnectionHistoryResponse>, Status> {
        let req = request.into_inner();

        // Default 50, clamp to max 500
        let limit = req.limit
            .filter(|&l| l > 0)
            .map(|l| l.min(500) as usize)
            .unwrap_or(50);

        let history = self.registry.get_history(Some(limit)).await;

        let connections: Vec<proto::HistoricalConnection> = history
            .into_iter()
            .map(|h| proto::HistoricalConnection {
                connection_id: h.connection_id,
                agent_id: h.agent_id,
                agent_hostname: h.agent_hostname,
                agent_ip: h.agent_ip,
                os_type: h.os_type,
                os_version: h.os_version,
                capabilities: h.capabilities,
                server_ip: h.server_ip,
                connected_at: h.connected_at,
                disconnected_at: h.disconnected_at,
                commands_executed: h.commands_executed,
                disconnect_reason: h.disconnect_reason,
            })
            .collect();

        let total_count = connections.len() as i32;
        info!("GetConnectionHistory: returning {} records", total_count);

        Ok(Response::new(proto::ConnectionHistoryResponse {
            connections,
            total_count,
        }))
    }
}

// Main function to start the gRPC server
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Initialize logging
    tracing_subscriber::fmt()
        .with_target(false)
        .with_thread_ids(true)
        .with_level(true)
        .init();

    info!("Control Center Server v1.0.0");
    info!("Starting gRPC server with JWT authentication...");

    // Load configuration from environment
    let jwt_secret = std::env::var("JWT_SECRET")
        .expect("JWT_SECRET environment variable must be set");

    if jwt_secret.len() < 32 {
        panic!("JWT_SECRET must be at least 32 characters");
    }

    let jwt_audience = std::env::var("JWT_AUDIENCE")
        .unwrap_or_else(|_| "control-center".to_string());

    let jwt_issuer = std::env::var("JWT_ISSUER")
        .unwrap_or_else(|_| "control-center-auth".to_string());

    info!("JWT Audience: {}", jwt_audience);
    info!("JWT Issuer: {}", jwt_issuer);

    let server_identity = identity::load_or_generate_identity();
    info!("Server ID: {}", server_identity.server_id);
    info!("Network: {}", server_identity.network);

    // Create connection registry (single-agent mode by default)
    let single_agent_mode = std::env::var("SINGLE_AGENT_MODE")
        .unwrap_or_else(|_| "true".to_string())
        .parse()
        .unwrap_or(true);

    let registry = Arc::new(registry::ConnectionRegistry::new(single_agent_mode, 100));

    // Create monitoring handler
    let monitoring_handler = Arc::new(monitoring::MonitoringHandler::new(
        registry.clone(),
        server_identity.clone(),
        env!("CARGO_PKG_VERSION").to_string(),
    ));

    // Create stream handler (Phase 2)
    let stream_handler = Arc::new(stream_handler::StreamHandler::new(
        registry.clone(),
        server_identity.server_id.clone(),
    ));

    let addr: std::net::SocketAddr = std::env::var("SERVER_ADDR")
        .unwrap_or_else(|_| "0.0.0.0:50051".to_string())
        .parse()?;

    let listen_address = addr.to_string();
    info!("Server will listen on {}", addr);

    // Create service
    let service = ControlCenterService::new(
        jwt_secret.clone(),
        jwt_audience.clone(),
        jwt_issuer.clone(),
        registry,
        server_identity,
        monitoring_handler,
        stream_handler,
        listen_address,
    );

    info!("Server will listen on {}", addr);
    info!("Ready to accept authenticated requests");

    Server::builder()
        .add_service(ControlServiceServer::new(service))
        .serve(addr)
        .await?;

    Ok(())
}