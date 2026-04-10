# Route Service

Region-partitioned road network service. Loads its region's real driving road graph from MongoDB at startup and serves A* shortest-path queries within that region. This service is **deliberately autonomous** — it only ever knows about its own region's roads. Cross-region planning is handled by journey-management.

One instance per region: `route-service-laos`, `route-service-cambodia`, `route-service-andorra`.

**Owner:** Kartik Singhal (25369980)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe — also reports live node/edge counts |
| GET | `/routes?origin=X&destination=Y` | A* shortest path within this region |
| GET | `/geocode?place=X` | Resolve a place name to the nearest OSM node in this region |

`origin` / `destination` accept:
- **Custom node IDs** (e.g. `laos-vientiane`) — resolved directly from `node_coords`, no Nominatim call
- **Numeric OSM node IDs** — used by journey-management when requesting legs that end at a gateway node
- **Place names** (e.g. `"Vientiane"`) — geocoded via Nominatim, then snapped to nearest graph node

## Data Pipeline

Road data is loaded from real OpenStreetMap extracts (downloaded from [Geofabrik](https://download.geofabrik.de)) and imported into MongoDB using the bundled script.

```bash
# One-time import per region (run from services/route-service/)
pip install -r requirements_import.txt
python import_osm.py --pbf pbfs/laos-latest.osm.pbf     --region laos
python import_osm.py --pbf pbfs/cambodia-latest.osm.pbf --region cambodia
python import_osm.py --pbf pbfs/andorra-latest.osm.pbf  --region andorra
```

For the **custom 3-region demo graph** (no PBF needed):
```bash
python import_custom_graph.py
```

All nodes/edges are tagged with `region` and stored in the regional MongoDB instance (`mongo-laos`, `mongo-cambodia`, `mongo-andorra`).

## Startup

On startup the service loads its region's graph from MongoDB into memory:
- `osm_nodes` collection → node coordinates dict
- `osm_edges` collection → adjacency dict

The service will not accept requests until loading completes. For real OSM data, loading takes 1–2 minutes; `/health` reports live counts when ready. For the custom graph, loading is near-instant.

### Road type filter

Only major road types are loaded into the in-memory graph:
`motorway`, `motorway_link`, `trunk`, `trunk_link`, `primary`, `primary_link`, `secondary`, `secondary_link`, `tertiary`, `tertiary_link`

For the custom demo graph, **all road types** are loaded (including `track`) so that the Andorra capacity demo works correctly.

## Routing

Uses **A\*** with a haversine distance heuristic (straight-line distance to goal). The heuristic is admissible — roads are never shorter than a straight line — so A* is guaranteed to find the shortest path.

## Geocoding

Place names are resolved via **Nominatim** (free OSM-based geocoder) to coordinates, then snapped to the nearest graph node using MongoDB's `2dsphere` index (`$near` query). Custom node IDs and OSM node IDs bypass geocoding entirely.

## Example Responses

```bash
GET /routes?origin=laos-vientiane&destination=laos-savannakhet
```
```json
{
  "origin": "laos-vientiane",
  "destination": "laos-savannakhet",
  "origin_node": "laos-vientiane",
  "destination_node": "laos-savannakhet",
  "path": ["laos-vientiane", "laos-thakhek", "laos-savannakhet"],
  "total_distance_km": 580.0,
  "segments": [
    {"from": "laos-vientiane", "to": "laos-thakhek", "distance_km": 340.0, "region": "laos"},
    {"from": "laos-thakhek", "to": "laos-savannakhet", "distance_km": 240.0, "region": "laos"}
  ],
  "region": "laos"
}
```

## MongoDB Collections

| Collection | Schema | Purpose |
|---|---|---|
| `osm_nodes` | `{_id: node_id, loc: GeoJSON Point, region: str}` | Node coordinates + 2dsphere index for nearest-node lookup |
| `osm_edges` | `{from: str, to: str, distance_km: float, road_type: str, region: str}` | Adjacency data loaded into memory at startup; also read by validation-service for capacity seeding |

## Region Coverage

| Region tag | Countries / data |
|---|---|
| `laos` | Laos (OSM) |
| `cambodia` | Cambodia (OSM) |
| `andorra` | Andorra (OSM) |

The custom demo graph (`import_custom_graph.py`) contains 19 nodes and 36 edges across all 3 regions.

## Key Technologies
- **FastAPI** — REST API, sync startup (graph load), sync routing
- **pymongo** — loads graph from MongoDB at startup
- **geopy / Nominatim** — place name → coordinates geocoding
- **A\*** — in-memory shortest path with haversine heuristic
- **MongoDB 2dsphere index** — nearest-node snapping
