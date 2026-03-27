import os
import networkx as nx
from fastapi import FastAPI, HTTPException

app = FastAPI(title="Route Service")

SERVICE_NAME = os.getenv("SERVICE_NAME", "route-service")
REGION = os.getenv("REGION", "local")

# Region-partitioned road network graph (loaded at startup).
# This service ONLY knows about its own region's roads — it never contacts
# other regions. Cross-region route planning is the Journey Orchestrator's
# responsibility (journey-management), which holds the coarse overlay graph.
GRAPH: nx.DiGraph = nx.DiGraph()

# Seed data is region-specific. Each region's graph includes one gateway node
# shared with the adjacent region so the orchestrator can request legs that
# terminate at a border crossing.
#
#  EU:  A─B─C─D──EU_US_GW           (EU_US_GW is the EU/US border node)
#  US:  EU_US_GW──X─Y─Z──US_ASIA_GW
#  Asia: US_ASIA_GW──P─Q─R

REGION_SEGMENTS = {
    "eu": [
        ("A",        "B",        {"segment_id": "eu-AB",      "capacity": 100, "distance_km": 12,  "region": "eu"}),
        ("B",        "C",        {"segment_id": "eu-BC",      "capacity": 80,  "distance_km": 8,   "region": "eu"}),
        ("A",        "C",        {"segment_id": "eu-AC",      "capacity": 60,  "distance_km": 18,  "region": "eu"}),
        ("C",        "D",        {"segment_id": "eu-CD",      "capacity": 120, "distance_km": 5,   "region": "eu"}),
        ("B",        "D",        {"segment_id": "eu-BD",      "capacity": 90,  "distance_km": 15,  "region": "eu"}),
        ("D",        "EU_US_GW", {"segment_id": "eu-gateway", "capacity": 200, "distance_km": 500, "region": "eu"}),
    ],
    "us": [
        ("EU_US_GW",   "X",          {"segment_id": "us-gwX",     "capacity": 200, "distance_km": 500,  "region": "us"}),
        ("X",          "Y",          {"segment_id": "us-XY",      "capacity": 150, "distance_km": 30,   "region": "us"}),
        ("Y",          "Z",          {"segment_id": "us-YZ",      "capacity": 130, "distance_km": 25,   "region": "us"}),
        ("X",          "Z",          {"segment_id": "us-XZ",      "capacity": 100, "distance_km": 50,   "region": "us"}),
        ("Z",          "US_ASIA_GW", {"segment_id": "us-gateway", "capacity": 200, "distance_km": 800,  "region": "us"}),
    ],
    "asia": [
        ("US_ASIA_GW", "P",          {"segment_id": "asia-gwP",   "capacity": 200, "distance_km": 800,  "region": "asia"}),
        ("P",          "Q",          {"segment_id": "asia-PQ",    "capacity": 150, "distance_km": 22,   "region": "asia"}),
        ("Q",          "R",          {"segment_id": "asia-QR",    "capacity": 120, "distance_km": 18,   "region": "asia"}),
        ("P",          "R",          {"segment_id": "asia-PR",    "capacity": 90,  "distance_km": 35,   "region": "asia"}),
    ],
    "local": [
        ("A", "B", {"segment_id": "local-AB", "capacity": 100, "distance_km": 12, "region": "local"}),
        ("B", "C", {"segment_id": "local-BC", "capacity": 80,  "distance_km": 8,  "region": "local"}),
        ("C", "D", {"segment_id": "local-CD", "capacity": 120, "distance_km": 5,  "region": "local"}),
    ],
}


def load_seed_graph():
    segs = REGION_SEGMENTS.get(REGION, REGION_SEGMENTS["local"])
    for u, v, data in segs:
        GRAPH.add_edge(u, v, **data)


@app.on_event("startup")
def startup():
    load_seed_graph()


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME, "region": REGION}


@app.get("/routes")
def find_route(origin: str, destination: str):
    """
    Shortest path between two nodes within THIS region's road graph.
    Called by journey-management for individual legs of a multi-region journey.
    """
    if origin not in GRAPH or destination not in GRAPH:
        raise HTTPException(
            status_code=404,
            detail=f"'{origin}' or '{destination}' not in {REGION} road network",
        )
    try:
        path = nx.shortest_path(GRAPH, source=origin, target=destination, weight="distance_km")
    except nx.NetworkXNoPath:
        raise HTTPException(status_code=404, detail="No route found within this region")

    segments = [
        {"from": path[i], "to": path[i + 1], **GRAPH[path[i]][path[i + 1]]}
        for i in range(len(path) - 1)
    ]
    return {
        "origin": origin,
        "destination": destination,
        "path": path,
        "segments": segments,
        "region": REGION,
    }


@app.get("/routes/segments")
def list_segments():
    """List all road segments in this region (used by journey-management's overlay planner)."""
    edges = [{"from": u, "to": v, **data} for u, v, data in GRAPH.edges(data=True)]
    return {"region": REGION, "segments": edges}


@app.get("/routes/nodes")
def list_nodes():
    return {"region": REGION, "nodes": list(GRAPH.nodes())}
