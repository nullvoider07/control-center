"""Verify that typed text arrives in a real text field byte-for-byte.

Every other test in this suite asserts on the argv that was *constructed*. This one
observes the keystrokes that actually *landed*, by typing through the full pipeline
(actuation builder -> TLS server -> agent -> xdotool) into a focused Tk Entry on the
harness Xvfb and reading the widget back.

It guards the regression that motivated the structured-argv work: the old builder
wrapped the payload in double quotes and ran it through `sh -c`, escaping only `\\`
and `"`. Shell metacharacters therefore stayed live, so ordinary text was silently
corrupted — `price $5 total` arrived as `price  total`, and `path $HOME/docs` leaked
the operator's home directory into the guest. Quotes alone were never the problem;
they mattered only because they interacted with shell parsing.
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

# Payloads that the shell-string builder mangled, plus quote combinations that must
# survive intact. Typed in sequence into one field and compared as a whole. Each ends
# with a non-space delimiter because the CLI strips surrounding whitespace, so a
# trailing space would not survive the builder and is not expected to.
PAYLOADS = [
    'say "hi"|',
    "it's|",
    'a"b\'c|',
    "price $5|",
    "path $HOME/x|",
    "sub $(whoami)|",
    "tick `id -u`|",
    r"back\slash|",
    "semi; amp& pipe|",
]

PROBE = r'''
import sys, tkinter as tk
out, dwell = sys.argv[1], int(sys.argv[2])
root = tk.Tk()
root.title("cc-typed-probe")
root.geometry("1000x120+40+40")
e = tk.Entry(root, width=140)
e.pack(padx=10, pady=30)
root.lift()
root.attributes("-topmost", True)
root.update_idletasks(); root.update()
# Toplevel focus first, then the widget: reversing these leaves the Entry without
# Tk focus, so XTEST keystrokes reach the window but land nowhere.
root.focus_force(); e.focus_set(); e.focus_force()
def dump():
    open(out, "w", encoding="utf-8").write(e.get())
    root.destroy()
root.after(dwell, dump)
root.mainloop()
'''


def _tk_available() -> bool:
    return subprocess.run(
        [sys.executable, "-c", "import tkinter"], capture_output=True
    ).returncode == 0


@pytest.mark.skipif(not _tk_available(), reason="tkinter not installed")
def test_typed_text_reaches_the_field_unmodified(stack, tmp_path):
    probe_py = tmp_path / "probe.py"
    probe_py.write_text(PROBE)
    out_file = tmp_path / "typed.txt"
    env = {**os.environ, "DISPLAY": harness.XVFB_DISPLAY}

    dwell_ms = 4000 + 400 * len(PAYLOADS)
    probe = subprocess.Popen(
        [sys.executable, str(probe_py), str(out_file), str(dwell_ms)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(2.0)  # let the window map and take focus
        subprocess.run(
            ["xdotool", "search", "--name", "cc-typed-probe",
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
            for payload in PAYLOADS:
                argv, human = la._build_keyboard_command(f"type {payload}")
                # The payload must survive the builder as one literal argv element.
                assert argv == ["xdotool", "type", payload]
                assert client.execute_command(argv=argv, human_command=human)["success"]
        finally:
            client.disconnect()

        probe.wait(timeout=30)
    finally:
        if probe.poll() is None:
            probe.kill()

    assert out_file.exists(), "probe never wrote its capture file"
    typed = out_file.read_text(encoding="utf-8")
    expected = "".join(PAYLOADS)
    assert typed == expected, (
        "typed text was altered in transit.\n"
        f"  expected: {expected!r}\n"
        f"  actual:   {typed!r}"
    )
