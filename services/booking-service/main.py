"""
Booking Service — CS7NS6 Traffic Booking System
Owner: Dylan Murray (20331999)

Entry point for all journey booking requests.
JWT auth is inlined (no shared/ dependency) for standalone deployment.

Failure handling:
  - Journey management down  → 503 (tenacity retry 3× with backoff)
  - Kafka down               → booking saved to MongoDB as "queued";
                               background outbox thread retries every 10 s
  - MongoDB down             → non-fatal; booking still published to Kafka
  - Duplicate request        → idempotency key (X-Idempotency-Key header)
                               deduplicates within MongoDB
"""
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
import jwt
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, field_validator
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("booking-service")

# ── Config ─────────────────────────────────────────────────────────────────────
SERVICE_NAME            = os.getenv("SERVICE_NAME", "booking-service")
REGION                  = os.getenv("REGION", "local")
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
JOURNEY_MANAGEMENT_URL  = os.getenv("JOURNEY_MANAGEMENT_URL", "http://journey-management:8000")
MONGO_URI               = os.getenv("MONGO_URI", "mongodb://localhost:27017")
JWT_SECRET              = os.getenv("JWT_SECRET", "changeme")

app = FastAPI(title="Booking Service", version="1.0.0")

# ── Kafka producer singleton ───────────────────────────────────────────────────
_kafka_producer = None


def _get_producer():
    global _kafka_producer
    if _kafka_producer is None:
        from kafka import KafkaProducer
        _kafka_producer = KafkaProducer(
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            retries=3,
            acks="all",
        )
    return _kafka_producer


# ── MongoDB ────────────────────────────────────────────────────────────────────
def _get_db():
    from pymongo import MongoClient
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
    return client["traffic"]


# ── Outbox retry (Kafka fault tolerance) ──────────────────────────────────────
def _outbox_worker():
    """
    Background thread: polls MongoDB for bookings with status='queued'
    (written when Kafka was unavailable) and republishes them.
    Runs every 10 seconds. Safe to restart — SET NX semantics via status field.
    """
    log.info("outbox worker started")
    while True:
        time.sleep(10)
        try:
            db = _get_db()
            queued = list(db.bookings.find({"status": "queued"}, {"_id": 0}))
            if not queued:
                continue
            producer = _get_producer()
            for record in queued:
                try:
                    msg = {k: record[k] for k in (
                        "booking_id", "driver_id", "vehicle_id",
                        "origin", "destination", "departure_time", "segments", "region",
                    ) if k in record}
                    msg["target_region"] = record.get("region", REGION)
                    producer.send("booking-requests", msg)
                    producer.flush()
                    db.bookings.update_one(
                        {"booking_id": record["booking_id"]},
                        {"$set": {"status": "pending", "queued_retry_at": datetime.utcnow().isoformat()}},
                    )
                    log.info("outbox.retry booking_id=%s published", record["booking_id"])
                except Exception as exc:
                    log.warning("outbox.retry failed booking_id=%s: %s", record.get("booking_id"), exc)
        except Exception as exc:
            log.warning("outbox.worker error: %s", exc)


@app.on_event("startup")
def _start_outbox():
    t = threading.Thread(target=_outbox_worker, daemon=True)
    t.start()


# ── Auth (inlined — no shared/ dependency) ────────────────────────────────────
def _require_driver(authorization: str) -> dict:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization header must be 'Bearer <token>'")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")
    if payload.get("role") != "DRIVER":
        raise HTTPException(status_code=403, detail="Only DRIVER tokens may create bookings")
    return payload


# ── Models ─────────────────────────────────────────────────────────────────────
class BookingRequest(BaseModel):
    driver_id:      str
    vehicle_id:     str
    origin:         str
    destination:    str
    departure_time: str

    @field_validator("departure_time")
    @classmethod
    def validate_departure_time(cls, v: str) -> str:
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("departure_time must be ISO 8601 (e.g. 2026-04-01T09:00:00Z)")
        return v

    @field_validator("driver_id", "vehicle_id", "origin", "destination")
    @classmethod
    def non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("field must not be blank")
        return v.strip()


# ── HTTP retry helpers ─────────────────────────────────────────────────────────
@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    reraise=True,
)
async def _post(client: httpx.AsyncClient, url: str, **kwargs):
    return await client.post(url, **kwargs)


@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    reraise=True,
)
async def _get(client: httpx.AsyncClient, url: str, **kwargs):
    return await client.get(url, **kwargs)


