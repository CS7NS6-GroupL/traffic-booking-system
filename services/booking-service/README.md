# Booking Service

Entry point for all journey booking requests. Validates the driver JWT, delegates route planning to the Journey Orchestrator, and routes to either the fast single-region path or the cross-region saga coordinator.

**Owner:** Dylan Murray (20331999)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe |
| POST | `/bookings` | Submit a booking (DRIVER JWT required) |
| GET | `/bookings/{booking_id}` | Proxy to journey-management for status |

### POST /bookings — Request Body
```json
{
  "driver_id": "drv-001",
  "vehicle_id": "veh-abc",
  "origin": "A",
  "destination": "Z",
  "departure_time": "2026-04-01T09:00:00Z"
}
```

### Response — single-region (202)
```json
{ "status": "accepted", "booking_id": "bk-a1b2c3d4", "flow": "single-region", "region": "eu" }
```

### Response — cross-region (202)
```json
{
  "status": "accepted",
  "booking_id": "bk-a1b2c3d4",
  "saga_id": "saga-e5f6g7h8",
  "flow": "cross-region-saga",
  "regions_involved": ["eu", "us"]
}
```

## Booking Flow

```
POST /bookings
  │
  ├─ 1. Validate JWT (role=DRIVER)
  ├─ 2. Generate booking_id (UUID)
  ├─ 3. POST journey-management/plan   ← Journey Orchestrator resolves the route
  │       │  using region overlay graph; delegates legs to regional route-services
  │       └─ returns { segments_by_region, is_cross_region, regions_involved }
  │
  ├─ Single-region ──→ publish to Kafka booking-requests (target_region=local)
  │                        │
  │                        └─→ validation-service → booking-outcomes → notification-service
  │
  └─ Cross-region ───→ POST journey-management/sagas
                           │
                           └─→ saga coordinator fans out sub-bookings per region
                                   ↓ all APPROVED → COMMITTED → notification
                                   ↓ any REJECTED → compensate approved regions → ABORTED → notification
```

## Key Technologies
- **FastAPI** (async) — awaits journey-management /plan and /sagas calls
- **httpx** — async HTTP client
- **kafka-python** — Kafka producer for single-region path
- **JWT** — DRIVER role enforcement via shared/auth.py

## Environment Variables
| Variable | Purpose |
|---|---|
| `JOURNEY_MANAGEMENT_URL` | URL of journey-management (route planning + saga) |
| `KAFKA_BOOTSTRAP_SERVERS` | Kafka broker(s) |
| `JWT_SECRET` | Token signing key |
