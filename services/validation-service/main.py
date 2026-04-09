"""
Validation Service
==================
Consumes `booking-requests` from Kafka.

For EVERY message it:
  1. Checks `target_region` and skips messages not intended for this region.
  2. Acquires Redis distributed locks on all route segments.
  3. Checks Redis occupancy counters vs. segment capacities.
  4. Checks MongoDB for a duplicate active booking on the same vehicle.
  5. Publishes APPROVED or REJECTED to `booking-outcomes`, including `saga_id`
     for cross-region sub-bookings so journey-management can advance the saga.

Capacity rollback
-----------------
Also consumes `capacity-releases` to decrement Redis counters when a booking
is cancelled or a saga is aborted. This is the compensating transaction.

Redis is regional — this service owns the segment locks and counters
for its region directly. MongoDB access (bookings, vehicle lookup) goes
via data-service HTTP.
"""

import json
import os
import sys
import threading
import time
from datetime import datetime, UTC

import redis as redis_lib

sys.path.insert(0, "/app/data")

from fastapi import FastAPI

app = FastAPI(title="Validation Service")

SERVICE_NAME            = os.getenv("SERVICE_NAME", "validation-service")
REGION                  = os.getenv("REGION", "local")
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
REDIS_URL               = os.getenv("REDIS_URL", "redis://localhost:6379")
MONGO_URI               = os.getenv("MONGO_URI", "mongodb://mongo:27017")
LOCK_TTL                = 10  # seconds

# Capacity per road type — default 100 for anything not listed
ROAD_TYPE_CAPACITY = {
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
    "track":              1,   # single-vehicle mountain lane — demo capacity limit
}

_redis = redis_lib.from_url(REDIS_URL, decode_responses=True)


def now_utc_iso() -> str:
    return datetime.now(UTC).isoformat()


# =============================================================================
# Redis key helpers
# =============================================================================

def _lock_key(f: str, t: str) -> str:
    return f"lock:segment:{f}:{t}"

def _cap_key(f: str, t: str) -> str:
    return f"capacity:segment:{f}:{t}"

def _cur_key(f: str, t: str) -> str:
    return f"current:segment:{f}:{t}"


# =============================================================================
# Segment validation — owns Redis directly (regional)
# =============================================================================

def validate_and_reserve(segments: list[dict], vehicle_id: str) -> dict:
    """
    Acquire locks, check capacity, check duplicate booking, increment counters.
    All Redis ops are local to this region's Redis instance.
    Duplicate vehicle check goes via data-service HTTP (MongoDB).
    """
    import data_service as ds

    sorted_segs = sorted(segments, key=lambda s: _lock_key(s["from"], s["to"]))
    acquired: list[tuple[str, str]] = []

    try:
        for seg in sorted_segs:
            if not _redis.set(_lock_key(seg["from"], seg["to"]), "1", nx=True, ex=LOCK_TTL):
                return {
                    "outcome": "REJECTED",
                    "reason":  f"Segment lock contention on {seg['from']}->{seg['to']} — please retry",
                }
            acquired.append((seg["from"], seg["to"]))

        for seg in sorted_segs:
            raw_cap = _redis.get(_cap_key(seg["from"], seg["to"]))
            cap = int(raw_cap) if raw_cap is not None else 0  # 0 = block if unseeded
            cur = int(_redis.get(_cur_key(seg["from"], seg["to"])) or 0)
            if cur >= cap:
                return {
                    "outcome": "REJECTED",
                    "reason":  f"Road segment {seg['from']}->{seg['to']} at full capacity ({cur}/{cap})",
                }

        if ds.get_vehicle_booking_fallback(vehicle_id):
            return {
                "outcome": "REJECTED",
                "reason":  f"Vehicle {vehicle_id} already has an active booking",
            }

        for seg in sorted_segs:
            _redis.incr(_cur_key(seg["from"], seg["to"]))

        print(f"[{REGION}] Redis locks released, counters incremented for {len(sorted_segs)} segments")
        return {"outcome": "APPROVED", "reason": "Booking approved"}

    finally:
        for fn, tn in reversed(acquired):
            try:
                _redis.delete(_lock_key(fn, tn))
            except Exception:
                pass


