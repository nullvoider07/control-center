#!/usr/bin/env python3
"""xdotool-compatible actuation for Wayland, backed by the RemoteDesktop portal.

Wayland compositors refuse to let one client synthesise input into another, so
xdotool reaches XWayland clients only and cannot touch a native-Wayland window.
The sanctioned route is org.freedesktop.portal.RemoteDesktop: consent-gated,
revocable from GNOME Settings, and needing neither root, the `input` group, nor
/dev/uinput.

Two properties of that portal shape this file.

1. A portal session is bound to its D-Bus connection. Measured: a button held
   inside a session is RELEASED when the connection closes. cc's grammar has
   `hold` and `release` as separate commands, so a process-per-command helper
   cannot implement it. Hence the daemon: one long-lived session, with a thin
   one-shot client that speaks the argv the controller already emits.
2. Only NotifyPointerMotionAbsolute takes coordinates, and it requires a
   ScreenCast stream - opening a screen capture merely to click at 433,360.
   Relative motion needs no stream, so absolute targeting is done as a closed
   loop: read the pointer, emit the delta, verify, correct. That also absorbs
   any pointer acceleration the compositor applies, which an open-loop delta
   would silently lose.

The CLI is deliberately a subset of xdotool's, matching exactly what
LinuxActuation emits, so the controller's argv is unchanged but for argv[0].

  daemon:  wayland_portal.py --daemon
  client:  wayland_portal.py mousemove 400 300 click 1
"""

import ctypes
import ctypes.util
import errno
import hmac
import json
import math
import os
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time
from typing import Dict, List, NoReturn, Optional, Tuple

try:
    from jeepney import DBusAddress, MatchRule, message_bus, new_method_call
    from jeepney.io.blocking import open_dbus_connection
except ImportError:  # daemon-only dependency; the client reports it on connect
    DBusAddress = None

PORTAL_IFACE = "org.freedesktop.portal.RemoteDesktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_BUS = "org.freedesktop.portal.Desktop"

DEVICE_KEYBOARD = 1
DEVICE_POINTER = 2
PERSIST_UNTIL_REVOKED = 2

# ScreenCast source types and cursor modes.
SOURCE_MONITOR = 1
# METADATA means the compositor reports the cursor position alongside each
# buffer instead of drawing it into the frames. It is the only way a Wayland
# client can learn where the pointer is, and it is why this file opens a
# screencast at all - no frame is ever inspected.
CURSOR_MODE_METADATA = 4

# evdev button codes (linux/input-event-codes.h), keyed by xdotool button number.
# xdotool numbers 2 middle and 3 right; evdev orders them the other way, which is
# an easy transposition to make and impossible to see in a log.
XDOTOOL_BUTTON_TO_EVDEV = {1: 0x110, 2: 0x112, 3: 0x111}

# xdotool spells scroll as buttons 4-7. The portal has a separate axis call.
# axis 0 is vertical, 1 is horizontal; negative steps are up/left by the Wayland
# sign convention.
XDOTOOL_BUTTON_TO_AXIS = {4: (0, -1), 5: (0, 1), 6: (1, -1), 7: (1, 1)}

STATE_PRESSED = 1
STATE_RELEASED = 0

# xdotool accepts these modifier aliases; they are not X keysym names, so
# XStringToKeysym returns 0 for them and they must be mapped first.
KEY_ALIASES = {
    "ctrl": "Control_L", "control": "Control_L",
    "shift": "Shift_L", "alt": "Alt_L", "meta": "Meta_L",
    "super": "Super_L", "cmd": "Super_L", "win": "Super_L",
}

SUBCOMMANDS = frozenset({
    "mousemove", "click", "mousedown", "mouseup",
    "keydown", "keyup", "key", "type", "getmouselocation",
})

MAX_MOVE_ITERATIONS = 6
DEBUG = bool(os.environ.get("CC_WAYLAND_DEBUG"))
MOVE_POLL_S = 0.008
MOVE_CONFIRM_TIMEOUT_S = 0.25
# Over-drive used to clamp the pointer into a corner of known coordinates.
CLAMP_OVERSHOOT = 3000
CLAMP_REPEATS = 3
DEFAULT_KEY_DELAY_S = 0.012
# Bound on a silent re-negotiation. A stored grant answers in well under a
# second; anything slower is a dialog that is not going to be answered.
RECONNECT_TIMEOUT_S = 5

# Human-like pointer travel, off by default.
#
# This changes the path the pointer takes between two points, never the command
# grammar: `1347 248 move` is the same command with the same recorded form
# whether it glides or jumps. So enabling it cannot change what a corpus session
# records, only what the frames between two steps look like.
#
# Only the absolute-motion route can do this. A relative-motion session absorbs
# each delta through the compositor's pointer acceleration, which is a function
# of speed - so splitting one delta into sixty would not travel the same
# distance, and the closed loop below would spend its iterations correcting a
# path it created. Smoothing is therefore tied to the screencast stream, not to
# Wayland.
SMOOTH_MOVE = bool(os.environ.get("CC_SMOOTH_MOVE"))
SMOOTH_STEP_HZ = 120
SMOOTH_MIN_MS = 120
SMOOTH_MAX_MS = 900
# Travel time per pixel of distance. 0.55 puts a full 1920px traverse at ~760ms
# and a 100px nudge at ~175ms, which is the range unaided human pointing falls
# in. It is not a speed limit: the clamps above are.
SMOOTH_MS_PER_PX = 0.55


# --------------------------------------------------------------------------
# X server readback. XWayland reports the live global pointer position and
# button mask, which is the only self-reporting signal available here: the
# portal calls return no acknowledgement that anything moved.
# --------------------------------------------------------------------------

class X11Readback:
    """Pointer position and button state via XQueryPointer.

    Every entry point declares argtypes/restype. Display* is a 64-bit pointer
    and ctypes truncates it to int without them, which segfaults rather than
    failing.
    """

    BUTTON_MASKS = {1: 1 << 8, 2: 1 << 9, 3: 1 << 10}

    def __init__(self):
        path = ctypes.util.find_library("X11")
        if not path:
            raise RuntimeError("libX11 not found; cannot read pointer position")
        x = ctypes.CDLL(path)
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XOpenDisplay.restype = ctypes.c_void_p
        x.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x.XDefaultRootWindow.restype = ctypes.c_ulong
        x.XQueryPointer.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint)]
        x.XQueryPointer.restype = ctypes.c_int
        x.XStringToKeysym.argtypes = [ctypes.c_char_p]
        x.XStringToKeysym.restype = ctypes.c_ulong
        x.XDisplayWidth.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x.XDisplayWidth.restype = ctypes.c_int
        x.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x.XDisplayHeight.restype = ctypes.c_int
        self._x = x
        self._display_name = (os.environ.get("DISPLAY") or ":0").encode()

    def query(self) -> Tuple[Tuple[int, int], int]:
        dpy = self._x.XOpenDisplay(self._display_name)
        if not dpy:
            raise RuntimeError(
                f"cannot open X display {self._display_name.decode()!r}; "
                "absolute positioning needs XWayland")
        try:
            root = self._x.XDefaultRootWindow(dpy)
            rr, cr = ctypes.c_ulong(), ctypes.c_ulong()
            rx, ry, wx, wy = (ctypes.c_int() for _ in range(4))
            mask = ctypes.c_uint()
            ok = self._x.XQueryPointer(
                dpy, root, ctypes.byref(rr), ctypes.byref(cr),
                ctypes.byref(rx), ctypes.byref(ry),
                ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(mask))
            if not ok:
                raise RuntimeError("XQueryPointer failed")
            return (rx.value, ry.value), mask.value
        finally:
            self._x.XCloseDisplay(dpy)

    def position(self) -> Tuple[int, int]:
        return self.query()[0]

    def screen_size(self) -> Tuple[int, int]:
        dpy = self._x.XOpenDisplay(self._display_name)
        if not dpy:
            raise RuntimeError("cannot open X display to read the screen size")
        try:
            return (self._x.XDisplayWidth(dpy, 0), self._x.XDisplayHeight(dpy, 0))
        finally:
            self._x.XCloseDisplay(dpy)

    def keysym(self, name: str) -> int:
        ks = self._x.XStringToKeysym(KEY_ALIASES.get(name.lower(), name).encode())
        if ks == 0:
            raise ValueError(f"unknown key name: {name}")
        return ks


# --------------------------------------------------------------------------
# Portal session
# --------------------------------------------------------------------------

