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

mongo_client = MongoClient(MONGO_URI)
db           = mongo_client["traffic"]


def now_utc_iso() -> str:
    return datetime.now(UTC).isoformat()


# =============================================================================
# Booking (MongoDB)
# =============================================================================

@_mongo_retry
def get_vehicle_booking(vehicle_id: str):
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
