// tools/generate_token.rs
// JWT Token Generator for Control Center

use jsonwebtoken::{encode, EncodingKey, Header, Algorithm};
use std::time::{SystemTime, UNIX_EPOCH};

// Single canonical Claims definition — shared across all crates.
// Do NOT define a local Claims struct here.
use control_center_common::Claims;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    
    if args.len() < 2 {
        eprintln!("Usage: generate_token <user_id> [duration_hours] [scopes...]");
        eprintln!();
        eprintln!("Examples:");
        eprintln!("  generate_token admin");
        eprintln!("  generate_token user123 24");
        eprintln!("  generate_token admin 168 execute metrics");
        eprintln!();
        eprintln!("Note: duration_hours must be greater than 0. Tokens without");
        eprintln!("      an expiry are not supported — the server requires 'exp'.");
        std::process::exit(1);
    }
    
    let user_id = &args[1];
    let duration_hours = if args.len() > 2 {
        let parsed = args[2].parse::<i64>().unwrap_or(0);
        if parsed <= 0 {
            eprintln!("ERROR: duration_hours must be a positive integer greater than 0.");
            eprintln!("       Tokens without an expiry are not supported.");
            eprintln!("       Example: generate_token admin 24");
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
    
    let expiration = now + (duration_hours * 3600);
    
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