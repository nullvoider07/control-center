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
    /// The Wayland actuation helper. A Wayland compositor refuses to let one
    /// client synthesise input into another, so xdotool reaches XWayland clients
    /// only; the helper speaks the same sub-command language over the
    /// RemoteDesktop portal. The token stream is identical, which is why it
    /// shares `validate_xdotool` rather than carrying a grammar of its own - two
    /// grammars for one language is how the two drift apart.
    CcWayland,
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
        /// Modifiers held for the scroll, as AppleScript `using`-clause elements
        /// ("command down"). Empty when the command carries none.
        ///
        /// They ride on each arrow-key event rather than being posted as a separate
        /// key-down first. A `kd:` would set the modifier globally and depend on a
        /// later `ku:` in a *different* process to clear it: if the repeat loop failed,
        /// the modifier would stay down across every later command with nothing
        /// tracking it. `key code N using {…}` cannot leak that way.
        modifiers: Vec<String>,
    },
    /// macOS middle click, posted as a CGEvent through JXA.
    ///
    /// cliclick has no middle button at all — its whole command set is
    /// `c rc dc tc m dd du dm kd ku kp t w p cp` — so `middle` mapped to a `mc:` token
    /// that does not exist and every middle click on macOS failed with
    /// "Unrecognized action shortcut". macOS itself supports the button natively
    /// (`kCGEventOtherMouseDown` with `kCGMouseButtonCenter`); the limitation was
    /// cliclick's, not the platform's, and Linux and Windows have had `middle` working
    /// all along.
    ///
    /// The agent composes the script from these bounded parameters. The client sends a
    /// click token and a modifier list and never any script text: JXA is a full
    /// scripting language, so accepting it over the wire would be accepting arbitrary
    /// code execution.
    MiddleClick {
        click: String,
        /// CGEventFlags bits for the modifiers held, 0 when none.
        ///
        /// Set on the event itself rather than bracketed with `kd:`/`ku:`, for the
        /// same reason the scroll path rides its modifiers on each key event: a
        /// bracket spanning two processes cannot be closed if the second one dies, and
        /// `check_modifier_bracket` now requires a `kd:` to be closed inside the same
        /// cliclick invocation, so spanning one was never available here.
        flags: u64,
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

/// The verb and trailing arguments of a Windows watcher payload.
///
/// `mouse_control.ahk` accepts `here <verb> [args…]` and `<x> <y> <verb> [args…]`;
/// anything else (a bare `position`) names no verb. The modifier prefix is stripped so
/// `#drag` is still a drag — the same treatment `button_transition` gives a `#hold`.
struct WatcherPayload<'a> {
    /// The point named before the verb, which for every verb but `drag` is where the
    /// command leaves the cursor. `None` for the `here` form, which names no point.
    origin: Option<ExpectedPos>,
    verb: &'a str,
    args: &'a [&'a str],
}

