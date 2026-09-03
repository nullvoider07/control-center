"""Linux-specific actuation with position tracking - Enhanced Implementation"""

import re
import os
from typing import Tuple, Optional, List

from . import command_hints

# LinuxActuation class definition
class LinuxActuation:
    """Linux actuation controller with position tracking for all mouse actions"""
    
    # Mouse action keywords
    MOUSE_ACTIONS = {
        'move', 'left', 'click', 'right', 'middle', 'double', 'triple',
        'scroll_up', 'scroll_down', 'scroll_left', 'scroll_right',
        'drag', 'here', 'hold', 'release', 'position'
    }
    
    # Keyboard action keywords
    # `key` is macOS's second spelling of `press`; it is an alias there
    # (`elif action in ['press', 'key']`), and an alias here.
    KEYBOARD_ACTIONS = {'type', 'press', 'key'}
    
    # Special keys mapping (AutoHotkey syntax → xdotool syntax)
    SPECIAL_KEYS_MAP = {
        '{Enter}': 'Return',
        '{Esc}': 'Escape',
        '{Tab}': 'Tab',
        '{Backspace}': 'BackSpace',
        '{BS}': 'BackSpace',
        '{Delete}': 'Delete',
        '{Del}': 'Delete',
        '{Space}': 'space',
        '{Up}': 'Up',
        '{Down}': 'Down',
        '{Left}': 'Left',
        '{Right}': 'Right',
        '{Home}': 'Home',
        '{End}': 'End',
        '{PgUp}': 'Prior',
        '{PgDn}': 'Next',
        '{F1}': 'F1', '{F2}': 'F2', '{F3}': 'F3', '{F4}': 'F4',
        '{F5}': 'F5', '{F6}': 'F6', '{F7}': 'F7', '{F8}': 'F8',
        '{F9}': 'F9', '{F10}': 'F10', '{F11}': 'F11', '{F12}': 'F12',
        '{LWin}': 'Super_L',
        '{RWin}': 'Super_R',
        '{LCtrl}': 'Control_L',
        '{RCtrl}': 'Control_R',
        '{LAlt}': 'Alt_L',
        '{RAlt}': 'Alt_R',
        '{LShift}': 'Shift_L',
        '{RShift}': 'Shift_R',
        '{Insert}': 'Insert',
        '{CapsLock}': 'Caps_Lock',
        '{NumLock}': 'Num_Lock',
        '{ScrollLock}': 'Scroll_Lock',
        '{PrintScreen}': 'Print',
        '{Pause}': 'Pause',
        # Explicit name for the plus key. A bare '+' in a press command is always
        # read as the Shift modifier, so "^+" is Ctrl+Shift and Ctrl+Plus must be
        # written "^{Plus}".
        '{Plus}': 'plus',
    }

    # Punctuation → X keysym name.
    #
    # xdotool resolves key specs through keysym names, not characters: "ctrl+minus"
    # works, "ctrl+-" does not. The two failure modes are both bad — '-', '[', '.'
    # abort the whole sequence with "Invalid key sequence", while ',', '/', '=', ';'
    # and friends exit 0 after logging "No such key name" and silently drop the
    # target, delivering a bare modifier press that is reported as a success.
    # Mapping the target here is what makes Ctrl+Minus, Ctrl+Comma, Ctrl+Slash and
    # the bracket/indent shortcuts work at all.
    #
    # Shifted characters are included: xdotool applies Shift itself when the keysym
    # is only reachable that way (e.g. "ctrl+plus" arrives as Ctrl+Shift+plus).
    PUNCT_KEYSYM_MAP = {
        '-': 'minus', '=': 'equal', ',': 'comma', '.': 'period',
        '/': 'slash', ';': 'semicolon', "'": 'apostrophe',
        '[': 'bracketleft', ']': 'bracketright', '\\': 'backslash',
        '`': 'grave', ' ': 'space',
        '_': 'underscore', '+': 'plus', '?': 'question', ':': 'colon',
        '"': 'quotedbl', '~': 'asciitilde', '|': 'bar',
        '{': 'braceleft', '}': 'braceright', '<': 'less', '>': 'greater',
        '!': 'exclam', '@': 'at', '#': 'numbersign', '$': 'dollar',
        '%': 'percent', '^': 'asciicircum', '&': 'ampersand',
        '*': 'asterisk', '(': 'parenleft', ')': 'parenright',
    }

    # Keyboard indicators (for detection)
    KEYBOARD_INDICATORS = set(SPECIAL_KEYS_MAP.keys())

    # Modifier symbol → display name, shared by the `press` echo and the mouse echo so
    # a Ctrl-click and a Ctrl-chord name the key identically.
    MODIFIER_DISPLAY = {'^': 'Ctrl', '+': 'Shift', '!': 'Alt', '#': 'Super'}

    # Modifier symbols accepted as a prefix on a MOUSE action, and the xdotool keysym
    # each resolves to. The agent restricts `keydown`/`keyup` in a pointer chain to
    # exactly these four (argv_policy::POINTER_MODIFIERS), so the two halves cannot
    # drift into a form the guest refuses.
    MOUSE_MODIFIER_SYMBOLS = {'^': 'ctrl', '+': 'shift', '!': 'alt', '#': 'super'}

    # Notches a scroll performs when the command names no count. One constant per
    # backend, used by both the argv builder and the console echo, because those two
    # disagreed: the backends scrolled 5 while the echo told the operator "1 notch".
    # test_scroll_default_count_is_the_same_on_every_backend reads this and its
    # counterparts, and fails if any of them drifts.
    DEFAULT_SCROLL_NOTCHES = 5

    # Drag bounds, matching the macOS backend so one grammar means one thing on
    # every platform. `dwell` is accepted and range-checked but not emitted here:
    # xdotool would need a chained `sleep`, which the agent's argv policy has no
    # arm for, so a dwell-bearing command would build and then be refused at
    # runtime. It would also buy nothing - a dwell-less drag was measured to
    # select exactly the same text as a dwelled one on this backend.
    DEFAULT_DRAG_DWELL_MS = 50
    MIN_DRAG_DWELL_MS = 1
    MAX_DRAG_DWELL_MS = 5000
    MAX_DRAG_WAYPOINTS = 16

    # xdotool spells scroll as a button click. 4/5 are vertical, 6/7 horizontal.
    SCROLL_BUTTONS = {
        'scroll_up': '4', 'scroll_down': '5',
        'scroll_left': '6', 'scroll_right': '7',
    }


    # Pointer verbs a modifier may be held across — the verbs this backend already
    # has, and no others. `move` and `position` are excluded because no held modifier
    # changes what they do, and accepting the prefix while ignoring it would record a
    # gesture that was never performed.
    MOUSE_MODIFIER_ACTIONS = frozenset({
        'left', 'click', 'right', 'middle', 'double', 'triple', 'drag',
        'hold', 'release',
        'scroll_up', 'scroll_down', 'scroll_left', 'scroll_right',
    })
    
    # Executable that receives the argv this builder produces.
    #
    # X11 is driven by xdotool. Wayland compositors refuse to let one client
    # synthesise input into another, so xdotool reaches XWayland clients only and
    # cannot touch a native-Wayland window at all; there the argv goes to a helper
    # that speaks the same sub-command language over the RemoteDesktop portal
    # (see wayland_portal.py). The token stream is identical either way - only
    # argv[0] differs - so the grammar, the console echo and the recorded
    # human_command are unchanged by which backend is in use.
    X11_BACKEND = 'xdotool'
    WAYLAND_BACKEND = 'cc-wayland-actuate'

    # Class-level default so instances built without __init__ (tests, replay
    # helpers) still have a defined policy rather than raising on attribute access.
    strict = True
    backend = X11_BACKEND

    def __init__(self, grpc_client, strict: bool = True):
        """Initialize controller with gRPC client.

        strict: reject commands that match no known verb instead of typing them.
        """
        self.grpc_client = grpc_client
        self.display = os.environ.get('DISPLAY', ':0')
        self.strict = strict
        self.backend = self.detect_backend()

    @classmethod
    def detect_backend(cls) -> str:
        """Pick the executable for this session's display server.

        CC_LINUX_BACKEND overrides, taking 'xdotool' or 'wayland'; an unknown
        value is an error rather than a silent fallback, because falling back to
        xdotool on Wayland produces the failure this detection exists to avoid -
        commands that exit 0 and actuate nothing outside XWayland.

        DISPLAY is not a usable signal on its own: a Wayland session almost
        always runs XWayland and sets it too, so a DISPLAY test would select
        xdotool on exactly the sessions that need the portal.
        """
        override = os.environ.get('CC_LINUX_BACKEND', '').strip().lower()
        if override:
            if override in ('xdotool', 'x11'):
                return cls.X11_BACKEND
            if override == 'wayland':
                return os.environ.get('CC_WAYLAND_HELPER') or cls.WAYLAND_BACKEND
            raise ValueError(
                f"CC_LINUX_BACKEND={override!r} is not one of 'xdotool', 'wayland'")

        session_type = os.environ.get('XDG_SESSION_TYPE', '').strip().lower()
        if session_type == 'wayland' or os.environ.get('WAYLAND_DISPLAY'):
            return os.environ.get('CC_WAYLAND_HELPER') or cls.WAYLAND_BACKEND
        return cls.X11_BACKEND

    # Helper method to format key combinations for display in CLI output
    def _format_press_for_display(self, keys: str) -> str:
        """Convert modifier-prefixed key notation to human-readable string for CLI output.
        
        The modifier symbols (^, +, !, #) and brace-enclosed key names are the
        same input syntax accepted by the controller for press commands. This
        method is only used for display; it does not affect actuation.
        
        Examples:
            "^c"        -> "Ctrl+C"
            "+t"        -> "Shift+T"
            "!{Tab}"    -> "Alt+Tab"
            "#r"        -> "Super+R"
            "{F5}"      -> "F5"
            "{Enter}"   -> "Enter"
        """
        modifier_display = self.MODIFIER_DISPLAY
        special_display = {
            '{LCtrl}': 'Ctrl', '{RCtrl}': 'Ctrl',
            '{LShift}': 'Shift', '{RShift}': 'Shift',
            '{LAlt}': 'Alt', '{RAlt}': 'Alt',
            '{LWin}': 'Super', '{RWin}': 'Super',
            '{Enter}': 'Enter', '{Esc}': 'Esc', '{Tab}': 'Tab',
            '{Backspace}': 'Backspace', '{BS}': 'Backspace',
            '{Delete}': 'Delete', '{Del}': 'Delete',
            '{Space}': 'Space', '{Up}': 'Up', '{Down}': 'Down',
            '{Left}': 'Left', '{Right}': 'Right',
            '{Home}': 'Home', '{End}': 'End',
            '{PgUp}': 'Page Up', '{PgDn}': 'Page Down',
            '{F1}': 'F1', '{F2}': 'F2', '{F3}': 'F3', '{F4}': 'F4',
            '{F5}': 'F5', '{F6}': 'F6', '{F7}': 'F7', '{F8}': 'F8',
            '{F9}': 'F9', '{F10}': 'F10', '{F11}': 'F11', '{F12}': 'F12',
            '{Plus}': 'Plus',
        }
        if keys in special_display:
            return special_display[keys]
        parts = []
        i = 0
        while i < len(keys) and keys[i] in modifier_display:
            parts.append(modifier_display[keys[i]])
            i += 1
        key_part = keys[i:]
        if key_part:
            if key_part in special_display:
                parts.append(special_display[key_part])
            elif key_part.startswith('{') and key_part.endswith('}'):
                parts.append(key_part[1:-1])
            elif len(key_part) == 1:
                parts.append(key_part.upper())
            else:
                parts.append(key_part)
        return '+'.join(parts) if parts else keys

    # Helper method to split a modifier prefix off a mouse action token
    def _split_mouse_modifiers(self, token: str) -> Tuple[List[str], str]:
        """Split "<mods><action>" into (xdotool keysyms, action).

        Returns ([], token) when there is no prefix. Raises ValueError with an
        operator-readable message when a prefix is present but the result cannot be
        actuated — never degrading to the unmodified action, which would succeed, echo
        plausibly and record a gesture other than the one performed.
        """
        mods: List[str] = []
        i = 0
        while i < len(token) and token[i] in self.MOUSE_MODIFIER_SYMBOLS:
            mod = self.MOUSE_MODIFIER_SYMBOLS[token[i]]
            if mod not in mods:
                mods.append(mod)
            i += 1

        action = token[i:]
        if not mods:
            return [], action

        if not action:
            raise ValueError(f"{token!r} is a modifier prefix with no mouse action after it")

        if action not in self.MOUSE_MODIFIER_ACTIONS:
            if action in self.MOUSE_ACTIONS:
                raise ValueError(
                    f"{action!r} does not take a modifier prefix; modifiers apply to "
                    f"{', '.join(sorted(self.MOUSE_MODIFIER_ACTIONS))}"
                )
            raise ValueError(
                f"unknown mouse action {action!r} after the modifier prefix {token[:i]!r}"
            )

        return mods, action

    # Helper method to parse the tokens following "drag"
    def _parse_drag(self, rest: List[str]) -> Tuple[List[Tuple[int, int]], Tuple[int, int], int]:
        """Parse the tokens after "drag" into (waypoints, destination, dwell_ms).

        Accepted forms, matching the macOS backend:
            <x2> <y2> [dwell <ms>]
            via <ax> <ay> [via ...] to <x2> <y2> [dwell <ms>]

        One deliberate difference from macOS: in the plain form, tokens after the
        destination are ignored rather than refused. This backend has always read
        the destination from fixed positions and ignored the rest, so refusing
        them now would reject commands that work today. The waypoint form is
        strict, because there "to" is what separates a waypoint from the
        destination and a stray token is genuinely ambiguous.

        The destination is returned rather than re-derived by the caller. The
        console echo used to read it from the two tokens after "drag", which is
        the destination in the plain form and the literal "via" in the waypoint
        form - the same defect the macOS backend records having fixed.
        """
        tokens = list(rest)

        dwell = self.DEFAULT_DRAG_DWELL_MS
        if len(tokens) >= 2 and tokens[-2] == 'dwell':
            try:
                dwell = int(tokens[-1])
            except ValueError:
                raise ValueError(f"drag dwell must be a number, got {tokens[-1]!r}")
            if not self.MIN_DRAG_DWELL_MS <= dwell <= self.MAX_DRAG_DWELL_MS:
                raise ValueError(
                    f"drag dwell {dwell} out of range "
                    f"{self.MIN_DRAG_DWELL_MS}-{self.MAX_DRAG_DWELL_MS} ms"
                )
            tokens = tokens[:-2]

        def point(pair: List[str]) -> Tuple[int, int]:
            try:
                return (int(pair[0]), int(pair[1]))
            except (ValueError, IndexError):
                raise ValueError(f"drag needs a pair of integer coordinates, got {pair!r}")

        waypoints: List[Tuple[int, int]] = []
        if tokens and tokens[0] == 'via':
            while tokens and tokens[0] == 'via':
                waypoints.append(point(tokens[1:3]))
                tokens = tokens[3:]
            if len(waypoints) > self.MAX_DRAG_WAYPOINTS:
                raise ValueError(
                    f"drag accepts at most {self.MAX_DRAG_WAYPOINTS} waypoints, "
                    f"got {len(waypoints)}"
                )
            if not tokens or tokens[0] != 'to':
                raise ValueError("drag with 'via' waypoints must end with 'to <x> <y>'")
            tokens = tokens[1:]
            destination = point(tokens[0:2])
            if len(tokens) > 2:
                raise ValueError(f"unexpected tokens after drag destination: {tokens[2:]}")
        else:
            destination = point(tokens[0:2])

        return waypoints, destination, dwell

    # Helper method to decide whether a token names a mouse action
    def _is_mouse_action_token(self, token: str) -> bool:
        """Whether a token names a mouse action, with or without a modifier prefix.

        Routing only. A malformed prefix is admitted so the builder can refuse it by
        name rather than the router rejecting it as an unrecognised command.
        """
        if token in self.MOUSE_ACTIONS:
            return True
        stripped = token.lstrip(''.join(self.MOUSE_MODIFIER_SYMBOLS))
        return bool(stripped) and stripped != token

    # Helper method to name the modifiers held during a mouse action, for the echo
    def _describe_mouse_modifiers(self, token: str) -> Tuple[str, str]:
        """(display prefix, bare action) for the console echo, e.g. ("Ctrl+", "left").

        Never raises: the command has already actuated by the time the echo runs.
        """
        parts: List[str] = []
        i = 0
        while i < len(token) and token[i] in self.MOUSE_MODIFIER_SYMBOLS:
            name = self.MODIFIER_DISPLAY[token[i]]
            if name not in parts:
                parts.append(name)
            i += 1
        return (''.join(f"{p}+" for p in parts), token[i:])

    # Translate AutoHotkey modifier syntax to xdotool syntax
    def _translate_modifier_keys(self, text: str) -> str:
        """
        Translate AutoHotkey modifier syntax to xdotool syntax
        
        Handles:
        - Modifier prefixes: ^ (Ctrl), + (Shift), ! (Alt), # (Super)
        - Standalone modifiers: just ^ or # alone
        - Special keys in braces: {Enter}, {Tab}, etc.
        - Punctuation targets, via PUNCT_KEYSYM_MAP

        Examples:
            "#r" → "super+r"
            "#" → "super"  (standalone Super key)
            "^c" → "ctrl+c"
            "^" → "ctrl"  (standalone Ctrl key)
            "^-" → "ctrl+minus"
            "^," → "ctrl+comma"
            "^{Plus}" → "ctrl+plus"
        """
        modifiers = []
        i = 0
        
        # Extract modifier symbols from the beginning
        while i < len(text) and text[i] in '^+!#':
            if text[i] == '^':
                modifiers.append('ctrl')
            elif text[i] == '+':
                modifiers.append('shift')
            elif text[i] == '!':
                modifiers.append('alt')
            elif text[i] == '#':
                modifiers.append('super')
            i += 1
        
        # Get the key part (after modifiers)
        key_part = text[i:]
        
        # Translate special keys in braces
        for ahk_key, xdo_key in self.SPECIAL_KEYS_MAP.items():
            key_part = key_part.replace(ahk_key, xdo_key)

        # Translate a bare punctuation target to its keysym name. Only a single
        # character is remapped: brace translation above already yields keysym names,
        # and a multi-character remainder is either a keysym name or malformed input
        # that xdotool should reject rather than have guessed at.
        if len(key_part) == 1 and key_part in self.PUNCT_KEYSYM_MAP:
            key_part = self.PUNCT_KEYSYM_MAP[key_part]

        # Build the xdotool key combination
        if modifiers and key_part:
            # Modifiers + key: e.g., "ctrl+c", "super+r"
            return '+'.join(modifiers + [key_part])
        elif key_part:
            # Just the key, no modifiers
            return key_part
        elif modifiers:
            # STANDALONE MODIFIER: Just the modifier key(s)
            # e.g., "#" becomes "super"
            # This is the fix for standalone Super/Win key!
            return '+'.join(modifiers)
        else:
            # Empty string fallback
            return text
    
    # Smart command type detection
    def detect_command_type(self, command: str) -> Tuple[str, str]:
        """
        Smart detection of command type (mouse/keyboard)
        Returns: (type, command)
        """
        tokens = command.strip().split()
        
        if not tokens:
            return 'invalid', command
        
        # Check for "here" or "position" keyword
        if tokens[0] in ['here', 'position']:
            if tokens[0] == 'position':
                return 'mouse', 'position'
            if len(tokens) >= 2 and self._is_mouse_action_token(tokens[1]):
                return 'mouse', command
            return 'invalid', command

        # Check if starts with coordinates (numbers)
        if len(tokens) >= 2:
            try:
                int(tokens[0])
                int(tokens[1])
                if len(tokens) >= 3 and self._is_mouse_action_token(tokens[2]):
                    return 'mouse', command
                elif len(tokens) == 2:
                    return 'mouse', f"{command} move"
            except ValueError:
                pass
        
        # Check for explicit keyboard actions
        if tokens[0] in self.KEYBOARD_ACTIONS:
            return 'keyboard', command
        
        # Check for modifier keys or special indicators
        modifier_pattern = r'^[\^+!#]'
        if re.match(modifier_pattern, command) or any(indicator in command for indicator in self.KEYBOARD_INDICATORS):
            if not command.startswith('press '):
                return 'keyboard', f"press {command}"
            return 'keyboard', command
        
        # Check if first token is a standalone mouse action
        if tokens[0] in self.MOUSE_ACTIONS:
            return 'mouse', command
        
        # Unrecognised. In strict mode this is refused; the legacy behaviour typed it
        # into whatever had focus and recorded it as a real step (see command_hints).
        if self.strict:
            return 'invalid', command
        return 'keyboard', f"type {command}"
    
    # Build xdotool command for mouse actions
    def _build_mouse_command(self, command: str) -> Optional[Tuple[List[str], str]]:
        """Build an (argv, human_command) pair for an xdotool mouse action.

        argv is executed directly by the agent (no shell); DISPLAY is set by the
        agent. Mouse commands carry no free-text, so every token is a safe literal.
        """
        parts = command.strip().split()

        def out(tokens: List[str], mods: Optional[List[str]] = None) -> Tuple[List[str], str]:
            """Wrap a pointer chain in keydown/keyup for any held modifiers.

            One xdotool invocation, so the release cannot be lost between commands, and
            one keydown per modifier so every token stays a single keysym the agent's
            grammar accepts. Released in reverse order: last pressed, first released.
            Every failure path returns None before reaching here, so no argv this
            builder produces carries a keydown without its keyup.

            That last sentence is about this function, not about the system: a caller
            holding the execute scope reaches the agent without passing through here.
            `xdotool keydown ctrl` sets the modifier on the X server, where it outlives
            the process and reinterprets every later command. The invariant is enforced
            in `check_pointer_modifier_bracket` (agent, argv_policy.rs); this is where
            it is intended, not where it holds.
            """
            if mods:
                down = [t for mod in mods for t in ('keydown', mod)]
                up = [t for mod in reversed(mods) for t in ('keyup', mod)]
                tokens = down + tokens + up
            return ([self.backend] + tokens, command)

        # POSITION COMMAND - Returns current coordinates
        if parts[0] == 'position':
            return out(['getmouselocation', '--shell'])

        # Handle 'here' commands
        if parts[0] == 'here':
            if len(parts) < 2:
                return None

            try:
                mods, action = self._split_mouse_modifiers(parts[1])
            except ValueError as e:
                print(f"[✗] {e}")
                return None

            # Scrolling requires an explicit count from the user. That rule is
            # this backend's own and applies to the horizontal directions for the
            # same reason it applies to the vertical ones.
            if action in self.SCROLL_BUTTONS:
                if len(parts) < 3:
                    print("[!] Error: You must specify a scroll count (e.g., 'here scroll_down 5')")
                    return None
                count = parts[2]
                return out(['click', '--repeat', count, self.SCROLL_BUTTONS[action]], mods)

            here_map = {
                'left':    ['click', '1'],
                'click':   ['click', '1'],
                'right':   ['click', '3'],
                'middle':  ['click', '2'],
                'double':  ['click', '--repeat', '2', '1'],
                'triple':  ['click', '--repeat', '3', '1'],
                'hold':    ['mousedown', '1'],
                'release': ['mouseup', '1'],
            }
            return out(here_map[action], mods) if action in here_map else None

        # Handle coordinate-based commands
        try:
            x, y = int(parts[0]), int(parts[1])
            if len(parts) == 2:
                return out(['mousemove', str(x), str(y)])

            try:
                mods, action = self._split_mouse_modifiers(parts[2])
            except ValueError as e:
                # Caught here rather than by the enclosing handler, which would swallow
                # the message and fall through to the standalone case.
                print(f"[✗] {e}")
                return None

            if action == 'move':
                return out(['mousemove', str(x), str(y)])
            elif action in ('left', 'click'):
                # `click` is macOS's spelling of `left`; same gesture, so the same
                # argv rather than a second code path that could drift from it.
                return out(['mousemove', str(x), str(y), 'click', '1'], mods)
            elif action == 'right':
                return out(['mousemove', str(x), str(y), 'click', '3'], mods)
            elif action == 'double':
                return out(['mousemove', str(x), str(y), 'click', '--repeat', '2', '1'], mods)
            elif action == 'triple':
                return out(['mousemove', str(x), str(y), 'click', '--repeat', '3', '1'], mods)
            elif action == 'middle':
                return out(['mousemove', str(x), str(y), 'click', '2'], mods)
            elif action == 'hold':
                # Coordinate-based hold/release existed on macOS but not here, so
                # "900 700 hold" failed to build on Linux while the same line worked
                # on the guest. The button stays down until a matching release; the
                # agent tracks it and releases it if the session ends first.
                return out(['mousemove', str(x), str(y), 'mousedown', '1'], mods)
            elif action == 'release':
                return out(['mousemove', str(x), str(y), 'mouseup', '1'], mods)
            elif action == 'drag':
                try:
                    waypoints, (x2, y2), _dwell = self._parse_drag(parts[3:])
                except ValueError as e:
                    # Caught here rather than by the enclosing handler, which
                    # swallows ValueError and would fall through to the standalone
                    # case, refusing the command without saying why.
                    print(f"[X] {e}")
                    return None
                chain = ['mousemove', str(x), str(y), 'mousedown', '1']
                for wx, wy in waypoints:
                    chain += ['mousemove', str(wx), str(wy)]
                chain += ['mousemove', str(x2), str(y2), 'mouseup', '1']
                return out(chain, mods)
            elif action in self.SCROLL_BUTTONS:
                count = parts[3] if len(parts) > 3 else str(self.DEFAULT_SCROLL_NOTCHES)
                return out(['mousemove', str(x), str(y), 'click', '--repeat', count,
                            self.SCROLL_BUTTONS[action]], mods)
        except (ValueError, IndexError):
            pass

        # Standalone scroll (must include count)
        if parts[0] in self.SCROLL_BUTTONS:
            if len(parts) < 2:
                print(f"[!] Error: {parts[0]} requires a count.")
                return None
            return out(['click', '--repeat', parts[1], self.SCROLL_BUTTONS[parts[0]]])

        return None

    # Build xdotool command for keyboard actions
    def _build_keyboard_command(self, command: str) -> Optional[Tuple[List[str], str]]:
        """Build an (argv, human_command) pair for an xdotool keyboard action.

        For 'type', the literal text is a single argv element passed straight to
        xdotool — no shell, so no escaping and no injection (F5).
        """
        parts = command.strip().split(maxsplit=1)

        if len(parts) < 1:
            return None

        action = parts[0]

        # Handle "type" action
        if action == 'type':
            if len(parts) < 2:
                return None
            text = parts[1]
            return ([self.backend, 'type', text], command)

        # Handle "press" / "key" action
        elif action in ('press', 'key'):
            if len(parts) < 2:
                return None
            translated = self._translate_modifier_keys(parts[1])
            return ([self.backend, 'key'] + translated.split(), command)

        return None
    
    # Main command execution method with position tracking
    def execute_command(self, command: str) -> bool:
        """
        Execute command via gRPC with position tracking
        
        Returns:
            Boolean success status
        """
        # Detect command type
        cmd_type, processed_cmd = self.detect_command_type(command)
        
        if cmd_type == 'invalid':
            print(command_hints.rejection_message(
                command, self.MOUSE_ACTIONS, self.KEYBOARD_ACTIONS))
            return False
        
        # Build the xdotool command as (argv, human_command)
        if cmd_type == 'mouse':
            built = self._build_mouse_command(processed_cmd)
        elif cmd_type == 'keyboard':
            built = self._build_keyboard_command(processed_cmd)
        else:
            built = None

        if not built:
            print(f"[✗] Failed to build command: {command}")
            return False

        argv, human_command = built
        position_after = None

        # Send to server via gRPC (structured argv — executed without a shell)
        result = self.grpc_client.execute_command(argv=argv, human_command=human_command)

        position_verified = False
        if cmd_type == 'mouse' and result['success'] and processed_cmd != 'position':
            mx = result.get('mouse_x')
            my = result.get('mouse_y')
            captured = result.get('position_captured', False)
            if mx is not None and my is not None:
                # The agent sends the requested point with position_captured=false
                # when it could not read the pointer back. Over a native-Wayland
                # surface nothing can read it - not xdotool, not the portal helper -
                # so the choice is this coordinate labelled as unverified, or no
                # coordinate at all. It is labelled, never silently promoted: a
                # requested point recorded as an observed one is the failure this
                # whole layer exists to prevent.
                position_after = (mx, my)
                position_verified = bool(captured)
            else:
                position_after = None
        
        # Handle position query result separately. The warning goes here too because
        # this branch returns before the shared result-printing block below, and a
        # status check is exactly when an operator wants to know a button is down.
        if processed_cmd == 'position' and result['success']:
            command_hints.print_held_button_warnings(result.get('metadata'))
            mx = result.get('mouse_x')
            my = result.get('mouse_y')
            captured = result.get('position_captured', False)
            if mx is not None and my is not None:
                if captured:
                    print(f"[POSITION] X={mx}, Y={my}")
                else:
                    # Derived from the motion this backend emitted, because the
                    # display could not be read. Exact for a pointer only cc has
                    # moved; blind to the physical mouse. Labelled rather than
                    # withheld, and never presented as measured.
                    print(f"[POSITION] X={mx}, Y={my} (derived, unverified — the "
                          f"display cannot report the pointer here)")
                return True
        
        # Display result with position info for mouse actions
        if result['success']:
            ms = result['execution_time_ms']
            # A button left down turns every later click or move into a drag-select,
            # and nothing else in the output would say so.
            command_hints.print_held_button_warnings(result.get('metadata'))
            if cmd_type == 'keyboard':
                kb_action, _, kb_content = processed_cmd.partition(' ')
                if kb_action in ('press', 'key'):
                    human = self._format_press_for_display(kb_content)
                    print(f"Pressed: {human}, time taken: {ms}ms")
                else:
                    # Do not echo typed content — it may contain secrets.
                    print(f"Typed: {len(kb_content)} chars, time taken: {ms}ms")
            else:
                tokens = command.strip().split()
                is_here = tokens[0] == 'here'
                action_tok = tokens[1] if is_here and len(tokens) >= 2 else (tokens[2] if len(tokens) >= 3 else None)
                if position_after and position_verified:
                    pos_str = f"X={position_after[0]}, Y={position_after[1]}"
                elif position_after:
                    pos_str = (f"X={position_after[0]}, Y={position_after[1]}"
                               " (requested, unverified)")
                else:
                    pos_str = "X=?, Y=?"

                # A modifier held across the action changes what the action does, so
                # the echo names it. Split off here so the verb branches below stay a
                # match on the bare verb.
                mod_prefix = ''
                if action_tok is not None:
                    mod_prefix, action_tok = self._describe_mouse_modifiers(action_tok)

                if action_tok == 'drag' and len(tokens) >= 5:
                    # The destination comes from the same parser the argv did.
                    # Reading tokens[3:5] names the first waypoint, not the
                    # endpoint, once "via" is in play.
                    try:
                        way, (dx2, dy2), _ = self._parse_drag(tokens[3:])
                    except ValueError:
                        way, dx2, dy2 = [], tokens[3], tokens[4]
                    via_note = f" via {len(way)} waypoint(s)" if way else ""
                    print(f"Executed: {command}, {mod_prefix}dragged from X={tokens[0]}, Y={tokens[1]} to X={dx2}, Y={dy2}{via_note}, time taken: {ms}ms")
                elif action_tok in ('left', 'click', 'right', 'double', 'triple', 'middle'):
                    print(f"Executed: {command}, clicked {mod_prefix}{action_tok} at {pos_str}, time taken: {ms}ms")
                elif action_tok in self.SCROLL_BUTTONS:
                    direction = action_tok.split('_', 1)[1]
                    count_idx = 2 if is_here else 3
                    try:
                        n = int(tokens[count_idx])
                    except (IndexError, ValueError):
                        # The command named no count, so the backend performs the
                        # default — say that number rather than 1, which described
                        # neither the gesture nor the record.
                        n = self.DEFAULT_SCROLL_NOTCHES
                    notch_str = "1 notch" if n == 1 else f"{n} notches"
                    print(f"Executed: {command}, {mod_prefix}scrolled {direction} {notch_str} at {pos_str}, time taken: {ms}ms")
                elif action_tok == 'move':
                    print(f"Executed: {command}, moved to {pos_str}, time taken: {ms}ms")
                elif action_tok == 'hold':
                    # Said here, not by the delayed warning: the operator reaches for
                    # the mouse straight after this command, not after the next one.
                    print(f"Executed: {command}, held at {pos_str}, time taken: {ms}ms")
                    print(command_hints.hold_notice())
                else:
                    print(f"Executed: {command}, at {pos_str}, time taken: {ms}ms")
            return True
        else:
            print(f"[✗] {result['message']}")
            return False
    
    # Batch execution method for commands from a file
    def execute_batch_file(self, filepath: str):
        """Execute commands from file with progress tracking"""
        try:
            with open(filepath, 'r') as f:
                commands = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        except FileNotFoundError:
            print(f"[✗] File not found: {filepath}")
            return
        except Exception as e:
            print(f"[✗] Error reading file: {e}")
            return
        
        if not commands:
            print("[✗] No commands found in file")
            return
        
        print(f"\n[*] Batch mode: Executing {len(commands)} commands...")
        
        success_count = 0
        total_count = 0
        
        for i, command in enumerate(commands, 1):
            cmd_type, processed_cmd = self.detect_command_type(command)
            if cmd_type == 'invalid':
                continue
            
            # Build xdotool command as (argv, human_command)
            if cmd_type == 'mouse':
                built = self._build_mouse_command(processed_cmd)
            elif cmd_type == 'keyboard':
                built = self._build_keyboard_command(processed_cmd)
            else:
                continue

            if not built:
                continue

            argv, human_command = built
            result = self.grpc_client.execute_command(argv=argv, human_command=human_command)
            total_count += 1
            
            if result['success']:
                success_count += 1
                print(f"[{i}/{len(commands)}] ✓ {command_hints.redact(command)}")
            else:
                print(f"[{i}/{len(commands)}] ✗ {command_hints.redact(command)}: {result['message']}")
        
        print(f"\n[✓] Batch complete: {success_count}/{total_count} succeeded")
    
    # Help method to display comprehensive command reference
    def show_help(self):
        """Display comprehensive help information"""
        help_text = """
╔══════════════════════════════════════════════════════════╗
║                   Linux COMMAND REFERENCE                ║
╠══════════════════════════════════════════════════════════╣
║ MOUSE COMMANDS                                           ║
╠══════════════════════════════════════════════════════════╣
║ <x> <y> move           → Move cursor to coordinates      ║
║ <x> <y> left           → Move and left-click             ║
║ <x> <y> right          → Move and right-click            ║
║ <x> <y> double         → Move and double-click           ║
║ <x> <y> middle         → Move and middle-click           ║
║ <x> <y> scroll_up [n]  → Move and scroll up (n times)    ║
║ <x> <y> scroll_down [n]→ Move and scroll down            ║
║ <x> <y> drag <x2> <y2> → Drag from (x,y) to (x2,y2)      ║
║ here <action>          → Action at current position      ║
║ position               → Get current mouse position      ║
║                                                          ║
║ <x> <y> <mods><action> → Hold modifiers for the action   ║
║ here <mods><action>    → e.g. ^left, +left, ^scroll_down ║
║   Modifiers: ^ + ! # (as in press); not on move/position ║
╠══════════════════════════════════════════════════════════╣
║ KEYBOARD COMMANDS                                        ║
╠══════════════════════════════════════════════════════════╣
║ type <text>            → Type literal text               ║
║ press <keys>           → Press keys/shortcuts            ║
║ {Enter}                → Press Enter (auto-detected)     ║
║ ^c                     → Ctrl+C (auto-detected)          ║
║ #                      → Super key (opens menu)          ║
║ #r                     → Super+R (Run dialog)            ║
║                                                          ║
║ Modifiers: ^ (Ctrl), + (Shift), ! (Alt), # (Super/Win)   ║
║ Special: {Enter}, {Tab}, {F1}-{F12}, {Up}, {Down}, etc.  ║
╠══════════════════════════════════════════════════════════╣
║ EXAMPLES                                                 ║
╠══════════════════════════════════════════════════════════╣
║ 960 540 right          → Right-click at center           ║
║ here left              → Left-click at current pos       ║
║ type Hello World       → Type text                       ║
║ press ^v               → Paste (Ctrl+V)                  ║
║ {Enter}                → Press Enter                     ║
║ #                      → Press Super key (app menu)      ║
║ press ^!{Delete}       → Ctrl+Alt+Delete                 ║
║ 200 200 drag 800 600   → Drag operation                  ║
║ here scroll_down 5     → Scroll down 5 notches           ║
║ 770 310 ^left          → Ctrl-click (multi-select)       ║
║ 770 310 +left          → Shift-click (extend selection)  ║
║ 770 310 ^scroll_down 3 → Ctrl-scroll (zoom out)          ║
║ position               → Get mouse coordinates           ║
╠══════════════════════════════════════════════════════════╣
║ POSITION TRACKING                                        ║
╠══════════════════════════════════════════════════════════╣
║ All mouse actions now report coordinates automatically   ║
║ Format: [MOUSE] Action @(x,y) (time)                     ║
║ Example: [MOUSE] Clicked @(960,540) (45ms)               ║
╚══════════════════════════════════════════════════════════╝
        """
        print(help_text)