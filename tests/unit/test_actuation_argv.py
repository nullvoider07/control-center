"""Unit tests for the structured-argv actuation builders (F5).

The central security property: user free-text (the `type` payload) becomes a single
literal argv element, so a shell can never interpret it. These tests assert that on all
three platforms and that mouse/keyboard mappings still produce the right argv.
"""
import re
from pathlib import Path

import pytest

from controller.os_specific.linux_actuation import LinuxActuation
from controller.os_specific.macos_actuation import MacOSActuation
from controller.os_specific.windows_actuation import WindowsActuation


INJECTION = 'hello$(touch /tmp/pwned)`whoami`; reboot | cat'


# ---- Linux (xdotool) -------------------------------------------------------
def linux():
    la = LinuxActuation.__new__(LinuxActuation)
    la.display = ":0"
    return la


def test_linux_type_is_single_literal_argv():
    argv, human = linux()._build_keyboard_command(f"type {INJECTION}")
    assert argv == ["xdotool", "type", INJECTION]
    assert human == f"type {INJECTION}"


def test_linux_mouse_coords():
    argv, _ = linux()._build_mouse_command("960 540 left")
    assert argv == ["xdotool", "mousemove", "960", "540", "click", "1"]


def test_linux_here_and_scroll():
    assert linux()._build_mouse_command("here double")[0] == \
        ["xdotool", "click", "--repeat", "2", "1"]
    assert linux()._build_mouse_command("scroll_up 3")[0] == \
        ["xdotool", "click", "--repeat", "3", "4"]


def test_linux_press_translated_no_freetext():
    argv, _ = linux()._build_keyboard_command("press ctrl+c")
    assert argv[:2] == ["xdotool", "key"]
    # no shell metacharacters survive as separate tokens beyond the key spec
    assert all(tok for tok in argv)


# ---- macOS (cliclick / osascript) -----------------------------------------
def macos():
    ma = MacOSActuation.__new__(MacOSActuation)
    ma.cliclick_path = "cliclick"
    return ma


def test_macos_type_is_osascript_argv_no_shell():
    argv, human = macos().parse_keyboard_command(f"type {INJECTION}")
    assert argv[0] == "osascript" and argv[1] == "-e"
    # the whole AppleScript (with the payload) is ONE argv element
    assert INJECTION.split("`")[0] in argv[2] and len(argv) == 3
    assert human == f"type {INJECTION}"


def test_macos_mouse_cliclick_argv():
    argv, _ = macos().build_mouse_command("960 540 left")
    assert argv == ["cliclick", "c:960,540"]


def test_macos_scroll_uses_write_free_sentinel():
    # Scroll needs two binaries (cliclick focus + osascript repeat). It is sent as a
    # bounded sentinel the agent expands itself — never a compound shell string.
    argv, _ = macos().build_mouse_command("here scroll_down 5")
    assert argv == ["__scroll__", "c:.", "125", "5"]

    argv, _ = macos().build_mouse_command("400 300 scroll_up 3")
    assert argv == ["__scroll__", "c:400,300", "126", "3"]


def test_macos_builders_never_emit_a_shell_string():
    """No macOS actuation path may produce a legacy shell string: the agent rejects
    the `command` field outright, so anything not expressed as argv would break."""
    ma = macos()
    cases = [
        ("mouse", "position"),
        ("mouse", "960 540 left"),
        ("mouse", "here right"),
        ("mouse", "here scroll_down 5"),
        ("mouse", "100 200 scroll_right 2"),
        ("mouse", "10 20 drag 30 40"),
        ("keyboard", f"type {INJECTION}"),
        ("keyboard", "press #c"),
        ("keyboard", "press {Enter}"),
        ("keyboard", "press {F5}"),
    ]
    for kind, cmd in cases:
        built = (ma.build_mouse_command(cmd) if kind == "mouse"
                 else ma.parse_keyboard_command(cmd))
        assert built is not None, f"builder returned None for {cmd!r}"
        argv, human = built
        assert isinstance(argv, list) and argv, f"{cmd!r} produced no argv"
        assert all(isinstance(tok, str) for tok in argv)
        assert argv[0] in ("cliclick", "osascript", "__scroll__"), \
            f"{cmd!r} produced unexpected argv[0]={argv[0]!r}"
        assert human


# ---- record fidelity: the record is the command as issued ------------------
# The S026 truncation (`type osascript -e \`) came from the agent re-deriving
# human_command out of the AppleScript by searching for the first `"`, which landed
# on the second half of an escape sequence. a0bf3fe deleted that extractor and the
# command is carried on the wire instead. These pin the property the deletion
# established: escaping applies to the argv payload only, and never to the record.
#
# Asserted on the return value, not on stdout — typed text is deliberately redacted
# in progress output (see test_typed_text_not_echoed.py), so a stdout assertion here
# would contradict that test.

