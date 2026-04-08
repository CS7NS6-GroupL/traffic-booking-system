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
"""

import json
import os
import sys
import threading

sys.path.insert(0, "/app/data")

from fastapi import FastAPI

app = FastAPI(title="Validation Service")

SERVICE_NAME = os.getenv("SERVICE_NAME", "validation-service")
REGION = os.getenv("REGION", "local")
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
from datetime import datetime, UTC

def now_utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def _message_segments(message: dict) -> list[dict]:
    """
    Normalize the payload to a segment list.
    Falls back to the legacy top-level origin/destination shape.
    """
    segments = message.get("segments") or []
    if segments:
        return [
            {"from": seg.get("from"), "to": seg.get("to")}
            for seg in segments
            if seg.get("from") and seg.get("to")
        ]

    origin = message.get("origin")
    destination = message.get("destination")
    if origin and destination:
        return [{"from": origin, "to": destination}]
    return []



@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME, "region": REGION}


@app.get("/validation/status")
def status():
    return {"consumer_group": f"validation-group-{REGION}", "topic": "booking-requests", "region": REGION}


def _validate_and_publish(message: dict, producer):
    """Validate one booking (or sub-booking) and publish the outcome."""
    import data_service as ds

    booking_id = message.get("booking_id", "unknown")
    saga_id    = message.get("saga_id")
    vehicle_id = message.get("vehicle_id")
    segments   = _message_segments(message)
    origin      = segments[0]["from"] if segments else message.get("origin")
    destination = segments[-1]["to"]  if segments else message.get("destination")

    try:
        if not segments:
            outcome = "REJECTED"
            reason  = "Booking contains no valid segments"
        else:
            result  = ds.validate_and_reserve(segments, vehicle_id)
            outcome = result["outcome"]
            reason  = result["reason"]
            if outcome == "APPROVED" and not saga_id:
                ds.insert_booking({
                    **{k: v for k, v in message.items() if k != "_id"},
                    "status":      "approved",
                    "approved_at": now_utc_iso(),
                })
    except Exception as exc:
        outcome = "REJECTED"
        reason  = f"Internal validation error: {exc}"

    result = {
        "booking_id": booking_id,
        "driver_id": message.get("driver_id"),
        "vehicle_id": vehicle_id,
        "origin": origin,
        "destination": destination,
        "region": REGION,
        "target_region": message.get("target_region", REGION),
        "outcome": outcome,
        "reason": reason,
    }
    if saga_id:
        result["saga_id"] = saga_id

    # Mark sub-booking outcomes so notification-service can skip intermediate results.
    # The saga coordinator publishes the definitive final outcome separately.
    if saga_id:
        result["is_sub_booking"] = True
    producer.send("booking-outcomes", result)


def _release_capacity(message: dict):
    """Decrement counters when a booking is cancelled or saga is aborted."""
    target_region = message.get("target_region", message.get("region", REGION))
    if target_region != REGION:
        return
    import data_service as ds
    ds.release_segments(_message_segments(message))


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
            target = payload.get("target_region")
            if target and target != REGION:
                continue
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


def _seed_redis_capacity():
    """
    Seed Redis capacity counters from MongoDB osm_edges for this region.
    Uses SET NX so a restart does not overwrite live counters or tuned limits.
    """
    import redis as redis_lib
    from pymongo import MongoClient

    try:
        r = redis_lib.from_url(REDIS_URL)
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        db = client["traffic"]
        edges = db.osm_edges.find(
            {"region": REGION},
            {"from": 1, "to": 1, "road_type": 1},
        )

        seeded = 0
        for edge in edges:
            origin = edge.get("from")
            destination = edge.get("to")
            if not origin or not destination:
                continue

            road_type = edge.get("road_type", "")
            capacity = ROAD_TYPE_CAPACITY.get(road_type, 100)
            cap_key = f"capacity:segment:{origin}:{destination}"
            cur_key = f"current:segment:{origin}:{destination}"
            r.set(cap_key, capacity, nx=True)
            r.set(cur_key, 0, nx=True)
            seeded += 1

        client.close()
        print(f"[validation-service] Seeded {seeded} Redis segment keys for region '{REGION}'")
    except Exception as exc:
        print(f"[validation-service] Redis seed warning: {exc}")


@app.on_event("startup")
def start_consumers():
    _seed_redis_capacity()
    for fn in (_booking_consumer_loop, _release_consumer_loop):
        t = threading.Thread(target=fn, daemon=True)
        t.start()
