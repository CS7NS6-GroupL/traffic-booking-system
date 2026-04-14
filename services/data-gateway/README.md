# Data Gateway Service

Transparent HTTP proxy to regional data-services, with automatic read fallback to MongoDB Atlas when a regional cluster is unreachable.

**Owner:** Stevin Joseph Sebastian (25377614)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe — reports each regional data-service status and whether Atlas is configured |
| ANY | `/data/{region}/{path}` | Proxy to the correct regional data-service; Atlas fallback for GET on failure |
| GET | `/data/regions/status` | List known regions and current region |

## Routing Logic

```
ANY /data/{region}/{native_path}
  │
  ├── resolve regional data-service URL for {region}
  │     laos     → http://data-service-laos:8009
  │     cambodia → http://data-service-cambodia:8009
  │     andorra  → http://data-service-andorra:8009
  │
  ├── forward request to {regional_data_service}/{native_path}
  │
  └── on ConnectError / TimeoutException:
        GET  → Atlas fallback (direct MongoDB read, if MONGO_ATLAS_URI configured)
        POST/PATCH/DELETE → 503 (writes require a live regional data-service)
```

### Atlas fallback — supported read patterns

| Path pattern | MongoDB query |
|---|---|
| `/bookings/{booking_id}` | `db.bookings.find_one({booking_id: ...})` |
| `/sagas/{saga_id}` | `db.sagas.find_one({saga_id: ...})` |
| `/bookings/driver/{driver_id}` | `db.bookings.find({driver_id: ..., region: ...})` |

Other paths return 503 with a clear message if Atlas is reached.

## Failure Handling

- **Regional data-service unreachable** (network partition, container crash):
  - GET requests → Atlas fallback (if `MONGO_ATLAS_URI` is set)
  - Write requests → 503 immediately — writes require a live data-service for consistency
- **Atlas not configured** (`MONGO_ATLAS_URI` blank): fallback returns 503 with a clear message
- **Atlas query fails**: 503 with error detail

## Key Technologies
- **FastAPI** — async REST API
- **httpx** — async HTTP client for regional data-service forwarding
- **pymongo** — direct Atlas read fallback
- **Environment vars** — `LAOS_DATA_URL`, `CAMBODIA_DATA_URL`, `ANDORRA_DATA_URL` configure regional targets

## Environment Variables

| Variable | Purpose |
|---|---|
| `REGION` | This instance's home region (`laos`, `cambodia`, or `andorra`) |
| `LAOS_DATA_URL` | URL of the Laos regional data-service (default: `http://data-service-laos:8009`) |
| `CAMBODIA_DATA_URL` | URL of the Cambodia regional data-service (default: `http://data-service-cambodia:8009`) |
| `ANDORRA_DATA_URL` | URL of the Andorra regional data-service (default: `http://data-service-andorra:8009`) |
| `MONGO_ATLAS_URI` | MongoDB Atlas connection string for read fallback. Leave blank to disable. Example: `mongodb+srv://user:pass@cluster.mongodb.net/traffic?retryWrites=true` |
