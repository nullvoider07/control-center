# Security review and audit — v1.3.0

Scope: the change set released as v1.3.0, measured against `v1.2.2` (`0083149`) — 13
files, 2645 insertions. The threat model treated as given: **the agent is the security
boundary.** A caller holding the `execute` scope is untrusted with respect to the
actuation grammar, and any invariant that only the Python command builders maintain is
not an invariant of the system.

---

## 1. Finding: a held modifier could be stranded on Linux — FIXED

**Severity:** medium. **Introduced by this change set.** **Confirmed on hardware.**

`validate_xdotool` was opened to `keydown`/`keyup` so a pointer chain can carry a
modifier (this release's headline capability). The key argument was restricted to four
modifiers, but **nothing required the release**. `xdotool keydown ctrl` validated and
executed.

`keydown` sets the modifier on the **X server**, not on the process. Measured on Xvfb,
with a control first:

```
control, nothing held        KeyPress state=0x0
xdotool keydown ctrl         (accepted by the policy, alone)
a later, SEPARATE command    KeyPress state=0x4   ← ControlMask
after the holder had exited  KeyPress state=0x4   ← still held
```

Every subsequent keystroke and click in that X session is reinterpreted through a
modifier nothing is tracking. The agent's shutdown release follows mouse *buttons*, not
keys, so it cannot clear it either.

This is the **same defect** as the `kd:`/`ku:` cliclick stranding that this very change
set added `check_modifier_bracket` to prevent — reintroduced on the other backend by the
same feature. It survived because `linux_actuation.py` documented the invariant as
already holding: *"no argv can carry a keydown without its keyup."* True of the builder,
false of the system. That sentence is a near-verbatim match for the one on
`macos_actuation.py`'s `emit()` that produced the original finding.

**Fix:** `check_pointer_modifier_bracket` in `argv_policy.rs`, mirroring the cliclick
guard. Accepted shape is the one the builder emits — every `keydown` in a leading run,
every `keyup` in a trailing run, the same modifiers in each, at least one action
bracketed. Rejected: no release; a release naming a different modifier; a hold
bracketing nothing; a hold in the interior of the chain; a release with nothing held.

**Verification.** Seven stranding shapes, each confirmed admitted before the guard and
refused after — checked individually, not as one assert that stops at the first. Five
legitimate shapes (including every form `linux_actuation.py` emits) still accepted, and
that test still passes with the guard disabled, so it is measuring the grammar rather
than the guard. Live on Xvfb: the stranding argv is refused with modifier state
unchanged at `0x0`, the agent logs `a modifier hold must bracket at least one action`,
and a legitimate Ctrl-click succeeds and leaves the state clean.

---

## 2. Reviewed and sound

**The new argv surface.** `__middle__` and `__scroll__` both carry a closed enumeration
of modifier *names* mapped agent-side to CGEventFlags bits and AppleScript `using`
clauses. An unrecognised name is refused, not ignored. No bitmask and no script text
crosses the wire, so a caller cannot aim at flags the grammar never intended.

**The JXA middle-click route.** Confirmed as designed: every value interpolated into the
script is an integer this agent parsed — coordinates from a validated cliclick point
token, flags from the closed list. No client-supplied text reaches it. The CoreGraphics
constants are numeric rather than named, so a gap in the bridge's metadata cannot
resolve to `undefined` and silently post nothing. `mc:` was correctly *removed* from the
cliclick vocabulary; it named an action cliclick does not have, so every macOS middle
click had been failing.

**`check_modifier_bracket` (cliclick).** Correct and appropriately strict: exactly one
`kd:` first, one `ku:` last, naming the same modifier set, with at least one action
between. A `ku:` naming a *different* modifier is rejected — a "there is a `ku:`
somewhere" check would have admitted it while the held modifier stayed down.

**Token revocation / TLS.** `CC_REVOKED_SUBJECTS` is not part of this change set
(pre-existing, v1.2.2). Re-checked anyway since a release locks in defaults: it is
enforced inside the single token-validation path, after signature and expiry, so it
covers every RPC that validates a token. Startup-only read, which the code states.

**Server-side classification.** `strip_mouse_modifiers` keeps the modifier out of
`action_subtype` and in `raw_command`. A token of modifiers alone is deliberately
returned unchanged so it fails the verb match downstream rather than resolving to an
empty subtype that would classify as a click.

---

## 3. Documented, not fixed — decisions that are yours

**`validate_write` does not validate content.** Unchanged by this release. `__write__`
is path-constrained to the two watcher files, but the content is passed through
unexamined to AutoHotkey, whose `Send` syntax makes `^ ! # {}` live. This is not an
escalation *past* the `execute` scope — that scope already types arbitrary text and
presses arbitrary keys on every backend — so it is a scope-design property rather than a
bypass. Worth stating explicitly because the release grows the content grammar.

**`human_command` is unbounded, and nothing reconciles it with the `argv` that ran.**
Pre-existing. The server requires it non-empty and applies no length bound.

Tracing exactly what it reaches, because the imprecise version of this claim is easy to
write and points at the wrong place:

| Recorded field | Derived from |
|---|---|
| `action_type`, `action_subtype`, `is_here_command` | `parse_command_meta(&human_command)`, unconditionally (`main.rs:643`) |
| `raw_command` verb phrase | the agent's `message` — but that is `build_detailed_message(&action, …, &human_cmd)`, and `action` is `parse_action_details(human_command)` |
| coordinates inside `raw_command`, `mouse_x`/`mouse_y`, `position_captured`, `success`, timing | genuinely measured by the agent |

So the split is **not** "narrative execution-derived, classification caller-asserted".
The agent is the one *speaking*, but the content of what it says about the gesture — verb,
notch count, modifiers — is parsed back out of the caller's string. Everything describing
**what gesture was performed** traces to `human_command`; only **position and outcome** are
execution-derived. `raw_command` falls back to `human_command` verbatim when the agent
errors or returns an empty message.

A caller with the `execute` scope can therefore run one gesture and have it recorded and
classified as another, and no consumer can detect the divergence. This matters more here
than it would elsewhere because **a capture artefact is training data**.

Worth noting for whoever closes it: the raw material already exists. The agent ships the
validated argv as ground truth in `executed_meta["argv"]` (`main.rs:1512`, commented
"Ground truth for the recorded event: what actually ran"), and the mouse-button tracker
already derives from the plan rather than `human_command` for exactly this reason. Nothing
consumes it for classification. The natural fix is `parse_command_meta` reading the plan,
not the string — but that changes recorded values, so it is a behaviour decision, not a
patch to slip into a release. Bounding the length has the same character: it requires
deciding what an over-long command should *replay* as.

**Windows modifier stranding via the watcher.** `mouse_control.ahk` releases modifiers in
a `finally`, which is in-process only and cannot survive the watcher being killed; and
`Send modDown` sits just outside that `try`, so a throw partway through pressing a
multi-modifier sequence strands what was already pressed. Narrow and pre-existing in
shape. **Not fixed deliberately:** the watcher is deployed to guests and I could not
verify a change to it on hardware in this session (`win10_agent` is down and guest
lifecycle is not mine), and shipping an unverified actuation change is worse than
shipping a documented one. The README now states the exception rather than implying the
guarantee is universal.

---

## 4. Comment and documentation accuracy

Three places asserted an invariant that was true of the code around them and not of the
system. Each now names where the invariant is actually enforced:

- `linux_actuation.py` `out()` — was the vehicle for finding 1.
- `macos_actuation.py` `emit()` — the original finding's wording, still builder-scoped.
- `argv_policy.rs`, the xdotool pointer branch — "the release cannot be lost because it
  is one invocation", which a bare `keydown ctrl` disproves.

The README told users "no modifier can be left down between commands" without
qualification. It now says the agent enforces it, and states the Windows watcher
exception plainly, including how to recover.

---

## 5. Suite state

| | |
|---|---|
| Rust | 67 + 4 + 17 passed, 0 failed |
| Python | 468 passed, 0 skipped |
| Packaging | 8/8, and shown to fail when one version site is drifted |

Hardware, this session: Windows drag position 2/2 against a `position` ground truth with
the pre-fix binary proven to fail the same check; Windows mouse surface 20/20; Linux
scroll effect 3/3 against a measured 50 px notch; macOS and Windows scroll echo 6/6 each.
