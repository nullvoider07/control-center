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
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::RwLock;

// Re-export Claims for use in other crates
use control_center_common::Claims;

/// OAuth token response
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TokenInfo {
    pub access_token: String,
    pub refresh_token: Option<String>,
    pub expires_in: i64,
    pub token_type: String,
    pub scopes: Vec<String>,
}

/// How long a stored PKCE verifier stays usable. An authorization redirect happens
/// in seconds; anything older is an abandoned flow.
const PKCE_TTL_SECS: i64 = 600;

/// How long a session stays valid without activity.
const SESSION_TIMEOUT_SECS: i64 = 86_400; // 24 hours

/// A PKCE verifier awaiting its redirect, with the time it was issued so abandoned
/// flows can be swept.
struct PendingAuthorization {
    verifier: PkceCodeVerifier,
    created_at: i64,
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
    pkce_verifiers: Arc<RwLock<HashMap<String, PendingAuthorization>>>,
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

        // Store the PKCE verifier against its CSRF token, and drop any that have
        // aged out. Only exchange_code removed entries before, so every abandoned
        // authorization — a user who closes the tab — left one behind for the life
        // of the process. An unbounded map of secrets is both a leak and a widening
        // window: a verifier is meant to be usable for one redirect, not forever.
        let csrf_value = csrf_token.secret().clone();
        let now = Utc::now().timestamp();
        {
            let mut verifiers = self.pkce_verifiers.write().await;
            verifiers.retain(|_, pending| now - pending.created_at < PKCE_TTL_SECS);
            verifiers.insert(
                csrf_value.clone(),
                PendingAuthorization { verifier: pkce_verifier, created_at: now },
            );
        }

        Ok((auth_url.to_string(), csrf_value))
    }

    /// Exchange authorization code for tokens
    pub async fn exchange_code(
        &self,
        code: String,
        csrf_token: String,
    ) -> Result<TokenInfo> {
        // Retrieve and remove the PKCE verifier, refusing one that has aged out
        // rather than accepting a redirect for a flow abandoned hours ago.
        let pending = self.pkce_verifiers.write().await
            .remove(&csrf_token)
            .context("Invalid or expired CSRF token")?;
        if Utc::now().timestamp() - pending.created_at >= PKCE_TTL_SECS {
            anyhow::bail!("Authorization request expired; start the sign-in again");
        }
        let pkce_verifier = pending.verifier;

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

        // Store the session, sweeping expired ones on the way in. cleanup_expired_
        // sessions is public and was never called by anything, so the map only ever
        // grew; doing it where growth happens means it cannot be forgotten again.
        {
            let mut sessions = self.sessions.write().await;
            sessions.retain(|_, s| now - s.last_activity < SESSION_TIMEOUT_SECS);
            sessions.insert(session_id.clone(), session);
        }

        Ok(jwt_token)
    }

    /// Create JWT token from session data.
    /// Signs with HS256 to match the server's validation algorithm.
    fn create_jwt(&self, user_id: &str, session_id: &str, scopes: &[String]) -> Result<String> {
        let now = Utc::now();
        let exp = (now + Duration::hours(24)).timestamp();

        let claims = Claims {
            sub: user_id.to_string(),
            exp,
            iat: now.timestamp(),
            nbf: Some(now.timestamp()),
            session_id: Some(session_id.to_string()),
            scopes: scopes.to_vec(),
            aud: self.config.audience.clone(),
            iss: self.config.issuer.clone(),
        };

        let header = Header {
            alg: Algorithm::HS256,   // FIX: was HS512, server validates with HS256
            ..Default::default()
        };

        encode(
            &header,
            &claims,
            &EncodingKey::from_secret(self.jwt_secret.as_bytes()),
        )
        .context("Failed to create JWT token")
    }

    /// Validate JWT token.
    /// Uses HS256 to match create_jwt and the server's validation algorithm.
    pub fn validate_token(&self, token: &str) -> Result<Claims> {
        let mut validation = Validation::new(Algorithm::HS256);   // FIX: was HS512
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

    /// Cleanup expired sessions. Also runs automatically on create_session, so a
    /// caller that never schedules this still cannot accumulate them.
    pub async fn cleanup_expired_sessions(&self) {
        let now = Utc::now().timestamp();
        self.sessions.write().await.retain(|_, session| {
            now - session.last_activity < SESSION_TIMEOUT_SECS
        });
    }

    /// Get session information, treating an expired session as absent.
    ///
    /// The expiry is checked here rather than left to the sweep: otherwise whether a
    /// timed-out session still authenticates depends on when cleanup last ran.
    pub async fn get_session(&self, session_id: &str) -> Option<Session> {
        let now = Utc::now().timestamp();
        self.sessions
            .read()
            .await
            .get(session_id)
            .filter(|session| now - session.last_activity < SESSION_TIMEOUT_SECS)
            .cloned()
    }
}

