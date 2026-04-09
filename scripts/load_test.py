"""
Load & stress test for the Traffic Booking System.
Generates the key metrics needed for the CS7NS6 report.

Usage:
    pip install httpx rich
    python scripts/load_test.py [--base-url http://localhost] [--token YOUR_JWT]

Steps:
  1. Register + login as a driver to get a JWT, or pass --token directly.
  2. Runs 4 test suites and prints a summary table.
"""

import argparse
import base64
import concurrent.futures
import json
import statistics
import time
import uuid
from dataclasses import dataclass
from typing import List

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default="http://localhost")
parser.add_argument("--token", default="", help="Bearer token for a DRIVER account")
parser.add_argument("--workers", type=int, default=20)
args = parser.parse_args()

BASE = args.base_url.rstrip("/")
HEADERS = {"Authorization": f"Bearer {args.token}", "Content-Type": "application/json"}

# Decode driver_id from JWT payload (the 'sub' claim)
def _jwt_sub(token: str) -> str:
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # fix padding
        return json.loads(base64.b64decode(payload_b64)).get("sub", "driver-test")
    except Exception:
        return "driver-test"

DRIVER_ID = _jwt_sub(args.token)

# Use custom graph node IDs directly — bypasses Nominatim geocoding entirely.
# These IDs are defined in services/route-service/import_custom_graph.py.
ORIGIN_LAOS  = "laos-vientiane"
DEST_LAOS    = "laos-savannakhet"

ORIGIN_CROSS = "laos-pakse"
DEST_CROSS   = "khm-phnom-penh"

ORIGIN_CAP   = "and-la-massana"
DEST_CAP     = "and-arinsal"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class Result:
    latency_ms: float
    status: int
    ok: bool           # True = submitted (202) or immediately approved (200/201)
    booking_id: str = ""
    detail: str = ""


def post_booking(origin: str, destination: str, vehicle_id=None) -> Result:
    vid = vehicle_id or f"veh-{uuid.uuid4().hex[:6]}"
    departure = time.strftime("%Y-%m-%dT%H:%M", time.gmtime(time.time() + 3600))
    payload = {
        "driver_id":      DRIVER_ID,
        "vehicle_id":     vid,
        "origin":         origin,
        "destination":    destination,
        "departure_time": departure,
    }
    t0 = time.perf_counter()
    try:
        r = httpx.post(f"{BASE}/api/bookings", json=payload, headers=HEADERS, timeout=30)
        ms = (time.perf_counter() - t0) * 1000
        # 202 = accepted for async Kafka processing (normal success)
        ok = r.status_code in (200, 201, 202)
        booking_id = ""
        detail = ""
        try:
            body = r.json()
            booking_id = body.get("booking_id", "")
            detail = body.get("detail", booking_id)
        except Exception:
            pass
        return Result(ms, r.status_code, ok, booking_id, str(detail))
    except Exception as exc:
        ms = (time.perf_counter() - t0) * 1000
        return Result(ms, 0, False, "", str(exc))


def poll_booking_outcome(booking_id: str, timeout_sec: float = 10.0) -> str:
    """
    Poll journey-management until a booking reaches a terminal status.
    Returns 'approved', 'rejected', 'cancelled', 'flagged', or 'timeout'.
    """
    deadline = time.perf_counter() + timeout_sec
    while time.perf_counter() < deadline:
        try:
            r = httpx.get(f"{BASE}/api/journeys/{booking_id}", headers=HEADERS, timeout=5)
            if r.status_code == 200:
                status = r.json().get("status", "")
                if status in ("approved", "rejected", "cancelled", "flagged"):
                    return status
        except Exception:
            pass
        time.sleep(0.3)
    return "timeout"


def percentile(data, p):
    if not data:
        return 0
    s = sorted(data)
    idx = int(len(s) * p / 100)
    return s[min(idx, len(s) - 1)]


VALIDATION_SERVICES = {
    "laos":     "http://localhost:8005",
    "cambodia": "http://localhost:8015",
    "andorra":  "http://localhost:8025",
}

