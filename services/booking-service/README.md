# Booking Service

Entry point for all journey booking requests. Validates the driver JWT, delegates route planning to the Journey Orchestrator, and routes to either the fast single-region path or the cross-region saga coordinator.

Runs as **2 instances** (`booking-service-1`, `booking-service-2`) behind nginx with `least_conn` load balancing and `proxy_next_upstream` failover.

**Owner:** Dylan Murray (20331999)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe — reports Kafka, JM reachability, and outbox depth |
| POST | `/bookings` | Submit a booking (DRIVER JWT required) |
| GET | `/bookings/{booking_id}` | Proxy to journey-management for status |

### POST /bookings — Request Body
```json
{
  "driver_id": "alice",
  "vehicle_id": "veh-001",
  "origin": "laos-vientiane",
  "destination": "laos-savannakhet",
  "departure_time": "2026-04-10T09:00:00Z"
}
```

`origin` and `destination` accept custom graph node IDs (e.g. `laos-vientiane`), numeric OSM node IDs, or human-readable place names. Node ID lookup bypasses Nominatim geocoding entirely for the custom graph.

`departure_time` must be a valid ISO 8601 datetime string. Both fields are validated on receipt and rejected with `422` if empty or malformed.

### Response — single-region (202)
```json
{ "status": "accepted", "booking_id": "bk-a1b2c3d4", "flow": "single-region", "region": "laos" }
```

### Response — cross-region (202)
```json
{
  "status": "accepted",
  "booking_id": "bk-a1b2c3d4",
  "saga_id": "saga-e5f6g7h8",
  "flow": "cross-region-saga",
  "regions_involved": ["laos", "cambodia"]
}
```

## Booking Flow

```
POST /bookings
  │
  ├─ 1. Validate JWT (role=DRIVER)
  ├─ 2. Check idempotency cache (X-Idempotency-Key header, 300s TTL)
  ├─ 3. Validate input fields (@field_validator: non-empty, ISO 8601 departure_time)
  ├─ 4. Generate booking_id (UUID)
  ├─ 5. POST journey-management/plan   ← tenacity retry (3 attempts, exponential backoff)
  │       │  using region overlay graph; delegates legs to regional route-services
  │       └─ returns { segments_by_region, is_cross_region, regions_involved }
  │
  ├─ Single-region ──→ publish to Kafka booking-requests (via singleton producer)
  │                        │  on Kafka failure → enqueue to in-memory outbox
  │                        └─→ validation-service → booking-outcomes → notification-service
  │
  └─ Cross-region ───→ POST journey-management/sagas  ← tenacity retry
                           │
                           └─→ saga coordinator fans out sub-bookings per region
                                   ↓ all APPROVED → commit to ALL regions → notification
                                   ↓ any REJECTED → compensate approved regions → notification
```

## Resilience Features

- **Kafka singleton producer** — one persistent connection with `threading.Lock`. Eliminates per-call TCP overhead. See `docs/performance.md` for the 7× latency improvement this delivers.
- **In-memory outbox** — failed Kafka publishes are queued; a background thread retries every 5s until the broker recovers.
- **tenacity retry** — all HTTP calls to journey-management use 3-attempt exponential backoff (0.5s → 4s).
- **Idempotency cache (Redis-backed)** — if the client retries with the same `X-Idempotency-Key`, the cached response is returned without re-submitting the booking. Stored in `redis-laos` under `idem:{key}` with a 300s TTL so both instances share the same cache. Degrades gracefully to pass-through if Redis is unavailable.
- **2 instances** — nginx routes to whichever instance is healthy; `proxy_next_upstream` retries on 502/503.

## Key Technologies
- **FastAPI** (async) — awaits journey-management /plan and /sagas calls
- **httpx** — async HTTP client
- **kafka-python** — singleton Kafka producer for single-region path
- **tenacity** — retry logic for JM HTTP calls
- **JWT** — DRIVER role enforcement via shared/auth.py

## Environment Variables
| Variable | Purpose |
|---|---|
| `JOURNEY_MANAGEMENT_URL` | URL of journey-management (route planning + saga) |
| `KAFKA_BOOTSTRAP_SERVERS` | Kafka broker(s) |
| `REDIS_URL` | Redis URL for shared idempotency cache (e.g. `redis://redis-laos:6379`) |
| `JWT_SECRET` | Token signing key |
