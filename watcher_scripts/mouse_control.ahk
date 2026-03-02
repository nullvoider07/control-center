; Full-Featured Mouse Control Script (Unified & Restored)
; Usage 1 (CLI):     AutoHotkey.exe mouse_control.ahk <x> <y> <action>
; Usage 2 (Watcher): AutoHotkey.exe mouse_control.ahk watcher

#SingleInstance Force
CoordMode "Mouse", "Screen"

; --- MODE 1: WATCHER SERVICE ---
if (A_Args.Length > 0 and A_Args[1] = "watcher") {
    SetTimer(TrackPosition, 10)
    Loop {
        if FileExist("C:\mouse_cmd.txt") {
            try {
                cmdText := FileRead("C:\mouse_cmd.txt")
                FileDelete "C:\mouse_cmd.txt"
                
                if (cmdText != "") {
                    cmdArgs := StrSplit(cmdText, " ")
                    ExecuteCommand(cmdArgs)
                }
            } catch as e {
                ; Ignore errors
            }
        }
        Sleep 50
    }
    ExitApp
}

TrackPosition() {
    MouseGetPos(&x, &y)
    RegWrite(x, "REG_SZ", "HKCU\Software\MouseTracker", "MouseX")
    RegWrite(y, "REG_SZ", "HKCU\Software\MouseTracker", "MouseY")
}

; --- MODE 2: ONE-SHOT CLI ---
if (A_Args.Length < 1) {
    MsgBox "Usage: mouse_control.ahk <x> <y> <action> OR mouse_control.ahk watcher"
    ExitApp 1
}

ExecuteCommand(A_Args)
ExitApp

ExecuteCommand(args) {
    ; 1. Handle standalone position query
    if (args[1] = "position") {
        return
    }

    ; 2. Parse Coordinates vs "Here"
    if (args[1] = "here") {
        if (args.Length < 2)
            return

        action := args[2]
        paramOffset := 3
    } else {
        if (args.Length < 3)
            return

        x := args[1]
        y := args[2]
        action := args[3]
        paramOffset := 4

        ; Move and Wait
        MouseMove x, y, 0
        Sleep 200
    }

    ; 3. Perform Action
    switch action {
        case "move":
            ; Just move (already done above)

        case "left":
            Click "Left"

        case "right":
            Send "{RButton}"

        case "middle":
            Click "Middle"

        case "double":
            Click "Left", 2

        case "hold":
            Click "Down"

        case "release":
            Click "Up"

        case "scroll_up":
            amount := (args.Length >= paramOffset) ? args[paramOffset] : 3
            Click "WheelUp", amount

        case "scroll_down":
            amount := (args.Length >= paramOffset) ? args[paramOffset] : 3
            Click "WheelDown", amount

        case "drag":
            if (args.Length < paramOffset + 1)
                return
            dest_x := args[paramOffset]
            dest_y := args[paramOffset+1]

            Click "Down"
            Sleep 100
            MouseMove dest_x, dest_y, 50
            Sleep 100
            Click "Up"

        default:
            ; Unknown action
    }

    Sleep 100
}