# Validation Service

Consumes booking requests from Kafka, validates them against road capacity (Redis), and publishes outcomes. Handles both single-region bookings and cross-region saga sub-bookings.

Runs as **2 instances per region** (`validation-service-laos-1/2`, etc.) in each regional cluster.

**Owner:** Raghav Gupta (25360079)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe — reports Redis ping, data-service reachability, and consumer thread status |
| GET | `/validation/status` | Consumer group info |

## Kafka Topics

| Direction | Topic | Consumer Group |
|-----------|-------|----------------|
| Consumes | `booking-requests` | `validation-group-{region}` |
| Produces | `booking-outcomes` | — |
| Consumes | `capacity-releases` | `capacity-release-group-{region}` |

## Validation Logic

```
For each message on booking-requests:

  0. At-least-once dedup
     └── check ds.get_booking_by_id(booking_id)
     └── if status in {approved, rejected, pending} → skip (already processed)

  1. target_region check
     └── skip if target_region != REGION  (cross-region filter)

  2. Redis distributed lock  SET lock:segment:A:D NX EX 10
     └── contention → REJECTED (retry)

  3. Capacity check  GET current:segment:A:D < GET capacity:segment:A:D
     └── full → REJECTED

  4. MongoDB duplicate check  bookings where vehicle_id=X AND status in [approved, pending]
     └── duplicate → REJECTED

  5. On success:
     └── INCR current:segment:A:D
     └── DEL lock
     └── INSERT booking to MongoDB (single-region only; sagas committed by journey-management)
         on MongoDB write failure → publish REJECTED outcome (no partial state)
     └── publish APPROVED to booking-outcomes (with saga_id if present)

  6. On failure:
     └── DEL lock
     └── publish REJECTED to booking-outcomes (with saga_id if present)
```

Both consumer loops run inside `while True` with exception catch + 5s sleep restart — a consumer crash is isolated and self-healing.

## Cross-Region Sub-Booking Handling

Messages carrying a `saga_id` are sub-bookings from the saga coordinator:
- This service validates its own regional segments only.
- It **does not** write the booking to MongoDB (the saga coordinator does that after collecting all regional approvals, writing to **all** involved regions).
- It includes `saga_id` in the outcome so journey-management can advance the saga state machine.

## Compensating Transactions (capacity-releases)

When a booking is cancelled or a saga is aborted, journey-management publishes to `capacity-releases`. This service:
- Filters by `target_region == REGION`
- Decrements `current:segment:<origin>:<destination>` in Redis using a **Lua safe DECR** script that prevents the counter from going below zero

## Concurrency

Redis `SET NX EX 10` ensures only one request holds the lock per segment at a time, preventing overbooking under concurrent load. In the capacity enforcement test: 50 simultaneous requests for a cap-1 segment → exactly 1 approved, 49 rejected.

## Redis Capacity Seeding and Recovery

### On startup — `_seed_redis_capacity()`

Seeds Redis from MongoDB `osm_edges` using `SET NX` (does not overwrite live counters):

```
capacity:segment:{edge.from}:{edge.to}  →  capacity based on road_type
current:segment:{edge.from}:{edge.to}   →  0  (SET NX — preserve live counter on restart)
```

### On Redis wipe — `_redis_watchdog()` → `rebuild_redis.main()`

The watchdog polls every 30s for a probe key (`watchdog:probe:{region}`). If Redis was wiped (probe key missing), it calls `rebuild_redis.main()` which performs a **full rebuild**:

1. **Hard-overwrite** all `capacity:segment:*` keys from `osm_edges` (not NX — Redis is known empty)
2. **Reset** all `current:segment:*` keys to 0
3. **Reconstruct occupancy** by scanning all `approved`/`pending` bookings in MongoDB and incrementing each segment's counter

This ensures current occupancy counters are accurate after a Redis crash, not just zeroed. The rebuild completes in ~30 seconds (depending on graph size).

To trigger manually:
```bash
docker exec -e REGION=laos -e MONGO_URI=mongodb://mongo-laos:27017 -e REDIS_URL=redis://redis-laos:6379 \
  validation-service-laos-1 python rebuild_redis.py
```

### Capacity values by road type

| road_type | capacity (vehicles) |
|---|---|
| `motorway` | 200 |
| `trunk` | 150 |
| `primary` | 100 |
| `secondary` | 75 |
| `tertiary` | 50 |
| `tertiary_link` | 25 |
| `track` | **1** (used for Andorra La Massana → Arinsal demo) |

A missing Redis key defaults to 0 — a fail-safe that blocks all bookings for an unseeded segment until the watchdog triggers a rebuild.

## Key Technologies
- **Apache Kafka** (`kafka-python`) — two consumer threads (booking-requests, capacity-releases), each in a `while True` restart loop
- **Redis** (`redis-py`) — distributed locking + occupancy counters + Lua safe DECR
- **MongoDB** (`pymongo`) — duplicate detection, single-region booking persistence, OSM edge data for capacity seeding