def release_segments(segments: list[dict]):
    """Decrement counters for cancelled or rolled-back bookings."""
    for seg in segments:
        key = _cur_key(seg["from"], seg["to"])
        if int(_redis.get(key) or 0) > 0:
            _redis.decr(key)


# =============================================================================
# Message helpers
# =============================================================================

def _message_segments(message: dict) -> list[dict]:
    segments = message.get("segments") or []
    return [
        {"from": seg.get("from"), "to": seg.get("to")}
        for seg in segments
        if seg.get("from") and seg.get("to")
    ]


# =============================================================================
# HTTP endpoints
# =============================================================================

@app.get("/health")
def health():
    try:
        redis_ok = bool(_redis.ping())
    except Exception:
        redis_ok = False
    return {"status": "ok", "service": SERVICE_NAME, "region": REGION, "redis": redis_ok}


@app.get("/validation/status")
def status():
    return {"consumer_group": f"validation-group-{REGION}", "topic": "booking-requests", "region": REGION}


@app.post("/internal/reset-counters")
def reset_counters():
    """
    Reset all current:segment:* counters to 0.
    Capacity limits (capacity:segment:*) are preserved.
    For load testing / demo resets only — not for production use.
    """
    keys = _redis.keys("current:segment:*")
    if keys:
        pipe = _redis.pipeline(transaction=False)
        for k in keys:
            pipe.set(k, 0)
        pipe.execute()
    return {"reset": len(keys), "region": REGION}


# =============================================================================
# Kafka handlers
# =============================================================================

def _validate_and_publish(message: dict, producer):
    """Validate one booking (or sub-booking) and publish the outcome."""
    import data_service as ds

    booking_id  = message.get("booking_id", "unknown")
    saga_id     = message.get("saga_id")
    vehicle_id  = message.get("vehicle_id")
    segments    = _message_segments(message)
    origin      = segments[0]["from"] if segments else message.get("origin")
    destination = segments[-1]["to"]  if segments else message.get("destination")

    try:
        if not segments:
            outcome = "REJECTED"
            reason  = "Booking contains no valid segments"
        else:
            result  = validate_and_reserve(segments, vehicle_id)
            outcome = result["outcome"]
            reason  = result["reason"]
            if not saga_id:
                ds.insert_booking({
                    **{k: v for k, v in message.items() if k != "_id"},
                    "status":      outcome.lower(),
                    "approved_at": now_utc_iso() if outcome == "APPROVED" else None,
                    "rejected_at": now_utc_iso() if outcome == "REJECTED" else None,
                    "reject_reason": reason if outcome == "REJECTED" else None,
                    "region":      REGION,   # override booking-service's region with actual processing region
                })
    except Exception as exc:
        outcome = "REJECTED"
        reason  = f"Internal validation error: {exc}"

    print(f"[{REGION}] booking {booking_id} → {outcome} ({reason})")

    result = {
        "booking_id":    booking_id,
        "driver_id":     message.get("driver_id"),
        "vehicle_id":    vehicle_id,
        "origin":        origin,
        "destination":   destination,
        "region":        REGION,
        "target_region": message.get("target_region", REGION),
        "outcome":       outcome,
        "reason":        reason,
    }
    if saga_id:
        result["saga_id"]       = saga_id
        result["is_sub_booking"] = True

    producer.send("booking-outcomes", result)


def _release_capacity(message: dict):
    """Decrement counters when a booking is cancelled or saga is aborted."""
    target_region = message.get("target_region", message.get("region", REGION))
    if target_region != REGION:
        return
    release_segments(_message_segments(message))