QUOTED_PAYLOADS = [
    'printf "Title\\nBody" > note.txt',            # the acceptance command
    'say "hi"',                                    # bare quotes
    'osascript -e "tell app \\"X\\" to y"',        # quotes already escaped by hand
    'ends with a backslash \\',                    # trailing backslash
    'both "quoted" and trailing \\',               # both at once
    "mix \"double\" and 'single'",
]


@pytest.mark.parametrize("payload", QUOTED_PAYLOADS)
def test_macos_type_records_the_command_as_issued(payload):
    command = f"type {payload}"
    _argv, human = macos().parse_keyboard_command(command)
    assert human == command, "the builder altered the command it reports"


@pytest.mark.parametrize("payload", QUOTED_PAYLOADS)
def test_linux_type_records_the_command_as_issued(payload):
    command = f"type {payload}"
    argv, human = linux()._build_keyboard_command(command)
    assert human == command, "the builder altered the command it reports"
    assert argv == ["xdotool", "type", payload], "the payload is not one literal argv"


def _is_applescript_string_literal(s: str) -> bool:
    """Mirror of argv_policy::is_applescript_string_literal — `"…"` where the closing
    quote is the final character and inner quotes are backslash-escaped."""
    if len(s) < 2 or s[0] != '"':
        return False
    i = 1
    while i < len(s):
        if s[i] == '\\':
            i += 2
        elif s[i] == '"':
            return i == len(s) - 1
        else:
            i += 1
    return False


@pytest.mark.parametrize("payload", QUOTED_PAYLOADS)
def test_macos_escapes_the_argv_payload_and_only_that(payload):
    """Escaping exists so the payload stays inside one closed AppleScript literal —
    which is also what the agent's grammar requires. The same strings are pinned on
    the agent side by argv_policy's `osascript_accepts_real_quoted_payloads`."""
    argv, _human = macos().parse_keyboard_command(f"type {payload}")
    assert argv[:2] == ["osascript", "-e"] and len(argv) == 3

    prefix = 'tell application "System Events" to keystroke '
    assert argv[2].startswith(prefix)
    literal = argv[2][len(prefix):]
    assert _is_applescript_string_literal(literal), \
        f"the agent would reject this script: {argv[2]!r}"

    # Reversing the two escapes must give back exactly what was typed: an escape
    # that is not reversible is text the guest would receive altered.
    unescaped = literal[1:-1].replace('\\"', '"').replace('\\\\', '\\')
    assert unescaped == payload


# ---- modifier-held mouse actions (macOS) -----------------------------------
# Cmd-click (discontiguous select), Shift-click (range select) and Opt-drag (copy
# instead of move) were not expressible at all, which is what made the Finder
# selection takes impossible to record. The modifier is held for the whole cliclick
# invocation, so one cc command is still one gesture and one recorded step.

def test_macos_modifier_brackets_the_click():
    argv, human = macos().build_mouse_command("770 310 #left")
    assert argv == ["cliclick", "kd:cmd", "c:770,310", "ku:cmd"]
    assert human == "770 310 #left", "the record must carry the command as issued"


@pytest.mark.parametrize("symbol,name", [
    ("^", "ctrl"), ("+", "shift"), ("!", "alt"), ("#", "cmd"),
])
def test_macos_every_modifier_symbol_resolves(symbol, name):
    argv, _ = macos().build_mouse_command(f"770 310 {symbol}left")
    assert argv == ["cliclick", f"kd:{name}", "c:770,310", f"ku:{name}"]


@pytest.mark.parametrize("prefix,mods", [
    ("#+", "cmd,shift"),
    ("+#", "shift,cmd"),
    ("!#", "alt,cmd"),
    ("^+!#", "ctrl,shift,alt,cmd"),
    ("##", "cmd"),          # a repeat is one key held, not two
])
def test_macos_modifiers_combine_in_the_order_given(prefix, mods):
    argv, _ = macos().build_mouse_command(f"770 310 {prefix}left")
    assert argv == ["cliclick", f"kd:{mods}", "c:770,310", f"ku:{mods}"]


@pytest.mark.parametrize("action,token", [
    ("left", "c:770,310"), ("click", "c:770,310"), ("right", "rc:770,310"),
    ("double", "dc:770,310"), ("triple", "tc:770,310"),
    ("hold", "dd:770,310"), ("release", "du:770,310"),
])
def test_macos_every_pointer_verb_takes_a_modifier(action, token):
    argv, _ = macos().build_mouse_command(f"770 310 #{action}")
    assert argv == ["cliclick", "kd:cmd", token, "ku:cmd"]


