#!/usr/bin/env python3
"""
scripts/import_osm.py
=====================
One-time (or periodic) script to parse a Geofabrik OSM PBF extract and
load the driving road network into MongoDB for use by route-service.

Usage:
    pip install -r scripts/requirements_import.txt
    python scripts/import_osm.py \
        --pbf data/ireland-and-northern-ireland-latest.osm.pbf \
        --region europe \
        --mongo-uri mongodb://localhost:27017

    # Clear existing region data before re-import:
    python scripts/import_osm.py --pbf data/india-latest.osm.pbf --region south-asia --clear

Download PBF extracts from https://download.geofabrik.de/
    europe (Ireland): https://download.geofabrik.de/europe/ireland-and-northern-ireland-latest.osm.pbf
    south-asia (India): https://download.geofabrik.de/asia/india-latest.osm.pbf
    middle-east (Turkey): https://download.geofabrik.de/europe/turkey-latest.osm.pbf
    middle-east (Iran):   https://download.geofabrik.de/asia/iran-latest.osm.pbf
    middle-east (Pakistan): https://download.geofabrik.de/asia/pakistan-latest.osm.pbf
    north-america (US Northeast): https://download.geofabrik.de/north-america/us/new-york-latest.osm.pbf

Collections written to MongoDB (database: traffic):
    osm_nodes  — { _id: str(osm_id), loc: GeoJSON Point, region: str }
    osm_edges  — { from: str, to: str, distance_km: float, road_type: str, region: str }

Indexes created (idempotent, safe to re-run):
    osm_nodes: 2dsphere on loc (enables nearest-node lookup in route-service)
    osm_nodes: ascending on region
    osm_edges: ascending on from (fast neighbour lookup for A*)
    osm_edges: compound (from, region)
"""

import argparse
import math
import sys
import time

import osmium
from pymongo import MongoClient, UpdateOne, GEOSPHERE, ASCENDING

# Driving-relevant road types only — excludes footways, cycleways, paths, etc.
VALID_HIGHWAYS = {
    "motorway", "motorway_link",
    "trunk", "trunk_link",
    "primary", "primary_link",
    "secondary", "secondary_link",
    "tertiary", "tertiary_link",
    "unclassified", "residential",
}

BATCH_SIZE = 500


def haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km between two lat/lng points (Haversine formula)."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class RoadHandler(osmium.SimpleHandler):
    """
    osmium handler that extracts driving road nodes and directed edges.

    Must be applied with locations=True so node coordinates are resolved
    inside way handlers (osmium stores a node location cache internally).

    One-way streets (oneway=yes) produce a single directed edge.
    All other roads produce two directed edges (both directions).
    """

    def __init__(self, region: str):
        super().__init__()
        self.region = region
        self._nodes: dict[int, tuple[float, float]] = {}   # osm_id -> (lat, lng)
        self._edges: list[dict] = []

    def way(self, w):
        if "highway" not in w.tags:
            return
        if w.tags["highway"] not in VALID_HIGHWAYS:
            return

        is_oneway = w.tags.get("oneway", "no") in ("yes", "1", "true")
        road_type = w.tags["highway"]

        # Collect node refs with valid locations for this way
        node_refs = []
        for n in w.nodes:
            if n.location.valid():
                node_refs.append((n.ref, n.location.lat, n.location.lon))
                self._nodes[n.ref] = (n.location.lat, n.location.lon)

        # Build one directed edge per consecutive node pair
        for i in range(len(node_refs) - 1):
            from_id, from_lat, from_lng = node_refs[i]
            to_id, to_lat, to_lng = node_refs[i + 1]
            dist = haversine(from_lat, from_lng, to_lat, to_lng)

            self._edges.append({
                "from": str(from_id),
                "to": str(to_id),
                "distance_km": round(dist, 4),
                "road_type": road_type,
                "region": self.region,
            })
            if not is_oneway:
                self._edges.append({
                    "from": str(to_id),
                    "to": str(from_id),
                    "distance_km": round(dist, 4),
                    "road_type": road_type,
                    "region": self.region,
                })

    def node_docs(self):
        """Yield MongoDB node documents with GeoJSON Point for 2dsphere index."""
        for osm_id, (lat, lng) in self._nodes.items():
            yield {
                "_id": str(osm_id),
                # GeoJSON requires [longitude, latitude] order
                "loc": {"type": "Point", "coordinates": [round(lng, 7), round(lat, 7)]},
                "region": self.region,
            }

    def edge_docs(self):
        yield from self._edges


