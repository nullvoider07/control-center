// crates/agent/src/argv_policy.rs
// Deny-by-default validation of the structured argv delivered by the server.
//
// The allow-listed actuation binaries are not themselves safe boundaries: `xdotool
// exec <cmd>` spawns arbitrary processes and `osascript -e` evaluates arbitrary
// AppleScript (including `do shell script`). Checking argv[0] alone therefore leaves
// an `execute`-scoped caller with arbitrary code execution on the guest. Every
// accepted argv form is enumerated below and anything outside the grammar is refused
// before a process is spawned.
//
// The grammar covers exactly what crates/controller/os_specific/*_actuation.py emits.

/// An actuation binary the agent is allowed to spawn.
#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub enum Bin {
    Xdotool,
    Cliclick,
    Osascript,
}

/// A validated action, ready to execute. Construction is only possible via
/// [`validate`], so holding one means the grammar has already been satisfied.
#[derive(Debug, PartialEq, Eq)]
pub enum Plan {
    /// Write `content` to an allow-listed AutoHotkey input file (Windows actuation).
    Write { path: String, content: String },
    /// Spawn `bin` with validated arguments.
    Run { bin: Bin, args: Vec<String> },
    /// macOS scroll: a cliclick focus click followed by an AppleScript key-repeat
    /// loop. Composed by the agent so the client never supplies script text.
    Scroll {
        click: String,
        key_code: u32,
        amount: u32,
    },
}

/// The screen position a command asks the cursor to end at.
///
/// The agent reads the cursor back after actuating and reports it as the command's
/// result. That readback is only trustworthy if something relates it to what was
/// asked for: a read that races the synthetic event returns the *previous* position,
/// and a warp that silently fails to take effect returns the position the cursor
/// never left. Both are reported as authoritative without this.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ExpectedPos {
    pub x: i32,
    pub y: i32,
}

/// A mouse button press or release, for tracking buttons left held.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MouseButton {
    Left,
    Middle,
    Right,
}

impl MouseButton {
    pub fn as_str(&self) -> &'static str {
        match self {
            MouseButton::Left => "left",
            MouseButton::Middle => "middle",
            MouseButton::Right => "right",
        }
    }
}

/// `true` = the button goes down and stays down.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ButtonTransition {
    pub button: MouseButton,
    pub down: bool,
}

/// Coordinates parsed out of a `<prefix>:<x>,<y>` cliclick token.
fn cliclick_token_pos(token: &str) -> Option<ExpectedPos> {
    let (prefix, value) = token.split_once(':')?;
    // `p:` queries the position rather than moving to it, so it predicts nothing.
    if !matches!(prefix, "c" | "rc" | "dc" | "tc" | "mc" | "dd" | "du" | "m" | "dm") {
        return None;
    }
    let (x, y) = value.split_once(',')?;
    Some(ExpectedPos {
        x: x.parse().ok()?,
        y: y.parse().ok()?,
    })
}

impl Plan {
    /// Where this command should leave the cursor, when it says.
    ///
    /// `None` for actions that operate wherever the cursor already is (`here …`,
    /// `c:.`) and for actions that do not move it — those keep a plain readback,
    /// since there is nothing to check it against.
    ///
    /// Reads only tokens the grammar above has already accepted, so it cannot
    /// disagree with validation about what a token means.
    pub fn expected_pos(&self) -> Option<ExpectedPos> {
        match self {
            // Windows carries the coordinate in the watcher-file content
            // ("1022 343 left"), not in argv.
            Plan::Write { path, content } => {
                if !path.eq_ignore_ascii_case(r"C:\mouse_cmd.txt") {
                    return None;
                }
                let mut tokens = content.split_whitespace();
                let x: i32 = tokens.next()?.parse().ok()?;
                let y: i32 = tokens.next()?.parse().ok()?;
                Some(ExpectedPos { x, y })
            }
            Plan::Run { bin, args } => match bin {
                // The last `mousemove X Y` wins, so "mousemove X Y click 1" predicts
                // the click point.
                Bin::Xdotool => args
                    .windows(3)
                    .rev()
                    .find(|w| w[0] == "mousemove")
                    .and_then(|w| {
                        Some(ExpectedPos {
                            x: w[1].parse().ok()?,
                            y: w[2].parse().ok()?,
                        })
                    }),
                // The last positional token wins, so a drag predicts its destination.
                Bin::Cliclick => args.iter().rev().find_map(|t| cliclick_token_pos(t)),
                // Keyboard only.
                Bin::Osascript => None,
            },
            // The focus click is the only pointer movement in a scroll.
            Plan::Scroll { click, .. } => cliclick_token_pos(click),
        }
    }

