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

## Key Technologies
- **Apache Kafka** (`kafka-python`) — two consumer threads (booking-requests, capacity-releases)
- **Redis** (`redis-py`) — distributed locking + occupancy counters
- **MongoDB** (`pymongo`) — duplicate detection, single-region booking persistence
