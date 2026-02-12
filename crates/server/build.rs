// crates/server/build.rs
use std::error::Error;
use std::path::PathBuf;
use std::env;

fn main() -> Result<(), Box<dyn Error>> {
    let manifest_dir = env::var("CARGO_MANIFEST_DIR")?;
    let crate_root = PathBuf::from(manifest_dir);
    
    // Navigate to Proto folder
    let proto_dir = crate_root.join("../../Proto");
    // Use the standardized US spelling
    let proto_file = proto_dir.join("control_center.proto");

    println!("cargo:rerun-if-changed={}", proto_file.display());

    if !proto_file.exists() {
        panic!(
            "Proto file not found at: {}. Ensure it exists and uses 'control_center.proto' filename.", 
            proto_file.display()
        );
    }

    tonic_build::configure()
        .build_server(true)
        .build_client(true) 
        .compile_protos(
            &[proto_file],
            &[proto_dir] 
        )?;

    Ok(())
}