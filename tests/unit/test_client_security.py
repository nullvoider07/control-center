"""Unit tests for client-side security helpers (F6 token-in-argv, F7 file perms,
TLS default resolution, gen-certs self-signed material)."""
import os
import stat

import pytest
from click.testing import CliRunner

from controller.management import cli


# ---- F6: tokens must not be forced onto argv ------------------------------
def test_read_token_arg_prompts_when_missing(monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda prompt="Token: ": "typed-secret")
    assert cli._read_token_arg(None) == "typed-secret"


def test_read_token_arg_warns_when_passed_on_argv(capsys):
    val = cli._read_token_arg("argv-token")
    assert val == "argv-token"
    err = capsys.readouterr().err
    assert "process list" in err and "Warning" in err


# ---- F7: owner-only file permissions --------------------------------------
@pytest.mark.skipif(os.name == "nt", reason="POSIX perms only")
def test_secure_write_is_0600(tmp_path):
    p = tmp_path / "sub" / "session.json"
    cli._secure_write(p, "sensitive typed history")
    assert p.read_text() == "sensitive typed history"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(p.parent).st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX perms only")
def test_restrict_perms_tightens_existing_file(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("x")
    os.chmod(p, 0o644)
    cli._restrict_perms(p)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


# ---- TLS default resolution -----------------------------------------------
def test_use_ssl_default_true(monkeypatch):
    monkeypatch.delenv("CC_ALLOW_INSECURE", raising=False)
    assert cli._resolve_use_ssl() is True


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "True"])
def test_use_ssl_opt_out(monkeypatch, val):
    monkeypatch.setenv("CC_ALLOW_INSECURE", val)
    assert cli._resolve_use_ssl() is False


@pytest.mark.parametrize("val", ["0", "false", "no", ""])
def test_use_ssl_stays_secure_for_non_optout(monkeypatch, val):
    monkeypatch.setenv("CC_ALLOW_INSECURE", val)
    assert cli._resolve_use_ssl() is True


# ---- CC_TLS_CA pins a trust anchor; a bad path must not silently unpin it ---
def _client():
    from controller.integrations.gRPC import GRPCClient
    return GRPCClient(host="127.0.0.1", port=50051, use_ssl=True)


def test_an_unreadable_ca_is_an_error_not_a_downgrade(monkeypatch, tmp_path):
    """It used to log a warning and pass root_certificates=None, which swaps the
    private CA the operator pinned for the public root set. A typo in the path
    should not change what the client trusts."""
    from controller.integrations.exceptions import ConnectionError as CCConnectionError

    monkeypatch.setenv("CC_TLS_CA", str(tmp_path / "does-not-exist.crt"))
    with pytest.raises(CCConnectionError) as excinfo:
        _client()._create_channel()

    message = str(excinfo.value)
    assert "CC_TLS_CA" in message
    assert "does-not-exist.crt" in message


def test_a_readable_ca_is_used_as_the_trust_anchor(monkeypatch, tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_bytes(b"-----BEGIN CERTIFICATE-----\nnot-a-real-cert\n-----END CERTIFICATE-----\n")
    monkeypatch.setenv("CC_TLS_CA", str(ca))

    captured = {}

    import grpc
    monkeypatch.setattr(
        grpc, "ssl_channel_credentials",
        lambda root_certificates=None, **kw: captured.update(roots=root_certificates))
    monkeypatch.setattr(grpc, "secure_channel", lambda *a, **kw: "channel")

    assert _client()._create_channel() == "channel"
    assert captured["roots"] == ca.read_bytes(), "the pinned CA was not passed through"


def test_no_ca_set_falls_back_to_system_roots(monkeypatch):
    """Unsetting CC_TLS_CA is a deliberate choice to use the system trust store; only
    a set-but-unreadable path is an error."""
    monkeypatch.delenv("CC_TLS_CA", raising=False)

    captured = {}
    import grpc
    monkeypatch.setattr(
        grpc, "ssl_channel_credentials",
        lambda root_certificates=None, **kw: captured.update(roots=root_certificates))
    monkeypatch.setattr(grpc, "secure_channel", lambda *a, **kw: "channel")

    assert _client()._create_channel() == "channel"
    assert captured["roots"] is None


# ---- gen-certs: real self-signed CA + server cert -------------------------
def test_gen_certs_emits_ca_and_server_material(tmp_path):
    out = tmp_path / "tls"
    result = CliRunner().invoke(
        cli.gen_certs, ["--out-dir", str(out), "--host", "example.test"]
    )
    assert result.exit_code == 0, result.output
    ca, crt, key = out / "ca.crt", out / "server.crt", out / "server.key"
    assert ca.exists() and crt.exists() and key.exists()

    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    ca_cert = x509.load_pem_x509_certificate(ca.read_bytes())
    srv_cert = x509.load_pem_x509_certificate(crt.read_bytes())

    # Server cert is signed by the CA (issuer == CA subject; signature verifies).
    assert srv_cert.issuer == ca_cert.subject
    ca_pub = ca_cert.public_key()
    assert isinstance(ca_pub, rsa.RSAPublicKey)
    algo = srv_cert.signature_hash_algorithm
    assert algo is not None
    ca_pub.verify(
        srv_cert.signature,
        srv_cert.tbs_certificate_bytes,
        padding.PKCS1v15(),
        algo,
    )

    # SANs include localhost, the loopback IP, and the requested extra host.
    san = srv_cert.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    dns = set(san.get_values_for_type(x509.DNSName))
    assert "localhost" in dns and "example.test" in dns

    if os.name != "nt":
        assert stat.S_IMODE(os.stat(key).st_mode) == 0o600


# ---- files are created restricted, not restricted after the fact -----------
# Writing at the process umask and chmod-ing afterwards leaves a window where the
# content is on disk group- and world-readable. Asserted with os.chmod disabled, so
# the test distinguishes "created 0600" from "fixed up to 0600".

@pytest.mark.skipif(os.name == "nt", reason="POSIX perms only")
def test_secure_write_creates_the_file_already_restricted(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "chmod", lambda *a, **kw: None)
    old_umask = os.umask(0)
    try:
        p = tmp_path / "sub" / "session.json"
        cli._secure_write(p, "sensitive typed history")
    finally:
        os.umask(old_umask)

    assert p.read_text() == "sensitive typed history"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600, \
        "the file existed at the umask before being tightened"


@pytest.mark.skipif(os.name == "nt", reason="POSIX perms only")
def test_history_flush_creates_the_file_already_restricted(tmp_path, monkeypatch):
    from controller.management.history import ServerHistoryStore

    pytest.importorskip("cryptography")
    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    store = ServerHistoryStore(base_dir=tmp_path / "history", key_provider=lambda: key)
    store.load(("srv-test", 1234))
    assert store._path is not None, "the store did not activate"

    monkeypatch.setattr(os, "chmod", lambda *a, **kw: None)
    old_umask = os.umask(0)
    try:
        store.append("press ^s")
    finally:
        os.umask(old_umask)

    assert store._path.exists(), "nothing was written"
    assert stat.S_IMODE(os.stat(store._path).st_mode) == 0o600, \
        "the history file existed at the umask before being tightened"