// `hash_password` was removed rather than fixed. It was a single unsalted SHA-256,
// which is not a password hash at any setting, and nothing in the workspace called
// it — its only effect was to offer a plausible-looking one to whoever wired this
// crate up next. Authentication here is OAuth plus JWT; if local passwords are ever
// needed, add argon2 at that point rather than keeping a placeholder that reads as a
// solution.

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
        // session_id is now Option<String> in the shared Claims struct
        assert_eq!(claims.session_id, Some("session456".to_string()));
    }

    fn test_config() -> OAuthConfig {
        OAuthConfig {
            client_id: "test-client".to_string(),
            client_secret: Some("test-secret".to_string()),
            auth_url: "https://example.com/oauth/authorize".to_string(),
            token_url: "https://example.com/oauth/token".to_string(),
            redirect_url: "http://localhost:8080/callback".to_string(),
            scopes: vec!["read".to_string()],
            issuer: "control-center-server".to_string(),
            audience: "control-center-api".to_string(),
        }
    }

    fn manager() -> AuthManager {
        AuthManager::new(
            test_config(),
            "test-secret-key-with-enough-length-32chars".to_string(),
        ).unwrap()
    }

    #[tokio::test]
    async fn abandoned_authorizations_do_not_accumulate() {
        // Only exchange_code removed entries, so a user who closed the tab left a
        // verifier behind for the life of the process.
        let auth = manager();
        let (_, stale_csrf) = auth.generate_auth_url().await.unwrap();

        // Age the pending entry past its TTL.
        {
            let mut verifiers = auth.pkce_verifiers.write().await;
            let pending = verifiers.get_mut(&stale_csrf).unwrap();
            pending.created_at -= PKCE_TTL_SECS + 1;
        }

        let (_, fresh_csrf) = auth.generate_auth_url().await.unwrap();

        let verifiers = auth.pkce_verifiers.read().await;
        assert!(!verifiers.contains_key(&stale_csrf), "the abandoned flow was kept");
        assert!(verifiers.contains_key(&fresh_csrf), "the live flow was swept");
        assert_eq!(verifiers.len(), 1);
    }

    #[tokio::test]
    async fn an_expired_session_does_not_resolve() {
        // Whether a timed-out session still authenticated used to depend on when the
        // sweep last ran — and nothing ever ran it.
        let auth = manager();
        let token_info = TokenInfo {
            access_token: "access".to_string(),
            refresh_token: None,
            expires_in: 3600,
            token_type: "Bearer".to_string(),
            scopes: vec!["read".to_string()],
        };
        auth.create_session(token_info, "user123".to_string()).await.unwrap();

        let session_id = {
            let sessions = auth.sessions.read().await;
            sessions.keys().next().unwrap().clone()
        };
        assert!(auth.get_session(&session_id).await.is_some());

        {
            let mut sessions = auth.sessions.write().await;
            sessions.get_mut(&session_id).unwrap().last_activity -= SESSION_TIMEOUT_SECS + 1;
        }
        assert!(auth.get_session(&session_id).await.is_none(), "an expired session resolved");
    }

    #[tokio::test]
    async fn creating_a_session_sweeps_expired_ones() {
        let auth = manager();
        let token_info = TokenInfo {
            access_token: "access".to_string(),
            refresh_token: None,
            expires_in: 3600,
            token_type: "Bearer".to_string(),
            scopes: vec!["read".to_string()],
        };
        auth.create_session(token_info.clone(), "old-user".to_string()).await.unwrap();
        {
            let mut sessions = auth.sessions.write().await;
            for session in sessions.values_mut() {
                session.last_activity -= SESSION_TIMEOUT_SECS + 1;
            }
        }

        auth.create_session(token_info, "new-user".to_string()).await.unwrap();

        let sessions = auth.sessions.read().await;
        assert_eq!(sessions.len(), 1, "the expired session was retained");
        assert_eq!(sessions.values().next().unwrap().user_id, "new-user");
    }
}
