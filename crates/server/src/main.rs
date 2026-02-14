// crates/server/src/main.rs
// gRPC Server with JWT Token Validation

use tonic::{transport::Server, Request, Response, Status, Code, metadata::MetadataMap};
use std::sync::Arc;
use tokio::sync::RwLock;
use tracing::{info, warn, error};
use std::collections::HashMap;
use std::time::{SystemTime, Duration, UNIX_EPOCH};
use jsonwebtoken::{decode, DecodingKey, Validation, Algorithm};
use serde::{Deserialize, Serialize};

// Protobuf generated code
mod proto {
    tonic::include_proto!("control_center");
}

// Re-exporting for easier access
use proto::{
    control_service_server::{ControlService, ControlServiceServer},
    agent_service_client::AgentServiceClient,
    *,
};

/// JWT Claims structure
#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Claims {
    pub sub: String,           // Subject (user ID)
    pub exp: i64,              // Expiration
    pub iat: i64,              // Issued at
    pub scopes: Vec<String>,   // Permissions
    pub aud: String,           // Audience
    pub iss: String,           // Issuer
}

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
pub struct ControlServiceImpl {
    jwt_secret: String,
    jwt_audience: String,
    jwt_issuer: String,
    agent_client: Arc<RwLock<Option<AgentServiceClient<tonic::transport::Channel>>>>,
    agent_info_cache: Arc<RwLock<Option<AgentInfo>>>,
    rate_limiter: Arc<RwLock<RateLimiter>>,
    metrics: Arc<RwLock<ServerMetrics>>,
}

// Metrics structure for monitoring server performance and security
#[derive(Default)]
struct ServerMetrics {
    total_requests: u64,
    successful_requests: u64,
    failed_requests: u64,
    auth_failures: u64,
    rate_limit_hits: u64,
}

// Implementation of the ControlService
impl ControlServiceImpl {
    pub fn new(jwt_secret: String, jwt_audience: String, jwt_issuer: String) -> Self {
        Self {
            jwt_secret,
            jwt_audience,
            jwt_issuer,
            agent_client: Arc::new(RwLock::new(None)),
            agent_info_cache: Arc::new(RwLock::new(None)),
            rate_limiter: Arc::new(RwLock::new(RateLimiter::new(100, 60))), // 100 req/min
            metrics: Arc::new(RwLock::new(ServerMetrics::default())),
        }
    }

    // Connect to the agent with retries and exponential backoff
    async fn connect_to_agent(&self) -> Result<(), Box<dyn std::error::Error>> {
        let agent_host = std::env::var("AGENT_HOST").unwrap_or_else(|_| "127.0.0.1".to_string());
        let agent_port = std::env::var("AGENT_PORT").unwrap_or_else(|_| "50052".to_string());
        let agent_url = format!("http://{}:{}", agent_host, agent_port);

        info!("Attempting to connect to agent at {}...", agent_url);

        let max_retries = 5;
        let mut retry_count = 0;

        while retry_count < max_retries {
            match AgentServiceClient::connect(agent_url.clone()).await {
                Ok(client) => {
                    info!("Successfully connected to agent");
                    
                    // Get agent info
                    let mut client_clone = client.clone();
                    match client_clone.get_info(InfoRequest {}).await {
                        Ok(response) => {
                            let agent_info = response.into_inner();
                            info!(
                                "Agent info: OS={:?}, Version={}",
                                OsType::try_from(agent_info.os).ok(),
                                agent_info.os_version
                            );

                            *self.agent_client.write().await = Some(client);
                            *self.agent_info_cache.write().await = Some(agent_info);
                            return Ok(());
                        }
                        Err(e) => {
                            error!("Failed to get agent info: {}", e);
                            return Err(Box::new(e));
                        }
                    }
                }
                Err(e) => {
                    retry_count += 1;
                    warn!("Failed to connect to agent (attempt {}/{}): {}", retry_count, max_retries, e);
                    
                    if retry_count < max_retries {
                        // Exponential backoff: 2^retry_count seconds
                        let wait_time = 2_u64.pow(retry_count);
                        tokio::time::sleep(tokio::time::Duration::from_secs(wait_time)).await;
                    }
                }
            }
        }

        Err("Failed to connect to agent after maximum retries".into())
    }

