"""The self-update path must be able to replace a binary that is currently running,
and must not misreport GitHub's quota as a network fault.

Both properties were broken and both failed in a way that pointed the operator at the
wrong thing:

* `shutil.copy2` onto the target binary returns ETXTBSY on Linux and macOS, because
  the CLI being replaced is itself executing. Only the Windows branch had a rename
  step, so `control-center update` could never replace itself on a POSIX host. The
  reported error, "Text file busy", named the symptom and not the cause.
* The update check made an unauthenticated GitHub API request. That quota is 60/hour
  keyed on the exit IP, so behind a VPN or carrier NAT it is spent by strangers — and
  the handler rendered the resulting 403 as "check your internet connection", which
  sends the operator to debug a working network.
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from controller.management import cli


# ---------------------------------------------------------------------------
# Replacing a running binary
# ---------------------------------------------------------------------------

def _running_binary(directory: Path, name: str):
    """Copy a real ELF/Mach-O interpreter and leave it executing.

    A shell script will not do: ETXTBSY applies to a file being executed as a
    program image, and a script is only read by its interpreter. python3 is used
    because it ignores argv[0], unlike a coreutils multi-call binary.
    """
    target = directory / name
    shutil.copy2(sys.executable, target)
    os.chmod(target, 0o755)
    proc = subprocess.Popen([str(target), "-c", "import time; time.sleep(30)"])

    deadline = time.time() + 5
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.05)
        break
    if proc.poll() is not None:
        proc.wait()
        pytest.skip("helper process exited immediately; cannot hold the image busy")
    return target, proc


@pytest.mark.skipif(os.name == "nt", reason="ETXTBSY is POSIX; Windows uses the rename path")
def test_direct_copy_over_a_running_binary_still_fails(tmp_path):
    """Pins the reason the fix exists. If this ever stops raising, the staged swap is
    no longer buying anything and the rationale should be revisited."""
    target, proc = _running_binary(tmp_path, "victim")
    try:
        replacement = tmp_path / "replacement"
        shutil.copy2(sys.executable, replacement)
        with pytest.raises(OSError) as excinfo:
            shutil.copy2(replacement, target)
        assert excinfo.value.errno == 26, f"expected ETXTBSY, got {excinfo.value}"
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.skipif(os.name == "nt", reason="ETXTBSY is POSIX; Windows uses the rename path")
def test_swap_replaces_a_running_binary(tmp_path):
    """The regression test for the reported failure: updating while the CLI runs."""
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    target, proc = _running_binary(install_dir, "control-center")
    original_size = target.stat().st_size

    try:
        source = tmp_path / "new"
        source.mkdir()
        payload = source / "control-center"
        payload.write_bytes(b"#!/bin/sh\necho new\n" + b"x" * 4096)

        staged = cli._stage_binaries(["control-center"], source, install_dir, executable=True)
        replaced, failures = cli._swap_binaries(staged, install_dir, windows=False)

        assert replaced == ["control-center"], f"swap failed: {failures}"
        assert not failures
        assert target.stat().st_size != original_size, "target was not actually replaced"
        assert os.access(target, os.X_OK), "replaced binary is not executable"
        assert proc.poll() is None, "swapping the image killed the running process"
    finally:
        proc.kill()
        proc.wait()


def test_staging_leaves_no_temporary_files_behind(tmp_path):
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    source = tmp_path / "new"
    source.mkdir()
    for name in ("control-center", "control-center-server"):
        (source / name).write_bytes(b"payload")

    staged = cli._stage_binaries(["control-center", "control-center-server"],
                                 source, install_dir, executable=True)
    cli._swap_binaries(staged, install_dir, windows=False)

    leftovers = [p.name for p in install_dir.iterdir() if p.name.startswith(".")]
    assert not leftovers, f"staging artefacts survived the swap: {leftovers}"


def test_absent_binaries_are_skipped_not_fatal(tmp_path):
    """The archive legitimately omits binaries on some platforms."""
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    source = tmp_path / "new"
    source.mkdir()
    (source / "control-center").write_bytes(b"payload")

    staged = cli._stage_binaries(
        ["control-center", "control-center-server", "control-center-agent"],
        source, install_dir, executable=True)

    assert [name for name, _ in staged] == ["control-center"]


def test_a_staging_failure_replaces_nothing(tmp_path):
    """The whole point of staging before swapping: a mid-install failure must not
    leave a v1.2.0 server next to a v1.0.0 CLI that no longer speaks to it."""
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    for name in ("control-center", "control-center-server"):
        (install_dir / name).write_bytes(b"OLD")

    source = tmp_path / "new"
    source.mkdir()
    (source / "control-center").write_bytes(b"NEW")
    # A directory where a regular file is expected: copy2 fails partway through.
    (source / "control-center-server").mkdir()

    with pytest.raises(cli.BinaryInstallError) as excinfo:
        cli._stage_binaries(["control-center", "control-center-server"],
                            source, install_dir, executable=True)

    assert "control-center-server" in str(excinfo.value)
    assert (install_dir / "control-center").read_bytes() == b"OLD", \
        "a later staging failure still modified an earlier binary"
    assert (install_dir / "control-center-server").read_bytes() == b"OLD"
    leftovers = [p.name for p in install_dir.iterdir() if p.name.startswith(".")]
    assert not leftovers, f"failed staging left {leftovers} behind"


# ---------------------------------------------------------------------------
# GitHub API quota handling
# ---------------------------------------------------------------------------

def test_request_is_identifiable_and_unauthenticated_without_a_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    headers = cli._github_headers()
    assert headers["User-Agent"].startswith("control-center/")
    assert "Authorization" not in headers


@pytest.mark.parametrize("var", ["GITHUB_TOKEN", "GH_TOKEN"])
def test_a_token_moves_the_quota_onto_the_account(monkeypatch, var):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv(var, "ghp_example")

    assert cli._github_headers()["Authorization"] == "Bearer ghp_example"


def test_exhausted_anonymous_quota_names_the_shared_ip_and_the_remedy():
    reset = int(time.time()) + 1830
    message = cli._rate_limit_message(
        {"x-ratelimit-remaining": "0", "x-ratelimit-limit": "60",
         "x-ratelimit-reset": str(reset)}, authenticated=False)

    assert "quota" in message.lower()
    assert "public IP" in message
    assert "GITHUB_TOKEN" in message, "the operator is not told how to fix it"
    assert "30m" in message, f"reset time not reported: {message}"
    assert "internet connection" not in message.lower(), \
        "the old misdiagnosis has come back"


def test_an_authenticated_403_does_not_suggest_setting_a_token():
    message = cli._rate_limit_message(
        {"x-ratelimit-remaining": "0", "x-ratelimit-limit": "5000"}, authenticated=True)

    assert "this account" in message
    assert "GITHUB_TOKEN" not in message


def test_a_403_with_quota_remaining_is_not_called_a_quota_problem():
    message = cli._rate_limit_message({"x-ratelimit-remaining": "44"}, authenticated=False)
    assert "not exhausted" in message


def test_fetch_raises_a_quota_error_rather_than_a_connectivity_error(monkeypatch):
    class _Response:
        status_code = 403
        headers = {"x-ratelimit-remaining": "0", "x-ratelimit-limit": "60"}

    class _Requests:
        @staticmethod
        def get(url, headers=None, timeout=None):
            return _Response()

    monkeypatch.setitem(sys.modules, "requests", _Requests)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    with pytest.raises(cli.UpdateCheckError) as excinfo:
        cli._fetch_latest_release("https://api.github.com/repos/x/y/releases/latest")

    assert "quota" in str(excinfo.value).lower()
    assert "could not reach" not in str(excinfo.value).lower()


def test_fetch_returns_the_release_payload_on_success(monkeypatch):
    sent = {}

    class _Response:
        status_code = 200
        headers = {}

        @staticmethod
        def json():
            return {"tag_name": "v9.9.9", "assets": []}

    class _Requests:
        @staticmethod
        def get(url, headers=None, timeout=None):
            sent.update(headers or {})
            return _Response()

    monkeypatch.setitem(sys.modules, "requests", _Requests)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_example")

    assert cli._fetch_latest_release("https://example.invalid")["tag_name"] == "v9.9.9"
    assert sent["Authorization"] == "Bearer ghp_example", \
        "the token was not actually attached to the request"


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("1.2.0", (1, 2, 0)),
    ("v1.2.0", (1, 2, 0)),
    ("1.2", (1, 2, 0)),
    ("1.10.0", (1, 10, 0)),
    ("1.2.0-rc1", (1, 2, 0)),
])
def test_version_parsing(text, expected):
    assert cli._version_tuple(text) == expected


def test_a_newer_release_sorts_above_the_current_build():
    assert cli._version_tuple("1.10.0") > cli._version_tuple("1.9.0")
    assert cli._version_tuple("2.0.0") > cli._version_tuple("1.2.0")


def test_an_older_release_does_not_read_as_an_update():
    """A string comparison called any difference "newer", so a build ahead of the
    latest release was offered a downgrade and would have installed it."""
    assert cli._version_tuple("1.1.0") < cli._version_tuple("1.2.0")
    assert not cli._version_tuple("1.1.0") > cli._version_tuple("1.2.0")


# ---------------------------------------------------------------------------
# Archive extraction
# ---------------------------------------------------------------------------

def test_tar_extraction_specifies_a_filter():
    """Python 3.12 warns when extractall() is called without a filter and 3.14 makes
    it an error, which would break the updater on a current interpreter."""
    source = Path(cli.__file__).read_text()
    assert "filter='data'" in source, \
        "tarfile.extractall must pass an explicit filter"


# ---------------------------------------------------------------------------
# Release asset integrity
# ---------------------------------------------------------------------------
# TLS to GitHub was the only integrity control on the update path, and v1.2.0 made
# that worse rather than better: the staged swap succeeds against a running binary,
# so a bad asset went from "fails noisily with ETXTBSY" to "installs silently".

def _release(assets):
    return {"tag_name": "v9.9.9",
            "assets": [{"name": n, "browser_download_url": f"https://example.invalid/{n}"}
                       for n in assets]}


def _serve(body: str, monkeypatch):
    class _Response:
        status_code = 200
        text = body
        headers = {}

        @staticmethod
        def raise_for_status():
            return None

    class _Requests:
        @staticmethod
        def get(url, headers=None, timeout=None):
            return _Response()

    monkeypatch.setitem(sys.modules, "requests", _Requests)


def test_a_release_without_checksums_is_refused():
    """Fail closed. Installing when the digests are absent means anyone who can strip
    one asset from the response also strips the only integrity control there is."""
    with pytest.raises(cli.IntegrityError) as excinfo:
        cli._fetch_checksums(_release(["control-center-9.9.9-linux-x64.tar.gz"]))
    assert "SHA256SUMS" in str(excinfo.value)


def test_checksums_are_parsed_from_the_sha256sum_format(monkeypatch):
    _serve(
        "a" * 64 + "  control-center-9.9.9-linux-x64.tar.gz\n"
        + "b" * 64 + " *control-center-9.9.9-windows-x64.zip\n",
        monkeypatch)

    sums = cli._fetch_checksums(_release(["SHA256SUMS", "control-center-9.9.9-linux-x64.tar.gz"]))
    assert sums["control-center-9.9.9-linux-x64.tar.gz"] == "a" * 64
    # sha256sum's binary-mode marker is part of the name field, not the name.
    assert sums["control-center-9.9.9-windows-x64.zip"] == "b" * 64


def test_an_empty_checksum_file_is_refused(monkeypatch):
    _serve("\n# nothing usable here\n", monkeypatch)
    with pytest.raises(cli.IntegrityError):
        cli._fetch_checksums(_release(["SHA256SUMS"]))


def test_a_matching_asset_verifies(tmp_path):
    payload = tmp_path / "control-center-9.9.9-linux-x64.tar.gz"
    payload.write_bytes(b"release bytes")
    digest = hashlib.sha256(b"release bytes").hexdigest()

    cli._verify_asset(str(payload), payload.name, {payload.name: digest})


def test_a_tampered_asset_is_refused(tmp_path):
    """The regression this exists for: a payload that is not what the release
    published must never reach the extractor, let alone the staged swap."""
    payload = tmp_path / "control-center-9.9.9-linux-x64.tar.gz"
    payload.write_bytes(b"malicious bytes")
    published = hashlib.sha256(b"release bytes").hexdigest()

    with pytest.raises(cli.IntegrityError) as excinfo:
        cli._verify_asset(str(payload), payload.name, {payload.name: published})
    assert published in str(excinfo.value), "the operator is not shown what was expected"


def test_an_asset_missing_from_the_checksum_file_is_refused(tmp_path):
    payload = tmp_path / "control-center-9.9.9-linux-arm64.tar.gz"
    payload.write_bytes(b"x")
    with pytest.raises(cli.IntegrityError):
        cli._verify_asset(str(payload), payload.name,
                          {"control-center-9.9.9-linux-x64.tar.gz": "0" * 64})


def test_verification_runs_before_extraction():
    """Ordering is the property: verifying after extraction would already have handed
    a tampered archive to the tar reader."""
    source = Path(cli.__file__).read_text()
    verify = source.index("_verify_asset(temp_file")
    extract = source.index("Extracting archive...")
    assert verify < extract, "the checksum is verified after the archive is extracted"


def test_the_release_workflow_publishes_checksums():
    workflow = (Path(cli.__file__).resolve().parents[3] / ".github/workflows/release.yml")
    body = workflow.read_text()
    assert "sha256sum control-center-*.tar.gz control-center-*.zip > SHA256SUMS" in body, \
        "the release job must generate SHA256SUMS covering every published asset"


@pytest.mark.parametrize("script", ["install/install.sh", "install/install.ps1"])
def test_the_install_scripts_verify_what_they_download(script):
    """The first-install path unpacks a release and puts it on PATH; it has the same
    exposure as the updater and needs the same check."""
    body = (Path(cli.__file__).resolve().parents[3] / script).read_text()
    assert "SHA256SUMS" in body, f"{script} does not fetch the published digests"


# ---------------------------------------------------------------------------
# What the updater replaces
# ---------------------------------------------------------------------------

def test_the_updater_replaces_every_binary_the_installers_install():
    """The update replaced three of the four installed binaries, so generate-token
    stayed at whatever version was last installed from scratch. It shares the Claims
    definition with the server, so a token-format change would leave it minting
    tokens the updated server rejects — and nothing would say so."""
    root = Path(cli.__file__).resolve().parents[3]
    installed = {
        line.split('=', 1)[1].strip().strip('"')
        for line in (root / "install/install.sh").read_text().splitlines()
        if line.startswith(("SERVER_BINARY=", "AGENT_BINARY=", "TOKEN_BINARY=",
                            "CLI_BINARY="))
    }
    assert installed, "could not read the binary names out of install.sh"

    source = Path(cli.__file__).read_text()
    start = source.index("binaries = ['control-center',")
    updated = set(re.findall(r"'([a-z-]+)'", source[start:source.index(']', start)]))

    missing = installed - updated
    assert not missing, f"the updater never replaces: {sorted(missing)}"


def test_the_windows_and_posix_binary_lists_agree():
    source = Path(cli.__file__).read_text()
    win_start = source.index("binaries = ['control-center.exe',")
    windows = set(re.findall(r"'([a-z.-]+)'", source[win_start:source.index(']', win_start)]))
    posix_start = source.index("binaries = ['control-center',")
    posix = set(re.findall(r"'([a-z-]+)'", source[posix_start:source.index(']', posix_start)]))
    assert {name.removesuffix('.exe') for name in windows} == posix
