"""macOS-specific actuation with position tracking - Enhanced Implementation"""

import re
from typing import Tuple, Optional

# macOS actuation class definition
class MacOSActuation:
    """macOS actuation controller with position tracking for all mouse actions"""
    
    # Mouse action keywords
    MOUSE_ACTIONS = {
        'move', 'click', 'left', 'right', 'middle', 'double', 
        'triple', 'scroll_up', 'scroll_down', 'scroll_left', 'scroll_right',
        'drag', 'here', 'hold', 'release', 'position'
    }
    
    # Keyboard action keywords
    KEYBOARD_ACTIONS = {'type', 'press', 'key'}
    
    # SPECIAL KEYS (Only keys supported by cliclick 'kp:')
    SPECIAL_KEYS = {
        # Function keys
        '{F1}': 'f1', '{F2}': 'f2', '{F3}': 'f3', '{F4}': 'f4',
        '{F5}': 'f5', '{F6}': 'f6', '{F7}': 'f7', '{F8}': 'f8',
        '{F9}': 'f9', '{F10}': 'f10', '{F11}': 'f11', '{F12}': 'f12',
        '{F13}': 'f13', '{F14}': 'f14', '{F15}': 'f15', '{F16}': 'f16',
        
        # Navigation & System
        '{Enter}': 'return', '{Return}': 'return',
        '{Tab}': 'tab', 
        '{Esc}': 'esc', '{Escape}': 'esc',
        '{Space}': 'space', 
        '{Backspace}': 'delete', '{BS}': 'delete',
        '{Delete}': 'fwd-delete', '{Del}': 'fwd-delete',
        '{Up}': 'arrow-up', '{Down}': 'arrow-down',
        '{Left}': 'arrow-left', '{Right}': 'arrow-right',
        '{Home}': 'home', '{End}': 'end',
        '{PgUp}': 'page-up', '{PgDn}': 'page-down',
        
        # Media (Supported by cliclick)
        '{VolumeUp}': 'volume-up',
        '{VolumeDown}': 'volume-down',
        '{Mute}': 'mute',
        '{BrightnessUp}': 'brightness-up',
        '{BrightnessDown}': 'brightness-down',
        '{PlayPause}': 'play-pause',
    }
    
    # MODIFIER MAP (Used for kd/ku commands)
    MODIFIER_MAP = {
        # Symbols
        '^': 'ctrl', '⌃': 'ctrl',
        '+': 'shift', '⇧': 'shift',
        '!': 'alt', '⌥': 'alt',
        '#': 'cmd', '⌘': 'cmd',
        
        # Names
        '{Cmd}': 'cmd', '{Command}': 'cmd',
        '{Option}': 'alt', '{Alt}': 'alt',
        '{Control}': 'ctrl', '{Ctrl}': 'ctrl',
        '{Shift}': 'shift',
        '{Fn}': 'fn'
    }
    
    def __init__(self, grpc_client):
        """Initialize controller with gRPC client"""
        self.grpc_client = grpc_client
        self.cliclick_path = "cliclick"
    
    # Helper method to get current mouse position using cliclick
    def _get_mouse_position(self) -> Optional[Tuple[int, int]]:
        """
        Get current mouse position using cliclick
        
        Returns:
            Tuple of (x, y) or None if failed
        """
        try:
            # Use cliclick to get position
            pos_cmd = f"{self.cliclick_path} p:."
            result = self.grpc_client.execute_command(pos_cmd)
            
            # BUG-007 FIX: Position data is returned in mouse_x/mouse_y fields,
            # not in a non-existent 'output' key. cliclick commands contain the
            # string "click" so the Rust agent classifies them as mouse actions,
            # captures position after execution, and returns it here.
            if result['success'] and result.get('position_captured'):
                mx = result.get('mouse_x')
                my = result.get('mouse_y')
                if mx is not None and my is not None:
                    return (mx, my)
            
            return None
        except Exception as e:
            print(f"[!] Failed to get mouse position: {e}")
            return None
    
    def _format_press_for_display(self, keys: str) -> str:
        """Convert modifier-prefixed key notation to human-readable string for CLI output.
        
        The modifier symbols (^, +, !, #, and Unicode equivalents) and brace-enclosed
        key names are the same input syntax accepted by the controller for press commands.
        This method is only used for display; it does not affect actuation.
        
        Examples:
            "#c"        -> "Cmd+C"
            "^v"        -> "Ctrl+V"
            "!{Tab}"    -> "Option+Tab"
            "{F11}"     -> "F11"
            "{Enter}"   -> "Return"
        """
        modifier_display = {'^': 'Ctrl', '+': 'Shift', '!': 'Option', '#': 'Cmd',
                            '⌃': 'Ctrl', '⇧': 'Shift', '⌥': 'Option', '⌘': 'Cmd'}
        special_display = {
            '{Enter}': 'Return', '{Return}': 'Return', '{Esc}': 'Esc',
            '{Tab}': 'Tab', '{Space}': 'Space',
            '{Backspace}': 'Delete', '{BS}': 'Delete',
            '{Delete}': 'Fwd Delete', '{Del}': 'Fwd Delete',
            '{Up}': 'Up', '{Down}': 'Down', '{Left}': 'Left', '{Right}': 'Right',
            '{Home}': 'Home', '{End}': 'End',
            '{PgUp}': 'Page Up', '{PgDn}': 'Page Down',
            '{Cmd}': 'Cmd', '{Command}': 'Cmd',
            '{Option}': 'Option', '{Alt}': 'Option',
            '{Control}': 'Ctrl', '{Ctrl}': 'Ctrl', '{Shift}': 'Shift',
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

    # Helper method to extract coordinates from command (if present)
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
        
        # Check for modifier keys at start (supporting Unicode symbols)
        modifier_pattern = r'^[⌘⌥⌃⇧^+!#]'
        if re.match(modifier_pattern, command):
            return 'keyboard', f"press {command}"
        
        # Check for special keys OR modifiers
        if any(key in command for key in self.SPECIAL_KEYS.keys()) or any(key in command for key in self.MODIFIER_MAP.keys()):
            if not command.startswith(tuple(self.KEYBOARD_ACTIONS)):
                return 'keyboard', f"press {command}"
            return 'keyboard', command
        
        # If first token is mouse action
        if tokens[0] in self.MOUSE_ACTIONS:
            return 'mouse', command
        
        # Default: assume it's text to type
        return 'keyboard', f"type {command}"
    
    # Helper method to build scroll command using AppleScript repeat loop
    def build_scroll_command(self, x: Optional[int], y: Optional[int], 
                              direction: str, amount: int) -> str:
        """Build scroll command using AppleScript repeat loop"""
        # Map direction to AppleScript Key Codes
        # 126=Up, 125=Down, 123=Left, 124=Right
        if direction in ['scroll_up', 'scroll-up', 'scrollup']:
            key_code = 126
        elif direction in ['scroll_down', 'scroll-down', 'scrolldown']:
            key_code = 125
        elif direction in ['scroll_left', 'scroll-left', 'scrollleft']:
            key_code = 123
        elif direction in ['scroll_right', 'scroll-right', 'scrollright']:
            key_code = 124
        else:
            return ""
        
        # Build Focus Command (Click to ensure window receives keys)
        if x is not None and y is not None:
            focus_cmd = f"{self.cliclick_path} c:{x},{y} w:50"
        else:
            focus_cmd = f"{self.cliclick_path} c:. w:50"
        
        scroll_cmd = (
            f"osascript -e 'tell application \"System Events\" to "
            f"repeat {amount} times' "
            f"-e 'key code {key_code}' "
            f"-e 'delay 0.02' "
            f"-e 'end repeat'"
        )
        return f"{focus_cmd} && {scroll_cmd}"
    
    # Method to build mouse command based on parsed input
    def build_mouse_command(self, command: str) -> str:
        """Build command for mouse actions"""
        tokens = command.strip().split()
        
        if not tokens:
            return ""
        
        cli = self.cliclick_path
        
        # Position command - Returns current coordinates
        if tokens[0] == 'position':
            return f"{cli} p:."
        
        # CASE A: "here <action> [amount]"
        if tokens[0] == 'here':
            action = tokens[1] if len(tokens) > 1 else 'left'
            
            # Simple clicks
            if action in ['left', 'click']: return f"{cli} c:."
            elif action == 'right':         return f"{cli} rc:."
            elif action == 'double':        return f"{cli} dc:."
            elif action == 'triple':        return f"{cli} tc:."
            elif action == 'middle':        return f"{cli} mc:."
            elif action == 'hold':          return f"{cli} dd:."
            elif action == 'release':       return f"{cli} du:."
            
            # Scrolling (No coords needed)
            elif 'scroll' in action:
                amount = int(tokens[2]) if len(tokens) > 2 else 5
                return self.build_scroll_command(None, None, action, amount)
            return ""
        
        # CASE B: "x y <action> [amount]"
        try:
            x, y = int(tokens[0]), int(tokens[1])
            if len(tokens) == 2:
                return f"{cli} m:{x},{y}"
            
            action = tokens[2]
            if action == 'move':
                return f"{cli} m:{x},{y}"
            elif action in ['left', 'click']:
                return f"{cli} c:{x},{y}"
            elif action == 'right':
                return f"{cli} rc:{x},{y}"
            elif action == 'double':
                return f"{cli} dc:{x},{y}"
            elif action == 'triple':
                return f"{cli} tc:{x},{y}"
            elif action == 'middle':
                return f"{cli} mc:{x},{y}"
            elif action == 'hold':
                return f"{cli} dd:{x},{y}"
            elif action == 'release':
                return f"{cli} du:{x},{y}"
            elif action == 'drag' and len(tokens) >= 5:
                x2, y2 = int(tokens[3]), int(tokens[4])
                return f"{cli} dd:{x},{y} w:50 m:{x2},{y2} w:50 du:{x2},{y2}"
            elif 'scroll' in action:
                amount = int(tokens[3]) if len(tokens) > 3 else 5
                return self.build_scroll_command(x, y, action, amount)
        except (ValueError, IndexError):
            pass
        
        # CASE C: Standalone actions
        action = tokens[0]
        if action in ['left', 'click']:     return f"{cli} c:."
        elif action == 'right':             return f"{cli} rc:."
        elif action == 'double':            return f"{cli} dc:."
        elif action == 'triple':            return f"{cli} tc:."
        elif action == 'middle':            return f"{cli} mc:."
        elif action == 'hold':              return f"{cli} dd:."
        elif action == 'release':           return f"{cli} du:."
        elif 'scroll' in action:
            amount = int(tokens[1]) if len(tokens) > 1 else 5
            return self.build_scroll_command(None, None, action, amount)
        
        return ""
    
    # Method to parse keyboard command and build appropriate cliclick or osascript command
    def parse_keyboard_command(self, command: str) -> str:
        """Parse keyboard command"""
        tokens = command.strip().split(maxsplit=1)
        
        if len(tokens) < 2:
            return ""
        
        action = tokens[0]
        text = tokens[1]
        
        cli = self.cliclick_path
        
        # CASE 1: type <text>
        if action == 'type':
            escaped = text.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('`', '\\`').replace("'", "\\'")
            return f'{cli} t:"{escaped}"'
        
        # CASE 2: press <keys>
        elif action in ['press', 'key']:
            # Parse modifiers and main key
            modifiers = []
            main_key = text
            
            # Extract modifier symbols
            i = 0
            while i < len(text):
                if text[i:i+1] in self.MODIFIER_MAP:
                    mod = self.MODIFIER_MAP[text[i]]
                    if mod not in modifiers:
                        modifiers.append(mod)
                    i += 1
                elif text[i:i+2] in ['⌘', '⌥', '⌃', '⇧']:
                    mod = self.MODIFIER_MAP[text[i:i+2]]
                    if mod not in modifiers:
                        modifiers.append(mod)
                    i += 2
                else:
                    main_key = text[i:]
                    break
            
            # Map for osascript key codes
            osascript_map = {
                'return': 36, 'tab': 48, 'delete': 51, 'fwd-delete': 117,
                'page-up': 116, 'page-down': 121, 'home': 115, 'end': 119,
                'arrow-left': 123, 'arrow-right': 124, 'arrow-down': 125, 'arrow-up': 126,
            }
            
            # Normalize special key
            normalized_key = main_key
            if main_key in self.SPECIAL_KEYS:
                normalized_key = self.SPECIAL_KEYS[main_key]
            
            # Check against keys that need osascript
            target_keys = [
                'return', 'tab', 'delete', 'fwd-delete', 
                'page-up', 'page-down', 'home', 'end',
                'arrow-left', 'arrow-right', 'arrow-down', 'arrow-up',
            ]
            
            if normalized_key in target_keys:
                code = osascript_map[normalized_key]
                
                # Build AppleScript command
                cmd = f'osascript -e \'tell application "System Events" to key code {code}'
                
                if modifiers:
                    # Map cliclick modifiers to AppleScript syntax
                    osa_mods = {
                        'cmd': 'command down', 'alt': 'option down',
                        'ctrl': 'control down', 'shift': 'shift down'
                    }
                    mod_list = [osa_mods[m] for m in modifiers if m in osa_mods]
                    if mod_list:
                        cmd += f' using {{{", ".join(mod_list)}}}'
                
                cmd += "'"
                return cmd

            # Handle Special Keys (cliclick fallback)
            if main_key in self.SPECIAL_KEYS:
                key_code = self.SPECIAL_KEYS[main_key]
                
                if modifiers:
                    mod_str = ','.join(modifiers)
                    return f"{cli} kd:{mod_str} kp:{key_code} ku:{mod_str}"
                else:
                    return f"{cli} kp:{key_code}"
            
            # Handle Single Characters
            if len(main_key) == 1:
                if modifiers:
                    mod_str = ','.join(modifiers)
                    return f"{cli} kd:{mod_str} t:{main_key} ku:{mod_str}"
                else:
                    return f"{cli} t:{main_key}"
            
            # Handle "space" as special case
            if main_key.lower() == 'space':
                if modifiers:
                    mod_str = ','.join(modifiers)
                    return f"{cli} kd:{mod_str} kp:space ku:{mod_str}"
                else:
                    return f"{cli} kp:space"
            
            # Only Modifiers
            if not main_key and modifiers:
                mod_str = ','.join(modifiers)
                return f"{cli} kd:{mod_str} w:50 ku:{mod_str}"
            
            # Fallback
            if main_key:
                if modifiers:
                    mod_str = ','.join(modifiers)
                    return f"{cli} kd:{mod_str} t:{main_key} ku:{mod_str}"
                else:
                    return f"{cli} t:{main_key}"
        
        return ""
    
    # Method to execute command via gRPC with position tracking
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
        
        # Build the cliclick command
        if cmd_type == 'mouse':
            cliclick_cmd = self.build_mouse_command(processed_cmd)
        else:
            cliclick_cmd = self.parse_keyboard_command(processed_cmd)
        
        if not cliclick_cmd:
            print(f"[✗] Failed to build command: {command}")
            return False
        
        # For mouse commands: Track position before and after
        position_before = None
        position_after = None
        target_coords = None
        
        if cmd_type == 'mouse':
            # Extract target coordinates from original command (if present)
            target_coords = self._extract_coordinates_from_command(command)
        
        # Send to server via gRPC
        result = self.grpc_client.execute_command(cliclick_cmd)
        
        # For mouse commands: Get position after action
        # BUG-007 FIX: Read mouse_x/mouse_y directly from the gRPC result.
        # The agent captures position internally after mouse commands and returns
        # it in the dedicated fields — no extra gRPC call needed.
        if cmd_type == 'mouse' and result['success']:
            mx = result.get('mouse_x')
            my = result.get('mouse_y')
            captured = result.get('position_captured', False)
            position_after = (mx, my) if captured and mx is not None and my is not None else None
        
        # Display results with position data
        if result['success']:
            ms = result['execution_time_ms']
            if cmd_type == 'keyboard':
                kb_action, _, kb_content = processed_cmd.partition(' ')
                if kb_action in ('press', 'key'):
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
                elif action_tok in ('left', 'click', 'right', 'double', 'triple', 'middle'):
                    click_label = 'double' if action_tok == 'double' else ('triple' if action_tok == 'triple' else action_tok)
                    print(f"Executed: {command}, clicked {click_label} at {pos_str}, time taken: {ms}ms")
                elif action_tok in ('scroll_up', 'scroll_down', 'scroll_left', 'scroll_right'):
                    direction = action_tok.replace('scroll_', '')
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
    
    # Method to execute batch commands from a file with progress tracking
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
            
            # Build cliclick command
            if cmd_type == 'mouse':
                cliclick_cmd = self.build_mouse_command(processed_cmd)
            else:
                cliclick_cmd = self.parse_keyboard_command(processed_cmd)
            
            if not cliclick_cmd:
                continue
            
            result = self.grpc_client.execute_command(cliclick_cmd)
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
║                   macOS COMMAND REFERENCE                ║
╠══════════════════════════════════════════════════════════╣
║ MOUSE COMMANDS                                           ║
╠══════════════════════════════════════════════════════════╣
║ <x> <y> move           → Move cursor to coordinates      ║
║ <x> <y> click/left     → Move and left-click             ║
║ <x> <y> right          → Move and right-click            ║
║ <x> <y> double         → Move and double-click           ║
║ <x> <y> triple         → Move and triple-click           ║
║ <x> <y> middle         → Move and middle-click           ║
║ <x> <y> scroll_up [n]  → Move and scroll up (n notches)  ║
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
║ #c                     → Cmd+C (auto-detected)           ║
║ ^v                     → Ctrl+V                          ║
║                                                          ║
║ Modifiers: ^ (Ctrl), + (Shift), ! (Alt/Option), # (Cmd)  ║
║ Unicode: ⌃ (Ctrl), ⇧ (Shift), ⌥ (Option), ⌘ (Cmd)        ║
║ Special: {Enter}, {Tab}, {F1}-{F16}, media keys          ║
╠══════════════════════════════════════════════════════════╣
║ EXAMPLES                                                 ║
╠══════════════════════════════════════════════════════════╣
║ 960 540 right          → Right-click at center           ║
║ here left              → Left-click at current pos       ║
║ type Hello World       → Type text                       ║
║ press #v               → Paste (Cmd+V)                   ║
║ #c                     → Copy (auto-detected)            ║
║ {F11}                  → Press F11 key                   ║
║ 200 200 drag 800 600   → Drag operation                  ║
║ 500 500 scroll_down 10 → Scroll down 10 notches          ║
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