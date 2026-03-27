"""
Validation Service
==================
Consumes `booking-requests` from Kafka.

For EVERY message it:
  1. Checks `target_region` — skips messages not intended for this region.
     (In a multi-Kafka deployment each cluster has its own broker; here we use
     a single broker with region filtering to simulate that topology.)
  2. Acquires a Redis distributed lock on the road segment.
  3. Checks Redis occupancy counter vs. segment capacity.
  4. Checks MongoDB for a duplicate active booking on the same vehicle.
  5. Publishes APPROVED or REJECTED to `booking-outcomes`, including `saga_id`
     if the message is a cross-region sub-booking so the saga coordinator in
     journey-management can advance the saga state machine.

Capacity rollback
-----------------
Also consumes `capacity-releases` to decrement Redis counters when a booking
is cancelled or a saga is aborted — this is the compensating transaction.
"""

import os
import json
import threading
from fastapi import FastAPI

app = FastAPI(title="Validation Service")

SERVICE_NAME = os.getenv("SERVICE_NAME", "validation-service")
REGION = os.getenv("REGION", "local")
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
LOCK_TTL = 10  # seconds


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME, "region": REGION}


@app.get("/validation/status")
def status():
    return {"consumer_group": "validation-group", "topic": "booking-requests", "region": REGION}


# ── Core validation logic ─────────────────────────────────────────────────────

def _validate_and_publish(message: dict, producer):
    """Validate one booking (or sub-booking) and publish the outcome."""
    import redis as redis_lib
    from pymongo import MongoClient

    booking_id = message.get("booking_id", "unknown")
    saga_id = message.get("saga_id")          # present for cross-region sub-bookings
    vehicle_id = message.get("vehicle_id")
    origin = message.get("origin")
    destination = message.get("destination")
    segments = message.get("segments", [])

    segment_key = f"segment:{origin}:{destination}"
    lock_key = f"lock:{segment_key}"
    capacity_key = f"capacity:{segment_key}"
    current_key = f"current:{segment_key}"

    r = redis_lib.from_url(REDIS_URL)

    outcome = "REJECTED"
    reason = "Unknown error"

    try:
        acquired = r.set(lock_key, "1", nx=True, ex=LOCK_TTL)
        if not acquired:
            reason = "Segment lock contention — please retry"
        else:
            try:
                capacity = int(r.get(capacity_key) or 100)
                current = int(r.get(current_key) or 0)

                if current >= capacity:
                    reason = f"Road segment {segment_key} at full capacity ({current}/{capacity})"
                else:
                    # Duplicate vehicle check
                    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
                    db = client["traffic"]
                    existing = db.bookings.find_one({
                        "vehicle_id": vehicle_id,
                        "status": {"$in": ["approved", "pending"]},
                    })
                    if existing:
                        reason = f"Vehicle {vehicle_id} already has an active booking"
                    else:
                        r.incr(current_key)
                        # Persist immediately for single-region bookings;
                        # for sagas, the saga coordinator writes the final booking.
                        if not saga_id:
                            db.bookings.insert_one({
                                **{k: v for k, v in message.items() if k != "_id"},
                                "status": "approved",
                                "approved_at": __import__("datetime").datetime.utcnow().isoformat(),
                            })
                        outcome = "APPROVED"
                        reason = "Booking approved"
            finally:
                r.delete(lock_key)

    except Exception as exc:
        reason = f"Internal validation error: {exc}"

    result = {
        "booking_id": booking_id,
        "driver_id":  message.get("driver_id"),
        "vehicle_id": vehicle_id,
        "origin":     origin,
        "destination": destination,
        "region":     REGION,                  # which cluster validated this
        "target_region": message.get("target_region", REGION),
        "outcome":    outcome,
        "reason":     reason,
    }
    if saga_id:
        result["saga_id"] = saga_id

    producer.send("booking-outcomes", result)


# ── Capacity release (compensating transaction) ───────────────────────────────

def _release_capacity(message: dict):
    """Decrement Redis counter when a booking is cancelled or saga is aborted."""
    target_region = message.get("target_region", message.get("region", REGION))
    if target_region != REGION:
        return  # Not our segments

    import redis as redis_lib
    r = redis_lib.from_url(REDIS_URL)
    origin = message.get("origin")
    destination = message.get("destination")
    if origin and destination:
        current_key = f"current:segment:{origin}:{destination}"
        current = int(r.get(current_key) or 0)
        if current > 0:
            r.decr(current_key)


# ── Kafka consumer threads ────────────────────────────────────────────────────

def _booking_consumer_loop():
    from kafka import KafkaConsumer, KafkaProducer
    try:
        consumer = KafkaConsumer(
            "booking-requests",
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            value_deserializer=lambda m: json.loads(m.decode("utf-8")),
            group_id="validation-group",
            auto_offset_reset="earliest",
        )
        producer = KafkaProducer(
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        )
        for msg in consumer:
            payload = msg.value
            target = payload.get("target_region")
            # Filter: only process messages targeted at this region.
            # Messages without target_region are legacy single-region bookings.
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
            group_id="capacity-release-group",
            auto_offset_reset="earliest",
        )
        for msg in consumer:
            _release_capacity(msg.value)
    except Exception as exc:
        print(f"[validation-service] release consumer error: {exc}")


def _seed_redis_capacity():
    """
    Seed Redis capacity counters from the known segment data on startup.
    Uses SET NX (only sets if key does not already exist) so a restart does
    not reset counters that already have live traffic tracked against them.
    The capacity values mirror the route-service seed data.
    """
    import redis as redis_lib
    SEGMENT_CAPACITIES = {
        # EU
        "eu-AB":      ("A",        "B",        100),
        "eu-BC":      ("B",        "C",        80),
        "eu-AC":      ("A",        "C",        60),
        "eu-CD":      ("C",        "D",        120),
        "eu-BD":      ("B",        "D",        90),
        "eu-gateway": ("D",        "EU_US_GW", 200),
        # US
        "us-gwX":     ("EU_US_GW", "X",        200),
        "us-XY":      ("X",        "Y",        150),
        "us-YZ":      ("Y",        "Z",        130),
        "us-XZ":      ("X",        "Z",        100),
        "us-gateway": ("Z",        "US_ASIA_GW",200),
        # Asia
        "asia-gwP":   ("US_ASIA_GW","P",       200),
        "asia-PQ":    ("P",        "Q",        150),
        "asia-QR":    ("Q",        "R",        120),
        "asia-PR":    ("P",        "R",        90),
        # Local/test
        "local-AB":   ("A",        "B",        100),
        "local-BC":   ("B",        "C",        80),
        "local-CD":   ("C",        "D",        120),
    }
    try:
        r = redis_lib.from_url(REDIS_URL)
        for seg_id, (origin, destination, capacity) in SEGMENT_CAPACITIES.items():
            cap_key = f"capacity:segment:{origin}:{destination}"
            cur_key = f"current:segment:{origin}:{destination}"
            r.set(cap_key, capacity, nx=True)   # NX: skip if already set
            r.set(cur_key, 0, nx=True)          # NX: don't reset live counter
        print(f"[validation-service] Redis capacity keys seeded for {REGION}")
    except Exception as exc:
        print(f"[validation-service] Redis seed warning: {exc}")


@app.on_event("startup")
def start_consumers():
    _seed_redis_capacity()
    for fn in (_booking_consumer_loop, _release_consumer_loop):
        t = threading.Thread(target=fn, daemon=True)
        t.start()
