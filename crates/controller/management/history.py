"""Encrypted, server-lifetime-scoped command history for the interactive console.

History persists across `connect` sessions and is cleared when the server process
restarts. It is keyed to (server_id, started_at), where started_at is the server's
true process-start timestamp obtained from QueryServers — GetServerIdentity.started_at
is regenerated on every call and must not be used for this.

Stored encrypted (Fernet) with a key held in the OS keyring. When cryptography or a
keyring backend is unavailable, the store degrades to in-memory-only (no file is
written) so history is never persisted under weaker protection than intended.
"""

import os
import json
import logging
from pathlib import Path
from typing import Optional, List, Callable, Tuple

logger = logging.getLogger('control-center')

# Optional dependencies — guarded so the console still works without them.
# Each name is bound to None when absent so callers can narrow with `is None`.
try:
    from cryptography.fernet import Fernet
except ImportError:
    Fernet = None

try:
    import keyring
except ImportError:
    keyring = None

try:
    import readline
except ImportError:
    readline = None

_KEYRING_SERVICE = 'control-center'
_KEYRING_KEY = 'history-key'
_MAX_ENTRIES = 5000


class ServerHistoryStore:
    """Per-server encrypted command history bound to a server process instance."""

    def __init__(
        self,
        base_dir: Optional[Path] = None,
        key_provider: Optional[Callable[[], Optional[bytes]]] = None,
        max_entries: int = _MAX_ENTRIES,
    ):
        if base_dir is None:
            from controller.management.config_manager import ConfigManager
            base_dir = ConfigManager.CONFIG_DIR / 'history'
        self.dir = Path(base_dir)
        self.max_entries = max_entries
        self._key_provider = key_provider or self._keyring_key
        self._commands: List[str] = []
        self._path: Optional[Path] = None
        self._started_at: Optional[int] = None
        self._fernet = None
        self._active = False  # True only when a usable key + instance are resolved
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            if os.name != 'nt':
                os.chmod(self.dir, 0o700)
        except Exception as e:
            logger.debug(f"history dir setup failed: {e}")

    # Key management -------------------------------------------------------
    def _keyring_key(self) -> Optional[bytes]:
        """Fetch or create the Fernet key in the OS keyring. None if unavailable."""
        if keyring is None or Fernet is None:
            return None
        try:
            existing = keyring.get_password(_KEYRING_SERVICE, _KEYRING_KEY)
            if existing:
                return existing.encode()
            new_key = Fernet.generate_key()
            keyring.set_password(_KEYRING_SERVICE, _KEYRING_KEY, new_key.decode())
            return new_key
        except Exception as e:
            logger.debug(f"keyring unavailable: {e}")
            return None

    def _init_cipher(self) -> bool:
        if Fernet is None:
            logger.warning(
                "cryptography unavailable — command history will not persist "
                "this session (in-memory only)"
            )
            return False
        key = self._key_provider()
        if not key:
            logger.warning(
                "No OS keyring backend — command history will not persist this "
                "session (in-memory only)"
            )
            return False
        try:
            self._fernet = Fernet(key)
            return True
        except Exception as e:
            logger.debug(f"cipher init failed: {e}")
            return False

    # Server instance identity --------------------------------------------
    @staticmethod
    def resolve_instance(client) -> Optional[Tuple[str, int]]:
        """Return (server_id, started_at) for the connected server, or None.

        server_id comes from GetServerIdentity (stable across restarts); started_at
        from QueryServers, whose value is fixed at the server's process start and so
        changes on restart. Returns None if either no-auth call fails.
        """
        try:
            ident = client.get_server_identity()
            if not ident or not ident.get('server_id'):
                return None
            server_id = ident['server_id']
            status = client.query_server_status(server_id=server_id)
            if not status:
                return None
            started_at = None
            for srv in status.get('servers', []):
                if srv.get('identity', {}).get('server_id') == server_id:
                    started_at = srv.get('identity', {}).get('started_at')
                    break
            if started_at is None:
                return None
            return (server_id, int(started_at))
        except Exception as e:
            logger.debug(f"resolve_instance failed: {e}")
            return None

    # Load / append --------------------------------------------------------
    def load(self, instance: Optional[Tuple[str, int]]) -> None:
        """Load persisted history for this server instance into readline.

        Runs in-memory-only (no file) when instance is unknown or crypto/keyring is
        unavailable. Starts empty when the server restarted (started_at changed).
        """
        if readline is not None:
            try:
                readline.set_history_length(self.max_entries)
            except Exception:
                pass

        if instance is None or not self._init_cipher():
            return

        fernet = self._fernet
        if fernet is None:  # unreachable after _init_cipher True; narrows for type checker
            return

        server_id, started_at = instance
        safe = "".join(c for c in server_id if c.isalnum() or c in ('-', '_')) or "server"
        self._path = self.dir / f"{safe}.enc"
        self._started_at = started_at
        self._active = True

        commands: List[str] = []
        if self._path.exists():
            try:
                blob = self._path.read_bytes()
                data = json.loads(fernet.decrypt(blob).decode())
                if int(data.get('started_at', -1)) == started_at:
                    commands = list(data.get('commands', []))[-self.max_entries:]
                # else: server restarted — start empty; overwritten on next append
            except Exception as e:
                # Bad key, tampered/corrupt file, or unparseable — start fresh.
                logger.debug(f"history unreadable, starting fresh: {e}")

        self._commands = commands
        if readline is not None and commands:
            try:
                readline.clear_history()
                for c in commands:
                    readline.add_history(c)
            except Exception:
                pass

    def append(self, command: str) -> None:
        """Record a command and persist. No-op when the store is in-memory-only."""
        if not self._active or not command:
            return
        self._commands.append(command)
        if len(self._commands) > self.max_entries:
            self._commands = self._commands[-self.max_entries:]
        self._flush()

    def _flush(self) -> None:
        if not self._active or self._path is None or self._fernet is None:
            return
        try:
            payload = json.dumps(
                {'started_at': self._started_at, 'commands': self._commands}
            ).encode()
            blob = self._fernet.encrypt(payload)
            tmp = self._path.with_name(self._path.name + '.tmp')
            tmp.write_bytes(blob)
            if os.name != 'nt':
                os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
            if os.name != 'nt':
                os.chmod(self._path, 0o600)
        except Exception as e:
            logger.debug(f"history flush failed: {e}")
