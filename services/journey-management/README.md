# Journey Management Service  (Journey Orchestrator)

Two responsibilities:
1. **Route planning** — holds the region overlay graph; plans multi-leg cross-region journeys.
2. **Booking lifecycle + cross-region saga coordinator** — retrieve/cancel bookings, orchestrate distributed capacity reservations with compensating transactions.

**Owner:** Dylan Thompson (20314016)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe |
| POST | `/plan` | Resolve a global route via the overlay graph |
| GET | `/journeys` | List driver's bookings |
| GET | `/journeys/{booking_id}` | Get a specific booking |
| DELETE | `/journeys/{booking_id}` | Cancel (compensating transactions) |
| POST | `/sagas` | Start a cross-region saga (called by booking-service) |
| GET | `/sagas/{saga_id}` | Get saga state |

---

## Route Planning — Overlay Graph

This service holds a **coarse region-level overlay graph** (not the road-level graphs, which are owned by the regional route-services).

```
Overlay nodes: eu, us, asia
Overlay edges (border corridors):
  eu  ──EU_US_GW──  us  ──US_ASIA_GW──  asia
```

### POST /plan

```json
{ "origin": "A", "destination": "Z" }
```

1. Look up `origin_region = NODE_REGION["A"]` → `"eu"`
2. Look up `dest_region = NODE_REGION["Z"]` → `"us"`
3. Dijkstra on overlay → region sequence `["eu", "us"]`
4. Leg 1: call EU route-service `GET /routes?origin=A&destination=EU_US_GW`
5. Leg 2: call US route-service `GET /routes?origin=EU_US_GW&destination=Z`
6. Combine → return `segments_by_region`, `is_cross_region`, `legs`

Each regional route-service is only ever asked about its own territory.

### Example response
```json
{
  "origin": "A", "destination": "Z",
  "region_sequence": ["eu", "us"],
  "is_cross_region": true,
  "segments_by_region": {
    "eu": [{"from": "A", "to": "B", ...}, ...],
    "us": [{"from": "EU_US_GW", "to": "X", ...}, ...]
  },
  "legs": [
    {"leg_index": 0, "region": "eu", "origin": "A", "destination": "EU_US_GW", ...},
    {"leg_index": 1, "region": "us", "origin": "EU_US_GW", "destination": "Z", ...}
  ]
}
```

---

## Cross-Region Saga Coordinator

A **saga** is a sequence of local transactions, one per regional cluster, with compensating transactions for rollback.

```
Saga state machine:

  PENDING ─── all regions APPROVED ───→ COMMITTED
     │
     └── any region REJECTED ──→ ABORTING
                                     │
                             publish capacity-releases
                             to already-APPROVED regions
                                     │
                                  ABORTED
```

### Flow
1. `booking-service` calls `POST /sagas` with `segments_by_region` from the plan.
2. This service stores the saga in MongoDB and publishes one sub-booking per region to `booking-requests` (tagged `saga_id` + `target_region`).
3. Each regional `validation-service` filters by `target_region`, validates its segments, publishes outcome to `booking-outcomes` with `saga_id`.
4. This service's Kafka consumer (`saga-coordinator-group`) collects outcomes:
   - **All APPROVED** → write final booking to MongoDB, publish APPROVED to `booking-outcomes`.
   - **Any REJECTED** → publish `capacity-releases` to each already-APPROVED region, publish REJECTED.

### Consistency model

Within a region: Redis distributed locks provide strong consistency (no overbooking).
Across regions: The saga provides atomicity at the application level. The consistency window is bounded by Kafka consumer lag.

---

## Cancellation

For cross-region bookings: publishes one `capacity-releases` message per involved region, each tagged with `target_region`, so each regional validation-service decrements its own Redis counter independently.

---

## Environment Variables
| Variable | Purpose |
|---|---|
| `EU_ROUTE_URL` | URL of EU cluster route-service |
| `US_ROUTE_URL` | URL of US cluster route-service |
| `ASIA_ROUTE_URL` | URL of Asia cluster route-service |
| `KAFKA_BOOTSTRAP_SERVERS` | Kafka broker(s) |
| `MONGO_URI` | MongoDB connection string |
| `JWT_SECRET` | Token signing key |
