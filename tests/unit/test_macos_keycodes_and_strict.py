"""Unit tests for the macOS keycode reroute, strict mode, keycode passthrough and drag.

Each group guards a defect that reported success while doing the wrong thing — the
failure class that costs a corpus take, because the on-screen artifact and every
self-reporting signal look fine and only the stored record or a missing screenshot
gives it away.
"""
import pytest

from controller.os_specific import command_hints
from controller.os_specific.linux_actuation import LinuxActuation
from controller.os_specific.macos_actuation import MacOSActuation
from controller.os_specific.windows_actuation import WindowsActuation


def macos(strict: bool = True):
    ma = MacOSActuation.__new__(MacOSActuation)
    ma.cliclick_path = "cliclick"
    ma.strict = strict
    return ma


def script(command: str):
    """Return the single AppleScript body a press command produces."""
    built = macos().parse_keyboard_command(command)
    assert built is not None, f"{command!r} was rejected"
    argv, _ = built
    assert argv[0] == "osascript" and argv[1] == "-e" and len(argv) == 3, argv
    return argv[2]


PREFIX = 'tell application "System Events" to '


# ---- Task 2: modified digits and punctuation need a keycode ----------------
# cliclick's t: synthesizes a character event; macOS hotkeys match on keycode +
# modifier flags, so ⇧⌘4 never fired and reported success with no screenshot taken.
@pytest.mark.parametrize("keys,expected", [
    ("#+4", "key code 21 using {command down, shift down}"),   # region capture
    ("#+3", "key code 20 using {command down, shift down}"),   # full screen
    ("#+5", "key code 23 using {command down, shift down}"),   # screenshot toolbar
    ("#+1", "key code 18 using {command down, shift down}"),
    ("#+0", "key code 29 using {command down, shift down}"),
    ("#-", "key code 27 using {command down}"),
    ("#,", "key code 43 using {command down}"),
    ("#/", "key code 44 using {command down}"),
    ("#[", "key code 33 using {command down}"),
    ("^`", "key code 50 using {control down}"),
])
def test_modified_digit_and_punctuation_use_key_codes(keys, expected):
    assert script(f"press {keys}") == PREFIX + expected


def test_shifted_glyph_supplies_its_own_shift():
    """'$' is Shift+4. Writing it directly must still hold Shift."""
    assert script("press #$") == \
        PREFIX + "key code 21 using {command down, shift down}"
    assert script("press #_") == \
        PREFIX + "key code 27 using {command down, shift down}"


def test_shift_is_not_duplicated_when_already_present():
    body = script("press #+$")
    assert body.count("shift down") == 1, body


# ---- Task 2 regression lock: paths that already worked --------------------
@pytest.mark.parametrize("keys,expected", [
    ("#q", ["cliclick", "kd:cmd", "t:q", "ku:cmd"]),
    ("#+g", ["cliclick", "kd:cmd,shift", "t:g", "ku:cmd,shift"]),
    ("{Space}", ["cliclick", "kp:space"]),
    ("{F5}", ["cliclick", "kp:f5"]),
    ("#", ["cliclick", "kd:cmd", "w:50", "ku:cmd"]),
    ("4", ["cliclick", "t:4"]),          # unmodified: no hotkey to match
    ("#{Mute}", ["cliclick", "kd:cmd", "kp:mute", "ku:cmd"]),  # no virtual keycode
])
def test_working_press_paths_are_unchanged(keys, expected):
    argv, _ = macos().parse_keyboard_command(f"press {keys}")
    assert argv == expected


def test_navigation_keys_still_use_their_own_key_codes():
    assert script("press ^+{Left}") == \
        PREFIX + "key code 123 using {control down, shift down}"


# ---- Item 3: a modified named key must carry its own modifiers -------------
# "kd:cmd kp:space ku:cmd" posts three independent events; the key event carries
# no flags of its own and relies on the system having already applied the modifier
# keydown. ⌘Space opened Spotlight only sometimes, and when it did not the whole
# following step sequence landed in whatever was frontmost — Finder type-selected
# a folder and silently changed state.
@pytest.mark.parametrize("keys,expected", [
    ("#{Space}", "key code 49 using {command down}"),          # Spotlight
    ("^{Space}", "key code 49 using {control down}"),          # input source
    ("!#{Space}", "key code 49 using {option down, command down}"),
    ("#{Esc}", "key code 53 using {command down}"),
    ("^{F2}", "key code 120 using {control down}"),            # focus menu bar
    ("#{F5}", "key code 96 using {command down}"),
    ("#{F16}", "key code 106 using {command down}"),
    ("#space", "key code 49 using {command down}"),            # bare-word form
    ("#Space", "key code 49 using {command down}"),
])
def test_modified_named_keys_use_key_codes(keys, expected):
    assert script(f"press {keys}") == PREFIX + expected


