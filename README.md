# CS7NS6 — Globally-Accessible Distributed Traffic Booking Service

A globally-distributed microservices system allowing drivers to pre-book road journeys before travelling, ensuring no driver starts a journey without a validated booking confirmation.

---

## Group L — Members

| Name | Student Number | Primary Service(s) |
|---|---|---|
| Dylan Murray | 20331999 | booking-service, NGINX config |
| Raghav Gupta | 25360079 | validation-service |
| Kartik Singhal | 25369980 | route-service, journey-management |
| Dylan Thompson | 20314016 | authority-service |
| Niket Ghai | 25361669 | notification-service, user-registry |
| Stevin Joseph Sebastian | 25377614 | data-gateway, all infrastructure |

---

## Architecture Overview

```
                        NGINX (port 80)
                               │
          ┌────────────────────┼────────────────────┐
          ▼                    ▼                     ▼
   Laos Cluster          Cambodia Cluster      Andorra Cluster
   ──────────────────    ─────────────────     ─────────────────
   validation-svc-1/2   validation-svc-1/2    validation-svc-1/2
   route-service        route-service          route-service
   data-service-1/2     data-service-1/2       data-service-1/2
   authority-service    authority-service      authority-service
   redis-laos           redis-cambodia         redis-andorra
   mongo-laos           mongo-cambodia         mongo-andorra
          │                    │                     │
          └────────────────────┼─────────────────────┘
                               ▼
              Global shared infrastructure (Docker Compose demo):
              booking-service-1/2    (behind nginx upstream)
              journey-management-1/2 (leader election — one runs saga coordinator)
              notification-service-1/2 (Redis pub/sub fanout for WebSocket)
              user-registry
              data-service (global — sagas, audit, flags)
              Apache Kafka (shared)
              mongo (global)
```

**3 regional clusters** (Laos, Cambodia, Andorra), each with its own Redis and MongoDB. **A single nginx** load-balances across multi-instance services. **Redis leader election** ensures only one journey-management instance runs the saga coordinator consumer. **Redis pub/sub** fans out WebSocket notifications to all notification-service instances.

### Booking Flow
```
Driver → NGINX → booking-service (JWT check) → journey-management /plan
  → single-region: Kafka booking-requests
    → validation-service (Redis lock + capacity check + MongoDB dedup)
    → booking-outcomes Kafka topic
    → notification-service → Redis pub/sub → WebSocket push to driver

  → cross-region: journey-management /sagas (saga coordinator)
    → sub-bookings per region → validation-services
    → saga coordinator collects outcomes → commit to ALL involved regions
    → booking-outcomes → notification-service → WebSocket push
```

---

## Getting Started

### Prerequisites
- Docker + Docker Compose v2
- Python 3.10+ (for load test script)

### Run locally with Docker Compose

```bash
git clone <repo-url>
cd traffic-booking-system

docker compose up --build -d
```

Services run as **2 instances** each behind nginx upstreams. All instances share Kafka and their regional Redis/MongoDB.

### Import custom road graph (required for routing)

```bash
# Import the custom 3-region test graph (Laos, Cambodia, Andorra)
python services/route-service/import_custom_graph.py

# Restart route and validation services to load the new graph
docker restart route-service-laos route-service-cambodia route-service-andorra
docker restart validation-service-laos validation-service-cambodia validation-service-andorra
```

### Health Check URLs

Services expose health via nginx. Multi-instance services have per-instance paths.

| Service | URL |
|---|---|
| booking-service | http://localhost/api/health/booking |
| booking-service (per-instance) | http://localhost/api/health/booking-1, /booking-2 |
| user-registry | http://localhost/api/health/user |
| authority-service | http://localhost/api/health/authority-laos |
| data-service (global) | http://localhost/api/health/data |
| validation-service | http://localhost/api/health/validation-laos |
| route-service | http://localhost/api/health/route-laos |
| notification-service | http://localhost/api/health/notification |
| journey-management | http://localhost/api/health/journey |
| journey-management (per-instance) | http://localhost/api/health/journey-1, /journey-2 |

Replace `-laos` with `-cambodia` or `-andorra` for the other regions.

