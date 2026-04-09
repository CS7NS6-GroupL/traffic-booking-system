# Performance & Load Testing — Traffic Booking System

## Overview

This document presents the results of load and stress testing conducted on the distributed
traffic booking system. Tests were run locally using Docker Compose with all services active.
The test script is at `scripts/load_test.py`.

All booking requests use custom graph node IDs (e.g. `laos-vientiane`) which bypass external
geocoding entirely, ensuring measured latencies reflect only system processing overhead.

**Test environment:**
- All services running in Docker containers on a single host machine
- 3 regional MongoDB instances (mongo-laos, mongo-cambodia, mongo-andorra)
- 3 regional Redis instances (redis-laos, redis-cambodia, redis-andorra)
- 1 Kafka broker (shared across regions in demo mode)
- Custom road graph: 3 regions, 19 nodes, 36 edges

---

## Test 1 — Health Check Baseline

**Purpose:** Establish a baseline for raw HTTP round-trip latency with no business logic.
Hits the `/api/health` endpoint 100 times sequentially.

| Metric | Value |
|---|---|
| Requests | 100 sequential |
| Success rate | 100% |
| p50 latency | 13 ms |
| p95 latency | 33 ms |
| Max latency | 43 ms |

**Interpretation:** The 13ms p50 represents the overhead of nginx proxying through to a
FastAPI service and back. This is the floor — no booking can be faster than this. The
low spread (13ms → 33ms p95) shows the infrastructure layer is stable and consistent.

---

## Test 2 — Single-Region Booking, Normal Load

**Purpose:** Measure booking submission latency at a sustainable request rate.
Sends bookings at 5 requests/second for 30 seconds (150 total).

Route: `laos-vientiane → laos-savannakhet` (single-region, no saga)

| Metric | Value |
|---|---|
| Requests | 150 over 30s |
| Rate | 5 req/s |
| Success rate | 100% (150/150) |
| p50 latency | 157 ms |
| p95 latency | 187 ms |
| p99 latency | 220 ms |
| Mean latency | 160 ms |
| Max latency | 275 ms |

**Interpretation:** At normal load the system is fast and consistent. The 157ms p50 covers
the full synchronous path: nginx → booking-service (JWT validation) → journey-management
(region resolution + A\* route planning) → Kafka produce → HTTP 202 back to client.
The tight spread between p50 and p95 (30ms) shows no queuing or GC pauses at this rate.

---

## Test 3 — Single-Region Booking, Stress Test

**Purpose:** Measure how the system behaves under sudden high concurrency. 50 booking
requests are fired simultaneously using 20 worker threads.

Route: `laos-vientiane → laos-savannakhet`

| Metric | Value |
|---|---|
| Requests | 50 concurrent |
| Workers | 20 threads |
| Success rate | 100% (50/50) |
| p50 latency | 2,436 ms |
| p95 latency | 2,863 ms |
| Mean latency | 2,230 ms |
| Max latency | 2,876 ms |
| Wall time | 6,719 ms |
| Throughput | 7.4 req/s |

**Interpretation:** Under 10× the normal load, latency increases ~15× (157ms → 2,436ms)
but the system accepts every request without dropping or erroring. This is graceful
degradation — the services queue internally rather than shedding load.

The bottleneck is journey-management: it is a singleton service that processes route
planning requests sequentially. With 50 concurrent requests all needing A\* path computation
and Kafka publishing, requests queue at the journey-management HTTP server. In a production
deployment, multiple journey-management replicas (with Redis-based saga leader election)
would distribute this load.

**Normal vs stress comparison:**

| Condition | p50 | p95 | Throughput |
|---|---|---|---|
| Normal load (5 req/s) | 157 ms | 187 ms | 5 req/s |
| Stress (50 concurrent) | 2,436 ms | 2,863 ms | 7.4 req/s |

---

## Test 4 — Cross-Region Saga, End-to-End Latency

**Purpose:** Measure the complete latency of a cross-region booking from HTTP submission
to final Kafka-delivered outcome. This exercises the full saga pattern across two regions.

Route: `laos-pakse → khm-phnom-penh` (Laos → Cambodia, crosses the Voen Kham border)

The measured latency includes:
1. HTTP POST → booking-service
2. Journey-management: region resolution, A\* for each leg (Laos leg + Cambodia leg)
3. Saga creation in MongoDB (global)
4. Kafka publish of 2 sub-bookings (one per region)
5. validation-service-laos: Redis lock + capacity check + Kafka publish outcome
6. validation-service-cambodia: Redis lock + capacity check + Kafka publish outcome
7. Journey-management saga coordinator: collect both outcomes, commit booking to mongo-laos
8. Kafka publish final APPROVED outcome
9. Client polls booking status until terminal state

