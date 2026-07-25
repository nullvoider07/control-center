"""End-to-end security assertions against the live TLS server + agent.

Covers the Phase B properties: TLS is mandatory, per-handler scopes are enforced,
monitoring/query RPCs reject unauthenticated callers, agent registration needs the
`agent` scope, and F5 (shell injection) is structurally dead — an injected `type`
payload lands as literal text via argv, never executing on the guest.
"""
import threading
import time

import grpc
import pytest

import harness
from conftest import raw_stub, meta, requires_stack
from controller.integrations.proto import control_center_pb2 as pb
from controller.integrations.gRPC import GRPCClient
from controller.integrations.proto import control_center_pb2_grpc
from controller.os_specific.linux_actuation import LinuxActuation

pytestmark = requires_stack


def _authed_client(stack, token, timeout=8):
    c = GRPCClient(host=stack.host, port=stack.port, timeout=timeout, use_ssl=True)
    c.channel = c._create_channel()
    c.stub = control_center_pb2_grpc.ControlServiceStub(c.channel)
    c.set_token(token)
    return c


# ---- TLS is mandatory ------------------------------------------------------
def test_plaintext_client_refused_against_tls_server(stack):
    ch, stub = raw_stub(stack, use_ssl=False)
    with pytest.raises(grpc.RpcError) as ei:
        stub.Ping(pb.PingRequest(), timeout=5)
    assert ei.value.code() in (
        grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.INTERNAL)
    ch.close()


# ---- Scope enforcement (F4) ------------------------------------------------
def test_monitor_token_cannot_execute(stack):
    ch, stub = raw_stub(stack)
    req = pb.CommandRequest(id="x", argv=["xdotool", "mousemove", "10", "10"],
                            human_command="10 10 move")
    with pytest.raises(grpc.RpcError) as ei:
        stub.ExecuteCommand(req, metadata=meta(stack.tokens["monitor"]), timeout=5)
    assert ei.value.code() == grpc.StatusCode.PERMISSION_DENIED
    ch.close()


def test_execute_token_can_execute(stack):
    c = _authed_client(stack, stack.tokens["execute"])
    r = c.execute_command(argv=["xdotool", "mousemove", "200", "150"],
                          human_command="200 150 move")
    c.disconnect()
    assert r["success"] is True


def test_disconnect_requires_admin_scope(stack):
    # execute-scope token must NOT be able to disconnect the agent (would kill the
    # shared session agent if it wrongly succeeded — it must be denied).
    ch, stub = raw_stub(stack)
    with pytest.raises(grpc.RpcError) as ei:
        stub.DisconnectAgent(pb.DisconnectAgentRequest(reason="test"),
                             metadata=meta(stack.tokens["execute"]), timeout=5)
    assert ei.value.code() == grpc.StatusCode.PERMISSION_DENIED
    ch.close()


# ---- Unauthenticated monitoring rejected (F3) ------------------------------
def test_unauthenticated_query_rejected(stack):
    ch, stub = raw_stub(stack)
    with pytest.raises(grpc.RpcError) as ei:
        stub.QueryConnections(pb.ConnectionQuery(), metadata=[], timeout=5)
    assert ei.value.code() == grpc.StatusCode.UNAUTHENTICATED
    ch.close()


def test_unauthenticated_watch_rejected(stack):
    ch, stub = raw_stub(stack)
    stream = stub.WatchCommands(pb.WatchRequest(), metadata=[], timeout=5)
    with pytest.raises(grpc.RpcError) as ei:
        next(stream)
    assert ei.value.code() == grpc.StatusCode.UNAUTHENTICATED
    ch.close()


