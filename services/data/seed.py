import os
from dotenv import load_dotenv
from pymongo import MongoClient
from redis import Redis

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/traffic_system")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

mongo = MongoClient(MONGO_URI)
db = mongo["traffic_system"]

redis = Redis.from_url(REDIS_URL, decode_responses=True)

print("✅ Connected to MongoDB + Redis")

# =========================
# 🔹 Clear Mongo collections
# =========================
db["regions"].delete_many({})
db["region_graph"].delete_many({})
db["users"].delete_many({})
db["vehicles"].delete_many({})
db["bookings"].delete_many({})
db["audit_logs"].delete_many({})
db["flags"].delete_many({})

print("✅ Cleared Mongo collections")

# =========================
# 🔹 Clear Redis keys
# =========================
for key in redis.keys("capacity:*"):
    redis.delete(key)

for key in redis.keys("active:vehicle:*"):
    redis.delete(key)

for key in redis.keys("hold:*"):
    redis.delete(key)

print("✅ Cleared Redis capacity, active booking, and hold keys")

# =========================
# 🔹 Seed regions
# =========================
regions = [
    {"regionId": "ASIA", "defaultCapacity": 5},
    {"regionId": "TRANSIT", "defaultCapacity": 2},
    {"regionId": "EUROPE", "defaultCapacity": 4},
    {"regionId": "US", "defaultCapacity": 4}
]

db["regions"].insert_many(regions)
print("✅ Regions seeded")

# =========================
# 🔹 Seed region graph
# =========================
region_graph = [
    {"regionId": "ASIA", "neighbors": ["TRANSIT"]},
    {"regionId": "US", "neighbors": ["TRANSIT"]},
    {"regionId": "EUROPE", "neighbors": ["TRANSIT"]},
    {"regionId": "TRANSIT", "neighbors": ["ASIA", "EUROPE", "US"]}
]

db["region_graph"].insert_many(region_graph)
print("✅ Region graph seeded")

# =========================
# 🔹 Seed users
# =========================
users = [
    {"userId": "driver1", "role": "driver", "name": "Test Driver"},
    {"userId": "authority1", "role": "authority", "name": "Traffic Authority"}
]

db["users"].insert_many(users)
print("✅ Users seeded")

# =========================
# 🔹 Seed vehicles
# =========================
vehicles = [
    {"vehicleId": "12D12345", "ownerUserId": "driver1", "homeRegion": "EUROPE"},
    {"vehicleId": "99X99999", "ownerUserId": "driver1", "homeRegion": "US"}
]

db["vehicles"].insert_many(vehicles)
print("✅ Vehicles seeded")

# =========================
# 🔹 Seed Redis capacities
# =========================
slots = [
    "2026-04-07T09:00",
    "2026-04-07T10:00",
    "2026-04-07T11:00"
]

capacities = {
    "ASIA": 5,
    "TRANSIT": 2,
    "EUROPE": 4,
    "US": 4
}

for region, capacity in capacities.items():
    for slot in slots:
        key = f"capacity:{region}:{slot}"
        redis.set(key, capacity)
        print(f"Set {key} = {capacity}")

print("✅ Redis capacities seeded")
print("\n🎉 FULL SYSTEM SEEDED SUCCESSFULLY")