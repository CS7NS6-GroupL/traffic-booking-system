# Journey Management Service (Journey Orchestrator)

Two responsibilities:
1. **Route planning** — holds a region-level overlay graph; geocodes origin/destination to their regions; plans multi-leg cross-region journeys by delegating each leg to the appropriate regional route-service.
2. **Booking lifecycle + cross-region saga coordinator** — retrieve/cancel bookings, orchestrate distributed capacity reservations with compensating transactions.

Runs as **2 instances** (`journey-management-1`, `journey-management-2`) behind nginx. **Only one instance is the leader** (Redis `SET NX`, 30s TTL + renewal loop). The leader runs the saga coordinator Kafka consumer; the standby waits and takes over within 30s if the leader stops renewing.

**Owner:** Kartik Singhal (25369980)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe — reports Kafka, data-service reachability, and `leader` status |
| POST | `/plan` | Resolve a global route via the overlay graph |
| GET | `/journeys` | List driver's bookings (JWT required) |
| GET | `/journeys/{booking_id}` | Get a specific booking (JWT required) |
| DELETE | `/journeys/{booking_id}` | Cancel booking with compensating transactions (JWT required) |
| POST | `/sagas` | Start a cross-region saga (called by booking-service) |
| GET | `/sagas/{saga_id}` | Get saga state (JWT required) |

---

## Route Planning — Overlay Graph

This service holds a **coarse region-level overlay graph** (not the road-level graphs, which are owned by the regional route-services).

```
Overlay nodes:   laos,  cambodia,  andorra

Land corridors:
  laos ──[Voen Kham / Don Kralor border crossing]── cambodia

  andorra: isolated (no land connection to laos or cambodia)
```

### POST /plan

```json
{ "origin": "laos-pakse", "destination": "khm-phnom-penh" }
```

1. Resolve `origin` region: `NODE_ID_TO_REGION["laos-pakse"]` = `"laos"` (no Nominatim call for custom node IDs)
2. Resolve `destination` region: `NODE_ID_TO_REGION["khm-phnom-penh"]` = `"cambodia"`
3. Dijkstra on overlay → region sequence `["laos", "cambodia"]`
4. Leg 1: call laos route-service `GET /routes?origin=laos-pakse&destination=<GATEWAY_LAOS_EXIT>`
5. Leg 2: call cambodia route-service `GET /routes?origin=<GATEWAY_CAMBODIA_ENTRY>&destination=khm-phnom-penh`
6. Combine → return `segments_by_region`, `is_cross_region`, `legs`

**Isolated region → 404:**
```json
{ "origin": "laos-vientiane", "destination": "andorra-la-vella" }
→ 404 "No land corridor exists between 'laos' and 'andorra' — regions are separated"
```

### Example response
```json
{
  "origin": "laos-pakse",
  "destination": "khm-phnom-penh",
  "region_sequence": ["laos", "cambodia"],
  "is_cross_region": true,
  "segments_by_region": {
    "laos":    [{"from": "laos-pakse", "to": "border-laos-camb", "distance_km": 40.0, "region": "laos"}, ...],
    "cambodia":[{"from": "border-camb-laos", "to": "khm-phnom-penh", "distance_km": 470.0, "region": "cambodia"}, ...]
  },
  "legs": [
    {"leg_index": 0, "region": "laos",    "origin": "laos-pakse",     "destination": "border-laos-camb", ...},
    {"leg_index": 1, "region": "cambodia","origin": "border-camb-laos","destination": "khm-phnom-penh", ...}
  ]
}
```

### Gateway node IDs

Gateway nodes are the physical border crossing nodes shared between regions.

Set in `services/journey-management/.env`:
```
GATEWAY_LAOS_EXIT=<OSM node ID or custom ID on Laos side>
GATEWAY_CAMBODIA_ENTRY=<OSM node ID or custom ID on Cambodia side>
```

To find them after data import, run in mongosh:
```js
// Laos side (Voen Kham, ~14.17°N 105.77°E)
db.osm_nodes.findOne({loc:{$near:{$geometry:{type:"Point",coordinates:[105.77,14.17]},$maxDistance:5000}},region:"laos"})

// Cambodia side (Don Kralor, ~13.92°N 105.79°E)
db.osm_nodes.findOne({loc:{$near:{$geometry:{type:"Point",coordinates:[105.79,13.92]},$maxDistance:5000}},region:"cambodia"})
```

---

## Cross-Region Saga Coordinator

A **saga** is a sequence of local transactions, one per regional cluster, with compensating transactions for rollback. Only the **leader instance** runs the saga coordinator Kafka consumer.

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
4. The leader instance's Kafka consumer (`saga-coordinator-group`) collects outcomes:
   - **All APPROVED** → write final booking to MongoDB in **every involved region** (laos AND cambodia each get a record with their own segments), publish APPROVED to `booking-outcomes`.
   - **Any REJECTED** → publish `capacity-releases` to each already-APPROVED region, publish REJECTED.

### Consistency model

Within a region: Redis distributed locks provide strong consistency (no overbooking).
Across regions: The saga provides atomicity at the application level. The consistency window is bounded by Kafka consumer lag (typically < 1s).

---

## Cancellation

For cross-region bookings: publishes one `capacity-releases` message per involved region, each tagged with `target_region`, so each regional validation-service decrements its own Redis counter independently. The booking is then marked `cancelled` in all involved regions' data-services.

---

## Region Coverage

| Region | Countries / custom graph | Route-service env var |
|---|---|---|
| `laos` | Laos | `LAOS_ROUTE_URL` |
| `cambodia` | Cambodia (connected to laos via Voen Kham border) | `CAMBODIA_ROUTE_URL` |
| `andorra` | Andorra (isolated — no land corridor) | `ANDORRA_ROUTE_URL` |

To add a new country: import its OSM data with the correct `--region` tag and add the country to `COUNTRY_TO_REGION` in `main.py`.

---

## Environment Variables

| Variable | Purpose |
|---|---|
| `LAOS_ROUTE_URL` | URL of laos cluster route-service |
| `CAMBODIA_ROUTE_URL` | URL of cambodia cluster route-service |
| `ANDORRA_ROUTE_URL` | URL of andorra cluster route-service |
| `GATEWAY_LAOS_EXIT` | Node ID on Laos side of Voen Kham border |
| `GATEWAY_CAMBODIA_ENTRY` | Node ID on Cambodia side of Voen Kham border |
| `REDIS_LEADER_URL` | Redis URL for leader election (e.g. `redis://redis-laos:6379`) |
| `KAFKA_BOOTSTRAP_SERVERS` | Kafka broker(s) |
| `MONGO_URI` | MongoDB connection string |
| `JWT_SECRET` | Token signing key |

## Key Technologies
- **FastAPI** — async REST API
- **geopy / Nominatim** — geocodes place names to determine region (bypassed for custom node IDs)
- **httpx** — sync HTTP client called via `asyncio.to_thread` for regional route-service calls
- **kafka-python** — saga outcome consumer + capacity-release producer (singleton with `threading.Lock`)
- **redis-py** — leader election (`SET NX` with 30s TTL + renewal loop)
- **pymongo** — saga/booking persistence (atomic `find_one_and_update` for saga state transitions)
- **tenacity** — retry on route-service HTTP calls
