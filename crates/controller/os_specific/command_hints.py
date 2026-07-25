"""Console presentation for commands: repair hints, and redaction of typed text.

Shared by all three platform modules. In strict mode an unrecognised command is
rejected instead of being typed into the guest, so the console message is the only
thing the operator has to work from — it has to name the likely typo, not just say
"invalid".

The failure this exists for: session S019 recorded `type 1022 343left`. The operator
meant `1022 343 left` and dropped a space. It typed harmlessly behind a modal, was
recorded as a real step, and had to be removed post-hoc with the whole trace
renumbered.
"""
import re
from typing import List, Optional

# "343left" -> ("343", "left"). A digit run glued to an alphabetic action token.
_GLUED_COORD = re.compile(r'^(\d+)([A-Za-z_]+)$')

# The only verb whose argument is free text, and therefore the only one to redact.
_TYPE_VERB = 'type'

# Seconds a button may stay down before the console mentions it. A hold is
# legitimately long during a slow drag, so this warns and never releases —
# a timer that released would cut a valid drag short.
HOLD_WARN_SECONDS = 5

# "left@900,700:7" -> button, position, seconds held. The position is "?" when the
# command that pressed the button named no coordinate ("here hold").
_HELD_ENTRY = re.compile(r'^([a-z]+)@(-?\d+,-?\d+|\?):(\d+)$')


def held_button_warnings(metadata: Optional[dict]) -> List[str]:
    """Console lines for buttons the agent reports as still down.

    `X Y hold` issues a button-down with no timeout. Left unmatched, the button
    stays physically down and every later click or move is read as a drag-select,
    while the console reports plain success throughout. The agent tracks the state;
    this surfaces it once it has been outstanding long enough to be a mistake rather
    than a deliberate drag.
    """
    if not metadata:
        return []
    raw = metadata.get('held_buttons', '')
    if not raw:
        return []

    lines = []
    for entry in raw.split(';'):
        match = _HELD_ENTRY.match(entry.strip())
        if not match:
            continue
        button, position, seconds = match.group(1), match.group(2), int(match.group(3))
        if seconds < HOLD_WARN_SECONDS:
            continue
        if position == '?':
            lines.append(
                f"[!] {button} button still held for {seconds}s — issue `here release`"
            )
        else:
            x, y = position.split(',')
            lines.append(
                f"[!] {button} button still held at ({x}, {y}) for {seconds}s — "
                f"issue `{x} {y} release`"
            )
    return lines


def print_held_button_warnings(metadata: Optional[dict]) -> None:
    for line in held_button_warnings(metadata):
        print(line)


def suggest(command: str, mouse_actions: set, keyboard_actions: set) -> Optional[str]:
    """Return a corrected command line, or None when nothing plausible is found."""
    tokens = command.strip().split()
    if not tokens:
        return None

    # Missing space between a coordinate and the action: "1022 343left".
    for i, token in enumerate(tokens):
        match = _GLUED_COORD.match(token)
        if match and match.group(2) in mouse_actions:
            repaired: List[str] = tokens[:i] + [match.group(1), match.group(2)] + tokens[i + 1:]
            return ' '.join(repaired)

    # Missing space after a keyboard verb: "typehello", "presstab".
    for verb in sorted(keyboard_actions, key=len, reverse=True):
        if tokens[0].startswith(verb) and len(tokens[0]) > len(verb):
            return ' '.join([verb, tokens[0][len(verb):]] + tokens[1:])

    return None


def redact(command: str) -> str:
    """Display form of a command, with typed text replaced by a character count.

    A `type` payload may carry a credential. The interactive result line already
    prints "Typed: <n> chars" rather than the content; every other place a command
    is echoed has to hold the same line, or the property only holds in one mode.
    Key names in a `press` command are not secret and stay visible.

    The prefix is matched without requiring a separator, so a command rejected for a
    missing space ("typehunter2") is redacted too — that form reaches the rejection
    message, which the well-formed one never does.

    Limit: a command whose *verb* is misspelled ("tpye secret") cannot be recognised
    and is echoed as given. It is already on the operator's screen and in readline
    history, so this adds no exposure beyond what typing it did.
    """
    stripped = command.strip()
    if stripped.startswith(_TYPE_VERB):
        rest = stripped[len(_TYPE_VERB):].lstrip()
        if rest:
            return f"{_TYPE_VERB} <{len(rest)} chars>"
    return command


def rejection_message(command: str, mouse_actions: set, keyboard_actions: set) -> str:
    """Build the multi-line console message for a rejected command.

    The command is echoed once, redacted. A rejected command is by definition not a
    recognised `type`, but it can be a *mistyped* one ("tpye mysecret"), so the raw
    text is not repeated — the operator can see what they typed on the line above.
    """
    lines = [f"[✗] Unrecognised command: {redact(command)!r}"]
    hint = suggest(command, mouse_actions, keyboard_actions)
    if hint:
        lines.append(f"    Did you mean: {redact(hint)}")
    lines.append("    Text must be sent explicitly with: type <text>")
    lines.append("    Pass --lenient to restore the old behaviour of typing unknown input.")
    return '\n'.join(lines)