    /// The button this command presses or releases, if any.
    ///
    /// Derived from the validated argv rather than the caller-supplied
    /// `human_command`: the tracker drives an automatic release at shutdown, so it
    /// must follow what was actually executed, not what a client said it was.
    pub fn button_transition(&self) -> Option<ButtonTransition> {
        match self {
            Plan::Write { path, content } => {
                if !path.eq_ignore_ascii_case(r"C:\mouse_cmd.txt") {
                    return None;
                }
                // The AutoHotkey watcher only drives the left button.
                let action = content.split_whitespace().last()?;
                match action {
                    "hold" => Some(ButtonTransition { button: MouseButton::Left, down: true }),
                    "release" => Some(ButtonTransition { button: MouseButton::Left, down: false }),
                    _ => None,
                }
            }
            // The LAST transition in the command wins. A drag presses and releases
            // within one argv (`dd:… m:… du:…`); taking the first would leave the
            // tracker believing a button is down, which would produce a spurious
            // console warning and, worse, a spurious uncommanded release at shutdown.
            Plan::Run { bin, args } => match bin {
                Bin::Xdotool => {
                    let idx = args.iter().rposition(|a| a == "mousedown" || a == "mouseup")?;
                    let down = args[idx] == "mousedown";
                    // xdotool button numbers: 1 left, 2 middle, 3 right.
                    let button = match args.get(idx + 1).map(|s| s.as_str()) {
                        Some("1") => MouseButton::Left,
                        Some("2") => MouseButton::Middle,
                        Some("3") => MouseButton::Right,
                        _ => return None,
                    };
                    Some(ButtonTransition { button, down })
                }
                // cliclick dd:/du: drive the left button only.
                Bin::Cliclick => args.iter().rev().find_map(|t| {
                    let prefix = t.split_once(':')?.0;
                    match prefix {
                        "dd" => Some(ButtonTransition { button: MouseButton::Left, down: true }),
                        "du" => Some(ButtonTransition { button: MouseButton::Left, down: false }),
                        _ => None,
                    }
                }),
                Bin::Osascript => None,
            },
            Plan::Scroll { .. } => None,
        }
    }
}

const ALLOWED_WRITE_PATHS: &[&str] = &[r"C:\keyboard_cmd.txt", r"C:\mouse_cmd.txt"];

/// AppleScript prefix shared by both accepted osascript templates.
const OSA_PREFIX: &str = "tell application \"System Events\" to ";
const OSA_MODIFIERS: &[&str] = &["command", "option", "control", "shift"];

/// Key codes for the four scroll directions (up/down/left/right arrows).
const SCROLL_KEY_CODES: &[u32] = &[123, 124, 125, 126];
const MAX_SCROLL_AMOUNT: u32 = 1000;

/// Upper bound (ms) on any caller-supplied wait or inter-keystroke delay. The agent
/// serves one command at a time, so an unbounded value would stall all actuation.
const MAX_DELAY_MS: u64 = 60_000;

/// Upper bound (ms) on the summed waits in one command.
///
/// [`MAX_DELAY_MS`] bounds each wait individually, which reads like a bound on the
/// command but is not one: nothing stopped a caller chaining hundreds of them, and
/// 500 `w:60000` tokens validated to more than eight hours of stall. The longest
/// legitimate form is a 16-waypoint drag at the maximum 5000 ms dwell, which is 18
/// waits totalling 90 s, so this leaves headroom over that and nothing else.
const MAX_TOTAL_DELAY_MS: u64 = 120_000;

/// Upper bound (ms) on `xdotool type --delay`. This is a *per-keystroke* delay
/// multiplied by the payload length, so the general per-value bound is far too
/// loose for it. The actuation layer never emits the flag at all.
const MAX_TYPE_DELAY_MS: u64 = 1_000;

/// Upper bound on a repeat count, matching [`MAX_SCROLL_AMOUNT`] on the macOS path.
/// `xdotool click --repeat <n>` had no bound at all, so `here scroll_down 999999999`
/// — a plausible typo — wedged the agent and flooded the display with clicks, while
/// the same action on macOS was refused above 1000.
const MAX_REPEAT: u64 = 1_000;

/// Upper bound on the number of arguments in one command. The longest form the
/// actuation layer emits is a 16-waypoint drag at 37 cliclick tokens.
const MAX_ARGS: usize = 64;

/// Validate an argv vector against the actuation grammar.
pub fn validate(argv: &[String]) -> Result<Plan, String> {
    let bin = argv.first().map(|s| s.as_str()).unwrap_or("");
    if bin.is_empty() {
        return Err("Empty command".to_string());
    }
    let args = &argv[1..];
    if args.len() > MAX_ARGS {
        return Err(format!(
            "Command has {} arguments, more than the {} permitted",
            args.len(),
            MAX_ARGS
        ));
    }

    match bin {
        "__write__" => validate_write(args),
        "__scroll__" => validate_scroll(args),
        "xdotool" => validate_xdotool(args).map(|args| Plan::Run {
            bin: Bin::Xdotool,
            args,
        }),
        "cliclick" => validate_cliclick(args).map(|args| Plan::Run {
            bin: Bin::Cliclick,
            args,
        }),
        "osascript" => validate_osascript(args).map(|args| Plan::Run {
            bin: Bin::Osascript,
            args,
        }),
        other => Err(format!(
            "Command '{}' is not an allowed actuation binary",
            other
        )),
    }
}

fn validate_write(args: &[String]) -> Result<Plan, String> {
    if args.len() != 2 {
        return Err("__write__ requires <path> <content>".to_string());
    }
    if !ALLOWED_WRITE_PATHS.contains(&args[0].as_str()) {
        return Err(format!("Write to '{}' is not permitted", args[0]));
    }
    Ok(Plan::Write {
        path: args[0].clone(),
        content: args[1].clone(),
    })
}

fn validate_scroll(args: &[String]) -> Result<Plan, String> {
    if args.len() != 3 {
        return Err("__scroll__ requires <click-token> <key-code> <amount>".to_string());
    }
    if !is_cliclick_point("c", &args[0]) {
        return Err(format!("Invalid scroll click token: '{}'", args[0]));
    }
    let key_code: u32 = args[1]
        .parse()
        .map_err(|_| format!("Invalid scroll key code: '{}'", args[1]))?;
    if !SCROLL_KEY_CODES.contains(&key_code) {
        return Err(format!("Scroll key code {} is not permitted", key_code));
    }
    let amount: u32 = args[2]
        .parse()
        .map_err(|_| format!("Invalid scroll amount: '{}'", args[2]))?;
    if amount == 0 || amount > MAX_SCROLL_AMOUNT {
        return Err(format!("Scroll amount {} out of range 1..{}", amount, MAX_SCROLL_AMOUNT));
    }
    Ok(Plan::Scroll {
        click: args[0].clone(),
        key_code,
        amount,
    })
}