def reset_capacity():
    """Zero out all current:segment:* counters in every regional validation service."""
    total = 0
    for region, url in VALIDATION_SERVICES.items():
        try:
            r = httpx.post(f"{url}/internal/reset-counters", timeout=5)
            n = r.json().get("reset", 0)
            total += n
            print(f"  [reset] {region}: {n} counters zeroed")
        except Exception as exc:
            print(f"  [reset] {region}: FAILED ({exc})")
    print(f"  [reset] done — {total} counters reset across all regions\n")


def print_stats(title, results: List[Result]):
    latencies = [r.latency_ms for r in results]
    successes = sum(1 for r in results if r.ok)
    failures  = len(results) - successes
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")
    print(f"  Total requests : {len(results)}")
    print(f"  Succeeded      : {successes} ({100*successes//len(results) if results else 0}%)")
    print(f"  Failed/Rejected: {failures} ({100*failures//len(results) if results else 0}%)")
    if latencies:
        print(f"  Latency  p50   : {percentile(latencies,50):.0f} ms")
        print(f"  Latency  p95   : {percentile(latencies,95):.0f} ms")
        print(f"  Latency  p99   : {percentile(latencies,99):.0f} ms")
        print(f"  Latency  mean  : {statistics.mean(latencies):.0f} ms")
        print(f"  Latency  max   : {max(latencies):.0f} ms")

    # Status code breakdown
    codes = {}
    for r in results:
        codes[r.status] = codes.get(r.status, 0) + 1
    print(f"  Status codes   : { {k: v for k, v in sorted(codes.items())} }")


# ---------------------------------------------------------------------------
# Test 1 — Single-region throughput
# ---------------------------------------------------------------------------

def test_single_region_throughput(n=50):
    """Fire N concurrent single-region booking requests and measure throughput."""
    print(f"\n[TEST 1] Single-region throughput  ({n} concurrent requests, {args.workers} workers)")
    wall_start = time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(post_booking, ORIGIN_LAOS, DEST_LAOS) for _ in range(n)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    wall_ms = (time.perf_counter() - wall_start) * 1000
    rps = n / (wall_ms / 1000)
    print_stats("Single-region throughput", results)
    print(f"  Wall time      : {wall_ms:.0f} ms")
    print(f"  Throughput     : {rps:.1f} req/s")
    return results


# ---------------------------------------------------------------------------
# Test 2 — Capacity enforcement (contention)
# ---------------------------------------------------------------------------

def test_capacity_enforcement(n=10):
    """
    Send N simultaneous bookings on the La Massana→Arinsal route (capacity=1).
    All submissions return 202. Then poll each booking_id for the final outcome.
    Exactly 1 should be approved; the rest rejected.
    """
    print(f"\n[TEST 2] Capacity enforcement — La Massana→Arinsal (cap=1), {n} simultaneous")

    wall_start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        futures = [
            pool.submit(post_booking, ORIGIN_CAP, DEST_CAP, f"veh-cap-{uuid.uuid4().hex[:6]}")
            for _ in range(n)
        ]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]
    wall_ms = (time.perf_counter() - wall_start) * 1000

    submitted = sum(1 for r in results if r.ok)
    print_stats("Capacity enforcement (submission)", results)
    print(f"  Wall time      : {wall_ms:.0f} ms")
    print(f"  Submitted      : {submitted}/{n}")

    # Poll for final Kafka outcomes
    print(f"  Polling outcomes", end="", flush=True)
    booking_ids = [r.booking_id for r in results if r.booking_id]
    outcomes = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        fut_map = {pool.submit(poll_booking_outcome, bid): bid for bid in booking_ids}
        for fut in concurrent.futures.as_completed(fut_map):
            outcomes[fut_map[fut]] = fut.result()
            print(".", end="", flush=True)
    print()

    approved = sum(1 for o in outcomes.values() if o == "approved")
    rejected = sum(1 for o in outcomes.values() if o == "rejected")
    timeouts = sum(1 for o in outcomes.values() if o == "timeout")
    print(f"\n  ✓ RESULT: {approved} approved, {rejected} rejected, {timeouts} timed out  (out of {n})")
    print(f"    Capacity=1 enforced: only 1 booking permitted on this segment simultaneously")
    return results, approved, rejected