def test_agent_info_requires_monitor_scope(stack):
    """GetAgentInfo fingerprints the guest (OS, version, capabilities). A token with
    no monitoring rights — here a metrics-only one — must not read it."""
    ch, stub = raw_stub(stack)
    with pytest.raises(grpc.RpcError) as ei:
        stub.GetAgentInfo(pb.AgentInfoRequest(),
                          metadata=meta(stack.tokens["none"]), timeout=5)
    assert ei.value.code() == grpc.StatusCode.PERMISSION_DENIED

    with pytest.raises(grpc.RpcError) as ei:
        stub.GetAgentInfo(pb.AgentInfoRequest(), metadata=[], timeout=5)
    assert ei.value.code() == grpc.StatusCode.UNAUTHENTICATED

    resp = stub.GetAgentInfo(pb.AgentInfoRequest(),
                             metadata=meta(stack.tokens["monitor"]), timeout=5)
    assert resp.agent_version
    ch.close()


def test_every_data_rpc_rejects_a_scopeless_token(stack):
    """Sweep: a valid token carrying only an unrelated scope must be refused by every
    RPC that returns data or acts. Ping is the deliberate exception (liveness only).
    This is the check that would have caught GetAgentInfo missing its scope."""
    ch, stub = raw_stub(stack)
    tok = meta(stack.tokens["none"])  # valid JWT, `metrics` scope only
    calls = [
        ("QueryConnections", lambda: stub.QueryConnections(pb.ConnectionQuery(), metadata=tok, timeout=5)),
        ("QueryServers", lambda: stub.QueryServers(pb.ServerStatusQuery(), metadata=tok, timeout=5)),
        ("GetServerIdentity", lambda: stub.GetServerIdentity(pb.InfoRequest(), metadata=tok, timeout=5)),
        ("GetAgentInfo", lambda: stub.GetAgentInfo(pb.AgentInfoRequest(), metadata=tok, timeout=5)),
        ("GetConnectionHistory", lambda: stub.GetConnectionHistory(pb.ConnectionHistoryRequest(limit=5), metadata=tok, timeout=5)),
        ("WatchCommands", lambda: next(stub.WatchCommands(pb.WatchRequest(), metadata=tok, timeout=5))),
        ("MonitorConnection", lambda: next(stub.MonitorConnection(pb.MonitorRequest(), metadata=tok, timeout=5))),
        ("DisconnectAgent", lambda: stub.DisconnectAgent(pb.DisconnectAgentRequest(reason="x"), metadata=tok, timeout=5)),
        ("ExecuteCommand", lambda: stub.ExecuteCommand(
            pb.CommandRequest(id="s1", argv=["xdotool", "mousemove", "1", "1"],
                              human_command="1 1 move"), metadata=tok, timeout=5)),
        ("RegisterAgent", lambda: stub.RegisterAgent(
            pb.RegistrationRequest(auth_token=stack.tokens["none"]), timeout=5)),
    ]
    leaked = []
    for name, call in calls:
        try:
            call()
            leaked.append(f"{name}: allowed with no relevant scope")
        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.PERMISSION_DENIED:
                leaked.append(f"{name}: {e.code()} (expected PERMISSION_DENIED)")
    assert not leaked, leaked
    ch.close()


def test_monitor_token_can_query(stack):
    ch, stub = raw_stub(stack)
    resp = stub.QueryConnections(pb.ConnectionQuery(),
                                 metadata=meta(stack.tokens["monitor"]), timeout=5)
    assert resp.total_count >= 1
    ch.close()


# ---- Agent registration needs the agent scope (F2) -------------------------
def test_registration_no_token_rejected(stack):
    ch, stub = raw_stub(stack)
    with pytest.raises(grpc.RpcError) as ei:
        stub.RegisterAgent(pb.RegistrationRequest(auth_token=""), timeout=5)
    assert ei.value.code() == grpc.StatusCode.UNAUTHENTICATED
    ch.close()


def test_registration_wrong_scope_rejected(stack):
    ch, stub = raw_stub(stack)
    with pytest.raises(grpc.RpcError) as ei:
        stub.RegisterAgent(
            pb.RegistrationRequest(auth_token=stack.tokens["monitor"]), timeout=5)
    assert ei.value.code() == grpc.StatusCode.PERMISSION_DENIED
    ch.close()


