# CS7NS6 — Globally-Accessible Distributed Traffic Booking Service

A globally-distributed microservices system allowing drivers to pre-book road journeys before travelling, ensuring no driver starts a journey without a validated booking confirmation.

---

## Group L — Members

| Name | Student Number | Primary Service(s) |
|---|---|---|
| Dylan Murray | 20331999 | booking-service, NGINX config |
| Raghav Gupta | 25360079 | validation-service |
| Kartik Singhal | 25369980 | route-service |
| Dylan Thompson | 20314016 | journey-management, authority-service |
| Niket Ghai | 25361669 | notification-service, user-registry |
| Stevin Joseph Sebastian | 25377614 | data-gateway, all infrastructure |

---

## Architecture Overview

```
                        AWS Route 53 (Geo-routing DNS)
                               │
          ┌────────────────────┼────────────────────┐
          ▼                    ▼                     ▼
   EU Cluster (K8s)     US Cluster (K8s)      Asia Cluster (K8s)
   ─────────────────    ─────────────────     ─────────────────
   NGINX Gateway        NGINX Gateway         NGINX Gateway
   booking-service      booking-service       booking-service
   validation-service   validation-service    validation-service
   route-service        route-service         route-service
   notification-svc     notification-svc      notification-svc
   Redis 7              Redis 7               Redis 7
   Apache Kafka         Apache Kafka          Apache Kafka
          │                    │                     │
          └────────────────────┼─────────────────────┘
                               ▼
                    MongoDB Atlas (Global)
                    user-registry (Global)
                    data-gateway (Cross-region router)
                    journey-management
                    authority-service
```

**3 regional Kubernetes clusters** (EU, US, Asia), each self-contained with its own Kafka broker and Redis cache. **MongoDB Atlas** provides globally-replicated persistent storage. **AWS Route 53** geo-routes drivers to the nearest cluster. **Data Gateway** routes cross-region data requests with automatic Atlas fallback on regional failure.

### Booking Flow
```
Driver → Route 53 → NGINX → booking-service (JWT check) → Kafka
  → validation-service (Redis lock + capacity check + MongoDB dedup)
  → booking-outcomes Kafka topic
  → notification-service → WebSocket push to driver
```

---

## Getting Started

### Prerequisites
- Docker + Docker Compose v2
- (Optional) `kubectl` + Minikube for K8s demo

### Run locally with Docker Compose

```bash
git clone <repo-url>
cd traffic-booking-system

cp .env.example .env
# Edit .env — at minimum set JWT_SECRET to a long random string

docker-compose up --build
```

### Health Check URLs

| Service | URL |
|---|---|
| booking-service | http://localhost:8001/health |
| user-registry | http://localhost:8002/health |
| authority-service | http://localhost:8003/health |
| data-gateway | http://localhost:8004/health |
| validation-service | http://localhost:8005/health |
| route-service | http://localhost:8006/health |
| notification-service | http://localhost:8007/health |
| journey-management | http://localhost:8008/health |

All return: `{"status": "ok", "service": "<name>", "region": "local"}`

### Quick smoke test
```bash
# Register a driver
curl -X POST http://localhost:8002/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"secret","vehicle_id":"veh-001","role":"DRIVER"}'

# Login and get token
TOKEN=$(curl -s -X POST http://localhost:8002/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"secret"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Submit a booking
curl -X POST http://localhost:8001/bookings \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"driver_id":"alice","vehicle_id":"veh-001","origin":"A","destination":"D","departure_time":"2026-04-01T09:00:00Z"}'

# Check a route
curl "http://localhost:8006/routes?origin=A&destination=D"
```

---

## Kubernetes Deployment

K8s manifests are under [infra/k8s/](infra/k8s/) with subdirectories for each region.

### Minikube (local demo)
```bash
# Start minikube
minikube start --nodes=3

# Create namespaces
kubectl create namespace eu
kubectl create namespace us
kubectl create namespace asia

# Apply EU cluster manifests
kubectl apply -f infra/k8s/eu/

# Apply US and Asia
kubectl apply -f infra/k8s/us/
kubectl apply -f infra/k8s/asia/
```

Each service runs with **2 replicas**, a liveness probe on `/health`, and environment from the regional ConfigMap.

---

## Failure Scenario Demos

Scripts are in [scripts/](scripts/). Make executable first: `chmod +x scripts/*.sh`

| Script | What it demonstrates |
|---|---|
| `scripts/demo_pod_crash.sh` | Delete booking-service pod → K8s restarts it, Kafka retains unacked message |
| `scripts/demo_kafka_failure.sh` | Delete kafka-0 → consumers reconnect after broker recovery |
| `scripts/demo_region_partition.sh` | Cordon EU nodes → Route 53 stops routing to EU, Data Gateway falls back to Atlas |