| Request | Submit | End-to-end |
|---|---|---|
| 1 | 232 ms | 610 ms |
| 2 | 199 ms | 247 ms |
| 3 | 183 ms | 223 ms |
| 4 | 230 ms | 620 ms |
| 5 | 193 ms | 226 ms |

| Metric | Value |
|---|---|
| Success rate | 5/5 APPROVED |
| p50 e2e latency | 247 ms |
| p95 e2e latency | 620 ms |
| Mean e2e latency | 385 ms |
| Submit latency (mean) | 207 ms |

**Interpretation:** The mean end-to-end latency of 385ms for a cross-region saga is the
headline result. This demonstrates that a booking requiring coordination across two
independent regional validation services, two Redis instances, and multiple Kafka round-trips
completes in under 400ms on average.

The bimodal distribution (~225ms vs ~615ms) reflects Kafka poll intervals. When both
regional sub-booking outcomes arrive within the same Kafka consumer poll cycle in
journey-management, the saga resolves in ~225ms. When the second outcome arrives in the
next poll cycle (~400ms later), total time reaches ~615ms. This is expected behaviour from
Kafka's `max.poll.interval.ms` batching.

---

## Test 5 — Capacity Enforcement Under Simultaneous Contention

**Purpose:** Prove that the Redis-based distributed capacity lock correctly enforces a
hard limit under race conditions. 10 bookings are submitted simultaneously for a road
segment with capacity = 1 (La Massana → Arinsal, `road_type=track`).

Each request uses a unique vehicle ID so the duplicate-vehicle check does not interfere —
only the segment capacity limit is under test.

Route: `and-la-massana → and-arinsal` (capacity = 1)

| Metric | Value |
|---|---|
| Simultaneous requests | 10 |
| Segment capacity | 1 |
| Approved | **1** |
| Rejected | **9** |
| Result | **PASS** |
| Submission p50 | 1,364 ms |
| Wall time | 1,368 ms |

**Interpretation:** Exactly 1 booking was approved and 9 were rejected with
`"Road segment at full capacity (1/1)"`. This is the correct result regardless of
submission order or timing.

The enforcement mechanism works as follows:
1. All 10 requests arrive at validation-service-andorra via Kafka
2. Each acquires a Redis distributed lock (`SET NX`) on the segment before checking
3. The first to acquire the lock increments `current:segment:and-la-massana:and-arinsal` from 0 → 1
4. All subsequent requests find `current (1) >= capacity (1)` → REJECTED
5. Locks are released in reverse order (deadlock prevention)

This demonstrates that the system treats road capacity as a hard distributed constraint,
not an approximate one. No two concurrent bookings can both be approved for a saturated
segment, even under race conditions.

---

## Summary Table

| Test | Metric | Value |
|---|---|---|
| Health baseline | p50 latency | 13 ms |
| Health baseline | p95 latency | 33 ms |
| Single-region, normal | p50 submission latency | 157 ms |
| Single-region, normal | p95 submission latency | 187 ms |
| Single-region, normal | Throughput | 5 req/s, 150/150 accepted |
| Single-region, stress | p50 submission latency | 2,436 ms |
| Single-region, stress | Throughput | 7.4 req/s peak, 50/50 accepted |
| Cross-region saga | Mean e2e latency | 385 ms |
| Cross-region saga | p50 e2e latency | 247 ms |
| Capacity enforcement | Correctness | 1/10 approved (cap=1) — PASS |

---

## Road Capacity Reference

Capacity limits are seeded into Redis at startup from each region's `osm_edges` collection.
Values are set by `road_type` and enforced per directed segment. A missing Redis key
defaults to 0 (blocks all bookings until seeded — fail-safe).

| Road type | Capacity | Used in custom graph |
|---|---|---|
| `motorway` | 200 | — |
| `trunk` | 150 | Laos Route 13, Cambodia NR5/NR7 |
| `primary` | 100 | Laos border road, Cambodia Route 7 |
| `secondary` | 75 | Andorra valley roads, Cambodia rural north |
| `tertiary` | 50 | — |
| `tertiary_link` | 25 | — |
| `track` | **1** | Andorra: La Massana → Arinsal (demo) |

---

## How to Reproduce

```bash
# 1. Start all services
docker compose up -d

# 2. Import custom graph into regional MongoDB instances
python services/route-service/import_custom_graph.py
docker restart route-service-laos route-service-cambodia route-service-andorra
docker restart validation-service-laos validation-service-cambodia validation-service-andorra

# 3. Register a driver account
curl -X POST http://localhost/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"password123","vehicle_id":"veh-001","role":"DRIVER"}'

# 4. Login and capture token
TOKEN=$(curl -s -X POST http://localhost/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"password123"}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# 5. Run the load test
pip install httpx
python scripts/load_test.py --token $TOKEN
```