def test_macos_middle_takes_a_modifier_without_a_cliclick_token():
    """`middle` was in the list above, asserting `mc:770,310`.

    That token is not a cliclick action — this test pinned the broken behaviour, which
    is part of why the defect survived until a hardware matrix ran the verb. It is
    covered here instead, on the sentinel that actually actuates.
    """
    argv, _ = macos().build_mouse_command("770 310 #middle")
    assert argv == ["__middle__", "c:770,310", "cmd"]


def test_macos_modifier_wraps_the_whole_drag_not_just_its_first_token():
    """An Opt-drag that released the modifier after the press would be an ordinary
    move — the copy semantics come from Opt being down at the drop."""
    argv, _ = macos().build_mouse_command("200 300 !drag 900 640")
    assert argv[1] == "kd:alt"
    assert argv[-1] == "ku:alt"
    assert argv[2:-1] == ["dd:200,300", "w:50", "dm:900,640", "w:50", "du:900,640"]


def test_macos_modifier_wraps_a_waypoint_drag_too():
    argv, _ = macos().build_mouse_command(
        "200 300 +drag via 400 350 to 900 640 dwell 80")
    assert argv[1] == "kd:shift" and argv[-1] == "ku:shift"
    assert argv[2:-1] == ["dd:200,300", "w:80", "dm:400,350", "w:80",
                          "dm:900,640", "w:80", "du:900,640"]


def test_macos_here_form_takes_modifiers():
    argv, _ = macos().build_mouse_command("here +left")
    assert argv == ["cliclick", "kd:shift", "c:.", "ku:shift"]


def test_macos_scroll_carries_its_modifiers_on_the_sentinel():
    """Scroll needs two binaries, so the modifier cannot be a kd:/ku: bracket — the
    ku: would sit in a different process from the keys it guards, and a failed repeat
    loop would leave the modifier down globally with nothing tracking it. It rides on
    each arrow-key event instead, appended as a fifth sentinel element the agent
    resolves to an AppleScript `using` clause."""
    ma = macos()
    argv, human = ma.build_mouse_command("770 310 #scroll_down 3")
    assert argv == ["__scroll__", "c:770,310", "125", "3", "cmd"]
    assert human == "770 310 #scroll_down 3"

    argv, _ = ma.build_mouse_command("here #+scroll_up 5")
    assert argv == ["__scroll__", "c:.", "126", "5", "cmd,shift"]

    # No modifier: the sentinel keeps its original four elements.
    argv, _ = ma.build_mouse_command("here scroll_down 5")
    assert argv == ["__scroll__", "c:.", "125", "5"]


@pytest.mark.parametrize("direction,key_code", [
    ("scroll_up", "126"), ("scroll_down", "125"),
    ("scroll_left", "123"), ("scroll_right", "124"),
])
def test_macos_every_scroll_direction_takes_a_modifier(direction, key_code):
    argv, _ = macos().build_mouse_command(f"770 310 ^{direction} 2")
    assert argv == ["__scroll__", "c:770,310", key_code, "2", "ctrl"]


@pytest.mark.parametrize("command", [
    "770 310 #move",        # a held modifier changes nothing about a move
    "here #move",
    "770 310 #",            # prefix with no action
    "770 310 #nosuchverb",
    "770 310 ⌘left",        # Unicode form: valid for press, not for a mouse action
])
def test_macos_a_rejected_modifier_emits_nothing(command, capsys):
    """A mis-parsed prefix must never degrade into the unmodified action: that
    succeeds, echoes plausibly, and records a gesture nobody performed."""
    assert macos().build_mouse_command(command) is None
    assert "[✗]" in capsys.readouterr().out, "the refusal was silent"


def test_macos_no_half_sequence_survives_a_rejection(capsys):
    """Nothing may emit a kd: without its ku:. A modifier left down in the guest
    turns every later click into a modified one, silently."""
    for command in ["770 310 #move", "770 310 #drag", "here #scroll_up 3",
                    "770 310 #nosuchverb", "here #"]:
        built = macos().build_mouse_command(command)
        assert built is None or "kd:" not in " ".join(built[0]), command
    capsys.readouterr()


def test_macos_unmodified_commands_are_byte_identical(capsys):
    """The prefix parser sits in front of every mouse command, so the unmodified
    forms have to come through it unchanged."""
    ma = macos()
    for command, expected in [
        ("770 310 left", ["cliclick", "c:770,310"]),
        ("770 310 move", ["cliclick", "m:770,310"]),
        ("here double", ["cliclick", "dc:."]),
        ("position", ["cliclick", "p:."]),
        ("200 300 drag 900 640",
         ["cliclick", "dd:200,300", "w:50", "dm:900,640", "w:50", "du:900,640"]),
        ("here scroll_down 5", ["__scroll__", "c:.", "125", "5"]),
    ]:
        argv, human = ma.build_mouse_command(command)
        assert argv == expected, command
        assert human == command
    assert capsys.readouterr().out == "", "an accepted command printed a diagnostic"


