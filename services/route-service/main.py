"""
Route Service
=============
Loads this region's driving road graph from MongoDB at startup into memory,
then serves A* shortest-path queries within the region.

origin / destination accepted as:
  - OSM node ID (numeric string) — used by journey-management for cross-region legs
  - Place name (e.g. "Dublin City Centre") — geocoded via Nominatim then snapped
    to the nearest graph node using the MongoDB 2dsphere index

This service ONLY knows about its own region's roads.
Cross-region planning is journey-management's responsibility.
"""

import heapq
import math
import os

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from geopy.exc import GeocoderTimedOut
from geopy.geocoders import Nominatim
from pymongo import MongoClient

SERVICE_NAME = os.getenv("SERVICE_NAME", "route-service")
REGION      = os.getenv("REGION",       "local")
MONGO_URI   = os.getenv("MONGO_URI",    "mongodb://localhost:27017")

# ── In-memory graph (loaded from MongoDB at startup) ──────────────────────────
# adjacency[node_id] = {neighbor_id: distance_km}
adjacency:   dict[str, dict[str, float]]   = {}
# node_coords[node_id] = (lat, lng)
node_coords: dict[str, tuple[float, float]] = {}

_geocoder = Nominatim(user_agent="route-service-tcd")


# ── Database ──────────────────────────────────────────────────────────────────

_db = None

def get_db():
    global _db
    if _db is None:
        _db = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)["traffic"]
    return _db


# ── Graph loading ─────────────────────────────────────────────────────────────

def _load_graph():
    db = get_db()

    print(f"[route-service] Loading nodes for region '{REGION}'...")
    for doc in db.osm_nodes.find({"region": REGION}, {"loc": 1}):
        lng, lat = doc["loc"]["coordinates"]   # GeoJSON is [lng, lat]
        node_coords[doc["_id"]] = (lat, lng)
    print(f"[route-service] {len(node_coords):,} nodes loaded.")

    # Residential and unclassified roads are excluded — they account for ~70% of
    # edges but are not useful for inter-city routing and would exhaust container memory.
    MAJOR_ROADS = {"motorway", "motorway_link", "trunk", "trunk_link",
                   "primary", "primary_link", "secondary", "secondary_link",
                   "tertiary", "tertiary_link"}

    print(f"[route-service] Loading edges for region '{REGION}'...")
    edge_count = 0
    for doc in db.osm_edges.find(
        {"region": REGION, "road_type": {"$in": list(MAJOR_ROADS)}},
        {"from": 1, "to": 1, "distance_km": 1},
    ):
        f, t, d = doc["from"], doc["to"], doc["distance_km"]
        if f in node_coords and t in node_coords:
            adjacency.setdefault(f, {})[t] = d
            edge_count += 1

    # Prune node coords to only nodes reachable via major roads
    reachable = set(adjacency.keys())
    for neighbours in adjacency.values():
        reachable.update(neighbours.keys())
    pruned = {nid: coords for nid, coords in node_coords.items() if nid in reachable}
    node_coords.clear()
    node_coords.update(pruned)

    print(f"[route-service] {edge_count:,} edges, {len(node_coords):,} nodes loaded. Graph ready.")


# ── A* ────────────────────────────────────────────────────────────────────────

def _haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km — used as the A* admissible heuristic."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def astar(start: str, goal: str) -> tuple[list[str], float]:
    """
    A* on the in-memory adjacency graph.

    Uses haversine distance to goal as the heuristic — admissible because
    roads are never shorter than a straight line between two points.

    Returns (path, total_distance_km).
    Raises ValueError if no path exists within this region.
    """
    goal_lat, goal_lng = node_coords[goal]

    # Heap: (f_score, tie_breaker, g_score, node_id)
    # tie_breaker avoids comparing node_id strings when f/g scores are equal
    counter  = 0
    h0       = _haversine(*node_coords[start], goal_lat, goal_lng)
    heap     = [(h0, counter, 0.0, start)]
    g_score: dict[str, float]       = {start: 0.0}
    came_from: dict[str, str | None] = {start: None}

    while heap:
        _, _, g, node = heapq.heappop(heap)

        if node == goal:
            path: list[str] = []
            cur = goal
            while cur is not None:
                path.append(cur)
                cur = came_from[cur]
            return list(reversed(path)), g

        # Skip stale heap entries
        if g > g_score.get(node, math.inf):
            continue

        for neighbor, dist in adjacency.get(node, {}).items():
            new_g = g + dist
            if new_g < g_score.get(neighbor, math.inf):
                g_score[neighbor]   = new_g
                came_from[neighbor] = node
                n_lat, n_lng        = node_coords[neighbor]
                h                   = _haversine(n_lat, n_lng, goal_lat, goal_lng)
                counter            += 1
                heapq.heappush(heap, (new_g + h, counter, new_g, neighbor))

    raise ValueError(f"No road path found from '{start}' to '{goal}' within region '{REGION}'")


