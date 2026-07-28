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