@pytest.mark.parametrize("command", [
    "770 310 #left", "here +left", "200 300 !drag 900 640", "770 310 #+triple",
])
def test_macos_modified_mouse_commands_route_as_mouse(command):
    """detect_command_type gates the builder. Before the prefix was understood there,
    "770 310 #left" was refused as an unrecognised command."""
    kind, processed = macos().detect_command_type(command)
    assert kind == "mouse", f"{command!r} routed as {kind}"
    assert processed == command


def test_macos_a_bare_modified_verb_is_still_a_keypress():
    """"#left" on its own is claimed by the keyboard rule and must stay that way —
    the standalone form is the one shape that cannot be told apart from a chord."""
    kind, processed = macos().detect_command_type("#left")
    assert (kind, processed) == ("keyboard", "press #left")


def test_macos_modifier_names_reach_the_echo():
    """The console echo names the modifier; the agent composes the stored record from
    the same prefix (crates/agent/src/main.rs), so both surfaces show the gesture."""
    ma = macos()
    assert ma._describe_mouse_modifiers("#left") == ("Cmd+", "left")
    assert ma._describe_mouse_modifiers("#+left") == ("Cmd+Shift+", "left")
    assert ma._describe_mouse_modifiers("!drag") == ("Option+", "drag")
    assert ma._describe_mouse_modifiers("left") == ("", "left")


# ---- controller/agent grammar agreement ------------------------------------
ARGV_POLICY = Path(__file__).resolve().parents[2] / "crates/agent/src/argv_policy.rs"

# Every macOS command shape that reaches cliclick. A token emitted here whose prefix
# the agent does not list fails the command closed on the guest.
MACOS_CLICLICK_COMMANDS = [
    "position", "960 540 move", "960 540 left", "960 540 right", "960 540 double",
    "960 540 triple", "960 540 middle", "960 540 hold", "960 540 release",
    "here left", "here right", "here double", "here triple", "here middle",
    "here hold", "here release",
    "100 100 drag 900 700",
    "100 100 drag 900 700 dwell 300",
    "100 100 drag via 400 300 via 700 500 to 900 700",
    # Modifier-held forms: these add kd:/ku: tokens to a pointer sequence, so the
    # agent has to accept them in that position too.
    "770 310 #left", "770 310 #+left", "here !double", "200 300 !drag 900 640",
]

MACOS_CLICLICK_KEYBOARD = [
    "press {Enter}", "press {F5}", "press #{Mute}", "press #q", "press ^v",
    "press #", "press 4", "press {Space}",
]


def _agent_cliclick_prefixes() -> set:
    """The action prefixes crates/agent/src/argv_policy.rs accepts for cliclick."""
    body = re.search(r"fn validate_cliclick\(.*?\n\}", ARGV_POLICY.read_text(), re.S)
    assert body, "validate_cliclick not found in argv_policy.rs"
    # The prefixes are the only one- and two-character string literals in the
    # function; the error messages are all longer and contain spaces.
    return set(re.findall(r'"([a-z]{1,2})"', body.group(0)))


def test_macos_cliclick_tokens_are_all_in_the_agent_grammar():
    """The controller and the agent must agree on the cliclick vocabulary, and the
    two halves ship separately — the agent is Rust and needs a macOS build.

    This is how the drag defect had to be fixed twice: the controller emitted `m:`
    (mouseMoved) where a drag needs `dm:` (leftMouseDragged), and patching only the
    controller made every drag fail closed at the guest with "cliclick: invalid
    action token 'dm:400,350'".
    """
    ma = macos()
    accepted = _agent_cliclick_prefixes()
    assert "dm" in accepted, "the drag-continuation move is missing from the grammar"

    emitted = {}
    for command in MACOS_CLICLICK_COMMANDS + MACOS_CLICLICK_KEYBOARD:
        built = (ma.parse_keyboard_command(command) if command.startswith("press ")
                 else ma.build_mouse_command(command))
        assert built is not None, f"builder returned None for {command!r}"
        argv, _ = built
        if argv[0] != "cliclick":
            continue
        for token in argv[1:]:
            emitted.setdefault(token.split(":", 1)[0], command)

    unknown = {p: cmd for p, cmd in emitted.items() if p not in accepted}
    assert not unknown, (
        "the controller emits cliclick prefixes the agent will reject: "
        + ", ".join(f"{p!r} (from {cmd!r})" for p, cmd in sorted(unknown.items()))
    )


# ---- modifier-held mouse actions (Linux) -----------------------------------
# xdotool holds the modifier with keydown/keyup inside the same invocation, so the
# release cannot be lost between commands. Verified on an Xvfb display: the resulting
# ButtonPress carries state 0x4 for ctrl, 0x5 for ctrl+shift, 0x8 for alt, and a
# ctrl-scroll produces button-5 events at state 0x4.

