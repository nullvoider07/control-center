// crates/server/src/identity.rs
// Server Identity Management - Generate, persist, and load server identity

use std::fs;
use std::path::PathBuf;
use uuid::Uuid;
use tracing::{info, warn};
use serde::{Deserialize, Serialize};

/// Server identity configuration (persisted to disk)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServerIdentityConfig {
    pub server_id: String,
    pub network: String,
    pub created_at: i64,
}

/// Get the server identity configuration file path
fn get_identity_file_path() -> PathBuf {
    let config_dir = if cfg!(target_os = "windows") {
        std::env::var("APPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from("."))
            .join("ControlCenter")
    } else {
        std::env::var("HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(".config")
            .join("control-center")
    };
    
    config_dir.join("server-identity.json")
}

/// Load or generate server identity
pub fn load_or_generate_identity() -> ServerIdentityConfig {
    let identity_path = get_identity_file_path();
    
    // Try to load existing identity
    if identity_path.exists() {
        match fs::read_to_string(&identity_path) {
            Ok(content) => {
                match serde_json::from_str::<ServerIdentityConfig>(&content) {
                    Ok(identity) => {
                        info!("Loaded existing server identity: {}", identity.server_id);
                        return identity;
                    }
                    Err(e) => {
                        warn!("Failed to parse server identity file: {}", e);
                        warn!("Generating new identity...");
                    }
                }
            }
            Err(e) => {
                warn!("Failed to read server identity file: {}", e);
                warn!("Generating new identity...");
            }
        }
    }
    
    // Generate new identity
    let identity = ServerIdentityConfig {
        server_id: format!("srv-{}", Uuid::new_v4()),
        network: detect_network_name(),
        created_at: chrono::Utc::now().timestamp(),
    };
    
    // Persist to disk
    if let Err(e) = persist_identity(&identity) {
        warn!("Failed to persist server identity: {}", e);
    } else {
        info!("Generated new server identity: {}", identity.server_id);
    }
    
    identity
}

/// Persist server identity to disk
fn persist_identity(identity: &ServerIdentityConfig) -> Result<(), Box<dyn std::error::Error>> {
    let identity_path = get_identity_file_path();
    
    // Create parent directory if it doesn't exist
    if let Some(parent) = identity_path.parent() {
        fs::create_dir_all(parent)?;
    }
    
    // Serialize and write
    let json = serde_json::to_string_pretty(identity)?;
    fs::write(&identity_path, json)?;
    
    info!("Server identity persisted to: {}", identity_path.display());
    Ok(())
}

/// Detect network name from environment or generate default
fn detect_network_name() -> String {
    // Check environment variable first
    if let Ok(network) = std::env::var("CONTROL_CENTER_NETWORK") {
        return network;
    }
    
    // Try to detect from hostname
    if let Ok(hostname) = hostname::get() {
        if let Some(hostname_str) = hostname.to_str() {
            // Extract potential network from hostname patterns
            if let Some(network_part) = extract_network_from_hostname(hostname_str) {
                return network_part;
            }
        }
    }
    
    // Default fallback
    "default-network".to_string()
}

/// Extract network identifier from hostname if it follows common patterns
fn extract_network_from_hostname(hostname: &str) -> Option<String> {
    let parts: Vec<&str> = hostname.split('-').collect();
    
    if parts.len() >= 3 {
        let mut network_parts = Vec::new();
        
        for (i, part) in parts.iter().enumerate() {
            // Skip first/last if they're "server" or all digits
            if i == 0 || i == parts.len() - 1 {
                if part.to_lowercase() == "server" || part.chars().all(|c| c.is_ascii_digit()) {
                    continue;
                }
            }
            network_parts.push(*part);
        }
        
        if !network_parts.is_empty() {
            return Some(network_parts.join("-"));
        }
    }
    
    None
}

/// Get hostname of the machine
pub fn get_hostname() -> String {
    hostname::get()
        .ok()
        .and_then(|h| h.into_string().ok())
        .unwrap_or_else(|| "unknown-host".to_string())
}

/// Build ServerIdentity protobuf message
pub fn build_server_identity(
    config: &ServerIdentityConfig,
    listen_address: String,
    version: String,
) -> crate::proto::ServerIdentity {
    crate::proto::ServerIdentity {
        server_id: config.server_id.clone(),
        hostname: get_hostname(),
        listen_address,
        version,
        started_at: chrono::Utc::now().timestamp(),
        network: config.network.clone(),
    }
}

// Unit tests for identity management
#[cfg(test)]
mod tests {
    use super::*;
    
    #[test]
    fn test_extract_network_from_hostname() {
        assert_eq!(
            extract_network_from_hostname("server-prod-01"),
            Some("prod".to_string())
        );
        
        assert_eq!(
            extract_network_from_hostname("server-datacenter-east-01"),
            Some("datacenter-east".to_string())
        );
    }
}