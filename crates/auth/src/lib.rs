// crates/auth/src/lib.rs

use oauth2::{
    AuthUrl, AuthorizationCode, ClientId, ClientSecret, CsrfToken,
    PkceCodeChallenge, PkceCodeVerifier, RedirectUrl, RefreshToken,
    Scope, TokenResponse, TokenUrl,
    basic::BasicClient,
    reqwest::async_http_client,
};
use jsonwebtoken::{encode, decode, Header, Validation, EncodingKey, DecodingKey, Algorithm};
use serde::{Deserialize, Serialize};
use chrono::{Utc, Duration};
use anyhow::{Result, Context, bail};
use uuid::Uuid;
use sha2::{Sha256, Digest};
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::RwLock;

/// JWT Claims structure
#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Claims {
    pub sub: String,           // Subject (user ID)
    pub exp: i64,              // Expiration time
    pub iat: i64,              // Issued at
    pub nbf: i64,              // Not before
    pub session_id: String,    // Session identifier
    pub scopes: Vec<String>,   // OAuth scopes granted
    pub aud: String,           // Audience (server identifier)
    pub iss: String,           // Issuer
}

/// OAuth token response
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TokenInfo {
    pub access_token: String,
    pub refresh_token: Option<String>,
    pub expires_in: i64,
    pub token_type: String,
    pub scopes: Vec<String>,
}

/// Session information
#[derive(Debug, Clone)]
pub struct Session {
    pub session_id: String,
    pub user_id: String,
    pub token_info: TokenInfo,
    pub created_at: i64,
    pub last_activity: i64,
    pub pkce_verifier: Option<String>,
}

/// OAuth configuration
#[derive(Debug, Clone)]
pub struct OAuthConfig {
    pub client_id: String,
    pub client_secret: Option<String>,
    pub auth_url: String,
    pub token_url: String,
    pub redirect_url: String,
    pub scopes: Vec<String>,
    pub issuer: String,
    pub audience: String,
}

/// Authentication manager
pub struct AuthManager {
    config: OAuthConfig,
    jwt_secret: String,
    oauth_client: BasicClient,
    sessions: Arc<RwLock<HashMap<String, Session>>>,
    pkce_verifiers: Arc<RwLock<HashMap<String, PkceCodeVerifier>>>,
}

/// Main implementation of authentication manager
impl AuthManager {
    /// Create new authentication manager with OAuth 2.0 + PKCE
    pub fn new(config: OAuthConfig, jwt_secret: String) -> Result<Self> {
        // Validate configuration
        if config.client_id.is_empty() {
            bail!("OAuth client_id cannot be empty");
        }
        if config.auth_url.is_empty() {
            bail!("OAuth auth_url cannot be empty");
        }
        if config.token_url.is_empty() {
            bail!("OAuth token_url cannot be empty");
        }
        if jwt_secret.len() < 32 {
            bail!("JWT secret must be at least 32 characters");
        }

        // Create OAuth client
        let oauth_client = BasicClient::new(
            ClientId::new(config.client_id.clone()),
            config.client_secret.as_ref().map(|s| ClientSecret::new(s.clone())),
            AuthUrl::new(config.auth_url.clone())
                .context("Invalid auth URL")?,
            Some(TokenUrl::new(config.token_url.clone())
                .context("Invalid token URL")?),
        )
        .set_redirect_uri(
            RedirectUrl::new(config.redirect_url.clone())
                .context("Invalid redirect URL")?
        );

        Ok(Self {
            config,
            jwt_secret,
            oauth_client,
            sessions: Arc::new(RwLock::new(HashMap::new())),
            pkce_verifiers: Arc::new(RwLock::new(HashMap::new())),
        })
    }

    /// Generate authorization URL with PKCE (Step 1 of OAuth flow)
    pub async fn generate_auth_url(&self) -> Result<(String, String)> {
        // Generate PKCE challenge
        let (pkce_challenge, pkce_verifier) = PkceCodeChallenge::new_random_sha256();
        
        // Generate CSRF token
        let (auth_url, csrf_token) = self.oauth_client
            .authorize_url(CsrfToken::new_random)
            .set_pkce_challenge(pkce_challenge)
            .add_scopes(self.config.scopes.iter().map(|s| Scope::new(s.clone())))
            .url();

        // Store PKCE verifier
        let csrf_value = csrf_token.secret().clone();
        self.pkce_verifiers.write().await.insert(
            csrf_value.clone(),
            pkce_verifier,
        );

        Ok((auth_url.to_string(), csrf_value))
    }

    /// Exchange authorization code for tokens (Step 2 of OAuth flow)
    pub async fn exchange_code(
        &self,
        code: String,
        csrf_token: String,
    ) -> Result<TokenInfo> {
        // Retrieve and remove PKCE verifier
        let pkce_verifier = self.pkce_verifiers.write().await
            .remove(&csrf_token)
            .context("Invalid or expired CSRF token")?;

        // Exchange code for token
        let token_result = self.oauth_client
            .exchange_code(AuthorizationCode::new(code))
            .set_pkce_verifier(pkce_verifier)
            .request_async(async_http_client)
            .await
            .context("Failed to exchange authorization code")?;

        // Extract token information
        let access_token = token_result.access_token().secret().clone();
        let refresh_token = token_result.refresh_token()
            .map(|t| t.secret().clone());
        let expires_in = token_result.expires_in()
            .map(|d| d.as_secs() as i64)
            .unwrap_or(3600);
        let scopes = token_result.scopes()
            .map(|scopes| scopes.iter().map(|s| s.to_string()).collect())
            .unwrap_or_else(|| self.config.scopes.clone());

        Ok(TokenInfo {
            access_token,
            refresh_token,
            expires_in,
            token_type: "Bearer".to_string(),
            scopes,
        })
    }