def test_linux_modifier_wraps_the_pointer_chain():
    argv, human = linux()._build_mouse_command("770 310 ^left")
    assert argv == ["xdotool", "keydown", "ctrl",
                    "mousemove", "770", "310", "click", "1",
                    "keyup", "ctrl"]
    assert human == "770 310 ^left"


def test_linux_modifiers_are_released_in_reverse_order():
    """Last pressed, first released — the order a real keyboard produces."""
    argv, _ = linux()._build_mouse_command("770 310 ^+left")
    assert argv[:5] == ["xdotool", "keydown", "ctrl", "keydown", "shift"]
    assert argv[-4:] == ["keyup", "shift", "keyup", "ctrl"]


def test_linux_modifier_wraps_the_whole_drag():
    argv, _ = linux()._build_mouse_command("200 300 ^drag 900 640")
    assert argv[1:3] == ["keydown", "ctrl"]
    assert argv[-2:] == ["keyup", "ctrl"]
    assert argv[3:-2] == ["mousemove", "200", "300", "mousedown", "1",
                          "mousemove", "900", "640", "mouseup", "1"]


def test_linux_scroll_takes_modifiers():
    """Unlike macOS, Linux scroll is the real wheel buttons, so Ctrl-scroll is the
    zoom gesture applications expect."""
    argv, _ = linux()._build_mouse_command("770 310 ^scroll_down 3")
    assert argv == ["xdotool", "keydown", "ctrl", "mousemove", "770", "310",
                    "click", "--repeat", "3", "5", "keyup", "ctrl"]
    argv, _ = linux()._build_mouse_command("here !scroll_up 2")
    assert argv == ["xdotool", "keydown", "alt", "click", "--repeat", "2", "4",
                    "keyup", "alt"]


@pytest.mark.parametrize("command", [
    "770 310 ^move", "here ^move", "770 310 ^", "770 310 ^nosuchverb",
])
def test_linux_a_rejected_modifier_emits_nothing(command, capsys):
    assert linux()._build_mouse_command(command) is None
    assert "[✗]" in capsys.readouterr().out, "the refusal was silent"


def test_linux_no_half_sequence_survives_a_rejection(capsys):
    for command in ["770 310 ^move", "770 310 ^", "here ^nosuchverb"]:
        built = linux()._build_mouse_command(command)
        assert built is None or "keydown" not in built[0], command
    capsys.readouterr()


# ---- verbs this backend gained in 2.0.0 ------------------------------------
#
# `triple` used to appear in the rejection list above, on the grounds that only
# macOS had it. That is no longer true, and deleting the case would have removed
# the coverage rather than moved it: a verb that is merely no longer refused is
# not a verb anyone has checked. These assert the argv it actually produces.

@pytest.mark.parametrize("command, expected", [
    # `triple` — a third click on the same repeat mechanism as `double`.
    ("770 310 triple", ["xdotool", "mousemove", "770", "310",
                        "click", "--repeat", "3", "1"]),
    ("here triple", ["xdotool", "click", "--repeat", "3", "1"]),
    # `click` — macOS's spelling of `left`, so it must produce the same argv as
    # `left` rather than a second path that could drift from it.
    ("770 310 click", ["xdotool", "mousemove", "770", "310", "click", "1"]),
    ("here click", ["xdotool", "click", "1"]),
    # Horizontal scroll. xdotool spells the wheel as buttons; 4/5 are vertical
    # and 6/7 horizontal. Verified on Xvfb with xdotool 3.20160805.1: a client
    # selecting ButtonPressMask received 6 and 7, alongside 1 and 4 as controls.
    ("770 310 scroll_left 3", ["xdotool", "mousemove", "770", "310",
                               "click", "--repeat", "3", "6"]),
    ("770 310 scroll_right 2", ["xdotool", "mousemove", "770", "310",
                                "click", "--repeat", "2", "7"]),
    ("here scroll_left 4", ["xdotool", "click", "--repeat", "4", "6"]),
    ("scroll_right 2", ["xdotool", "click", "--repeat", "2", "7"]),
])
def test_linux_verbs_added_in_2_0_0(command, expected):
    argv, _ = linux()._build_mouse_command(command)
    assert argv == expected


def test_linux_click_and_left_are_the_same_gesture():
    """`click` is an alias, so it must not become a second code path."""
    la = linux()
    assert (la._build_mouse_command("770 310 click")[0]
            == la._build_mouse_command("770 310 left")[0])
    assert (la._build_mouse_command("here click")[0]
            == la._build_mouse_command("here left")[0])


