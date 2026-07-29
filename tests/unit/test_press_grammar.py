"""Unit tests for `press` / modifier-key translation on all three platforms.

These guard the class of bug that motivated the punctuation keysym map: a press
command that the builder emits in a form the actuation backend cannot resolve. On
Linux that failed two ways — `ctrl+-` aborted the sequence outright, while `ctrl+,`
exited 0 after silently dropping the target and delivering a bare Ctrl press, which
the operator saw reported as a success.

The Linux cases are mirrored against the agent's `is_keysym` rule (argv_policy.rs),
because a spec the agent refuses never reaches xdotool at all.
"""
import pytest

from controller.os_specific.linux_actuation import LinuxActuation
from controller.os_specific.macos_actuation import MacOSActuation
from controller.os_specific.windows_actuation import WindowsActuation


def linux():
    la = LinuxActuation.__new__(LinuxActuation)
    la.display = ":0"
    return la


def macos():
    ma = MacOSActuation.__new__(MacOSActuation)
    ma.cliclick_path = "cliclick"
    return ma


def windows():
    return WindowsActuation.__new__(WindowsActuation)


def is_keysym(spec: str) -> bool:
    """Mirror of argv_policy::is_keysym — `[A-Za-z0-9_+]+`, non-empty."""
    return bool(spec) and all(
        (c.isalnum() and c.isascii()) or c in "_+" for c in spec
    )


def linux_spec(keys: str) -> str:
    argv, _ = linux()._build_keyboard_command(f"press {keys}")
    assert argv[:2] == ["xdotool", "key"]
    assert len(argv) == 3, f"press {keys!r} produced {argv!r}"
    return argv[2]


def windows_payload(keys: str) -> str:
    wa = windows()
    processed = wa._process_keyboard_command(f"press {keys}")
    return wa._convert_modifiers_to_explicit(processed.partition(" ")[2])


# ---- Linux: punctuation must become X keysym names -------------------------
# Every one of these was broken before the map existed: the left column is what
# xdotool needs, the raw character is what it used to receive.
LINUX_PUNCTUATION = [
    ("^-", "ctrl+minus"),          # zoom out
    ("^=", "ctrl+equal"),          # zoom in (unshifted)
    ("^,", "ctrl+comma"),          # preferences
    ("^.", "ctrl+period"),
    ("^/", "ctrl+slash"),          # toggle comment
    ("^;", "ctrl+semicolon"),
    ("^'", "ctrl+apostrophe"),
    ("^[", "ctrl+bracketleft"),    # outdent
    ("^]", "ctrl+bracketright"),   # indent
    ("^\\", "ctrl+backslash"),
    ("^`", "ctrl+grave"),
    ("^_", "ctrl+underscore"),
    ("^?", "ctrl+question"),
    ("^+{Left}", "ctrl+shift+Left"),
]


@pytest.mark.parametrize("keys,expected", LINUX_PUNCTUATION)
def test_linux_punctuation_becomes_a_keysym_name(keys, expected):
    assert linux_spec(keys) == expected


@pytest.mark.parametrize("keys", [k for k, _ in LINUX_PUNCTUATION])
def test_linux_punctuation_survives_the_agent_grammar(keys):
    """Keysym names are alphanumeric, so the fix needs no grammar widening."""
    assert is_keysym(linux_spec(keys)), \
        f"press {keys!r} would be refused by the agent"


LINUX_UNCHANGED = [
    ("^c", "ctrl+c"),
    ("^+z", "ctrl+shift+z"),
    ("^1", "ctrl+1"),
    ("#r", "super+r"),
    ("!{Tab}", "alt+Tab"),
    ("+{Tab}", "shift+Tab"),
    ("{Enter}", "Return"),
    ("{Esc}", "Escape"),
    ("{PgDn}", "Next"),
    ("{Space}", "space"),
    ("{F5}", "F5"),
    ("^{Left}", "ctrl+Left"),
    ("^", "ctrl"),          # standalone modifier
    ("#", "super"),
    ("+", "shift"),
    ("!", "alt"),
]


@pytest.mark.parametrize("keys,expected", LINUX_UNCHANGED)
def test_linux_existing_translations_are_unchanged(keys, expected):
    spec = linux_spec(keys)
    assert spec == expected
    assert is_keysym(spec)