# ---- F5: injection is inert (argv, no shell) -------------------------------
def test_injection_payload_stays_literal(stack, tmp_path):
    marker = tmp_path / "pwned"
    la = LinuxActuation.__new__(LinuxActuation)
    la.display = stack  # unused by builder; attribute must exist
    payload = f"type hello$(touch {marker})`touch {marker}`"
    argv, human = LinuxActuation._build_keyboard_command(la, payload)
    assert argv == ["xdotool", "type", payload.split("type ", 1)[1]]

    c = _authed_client(stack, stack.tokens["execute"])
    r = c.execute_command(argv=argv, human_command=human)
    c.disconnect()
    assert r["success"] is True
    time.sleep(0.3)
    assert not marker.exists(), "injection executed a shell — F5 regression!"


# ---- The shell path is gone, not merely bypassed --------------------------
def test_legacy_command_field_rejected(stack, tmp_path):
    """A pre-argv client could previously hand the agent a shell string. The field is
    now refused at the server, so no request can reach `sh -c` at all."""
    marker = tmp_path / "legacy-pwned"
    ch, stub = raw_stub(stack)
    req = pb.CommandRequest(id="legacy1", command=f"touch {marker}", argv=[],
                            human_command="")
    with pytest.raises(grpc.RpcError) as ei:
        stub.ExecuteCommand(req, metadata=meta(stack.tokens["execute"]), timeout=5)
    assert ei.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    ch.close()
    time.sleep(0.3)
    assert not marker.exists(), "legacy shell string executed — F1 regression!"


def test_argv_without_human_command_rejected(stack):
    ch, stub = raw_stub(stack)
    req = pb.CommandRequest(id="nohuman", argv=["xdotool", "mousemove", "5", "5"],
                            human_command="")
    with pytest.raises(grpc.RpcError) as ei:
        stub.ExecuteCommand(req, metadata=meta(stack.tokens["execute"]), timeout=5)
    assert ei.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    ch.close()


def test_execute_rpc_retired(stack):
    """The legacy Execute RPC carried only a shell string; it must no longer route."""
    ch, stub = raw_stub(stack)
    req = pb.ExecuteRequest(id="e1", command="xdotool mousemove 5 5")
    with pytest.raises(grpc.RpcError) as ei:
        stub.Execute(req, metadata=meta(stack.tokens["execute"]), timeout=5)
    assert ei.value.code() == grpc.StatusCode.UNIMPLEMENTED
    ch.close()


# ---- The argv allow-list confines, it does not merely name binaries --------
def test_xdotool_exec_is_rejected(stack, tmp_path):
    """`xdotool exec` spawns arbitrary processes; allow-listing argv[0] alone would
    let an execute-scoped caller run anything."""
    marker = tmp_path / "xdotool-pwned"
    c = _authed_client(stack, stack.tokens["execute"])
    r = c.execute_command(
        argv=["xdotool", "exec", "/bin/sh", "-c", f"touch {marker}"],
        human_command="here left")
    c.disconnect()
    assert r["success"] is False
    time.sleep(0.3)
    assert not marker.exists(), "xdotool exec ran a shell — F2 regression!"


def test_xdotool_type_file_read_primitive_is_dead(stack, tmp_path):
    """`xdotool type --file=PATH` types a file's contents into the focused window.
    The payload is a single argv element, so arity alone does not stop it — the agent
    must neutralise it with a `--` terminator and type it literally instead."""
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-CANARY\n")

    c = _authed_client(stack, stack.tokens["execute"])
    try:
        # Existing file: must be typed literally, not read. If --file= were honoured
        # xdotool exits 0 too, so the discriminator is the missing-file case below.
        r = c.execute_command(argv=["xdotool", "type", f"--file={secret}"],
                              human_command="type marker")
        assert r["success"] is True

        # A path that cannot be opened: xdotool fails only if --file= was parsed as an
        # option. Success here proves the payload was treated as text.
        missing = tmp_path / "does-not-exist.txt"
        r = c.execute_command(argv=["xdotool", "type", f"--file={missing}"],
                              human_command="type marker")
        assert r["success"] is True, \
            "xdotool parsed --file= as an option — the file-read primitive is live!"

        # The two-element option form is refused outright by the grammar.
        r = c.execute_command(argv=["xdotool", "type", "--file", str(secret)],
                              human_command="type marker")
        assert r["success"] is False
    finally:
        c.disconnect()


