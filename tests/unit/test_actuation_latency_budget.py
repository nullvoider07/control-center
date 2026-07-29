"""Bound the fixed delays on the actuation path.

None of this measures anything — it cannot, because the AHK watchers only run on a
Windows guest and there is no AutoHotkey on a build machine. What it does is stop a
large fixed sleep from reappearing unnoticed, which is how the previous ones
survived: `Sleep 500` before every keystroke was a UI-launch wait applied to every
command, and nothing in the suite had an opinion about it.

Measured figures for the parts that *can* be measured are in the agent source beside
the constant they justify (crates/agent/src/main.rs, capture_position_if_mouse).
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WATCHERS = ROOT / "watcher_scripts"
AGENT = ROOT / "crates/agent/src/main.rs"

# No single fixed pause on a per-command path should exceed this. A wait longer than
# this is a wait for something specific, and belongs behind a condition that names
# what it is waiting for rather than being charged to every command.
MAX_SINGLE_SLEEP_MS = 50

# The unconditional cost of one command, before any work: watcher poll + settle +
# trailing pause. Drag dwell is excluded — it is a deliberate part of the gesture.
MAX_FIXED_PATH_MS = 100


def sleeps(script: str) -> list:
    body = (WATCHERS / script).read_text()
    return [int(ms) for ms in re.findall(r"^\s*Sleep\s+(\d+)\s*$", body, re.M)]


@pytest.mark.parametrize("script", ["keyboard_control.ahk", "mouse_control.ahk"])
def test_no_single_sleep_dominates_a_command(script):
    values = sleeps(script)
    assert values, f"no Sleep statements parsed out of {script}"
    over = [ms for ms in values if ms > MAX_SINGLE_SLEEP_MS]
    assert not over, (
        f"{script} has fixed sleeps over {MAX_SINGLE_SLEEP_MS}ms: {over}. "
        "A wait this long is waiting for something in particular; put it behind a "
        "condition rather than charging every command for it."
    )


def test_the_keyboard_path_has_a_bounded_fixed_cost():
    """Every Sleep in keyboard_control.ahk is on the per-command path — the poll, the
    settle before Send, and the trailing pause that delays the next pickup. Their sum
    is the floor under a Windows keystroke."""
    total = sum(sleeps("keyboard_control.ahk"))
    assert total <= MAX_FIXED_PATH_MS, (
        f"a Windows keystroke now costs at least {total}ms of sleep "
        f"(budget {MAX_FIXED_PATH_MS}ms)"
    )


def test_the_watchers_poll_promptly():
    """The poll interval is the average latency added before a command is even read.
    mouse_control.ahk already runs a position tracker on a 10ms timer, so a 10ms poll
    is not a new cost profile."""
    for script in ("keyboard_control.ahk", "mouse_control.ahk"):
        body = (WATCHERS / script).read_text()
        loop = body[body.index("Loop {"):]
        poll = [int(ms) for ms in re.findall(r"^\s*Sleep\s+(\d+)\s*$",
                                             loop[:loop.index("\n    }")], re.M)]
        assert poll, f"could not find the watcher poll in {script}"
        assert max(poll) <= 10, f"{script} polls every {max(poll)}ms"


def test_the_agent_settles_briefly_before_the_first_readback():
    """The settle is an optimisation, not the correctness mechanism — the
    verify-and-retry loop after it is. A long settle taxes every command that did not
    need one; a short settle costs one re-read on the few that do."""
    body = AGENT.read_text()
    match = re.search(
        r'#\[cfg\(not\(target_os = "windows"\)\)\]\s*\n\s*let settle_ms = (\d+);', body)
    assert match, "could not find the non-Windows settle constant"
    assert int(match.group(1)) <= 20, (
        f"settle_ms is {match.group(1)}ms; measured A/B in the source comment shows "
        "10ms holds capture at 60/60 while halving round-trip latency"
    )
