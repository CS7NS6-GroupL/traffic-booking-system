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
import logging
import os
import sys
import threading
import time
from datetime import datetime, UTC

import httpx
import redis as redis_lib

sys.path.insert(0, "/app/data")

from fastapi import FastAPI

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("validation-service")

app = FastAPI(title="Validation Service")

SERVICE_NAME            = os.getenv("SERVICE_NAME", "validation-service")
REGION                  = os.getenv("REGION", "local")
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
REDIS_URL               = os.getenv("REDIS_URL", "redis://localhost:6379")
MONGO_URI               = os.getenv("MONGO_URI", "mongodb://mongo:27017")
DATA_SERVICE_URL        = os.getenv("DATA_SERVICE_URL", "http://data-gateway:8004")
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

LUA_SAFE_DECR = """
local cur = tonumber(redis.call('GET', KEYS[1]) or '0')
if cur > 0 then
    return redis.call('DECR', KEYS[1])
else
    return 0
end
"""
_safe_decr = _redis.register_script(LUA_SAFE_DECR)


def _safe_release(f: str, t: str):
    _safe_decr(keys=[_cur_key(f, t)])


# Thread references for health check
_booking_thread = None
_release_thread = None


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

        log.info("[%s] Redis locks released, counters incremented for %d segments", REGION, len(sorted_segs))
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
        _safe_release(seg["from"], seg["to"])


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
    checks = {}
    try:
        _redis.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"
    try:
        r = httpx.get(f"{DATA_SERVICE_URL}/health", timeout=3.0)
        checks["data_service"] = "ok" if r.status_code == 200 else f"http {r.status_code}"
    except Exception as e:
        checks["data_service"] = f"error: {e}"
    checks["booking_consumer_alive"] = _booking_thread is not None and _booking_thread.is_alive()
    checks["release_consumer_alive"] = _release_thread is not None and _release_thread.is_alive()
    ok = checks["redis"] == "ok" and checks["data_service"] == "ok"
    return {"status": "ok" if ok else "degraded", "service": SERVICE_NAME,
            "region": REGION, "checks": checks}


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
    driver_id   = message.get("driver_id")
    saga_id     = message.get("saga_id")
    vehicle_id  = message.get("vehicle_id")
    segments    = _message_segments(message)
    origin      = segments[0]["from"] if segments else message.get("origin")
    destination = segments[-1]["to"]  if segments else message.get("destination")

    try:
        existing = ds.get_booking_by_id(booking_id)
    except Exception as exc:
        log.warning("[%s] dedup check unavailable for booking_id=%s — treating as new: %s",
                    REGION, booking_id, exc)
        existing = None
    if existing and existing.get("status") in ("approved", "rejected", "pending"):
        log.warning("[%s] dedup.skip booking_id=%s already processed status=%s",
                    REGION, booking_id, existing.get("status"))
        return

    try:
        if not segments:
            outcome = "REJECTED"
            reason  = "Booking contains no valid segments"
        else:
            result  = validate_and_reserve(segments, vehicle_id)
            outcome = result["outcome"]
            reason  = result["reason"]
            if not saga_id:
                try:
                    ds.insert_booking({
                        **{k: v for k, v in message.items() if k != "_id"},
                        "status":      outcome.lower(),
                        "approved_at": now_utc_iso() if outcome == "APPROVED" else None,
                        "rejected_at": now_utc_iso() if outcome == "REJECTED" else None,
                        "reject_reason": reason if outcome == "REJECTED" else None,
                        "region":      REGION,   # override booking-service's region with actual processing region
                    })
                except Exception as exc:
                    log.error("[%s] db.insert_booking failed booking_id=%s: %s", REGION, booking_id, exc)
                    producer.send("booking-outcomes", {
                        "booking_id": booking_id,
                        "driver_id":  driver_id,
                        "outcome":    "REJECTED",
                        "reason":     f"Database write failed: {exc}",
                        "region":     REGION,
                    })
                    producer.flush()
                    return
    except Exception as exc:
        outcome = "REJECTED"
        reason  = f"Internal validation error: {exc}"

    log.info("[%s] booking %s → %s (%s)", REGION, booking_id, outcome, reason)

    result = {
        "booking_id":    booking_id,
        "driver_id":     driver_id,
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
    while True:
        try:
            from kafka import KafkaConsumer, KafkaProducer

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
                log.info("[%s] received from Kafka: booking %s saga=%s",
                         REGION, payload.get("booking_id"), payload.get("saga_id"))
                _validate_and_publish(payload, producer)
        except Exception as exc:
            log.error("[%s] booking-consumer crashed: %s — restarting in 5s", REGION, exc)
            time.sleep(5)


def _release_consumer_loop():
    while True:
        try:
            from kafka import KafkaConsumer

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
            log.error("[%s] release-consumer crashed: %s — restarting in 5s", REGION, exc)
            time.sleep(5)


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
        log.info("[validation-service] Seeded %d Redis segment keys for region '%s'", seeded, REGION)
    except Exception as exc:
        log.warning("[validation-service] Redis seed warning: %s", exc)


def _redis_watchdog():
    """
    Polls Redis every 30s. If the probe key has disappeared (Redis was wiped),
    runs a full rebuild via rebuild_redis.main() — restores both capacity limits
    and current occupancy counters from MongoDB, then resets the probe key.

    Uses a fresh Redis connection each poll cycle so that connection pool state
    from a previous outage does not suppress detection of recovery.
    """
    PROBE_KEY    = f"watchdog:probe:{REGION}"
    POLL_SECONDS = 30
    _was_down    = False

    try:
        _redis.set(PROBE_KEY, "1")
    except Exception:
        pass

    while True:
        time.sleep(POLL_SECONDS)
        try:
            # Fresh connection each cycle — bypasses stale pool state after an outage.
            r = redis_lib.from_url(REDIS_URL, decode_responses=True,
                                   socket_connect_timeout=3, socket_timeout=3)
            try:
                exists = r.exists(PROBE_KEY)

                if not exists:
                    # Use a short-lived lock so only one instance runs the rebuild
                    # when both watchdogs fire in the same poll window.
                    LOCK_KEY = f"watchdog:rebuild_lock:{REGION}"
                    acquired = r.set(LOCK_KEY, "1", nx=True, ex=120)
                    recovering = _was_down
                    _was_down = False
                    if not acquired:
                        log.info(
                            "[validation-service] Rebuild already running for '%s' (lock held) — skipping", REGION
                        )
                    else:
                        log.warning(
                            "[validation-service] Redis %s for '%s' — probe key missing, "
                            "running full rebuild (capacity + occupancy)",
                            "back up" if recovering else "probe key gone", REGION
                        )
                        import rebuild_redis
                        rebuild_redis.main()
                        _redis.set(PROBE_KEY, "1")
                else:
                    if _was_down:
                        log.info("[validation-service] Redis recovered for '%s', probe key intact", REGION)
                    _was_down = False
            finally:
                r.close()
        except Exception as exc:
            if not _was_down:
                log.warning(
                    "[validation-service] Redis unreachable for '%s': %s — "
                    "bookings will be rejected until Redis recovers", REGION, exc
                )
                _was_down = True


@app.on_event("startup")
def start_consumers():
    global _booking_thread, _release_thread

    seed_thread = threading.Thread(target=_seed_redis_capacity, daemon=True)
    seed_thread.start()

    _booking_thread = threading.Thread(target=_booking_consumer_loop, daemon=True)
    _booking_thread.start()

    _release_thread = threading.Thread(target=_release_consumer_loop, daemon=True)
    _release_thread.start()

    watchdog_thread = threading.Thread(target=_redis_watchdog, daemon=True)
    watchdog_thread.start()
