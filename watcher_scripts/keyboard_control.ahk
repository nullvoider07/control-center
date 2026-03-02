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
        Sleep 50
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
    ; CRITICAL: Wait for process to stabilize before sending input
    Sleep 500
    
    switch action {
        case "type":
            SendText payload
            
        case "press":
            Send payload
    }
    
    Sleep 100
}