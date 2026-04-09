# Service Flow — Traffic Booking System

## Single-Region Booking Flow

```
DRIVER (browser)
    │
    │  POST /api/bookings  {origin, destination, vehicle_id, departure_time}
    │  Authorization: Bearer <JWT>
    ▼
NGINX (port 80)
    │  proxy_pass → booking-service:8000/bookings
    ▼
BOOKING-SERVICE
    │  1. Verify JWT — role must be DRIVER
    │  2. Generate booking_id = "bk-{uuid[:12]}"
    │  3. HTTP POST → journey-management:8000/plan
    │         {origin, destination}
    ▼
JOURNEY-MANAGEMENT  /plan
    │  1. Geocode origin  → Nominatim → country → COUNTRY_TO_REGION → "laos"
    │  2. Geocode dest    → Nominatim → country → COUNTRY_TO_REGION → "laos"
    │  3. Dijkstra on OVERLAY graph → region_sequence = ["laos"]
    │  4. HTTP GET → route-service-laos:8000/routes
    │         ?origin=Vientiane&destination=Luang Prabang
    ▼
ROUTE-SERVICE-LAOS  /routes
    │  1. Resolve "Vientiane" → Nominatim → (lat,lng) → mongo-laos 2dsphere
    │         nearest node in osm_nodes WHERE region=laos
    │  2. Resolve "Luang Prabang" → same
    │  3. A* on in-memory graph (loaded from mongo-laos osm_edges at startup)
    │         adjacency[node][neighbor] = distance_km
    │         heuristic = haversine(node, dest)
    │  4. Return {segments: [{from, to, distance_km, region}, ...], path, total_km}
    │
    ▼  (back to journey-management)
JOURNEY-MANAGEMENT
    │  Returns plan:
    │    {segments, is_cross_region: false, regions_involved: ["laos"],
    │     segments_by_region: {"laos": [...]}}
    │
    ▼  (back to booking-service)
BOOKING-SERVICE
    │  4. Produce to Kafka topic "booking-requests":
    │     {booking_id, driver_id, vehicle_id, origin, destination,
    │      departure_time, segments, target_region: "laos"}
    │  5. Return HTTP 202 to client:
    │     {status:"accepted", booking_id, flow:"single-region"}
    ▼
KAFKA  topic: booking-requests  (partition by booking_id)
    ▼
VALIDATION-SERVICE-LAOS
    │  Consumer group: "validation-group-laos"
    │  All 3 validation-services receive the message, only laos processes it
    │  (cambodia/andorra skip — target_region != REGION)
    │
    │  validate_and_reserve(segments, vehicle_id)  — all Redis ops in-process:
    │  1. Sort segments by lock key (deadlock prevention)
    │  2. For each segment: Redis SET NX lock:segment:{f}:{t} TTL=10s  (redis-laos)
    │     → if any fail: REJECTED "lock contention"
    │  3. For each segment: Redis GET capacity:segment:{f}:{t} (default 100)
    │                        Redis GET current:segment:{f}:{t}
    │     → if cur >= cap: REJECTED "at full capacity"
    │  4. HTTP GET → data-service-laos:8009/bookings/vehicle/{vehicle_id}
    │     → MongoDB find_one bookings WHERE vehicle_id=X AND status IN [approved,pending]
    │     → if found: REJECTED "vehicle already has active booking"
    │  5. For each segment: Redis INCR current:segment:{f}:{t}
    │  6. Release all locks (reverse order, in finally block)
    │  7. Return {outcome: "APPROVED", reason: "..."}
    │
    │  If APPROVED (and not a sub-booking):
    │    HTTP POST → data-service-laos:8009/bookings
    │      {booking_id, ..., status:"approved", approved_at}
    │      → writes to mongo-laos  (regional booking storage)
    │
    │  Produce to Kafka topic "booking-outcomes":
    │    {booking_id, driver_id, outcome:"APPROVED", region:"laos"}
    │    (no is_sub_booking flag — this is a final result)
    ▼
KAFKA  topic: booking-outcomes
    │
    ├──▶ JOURNEY-MANAGEMENT (saga-coordinator-group)
    │      Sees no saga_id → ignores
    │
    └──▶ NOTIFICATION-SERVICE (notification-group)
             1. Receives message
             2. Checks is_sub_booking — not set, so this is final
             3. Looks up WebSocket connection for driver_id
             4. asyncio.run_coroutine_threadsafe → ws.send_json(payload)
             ▼
           BROWSER WebSocket
             Receives {outcome:"APPROVED", booking_id, reason, ...}
```

