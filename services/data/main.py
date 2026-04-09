"""
Data Service — HTTP API
=======================
Thin FastAPI wrapper around data_service.py.
All other services call this instead of touching MongoDB/Redis directly.
"""
import os
from typing import Any
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import data_service as ds

app = FastAPI(title="Data Service")

REGION = os.getenv("REGION", "global")
SERVICE_NAME = os.getenv("SERVICE_NAME", "data-service")


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME, "region": REGION}


# =============================================================================
# Bookings
# =============================================================================

@app.get("/bookings/vehicle/{vehicle_id}")
def get_vehicle_booking(vehicle_id: str):
    result = ds.get_vehicle_booking_fallback(vehicle_id)
    return result or {}


@app.post("/bookings")
def insert_booking(booking: dict[str, Any]):
    ds.insert_booking(booking)
    return {"ok": True}


@app.get("/bookings/driver/{driver_id}")
def get_bookings_by_driver(driver_id: str):
    return ds.get_bookings_by_driver(driver_id)


@app.get("/bookings/{booking_id}")
def get_booking(booking_id: str):
    result = ds.get_booking_by_id(booking_id)
    if not result:
        raise HTTPException(status_code=404, detail="Booking not found")
    return result


@app.get("/bookings")
def get_all_bookings(limit: int = 50):
    return ds.get_all_bookings(limit)


@app.patch("/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: str, cancelled_at: str):
    ds.cancel_booking_record(booking_id, cancelled_at)
    return {"ok": True}


# =============================================================================
# Sagas
# =============================================================================

@app.post("/sagas")
def create_saga(saga: dict[str, Any]):
    ds.create_saga(saga)
    return {"ok": True}


@app.get("/sagas/{saga_id}")
def get_saga(saga_id: str):
    result = ds.get_saga(saga_id)
    if not result:
        raise HTTPException(status_code=404, detail="Saga not found")
    result.pop("_id", None)
    return result


class OutcomeBody(BaseModel):
    region:  str
    outcome: str
    reason:  str


@app.patch("/sagas/{saga_id}/outcome")
def update_saga_outcome(saga_id: str, body: OutcomeBody):
    result = ds.update_saga_regional_outcome(saga_id, body.region, body.outcome, body.reason)
    if not result:
        raise HTTPException(status_code=404, detail="Saga not found")
    result.pop("_id", None)
    return result


class StatusBody(BaseModel):
    status: str
    extra:  dict[str, Any] = {}


@app.patch("/sagas/{saga_id}/status")
def update_saga_status(saga_id: str, body: StatusBody):
    ds.update_saga_status(saga_id, body.status, body.extra or None)
    return {"ok": True}


# =============================================================================
# Flags and audit
# =============================================================================

class FlagBody(BaseModel):
    booking_id: str
    reason:     str
    region:     str


@app.post("/flags")
def insert_flag(body: FlagBody):
    ds.insert_flag(body.booking_id, body.reason, body.region)
    return {"ok": True}


class AuditBody(BaseModel):
    action:     str
    actor_id:   str
    actor_role: str
    details:    dict[str, Any]


@app.post("/audit")
def log_audit(body: AuditBody):
    ds.log_audit_event(body.action, body.actor_id, body.actor_role, body.details)
    return {"ok": True}