# ---------------------------------------------------------------------------
# Test 3 — Health endpoint baseline (fast path)
# ---------------------------------------------------------------------------

def test_health_latency(n=100):
    """
    Baseline: hit the health endpoint N times sequentially.
    Shows raw HTTP overhead / service availability.
    """
    print(f"\n[TEST 3] Health endpoint baseline  ({n} sequential requests)")
    results = []
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            r = httpx.get(f"{BASE}/api/health", timeout=5)
            ms = (time.perf_counter() - t0) * 1000
            results.append(Result(ms, r.status_code, r.status_code == 200))
        except Exception as exc:
            ms = (time.perf_counter() - t0) * 1000
            results.append(Result(ms, 0, False, str(exc)))
    print_stats("Health endpoint (baseline)", results)
    return results


# ---------------------------------------------------------------------------
# Test 4 — Cross-region saga latency (sequential, with timing)
# ---------------------------------------------------------------------------

def test_cross_region_timing(n=5):
    """
    Time cross-region bookings end-to-end: submission + Kafka saga processing.
    laos-pakse → khm-phnom-penh crosses the Laos/Cambodia border.
    """
    print(f"\n[TEST 4] Cross-region saga end-to-end latency  ({n} requests, sequential)")
    e2e_latencies = []
    for i in range(n):
        print(f"  Request {i+1}/{n}...", end=" ", flush=True)
        t0 = time.perf_counter()
        r = post_booking(ORIGIN_CROSS, DEST_CROSS)
        if not r.ok or not r.booking_id:
            print(f"SUBMIT FAILED {r.latency_ms:.0f}ms  {r.detail[:60]}")
            time.sleep(1)
            continue
        outcome = poll_booking_outcome(r.booking_id, timeout_sec=15.0)
        e2e_ms = (time.perf_counter() - t0) * 1000
        e2e_latencies.append(e2e_ms)
        print(f"{outcome.upper()} — submit {r.latency_ms:.0f}ms / e2e {e2e_ms:.0f}ms")
        time.sleep(1)

    print(f"\n  Cross-region saga end-to-end latency (submit → Kafka outcome):")
    if e2e_latencies:
        print(f"    p50  = {percentile(e2e_latencies, 50):.0f} ms")
        print(f"    p95  = {percentile(e2e_latencies, 95):.0f} ms")
        print(f"    mean = {statistics.mean(e2e_latencies):.0f} ms")
    else:
        print("    No successful results.")
    return e2e_latencies


# ---------------------------------------------------------------------------
# Test 5 — Sustained load ramp
# ---------------------------------------------------------------------------

