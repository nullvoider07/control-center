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