def _private_dir(base: str) -> str:
    """`<base>/control-center`, created 0700 and re-asserted 0700 if it existed.

    `makedirs(mode=0o700)` applies the mode only when it creates the directory:
    `exist_ok=True` accepts a pre-existing one at whatever mode it happens to
    carry. Everything written below is 0600 in its own right, so this is depth
    rather than the barrier - but a directory that has been group-readable since
    some unrelated tool created it is the kind of thing that is true for years
    before it matters.
    """
    d = os.path.join(base, "control-center")
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _state_dir() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return _private_dir(base)


def _token_path() -> str:
    return os.path.join(_state_dir(), "portal_restore_token.json")


class PortalSession:
    """One RemoteDesktop session, restored silently once consent has been given.

    The restore token is a durable, unprompted input-synthesis capability for
    anyone who can read it AND reach this user's session bus, so it is written
    0600 and never logged.
    """

    def __init__(self):
        if DBusAddress is None:
            raise RuntimeError("python3-jeepney is required for the Wayland backend")
        self.addr = DBusAddress(PORTAL_PATH, bus_name=PORTAL_BUS,
                                interface=PORTAL_IFACE)
        self.sc_addr = DBusAddress(PORTAL_PATH, bus_name=PORTAL_BUS,
                                   interface=SCREENCAST_IFACE)
        # enable_fds is required: OpenPipeWireRemote returns the PipeWire socket
        # as a unix fd, and a connection without it cannot receive one.
        self.conn = open_dbus_connection(bus="SESSION", enable_fds=True)
        self._base = self.conn.unique_name[1:].replace(".", "_")
        self._seq = 0
        self.conn.send_and_get_reply(message_bus.AddMatch(MatchRule(
            type="signal", interface="org.freedesktop.portal.Request",
            member="Response")))
        self.session: Optional[str] = None
        # Set once the screencast half of the session is up. When node_id is
        # None every caller falls back to the relative-motion paths, so the
        # screencast is an upgrade rather than a requirement.
        self.node_id: Optional[int] = None
        self.stream_size: Optional[Tuple[int, int]] = None
        self.pw_fd: Optional[int] = None

    # -- request/response plumbing -----------------------------------------
    def _request(self, member, signature, body_fn, timeout, label, addr=None):
        self._seq += 1
        tok = f"r{self._seq}"
        path = f"/org/freedesktop/portal/desktop/request/{self._base}/{tok}"
        reply = self.conn.send_and_get_reply(
            new_method_call(addr or self.addr, member, signature, body_fn(tok)))
        if reply.header.message_type.name == "error":
            raise RuntimeError(f"{label}: {reply.header.fields.get(4)}: {reply.body}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self.conn.receive(timeout=max(0.5, deadline - time.monotonic()))
            except Exception:
                continue
            if msg.header.message_type.name != "signal":
                continue
            if str(msg.header.fields.get(1)) != path:
                continue
            code, results = msg.body[0], msg.body[1]
            if code != 0:
                raise RuntimeError(f"{label}: refused (response code {code})")
            return results
        raise RuntimeError(f"{label}: timed out after {timeout}s")

    def _notify(self, member, signature, body):
        reply = self.conn.send_and_get_reply(
            new_method_call(self.addr, member, signature, body))
        if reply.header.message_type.name == "error":
            raise RuntimeError(f"{member}: {reply.header.fields.get(4)}")

    # -- lifecycle ----------------------------------------------------------
    def start(self, consent_timeout: int = 180) -> None:
        """Bring up the session, re-consenting once if the stored grant is stale.

        A restore token records the shape of the grant it was minted from. One
        minted before this file asked for a screencast restores an INPUT-ONLY
        session: SelectSources is accepted, Start returns no `streams`, and no
        error is raised anywhere. Writing the resulting token back then mints
        another input-only token, so the session never acquires a stream again
        no matter how many times it restarts - a stale grant that repairs itself
        into permanence. Measured, on a token from before the screencast half
        existed.

        So a missing stream after a restore is treated as a stale grant rather
        than as a refusal: the token is discarded and consent asked once.
        """
        stored, stored_has_stream = self._load_token()
        got_stream = self._negotiate(stored, stored_has_stream, consent_timeout)

        if stored and not got_stream and self.node_id is None:
            print("wayland: the stored grant covers input only; asking once for "
                  "screen access so the pointer position can be read",
                  file=sys.stderr, flush=True)
            self._discard_token()
            self._reset_connection()
            self._negotiate(None, False, consent_timeout)

    def reconnect(self) -> bool:
        """Re-negotiate a session that has stopped accepting input.

        Observed live: NotifyPointerMotionAbsolute begins failing with a bare
        org.freedesktop.DBus.Error.Failed while the ScreenCast half of the same
        session keeps delivering frames - so the cursor reader stays live and
        every capability check still says yes. Cause not established; what is
        established is that the grant is intact, because re-negotiating from the
        stored token restores it with no consent dialog.

        Silent by construction: a short timeout and a stored token. If this ever
        does need consent it is not a stale session but a revoked grant, and
        raising a dialog under a command the operator did not associate with one
        would be worse than the failure it replaces.
        """
        self._reset_connection()
        stored, stored_has_stream = self._load_token()
        if not stored:
            return False
        try:
            return self._negotiate(stored, stored_has_stream, RECONNECT_TIMEOUT_S)
        except Exception:
            return False

    # -- token ---------------------------------------------------------------
    @staticmethod
    def _load_token() -> Tuple[Optional[str], bool]:
        tp = _token_path()
        if not os.path.exists(tp):
            return None, False
        try:
            blob = json.load(open(tp))
        except (ValueError, OSError):
            return None, False
        return blob.get("restore_token"), bool(blob.get("screencast"))

    @staticmethod
    def _store_token(token: str, has_stream: bool) -> None:
        fd = os.open(_token_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"restore_token": token, "screencast": has_stream}, fh)

    @staticmethod
    def _discard_token() -> None:
        try:
            os.unlink(_token_path())
        except OSError:
            pass

    def _reset_connection(self) -> None:
        """Drop the D-Bus connection and open a fresh one.

        A session handle cannot be reused once Start has been answered, and the
        Request paths are derived from the connection's unique name, so a retry
        needs its own connection rather than another session on this one.
        """
        if self.pw_fd is not None:
            try:
                os.close(self.pw_fd)
            except OSError:
                pass
            self.pw_fd = None
        try:
            self.conn.close()
        except Exception:
            pass
        self.conn = open_dbus_connection(bus="SESSION", enable_fds=True)
        self._base = self.conn.unique_name[1:].replace(".", "_")
        self._seq = 0
        self.conn.send_and_get_reply(message_bus.AddMatch(MatchRule(
            type="signal", interface="org.freedesktop.portal.Request",
            member="Response")))
        self.session = None
        self.node_id = None
        self.stream_size = None

    # -- negotiation ---------------------------------------------------------
    def _negotiate(self, stored: Optional[str], stored_has_stream: bool,
                   consent_timeout: int) -> bool:
        """One CreateSession/SelectDevices/SelectSources/Start round.

        Returns True if the session came up with a usable screencast stream.
        """
        res = self._request(
            "CreateSession", "a{sv}",
            lambda t: ({"handle_token": ("s", t),
                        "session_handle_token": ("s", f"cc{os.getpid()}_{self._seq}")},),
            15, "CreateSession")
        self.session = res["session_handle"][1]

        def devices_body(t):
            opts = {"handle_token": ("s", t),
                    "types": ("u", DEVICE_KEYBOARD | DEVICE_POINTER),
                    "persist_mode": ("u", PERSIST_UNTIL_REVOKED)}
            if stored:
                # For RemoteDesktop the restore token is a SelectDevices option.
                # Passing it to Start instead is silently ignored and the user is
                # prompted again, which reads as persistence not working.
                opts["restore_token"] = ("s", stored)
            return (self.session, opts)

        self._request("SelectDevices", "oa{sv}", devices_body, 15, "SelectDevices")

        # The screencast half rides on the SAME session handle, which is what
        # makes it one consent dialog rather than two: the portal has a single
        # session to describe, so it describes it once. It must NOT carry its own
        # persist_mode - the portal refuses that outright with "Remote desktop
        # sessions cannot persist". The RemoteDesktop grant covers both halves,
        # and one restore token brings both back silently.
        want_stream = True
        try:
            self._request(
                "SelectSources", "oa{sv}",
                lambda t: (self.session, {"handle_token": ("s", t),
                                          "types": ("u", SOURCE_MONITOR),
                                          "multiple": ("b", False),
                                          "cursor_mode": ("u", CURSOR_MODE_METADATA)}),
                15, "SelectSources", addr=self.sc_addr)
        except Exception as e:
            # Degrade rather than fail: without a stream the pointer paths fall
            # back to relative motion and position reporting to its old,
            # unverified route. Everything that worked before still works.
            want_stream = False
            print(f"wayland: screencast unavailable ({e}); absolute motion and "
                  "cursor readback are disabled", file=sys.stderr, flush=True)

        # A token minted before the screencast half existed describes a narrower
        # grant, so adding the stream re-prompts. That is a human-scale wait, and
        # timing it as if it were a silent restore reports a failure to a user who
        # is about to consent.
        silent = bool(stored) and (stored_has_stream or not want_stream)
        res = self._request(
            "Start", "osa{sv}",
            lambda t: (self.session, "", {"handle_token": ("s", t)}),
            25 if silent else consent_timeout, "Start")

        got_stream = False
        if want_stream:
            streams = res.get("streams")
            if streams and streams[1]:
                node_id, props = streams[1][0][0], streams[1][0][1]
                self.node_id = int(node_id)
                size = props.get("size")
                if size:
                    self.stream_size = (int(size[1][0]), int(size[1][1]))
                got_stream = True
                try:
                    self.pw_fd = self._open_pipewire_remote()
                except Exception as e:
                    self.node_id = None
                    got_stream = False
                    print(f"wayland: OpenPipeWireRemote failed ({e}); cursor "
                          "readback disabled", file=sys.stderr, flush=True)

        tok = res.get("restore_token")
        if tok:
            # The flag records what this grant actually covers, so a later start
            # can tell a silent restore from one that must re-consent. Writing it
            # unconditionally true would recreate the stale-grant loop above.
            self._store_token(tok[1], got_stream)
        return got_stream

    def _open_pipewire_remote(self) -> int:
        reply = self.conn.send_and_get_reply(
            new_method_call(self.sc_addr, "OpenPipeWireRemote", "oa{sv}",
                            (self.session, {})))
        if reply.header.message_type.name == "error":
            raise RuntimeError(f"OpenPipeWireRemote: {reply.header.fields.get(4)}")
        handle = reply.body[0]
        fd = handle.to_raw_fd() if hasattr(handle, "to_raw_fd") else int(handle)
        os.set_inheritable(fd, True)
        return fd

    def close(self) -> None:
        if self.pw_fd is not None:
            try:
                os.close(self.pw_fd)
            except OSError:
                pass
            self.pw_fd = None
        try:
            self.conn.close()
        except Exception:
            pass

    # -- input primitives ---------------------------------------------------
    def motion(self, dx: float, dy: float) -> None:
        self._notify("NotifyPointerMotion", "oa{sv}dd",
                     (self.session, {}, float(dx), float(dy)))

    def motion_absolute(self, x: float, y: float) -> None:
        """Place the pointer exactly. Requires the screencast stream.

        This is what retires the corner-clamping route below: coordinates are
        stream-relative and the compositor applies no acceleration to them, so
        there is nothing to observe, correct, or drift.
        """
        if self.node_id is None:
            raise RuntimeError("absolute motion needs the screencast stream")
        self._notify("NotifyPointerMotionAbsolute", "oa{sv}udd",
                     (self.session, {}, int(self.node_id), float(x), float(y)))

    def button(self, evdev_code: int, state: int) -> None:
        self._notify("NotifyPointerButton", "oa{sv}iu",
                     (self.session, {}, int(evdev_code), int(state)))

    def axis_discrete(self, axis: int, steps: int) -> None:
        self._notify("NotifyPointerAxisDiscrete", "oa{sv}ui",
                     (self.session, {}, int(axis), int(steps)))

    def keysym(self, ks: int, state: int) -> None:
        self._notify("NotifyKeyboardKeysym", "oa{sv}iu",
                     (self.session, {}, int(ks), int(state)))


