"""Windows-specific actuation with position tracking - Enhanced Implementation"""

import re
import time
from typing import Tuple, Optional, List

from . import command_hints

# Windows actuation class definition
class WindowsActuation:
    """Windows actuation controller with position tracking for all mouse actions"""
    
    # Mouse action keywords
    MOUSE_ACTIONS = {
        'move', 'left', 'right', 'middle', 'double', 
        'scroll_up', 'scroll_down', 'drag', 'here',
        'hold', 'release', 'position'
    }
    
    # Keyboard action keywords
    KEYBOARD_ACTIONS = {'type', 'press'}
    
    # Special keys that indicate keyboard command
    KEYBOARD_INDICATORS = {
        '{Enter}', '{Esc}', '{Tab}', '{Backspace}', '{BS}',
        '{Delete}', '{Del}', '{Space}', '{Up}', '{Down}',
        '{Left}', '{Right}', '{Home}', '{End}', '{PgUp}',
        '{PgDn}', '{F1}', '{F2}', '{F3}', '{F4}', '{F5}',
        '{F6}', '{F7}', '{F8}', '{F9}', '{F10}', '{F11}', '{F12}',
        '{LWin}', '{RWin}', '{Plus}'
    }

    # Brace names that stand for a literal character, spelled as AutoHotkey's Send
    # expects them. A bare '+' in a press command is always read as the Shift
    # modifier, so Ctrl+Plus must be written "^{Plus}".
    BRACE_CHAR_MAP = {'{Plus}': '{+}'}

    # Modifier symbol → display name, shared by the `press` echo and the mouse echo so
    # a Ctrl-click and a Ctrl-chord name the key identically.
    MODIFIER_DISPLAY = {'^': 'Ctrl', '+': 'Shift', '!': 'Alt', '#': 'Win'}

    # Modifier symbols accepted as a prefix on a MOUSE action. Unlike the other two
    # backends this list is not resolved to a backend key name here: the mouse command
    # travels to the guest as text and mouse_control.ahk maps the symbol to
    # "{Ctrl down}" itself. The prefix is still parsed here so a malformed one is
    # refused before it reaches a watcher that would silently do nothing with it.
    MOUSE_MODIFIER_SYMBOLS = {'^': 'ctrl', '+': 'shift', '!': 'alt', '#': 'win'}

    # Notches a scroll performs when the command names no count. One constant per
    # backend, used by both the argv builder and the console echo, because those two
    # disagreed: the backends scrolled 5 while the echo told the operator "1 notch".
    # test_scroll_default_count_is_the_same_on_every_backend reads this and its
    # counterparts, and fails if any of them drifts.
    DEFAULT_SCROLL_NOTCHES = 5


    # Pointer verbs a modifier may be held across — the verbs this backend already
    # has, and no others. `move` and `position` are excluded because no held modifier
    # changes what they do.
    MOUSE_MODIFIER_ACTIONS = frozenset({
        'left', 'right', 'middle', 'double', 'drag', 'hold', 'release',
        'scroll_up', 'scroll_down',
    })

    # Class-level default so instances built without __init__ (tests, replay
    # helpers) still have a defined policy rather than raising on attribute access.
    strict = True

    def __init__(self, grpc_client, strict: bool = True):
        """Initialize controller with gRPC client.

        strict: reject commands that match no known verb instead of typing them.
        """
        self.grpc_client = grpc_client
        self.strict = strict
    
    # Build command to get mouse position using PowerShell
    def _build_position_command(self) -> List[str]:
        """
        Build the argv to query mouse position.

        Sends "position" through the same mouse_cmd.txt path as every other
        mouse command via a direct file write (no shell).  The AHK v2 script on
        the agent side handles "position", and the Rust agent captures the
        coordinates and returns them in mouse_x / mouse_y.
        """
        return ['__write__', r'C:\mouse_cmd.txt', 'position']
    
    # Parse the output from position command
    def _parse_position_output(self, output: str) -> Optional[Tuple[int, int]]:
        """
        Parse position output from command
        
        Args:
            output: Command output containing X=123\nY=456
            
        Returns:
            Tuple of (x, y) or None if parsing failed
        """
        try:
            x_match = re.search(r'X=(\d+)', output)
            y_match = re.search(r'Y=(\d+)', output)
            
            if x_match and y_match:
                return (int(x_match.group(1)), int(y_match.group(1)))
            
            return None
        except Exception as e:
            print(f"[!] Failed to parse position: {e}")
            return None
    
    # Method to get current mouse position
    def _get_mouse_position(self) -> Optional[Tuple[int, int]]:
        """
        Get current mouse position
        
        Returns:
            Tuple of (x, y) or None if failed
        """
        try:
            result = self.grpc_client.execute_command(
                argv=self._build_position_command(), human_command='position',
            )
            
            command_hints.print_held_button_warnings(result.get('metadata'))
            if result['success'] and result.get('position_captured'):
                mx = result.get('mouse_x')
                my = result.get('mouse_y')
                if mx is not None and my is not None:
                    return (mx, my)
            
            return None
        except Exception as e:
            print(f"[!] Failed to get mouse position: {e}")
            return None
    
    # Helper method to extract coordinates from command if present
    def _extract_coordinates_from_command(self, command: str) -> Optional[Tuple[int, int]]:
        """
        Extract target coordinates from command (if present)
        
        Args:
            command: Original command string
            
        Returns:
            Tuple of (x, y) if coordinates found, None otherwise
        """
        tokens = command.strip().split()
        
        # Try to parse first two tokens as coordinates
        if len(tokens) >= 2:
            try:
                x = int(tokens[0])
                y = int(tokens[1])
                return (x, y)
            except ValueError:
                pass
        
        return None
    
    # Process keyboard command to handle standalone modifier keys
    def _process_keyboard_command(self, command: str) -> str:
        """
        Process keyboard command, handling standalone modifier keys
        
        In AutoHotkey:
        - # is a modifier (Win key) that must be followed by another key
        - To press Win key alone, use {LWin} or {RWin}
        - Same for ^, +, ! when used standalone
        
        Args:
            command: Keyboard command (e.g., "press #", "press ^c")
            
        Returns:
            Processed command ready for AutoHotkey
        """
        parts = command.strip().split(maxsplit=1)
        
        if len(parts) < 2:
            return command
        
        action = parts[0]
        keys = parts[1] if len(parts) > 1 else ""
        
        if action != 'press':
            return command
        
        # Handle standalone modifier keys
        # These need to be converted to their actual key names
        standalone_modifiers = {
            '#': '{LWin}',      # Windows key
            '^': '{LCtrl}',     # Ctrl key  
            '+': '{LShift}',    # Shift key
            '!': '{LAlt}',      # Alt key
        }
        
        # Check if it's ONLY a modifier (no other keys)
        if keys in standalone_modifiers:
            return f"press {standalone_modifiers[keys]}"
        
        # If it starts with modifiers but has other keys, keep as-is
        # e.g., "#r" (Win+R) stays as "#r"
        return command

    def _format_press_for_display(self, keys: str) -> str:
        """
        Convert AHK key notation to a human-readable string for CLI output.

        Examples:
            "^t"        -> "Ctrl+T"
            "^+{Esc}"   -> "Ctrl+Shift+Esc"
            "!{Tab}"    -> "Alt+Tab"
            "#r"        -> "Win+R"
            "{LCtrl}"   -> "Ctrl"
            "{Enter}"   -> "Enter"
            "{F5}"      -> "F5"
        """
        modifier_display = self.MODIFIER_DISPLAY
        special_display = {
            '{LCtrl}': 'Ctrl', '{RCtrl}': 'Ctrl',
            '{LShift}': 'Shift', '{RShift}': 'Shift',
            '{LAlt}': 'Alt', '{RAlt}': 'Alt',
            '{LWin}': 'Win', '{RWin}': 'Win',
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

        # Whole string is a standalone special key
        if keys in special_display:
            return special_display[keys]

        # Parse modifier prefix(es) then the remaining key
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
                parts.append(key_part[1:-1])   # strip braces
            elif len(key_part) == 1:
                parts.append(key_part.upper())
            else:
                parts.append(key_part)

        return '+'.join(parts) if parts else keys

    # Helper method to split a modifier prefix off a mouse action token
    def _split_mouse_modifiers(self, token: str) -> Tuple[List[str], str]:
        """Split "<mods><action>" into (modifier symbols, action).

        The symbols are returned rather than a backend key name: the mouse command
        travels to the guest verbatim and mouse_control.ahk does the mapping. This
        exists to refuse a malformed prefix at the controller — the watcher's `switch`
        has no error path, so an unrecognised action there does nothing at all and the
        step still reports success.
        """
        mods: List[str] = []
        i = 0
        while i < len(token) and token[i] in self.MOUSE_MODIFIER_SYMBOLS:
            if token[i] not in mods:
                mods.append(token[i])
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

    # Helper method to validate a mouse command before it reaches the guest watcher
    def _check_mouse_command(self, command: str) -> bool:
        """Refuse a mouse command whose modifier prefix does not parse.

        Windows mouse commands are not built into argv here — they are written to
        C:\\mouse_cmd.txt and parsed on the guest — so this is the only place a bad
        prefix can be caught before it becomes a step that reports success and moves
        nothing.
        """
        tokens = command.strip().split()
        if not tokens:
            return False
        if tokens[0] == 'here':
            index = 1
        elif len(tokens) >= 3 and tokens[0].lstrip('-').isdigit():
            index = 2
        else:
            return True
        if len(tokens) <= index:
            return True
        try:
            self._split_mouse_modifiers(tokens[index])
        except ValueError as e:
            print(f"[✗] {e}")
            return False
        return True

    # Convert AHK modifier prefix notation to explicit down/up syntax for reliable execution
    def _convert_modifiers_to_explicit(self, keys: str) -> str:
        """
        Convert AHK modifier prefix notation to explicit {Key down}/{Key up} syntax.

        The payload reaches the guest through a direct file write to
        C:\\keyboard_cmd.txt and is consumed by keyboard_control.ahk's `Send`, so the
        AHK metacharacters ^ + ! # are live in the key part. Converting the modifier
        prefix to explicit down/up pairs leaves a key part that Send reads literally.

        Examples:
            "^t"        -> "{Ctrl down}t{Ctrl up}"
            "^+{Esc}"   -> "{Ctrl down}{Shift down}{Esc}{Shift up}{Ctrl up}"
            "!{Tab}"    -> "{Alt down}{Tab}{Alt up}"
            "#r"        -> "{LWin down}r{LWin up}"
            "^{Plus}"   -> "{Ctrl down}{+}{Ctrl up}"
            "{F5}"      -> "{F5}"          (no modifier prefix, unchanged)
            "{LCtrl}"   -> "{LCtrl}"       (already explicit, unchanged)
        """
        modifier_map = {
            '^': ('{Ctrl down}',  '{Ctrl up}'),
            '+': ('{Shift down}', '{Shift up}'),
            '!': ('{Alt down}',   '{Alt up}'),
            '#': ('{LWin down}',  '{LWin up}'),
        }
        prefix_down = []
        prefix_up = []
        i = 0
        while i < len(keys) and keys[i] in modifier_map:
            down, up = modifier_map[keys[i]]
            prefix_down.append(down)
            prefix_up.insert(0, up)  # reverse order: last pressed, first released
            i += 1
        key_part = keys[i:]
        # Brace names standing for a literal character, spelled the way Send expects.
        # A bare '+' is always read as the Shift modifier, so Ctrl+Plus is "^{Plus}".
        key_part = self.BRACE_CHAR_MAP.get(key_part, key_part)
        if not prefix_down:
            return key_part  # no modifier prefix — key part only
        return ''.join(prefix_down) + key_part + ''.join(prefix_up)

    # Method to detect command type (mouse/keyboard) with smart parsing
    def detect_command_type(self, command: str) -> Tuple[str, str]:
        """
        Smart detection of command type (mouse/keyboard)
        Returns: (type, command)
        """
        tokens = command.strip().split()
        
        if not tokens:
            return 'invalid', command
        
        # Check for position command
        if tokens[0] == 'position':
            return 'mouse', 'position'
        
        # Check if starts with coordinates (numbers)
        if len(tokens) >= 2:
            try:
                int(tokens[0])
                int(tokens[1])
                # Has coordinates - likely mouse command
                if len(tokens) >= 3 and self._is_mouse_action_token(tokens[2]):
                    return 'mouse', command
                elif len(tokens) == 2:
                    # Just coordinates, assume move
                    return 'mouse', f"{command} move"
            except ValueError:
                pass
        
        # Check for "here" keyword (mouse)
        if tokens[0] == 'here':
            if len(tokens) >= 2 and self._is_mouse_action_token(tokens[1]):
                return 'mouse', command
            else:
                return 'invalid', command
        
        # Check for explicit keyboard actions
        if tokens[0] in self.KEYBOARD_ACTIONS:
            return 'keyboard', command
        
        # Check for modifier keys at start (e.g., ^, +, !, #)
        modifier_pattern = r'^[\^+!#]'
        if re.match(modifier_pattern, command):
            return 'keyboard', f"press {command}"
        
        # Check for keyboard indicators (special keys) - but not if already has 'press'
        if any(indicator in command for indicator in self.KEYBOARD_INDICATORS):
            if not command.startswith('press '):
                return 'keyboard', f"press {command}"
            return 'keyboard', command
        
        # If none of the above, check if first token is mouse action
        if tokens[0] in self.MOUSE_ACTIONS:
            return 'mouse', command
        
        # Unrecognised. In strict mode this is refused; the legacy behaviour typed it
        # into whatever had focus and recorded it as a real step (see command_hints).
        if self.strict:
            return 'invalid', command
        return 'keyboard', f"type {command}"
    
    # Main method to execute command with position tracking for mouse actions
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
        
        # Handle position command
        if processed_cmd == 'position':
            pos = self._get_mouse_position()
            if pos:
                x, y = pos
                print(f"[POSITION] X={x}, Y={y}")
                return True
            else:
                print("[✗] Failed to get mouse position")
                return False
        
        kb_action = ''
        original_kb_content = ''

        if cmd_type == 'keyboard':
            processed_cmd = self._process_keyboard_command(processed_cmd)
            kb_action, _, kb_content = processed_cmd.partition(' ')
            # Save original kb_content for display before any transformation
            original_kb_content = kb_content

            if kb_action == 'press':
                kb_content = self._convert_modifiers_to_explicit(kb_content)
                file_payload = f'press {kb_content}'
            else:
                # Typed text stays on the 'type' action, which keyboard_control.ahk
                # dispatches to SendText — literal, with no metacharacter pass. It must
                # never be rewritten to 'press': that routes the payload through Send,
                # where ! + # { } become live, so "type a^b!c" would press Alt+C.
                file_payload = f'type {kb_content}'

            # Write the AHK watcher file directly (agent uses fs, no cmd /c echo) — this
            # removes the `> file` / echo shell-injection surface and preserves special
            # characters verbatim (F5).
            #
            # The expansion is transport, not the record. `human_command` is parsed by
            # the agent into the display string the server stores as CommandEvent
            # .raw_command, so reporting the AHK wire form put "{Ctrl down}s{Ctrl up}"
            # into the corpus where every other backend records the command as issued.
            # The mouse branch below and macOS's parse_keyboard_command both report
            # the canonical command; this was the one branch that did not.
            argv = ['__write__', r'C:\keyboard_cmd.txt', file_payload]
            human_command = processed_cmd
        else:
            if not self._check_mouse_command(processed_cmd):
                return False
            argv = ['__write__', r'C:\mouse_cmd.txt', processed_cmd]
            human_command = processed_cmd

        position_after = None

        # Send to server via gRPC (structured argv — no shell)
        result = self.grpc_client.execute_command(argv=argv, human_command=human_command)
        
        # For mouse commands: Get position after action
        if cmd_type == 'mouse' and result['success']:
            mx = result.get('mouse_x')
            my = result.get('mouse_y')
            captured = result.get('position_captured', False)
            position_after = (mx, my) if captured and mx is not None and my is not None else None
        
        # Display results
        if result['success']:
            ms = result['execution_time_ms']
            # A button left down turns every later click or move into a drag-select,
            # and nothing else in the output would say so.
            command_hints.print_held_button_warnings(result.get('metadata'))
            if cmd_type == 'keyboard':
                if kb_action == 'press':
                    human = self._format_press_for_display(original_kb_content)
                    print(f"Pressed: {human}, time taken: {ms}ms")
                else:
                    # Do not echo typed content — it may contain secrets.
                    print(f"Typed: {len(original_kb_content)} chars, time taken: {ms}ms")
            else:
                # Mouse
                tokens = command.strip().split()
                is_here = tokens[0] == 'here'
                action_tok = tokens[1] if is_here and len(tokens) >= 2 else (tokens[2] if len(tokens) >= 3 else None)
                pos_str = f"X={position_after[0]}, Y={position_after[1]}" if position_after else "X=?, Y=?"

                # A modifier held across the action changes what the action does, so
                # the echo names it. Split off here so the verb branches below stay a
                # match on the bare verb.
                mod_prefix = ''
                if action_tok is not None:
                    mod_prefix, action_tok = self._describe_mouse_modifiers(action_tok)

                if action_tok == 'drag' and len(tokens) >= 5:
                    print(f"Executed: {command}, {mod_prefix}dragged from X={tokens[0]}, Y={tokens[1]} to X={tokens[3]}, Y={tokens[4]}, time taken: {ms}ms")
                elif action_tok in ('left', 'right', 'double', 'middle'):
                    print(f"Executed: {command}, clicked {mod_prefix}{action_tok} at {pos_str}, time taken: {ms}ms")
                elif action_tok in ('scroll_up', 'scroll_down'):
                    direction = 'up' if action_tok == 'scroll_up' else 'down'
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
                    # Announced by the command that creates the hold. The delayed
                    # warning only speaks on a later command, so an operator who
                    # holds and then reaches for the mouse is told nothing, and
                    # finds their physical clicks dead with no explanation.
                    print(f"Executed: {command}, held at {pos_str}, time taken: {ms}ms")
                    print(command_hints.hold_notice())
                else:
                    print(f"Executed: {command}, at {pos_str}, time taken: {ms}ms")
            
            # UI opening commands need delay
            ui_opening_commands = [
                'press #r',
                'press #',
                'press {LWin}',  # Added this
                'press !{Tab}',
                'press ^+{Esc}',
            ]
            
            # Check if this command opens a UI element
            for ui_cmd in ui_opening_commands:
                if ui_cmd in processed_cmd:
                    time.sleep(0.3)
                    break
            
            return True
        else:
            print(f"[✗] {result['message']}")
            return False
    
    # Method to execute batch commands from a file with progress tracking
    def execute_batch_file(self, filepath: str):
        """Execute commands from file with progress tracking"""
        # Read all commands first
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
        
        # Generator to yield (argv, human_command) with progress info
        def command_generator():
            for i, command in enumerate(commands, 1):
                cmd_type, formatted_cmd = self.detect_command_type(command)
                if cmd_type != 'invalid':
                    if cmd_type == 'keyboard':
                        formatted_cmd = self._process_keyboard_command(formatted_cmd)
                        kb_action, _, kb_content = formatted_cmd.partition(' ')
                        if kb_action == 'press':
                            kb_content = self._convert_modifiers_to_explicit(kb_content)
                            file_payload = f'press {kb_content}'
                        else:
                            file_payload = f'type {kb_content}'
                        argv = ['__write__', r'C:\keyboard_cmd.txt', file_payload]
                    else:
                        if not self._check_mouse_command(formatted_cmd):
                            continue
                        argv = ['__write__', r'C:\mouse_cmd.txt', formatted_cmd]
                    # Same split as execute_command: the AHK expansion travels in argv,
                    # the canonical command is what gets reported and recorded.
                    yield argv, formatted_cmd, i, len(commands), command

        success_count = 0
        total_count = 0

        for argv, human_command, i, total, original_cmd in command_generator():
            result = self.grpc_client.execute_command(argv=argv, human_command=human_command)
            total_count += 1
            
            if result['success']:
                success_count += 1
                print(f"[{i}/{total}] ✓ {command_hints.redact(original_cmd)}")
            else:
                print(f"[{i}/{total}] ✗ {command_hints.redact(original_cmd)}: {result['message']}")
            
            # Small delay between commands
            if i < total:
                time.sleep(0.1)
        
        print(f"\n[✓] Batch complete: {success_count}/{total_count} succeeded")
    
    # Help method to display comprehensive command reference
    def show_help(self):
        """Display comprehensive help information"""
        help_text = """
╔══════════════════════════════════════════════════════════╗
║                   Windows COMMAND REFERENCE              ║
╠══════════════════════════════════════════════════════════╣
║ MOUSE COMMANDS                                           ║
╠══════════════════════════════════════════════════════════╣
║ <x> <y> move           → Move cursor to coordinates      ║
║ <x> <y> left           → Move and left-click             ║
║ <x> <y> right          → Move and right-click            ║
║ <x> <y> double         → Move and double-click           ║
║ <x> <y> middle         → Move and middle-click           ║
║ <x> <y> scroll_up [n]  → Move and scroll up              ║
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
║ #                      → Windows key (opens Start)       ║
║ #r                     → Win+R (Run dialog)              ║
║                                                          ║
║ Modifiers: ^ (Ctrl), + (Shift), ! (Alt), # (Win)         ║
║ Special: {Enter}, {Esc}, {Tab}, {F1}-{F12}, etc.         ║
╠══════════════════════════════════════════════════════════╣
║ EXAMPLES                                                 ║
╠══════════════════════════════════════════════════════════╣
║ 960 540 right          → Right-click at center           ║
║ here left              → Left-click at current pos       ║
║ type Hello World       → Type text                       ║
║ press ^v               → Paste (Ctrl+V)                  ║
║ {Enter}                → Press Enter                     ║
║ #                      → Press Windows key (Start menu)  ║
║ {LWin}                 → Press Windows key (alternative) ║
║ 200 200 drag 800 600   → Drag operation                  ║
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