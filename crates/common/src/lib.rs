// crates/common/src/lib.rs
//
// Shared types used across control-center Rust crates.
// Import this crate anywhere a JWT Claims struct is needed instead of
// defining a local copy.

use serde::{Deserialize, Serialize};

/// Canonical JWT Claims structure for Control Center.
///
/// All token producers (auth crate, generate-token tool) and all token
/// consumers (server) must use this single definition so that the
/// serialized payload is always compatible.
///
/// Required claims
/// ---------------
/// - `sub`     : Subject — the user / agent identifier
/// - `exp`     : Expiration — Unix timestamp (seconds). Always required;
///               the server rejects tokens without it.
/// - `iat`     : Issued-at — Unix timestamp (seconds).
/// - `scopes`  : Permission list, e.g. ["execute", "monitor"].
/// - `aud`     : Audience — must match the server's configured audience.
/// - `iss`     : Issuer  — must match the server's configured issuer.
///
/// Optional claims
/// ---------------
/// - `nbf`        : Not-before — Unix timestamp. Produced by the OAuth flow
///                  in auth/src/lib.rs but omitted by generate-token and the
///                  Python CLI. Skipped during serialization when None so
///                  tokens from those paths stay clean.
/// - `session_id` : OAuth session identifier. Same rule as nbf — present in
///                  OAuth-flow tokens, absent in directly-generated tokens.
///                  The server does not require it for validation.
#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Claims {
    /// Subject (user ID)
    pub sub: String,

    /// Expiration time (Unix timestamp, seconds). Always required.
    pub exp: i64,

    /// Issued-at (Unix timestamp, seconds).
    pub iat: i64,

    /// Not-before (Unix timestamp, seconds). Optional — only set by the
    /// OAuth flow.
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(default)]
    pub nbf: Option<i64>,

    /// OAuth session identifier. Optional — only set by the OAuth flow.
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(default)]
    pub session_id: Option<String>,

    /// Permission scopes granted to this token.
    pub scopes: Vec<String>,

    /// Audience — must match the server's jwt_audience config value.
    pub aud: String,

    /// Issuer — must match the server's jwt_issuer config value.
    pub iss: String,
}