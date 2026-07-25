r"""Verify that what gets RECORDED matches what was actuated.

`test_typed_text_fidelity.py` observes the keystrokes that land in a text field.
This one observes the other half: the `CommandEvent.raw_command` the server
broadcasts on WatchCommands, which is the single value ma-core turns into
`commands/cc_commands.json`, `commands/actuation_commands.json`,
`commands/raw_input.md` and `commands/converted_input.md`. One assertion here
therefore covers all four stored files.

The defect this guards (S011, S013, S026) is worse than a failure. In S026 the
console echoed the full command, the guest Terminal showed it, and running it set
the wallpaper correctly — but every stored record read `type osascript -e \`. The
truncation was in the agent's `extract_osascript_human_command`, which located the
payload with `command.find("keystroke \"")` and then ended it at the first `"` found
by `after.find('"')`. Because the controller escaped `"` to `\"` before building the
shell string, that search stopped on the second half of an escape sequence.

v1.1.0 removed the extractor: `human_command` is carried on the wire
(CommandRequest field 6) and broadcast unmodified. These tests exist so that
property cannot silently regress — nothing else in the suite reads the record.
"""
import threading
import time

from conftest import requires_stack
from controller.integrations.gRPC import GRPCClient
from controller.integrations.proto import control_center_pb2_grpc
from controller.os_specific.linux_actuation import LinuxActuation

pytestmark = requires_stack

# The acceptance forms from the fidelity brief, plus the verbatim S026 payload that
# produced the truncated record. Each must survive into the record unaltered.
PAYLOADS = [
    'printf "hello world"',
    'osascript -e "tell application \\"System Events\\" to get name"',
    "sed -i '' 's/a/b/g' file.txt",
    'mix "double" and \'single\' and back\\slash',
    'osascript -e "tell application "System Events" to set picture of every '
    'desktop to "/Users/agentuser/corpus-seed/wall-A.jpg""',
]


class _Recorder:
    """Collect CommandEvents off a WatchCommands stream on a background thread."""

    def __init__(self, stack):
        self.client = GRPCClient(host=stack.host, port=stack.port, timeout=60,
                                 use_ssl=True)
        self.client.channel = self.client._create_channel()
        self.client.stub = control_center_pb2_grpc.ControlServiceStub(self.client.channel)
        self.client.set_token(stack.tokens["monitor"])
        self.events = []
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        for event in self.client.watch_commands():
            if not event["is_heartbeat"]:
                self.events.append(event)

    def __enter__(self):
        self._thread.start()
        time.sleep(1.5)  # let the subscription attach before commands are sent
        return self

    def __exit__(self, *exc):
        self.client.disconnect()
        self._thread.join(timeout=5)
        return False


def _send(stack, commands):
    """Send each console line through the real builder and stack."""
    la = LinuxActuation.__new__(LinuxActuation)
    la.display = ":99"
    client = GRPCClient(host=stack.host, port=stack.port, timeout=15, use_ssl=True)
    client.channel = client._create_channel()
    client.stub = control_center_pb2_grpc.ControlServiceStub(client.channel)
    client.set_token(stack.tokens["execute"])
    try:
        for line in commands:
            argv, human = la._build_keyboard_command(line)
            assert human == line, \
                f"builder altered the human command: {line!r} -> {human!r}"
            client.execute_command(argv=argv, human_command=human)
            time.sleep(0.1)
    finally:
        client.disconnect()


def test_typed_payload_is_recorded_verbatim(stack):
    lines = [f"type {payload}" for payload in PAYLOADS]

    with _Recorder(stack) as recorder:
        _send(stack, lines)
        time.sleep(2.0)  # let the broadcast drain

    recorded = [e["raw_command"] for e in recorder.events]
    assert len(recorded) == len(PAYLOADS), (
        f"expected {len(PAYLOADS)} recorded events, got {len(recorded)}: {recorded}"
    )

    mismatches = [
        f"  payload : {payload!r}\n  recorded: {actual!r}"
        for payload, actual in zip(PAYLOADS, recorded)
        if actual != f"Typed: {payload}"
    ]
    assert not mismatches, (
        "the record does not match what was actuated:\n" + "\n".join(mismatches)
    )


def test_record_is_not_truncated_at_the_first_quote(stack):
    """The precise S026 signature: truncation at the first escaped quote, leaving a
    trailing backslash. Asserted separately so a regression names itself."""
    payload = PAYLOADS[-1]

    with _Recorder(stack) as recorder:
        _send(stack, [f"type {payload}"])
        time.sleep(2.0)

    assert recorder.events, "no command event was broadcast"
    recorded = recorder.events[0]["raw_command"]

    assert recorded != "Typed: osascript -e \\", \
        "the S026 truncation has returned"
    assert not recorded.endswith("\\"), \
        f"record ends mid-escape-sequence: {recorded!r}"
    assert "wall-A.jpg" in recorded, \
        f"record lost everything after the first quote: {recorded!r}"


def test_press_command_is_recorded_verbatim(stack):
    """Keyboard shortcuts are recorded from the same field, so the same property has
    to hold for them — including the punctuation targets."""
    lines = ["press ^c", "press ^-", "press ^{Plus}", "press ^+{Left}", "press {Enter}"]
    expected = ["Pressed: Ctrl+C", "Pressed: Ctrl+-", "Pressed: Ctrl+Plus",
                "Pressed: Ctrl+Shift+Left", "Pressed: Return"]

    with _Recorder(stack) as recorder:
        _send(stack, lines)
        time.sleep(2.0)

    recorded = [e["raw_command"] for e in recorder.events]
    assert recorded == expected, (
        f"press commands were not recorded as expected:\n"
        f"  expected: {expected}\n  actual:   {recorded}"
    )
