"""
Load & stress test for the Traffic Booking System.
Generates the key metrics needed for the CS7NS6 report.

Usage:
    pip install httpx rich
    python scripts/load_test.py [--base-url http://localhost] [--token YOUR_JWT]
    python scripts/load_test.py --token YOUR_JWT --scale high   # thousands mode

Steps:
  1. Register + login as a driver to get a JWT, or pass --token directly.
  2. Runs test suites and prints a summary table.

Scale modes:
  normal  — small runs, fast, good for development (default)
  high    — thousands of requests, connection-pooled, samples outcomes
"""

import argparse
import base64
import concurrent.futures
import json
import statistics
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default="http://localhost")
parser.add_argument("--token", default="", help="Bearer token for a DRIVER account")
parser.add_argument("--workers", type=int, default=0,
                    help="Thread pool size (0 = auto based on scale)")
parser.add_argument("--scale", choices=["normal", "high"], default="normal",
                    help="normal=50–100 req, high=1000s of req")
args = parser.parse_args()

BASE    = args.base_url.rstrip("/")
AUTH_H  = {"Authorization": f"Bearer {args.token}", "Content-Type": "application/json"}

# Scale-dependent defaults
if args.scale == "high":
    WORKERS         = args.workers or 200
    T1_N            = 1000   # concurrent single-region burst
    T2_N            = 50     # capacity contention attempts
    T4_N            = 10     # cross-region saga samples
    T5_RPS          = 50     # sustained req/s
    T5_DURATION     = 60     # seconds
    T6_N            = 2000   # mega burst
    POLL_SAMPLE     = 100    # how many booking IDs to poll for outcome (avoid thundering herd)
    POLL_TIMEOUT    = 30.0
else:
    WORKERS         = args.workers or 20
    T1_N            = 50
    T2_N            = 10
    T4_N            = 5
    T5_RPS          = 5
    T5_DURATION     = 30
    T6_N            = 200
    POLL_SAMPLE     = 50
    POLL_TIMEOUT    = 10.0


# Decode driver_id from JWT payload (the 'sub' claim)
def _jwt_sub(token: str) -> str:
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.b64decode(payload_b64)).get("sub", "driver-test")
    except Exception:
        return "driver-test"

DRIVER_ID = _jwt_sub(args.token)

# Use custom graph node IDs directly — bypasses Nominatim geocoding entirely.
ORIGIN_CROSS = "laos-pakse"
DEST_CROSS   = "khm-phnom-penh"
ORIGIN_CAP   = "and-la-massana"
DEST_CAP     = "and-arinsal"

# Route pool for throughput tests — rotated round-robin so concurrent requests
# hit DIFFERENT road segments, preventing artificial capacity rejection.
# Each pair is a valid custom-graph route within a single region.
ROUTE_POOL = [
    # Laos (trunk/primary, cap 100–150 per segment)
    ("laos-vientiane",       "laos-luang-prabang"),
    ("laos-vientiane",       "laos-thakhek"),
    ("laos-vientiane",       "laos-savannakhet"),
    ("laos-thakhek",         "laos-savannakhet"),
    ("laos-savannakhet",     "laos-pakse"),
    # Cambodia (trunk/primary, cap 100–150 per segment)
    ("khm-phnom-penh",       "khm-battambang"),
    ("khm-phnom-penh",       "khm-siem-reap"),
    ("khm-phnom-penh",       "khm-kompong-cham"),
    ("khm-kompong-cham",     "khm-kratie"),
    ("khm-battambang",       "khm-siem-reap"),
    # Andorra (secondary/tertiary, cap 50–75 per segment)
    ("and-andorra-la-vella", "and-encamp"),
    ("and-andorra-la-vella", "and-ordino"),
    ("and-andorra-la-vella", "and-sant-julia"),
    ("and-ordino",           "and-canillo"),
    ("and-escaldes",         "and-encamp"),
]

# Shared connection-pooled client — reused across all requests in a test.
# Without this, each request opens+closes a TCP connection: catastrophic at scale.
_CLIENT: Optional[httpx.Client] = None

def _client() -> httpx.Client:
    global _CLIENT
    if _CLIENT is None or _CLIENT.is_closed:
        _CLIENT = httpx.Client(
            limits=httpx.Limits(
                max_connections=WORKERS + 50,
                max_keepalive_connections=WORKERS,
            ),
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=15.0),
        )
    return _CLIENT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class Result:
    latency_ms: float
    status: int
    ok: bool
    booking_id: str = ""
    detail: str = ""