### Quick smoke test
```bash
# Register a driver
curl -X POST http://localhost/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"password123","vehicle_id":"veh-001","role":"DRIVER"}'

# Login and get token
TOKEN=$(curl -s -X POST http://localhost/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"password123"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Submit a single-region booking (Laos)
curl -X POST http://localhost/api/bookings \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"driver_id":"alice","vehicle_id":"veh-001","origin":"laos-vientiane","destination":"laos-savannakhet","departure_time":"2026-04-10T09:00:00Z"}'

# Submit a cross-region booking (Laos → Cambodia)
curl -X POST http://localhost/api/bookings \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"driver_id":"alice","vehicle_id":"veh-001","origin":"laos-pakse","destination":"khm-phnom-penh","departure_time":"2026-04-10T09:00:00Z"}'

# Check a route
curl "http://localhost/api/routes/laos?origin=laos-vientiane&destination=laos-savannakhet"
```

---

## Kubernetes Deployment

K8s manifests are under [infra/k8s/](infra/k8s/) with subdirectories for each region.

### Minikube (local demo)
```bash
# Start minikube
minikube start --nodes=3

# Create namespaces (matching region names)
kubectl create namespace laos
kubectl create namespace cambodia
kubectl create namespace andorra

# Apply regional manifests
kubectl apply -f infra/k8s/laos/
kubectl apply -f infra/k8s/cambodia/
kubectl apply -f infra/k8s/andorra/
```

Each service runs with **2 replicas**, a liveness probe on `/health`, and environment from the regional ConfigMap.

---

## Failure Scenario Demos

Scripts are in [scripts/](scripts/). Make executable first: `chmod +x scripts/*.sh`

| Script | What it demonstrates |
|---|---|
| `scripts/demo_pod_crash.sh` | Kill booking-service-1 → nginx routes to booking-service-2, Kafka retains unacked messages; instance recovers and rejoins |
| `scripts/demo_kafka_failure.sh` | Stop kafka → booking-service outbox buffers messages, retries every 5s; broker recovers, buffered messages drain |
| `scripts/demo_region_partition.sh` | Stop validation-service-laos → cross-region sagas involving laos time out; single-region laos bookings fail; cambodia/andorra unaffected |

---

## Infrastructure Components

| Component | Purpose | Port |
|---|---|---|
| Apache Kafka | Async message queue (booking-requests, booking-outcomes, capacity-releases) | 9092 |
| Redis (per-region) | Capacity counters + distributed locking + leader election + pub/sub | 6379 |
| MongoDB (per-region) | Persistent booking + OSM road graph storage | 27017–27022 |
| NGINX | Load balancer / API gateway — upstreams for multi-instance services | 80 |

---

## Cross-Region Journey Consistency

This is the central distributed systems challenge: a journey from Pakse (Laos) to Phnom Penh (Cambodia) requires capacity reservations on road segments owned by two independent regional clusters. We address this with a **two-layer routing architecture** plus the **Saga pattern**.

### Layer 1 — Region overlay graph (in journey-management)

Each regional `route-service` is autonomous and only knows its own roads. `journey-management` holds a small **region-level overlay graph**:

```
Overlay nodes:   laos,  cambodia,  andorra
Land corridors:  laos ──[Voen Kham/Don Kralor border]── cambodia

andorra: isolated (no land connection to laos or cambodia)
```

### Layer 2 — Regional road graphs (in route-service)

Each cluster holds only its own roads, including the shared gateway node at its border:

```
Laos:     vientiane ─ pakse ─── GATEWAY_LAOS_EXIT
                                       │
Cambodia:              GATEWAY_CAMBODIA_ENTRY ─── phnom-penh ─ siem-reap

Andorra:  andorra-la-vella ─ la-massana ─ arinsal   (isolated)
```

### How a cross-region route is planned (POST /plan)

```
booking-service  →  POST journey-management/plan { origin: "laos-pakse", destination: "khm-phnom-penh" }
                                  │
          1. NODE_ID_TO_REGION["laos-pakse"] = "laos"
          2. NODE_ID_TO_REGION["khm-phnom-penh"] = "cambodia"
          3. Dijkstra on overlay  →  region sequence ["laos", "cambodia"]
          4. Call laos route-service: GET /routes?origin=laos-pakse&destination=GATEWAY_LAOS_EXIT
          5. Call cambodia route-service: GET /routes?origin=GATEWAY_CAMBODIA_ENTRY&destination=khm-phnom-penh
          6. Return combined segments_by_region + is_cross_region=true
```

### Saga coordinator (journey-management)

