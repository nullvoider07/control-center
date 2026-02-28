"""Linux-specific actuation with position tracking - Enhanced Implementation"""

import re
import os
from typing import Tuple, Optional

# LinuxActuation class definition
class LinuxActuation:
    """Linux actuation controller with position tracking for all mouse actions"""
    
    # Mouse action keywords
    MOUSE_ACTIONS = {
        'move', 'left', 'right', 'middle', 'double', 
        'scroll_up', 'scroll_down', 'drag', 'here',
        'hold', 'release', 'position'
    }
    
    # Keyboard action keywords
    KEYBOARD_ACTIONS = {'type', 'press'}
    
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
    }
    
    # Keyboard indicators (for detection)
    KEYBOARD_INDICATORS = set(SPECIAL_KEYS_MAP.keys())
    
    def __init__(self, grpc_client):
        """Initialize controller with gRPC client"""
        self.grpc_client = grpc_client
        self.display = os.environ.get('DISPLAY', ':0')
    
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
        modifier_display = {'^': 'Ctrl', '+': 'Shift', '!': 'Alt', '#': 'Super'}
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

    # Translate AutoHotkey modifier syntax to xdotool syntax
    def _translate_modifier_keys(self, text: str) -> str:
        """
        Translate AutoHotkey modifier syntax to xdotool syntax
        
        Handles:
        - Modifier prefixes: ^ (Ctrl), + (Shift), ! (Alt), # (Super)
        - Standalone modifiers: just ^ or # alone
        - Special keys in braces: {Enter}, {Tab}, etc.
        
        Examples:
            "#r" → "super+r"
            "#" → "super"  (standalone Super key)
            "^c" → "ctrl+c"
            "^" → "ctrl"  (standalone Ctrl key)
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
            if len(tokens) >= 2 and tokens[1] in self.MOUSE_ACTIONS:
                return 'mouse', command
            return 'invalid', command
        
        # Check if starts with coordinates (numbers)
        if len(tokens) >= 2:
            try:
                int(tokens[0])
                int(tokens[1])
                if len(tokens) >= 3 and tokens[2] in self.MOUSE_ACTIONS:
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
        
        # Default fallback
        return 'keyboard', f"type {command}"
    
    # Build xdotool command for mouse actions
    def _build_mouse_command(self, command: str) -> Optional[str]:
        """Build xdotool mouse command"""
        parts = command.strip().split()
        
        # POSITION COMMAND - Returns current coordinates
        if parts[0] == 'position':
            return f"DISPLAY={self.display} xdotool getmouselocation --shell"
        
        # Handle 'here' commands
        if parts[0] == 'here':
            if len(parts) < 2:
                return None
            
            action = parts[1]
            
            # Scrolling requires an explicit count from the user
            if action in ['scroll_up', 'scroll_down']:
                if len(parts) < 3:
                    print("[!] Error: You must specify a scroll count (e.g., 'here scroll_down 5')")
                    return None
                count = parts[2]
                button = '4' if action == 'scroll_up' else '5'
                return f'DISPLAY={self.display} xdotool click --repeat {count} {button}'
            
            if action == 'left':
                return f'DISPLAY={self.display} xdotool click 1'
            elif action == 'right':
                return f'DISPLAY={self.display} xdotool click 3'
            elif action == 'middle':
                return f'DISPLAY={self.display} xdotool click 2'
            elif action == 'double':
                return f'DISPLAY={self.display} xdotool click --repeat 2 1'
            elif action == 'hold':
                return f'DISPLAY={self.display} xdotool mousedown 1'
            elif action == 'release':
                return f'DISPLAY={self.display} xdotool mouseup 1'
            
            return None
        
        # Handle coordinate-based commands
        try:
            x, y = int(parts[0]), int(parts[1])
            if len(parts) == 2:
                return f'DISPLAY={self.display} xdotool mousemove {x} {y}'
            
            action = parts[2]
            if action == 'move':
                return f'DISPLAY={self.display} xdotool mousemove {x} {y}'
            elif action == 'left':
                return f'DISPLAY={self.display} xdotool mousemove {x} {y} click 1'
            elif action == 'right':
                return f'DISPLAY={self.display} xdotool mousemove {x} {y} click 3'
            elif action == 'double':
                return f'DISPLAY={self.display} xdotool mousemove {x} {y} click --repeat 2 1'
            elif action == 'middle':
                return f'DISPLAY={self.display} xdotool mousemove {x} {y} click 2'
            elif action == 'drag' and len(parts) >= 5:
                x2, y2 = int(parts[3]), int(parts[4])
                return f'DISPLAY={self.display} xdotool mousemove {x} {y} mousedown 1 mousemove {x2} {y2} mouseup 1'
            elif action in ['scroll_up', 'scroll_down']:
                # Scrolling with coordinates
                count = parts[3] if len(parts) > 3 else '5'
                button = '4' if action == 'scroll_up' else '5'
                return f'DISPLAY={self.display} xdotool mousemove {x} {y} click --repeat {count} {button}'
        except (ValueError, IndexError):
            pass
        
        # Standalone scroll (must include count)
        if parts[0] in ['scroll_up', 'scroll_down']:
            if len(parts) < 2:
                print(f"[!] Error: {parts[0]} requires a count.")
                return None
            button = '4' if parts[0] == 'scroll_up' else '5'
            return f'DISPLAY={self.display} xdotool click --repeat {parts[1]} {button}'
        
        return None
    
    # Build xdotool command for keyboard actions
    def _build_keyboard_command(self, command: str) -> Optional[str]:
        """
        Build xdotool keyboard command
        
        Handles:
        - type: Types literal text
        - press: Presses key combinations (including standalone modifiers)
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
            # Escape special characters for typing
            escaped_text = text.replace('\\', '\\\\').replace('"', '\\"')
            return f'DISPLAY={self.display} xdotool type "{escaped_text}"'
        
        # Handle "press" action
        elif action == 'press':
            if len(parts) < 2:
                # "press" with no arguments is invalid
                return None
            
            text = parts[1]
            
            # Translate the key combination (handles standalone modifiers!)
            translated = self._translate_modifier_keys(text)
            
            # Return xdotool key command
            return f'DISPLAY={self.display} xdotool key {translated}'
        
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
            print(f"[✗] Invalid command: {command}")
            return False
        
        # Build the xdotool command
        if cmd_type == 'mouse':
            xdotool_cmd = self._build_mouse_command(processed_cmd)
        elif cmd_type == 'keyboard':
            xdotool_cmd = self._build_keyboard_command(processed_cmd)
        else:
            xdotool_cmd = None
        
        if not xdotool_cmd:
            print(f"[✗] Failed to build command: {command}")
            return False
        
        position_after = None

        # Send to server via gRPC
        result = self.grpc_client.execute_command(xdotool_cmd)
        
        if cmd_type == 'mouse' and result['success'] and processed_cmd != 'position':
            mx = result.get('mouse_x')
            my = result.get('mouse_y')
            captured = result.get('position_captured', False)
            position_after = (mx, my) if captured and mx is not None and my is not None else None
        
        # Handle position query result separately
        if processed_cmd == 'position' and result['success']:
            mx = result.get('mouse_x')
            my = result.get('mouse_y')
            captured = result.get('position_captured', False)
            if captured and mx is not None and my is not None:
                print(f"[POSITION] X={mx}, Y={my}")
                return True
        
        # Display result with position info for mouse actions
        if result['success']:
            ms = result['execution_time_ms']
            if cmd_type == 'keyboard':
                kb_action, _, kb_content = processed_cmd.partition(' ')
                if kb_action == 'press':
                    human = self._format_press_for_display(kb_content)
                    print(f"Pressed: {human}, time taken: {ms}ms")
                else:
                    print(f"Typed: {kb_content}, time taken: {ms}ms")
            else:
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
            
            # Build xdotool command
            if cmd_type == 'mouse':
                xdotool_cmd = self._build_mouse_command(processed_cmd)
            elif cmd_type == 'keyboard':
                xdotool_cmd = self._build_keyboard_command(processed_cmd)
            else:
                continue
            
            if not xdotool_cmd:
                continue
            
            result = self.grpc_client.execute_command(xdotool_cmd)
            total_count += 1
            
            if result['success']:
                success_count += 1
                print(f"[{i}/{len(commands)}] ✓ {command}")
            else:
                print(f"[{i}/{len(commands)}] ✗ {command}: {result['message']}")
        
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