def _flush_nodes(collection, batch: list):
    if not batch:
        return
    ops = [
        UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True)
        for doc in batch
    ]
    collection.bulk_write(ops, ordered=False)


def _flush_edges(collection, batch: list):
    if not batch:
        return
    # Edges have no natural unique key so use insert_many; run with --clear to avoid dupes
    try:
        collection.insert_many(batch, ordered=False)
    except Exception:
        pass  # ignore duplicate key errors on re-run without --clear


def ensure_indexes(db):
    db.osm_nodes.create_index([("loc", GEOSPHERE)])
    db.osm_nodes.create_index([("region", ASCENDING)])
    db.osm_edges.create_index([("from", ASCENDING)])
    db.osm_edges.create_index([("from", ASCENDING), ("region", ASCENDING)])
    print("Indexes ensured.")


def main():
    parser = argparse.ArgumentParser(
        description="Import Geofabrik OSM PBF road data into MongoDB for route-service"
    )
    parser.add_argument("--pbf", required=True,
                        help="Path to local OSM PBF file (download from geofabrik.de)")
    parser.add_argument("--region", required=True,
                        help="Region tag written to every document "
                             "(e.g. europe, south-asia, middle-east, north-america)")
    parser.add_argument("--mongo-uri", default="mongodb://localhost:27017",
                        help="MongoDB connection URI (default: mongodb://localhost:27017)")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                        help=f"Documents per MongoDB bulk write (default: {BATCH_SIZE})")
    parser.add_argument("--clear", action="store_true",
                        help="Delete all existing documents for this region before import")
    args = parser.parse_args()

    # Connect
    client = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=5000)
    db = client["traffic"]
    try:
        client.admin.command("ping")
        print(f"Connected to MongoDB at {args.mongo_uri}")
    except Exception as e:
        print(f"Cannot connect to MongoDB: {e}", file=sys.stderr)
        sys.exit(1)

    if args.clear:
        print(f"Clearing existing data for region '{args.region}'...")
        r1 = db.osm_nodes.delete_many({"region": args.region})
        r2 = db.osm_edges.delete_many({"region": args.region})
        print(f"  Removed {r1.deleted_count:,} nodes, {r2.deleted_count:,} edges.")

    # Parse PBF
    print(f"Parsing {args.pbf}  (region={args.region})...")
    print("  This may take several minutes for large extracts.")
    t0 = time.time()
    handler = RoadHandler(args.region)
    handler.apply_file(args.pbf, locations=True)
    elapsed = time.time() - t0
    print(f"  Parsed in {elapsed:.1f}s "
          f"— {len(handler._nodes):,} road nodes, {len(handler._edges):,} directed edges")

    # Write nodes
    print("Writing nodes...")
    batch: list = []
    total_nodes = 0
    for doc in handler.node_docs():
        batch.append(doc)
        if len(batch) >= args.batch_size:
            _flush_nodes(db.osm_nodes, batch)
            total_nodes += len(batch)
            batch = []
            print(f"  {total_nodes:,} nodes written...", end="\r")
    _flush_nodes(db.osm_nodes, batch)
    total_nodes += len(batch)
    print(f"  {total_nodes:,} nodes written.      ")

    # Write edges
    print("Writing edges...")
    batch = []
    total_edges = 0
    for doc in handler.edge_docs():
        batch.append(doc)
        if len(batch) >= args.batch_size:
            _flush_edges(db.osm_edges, batch)
            total_edges += len(batch)
            batch = []
            print(f"  {total_edges:,} edges written...", end="\r")
    _flush_edges(db.osm_edges, batch)
    total_edges += len(batch)
    print(f"  {total_edges:,} edges written.      ")

    ensure_indexes(db)
    print(f"\nDone. Region '{args.region}': {total_nodes:,} nodes, {total_edges:,} edges.")


if __name__ == "__main__":
    main()