---

## Cross-Region Booking Flow (Laos → Cambodia)

```
DRIVER (browser)
    │
    │  POST /api/bookings  {origin:"Pakse, Laos", destination:"Siem Reap, Cambodia", ...}
    ▼
BOOKING-SERVICE
    │  1–2. Same JWT check + booking_id generation
    │  3. HTTP POST → journey-management /plan
    ▼
JOURNEY-MANAGEMENT  /plan
    │  1. Geocode "Pakse, Laos"     → "laos"
    │  2. Geocode "Siem Reap, Cam." → "cambodia"
    │  3. Dijkstra on OVERLAY:
    │       laos → cambodia (cost 600km, via gateway nodes)
    │       region_sequence = ["laos", "cambodia"]
    │  4. Build legs:
    │     Leg 1: HTTP GET → route-service-laos
    │               origin="Pakse", dest=GATEWAY_LAOS_EXIT (node 325778714)
    │     Leg 2: HTTP GET → route-service-cambodia
    │               origin=GATEWAY_CAMBODIA_ENTRY (node 5178604282), dest="Siem Reap"
    │  5. Returns plan:
    │       {is_cross_region: true,
    │        segments_by_region: {"laos":[...], "cambodia":[...]},
    │        regions_involved: ["laos","cambodia"]}
    │
    ▼  (back to booking-service)
BOOKING-SERVICE
    │  is_cross_region=true → calls /sagas instead of Kafka directly
    │  HTTP POST → journey-management:8000/sagas
    │    {booking_id, driver_id, vehicle_id, segments_by_region, regions_involved, ...}
    ▼
JOURNEY-MANAGEMENT  /sagas
    │  1. Generate saga_id = "saga-{uuid[:12]}"
    │  2. HTTP POST → data-service:8009/sagas  (GLOBAL — saga metadata lives here)
    │       {saga_id, booking_id, status:"PENDING", regional_outcomes:{},
    │        regions_involved:["laos","cambodia"], segments_by_region:{...}}
    │  3. Fan-out: for EACH region → produce to Kafka "booking-requests":
    │     Sub-booking for laos:
    │       {booking_id, saga_id, segments:[...laos only...],
    │        target_region:"laos", is_sub_booking:true}
    │     Sub-booking for cambodia:
    │       {booking_id, saga_id, segments:[...cambodia only...],
    │        target_region:"cambodia", is_sub_booking:true}
    │  4. Return {saga_id, status:"PENDING"}
    │
    ▼  (booking-service returns 202 to client immediately)
    │  {status:"accepted", booking_id, saga_id, flow:"cross-region-saga"}

KAFKA  topic: booking-requests  (2 messages, independently consumed)
    │
    ├──▶ VALIDATION-SERVICE-LAOS  (target_region=="laos" → process)
    │        → validate_and_reserve(laos_segments, vehicle_id)
    │             Redis ops on redis-laos, vehicle check via data-service-laos
    │        → Produce to "booking-outcomes":
    │            {saga_id, region:"laos", outcome:"APPROVED", is_sub_booking:true}
    │
    └──▶ VALIDATION-SERVICE-CAMBODIA  (target_region=="cambodia" → process)
             → validate_and_reserve(cambodia_segments, vehicle_id)
                  Redis ops on redis-cambodia, vehicle check via data-service-cambodia
             → Produce to "booking-outcomes":
                 {saga_id, region:"cambodia", outcome:"APPROVED", is_sub_booking:true}

KAFKA  topic: booking-outcomes  (2 messages, order not guaranteed)
    │
    ├──▶ NOTIFICATION-SERVICE
    │      sees is_sub_booking:true on both → skips both (waits for final outcome)
    │
    └──▶ JOURNEY-MANAGEMENT  (saga-coordinator-group)
             Receives message 1 (e.g. laos APPROVED):
               _advance_saga(saga_id, "laos", "APPROVED", ...)
               1. Get saga from global data-service (mongo)
               2. PATCH /sagas/{id}/outcome → regional_outcomes["laos"] = APPROVED
               3. All regions reported? {"laos"} vs {"laos","cambodia"} → NO → wait

             Receives message 2 (cambodia APPROVED):
               _advance_saga(saga_id, "cambodia", "APPROVED", ...)
               1. Get saga; update outcome for cambodia
               2. All regions reported? YES. Any rejected? NO
               3. COMMIT PATH:
                  a. HTTP POST → data-service-laos:8009/bookings  (origin region)
                       {booking_id, saga_id, status:"approved",
                        regions_involved:["laos","cambodia"], ...}
                       → written to mongo-laos
                  b. HTTP PATCH → data-service:8009/sagas/{id}/status
                       {status:"COMMITTED"}  → updated in global mongo
                  c. Produce to "booking-outcomes" (FINAL, no is_sub_booking):
                       {booking_id, saga_id, outcome:"APPROVED",
                        regional_outcomes:{laos:{...}, cambodia:{...}}}
    ▼
KAFKA  topic: booking-outcomes  (1 final message)
    ▼
NOTIFICATION-SERVICE
    │  is_sub_booking not set → this is final
    │  → ws.send_json to driver WebSocket
    ▼
BROWSER  ← "Your booking is APPROVED"
```

