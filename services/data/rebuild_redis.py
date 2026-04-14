"""
rebuild_redis.py — Redis state recovery script
===============================================
Run this after a Redis restart to restore capacity counters and current
occupancy from durable MongoDB state for a single region.

Usage (inside the container or via docker exec):
    python rebuild_redis.py

Environment variables (same as data-service):
    REGION    — region name, e.g. "laos"
    MONGO_URI — MongoDB connection string, e.g. "mongodb://mongo-laos:27017"
    REDIS_URL — Redis connection string, e.g. "redis://redis-laos:6379"

What it does:
    1. Reads every osm_edge for this region from MongoDB and writes
       capacity:segment:{from}:{to} keys (hard overwrite — recovery intent).
    2. Resets every current:segment:{from}:{to} key to 0.
    3. Scans all approved/pending bookings for this region and increments
       current:segment:{from}:{to} for every segment in each booking.

This matches exactly the Redis key schema owned by validation-service.
"""

import os
import sys
import logging

from pymongo import MongoClient
import redis as redis_lib

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("rebuild_redis")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REGION            = os.getenv("REGION",            "laos")
MONGO_URI         = os.getenv("MONGO_URI",         "mongodb://localhost:27017")
MONGO_SHARD_1_URI = os.getenv("MONGO_SHARD_1_URI", "")
REDIS_URL         = os.getenv("REDIS_URL",         "redis://localhost:6379")

# Must match ROAD_TYPE_CAPACITY in validation-service/main.py exactly.
ROAD_TYPE_CAPACITY: dict[str, int] = {
    "motorway":         200,
    "motorway_link":    100,
    "trunk":            150,
    "trunk_link":        75,
    "primary":          100,
    "primary_link":      50,
    "secondary":         75,
    "secondary_link":    40,
    "tertiary":          50,
    "tertiary_link":     25,
    "track":              1,
}
DEFAULT_CAPACITY = 100

PIPELINE_BATCH = 500


# ---------------------------------------------------------------------------
# Key helpers (mirror validation-service)
# ---------------------------------------------------------------------------

def _cap_key(f: str, t: str) -> str:
    return f"capacity:segment:{f}:{t}"

def _cur_key(f: str, t: str) -> str:
    return f"current:segment:{f}:{t}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("=== rebuild_redis starting  region=%s ===", REGION)

    # Connect
    try:
        mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        mongo.admin.command("ping")
        db = mongo["traffic"]
        log.info("MongoDB connected  uri=%s", MONGO_URI)
    except Exception as exc:
        log.error("MongoDB connection failed: %s", exc)
        sys.exit(1)

    try:
        r = redis_lib.from_url(REDIS_URL, decode_responses=True)
        r.ping()
        log.info("Redis connected  url=%s", REDIS_URL)
    except Exception as exc:
        log.error("Redis connection failed: %s", exc)
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 1 — Seed capacity limits from osm_edges
    # ------------------------------------------------------------------
    log.info("Step 1: seeding capacity limits from osm_edges …")

    edges = db.osm_edges.find(
        {"region": REGION},
        {"from": 1, "to": 1, "road_type": 1},
    )

    pipe = r.pipeline(transaction=False)
    seeded = 0

    for edge in edges:
        f = edge.get("from")
        t = edge.get("to")
        if not f or not t:
            continue
        capacity = ROAD_TYPE_CAPACITY.get(edge.get("road_type", ""), DEFAULT_CAPACITY)
        pipe.set(_cap_key(f, t), capacity)   # hard overwrite — this is a recovery
        pipe.set(_cur_key(f, t), 0)          # reset occupancy to zero before scan
        seeded += 1
        if seeded % PIPELINE_BATCH == 0:
            pipe.execute()
            pipe = r.pipeline(transaction=False)

    pipe.execute()
    log.info("  seeded %d segment capacity keys", seeded)

    if seeded == 0:
        log.warning(
            "No osm_edges found for region '%s'. "
            "Has OSM data been imported? Run import_osm.py first.",
            REGION,
        )

    # ------------------------------------------------------------------
    # Step 2 — Reconstruct current occupancy from approved/pending bookings
    # ------------------------------------------------------------------
    log.info("Step 2: reconstructing current occupancy from bookings …")

    # Connect to shard 1 if configured — bookings are hash-sharded across both.
    shard_dbs = [db]
    if MONGO_SHARD_1_URI:
        try:
            mongo_s1 = MongoClient(MONGO_SHARD_1_URI, serverSelectionTimeoutMS=5000)
            mongo_s1.admin.command("ping")
            shard_dbs.append(mongo_s1["traffic"])
            log.info("  shard 1 connected  uri=%s", MONGO_SHARD_1_URI)
        except Exception as exc:
            log.warning("  shard 1 connection failed: %s — scanning shard 0 only", exc)

    incremented = 0
    total_bookings = 0
    for shard_idx, shard_db in enumerate(shard_dbs):
        bookings = list(shard_db.bookings.find(
            {"region": REGION, "status": {"$in": ["approved", "pending"]}},
            {"booking_id": 1, "segments": 1, "status": 1},
        ))
        log.info("  shard %d: found %d approved/pending bookings for region '%s'",
                 shard_idx, len(bookings), REGION)
        total_bookings += len(bookings)
        for booking in bookings:
            segments = booking.get("segments") or []
            for seg in segments:
                f = seg.get("from")
                t = seg.get("to")
                if f and t:
                    r.incr(_cur_key(f, t))
                    incremented += 1

    log.info("  incremented %d segment counters across %d bookings (all shards)",
             incremented, total_bookings)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    log.info("=== rebuild_redis complete ===")
    log.info("  region        : %s", REGION)
    log.info("  shards scanned: %d", len(shard_dbs))
    log.info("  capacity keys : %d", seeded)
    log.info("  bookings read : %d", total_bookings)
    log.info("  incr ops      : %d", incremented)


if __name__ == "__main__":
    main()
