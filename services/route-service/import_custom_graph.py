"""
Custom Lightweight Road Graph Import
=====================================
Replaces heavy OSM data with a small hand-crafted graph for demo purposes.

Regions covered:
  - andorra   (6 nodes, isolated — no land border to other regions)
  - laos      (6 nodes, connected to cambodia via Voen Kham border)
  - cambodia  (7 nodes, connected to laos via Don Kralor border)

The Laos/Cambodia border uses a SHARED node ID "border-laos-camb" that
exists in BOTH regions' graphs. journey-management uses this as the
gateway node for cross-region routing.

Run this script ONCE on any machine that has Docker running:
    python import_custom_graph.py

It will:
  1. Clear existing osm_nodes and osm_edges for these three regions.
  2. Insert the custom nodes and edges.
  3. Create the 2dsphere index needed by route-service.

Teammate instructions:
  - Make sure Docker is running and containers are up (docker-compose up -d)
  - Install pymongo if needed:  pip install pymongo
  - Run from the project root:  python services/route-service/import_custom_graph.py
  - Then restart the route services:
      docker restart route-service-andorra route-service-laos route-service-cambodia
      docker restart validation-service-laos validation-service-cambodia
"""

from pymongo import MongoClient, GEOSPHERE

MONGO_URI = "mongodb://localhost:27017"   # default local Docker port

client = MongoClient(MONGO_URI)
db = client["traffic"]

# ── 1. Clear existing data for these regions ──────────────────────────────────

print("Clearing existing nodes and edges for andorra / laos / cambodia ...")
db.osm_nodes.delete_many({"region": {"$in": ["andorra", "laos", "cambodia"]}})
db.osm_edges.delete_many({"region": {"$in": ["andorra", "laos", "cambodia"]}})
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
for pair, km in [
    (("and-andorra-la-vella", "and-escaldes"),    2.0),
    (("and-andorra-la-vella", "and-sant-julia"),  7.5),
    (("and-andorra-la-vella", "and-ordino"),     10.0),
    (("and-escaldes",         "and-encamp"),      5.5),
    (("and-encamp",           "and-canillo"),     6.0),
    (("and-ordino",           "and-canillo"),    12.0),
]:
    EDGES.extend(bidir(*pair, "andorra", km))

# ── Laos roads ────────────────────────────────────────────────────────────────
for pair, km in [
    (("laos-vientiane",     "laos-thakhek"),       220),
    (("laos-thakhek",       "laos-savannakhet"),   125),
    (("laos-savannakhet",   "laos-pakse"),         230),
    (("laos-pakse",         "border-laos-camb"),   100),
    (("laos-vientiane",     "laos-luang-prabang"), 380),
]:
    EDGES.extend(bidir(*pair, "laos", km))

# ── Cambodia roads ────────────────────────────────────────────────────────────
for pair, km in [
    (("border-camb-laos",  "khm-stung-treng"),   45),
    (("khm-stung-treng",   "khm-kratie"),        140),
    (("khm-kratie",        "khm-kompong-cham"),  145),
    (("khm-kompong-cham",  "khm-phnom-penh"),    120),
    (("khm-phnom-penh",    "khm-battambang"),    295),
    (("khm-battambang",    "khm-siem-reap"),      95),
    (("khm-siem-reap",     "khm-stung-treng"),   250),
]:
    EDGES.extend(bidir(*pair, "cambodia", km))


# ── 4. Insert nodes and edges ─────────────────────────────────────────────────

print(f"Inserting {len(NODES)} nodes ...")
db.osm_nodes.insert_many(NODES)

print(f"Inserting {len(EDGES)} edges ...")
db.osm_edges.insert_many(EDGES)

# ── 5. Ensure indexes (needed by route-service) ───────────────────────────────
print("Ensuring indexes ...")
db.osm_nodes.create_index([("loc", GEOSPHERE)])
db.osm_nodes.create_index([("region", 1)])
db.osm_edges.create_index([("region", 1)])

print()
print("Custom graph import complete.")
print(f"  andorra : {db.osm_nodes.count_documents({'region':'andorra'})} nodes, "
      f"{db.osm_edges.count_documents({'region':'andorra'})} edges")
print(f"  laos    : {db.osm_nodes.count_documents({'region':'laos'})} nodes, "
      f"{db.osm_edges.count_documents({'region':'laos'})} edges")
print(f"  cambodia: {db.osm_nodes.count_documents({'region':'cambodia'})} nodes, "
      f"{db.osm_edges.count_documents({'region':'cambodia'})} edges")
print()
print("Next steps:")
print("  docker restart route-service-andorra route-service-laos route-service-cambodia")
print("  docker restart validation-service-laos validation-service-cambodia")
