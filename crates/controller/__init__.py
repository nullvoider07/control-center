"""Control Center controller package: CLI, gRPC client, and OS actuation backends.

Deliberately empty of re-exports. Each subpackage's __init__ eagerly imports its
own modules, so re-exporting them here would make a bare `import controller` pull
in grpc, psutil and keyring, and would route through management.cli — which
imports integrations, creating a cycle.
"""

# Single source for the Python side: setup.py parses this literal and
# management.cli imports it for `--version`. Keep in step with Cargo.toml on a
# release bump — v1.2.0 shipped with the CLI still reporting 1.1.0 because the
# version lived in two unconnected places.
__version__ = "1.3.0"
