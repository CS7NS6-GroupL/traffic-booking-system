import os
import json
from datetime import datetime, UTC
from dotenv import load_dotenv
from pymongo import MongoClient
from redis import Redis

# =========================
# 🔹 Setup
# =========================
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/traffic_system")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

mongo_client = MongoClient(MONGO_URI)
db = mongo_client["traffic_system"]

bookings = db["bookings"]
regions_collection = db["regions"]
region_graph_collection = db["region_graph"]
audit_logs = db["audit_logs"]
flags = db["flags"]

redis_client = Redis.from_url(REDIS_URL, decode_responses=True)


def now_utc_iso() -> str:
    """Return a timezone-aware UTC ISO timestamp."""
    return datetime.now(UTC).isoformat()


# =========================
# 🔹 Key helpers
# =========================
def capacity_key(region: str, slot: str) -> str:
    """Return the Redis key for live remaining capacity."""
    return f"capacity:{region}:{slot}"


def hold_key(request_id: str, region: str) -> str:
    """Return the Redis key for a temporary regional hold."""
    return f"hold:{request_id}:{region}"


def active_booking_key(vehicle_id: str) -> str:
    """Return the Redis key for an active vehicle booking cache entry."""
    return f"active:vehicle:{vehicle_id}"


# =========================
# 🔹 Health / availability
# =========================
def redis_available() -> bool:
    """Return True if Redis is reachable."""
    try:
        return bool(redis_client.ping())
    except Exception:
        return False


# =========================
# 🔹 Region / config helpers
# =========================
def get_region(region_id: str):
    """Return a region document from MongoDB."""
    return regions_collection.find_one({"regionId": region_id})


def get_all_regions():
    """Return all region documents from MongoDB."""
    return list(regions_collection.find({}))


def get_region_graph(region_id: str):
    """Return the routing/graph document for a region."""
    return region_graph_collection.find_one({"regionId": region_id})


def get_neighbors(region_id: str):
    """Return neighbor regions for a given region."""
    region = get_region_graph(region_id)
    return region["neighbors"] if region and "neighbors" in region else []


# =========================
# 🔹 Capacity
# =========================
def get_capacity(region: str, slot: str):
    """Return remaining capacity for a region and slot from Redis."""
    value = redis_client.get(capacity_key(region, slot))
    return None if value is None else int(value)


def get_hold(request_id: str, region: str):
    """Return the hold payload for a request/region pair, if it exists."""
    value = redis_client.get(hold_key(request_id, region))
    return None if value is None else json.loads(value)


def reserve_capacity(request_id: str, vehicle_id: str, region: str, slot: str, ttl: int = 30):
    """
    Reserve one unit of capacity for a region/slot and create a temporary hold.
    Prevents duplicate active bookings for the same vehicle.
    """
    if redis_client.get(active_booking_key(vehicle_id)):
        return {"success": False, "error": "Vehicle already booked"}

    cap = redis_client.get(capacity_key(region, slot))
    if cap is None:
        return {"success": False, "error": "Capacity not set"}

    if int(cap) <= 0:
        return {"success": False, "error": "No capacity"}

    remaining = redis_client.decr(capacity_key(region, slot))

    if remaining < 0:
        redis_client.incr(capacity_key(region, slot))
        return {"success": False, "error": "No capacity"}

    hold = {
        "requestId": request_id,
        "vehicleId": vehicle_id,
        "region": region,
        "slot": slot,
        "createdAt": now_utc_iso()
    }

    redis_client.set(
        hold_key(request_id, region),
        json.dumps(hold),
        ex=ttl
    )

    return {"success": True, "remainingCapacity": remaining}


def release_capacity(request_id: str, region: str, slot: str):
    """Release a temporary hold and return one unit of capacity."""
    if not redis_client.get(hold_key(request_id, region)):
        return {"success": False, "error": "Hold not found"}

    redis_client.incr(capacity_key(region, slot))
    redis_client.delete(hold_key(request_id, region))

    return {"success": True}


def reserve_multi_region(request_id: str, vehicle_id: str, regions: list[str], slot: str, ttl: int = 30):
    """
    Attempt to reserve capacity across multiple regions.
    On failure, rolls back all successful earlier reservations.
    """
    reserved_regions = []

    for region in regions:
        result = reserve_capacity(request_id, vehicle_id, region, slot, ttl)
        if not result["success"]:
            for reserved_region in reserved_regions:
                release_capacity(request_id, reserved_region, slot)
            return {
                "success": False,
                "error": f"Failed to reserve in region {region}",
                "failedRegion": region
            }

        reserved_regions.append(region)

    return {"success": True, "regions": reserved_regions}