    /// Extract and validate JWT token from metadata
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
                // Track authentication failure
                let mut metrics = self.metrics.write().await;
                metrics.auth_failures += 1;
                
                warn!("JWT validation failed: {}", e);
                return Err(Status::unauthenticated(format!("Invalid token: {}", e)));
            }
        };
        

        info!("Token validated for user: {}", token_data.claims.sub);
        Ok(token_data.claims)
    }

    // Check rate limit for the user and record metrics
    async fn check_rate_limit(&self, user_id: &str) -> Result<(), Status> {
        let mut limiter = self.rate_limiter.write().await;
        
        if !limiter.check_rate_limit(user_id) {
            let mut metrics = self.metrics.write().await;
            metrics.rate_limit_hits += 1;
            
            warn!("Rate limit exceeded for user: {}", user_id);
            return Err(Status::new(
                Code::ResourceExhausted,
                "Rate limit exceeded. Please try again later.",
            ));
        }

        Ok(())
    }
}

// Implementing the gRPC service methods
#[tonic::async_trait]
impl ControlService for ControlServiceImpl {
    async fn get_agent_info(
        &self,
        request: Request<AgentInfoRequest>,
    ) -> Result<Response<AgentInfo>, Status> {
        // Validate token from metadata
        let claims = self.validate_token(request.metadata()).await?;
        
        // Check rate limit
        self.check_rate_limit(&claims.sub).await?;

        let info = self.agent_info_cache.read().await.clone()
            .ok_or_else(|| Status::internal("Agent info not available"))?;

        let mut metrics = self.metrics.write().await;
        metrics.total_requests += 1;
        metrics.successful_requests += 1;

        info!("Agent info requested by user: {}", claims.sub);

        Ok(Response::new(info))
    }

    // Execute command with validation, rate limiting, and metrics
    async fn execute_command(
        &self,
        request: Request<CommandRequest>,
    ) -> Result<Response<CommandResponse>, Status> {
        // Validate token from metadata
        let claims = self.validate_token(request.metadata()).await?;
        
        // Check rate limit
        self.check_rate_limit(&claims.sub).await?;

        let req = request.into_inner();

        // Validate command
        if req.command.is_empty() {
            return Err(Status::invalid_argument("Command cannot be empty"));
        }

        if req.command.len() > 10000 {
            return Err(Status::invalid_argument("Command too long"));
        }

        // Record metrics
        let mut metrics = self.metrics.write().await;
        metrics.total_requests += 1;

        // Forward to agent
        let mut client = self.agent_client.write().await;
        let agent_client = client.as_mut()
            .ok_or_else(|| Status::internal("Agent not connected"))?;

        let execute_req = ExecuteRequest {
            id: req.id.clone(),
            command: req.command.clone(),
            user_id: Some(claims.sub.clone()),  // Pass user ID for auditing
        };

        match agent_client.execute(execute_req).await {
            Ok(response) => {
                let exec_response = response.into_inner();
                metrics.successful_requests += 1;

                info!(
                    "Command executed: user={}, id={}, time={}ms",
                    claims.sub, exec_response.id, exec_response.execution_time_ms
                );

                Ok(Response::new(CommandResponse {
                    id: exec_response.id,
                    success: exec_response.success,
                    message: exec_response.message,
                    execution_time_ms: exec_response.execution_time_ms,
                    mouse_x: exec_response.mouse_x,
                    mouse_y: exec_response.mouse_y,
                    position_captured: exec_response.position_captured,
                }))
            }
            Err(e) => {
                metrics.failed_requests += 1;
                error!("Command execution failed for user {}: {}", claims.sub, e);
                Err(Status::internal(format!("Command execution failed: {}", e)))
            }
        }
    }

    // Stream command execution results with validation, rate limiting, and metrics
    type ExecuteCommandStreamStream = tokio_stream::wrappers::ReceiverStream<
        Result<CommandResponse, Status>
    >;

