import data_service
from datetime import datetime

def run_test_booking():
    request_id = f"req-{int(datetime.utcnow().timestamp())}"
    vehicle_id = "12D12345"
    region = "EUROPE"
    slot = "2026-04-07T09:00"

    print("Checking capacity...")
    cap = data_service.get_capacity(region, slot)
    print("Capacity:", cap)

    print("Reserving capacity...")
    res = data_service.reserve_capacity(request_id, vehicle_id, region, slot)

    if not res["success"]:
        print("Reservation failed:", res)
        return

    print("Reserved:", res)

    booking = {
        "bookingId": f"booking-{request_id}",
        "requestId": request_id,
        "vehicleId": vehicle_id,
        "region": region,
        "startTime": slot,
        "endTime": "2026-04-07T10:00:00Z",
        "status": "confirmed",
        "createdAt": datetime.utcnow().isoformat()
    }

    print("Writing booking to Mongo...")
    inserted_id = data_service.create_booking(booking)
    booking["_id"] = inserted_id

    print("Caching active booking...")
    data_service.cache_active_booking(booking)

    print("Publishing event...")
    data_service.publish_booking_event({
        "type": "booking_confirmed",
        "bookingId": booking["bookingId"],
        "vehicleId": vehicle_id
    })

    print("Booking complete!")

if __name__ == "__main__":
    run_test_booking()