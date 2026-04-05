# Journey Management Service (Journey Orchestrator)

Two responsibilities:
1. **Route planning** — holds a region-level overlay graph; geocodes origin/destination to their regions; plans multi-leg cross-region journeys by delegating each leg to the appropriate regional route-service.
2. **Booking lifecycle + cross-region saga coordinator** — retrieve/cancel bookings, orchestrate distributed capacity reservations with compensating transactions.

**Owner:** Dylan Thompson (20314016)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe |
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
Overlay nodes:   europe,  middle-east,  south-asia,  north-america

Land corridors:
  europe ──[Bulgaria/Turkey border]── middle-east ──[Wagah crossing]── south-asia

  north-america: isolated (ocean barriers — no land connection to other regions)
```

### POST /plan

```json
{ "origin": "Dublin, Ireland", "destination": "Mumbai, India" }
```

1. Geocode `origin` via Nominatim → country → `"europe"`
2. Geocode `destination` via Nominatim → country → `"south-asia"`
3. Dijkstra on overlay → region sequence `["europe", "middle-east", "south-asia"]`
4. Leg 1: call europe route-service `GET /routes?origin=Dublin, Ireland&destination=<EU_ME_gateway_node_id>`
5. Leg 2: call middle-east route-service `GET /routes?origin=<EU_ME_gateway_node_id>&destination=<ME_SA_gateway_node_id>`
6. Leg 3: call south-asia route-service `GET /routes?origin=<ME_SA_gateway_node_id>&destination=Mumbai, India`
7. Combine → return `segments_by_region`, `is_cross_region`, `legs`

**No land corridor → 404:**
```json
{ "origin": "Austin, Texas", "destination": "Dublin, Ireland" }
→ 404 "No land corridor exists between 'north-america' and 'europe' — regions are separated by ocean"
```

### Example response
```json
{
  "origin": "Dublin, Ireland",
  "destination": "Mumbai, India",
  "region_sequence": ["europe", "middle-east", "south-asia"],
  "is_cross_region": true,
  "segments_by_region": {
    "europe":      [{"from": "...", "to": "...", "distance_km": 0.01, "region": "europe"}, ...],
    "middle-east": [...],
    "south-asia":  [...]
  },
  "legs": [
    {"leg_index": 0, "region": "europe",      "origin": "Dublin, Ireland", "destination": "<gateway_node>", ...},
    {"leg_index": 1, "region": "middle-east", "origin": "<gateway_node>",  "destination": "<gateway_node>", ...},
    {"leg_index": 2, "region": "south-asia",  "origin": "<gateway_node>",  "destination": "Mumbai, India", ...}
  ]
}
```

### Gateway node IDs

Gateway nodes are real OSM node IDs at physical border crossings. They must exist in both adjacent regions' graphs. Set via environment variables (see below). To find them after data import, run in mongosh:

```js
// Bulgaria/Turkey border (Kapitan Andreevo, ~41.7445°N 26.3570°E)
db.osm_nodes.find_one({loc:{$near:{$geometry:{type:"Point",coordinates:[26.357,41.7445]},$maxDistance:500}}})

// Pakistan/India border - Wagah crossing (~31.6040°N 74.5730°E)
db.osm_nodes.find_one({loc:{$near:{$geometry:{type:"Point",coordinates:[74.573,31.604]},$maxDistance:500}}})
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
Across regions: The saga provides atomicity at the application level. The consistency window is bounded by Kafka consumer lag (typically < 1s).

---

## Cancellation

For cross-region bookings: publishes one `capacity-releases` message per involved region, each tagged with `target_region`, so each regional validation-service decrements its own Redis counter independently.

---

## Region Coverage

| Region | Countries | Route-service env var |
|---|---|---|
| `europe` | Ireland, UK, France, Germany, Austria, Hungary, Romania, Bulgaria | `EUROPE_ROUTE_URL` |
| `middle-east` | Turkey, Iran, Pakistan | `MIDDLE_EAST_ROUTE_URL` |
| `south-asia` | India | `SOUTH_ASIA_ROUTE_URL` |
| `north-america` | United States | `NORTH_AMERICA_ROUTE_URL` |

To add a new country: import its OSM data with the correct `--region` tag (see route-service README) and add the country to `COUNTRY_TO_REGION` in `main.py`.

---

## Environment Variables

| Variable | Purpose |
|---|---|
| `EUROPE_ROUTE_URL` | URL of europe cluster route-service |
| `MIDDLE_EAST_ROUTE_URL` | URL of middle-east cluster route-service |
| `SOUTH_ASIA_ROUTE_URL` | URL of south-asia cluster route-service |
| `NORTH_AMERICA_ROUTE_URL` | URL of north-america cluster route-service |
| `GATEWAY_EU_ME` | OSM node ID at Bulgaria/Turkey border crossing |
| `GATEWAY_ME_SA` | OSM node ID at Pakistan/India (Wagah) border crossing |
| `KAFKA_BOOTSTRAP_SERVERS` | Kafka broker(s) |
| `MONGO_URI` | MongoDB connection string |
| `JWT_SECRET` | Token signing key |

## Key Technologies
- **FastAPI** — async REST API
- **geopy / Nominatim** — geocodes place names to determine region
- **httpx** — async HTTP client for calling regional route-services
- **kafka-python** — saga outcome consumer + capacity-release producer
- **pymongo** — saga/booking persistence