# ── Health ─────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    checks: dict = {}

    # Kafka
    try:
        from kafka.admin import KafkaAdminClient
        admin = KafkaAdminClient(
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            request_timeout_ms=3000,
        )
        admin.list_topics()
        admin.close()
        checks["kafka"] = "ok"
    except Exception as exc:
        checks["kafka"] = f"error: {exc}"

    # MongoDB
    try:
        db = _get_db()
        db.command("ping")
        checks["mongo"] = "ok"
    except Exception as exc:
        checks["mongo"] = f"error: {exc}"

    # Journey management
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{JOURNEY_MANAGEMENT_URL}/health")
            checks["journey_management"] = "ok" if resp.status_code == 200 else f"http {resp.status_code}"
    except Exception as exc:
        checks["journey_management"] = f"error: {exc}"

    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {
        "status":  overall,
        "service": SERVICE_NAME,
        "region":  REGION,
        "checks":  checks,
    }


# ── Create booking ─────────────────────────────────────────────────────────────
@app.post("/bookings", status_code=202)
async def create_booking(
    booking: BookingRequest,
    authorization: str = Header(...),
    x_idempotency_key: Optional[str] = Header(None),
):
    """
    Booking entry point.

    1. Validate JWT (DRIVER role).
    2. Generate booking_id (UUID).
    3. POST journey-management /plan — the Journey Orchestrator resolves the full
       route using the region overlay graph and delegates each leg to the appropriate
       regional route-service. No global road graph is assembled here.
    4a. Single-region: publish directly to Kafka booking-requests.
    4b. Cross-region: delegate to journey-management POST /sagas (saga coordinator).
    5. Persist accepted booking record to local MongoDB.
    """
    _require_driver(authorization)

    # ── Idempotency check ─────────────────────────────────────────────────────
    if x_idempotency_key:
        try:
            db = _get_db()
            existing = db.bookings.find_one({"idempotency_key": x_idempotency_key}, {"_id": 0})
            if existing:
                log.info("idempotency hit key=%s booking_id=%s", x_idempotency_key, existing.get("booking_id"))
                return existing
        except Exception:
            pass  # MongoDB down — proceed; worst case is a duplicate which saga handles

    booking_id = f"bk-{uuid.uuid4().hex[:12]}"
    log.info(
        "booking.create booking_id=%s driver=%s origin=%s destination=%s",
        booking_id, booking.driver_id, booking.origin, booking.destination,
    )

    # ── Resolve route via Journey Orchestrator ────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await _post(
                client,
                f"{JOURNEY_MANAGEMENT_URL}/plan",
                json={"origin": booking.origin, "destination": booking.destination},
            )
            if resp.status_code in (404, 422):
                raise HTTPException(
                    status_code=422,
                    detail=resp.json().get(
                        "detail",
                        f"No route from {booking.origin} to {booking.destination}",
                    ),
                )
            resp.raise_for_status()
            plan = resp.json()
    except HTTPException:
        raise
    except Exception as exc:
        log.error("journey_management /plan failed: %s", exc)
        raise HTTPException(status_code=503, detail=f"Journey management unavailable: {exc}")

    is_cross = plan.get("is_cross_region", False)

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

    # ── Persist accepted booking to local MongoDB ─────────────────────────────
    record = {
        **base_message,
        "status":     "pending",
        "flow":       "cross-region-saga" if is_cross else "single-region",
        "created_at": datetime.utcnow().isoformat(),
    }
    if is_cross:
        record["regions_involved"] = plan.get("regions_involved", [])
    if x_idempotency_key:
        record["idempotency_key"] = x_idempotency_key
    try:
        db = _get_db()
        db.bookings.insert_one({**record, "_id": booking_id})
    except Exception as exc:
        log.warning("MongoDB write failed (non-fatal): %s", exc)

    # ── Single-region fast path ───────────────────────────────────────────────
    if not is_cross:
        # Use the journey's actual region (not this service's region) so the
        # correct regional validation-service picks up the message.
        journey_region = plan.get("regions_involved", [REGION])[0]
        try:
            producer = _get_producer()
            producer.send("booking-requests", {**base_message, "target_region": journey_region})
            producer.flush()
            log.info("booking.single_region booking_id=%s published to kafka", booking_id)
        except Exception as exc:
            log.error("Kafka publish failed — saving as queued for outbox retry: %s", exc)
            try:
                db = _get_db()
                db.bookings.update_one(
                    {"_id": booking_id},
                    {"$set": {"status": "queued"}},
                )
            except Exception:
                pass
            # Return 202 — outbox worker will publish when Kafka recovers
            return {
                "status":     "queued",
                "booking_id": booking_id,
                "flow":       "single-region",
                "region":     REGION,
                "note":       "Kafka temporarily unavailable; booking queued for retry",
            }
        return {
            "status":     "accepted",
            "booking_id": booking_id,
            "flow":       "single-region",
            "region":     REGION,
        }

    # ── Cross-region saga ─────────────────────────────────────────────────────
    saga_payload = {
        **base_message,
        "segments_by_region": plan["segments_by_region"],
        "regions_involved":   plan["regions_involved"],
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await _post(
                client,
                f"{JOURNEY_MANAGEMENT_URL}/sagas",
                json=saga_payload,
                headers={"Authorization": authorization},
            )
            resp.raise_for_status()
            saga = resp.json()
    except HTTPException:
        raise
    except Exception as exc:
        log.error("journey_management /sagas failed: %s", exc)
        raise HTTPException(status_code=503, detail=f"Journey management unavailable: {exc}")

    saga_id = saga.get("saga_id")

    try:
        db = _get_db()
        db.bookings.update_one({"_id": booking_id}, {"$set": {"saga_id": saga_id}})
    except Exception:
        pass

    log.info("booking.cross_region booking_id=%s saga_id=%s", booking_id, saga_id)
    return {
        "status":           "accepted",
        "booking_id":       booking_id,
        "saga_id":          saga_id,
        "flow":             "cross-region-saga",
        "regions_involved": plan["regions_involved"],
    }


# ── List my bookings ───────────────────────────────────────────────────────────
@app.get("/bookings")
def list_bookings(authorization: str = Header(...)):
    """List all bookings for the authenticated driver (from local MongoDB)."""
    payload = _require_driver(authorization)
    # sub is the username set by user-registry at login
    driver_id = payload.get("sub") or payload.get("driver_id", "")
    try:
        db = _get_db()
        results = list(
            db.bookings
            .find({"driver_id": driver_id}, {"_id": 0})
            .sort("created_at", -1)
            .limit(50)
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {exc}")
    return {"bookings": results, "count": len(results)}


# ── Get single booking ─────────────────────────────────────────────────────────
@app.get("/bookings/{booking_id}")
async def get_booking(booking_id: str, authorization: str = Header(...)):
    """
    Return booking status.
    Tries journey-management first (live state machine), falls back to local MongoDB.
    """
    _require_driver(authorization)

    # Live status from journey-management
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await _get(
                client,
                f"{JOURNEY_MANAGEMENT_URL}/journeys/{booking_id}",
                headers={"Authorization": authorization},
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass

    # Fallback: local MongoDB record
    try:
        db = _get_db()
        record = db.bookings.find_one({"_id": booking_id}, {"_id": 0})
        if record:
            return {**record, "_source": "local-fallback"}
    except Exception:
        pass

    raise HTTPException(status_code=404, detail="Booking not found")


# ── Cancel booking ─────────────────────────────────────────────────────────────
@app.delete("/bookings/{booking_id}", status_code=200)
async def cancel_booking(booking_id: str, authorization: str = Header(...)):
    """
    Cancel a booking.
    - Marks local MongoDB record as 'cancelled'.
    - Delegates to journey-management DELETE /journeys/{booking_id}, which publishes
      capacity-releases to every affected region (compensating transaction).
    """
    _require_driver(authorization)
    log.info("booking.cancel booking_id=%s", booking_id)

    # Mark local record cancelled
    cancelled_locally = False
    try:
        db = _get_db()
        result = db.bookings.update_one(
            {"_id": booking_id},
            {"$set": {"status": "cancelled", "cancelled_at": datetime.utcnow().isoformat()}},
        )
        cancelled_locally = result.matched_count > 0
    except Exception as exc:
        log.warning("MongoDB cancel update failed: %s", exc)

    # Delegate capacity release to journey-management
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.delete(
                f"{JOURNEY_MANAGEMENT_URL}/journeys/{booking_id}",
                headers={"Authorization": authorization},
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404 and not cancelled_locally:
                raise HTTPException(status_code=404, detail="Booking not found")
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("journey_management cancel failed: %s", exc)

    return {"status": "cancelled", "booking_id": booking_id}
