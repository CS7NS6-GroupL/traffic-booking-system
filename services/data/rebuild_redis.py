import json
import data_service

print("✅ Connected to MongoDB + Redis")

# -------------------------
# 1. Clear old Redis state
# -------------------------
for key in data_service.redis_client.keys("capacity:*"):
    data_service.redis_client.delete(key)

for key in data_service.redis_client.keys("active:vehicle:*"):
    data_service.redis_client.delete(key)

print("✅ Cleared old Redis capacity and active booking keys")

# -------------------------
# 2. Rebuild capacity from region defaults
# -------------------------
slots = [
    "2026-04-07T09:00",
    "2026-04-07T10:00",
    "2026-04-07T11:00"
]

regions = data_service.get_all_regions()

for region in regions:
    region_id = region["regionId"]
    default_capacity = region.get("defaultCapacity", 4)

    for slot in slots:
        key = data_service.capacity_key(region_id, slot)
        data_service.redis_client.set(key, default_capacity)
        print(f"Set {key} = {default_capacity}")

print("✅ Rebuilt base capacities")

# -------------------------
# 3. Rebuild active bookings from MongoDB
# -------------------------
confirmed_bookings = data_service.get_confirmed_bookings()
print(f"Found {len(confirmed_bookings)} confirmed bookings")

for booking in confirmed_bookings:
    vehicle_id = booking["vehicleId"]
    region = booking.get("region")
    slot = booking.get("startTime")

    # restore active vehicle cache
    active_key = data_service.active_booking_key(vehicle_id)
    data_service.redis_client.set(active_key, json.dumps(booking, default=str))
    print(f"Restored {active_key}")

    # reduce capacity again for its region/slot
    if region and slot:
        cap_key = data_service.capacity_key(region, slot)
        data_service.redis_client.decr(cap_key)
        print(f"Decremented {cap_key}")

print("✅ Rebuilt active bookings and adjusted capacities")

print("\n🎉 REDIS REBUILD COMPLETE")