def test_modifier_order_follows_the_command():
    """The "using" clause is built from the modifiers as parsed, so a two-modifier
    hotkey names both and nothing else."""
    body = script("press ^!{Space}")
    assert body == PREFIX + "key code 49 using {control down, option down}"


# ---- Task 4: {code:N} passthrough -----------------------------------------
def test_keycode_passthrough():
    assert script("press {code:21}") == PREFIX + "key code 21"
    assert script("press #+{code:21}") == \
        PREFIX + "key code 21 using {command down, shift down}"
    assert script("press {code:0}") == PREFIX + "key code 0"
    assert script("press {code:127}") == PREFIX + "key code 127"


@pytest.mark.parametrize("keys", ["{code:128}", "{code:200}", "{code:x}", "{code:}"])
def test_malformed_keycode_is_rejected_not_typed(keys):
    """A silent fall-through would type the literal '{code:200}' into whatever has
    focus and record it as a successful step — the S019 failure in miniature."""
    assert macos().parse_keyboard_command(f"press {keys}") is None


# ---- Task 5: drag dwell and waypoints -------------------------------------
def drag(command: str):
    built = macos().build_mouse_command(command)
    return built[0] if built else None


def test_plain_drag_is_unchanged():
    assert drag("100 100 drag 900 700") == [
        "cliclick", "dd:100,100", "w:50", "dm:900,700", "w:50", "du:900,700",
    ]


def test_drag_dwell_override():
    assert drag("100 100 drag 900 700 dwell 150") == [
        "cliclick", "dd:100,100", "w:150", "dm:900,700", "w:150", "du:900,700",
    ]


def test_drag_waypoints():
    assert drag("100 100 drag via 400 300 via 700 500 to 900 700") == [
        "cliclick", "dd:100,100", "w:50", "dm:400,300", "w:50",
        "dm:700,500", "w:50", "dm:900,700", "w:50", "du:900,700",
    ]


def test_drag_waypoints_with_dwell():
    assert drag("100 100 drag via 400 300 to 900 700 dwell 200") == [
        "cliclick", "dd:100,100", "w:200", "dm:400,300", "w:200",
        "dm:900,700", "w:200", "du:900,700",
    ]


# ---- Item 1: the moves between the press and the release must be drags -----
@pytest.mark.parametrize("command", [
    "100 100 drag 900 700",
    "100 100 drag 900 700 dwell 300",
    "100 100 drag via 400 300 via 700 500 to 900 700",
    "100 100 drag via 400 300 to 900 700 dwell 200",
])
def test_drag_moves_continue_the_drag(command):
    """cliclick's m: posts mouseMoved and dm: posts leftMouseDragged. Anything that
    tracks a drag listens for the latter, so a run built from m: is seen as a press,
    unrelated pointer motion and a release: the ⇧⌘4 overlay drew no selection and
    wrote no file while the command reported success."""
    argv = drag(command)
    assert argv is not None, f"{command!r} failed to build"

    moves = argv[argv.index("dd:100,100") + 1:argv.index("du:900,700")]
    assert any(t.startswith("dm:") for t in moves), argv
    assert not any(t.startswith("m:") for t in moves), \
        f"a plain move between the press and the release is not a drag: {argv}"


@pytest.mark.parametrize("command", [
    "100 100 drag 900 700 dwell 0",
    "100 100 drag 900 700 dwell 5001",
    "100 100 drag 900 700 dwell abc",
    "100 100 drag via 400 300 900 700",       # missing 'to'
    "100 100 drag 900 700 999",               # trailing junk
    "100 100 drag via 400 to 900 700",        # waypoint missing a coordinate
])
def test_malformed_drag_is_rejected(command):
    assert drag(command) is None


def test_drag_waypoint_limit():
    via = " ".join("via 10 10" for _ in range(17))
    assert drag(f"100 100 drag {via} to 900 700") is None


