# Route Service

Region-partitioned road network service. Loads its region's real driving road graph from MongoDB at startup and serves A* shortest-path queries within that region. This service is **deliberately autonomous** — it only ever knows about its own region's roads. Cross-region planning is handled by journey-management.

**Owner:** Kartik Singhal (25369980)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe — also reports live node/edge counts |
| GET | `/routes?origin=X&destination=Y` | A* shortest path within this region |
| GET | `/geocode?place=X` | Resolve a place name to the nearest OSM node in this region |

`origin` / `destination` accept either a **place name** (e.g. `"Dublin City Centre"`) or a numeric **OSM node ID** (used by journey-management when requesting legs that end at a gateway node).

## Data Pipeline

Road data is loaded from real OpenStreetMap extracts (downloaded from [Geofabrik](https://download.geofabrik.de)) and imported into MongoDB using the bundled script.

```bash
# One-time import per region (run from services/route-service/)
pip install -r requirements_import.txt
python import_osm.py --pbf pbfs/ireland-and-northern-ireland.osm.pbf --region europe
python import_osm.py --pbf pbfs/great-britain.osm.pbf --region europe
# ... repeat for all countries in a region (see import_all.bat)
```

All countries in a region are imported with the same `--region` tag and merged into a single in-memory graph at startup.

## Startup

On startup the service loads its region's graph from MongoDB into memory:
- `osm_nodes` collection → node coordinates dict
- `osm_edges` collection → adjacency dict (major roads only — see below)

The service will not accept requests until loading completes. Loading takes 1–2 minutes; `/health` reports live counts when ready.

### Road type filter

Only major road types are loaded into the in-memory graph:
`motorway`, `motorway_link`, `trunk`, `trunk_link`, `primary`, `primary_link`, `secondary`, `secondary_link`, `tertiary`, `tertiary_link`

Residential and unclassified streets account for ~70% of OSM edges but add no value for inter-city routing. Excluding them reduces memory from ~10 GB to ~2–3 GB for Ireland + Bulgaria combined, leaving headroom for A* search and other containers.

## Routing

Uses **A\*** with a haversine distance heuristic (straight-line distance to goal). The heuristic is admissible — roads are never shorter than a straight line — so A* is guaranteed to find the shortest path. Significantly faster than Dijkstra on geographically large graphs.

## Geocoding

Place names are resolved via **Nominatim** (free OSM-based geocoder) to coordinates, then snapped to the nearest graph node using MongoDB's `2dsphere` index (`$near` query). OSM node IDs passed directly bypass geocoding entirely.

## Example Responses

```bash
GET /routes?origin=Dublin&destination=Cork
```
```json
{
  "origin": "Dublin",
  "destination": "Cork",
  "origin_node": "12020904933",
  "destination_node": "8062498128",
  "path": ["12020904933", "...", "8062498128"],
  "total_distance_km": 245.18,
  "segments": [
    {"from": "12020904933", "to": "5300250107", "distance_km": 0.011, "region": "europe"},
    ...
  ],
  "region": "europe"
}
```

```bash
GET /geocode?place=Dublin Airport
```
```json
{"place": "Dublin Airport", "node_id": "289762316", "lat": 53.4283, "lng": -6.2447, "region": "europe"}
```

## MongoDB Collections

| Collection | Schema | Purpose |
|---|---|---|
| `osm_nodes` | `{_id: osm_id, loc: GeoJSON Point, region: str}` | Node coordinates + 2dsphere index for nearest-node lookup |
| `osm_edges` | `{from: str, to: str, distance_km: float, road_type: str, region: str}` | Adjacency data loaded into memory at startup |

## Region Coverage

| Region tag | Countries imported |
|---|---|
| `europe` | Ireland, Great Britain, France, Germany, Austria, Hungary, Romania, Bulgaria |
| `middle-east` | Turkey, Iran, Pakistan |
| `south-asia` | India |
| `north-america` | Texas (US) |

## Key Technologies
- **FastAPI** — REST API, sync startup (graph load), sync routing
- **pymongo** — loads graph from MongoDB at startup
- **geopy / Nominatim** — place name → coordinates geocoding
- **A\*** — in-memory shortest path with haversine heuristic
- **MongoDB 2dsphere index** — nearest-node snapping
