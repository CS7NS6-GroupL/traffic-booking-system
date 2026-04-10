"""
Extreme stress test -- Traffic Booking System (CS7NS6).

Focused on finding system limits, not general coverage. Run AFTER load_test.py
has confirmed baseline correctness.

Usage:
    pip install httpx rich
    python scripts/stress_test.py --token YOUR_JWT
    python scripts/stress_test.py --token YOUR_JWT --workers 300

Tests:
  S1 -- 5000-request concurrent burst         (find journey-management queue depth)
  S2 -- 200-simultaneous cap-1 contention     (Redis lock correctness at scale)
  S3 -- 20 concurrent cross-region sagas      (saga coordinator under parallel load)
  S4 -- 100 req/s sustained for 120 s         (extended sustained load, ~12 000 requests)
"""

import argparse
import base64
import concurrent.futures
import datetime
import json
import statistics
import sys
import time
import uuid
from dataclasses import dataclass
from typing import List, Optional

import httpx


# ---------------------------------------------------------------------------
# Tee writer -- simultaneous terminal + file output, UTF-8, no encoding crash
# ---------------------------------------------------------------------------

class _Tee:
    """Write to multiple streams; skip any that fail (e.g. cp1252 console)."""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
            except (UnicodeEncodeError, UnicodeDecodeError):
                try:
                    s.write(data.encode("ascii", errors="replace").decode("ascii"))
                except Exception:
                    pass
            except Exception:
                pass

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass

    def fileno(self):
        return self.streams[0].fileno()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default="http://localhost")
parser.add_argument("--token",   default="", help="Bearer token for a DRIVER account")
parser.add_argument("--workers", type=int, default=300, help="Thread pool size")
parser.add_argument("--output",  default="",
                    help="Write output to this file (default: auto-named stress_YYYYMMDD_HHMMSS.txt)")
args = parser.parse_args()

BASE   = args.base_url.rstrip("/")
AUTH_H = {"Authorization": f"Bearer {args.token}", "Content-Type": "application/json"}
W      = args.workers

# Test parameters
S1_N          = 5_000    # concurrent booking burst
S2_N          = 200      # cap-1 contention attempts
S3_N          = 20       # concurrent cross-region sagas
S4_RPS        = 100      # sustained req/s
S4_DURATION   = 120      # seconds
POLL_TIMEOUT  = 45.0     # longer timeout for cross-region sagas under load
POLL_SAMPLE   = 200      # booking IDs to sample for outcome distribution

# Custom graph node IDs (bypass Nominatim geocoding)
ORIGIN_CROSS = "laos-pakse"
DEST_CROSS   = "khm-phnom-penh"
ORIGIN_CAP   = "and-la-massana"
DEST_CAP     = "and-arinsal"

ROUTE_POOL = [
    ("laos-vientiane",       "laos-luang-prabang"),
    ("laos-vientiane",       "laos-thakhek"),
    ("laos-vientiane",       "laos-savannakhet"),
    ("laos-thakhek",         "laos-savannakhet"),
    ("laos-savannakhet",     "laos-pakse"),
    ("khm-phnom-penh",       "khm-battambang"),
    ("khm-phnom-penh",       "khm-siem-reap"),
    ("khm-phnom-penh",       "khm-kompong-cham"),
    ("khm-kompong-cham",     "khm-kratie"),
    ("khm-battambang",       "khm-siem-reap"),
    ("and-andorra-la-vella", "and-encamp"),
    ("and-andorra-la-vella", "and-ordino"),
    ("and-andorra-la-vella", "and-sant-julia"),
    ("and-ordino",           "and-canillo"),
    ("and-escaldes",         "and-encamp"),
]

VALIDATION_SERVICES = {
    "laos":     "http://localhost:8005",
    "cambodia": "http://localhost:8015",
    "andorra":  "http://localhost:8025",
}

# ---------------------------------------------------------------------------
# HTTP client (connection-pooled, shared across all tests)
# ---------------------------------------------------------------------------

_CLIENT: Optional[httpx.Client] = None

def _client() -> httpx.Client:
    global _CLIENT
    if _CLIENT is None or _CLIENT.is_closed:
        _CLIENT = httpx.Client(
            limits=httpx.Limits(
                max_connections=W + 100,
                max_keepalive_connections=W,
            ),
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=20.0),
        )
    return _CLIENT

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _jwt_sub(token: str) -> str:
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.b64decode(payload_b64)).get("sub", "driver-stress")
    except Exception:
        return "driver-stress"

