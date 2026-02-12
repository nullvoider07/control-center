// crates/agent/build.rs
use std::error::Error;
use std::path::PathBuf;
use std::env;
use protoc_bin_vendored::protoc_bin_path;

fn main() -> Result<(), Box<dyn Error>> {
    let manifest_dir = env::var("CARGO_MANIFEST_DIR")?;
    let crate_root = PathBuf::from(&manifest_dir);
    
    println!("cargo:warning===========================================");
    println!("cargo:warning=BUILD SCRIPT DIAGNOSTICS");
    println!("cargo:warning===========================================");
    println!("cargo:warning=CARGO_MANIFEST_DIR: {}", manifest_dir);
    println!("cargo:warning=Current working dir: {:?}", env::current_dir()?);
    
    // Navigate to Project-Dockyard/control-center/Proto
    let proto_dir = crate_root.join("../../Proto");
    let proto_file = proto_dir.join("control_center.proto");
    
    println!("cargo:warning=");
    println!("cargo:warning=Expected proto directory: {}", proto_dir.display());
    println!("cargo:warning=Expected proto file: {}", proto_file.display());
    println!("cargo:warning=");
    
    // Check if directory exists
    if proto_dir.exists() {
        println!("cargo:warning=✓ Proto directory EXISTS");
        
        // List contents
        println!("cargo:warning=");
        println!("cargo:warning=Contents of {}:", proto_dir.display());
        match std::fs::read_dir(&proto_dir) {
            Ok(entries) => {
                for entry in entries {
                    if let Ok(e) = entry {
                        let path = e.path();
                        let metadata = std::fs::metadata(&path);
                        println!("cargo:warning=  - {} (readable: {})", 
                            path.file_name().unwrap().to_string_lossy(),
                            metadata.is_ok()
                        );
                    }
                }
            }
            Err(e) => {
                println!("cargo:warning=ERROR reading directory: {}", e);
            }
        }
    } else {
        println!("cargo:warning=✗ Proto directory DOES NOT EXIST");
        
        // Show what does exist
        let parent = proto_dir.parent().unwrap();
        if parent.exists() {
            println!("cargo:warning=");
            println!("cargo:warning=Contents of parent directory {}:", parent.display());
            if let Ok(entries) = std::fs::read_dir(parent) {
                for entry in entries {
                    if let Ok(e) = entry {
                        println!("cargo:warning=  - {}", e.path().file_name().unwrap().to_string_lossy());
                    }
                }
            }
        }
    }
    
    println!("cargo:warning=");
    
    // Check if file exists
    if proto_file.exists() {
        println!("cargo:warning=✓ Proto file EXISTS");
        
        // Check if readable
        match std::fs::read_to_string(&proto_file) {
            Ok(content) => {
                println!("cargo:warning=✓ Proto file is READABLE");
                println!("cargo:warning=  File size: {} bytes", content.len());
                println!("cargo:warning=  First line: {}", 
                    content.lines().next().unwrap_or("(empty)"));
            }
            Err(e) => {
                println!("cargo:warning=✗ Proto file is NOT READABLE: {}", e);
                return Err(Box::new(e));
            }
        }
    } else {
        println!("cargo:warning=✗ Proto file DOES NOT EXIST");
        println!("cargo:warning=");
        println!("cargo:warning=FATAL ERROR: Cannot proceed without proto file");
        println!("cargo:warning=Expected location: {}", proto_file.display());
        println!("cargo:warning=");
        
        panic!("Proto file not found at expected location: {}", proto_file.display());
    }

    println!("cargo:warning=");
    println!("cargo:warning=Configuring tonic_build...");
    println!("cargo:warning=Using protoc-bin-vendored for reliable cross-compilation...");
    let protoc_path = protoc_bin_path().expect("Failed to get protoc binary");
    unsafe {
        std::env::set_var("PROTOC", &protoc_path);
    }
    println!("cargo:warning=PROTOC set to: {:?}", protoc_path);
    println!("cargo:warning=PROTOC set to: {:?}", protoc_bin_path().unwrap());
    println!("cargo:rerun-if-changed={}", proto_file.display());
    println!("cargo:rerun-if-changed={}", proto_dir.display());

    match tonic_build::configure()
        .build_server(true)
        .build_client(false)
        .compile_protos(&[&proto_file], &[&proto_dir]) 
    {
        Ok(_) => {
            println!("cargo:warning=✓ PROTO COMPILATION SUCCESSFUL!");
            println!("cargo:warning===========================================");
            Ok(())
        }
        Err(e) => {
            println!("cargo:warning=✗ PROTO COMPILATION FAILED!");
            println!("cargo:warning=Error: {}", e);
            println!("cargo:warning===========================================");
            Err(Box::new(e))
        }
    }
}