"""
Custom Lightweight Road Graph Import
=====================================
Replaces heavy OSM data with a small hand-crafted graph for demo purposes.

Regions covered:
  - andorra   (6 nodes, isolated — no land border to other regions)
  - laos      (6 nodes, connected to cambodia via Voen Kham border)
  - cambodia  (7 nodes, connected to laos via Don Kralor border)

The Laos/Cambodia border crossing is represented by two separate nodes,
one in each region's graph:
  - "border-laos-camb"  (region=laos,     in mongo-laos)    ← GATEWAY_LAOS_EXIT
  - "border-camb-laos"  (region=cambodia, in mongo-cambodia) ← GATEWAY_CAMBODIA_ENTRY

These IDs match the GATEWAY_LAOS_EXIT / GATEWAY_CAMBODIA_ENTRY env vars set in
docker-compose for journey-management.

Each region's data is written to its OWN MongoDB instance:
  - andorra → mongo-andorra  (localhost:27022)
  - laos    → mongo-laos     (localhost:27020)
  - cambodia→ mongo-cambodia (localhost:27021)

Run this script ONCE on any machine that has Docker running:
    python import_custom_graph.py

It will:
  1. Clear existing osm_nodes and osm_edges in each regional MongoDB.
  2. Insert the custom nodes and edges.
  3. Create all indexes needed by route-service and validation-service.

Teammate instructions:
  - Make sure Docker is running and containers are up (docker-compose up -d)
  - Install pymongo if needed:  pip install pymongo
  - Run from the project root:  python services/route-service/import_custom_graph.py
  - Then restart the route and validation services:
      docker restart route-service-andorra route-service-laos route-service-cambodia
      docker restart validation-service-andorra validation-service-laos validation-service-cambodia
"""

from pymongo import MongoClient, GEOSPHERE, ASCENDING

# ── Regional MongoDB connections ──────────────────────────────────────────────
# Each region has its own MongoDB instance (separate containers in docker-compose).
# Ports match the host port mappings in docker-compose.yml.

MONGO_LAOS     = "mongodb://localhost:27020"
MONGO_CAMBODIA = "mongodb://localhost:27021"
MONGO_ANDORRA  = "mongodb://localhost:27022"

db_laos     = MongoClient(MONGO_LAOS,     serverSelectionTimeoutMS=5000)["traffic"]
db_cambodia = MongoClient(MONGO_CAMBODIA, serverSelectionTimeoutMS=5000)["traffic"]
db_andorra  = MongoClient(MONGO_ANDORRA,  serverSelectionTimeoutMS=5000)["traffic"]

REGION_DBS = {
    "laos":     db_laos,
    "cambodia": db_cambodia,
    "andorra":  db_andorra,
}

# ── 1. Clear existing data in each regional MongoDB ───────────────────────────

for region, db in REGION_DBS.items():
    print(f"Clearing existing nodes and edges in {region} MongoDB ...")
    db.osm_nodes.delete_many({"region": region})
    db.osm_edges.delete_many({"region": region})
print("  Done.")


# ── 2. Define nodes ───────────────────────────────────────────────────────────
# Format: { _id, region, loc: {type:"Point", coordinates:[lng, lat]} }
# NOTE: GeoJSON uses [longitude, latitude] order.

