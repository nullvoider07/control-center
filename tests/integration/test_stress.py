"""Concurrent-load stress test against the live TLS stack.

Drives N worker threads issuing M authenticated ExecuteCommand calls each (safe
mousemoves on the isolated Xvfb :99 display), then reports throughput and latency
percentiles and asserts a clean run (no errors, agent still alive afterwards).

Scale via env: CC_STRESS_WORKERS (default 8), CC_STRESS_PER_WORKER (default 25).
"""
import os
import statistics
import threading
import time

import grpc

import harness
from conftest import raw_stub, meta, requires_stack
from controller.integrations.gRPC import GRPCClient
from controller.integrations.proto import control_center_pb2 as pb
from controller.integrations.proto import control_center_pb2_grpc

pytestmark = requires_stack

# The server rate-limits execute to 100 req / 60s PER USER (JWT sub). Give each
# worker its own sub so this test stresses server routing + the single agent
# stream under concurrency, not the per-user limiter (exercised separately below).
WORKERS = int(os.environ.get("CC_STRESS_WORKERS", "8"))
PER_WORKER = int(os.environ.get("CC_STRESS_PER_WORKER", "25"))  # keep <=100/user


def _client(stack, token, timeout=15):
    c = GRPCClient(host=stack.host, port=stack.port, timeout=timeout, use_ssl=True)
    c.channel = c._create_channel()
    c.stub = control_center_pb2_grpc.ControlServiceStub(c.channel)
    c.set_token(token)
    return c


def _percentile(sorted_vals, pct):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def test_concurrent_execute_load(stack):
    # Distinct sub per worker → independent rate-limit buckets.
    worker_tokens = [
        harness.mint_token(["execute"], user=f"stress-w{w}") for w in range(WORKERS)
    ]
    latencies = []
    errors = []
    lock = threading.Lock()

    def worker(wid):
        c = _client(stack, worker_tokens[wid])
        local = []
        try:
            for i in range(PER_WORKER):
                x = 100 + ((wid * 7 + i) % 900)
                y = 100 + ((wid * 13 + i) % 700)
                t0 = time.perf_counter()
                try:
                    r = c.execute_command(
                        argv=["xdotool", "mousemove", str(x), str(y)],
                        human_command=f"{x} {y} move")
                    dt = (time.perf_counter() - t0) * 1000.0
                    if not r.get("success"):
                        with lock:
                            errors.append(f"w{wid}#{i}: {r.get('message')}")
                    else:
                        local.append(dt)
                except Exception as e:  # noqa: BLE001
                    with lock:
                        errors.append(f"w{wid}#{i}: {e!r}")
        finally:
            c.disconnect()
        with lock:
            latencies.extend(local)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(WORKERS)]
    wall_start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - wall_start

    total = WORKERS * PER_WORKER
    ok = len(latencies)
    latencies.sort()
    metrics = {
        "workers": WORKERS,
        "per_worker": PER_WORKER,
        "total": total,
        "ok": ok,
        "errors": len(errors),
        "wall_s": round(wall, 3),
        "throughput_rps": round(ok / wall, 1) if wall else 0.0,
        "lat_ms_mean": round(statistics.mean(latencies), 2) if latencies else 0.0,
        "lat_ms_p50": round(_percentile(latencies, 0.50), 2),
        "lat_ms_p95": round(_percentile(latencies, 0.95), 2),
        "lat_ms_p99": round(_percentile(latencies, 0.99), 2),
        "lat_ms_max": round(latencies[-1], 2) if latencies else 0.0,
    }
    print("\n[stress] " + "  ".join(f"{k}={v}" for k, v in metrics.items()))

    # A clean run: no errors, every command succeeded, agent still serving.
    assert not errors, f"{len(errors)} failures, sample: {errors[:5]}"
    assert ok == total

    # Agent survived the load and still answers a monitor query.
    mon = _client(stack, stack.tokens["monitor"])
    try:
        conns = mon.query_connections()
        assert conns and conns["total_count"] >= 1
    finally:
        mon.disconnect()


def test_rate_limiter_enforced_per_user(stack):
    """A single user firing >100 execute/min must be throttled (RESOURCE_EXHAUSTED)
    after ~100 succeed — the abuse control is real, not advisory."""
    token = harness.mint_token(["execute"], user="ratelimit-victim")
    ch, stub = raw_stub(stack)
    ok = 0
    throttled = 0
    try:
        for i in range(130):
            req = pb.CommandRequest(
                id=f"rl{i}", argv=["xdotool", "mousemove", "300", "300"],
                human_command="300 300 move")
            try:
                stub.ExecuteCommand(req, metadata=meta(token), timeout=5)
                ok += 1
            except grpc.RpcError as e:
                if e.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
                    throttled += 1
                else:
                    raise
    finally:
        ch.close()
    print(f"\n[ratelimit] ok={ok} throttled={throttled} (limit=100/60s)")
    assert ok <= 100, f"limiter let {ok} through (>100)"
    assert throttled >= 20, "limiter never tripped under 130-request burst"


def test_rapid_reconnect_churn(stack):
    """Many short-lived monitor clients connecting/disconnecting must not wedge
    the server or the single agent slot."""
    errors = []
    for i in range(40):
        c = _client(stack, stack.tokens["monitor"], timeout=8)
        try:
            r = c.query_connections()
            if not (r and r["total_count"] >= 1):
                errors.append(f"iter {i}: {r}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"iter {i}: {e!r}")
        finally:
            c.disconnect()
    assert not errors, errors[:5]