# ---- Item 2: the echo must name the destination the drag actually reached --
class _StubClient:
    """Accepts any argv and reports the drag as having landed at `position`."""

    def __init__(self, position=(850, 650)):
        self.position = position
        self.sent = []

    def execute_command(self, argv, human_command):
        self.sent.append((argv, human_command))
        return {
            'success': True,
            'message': 'ok',
            'execution_time_ms': 2270,
            'mouse_x': self.position[0],
            'mouse_y': self.position[1],
            'position_captured': True,
            'metadata': {},
        }


def echo(command: str, capsys) -> str:
    ma = macos()
    ma.grpc_client = _StubClient()
    assert ma.execute_command(command) is True, f"{command!r} was not executed"
    return capsys.readouterr().out


@pytest.mark.parametrize("command,expected", [
    ("150 150 drag 850 650", "to X=850, Y=650"),
    ("150 150 drag 850 650 dwell 300", "to X=850, Y=650"),
    ("150 150 drag via 400 350 to 850 650", "to X=850, Y=650"),
    ("150 150 drag via 400 350 via 650 500 to 850 650 dwell 300", "to X=850, Y=650"),
])
def test_drag_echo_names_the_real_destination(command, expected, capsys):
    """The echo used to read the destination from the two tokens after "drag", which
    is the literal "via" in the waypoint form: a drag that ran to (850, 650) reported
    "dragged from X=150, Y=150 to X=via, Y=400". The action landed and the report
    lied — the failure class the position work exists to close."""
    out = echo(command, capsys)
    assert "dragged from X=150, Y=150" in out, out
    assert expected in out, out
    assert "X=via" not in out, out


def test_drag_echo_agrees_with_the_argv_it_sent(capsys):
    """One parser feeds both, so the reported endpoint and the released point are the
    same value rather than two readings of the same tokens."""
    ma = macos()
    ma.grpc_client = _StubClient()
    ma.execute_command("150 150 drag via 400 350 via 650 500 to 850 650 dwell 300")
    argv, _ = ma.grpc_client.sent[0]
    out = capsys.readouterr().out

    assert argv[-1] == "du:850,650", argv
    assert "to X=850, Y=650" in out, out


def test_drag_stays_one_recorded_step():
    """human_command is the original line, so ma-core records one step however many
    cliclick tokens the drag expands to."""
    command = "100 100 drag via 400 300 to 900 700 dwell 120"
    _, human = macos().build_mouse_command(command)
    assert human == command


# ---- Task 3: strict mode --------------------------------------------------
UNRECOGNISED = ["1022 343left", "foo bar", "hello world!", "typehello", "343left"]


@pytest.mark.parametrize("cls", [MacOSActuation, LinuxActuation, WindowsActuation])
@pytest.mark.parametrize("command", UNRECOGNISED)
def test_strict_mode_rejects_unrecognised_commands(cls, command):
    inst = cls.__new__(cls)
    inst.strict = True
    assert inst.detect_command_type(command)[0] == "invalid"


@pytest.mark.parametrize("cls", [MacOSActuation, LinuxActuation, WindowsActuation])
@pytest.mark.parametrize("command", UNRECOGNISED)
def test_lenient_mode_preserves_the_old_fall_through(cls, command):
    inst = cls.__new__(cls)
    inst.strict = False
    kind, processed = inst.detect_command_type(command)
    assert kind == "keyboard"
    assert processed == f"type {command}"


@pytest.mark.parametrize("cls", [MacOSActuation, LinuxActuation, WindowsActuation])
@pytest.mark.parametrize("command", [
    "type 1022 343left", "type hello world!", "1022 343 left", "press ^c", "here left",
])
def test_strict_mode_accepts_valid_commands(cls, command):
    inst = cls.__new__(cls)
    inst.strict = True
    assert inst.detect_command_type(command)[0] != "invalid"


def test_strict_is_on_by_default():
    assert MacOSActuation.strict is True
    assert LinuxActuation.strict is True
    assert WindowsActuation.strict is True


def test_bare_modifier_symbol_inside_text_is_not_a_keypress():
    """MODIFIER_MAP holds the bare symbols ^ + ! #, and the old membership test was
    `in command`, so any text merely containing one was rerouted to a keypress."""
    inst = macos(strict=False)
    for command in ["hello world!", "2 + 2", "issue #12", "a^b"]:
        _, processed = inst.detect_command_type(command)
        assert processed != f"press {command}", \
            f"{command!r} was rerouted to a keypress"


