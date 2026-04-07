import os
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/traffic_system")

mongo = MongoClient(MONGO_URI)
db = mongo["traffic_system"]

try:
    db["regions"].create_index("regionId", unique=True)
    db["region_graph"].create_index("regionId", unique=True)
    db["users"].create_index("userId", unique=True)
    db["vehicles"].create_index("vehicleId", unique=True)
    db["bookings"].create_index("bookingId", unique=True)
    db["bookings"].create_index("requestId", unique=True)

    print("✅ Indexes created successfully")
except Exception as e:
    print("❌ Failed to create indexes")
    print(e)