# ── Geocoding helpers ─────────────────────────────────────────────────────────

def _nearest_node(lat: float, lng: float) -> str:
    """Snap coordinates to the nearest graph node using MongoDB 2dsphere index."""
    docs = get_db().osm_nodes.find(
        {
            "loc": {
                "$near": {
                    "$geometry": {"type": "Point", "coordinates": [lng, lat]},
                    "$maxDistance": 50000,   # metres
                }
            },
            "region": REGION,
        },
        limit=20,
    )
    for doc in docs:
        if doc["_id"] in node_coords:
            return doc["_id"]
    raise HTTPException(
        status_code=404,
        detail=f"No road node within 50 km of ({lat:.5f}, {lng:.5f}) in region '{REGION}'"
    )


def _resolve(place: str) -> str:
    """
    Resolve a place name or node ID to a node ID in this region's graph.

    Known node ID   → returned directly if it exists in node_coords (works for
                      both numeric OSM IDs and custom string IDs like 'border-laos-camb').
    Numeric string  → treated as OSM node ID; 404 if not in graph.
    Anything else   → geocoded with Nominatim, then snapped to nearest graph node.
    """
    # Direct match — covers custom string IDs (e.g. "border-laos-camb")
    if place in node_coords:
        return place

    if place.lstrip("-").isdigit():
        raise HTTPException(
            status_code=404,
            detail=f"Node ID '{place}' not found in region '{REGION}'"
        )

    try:
        location = _geocoder.geocode(place, timeout=10)
    except GeocoderTimedOut:
        raise HTTPException(status_code=503, detail=f"Geocoding timed out for '{place}'")

    if not location:
        raise HTTPException(status_code=404, detail=f"Could not geocode '{place}'")

    return _nearest_node(location.latitude, location.longitude)


# ── App ───────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_app):
    _load_graph()
    yield

app = FastAPI(title="Route Service", lifespan=lifespan)


@app.get("/health")
def health():
    return {
        "status":      "ok",
        "service":     SERVICE_NAME,
        "region":      REGION,
        "graph_nodes": len(node_coords),
        "graph_edges": sum(len(v) for v in adjacency.values()),
    }


@app.get("/geocode")
def geocode(place: str):
    """
    Resolve a place name to the nearest OSM node in this region's graph.
    Used by journey-management to determine which region a place belongs to
    and to obtain the node ID for cross-region leg planning.
    """
    node_id      = _resolve(place)
    lat, lng     = node_coords[node_id]
    return {"place": place, "node_id": node_id, "lat": lat, "lng": lng, "region": REGION}


@app.get("/routes")
def find_route(origin: str, destination: str):
    """
    A* shortest path between two points within THIS region's road graph.

    origin / destination: place name or OSM node ID.
    Called by journey-management for individual legs of a multi-region journey.
    """
    if not node_coords:
        raise HTTPException(status_code=503, detail="Graph not loaded yet — try again shortly")

    origin_node = _resolve(origin)
    dest_node   = _resolve(destination)

    if origin_node not in node_coords:
        raise HTTPException(status_code=404, detail=f"Origin '{origin}' snapped to a node not in the road graph")
    if dest_node not in node_coords:
        raise HTTPException(status_code=404, detail=f"Destination '{destination}' snapped to a node not in the road graph")

    if origin_node == dest_node:
        raise HTTPException(status_code=422, detail="Origin and destination resolve to the same node")

    try:
        path, total_km = astar(origin_node, dest_node)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    segments = [
        {
            "from":        path[i],
            "to":          path[i + 1],
            "distance_km": adjacency[path[i]][path[i + 1]],
            "region":      REGION,
        }
        for i in range(len(path) - 1)
    ]

    return {
        "origin":             origin,
        "destination":        destination,
        "origin_node":        origin_node,
        "destination_node":   dest_node,
        "path":               path,
        "total_distance_km":  round(total_km, 2),
        "segments":           segments,
        "region":             REGION,
    }