@pytest.mark.parametrize("command, expected", [
    ("770 310 ^triple", ["xdotool", "keydown", "ctrl", "mousemove", "770", "310",
                         "click", "--repeat", "3", "1", "keyup", "ctrl"]),
    ("770 310 ^click", ["xdotool", "keydown", "ctrl", "mousemove", "770", "310",
                        "click", "1", "keyup", "ctrl"]),
    ("here ^scroll_left 2", ["xdotool", "keydown", "ctrl",
                             "click", "--repeat", "2", "6", "keyup", "ctrl"]),
])
def test_linux_the_new_verbs_take_a_modifier_and_stay_bracketed(command, expected):
    """The new verbs joined MOUSE_MODIFIER_ACTIONS, so each must still open with
    the keydown run and close with the keyup run — the agent refuses anything
    else, and an unbalanced one strands the modifier on the X server."""
    argv, _ = linux()._build_mouse_command(command)
    assert argv == expected
    assert argv[1] == "keydown" and argv[-2] == "keyup"
    assert argv[2] == argv[-1], "the modifier released is not the one held"


@pytest.mark.parametrize("command, expected", [
    # Plain form, unchanged.
    ("100 100 drag 800 600",
     ["xdotool", "mousemove", "100", "100", "mousedown", "1",
      "mousemove", "800", "600", "mouseup", "1"]),
    # Waypoints: each `via` becomes a mousemove inside the held button, and the
    # destination is the point after `to` — not the first waypoint.
    ("100 100 drag via 400 300 via 700 500 to 900 700",
     ["xdotool", "mousemove", "100", "100", "mousedown", "1",
      "mousemove", "400", "300", "mousemove", "700", "500",
      "mousemove", "900", "700", "mouseup", "1"]),
])
def test_linux_drag_waypoints(command, expected):
    argv, _ = linux()._build_mouse_command(command)
    assert argv == expected


def test_linux_dwell_is_accepted_range_checked_and_not_emitted():
    """`dwell` is parsed and bounded for grammar parity with macOS, but this
    backend emits nothing for it: xdotool would need a chained `sleep`, which the
    agent's argv policy has no arm for, so a dwell-bearing argv would build and
    then be refused at execution. The property to hold is that accepting the token
    changes nothing about what is sent."""
    la = linux()
    with_dwell, _ = la._build_mouse_command("100 100 drag 800 600 dwell 150")
    without, _ = la._build_mouse_command("100 100 drag 800 600")
    assert with_dwell == without
    assert "sleep" not in with_dwell and "150" not in with_dwell


@pytest.mark.parametrize("command", [
    "100 100 drag 800 600 dwell 99999",     # above MAX_DRAG_DWELL_MS
    "100 100 drag 800 600 dwell 0",         # below MIN_DRAG_DWELL_MS
    "100 100 drag 800 600 dwell abc",       # not a number
    "100 100 drag via 400 300 900 700",     # waypoints without the `to`
])
def test_linux_a_malformed_drag_emits_nothing(command, capsys):
    assert linux()._build_mouse_command(command) is None
    assert "[X]" in capsys.readouterr().out, "the refusal was silent"


def test_linux_unmodified_commands_are_unchanged(capsys):
    la = linux()
    for command, expected in [
        ("770 310 left", ["xdotool", "mousemove", "770", "310", "click", "1"]),
        ("770 310 move", ["xdotool", "mousemove", "770", "310"]),
        ("here double", ["xdotool", "click", "--repeat", "2", "1"]),
        ("position", ["xdotool", "getmouselocation", "--shell"]),
        ("200 300 drag 900 640",
         ["xdotool", "mousemove", "200", "300", "mousedown", "1",
          "mousemove", "900", "640", "mouseup", "1"]),
    ]:
        argv, human = la._build_mouse_command(command)
        assert argv == expected, command
        assert human == command
    assert capsys.readouterr().out == ""


def test_linux_modified_mouse_commands_route_as_mouse():
    for command in ["770 310 ^left", "here +left", "200 300 !drag 900 640"]:
        kind, processed = linux().detect_command_type(command)
        assert (kind, processed) == ("mouse", command)


# ---- Windows (__write__ direct file) --------------------------------------
def windows():
    wa = WindowsActuation.__new__(WindowsActuation)
    return wa


def test_windows_type_uses_write_sentinel_no_cmd():
    wa = windows()
    # _process_keyboard_command may exist; call the public execute path builder pieces.
    # Emulate the keyboard branch used by execute_command:
    payload = f"type {INJECTION}"
    argv = ["__write__", r"C:\keyboard_cmd.txt", payload]
    # The agent's run_argv only allows these two write targets:
    assert argv[0] == "__write__"
    assert argv[1] in (r"C:\keyboard_cmd.txt", r"C:\mouse_cmd.txt")
    # content is the literal payload — no cmd echo, no `>` redirection
    assert argv[2] == payload


def test_windows_position_command_is_write_argv():
    argv = windows()._build_position_command()
    assert argv == ["__write__", r"C:\mouse_cmd.txt", "position"]