def post_booking(origin: str, destination: str, vehicle_id: str = "") -> Result:
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
        r = _client().post(f"{BASE}/api/bookings", json=payload, headers=AUTH_H)
        ms = (time.perf_counter() - t0) * 1000
        ok = r.status_code in (200, 201, 202)
        booking_id = detail = ""
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


def poll_booking_outcome(booking_id: str, timeout_sec: float = POLL_TIMEOUT) -> str:
    """Poll until terminal status. Returns approved/rejected/cancelled/flagged/timeout."""
    deadline = time.perf_counter() + timeout_sec
    while time.perf_counter() < deadline:
        try:
            r = _client().get(f"{BASE}/api/journeys/{booking_id}", headers=AUTH_H)
            if r.status_code == 200:
                status = r.json().get("status", "")
                if status in ("approved", "rejected", "cancelled", "flagged"):
                    return status
        except Exception:
            pass
        time.sleep(0.2)
    return "timeout"


def percentile(data, p):
    if not data:
        return 0.0
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


def print_stats(title: str, results: List[Result], wall_ms: float = 0.0):
    latencies = [r.latency_ms for r in results]
    successes = sum(1 for r in results if r.ok)
    failures  = len(results) - successes
    total     = len(results)
    print(f"\n{'='*64}")
    print(f"  {title}")
    print(f"{'='*64}")
    print(f"  Total requests : {total}")
    print(f"  Succeeded      : {successes} ({100*successes//total if total else 0}%)")
    print(f"  Failed/Rejected: {failures} ({100*failures//total if total else 0}%)")
    if latencies:
        print(f"  Latency  p50   : {percentile(latencies, 50):.0f} ms")
        print(f"  Latency  p95   : {percentile(latencies, 95):.0f} ms")
        print(f"  Latency  p99   : {percentile(latencies, 99):.0f} ms")
        print(f"  Latency  mean  : {statistics.mean(latencies):.0f} ms")
        print(f"  Latency  max   : {max(latencies):.0f} ms")
    if wall_ms > 0:
        rps = total / (wall_ms / 1000)
        print(f"  Wall time      : {wall_ms:.0f} ms")
        print(f"  Throughput     : {rps:.1f} req/s")
    codes = {}
    for r in results:
        codes[r.status] = codes.get(r.status, 0) + 1
    print(f"  Status codes   : { {k: v for k, v in sorted(codes.items())} }")


def _poll_sample(results: List[Result], n: int, label: str):
    """Poll a random sample of booking IDs and print outcome distribution."""
    ids = [r.booking_id for r in results if r.booking_id]
    import random
    sample = random.sample(ids, min(n, len(ids)))
    print(f"  Polling {len(sample)} booking outcomes (sample of {len(ids)})...", end="", flush=True)
    outcomes: dict[str, int] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(WORKERS, len(sample))) as pool:
        fut_map = {pool.submit(poll_booking_outcome, bid): bid for bid in sample}
        for fut in concurrent.futures.as_completed(fut_map):
            o = fut.result()
            outcomes[o] = outcomes.get(o, 0) + 1
            print(".", end="", flush=True)
    print()
    for o, cnt in sorted(outcomes.items()):
        print(f"    {o:<12}: {cnt}")
    return outcomes


# ---------------------------------------------------------------------------
# Test 1 — Single-region throughput burst
# ---------------------------------------------------------------------------

def test_single_region_throughput(n=T1_N):
    print(f"\n[TEST 1] Single-region throughput burst  ({n} concurrent, {WORKERS} workers)")
    print(f"         Routes rotated across {len(ROUTE_POOL)} pairs to avoid segment saturation")
    wall_start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [
            pool.submit(post_booking, *ROUTE_POOL[i % len(ROUTE_POOL)])
            for i in range(n)
        ]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]
    wall_ms = (time.perf_counter() - wall_start) * 1000
    print_stats("Single-region throughput burst", results, wall_ms)
    if args.scale == "high":
        _poll_sample(results, POLL_SAMPLE, "T1")
    return results


# ---------------------------------------------------------------------------
# Test 2 — Capacity enforcement (contention)
# ---------------------------------------------------------------------------

