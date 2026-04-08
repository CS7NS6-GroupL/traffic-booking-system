"""
Data Service — business logic layer.
All Redis and MongoDB access lives here.
Imported directly by main.py (the HTTP wrapper).
"""
import os
import json
from datetime import datetime, UTC
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
from redis import Redis
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)
import logging

log = logging.getLogger(__name__)

# Retry on any pymongo connection/network error.
# 3 attempts: immediate, ~1s, ~2s — total max wait ~3s before giving up.
_mongo_retry = retry(
    retry=retry_if_exception_type((ConnectionFailure, ServerSelectionTimeoutError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
LOCK_TTL  = 10  # seconds

mongo_client = MongoClient(MONGO_URI)
db           = mongo_client["traffic"]
redis_client = Redis.from_url(REDIS_URL, decode_responses=True)


def now_utc_iso() -> str:
    return datetime.now(UTC).isoformat()


# =============================================================================
# Redis health
# =============================================================================

def redis_available() -> bool:
    try:
        return bool(redis_client.ping())
    except Exception:
        return False


# =============================================================================
# Segment keys
# Key structure (shared across all services via this module):
#   lock:segment:{from}:{to}      — distributed mutex, TTL=10s
#   capacity:segment:{from}:{to}  — max vehicles allowed (default 100)
#   current:segment:{from}:{to}   — current vehicle count
# =============================================================================

def _lock_key(f: str, t: str) -> str:
    return f"lock:segment:{f}:{t}"

def _capacity_key(f: str, t: str) -> str:
    return f"capacity:segment:{f}:{t}"

def _current_key(f: str, t: str) -> str:
    return f"current:segment:{f}:{t}"


def acquire_segment_lock(from_node: str, to_node: str, ttl: int = LOCK_TTL) -> bool:
    return bool(redis_client.set(_lock_key(from_node, to_node), "1", nx=True, ex=ttl))

def release_segment_lock(from_node: str, to_node: str):
    redis_client.delete(_lock_key(from_node, to_node))

def get_segment_capacity(from_node: str, to_node: str) -> int:
    val = redis_client.get(_capacity_key(from_node, to_node))
    return int(val) if val is not None else 100

def get_segment_current(from_node: str, to_node: str) -> int:
    val = redis_client.get(_current_key(from_node, to_node))
    return int(val) if val is not None else 0

def increment_segment_current(from_node: str, to_node: str):
    redis_client.incr(_current_key(from_node, to_node))

def decrement_segment_current(from_node: str, to_node: str):
    if get_segment_current(from_node, to_node) > 0:
        redis_client.decr(_current_key(from_node, to_node))


# =============================================================================
# Batched segment validation
# Called once per booking — acquires locks, checks capacity, checks duplicate,
# increments counters, releases locks. All in one call to avoid chatty HTTP.
# =============================================================================

def validate_and_reserve(segments: list[dict], vehicle_id: str) -> dict:
    """
    Full validation pass for a booking:
      1. Acquire all segment locks (sorted to prevent deadlock).
      2. Check capacity on every segment.
      3. Check for a duplicate active booking on the same vehicle.
      4. Increment counters on all segments.
      5. Release all locks.
    Returns {"outcome": "APPROVED"|"REJECTED", "reason": str}.
    """
    sorted_segs = sorted(segments, key=lambda s: _lock_key(s["from"], s["to"]))
    acquired: list[tuple[str, str]] = []

    try:
        for seg in sorted_segs:
            if not acquire_segment_lock(seg["from"], seg["to"]):
                return {
                    "outcome": "REJECTED",
                    "reason":  f"Segment lock contention on {seg['from']}->{seg['to']} — please retry",
                }
            acquired.append((seg["from"], seg["to"]))

        for seg in sorted_segs:
            cap = get_segment_capacity(seg["from"], seg["to"])
            cur = get_segment_current(seg["from"], seg["to"])
            if cur >= cap:
                return {
                    "outcome": "REJECTED",
                    "reason":  f"Road segment {seg['from']}->{seg['to']} at full capacity ({cur}/{cap})",
                }

        if get_vehicle_booking_fallback(vehicle_id):
            return {
                "outcome": "REJECTED",
                "reason":  f"Vehicle {vehicle_id} already has an active booking",
            }

        for seg in sorted_segs:
            increment_segment_current(seg["from"], seg["to"])

        return {"outcome": "APPROVED", "reason": "Booking approved"}

    finally:
        for fn, tn in reversed(acquired):
            try:
                release_segment_lock(fn, tn)
            except Exception:
                pass


def release_segments(segments: list[dict]):
    """Decrement counters for a list of segments (cancellation / saga rollback)."""
    for seg in segments:
        decrement_segment_current(seg["from"], seg["to"])


# =============================================================================
# Booking (MongoDB)
# =============================================================================

@_mongo_retry
def get_vehicle_booking(vehicle_id: str):
    cached = redis_client.get(f"active:vehicle:{vehicle_id}")
    if cached:
        return {"source": "redis", "booking": json.loads(cached)}
    booking = db.bookings.find_one(
        {"vehicle_id": vehicle_id, "status": {"$in": ["approved", "pending"]}},
        {"_id": 0},
    )
    return {"source": "mongodb", "booking": booking} if booking else None


def get_vehicle_booking_fallback(vehicle_id: str):
    try:
        return get_vehicle_booking(vehicle_id)
    except Exception:
        # MongoDB exhausted retries — return None rather than crashing validation
        log.error("MongoDB unavailable for vehicle booking lookup — treating as no active booking")
        return None


@_mongo_retry
def insert_booking(booking_doc: dict):
    db.bookings.insert_one({k: v for k, v in booking_doc.items() if k != "_id"})


@_mongo_retry
def get_booking_by_id(booking_id: str) -> dict | None:
    return db.bookings.find_one({"booking_id": booking_id}, {"_id": 0})


@_mongo_retry
def get_bookings_by_vehicle(vehicle_id: str) -> list:
    return list(db.bookings.find({"vehicle_id": vehicle_id}, {"_id": 0}))


@_mongo_retry
def get_bookings_by_driver(driver_id: str) -> list:
    return list(db.bookings.find({"driver_id": driver_id}, {"_id": 0}))


@_mongo_retry
def get_all_bookings(limit: int = 50) -> list:
    return list(db.bookings.find({}, {"_id": 0}).sort("created_at", -1).limit(limit))


@_mongo_retry
def cancel_booking_record(booking_id: str, cancelled_at: str):
    db.bookings.update_one(
        {"booking_id": booking_id},
        {"$set": {"status": "cancelled", "cancelled_at": cancelled_at}},
    )


# =============================================================================
# Saga (MongoDB)
# =============================================================================

@_mongo_retry
def create_saga(saga_doc: dict):
    db.sagas.insert_one({**saga_doc, "_id": saga_doc["saga_id"]})


@_mongo_retry
def get_saga(saga_id: str) -> dict | None:
    return db.sagas.find_one({"saga_id": saga_id})


@_mongo_retry
def update_saga_regional_outcome(saga_id: str, region: str, outcome: str, reason: str) -> dict | None:
    db.sagas.update_one(
        {"saga_id": saga_id},
        {"$set": {f"regional_outcomes.{region}": {"outcome": outcome, "reason": reason}}},
    )
    return get_saga(saga_id)


@_mongo_retry
def update_saga_status(saga_id: str, status: str, extra: dict = None):
    update = {"status": status}
    if extra:
        update.update(extra)
    db.sagas.update_one({"saga_id": saga_id}, {"$set": update})


# =============================================================================
# Flags and audit (MongoDB)
# =============================================================================

@_mongo_retry
def insert_flag(booking_id: str, reason: str, region: str):
    db.flags.insert_one({
        "booking_id": booking_id,
        "reason":     reason,
        "flagged_at": now_utc_iso(),
        "region":     region,
    })


@_mongo_retry
def log_audit_event(action: str, actor_id: str, actor_role: str, details: dict):
    db.audit_logs.insert_one({
        "timestamp": now_utc_iso(),
        "action":    action,
        "actorId":   actor_id,
        "actorRole": actor_role,
        "details":   details,
    })
