# Performance & Load Testing — Traffic Booking System

## Overview

This document presents load and stress testing results across two configurations:
- **Pre-hardening** (single-instance services, per-call Kafka producer)
- **Post-hardening** (multi-instance services, Kafka singleton producer, Redis leader election, pub/sub fanout)

Tests were run locally using Docker Compose with all services active.
The test script is at `scripts/load_test.py --scale high`.

All booking requests use custom graph node IDs (e.g. `laos-vientiane`) which bypass external
geocoding entirely, ensuring measured latencies reflect only system processing overhead.

**Test environment:**
- All services running in Docker containers on a single host machine
- 3 regional MongoDB instances (mongo-laos, mongo-cambodia, mongo-andorra)
- 3 regional Redis instances (redis-laos, redis-cambodia, redis-andorra)
- 1 Kafka broker (shared across regions in demo mode)
- Custom road graph: 3 regions, 19 nodes, 36 edges
- Scale: `--scale high` (200 workers)

---

## Before vs After Hardening — Key Comparison

The hardening changes (Kafka singleton producer, multi-instance services, Redis leader election)
produced dramatic improvements in high-concurrency submission latency:

| Test | Metric | Pre-hardening | Post-hardening | Improvement |
|---|---|---|---|---|
| T1 — 1000 concurrent burst | p50 submission latency | 24,360 ms | 3,479 ms | **7× faster** |
| T1 — 1000 concurrent burst | Throughput | 8.1 req/s | 53.4 req/s | **6.6× faster** |
| T2 — Capacity enforcement | p50 submission | 3,203 ms | 594 ms | **5.4× faster** |
| T4 — Cross-region saga e2e | mean | 475 ms | 401 ms | 15% faster |
| T5 — Sustained 50 req/s | p50 submission | 24,212 ms | 81 ms | **299× faster** |
| T6 — 2000 req mega burst | Throughput | 8.2 req/s | 86.5 req/s | **10.5× faster** |

**Root cause of pre-hardening degradation:** the old booking-service created a new
`KafkaProducer` instance on every request — each call opened a TCP connection to the Kafka
broker, sent, flushed, and closed. Under 200 concurrent workers this serialized at the broker,
causing ~24s queueing latency. The singleton producer (one persistent connection, shared across
all threads) eliminates this entirely.

---

## Post-Hardening Results

### Test 1 — Single-Region Throughput Burst

**Purpose:** Maximum concurrency — 1000 requests fired simultaneously across 15 rotated
routes (to avoid segment saturation), 200 worker threads.

| Metric | Value |
|---|---|
| Requests | 1,000 concurrent |
| Workers | 200 threads |
| Success rate | 100% (1000/1000) |
| p50 submission latency | 3,479 ms |
| p95 submission latency | 4,217 ms |
| p99 submission latency | 4,324 ms |
| Mean submission latency | 3,256 ms |
| Max submission latency | 4,379 ms |
| Wall time | 18,736 ms |
| Throughput | **53.4 req/s** |
| Outcome sample (n=100) | 92 approved, 8 rejected |

**Interpretation:** The system accepts all 1000 requests without a single error. The 3.5s p50
under 1000-way concurrency reflects journey-management planning load (A\* + region resolution
for each request) rather than Kafka overhead. The 8% rejection rate in the outcome sample is
expected capacity enforcement — some routes filled their segment counters across the burst.

---

### Test 2 — Capacity Enforcement Under Contention

**Purpose:** Prove that Redis-based capacity locking is correct under race conditions.
50 simultaneous bookings for La Massana → Arinsal (`road_type=track`, capacity = 1).
Each request uses a unique vehicle ID so only segment capacity is under test.

| Metric | Value |
|---|---|
| Simultaneous requests | 50 |
| Segment capacity | 1 |
| p50 submission latency | 594 ms |
| p95 submission latency | 815 ms |
| Wall time | 866 ms |
| **Approved** | **1** |
| **Rejected** | **49** |
| **Result** | **PASS** |

**Interpretation:** Exactly 1 booking approved out of 50 simultaneous attempts at a cap-1
segment — correct regardless of arrival order or timing. The Redis `SET NX` lock ensures
atomicity: the first validation-service consumer to acquire the lock increments the counter
from 0→1; all subsequent checks find `current (1) >= capacity (1)` and reject. The 594ms p50
(vs 3,203ms pre-hardening) reflects the Kafka singleton producer improvement.

---

### Test 3 — Health Endpoint Baseline

**Purpose:** Establish the raw HTTP round-trip floor with no business logic.

| Metric | Value |
|---|---|
| Requests | 100 sequential |
| Success rate | 100% |
| p50 latency | 6 ms |
| p95 latency | 9 ms |
| Max latency | 14 ms |

**Interpretation:** 6ms p50 is the nginx proxy overhead floor. No booking can be faster than
this. The tight spread (6ms → 14ms max) confirms the infrastructure layer is stable.

---

### Test 4 — Cross-Region Saga, End-to-End Latency