DRIVER_ID = _jwt_sub(args.token)


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


def poll_outcome(booking_id: str, timeout_sec: float = POLL_TIMEOUT) -> str:
    """Poll until terminal status. Returns approved/rejected/cancelled/timeout."""
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


def reset_capacity():
    total = 0
    for region, url in VALIDATION_SERVICES.items():
        try:
            r = httpx.post(f"{url}/internal/reset-counters", timeout=5)
            n = r.json().get("reset", 0)
            total += n
            print(f"  [reset] {region}: {n} counters zeroed")
        except Exception as exc:
            print(f"  [reset] {region}: FAILED ({exc})")
    print(f"  [reset] done -- {total} counters reset\n")


def print_stats(title: str, results: List[Result], wall_ms: float = 0.0):
    latencies = [r.latency_ms for r in results]
    successes = sum(1 for r in results if r.ok)
    failures  = len(results) - successes
    total     = len(results)
    print(f"\n{'='*68}")
    print(f"  {title}")
    print(f"{'='*68}")
    print(f"  Total       : {total}")
    print(f"  Succeeded   : {successes}  ({100*successes//total if total else 0}%)")
    print(f"  Failed      : {failures}  ({100*failures//total if total else 0}%)")
    if latencies:
        print(f"  p50 latency : {percentile(latencies, 50):.0f} ms")
        print(f"  p95 latency : {percentile(latencies, 95):.0f} ms")
        print(f"  p99 latency : {percentile(latencies, 99):.0f} ms")
        print(f"  mean latency: {statistics.mean(latencies):.0f} ms")
        print(f"  max latency : {max(latencies):.0f} ms")
    if wall_ms > 0:
        rps = total / (wall_ms / 1000)
        print(f"  Wall time   : {wall_ms:.0f} ms  ({wall_ms/1000:.1f} s)")
        print(f"  Throughput  : {rps:.1f} req/s")
    codes = {}
    for r in results:
        codes[r.status] = codes.get(r.status, 0) + 1
    print(f"  Status codes: { {k: v for k, v in sorted(codes.items())} }")


def poll_sample(results: List[Result], n: int):
    """Concurrently poll a sample of booking IDs and print outcome distribution."""
    import random
    ids = [r.booking_id for r in results if r.booking_id]
    sample = random.sample(ids, min(n, len(ids)))
    print(f"\n  Polling {len(sample)} outcomes (sample of {len(ids)})...", end="", flush=True)
    outcomes: dict[str, int] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(W, len(sample))) as pool:
        for o in pool.map(poll_outcome, sample):
            outcomes[o] = outcomes.get(o, 0) + 1
            print(".", end="", flush=True)
    print()
    for o, cnt in sorted(outcomes.items()):
        pct = 100 * cnt // len(sample)
        print(f"    {o:<12}: {cnt}  ({pct}%)")
    return outcomes


# ---------------------------------------------------------------------------
# S1 -- 5 000-request concurrent burst
# ---------------------------------------------------------------------------

def test_s1_burst(n=S1_N):
    print(f"\n[S1] 5 000-request burst  ({n} concurrent, {W} workers)")
    print(f"     Routes rotated across {len(ROUTE_POOL)} pairs")
    wall_start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=W) as pool:
        futures = [
            pool.submit(post_booking, *ROUTE_POOL[i % len(ROUTE_POOL)])
            for i in range(n)
        ]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]
    wall_ms = (time.perf_counter() - wall_start) * 1000
    print_stats(f"S1 -- {n}-request burst", results, wall_ms)
    poll_sample(results, POLL_SAMPLE)
    return results, wall_ms


# ---------------------------------------------------------------------------
# S2 -- 200-simultaneous cap-1 contention
# ---------------------------------------------------------------------------