def test_non_actuation_binary_is_rejected(stack, tmp_path):
    marker = tmp_path / "sh-pwned"
    c = _authed_client(stack, stack.tokens["execute"])
    r = c.execute_command(argv=["sh", "-c", f"touch {marker}"],
                          human_command="here left")
    c.disconnect()
    assert r["success"] is False
    time.sleep(0.3)
    assert not marker.exists()


def test_allowed_actuation_still_works(stack):
    """The grammar must not break the vocabulary the controller actually emits."""
    c = _authed_client(stack, stack.tokens["execute"])
    try:
        for argv, human in [
            (["xdotool", "mousemove", "300", "200"], "300 200 move"),
            (["xdotool", "mousemove", "310", "210", "click", "1"], "310 210 left"),
            (["xdotool", "click", "--repeat", "2", "1"], "here double"),
            (["xdotool", "key", "ctrl+a"], "press ^a"),
            (["xdotool", "type", "plain text"], "type plain text"),
            (["xdotool", "getmouselocation", "--shell"], "position"),
        ]:
            r = c.execute_command(argv=argv, human_command=human)
            assert r["success"] is True, f"{argv} was rejected: {r['message']}"
    finally:
        c.disconnect()


# ---- The agent stream is bound to the agent that registered ----------------
def _empty_request_stream():
    return
    yield  # noqa: unreachable — makes this a generator


def test_second_agent_stream_rejected(stack):
    """Scope alone is not enough: another `agent`-token holder must not be able to
    attach a second handler and race the live agent for queued commands."""
    ch, stub = raw_stub(stack)
    try:
        # (a) A different agent principal — subject does not match the registration.
        other = harness.mint_token(["agent"], user="impostor-agent")
        stream = stub.AgentStream(_empty_request_stream(), metadata=meta(other))
        with pytest.raises(grpc.RpcError) as ei:
            next(stream)
        assert ei.value.code() == grpc.StatusCode.PERMISSION_DENIED

        # (b) The same principal — the connection already has a bound stream.
        stream = stub.AgentStream(_empty_request_stream(),
                                  metadata=meta(stack.tokens["agent"]))
        with pytest.raises(grpc.RpcError) as ei:
            next(stream)
        assert ei.value.code() == grpc.StatusCode.PERMISSION_DENIED
    finally:
        ch.close()

    # The real agent must be untouched by the rejected attempts.
    c = _authed_client(stack, stack.tokens["execute"])
    try:
        r = c.execute_command(argv=["xdotool", "mousemove", "150", "150"],
                              human_command="150 150 move")
        assert r["success"] is True, "legit agent was disturbed by a rejected stream"
    finally:
        c.disconnect()


def test_human_command_drives_recording(stack):
    """The broadcast CommandEvent derives its action metadata from the explicit
    human_command (not a reconstructed shell string), and the recorded text
    faithfully reflects the typed payload."""
    human = "type recorded-exactly-123"
    events = []

    def watch():
        c = _authed_client(stack, stack.tokens["monitor"], timeout=15)
        try:
            for ev in c.watch_commands():
                if ev["is_heartbeat"]:
                    continue
                events.append(ev)
                break
        finally:
            c.disconnect()

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    time.sleep(1.0)  # let the watch stream attach before we emit

    c = _authed_client(stack, stack.tokens["execute"])
    c.execute_command(argv=["xdotool", "type", "recorded-exactly-123"],
                      human_command=human)
    c.disconnect()

    t.join(timeout=8)
    assert events, "no non-heartbeat command event was streamed"
    ev = events[0]
    # Metadata is parsed from human_command ("type ...") — proves the explicit
    # human_command was forwarded, not reconstructed from a shell string.
    assert ev["action_type"] == "keyboard" and ev["action_subtype"] == "type"
    # The recorded command reflects the typed payload faithfully.
    assert "recorded-exactly-123" in ev["raw_command"]