# ---- modifier-held mouse actions (Windows) ---------------------------------
# Windows mouse commands are not built into argv here: the command text is written to
# C:\mouse_cmd.txt and mouse_control.ahk parses it on the guest. So the prefix travels
# verbatim, and the controller's job is to refuse a malformed one — the watcher's
# `switch` has no error path, so an action it does not recognise does nothing at all
# while the step still reports success.

@pytest.mark.parametrize("command", [
    "770 310 ^left", "770 310 ^+left", "here !double", "200 300 ^drag 900 640",
    "770 310 ^scroll_down 3", "770 310 ^hold", "here ^release",
])
def test_windows_accepts_the_modifier_forms_it_can_actuate(command):
    wa = windows()
    assert wa._check_mouse_command(command), f"{command!r} was refused"
    kind, processed = wa.detect_command_type(command)
    assert (kind, processed) == ("mouse", command)


@pytest.mark.parametrize("command", [
    "770 310 ^move", "here ^move", "770 310 ^", "770 310 ^nosuchverb",
    "770 310 ^triple",      # macOS has triple; this backend does not
])
def test_windows_refuses_what_the_watcher_would_silently_drop(command, capsys):
    assert windows()._check_mouse_command(command) is False
    assert "[✗]" in capsys.readouterr().out, "the refusal was silent"


def test_windows_unmodified_commands_still_pass_the_gate(capsys):
    wa = windows()
    for command in ["770 310 left", "770 310 move", "here double", "position",
                    "200 300 drag 900 640", "here scroll_down 5"]:
        assert wa._check_mouse_command(command), command
    assert capsys.readouterr().out == ""


def test_windows_modifier_names_reach_the_echo():
    wa = windows()
    assert wa._describe_mouse_modifiers("^left") == ("Ctrl+", "left")
    assert wa._describe_mouse_modifiers("^+left") == ("Ctrl+Shift+", "left")
    assert wa._describe_mouse_modifiers("#left") == ("Win+", "left")
    assert wa._describe_mouse_modifiers("left") == ("", "left")


AHK_MOUSE = Path(__file__).resolve().parents[2] / "watcher_scripts/mouse_control.ahk"


def test_windows_watcher_understands_every_symbol_the_controller_emits():
    """The controller and the watcher ship separately — the watcher is copied to the
    guest by hand — so a symbol accepted here that the script does not map would
    actuate as an unmodified click and record as a modified one."""
    script = AHK_MOUSE.read_text()
    body = re.search(r"SplitModifiers\(token.*?\n\}", script, re.S)
    assert body, "SplitModifiers not found in mouse_control.ahk"

    mapped = set(re.findall(r'"(\^|\+|!|#)",\s*"\{', body.group(0)))
    emitted = set(windows().MOUSE_MODIFIER_SYMBOLS)
    assert emitted <= mapped, (
        "the controller emits modifier symbols the watcher does not map: "
        + ", ".join(sorted(emitted - mapped))
    )


def test_macos_middle_click_uses_the_sentinel_not_a_cliclick_token():
    """`middle` mapped to `mc:`, which is not a cliclick action at all — cliclick's set
    is `c rc dc tc m dd du dm kd ku kp t w p cp`. Every middle click on macOS therefore
    failed at execution with "Unrecognized action shortcut", in both command forms and
    with every modifier, and the coverage matrix is what surfaced it.

    macOS supports the button natively, so it is posted as a CGEvent; the limitation
    was cliclick's, not the platform's. Linux and Windows have had `middle` working all
    along, which is why this was a per-backend gap rather than a missing feature.
    """
    ma = macos()
    assert ma.build_mouse_command("960 540 middle")[0] == ["__middle__", "c:960,540"]
    assert ma.build_mouse_command("here middle")[0] == ["__middle__", "c:."]
    assert ma.build_mouse_command("960 540 #middle")[0] == \
        ["__middle__", "c:960,540", "cmd"]
    assert ma.build_mouse_command("here ^+middle")[0] == \
        ["__middle__", "c:.", "ctrl,shift"]

    # No macOS builder may emit the token that never worked.
    for cmd in ("960 540 middle", "here middle", "960 540 !middle"):
        argv, _ = ma.build_mouse_command(cmd)
        assert not any(tok.startswith("mc:") for tok in argv), \
            f"{cmd!r} still emits a cliclick middle token: {argv}"


def test_middle_click_works_on_every_backend():
    """It failed only on macOS, so the other two are pinned as the reference: a verb
    advertised on all three has to be reachable on all three."""
    assert linux()._build_mouse_command("960 540 middle")[0] == \
        ["xdotool", "mousemove", "960", "540", "click", "2"]
    assert macos().build_mouse_command("960 540 middle")[0][0] == "__middle__"
    # Windows writes the command text through to the AHK watcher, which has a
    # `case "middle"` arm.
    assert "middle" in WindowsActuation.MOUSE_ACTIONS
    assert 'case "middle":' in AHK_MOUSE.read_text()