def test_linux_unmapped_brace_name_is_still_refused():
    """`{Menu}` has no keysym mapping. It must not be silently guessed at — the
    agent refuses it, which is a clearer failure than xdotool's."""
    assert not is_keysym(linux_spec("{Menu}"))


# ---- {Plus}: the plus key, on every platform -------------------------------
# A bare '+' is unconditionally the Shift modifier, so "^+" is Ctrl+Shift and
# Ctrl+Plus needs an explicit brace name.
def test_bare_plus_still_means_shift():
    assert linux_spec("^+") == "ctrl+shift"
    assert windows_payload("^+") == "{Ctrl down}{Shift down}{Shift up}{Ctrl up}"
    argv, _ = macos().parse_keyboard_command("press ^+")
    assert argv == ["cliclick", "kd:ctrl,shift", "w:50", "ku:ctrl,shift"]


def test_plus_brace_name_reaches_the_plus_key():
    assert linux_spec("^{Plus}") == "ctrl+plus"
    assert windows_payload("^{Plus}") == "{Ctrl down}{+}{Ctrl up}"
    # macOS routes modified punctuation through a keycode: '+' is Shift+Equal, so
    # the emitter supplies the Shift the glyph needs.
    argv, _ = macos().parse_keyboard_command("press ^{Plus}")
    assert argv == [
        "osascript", "-e",
        'tell application "System Events" to key code 24 '
        "using {control down, shift down}",
    ]


def test_plus_brace_name_alone():
    assert linux_spec("{Plus}") == "plus"
    assert windows_payload("{Plus}") == "{+}"


# ---- macOS: standalone modifiers -------------------------------------------
@pytest.mark.parametrize("keys,mod", [
    ("#", "cmd"), ("^", "ctrl"), ("+", "shift"), ("!", "alt"),
])
def test_macos_standalone_modifier_is_a_bare_tap(keys, mod):
    """`main_key` used to keep its initial value when the whole string was
    modifiers, so "press #" sent Cmd while typing "#" instead of tapping Cmd."""
    argv, _ = macos().parse_keyboard_command(f"press {keys}")
    assert argv == ["cliclick", f"kd:{mod}", "w:50", f"ku:{mod}"]


def test_macos_modified_key_still_holds_the_modifier():
    argv, _ = macos().parse_keyboard_command("press #c")
    assert argv == ["cliclick", "kd:cmd", "t:c", "ku:cmd"]


def test_macos_navigation_keys_still_use_key_codes():
    argv, _ = macos().parse_keyboard_command("press ^+{Left}")
    assert argv == [
        "osascript", "-e",
        'tell application "System Events" to key code 123 '
        "using {control down, shift down}",
    ]


# ---- Windows: typed text must never be routed through Send -----------------
# keyboard_control.ahk dispatches `type` to SendText (literal) and `press` to Send
# (metacharacters live). A '^' in the payload used to flip type into press, making
# every other AHK metacharacter active: "type a^b!c" pressed Alt+C.
WINDOWS_TYPE_PAYLOADS = [
    "a^b!c",
    "v1^2 #r",
    "caret ^ alone",
    "x^y{Enter}z",
    "2^10 = 1024",
]


@pytest.mark.parametrize("payload", WINDOWS_TYPE_PAYLOADS)
def test_windows_type_with_caret_stays_a_type_action(payload):
    wa = windows()
    processed = wa._process_keyboard_command(f"type {payload}")
    action, _, content = processed.partition(" ")
    assert action == "type"
    assert content == payload, "typed text must not be rewritten"


def test_windows_press_still_converts_modifiers():
    assert windows_payload("^t") == "{Ctrl down}t{Ctrl up}"
    assert windows_payload("!{Tab}") == "{Alt down}{Tab}{Alt up}"
    assert windows_payload("{F5}") == "{F5}"


def test_windows_cmd_escaping_helper_is_gone():
    """No cmd.exe is involved any more — `__write__` writes the file directly, so
    escaping <>|&" would corrupt the payload rather than protect it."""
    assert not hasattr(WindowsActuation, "_escape_cmd_special")