class CursorTracker:
    """The live pointer position, from the compositor, via the screencast stream.

    The stream is held open for the life of the daemon and a helper follows it,
    printing a line whenever the position changes. Measured on GNOME 50: 679 of
    679 buffers carried valid cursor metadata over one minute, about 11 updates
    a second, tracking both synthetic motion and the physical mouse. So a
    position read is a lookup of a value at most ~90ms old, not a probe.

    Holding the stream open is what makes absolute motion available between
    commands as well; the cost is that the compositor shows its screen-sharing
    indicator for as long as the daemon runs.

    The helper is C because the request that matters - ParamMeta for
    SPA_META_Cursor, negotiated after the format - has no binding reachable from
    ctypes without hand-building SPA pods. It is compiled on first use and
    cached. If it cannot be built, this class reports unavailable and every
    caller falls back to the previous behaviour.
    """

    SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "wayland_cursor.c")

    def __init__(self, session: "PortalSession"):
        self.session = session
        self.proc: Optional[subprocess.Popen] = None
        self.thread = None
        self._pos: Optional[Tuple[int, int]] = None
        self._updated = 0.0
        self._error: Optional[str] = None

    # -- build ---------------------------------------------------------------
    @classmethod
    def _binary_path(cls) -> str:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
        return os.path.join(_private_dir(base), "wayland_cursor")

    @classmethod
    def build(cls) -> str:
        """Return the helper path, compiling it if the cached copy is stale."""
        binary = cls._binary_path()
        if not os.path.exists(cls.SOURCE):
            raise RuntimeError(f"{cls.SOURCE} is missing")
        if (os.path.exists(binary)
                and os.path.getmtime(binary) >= os.path.getmtime(cls.SOURCE)):
            return binary
        try:
            flags = subprocess.run(
                ["pkg-config", "--cflags", "--libs", "libpipewire-0.3"],
                capture_output=True, text=True, check=True).stdout.split()
        except (OSError, subprocess.CalledProcessError) as e:
            raise RuntimeError(
                "libpipewire-0.3 development files are needed to read the "
                "cursor position (install libpipewire-0.3-dev)") from e
        tmp = binary + f".{os.getpid()}"
        cmd = ["cc", "-O2", "-o", tmp, cls.SOURCE] + flags
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
        except OSError as e:
            raise RuntimeError(f"no C compiler available: {e}") from e
        if r.returncode != 0:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise RuntimeError(f"building {cls.SOURCE} failed: {r.stderr.strip()}")
        # Rename into place so a concurrent daemon never sees a partial binary.
        os.replace(tmp, binary)
        return binary

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> bool:
        if self.session.node_id is None or self.session.pw_fd is None:
            self._error = "no screencast stream"
            return False
        try:
            binary = self.build()
        except RuntimeError as e:
            self._error = str(e)
            print(f"wayland: cursor readback unavailable: {e}",
                  file=sys.stderr, flush=True)
            return False

        env = dict(os.environ, CC_CURSOR_FOLLOW="1")
        try:
            # timeout 0 means no timer: the helper runs until the daemon stops it.
            self.proc = subprocess.Popen(
                [binary, str(self.session.pw_fd), str(self.session.node_id), "0"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, env=env, pass_fds=(self.session.pw_fd,))
        except OSError as e:
            self._error = str(e)
            return False

        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()
        # The first line arrives as soon as the stream negotiates. Waiting for it
        # turns "is the tracker working" from a guess into an observation, and
        # the answer is needed before the first command is served.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self._pos is not None:
                return True
            if self.proc.poll() is not None:
                self._error = "cursor helper exited immediately"
                return False
            time.sleep(0.05)
        self._error = "no cursor metadata within 5s"
        self.stop()
        return False

    def _pump(self) -> None:
        try:
            for line in self.proc.stdout:
                parts = line.split()
                if len(parts) == 2:
                    try:
                        self._pos = (int(parts[0]), int(parts[1]))
                        self._updated = time.monotonic()
                    except ValueError:
                        pass
        except (OSError, ValueError):
            pass

    def stop(self) -> None:
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.proc.kill()
                except OSError:
                    pass
            self.proc = None

    # -- reading -------------------------------------------------------------
    @property
    def alive(self) -> bool:
        return (self.proc is not None and self.proc.poll() is None
                and self._pos is not None)

    def position(self) -> Optional[Tuple[int, int]]:
        """The current pointer position, or None if the tracker is not live."""
        return self._pos if self.alive else None

    @property
    def error(self) -> Optional[str]:
        return self._error


# --------------------------------------------------------------------------
# xdotool argv -> portal calls
# --------------------------------------------------------------------------

class UsageError(Exception):
    """Argv the helper refuses. Deny-by-default: an unrecognised token is an
    error, never something to guess at or pass through."""


def _int(tok: str, what: str) -> int:
    try:
        return int(tok)
    except ValueError:
        raise UsageError(f"{what}: expected an integer, got {tok!r}")


# Bounds and vocabulary mirrored from crates/agent/src/argv_policy.rs. They are
# duplicated rather than shared because the two live in different languages, so
# they are named here with the same values and the same reasoning, and the
# agreement is asserted by tests/unit/test_wayland_portal_grammar.py.
#
# The agent validates every argv it forwards, so nothing arriving over gRPC needs
# these. What needs them is an argv arriving directly on the daemon socket, which
# the agent never sees. Until this existed the daemon was a second grammar for one
# language - the thing this file's own docstring gives as the reason the helper
# shares xdotool's - and it was already the more permissive of the two.
POINTER_MODIFIERS = frozenset({"ctrl", "shift", "alt", "super"})
MAX_REPEAT = 1000
MAX_TYPE_DELAY_MS = 1000


def _check_modifier_bracket(ops: List[tuple]) -> None:
    """Require a held modifier to be released inside the same invocation.

    `keydown ctrl` sets the modifier on the display server, not on the process.
    Over the socket the process is a long-lived daemon, so an unclosed hold does
    not merely outlive one command - it stays down until something releases it,
    and a held key suppresses the idle timeout, so the daemon that owns it stops
    expiring too. Same defect as the cliclick `kd:` and xdotool `keydown`
    stranding this project has already fixed twice, on the one path that had no
    check.

    The accepted shape is the one the controller emits and the one
    `check_pointer_modifier_bracket` accepts: every `keydown` in a leading run,
    every `keyup` in a trailing run, the same modifiers in each, and at least one
    action bracketed between them.

    This runs on the parsed ops rather than on the raw tokens, which is the one
    deliberate difference from the Rust. `xdotool type -- keyup` is a command that
    types the word "keyup"; on the token stream that payload is indistinguishable
    from structure, and reading it as structure would refuse a legitimate command.
    The Rust never meets the case because it dispatches on argv[0] and its `type`
    arm cannot chain a pointer verb at all. This parser can chain, so it checks
    where the two are already separated.
    """
    i = 0
    downs: List[str] = []
    while i < len(ops) and ops[i][0] == "keydown":
        downs.append(ops[i][1])
        i += 1
    action_start = i
    while i < len(ops) and ops[i][0] not in ("keydown", "keyup"):
        i += 1
    action_end = i
    ups: List[str] = []
    while i < len(ops) and ops[i][0] == "keyup":
        ups.append(ops[i][1])
        i += 1

    # Structure first, and before the "no modifiers here" exit: a keydown sitting
    # in the INTERIOR of the chain leaves both runs empty at this point, so
    # exiting early on that emptiness would wave through exactly the shape being
    # guarded against.
    if i != len(ops):
        raise UsageError(
            "a modifier hold must open the chain and close it, with the action "
            "between")
    if not downs and not ups:
        return
    if action_end == action_start:
        raise UsageError("a modifier hold must bracket at least one action")
    if sorted(downs) != sorted(ups):
        raise UsageError(
            f"modifiers held ({','.join(downs) or 'none'}) are not the ones "
            f"released ({','.join(ups) or 'none'})")


def parse_chain(tokens: List[str]) -> List[tuple]:
    """Parse a chained xdotool command line into ops.

    xdotool takes several sub-commands in one invocation and the controller
    relies on it: a modified click is `keydown ctrl mousemove X Y click 1
    keyup ctrl`, one process, so the release cannot be lost between commands.
    """
    ops: List[tuple] = []
    i = 0
    n = len(tokens)
    while i < n:
        sub = tokens[i]
        i += 1
        if sub == "mousemove":
            # The grok port inserts `--` before coordinates so a negative x is
            # not read as an option by getopt_long. Accept and skip it.
            if i < n and tokens[i] == "--":
                i += 1
            if i + 1 >= n:
                raise UsageError("mousemove: expected x and y")
            ops.append(("mousemove", _int(tokens[i], "mousemove x"),
                        _int(tokens[i + 1], "mousemove y")))
            i += 2
        elif sub in ("click", "mousedown", "mouseup"):
            repeat = 1
            while i < n and tokens[i].startswith("--"):
                if tokens[i] == "--repeat":
                    if sub != "click":
                        raise UsageError(f"{sub}: --repeat is only valid for click")
                    if i + 1 >= n:
                        raise UsageError("click: --repeat needs a value")
                    repeat = _int(tokens[i + 1], "click --repeat")
                    if not 1 <= repeat <= MAX_REPEAT:
                        raise UsageError(
                            f"click: --repeat {repeat} out of range 1..{MAX_REPEAT}")
                    i += 2
                elif tokens[i] == "--":
                    i += 1
                    break
                else:
                    raise UsageError(f"{sub}: unsupported option {tokens[i]!r}")
            if i >= n:
                raise UsageError(f"{sub}: expected a button")
            ops.append((sub, _int(tokens[i], f"{sub} button"), repeat))
            i += 1
        elif sub in ("keydown", "keyup"):
            if i >= n:
                raise UsageError(f"{sub}: expected a key")
            # Modifiers only. This branch exists so a click can be modified, not
            # so any key can be held down through the pointer path - and an
            # arbitrary keysym left down is the harder one to notice.
            if tokens[i] not in POINTER_MODIFIERS:
                raise UsageError(
                    f"{sub}: {tokens[i]!r} may not be held for a pointer action")
            ops.append((sub, tokens[i]))
            i += 1
        elif sub == "key":
            specs = []
            while i < n and tokens[i] not in SUBCOMMANDS:
                specs.append(tokens[i])
                i += 1
            if not specs:
                raise UsageError("key: expected at least one key spec")
            ops.append(("key", specs))
        elif sub == "type":
            delay = DEFAULT_KEY_DELAY_S
            while i < n and tokens[i].startswith("--"):
                if tokens[i] == "--delay":
                    if i + 1 >= n:
                        raise UsageError("type: --delay needs a value")
                    delay_ms = _int(tokens[i + 1], "type --delay")
                    # Per keystroke, so it multiplies by the payload length.
                    if not 0 <= delay_ms <= MAX_TYPE_DELAY_MS:
                        raise UsageError(
                            f"type: --delay {delay_ms} out of range "
                            f"0..{MAX_TYPE_DELAY_MS}")
                    delay = delay_ms / 1000.0
                    i += 2
                elif tokens[i] == "--clearmodifiers":
                    i += 1
                elif tokens[i] == "--":
                    i += 1
                    break
                else:
                    raise UsageError(f"type: unsupported option {tokens[i]!r}")
            if i >= n:
                raise UsageError("type: expected exactly one text argument")
            ops.append(("type", tokens[i], delay))
            i += 1
        elif sub == "getmouselocation":
            shell = False
            while i < n and tokens[i].startswith("--"):
                if tokens[i] == "--shell":
                    shell = True
                    i += 1
                else:
                    raise UsageError(f"getmouselocation: unsupported option {tokens[i]!r}")
            ops.append(("getmouselocation", shell))
        else:
            raise UsageError(f"unsupported sub-command: {sub!r}")
    if not ops:
        raise UsageError("no sub-command given")
    _check_modifier_bracket(ops)
    return ops


def _glide_path(x0: int, y0: int, x1: int, y1: int
                ) -> Tuple[List[Tuple[float, float]], float]:
    """Intermediate points from (x0,y0) to (x1,y1), and the seconds to take.

    The timing curve is minimum-jerk, 10t^3 - 15t^4 + 6t^5, which is the
    measured velocity profile of human reaching movement rather than a curve
    picked for looking right: slow at both ends, fastest in the middle. A
    constant-velocity interpolation travels the same path and still reads as
    machine-driven, because it is the acceleration that carries the tell.

    The path itself is straight. Real movement bows slightly and overshoots,
    but both would have to be bounded against the screen edges and neither
    survives the thing that matters here - the last point emitted is the exact
    integer target, so a glide lands where a jump landed.
    """
    distance = math.hypot(x1 - x0, y1 - y0)
    duration_ms = min(SMOOTH_MAX_MS, SMOOTH_MIN_MS + distance * SMOOTH_MS_PER_PX)
    steps = max(2, round(duration_ms / (1000.0 / SMOOTH_STEP_HZ)))

    points: List[Tuple[float, float]] = []
    for i in range(1, steps + 1):
        t = i / steps
        eased = t * t * t * (10.0 + t * (-15.0 + 6.0 * t))
        points.append((x0 + (x1 - x0) * eased, y0 + (y1 - y0) * eased))
    # Exactness is not the easing's to guarantee: at t=1 the curve is 1.0 in
    # real arithmetic and the float that comes back need not be.
    points[-1] = (float(x1), float(y1))
    return points, duration_ms / 1000.0


class Executor:
    """Runs parsed ops against a live portal session, tracking held state."""

    def __init__(self, session: PortalSession, x11: X11Readback,
                 cursor: Optional["CursorTracker"] = None):
        self.session = session
        self.x11 = x11
        # When live, this is the compositor's own view of the pointer and it
        # supersedes every other source below. When it is not, all of them
        # remain exactly as they were.
        self.cursor = cursor
        self.held_buttons: Dict[int, int] = {}   # evdev code -> xdotool number
        self.held_keys: List[int] = []
        # Where this executor last placed the pointer, valid only for the rest of
        # the current argv chain. See move_absolute.
        self._known_pos: Optional[Tuple[int, int]] = None
        # Where the pointer is according to everything this daemon has emitted
        # since the last reading it could verify. Survives across commands, unlike
        # _known_pos: it is what lets a position be reported at all while the
        # X reader is stuck. It cannot see the physical mouse, which is exactly
        # why a position derived from it is reported as unverified.
        self._tracked: Optional[Tuple[int, int]] = None

    def screen_size(self) -> Tuple[int, int]:
        """Screen bounds, preferring the stream's own size.

        The screencast stream reports the monitor it is capturing, which is the
        coordinate space absolute motion is expressed in. Falling back to the X
        reader is right only while there is no stream.
        """
        if self.session.stream_size is not None:
            return self.session.stream_size
        return self.x11.screen_size()

    # -- absolute positioning ----------------------------------------------
    def _await_change(self, previous: Tuple[int, int]) -> Optional[Tuple[int, int]]:
        """Poll the readback until it differs from `previous`, or give up.

        Returns the new position, or None if the reading never changed.
        """
        deadline = time.monotonic() + MOVE_CONFIRM_TIMEOUT_S
        while time.monotonic() < deadline:
            time.sleep(MOVE_POLL_S)
            pos = self.x11.position()
            if pos != previous:
                return pos
        return None

    def _move_by_clamping(self, x: int, y: int) -> None:
        """Reach an absolute point without observing the pointer.

        Two situations stop the readback reporting, and neither stops the motion
        landing: a compositor grab, and - far more commonly - the pointer sitting
        over a native Wayland surface, because XWayland only tracks a pointer that
        is over one of its own. Measured: over a Brave window the reading stayed
        fixed indefinitely while injected scroll events still arrived.

        So the position is established rather than measured. Driving well past an
        edge makes the compositor clamp the pointer to a corner whose coordinates
        follow from the screen size, and one delta from there is exact - confirmed
        by the receiving application reporting the click at the requested point.

        The bottom-right corner is deliberate: the top-left is GNOME's hot corner
        and entering it opens the overview, which is itself a grab.
        """
        w, h = self.x11.screen_size()
        corner = (w - 1, h - 1)
        for _ in range(CLAMP_REPEATS):
            self.session.motion(CLAMP_OVERSHOOT, CLAMP_OVERSHOOT)
            time.sleep(0.02)
        time.sleep(0.08)
        self.session.motion(x - corner[0], y - corner[1])
        time.sleep(0.08)
        if DEBUG:
            print(f"  move: clamped via {corner} then delta to ({x},{y})",
                  file=sys.stderr, flush=True)

    def absolute_motion_works(self) -> bool:
        """Whether the portal will actually accept an absolute placement.

        Measured, not declared. `node_id is not None` says the session was given
        a stream, which is a fact about how it was negotiated and stays true
        after the input half has stopped answering - it reported capable while
        every move was failing. The probe places the pointer where the reader
        already says it is, so it proves the call path with no movement.
        """
        if self.session.node_id is None:
            return False
        at = self.cursor.position() if self.cursor is not None else None
        if at is None:
            at = self._tracked
        if at is None:
            try:
                at = self.x11.position()
            except Exception:
                return False
        try:
            self.session.motion_absolute(at[0], at[1])
            return True
        except RuntimeError:
            return False

    def _place(self, x: float, y: float) -> None:
        """Absolute placement, recovering once from a session gone stale.

        The recovery is refused outright while a button is held. Re-negotiating
        drops every held button - that is how the portal guarantees it cannot
        leave one down - so a silent recovery mid-drag would turn one gesture
        into a press and a release with unrelated motion between them, and
        report success for it. A refusal the operator can see is the better of
        the two.
        """
        try:
            self.session.motion_absolute(x, y)
            return
        except RuntimeError:
            if self.held_buttons:
                raise RuntimeError(
                    "the portal session stopped accepting input mid-gesture; "
                    "recovering it would release the held button and split the "
                    "drag in two, so the command is refused instead")
            if not self.session.reconnect():
                raise
            # The reader held a stream belonging to the session that just went
            # away, so it is now reporting a position nothing updates.
            if self.cursor is not None:
                self.cursor.stop()
                self.cursor.start()
            print("wayland: portal session went stale; re-negotiated it",
                  file=sys.stderr, flush=True)
        self.session.motion_absolute(x, y)

    def _glide_absolute(self, x: int, y: int) -> bool:
        """Travel to (x,y) as a paced sequence of absolute placements.

        Returns False when it declines, so the caller places the pointer the way
        it always did. It declines when there is no trustworthy origin to
        interpolate from: a glide from a wrong start is a straight line through
        the wrong part of the screen with the button possibly held, which is
        worse than the jump it replaces.

        Pacing is against a monotonic schedule rather than a fixed sleep per
        step. Every placement is a blocking D-Bus round trip, so a fixed sleep
        would add the transport cost to each step and stretch the movement by
        however busy the bus happened to be.
        """
        origin = self._known_pos
        if origin is None and self.cursor is not None:
            origin = self.cursor.position()
        if origin is None:
            # Last resort, and guarded: the absolute-motion route exists so that
            # placing the pointer needs no X server at all, so a glide must not
            # be the thing that reintroduces the dependency. No reader, no
            # glide - not a failed command.
            try:
                origin = self.x11.position()
            except Exception:
                return False

        x0, y0 = origin
        if abs(x - x0) <= 1 and abs(y - y0) <= 1:
            return False

        points, duration_s = _glide_path(x0, y0, x, y)
        started = time.monotonic()
        for i, (px, py) in enumerate(points, 1):
            self._place(px, py)
            # Sleep to the point's own place in the schedule, so a slow round
            # trip costs the next step's wait instead of the movement's length.
            remaining = (started + duration_s * i / len(points)) - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
        if DEBUG:
            print(f"  glide: ({x0},{y0}) -> ({x},{y}) in {len(points)} steps, "
                  f"{(time.monotonic() - started) * 1000:.0f}ms",
                  file=sys.stderr, flush=True)
        return True

    def move_absolute(self, x: int, y: int) -> None:
        """Closed loop: emit the delta, confirm it landed, correct the remainder.

        The portal's only coordinate call needs a ScreenCast stream, so this is
        relative motion aimed at an absolute target. Reading back is not
        belt-and-braces: it is what makes the result independent of any pointer
        acceleration the compositor applies to a synthetic delta.

        Each iteration must see the reading change before it emits another delta.
        That is not impatience, it is the difference between failing and doing
        harm. While a compositor grab is active - the GNOME Activities overview
        is the one to know about, and a pointer sent into the top-left hot corner
        opens it - XWayland keeps reporting the position the pointer had when the
        grab began, while the motion itself still lands. Measured: motion emitted
        under a grab applied in full the moment the grab lifted. A loop that kept
        correcting against the frozen reading would therefore queue up every
        delta and fling the pointer across the screen when the overview closed,
        which is worse than the no-op it resembles.
        """
        # An off-screen target is refused rather than attempted. Both routes below
        # reach an edge and stop: the compositor clamps the pointer to the screen,
        # the reading then stops changing, and the clamping fallback "establishes"
        # a position the pointer is not at. The command would report success with
        # the click landing in a corner - actuation recorded, effect absent. The
        # bounds are known without observing anything, so this is decidable up
        # front rather than after the fact.
        w, h = self.screen_size()
        if not (0 <= x < w and 0 <= y < h):
            raise UsageError(
                f"target {x},{y} is outside the {w}x{h} screen; the pointer would "
                "stop at the edge and the command would report a click it did not "
                "perform where it was asked")

        # With the screencast stream up the pointer can simply be placed. No
        # reading, no correction, no clamping through a corner, and no pointer
        # acceleration to absorb - the coordinates are stream-relative and the
        # compositor applies none. Measured 5/5 exact. Everything below this
        # branch exists only for a session that has no stream.
        if self.session.node_id is not None:
            if not (SMOOTH_MOVE and self._glide_absolute(x, y)):
                self._place(x, y)
                time.sleep(MOVE_POLL_S)
            self._known_pos = (x, y)
            self._tracked = (x, y)
            return

        # A move inside a chain starts from a point this executor set itself, so
        # it needs neither a reading nor a clamp: emit the delta and be done. That
        # is not an optimisation. A drag is `mousemove x1 y1 mousedown 1
        # mousemove x2 y2 mouseup 1`, and routing the second move through the
        # corner would drag *through* the corner with the button down - measured,
        # it reduced a line selection to a single character. The path a drag takes
        # is part of the gesture, not an implementation detail.
        if self._known_pos is not None:
            kx, ky = self._known_pos
            if (kx, ky) != (x, y):
                self.session.motion(x - kx, y - ky)
                time.sleep(MOVE_POLL_S * 2)
            self._known_pos = (x, y)
            self._tracked = (x, y)
            return

        for _ in range(MAX_MOVE_ITERATIONS):
            cx, cy = self.x11.position()
            dx, dy = x - cx, y - cy
            if DEBUG:
                print(f"  move: at ({cx},{cy}) target ({x},{y}) delta ({dx},{dy})",
                      file=sys.stderr, flush=True)
            if dx == 0 and dy == 0:
                self._known_pos = (x, y)
                self._tracked = (x, y)
                return
            self.session.motion(dx, dy)
            if self._await_change((cx, cy)) is None:
                self._move_by_clamping(x, y)
                self._known_pos = (x, y)
                self._tracked = (x, y)
                return
        cx, cy = self.x11.position()
        if (cx, cy) != (x, y):
            # The loop ran out of iterations while the reading was still moving,
            # so the reading is live and simply has not converged. Establish the
            # position instead of chasing it.
            self._move_by_clamping(x, y)
        self._known_pos = (x, y)
        self._tracked = (x, y)

    # -- ops ----------------------------------------------------------------
    def button_op(self, kind: str, button: int, repeat: int) -> None:
        if button in XDOTOOL_BUTTON_TO_AXIS:
            if kind != "click":
                raise UsageError(f"{kind}: button {button} is a scroll axis, "
                                 "which cannot be held")
            axis, steps = XDOTOOL_BUTTON_TO_AXIS[button]
            for _ in range(repeat):
                self.session.axis_discrete(axis, steps)
                time.sleep(0.01)
            return
        code = XDOTOOL_BUTTON_TO_EVDEV.get(button)
        if code is None:
            raise UsageError(f"unsupported button: {button}")
        if kind == "click":
            for _ in range(repeat):
                self.session.button(code, STATE_PRESSED)
                time.sleep(0.01)
                self.session.button(code, STATE_RELEASED)
                time.sleep(0.01)
        elif kind == "mousedown":
            self.session.button(code, STATE_PRESSED)
            self.held_buttons[code] = button
        else:
            self.session.button(code, STATE_RELEASED)
            self.held_buttons.pop(code, None)

    def key_spec(self, spec: str) -> None:
        """A chord such as `ctrl+shift+t`: down in order, up in reverse."""
        names = spec.split("+")
        if any(not part for part in names):
            raise UsageError(f"invalid key spec: {spec!r}")
        syms = [self.x11.keysym(part) for part in names]
        for ks in syms:
            self.session.keysym(ks, STATE_PRESSED)
            time.sleep(0.008)
        for ks in reversed(syms):
            self.session.keysym(ks, STATE_RELEASED)
            time.sleep(0.008)

    def type_text(self, text: str, delay: float) -> None:
        for ch in text:
            cp = ord(ch)
            # Latin-1 maps to itself; everything else uses the Unicode keysym
            # encoding from the X protocol (codepoint | 0x01000000).
            ks = cp if cp <= 0xFF else cp + 0x01000000
            self.session.keysym(ks, STATE_PRESSED)
            time.sleep(delay)
            self.session.keysym(ks, STATE_RELEASED)
            time.sleep(delay)

    def run(self, tokens: List[str]) -> str:
        # Dropped per chain: between commands the physical mouse moves the pointer
        # and nothing tells us, so a position carried across commands would be a
        # guess presented as knowledge.
        self._known_pos = None
        ops = parse_chain(tokens)
        out: List[str] = []
        for op in ops:
            kind = op[0]
            if kind == "mousemove":
                self.move_absolute(op[1], op[2])
            elif kind in ("click", "mousedown", "mouseup"):
                self.button_op(kind, op[1], op[2])
            elif kind == "keydown":
                ks = self.x11.keysym(op[1])
                self.session.keysym(ks, STATE_PRESSED)
                self.held_keys.append(ks)
            elif kind == "keyup":
                ks = self.x11.keysym(op[1])
                self.session.keysym(ks, STATE_RELEASED)
                if ks in self.held_keys:
                    self.held_keys.remove(ks)
            elif kind == "key":
                for spec in op[1]:
                    self.key_spec(spec)
            elif kind == "type":
                self.type_text(op[1], op[2])
            elif kind == "getmouselocation":
                px, py, verified = self.resolve_position()
                if op[1]:
                    # POSITION_VERIFIED is an addition to xdotool's shell format.
                    # Readers that do not know it see the same X=/Y=/SCREEN=/WINDOW=
                    # they always did; the one that does can tell a measured
                    # coordinate from a derived one.
                    out.append(f"X={px}\nY={py}\nSCREEN=0\nWINDOW=0\n"
                               f"POSITION_VERIFIED={1 if verified else 0}")
                else:
                    suffix = "" if verified else " (unverified)"
                    out.append(f"x:{px} y:{py} screen:0 window:0{suffix}")
        return "\n".join(out)

    def resolve_position(self) -> Tuple[int, int, bool]:
        """Return (x, y, verified) - and always return something.

        Three sources, in descending order of trust:

        1. The X reader, once it has been proved live. XWayland only observes the
           pointer over its own surfaces; over a native-Wayland window it repeats
           the last position it saw, forever and without error, so two agreeing
           reads are not evidence. It is nudged one pixel and put back: if the
           reading does not follow, it is not a reading.
        2. What this daemon has emitted. Exact for a pointer only cc has moved,
           and blind to the physical mouse - hence unverified, not wrong.
        3. The stuck reader's last value, when nothing else exists.

        Only the first is reported as verified. Refusing outright was the previous
        behaviour and it was honest but useless: `position` is the command an
        operator runs to obtain coordinates, and on a Wayland session most windows
        are native.
        """
        # The compositor's own reading, when the screencast tracker is live. It
        # sees the physical mouse and every window type, so none of the caveats
        # below apply to it and there is nothing to prove about it.
        if self.cursor is not None:
            live = self.cursor.position()
            if live is not None:
                self._tracked = live
                return live[0], live[1], True

        before = self.x11.position()
        self.session.motion(1.0, 0.0)
        time.sleep(MOVE_POLL_S * 3)
        probed = self.x11.position()
        self.session.motion(-1.0, 0.0)
        time.sleep(MOVE_POLL_S * 2)
        if probed != before:
            pos = self.x11.position()
            self._tracked = pos
            return pos[0], pos[1], True
        if self._tracked is not None:
            return self._tracked[0], self._tracked[1], False
        return before[0], before[1], False

    def live_position(self) -> Tuple[int, int]:
        """Read the pointer, having first proved the reader is not stuck.

        XWayland only observes the pointer while it is over one of its own
        surfaces. Over a native-Wayland window it keeps returning the last
        position it saw - forever, and without any error. Two consecutive reads
        therefore agree, which is exactly the evidence a caller uses to conclude
        the value is trustworthy. Measured: with the pointer moved three times
        under a Brave window, this reported one unchanging coordinate each time
        while the page itself reported the true position.

        A stationary pointer and a stuck reader are indistinguishable by reading
        alone, so the reader is tested instead of trusted: nudge one pixel, look,
        and put it back. If the reading did not follow, it is not a reading.

        None of that applies when the screencast tracker is live, which is why it
        is consulted first: the compositor reports the pointer wherever it is.
        """
        if self.cursor is not None:
            live = self.cursor.position()
            if live is not None:
                return live

        before = self.x11.position()
        self.session.motion(1.0, 0.0)
        time.sleep(MOVE_POLL_S * 3)
        probed = self.x11.position()
        self.session.motion(-1.0, 0.0)
        time.sleep(MOVE_POLL_S * 2)
        if probed == before:
            raise UsageError(
                f"the pointer position cannot be read: it reports {before[0]},"
                f"{before[1]} and did not follow a one-pixel test move. XWayland "
                "stops observing the pointer while it is over a native-Wayland "
                "window, so this coordinate is the last one it saw, not where the "
                "pointer is. Move the pointer over an X11/XWayland window, or run "
                "on an X11 session, to read a position here")
        return self.x11.position()

    def release_all(self) -> None:
        """Release everything this session is holding. Idempotent."""
        for code in list(self.held_buttons):
            try:
                self.session.button(code, STATE_RELEASED)
            except Exception:
                pass
            self.held_buttons.pop(code, None)
        for ks in list(self.held_keys):
            try:
                self.session.keysym(ks, STATE_RELEASED)
            except Exception:
                pass
        self.held_keys.clear()


# --------------------------------------------------------------------------
# Daemon and client
# --------------------------------------------------------------------------

def socket_path() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(_private_dir(base), "wayland-actuate.sock")


# --------------------------------------------------------------------------
# Request authentication
#
# What this defends, stated precisely, because it is easy to overclaim. The
# socket is 0600 inside a 0700 directory, so reaching it already requires this
# UID - and a process with this UID can read a 0600 token file exactly as easily
# as it can open a 0600 socket. Against an UNCONFINED same-UID process the token
# is a step, not a boundary, and that path cannot be closed from inside this
# daemon: on Wayland the only thing that could close it is the compositor, which
# has already been asked for the grant.
#
# What it does defend is the CONFINED deputy. A sandboxed application (Flatpak,
# Snap) is routinely given the session bus and XDG_RUNTIME_DIR - putting it
# inside the socket's boundary - while ~/.local/state is outside its sandbox.
# So the token lives in the state directory and MUST NOT move into the runtime
# directory: there it would be readable by exactly the callers it excludes, and
# the whole measure would become decorative.
# --------------------------------------------------------------------------

def _auth_token_path() -> str:
    return os.path.join(_state_dir(), "wayland-actuate.token")


def load_or_create_auth_token() -> str:
    """The shared secret for this user's daemon, minted on first use.

    O_EXCL rather than a check-then-write: two clients racing the first command
    would otherwise each mint one and the loser would authenticate against a
    token the daemon never saw.
    """
    path = _auth_token_path()
    try:
        with open(path, "r") as fh:
            existing = fh.read().strip()
        if existing:
            return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        with open(path, "r") as fh:
            return fh.read().strip()
    with os.fdopen(fd, "w") as fh:
        fh.write(token)
    return token


def _read_auth_token() -> Optional[str]:
    """The token, or None when this caller cannot read it - which is the answer
    for a confined caller, and the reason `ensure_daemon` refuses to spawn."""
    try:
        with open(_auth_token_path(), "r") as fh:
            token = fh.read().strip()
        return token or None
    except OSError:
        return None


def _no_token_reason() -> str:
    """Why there is no token, in terms the operator can act on.

    "Not readable" for a file that does not exist sends someone to check
    permissions on nothing. The two cases have different answers - one is a
    daemon predating the token, the other is the confinement boundary doing its
    job - so they are worth separating even though the code path is the same.
    """
    path = _auth_token_path()
    if not os.path.exists(path):
        return (
            f"no token exists at {path}. It is minted by the first command that "
            "starts the daemon; the control verbs (--status, --ping, --shutdown, "
            "--release-all) deliberately do not create one. If a daemon is already "
            "running it predates this token and should be restarted."
        )
    return (
        f"the token at {path} exists but this process cannot read it. For a "
        "sandboxed caller that is the intended answer: the token sits outside "
        "XDG_RUNTIME_DIR precisely so that reaching the socket is not enough."
    )


# A live session is an unprompted input-synthesis capability, so it is not left
# open indefinitely.
DEFAULT_IDLE_TIMEOUT_S = 1800

# Ceiling on how long a held button or key may suppress the idle timeout.
#
# The timer does not fire while something is held, so that expiry cannot release
# a deliberate `hold` behind the operator's back. On its own that is right;
# composed with a stranded hold it is not, because "held" then never ends and the
# daemon - an input-synthesis capability - becomes permanent. Fifteen minutes is
# far longer than any gesture and far shorter than forever: past it the hold is
# released and the daemon expires, which is the same thing the agent's own
# shutdown path does with a button it finds still down.
MAX_HELD_SUPPRESSION_S = 900


def idle_timeout_s() -> int:
    """Read CC_WAYLAND_IDLE_TIMEOUT, refusing a value that disables expiry.

    Zero used to mean "never expire". A never-expiring input-synthesis daemon is
    not something an environment variable should be able to ask for, so it is now
    an error rather than a silently honoured setting.
    """
    raw = os.environ.get("CC_WAYLAND_IDLE_TIMEOUT")
    if raw is None or raw.strip() == "":
        return DEFAULT_IDLE_TIMEOUT_S
    try:
        value = int(raw)
    except ValueError:
        raise UsageError(
            f"CC_WAYLAND_IDLE_TIMEOUT={raw!r} is not an integer number of seconds")
    if value <= 0:
        raise UsageError(
            f"CC_WAYLAND_IDLE_TIMEOUT={value} would leave the actuation daemon - "
            "and the input-synthesis grant behind it - alive indefinitely. Give a "
            "positive number of seconds, or unset it for the "
            f"{DEFAULT_IDLE_TIMEOUT_S}s default.")
    return value


def run_daemon(foreground: bool = True) -> int:
    import signal

    # Resolved before anything is bound or negotiated, so a rejected timeout
    # fails here rather than after the operator has answered a consent dialog.
    idle_timeout = idle_timeout_s()
    auth_token = load_or_create_auth_token()

    path = socket_path()
    # A stale socket from a killed daemon refuses connections forever; a
    # successful connect proves an owner, so only an unreachable one is removed.
    if os.path.exists(path):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(path)
            probe.close()
            print(f"daemon already running at {path}", file=sys.stderr)
            return 0
        except OSError:
            os.unlink(path)
        finally:
            try:
                probe.close()
            except OSError:
                pass

    x11 = X11Readback()

    # Listen before starting the session. Start blocks until the consent dialog is
    # answered, which is a human-scale wait; a client polling for a socket that only
    # appears afterwards times out while the grant is still pending, and reports a
    # failure to a user who is about to succeed. Binding first lets the client
    # connect immediately and simply wait for the first accept.
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    srv.listen(8)
    srv.settimeout(30.0)

    try:
        session = PortalSession()
        session.start()
    except Exception:
        # Nothing may be left listening on a socket with no session behind it: the
        # next client would connect, send, and block on a daemon that cannot serve.
        srv.close()
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    # Held open for the life of the daemon, so a position read is a lookup and
    # absolute motion is available between commands as well as within one. The
    # visible cost is the compositor's screen-sharing indicator while it runs.
    cursor = CursorTracker(session)
    if cursor.start():
        if foreground:
            print(f"cursor tracking live (node {session.node_id}, "
                  f"{session.stream_size[0]}x{session.stream_size[1]})", flush=True)
    executor = Executor(session, x11, cursor)

    stopping = {"now": False}

    def shutdown(signum=None, frame=None):
        stopping["now"] = True

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    if foreground:
        print(f"listening on {path}", flush=True)

    last_activity = time.monotonic()
    holding_since: Optional[float] = None
    try:
        while not stopping["now"]:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                now = time.monotonic()
                idle = now - last_activity
                holding = bool(executor.held_buttons or executor.held_keys)
                if not holding:
                    holding_since = None
                elif holding_since is None:
                    holding_since = now
                # A hold defers expiry so that the timer cannot release a
                # deliberate `hold` behind the operator's back - but only for so
                # long. Past the ceiling the hold is treated as stranded rather
                # than intended, released, and the daemon expires with it;
                # otherwise "held" never ends and this input-synthesis capability
                # becomes permanent.
                if (holding and holding_since is not None
                        and now - holding_since > MAX_HELD_SUPPRESSION_S):
                    print(
                        f"wayland: a button or key has been held for "
                        f"{int(now - holding_since)}s, past the "
                        f"{MAX_HELD_SUPPRESSION_S}s ceiling. Releasing it and "
                        "shutting down; this release is not recorded.",
                        file=sys.stderr, flush=True)
                    break
                if idle > idle_timeout and not holding:
                    break
                continue
            last_activity = time.monotonic()
            with conn:
                conn.settimeout(30.0)
                try:
                    data = b""
                    while not data.endswith(b"\n"):
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    if not data:
                        continue
                    req = json.loads(data.decode("utf-8"))
                    resp = handle_request(executor, req, stopping, auth_token)
                except UsageError as e:
                    resp = {"ok": False, "stdout": "", "stderr": str(e), "code": 2}
                except Exception as e:
                    resp = {"ok": False, "stdout": "",
                            "stderr": f"{type(e).__name__}: {e}", "code": 1}
                try:
                    conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
                except OSError:
                    pass
    finally:
        executor.release_all()
        cursor.stop()
        session.close()
        try:
            os.unlink(path)
        except OSError:
            pass
    return 0


def handle_request(executor: Executor, req: dict, stopping: dict,
                   auth_token: str) -> dict:
    # First, before any op is dispatched and before any argv is looked at. Every
    # op below synthesises input or reports on a session that can; there is no
    # verb here that is safe to serve to a caller that cannot present the token.
    #
    # compare_digest rather than `==` as a matter of habit rather than because a
    # timing oracle is the realistic attack on a local socket.
    presented = req.get("auth_token")
    if not isinstance(presented, str) or not hmac.compare_digest(presented, auth_token):
        raise UsageError(
            "unauthenticated request: this socket requires the token in "
            f"{_auth_token_path()}, which is readable only by this user")
    op = req.get("op")
    if op == "ping":
        return {"ok": True, "stdout": "pong", "stderr": "", "code": 0}
    if op == "release_all":
        executor.release_all()
        return {"ok": True, "stdout": "", "stderr": "", "code": 0}
    if op == "motion":
        # Raw relative motion, for diagnosing the pointer path itself. Not part of
        # the xdotool surface: it exists so a failure can be isolated to the portal
        # call rather than to the closed loop that drives it.
        executor.session.motion(float(req["dx"]), float(req["dy"]))
        return {"ok": True, "stdout": "", "stderr": "", "code": 0}
    if op == "status":
        live = executor.cursor.position() if executor.cursor else None
        return {"ok": True, "code": 0, "stderr": "",
                "stdout": json.dumps({
                    "position": list(live) if live else executor.x11.position(),
                    "position_source": "compositor" if live else "xwayland",
                    "absolute_motion": executor.absolute_motion_works(),
                    "cursor_error": (executor.cursor.error
                                     if executor.cursor and live is None else None),
                    "held_buttons": sorted(executor.held_buttons.values()),
                    "held_keys": len(executor.held_keys)})}
    if op == "shutdown":
        stopping["now"] = True
        return {"ok": True, "stdout": "", "stderr": "", "code": 0}
    argv = req.get("argv")
    if not isinstance(argv, list) or not all(isinstance(t, str) for t in argv):
        raise UsageError("request must carry an argv list of strings")
    out = executor.run(argv)
    return {"ok": True, "stdout": out, "stderr": "", "code": 0}


def _connect(path: str, timeout: float = 5.0):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(path)
    return s


def ensure_daemon(path: str, spawn_timeout: float = 30.0) -> bool:
    """Connect, or start the daemon and wait for its socket.

    Returns True if this call spawned the daemon, so the caller can allow a
    human-scale budget for the reply: the daemon now listens before it asks for
    consent, so the socket appears quickly but the first request is not answered
    until the dialog is accepted.
    """
    try:
        _connect(path).close()
        return False
    except OSError as e:
        if e.errno not in (errno.ENOENT, errno.ECONNREFUSED):
            raise

    # A caller that cannot reach the token does not get to bring the daemon into
    # existence. Without this, a confined caller that the token excludes could
    # still start the session - raising the consent dialog, or restoring the
    # grant silently - and leave a capability running for something else to use.
    if _read_auth_token() is None:
        raise RuntimeError(
            f"cannot start the Wayland actuation daemon: {_no_token_reason()}")

    # Detached so it outlives this one-shot client, which is the entire point of
    # the daemon: the portal session must survive between commands.
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--daemon", "--quiet"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)

    deadline = time.monotonic() + spawn_timeout
    while time.monotonic() < deadline:
        try:
            _connect(path).close()
            return True
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(
        "Wayland actuation daemon did not start. If a 'Remote Desktop' consent "
        "dialog appeared, it must be accepted once; after that the session is "
        "restored silently.")


# A request that had to raise the consent dialog is waiting on a person, not on a
# machine, so it gets a human-scale budget; every later request uses the short one.
CONSENT_TIMEOUT_S = 240.0


def client_request(req: dict, timeout: float = 60.0, spawn: bool = True) -> dict:
    """Send one request to the daemon.

    spawn=False for the control verbs. Starting a session in order to ask it to
    stop is absurd on its own terms, and it would raise a consent dialog to do
    it - so `--shutdown` against a daemon that is not running must report that,
    not create one.
    """
    path = socket_path()
    spawned = False
    if spawn:
        # Minted here when it does not exist yet, so the first command of a fresh
        # install works without a setup step. Creating it is itself the privilege
        # the token represents: a caller that cannot write ~/.local/state cannot
        # mint one, and cannot read the one an unconfined process minted.
        load_or_create_auth_token()
        spawned = ensure_daemon(path)
    # After the connect, not before: `--shutdown` and `--status` run with
    # spawn=False against a daemon that may not exist, and their answer for that
    # is the ENOENT/ECONNREFUSED path in main(). Reading the token first would
    # replace "no daemon is running" with an authentication error on a machine
    # that has never started one.
    s = _connect(path, max(timeout, CONSENT_TIMEOUT_S) if spawned else timeout)
    token = _read_auth_token()
    if token is None:
        s.close()
        raise RuntimeError(
            f"cannot authenticate to the Wayland actuation daemon: "
            f"{_no_token_reason()}")
    try:
        s.sendall((json.dumps({**req, "auth_token": token}) + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        s.close()
    if not data:
        raise RuntimeError("no response from Wayland actuation daemon")
    return json.loads(data.decode("utf-8"))


def main(argv: List[str]) -> int:
    if argv and argv[0] == "--daemon":
        return run_daemon(foreground="--quiet" not in argv)
    if argv and argv[0] in ("--status", "--release-all", "--shutdown", "--ping"):
        op = {"--status": "status", "--release-all": "release_all",
              "--shutdown": "shutdown", "--ping": "ping"}[argv[0]]
        try:
            resp = client_request({"op": op}, spawn=False)
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ECONNREFUSED):
                print("no Wayland actuation daemon is running", file=sys.stderr)
                return 0 if op in ("shutdown", "release_all") else 1
            raise
    elif not argv:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: wayland_portal.py <xdotool-style command>", file=sys.stderr)
        return 2
    else:
        resp = client_request({"argv": argv})
    if resp.get("stdout"):
        print(resp["stdout"])
    if resp.get("stderr"):
        print(resp["stderr"], file=sys.stderr)
    return int(resp.get("code", 0 if resp.get("ok") else 1))


def console_main() -> NoReturn:
    """Entry point for the `cc-wayland-actuate` console script.

    The agent spawns this helper by name - `ProcessCommand::new("cc-wayland-actuate")`
    in crates/agent/src/main.rs, for both actuation and the position readback - so
    the name has to be on PATH for the Wayland backend to work at all. Nothing
    installed it before this existed: setup.py declared only `control-center`, and
    wayland_portal.py shipped as a module. A Wayland install therefore failed every
    actuation with "No such file or directory", while development machines worked
    because a hand-made symlink happened to resolve the name.

    Zero-argument by necessity: console_scripts calls it with none, so argv is read
    here rather than passed in. `__main__` below delegates to it so that running the
    file directly and running the installed script take the same path, including the
    exit codes - two entry points that drift is how one of them stops being tested.
    """
    try:
        sys.exit(main(sys.argv[1:]))
    except UsageError as e:
        print(f"{e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    console_main()