### Rollback Path — if a Region Rejects

```
JOURNEY-MANAGEMENT  _advance_saga():
    1. All regions reported, any rejected? YES
    2. PATCH → data-service /sagas/{id}/status → "ABORTING"
    3. For each APPROVED region → produce to "capacity-releases":
         {target_region:"laos", booking_id, saga_id, segments:[...laos segs...]}
    4. PATCH → data-service /sagas/{id}/status → "ABORTED"
    5. Produce to "booking-outcomes" (FINAL):
         {outcome:"REJECTED", reason:"One or more regions rejected..."}
    ▼
VALIDATION-SERVICE-LAOS  (capacity-releases consumer)
    consumer group: "capacity-release-group-laos"
    → release_segments(laos_segments)
    → Redis DECR current:segment:{f}:{t}  for each laos segment (redis-laos)
    (laos capacity restored — compensating transaction; no HTTP round-trip)
    ▼
NOTIFICATION-SERVICE → driver: "REJECTED"
```

---

## Authority Service Flow

```
AUTHORITY USER
    │
    │  GET /api/authority/{region}/bookings/{vehicle_id}
    │  GET /api/authority/{region}/audit
    │  POST /api/authority/{region}/flag/{booking_id}
    │  where {region} = laos | cambodia | andorra
    ▼
NGINX
    │  /api/authority/laos/... → authority-service-laos:8000/authority/...
    │  /api/authority/cambodia/... → authority-service-cambodia:8000/authority/...
    │  /api/authority/andorra/... → authority-service-andorra:8000/authority/...
    ▼
AUTHORITY-SERVICE-{REGION}
    │  1. Verify JWT — role must be AUTHORITY
    │  2. Query only its own regional data-service:
    │     authority-service-laos → data-service-laos → mongo-laos
    │     authority-service-cambodia → data-service-cambodia → mongo-cambodia
    │     authority-service-andorra → data-service-andorra → mongo-andorra
    │
    │  Each authority can ONLY see bookings stored in its own region.
    │  Regional isolation is enforced by the data-service URL — there is no
    │  global booking index accessible to any authority instance.
```

---

## Booking Storage Summary