    /// Create session from OAuth token
    pub async fn create_session(&self, token_info: TokenInfo, user_id: String) -> Result<String> {
        let session_id = Uuid::new_v4().to_string();
        let now = Utc::now().timestamp();

        let session = Session {
            session_id: session_id.clone(),
            user_id: user_id.clone(),
            token_info: token_info.clone(),
            created_at: now,
            last_activity: now,
            pkce_verifier: None,
        };

        // Create JWT token
        let jwt_token = self.create_jwt(&user_id, &session_id, &token_info.scopes)?;

        // Store session
        self.sessions.write().await.insert(session_id.clone(), session);

        Ok(jwt_token)
    }

    /// Create JWT token from session data
    fn create_jwt(&self, user_id: &str, session_id: &str, scopes: &[String]) -> Result<String> {
        let now = Utc::now();
        let exp = (now + Duration::hours(24)).timestamp();

        let claims = Claims {
            sub: user_id.to_string(),
            exp,
            iat: now.timestamp(),
            nbf: now.timestamp(),
            session_id: session_id.to_string(),
            scopes: scopes.to_vec(),
            aud: self.config.audience.clone(),
            iss: self.config.issuer.clone(),
        };

        let header = Header {
            alg: Algorithm::HS512,
            ..Default::default()
        };

        encode(
            &header,
            &claims,
            &EncodingKey::from_secret(self.jwt_secret.as_bytes()),
        )
        .context("Failed to create JWT token")
    }

    /// Validate JWT token
    pub fn validate_token(&self, token: &str) -> Result<Claims> {
        let mut validation = Validation::new(Algorithm::HS512);
        validation.set_audience(&[&self.config.audience]);
        validation.set_issuer(&[&self.config.issuer]);

        let token_data = decode::<Claims>(
            token,
            &DecodingKey::from_secret(self.jwt_secret.as_bytes()),
            &validation,
        )
        .context("Invalid JWT token")?;

        Ok(token_data.claims)
    }

    /// Refresh access token using refresh token
    pub async fn refresh_token(&self, refresh_token_str: String) -> Result<TokenInfo> {
        let token_result = self.oauth_client
            .exchange_refresh_token(&RefreshToken::new(refresh_token_str))
            .request_async(async_http_client)
            .await
            .context("Failed to refresh token")?;

        let access_token = token_result.access_token().secret().clone();
        let refresh_token = token_result.refresh_token()
            .map(|t| t.secret().clone());
        let expires_in = token_result.expires_in()
            .map(|d| d.as_secs() as i64)
            .unwrap_or(3600);
        let scopes = token_result.scopes()
            .map(|scopes| scopes.iter().map(|s| s.to_string()).collect())
            .unwrap_or_else(|| self.config.scopes.clone());

        Ok(TokenInfo {
            access_token,
            refresh_token,
            expires_in,
            token_type: "Bearer".to_string(),
            scopes,
        })
    }

    /// Update session activity
    pub async fn update_session_activity(&self, session_id: &str) -> Result<()> {
        let mut sessions = self.sessions.write().await;
        if let Some(session) = sessions.get_mut(session_id) {
            session.last_activity = Utc::now().timestamp();
            Ok(())
        } else {
            bail!("Session not found")
        }
    }

    /// Revoke session
    pub async fn revoke_session(&self, session_id: &str) -> Result<()> {
        self.sessions.write().await.remove(session_id);
        Ok(())
    }

    /// Cleanup expired sessions (should be run periodically)
    pub async fn cleanup_expired_sessions(&self) {
        let now = Utc::now().timestamp();
        let session_timeout = 86400; // 24 hours

        self.sessions.write().await.retain(|_, session| {
            now - session.last_activity < session_timeout
        });
    }

    /// Get session information
    pub async fn get_session(&self, session_id: &str) -> Option<Session> {
        self.sessions.read().await.get(session_id).cloned()
    }
}

/// Helper: Hash password (for development/testing only - use proper identity provider in production)
pub fn hash_password(password: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(password.as_bytes());
    format!("{:x}", hasher.finalize())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_jwt_creation_and_validation() {
        let config = OAuthConfig {
            client_id: "test-client".to_string(),
            client_secret: Some("test-secret".to_string()),
            auth_url: "https://example.com/oauth/authorize".to_string(),
            token_url: "https://example.com/oauth/token".to_string(),
            redirect_url: "http://localhost:8080/callback".to_string(),
            scopes: vec!["read".to_string(), "write".to_string()],
            issuer: "control-center-server".to_string(),
            audience: "control-center-api".to_string(),
        };

        let auth_manager = AuthManager::new(
            config,
            "test-secret-key-with-enough-length-32chars".to_string()
        ).unwrap();

        let token = auth_manager.create_jwt(
            "user123",
            "session456",
            &["read".to_string(), "write".to_string()]
        ).unwrap();

        let claims = auth_manager.validate_token(&token).unwrap();
        assert_eq!(claims.sub, "user123");
        assert_eq!(claims.session_id, "session456");
    }
}
