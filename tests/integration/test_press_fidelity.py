"""Verify that `press` commands deliver the modifier AND the target key.

The unit tests assert on the key spec the builder constructs. This one observes the
X key events that actually arrive, by pressing through the full pipeline (actuation
builder -> TLS server -> agent -> xdotool) into a focused Tk window on the harness
Xvfb and reading back the recorded (state, keysym) pairs.

It guards the failure mode the punctuation keysym map fixed. Before it, `press ^-`
emitted `xdotool key ctrl+-`, which xdotool refused outright, and `press ^,` emitted
`ctrl+,`, which xdotool accepted with exit 0 while logging "No such key name" and
dropping the target — so a bare Ctrl press landed and the operator was told the
command had succeeded. Only reading the delivered events distinguishes those.
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

pytestmark = requires_stack

# X modifier mask bits as reported in Tk's event.state.
SHIFT, CTRL, MOD1, MOD4 = 0x1, 0x4, 0x8, 0x40

# (press argument, expected modifier mask, expected keysym).
#
# Two families are deliberately absent, both for reasons outside the translation
# layer this test covers; their key specs are pinned by tests/unit/test_press_grammar.py.
#   - Alt/Super combinations: with no window manager on the harness Xvfb there is
#     nothing to grab them.
#   - Function keys: on the harness keymap xdotool resolves F1-F12 to a modified
#     level and emits Alt_L before the F-key (confirmed with xev), and Tk does not
#     dispatch Alt-modified F-keys to bind_all.
CASES = [
    ("^c", CTRL, "c"),
    ("^+z", CTRL | SHIFT, "Z"),
    ("^-", CTRL, "minus"),
    ("^=", CTRL, "equal"),
    ("^,", CTRL, "comma"),
    ("^.", CTRL, "period"),
    ("^/", CTRL, "slash"),
    ("^;", CTRL, "semicolon"),
    ("^[", CTRL, "bracketleft"),
    ("^]", CTRL, "bracketright"),
    ("^\\", CTRL, "backslash"),
    ("^`", CTRL, "grave"),
    ("^{Plus}", CTRL | SHIFT, "plus"),      # xdotool adds Shift to reach plus
    ("^_", CTRL | SHIFT, "underscore"),
    ("^{Left}", CTRL, "Left"),
    ("^+{Left}", CTRL | SHIFT, "Left"),
    ("{Enter}", 0, "Return"),
    ("{Space}", 0, "space"),
]

PROBE = r'''
import sys, tkinter as tk
out, dwell = sys.argv[1], int(sys.argv[2])
root = tk.Tk()
root.title("cc-press-probe")
root.geometry("800x120+40+40")
e = tk.Entry(root, width=100)
e.pack(padx=10, pady=30)
rec = []
# Record every key press with its modifier mask. Modifier keys report their own
# press with state=0, so a dropped target is visible as a lone Control_L.
root.bind_all("<KeyPress>", lambda ev: rec.append(f"{ev.state:x}\t{ev.keysym}"))
root.lift()
root.attributes("-topmost", True)
root.update_idletasks(); root.update()
# Toplevel focus first, then the widget: reversing these leaves the Entry without
# Tk focus, so XTEST keystrokes reach the window but land nowhere.
root.focus_force(); e.focus_set(); e.focus_force()
def dump():
    open(out, "w", encoding="utf-8").write("\n".join(rec))
    root.destroy()
root.after(dwell, dump)
root.mainloop()
'''

MODIFIER_KEYSYMS = {
    "Control_L", "Control_R", "Shift_L", "Shift_R",
    "Alt_L", "Alt_R", "Super_L", "Super_R",
}


def _tk_available() -> bool:
    return subprocess.run(
        [sys.executable, "-c", "import tkinter"], capture_output=True
    ).returncode == 0


@pytest.mark.skipif(not _tk_available(), reason="tkinter not installed")
def test_press_delivers_the_modifier_and_the_target_key(stack, tmp_path):
    probe_py = tmp_path / "probe.py"
    probe_py.write_text(PROBE)
    out_file = tmp_path / "keys.txt"
    env = {**os.environ, "DISPLAY": harness.XVFB_DISPLAY}

    dwell_ms = 4000 + 300 * len(CASES)
    probe = subprocess.Popen(
        [sys.executable, str(probe_py), str(out_file), str(dwell_ms)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    failures = []
    try:
        time.sleep(2.0)  # let the window map and take focus
        subprocess.run(
            ["xdotool", "search", "--name", "cc-press-probe",
             "windowactivate", "--sync", "windowfocus"],
            env=env, capture_output=True,
        )
        time.sleep(0.5)

        la = LinuxActuation.__new__(LinuxActuation)
        la.display = harness.XVFB_DISPLAY

        client = GRPCClient(host=stack.host, port=stack.port, timeout=15, use_ssl=True)
        client.channel = client._create_channel()
        client.stub = control_center_pb2_grpc.ControlServiceStub(client.channel)
        client.set_token(stack.tokens["execute"])
        try:
            for keys, _, _ in CASES:
                argv, human = la._build_keyboard_command(f"press {keys}")
                result = client.execute_command(argv=argv, human_command=human)
                if not result["success"]:
                    failures.append(
                        f"press {keys!r} -> {argv!r} was rejected: "
                        f"{result.get('error') or result.get('output')}"
                    )
                time.sleep(0.15)
        finally:
            client.disconnect()

        probe.wait(timeout=30)
    finally:
        if probe.poll() is None:
            probe.kill()

    assert not failures, "commands did not execute:\n  " + "\n  ".join(failures)
    assert out_file.exists(), "probe never wrote its capture file"

    # Keep only the target keys: the modifier's own press event carries state=0 and
    # is an artefact of how xdotool builds the sequence, not part of the result.
    events = []
    for line in out_file.read_text(encoding="utf-8").splitlines():
        state, _, keysym = line.partition("\t")
        if keysym and keysym not in MODIFIER_KEYSYMS:
            events.append((int(state, 16), keysym))

    expected = [(mask, keysym) for _, mask, keysym in CASES]
    assert len(events) == len(expected), (
        "a press command delivered no target key — the modifier landed alone.\n"
        f"  sent:     {[c[0] for c in CASES]}\n"
        f"  expected: {expected}\n"
        f"  actual:   {events}"
    )

    mismatches = [
        f"press {keys!r}: expected state=0x{mask:x} keysym={keysym}, "
        f"got state=0x{got_mask:x} keysym={got_keysym}"
        for (keys, mask, keysym), (got_mask, got_keysym) in zip(CASES, events)
        # Only the modifier bits under test are compared; Lock/NumLock state is
        # incidental and not something the actuation layer controls.
        if (got_mask & (SHIFT | CTRL | MOD1 | MOD4)) != mask or got_keysym != keysym
    ]
    assert not mismatches, "press commands were altered in transit:\n  " + \
        "\n  ".join(mismatches)


@pytest.mark.skipif(not _tk_available(), reason="tkinter not installed")
def test_a_key_xdotool_cannot_resolve_is_reported_as_a_failure(stack):
    """`ctrl+_` satisfies the agent's keysym grammar but xdotool cannot resolve '_',
    so it exits 0 after logging "No such key name" and delivering a bare Ctrl. The
    argv is sent raw, bypassing the builder, because the builder no longer produces
    this shape — the point is that the agent refuses to call it a success.
    """
    client = GRPCClient(host=stack.host, port=stack.port, timeout=15, use_ssl=True)
    client.channel = client._create_channel()
    client.stub = control_center_pb2_grpc.ControlServiceStub(client.channel)
    client.set_token(stack.tokens["execute"])
    try:
        result = client.execute_command(
            argv=["xdotool", "key", "ctrl+_"], human_command="press ^_",
        )
    finally:
        client.disconnect()

    assert not result["success"], (
        "a silently dropped key was reported as a successful command"
    )
