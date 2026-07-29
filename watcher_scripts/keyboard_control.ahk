; Keyboard Control (Stabilized)
; Usage 1 (CLI):     AutoHotkey.exe keyboard_control.ahk <action> <content...>
; Usage 2 (Watcher): AutoHotkey.exe keyboard_control.ahk watcher

#SingleInstance Force
SetKeyDelay 50, 50

; --- MODE 1: WATCHER SERVICE ---
if (A_Args.Length > 0 and A_Args[1] = "watcher") {
    Loop {
        if FileExist("C:\keyboard_cmd.txt") {
            try {
                cmdText := FileRead("C:\keyboard_cmd.txt")
                FileDelete "C:\keyboard_cmd.txt"
                
                if (cmdText != "") {
                    splitPos := InStr(cmdText, " ")
                    if (splitPos > 0) {
                        action := SubStr(cmdText, 1, splitPos - 1)
                        payload := Trim(SubStr(cmdText, splitPos + 1), " `t`r`n")
                        ExecuteKeyboard(action, payload)
                    }
                }
            } catch as e {
                ; Ignore errors
            }
        }
        ; Poll interval, so it is also the average latency added to every command
        ; before it is even read. 50ms here cost ~25ms per command for nothing;
        ; FileExist at 100Hz is far cheaper than the position tracker already
        ; running at the same rate in mouse_control.ahk.
        Sleep 10
    }
    ExitApp
}

; --- MODE 2: ONE-SHOT CLI ---
if (A_Args.Length < 2) {
    ExitApp 1
}

; Reconstruct payload from all args after action
action := A_Args[1]
payload := ""
Loop A_Args.Length - 1 {
    idx := A_Index + 1
    payload .= (payload = "" ? "" : " ") . A_Args[idx]
}

ExecuteKeyboard(action, payload)
ExitApp

; --- SHARED LOGIC ---
ExecuteKeyboard(action, payload) {
    ; Settle before sending. This was 500ms on every command, which is a UI-launch
    ; wait applied to every keystroke — including the long runs of ordinary typing
    ; into an already-focused window that make up most of a capture session.
    ;
    ; The launch case is guarded on the controller side, where the knowledge
    ; actually is: windows_actuation.py sleeps after the commands that open a UI
    ; (`press #r`, `press #`, `press {LWin}`, `press !{Tab}`, `press ^+{Esc}`).
    ; Duplicating that here as an unconditional wait taxed every other command to
    ; re-guard a case already handled. What is left is the short pause for input to
    ; be accepted at all.
    Sleep 40
    
    switch action {
        case "type":
            SendText payload
            
        case "press":
            Send payload
    }

    ; Trailing settle. Delays when the watcher picks up the *next* command, so it is
    ; paid by every command in a sequence.
    Sleep 20
}