def test_s2_capacity(n=S2_N):
    """
    200 simultaneous bookings on La Massana->Arinsal (cap=1).
    Exactly 1 should be approved; 199 rejected.
    """
    print(f"\n[S2] Capacity contention -- La Massana->Arinsal (cap=1), {n} simultaneous")
    wall_start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        futures = [
            pool.submit(post_booking, ORIGIN_CAP, DEST_CAP, f"veh-stress-{uuid.uuid4().hex[:6]}")
            for _ in range(n)
        ]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]
    wall_ms = (time.perf_counter() - wall_start) * 1000
    print_stats(f"S2 -- {n}-way cap-1 contention (submission)", results, wall_ms)

    booking_ids = [r.booking_id for r in results if r.booking_id]
    print(f"\n  Polling all {len(booking_ids)} outcomes...", end="", flush=True)
    outcomes: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(W, n)) as pool:
        fut_map = {pool.submit(poll_outcome, bid, POLL_TIMEOUT): bid for bid in booking_ids}
        for fut in concurrent.futures.as_completed(fut_map):
            outcomes[fut_map[fut]] = fut.result()
            print(".", end="", flush=True)
    print()

    approved = sum(1 for o in outcomes.values() if o == "approved")
    rejected = sum(1 for o in outcomes.values() if o == "rejected")
    timeouts = sum(1 for o in outcomes.values() if o == "timeout")
    print(f"\n  RESULT : {approved} approved  {rejected} rejected  {timeouts} timed out  (n={n})")
    print(f"  cap=1  : {'PASS -- exactly 1 approved' if approved == 1 else 'FAIL -- ' + str(approved) + ' approved (expected 1)'}")
    return results, approved, rejected


# ---------------------------------------------------------------------------
# S3 -- 20 concurrent cross-region sagas
# ---------------------------------------------------------------------------

def test_s3_concurrent_sagas(n=S3_N):
    """
    Fire N cross-region saga requests simultaneously.
    Stresses the journey-management leader's saga coordinator under parallel load.
    All should complete (approved or rejected) without getting stuck PENDING.
    """
    print(f"\n[S3] {n} concurrent cross-region sagas  ({ORIGIN_CROSS} -> {DEST_CROSS})")
    print(f"     Each saga: 2 sub-bookings (Laos + Cambodia), 4 Redis locks, 2 Mongo writes")

    wall_start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        futures = [
            pool.submit(post_booking, ORIGIN_CROSS, DEST_CROSS, f"veh-saga-{uuid.uuid4().hex[:6]}")
            for _ in range(n)
        ]
        submissions = [f.result() for f in concurrent.futures.as_completed(futures)]
    submit_wall_ms = (time.perf_counter() - wall_start) * 1000
    print_stats(f"S3 -- {n} concurrent saga submissions", submissions, submit_wall_ms)

    # Poll all to terminal state
    booking_ids = [r.booking_id for r in submissions if r.booking_id]
    print(f"\n  Polling {len(booking_ids)} saga outcomes (timeout {POLL_TIMEOUT}s each)...",
          end="", flush=True)
    e2e_times: list[float] = []
    outcomes: dict[str, int] = {}
    t_poll_start = time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(W, n)) as pool:
        t0s = {bid: time.perf_counter() for bid in booking_ids}
        fut_map = {pool.submit(poll_outcome, bid, POLL_TIMEOUT): bid for bid in booking_ids}
        for fut in concurrent.futures.as_completed(fut_map):
            bid = fut_map[fut]
            o = fut.result()
            outcomes[o] = outcomes.get(o, 0) + 1
            e2e_times.append((time.perf_counter() - t0s[bid]) * 1000)
            print(".", end="", flush=True)
    print()

    print(f"\n  Saga outcomes ({len(booking_ids)} total):")
    for o, cnt in sorted(outcomes.items()):
        print(f"    {o:<12}: {cnt}")

    stuck = outcomes.get("timeout", 0)
    if stuck:
        print(f"\n  WARNING: {stuck} sagas timed out (still PENDING after {POLL_TIMEOUT}s)")
        print(f"           This may indicate leader election lag or Kafka consumer backlog.")
    else:
        print(f"\n  All {len(booking_ids)} sagas reached terminal state -- no stuck PENDING.")

    if e2e_times:
        print(f"\n  End-to-end saga latency (poll start -> terminal):")
        print(f"    p50  = {percentile(e2e_times, 50):.0f} ms")
        print(f"    p95  = {percentile(e2e_times, 95):.0f} ms")
        print(f"    mean = {statistics.mean(e2e_times):.0f} ms")
        print(f"    max  = {max(e2e_times):.0f} ms")

    return submissions, outcomes


# ---------------------------------------------------------------------------
# S4 -- 100 req/s sustained for 120 seconds (~12 000 requests)
# ---------------------------------------------------------------------------