```
booking-service  →  POST /sagas  →  journey-management (leader instance)
                                         │
                        publishes sub-booking to each region's
                        Kafka `booking-requests` (tagged target_region)
                                         │
                   each validation-service processes its own segments
                   and publishes outcome to `booking-outcomes` with saga_id
                                         │
                   journey-management consumes outcomes, advances state machine:
                                         │
                    ┌────────────────────┴────────────────────┐
               all APPROVED                             any REJECTED
                    │                                        │
          write booking to ALL                  publish `capacity-releases`
          involved regions' MongoDB             to each already-APPROVED region
          (laos + cambodia)                     (compensating transactions)
                    │                                        │
             publish APPROVED                        publish REJECTED
             to booking-outcomes                     to booking-outcomes
                    │                                        │
                    └────────────┬────────────────────────────┘
                                 │
                      notification-service (any instance)
                      receives via Redis pub/sub → WebSocket push
```

### Consistency model

- **Within a region**: Redis distributed locks guarantee at-most-one reservation per segment (strong consistency).
- **Across regions**: The saga provides **atomicity** at the application level — either all regions commit or all roll back. The consistency window is bounded by Kafka consumer lag, typically < 1 second.
- **On regional failure during saga**: If a region's validation-service is unavailable, that sub-booking times out as REJECTED, triggering compensations in already-approved regions.
- **Multi-instance leader election**: Only one journey-management instance runs the saga coordinator Kafka consumer at a time (Redis `SET NX` with 30s TTL + renewal). The other instance runs as a hot standby and takes over within 30s on leader failure.

### Traffic Authority Interface

The `authority-service` (per region) provides a read-only API for traffic enforcement. It accepts **only `AUTHORITY`-role JWTs**.

| Endpoint | Purpose |
|---|---|
| `GET /authority/bookings/{vehicle_id}` | Confirm a vehicle has a valid booking |
| `GET /authority/audit` | Full audit log (most recent first) |
| `POST /authority/flag/{booking_id}` | Flag a booking for review |

For **cross-region bookings**, the final booking record is written to **all involved regions'** MongoDB instances, so the authority in each region can independently verify and flag the booking.

## Key Design Decisions

- **Async validation via Kafka** — booking-service returns `202 Accepted` immediately; validation is decoupled and fault-tolerant. An in-memory outbox retries Kafka failures every 5s.
- **Kafka singleton producer** — one persistent connection shared across all threads (vs. per-call producer creation). Eliminates ~24s queueing latency under 1000-way concurrency. See `docs/performance.md`.
- **Redis distributed locks** — prevent overbooking when concurrent requests target the same road segment. Safe Lua DECR ensures counters never go below zero on capacity release.
- **Leader election** — journey-management uses Redis `SET NX` so only one instance runs the saga coordinator Kafka consumer. Hot standby takes over within 30s on failure.
- **Redis pub/sub for notifications** — notification-service instances share a pub/sub channel; any instance can push to any driver regardless of which instance holds the WebSocket connection.
- **Multi-instance deployment** — booking-service, journey-management, notification-service, and all data-services run as `-1`/`-2` pairs behind nginx upstreams with `proxy_next_upstream` failover.
- **Saga pattern** — cross-region journeys use a saga coordinator rather than 2PC, trading isolation for availability.
- **Cross-region booking storage** — at saga commit, the booking record is written to **every** involved region's MongoDB, so each regional authority has full visibility.
- **Idempotency** — booking-service caches responses keyed on `X-Idempotency-Key` header (300s TTL); validation-service deduplicates by checking booking status before processing.
- **AUTHORITY vs DRIVER tokens** — authority-service rejects DRIVER tokens at the JWT role level.

---

## Load Test Results

See `docs/performance.md` for full results. Summary:

| Test | Metric | Value |
|---|---|---|
| 1000 concurrent burst | p50 submission latency | 3,479 ms |
| 1000 concurrent burst | Throughput | **53.4 req/s** |
| Sustained 50 req/s × 60s | p50 submission | **81 ms** |
| Cross-region saga e2e | Mean latency | **401 ms** |
| 2000 req mega burst | Throughput | **86.5 req/s** |
| Capacity enforcement | 50 concurrent, cap=1 | 1/50 approved — **PASS** |

---

## Assignment

CS7NS6 Distributed Systems, Trinity College Dublin, 2025–2026.