# =========================
# 🔹 Booking
# =========================
def create_booking(booking: dict):
    """Insert a confirmed booking into MongoDB."""
    result = bookings.insert_one(booking)
    return str(result.inserted_id)


def cache_active_booking(booking: dict):
    """Cache an active booking in Redis for fast lookup."""
    redis_client.set(
        active_booking_key(booking["vehicleId"]),
        json.dumps(booking, default=str)
    )


def clear_active_booking(vehicle_id: str):
    """Remove an active booking cache entry from Redis."""
    redis_client.delete(active_booking_key(vehicle_id))


def get_vehicle_booking(vehicle_id: str):
    """
    Get vehicle booking, preferring Redis cache first,
    then falling back to MongoDB.
    """
    cached = redis_client.get(active_booking_key(vehicle_id))
    if cached:
        return {
            "source": "redis",
            "booking": json.loads(cached)
        }

    booking = bookings.find_one({
        "vehicleId": vehicle_id,
        "status": "confirmed"
    })

    if not booking:
        return None

    booking["_id"] = str(booking["_id"])

    return {
        "source": "mongodb",
        "booking": booking
    }


def get_vehicle_booking_fallback(vehicle_id: str):
    """
    Try Redis-backed lookup first; if Redis is unavailable,
    fall back directly to MongoDB.
    """
    try:
        return get_vehicle_booking(vehicle_id)
    except Exception:
        booking = bookings.find_one({
            "vehicleId": vehicle_id,
            "status": "confirmed"
        })

        if not booking:
            return None

        booking["_id"] = str(booking["_id"])

        return {
            "source": "mongodb-fallback",
            "booking": booking
        }


def get_confirmed_bookings():
    """Return all confirmed bookings from MongoDB."""
    results = []
    for booking in bookings.find({"status": "confirmed"}):
        booking["_id"] = str(booking["_id"])
        results.append(booking)
    return results


def get_region_bookings(region: str):
    """Return all confirmed bookings for a region."""
    results = []
    for booking in bookings.find({"region": region, "status": "confirmed"}):
        booking["_id"] = str(booking["_id"])
        results.append(booking)
    return results


def cancel_booking(vehicle_id: str, region: str = None, slot: str = None):
    """
    Cancel the current confirmed booking for a vehicle,
    restore capacity, clear Redis cache, and publish an event.
    """
    booking = bookings.find_one({
        "vehicleId": vehicle_id,
        "status": "confirmed"
    })

    if not booking:
        return {"success": False, "error": "No booking"}

    bookings.update_one(
        {"_id": booking["_id"]},
        {
            "$set": {
                "status": "cancelled",
                "updatedAt": now_utc_iso()
            }
        }
    )

    if region and slot:
        redis_client.incr(capacity_key(region, slot))

    clear_active_booking(vehicle_id)

    redis_client.publish("booking-events", json.dumps({
        "type": "booking_cancelled",
        "vehicleId": vehicle_id,
        "bookingId": booking["bookingId"]
    }))

    return {
        "success": True,
        "bookingId": booking["bookingId"]
    }


# =========================
# 🔹 Audit / flags
# =========================
def log_audit_event(action: str, actor_id: str, actor_role: str, details: dict):
    """Write an audit event to MongoDB."""
    result = audit_logs.insert_one({
        "timestamp": now_utc_iso(),
        "action": action,
        "actorId": actor_id,
        "actorRole": actor_role,
        "details": details
    })
    return str(result.inserted_id)


def flag_vehicle(vehicle_id: str, flagged_by: str, reason: str):
    """Record a suspicious or invalid vehicle/booking flag."""
    result = flags.insert_one({
        "vehicleId": vehicle_id,
        "flaggedBy": flagged_by,
        "reason": reason,
        "createdAt": now_utc_iso()
    })
    return str(result.inserted_id)


# =========================
# 🔹 Events
# =========================
def publish_booking_event(event: dict):
    """Publish a booking-related event on Redis Pub/Sub."""
    return redis_client.publish("booking-events", json.dumps(event, default=str))


# =========================
# 🔹 Rollback
# =========================
def rollback_reservation(request_id: str, region: str, slot: str, vehicle_id: str = None):
    """Best-effort rollback of Redis reservation state."""
    try:
        redis_client.incr(capacity_key(region, slot))
        redis_client.delete(hold_key(request_id, region))

        if vehicle_id:
            clear_active_booking(vehicle_id)
    except Exception as e:
        print("Rollback failed:", e)