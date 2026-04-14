"""
Data Service — business logic layer.
All Redis and MongoDB access lives here.
Imported directly by main.py (the HTTP wrapper).

Booking sharding
----------------
Booking documents are hash-sharded across up to two MongoDB instances:
  shard 0 — MONGO_URI          (the existing regional MongoDB)
  shard 1 — MONGO_SHARD_1_URI  (second MongoDB, same region)

Shard routing: MD5(booking_id) % TOTAL_BOOKING_SHARDS
MD5 is used instead of Python's built-in hash() because hash() is
randomised per-process (PYTHONHASHSEED) and would route differently
across the two data-service replicas.

All non-booking collections (sagas, flags, audit_logs, osm_edges, …)
remain on shard 0 (db).  If MONGO_SHARD_1_URI is not set, the service
runs in single-shard mode and all booking writes go to db (no change
from the pre-sharding behaviour).
"""
import hashlib
import os
import json
from datetime import datetime, UTC
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, DuplicateKeyError, ServerSelectionTimeoutError
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

MONGO_URI         = os.getenv("MONGO_URI",         "mongodb://localhost:27017")
MONGO_SHARD_1_URI = os.getenv("MONGO_SHARD_1_URI", "")

mongo_client = MongoClient(
    MONGO_URI,
    maxPoolSize=50,
    minPoolSize=5,
    serverSelectionTimeoutMS=5000,
)
db = mongo_client["traffic"]

# --- Shard 1 (optional) ---------------------------------------------------
if MONGO_SHARD_1_URI:
    _mongo_shard1_client = MongoClient(
        MONGO_SHARD_1_URI,
        maxPoolSize=50,
        minPoolSize=5,
        serverSelectionTimeoutMS=5000,
    )
    _shard1_db = _mongo_shard1_client["traffic"]
    _booking_shards   = [db, _shard1_db]
    _booking_clients  = [mongo_client, _mongo_shard1_client]
    log.info("Booking sharding enabled: shard 0=%s  shard 1=%s", MONGO_URI, MONGO_SHARD_1_URI)
else:
    _shard1_db        = None
    _booking_shards   = [db]
    _booking_clients  = [mongo_client]

TOTAL_BOOKING_SHARDS = len(_booking_shards)


def _shard_db(booking_id: str):
    """
    Return the MongoDB database object for this booking_id.
    Uses MD5 for a stable, process-independent hash.
    """
    if TOTAL_BOOKING_SHARDS == 1:
        return db
    idx = int(hashlib.md5(booking_id.encode()).hexdigest(), 16) % TOTAL_BOOKING_SHARDS
    return _booking_shards[idx]


def ping_all_shards() -> dict:
    """Health check: ping every booking shard. Returns {shard_N: "ok"|"error: …"}."""
    results = {}
    for i, client in enumerate(_booking_clients):
        try:
            client.admin.command("ping")
            results[f"shard_{i}"] = "ok"
        except Exception as exc:
            results[f"shard_{i}"] = f"error: {exc}"
    return results


def now_utc_iso() -> str:
    return datetime.now(UTC).isoformat()


# =============================================================================
# Booking (MongoDB)
# =============================================================================

@_mongo_retry
def get_vehicle_booking(vehicle_id: str):
    """Fan-out to all shards — vehicle_id cannot be used as a shard key."""
    for shard in _booking_shards:
        booking = shard.bookings.find_one(
            {"vehicle_id": vehicle_id, "status": {"$in": ["approved", "pending"]}},
            {"_id": 0},
        )
        if booking:
            return {"source": "mongodb", "booking": booking}
    return None


def get_vehicle_booking_fallback(vehicle_id: str):
    try:
        return get_vehicle_booking(vehicle_id)
    except Exception:
        # MongoDB exhausted retries — return None rather than crashing validation
        log.error("MongoDB unavailable for vehicle booking lookup — treating as no active booking")
        return None


@_mongo_retry
def insert_booking(booking_doc: dict):
    booking_id = booking_doc.get("booking_id", "")
    _shard_db(booking_id).bookings.insert_one(
        {k: v for k, v in booking_doc.items() if k != "_id"}
    )


@_mongo_retry
def get_booking_by_id(booking_id: str) -> dict | None:
    return _shard_db(booking_id).bookings.find_one({"booking_id": booking_id}, {"_id": 0})


@_mongo_retry
def get_bookings_by_vehicle(vehicle_id: str) -> list:
    """Fan-out to all shards."""
    results = []
    for shard in _booking_shards:
        results.extend(shard.bookings.find({"vehicle_id": vehicle_id}, {"_id": 0}))
    return results


@_mongo_retry
def get_bookings_by_driver(driver_id: str) -> list:
    """Fan-out to all shards."""
    results = []
    for shard in _booking_shards:
        results.extend(shard.bookings.find({"driver_id": driver_id}, {"_id": 0}))
    return results


@_mongo_retry
def get_all_bookings(limit: int = 50) -> list:
    """Fan-out to all shards, merge by created_at descending."""
    results = []
    for shard in _booking_shards:
        results.extend(shard.bookings.find({}, {"_id": 0}).sort("created_at", -1).limit(limit))
    results.sort(key=lambda b: b.get("created_at", ""), reverse=True)
    return results[:limit]


@_mongo_retry
def cancel_booking_record(booking_id: str, cancelled_at: str):
    _shard_db(booking_id).bookings.update_one(
        {"booking_id": booking_id},
        {"$set": {"status": "cancelled", "cancelled_at": cancelled_at}},
    )


@_mongo_retry
def flag_booking_record(booking_id: str, reason: str = ""):
    _shard_db(booking_id).bookings.update_one(
        {"booking_id": booking_id},
        {"$set": {"status": "flagged", "flagged_reason": reason, "flagged_at": now_utc_iso()}},
    )


# =============================================================================
# Saga (MongoDB)
# =============================================================================

@_mongo_retry
def create_saga(saga_doc: dict):
    try:
        db.sagas.insert_one({**saga_doc, "_id": saga_doc["saga_id"]})
    except DuplicateKeyError:
        log.warning("saga.create duplicate saga_id=%s — returning existing", saga_doc["saga_id"])
        return db.sagas.find_one({"saga_id": saga_doc["saga_id"]})
    return saga_doc


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
def get_sagas_by_status(statuses: list[str]) -> list:
    return list(db.sagas.find({"status": {"$in": statuses}}, {"_id": 0}))


@_mongo_retry
def update_saga_status(saga_id: str, new_status: str, expected_status: str = None):
    filter_ = {"saga_id": saga_id}
    if expected_status:
        filter_["status"] = expected_status
    result = db.sagas.find_one_and_update(
        filter_,
        {"$set": {"status": new_status, "updated_at": now_utc_iso()}},
        return_document=True,
    )
    if not result and expected_status:
        raise ValueError(f"saga {saga_id} not in expected state {expected_status}")
    return result


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
