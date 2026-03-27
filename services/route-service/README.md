# Route Service

Region-partitioned road network service. This service is **deliberately autonomous** — it only ever knows about its own region's roads. Cross-region route planning is handled by the Journey Orchestrator (journey-management), not here.

**Owner:** Kartik Singhal (25369980)

## Design principle

Each cluster runs its own route-service with its own regional road graph. No service holds a global road map. The Journey Orchestrator holds a small region-level overlay graph (3 nodes: EU, US, Asia) and delegates individual legs to the appropriate regional route-service. This keeps each region autonomous and the system scalable.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe |
| GET | `/routes?origin=A&destination=EU_US_GW` | Shortest path within this region only |
| GET | `/routes/segments` | All segments in this region (read by the orchestrator) |
| GET | `/routes/nodes` | All nodes in this region |

## Regional Graph Structure

Each cluster holds only its own road network. Adjacent regions share one **gateway node** that acts as the cross-region entry/exit point:

```
EU:  A ─ B ─ C ─ D ─── EU_US_GW
                              │
US:               EU_US_GW ─ X ─ Y ─ Z ─── US_ASIA_GW
                                                  │
Asia:                         US_ASIA_GW ─ P ─ Q ─ R
```

When the orchestrator needs the EU leg of a Dublin→New York journey it calls:
```
GET /routes?origin=A&destination=EU_US_GW
```
It never asks this service about US roads.

## Example Response

```json
{
  "origin": "A",
  "destination": "EU_US_GW",
  "path": ["A", "B", "D", "EU_US_GW"],
  "segments": [
    {"from": "A", "to": "B", "segment_id": "eu-AB", "capacity": 100, "distance_km": 12, "region": "eu"},
    ...
  ],
  "region": "eu"
}
```

## Key Technologies
- **NetworkX** — directed weighted graph, Dijkstra shortest path
- **FastAPI** — REST API, no async needed (pure in-memory graph, no external calls)
