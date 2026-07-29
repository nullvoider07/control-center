// tools/generate_token.rs
// JWT Token Generator for Control Center

use jsonwebtoken::{encode, EncodingKey, Header, Algorithm};
use std::time::{SystemTime, UNIX_EPOCH};

// Single canonical Claims definition — shared across all crates.
// Do NOT define a local Claims struct here.
use control_center_common::Claims;

/// Upper bound on a token's lifetime. A token cannot be revoked once issued, so its
/// expiry is the only thing that ends it; without a cap, `generate-token admin 876000`
/// mints a credential valid for a century. Re-issue rather than raising this.
const MAX_DURATION_HOURS: i64 = 8_760; // 365 days

fn usage() {
    println!("Usage: generate-token <user_id> [duration_hours] [scopes...]");
    println!();
    println!("Examples:");
    println!("  generate-token admin");
    println!("  generate-token user123 24");
    println!("  generate-token admin 168 execute metrics");
    println!();
    println!("Arguments:");
    println!("  user_id         Subject the token is issued to.");
    println!("  duration_hours  Positive integer, at most {} (365 days).", MAX_DURATION_HOURS);
    println!("                  Defaults to 24. Tokens without an expiry are not");
    println!("                  supported — the server requires 'exp'.");
    println!("  scopes          Defaults to: execute monitor");
    println!("                  Known scopes: execute, monitor, agent, metrics, admin");
    println!();
    println!("JWT_SECRET must be set, and must match the server's.");
}

fn main() {
    let args: Vec<String> = std::env::args().collect();

    // Before anything else: `generate-token --help` is printed by both installers, and
    // it used to be read as a user_id, so the documented help command silently minted
    // a live 24-hour execute+monitor credential and echoed it to the terminal.
    if args.len() < 2 || args[1..].iter().any(|a| a == "--help" || a == "-h" || a == "help") {
        usage();
        std::process::exit(if args.len() < 2 { 1 } else { 0 });
    }

    let user_id = &args[1];
    if user_id.starts_with('-') {
        eprintln!("ERROR: '{}' is not a recognised option.", user_id);
        eprintln!("       The first argument is the user_id. Run with --help for usage.");
        std::process::exit(1);
    }

    let duration_hours = if args.len() > 2 {
        let parsed = args[2].parse::<i64>().unwrap_or(0);
        if parsed <= 0 {
            eprintln!("ERROR: duration_hours must be a positive integer greater than 0.");
            eprintln!("       Tokens without an expiry are not supported.");
            eprintln!("       Example: generate-token admin 24");
            std::process::exit(1);
        }
        if parsed > MAX_DURATION_HOURS {
            eprintln!(
                "ERROR: duration_hours {} exceeds the maximum of {} (365 days).",
                parsed, MAX_DURATION_HOURS
            );
            eprintln!("       A token cannot be revoked, so its expiry is the only thing");
            eprintln!("       that ends it. Issue a shorter one and re-issue when it lapses.");
            std::process::exit(1);
        }
        parsed
    } else {
        24
    };

    let scopes = if args.len() > 3 {
        args[3..].to_vec()
    } else {
        vec!["execute".to_string(), "monitor".to_string()]
    };
    
    // Get JWT secret from environment
    let jwt_secret = std::env::var("JWT_SECRET")
        .expect("JWT_SECRET environment variable must be set");
    
    if jwt_secret.len() < 32 {
        eprintln!("ERROR: JWT_SECRET must be at least 32 characters");
        std::process::exit(1);
    }
    
    let jwt_audience = std::env::var("JWT_AUDIENCE")
        .unwrap_or_else(|_| "control-center".to_string());
    
    let jwt_issuer = std::env::var("JWT_ISSUER")
        .unwrap_or_else(|_| "control-center-auth".to_string());
    
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64;
    
    // Checked: the cap above keeps this in range, but an unchecked multiply here
    // wraps in a release build rather than failing, which would produce a token with
    // a nonsense expiry instead of an error.
    let expiration = duration_hours
        .checked_mul(3600)
        .and_then(|seconds| now.checked_add(seconds))
        .unwrap_or_else(|| {
            eprintln!("ERROR: duration_hours {} does not fit in a timestamp.", duration_hours);
            std::process::exit(1);
        });
    
    let claims = Claims {
        sub: user_id.clone(),
        exp: expiration,
        iat: now,
        nbf: None,
        session_id: None,
        scopes: scopes.clone(),
        aud: jwt_audience.clone(),
        iss: jwt_issuer.clone(),
    };
    
    let token = encode(
        &Header::new(Algorithm::HS256),
        &claims,
        &EncodingKey::from_secret(jwt_secret.as_bytes())
    ).expect("Failed to generate token");
    
    println!("═══════════════════════════════════════════════════════════");
    println!("JWT Token Generated Successfully");
    println!("═══════════════════════════════════════════════════════════");
    println!();
    println!("User:       {}", user_id);
    println!("Expires:    {} hours from now", duration_hours);
    println!("Scopes:     {:?}", scopes);
    println!("Audience:   {}", jwt_audience);
    println!("Issuer:     {}", jwt_issuer);
    println!();
    println!("═══════════════════════════════════════════════════════════");
    println!("TOKEN (copy this):");
    println!("═══════════════════════════════════════════════════════════");
    println!("{}", token);
    println!("═══════════════════════════════════════════════════════════");
    println!();
    println!("Usage:");
    println!("  export TOKEN=\"{}\"", token);
    println!("  control-center execute -c \"960 540 left\"");
    println!();
}