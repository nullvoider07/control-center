"""The Wayland daemon's grammar must not be looser than the agent's.

`cc-wayland-actuate` shares xdotool's sub-command language, and the agent
validates every argv it forwards with `argv_policy::validate_xdotool`. But the
daemon listens on a Unix socket of its own, and an argv arriving there never
passes through the agent at all — so any rule the agent enforces and the daemon
does not is a rule that holds on one path and not the other.

Before these tests, three did not hold on the socket path: `keydown` accepted any
keysym rather than the four pointer modifiers, an unclosed hold was accepted, and
`--repeat`/`--delay` were unbounded. The unclosed hold is the one that mattered
most, because a held key suppresses the daemon's idle timeout — so a stranded
modifier also made the daemon, and the input-synthesis grant behind it, permanent.

The constants are duplicated across a language boundary rather than shared, so the
values are pinned here against the Rust ones by hand. If `argv_policy.rs` changes
`POINTER_MODIFIERS`, `MAX_REPEAT` or `MAX_TYPE_DELAY_MS`, this file is the thing
that should fail.
"""
import re
from pathlib import Path

import pytest

from controller.os_specific import wayland_portal
from controller.os_specific.wayland_portal import UsageError, parse_chain


ARGV_POLICY = Path(__file__).resolve().parents[2] / "crates/agent/src/argv_policy.rs"


def accepts(tokens):
    try:
        parse_chain(list(tokens))
        return True
    except UsageError:
        return False


# ---- the shapes the controller actually emits -------------------------------

@pytest.mark.parametrize("tokens", [
    ["mousemove", "400", "300"],
    ["mousemove", "400", "300", "click", "1"],
    ["mousemove", "400", "300", "click", "--repeat", "2", "1"],
    ["mousemove", "400", "300", "mousedown", "1"],
    ["mousemove", "400", "300", "mouseup", "1"],
    ["click", "--repeat", "5", "4"],
    ["getmouselocation", "--shell"],
    ["key", "ctrl+c"],
    ["type", "--", "hello  world"],
    # A modifier held across a pointer action, both the single and multi form.
    ["keydown", "ctrl", "mousemove", "1", "2", "click", "1", "keyup", "ctrl"],
    ["keydown", "ctrl", "keydown", "shift", "click", "1", "keyup", "shift",
     "keyup", "ctrl"],
    # A waypoint drag: the longest pointer chain the builder produces.
    ["mousemove", "400", "300", "mousedown", "1", "mousemove", "500", "400",
     "mousemove", "650", "500", "mousemove", "800", "600", "mouseup", "1"],
])
def test_the_forms_the_controller_emits_are_accepted(tokens):
    assert accepts(tokens), f"legitimate argv refused: {tokens}"


def test_the_grok_ports_coordinate_terminator_is_tolerated():
    """`mousemove -- X Y` is not emitted by this repo's controller, but the sibling
    port emits it so a negative x is not read as an option. Accepting it costs
    nothing and refusing it would break that caller for no gain."""
    assert accepts(["mousemove", "--", "400", "300"])


# ---- the rules that must hold on this path too ------------------------------

@pytest.mark.parametrize("tokens, why", [
    (["keydown", "ctrl"], "an unclosed hold strands the modifier"),
    (["keyup", "ctrl"], "a release with nothing held is not a bracket"),
    (["keydown", "ctrl", "keyup", "ctrl"], "a bracket must contain an action"),
    (["keydown", "ctrl", "click", "1", "keyup", "shift"],
     "releasing a different modifier strands the one held"),
    (["click", "1", "keydown", "ctrl", "click", "1", "keyup", "ctrl"],
     "a hold in the interior of the chain is not bracketed"),
    (["keydown", "ctrl", "click", "1"], "the hold is never closed"),
])
def test_a_hold_that_is_not_bracketed_is_refused(tokens, why):
    assert not accepts(tokens), why


@pytest.mark.parametrize("key", ["a", "Return", "F1", "space", "0"])
def test_only_a_pointer_modifier_may_be_held(key):
    """The keydown branch exists so a click can be modified, not so any key can be
    pressed and left down through the pointer path."""
    assert not accepts(["keydown", key, "click", "1", "keyup", key])


def test_repeat_and_delay_are_bounded():
    assert accepts(["click", "--repeat", str(wayland_portal.MAX_REPEAT), "1"])
    assert not accepts(["click", "--repeat", str(wayland_portal.MAX_REPEAT + 1), "1"])
    assert not accepts(["click", "--repeat", "0", "1"])

    ok = str(wayland_portal.MAX_TYPE_DELAY_MS)
    too_much = str(wayland_portal.MAX_TYPE_DELAY_MS + 1)
    assert accepts(["type", "--delay", ok, "--", "x"])
    assert not accepts(["type", "--delay", too_much, "--", "x"])


