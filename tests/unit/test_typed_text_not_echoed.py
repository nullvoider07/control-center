"""Typed text must not reach stdout or a world-readable file.

A `type` payload can carry a credential. The interactive result line has printed a
character count rather than the content since the command-path hardening, but that
commit changed only that one line: batch progress output still echoed the whole
command, and `batch --output` wrote every command verbatim with default umask
permissions while the sibling metrics/session/token exports all restricted theirs.

These tests pin the property in every place a command is presented or persisted, so
the next new output site has to satisfy it too.
"""
import ast
import inspect
import os
import stat
from pathlib import Path

import pytest

from controller.management import cli
from controller.os_specific import command_hints
from controller.os_specific.linux_actuation import LinuxActuation
from controller.os_specific.macos_actuation import MacOSActuation
from controller.os_specific.windows_actuation import WindowsActuation

SECRET = "hunter2-correct-horse"


# ---- redact ----------------------------------------------------------------
def test_redact_replaces_typed_text_with_a_count():
    assert command_hints.redact(f"type {SECRET}") == f"type <{len(SECRET)} chars>"
    assert SECRET not in command_hints.redact(f"type {SECRET}")


@pytest.mark.parametrize("command", [
    "press #+4", "press ^c", "960 540 left", "here left", "position",
    "100 100 drag 900 700 dwell 150", "type",
])
def test_redact_leaves_non_typed_commands_intact(command):
    """Key names and coordinates are not secret, and redacting them would make the
    progress output useless for diagnosing a bad step."""
    assert command_hints.redact(command) == command


def test_redact_handles_surrounding_whitespace():
    assert command_hints.redact(f"  type {SECRET}  ") == f"type <{len(SECRET)} chars>"


# ---- rejection message ----------------------------------------------------
@pytest.mark.parametrize("command", [f"type{SECRET}", f"type {SECRET}"])
def test_rejection_message_does_not_repeat_the_raw_command(command):
    """It used to print the command three times: header, hint, and a literal
    'To type it literally: type <command>' line. The glued form is the one that
    actually reaches here, since a well-formed `type` is never rejected."""
    message = command_hints.rejection_message(
        command, MacOSActuation.MOUSE_ACTIONS, MacOSActuation.KEYBOARD_ACTIONS
    )
    assert SECRET not in message, message


def test_rejection_message_still_names_the_repair():
    message = command_hints.rejection_message(
        "1022 343left", MacOSActuation.MOUSE_ACTIONS, MacOSActuation.KEYBOARD_ACTIONS
    )
    assert "Did you mean: 1022 343 left" in message
    assert "type <text>" in message
    assert "--lenient" in message


# ---- batch progress output -------------------------------------------------
@pytest.mark.parametrize("cls", [LinuxActuation, MacOSActuation, WindowsActuation])
def test_batch_progress_lines_redact_typed_text(cls):
    """Assert on the source of the batch loop: it must pass every command through
    `redact` before printing. Executing the loop needs a live agent, but the property
    is a property of the format string."""
    source = inspect.getsource(cls.execute_batch_file if hasattr(cls, "execute_batch_file")
                               else cls.execute_batch)
    printed_bare = [
        line.strip() for line in source.splitlines()
        if "print(" in line and ("✓ {" in line or "✗ {" in line)
        and "redact" not in line
    ]
    assert not printed_bare, (
        f"{cls.__name__} batch output echoes a command without redaction:\n  "
        + "\n  ".join(printed_bare)
    )


def test_cli_batch_progress_redacts_typed_text():
    # `cli.batch` is a click Command; the function is on .callback.
    source = inspect.getsource(cli.batch.callback)
    assert "command_hints.redact(cmd)" in source, \
        "the CLI batch progress line must redact the command"


# ---- batch results file ----------------------------------------------------
def test_batch_results_file_is_owner_only(tmp_path):
    """`batch --output` records each command verbatim, so the file needs the same
    treatment as the session and metrics exports."""
    target = tmp_path / "nested" / "results.json"
    cli._secure_write(target, '{"results": [{"command": "type ' + SECRET + '"}]}')
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(target.parent).st_mode) == 0o700


def test_batch_does_not_write_its_results_unrestricted():
    """The regression was a bare `Path(output).write_text(...)`. Parse the function
    and assert no write goes out without the secure helper."""
    tree = ast.parse(inspect.getsource(cli.batch.callback).lstrip())
    bare_writes = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("write_text", "write_bytes")
    ]
    assert not bare_writes, (
        f"batch writes a file without _secure_write at offset line(s) {bare_writes}"
    )


# Writes of material that is public by definition and needs no restriction.
PUBLIC_WRITES = {"ca_path"}


def test_every_export_path_restricts_permissions():
    """Sweep the CLI for file writes that skip both helpers — the miss that made the
    batch results file world-readable while three sibling exports were correct.

    Scoped per enclosing function rather than by a line window, because the metrics
    and session exports branch on format before restricting at the end.
    """
    source = Path(inspect.getfile(cli)).read_text()
    tree = ast.parse(source)
    lines = source.splitlines()

    offenders = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef) or func.name == "_secure_write":
            continue
        body = "\n".join(lines[func.lineno - 1: (func.end_lineno or func.lineno)])
        if "_restrict_perms" in body or "_secure_write" in body:
            continue
        for node in ast.walk(func):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in ("write_text", "write_bytes"):
                continue
            target = node.func.value
            if isinstance(target, ast.Name) and target.id in PUBLIC_WRITES:
                continue
            offenders.append(
                f"{func.name}() line {node.lineno}: {lines[node.lineno - 1].strip()}"
            )

    assert not offenders, (
        "file write(s) in a function that neither uses _secure_write nor calls "
        "_restrict_perms:\n  " + "\n  ".join(offenders)
    )
