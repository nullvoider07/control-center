"""The reported cursor position must match the position that was requested.

A mouse command's coordinate is produced once, by the agent, and consumed twice:
the console echoes it to gate the next step, and the server copies the same field
into `CommandEvent` → `actuation_commands.json` in the corpus. There is no second
readback, so a wrong value is simultaneously a lie to the operator and corrupt
training data.

The failure it guards was observed on 2026-07-25: `1093 660 move` reported
`X=1253, Y=1079` on a 1080-high display, twice in one session, with the action
itself landing correctly both times. A bare readback cannot tell a good read from
one that raced the synthetic event or from a warp that silently did nothing.

Both regimes below are real and both are exercised here:

* **No mapped window** — `xdotool mousemove` exits 0 and moves nothing, and the
  readback returns the stale pointer position. Measured 400/400 on a bare Xvfb.
  This is a deterministic stand-in for the intermittent production fault, and it is
  what makes these tests a discriminator rather than a formality: before the fix the
  agent reported that stale coordinate with `position_captured: true`.
* **With a mapped window** — the warp works and the readback is immediate (0/200
  mismatches with no delay), so the verification must not cost real coordinates.
"""
import os
import subprocess
import sys
import time

import pytest

import harness
from conftest import requires_stack
from controller.integrations.gRPC import GRPCClient
from controller.integrations.proto import control_center_pb2_grpc
from controller.os_specific.linux_actuation import LinuxActuation

from test_record_fidelity import _Recorder

pytestmark = requires_stack

# Widely separated so a stale read can never coincidentally equal a requested point.
TARGETS = [
    (120, 90), (1100, 880), (300, 700), (900, 200), (60, 950),
    (1200, 120), (500, 500), (1000, 640), (200, 300), (760, 810),
]

WINDOW = r'''
import sys, tkinter as tk
r = tk.Tk(); r.title("cc-pos-probe"); r.geometry("1270x1010+0+0")
r.after(int(sys.argv[1]), r.destroy); r.mainloop()
'''


def _client(stack, user):
    """Client with its own token subject.

    The server rate-limits per token subject (100 requests / 60 s). These tests issue
    a lot of moves on purpose — a tight loop is the point — so they mint their own
    subject rather than spending the shared `execute` budget and starving whatever
    runs next. Same pattern as test_stress.py.
    """
    c = GRPCClient(host=stack.host, port=stack.port, timeout=15, use_ssl=True)
    c.channel = c._create_channel()
    c.stub = control_center_pb2_grpc.ControlServiceStub(c.channel)
    c.set_token(harness.mint_token(["execute", "monitor"], user=f"pos-{user}"))
    return c


def _move(client, la, x, y):
    argv, human = la._build_mouse_command(f"{x} {y} move")
    return client.execute_command(argv=argv, human_command=human)


def _linux():
    la = LinuxActuation.__new__(LinuxActuation)
    la.display = harness.XVFB_DISPLAY
    return la


def _tk_available() -> bool:
    return subprocess.run(
        [sys.executable, "-c", "import tkinter"], capture_output=True
    ).returncode == 0


def test_a_reported_coordinate_always_matches_the_request(stack):
    """The core property, on whichever regime the harness is in: a result either
    carries the requested coordinate, or reports that it captured none. A third
    value is the bug."""
    la = _linux()
    client = _client(stack, "always-matches")
    violations = []
    try:
        for x, y in TARGETS * 4:
            result = _move(client, la, x, y)
            assert result["success"], result
            if not result.get("position_captured"):
                continue
            got = (result.get("mouse_x"), result.get("mouse_y"))
            if got != (x, y):
                violations.append(f"requested ({x}, {y}) but reported {got}")
    finally:
        client.disconnect()

    assert not violations, (
        "a coordinate was reported that was never actuated:\n  "
        + "\n  ".join(violations[:10])
    )


def test_a_stale_readback_is_reported_as_uncaptured(stack):
    """On a display with no mapped window the warp silently no-ops, so the readback
    can only ever return a stale position. The agent must say so rather than pass
    that coordinate off as the result — this is the case that used to report
    `position_captured: true` with a coordinate the command never asked for."""
    env = {**os.environ, "DISPLAY": harness.XVFB_DISPLAY}
    # Two separate invocations, matching the agent: it spawns the actuation and then
    # spawns the readback. Chaining them in one xdotool process would report the
    # position that process *intended* to warp to, not the one the server holds, and
    # would make this test skip itself on exactly the display it exists to cover.
    subprocess.run(["xdotool", "mousemove", "5", "5"], env=env, capture_output=True)
    probe = subprocess.run(
        ["xdotool", "getmouselocation", "--shell"],
        env=env, capture_output=True, text=True,
    )
    if "X=5\n" in probe.stdout and "Y=5\n" in probe.stdout:
        pytest.skip("this display honours warps; covered by the windowed test")

    la = _linux()
    client = _client(stack, "stale")
    try:
        for x, y in TARGETS:
            result = _move(client, la, x, y)
            assert result["success"], result
            assert not result.get("position_captured"), (
                f"requested ({x}, {y}) on a display that does not move the cursor, "
                f"but the agent reported it captured "
                f"({result.get('mouse_x')}, {result.get('mouse_y')})"
            )
    finally:
        client.disconnect()


