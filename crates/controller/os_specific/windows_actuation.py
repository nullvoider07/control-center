"""Windows-specific actuation with position tracking - Enhanced Implementation"""

import re
import time
from typing import Tuple, Optional

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
    def _build_position_command(self) -> str:
        """
        Build AutoHotkey command to get mouse position
        
        Returns:
            PowerShell command that executes AHK script to get position
        """
        # AutoHotkey script to get mouse position
        ahk_script = """
        MouseGetPos, xpos, ypos
        FileAppend, X=%xpos%`nY=%ypos%, *
        """
        
        # Return as PowerShell command
        return f'powershell -Command "Add-Type -AssemblyName System.Windows.Forms; $p=[System.Windows.Forms.Cursor]::Position; Write-Output \\"X=$($p.X)`nY=$($p.Y)\\""'
    
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
            pos_cmd = self._build_position_command()
            result = self.grpc_client.execute_command(pos_cmd)
            
            if result['success'] and 'output' in result:
                return self._parse_position_output(result['output'])
            
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
        
        # For keyboard commands, escape carets
        if cmd_type == 'keyboard':
            # The agent will write this to the file, so we need to escape carets
            processed_cmd = processed_cmd.replace('^', '^^')
        
        # For mouse commands: Track position before and after
        position_before = None
        position_after = None
        target_coords = None
        
        if cmd_type == 'mouse':
            # Extract target coordinates from original command (if present)
            target_coords = self._extract_coordinates_from_command(command)
            
            # Get position before action
            position_before = self._get_mouse_position()
        
        # Send to server via gRPC
        result = self.grpc_client.execute_command(processed_cmd)
        
        # For mouse commands: Get position after action
        if cmd_type == 'mouse' and result['success']:
            position_after = self._get_mouse_position()
        
        # Display results with position data
        if result['success']:
            if cmd_type == 'mouse':
                prefix = "[MOUSE]"
                
                # Build position info string
                position_info = ""
                if position_after:
                    x, y = position_after
                    position_info = f" @({x},{y})"
                    
                    # If target coords exist, show them too
                    if target_coords:
                        tx, ty = target_coords
                        position_info = f" @({tx},{ty})→({x},{y})"
                
                # Enhanced feedback for drag operations
                if 'drag' in processed_cmd:
                    parts = processed_cmd.split()
                    if len(parts) >= 5:
                        print(f"{prefix} Drag: ({parts[0]},{parts[1]}) → ({parts[3]},{parts[4]}){position_info}")
                    else:
                        print(f"{prefix} Executed: {processed_cmd}{position_info}")
                else:
                    print(f"{prefix} {result['message']}{position_info} ({result['execution_time_ms']}ms)")
            else:
                prefix = "[KEYBOARD]"
                print(f"{prefix} {result['message']} ({result['execution_time_ms']}ms)")
            
            # UI opening commands need delay
            ui_opening_commands = [
                'press #r',
                'press #',
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
        
        def command_generator():
            for i, command in enumerate(commands, 1):
                cmd_type, formatted_cmd = self.detect_command_type(command)
                if cmd_type != 'invalid':
                    # Escape carets for keyboard commands
                    if cmd_type == 'keyboard':
                        formatted_cmd = formatted_cmd.replace('^', '^^')
                    yield formatted_cmd, i, len(commands), command
        
        success_count = 0
        total_count = 0
        
        for formatted_cmd, i, total, original_cmd in command_generator():
            result = self.grpc_client.execute_command(formatted_cmd)
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
║ {LWin}                 → Press Windows key (opens Start) ║
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