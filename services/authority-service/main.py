import os
import sys
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

sys.path.insert(0, "/app/data")
import data_service as ds

app = FastAPI(title="Authority Service")

SERVICE_NAME = os.getenv("SERVICE_NAME", "authority-service")
REGION = os.getenv("REGION", "local")
JWT_SECRET = os.getenv("JWT_SECRET", "changeme")


def _require_authority(authorization: str) -> dict:
    """Only AUTHORITY role tokens are accepted by this service."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    sys.path.insert(0, "/app/shared")
    from auth import decode_token
    try:
        payload = decode_token(authorization.split(" ", 1)[1], JWT_SECRET)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")
    if payload.get("role") != "AUTHORITY":
        raise HTTPException(status_code=403, detail="AUTHORITY role required")
    return payload


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
        bookings = ds.get_bookings_by_vehicle(vehicle_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}")
    return {
        "vehicle_id": vehicle_id,
        "bookings":   bookings,
        "count":      len(bookings),
        "region":     REGION,
    }


@app.get("/authority/audit")
def audit_log(limit: int = 50, authorization: str = Header(...)):
    """Read-only audit log of all bookings (most recent first)."""
    _require_authority(authorization)
    try:
        records = ds.get_all_bookings(limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}")
    return {"audit_records": records, "count": len(records), "region": REGION}


class FlagRequest(BaseModel):
    reason: str = ""


@app.post("/authority/flag/{booking_id}")
def flag_booking(booking_id: str, body: FlagRequest = None, authorization: str = Header(...)):
    """
    Flag a booking for review (e.g., vehicle observed without valid booking).
    Writes to the flags collection and audit_logs — does not modify the booking itself.
    """
    actor = _require_authority(authorization)
    reason = body.reason if body else ""
    try:
        if not ds.get_booking_by_id(booking_id):
            raise HTTPException(status_code=404, detail="Booking not found")
        ds.insert_flag(booking_id, reason, REGION)
        ds.flag_booking_record(booking_id, reason)
        ds.log_audit_event(
            action="flag_booking",
            actor_id=actor.get("sub", "unknown"),
            actor_role="AUTHORITY",
            details={"booking_id": booking_id, "reason": reason, "region": REGION},
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}")
    return {"status": "flagged", "booking_id": booking_id, "reason": reason}
