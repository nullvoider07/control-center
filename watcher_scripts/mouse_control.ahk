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
        ; Poll interval, and so the average latency before a command is even read.
        Sleep 10
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

; Split a modifier prefix off an action token: "#left" -> action "left", with the
; matching down/up Send sequences. The symbols are the same alphabet the keyboard
; watcher uses. A repeated symbol is one key held, and the releases are emitted in
; reverse order: last pressed, first released.
SplitModifiers(token, &downSeq, &upSeq) {
    downMap := Map("^", "{Ctrl down}", "+", "{Shift down}", "!", "{Alt down}", "#", "{LWin down}")
    upMap   := Map("^", "{Ctrl up}",   "+", "{Shift up}",   "!", "{Alt up}",   "#", "{LWin up}")
    downSeq := ""
    upSeq := ""
    seen := Map()
    i := 1
    while (i <= StrLen(token)) {
        ch := SubStr(token, i, 1)
        if (!downMap.Has(ch))
            break
        if (!seen.Has(ch)) {
            seen[ch] := true
            downSeq .= downMap[ch]
            upSeq := upMap[ch] . upSeq
        }
        i++
    }
    return SubStr(token, i)
}

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

        ; Move, then let the pointer settle before the click lands. 200ms was paid
        ; by every coordinate mouse command; the agent re-reads and verifies the
        ; position afterwards, so an occasional early read is corrected rather than
        ; reported.
        if (SmoothEnabled())
            GlideTo(x, y)
        else
            MouseMove x, y, 0
        Sleep 25
    }

    ; 3. Hold any modifiers named by the action's prefix.
    ;
    ; Pressed after the move so the pointer is already in place, and released in a
    ; `finally` so an action that throws cannot leave a modifier physically down —
    ; which would silently turn every later click in the session into a modified one.
    action := SplitModifiers(action, &modDown, &modUp)
    if (modDown != "")
        Send modDown

    try {

    ; 4. Perform Action
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

        ; 5 matches the default in macos_actuation.py and linux_actuation.py. It was
        ; 3 here, so "here scroll_down" with no count scrolled a different distance on
        ; Windows than the same command did everywhere else.
        case "scroll_up":
            amount := (args.Length >= paramOffset) ? args[paramOffset] : 5
            Click "WheelUp", amount

        case "scroll_down":
            amount := (args.Length >= paramOffset) ? args[paramOffset] : 5
            Click "WheelDown", amount

        case "drag":
            if (args.Length < paramOffset + 1)
                return
            dest_x := args[paramOffset]
            dest_y := args[paramOffset+1]

            ; Dwell either side of the drag so the target registers press, motion
            ; and release as one gesture. 50ms matches DEFAULT_DRAG_DWELL_MS in
            ; macos_actuation.py, so a drag dwells the same on both platforms.
            Click "Down"
            Sleep 50
            if (SmoothEnabled())
                GlideTo(dest_x, dest_y)
            else
                MouseMove dest_x, dest_y, 50
            Sleep 50
            Click "Up"

        default:
            ; Unknown action
    }

    } finally {
        if (modUp != "")
            Send modUp
    }

    ; Trailing settle. Delays pickup of the next command, so every command in a
    ; sequence pays it.
    Sleep 20
}


; --- Human-like pointer travel (opt-in: CC_SMOOTH_MOVE=1) ---
;
; Minimum-jerk timing, 10t^3 - 15t^4 + 6t^5: the same curve the Linux portal
; path uses, so a glide has one velocity profile across platforms rather than
; each backend getting whatever its own toolkit happens to call smooth. AHK's
; own `MouseMove x, y, <speed>` interpolation was the cheaper option and is not
; used, precisely because its curve is neither documented nor shared.
;
; Nothing about the command grammar changes: `700 300 move` is the same command,
; recorded the same way, whether it glides or jumps.
SmoothEnabled() {
    return EnvGet("CC_SMOOTH_MOVE") != ""
}

GlideTo(x1, y1) {
    MouseGetPos(&x0, &y0)
    dist := Sqrt((x1 - x0) ** 2 + (y1 - y0) ** 2)
    if (dist <= 1) {
        MouseMove x1, y1, 0
        return
    }

    durMs := 120 + dist * 0.55
    if (durMs > 900)
        durMs := 900

    ; 16ms, not the portal's 8ms. Sleep and A_TickCount both run on the ~15.6ms
    ; system timer tick here, so asking for 8 would sleep 15 anyway and stretch
    ; every glide to twice its intended length while reporting the shorter one.
    steps := Round(durMs / 16)
    if (steps < 2)
        steps := 2

    ; MouseMove otherwise pays SetMouseDelay after every step - 10ms by default,
    ; which is most of a frame added to each of fifty steps.
    prevDelay := A_MouseDelay
    SetMouseDelay -1
    try {
        start := A_TickCount
        Loop steps {
            t := A_Index / steps
            e := t * t * t * (10 + t * (-15 + 6 * t))
            MouseMove Round(x0 + (x1 - x0) * e), Round(y0 + (y1 - y0) * e), 0
            ; Paced against elapsed time, not a fixed sleep per step, so a slow
            ; step costs the next one's wait instead of the movement's length.
            remain := start + durMs * A_Index / steps - A_TickCount
            if (remain > 0)
                Sleep remain
        }
    } finally {
        SetMouseDelay prevDelay
    }
    ; The curve reaches 1.0 in real arithmetic; the float need not. Land exact.
    MouseMove x1, y1, 0
}
