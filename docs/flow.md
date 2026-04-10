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
    │  1. Resolve origin region:
    │       if place in NODE_ID_TO_REGION → direct lookup, no Nominatim call
    │       else → Nominatim → country → COUNTRY_TO_REGION → "laos"
    │  2. Resolve dest region (same logic)
    │  3. Dijkstra on OVERLAY graph → region_sequence = ["laos"]
    │  4. HTTP GET → route-service-laos:8000/routes
    │         ?origin=laos-vientiane&destination=laos-savannakhet
    ▼
ROUTE-SERVICE-LAOS  /routes
    │  1. Resolve origin:
    │       if place in node_coords (custom node ID) → return directly, no Nominatim
    │       elif numeric → treat as OSM node ID
    │       else → Nominatim → (lat,lng) → mongo-laos 2dsphere nearest node
    │  2. Resolve destination (same logic)
    │  3. A* on in-memory graph (loaded from mongo-laos osm_edges at startup,
    │         ALL road types loaded — no MAJOR_ROADS filter for custom graph)
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
    │  3. For each segment: Redis GET capacity:segment:{f}:{t}
    │                          → None (unseeded) → cap=0 → REJECTED immediately
    │                        Redis GET current:segment:{f}:{t}
    │     → if cur >= cap: REJECTED "at full capacity ({cur}/{cap})"
    │  4. HTTP GET → data-service-laos:8009/bookings/vehicle/{vehicle_id}
    │     → MongoDB find_one bookings WHERE vehicle_id=X AND status IN [approved,pending]
    │     → if found: REJECTED "vehicle already has active booking"
    │  5. For each segment: Redis INCR current:segment:{f}:{t}
    │  6. Release all locks (reverse order, in finally block)
    │  7. Return {outcome: "APPROVED", reason: "..."}
    │
    │  If not a sub-booking (single-region final result):
    │    HTTP POST → data-service-laos:8009/bookings
    │      APPROVED: {booking_id, ..., status:"approved", approved_at}
    │      REJECTED: {booking_id, ..., status:"rejected", rejected_at, reject_reason}
    │      → writes to mongo-laos  (regional booking storage, both outcomes)
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
             3. Publishes to Redis "notifications" channel (_pub_redis)
             4. _redis_subscriber_loop on each instance dispatches to local WebSocket
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
    │  1. "laos-pakse"    → NODE_ID_TO_REGION → "laos"    (no Nominatim)
    │  2. "khm-phnom-penh" → NODE_ID_TO_REGION → "cambodia" (no Nominatim)
    │  3. Dijkstra on OVERLAY:
    │       laos → cambodia (cost 600km, via gateway nodes)
    │       region_sequence = ["laos", "cambodia"]
    │  4. Build legs:
    │     Leg 1: HTTP GET → route-service-laos
    │               origin="laos-pakse", dest=GATEWAY_LAOS_EXIT ("border-laos-camb")
    │     Leg 2: HTTP GET → route-service-cambodia
    │               origin=GATEWAY_CAMBODIA_ENTRY ("border-camb-laos"), dest="khm-phnom-penh"
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
                  a. For EACH region in regions_involved ["laos","cambodia"]:
                       HTTP POST → data-service-{region}:8009/bookings
                         {booking_id, saga_id, status:"approved",
                          region: "{region}", segments: [...region-specific segs...],
                          regions_involved:["laos","cambodia"], ...}
                         → written to mongo-laos  (laos record)
                         → written to mongo-cambodia  (cambodia record)
                  b. HTTP PATCH → data-service:8009/sagas/{id}/status
                       {status:"COMMITTED"}  → updated in global mongo
                  c. Produce to "booking-outcomes" (FINAL, no is_sub_booking):
                       {booking_id, saga_id, outcome:"APPROVED",
                        regional_outcomes:{laos:{...}, cambodia:{...}}}
    ▼
KAFKA  topic: booking-outcomes  (1 final message)
    ▼
NOTIFICATION-SERVICE  (any instance — Kafka consumer)
    │  is_sub_booking not set → this is final
    │  → publish to Redis channel "notifications" (_pub_redis)
    ▼
REDIS pub/sub channel "notifications"
    │
    └──▶ ALL notification-service instances (_redis_subscriber_loop)
             each instance checks connections dict for driver_id
             → ws.send_json to local WebSocket if connected
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
    │  Vehicle lookup: GET /authority/bookings/{vehicle_id}
    │    → data-service /bookings/vehicle/{id}/all  (all statuses incl. flagged/rejected)
    │
    │  Flag booking: POST /authority/flag/{booking_id}  {reason}
    │    → data-service PATCH /bookings/{id}/flag?reason=...
    │         sets status="flagged", flagged_reason, flagged_at on the booking doc
    │    → data-service POST /flags  (separate flags collection entry)
    │    → data-service POST /audit  (audit_logs collection entry)
    │
    │  Each authority can ONLY see bookings stored in its own region.
    │  Regional isolation is enforced by the data-service URL — there is no
    │  global booking index accessible to any authority instance.
```

---

## Booking Storage Summary

| Booking type | Status saved | Written by | Written to | MongoDB instance |
|---|---|---|---|---|
| Single-region Laos | approved **and** rejected | validation-service-laos | data-service-laos `/bookings` | mongo-laos |
| Single-region Cambodia | approved **and** rejected | validation-service-cambodia | data-service-cambodia `/bookings` | mongo-cambodia |
| Single-region Andorra | approved **and** rejected | validation-service-andorra | data-service-andorra `/bookings` | mongo-andorra |
| Cross-region saga (commit) | approved | journey-management `_advance_saga` | data-service-{**each involved region**} `/bookings` | mongo-laos **and** mongo-cambodia |
| Saga metadata | PENDING/COMMITTED/ABORTED | journey-management `/sagas` | data-service (global) `/sagas` | mongo (global) |

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
| `capacity:segment:{from}:{to}` | String | Max vehicles on segment (seeded from osm_edges road_type). Missing key → treated as 0 (blocks all bookings until seeded) |
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
| **journey-management** | **2 instances, leader election** | Redis `SET NX` (30s TTL + renewal) — only the leader runs the saga coordinator consumer. Standby takes over within 30s on leader failure. |
| **route-service** | One per region, loads full OSM graph in RAM | Persistent graph store (e.g. Neo4j) per region so replicas share graph without each loading it on startup. |
| **Kafka** | Single broker, shared | 3-broker cluster per region. Cross-region topics use Kafka MirrorMaker. |
| **Redis** | One instance per region | Redis Cluster (sharded) or Sentinel (HA) per region. |
| **MongoDB (regional)** | One instance per region | 3-node replica set per region. |
| **MongoDB (global)** | Single instance | 3-node replica set, globally distributed with read replicas per region. |
| **data-service** | One global + one per region | Multiple replicas behind nginx per region. Stateless. |
| **authority-service** | One per region | Multiple replicas per region. Role enforcement is stateless. |
| **Saga timeout** | Not implemented | Background watchdog scans `status:PENDING` sagas older than N seconds and triggers compensating rollback. |
| **Notification WebSocket** | **Redis pub/sub (implemented)** | Kafka consumer publishes to Redis channel; each instance's subscriber delivers to its own local connections. nginx `ip_hash` for WebSocket stickiness. |
| **Cross-region network** | All on one Docker bridge | Each region is a separate K8s cluster/VPC. Cross-region traffic over private WAN or VPN. |