NODES = [

    # ── Andorra ────────────────────────────────────────────────────────────────
    {"_id": "and-andorra-la-vella", "region": "andorra",
     "loc": {"type": "Point", "coordinates": [1.5218, 42.5063]}},   # capital

    {"_id": "and-escaldes",         "region": "andorra",
     "loc": {"type": "Point", "coordinates": [1.5341, 42.5072]}},

    {"_id": "and-encamp",           "region": "andorra",
     "loc": {"type": "Point", "coordinates": [1.5800, 42.5350]}},

    {"_id": "and-canillo",          "region": "andorra",
     "loc": {"type": "Point", "coordinates": [1.5973, 42.5667]}},

    {"_id": "and-ordino",           "region": "andorra",
     "loc": {"type": "Point", "coordinates": [1.5333, 42.5560]}},

    {"_id": "and-sant-julia",       "region": "andorra",
     "loc": {"type": "Point", "coordinates": [1.4914, 42.4635]}},

    # Demo capacity nodes — connected by a single-track mountain road (capacity=1)
    {"_id": "and-la-massana",       "region": "andorra",
     "loc": {"type": "Point", "coordinates": [1.5144, 42.5452]}},   # parish capital NW

    {"_id": "and-arinsal",          "region": "andorra",
     "loc": {"type": "Point", "coordinates": [1.4847, 42.5733]}},   # mountain village

    # ── Laos ───────────────────────────────────────────────────────────────────
    {"_id": "laos-vientiane",       "region": "laos",
     "loc": {"type": "Point", "coordinates": [102.6331, 17.9757]}},  # capital

    {"_id": "laos-luang-prabang",   "region": "laos",
     "loc": {"type": "Point", "coordinates": [102.1351, 19.8845]}},

    {"_id": "laos-pakse",           "region": "laos",
     "loc": {"type": "Point", "coordinates": [105.7833, 15.1167]}},

    {"_id": "laos-savannakhet",     "region": "laos",
     "loc": {"type": "Point", "coordinates": [104.7500, 16.5500]}},

    {"_id": "laos-thakhek",         "region": "laos",
     "loc": {"type": "Point", "coordinates": [104.8167, 17.4000]}},

    # Shared border node — exists in BOTH laos AND cambodia graphs
    {"_id": "border-laos-camb",     "region": "laos",
     "loc": {"type": "Point", "coordinates": [105.7900, 13.9200]}},  # Voen Kham crossing

    # ── Cambodia ───────────────────────────────────────────────────────────────
    {"_id": "khm-phnom-penh",       "region": "cambodia",
     "loc": {"type": "Point", "coordinates": [104.9160, 11.5625]}},  # capital

    {"_id": "khm-siem-reap",        "region": "cambodia",
     "loc": {"type": "Point", "coordinates": [103.8597, 13.3671]}},

    {"_id": "khm-battambang",       "region": "cambodia",
     "loc": {"type": "Point", "coordinates": [103.1990, 13.0957]}},

    {"_id": "khm-kompong-cham",     "region": "cambodia",
     "loc": {"type": "Point", "coordinates": [105.4635, 11.9927]}},

    {"_id": "khm-stung-treng",      "region": "cambodia",
     "loc": {"type": "Point", "coordinates": [105.9683, 13.5260]}},

    {"_id": "khm-kratie",           "region": "cambodia",
     "loc": {"type": "Point", "coordinates": [106.0167, 12.4833]}},

    # Cambodia-side border node (Don Kralor crossing — same physical location)
    {"_id": "border-camb-laos",     "region": "cambodia",
     "loc": {"type": "Point", "coordinates": [105.7900, 13.9200]}},
]


# ── 3. Define edges ───────────────────────────────────────────────────────────
# Format: { from, to, region, road_type, distance_km }
# road_type must be in route-service's MAJOR_ROADS set.

def bidir(frm, to, region, km, rtype="primary"):
    """Return both directions of a road."""
    return [
        {"from": frm, "to": to, "region": region, "road_type": rtype, "distance_km": round(km, 2)},
        {"from": to, "to": frm, "region": region, "road_type": rtype, "distance_km": round(km, 2)},
    ]


EDGES = []

# ── Andorra roads ──────────────────────────────────────────────────────────────
# Andorra is a tiny mountain country — no motorways, mostly secondary/tertiary.
for pair, km, rtype in [
    (("and-andorra-la-vella", "and-escaldes"),    2.0, "secondary"),   # urban connector, cap=75
    (("and-andorra-la-vella", "and-sant-julia"),  7.5, "primary"),     # main road south, cap=100
    (("and-andorra-la-vella", "and-ordino"),     10.0, "secondary"),   # road north, cap=75
    (("and-escaldes",         "and-encamp"),      5.5, "secondary"),   # valley road, cap=75
    (("and-encamp",           "and-canillo"),     6.0, "tertiary"),    # mountain road, cap=50
    (("and-ordino",           "and-canillo"),    12.0, "tertiary"),    # mountain pass, cap=50
]:
    EDGES.extend(bidir(*pair, "andorra", km, rtype))

