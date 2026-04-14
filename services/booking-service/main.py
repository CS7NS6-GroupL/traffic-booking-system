import os
import json
import uuid
import httpx
import sys
import logging
import queue
import threading
import time
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, field_validator
from datetime import datetime as dt
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("booking-service")

sys.path.insert(0, "/app/shared")

app = FastAPI(title="Booking Service")

SERVICE_NAME = os.getenv("SERVICE_NAME", "booking-service")
REGION = os.getenv("REGION", "local")
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
JOURNEY_MANAGEMENT_URL = os.getenv("JOURNEY_MANAGEMENT_URL", "http://journey-management:8000")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis-laos:6379")

# ── Kafka singleton producer ──────────────────────────────────────────────────
_kafka_producer = None
_kafka_lock = threading.Lock()

def _get_producer():
    global _kafka_producer
    with _kafka_lock:
        if _kafka_producer is None:
            from kafka import KafkaProducer
            _kafka_producer = KafkaProducer(
                bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
                value_serializer=lambda v: json.dumps(v).encode(),
                retries=3,
                acks="all",
            )
            log.info("kafka.producer initialized")
    return _kafka_producer

# ── In-memory outbox ──────────────────────────────────────────────────────────
_outbox: queue.Queue = queue.Queue()

def _outbox_worker():
    while True:
        time.sleep(5)
        batch = []
        while not _outbox.empty():
            batch.append(_outbox.get_nowait())
        failed = []
        for msg in batch:
            try:
                _get_producer().send("booking-requests", msg)
                log.info("outbox.retry queued booking_id=%s", msg.get("booking_id"))
            except Exception as exc:
                log.warning("outbox.retry failed booking_id=%s: %s", msg.get("booking_id"), exc)
                failed.append(msg)
        if batch:
            try:
                _get_producer().flush()
            except Exception as exc:
                log.warning("outbox.flush failed: %s", exc)
                failed.extend(m for m in batch if m not in failed)
        for msg in failed:
            _outbox.put(msg)

def _publish_to_kafka(message: dict):
    try:
        _get_producer().send("booking-requests", message)
        _get_producer().flush()
    except Exception as exc:
        log.warning("kafka.send failed booking_id=%s — queuing in outbox: %s",
                    message.get("booking_id"), exc)
        _outbox.put(message)

# ── Idempotency cache (Redis-backed, shared across both instances) ─────────────
IDEM_TTL        = 300   # seconds
IDEM_KEY_PREFIX = "idem:"

_redis_client      = None
_redis_client_lock = threading.Lock()

def _get_redis():
    global _redis_client
    with _redis_client_lock:
        if _redis_client is None:
            import redis as redis_lib
            _redis_client = redis_lib.from_url(REDIS_URL, decode_responses=True)
    return _redis_client

def _check_idem(key: str):
    """Return the cached response for this idempotency key, or None."""
    try:
        raw = _get_redis().get(f"{IDEM_KEY_PREFIX}{key}")
        if raw:
            return json.loads(raw)
    except Exception as exc:
        log.warning("idem.check failed (Redis unavailable) — proceeding without cache: %s", exc)
    return None

def _store_idem(key: str, r):
    """Store the response under this idempotency key with TTL."""
    try:
        _get_redis().setex(f"{IDEM_KEY_PREFIX}{key}", IDEM_TTL, json.dumps(r))
    except Exception as exc:
        log.warning("idem.store failed (Redis unavailable): %s", exc)

# ── tenacity retry helpers for JM HTTP calls ──────────────────────────────────
@retry(retry=retry_if_exception_type(httpx.TransportError),
       stop=stop_after_attempt(3), wait=wait_exponential(min=0.5, max=4), reraise=True)
async def _jm_request(client, method: str, url, **kw):
    return await getattr(client, method)(url, **kw)


async def _jm_post(client, url, **kw):
    return await _jm_request(client, "post", url, **kw)


async def _jm_get(client, url, **kw):
    return await _jm_request(client, "get", url, **kw)


class BookingRequest(BaseModel):
    driver_id: str
    vehicle_id: str
    origin: str
    destination: str
    departure_time: str

    @field_validator("driver_id", "vehicle_id", "origin", "destination")
    @classmethod
    def non_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("must not be blank")
        return v.strip()

    @field_validator("departure_time")
    @classmethod
    def iso8601(cls, v):
        try:
            dt.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("must be ISO 8601 e.g. 2026-04-01T09:00")
        return v


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


@app.on_event("startup")
def _start_outbox():
    threading.Thread(target=_outbox_worker, daemon=True).start()
    log.info("booking-service started region=%s", REGION)


@app.get("/health")
async def health():
    checks = {}
    try:
        from kafka.admin import KafkaAdminClient
        a = KafkaAdminClient(bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS, request_timeout_ms=3000)
        a.list_topics()
        a.close()
        checks["kafka"] = "ok"
    except Exception as e:
        checks["kafka"] = f"error: {e}"
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{JOURNEY_MANAGEMENT_URL}/health")
            checks["journey_management"] = "ok" if r.status_code == 200 else f"http {r.status_code}"
    except Exception as e:
        checks["journey_management"] = f"error: {e}"
    checks["outbox_queued"] = _outbox.qsize()
    ok = all(v == "ok" for k, v in checks.items() if k != "outbox_queued")
    return {"status": "ok" if ok else "degraded", "service": SERVICE_NAME,
            "region": REGION, "checks": checks}


@app.post("/bookings", status_code=202)
async def create_booking(
    booking: BookingRequest,
    authorization: str = Header(...),
    x_idempotency_key: str = Header(None),
):
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

    if x_idempotency_key:
        cached = _check_idem(x_idempotency_key)
        if cached is not None:
            return cached

    booking_id = f"bk-{uuid.uuid4().hex[:12]}"

    # ── Step 3: resolve route via Journey Orchestrator ────────────────────────
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await _jm_post(
                client,
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
        # Use the journey's actual region (not this service's region) so the
        # correct regional validation-service picks up the message.
        journey_region = plan.get("regions_involved", [REGION])[0]
        _publish_to_kafka({**base_message, "target_region": journey_region})
        result = {
            "status":     "accepted",
            "booking_id": booking_id,
            "flow":       "single-region",
            "region":     REGION,
        }
        if x_idempotency_key:
            _store_idem(x_idempotency_key, result)
        return result

    # ── Step 4b: cross-region saga ────────────────────────────────────────────
    saga_payload = {
        **base_message,
        "segments_by_region": plan["segments_by_region"],
        "regions_involved":   plan["regions_involved"],
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await _jm_post(
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
        raise HTTPException(status_code=503, detail=f"Journey management unavailable: {exc}")

    result = {
        "status":           "accepted",
        "booking_id":       booking_id,
        "saga_id":          saga.get("saga_id"),
        "flow":             "cross-region-saga",
        "regions_involved": plan["regions_involved"],
    }
    if x_idempotency_key:
        _store_idem(x_idempotency_key, result)
    return result


@app.get("/bookings/{booking_id}")
async def get_booking_status(booking_id: str, authorization: str = Header(...)):
    """Proxy to journey-management for booking lifecycle queries."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await _jm_get(
                client,
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
