import os
import json
from datetime import datetime
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

redis_client = Redis.from_url(REDIS_URL, decode_responses=True)

# =========================
# 🔹 Key helpers
# =========================
def capacity_key(region: str, slot: str) -> str:
    return f"capacity:{region}:{slot}"

def hold_key(request_id: str, region: str) -> str:
    return f"hold:{request_id}:{region}"

def active_booking_key(vehicle_id: str) -> str:
    return f"active:vehicle:{vehicle_id}"

# =========================
# 🔹 Capacity
# =========================
def get_capacity(region: str, slot: str):
    value = redis_client.get(capacity_key(region, slot))
    return None if value is None else int(value)

def reserve_capacity(request_id: str, vehicle_id: str, region: str, slot: str, ttl: int = 30):
    # prevent double booking
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
        "createdAt": datetime.utcnow().isoformat()
    }

    redis_client.set(
        hold_key(request_id, region),
        json.dumps(hold),
        ex=ttl
    )

    return {"success": True, "remainingCapacity": remaining}

def release_capacity(request_id: str, region: str, slot: str):
    if not redis_client.get(hold_key(request_id, region)):
        return {"success": False, "error": "Hold not found"}

    redis_client.incr(capacity_key(region, slot))
    redis_client.delete(hold_key(request_id, region))

    return {"success": True}

# =========================
# 🔹 Booking
# =========================
def create_booking(booking: dict):
    result = bookings.insert_one(booking)
    return str(result.inserted_id)

def cache_active_booking(booking: dict):
    redis_client.set(
        active_booking_key(booking["vehicleId"]),
        json.dumps(booking, default=str)
    )

def get_vehicle_booking(vehicle_id: str):
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

def cancel_booking(vehicle_id: str, region: str = None, slot: str = None):
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
                "updatedAt": datetime.utcnow().isoformat()
            }
        }
    )

    if region and slot:
        redis_client.incr(capacity_key(region, slot))

    redis_client.delete(active_booking_key(vehicle_id))

    redis_client.publish("booking-events", json.dumps({
        "type": "booking_cancelled",
        "vehicleId": vehicle_id
    }))

    return {"success": True}

# =========================
# 🔹 Events
# =========================
def publish_booking_event(event: dict):
    return redis_client.publish("booking-events", json.dumps(event, default=str))

# =========================
# 🔹 Rollback (important)
# =========================
def rollback_reservation(request_id: str, region: str, slot: str, vehicle_id: str = None):
    try:
        redis_client.incr(capacity_key(region, slot))
        redis_client.delete(hold_key(request_id, region))

        if vehicle_id:
            redis_client.delete(active_booking_key(vehicle_id))
    except Exception as e:
        print("Rollback failed:", e)