# Demo road — capacity 1 (single-track mountain trail, one vehicle at a time)
EDGES.extend(bidir("and-la-massana", "and-arinsal", "andorra", 8.0, "track"))

# ── Laos roads ────────────────────────────────────────────────────────────────
# Route 13 is Laos's main national artery (trunk). Border road is primary.
for pair, km, rtype in [
    (("laos-vientiane",     "laos-thakhek"),       220, "trunk"),     # Route 13 south, cap=150
    (("laos-thakhek",       "laos-savannakhet"),   125, "trunk"),     # Route 13 south, cap=150
    (("laos-savannakhet",   "laos-pakse"),         230, "trunk"),     # Route 13 south, cap=150
    (("laos-pakse",         "border-laos-camb"),   100, "primary"),   # border approach, cap=100
    (("laos-vientiane",     "laos-luang-prabang"), 380, "primary"),   # Route 13 north, cap=100
]:
    EDGES.extend(bidir(*pair, "laos", km, rtype))

# ── Cambodia roads ────────────────────────────────────────────────────────────
# NR5/NR6 are trunk-level. Northern rural routes are secondary.
for pair, km, rtype in [
    (("border-camb-laos",  "khm-stung-treng"),    45, "primary"),    # border entry, cap=100
    (("khm-stung-treng",   "khm-kratie"),         140, "primary"),   # Route 7, cap=100
    (("khm-kratie",        "khm-kompong-cham"),   145, "primary"),   # Route 7, cap=100
    (("khm-kompong-cham",  "khm-phnom-penh"),     120, "trunk"),     # NR7 to capital, cap=150
    (("khm-phnom-penh",    "khm-battambang"),     295, "trunk"),     # NR5 main highway, cap=150
    (("khm-battambang",    "khm-siem-reap"),       95, "primary"),   # NR6, cap=100
    (("khm-siem-reap",     "khm-stung-treng"),    250, "secondary"), # rural north, cap=75
]:
    EDGES.extend(bidir(*pair, "cambodia", km, rtype))


# ── 4. Insert nodes and edges into each regional MongoDB ──────────────────────

for region, db in REGION_DBS.items():
    region_nodes = [n for n in NODES if n["region"] == region]
    region_edges = [e for e in EDGES if e["region"] == region]
    print(f"Inserting {len(region_nodes)} nodes, {len(region_edges)} edges → {region} MongoDB ...")
    if region_nodes:
        db.osm_nodes.insert_many(region_nodes)
    if region_edges:
        db.osm_edges.insert_many(region_edges)

# ── 5. Ensure indexes in each regional MongoDB ────────────────────────────────
# route-service needs: 2dsphere on loc, compound (from, region) on edges
# validation-service needs: (region) on edges for Redis seeding

print("Ensuring indexes ...")
for region, db in REGION_DBS.items():
    db.osm_nodes.create_index([("loc", GEOSPHERE)])
    db.osm_nodes.create_index([("region", ASCENDING)])
    db.osm_edges.create_index([("region", ASCENDING)])
    db.osm_edges.create_index([("from", ASCENDING)])
    db.osm_edges.create_index([("from", ASCENDING), ("region", ASCENDING)])

print()
print("Custom graph import complete.")
for region, db in REGION_DBS.items():
    n = db.osm_nodes.count_documents({"region": region})
    e = db.osm_edges.count_documents({"region": region})
    print(f"  {region:<10}: {n} nodes, {e} edges")
print()
print("Next steps:")
print("  docker restart route-service-andorra route-service-laos route-service-cambodia")
print("  docker restart validation-service-andorra validation-service-laos validation-service-cambodia")
