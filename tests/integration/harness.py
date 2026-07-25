"""Local full-stack harness: TLS server + Xvfb + registered agent.

Brings up the real Rust `control-center-server` (over one-way TLS) and the real
`control-center-agent` (talking to that server, actuating an isolated Xvfb display),
plus a small `cryptography`-generated CA/server cert and JWTs minted by the real
`generate-token` binary. Everything binds to loopback on ephemeral ports and tears
itself down. No cluster, no live display (:99, never the user's :0).
"""
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BIN = REPO_ROOT / "target" / "debug"
SERVER_BIN = BIN / "control-center-server"
AGENT_BIN = BIN / "control-center-agent"
GENTOKEN_BIN = BIN / "generate-token"

JWT_SECRET = "integration-test-secret-0123456789abcdef"
XVFB_DISPLAY = ":99"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise TimeoutError(f"port {port} did not open within {timeout}s")


def _gen_certs(tls_dir: Path) -> Dict[str, Path]:
    """Self-signed CA + server cert (SANs: localhost + 127.0.0.1)."""
    import datetime as dt
    import ipaddress
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    tls_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    not_after = now + dt.timedelta(days=2)

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "IT CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now).not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    srv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    srv_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "control-center-server")])
    srv_cert = (
        x509.CertificateBuilder()
        .subject_name(srv_name).issuer_name(ca_name)
        .public_key(srv_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now).not_valid_after(not_after)
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = tls_dir / "ca.crt"
    crt_path = tls_dir / "server.crt"
    key_path = tls_dir / "server.key"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    crt_path.write_bytes(srv_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(srv_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    return {"ca": ca_path, "cert": crt_path, "key": key_path}


def mint_token(scopes: List[str], user: str = "it", hours: int = 1) -> str:
    """Mint a JWT with the given scopes via the real generate-token binary."""
    env = {**os.environ, "JWT_SECRET": JWT_SECRET}
    out = subprocess.run(
        [str(GENTOKEN_BIN), user, str(hours), *scopes],
        env=env, capture_output=True, text=True, check=True,
    ).stdout
    m = re.search(r"eyJ[A-Za-z0-9._-]+", out)
    if not m:
        raise RuntimeError(f"could not parse token from generate-token output:\n{out}")
    return m.group(0)


@dataclass
class Stack:
    host: str
    port: int
    ca_path: Path
    tokens: Dict[str, str]
    server: subprocess.Popen
    agent: subprocess.Popen
    xvfb: Optional[subprocess.Popen]
    log_dir: Path
    _files: List = field(default_factory=list)

    def env_for_client(self, insecure: bool = False) -> Dict[str, str]:
        e = {**os.environ}
        if insecure:
            e["CC_ALLOW_INSECURE"] = "true"
        else:
            e.pop("CC_ALLOW_INSECURE", None)
            e["CC_TLS_CA"] = str(self.ca_path)
        return e


def launch(workdir: Path, *, own_xvfb: bool = True) -> Stack:
    log_dir = workdir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    tls = _gen_certs(workdir / "tls")

    tokens = {
        "execute": mint_token(["execute", "monitor"]),
        "monitor": mint_token(["monitor"]),
        "agent": mint_token(["agent"]),
        "admin": mint_token(["admin"]),
        "none": mint_token(["metrics"]),  # a valid token with no relevant scope
    }

    # Xvfb on :99 for isolated actuation (never the user's live :0).
    xvfb = None
    display = XVFB_DISPLAY
    if own_xvfb:
        xvfb = subprocess.Popen(
            ["Xvfb", XVFB_DISPLAY, "-screen", "0", "1280x1024x24", "-nolisten", "tcp"],
            stdout=open(log_dir / "xvfb.log", "wb"),
            stderr=subprocess.STDOUT,
        )
        time.sleep(1.0)
    else:
        display = os.environ.get("DISPLAY", XVFB_DISPLAY)

    port = _free_port()
    server_env = {
        **os.environ,
        "JWT_SECRET": JWT_SECRET,
        "SERVER_ADDR": f"127.0.0.1:{port}",
        "CC_TLS_CERT": str(tls["cert"]),
        "CC_TLS_KEY": str(tls["key"]),
        "SINGLE_AGENT_MODE": "true",
        "RUST_LOG": "info",
    }
    server_env.pop("CC_ALLOW_INSECURE", None)
    server = subprocess.Popen(
        [str(SERVER_BIN)], env=server_env,
        stdout=open(log_dir / "server.log", "wb"), stderr=subprocess.STDOUT,
    )
    _wait_port(port)

    agent_env = {
        **os.environ,
        "AGENT_SERVER_HOST": "127.0.0.1",
        "AGENT_SERVER_PORT": str(port),
        "AGENT_TLS_CA": str(tls["ca"]),
        "CONTROL_CENTER_TOKEN": tokens["agent"],
        "DISPLAY": display,
        "RUST_LOG": "info",
    }
    agent_env.pop("AGENT_ALLOW_INSECURE", None)
    agent = subprocess.Popen(
        [str(AGENT_BIN)], env=agent_env,
        stdout=open(log_dir / "agent.log", "wb"), stderr=subprocess.STDOUT,
    )

    stack = Stack(
        host="127.0.0.1", port=port, ca_path=tls["ca"], tokens=tokens,
        server=server, agent=agent, xvfb=xvfb, log_dir=log_dir,
    )
    _wait_agent_registered(stack)
    return stack


def _wait_agent_registered(stack: Stack, timeout: float = 20.0) -> None:
    """Poll query_connections (monitor token) until the agent shows up."""
    import sys
    sys.path.insert(0, str(REPO_ROOT / "crates"))
    from controller.integrations.gRPC import GRPCClient

    os.environ["CC_TLS_CA"] = str(stack.ca_path)
    deadline = time.time() + timeout
    last_err: Optional[Exception] = None
    while time.time() < deadline:
        if stack.agent.poll() is not None:
            raise RuntimeError(
                f"agent exited early (code {stack.agent.returncode}); "
                f"see {stack.log_dir / 'agent.log'}")
        client = GRPCClient(host=stack.host, port=stack.port, timeout=5, use_ssl=True)
        client.channel = client._create_channel()
        from controller.integrations.proto import control_center_pb2_grpc
        client.stub = control_center_pb2_grpc.ControlServiceStub(client.channel)
        client.set_token(stack.tokens["monitor"])
        try:
            conns = client.query_connections()
            if conns and conns.get("total_count", 0) > 0:
                client.disconnect()
                return
        except Exception as e:  # noqa: BLE001 — transient during startup
            last_err = e
        finally:
            try:
                client.disconnect()
            except Exception:
                pass
        time.sleep(0.5)
    raise TimeoutError(f"agent did not register within {timeout}s (last: {last_err})")


def teardown(stack: Stack) -> None:
    for proc in (stack.agent, stack.server, stack.xvfb):
        if proc is None:
            continue
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