/// xdotool: dispatch on the sub-command. `exec`, `spawn` and `behave` are excluded by
/// virtue of not appearing in any accepted branch.
fn validate_xdotool(args: &[String]) -> Result<Vec<String>, String> {
    let sub = args.first().map(|s| s.as_str()).unwrap_or("");
    let rest = if args.is_empty() { &args[0..0] } else { &args[1..] };

    match sub {
        // "type" carries free text: the payload is data and is never scanned, but it
        // must be the single trailing argument so it cannot be read as a sub-command.
        //
        // A lone trailing argument is still not enough. xdotool parses type's options
        // with getopt_long, so a payload in `--opt=value` form is read as an option
        // even though it is one argv element — and `type --file=PATH` types the
        // contents of PATH, turning the actuation channel into a file-read primitive.
        // A `--` terminator is inserted below so the payload is always data; verified
        // against xdotool 3.x, including combined with the flags accepted here.
        "type" => {
            let mut i = 0;
            let mut out: Vec<String> = vec!["type".to_string()];
            while i < rest.len() {
                match rest[i].as_str() {
                    "--clearmodifiers" => {
                        out.push(rest[i].clone());
                        i += 1;
                    }
                    "--delay" => {
                        let n = rest.get(i + 1).ok_or("xdotool type: --delay needs a value")?;
                        // Per keystroke, so it multiplies by the payload length.
                        if !matches!(n.parse::<u64>(), Ok(v) if v <= MAX_TYPE_DELAY_MS) {
                            return Err(format!(
                                "xdotool type: --delay '{}' out of range 0..{}",
                                n, MAX_TYPE_DELAY_MS
                            ));
                        }
                        out.push(rest[i].clone());
                        out.push(n.clone());
                        i += 2;
                    }
                    _ => break,
                }
            }
            if rest.len() - i != 1 {
                return Err("xdotool type: expected exactly one text argument".to_string());
            }
            out.push("--".to_string());
            out.push(rest[i].clone());
            Ok(out)
        }
        // Key specs only: no free text, so every token is constrained.
        "key" => {
            if rest.is_empty() {
                return Err("xdotool key: expected at least one key".to_string());
            }
            for t in rest {
                if t == "--clearmodifiers" {
                    continue;
                }
                if !is_keysym(t) {
                    return Err(format!("xdotool key: invalid key spec '{}'", t));
                }
            }
            Ok(args.to_vec())
        }
        // Pointer actions, optionally chained (e.g. "mousemove X Y click 1").
        //
        // `--repeat` is read with its value rather than as a bare token: digits appear
        // here as coordinates, button numbers and repeat counts, and only the repeat
        // count multiplies into an unbounded amount of actuation.
        "getmouselocation" | "click" | "mousemove" | "mousedown" | "mouseup" => {
            let mut i = 0;
            while i < rest.len() {
                let t = rest[i].as_str();
                match t {
                    "--repeat" => {
                        let n = rest
                            .get(i + 1)
                            .ok_or_else(|| format!("xdotool {}: --repeat needs a value", sub))?;
                        if !matches!(n.parse::<u64>(), Ok(v) if (1..=MAX_REPEAT).contains(&v)) {
                            return Err(format!(
                                "xdotool {}: --repeat '{}' out of range 1..{}",
                                sub, n, MAX_REPEAT
                            ));
                        }
                        i += 2;
                    }
                    "--shell" | "--clearmodifiers" | "click" | "mousemove" | "mousedown"
                    | "mouseup" => i += 1,
                    _ if is_digits(t) => i += 1,
                    _ => return Err(format!("xdotool {}: unexpected token '{}'", sub, t)),
                }
            }
            Ok(args.to_vec())
        }
        other => Err(format!("xdotool sub-command '{}' is not permitted", other)),
    }
}

/// cliclick: every argument is a `prefix:value` action token. cliclick has no
/// process-spawning verb, so constraining the token shapes is sufficient.
fn validate_cliclick(args: &[String]) -> Result<Vec<String>, String> {
    if args.is_empty() {
        return Err("cliclick: expected at least one action".to_string());
    }
    let mut total_delay: u64 = 0;
    for t in args {
        let (prefix, value) = t
            .split_once(':')
            .ok_or_else(|| format!("cliclick: invalid action token '{}'", t))?;
        let ok = match prefix {
            // Pointer actions: "." (current position) or explicit coordinates.
            // `dm` is the drag-continuation move: it posts leftMouseDragged where `m`
            // posts mouseMoved, so a drag built from `m` is not seen as a drag at all.
            "p" | "c" | "rc" | "dc" | "tc" | "mc" | "dd" | "du" | "m" | "dm" => {
                is_cliclick_point(prefix, t)
            }
            // Wait, in milliseconds. Accumulated so the per-token bound cannot be
            // sidestepped by chaining waits.
            "w" => match value.parse::<u64>() {
                Ok(n) if n <= MAX_DELAY_MS => {
                    total_delay = total_delay.saturating_add(n);
                    true
                }
                _ => false,
            },
            // Key down / up / press: key names, optionally comma-separated.
            "kd" | "ku" | "kp" => {
                !value.is_empty()
                    && value
                        .chars()
                        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, ',' | '+' | '-'))
            }
            // Typed text: the payload is data.
            "t" => !value.is_empty(),
            _ => false,
        };
        if !ok {
            return Err(format!("cliclick: invalid action token '{}'", t));
        }
    }
    if total_delay > MAX_TOTAL_DELAY_MS {
        return Err(format!(
            "cliclick: waits total {}ms, more than the {}ms permitted for one command",
            total_delay, MAX_TOTAL_DELAY_MS
        ));
    }
    Ok(args.to_vec())
}