@pytest.mark.skipif(not _tk_available(), reason="tkinter not installed")
def test_verification_does_not_cost_real_coordinates(stack, tmp_path):
    """With a window mapped the warp works and the readback is immediate, so every
    move must still report its coordinate. Guards the opposite failure: a check so
    strict that it reports `position_captured: false` for correct commands."""
    win = tmp_path / "win.py"
    win.write_text(WINDOW)
    env = {**os.environ, "DISPLAY": harness.XVFB_DISPLAY}
    proc = subprocess.Popen(
        [sys.executable, str(win), "30000"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(2.0)
        la = _linux()
        client = _client(stack, "windowed")
        captured = 0
        try:
            for x, y in TARGETS:
                result = _move(client, la, x, y)
                assert result["success"], result
                if result.get("position_captured"):
                    captured += 1
                    assert (result["mouse_x"], result["mouse_y"]) == (x, y)
        finally:
            client.disconnect()
    finally:
        proc.kill()

    assert captured == len(TARGETS), (
        f"only {captured}/{len(TARGETS)} moves reported a position on a display "
        "that honours warps — the verification is rejecting good reads"
    )


def test_the_recorded_coordinate_matches_the_echo(stack):
    """The echo and the corpus record are the same field, so the property has to be
    asserted on the recorded surface too — that is the one that becomes training
    data and the one nobody looks at until an audit."""
    la = _linux()
    with _Recorder(stack) as recorder:
        client = _client(stack, "recorded")
        echoes = []
        try:
            for x, y in TARGETS:
                result = _move(client, la, x, y)
                echoes.append(
                    (result.get("mouse_x"), result.get("mouse_y"),
                     bool(result.get("position_captured")))
                )
                time.sleep(0.1)
        finally:
            client.disconnect()
        time.sleep(2.0)

    events = [e for e in recorder.events if e["action_type"] == "mouse"]
    assert len(events) == len(TARGETS), f"expected {len(TARGETS)} events, got {len(events)}"

    for (x, y), echo, event in zip(TARGETS, echoes, events):
        recorded = (event["mouse_x"], event["mouse_y"], event["position_captured"])
        if echo[2]:
            assert echo[:2] == (x, y), f"echo for ({x}, {y}) was {echo[:2]}"
            assert recorded == (x, y, True), (
                f"recorded {recorded} for a step echoed as {echo}"
            )
        else:
            # An uncaptured mouse step records the REQUESTED coordinate with the
            # flag false (agent 2.0.0; before it, (0, 0)). The property that has
            # to hold is not the value but the labelling: whatever is recorded
            # must carry position_captured=false, and must be the point the
            # command asked for rather than some third coordinate the agent
            # invented. A record that disagreed with the request would mean the
            # agent published a reading it could not verify, which is the whole
            # failure this file exists to catch.
            assert recorded == (x, y, False), (
                f"echo reported no position for ({x}, {y}) but the record kept "
                f"{recorded}"
            )
            assert echo[:2] == (x, y), (
                f"echo for an uncaptured ({x}, {y}) was {echo[:2]}"
            )


@pytest.mark.parametrize("command", ["here left", "here right", "position"])
def test_commands_that_name_no_coordinate_are_still_verified(stack, command):
    """`here …` and `position` do not move the cursor, so a read before and a read
    after must agree. They carry no requested coordinate, so before this they were
    the one path still publishing an unverified readback as authoritative — and
    `position` is precisely what an operator runs to get coordinates for the next
    step."""
    la = _linux()
    client = _client(stack, "no-coordinate")
    try:
        # Park the cursor somewhere known first so the reads have a stable subject.
        _move(client, la, 640, 480)
        for _ in range(6):
            argv, human = la._build_mouse_command(command)
            result = client.execute_command(argv=argv, human_command=human)
            assert result["success"], result
            if result.get("position_captured"):
                # A captured value here means two independent reads agreed.
                assert isinstance(result["mouse_x"], int)
                assert isinstance(result["mouse_y"], int)
    finally:
        client.disconnect()


@pytest.mark.skipif(not _tk_available(), reason="tkinter not installed")
@pytest.mark.parametrize("command", ["here left", "position"])
def test_a_moving_cursor_defeats_the_here_readback(stack, tmp_path, command):
    """If something moves the cursor across a command that names no coordinate,
    neither sample describes it, so the agent must report no position rather than
    pick one.

    `position` is parametrised here deliberately. Agent 2.0.0 exempted it from the
    before/after agreement check (`action.action_type == "position" && captured`
    returns early), on the reasoning that a stale reader now reports
    captured=false so a captured read is already known to be live. That holds for
    the Wayland helper, which emits POSITION_VERIFIED=0. It does not obviously
    hold for xdotool, where `verified` is unconditionally true. Until this
    parameter existed the suite could not tell either way: the only other test
    naming `position` runs against a stationary cursor, where the check passes
    whether or not it runs. Deciding whether the exemption is safe without this
    was guessing.

    Needs a mapped window: on a bare X display the warp silently no-ops, so the
    interference would not actually move anything and the test would pass without
    exercising the check.
    """
    import random
    import threading

    # The agent samples the cursor, actuates, settles ~50ms, samples again, and
    # re-reads a bounded number of times; any read matching the pre-read counts as a
    # capture (agent/src/main.rs). Two things follow for the interference:
    #
    #  * It must be continuous. One xdotool process per move costs ~50ms on an idle
    #    machine and more on a loaded runner, comparable to the sample window itself,
    #    so the race often went unstaged and the assertion failed for an unrelated
    #    reason. Chaining many moves into one invocation keeps the cursor in motion.
    #  * The coordinates must not repeat. Cycling a handful of fixed points meant the
    #    pre-read was itself one of them, so a later re-read landed back on it by
    #    coincidence and the command was reported as captured — the test was measuring
    #    that coincidence rather than the check.
    MOVES_PER_BATCH = 200
    MIN_MOVES_PER_SECOND = 200  # a move every 5ms against a >=50ms sample window
    COMMANDS = 12

    win = tmp_path / "win.py"
    win.write_text(WINDOW)
    env = {**os.environ, "DISPLAY": harness.XVFB_DISPLAY}
    proc = subprocess.Popen(
        [sys.executable, str(win), "40000"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    stop = threading.Event()

    rng = random.Random(20260727)
    moves = [0]

    def jitter():
        while not stop.is_set():
            batch = ["xdotool"]
            for _ in range(MOVES_PER_BATCH):
                batch += ["mousemove",
                          str(rng.randrange(60, 1200)), str(rng.randrange(60, 950))]
            if subprocess.run(batch, env=env, capture_output=True).returncode != 0:
                return
            moves[0] += MOVES_PER_BATCH

    worker = threading.Thread(target=jitter, daemon=True)
    try:
        time.sleep(2.0)
        # Confirm warps land here, or the interference proves nothing.
        subprocess.run(["xdotool", "mousemove", "77", "88"], env=env, capture_output=True)
        probe = subprocess.run(["xdotool", "getmouselocation", "--shell"],
                               env=env, capture_output=True, text=True)
        if "X=77\n" not in probe.stdout:
            pytest.skip("display does not honour warps; interference cannot be staged")

        la = _linux()
        client = _client(stack, "moving")
        worker.start()
        started = time.time()
        uncaptured = 0
        try:
            for _ in range(COMMANDS):
                argv, human = la._build_mouse_command(command)
                result = client.execute_command(argv=argv, human_command=human)
                assert result["success"], result
                if not result.get("position_captured"):
                    uncaptured += 1
        finally:
            client.disconnect()
            elapsed = time.time() - started
    finally:
        stop.set()
        worker.join(timeout=5)
        proc.kill()

    rate = moves[0] / elapsed if elapsed > 0 else 0.0
    if uncaptured == 0 and rate < MIN_MOVES_PER_SECOND:
        # Distinguish "the check is not running" from "this host could not move the
        # cursor fast enough to overlap the sample window". Failing on the latter
        # makes the release gate flaky without telling anyone anything true.
        pytest.skip(f"cursor moved only {rate:.0f} times/sec, too slow to stage the "
                    f"race (need {MIN_MOVES_PER_SECOND}/sec)")

    assert uncaptured > 0, (
        f"the cursor was moved {rate:.0f} times/sec throughout, yet every "
        f"`{command}` still claimed a captured position — the before/after check "
        "is not running for this verb"
    )


def test_keyboard_steps_record_no_position(stack):
    """`position_captured` is the only valid guard: an uncaptured position is stored
    as (0, 0), which is itself a real screen coordinate. A consumer that reads
    mouse_x/mouse_y without checking the flag sees the top-left corner."""
    la = _linux()
    with _Recorder(stack) as recorder:
        client = _client(stack, "keyboard")
        try:
            for line in ["type hello", "press ^c", "press {Enter}"]:
                argv, human = la._build_keyboard_command(line)
                client.execute_command(argv=argv, human_command=human)
                time.sleep(0.1)
        finally:
            client.disconnect()
        time.sleep(2.0)

    assert recorder.events, "no events recorded"
    for event in recorder.events:
        assert event["position_captured"] is False
        assert (event["mouse_x"], event["mouse_y"]) == (0, 0)