def test_modifier_symbol_in_prefix_position_is_still_a_keypress():
    inst = macos()
    for command in ["^c", "#q", "+{Tab}", "!{Tab}"]:
        kind, processed = inst.detect_command_type(command)
        assert kind == "keyboard"
        assert processed == f"press {command}"


# ---- Record fidelity: human_command must leave the builder untouched -------
# The integration suite proves the record survives the wire, but its agent runs on
# Linux. This is the macOS half: the value the controller puts in
# CommandRequest.human_command is what ends up in every stored file, so the builder
# must not transform it. The v1.0.0 agent derived it from the escaped shell string
# instead and truncated at the first escaped quote (S011/S013/S026).
RECORD_PAYLOADS = [
    'printf "hello world"',
    'osascript -e "tell application \\"System Events\\" to get name"',
    "sed -i '' 's/a/b/g' file.txt",
    'mix "double" and \'single\' and back\\slash',
    'osascript -e "tell application "System Events" to set picture of every '
    'desktop to "/Users/agentuser/corpus-seed/wall-A.jpg""',
]


@pytest.mark.parametrize("payload", RECORD_PAYLOADS)
def test_macos_type_leaves_the_human_command_verbatim(payload):
    line = f"type {payload}"
    _, human = macos().parse_keyboard_command(line)
    assert human == line


@pytest.mark.parametrize("payload", RECORD_PAYLOADS)
def test_macos_type_payload_is_one_argv_element(payload):
    """The payload is embedded in the AppleScript as a closed string literal and
    handed to osascript as a single argv element — no shell, so no re-escaping pass
    can lose or reinterpret part of it."""
    argv, _ = macos().parse_keyboard_command(f"type {payload}")
    assert argv[0] == "osascript" and argv[1] == "-e" and len(argv) == 3
    assert argv[2].startswith(PREFIX + 'keystroke "')
    assert argv[2].endswith('"')


@pytest.mark.parametrize("keys,expected", [
    ("^c", "press ^c"),
    ("#+4", "press #+4"),
    ("#+{code:21}", "press #+{code:21}"),
])
def test_macos_press_leaves_the_human_command_verbatim(keys, expected):
    _, human = macos().parse_keyboard_command(f"press {keys}")
    assert human == expected


# ---- Task 3: repair hints -------------------------------------------------
@pytest.mark.parametrize("command,expected", [
    ("1022 343left", "1022 343 left"),
    ("500 200right", "500 200 right"),
    ("typehello", "type hello"),
    ("presstab", "press tab"),
])
def test_suggestion_finds_the_dropped_space(command, expected):
    assert command_hints.suggest(
        command, MacOSActuation.MOUSE_ACTIONS, MacOSActuation.KEYBOARD_ACTIONS
    ) == expected


def test_no_suggestion_when_nothing_plausible():
    assert command_hints.suggest(
        "foo bar", MacOSActuation.MOUSE_ACTIONS, MacOSActuation.KEYBOARD_ACTIONS
    ) is None


def test_rejection_message_always_offers_the_literal_escape():
    message = command_hints.rejection_message(
        "1022 343left", MacOSActuation.MOUSE_ACTIONS, MacOSActuation.KEYBOARD_ACTIONS
    )
    assert "Did you mean: 1022 343 left" in message
    # The escape is named as a form, not by echoing the command back a second time —
    # see tests/unit/test_typed_text_not_echoed.py.
    assert "type <text>" in message
    assert "--lenient" in message


# ---- typed text survives the whole macOS controller path -------------------
# The builder-level halves of this live in tests/unit/test_actuation_argv.py
# (QUOTED_PAYLOADS). This one goes through execute_command, so the dispatcher in
# detect_command_type is covered too: a `type` payload full of quotes must not be
# re-routed, re-split or altered on its way to human_command.

@pytest.mark.parametrize("payload", [
    'printf "Title\\nBody" > note.txt',
    'say "hi"',
    'osascript -e "tell app \\"X\\" to y"',
    'ends with a backslash \\',
])
def test_typed_text_reaches_the_wire_as_issued(payload):
    ma = macos()
    ma.strict = True
    ma.grpc_client = _StubClient()
    command = f"type {payload}"
    assert ma.execute_command(command) is True

    (argv, human), = ma.grpc_client.sent
    assert human == command, "the command was altered before it was recorded"
    assert argv[:2] == ["osascript", "-e"] and len(argv) == 3, argv
    # The payload is one argv element; there is no shell, so nothing re-parses it.
    assert payload.split('"')[0] in argv[2]