/// osascript: only `-e <body>` pairs, each body matching one of the two templates the
/// actuation layer emits. Free-form scripts (and therefore `do shell script`) are
/// rejected.
fn validate_osascript(args: &[String]) -> Result<Vec<String>, String> {
    if args.is_empty() || !args.len().is_multiple_of(2) {
        return Err("osascript: expected -e <script> pairs".to_string());
    }
    for pair in args.chunks(2) {
        if pair[0] != "-e" {
            return Err(format!("osascript: unexpected argument '{}'", pair[0]));
        }
        let body = &pair[1];
        if body.contains('\n') || body.contains('\r') {
            return Err("osascript: multi-line scripts are not permitted".to_string());
        }
        let tail = body
            .strip_prefix(OSA_PREFIX)
            .ok_or_else(|| format!("osascript: script '{}' is not an accepted template", body))?;
        if let Some(literal) = tail.strip_prefix("keystroke ") {
            if !is_applescript_string_literal(literal) {
                return Err("osascript: keystroke argument is not a closed string literal".to_string());
            }
        } else if let Some(spec) = tail.strip_prefix("key code ") {
            if !is_key_code_spec(spec) {
                return Err(format!("osascript: invalid key code spec '{}'", spec));
            }
        } else {
            return Err(format!("osascript: script '{}' is not an accepted template", body));
        }
    }
    Ok(args.to_vec())
}

/// `"…"` where the closing quote is the final character, so nothing can follow the
/// literal. Inner quotes must be backslash-escaped, which is what keeps an injected
/// `do shell script` inside the string rather than beside it.
fn is_applescript_string_literal(s: &str) -> bool {
    let bytes = s.as_bytes();
    if bytes.len() < 2 || bytes[0] != b'"' {
        return false;
    }
    let mut i = 1;
    while i < bytes.len() {
        match bytes[i] {
            b'\\' => i += 2,
            b'"' => return i == bytes.len() - 1,
            _ => i += 1,
        }
    }
    false
}

/// `<digits>` optionally followed by ` using {mod down[, mod down]…}`.
fn is_key_code_spec(spec: &str) -> bool {
    let (code, modifiers) = match spec.split_once(" using ") {
        Some((code, rest)) => (code, Some(rest)),
        None => (spec, None),
    };
    if code.is_empty() || code.len() > 3 || !is_digits(code) {
        return false;
    }
    let Some(modifiers) = modifiers else {
        return true;
    };
    let Some(inner) = modifiers.strip_prefix('{').and_then(|m| m.strip_suffix('}')) else {
        return false;
    };
    if inner.is_empty() {
        return false;
    }
    inner.split(", ").all(|m| {
        m.strip_suffix(" down")
            .is_some_and(|name| OSA_MODIFIERS.contains(&name))
    })
}

/// A cliclick pointer token: `<prefix>:.` or `<prefix>:<x>,<y>`.
fn is_cliclick_point(prefix: &str, token: &str) -> bool {
    let Some(value) = token
        .strip_prefix(prefix)
        .and_then(|rest| rest.strip_prefix(':'))
    else {
        return false;
    };
    if value == "." {
        return true;
    }
    match value.split_once(',') {
        Some((x, y)) => is_signed_int(x) && is_signed_int(y),
        None => false,
    }
}

fn is_digits(s: &str) -> bool {
    !s.is_empty() && s.chars().all(|c| c.is_ascii_digit())
}

fn is_signed_int(s: &str) -> bool {
    let body = s.strip_prefix('-').unwrap_or(s);
    is_digits(body)
}

