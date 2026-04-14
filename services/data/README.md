# Data Service

Thin FastAPI HTTP wrapper around `data_service.py`. Provides a single REST interface for all MongoDB access — bookings, sagas, flags, and audit logs. Deployed as 2 replicas per region (and 2 global replicas for saga state), all behind nginx.

**Owner:** Kartik Singhal (25369980)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe — reports per-shard MongoDB status |
| GET | `/bookings/{booking_id}` | Get a specific booking |
| POST | `/bookings` | Insert a booking record |
| GET | `/bookings/driver/{driver_id}` | List bookings for a driver (fan-out across shards) |
| GET | `/bookings/vehicle/{vehicle_id}` | Get active booking for a vehicle (fan-out) |
| GET | `/bookings/vehicle/{vehicle_id}/all` | All bookings for a vehicle |
| GET | `/bookings` | List recent bookings (fan-out, sorted by created_at) |
| PATCH | `/bookings/{booking_id}/cancel` | Mark booking cancelled |
| PATCH | `/bookings/{booking_id}/flag` | Mark booking flagged |
| POST | `/sagas` | Create a saga document |
| GET | `/sagas/{saga_id}` | Get saga state |
| GET | `/sagas?status=PENDING,ABORTING` | List sagas by status |
| PATCH | `/sagas/{saga_id}/outcome` | Record a regional outcome |
| PATCH | `/sagas/{saga_id}/status` | Update saga status |
| POST | `/flags` | Insert a flag record |
| POST | `/audit` | Insert an audit log event |

---

## Booking Sharding

Booking documents are hash-sharded across two MongoDB instances per region:

```
shard 0  →  mongo-{region}     (MONGO_URI)         — existing regional MongoDB
shard 1  →  mongo-{region}-s1  (MONGO_SHARD_1_URI) — second MongoDB, same region
```

**Shard key:** `booking_id`  
**Routing:** `MD5(booking_id) % 2`

MD5 is used instead of Python's `hash()` because `hash()` is randomised per-process (PYTHONHASHSEED) and would route the same booking to different shards across the two data-service replicas.

### Point operations (O(1))

| Operation | Strategy |
|---|---|
| `insert_booking` | Hash `booking_id` → write to one shard |
| `get_booking_by_id` | Hash `booking_id` → read from one shard |
| `cancel_booking` | Hash `booking_id` → update one shard |
| `flag_booking` | Hash `booking_id` → update one shard |

### Fan-out operations (O(shards))

| Operation | Strategy |
|---|---|
| `get_bookings_by_driver` | Query both shards, merge |
| `get_bookings_by_vehicle` | Query both shards, merge |
| `get_vehicle_booking` (active check) | Query both shards, return first match |
| `get_all_bookings` | Query both shards, merge + sort by `created_at` |

### Non-sharded collections

Sagas, flags, audit logs, osm_edges, and osm_nodes remain on **shard 0 only**. Sharding is applied exclusively to the `bookings` collection.

### Single-shard fallback

If `MONGO_SHARD_1_URI` is not set, the service runs in single-shard mode — all operations go to `MONGO_URI`. No code change needed; the shard list degrades to `[db]`.

---

## Environment Variables

| Variable | Purpose |
|---|---|
| `REGION` | Region name (`laos`, `cambodia`, `andorra`, or unset for global) |
| `SERVICE_NAME` | Reported in health response |
| `MONGO_URI` | MongoDB connection string for shard 0 (primary) |
| `MONGO_SHARD_1_URI` | MongoDB connection string for shard 1. Leave blank to disable sharding. |

## Key Technologies
- **FastAPI** — async REST API
- **pymongo** — MongoDB driver with connection pooling (50 max connections per shard)
- **tenacity** — retry on transient MongoDB connection errors (3 attempts, exponential backoff)
- **hashlib.md5** — deterministic shard routing
