"""Unit tests for the encrypted, server-lifetime-scoped command history store."""
import json
import os
import stat

import pytest
from cryptography.fernet import Fernet

from controller.management.history import ServerHistoryStore


KEY = Fernet.generate_key()


class FakeClient:
    """Minimal client exposing the two no-auth identity RPCs resolve_instance uses."""

    def __init__(self, server_id, started_at):
        self._sid = server_id
        self._sa = started_at

    def get_server_identity(self):
        # started_at here is deliberately bogus (GetServerIdentity returns now()).
        return {"server_id": self._sid, "started_at": 111}

    def query_server_status(self, server_id=None, network=None):
        return {"total_count": 1, "servers": [
            {"identity": {"server_id": self._sid, "started_at": self._sa}}]}


def store(tmp_path, key=KEY):
    return ServerHistoryStore(base_dir=tmp_path / "history", key_provider=lambda: key)


def test_resolve_instance_uses_queryservers_started_at(tmp_path):
    inst = ServerHistoryStore.resolve_instance(FakeClient("srv-a", 1000))
    assert inst == ("srv-a", 1000)  # not 111 from GetServerIdentity


def test_encrypted_at_rest_and_0600(tmp_path):
    s = store(tmp_path)
    s.load(("srv-a", 1000))
    s.append("type secret-password")
    f = tmp_path / "history" / "srv-a.enc"
    raw = f.read_bytes()
    assert b"secret-password" not in raw  # ciphertext, not plaintext
    assert json.loads(Fernet(KEY).decrypt(raw).decode())["commands"] == ["type secret-password"]
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600


def test_persists_across_sessions_same_started_at(tmp_path):
    s1 = store(tmp_path)
    s1.load(("srv-a", 1000))
    s1.append("cmd-1")
    s2 = store(tmp_path)
    s2.load(("srv-a", 1000))
    assert s2._commands == ["cmd-1"]  # survived a new "connect"


def test_wiped_on_server_restart(tmp_path):
    s1 = store(tmp_path)
    s1.load(("srv-a", 1000))
    s1.append("before-restart")
    s2 = store(tmp_path)
    s2.load(("srv-a", 2000))  # started_at changed => restart
    assert s2._commands == []


def test_cap_enforced(tmp_path):
    s = store(tmp_path)
    s.load(("srv-cap", 5))
    for i in range(5100):
        s.append(f"cmd{i}")
    assert len(s._commands) == 5000
    assert s._commands[0] == "cmd100" and s._commands[-1] == "cmd5099"


def test_no_key_degrades_to_memory_only(tmp_path):
    s = ServerHistoryStore(base_dir=tmp_path / "history", key_provider=lambda: None)
    s.load(("srv-a", 7))
    s.append("secret")
    assert s._active is False
    assert not (tmp_path / "history" / "srv-a.enc").exists()


def test_unknown_instance_degrades_to_memory_only(tmp_path):
    s = store(tmp_path)
    s.load(None)
    s.append("x")
    assert s._active is False


def test_corrupt_file_starts_fresh(tmp_path):
    d = tmp_path / "history"
    d.mkdir(parents=True)
    (d / "srv-a.enc").write_bytes(b"not-a-valid-fernet-token")
    s = store(tmp_path)
    s.load(("srv-a", 1000))  # must not raise
    assert s._commands == []