fn parse_watcher_payload<'a>(tokens: &'a [&'a str]) -> Option<WatcherPayload<'a>> {
    let (origin, verb_index) = if tokens.first() == Some(&"here") {
        (None, 1)
    } else {
        let x = tokens.first()?.parse().ok()?;
        let y = tokens.get(1)?.parse().ok()?;
        (Some(ExpectedPos { x, y }), 2)
    };
    Some(WatcherPayload {
        origin,
        verb: tokens.get(verb_index)?.trim_start_matches(['^', '+', '!', '#']),
        args: tokens.get(verb_index + 1..).unwrap_or(&[]),
    })
}

/// The last `<x> <y>` pair in a token list.
fn last_int_pair(tokens: &[&str]) -> Option<ExpectedPos> {
    let y = tokens.last()?.parse().ok()?;
    let x = tokens.get(tokens.len().checked_sub(2)?)?.parse().ok()?;
    Some(ExpectedPos { x, y })
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
                let tokens: Vec<&str> = content.split_whitespace().collect();
                let payload = parse_watcher_payload(&tokens)?;
                if payload.verb == "drag" {
                    // A drag payload names both ends ("<origin> drag <destination>"),
                    // and the destination is where it leaves the cursor. Same rule as
                    // Bin::Cliclick below.
                    //
                    // Taking the leading pair — which is the acting point for every
                    // other verb — meant the re-read loop was satisfied the instant the
                    // cursor reached the START of the gesture. The agent then published
                    // the point the drag left as `position_captured: true`. `here drag`
                    // was worse: naming no leading pair, it predicted nothing and fell
                    // to the "the cursor must not have moved" check, which a drag always
                    // violates and which passed anyway because the read beat the
                    // watcher.
                    return last_int_pair(payload.args);
                }
                payload.origin
            }
            Plan::Run { bin, args } => match bin {
                // The last `mousemove X Y` wins, so "mousemove X Y click 1" predicts
                // the click point. The Wayland helper takes the same argv, so it
                // predicts identically - the backend differs, the grammar does not.
                Bin::Xdotool | Bin::CcWayland => args
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
            // Same shape: the click token is where the button is pressed.
            Plan::MiddleClick { click, .. } => cliclick_token_pos(click),
        }
    }

    /// Whether the cursor reaches `expected_pos` only after actuation this agent does
    /// not wait for.
    ///
    /// The Windows watcher is asynchronous: the agent writes `C:\mouse_cmd.txt` and
    /// returns, `mouse_control.ahk` picks it up on a 10ms poll, and a drag then ends
    /// with a deliberately slow `MouseMove`. Measured on the guest, the cursor arrives
    /// 110-166ms after the write — five runs each at 500px and 1190px, which differ
    /// little because the AutoHotkey move interpolates in fixed steps rather than at a
    /// fixed speed.
    ///
    /// Every other Windows command puts the cursor in place before its click, well
    /// inside the default budget, and the other backends run their binary to
    /// completion before returning. So this is true only for the case that needs it.
    pub fn pointer_arrival_is_deferred(&self) -> bool {
        match self {
            Plan::Write { path, content } => {
                if !path.eq_ignore_ascii_case(r"C:\mouse_cmd.txt") {
                    return false;
                }
                let tokens: Vec<&str> = content.split_whitespace().collect();
                // `drag` always paces the pointer either side of the gesture, and
                // `move` paces it when CC_SMOOTH_MOVE glides it - in both cases the
                // Windows watcher is still moving the cursor after the agent has
                // written the command file and returned, so the readback must wait
                // for it. `move` is included unconditionally rather than gated on the
                // env var: a non-smooth move arrives in ~35ms and the readback loop
                // exits on the first match, paying none of the extra budget, so the
                // only cost of naming it here is a longer wait on a move that never
                // actually lands - which is the case that most needs the honest
                // answer anyway. This is Windows-only by construction: macOS and
                // Linux moves are `Plan::Run`, and their glide is synchronous - the
                // agent blocks until the pointer has arrived before it reads back.
                parse_watcher_payload(&tokens)
                    .is_some_and(|p| p.verb == "drag" || p.verb == "move")
            }
            _ => false,
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
                //
                // The modifier prefix is stripped first: "900 700 #hold" is still a
                // hold, and matching the token whole would return None — leaving a
                // button that is physically down untracked, so the console never warns
                // and the shutdown release never fires for it.
                let action = content
                    .split_whitespace()
                    .last()?
                    .trim_start_matches(['^', '+', '!', '#']);
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
                Bin::Xdotool | Bin::CcWayland => {
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
            // Down and up are posted together in the one command, so nothing is left
            // held for the shutdown release to chase.
            Plan::MiddleClick { .. } => None,
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

/// Keys `xdotool keydown`/`keyup` may hold across a pointer action.
///
/// Only the four modifiers. The chained pointer branch accepts these so a click can
/// carry Ctrl or Shift; admitting the whole keysym space there would turn the mouse
/// path into a second, unaudited way to press any key.
const POINTER_MODIFIERS: &[&str] = &["ctrl", "shift", "alt", "super"];

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
        "__middle__" => validate_middle(args),
        "xdotool" => validate_xdotool(args).map(|args| Plan::Run {
            bin: Bin::Xdotool,
            args,
        }),
        "cc-wayland-actuate" => validate_xdotool(args).map(|args| Plan::Run {
            bin: Bin::CcWayland,
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

/// CGEventFlags bits, from CoreGraphics. Named here rather than passed through from
/// the client so the wire carries a closed set of modifier names, never a bitmask a
/// caller could aim at flags this grammar never intended.
const CG_FLAG_SHIFT: u64 = 0x0002_0000;
const CG_FLAG_CONTROL: u64 = 0x0004_0000;
const CG_FLAG_ALTERNATE: u64 = 0x0008_0000;
const CG_FLAG_COMMAND: u64 = 0x0010_0000;

fn validate_middle(args: &[String]) -> Result<Plan, String> {
    if args.is_empty() || args.len() > 2 {
        return Err("__middle__ requires <click-token> [modifiers]".to_string());
    }
    if !is_cliclick_point("c", &args[0]) {
        return Err(format!("Invalid middle-click token: '{}'", args[0]));
    }
    // A closed enumeration mapped here, matching validate_scroll: only bounded
    // parameters cross the wire, and an unknown name is refused rather than ignored.
    let mut flags = 0u64;
    if let Some(list) = args.get(1) {
        if list.is_empty() {
            return Err("__middle__: empty modifier list".to_string());
        }
        for name in list.split(',') {
            flags |= match name {
                "cmd" => CG_FLAG_COMMAND,
                "alt" => CG_FLAG_ALTERNATE,
                "ctrl" => CG_FLAG_CONTROL,
                "shift" => CG_FLAG_SHIFT,
                _ => return Err(format!("__middle__: '{}' is not a modifier", name)),
            };
        }
    }
    Ok(Plan::MiddleClick { click: args[0].clone(), flags })
}

fn validate_scroll(args: &[String]) -> Result<Plan, String> {
    if args.len() < 3 || args.len() > 4 {
        return Err(
            "__scroll__ requires <click-token> <key-code> <amount> [modifiers]".to_string(),
        );
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
    // A closed enumeration, mapped here rather than carried as script text: the agent
    // authors the AppleScript, and only bounded parameters cross the wire.
    let modifiers = match args.get(3) {
        None => Vec::new(),
        Some(list) => {
            let mut out: Vec<String> = Vec::new();
            for name in list.split(',') {
                let clause = match name {
                    "cmd" => "command down",
                    "alt" => "option down",
                    "ctrl" => "control down",
                    "shift" => "shift down",
                    _ => return Err(format!("__scroll__: '{}' is not a modifier", name)),
                };
                if !out.iter().any(|c| c == clause) {
                    out.push(clause.to_string());
                }
            }
            out
        }
    };

    Ok(Plan::Scroll {
        click: args[0].clone(),
        key_code,
        amount,
        modifiers,
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
        //
        // `keydown`/`keyup` hold a modifier across the pointer action. Their key
        // argument is restricted to POINTER_MODIFIERS: this branch exists so a click
        // can be modified, not so any key can be pressed through the pointer path.
        // Verified against xdotool 3.x on an Xvfb display, where the resulting
        // ButtonPress carries state 0x4 (ControlMask) for `keydown ctrl … click 1`.
        //
        // That the release "cannot be lost because it is one invocation" is a property
        // of the argv, not of this branch admitting the verbs — a bare `keydown ctrl`
        // is also one invocation and strands the modifier on the X server. The pairing
        // is enforced by `check_pointer_modifier_bracket` at the end of this arm.
        "getmouselocation" | "click" | "mousemove" | "mousedown" | "mouseup"
        | "keydown" | "keyup" => {
            let mut i = 0;
            // The sub-command's own key argument, when the chain opens with a hold.
            if matches!(sub, "keydown" | "keyup") {
                let k = rest
                    .first()
                    .ok_or_else(|| format!("xdotool {}: expected a key", sub))?;
                if !POINTER_MODIFIERS.contains(&k.as_str()) {
                    return Err(format!(
                        "xdotool {}: '{}' may not be held for a pointer action",
                        sub, k
                    ));
                }
                i = 1;
            }
            while i < rest.len() {
                let t = rest[i].as_str();
                match t {
                    "keydown" | "keyup" => {
                        let k = rest.get(i + 1).ok_or_else(|| {
                            format!("xdotool {}: {} needs a key", sub, t)
                        })?;
                        if !POINTER_MODIFIERS.contains(&k.as_str()) {
                            return Err(format!(
                                "xdotool {}: '{}' may not be held for a pointer action",
                                sub, k
                            ));
                        }
                        i += 2;
                    }
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
            check_pointer_modifier_bracket(args)?;
            Ok(args.to_vec())
        }
        other => Err(format!("xdotool sub-command '{}' is not permitted", other)),
    }
}

/// Require a held modifier to be released inside the same xdotool invocation.
///
/// `xdotool keydown ctrl` sets the modifier on the X server, not on the process: it
/// stays down after the command completes and after the process exits, and every later
/// keystroke and click in that session is reinterpreted through it. Measured on an Xvfb
/// display before this check existed: a `KeyPress` read `state=0x0`, then a bare
/// `keydown ctrl` was accepted, and every subsequent key from a *separate* command read
/// `state=0x4` (ControlMask) — including after the holding process had exited.
///
/// This is the same defect as the cliclick `kd:`/`ku:` stranding that
/// [`check_modifier_bracket`] guards, on the backend the modifier work opened up.
/// `linux_actuation.py`'s `out()` does emit both halves and says so — "no argv can
/// carry a keydown without its keyup" — but that is a property of the builder, and the
/// builder is not the boundary. This validates argv from any caller holding the execute
/// scope. The original finding had exactly this shape: `macos_actuation.py`'s `emit()`
/// made the same true-of-itself claim while the agent checked nothing.
///
/// The accepted shape is the one the builder emits: every `keydown` in a leading run,
/// every `keyup` in a trailing run, the same modifiers in each, and at least one
/// pointer action bracketed between them.
fn check_pointer_modifier_bracket(args: &[String]) -> Result<(), String> {
    let key_at = |i: usize, verb: &str| -> Result<&str, String> {
        args.get(i + 1)
            .map(String::as_str)
            .ok_or_else(|| format!("xdotool: {} needs a key", verb))
    };

    let mut i = 0;
    let mut downs: Vec<&str> = Vec::new();
    while i < args.len() && args[i] == "keydown" {
        downs.push(key_at(i, "keydown")?);
        i += 2;
    }
    let action_start = i;
    while i < args.len() && args[i] != "keydown" && args[i] != "keyup" {
        i += 1;
    }
    let action_end = i;
    let mut ups: Vec<&str> = Vec::new();
    while i < args.len() && args[i] == "keyup" {
        ups.push(key_at(i, "keyup")?);
        i += 2;
    }

    // Structure first, and before the "no modifiers here" exit. A keydown sitting in
    // the INTERIOR of the chain leaves both runs empty at this point, so exiting early
    // on that emptiness would wave through exactly the shape being guarded against.
    if i != args.len() {
        return Err(
            "xdotool: a modifier hold must open the chain and close it, with the \
             pointer action between"
                .to_string(),
        );
    }
    if downs.is_empty() && ups.is_empty() {
        return Ok(());
    }
    if action_end == action_start {
        return Err("xdotool: a modifier hold must bracket at least one action".to_string());
    }
    let (mut held, mut released) = (downs.clone(), ups.clone());
    held.sort_unstable();
    released.sort_unstable();
    if held != released {
        // Releasing a different modifier strands what was actually held, so this is
        // rejected as firmly as releasing nothing — the same reasoning as the cliclick
        // bracket, where "there is a keyup somewhere" would have admitted it.
        let released_names = if ups.is_empty() { "none".to_string() } else { ups.join(",") };
        return Err(format!(
            "xdotool: modifiers held ({}) are not the ones released ({})",
            downs.join(","),
            released_names
        ));
    }
    Ok(())
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
            // `mc` is deliberately absent: cliclick has no middle-click action, so a
            // policy that accepted it was claiming a vocabulary the binary does not
            // have, and every such command failed at execution. Middle click goes
            // through the __middle__ sentinel instead.
            "p" | "c" | "rc" | "dc" | "tc" | "dd" | "du" | "m" | "dm" => {
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
    check_modifier_bracket(args)?;
    Ok(args.to_vec())
}

/// The modifiers a `kd:`/`ku:` token names, deduplicated and ordered so two tokens
/// naming the same set compare equal regardless of how they were written.
fn bracket_modifiers(token: &str) -> Vec<&str> {
    let mut names: Vec<&str> = token
        .split_once(':')
        .map_or("", |(_, v)| v)
        .split(',')
        .filter(|s| !s.is_empty())
        .collect();
    names.sort_unstable();
    names.dedup();
    names
}

/// Require a modifier hold to be balanced inside the one invocation.
///
/// `kd:` sets the modifier globally: it stays down until some later `ku:` clears it,
/// across every subsequent command and the rest of the login session. So an argv
/// carrying a `kd:` without the matching `ku:` does not fail — it succeeds, and leaves
/// the machine reinterpreting later clicks and keystrokes through a modifier nothing
/// is tracking. Measured on the macOS guest before this check existed:
/// `cliclick kd:cmd c:770,310` was accepted, and Cmd was still held after the command
/// completed and after the process exited.
///
/// The controller can only emit both halves or neither (`emit()` in
/// macos_actuation.py brackets the whole gesture), but the controller is not the
/// boundary — this validates argv from any caller holding the execute scope, and the
/// invariant has to hold where it is enforced rather than where it is intended.
///
/// The accepted shape is the one the builder emits: `kd:` first, `ku:` last, naming
/// the same modifiers, with at least one action between them. `ku:` naming a
/// *different* modifier is rejected too — it releases something else and strands what
/// was actually held, which a "there is a ku: somewhere" check would admit.
fn check_modifier_bracket(args: &[String]) -> Result<(), String> {
    let idx = |p: &str| -> Vec<usize> {
        args.iter()
            .enumerate()
            .filter(|(_, t)| t.starts_with(p))
            .map(|(i, _)| i)
            .collect()
    };
    let downs = idx("kd:");
    let ups = idx("ku:");

    if downs.is_empty() && ups.is_empty() {
        return Ok(());
    }
    if downs.len() != 1 || ups.len() != 1 {
        return Err(format!(
            "cliclick: a modifier hold needs exactly one kd: and one ku:, got {} and {}",
            downs.len(),
            ups.len()
        ));
    }
    let (down, up) = (downs[0], ups[0]);
    if down != 0 {
        return Err("cliclick: kd: must be the first token of a modifier hold".to_string());
    }
    if up != args.len() - 1 {
        return Err("cliclick: ku: must be the last token of a modifier hold".to_string());
    }
    if up <= down + 1 {
        return Err("cliclick: a modifier hold must bracket at least one action".to_string());
    }
    if bracket_modifiers(&args[down]) != bracket_modifiers(&args[up]) {
        return Err(format!(
            "cliclick: '{}' is not released by '{}'",
            args[down], args[up]
        ));
    }
    Ok(())
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
    // The Wayland helper shares xdotool's grammar, so the property worth pinning is
    // agreement, not a second copy of the rules: whatever one accepts the other
    // accepts, and both refuse the same argv.
    // A drag with waypoints is a longer pointer chain than any command produced
    // before it. The policy has to accept it, `expected_pos` has to name the
    // destination rather than the first waypoint, and the tracker has to see the
    // release - a drag that reads as still-held would provoke a spurious
    // uncommanded release when the session ends.
    #[test]
    fn a_waypoint_drag_is_accepted_and_reads_as_its_destination() {
        let argv: Vec<String> = [
            "cc-wayland-actuate", "mousemove", "400", "300", "mousedown", "1",
            "mousemove", "500", "400", "mousemove", "650", "500",
            "mousemove", "800", "600", "mouseup", "1",
        ]
        .iter()
        .map(|s| s.to_string())
        .collect();

        let plan = validate(&argv).expect("waypoint drag must be accepted");
        assert_eq!(
            plan.expected_pos(),
            Some(ExpectedPos { x: 800, y: 600 }),
            "the destination is the last mousemove, not the first waypoint"
        );
        let t = plan.button_transition().expect("a drag names a button transition");
        assert!(!t.down, "a drag must not read as leaving a button held");
        assert_eq!(t.button, MouseButton::Left);

        // The same chain under xdotool, since the two share one grammar.
        let mut x = argv.clone();
        x[0] = "xdotool".to_string();
        assert!(validate(&x).is_ok());
    }

    #[test]
    fn a_modifier_held_waypoint_drag_is_still_bracketed() {
        let argv: Vec<String> = [
            "cc-wayland-actuate", "keydown", "ctrl", "mousemove", "400", "300",
            "mousedown", "1", "mousemove", "500", "400", "mousemove", "800", "600",
            "mouseup", "1", "keyup", "ctrl",
        ]
        .iter()
        .map(|s| s.to_string())
        .collect();
        assert!(validate(&argv).is_ok(), "a bracketed modifier must survive waypoints");
    }

    #[test]
    fn cc_wayland_accepts_exactly_what_xdotool_accepts() {
        let cases: &[&[&str]] = &[
            &["mousemove", "400", "300"],
            &["mousemove", "400", "300", "click", "1"],
            &["keydown", "ctrl", "mousemove", "1", "2", "click", "1", "keyup", "ctrl"],
            &["click", "--repeat", "5", "5"],
            &["mousedown", "1"],
            &["mouseup", "1"],
            &["getmouselocation", "--shell"],
            &["key", "ctrl+c"],
            &["type", "hello  world"],
            // refused on both
            &["exec", "/bin/sh"],
            &["spawn", "xterm"],
            &["behave", "1", "mouse-enter", "exec", "sh"],
            &["key"],
            &["type"],
        ];
        for case in cases {
            let x: Vec<String> = std::iter::once("xdotool".to_string())
                .chain(case.iter().map(|s| s.to_string()))
                .collect();
            let w: Vec<String> = std::iter::once("cc-wayland-actuate".to_string())
                .chain(case.iter().map(|s| s.to_string()))
                .collect();
            let xr = validate(&x);
            let wr = validate(&w);
            assert_eq!(
                xr.is_ok(),
                wr.is_ok(),
                "disagreement on {:?}: xdotool={:?} cc-wayland={:?}",
                case,
                xr,
                wr
            );
            if let (Ok(Plan::Run { bin: xb, args: xa }), Ok(Plan::Run { bin: wb, args: wa })) =
                (&xr, &wr)
            {
                assert_eq!(xa, wa, "validated args differ for {:?}", case);
                assert_eq!(*xb, Bin::Xdotool);
                assert_eq!(*wb, Bin::CcWayland);
            }
        }
    }

    #[test]
    fn an_unlisted_binary_is_still_refused() {
        let argv = vec!["cc-wayland".to_string(), "mousemove".to_string()];
        assert!(validate(&argv).is_err(), "a near-miss name must not be accepted");
    }

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

    fn deferred(parts: &[&str]) -> bool {
        validate(&argv(parts))
            .expect("should validate")
            .pointer_arrival_is_deferred()
    }

    #[test]
    fn a_windows_drag_predicts_its_destination_not_its_origin() {
        // The watcher payload names both ends. Taking the leading pair — the acting
        // point for every other verb — satisfied the re-read loop the moment the cursor
        // reached the START of the gesture, so the agent published the point the drag
        // left and reported position_captured=true. Measured on the guest:
        // "500 400 drag 900 700" reported (500, 400) while the cursor ended at (900, 700).
        assert_eq!(
            pos_of(&["__write__", r"C:\mouse_cmd.txt", "500 400 drag 900 700"]),
            Some(ExpectedPos { x: 900, y: 700 })
        );
        // `here drag` names no leading pair, so it predicted nothing and fell to the
        // "the cursor must not have moved" check — which a drag always violates, and
        // which passed anyway because the readback beat the watcher. It reported the
        // pre-drag position as captured.
        assert_eq!(
            pos_of(&["__write__", r"C:\mouse_cmd.txt", "here drag 700 500"]),
            Some(ExpectedPos { x: 700, y: 500 })
        );
        // A held modifier does not stop it being a drag.
        assert_eq!(
            pos_of(&["__write__", r"C:\mouse_cmd.txt", "500 400 #drag 900 700"]),
            Some(ExpectedPos { x: 900, y: 700 })
        );
        assert_eq!(
            pos_of(&["__write__", r"C:\mouse_cmd.txt", "here ^+drag 700 500"]),
            Some(ExpectedPos { x: 700, y: 500 })
        );
    }

    #[test]
    fn every_other_windows_verb_still_predicts_the_point_it_names() {
        // The drag rule must not disturb the verbs whose coordinate is where they act.
        assert_eq!(
            pos_of(&["__write__", r"C:\mouse_cmd.txt", "770 310 left"]),
            Some(ExpectedPos { x: 770, y: 310 })
        );
        assert_eq!(
            pos_of(&["__write__", r"C:\mouse_cmd.txt", "770 310 #scroll_down 3"]),
            Some(ExpectedPos { x: 770, y: 310 })
        );
        assert_eq!(
            pos_of(&["__write__", r"C:\mouse_cmd.txt", "900 700 ^+hold"]),
            Some(ExpectedPos { x: 900, y: 700 })
        );
        // `here` forms other than drag still predict nothing: they act wherever the
        // cursor is and genuinely do not move it, so the before/after check is sound.
        assert_eq!(pos_of(&["__write__", r"C:\mouse_cmd.txt", "here left"]), None);
        assert_eq!(pos_of(&["__write__", r"C:\mouse_cmd.txt", "here !scroll_up"]), None);
        assert_eq!(pos_of(&["__write__", r"C:\mouse_cmd.txt", "position"]), None);
    }

    #[test]
    fn only_the_windows_drag_waits_longer_for_the_cursor_to_arrive() {
        // The longer re-read budget exists for the two verbs the Windows watcher
        // paces the pointer through asynchronously: a `drag` always, and a `move`
        // when CC_SMOOTH_MOVE glides it. In both, the slow MouseMove lands well after
        // the write - up to the ~900ms glide cap. Every other command is in place
        // inside the default budget, and widening it for them would charge a wait
        // they do not need.
        assert!(deferred(&["__write__", r"C:\mouse_cmd.txt", "500 400 drag 900 700"]));
        assert!(deferred(&["__write__", r"C:\mouse_cmd.txt", "here drag 700 500"]));
        assert!(deferred(&["__write__", r"C:\mouse_cmd.txt", "500 400 #drag 900 700"]));
        // A move: a smooth one glides asynchronously, and the readback must wait for
        // it. A non-smooth move lands in ~35ms and the loop exits on the first match,
        // so naming it here costs nothing on that path.
        assert!(deferred(&["__write__", r"C:\mouse_cmd.txt", "770 310 move"]));
        assert!(deferred(&["__write__", r"C:\mouse_cmd.txt", "770 310 #move"]));

        assert!(!deferred(&["__write__", r"C:\mouse_cmd.txt", "770 310 left"]));
        assert!(!deferred(&["__write__", r"C:\mouse_cmd.txt", "here left"]));
        assert!(!deferred(&["__write__", r"C:\mouse_cmd.txt", "position"]));
        assert!(!deferred(&["__write__", r"C:\keyboard_cmd.txt", "type hello"]));
        // The other backends run their binary to completion before returning: their
        // glide is synchronous, so the pointer has already arrived by the readback.
        assert!(!deferred(&["cliclick", "dd:100,100", "w:50", "du:900,700"]));
        assert!(!deferred(&["cliclick", "m:900,700"]));
        assert!(!deferred(&["xdotool", "mousemove", "900", "700", "click", "1"]));
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
        // A modifier-held hold is still a hold. Matching the prefixed token whole
        // returned None, so the button went untracked: no console warning while it was
        // down, and no automatic release when the session ended.
        assert_eq!(
            button_of(&["__write__", r"C:\mouse_cmd.txt", "900 700 #hold"]),
            Some(ButtonTransition { button: MouseButton::Left, down: true })
        );
        assert_eq!(
            button_of(&["__write__", r"C:\mouse_cmd.txt", "900 700 ^+release"]),
            Some(ButtonTransition { button: MouseButton::Left, down: false })
        );
    }

    #[test]
    fn a_pointer_chain_may_hold_a_modifier() {
        // The Linux form. Verified end-to-end on an Xvfb display: each of these argvs
        // exits 0 and the resulting ButtonPress carries the modifier — state 0x5 for
        // ctrl+shift, 0x8 for alt, 0x4 on the three button-5 events of a ctrl-scroll.
        for parts in [
            vec!["xdotool", "keydown", "ctrl", "mousemove", "770", "310", "click", "1",
                 "keyup", "ctrl"],
            vec!["xdotool", "keydown", "ctrl", "keydown", "shift", "mousemove", "770",
                 "310", "click", "1", "keyup", "shift", "keyup", "ctrl"],
            vec!["xdotool", "keydown", "alt", "click", "--repeat", "2", "1", "keyup", "alt"],
            vec!["xdotool", "keydown", "ctrl", "mousemove", "200", "300", "mousedown", "1",
                 "mousemove", "900", "640", "mouseup", "1", "keyup", "ctrl"],
            vec!["xdotool", "keydown", "super", "mousemove", "770", "310", "click",
                 "--repeat", "3", "5", "keyup", "super"],
        ] {
            validate(&argv(&parts))
                .unwrap_or_else(|e| panic!("{parts:?} was refused: {e}"));
        }

        // The hold must not become a second way to press arbitrary keys through the
        // pointer path, and it must not be left dangling without a key.
        for parts in [
            vec!["xdotool", "keydown", "Return", "click", "1", "keyup", "Return"],
            vec!["xdotool", "keydown", "a", "click", "1", "keyup", "a"],
            vec!["xdotool", "keydown", "ctrl+shift", "click", "1", "keyup", "ctrl+shift"],
            vec!["xdotool", "click", "1", "keydown"],
            vec!["xdotool", "keydown"],
        ] {
            assert!(validate(&argv(&parts)).is_err(), "should reject {parts:?}");
        }
    }

    #[test]
    fn a_held_modifier_does_not_disturb_what_the_argv_reports() {
        // The keydown/keyup tokens sit around the pointer chain, so the position the
        // readback is checked against and the button the tracker follows must both
        // still be found.
        let click = ["xdotool", "keydown", "ctrl", "mousemove", "770", "310", "click", "1",
                     "keyup", "ctrl"];
        assert_eq!(pos_of(&click), Some(ExpectedPos { x: 770, y: 310 }));
        assert_eq!(button_of(&click), None);

        let hold = ["xdotool", "keydown", "ctrl", "mousemove", "770", "310", "mousedown", "1",
                    "keyup", "ctrl"];
        assert_eq!(pos_of(&hold), Some(ExpectedPos { x: 770, y: 310 }));
        assert_eq!(
            button_of(&hold),
            Some(ButtonTransition { button: MouseButton::Left, down: true })
        );

        let drag = ["xdotool", "keydown", "ctrl", "mousemove", "200", "300", "mousedown", "1",
                    "mousemove", "900", "640", "mouseup", "1", "keyup", "ctrl"];
        assert_eq!(pos_of(&drag), Some(ExpectedPos { x: 900, y: 640 }));
        assert_eq!(
            button_of(&drag),
            Some(ButtonTransition { button: MouseButton::Left, down: false })
        );
    }

    #[test]
    fn a_modifier_held_pointer_action_is_accepted_and_still_reads_as_one() {
        // The controller brackets the pointer tokens with kd:/ku: to hold a modifier
        // across the gesture. The bracket must not disturb what the agent derives from
        // the argv: the position it verifies the readback against, and the button it
        // tracks for the shutdown release. A `ku:` token appears *after* the pointer
        // token, so both are read by scanning back past it.
        for (argv_parts, pos, button) in [
            (
                vec!["cliclick", "kd:cmd", "c:770,310", "ku:cmd"],
                Some(ExpectedPos { x: 770, y: 310 }),
                None,
            ),
            (
                vec!["cliclick", "kd:cmd,shift", "dd:770,310", "ku:cmd,shift"],
                Some(ExpectedPos { x: 770, y: 310 }),
                Some(ButtonTransition { button: MouseButton::Left, down: true }),
            ),
            (
                vec![
                    "cliclick", "kd:alt", "dd:200,300", "w:50", "dm:900,640", "w:50",
                    "du:900,640", "ku:alt",
                ],
                Some(ExpectedPos { x: 900, y: 640 }),
                Some(ButtonTransition { button: MouseButton::Left, down: false }),
            ),
        ] {
            validate(&argv(&argv_parts))
                .unwrap_or_else(|e| panic!("{argv_parts:?} was refused: {e}"));
            assert_eq!(pos_of(&argv_parts), pos, "{argv_parts:?}");
            assert_eq!(button_of(&argv_parts), button, "{argv_parts:?}");
        }
    }

    #[test]
    fn cliclick_is_not_offered_a_middle_click_it_cannot_perform() {
        // The vocabulary test above once listed `mc:.` as accepted. cliclick has no
        // such action -- the policy was vouching for a token the binary rejects at
        // execution, which is how `middle` looked implemented on macOS for so long.
        assert!(validate(&argv(&["cliclick", "mc:."])).is_err());
        assert!(validate(&argv(&["cliclick", "mc:960,540"])).is_err());
    }

    #[test]
    fn a_middle_click_is_accepted_and_carries_only_bounded_parameters() {
        // `middle` mapped to a cliclick `mc:` token that does not exist — cliclick has
        // no middle button — so every middle click on macOS failed at execution with
        // "Unrecognized action shortcut". It is posted as a CGEvent now, and the only
        // things crossing the wire are a click token and modifier names.
        assert_eq!(
            validate(&argv(&["__middle__", "c:960,540"])).unwrap(),
            Plan::MiddleClick { click: "c:960,540".to_string(), flags: 0 },
        );
        assert_eq!(
            validate(&argv(&["__middle__", "c:."])).unwrap(),
            Plan::MiddleClick { click: "c:.".to_string(), flags: 0 },
        );
        // Modifier names map to CGEventFlags here, so a caller cannot hand over a raw
        // bitmask aimed at flags this grammar never intended.
        assert_eq!(
            validate(&argv(&["__middle__", "c:960,540", "cmd"])).unwrap(),
            Plan::MiddleClick { click: "c:960,540".to_string(), flags: 0x0010_0000 },
        );
        assert_eq!(
            validate(&argv(&["__middle__", "c:.", "ctrl,shift"])).unwrap(),
            Plan::MiddleClick { click: "c:.".to_string(), flags: 0x0004_0000 | 0x0002_0000 },
        );

        // The click point predicts where the button goes down, so the readback has
        // something to verify against; `c:.` names no point, as with scroll.
        assert_eq!(
            pos_of(&["__middle__", "c:960,540"]),
            Some(ExpectedPos { x: 960, y: 540 })
        );
        assert_eq!(pos_of(&["__middle__", "c:."]), None);
        // Down and up are posted together, so nothing is left held.
        assert_eq!(button_of(&["__middle__", "c:960,540"]), None);

        for bad in [
            vec!["__middle__"],                                  // no click token
            vec!["__middle__", "rc:960,540"],                    // wrong click prefix
            vec!["__middle__", "c:960,540", "bogus"],            // unknown modifier
            vec!["__middle__", "c:960,540", ""],                 // empty modifier list
            vec!["__middle__", "c:960,540", "cmd shift"],        // space-separated
            vec!["__middle__", "c:960,540", "cmd", "extra"],     // arity
            // A raw flag bitmask is not a modifier name and must not be accepted.
            vec!["__middle__", "c:960,540", "1048576"],
        ] {
            assert!(
                validate(&argv(&bad)).is_err(),
                "{bad:?} must be refused",
            );
        }
    }

    #[test]
    fn a_modifier_hold_that_is_not_closed_is_refused() {
        // Each of these was accepted by the policy and executed on the macOS guest,
        // leaving Cmd physically held after the command finished — verified by reading
        // NSEvent.modifierFlags in the guest, which reported CMD down until it was
        // cleared out of band. A stranded modifier does not announce itself: it
        // silently re-reads every later click and keystroke as a chord.
        for (bad, why) in [
            (vec!["cliclick", "kd:cmd", "c:770,310"], "kd: with no ku:"),
            (vec!["cliclick", "kd:cmd"], "kd: alone, no action and no ku:"),
            (vec!["cliclick", "c:770,310", "ku:cmd"], "ku: with no kd:"),
            // The case a "there is a ku: somewhere" check would admit: shift is
            // released, cmd is left held.
            (vec!["cliclick", "kd:cmd", "c:770,310", "ku:shift"], "ku: releases a different modifier"),
            (vec!["cliclick", "kd:cmd,shift", "c:770,310", "ku:cmd"], "ku: releases only part of the set"),
            (vec!["cliclick", "kd:cmd", "c:1,2", "kd:shift", "c:3,4", "ku:cmd"], "two kd: tokens"),
            (vec!["cliclick", "kd:cmd", "ku:cmd"], "brackets no action at all"),
            (vec!["cliclick", "c:1,2", "kd:cmd", "c:3,4", "ku:cmd"], "kd: is not first"),
            (vec!["cliclick", "kd:cmd", "c:1,2", "ku:cmd", "c:3,4"], "ku: is not last"),
        ] {
            assert!(
                validate(&argv(&bad)).is_err(),
                "{why}: {bad:?} must be refused — it strands a modifier",
            );
        }
    }

    #[test]
    fn the_bracketed_forms_the_controller_emits_are_still_accepted() {
        // The guard must not be tightened into rejecting real traffic. These are the
        // shapes macos_actuation.py's emit() produces, plus the unbracketed forms.
        for good in [
            vec!["cliclick", "kd:cmd", "c:770,310", "ku:cmd"],
            vec!["cliclick", "kd:cmd,shift", "c:770,310", "ku:cmd,shift"],
            // Same set, written in the other order: it releases everything held, so it
            // is balanced and must not be refused on spelling.
            vec!["cliclick", "kd:cmd,shift", "c:770,310", "ku:shift,cmd"],
            vec![
                "cliclick", "kd:alt", "dd:200,300", "w:50", "dm:900,640", "w:50",
                "du:900,640", "ku:alt",
            ],
            // The keyboard path brackets too — `press #k` and friends — so the guard
            // covers more than the mouse work that prompted it. These are the shapes
            // macos_actuation.py emits at 832/840/848/855.
            vec!["cliclick", "kd:cmd", "kp:a", "ku:cmd"],
            vec!["cliclick", "kd:cmd", "t:h", "ku:cmd"],
            vec!["cliclick", "kd:cmd", "kp:space", "ku:cmd"],
            // A modifier bracketing nothing but a wait. "At least one action between"
            // has to keep meaning "any token", or this legitimate command breaks.
            vec!["cliclick", "kd:cmd", "w:50", "ku:cmd"],
            vec!["cliclick", "c:770,310"],
            vec!["cliclick", "p:."],
        ] {
            validate(&argv(&good))
                .unwrap_or_else(|e| panic!("{good:?} is emitted by the controller but was refused: {e}"));
        }
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
            // The corpus command the "no quotes in a type command" workaround exists
            // to avoid: quotes, an escaped newline and a redirection in one payload.
            // Paired with test_actuation_argv.py's QUOTED_PAYLOADS, which asserts
            // macos_actuation.py emits exactly these scripts.
            (r#"printf "Title\nBody" > note.txt"#,
             r#"tell application "System Events" to keystroke "printf \"Title\\nBody\" > note.txt""#),
            (r#"osascript -e "tell app \"X\" to y""#,
             r#"tell application "System Events" to keystroke "osascript -e \"tell app \\\"X\\\" to y\"""#),
            (r"ends with a backslash \",
             r#"tell application "System Events" to keystroke "ends with a backslash \\""#),
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
    fn an_xdotool_keydown_must_be_released_in_the_same_command() {
        // Found in the pre-release audit, not by a test failing. `xdotool keydown ctrl`
        // sets the modifier on the X SERVER: measured on Xvfb, a later key from a
        // separate command read state=0x4 against a control of 0x0, and kept reading it
        // after the holding process exited. Every command afterwards is reinterpreted
        // through a modifier nothing tracks — the mouse-button tracker follows buttons,
        // not keys, so the shutdown release cannot chase it either.
        //
        // This is the cliclick kd:/ku: stranding on the other backend, admitted by
        // opening the pointer chain to keydown/keyup for modifier-held clicks.
        for stranding in [
            argv(&["xdotool", "keydown", "ctrl"]),
            argv(&["xdotool", "keydown", "ctrl", "click", "1"]),
            argv(&["xdotool", "keydown", "ctrl", "keydown", "shift", "click", "1", "keyup", "shift"]),
            // Releasing something else strands what was actually held.
            argv(&["xdotool", "keydown", "ctrl", "click", "1", "keyup", "shift"]),
            // A hold that brackets nothing is not a modified action.
            argv(&["xdotool", "keydown", "ctrl", "keyup", "ctrl"]),
            // The hold has to open and close the chain, not sit inside it.
            argv(&["xdotool", "click", "1", "keydown", "ctrl", "click", "1", "keyup", "ctrl"]),
            // A release with nothing held clears a modifier this command never set.
            argv(&["xdotool", "click", "1", "keyup", "ctrl"]),
        ] {
            assert!(validate(&stranding).is_err(), "should reject {:?}", stranding);
        }
    }

    #[test]
    fn a_balanced_modifier_held_click_is_still_accepted() {
        // The shapes linux_actuation.py's out() actually emits. A guard that rejected
        // these would take modifier-held clicks away from Linux entirely, which is the
        // capability this release exists to add.
        for ok in [
            argv(&["xdotool", "keydown", "ctrl", "click", "1", "keyup", "ctrl"]),
            argv(&[
                "xdotool", "keydown", "ctrl", "keydown", "shift", "mousemove", "770", "310",
                "click", "1", "keyup", "shift", "keyup", "ctrl",
            ]),
            argv(&["xdotool", "keydown", "super", "mousemove", "10", "20", "click", "3",
                   "keyup", "super"]),
            // No modifier at all: the common path, unchanged.
            argv(&["xdotool", "mousemove", "770", "310", "click", "1"]),
            argv(&["xdotool", "getmouselocation", "--shell"]),
        ] {
            assert!(validate(&ok).is_ok(), "should accept {:?}: {:?}", ok, validate(&ok));
        }
    }

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
            Plan::Scroll {
                click: "c:.".to_string(),
                key_code: 125,
                amount: 5,
                modifiers: Vec::new(),
            }
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

    #[test]
    fn a_scroll_modifier_list_is_a_closed_enumeration() {
        // The agent writes the AppleScript; the wire carries only names it resolves
        // itself, so nothing a caller sends can become script text.
        assert_eq!(
            validate(&argv(&["__scroll__", "c:.", "125", "5", "cmd,shift"])).unwrap(),
            Plan::Scroll {
                click: "c:.".to_string(),
                key_code: 125,
                amount: 5,
                modifiers: vec!["command down".to_string(), "shift down".to_string()],
            }
        );
        // A repeat is one key held, as everywhere else in the grammar.
        assert_eq!(
            validate(&argv(&["__scroll__", "c:.", "125", "5", "cmd,cmd"])).unwrap(),
            Plan::Scroll {
                click: "c:.".to_string(),
                key_code: 125,
                amount: 5,
                modifiers: vec!["command down".to_string()],
            }
        );
        for case in [
            argv(&["__scroll__", "c:.", "125", "5", ""]),               // empty list
            argv(&["__scroll__", "c:.", "125", "5", "command down"]),   // clause text
            argv(&["__scroll__", "c:.", "125", "5", "cmd, shift"]),     // stray space
            argv(&["__scroll__", "c:.", "125", "5", "fn"]),             // not a modifier
            argv(&["__scroll__", "c:.", "125", "5", "cmd", "extra"]),   // arity
        ] {
            assert!(validate(&case).is_err(), "should reject {:?}", case);
        }
    }
}
