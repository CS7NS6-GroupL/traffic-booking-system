import os
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from datetime import datetime

app = FastAPI(title="Authority Service")

SERVICE_NAME = os.getenv("SERVICE_NAME", "authority-service")
REGION = os.getenv("REGION", "local")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
JWT_SECRET = os.getenv("JWT_SECRET", "changeme")


def _require_authority(authorization: str) -> dict:
    """Only AUTHORITY role tokens are accepted by this service."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    import sys
    sys.path.insert(0, "/app/shared")
    from auth import decode_token
    try:
        payload = decode_token(authorization.split(" ", 1)[1], JWT_SECRET)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")
    if payload.get("role") != "AUTHORITY":
        raise HTTPException(status_code=403, detail="AUTHORITY role required")
    return payload


def get_db():
    from pymongo import MongoClient
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
    return client["traffic"]


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME, "region": REGION}


@app.get("/authority/bookings/{vehicle_id}")
def verify_vehicle_bookings(vehicle_id: str, authorization: str = Header(...)):
    """
    Read-only. Verify all bookings for a given vehicle.
    Used by traffic authority to confirm a vehicle is authorised to be on a road.
    """
    _require_authority(authorization)
    try:
        db = get_db()
        bookings = list(db.bookings.find({"vehicle_id": vehicle_id}, {"_id": 0}))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {exc}")
    return {
        "vehicle_id": vehicle_id,
        "bookings": bookings,
        "count": len(bookings),
        "region": REGION,
    }


@app.get("/authority/audit")
def audit_log(limit: int = 50, authorization: str = Header(...)):
    """
    Read-only audit log of all bookings (most recent first).
    """
    _require_authority(authorization)
    try:
        db = get_db()
        records = list(
            db.bookings.find({}, {"_id": 0})
            .sort("created_at", -1)
            .limit(limit)
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {exc}")
    return {"audit_records": records, "count": len(records), "region": REGION}


class FlagRequest(BaseModel):
    reason: str = ""


@app.post("/authority/flag/{booking_id}")
def flag_booking(booking_id: str, body: FlagRequest = None, authorization: str = Header(...)):
    """
    Flag a booking for review (e.g., vehicle observed without valid booking).
    Writes to the flags collection — does not modify the booking itself.
    """
    _require_authority(authorization)
    reason = body.reason if body else ""
    try:
        db = get_db()
        booking = db.bookings.find_one({"booking_id": booking_id}, {"_id": 0})
        if not booking:
            raise HTTPException(status_code=404, detail="Booking not found")
        db.flags.insert_one({
            "booking_id": booking_id,
            "reason":     reason,
            "flagged_at": datetime.utcnow().isoformat(),
            "region":     REGION,
        })
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {exc}")
    return {"status": "flagged", "booking_id": booking_id, "reason": reason}