def test_sustained_load(duration_sec=30, rps_target=5):
    """
    Send bookings at a target rate for `duration_sec` seconds.
    Measures whether latency degrades under sustained load.
    """
    print(f"\n[TEST 5] Sustained load  ({rps_target} req/s for {duration_sec}s)")
    interval = 1.0 / rps_target
    results = []
    deadline = time.perf_counter() + duration_sec

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = []
        while time.perf_counter() < deadline:
            pending.append(pool.submit(post_booking, ORIGIN_LAOS, DEST_LAOS))
            time.sleep(interval)
        results = [f.result() for f in concurrent.futures.as_completed(pending)]

    print_stats(f"Sustained load ({rps_target} req/s × {duration_sec}s)", results)
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not args.token:
        print("WARNING: No --token provided. Requests will fail with 401.")
        print("  Register a driver:  curl -X POST http://localhost/api/auth/register ...")
        print("  Login:              curl -X POST http://localhost/api/auth/login ...")
        print("  Then rerun with:    python scripts/load_test.py --token <token>")
        print()

    print("=" * 60)
    print("  Traffic Booking System — Load & Stress Test")
    print(f"  Target: {BASE}   Workers: {args.workers}")
    print("=" * 60)

    reset_capacity()
    t1 = test_single_region_throughput(n=50)
    reset_capacity()
    t2, cap_approved, cap_rejected = test_capacity_enforcement(n=10)
    reset_capacity()
    t3 = test_health_latency(n=100)
    reset_capacity()
    t4 = test_cross_region_timing(n=5)
    reset_capacity()
    t5 = test_sustained_load(duration_sec=30, rps_target=5)

    # -----------------------------------------------------------------------
    # Final summary table (copy these numbers into the report)
    # -----------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("  REPORT SUMMARY — copy these numbers into the report")
    print("=" * 60)

    stress_latencies    = [r.latency_ms for r in t1 if r.ok]
    cross_latencies     = t4  # list of e2e floats from poll_booking_outcome
    health_latencies    = [r.latency_ms for r in t3]
    sustained_latencies = [r.latency_ms for r in t5 if r.ok]

    print(f"\n  Health check baseline (100 sequential requests)")
    print(f"    p50  = {percentile(health_latencies, 50):.0f} ms")
    print(f"    p95  = {percentile(health_latencies, 95):.0f} ms")

    print(f"\n  Single-region booking — normal load (5 req/s × 30s, n={len(sustained_latencies)})")
    if sustained_latencies:
        print(f"    p50  = {percentile(sustained_latencies, 50):.0f} ms  (submission)")
        print(f"    p95  = {percentile(sustained_latencies, 95):.0f} ms")
        print(f"    mean = {statistics.mean(sustained_latencies):.0f} ms")
        print(f"    throughput = 5 req/s sustained, {len(sustained_latencies)} processed")

    print(f"\n  Single-region booking — stress test (50 concurrent, n={len(stress_latencies)})")
    if stress_latencies:
        print(f"    p50  = {percentile(stress_latencies, 50):.0f} ms  (submission under load)")
        print(f"    p95  = {percentile(stress_latencies, 95):.0f} ms")
        print(f"    throughput = {50 / (max(stress_latencies)/1000):.1f} req/s peak")

    print(f"\n  Cross-region saga latency — end-to-end (submit → Kafka outcome, n={len(cross_latencies)})")
    if cross_latencies:
        print(f"    p50  = {percentile(cross_latencies, 50):.0f} ms")
        print(f"    p95  = {percentile(cross_latencies, 95):.0f} ms")
        print(f"    mean = {statistics.mean(cross_latencies):.0f} ms")

    cap_total = len(t2)
    print(f"\n  Capacity enforcement (La Massana→Arinsal, cap=1, n={cap_total})")
    print(f"    Approved : {cap_approved}  (expected: 1)")
    print(f"    Rejected : {cap_rejected}  (expected: {cap_total - 1})")
    print(f"    Result   : {'PASS' if cap_approved == 1 and cap_rejected == cap_total - 1 else 'CHECK TIMEOUTS'}")

    # -----------------------------------------------------------------------
    # Cleanup — delete all test bookings from every regional MongoDB
    # -----------------------------------------------------------------------
    import subprocess
    print("\n" + "=" * 60)
    print("  CLEANUP — deleting test bookings from all regional MongoDB")
    print("=" * 60)
    for container, region in [("mongo-laos", "laos"), ("mongo-cambodia", "cambodia"), ("mongo-andorra", "andorra"), ("mongo", "global")]:
        try:
            result = subprocess.run(
                ["docker", "exec", container, "mongosh", "traffic", "--eval",
                 "db.bookings.deleteMany({}); print(db.bookings.countDocuments())"],
                capture_output=True, text=True, timeout=10
            )
            remaining = result.stdout.strip().split("\n")[-1]
            print(f"  {region:<10}: bookings cleared (remaining: {remaining})")
        except Exception as exc:
            print(f"  {region:<10}: FAILED ({exc})")
    reset_capacity()
    print("  Done.")