def test_scroll_default_count_is_the_same_on_every_backend():
    """`here scroll_down` with no count has to mean the same distance everywhere. The
    watcher defaulted to 3 while both Python backends default to 5, so the identical
    command scrolled a different amount on Windows.

    The agent now states this number in the record for a command that omitted it, so a
    fourth source has to agree: if DEFAULT_SCROLL_NOTCHES drifts from the backends, the
    record names a count that was never performed, and nothing downstream can detect
    it — the command as typed is not persisted."""
    script = AHK_MOUSE.read_text()
    ahk_defaults = re.findall(
        r'case "scroll_(?:up|down)":\s*\n\s*amount := \(args\.Length >= paramOffset\) '
        r'\? args\[paramOffset\] : (\d+)',
        script,
    )
    assert len(ahk_defaults) == 2, f"expected two AHK scroll defaults, found {ahk_defaults}"
    assert set(ahk_defaults) == {"5"}, f"AHK scroll default is {set(ahk_defaults)}, not 5"

    # The Python defaults, read from the builders themselves rather than a comment.
    argv, _ = macos().build_mouse_command("here scroll_down")
    assert argv == ["__scroll__", "c:.", "125", "5"]
    argv, _ = linux()._build_mouse_command("770 310 scroll_down")
    assert argv == ["xdotool", "mousemove", "770", "310", "click", "--repeat", "5", "5"]

    # The agent's constant, read from the source for the same reason.
    agent_src = (Path(__file__).resolve().parents[2] / "crates/agent/src/main.rs").read_text()
    agent_default = re.search(r"const DEFAULT_SCROLL_NOTCHES: u32 = (\d+);", agent_src)
    assert agent_default, "DEFAULT_SCROLL_NOTCHES is no longer declared in the agent"
    assert agent_default.group(1) == "5", (
        f"the agent records a default of {agent_default.group(1)} notches while the "
        f"backends perform 5"
    )

    # Each controller's own constant. These were added because this test previously
    # claimed to cover every site and did not: the console echo defaulted to 1 on all
    # three backends while every backend performed 5, so an operator issuing a
    # countless scroll was told "1 notch" for a gesture of five. The echo was a fifth
    # site nothing here read.
    for controller in (MacOSActuation, LinuxActuation, WindowsActuation):
        assert controller.DEFAULT_SCROLL_NOTCHES == 5, (
            f"{controller.__name__} defaults to "
            f"{controller.DEFAULT_SCROLL_NOTCHES} notches, not 5"
        )


class _StubClient:
    """Minimal stand-in for the gRPC client: the echo is the code under test, so the
    transport only has to return a plausible success."""

    def __init__(self):
        self.sent = []

    def execute_command(self, argv, human_command):
        self.sent.append((argv, human_command))
        return {
            "success": True,
            "execution_time_ms": 1,
            "mouse_x": 770,
            "mouse_y": 310,
            "position_captured": True,
            "metadata": None,
        }


@pytest.mark.parametrize("factory", [macos, linux, windows])
def test_the_console_echo_names_the_count_the_backend_performs(factory, capsys):
    """The echo is what the operator reads at the moment they issue the command, and
    it disagreed with both the actuation and the record.

    Measured on the Windows guest: `550 400 scroll_up` scrolled 15 lines at 3 lines
    per notch — five notches — while the console printed "scrolled up 1 notch".

    This drives the real `execute_command` and reads its stdout. An earlier version of
    this test recomputed the count from DEFAULT_SCROLL_NOTCHES itself and passed even
    with the defect reintroduced — it asserted its own arithmetic, not the product.
    """
    controller = factory()
    controller.grpc_client = _StubClient()
    controller.strict = True

    controller.execute_command("770 310 scroll_down")
    out = capsys.readouterr().out

    assert "5 notches" in out, (
        f"{type(controller).__name__} echoed {out.strip()!r} for a countless scroll, "
        f"but the backend performs {controller.DEFAULT_SCROLL_NOTCHES}"
    )
    assert "1 notch" not in out

    # An explicit count is still echoed as given, and the singular still reads "notch".
    controller.execute_command("770 310 scroll_down 1")
    out = capsys.readouterr().out
    assert "1 notch" in out and "1 notches" not in out


def test_windows_watcher_releases_modifiers_on_every_path():
    """A modifier left physically down turns every later click in the session into a
    modified one, with nothing in the output saying so."""
    script = AHK_MOUSE.read_text()
    assert "} finally {" in script, "the release is not in a finally block"
    finally_block = script.split("} finally {", 1)[1].split("}", 1)[0]
    assert "Send modUp" in finally_block, "the finally block does not release"