def test_s4_sustained(duration_sec=S4_DURATION, rps_target=S4_RPS):
    print(f"\n[S4] Sustained load -- {rps_target} req/s x {duration_sec}s"
          f"  (~{rps_target * duration_sec:,} requests)")
    interval = 1.0 / rps_target
    results: list[Result] = []

    # Buckets: collect latency every 30s to show degradation curve
    buckets: list[list[float]] = []
    bucket: list[float] = []
    bucket_end = time.perf_counter() + 30.0

    i = 0
    deadline = time.perf_counter() + duration_sec
    with concurrent.futures.ThreadPoolExecutor(max_workers=W) as pool:
        pending: list[concurrent.futures.Future] = []
        while time.perf_counter() < deadline:
            origin, dest = ROUTE_POOL[i % len(ROUTE_POOL)]
            pending.append(pool.submit(post_booking, origin, dest))
            i += 1
            time.sleep(interval)
        results = [f.result() for f in concurrent.futures.as_completed(pending)]

    print_stats(f"S4 -- {rps_target} req/s x {duration_sec}s", results,
                wall_ms=duration_sec * 1000)
    poll_sample(results, POLL_SAMPLE)
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Set up output file (always written; also echoed to terminal)
    _outfile_name = args.output or (
        "stress_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".txt"
    )
    _outfile = open(_outfile_name, "w", encoding="utf-8", errors="replace")
    sys.stdout = _Tee(sys.__stdout__, _outfile)
    print(f"Output also saved to: {_outfile_name}\n")

    if not args.token:
        print("WARNING: No --token provided. Requests will fail with 401.")
        print("  Login: curl -s -X POST http://localhost/api/auth/login \\")
        print("           -H 'Content-Type: application/json' \\")
        print("           -d '{\"username\":\"alice\",\"password\":\"password123\"}'")
        print("  Then:  python scripts/stress_test.py --token <token>")
        print()

    print("=" * 68)
    print("  Traffic Booking System -- Extreme Stress Test")
    print(f"  Target: {BASE}   Workers: {W}")
    print("=" * 68)

    reset_capacity()

    # S1 -- 5 000-request burst
    s1, s1_wall_ms = test_s1_burst()
    reset_capacity()

    # S2 -- 200-simultaneous cap-1 contention
    s2, s2_approved, s2_rejected = test_s2_capacity()
    reset_capacity()

    # S3 -- 20 concurrent cross-region sagas
    s3_submissions, s3_outcomes = test_s3_concurrent_sagas()
    reset_capacity()

    # S4 -- 100 req/s x 120s sustained
    s4 = test_s4_sustained()
    reset_capacity()

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    print("\n" + "=" * 68)
    print("  STRESS TEST SUMMARY")
    print("=" * 68)

    def _lat(results, ok_only=True):
        return [r.latency_ms for r in results if (r.ok if ok_only else True)]

    s1_lat = _lat(s1)
    s4_lat = _lat(s4)

    if s1_lat:
        print(f"\n  S1 -- {S1_N:,}-req burst ({W} workers)")
        print(f"    p50={percentile(s1_lat,50):.0f}ms  p95={percentile(s1_lat,95):.0f}ms"
              f"  mean={statistics.mean(s1_lat):.0f}ms"
              f"  throughput~{S1_N/(s1_wall_ms/1000):.0f} req/s")

    print(f"\n  S2 -- {S2_N}-way cap=1 contention")
    print(f"    Approved={s2_approved}  Rejected={s2_rejected}"
          f"  {'PASS' if s2_approved == 1 else 'FAIL'}")

    approved_sagas = s3_outcomes.get("approved", 0)
    rejected_sagas = s3_outcomes.get("rejected", 0)
    stuck_sagas    = s3_outcomes.get("timeout", 0)
    print(f"\n  S3 -- {S3_N} concurrent cross-region sagas")
    print(f"    Approved={approved_sagas}  Rejected={rejected_sagas}  Stuck={stuck_sagas}"
          f"  {'PASS -- none stuck' if stuck_sagas == 0 else 'CHECK -- ' + str(stuck_sagas) + ' stuck'}")

    if s4_lat:
        print(f"\n  S4 -- {S4_RPS} req/s x {S4_DURATION}s sustained (~{S4_RPS*S4_DURATION:,} req)")
        print(f"    p50={percentile(s4_lat,50):.0f}ms  p95={percentile(s4_lat,95):.0f}ms"
              f"  mean={statistics.mean(s4_lat):.0f}ms  total={len(s4)}")

    # -----------------------------------------------------------------------
    # Cleanup
    # -----------------------------------------------------------------------
    import subprocess
    print("\n" + "=" * 68)
    print("  CLEANUP -- clearing test bookings from all MongoDB instances")
    print("=" * 68)
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
    _outfile.close()
    sys.stdout = sys.__stdout__
    print(f"\nResults saved to: {_outfile_name}")
