#!/usr/bin/env python3
"""Generate the Python protobuf/gRPC modules for controller.integrations.proto.

grpc_tools.protoc emits `import control_center_pb2 as ...` into the _grpc module: a
bare top-level import that resolves only when the proto directory itself is on
sys.path. That is why integrations/gRPC.py carries a fallback which inserts the
directory into sys.path — and in an installed package that fallback injects a
site-packages subdirectory onto sys.path and binds the generic name
`control_center_pb2` as a top-level module, where it can collide with anything else.
Rewriting the emitted import to a package-relative one removes both problems.

Every workflow that builds the controller calls this rather than inlining protoc, so
generation cannot drift between the test job and the release build jobs.
"""
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROTO_SRC = REPO_ROOT / "Proto"
OUT_DIR = REPO_ROOT / "crates" / "controller" / "integrations" / "proto"

# `import control_center_pb2 as control__center__pb2`
#   -> `from . import control_center_pb2 as control__center__pb2`
_BARE_IMPORT = re.compile(r"^import (\w+_pb2) as (\w+)$", re.MULTILINE)

_INIT = '''"""Generated protobuf modules. Regenerate with scripts/generate_proto.py."""

from .control_center_pb2 import *
from .control_center_pb2_grpc import *
'''


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    result = subprocess.run([
        sys.executable, "-m", "grpc_tools.protoc",
        f"-I{PROTO_SRC}",
        f"--python_out={OUT_DIR}",
        f"--grpc_python_out={OUT_DIR}",
        str(PROTO_SRC / "control_center.proto"),
    ])
    if result.returncode != 0:
        return result.returncode

    grpc_module = OUT_DIR / "control_center_pb2_grpc.py"
    source = grpc_module.read_text()
    patched, count = _BARE_IMPORT.subn(r"from . import \1 as \2", source)
    if count == 0 and "from . import" not in source:
        # Fail loudly rather than emit a module that only imports via the sys.path
        # fallback: a silent miss here is invisible until something collides.
        print(f"error: no sibling _pb2 import found in {grpc_module.name}; "
              "protoc output format changed", file=sys.stderr)
        return 1
    grpc_module.write_text(patched)

    (OUT_DIR / "__init__.py").write_text(_INIT)
    print(f"generated {OUT_DIR.relative_to(REPO_ROOT)} "
          f"({count} import(s) rewritten as relative)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
