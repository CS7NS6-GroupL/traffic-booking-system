# Data Gateway Service

Cross-regional data router. Abstracts regional boundaries so any cluster can access any data, with automatic fallback to MongoDB Atlas on regional failure.

**Owner:** Stevin Joseph Sebastian (25377614)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe |
| GET | `/data/{region}/{collection}/{doc_id}` | Read a document from any region |
| POST | `/data/{region}/{collection}` | Write a document to a target region |
| GET | `/data/regions/status` | List known regions and current region |

## Routing Logic
```
Request for region X
  ├── X == local region → query local MongoDB
  └── X != local region → HTTP forward to data-gateway in region X
        └── on failure (timeout / 5xx) → fallback to MongoDB Atlas global store
```

## Key Technologies
- **FastAPI** — async REST API
- **httpx** — async HTTP client for cross-region forwarding
- **MongoDB Atlas** (`pymongo`) — global fallback when a regional cluster is unreachable
- **Environment vars** — `EU_DATA_URL`, `US_DATA_URL`, `ASIA_DATA_URL` configure peer gateways

## Failure Handling
When the target regional cluster is unreachable (network partition, pod crash), the gateway automatically reads from the globally-replicated MongoDB Atlas instance, ensuring continued availability at the cost of slight consistency lag.
