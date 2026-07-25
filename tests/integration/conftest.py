"""Integration fixtures: one running TLS server + agent + Xvfb per session, plus
raw-stub helpers for asserting on gRPC status codes the client wrapper hides."""
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "crates"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402


def _binaries_present() -> bool:
    return all(p.exists() for p in (harness.SERVER_BIN, harness.AGENT_BIN, harness.GENTOKEN_BIN))


requires_stack = pytest.mark.skipif(
    not _binaries_present(),
    reason="debug binaries not built (cargo build --workspace)",
)


@pytest.fixture(scope="session")
def stack(tmp_path_factory):
    if not _binaries_present():
        pytest.skip("debug binaries not built")
    workdir = tmp_path_factory.mktemp("stack")
    st = harness.launch(workdir)
    yield st
    harness.teardown(st)


def raw_channel(stack, use_ssl: bool = True):
    """A bare grpc channel to the stack (TLS by default, trusting the harness CA)."""
    import grpc
    target = f"{stack.host}:{stack.port}"
    if use_ssl:
        creds = grpc.ssl_channel_credentials(root_certificates=stack.ca_path.read_bytes())
        return grpc.secure_channel(target, creds)
    return grpc.insecure_channel(target)


def raw_stub(stack, use_ssl: bool = True):
    from controller.integrations.proto import control_center_pb2_grpc
    ch = raw_channel(stack, use_ssl=use_ssl)
    return ch, control_center_pb2_grpc.ControlServiceStub(ch)


def meta(token: Optional[str]) -> List[Tuple[str, str]]:
    return [("authorization", f"Bearer {token}")] if token else []