---

## Infrastructure Components

| Component | Purpose | Port |
|---|---|---|
| Apache Kafka | Async message queue (booking-requests, booking-outcomes, capacity-releases, notifications) | 9092 |
| Redis 7 | Per-cluster capacity cache + distributed locking | 6379 |
| MongoDB | Persistent storage for bookings, users, road network | 27017 |
| NGINX | Regional load balancer / API gateway | 80/443 |

---

## Cross-Region Journey Consistency

This is the central distributed systems challenge: a journey from Dublin (EU) to New York (US) requires capacity reservations on road segments owned by two independent regional clusters. We address this with a **two-layer routing architecture** plus the **Saga pattern**.

### Layer 1 — Region overlay graph (in journey-management)

Each regional `route-service` is autonomous and only knows its own roads. No service assembles a global road graph. Instead, `journey-management` (the Journey Orchestrator) holds a small **region-level overlay graph**:

```
Overlay nodes:  eu,  us,  asia
Overlay edges:  eu ──EU_US_GW── us ──US_ASIA_GW── asia
```

This mirrors the hierarchical overlay routing technique — coarse at the region level, detailed within each region.

### Layer 2 — Regional road graphs (in route-service)

Each cluster holds only its own roads, including the shared gateway node at its border:

```
EU:  A ─ B ─ C ─ D ─── EU_US_GW
                              │
US:               EU_US_GW ─ X ─ Y ─ Z ─── US_ASIA_GW
                                                  │
Asia:                         US_ASIA_GW ─ P ─ Q ─ R
```

### How a cross-region route is planned (POST /plan)

```
booking-service  →  POST journey-management/plan { origin: "A", destination: "Z" }
                                  │
          1. NODE_REGION["A"] = "eu", NODE_REGION["Z"] = "us"
          2. Dijkstra on overlay  →  region sequence ["eu", "us"]
          3. Call EU route-service: GET /routes?origin=A&destination=EU_US_GW
          4. Call US route-service: GET /routes?origin=EU_US_GW&destination=Z
          5. Return combined segments_by_region + is_cross_region=true
```

Each regional route-service is only ever asked about its own territory — consistent with the design principle that no service holds a monolithic global road graph.

### Saga coordinator (journey-management)

```
booking-service  →  POST /sagas  →  journey-management
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
             write booking                    publish `capacity-releases`
             to MongoDB                       to each already-APPROVED region
                    │                         (compensating transactions)
             publish APPROVED                        │
             to booking-outcomes              publish REJECTED
                    │                         to booking-outcomes
                    └────────────┬────────────┘
                                 │
                      notification-service → WebSocket push to driver
```

### Consistency model

- **Within a region**: Redis distributed locks guarantee at-most-one reservation per segment (strong consistency).
- **Across regions**: The saga provides **atomicity** at the application level — either all regions commit or all roll back. The consistency window (between sub-booking approval and saga commit) is bounded by Kafka consumer lag, typically < 1 second.
- **On regional failure during saga**: If a region's validation-service is unavailable, that sub-booking times out as REJECTED, triggering compensations in the regions that had already approved. The driver is notified that the booking failed.

### Traffic Authority Interface

The `authority-service` (port 8003) provides a read-only API for traffic enforcement. It accepts **only `AUTHORITY`-role JWTs** — driver tokens are rejected at the role-check level.

| Endpoint | Purpose |
|---|---|
| `GET /authority/bookings/{vehicle_id}` | Confirm a vehicle has a valid booking for enforcement |
| `GET /authority/audit` | Full audit log (most recent first) |
| `POST /authority/flag/{booking_id}` | Flag a booking for review (written to separate `flags` collection) |

## Key Design Decisions

- **Async validation via Kafka** — booking-service returns `202 Accepted` immediately; validation is decoupled and fault-tolerant.
- **Redis distributed locks** — prevent overbooking when concurrent requests target the same road segment.
- **Data partitioning** — road network data is stored on the cluster closest to where those roads are (EU roads on EU cluster, connected via gateway nodes).
- **Saga pattern** — cross-region journeys use a saga coordinator in journey-management rather than a 2PC distributed transaction, trading isolation for availability.
- **Compensating transactions** — both saga abort and journey cancellation publish `capacity-releases` events to decrement Redis counters in each involved region.
- **AUTHORITY vs DRIVER tokens** — authority-service rejects DRIVER tokens at the JWT role level; no route can accidentally expose enforcement data to drivers.

---

## Assignment

CS7NS6 Distributed Systems, Trinity College Dublin, 2025–2026.