def _booking_consumer_loop():
    from kafka import KafkaConsumer, KafkaProducer

    try:
        consumer = KafkaConsumer(
            "booking-requests",
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            value_deserializer=lambda m: json.loads(m.decode("utf-8")),
            group_id=f"validation-group-{REGION}",
            auto_offset_reset="earliest",
        )
        producer = KafkaProducer(
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        )
        for msg in consumer:
            payload = msg.value
            target  = payload.get("target_region")
            if target and target != REGION:
                continue
            print(f"[{REGION}] received from Kafka: booking {payload.get('booking_id')} saga={payload.get('saga_id')}")
            _validate_and_publish(payload, producer)
    except Exception as exc:
        print(f"[validation-service] booking consumer error: {exc}")


def _release_consumer_loop():
    from kafka import KafkaConsumer

    try:
        consumer = KafkaConsumer(
            "capacity-releases",
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            value_deserializer=lambda m: json.loads(m.decode("utf-8")),
            group_id=f"capacity-release-group-{REGION}",
            auto_offset_reset="earliest",
        )
        for msg in consumer:
            _release_capacity(msg.value)
    except Exception as exc:
        print(f"[validation-service] release consumer error: {exc}")


# =============================================================================
# Startup — seed Redis capacity from OSM edges
# =============================================================================

def _seed_redis_capacity():
    """
    Seed Redis capacity counters from osm_edges for this region.
    Uses SET NX so a restart does not overwrite live counters.
    Uses pipelining to batch Redis writes — avoids one round-trip per edge.
    MONGO_URI points to the regional MongoDB (Phase 2 will set this per-region;
    for now it points to the shared mongo).
    """
    from pymongo import MongoClient

    BATCH = 500  # pipeline flush size

    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        db     = client["traffic"]
        edges  = db.osm_edges.find(
            {"region": REGION},
            {"from": 1, "to": 1, "road_type": 1},
        )

        seeded = 0
        pipe   = _redis.pipeline(transaction=False)

        for edge in edges:
            f = edge.get("from")
            t = edge.get("to")
            if not f or not t:
                continue
            capacity = ROAD_TYPE_CAPACITY.get(edge.get("road_type", ""), 100)
            pipe.set(_cap_key(f, t), capacity, nx=True)
            pipe.set(_cur_key(f, t), 0,        nx=True)
            seeded += 1
            if seeded % BATCH == 0:
                pipe.execute()
                pipe = _redis.pipeline(transaction=False)

        pipe.execute()  # flush remainder
        client.close()
        print(f"[validation-service] Seeded {seeded} Redis segment keys for region '{REGION}'")
    except Exception as exc:
        print(f"[validation-service] Redis seed warning: {exc}")


def _redis_watchdog():
    """
    Polls Redis every 30s. If it detects an empty store (capacity keys gone —
    e.g. after a Redis crash and restart), re-seeds from MongoDB automatically.
    Uses a probe key written at startup; if that key disappears, Redis was wiped.
    """
    PROBE_KEY    = f"watchdog:probe:{REGION}"
    POLL_SECONDS = 30

    # Write the probe key — if Redis is up this always succeeds
    try:
        _redis.set(PROBE_KEY, "1")
    except Exception:
        pass

    while True:
        time.sleep(POLL_SECONDS)
        try:
            if not _redis.exists(PROBE_KEY):
                print(f"[validation-service] Redis probe key missing for '{REGION}' — re-seeding capacity")
                _seed_redis_capacity()
                _redis.set(PROBE_KEY, "1")
        except Exception as exc:
            print(f"[validation-service] Redis watchdog error: {exc}")


@app.on_event("startup")
def start_consumers():
    for fn in (_seed_redis_capacity, _booking_consumer_loop, _release_consumer_loop, _redis_watchdog):
        t = threading.Thread(target=fn, daemon=True)
        t.start()
