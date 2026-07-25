"""Windows-specific actuation with position tracking - Enhanced Implementation"""

import re
import time
from typing import Tuple, Optional, List

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
        '{LWin}', '{RWin}'
    }
    
    def __init__(self, grpc_client):
        """Initialize controller with gRPC client"""
        self.grpc_client = grpc_client
    
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

    # Helper method to escape cmd.exe special characters in keyboard commands
    def _escape_cmd_special(self, text: str) -> str:
        """Escape characters cmd.exe intercepts before they reach keyboard_cmd.txt."""
        for ch in ['<', '>', '|', '&', '"']:
            text = text.replace(ch, f'^{ch}')
        return text

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
        modifier_display = {'^': 'Ctrl', '+': 'Shift', '!': 'Alt', '#': 'Win'}
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

    # Convert AHK modifier prefix notation to explicit down/up syntax for reliable execution
    def _convert_modifiers_to_explicit(self, keys: str) -> str:
        """
        Convert AHK modifier prefix notation to explicit {Key down}/{Key up} syntax.

        The Rust agent calls ProcessCommand::new("cmd").arg("/c").arg(command),
        which passes the command as a pre-tokenized argument. Windows CreateProcess
        quotes it, so cmd.exe never runs its escape-character pass — meaning '^'
        is never eaten and '^^' never collapses. Therefore '^' must be removed
        from the echo string for 'press' commands entirely, by converting to
        explicit AHK down/up syntax that contains no special characters at all.

        Examples:
            "^t"        -> "{Ctrl down}t{Ctrl up}"
            "^+{Esc}"   -> "{Ctrl down}{Shift down}{Esc}{Shift up}{Ctrl up}"
            "!{Tab}"    -> "{Alt down}{Tab}{Alt up}"
            "#r"        -> "{LWin down}r{LWin up}"
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
        if not prefix_down:
            return keys  # no modifier prefix — pass through unchanged
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
                if len(tokens) >= 3 and tokens[2] in self.MOUSE_ACTIONS:
                    return 'mouse', command
                elif len(tokens) == 2:
                    # Just coordinates, assume move
                    return 'mouse', f"{command} move"
            except ValueError:
                pass
        
        # Check for "here" keyword (mouse)
        if tokens[0] == 'here':
            if len(tokens) >= 2 and tokens[1] in self.MOUSE_ACTIONS:
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
        
        # Default: assume it's text to type
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
            print(f"[✗] Invalid command: {command}")
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
                if '^' in kb_content:
                    safe = kb_content.replace('^', '{U+005E}')
                    file_payload = f'press {safe}'
                else:
                    file_payload = f'type {kb_content}'

            # Write the AHK watcher file directly (agent uses fs, no cmd /c echo) — this
            # removes the `> file` / echo shell-injection surface and preserves special
            # characters verbatim (F5).
            argv = ['__write__', r'C:\keyboard_cmd.txt', file_payload]
            human_command = file_payload
        else:
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

                if action_tok == 'drag' and len(tokens) >= 5:
                    print(f"Executed: {command}, dragged from X={tokens[0]}, Y={tokens[1]} to X={tokens[3]}, Y={tokens[4]}, time taken: {ms}ms")
                elif action_tok in ('left', 'right', 'double', 'middle'):
                    print(f"Executed: {command}, clicked {action_tok} at {pos_str}, time taken: {ms}ms")
                elif action_tok in ('scroll_up', 'scroll_down'):
                    direction = 'up' if action_tok == 'scroll_up' else 'down'
                    count_idx = 2 if is_here else 3
                    try:
                        n = int(tokens[count_idx])
                    except (IndexError, ValueError):
                        n = 1
                    notch_str = "1 notch" if n == 1 else f"{n} notches"
                    print(f"Executed: {command}, scrolled {direction} {notch_str} at {pos_str}, time taken: {ms}ms")
                elif action_tok == 'move':
                    print(f"Executed: {command}, moved to {pos_str}, time taken: {ms}ms")
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
                            if '^' in kb_content:
                                safe = kb_content.replace('^', '{U+005E}')
                                file_payload = f'press {safe}'
                            else:
                                file_payload = f'type {kb_content}'
                        argv = ['__write__', r'C:\keyboard_cmd.txt', file_payload]
                    else:
                        argv = ['__write__', r'C:\mouse_cmd.txt', formatted_cmd]
                        file_payload = formatted_cmd
                    yield argv, file_payload, i, len(commands), command

        success_count = 0
        total_count = 0

        for argv, human_command, i, total, original_cmd in command_generator():
            result = self.grpc_client.execute_command(argv=argv, human_command=human_command)
            total_count += 1
            
            if result['success']:
                success_count += 1
                print(f"[{i}/{total}] ✓ {original_cmd}")
            else:
                print(f"[{i}/{total}] ✗ {original_cmd}: {result['message']}")
            
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