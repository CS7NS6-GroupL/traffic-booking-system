# Validation Service

Consumes booking requests from Kafka, validates them against road capacity (Redis), and publishes outcomes. Handles both single-region bookings and cross-region saga sub-bookings.

**Owner:** Raghav Gupta (25360079)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe |
| GET | `/validation/status` | Consumer group info |

## Kafka Topics

| Direction | Topic | Consumer Group |
|-----------|-------|----------------|
| Consumes | `booking-requests` | `validation-group` |
| Produces | `booking-outcomes` | — |
| Consumes | `capacity-releases` | `capacity-release-group` |

## Validation Logic

```
For each message on booking-requests:

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
     └── INSERT booking to MongoDB (single-region only; sagas committed by journey-management)
     └── DEL lock
     └── publish APPROVED to booking-outcomes (with saga_id if present)

  6. On failure:
     └── DEL lock
     └── publish REJECTED to booking-outcomes (with saga_id if present)
```

## Cross-Region Sub-Booking Handling

Messages carrying a `saga_id` are sub-bookings from the saga coordinator:
- This service validates its own regional segments only.
- It **does not** write the booking to MongoDB (the saga coordinator does that after collecting all regional approvals).
- It includes `saga_id` in the outcome so journey-management can advance the saga state machine.

## Compensating Transactions (capacity-releases)

When a booking is cancelled or a saga is aborted, journey-management publishes to `capacity-releases`. This service:
- Filters by `target_region == REGION`
- Decrements `current:segment:<origin>:<destination>` in Redis

## Concurrency

Redis `SET NX EX 10` ensures only one request holds the lock per segment at a time, preventing overbooking under concurrent load.

## Required Change: Dynamic Redis Capacity Seeding

The current `_seed_redis_capacity()` function uses a hardcoded dict of letter-based segment IDs
(`eu-AB`, `local-BC`, etc.). This must be replaced with dynamic seeding from MongoDB now that
route-service loads real OSM road data.

On startup, validation-service should query the `osm_edges` collection for its region and seed
Redis capacity keys for every edge:

```
capacity:segment:{edge.from}:{edge.to}  →  default capacity based on road_type
current:segment:{edge.from}:{edge.to}   →  0  (SET NX — preserve live counter on restart)
```

Default capacity values by road type (suggested):

| road_type | capacity (vehicles) |
|---|---|
| motorway / motorway_link | 500 |
| trunk / trunk_link | 400 |
| primary / primary_link | 300 |
| secondary / secondary_link | 200 |
| tertiary / tertiary_link | 150 |
| unclassified / residential | 100 |

The `osm_edges` collection schema (written by `scripts/import_osm.py`):
```json
{ "from": "osm_node_id_str", "to": "osm_node_id_str", "distance_km": 1.5, "road_type": "primary", "region": "europe" }
```

Query to use:
```python
edges = db.osm_edges.find({"region": REGION}, {"from": 1, "to": 1, "road_type": 1})
```

The `target_region` filter logic and Redis lock keys remain unchanged — only the seeding source changes.

## Key Technologies
- **Apache Kafka** (`kafka-python`) — two consumer threads (booking-requests, capacity-releases)
- **Redis** (`redis-py`) — distributed locking + occupancy counters
- **MongoDB** (`pymongo`) — duplicate detection, single-region booking persistence, OSM edge data for capacity seeding