    // This method allows clients to send a stream of commands and receive a stream of responses
    async fn execute_command_stream(
        &self,
        request: Request<tonic::Streaming<CommandRequest>>,
    ) -> Result<Response<Self::ExecuteCommandStreamStream>, Status> {
        // Validate token from metadata
        let claims = self.validate_token(request.metadata()).await?;
        
        let user_id = claims.sub.clone();
        let mut stream = request.into_inner();
        let agent_client = self.agent_client.clone();
        let rate_limiter = self.rate_limiter.clone();
        let (tx, rx) = tokio::sync::mpsc::channel(100);

        tokio::spawn(async move {
            while let Some(result) = stream.message().await.transpose() {
                match result {
                    Ok(req) => {
                        // Check rate limit
                        let mut limiter = rate_limiter.write().await;
                        if !limiter.check_rate_limit(&user_id) {
                            let _ = tx.send(Err(Status::resource_exhausted("Rate limit exceeded"))).await;
                            break;
                        }
                        drop(limiter);

                        let mut client = agent_client.write().await;
                        if let Some(agent) = client.as_mut() {
                            let exec_req = ExecuteRequest {
                                id: req.id.clone(),
                                command: req.command,
                                user_id: Some(user_id.clone()),
                            };

                            match agent.execute(exec_req).await {
                                Ok(response) => {
                                    let exec_response = response.into_inner();
                                    let _ = tx.send(Ok(CommandResponse {
                                        id: exec_response.id,
                                        success: exec_response.success,
                                        message: exec_response.message,
                                        execution_time_ms: exec_response.execution_time_ms,
                                        mouse_x: exec_response.mouse_x,
                                        mouse_y: exec_response.mouse_y,
                                        position_captured: exec_response.position_captured,
                                    })).await;
                                }
                                Err(e) => {
                                    let _ = tx.send(Err(Status::internal(e.to_string()))).await;
                                }
                            }
                        }
                    }
                    Err(e) => {
                        let _ = tx.send(Err(e)).await;
                        break;
                    }
                }
            }
        });

        Ok(Response::new(tokio_stream::wrappers::ReceiverStream::new(rx)))
    }

    // Monitor connection status with validation, rate limiting, and metrics
    type MonitorConnectionStream = tokio_stream::wrappers::ReceiverStream<
        Result<ConnectionStatus, Status>
    >;

    // This method allows clients to receive real-time updates about the connection status to the agent
    async fn monitor_connection(
        &self,
        request: Request<MonitorRequest>,
    ) -> Result<Response<Self::MonitorConnectionStream>, Status> {
        // Validate token from metadata
        let claims = self.validate_token(request.metadata()).await?;

        let agent_client = self.agent_client.clone();
        let (tx, rx) = tokio::sync::mpsc::channel(100);

        info!("Connection monitoring started for user: {}", claims.sub);

        tokio::spawn(async move {
            loop {
                tokio::time::sleep(tokio::time::Duration::from_secs(5)).await;

                let client = agent_client.read().await;
                let connected = client.is_some();

                let status = ConnectionStatus {
                    connected,
                    message: if connected {
                        "Connected to agent".to_string()
                    } else {
                        "Disconnected from agent".to_string()
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

    let service = ControlServiceImpl::new(jwt_secret, jwt_audience, jwt_issuer);

    // Connect to agent with retries
    service.connect_to_agent().await?;

    // Spawn background task for cleanup
    let rate_limiter = service.rate_limiter.clone();
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(3600)); // Every hour
        loop {
            interval.tick().await;
            rate_limiter.write().await.cleanup_old_entries();
            info!("Cleaned up old rate limit entries");
        }
    });

    // Start gRPC server
    let addr = std::env::var("SERVER_ADDR")
        .unwrap_or_else(|_| "0.0.0.0:50051".to_string())
        .parse()?;

    info!("Server listening on {}", addr);
    info!("Ready to accept authenticated requests");

    Server::builder()
        .add_service(ControlServiceServer::new(service))
        .serve(addr)
        .await?;

    Ok(())
}