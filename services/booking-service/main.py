import os
import json
import uuid
import httpx
import sys
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

sys.path.insert(0, "/app/shared")

app = FastAPI(title="Booking Service")

SERVICE_NAME = os.getenv("SERVICE_NAME", "booking-service")
REGION = os.getenv("REGION", "local")
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
JOURNEY_MANAGEMENT_URL = os.getenv("JOURNEY_MANAGEMENT_URL", "http://journey-management:8000")


class BookingRequest(BaseModel):
    driver_id: str
    vehicle_id: str
    origin: str
    destination: str
    departure_time: str


def _decode_driver_token(authorization: str) -> dict:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    from auth import decode_token
    try:
        payload = decode_token(authorization.split(" ", 1)[1], os.getenv("JWT_SECRET", "changeme"))
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if payload.get("role") != "DRIVER":
        raise HTTPException(status_code=403, detail="Only DRIVER tokens may create bookings")
    return payload


def _publish_to_kafka(message: dict):
    from kafka import KafkaProducer
    producer = KafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
    )
    producer.send("booking-requests", message)
    producer.flush()


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME, "region": REGION}


@app.post("/bookings", status_code=202)
async def create_booking(booking: BookingRequest, authorization: str = Header(...)):
    """
    Booking entry point:
    1. Validate JWT (DRIVER role required).
    2. Generate booking_id.
    3. Call journey-management POST /plan — the Journey Orchestrator resolves the
       full route using the region overlay graph, delegating each leg to the
       appropriate regional route-service. No global road graph is assembled here.
    4a. Single-region: publish directly to local Kafka booking-requests.
    4b. Cross-region: delegate to journey-management POST /sagas, which fans out
        sub-bookings and manages compensating transactions if any region rejects.
    """
    _decode_driver_token(authorization)

    booking_id = f"bk-{uuid.uuid4().hex[:12]}"

    # ── Step 3: resolve route via Journey Orchestrator ────────────────────────
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{JOURNEY_MANAGEMENT_URL}/plan",
                json={"origin": booking.origin, "destination": booking.destination},
            )
            if resp.status_code in (404, 422):
                raise HTTPException(
                    status_code=422,
                    detail=resp.json().get("detail", f"No route from {booking.origin} to {booking.destination}"),
                )
            resp.raise_for_status()
            plan = resp.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Journey management unavailable: {exc}")

    base_message = {
        "booking_id":     booking_id,
        "driver_id":      booking.driver_id,
        "vehicle_id":     booking.vehicle_id,
        "origin":         booking.origin,
        "destination":    booking.destination,
        "departure_time": booking.departure_time,
        "segments":       plan["segments"],
        "region":         REGION,
    }

    # ── Step 4a: single-region fast path ──────────────────────────────────────
    if not plan.get("is_cross_region", False):
        try:
            _publish_to_kafka({**base_message, "target_region": REGION})
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Kafka unavailable: {exc}")
        return {
            "status":     "accepted",
            "booking_id": booking_id,
            "flow":       "single-region",
            "region":     REGION,
        }

    # ── Step 4b: cross-region saga ────────────────────────────────────────────
    saga_payload = {
        **base_message,
        "segments_by_region": plan["segments_by_region"],
        "regions_involved":   plan["regions_involved"],
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{JOURNEY_MANAGEMENT_URL}/sagas",
                json=saga_payload,
                headers={"Authorization": authorization},
            )
            resp.raise_for_status()
            saga = resp.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Journey management unavailable: {exc}")

    return {
        "status":           "accepted",
        "booking_id":       booking_id,
        "saga_id":          saga.get("saga_id"),
        "flow":             "cross-region-saga",
        "regions_involved": plan["regions_involved"],
    }


@app.get("/bookings/{booking_id}")
async def get_booking_status(booking_id: str, authorization: str = Header(...)):
    """Proxy to journey-management for booking lifecycle queries."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{JOURNEY_MANAGEMENT_URL}/journeys/{booking_id}",
                headers={"Authorization": authorization},
            )
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail="Booking not found")
            resp.raise_for_status()
            return resp.json()
    except HTTPException:
        raise
    except Exception:
        return {"booking_id": booking_id, "status": "unknown", "region": REGION}