/// An X keysym as emitted by the Linux actuation layer, e.g. "ctrl+shift+t".
fn is_keysym(s: &str) -> bool {
    !s.is_empty()
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '+'))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn argv(parts: &[&str]) -> Vec<String> {
        parts.iter().map(|s| s.to_string()).collect()
    }

    fn pos_of(parts: &[&str]) -> Option<ExpectedPos> {
        validate(&argv(parts)).expect("should validate").expected_pos()
    }

    fn button_of(parts: &[&str]) -> Option<ButtonTransition> {
        validate(&argv(parts))
            .expect("should validate")
            .button_transition()
    }

    // ---- expected end position ---------------------------------------------
    // The agent compares its cursor readback against this. Getting it wrong in
    // either direction is costly: too permissive and a stale read is reported as
    // real (the bug), too strict and correct commands report no position at all.

    #[test]
    fn expected_pos_reads_the_requested_coordinate() {
        // Linux: the controller emits mousemove alone, or chained with a click.
        assert_eq!(pos_of(&["xdotool", "mousemove", "960", "540"]),
                   Some(ExpectedPos { x: 960, y: 540 }));
        assert_eq!(pos_of(&["xdotool", "mousemove", "960", "540", "click", "1"]),
                   Some(ExpectedPos { x: 960, y: 540 }));
        // macOS pointer actions.
        assert_eq!(pos_of(&["cliclick", "c:960,540"]), Some(ExpectedPos { x: 960, y: 540 }));
        assert_eq!(pos_of(&["cliclick", "m:12,34"]), Some(ExpectedPos { x: 12, y: 34 }));
        assert_eq!(pos_of(&["cliclick", "rc:-5,-9"]), Some(ExpectedPos { x: -5, y: -9 }));
        // Scroll: the focus click is the only movement.
        assert_eq!(pos_of(&["__scroll__", "c:400,300", "125", "5"]),
                   Some(ExpectedPos { x: 400, y: 300 }));
        // Windows carries the coordinate in the watcher-file content.
        assert_eq!(pos_of(&["__write__", r"C:\mouse_cmd.txt", "1022 343 left"]),
                   Some(ExpectedPos { x: 1022, y: 343 }));
    }

    #[test]
    fn drag_predicts_its_destination() {
        // The last positional token wins, so the readback is checked against where
        // the drag ended rather than where it started.
        assert_eq!(
            pos_of(&["cliclick", "dd:100,100", "w:50", "dm:900,700", "w:50", "du:900,700"]),
            Some(ExpectedPos { x: 900, y: 700 })
        );
        assert_eq!(
            pos_of(&[
                "cliclick", "dd:100,100", "w:50", "dm:400,300", "w:50",
                "dm:700,500", "w:50", "dm:900,700", "w:50", "du:900,700",
            ]),
            Some(ExpectedPos { x: 900, y: 700 })
        );
        // A drag-continuation move names a position like any other pointer token.
        assert_eq!(pos_of(&["cliclick", "dm:12,34"]), Some(ExpectedPos { x: 12, y: 34 }));
    }

    #[test]
    fn commands_that_name_no_coordinate_predict_nothing() {
        // "here <action>" acts wherever the cursor is; there is nothing to verify
        // against, so these must keep a plain readback rather than report false.
        assert_eq!(pos_of(&["cliclick", "c:."]), None);
        assert_eq!(pos_of(&["cliclick", "dd:."]), None);
        assert_eq!(pos_of(&["xdotool", "click", "1"]), None);
        assert_eq!(pos_of(&["xdotool", "click", "--repeat", "2", "1"]), None);
        assert_eq!(pos_of(&["__scroll__", "c:.", "125", "5"]), None);
        // A position query reads the cursor, it does not move it.
        assert_eq!(pos_of(&["cliclick", "p:."]), None);
        assert_eq!(pos_of(&["xdotool", "getmouselocation", "--shell"]), None);
        // Keyboard actions never move the pointer.
        assert_eq!(pos_of(&["xdotool", "key", "ctrl+c"]), None);
        assert_eq!(
            pos_of(&["osascript", "-e", "tell application \"System Events\" to key code 36"]),
            None
        );
        // The keyboard watcher file is not a mouse command.
        assert_eq!(pos_of(&["__write__", r"C:\keyboard_cmd.txt", "type hello"]), None);
        // Windows mouse content without coordinates ("here left").
        assert_eq!(pos_of(&["__write__", r"C:\mouse_cmd.txt", "here left"]), None);
        assert_eq!(pos_of(&["__write__", r"C:\mouse_cmd.txt", "position"]), None);
    }

    // ---- held-button tracking ----------------------------------------------
    #[test]
    fn button_transitions_are_read_from_the_executed_argv() {
        assert_eq!(
            button_of(&["cliclick", "dd:900,700"]),
            Some(ButtonTransition { button: MouseButton::Left, down: true })
        );
        assert_eq!(
            button_of(&["cliclick", "du:900,700"]),
            Some(ButtonTransition { button: MouseButton::Left, down: false })
        );
        assert_eq!(
            button_of(&["xdotool", "mousemove", "9", "9", "mousedown", "1"]),
            Some(ButtonTransition { button: MouseButton::Left, down: true })
        );
        assert_eq!(
            button_of(&["xdotool", "mouseup", "3"]),
            Some(ButtonTransition { button: MouseButton::Right, down: false })
        );
        assert_eq!(
            button_of(&["__write__", r"C:\mouse_cmd.txt", "900 700 hold"]),
            Some(ButtonTransition { button: MouseButton::Left, down: true })
        );
        assert_eq!(
            button_of(&["__write__", r"C:\mouse_cmd.txt", "900 700 release"]),
            Some(ButtonTransition { button: MouseButton::Left, down: false })
        );
    }

    #[test]
    fn a_drag_does_not_leave_a_button_held() {
        // A drag presses and releases inside one argv. Reading the first transition
        // would record a hold that never existed, and the agent would then issue an
        // uncommanded release at shutdown for a button already up.
        for drag in [
            vec!["cliclick", "dd:100,100", "w:50", "dm:900,700", "w:50", "du:900,700"],
            vec![
                "cliclick", "dd:100,100", "w:50", "dm:400,300", "w:50",
                "dm:900,700", "w:50", "du:900,700",
            ],
            vec!["xdotool", "mousemove", "1", "1", "mousedown", "1",
                 "mousemove", "9", "9", "mouseup", "1"],
        ] {
            assert_eq!(
                button_of(&drag),
                Some(ButtonTransition { button: MouseButton::Left, down: false }),
                "{:?} ends with the button up",
                drag,
            );
        }
    }

    #[test]
    fn ordinary_actions_hold_no_button() {
        for case in [
            vec!["cliclick", "c:960,540"],
            vec!["xdotool", "mousemove", "960", "540", "click", "1"],
            vec!["xdotool", "key", "ctrl+c"],
            vec!["__write__", r"C:\keyboard_cmd.txt", "type hi"],
            vec!["__write__", r"C:\mouse_cmd.txt", "960 540 left"],
            vec!["__scroll__", "c:400,300", "125", "5"],
        ] {
            assert_eq!(button_of(&case), None, "{:?} should hold no button", case);
        }
    }

    // ---- the escapes that motivated this module ----------------------------

    #[test]
    fn xdotool_exec_is_rejected() {
        for case in [
            argv(&["xdotool", "exec", "/bin/sh", "-c", "id"]),
            argv(&["xdotool", "exec", "--sync", "/bin/sh"]),
            argv(&["xdotool", "spawn", "/bin/sh"]),
            argv(&["xdotool", "behave", "$0", "mouse-enter", "exec", "id"]),
        ] {
            assert!(validate(&case).is_err(), "should reject {:?}", case);
        }
    }

    #[test]
    fn xdotool_type_chained_with_exec_is_rejected() {
        // The payload must be the single trailing argument, so a chained sub-command
        // after the text cannot slip through.
        let case = argv(&["xdotool", "type", "hi", "exec", "/bin/sh"]);
        assert!(validate(&case).is_err());
    }

    #[test]
    fn osascript_do_shell_script_is_rejected() {
        for body in [
            "do shell script \"id\"",
            "tell application \"System Events\" to do shell script \"id\"",
            "tell application \"System Events\" to keystroke \"a\" \ndo shell script \"id\"",
        ] {
            let case = argv(&["osascript", "-e", body]);
            assert!(validate(&case).is_err(), "should reject {:?}", body);
        }
    }

    #[test]
    fn osascript_keystroke_cannot_escape_the_literal() {
        // A payload that closes the literal and appends a statement must be refused.
        let body = "tell application \"System Events\" to keystroke \"a\" \
                    & (do shell script \"id\")";
        assert!(validate(&argv(&["osascript", "-e", body])).is_err());
    }

    #[test]
    fn unknown_binary_is_rejected() {
        assert!(validate(&argv(&["sh", "-c", "id"])).is_err());
        assert!(validate(&argv(&["/usr/bin/xdotool", "type", "hi"])).is_err());
        assert!(validate(&[]).is_err());
    }

    // ---- everything the actuation layer actually emits ---------------------

    #[test]
    fn xdotool_actuation_vocabulary_is_accepted() {
        for case in [
            argv(&["xdotool", "getmouselocation", "--shell"]),
            argv(&["xdotool", "click", "1"]),
            argv(&["xdotool", "click", "--repeat", "5", "4"]),
            argv(&["xdotool", "mousemove", "960", "540"]),
            argv(&["xdotool", "mousemove", "960", "540", "click", "1"]),
            argv(&["xdotool", "mousemove", "10", "20", "click", "--repeat", "2", "1"]),
            argv(&["xdotool", "mousemove", "1", "2", "mousedown", "1", "mousemove", "3", "4", "mouseup", "1"]),
            argv(&["xdotool", "mousedown", "1"]),
            argv(&["xdotool", "mouseup", "1"]),
            argv(&["xdotool", "key", "ctrl+shift+t"]),
            argv(&["xdotool", "key", "Return"]),
        ] {
            assert!(validate(&case).is_ok(), "should accept {:?}", case);
        }
    }

    #[test]
    fn typed_text_is_data_not_syntax() {
        // Shell metacharacters in the payload are accepted and passed through as a
        // single argument — there is no shell for them to act on.
        let case = argv(&["xdotool", "type", "hello$(touch /tmp/pwned)`id`; rm -rf /"]);
        assert_eq!(
            validate(&case).unwrap(),
            Plan::Run {
                bin: Bin::Xdotool,
                args: argv(&["type", "--", "hello$(touch /tmp/pwned)`id`; rm -rf /"]),
            }
        );
    }

    #[test]
    fn typed_text_cannot_become_an_xdotool_option() {
        // `xdotool type --file=PATH` types the contents of PATH. The payload is one
        // argv element, so arity alone does not stop it — the inserted `--` does.
        let case = argv(&["xdotool", "type", "--file=/etc/shadow"]);
        assert_eq!(
            validate(&case).unwrap(),
            Plan::Run {
                bin: Bin::Xdotool,
                args: argv(&["type", "--", "--file=/etc/shadow"]),
            }
        );

        // The terminator goes after any accepted flags, never before them.
        let case = argv(&["xdotool", "type", "--clearmodifiers", "--file=-"]);
        assert_eq!(
            validate(&case).unwrap(),
            Plan::Run {
                bin: Bin::Xdotool,
                args: argv(&["type", "--clearmodifiers", "--", "--file=-"]),
            }
        );

        let case = argv(&["xdotool", "type", "--delay", "12", "-h"]);
        assert_eq!(
            validate(&case).unwrap(),
            Plan::Run {
                bin: Bin::Xdotool,
                args: argv(&["type", "--delay", "12", "--", "-h"]),
            }
        );
    }

    #[test]
    fn xdotool_type_file_option_form_is_still_rejected() {
        // The two-element form fails arity before it can reach the terminator.
        assert!(validate(&argv(&["xdotool", "type", "--file", "/etc/shadow"])).is_err());
        assert!(validate(&argv(&["xdotool", "type", "--delay", "abc", "hi"])).is_err());
        assert!(validate(&argv(&["xdotool", "type"])).is_err());
    }

    #[test]
    fn cliclick_actuation_vocabulary_is_accepted() {
        for case in [
            argv(&["cliclick", "p:."]),
            argv(&["cliclick", "c:."]),
            argv(&["cliclick", "c:960,540"]),
            argv(&["cliclick", "c:-10,-20"]),
            argv(&["cliclick", "rc:."]),
            argv(&["cliclick", "dc:."]),
            argv(&["cliclick", "tc:."]),
            argv(&["cliclick", "mc:."]),
            argv(&["cliclick", "dd:."]),
            argv(&["cliclick", "du:."]),
            argv(&["cliclick", "dm:900,700"]),
            argv(&["cliclick", "c:.", "w:50"]),
            argv(&["cliclick", "kp:return"]),
            argv(&["cliclick", "kd:cmd", "kp:a", "ku:cmd"]),
            argv(&["cliclick", "t:x"]),
        ] {
            assert!(validate(&case).is_ok(), "should accept {:?}", case);
        }
    }

    #[test]
    fn waits_and_delays_are_bounded() {
        // The agent serves one command at a time; an unbounded wait stalls actuation.
        assert!(validate(&argv(&["cliclick", "w:50"])).is_ok());
        assert!(validate(&argv(&["cliclick", "w:60000"])).is_ok());
        assert!(validate(&argv(&["cliclick", "w:60001"])).is_err());
        assert!(validate(&argv(&["cliclick", "w:999999999999999999999"])).is_err());
        assert!(validate(&argv(&["xdotool", "type", "--delay", "12", "hi"])).is_ok());
        assert!(validate(&argv(&["xdotool", "type", "--delay", "600000", "hi"])).is_err());
    }

    #[test]
    fn waits_are_bounded_in_aggregate_not_only_per_token() {
        // The per-token bound reads like a bound on the command but is not one: each
        // of these tokens is individually legal and the run stalls the agent, which
        // serves one command at a time, for as long as the caller cares to make it.
        let mut chained = vec!["cliclick".to_string()];
        for _ in 0..40 {
            chained.push("w:60000".to_string());
        }
        assert!(
            validate(&chained).is_err(),
            "40 legal waits chained to 40 minutes must be refused"
        );

        // Three maximal waits (180 s) is over the limit; two (120 s) is not.
        assert!(validate(&argv(&["cliclick", "w:60000", "w:60000", "w:60000"])).is_err());
        assert!(validate(&argv(&["cliclick", "w:60000", "w:60000"])).is_ok());
    }

    #[test]
    fn the_longest_legitimate_drag_still_validates() {
        // 16 waypoints at the maximum 5000 ms dwell: 18 waits totalling 90 s and 37
        // tokens. The aggregate and argument-count bounds must clear this, or the
        // documented drag grammar would be refused by the agent that serves it.
        let mut drag = vec!["cliclick".to_string(), "dd:0,0".to_string(), "w:5000".to_string()];
        for i in 0..17 {
            drag.push(format!("dm:{},{}", i * 10, i * 10));
            drag.push("w:5000".to_string());
        }
        drag.push("du:160,160".to_string());
        assert_eq!(drag.len() - 1, 37, "argument count drifted from the documented worst case");
        assert!(validate(&drag).is_ok(), "the maximal documented drag was refused");
    }

    #[test]
    fn repeat_counts_are_bounded() {
        // `here scroll_down 999999999` is a plausible typo. Unbounded, it wedged the
        // agent and flooded the display, while the same action on the macOS path was
        // refused above MAX_SCROLL_AMOUNT — the asymmetry this closes.
        assert!(validate(&argv(&["xdotool", "click", "--repeat", "5", "4"])).is_ok());
        assert!(validate(&argv(&["xdotool", "click", "--repeat", "1000", "4"])).is_ok());
        assert!(validate(&argv(&["xdotool", "click", "--repeat", "1001", "4"])).is_err());
        assert!(validate(&argv(&["xdotool", "click", "--repeat", "999999999", "4"])).is_err());
        // Wider than u64, so a parse must reject rather than wrap or truncate.
        assert!(validate(&argv(&["xdotool", "click", "--repeat", "18446744073709551616", "4"])).is_err());
        assert!(validate(&argv(&["xdotool", "click", "--repeat", "0", "4"])).is_err());
        assert!(validate(&argv(&["xdotool", "click", "--repeat", "abc", "4"])).is_err());
        assert!(validate(&argv(&["xdotool", "click", "--repeat"])).is_err());
        // The count still reads correctly when the repeat is chained after a move.
        assert!(validate(&argv(&["xdotool", "mousemove", "10", "20", "click", "--repeat", "2", "1"])).is_ok());
        assert!(validate(&argv(&["xdotool", "mousemove", "10", "20", "click", "--repeat", "5000", "1"])).is_err());
    }

    #[test]
    fn the_argument_count_is_bounded() {
        let mut many = vec!["cliclick".to_string()];
        for _ in 0..100 {
            many.push("c:1,1".to_string());
        }
        assert!(validate(&many).is_err(), "100 pointer actions in one command");
        // The bound is on the argument list, not on a payload's length: typed text is
        // one element however long it is.
        let long_text = "a".repeat(100_000);
        assert!(validate(&argv(&["xdotool", "type", &long_text])).is_ok());
    }

    #[test]
    fn cliclick_rejects_malformed_tokens() {
        for case in [
            argv(&["cliclick", "c:abc"]),
            argv(&["cliclick", "nope"]),
            argv(&["cliclick", "x:1,2"]),
            argv(&["cliclick", "dm:abc"]),
            argv(&["cliclick", "dm:900"]),
            argv(&["cliclick", "w:soon"]),
            argv(&["cliclick"]),
        ] {
            assert!(validate(&case).is_err(), "should reject {:?}", case);
        }
    }

    #[test]
    fn osascript_templates_are_accepted() {
        for body in [
            "tell application \"System Events\" to keystroke \"hello world\"",
            // Escaped quote and backslash inside the literal.
            "tell application \"System Events\" to keystroke \"say \\\"hi\\\" \\\\ ok\"",
            "tell application \"System Events\" to key code 36",
            "tell application \"System Events\" to key code 123 using {command down}",
            "tell application \"System Events\" to key code 51 using {command down, shift down}",
        ] {
            let case = argv(&["osascript", "-e", body]);
            assert!(validate(&case).is_ok(), "should accept {:?}", body);
        }
    }

    #[test]
    fn key_code_reroute_and_passthrough_are_accepted() {
        // Emitted by macos_actuation.py for modified digits/punctuation (⇧⌘4 and
        // friends, which cliclick's t: could never trigger) and for the {code:N}
        // passthrough. These must validate or the fix cannot reach the guest.
        for body in [
            // press #+4 / #+3 / #+5 — screenshot shortcuts
            "tell application \"System Events\" to key code 21 using {command down, shift down}",
            "tell application \"System Events\" to key code 20 using {command down, shift down}",
            "tell application \"System Events\" to key code 23 using {command down, shift down}",
            // press #- / #, — unshifted punctuation
            "tell application \"System Events\" to key code 27 using {command down}",
            "tell application \"System Events\" to key code 43 using {command down}",
            // press {code:N} passthrough, bare and modified, at both bounds
            "tell application \"System Events\" to key code 0",
            "tell application \"System Events\" to key code 127",
            "tell application \"System Events\" to key code 21 using {control down, option down, shift down}",
        ] {
            let case = argv(&["osascript", "-e", body]);
            assert!(validate(&case).is_ok(), "should accept {:?}", body);
        }
    }

    #[test]
    fn drag_token_sequences_are_accepted() {
        // The dwell/waypoint drag forms expand to a longer cliclick token run than
        // the original fixed `dd w dm w du`. The grammar has to accept the whole run.
        //
        // `dm` in particular: the controller emits it for every move inside a drag,
        // because cliclick's `m` posts mouseMoved and nothing tracking a drag reacts
        // to that. Omitting it here fails the drag closed at the agent — "cliclick:
        // invalid action token 'dm:400,350'" — which is how the two halves of this
        // fix are coupled.
        for case in [
            // 100 100 drag 900 700
            argv(&["cliclick", "dd:100,100", "w:50", "dm:900,700", "w:50", "du:900,700"]),
            // 100 100 drag 900 700 dwell 150
            argv(&["cliclick", "dd:100,100", "w:150", "dm:900,700", "w:150", "du:900,700"]),
            // 100 100 drag via 400 300 via 700 500 to 900 700
            argv(&[
                "cliclick", "dd:100,100", "w:50", "dm:400,300", "w:50",
                "dm:700,500", "w:50", "dm:900,700", "w:50", "du:900,700",
            ]),
            // Negative coordinates: a second display left of or above the primary.
            argv(&["cliclick", "dd:-10,-20", "w:50", "dm:-400,-300", "w:50", "du:-400,-300"]),
        ] {
            assert!(validate(&case).is_ok(), "should accept {:?}", case);
        }
    }

    #[test]
    fn osascript_accepts_real_quoted_payloads() {
        // Exactly what macos_actuation.py emits for quote-heavy input. Typed text
        // containing quotes must survive validation, or the grammar would break
        // ordinary typing on macOS — the failure mode the old escaping bug caused.
        for (typed, body) in [
            (r#"say "hi""#, r#"tell application "System Events" to keystroke "say \"hi\"""#),
            ("it's fine", r#"tell application "System Events" to keystroke "it's fine""#),
            (r#"""#, r#"tell application "System Events" to keystroke "\"""#),
            ("'", r#"tell application "System Events" to keystroke "'""#),
            (r#"a"b'c"#, r#"tell application "System Events" to keystroke "a\"b'c""#),
            (r#"he said "it's" done"#,
             r#"tell application "System Events" to keystroke "he said \"it's\" done""#),
            (r"back\slash", r#"tell application "System Events" to keystroke "back\\slash""#),
            (r"trailing\", r#"tell application "System Events" to keystroke "trailing\\""#),
            (r#""quoted phrase""#,
             r#"tell application "System Events" to keystroke "\"quoted phrase\"""#),
        ] {
            let case = argv(&["osascript", "-e", body]);
            assert!(
                validate(&case).is_ok(),
                "typing {:?} must be accepted, but its script was rejected: {:?}",
                typed, body
            );
        }
    }

    #[test]
    fn osascript_rejects_bad_key_code_specs() {
        for body in [
            "tell application \"System Events\" to key code 9999",
            "tell application \"System Events\" to key code abc",
            "tell application \"System Events\" to key code 36 using {evil down}",
            "tell application \"System Events\" to key code 36 using {}",
        ] {
            assert!(validate(&argv(&["osascript", "-e", body])).is_err(), "should reject {:?}", body);
        }
    }

    #[test]
    fn osascript_requires_e_pairs() {
        assert!(validate(&argv(&["osascript", "/tmp/script.scpt"])).is_err());
        assert!(validate(&argv(&[
            "osascript",
            "-e",
            "tell application \"System Events\" to key code 36",
            "extra"
        ]))
        .is_err());
    }

    // ---- sentinels ---------------------------------------------------------

    #[test]
    fn write_sentinel_is_path_constrained() {
        assert!(validate(&argv(&["__write__", r"C:\mouse_cmd.txt", "960 540 left"])).is_ok());
        assert!(validate(&argv(&["__write__", r"C:\Windows\System32\evil.bat", "x"])).is_err());
        assert!(validate(&argv(&["__write__", r"C:\mouse_cmd.txt"])).is_err());
    }

    #[test]
    fn scroll_sentinel_is_bounded() {
        assert_eq!(
            validate(&argv(&["__scroll__", "c:.", "125", "5"])).unwrap(),
            Plan::Scroll { click: "c:.".to_string(), key_code: 125, amount: 5 }
        );
        assert!(validate(&argv(&["__scroll__", "c:960,540", "126", "3"])).is_ok());
        for case in [
            argv(&["__scroll__", "c:.", "36", "5"]),      // non-scroll key code
            argv(&["__scroll__", "c:.", "125", "0"]),     // amount below range
            argv(&["__scroll__", "c:.", "125", "100000"]), // amount above range
            argv(&["__scroll__", "rc:.", "125", "5"]),    // wrong click prefix
            argv(&["__scroll__", "c:.", "125"]),          // arity
        ] {
            assert!(validate(&case).is_err(), "should reject {:?}", case);
        }
    }
}
