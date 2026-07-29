// crates/server/src/main.rs
// gRPC Server with JWT Token Validation

use tonic::{transport::Server, Request, Response, Status, metadata::MetadataMap};
use tonic::transport::{Identity, ServerTlsConfig};
use std::sync::Arc;
use tracing::{info, warn, debug};
use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};
use jsonwebtoken::{decode, DecodingKey, Validation, Algorithm};
use tokio::sync::broadcast;
use tokio::time::Duration;
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

/// Require that a validated token carries a specific scope.
fn require_scope(claims: &Claims, scope: &str) -> Result<(), Status> {
    if claims.scopes.iter().any(|s| s == scope) {
        Ok(())
    } else {
        warn!("User '{}' missing required scope '{}'", claims.sub, scope);
        Err(Status::permission_denied(format!(
            "Missing required scope: {}",
            scope
        )))
    }
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
    event_tx: broadcast::Sender<proto::CommandEvent>,
    /// Subjects whose tokens are refused regardless of signature or expiry, from
    /// CC_REVOKED_SUBJECTS. Read once at startup: a revocation takes effect on the
    /// next restart, which is the same requirement as rotating the secret but
    /// without invalidating every other token.
    revoked_subjects: std::collections::HashSet<String>,
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
        event_tx: broadcast::Sender<proto::CommandEvent>,
        revoked_subjects: std::collections::HashSet<String>,
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
            event_tx,
            revoked_subjects,
        }
    }
    
    /// Validate a raw JWT string (audience/issuer/expiry + HS256 signature).
    fn validate_jwt(&self, token: &str) -> Result<Claims, Status> {
        let mut validation = Validation::new(Algorithm::HS256);
        validation.set_audience(&[&self.jwt_audience]);
        validation.set_issuer(&[&self.jwt_issuer]);
        // jsonwebtoken defaults validate_nbf to false; the OAuth flow issues nbf, so
        // honour it rather than accepting a token before its validity window opens.
        validation.validate_nbf = true;

        match decode::<Claims>(
            token,
            &DecodingKey::from_secret(self.jwt_secret.as_bytes()),
            &validation,
        ) {
            Ok(data) => {
                // A signed token cannot be withdrawn, so without this the only answer
                // to a leaked credential is rotating JWT_SECRET — which invalidates
                // every other token too, including the one baked into the guest image.
                // This revokes one principal and leaves the rest working.
                if self.revoked_subjects.contains(&data.claims.sub) {
                    warn!("Rejecting token for revoked subject '{}'", data.claims.sub);
                    return Err(Status::unauthenticated("Token has been revoked"));
                }
                debug!("Token validated for user: {}", data.claims.sub);
                Ok(data.claims)
            }
            Err(e) => {
                warn!("JWT validation failed: {}", e);
                Err(Status::unauthenticated(format!("Invalid token: {}", e)))
            }
        }
    }

    /// Validate the JWT carried in the `authorization: Bearer <token>` metadata.
    async fn validate_token(&self, metadata: &MetadataMap) -> Result<Claims, Status> {
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

        let token = auth_header
            .strip_prefix("Bearer ")
            .ok_or_else(|| {
                warn!("Authorization header not in Bearer format");
                Status::unauthenticated("Invalid authorization format. Expected: Bearer <token>")
            })?;

        self.validate_jwt(token)
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

    /// Parse a raw command string — which may be a user-level command
    /// ("here left", "type hello") OR a translated platform command
    /// (cliclick, osascript, xdotool, Windows cmd) — into
    /// (action_type, action_subtype, is_here_command).
    fn parse_command_meta(command: &str) -> (String, String, bool) {
        let trimmed = command.trim();
        let tokens: Vec<&str> = trimmed.splitn(4, ' ').collect();
        let first = tokens.first().copied().unwrap_or("").to_lowercase();

        // User-level commands
        if first == "type" {
            return ("keyboard".to_string(), "type".to_string(), false);
        }
        if first == "press" || first == "key" {
            return ("keyboard".to_string(), "press".to_string(), false);
        }
        if first == "position" {
            return ("position".to_string(), "position".to_string(), false);
        }
        if first == "here" {
            let subtype = tokens.get(1).copied().unwrap_or("left").to_lowercase();
            return ("mouse".to_string(), subtype, true);
        }
        if first.parse::<i32>().is_ok() {
            let subtype = tokens.get(2).copied().unwrap_or("left").to_lowercase();
            let subtype = subtype.split_whitespace().next().unwrap_or("left").to_string();
            return ("mouse".to_string(), subtype, false);
        }

        // macOS: cliclick
        // e.g. "cliclick c:."  "/opt/homebrew/bin/cliclick rc:960,540"
        //      "cliclick kd:cmd t:a ku:cmd"
        if first.ends_with("cliclick") || trimmed.contains("/cliclick ") {
            let rest = trimmed.splitn(2, "cliclick ").nth(1).unwrap_or("");
            let action_token = rest.split_whitespace().next().unwrap_or("");

            if action_token.starts_with("t:") {
                return ("keyboard".to_string(), "type".to_string(), false);
            }
            if action_token.starts_with("kp:") || action_token.starts_with("kd:") {
                return ("keyboard".to_string(), "press".to_string(), false);
            }
            if action_token.starts_with("p:") {
                return ("position".to_string(), "position".to_string(), false);
            }
            let shortcut = action_token.split(':').next().unwrap_or("c");
            let coords   = action_token.split(':').nth(1).unwrap_or(".");
            let is_here  = coords == ".";
            let subtype  = match shortcut {
                "c"  => "left",
                "rc" => "right",
                "dc" => "double",
                "tc" => "triple",
                "mc" => "middle",
                "dd" => "hold",
                "du" => "release",
                "m"  => "move",
                _    => "left",
            };
            return ("mouse".to_string(), subtype.to_string(), is_here);
        }

        // macOS: osascript
        // e.g. "osascript -e 'tell application "System Events" to keystroke "hello"'"
        //      "osascript -e 'tell application "System Events" to key code 36'"
        if first == "osascript" {
            if trimmed.contains("keystroke") {
                return ("keyboard".to_string(), "type".to_string(), false);
            }
            return ("keyboard".to_string(), "press".to_string(), false);
        }

        // Linux: xdotool
        // e.g. "DISPLAY=:0 xdotool click 1"
        //      "DISPLAY=:0 xdotool mousemove 960 540 click 1"
        //      "DISPLAY=:0 xdotool type \"hello\""
        //      "DISPLAY=:0 xdotool key ctrl+c"
        if trimmed.contains("xdotool") {
            let after = trimmed.splitn(2, "xdotool ").nth(1).unwrap_or("");
            let sub = after.split_whitespace().next().unwrap_or("");
            return match sub {
                "type"             => ("keyboard".to_string(), "type".to_string(),     false),
                "key"              => ("keyboard".to_string(), "press".to_string(),    false),
                "getmouselocation" => ("position".to_string(), "position".to_string(), false),
                "mousedown"        => ("mouse".to_string(),    "hold".to_string(),     true),
                "mouseup"          => ("mouse".to_string(),    "release".to_string(),  true),
                "click" => {
                    let is_dbl  = after.contains("--repeat 2");
                    let subtype = if is_dbl                      { "double" }
                                  else if after.contains(" 3")   { "right"  }
                                  else if after.contains(" 2")   { "middle" }
                                  else                            { "left"   };
                    ("mouse".to_string(), subtype.to_string(), true)
                }
                "mousemove" => {
                    if !after.contains(" click ") {
                        return ("mouse".to_string(), "move".to_string(), false);
                    }
                    let is_dbl  = after.contains("--repeat 2");
                    let subtype = if is_dbl                          { "double" }
                                  else if after.contains("click 3")  { "right"  }
                                  else if after.contains("click 2")  { "middle" }
                                  else                                { "left"   };
                    ("mouse".to_string(), subtype.to_string(), false)
                }
                _ => ("mouse".to_string(), sub.to_string(), false),
            };
        }

        // Windows: cmd /c echo <human_cmd> > C:\*.txt
        // e.g. "cmd /c echo here left > C:\mouse_cmd.txt"
        if first == "cmd" && trimmed.contains("> C:\\") {
            if let Some(after_echo) = trimmed.splitn(2, "echo ").nth(1) {
                let human_cmd = after_echo.split(" > C:\\").next().unwrap_or("").trim();
                return Self::parse_command_meta(human_cmd);
            }
        }

        ("unknown".to_string(), first, false)
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

        // Authenticate the agent: the auth_token must be a valid JWT carrying the
        // `agent` scope. (Previously this field was accepted but never checked.)
        let agent_claims = self.validate_jwt(&registration_req.auth_token)?;
        require_scope(&agent_claims, "agent")?;

        let agent_identity = registration_req.agent_identity
            .ok_or_else(|| Status::invalid_argument("Agent identity required"))?;

        info!(
            "Agent registration request: {} (Hostname: {}, IP: {}) authenticated as '{}'",
            agent_identity.agent_id,
            agent_identity.hostname,
            agent_identity.ip_address,
            agent_claims.sub
        );
        
        // Generate connection ID
        let connection_id = format!("conn-{}", uuid::Uuid::new_v4());
        
        // Register in registry (1:1 enforcement here)
        match self.registry.register_agent(
            &agent_identity,
            connection_id.clone(),
            agent_identity.ip_address.clone(),
            agent_claims.sub.clone(),
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
        // Authenticate the stream opener: require a valid `agent`-scoped JWT in the
        // request metadata so an unauthenticated peer cannot hijack the command feed.
        let agent_claims = self.validate_token(request.metadata()).await?;
        require_scope(&agent_claims, "agent")?;
        info!("Agent stream connection received (authenticated as '{}')", agent_claims.sub);

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

        // Bind the stream to the agent that registered it. Scope alone is not enough:
        // any holder of an `agent` token could otherwise attach a second handler and
        // race the shared command queue for the operator's typed text.
        self.registry
            .bind_stream(&connection_id, &agent_claims.sub)
            .await
            .map_err(Status::permission_denied)?;

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
        let claims = self.validate_token(request.metadata()).await?;
        require_scope(&claims, "monitor")?;
        self.monitoring.handle_connection_query(request).await
    }

    /// Query servers (monitoring API)
    async fn query_servers(
        &self,
        request: Request<proto::ServerStatusQuery>,
    ) -> Result<Response<proto::ServerStatusResponse>, Status> {
        let claims = self.validate_token(request.metadata()).await?;
        require_scope(&claims, "monitor")?;
        self.monitoring.handle_server_status_query(request).await
    }

    /// Get server identity
    async fn get_server_identity(
        &self,
        request: Request<proto::InfoRequest>,
    ) -> Result<Response<proto::ServerIdentity>, Status> {
        let claims = self.validate_token(request.metadata()).await?;
        require_scope(&claims, "monitor")?;
        let server_identity = identity::build_server_identity(
            &self.server_identity,
            "0.0.0.0:50051".to_string(), // TODO: Get actual listen address
            env!("CARGO_PKG_VERSION").to_string(),
        );
        
        Ok(Response::new(server_identity))
    }
    
    /// Retired. ExecuteRequest carries only a shell string, which the agent no longer
    /// accepts; routing it would reintroduce the shell path. Callers must use
    /// ExecuteCommand with a structured argv.
    async fn execute(
        &self,
        request: Request<proto::ExecuteRequest>,
    ) -> Result<Response<proto::ExecuteResponse>, Status> {
        let claims = self.validate_token(request.metadata()).await?;
        warn!(
            "User '{}' called the retired Execute RPC; rejecting",
            claims.sub
        );
        Err(Status::unimplemented(
            "Execute is retired: use ExecuteCommand with a structured argv",
        ))
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
    
    /// Get agent info. Requires the `monitor` scope: the response fingerprints the
    /// guest (OS, version, capabilities), which is reconnaissance for an attacker
    /// holding only a narrow token.
    async fn get_agent_info(
        &self,
        request: Request<proto::AgentInfoRequest>,
    ) -> Result<Response<proto::AgentInfo>, Status> {
        let claims = self.validate_token(request.metadata()).await?;
        require_scope(&claims, "monitor")?;
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
        require_scope(&claims, "execute")?;

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

        // Structured argv is the only accepted form. The legacy `command` shell string
        // is rejected outright so no request can reach a shell on the agent, and
        // human_command must be supplied explicitly rather than derived from it.
        if cmd_req.argv.is_empty() {
            return Err(Status::invalid_argument(
                "argv is required: the legacy shell command field is no longer accepted",
            ));
        }
        if cmd_req.human_command.is_empty() {
            return Err(Status::invalid_argument(
                "human_command is required alongside argv",
            ));
        }
        let human_command = cmd_req.human_command.clone();

        debug!(
            "Executing command {} via agent: {}",
            command_id,
            human_command
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
        
        // Broadcast CommandEvent to WatchCommands subscribers
        let (action_type, action_subtype, is_here_command) = Self::parse_command_meta(&human_command);
        let agent = self.registry.get_current_connection().await;
        let event = proto::CommandEvent {
            session_id: agent.as_ref().map(|a| a.connection_id.clone()).unwrap_or_default(),
            agent_id: agent.as_ref().map(|a| a.agent_id.clone()).unwrap_or_default(),
            agent_version: agent.as_ref().map(|a| a.agent_version.clone()).unwrap_or_default(),
            os_type: agent.as_ref().map(|a| match a.os_type {
                0 => "WINDOWS".to_string(),
                1 => "MACOS".to_string(),
                2 => "LINUX".to_string(),
                _ => "UNKNOWN".to_string(),
            }).unwrap_or_default(),
            timestamp: chrono::Utc::now().format("%Y-%m-%dT%H:%M:%S%.3fZ").to_string(),
            raw_command: response.as_ref()
                .ok()
                .map(|r| r.message.clone())
                .filter(|m| !m.is_empty())
                .unwrap_or_else(|| human_command.clone()),
            action_type,
            action_subtype,
            is_here_command,
            success: response.as_ref().map(|r| r.success).unwrap_or(false),
            error_message: response.as_ref().err().map(|e| e.to_string()).unwrap_or_default(),
            execution_time_ms: execution_time.as_millis() as i32,
            mouse_x: response.as_ref().ok().and_then(|r| r.mouse_x).unwrap_or(0),
            mouse_y: response.as_ref().ok().and_then(|r| r.mouse_y).unwrap_or(0),
            position_captured: response.as_ref().ok().and_then(|r| r.position_captured).unwrap_or(false),
            is_heartbeat: false,
            agent_alive: true,
        };
        // Ignore send errors — no subscribers is fine
        let _ = self.event_tx.send(event);

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
        require_scope(&claims, "monitor")?;
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
    /// Requires a valid JWT token carrying the `admin` scope.
    async fn disconnect_agent(
        &self,
        request: Request<proto::DisconnectAgentRequest>,
    ) -> Result<Response<proto::DisconnectAgentResponse>, Status> {
        let claims = self.validate_token(request.metadata()).await?;
        require_scope(&claims, "admin")?;
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
    /// Requires a valid JWT carrying the `monitor` scope.
    async fn get_connection_history(
        &self,
        request: Request<proto::ConnectionHistoryRequest>,
    ) -> Result<Response<proto::ConnectionHistoryResponse>, Status> {
        let claims = self.validate_token(request.metadata()).await?;
        require_scope(&claims, "monitor")?;
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

    /// WatchCommands stream — live command feed for Memory Archive. Requires a valid
    /// JWT carrying the `monitor` scope (the stream carries raw typed command text).
    type WatchCommandsStream = tokio_stream::wrappers::ReceiverStream<Result<proto::CommandEvent, Status>>;
    async fn watch_commands(
        &self,
        request: Request<proto::WatchRequest>,
    ) -> Result<Response<Self::WatchCommandsStream>, Status> {
        let claims = self.validate_token(request.metadata()).await?;
        require_scope(&claims, "monitor")?;
        let mut rx = self.event_tx.subscribe();
        let registry = self.registry.clone();
        let (tx, stream_rx) = tokio::sync::mpsc::channel(64);

        info!("WatchCommands subscriber connected");

        tokio::spawn(async move {
            loop {
                match tokio::time::timeout(Duration::from_secs(5), rx.recv()).await {
                    // Real command event received — forward it
                    Ok(Ok(event)) => {
                        if tx.send(Ok(event)).await.is_err() {
                            break; // Subscriber disconnected
                        }
                    }
                    // Broadcast channel closed — agent disconnected, close stream
                    Ok(Err(_)) => {
                        info!("WatchCommands: broadcast channel closed, stream ending");
                        break;
                    }
                    // Timeout — idle for 5s, send heartbeat (or close if agent gone)
                    Err(_) => {
                        let agent = registry.get_current_connection().await;
                        let agent_alive = agent.is_some();
                        let heartbeat = proto::CommandEvent {
                            session_id:    agent.as_ref().map(|a| a.connection_id.clone()).unwrap_or_default(),
                            agent_id:      agent.as_ref().map(|a| a.agent_id.clone()).unwrap_or_default(),
                            agent_version: agent.as_ref().map(|a| a.agent_version.clone()).unwrap_or_default(),
                            os_type: agent.as_ref().map(|a| match a.os_type {
                                0 => "WINDOWS".to_string(),
                                1 => "MACOS".to_string(),
                                2 => "LINUX".to_string(),
                                _ => "UNKNOWN".to_string(),
                            }).unwrap_or_default(),
                            timestamp:    chrono::Utc::now().format("%Y-%m-%dT%H:%M:%S%.3fZ").to_string(),
                            is_heartbeat: true,
                            agent_alive,
                            ..Default::default()
                        };
                        if tx.send(Ok(heartbeat)).await.is_err() {
                            break; // Subscriber disconnected
                        }
                        // Agent gone — send one final heartbeat with agent_alive=false
                        // then close the stream.
                        if !agent_alive {
                            info!("WatchCommands: agent disconnected, closing stream");
                            break;
                        }
                    }
                }
            }
            info!("WatchCommands stream closed");
        });

        Ok(Response::new(tokio_stream::wrappers::ReceiverStream::new(stream_rx)))
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

    info!("Control Center Server v{}", env!("CARGO_PKG_VERSION"));
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

    let addr: std::net::SocketAddr = std::env::var("SERVER_ADDR")
    .unwrap_or_else(|_| "0.0.0.0:50051".to_string())
    .parse()?;

    let listen_address = addr.to_string();
    info!("Server will listen on {}", addr);

    let registry = Arc::new(registry::ConnectionRegistry::new(
        single_agent_mode,
        100,
        addr.ip().to_string(),
    ));

    // Create monitoring handler
    let monitoring_handler = Arc::new(monitoring::MonitoringHandler::new(
        registry.clone(),
        server_identity.clone(),
        env!("CARGO_PKG_VERSION").to_string(),
    ));

    // Create stream handler
    let stream_handler = Arc::new(stream_handler::StreamHandler::new(
        registry.clone(),
        server_identity.server_id.clone(),
    ));

    // Create broadcast channel for WatchCommands (buffer 256 events)
    let (event_tx, _) = broadcast::channel::<proto::CommandEvent>(256);

    // Create service
    // Comma-separated JWT subjects to refuse. The answer to a leaked credential that
    // does not require rotating the secret and re-baking the guest image.
    let revoked_subjects: std::collections::HashSet<String> = std::env::var("CC_REVOKED_SUBJECTS")
        .unwrap_or_default()
        .split(',')
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect();
    if !revoked_subjects.is_empty() {
        let mut names: Vec<&str> = revoked_subjects.iter().map(String::as_str).collect();
        names.sort();
        warn!("Refusing tokens for revoked subject(s): {}", names.join(", "));
    }

    let service = ControlCenterService::new(
        jwt_secret.clone(),
        jwt_audience.clone(),
        jwt_issuer.clone(),
        registry,
        server_identity,
        monitoring_handler,
        stream_handler,
        listen_address,
        event_tx,
        revoked_subjects,
    );

    info!("Server will listen on {}", addr);
    info!("Ready to accept authenticated requests");

    // Transport security (F1). TLS is required unless explicitly disabled for local
    // development via CC_ALLOW_INSECURE=true.
    let tls_cert = std::env::var("CC_TLS_CERT").ok();
    let tls_key = std::env::var("CC_TLS_KEY").ok();
    let allow_insecure = std::env::var("CC_ALLOW_INSECURE")
        .map(|v| v == "true" || v == "1")
        .unwrap_or(false);

    let mut builder = Server::builder();

    match (tls_cert, tls_key) {
        (Some(cert_path), Some(key_path)) => {
            let cert = std::fs::read(&cert_path)
                .map_err(|e| format!("Failed to read CC_TLS_CERT '{}': {}", cert_path, e))?;
            let key = std::fs::read(&key_path)
                .map_err(|e| format!("Failed to read CC_TLS_KEY '{}': {}", key_path, e))?;
            let identity = Identity::from_pem(cert, key);
            builder = builder.tls_config(ServerTlsConfig::new().identity(identity))?;
            info!("TLS enabled (cert: {})", cert_path);
        }
        _ => {
            if !allow_insecure {
                return Err(
                    "TLS is required: set CC_TLS_CERT and CC_TLS_KEY, or set \
                     CC_ALLOW_INSECURE=true to run without TLS (development only)."
                        .into(),
                );
            }
            warn!("CC_ALLOW_INSECURE set — serving WITHOUT TLS (development only)");
        }
    }

    builder
        .add_service(ControlServiceServer::new(service))
        .serve(addr)
        .await?;

    Ok(())
}