**Purpose:** Measure the complete latency from HTTP submission to final Kafka-delivered outcome
for a booking that crosses two regions. This exercises the full distributed saga pattern.

Route: `laos-pakse → khm-phnom-penh` (Laos → Cambodia via Voen Kham border)

The measured latency includes all saga steps:
1. HTTP POST → booking-service → journey-management (A\* for each leg)
2. Saga created in MongoDB, 2 sub-bookings published to Kafka
3. validation-service-laos + validation-service-cambodia: Redis lock + capacity check
4. Saga coordinator collects both outcomes, commits to mongo-laos + mongo-cambodia
5. Final APPROVED published to Kafka, client polls to terminal state

| Request | Submit | End-to-end |
|---|---|---|
| 1 | 202 ms | 468 ms |
| 2 | 93 ms | 364 ms |
| 3 | 119 ms | 412 ms |
| 4 | 184 ms | 464 ms |
| 5 | 97 ms | 372 ms |
| 6 | 126 ms | 407 ms |
| 7 | 106 ms | 392 ms |
| 8 | 132 ms | 417 ms |
| 9 | 122 ms | 376 ms |
| 10 | 94 ms | 340 ms |

| Metric | Value |
|---|---|
| Success rate | 10/10 APPROVED |
| p50 e2e latency | 407 ms |
| p95 e2e latency | 468 ms |
| Mean e2e latency | **401 ms** |
| Min / Max | 340 ms / 468 ms |

**Interpretation:** The mean end-to-end latency of 401ms for a cross-region saga is the
headline distributed correctness result. A booking requiring coordination across two
independent regional validation services, two Redis instances, and multiple Kafka round-trips
completes in under half a second on average — 10/10 requests approved with no failures.

The tight spread (340ms–468ms, only 128ms range) shows consistent saga coordination. The
variance reflects Kafka consumer poll timing: when both regional outcomes arrive within the
same poll cycle the saga resolves faster; when one arrives in the next cycle it adds ~100ms.

---

### Test 5 — Sustained Load

**Purpose:** Measure latency stability under continuous load at the target throughput rate.
50 req/s for 60 seconds (~3000 total requests).

| Metric | Value |
|---|---|
| Target rate | 50 req/s |
| Achieved rate | **48.1 req/s** (96% of target) |
| Total requests | 2,888 |
| Success rate | 100% |
| p50 submission latency | **81 ms** |
| p95 submission latency | 792 ms |
| p99 submission latency | 1,189 ms |
| Mean submission latency | 177 ms |
| Max submission latency | 1,532 ms |
| Outcome sample (n=100) | 43 approved, 57 rejected |

**Interpretation:** Under sustained load at a paced rate (not burst), the p50 drops to 81ms —
close to the health check baseline. This shows the system performs well when requests arrive
at a steady rate rather than all at once. The p95/p99 tail (792ms/1189ms) reflects occasional
journey-management planning bursts when multiple requests cluster together within the same
interval. The 57% rejection rate in the sample reflects segment capacity filling over ~3000
bookings across the 60-second run (expected — counters are not reset mid-test).

---

### Test 6 — Mega Burst

**Purpose:** Peak ingestion test. 2000 requests fired as fast as possible with 200 workers.
Tests maximum throughput and whether Kafka back-pressure is handled gracefully.

| Metric | Value |
|---|---|
| Requests | 2,000 |
| Workers | 200 threads |
| Success rate | 100% |
| p50 submission latency | 2,231 ms |
| p95 submission latency | 2,670 ms |
| p99 submission latency | 2,738 ms |
| Mean submission latency | 2,202 ms |
| Wall time | 23,131 ms |
| Throughput | **86.5 req/s** |
| Outcome sample (n=100) | 57 approved, 43 rejected |

**Interpretation:** The system handles 2000 requests at 86.5 req/s without dropping a single
submission. The 2.2s mean latency under a 2000-way burst is a 10.5× improvement over the
pre-hardening result (23.3s), entirely attributable to the Kafka singleton producer.
The system degrades gracefully — latency increases under extreme load but no requests fail.

---

## Summary Table

| Test | Scale | Metric | Value |
|---|---|---|---|
| Health baseline | 100 sequential | p50 / p95 | 6 ms / 9 ms |
| Burst (1000 concurrent) | 200 workers | p50 submission | 3,479 ms |
| Burst (1000 concurrent) | 200 workers | Throughput | 53.4 req/s |
| Sustained (50 req/s × 60s) | 200 workers | p50 submission | 81 ms |
| Sustained (50 req/s × 60s) | 200 workers | Achieved rate | 48.1 req/s |
| Cross-region saga | 10 sequential | Mean e2e | 401 ms |
| Cross-region saga | 10 sequential | p95 e2e | 468 ms |
| Capacity enforcement | 50 concurrent, cap=1 | Correctness | 1/50 approved — **PASS** |
| Mega burst | 2000 requests | Throughput | 86.5 req/s |

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
| `tertiary` | 50 | Andorra mountain roads |
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

# 5. Run the full high-scale load test
pip install httpx
python scripts/load_test.py --token $TOKEN --scale high
```
