"""A held mouse button must be tracked, surfaced, and released on agent exit.

`X Y hold` issues a button-down with no timeout and no tracking. Unmatched, the
button stays physically down: every later click or move is interpreted as a
drag-select, and the console reports plain success throughout. A failed S027 attempt
recorded `900 700 hold` and the button stayed down until a manual release.

The auto-release is the one place the agent actuates without being asked, so these
tests pin its limits as much as its behaviour: it releases only buttons it recorded
going down, and a drag — which presses and releases inside a single command — must
never leave the tracker believing anything is held.
"""
import itertools
import os
import re
import subprocess
import time

import pytest

import harness
from conftest import requires_stack
from controller.integrations.gRPC import GRPCClient
from controller.integrations.proto import control_center_pb2_grpc
from controller.os_specific import command_hints
from controller.os_specific.linux_actuation import LinuxActuation

pytestmark = requires_stack


_subject = itertools.count()


def _client(stack):
    # Own token subject per client: the server rate-limits per subject, and these
    # tests would otherwise spend the shared `execute` budget that later files need.
    c = GRPCClient(host=stack.host, port=stack.port, timeout=15, use_ssl=True)
    c.channel = c._create_channel()
    c.stub = control_center_pb2_grpc.ControlServiceStub(c.channel)
    c.set_token(harness.mint_token(["execute"], user=f"hold-{next(_subject)}"))
    return c


def _linux():
    la = LinuxActuation.__new__(LinuxActuation)
    la.display = harness.XVFB_DISPLAY
    return la


def _send(client, la, command):
    argv, human = la._build_mouse_command(command)
    return client.execute_command(argv=argv, human_command=human)


def _held(result):
    return result.get("metadata", {}).get("held_buttons", "")


def test_hold_is_tracked_and_released(stack):
    la, client = _linux(), _client(stack)
    try:
        # Nothing held to begin with.
        assert _held(_send(client, la, "400 300 move")) == ""

        assert _send(client, la, "900 700 hold")["success"]
        # The hold is visible on the next command.
        held = _held(_send(client, la, "400 300 move"))
        assert re.match(r"^left@900,700:\d+$", held), held

        assert _send(client, la, "900 700 release")["success"]
        assert _held(_send(client, la, "400 300 move")) == ""
    finally:
        client.disconnect()


def test_a_drag_leaves_nothing_held(stack):
    """A drag presses and releases within one command. Reading the first transition
    rather than the last would record a phantom hold — and the agent would then
    issue an uncommanded release at shutdown for a button already up."""
    la, client = _linux(), _client(stack)
    try:
        assert _send(client, la, "100 100 drag 900 700")["success"]
        assert _held(_send(client, la, "400 300 move")) == ""
    finally:
        client.disconnect()


@pytest.mark.parametrize("command", [
    "960 540 left", "960 540 right", "960 540 double", "400 300 move", "here left",
])
def test_ordinary_actions_leave_nothing_held(stack, command):
    la, client = _linux(), _client(stack)
    try:
        assert _held(_send(client, la, command)) == ""
    finally:
        client.disconnect()


def test_a_hold_without_coordinates_is_tracked_without_inventing_one(stack):
    """`here hold` names no coordinate. Recording (0, 0) as a stand-in would make the
    shutdown release move the cursor to the corner first — on macOS and Windows the
    release form moves before it releases, so it would drag whatever is grabbed to
    the origin. The position is reported as unknown instead."""
    la, client = _linux(), _client(stack)
    try:
        assert _send(client, la, "here hold")["success"]
        held = _held(_send(client, la, "400 300 move"))
        assert re.match(r"^left@\?:\d+$", held), (
            f"expected an unknown position, got {held!r} — a coordinate was invented"
        )
    finally:
        _send(client, la, "here release")
        client.disconnect()


def test_unknown_position_warning_asks_for_here_release():
    lines = command_hints.held_button_warnings(
        {"held_buttons": f"left@?:{command_hints.HOLD_WARN_SECONDS + 3}"}
    )
    assert len(lines) == 1
    assert "here release" in lines[0]
    assert "(0, 0)" not in lines[0], "must not imply a coordinate it does not have"


def test_console_warns_only_after_the_threshold():
    """The warning is time-gated so a deliberate slow drag does not trip it, and it
    never releases — a timer that released would cut a valid drag short."""
    assert command_hints.held_button_warnings(
        {"held_buttons": "left@900,700:1"}
    ) == []

    lines = command_hints.held_button_warnings(
        {"held_buttons": f"left@900,700:{command_hints.HOLD_WARN_SECONDS + 2}"}
    )
    assert len(lines) == 1
    assert "left button still held at (900, 700)" in lines[0]
    assert "900 700 release" in lines[0]


def test_hold_warning_reaches_the_console(stack, capsys):
    """End to end: the agent's metadata drives a real console line."""
    la, client = _linux(), _client(stack)
    try:
        assert _send(client, la, "900 700 hold")["success"]
        time.sleep(command_hints.HOLD_WARN_SECONDS + 1)
        result = _send(client, la, "400 300 move")
        command_hints.print_held_button_warnings(result.get("metadata"))
        assert "left button still held at (900, 700)" in capsys.readouterr().out
    finally:
        _send(client, la, "900 700 release")
        client.disconnect()


def test_agent_releases_a_held_button_on_shutdown(stack, tmp_path, monkeypatch):
    """The agent must not leave a button down when it exits. Runs against a private
    stack so killing the agent does not disturb the session-scoped one."""
    workdir = tmp_path / "shutdown-stack"
    workdir.mkdir()
    # launch(own_xvfb=False) reads DISPLAY from the environment, which outside CI is
    # the operator's real screen. Pin it to the harness Xvfb the session fixture is
    # already running: actuation must never reach a live display.
    monkeypatch.setenv("DISPLAY", harness.XVFB_DISPLAY)

    # harness.launch sets the process-global CC_TLS_CA and teardown does not put it
    # back, so a second stack silently repoints every later client at the wrong CA.
    # Restore it explicitly, or every test ordered after this one fails on TLS.
    session_ca = os.environ.get("CC_TLS_CA")
    own = harness.launch(workdir, own_xvfb=False)
    try:
        la, client = _linux(), _client(own)
        try:
            assert _send(client, la, "900 700 hold")["success"]
            assert _held(_send(client, la, "400 300 move")).startswith("left@")
        finally:
            client.disconnect()

        own.agent.terminate()
        try:
            own.agent.wait(timeout=15)
        except subprocess.TimeoutExpired:
            own.agent.kill()
            pytest.fail("agent did not exit after SIGTERM")

        log = (workdir / "logs" / "agent.log")
        text = log.read_text(errors="replace") if log.exists() else ""
        assert "Auto-released left button" in text, (
            "agent exited without releasing the held button.\n"
            f"--- agent log tail ---\n{text[-2000:]}"
        )
        assert "NOT recorded" in text, (
            "the auto-release must say it could not be recorded — the stream is "
            "closed by then, so the step is absent from the trace"
        )
    finally:
        harness.teardown(own)
        if session_ca is not None:
            os.environ["CC_TLS_CA"] = session_ca
        else:
            os.environ.pop("CC_TLS_CA", None)