| Booking type | Written by | Written to | MongoDB instance |
|---|---|---|---|
| Single-region Laos | validation-service-laos | data-service-laos `/bookings` | mongo-laos |
| Single-region Cambodia | validation-service-cambodia | data-service-cambodia `/bookings` | mongo-cambodia |
| Single-region Andorra | validation-service-andorra | data-service-andorra `/bookings` | mongo-andorra |
| Cross-region saga (commit) | journey-management `_advance_saga` | data-service-{**origin region**} `/bookings` | mongo-{origin} |
| Saga metadata | journey-management `/sagas` | data-service (global) `/sagas` | mongo (global) |

**Known limitation:** single-region validation checks for duplicate vehicles only in the booking's own regional MongoDB. A vehicle booked in Laos would not be seen by Cambodia's validation-service for a separate single-region Cambodia booking. Cross-region sagas handle this correctly — each region independently rejects if the vehicle has an active booking in its own DB, and any single rejection aborts the saga.

---

## Kafka Topics

| Topic | Producer | Consumer | Key message fields |
|---|---|---|---|
| `booking-requests` | booking-service, journey-management | validation-service-* | `booking_id, saga_id?, segments[], target_region, is_sub_booking?` |
| `booking-outcomes` | validation-service-*, journey-management | notification-service, journey-management | `booking_id, saga_id?, region, outcome, reason, is_sub_booking?` |
| `capacity-releases` | journey-management | validation-service-* | `booking_id, saga_id?, target_region, segments[]` |

## Redis Keys (per-region, e.g. redis-laos)

| Key | Type | Purpose |
|---|---|---|
| `lock:segment:{from}:{to}` | String | Distributed mutex (SET NX, TTL 10s) |
| `capacity:segment:{from}:{to}` | String | Max vehicles on segment (seeded from osm_edges) |
| `current:segment:{from}:{to}` | String | Current vehicle count |
| `watchdog:probe:{region}` | String | Auto-reseed trigger key |

## MongoDB Instances

| Instance | Container | Port | Collections |
|---|---|---|---|
| Global | mongo | 27017 | `sagas`, `flags`, `audit_logs` |
| Laos | mongo-laos | 27020 | `bookings`, `osm_nodes`, `osm_edges` |
| Cambodia | mongo-cambodia | 27021 | `bookings`, `osm_nodes`, `osm_edges` |
| Andorra | mongo-andorra | 27022 | `bookings`, `osm_nodes`, `osm_edges` |

---

## Production Distribution Notes

| Component | Current (demo) | Production |
|---|---|---|
| **NGINX** | Single container | Multiple replicas behind a cloud LB. Terminates TLS. |
| **booking-service** | Single container | 3+ replicas, stateless — nginx round-robins. |
| **journey-management** | **Singleton — SPOF** | Needs leader election (Redis `SET NX`) so only one instance runs the saga coordinator consumer. |
| **route-service** | One per region, loads full OSM graph in RAM | Persistent graph store (e.g. Neo4j) per region so replicas share graph without each loading it on startup. |
| **Kafka** | Single broker, shared | 3-broker cluster per region. Cross-region topics use Kafka MirrorMaker. |
| **Redis** | One instance per region | Redis Cluster (sharded) or Sentinel (HA) per region. |
| **MongoDB (regional)** | One instance per region | 3-node replica set per region. |
| **MongoDB (global)** | Single instance | 3-node replica set, globally distributed with read replicas per region. |
| **data-service** | One global + one per region | Multiple replicas behind nginx per region. Stateless. |
| **authority-service** | One per region | Multiple replicas per region. Role enforcement is stateless. |
| **Saga timeout** | Not implemented | Background watchdog scans `status:PENDING` sagas older than N seconds and triggers compensating rollback. |
| **Notification WebSocket** | Connections held in RAM | Sticky sessions (nginx `ip_hash`) or Redis pub/sub so any replica can push to any driver. |
| **Cross-region network** | All on one Docker bridge | Each region is a separate K8s cluster/VPC. Cross-region traffic over private WAN or VPN. |