@pytest.mark.parametrize("payload", ["keyup", "keydown ctrl", "click 1", "--repeat"])
def test_a_type_payload_that_looks_like_structure_is_still_data(payload):
    """The bracket check runs on parsed ops, not on the raw tokens, precisely so
    `type -- keyup` types the word rather than being read as an unbalanced
    release. The agent never meets this case because its `type` arm cannot chain a
    pointer verb; this parser can chain, so it has to separate them itself."""
    assert accepts(["type", "--", payload])


# ---- the constants must match the Rust --------------------------------------

def _rust_const(pattern: str) -> str:
    source = ARGV_POLICY.read_text()
    match = re.search(pattern, source)
    assert match, f"{pattern!r} no longer matches {ARGV_POLICY.name}"
    return match.group(1)


def test_the_bounds_match_the_agents():
    """Duplicated across a language boundary, so the duplication is checked rather
    than trusted. A drift here means the socket path and the gRPC path disagree
    about what is allowed, which is the condition this whole file exists to
    prevent."""
    assert wayland_portal.MAX_REPEAT == int(
        _rust_const(r"const MAX_REPEAT: u64 = ([0-9_]+);").replace("_", ""))
    assert wayland_portal.MAX_TYPE_DELAY_MS == int(
        _rust_const(r"const MAX_TYPE_DELAY_MS: u64 = ([0-9_]+);").replace("_", ""))

    rust_modifiers = _rust_const(r"const POINTER_MODIFIERS: &\[&str\] = &\[([^\]]*)\]")
    assert set(re.findall(r'"([a-z]+)"', rust_modifiers)) == set(
        wayland_portal.POINTER_MODIFIERS)


# ---- daemon lifetime --------------------------------------------------------

@pytest.mark.parametrize("value", ["0", "-1", "-3600"])
def test_a_timeout_that_never_expires_is_refused(monkeypatch, value):
    """Zero used to mean "never expire". A never-expiring input-synthesis daemon is
    not something an environment variable should be able to ask for."""
    monkeypatch.setenv("CC_WAYLAND_IDLE_TIMEOUT", value)
    with pytest.raises(UsageError):
        wayland_portal.idle_timeout_s()


@pytest.mark.parametrize("value, expected", [
    ("60", 60), ("1800", 1800), ("", wayland_portal.DEFAULT_IDLE_TIMEOUT_S),
])
def test_a_positive_timeout_is_honoured(monkeypatch, value, expected):
    monkeypatch.setenv("CC_WAYLAND_IDLE_TIMEOUT", value)
    assert wayland_portal.idle_timeout_s() == expected


def test_an_unset_timeout_takes_the_default(monkeypatch):
    monkeypatch.delenv("CC_WAYLAND_IDLE_TIMEOUT", raising=False)
    assert wayland_portal.idle_timeout_s() == wayland_portal.DEFAULT_IDLE_TIMEOUT_S


def test_a_hold_cannot_suppress_expiry_forever():
    """The ceiling is what stops "no expiry while held" composing with a stranded
    hold into a permanent capability. Its value is a judgement call; that it exists
    and is finite is not."""
    assert 0 < wayland_portal.MAX_HELD_SUPPRESSION_S < float("inf")
    assert wayland_portal.MAX_HELD_SUPPRESSION_S <= wayland_portal.DEFAULT_IDLE_TIMEOUT_S


# ---- request authentication -------------------------------------------------

def test_the_auth_token_is_not_in_the_runtime_directory(monkeypatch, tmp_path):
    """The token defends against a confined caller — one given the session bus and
    XDG_RUNTIME_DIR but not the home directory. Putting it in the runtime directory
    would make it readable by exactly the callers it excludes, and the measure
    would become decorative. This is the one property of its location that matters.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    token_path = Path(wayland_portal._auth_token_path())
    runtime_dir = Path(wayland_portal.socket_path()).parent
    assert runtime_dir not in token_path.parents


def test_the_auth_token_is_created_private(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    token = wayland_portal.load_or_create_auth_token()
    assert len(token) >= 32
    path = Path(wayland_portal._auth_token_path())
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    # Stable across calls, or the client and daemon would never agree.
    assert wayland_portal.load_or_create_auth_token() == token


def test_a_pre_existing_loose_directory_is_tightened(monkeypatch, tmp_path):
    """`makedirs(mode=0o700, exist_ok=True)` applies the mode only when it creates
    the directory, so a directory some other tool left group-readable was accepted
    as-is."""
    state = tmp_path / "state"
    (state / "control-center").mkdir(parents=True)
    (state / "control-center").chmod(0o755)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    wayland_portal.load_or_create_auth_token()
    assert (state / "control-center").stat().st_mode & 0o777 == 0o700