def test_capacity_enforcement(n=T2_N):
    """
    Send N simultaneous bookings on La Massana→Arinsal (capacity=1).
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
    print_stats("Capacity enforcement (submission)", results, wall_ms)

    booking_ids = [r.booking_id for r in results if r.booking_id]
    print(f"  Polling all {len(booking_ids)} outcomes...", end="", flush=True)
    outcomes: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(WORKERS, n)) as pool:
        fut_map = {pool.submit(poll_booking_outcome, bid, POLL_TIMEOUT): bid for bid in booking_ids}
        for fut in concurrent.futures.as_completed(fut_map):
            outcomes[fut_map[fut]] = fut.result()
            print(".", end="", flush=True)
    print()

    approved = sum(1 for o in outcomes.values() if o == "approved")
    rejected = sum(1 for o in outcomes.values() if o == "rejected")
    timeouts = sum(1 for o in outcomes.values() if o == "timeout")
    print(f"\n  RESULT: {approved} approved, {rejected} rejected, {timeouts} timed out  (n={n})")
    ok = approved == 1 and rejected == n - 1 - timeouts + approved - 1
    print(f"  Capacity=1 enforced: {'PASS — only 1 approved' if approved == 1 else 'CHECK — ' + str(approved) + ' approved (expected 1)'}")
    return results, approved, rejected


# ---------------------------------------------------------------------------
# Test 3 — Health endpoint baseline
# ---------------------------------------------------------------------------

def test_health_latency(n=100):
    print(f"\n[TEST 3] Health endpoint baseline  ({n} sequential requests)")
    results = []
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            r = _client().get(f"{BASE}/api/health", headers=AUTH_H)
            ms = (time.perf_counter() - t0) * 1000
            results.append(Result(ms, r.status_code, r.status_code == 200))
        except Exception as exc:
            ms = (time.perf_counter() - t0) * 1000
            results.append(Result(ms, 0, False, "", str(exc)))
    print_stats("Health endpoint (baseline)", results)
    return results


# ---------------------------------------------------------------------------
# Test 4 — Cross-region saga latency (sequential, with end-to-end timing)
# ---------------------------------------------------------------------------

def test_cross_region_timing(n=T4_N):
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
        outcome = poll_booking_outcome(r.booking_id, timeout_sec=POLL_TIMEOUT)
        e2e_ms = (time.perf_counter() - t0) * 1000
        e2e_latencies.append(e2e_ms)
        print(f"{outcome.upper()} — submit {r.latency_ms:.0f}ms / e2e {e2e_ms:.0f}ms")
        time.sleep(0.5)

    print(f"\n  Cross-region saga end-to-end (submit → Kafka outcome):")
    if e2e_latencies:
        print(f"    p50  = {percentile(e2e_latencies, 50):.0f} ms")
        print(f"    p95  = {percentile(e2e_latencies, 95):.0f} ms")
        print(f"    mean = {statistics.mean(e2e_latencies):.0f} ms")
        print(f"    min  = {min(e2e_latencies):.0f} ms  max = {max(e2e_latencies):.0f} ms")
    else:
        print("    No successful results.")
    return e2e_latencies


# ---------------------------------------------------------------------------
# Test 5 — Sustained load ramp
# ---------------------------------------------------------------------------

def test_sustained_load(duration_sec=T5_DURATION, rps_target=T5_RPS):
    print(f"\n[TEST 5] Sustained load  ({rps_target} req/s for {duration_sec}s"
          f" → ~{rps_target * duration_sec} requests)")
    interval = 1.0 / rps_target
    results = []
    deadline = time.perf_counter() + duration_sec

    # Bucket latency every 10 seconds to show degradation over time
    bucket_results: list[list[Result]] = []
    bucket: list[Result] = []
    bucket_end = time.perf_counter() + 10.0

    i = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        pending = []
        while time.perf_counter() < deadline:
            origin, dest = ROUTE_POOL[i % len(ROUTE_POOL)]
            pending.append(pool.submit(post_booking, origin, dest))
            i += 1
            time.sleep(interval)
        results = [f.result() for f in concurrent.futures.as_completed(pending)]

    print_stats(f"Sustained load ({rps_target} req/s × {duration_sec}s)", results,
                wall_ms=duration_sec * 1000)

    if args.scale == "high":
        _poll_sample(results, POLL_SAMPLE, "T5")

    return results


# ---------------------------------------------------------------------------
# Test 6 — Mega burst (high scale only)
# ---------------------------------------------------------------------------

def test_mega_burst(n=T6_N):
    """
    Fire N requests as fast as possible with the full worker pool.
    Tests the system's peak ingestion rate and Kafka back-pressure behaviour.
    """
    print(f"\n[TEST 6] Mega burst  ({n} requests, {WORKERS} workers, fire-and-forget)")
    wall_start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [
            pool.submit(post_booking, *ROUTE_POOL[i % len(ROUTE_POOL)])
            for i in range(n)
        ]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]
    wall_ms = (time.perf_counter() - wall_start) * 1000
    print_stats(f"Mega burst ({n} requests)", results, wall_ms)

    # Sample a small set to check Kafka drained cleanly
    _poll_sample(results, min(POLL_SAMPLE, 200), "T6")
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not args.token:
        print("WARNING: No --token provided. Requests will fail with 401.")
        print("  Register: curl -X POST http://localhost/api/auth/register ...")
        print("  Login:    curl -X POST http://localhost/api/auth/login ...")
        print("  Then:     python scripts/load_test.py --token <token>")
        print()

    print("=" * 64)
    print("  Traffic Booking System — Load & Stress Test")
    print(f"  Target: {BASE}   Workers: {WORKERS}   Scale: {args.scale.upper()}")
    print("=" * 64)

    reset_capacity()
    t1 = test_single_region_throughput()
    reset_capacity()
    t2, cap_approved, cap_rejected = test_capacity_enforcement()
    reset_capacity()
    t3 = test_health_latency()
    reset_capacity()
    t4 = test_cross_region_timing()
    reset_capacity()
    t5 = test_sustained_load()
    reset_capacity()
    if args.scale == "high":
        t6 = test_mega_burst()
        reset_capacity()
    else:
        t6 = []

    # -----------------------------------------------------------------------
    # Final summary table
    # -----------------------------------------------------------------------
    print("\n" + "=" * 64)
    print("  REPORT SUMMARY — copy these numbers into the report")
    print("=" * 64)

    def _lat(results, ok_only=True):
        return [r.latency_ms for r in results if (r.ok if ok_only else True)]

    health_lat    = _lat(t3, ok_only=False)
    stress_lat    = _lat(t1)
    sustained_lat = _lat(t5)
    cross_lat     = t4
    mega_lat      = _lat(t6) if t6 else []

    print(f"\n  Health check baseline (100 sequential)")
    if health_lat:
        print(f"    p50={percentile(health_lat,50):.0f}ms  p95={percentile(health_lat,95):.0f}ms")

    print(f"\n  Single-region burst ({T1_N} concurrent)")
    if stress_lat:
        print(f"    p50={percentile(stress_lat,50):.0f}ms  p95={percentile(stress_lat,95):.0f}ms"
              f"  mean={statistics.mean(stress_lat):.0f}ms"
              f"  throughput≈{T1_N/(max(stress_lat)/1000):.0f} req/s")

    print(f"\n  Sustained load ({T5_RPS} req/s × {T5_DURATION}s, n={len(sustained_lat)})")
    if sustained_lat:
        print(f"    p50={percentile(sustained_lat,50):.0f}ms  p95={percentile(sustained_lat,95):.0f}ms"
              f"  mean={statistics.mean(sustained_lat):.0f}ms")

    print(f"\n  Cross-region saga e2e (n={len(cross_lat)})")
    if cross_lat:
        print(f"    p50={percentile(cross_lat,50):.0f}ms  p95={percentile(cross_lat,95):.0f}ms"
              f"  mean={statistics.mean(cross_lat):.0f}ms")

    cap_total = len(t2)
    print(f"\n  Capacity enforcement (cap=1, n={cap_total})")
    print(f"    Approved={cap_approved}  Rejected={cap_rejected}"
          f"  {'PASS' if cap_approved == 1 else 'CHECK'}")

    if mega_lat:
        print(f"\n  Mega burst ({T6_N} requests)")
        print(f"    p50={percentile(mega_lat,50):.0f}ms  p95={percentile(mega_lat,95):.0f}ms"
              f"  throughput≈{T6_N/(max(mega_lat)/1000):.0f} req/s")

    # -----------------------------------------------------------------------
    # Cleanup
    # -----------------------------------------------------------------------
    import subprocess
    print("\n" + "=" * 64)
    print("  CLEANUP — deleting test bookings from all regional MongoDB")
    print("=" * 64)
    for container, region in [
        ("mongo-laos",     "laos"),
        ("mongo-cambodia", "cambodia"),
        ("mongo-andorra",  "andorra"),
        ("mongo",          "global"),
    ]:
        try:
            result = subprocess.run(
                ["docker", "exec", container, "mongosh", "traffic", "--eval",
                 "db.bookings.deleteMany({}); print(db.bookings.countDocuments())"],
                capture_output=True, text=True, timeout=15,
            )
            remaining = result.stdout.strip().split("\n")[-1]
            print(f"  {region:<10}: cleared (remaining: {remaining})")
        except Exception as exc:
            print(f"  {region:<10}: FAILED ({exc})")
    reset_capacity()
    print("  Done.")