# ---- Windows: the transport form must not become the record ----------------
# argv carries the AHK expansion because `Send` needs it; human_command is what the
# agent turns into the display string the server stores as CommandEvent.raw_command.
# Reporting the expansion put "{Ctrl down}s{Ctrl up}" into the corpus, which the
# agent's brace-unwrap then mangled to "Ctrl down}s{Ctrl up" — an unbalanced brace
# that panicked the Memory Archive converter and voided the whole session.


class _StubClient:
    """Records what the controller sent; reports every command as succeeding."""

    def __init__(self):
        self.sent = []

    def execute_command(self, argv, human_command):
        self.sent.append((argv, human_command))
        return {
            'success': True,
            'message': 'ok',
            'execution_time_ms': 3,
            'mouse_x': 0,
            'mouse_y': 0,
            'position_captured': False,
            'metadata': {},
        }


def windows_sent(command: str):
    """Run one command through the real execute_command and return (argv, human)."""
    wa = windows()
    wa.grpc_client = _StubClient()
    assert wa.execute_command(command) is True, f"{command!r} was not executed"
    assert len(wa.grpc_client.sent) == 1
    return wa.grpc_client.sent[0]


WINDOWS_RECORDING = [
    # issued,        argv payload (AHK transport),                         recorded
    ("press ^s",     "press {Ctrl down}s{Ctrl up}",                        "press ^s"),
    ("press ^+n",    "press {Ctrl down}{Shift down}n{Shift up}{Ctrl up}",  "press ^+n"),
    ("press ^{Tab}", "press {Ctrl down}{Tab}{Ctrl up}",                    "press ^{Tab}"),
    ("^a",           "press {Ctrl down}a{Ctrl up}",                        "press ^a"),
    ("press !{F4}",  "press {Alt down}{F4}{Alt up}",                       "press !{F4}"),
    # Bare modifiers and unmodified keys need no expansion — the two agree already.
    ("press {F5}",   "press {F5}",                                         "press {F5}"),
    ("press {LWin}", "press {LWin}",                                       "press {LWin}"),
    # Typed text stays on the `type` action and is never rewritten.
    ("type a^b!c",   "type a^b!c",                                         "type a^b!c"),
    # Quotes and backslashes: nothing on this path escapes or splits on them, so the
    # payload reaches the AHK file and the record byte-identical. keyboard_control.ahk
    # splits at the first space and hands the rest to SendText. The macOS and Linux
    # halves of this property live in test_actuation_argv.py (QUOTED_PAYLOADS).
    ('type printf "Title\\nBody" > note.txt',
     'type printf "Title\\nBody" > note.txt',
     'type printf "Title\\nBody" > note.txt'),
    ('type osascript -e "tell app \\"X\\" to y"',
     'type osascript -e "tell app \\"X\\" to y"',
     'type osascript -e "tell app \\"X\\" to y"'),
    ('type ends with a backslash \\',
     'type ends with a backslash \\',
     'type ends with a backslash \\'),
]


@pytest.mark.parametrize("command,payload,recorded", WINDOWS_RECORDING)
def test_windows_keyboard_records_the_command_not_the_transport(
        command, payload, recorded):
    argv, human = windows_sent(command)
    assert argv == ['__write__', r'C:\keyboard_cmd.txt', payload], \
        "the AHK expansion must still reach the guest unchanged"
    assert human == recorded, "the recorded command is not the one that was issued"


@pytest.mark.parametrize("command,_payload,_recorded", WINDOWS_RECORDING)
def test_windows_never_reports_a_down_up_sequence(command, _payload, _recorded):
    """The direct symptom: any ' down}' in human_command reaches the converter as an
    unbalanced brace once the agent strips the outer pair."""
    _argv, human = windows_sent(command)
    assert " down}" not in human and " up}" not in human, human
    assert human.count("{") == human.count("}"), human


def test_windows_batch_records_the_same_form_as_interactive(tmp_path):
    """execute_batch_file builds its own payload, so the split has to hold there too."""
    script = tmp_path / "batch.txt"
    script.write_text("press ^s\n960 540 left\n")

    wa = windows()
    wa.grpc_client = _StubClient()
    wa.execute_batch_file(str(script))

    sent = wa.grpc_client.sent
    assert [human for _argv, human in sent] == ["press ^s", "960 540 left"]
    assert sent[0][0] == ['__write__', r'C:\keyboard_cmd.txt',
                          "press {Ctrl down}s{Ctrl up}"]
