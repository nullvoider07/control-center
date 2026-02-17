// crates/agent/src/identity.rs
// Agent Identity Generation - Create unique agent identity with system information

use uuid::Uuid;
use tracing::{info, warn};
use std::collections::HashMap;
use get_if_addrs::IfAddr;

/// Generate unique agent ID
pub fn generate_agent_id() -> String {
    format!("agent-{}", Uuid::new_v4())
}

/// Get machine hostname
pub fn get_hostname() -> String {
    hostname::get()
        .ok()
        .and_then(|h| h.into_string().ok())
        .unwrap_or_else(|| {
            warn!("Failed to get hostname, using 'unknown-host'");
            "unknown-host".to_string()
        })
}

/// Detect agent's IP address (as seen from network)
pub fn detect_ip_address() -> String {
    // Try to get local IP address from network interfaces
    if let Ok(interfaces) = get_if_addrs::get_if_addrs() {
        // First pass: Look for IPv4 addresses
        for interface in &interfaces {
            // Skip loopback
            if interface.is_loopback() {
                continue;
            }
            
            // Check if it's IPv4
            if let IfAddr::V4(ref addr) = interface.addr {
                return addr.ip.to_string();
            }
        }
        
        // Fall back to IPv6 if no IPv4 found
        for interface in &interfaces {
            if !interface.is_loopback() {
                if let IfAddr::V6(ref addr) = interface.addr {
                    return addr.ip.to_string();
                }
            }
        }
    }
    
    warn!("Failed to detect IP address, using '0.0.0.0'");
    "0.0.0.0".to_string()
}

/// Build complete agent identity for registration
pub fn build_agent_identity(
    os_type: crate::proto::OsType,
    os_version: String,
    capabilities: Vec<String>,
    version: String,
) -> crate::proto::AgentIdentity {
    let agent_id = generate_agent_id();
    let hostname = get_hostname();
    let ip_address = detect_ip_address();
    
    info!("Generated agent identity:");
    info!("  Agent ID: {}", agent_id);
    info!("  Hostname: {}", hostname);
    info!("  IP Address: {}", ip_address);
    info!("  OS: {:?} - {}", os_type, os_version);
    info!("  Capabilities: {:?}", capabilities);
    
    // Build metadata
    let mut metadata = HashMap::new();
    metadata.insert("version".to_string(), version.clone());
    
    // Add system information to metadata
    #[cfg(target_os = "windows")]
    {
        if let Some(computer_name) = get_windows_computer_name() {
            metadata.insert("computer_name".to_string(), computer_name);
        }
        if let Some(username) = get_windows_username() {
            metadata.insert("username".to_string(), username);
        }
    }
    
    #[cfg(any(target_os = "linux", target_os = "macos"))]
    {
        if let Some(username) = get_unix_username() {
            metadata.insert("username".to_string(), username);
        }
    }
    
    crate::proto::AgentIdentity {
        agent_id,
        hostname,
        os_type: os_type as i32,
        os_version,
        ip_address,
        version,
        capabilities,
        metadata,
    }
}

/// Get Windows computer name
#[cfg(target_os = "windows")]
fn get_windows_computer_name() -> Option<String> {
    use winapi::um::winbase::GetComputerNameW;
    use winapi::shared::minwindef::DWORD;
    
    let mut size: DWORD = 0;
    unsafe {
        GetComputerNameW(std::ptr::null_mut(), &mut size);
    }
    
    if size == 0 {
        return None;
    }
    
    let mut buffer: Vec<u16> = vec![0; size as usize];
    let result = unsafe {
        GetComputerNameW(buffer.as_mut_ptr(), &mut size)
    };
    
    if result != 0 {
        Some(String::from_utf16_lossy(&buffer[..size as usize]))
    } else {
        None
    }
}

/// Get Windows username
#[cfg(target_os = "windows")]
fn get_windows_username() -> Option<String> {
    std::env::var("USERNAME").ok()
}

/// Get Unix username
#[cfg(any(target_os = "linux", target_os = "macos"))]
fn get_unix_username() -> Option<String> {
    std::env::var("USER").ok()
}

/// Validate agent identity before sending
pub fn validate_agent_identity(identity: &crate::proto::AgentIdentity) -> Result<(), String> {
    if identity.agent_id.is_empty() {
        return Err("Agent ID cannot be empty".to_string());
    }
    
    if !identity.agent_id.starts_with("agent-") {
        return Err("Agent ID must start with 'agent-'".to_string());
    }
    
    if identity.hostname.is_empty() {
        return Err("Hostname cannot be empty".to_string());
    }
    
    if identity.ip_address.is_empty() {
        return Err("IP address cannot be empty".to_string());
    }
    
    if identity.version.is_empty() {
        return Err("Version cannot be empty".to_string());
    }
    
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    
    #[test]
    fn test_generate_agent_id() {
        let id1 = generate_agent_id();
        let id2 = generate_agent_id();
        
        assert!(id1.starts_with("agent-"));
        assert!(id2.starts_with("agent-"));
        assert_ne!(id1, id2); // Should be unique
    }
    
    #[test]
    fn test_validate_agent_identity() {
        let valid_identity = crate::proto::AgentIdentity {
            agent_id: "agent-123".to_string(),
            hostname: "test-host".to_string(),
            os_type: 0,
            os_version: "Test OS".to_string(),
            ip_address: "192.168.1.1".to_string(),
            version: "1.0.0".to_string(),
            capabilities: vec![],
            metadata: HashMap::new(),
        };
        
        assert!(validate_agent_identity(&valid_identity).is_ok());
        
        let invalid_identity = crate::proto::AgentIdentity {
            agent_id: "invalid-id".to_string(), // Wrong prefix
            hostname: "test-host".to_string(),
            os_type: 0,
            os_version: "Test OS".to_string(),
            ip_address: "192.168.1.1".to_string(),
            version: "1.0.0".to_string(),
            capabilities: vec![],
            metadata: HashMap::new(),
        };
        
        assert!(validate_agent_identity(&invalid_identity).is_err());
    }
}