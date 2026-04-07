"""
Journey Management Service  (Journey Orchestrator)
===================================================
Two responsibilities:

1. Route planning  — POST /plan
   Holds a coarse region-level overlay graph (nodes = regions, edges = border
   corridors). For any origin/destination pair it:
     a) Looks up which region each node belongs to.
     b) Runs Dijkstra on the overlay to get the sequence of regions (legs).
     c) Calls each region's route-service for its leg segment list.
     d) Returns the full combined plan with segments grouped by region.
   No single service ever holds the global road graph — each regional
   route-service only knows its own territory.

2. Booking lifecycle + cross-region saga coordinator
   - GET/DELETE /journeys — retrieve and cancel bookings with compensating txns.
   - POST /sagas — starts a cross-region saga: publishes one sub-booking per
     region to Kafka, collects regional validation outcomes, commits or aborts.
"""

import os
import json
import uuid
import threading
import sys
from datetime import datetime
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
import httpx
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut

sys.path.insert(0, "/app/shared")

_geocoder = Nominatim(user_agent="journey-management-tcd")

# Maps the country name returned by Nominatim to the region served by this system.
# Add entries here as more regional route-service data is imported.
COUNTRY_TO_REGION: dict[str, str] = {
    # andorra region — Andorra imported (isolated, no land corridor to other regions)
    "Andorra":  "andorra",
    # laos region — Laos imported
    "Laos":     "laos",
    # cambodia region — Cambodia imported (connected to laos via Voen Kham/Don Kralor crossing)
    "Cambodia": "cambodia",
}

app = FastAPI(title="Journey Management Service")

SERVICE_NAME = os.getenv("SERVICE_NAME", "journey-management")
REGION = os.getenv("REGION", "local")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
JWT_SECRET = os.getenv("JWT_SECRET", "changeme")
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")

# Per-region route-service URLs.
# In production each points to a separate regional cluster.
# Locally all resolve to the same container (single-region demo mode).
REGION_ROUTE_URLS: dict[str, str] = {
    "andorra":  os.getenv("ANDORRA_ROUTE_URL",  "http://route-service-andorra:8000"),
    "laos":     os.getenv("LAOS_ROUTE_URL",     "http://route-service-laos:8000"),
    "cambodia": os.getenv("CAMBODIA_ROUTE_URL", "http://route-service-cambodia:8000"),
}

# ── Gateway node IDs ──────────────────────────────────────────────────────────
# Real OSM node IDs at physical border crossings.
# These nodes must exist in BOTH adjacent regions' graphs.
#
# To find them once the OSM data is imported, run these queries in mongosh:
#
#   Laos/Cambodia border (Voen Kham/Don Kralor, ~13.92°N 105.79°E):
#   db.osm_nodes.find_one({loc:{$near:{$geometry:{type:"Point",coordinates:[105.79,13.92]},$maxDistance:1000}},region:"laos"})
#   db.osm_nodes.find_one({loc:{$near:{$geometry:{type:"Point",coordinates:[105.79,13.92]},$maxDistance:1000}},region:"cambodia"})
#
GATEWAY_LAOS_EXIT     = os.getenv("GATEWAY_LAOS_EXIT",     "")  # Laos side of Laos/Cambodia border
GATEWAY_CAMBODIA_ENTRY = os.getenv("GATEWAY_CAMBODIA_ENTRY", "") # Cambodia side

# ── Overlay graph ─────────────────────────────────────────────────────────────
# Region-level graph: nodes = regions, edges = land corridors between them.
# andorra has no land corridor to other imported regions (mountainous micro-state).
# "exit" = last node in the departing region's graph at the border crossing.
# "entry" = first node in the arriving region's graph at the border crossing.
# These differ because Laos and Cambodia OSM extracts use separate node IDs.
OVERLAY: dict[str, dict] = {
    "laos": {
        "cambodia": {"exit": GATEWAY_LAOS_EXIT, "entry": GATEWAY_CAMBODIA_ENTRY, "cost_km": 600},
    },
    "cambodia": {
        "laos": {"exit": GATEWAY_CAMBODIA_ENTRY, "entry": GATEWAY_LAOS_EXIT, "cost_km": 600},
    },
    "andorra": {},  # isolated — no land corridor to other imported regions
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_db():
    from pymongo import MongoClient
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
    return client["traffic"]


def _verify(authorization: str) -> dict:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    from auth import decode_token
    try:
        return decode_token(authorization.split(" ", 1)[1], JWT_SECRET)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


def _kafka_producer():
    from kafka import KafkaProducer
    return KafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
    )


# ── Overlay routing helpers ───────────────────────────────────────────────────

def _overlay_shortest_path(origin_region: str, dest_region: str) -> list[str]:
    """Dijkstra on the region overlay graph. Returns ordered list of regions."""
    if origin_region == dest_region:
        return [origin_region]
    # BFS / Dijkstra with cost
    import heapq
    heap = [(0, origin_region, [origin_region])]
    visited = set()
    while heap:
        cost, current, path = heapq.heappop(heap)
        if current in visited:
            continue
        visited.add(current)
        if current == dest_region:
            return path
        for neighbour, edge in OVERLAY.get(current, {}).items():
            if neighbour not in visited:
                heapq.heappush(heap, (cost + edge["cost_km"], neighbour, path + [neighbour]))
    return []  # no path found


def _region_for_place(place: str) -> str:
    """
    Geocode a place name with Nominatim and map the country to a region.
    Raises HTTP 422 if the place is unknown or in an unserved country.
    """
    try:
        location = _geocoder.geocode(place, addressdetails=True, timeout=10, language="en")
    except GeocoderTimedOut:
        raise HTTPException(status_code=503, detail=f"Geocoding timed out for '{place}'")
    if not location:
        raise HTTPException(status_code=404, detail=f"Could not geocode '{place}'")
    country = location.raw.get("address", {}).get("country", "")
    region = COUNTRY_TO_REGION.get(country)
    if not region:
        raise HTTPException(
            status_code=422,
            detail=f"'{place}' is in {country!r} which is not served by this system",
        )
    return region


async def _call_regional_route(region: str, origin: str, destination: str) -> dict:
    """Call a regional route-service for one leg."""
    base_url = REGION_ROUTE_URLS.get(region)
    if not base_url:
        raise HTTPException(status_code=422, detail=f"No route-service configured for region '{region}'")
    # A* on a large graph can take several seconds — allow up to 60s
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(f"{base_url}/routes", params={"origin": origin, "destination": destination})
        if resp.status_code == 404:
            raise HTTPException(
                status_code=422,
                detail=f"No route from '{origin}' to '{destination}' in region '{region}'",
            )
        if resp.status_code == 422:
            body = resp.json()
            if "same node" in body.get("detail", ""):
                # Origin is already at the gateway node — return an empty leg
                return {"segments": [], "total_km": 0, "origin": origin, "destination": destination}
            raise HTTPException(status_code=422, detail=body.get("detail", "Route error"))
        resp.raise_for_status()
        return resp.json()


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME, "region": REGION}


# ── Route planning (Journey Orchestrator) ─────────────────────────────────────

class PlanRequest(BaseModel):
    origin: str
    destination: str


@app.post("/plan")
async def plan_journey(req: PlanRequest):
    """
    Global route planning via the region overlay graph.

    1. Map origin/destination to their regions.
    2. Run Dijkstra on the overlay to get the region sequence (legs).
    3. For each leg, call that region's route-service for the detailed segment list.
    4. Return the combined plan with segments_by_region for the saga coordinator.

    Each regional route-service is only asked about its own territory — no service
    ever holds a monolithic global road graph.
    """
    origin_region = _region_for_place(req.origin)
    dest_region   = _region_for_place(req.destination)

    region_sequence = _overlay_shortest_path(origin_region, dest_region)
    if not region_sequence:
        raise HTTPException(
            status_code=404,
            detail=f"No land corridor exists between '{origin_region}' and '{dest_region}' "
                   f"— regions are separated by ocean with no road connection",
        )

    # Build legs: each leg runs from leg_origin to leg_destination within one region
    legs = []
    all_segments = []
    segments_by_region: dict[str, list] = {}

    leg_origin = req.origin

    for i, region in enumerate(region_sequence):
        # Determine leg destination
        if i < len(region_sequence) - 1:
            next_region = region_sequence[i + 1]
            leg_destination = OVERLAY[region][next_region]["exit"]
        else:
            leg_destination = req.destination

        leg_data = await _call_regional_route(region, leg_origin, leg_destination)
        leg_segments = leg_data.get("segments", [])

        legs.append({
            "leg_index": i,
            "region": region,
            "origin": leg_origin,
            "destination": leg_destination,
            "path": leg_data.get("path", []),
            "segments": leg_segments,
        })
        all_segments.extend(leg_segments)
        segments_by_region[region] = leg_segments

        # Next leg starts at the border entry node of the next region
        if i < len(region_sequence) - 1:
            next_region = region_sequence[i + 1]
            leg_origin = OVERLAY[region][next_region]["entry"]

    is_cross_region = len(region_sequence) > 1

    return {
        "origin": req.origin,
        "destination": req.destination,
        "region_sequence": region_sequence,
        "legs": legs,
        "segments": all_segments,
        "segments_by_region": segments_by_region,
        "regions_involved": region_sequence,
        "is_cross_region": is_cross_region,
    }


# ── Saga coordinator ──────────────────────────────────────────────────────────

class SagaCreate(BaseModel):
    booking_id: str
    driver_id: str
    vehicle_id: str
    origin: str
    destination: str
    departure_time: str
    segments: list
    segments_by_region: dict
    regions_involved: list


@app.post("/sagas", status_code=202)
def create_saga(req: SagaCreate, authorization: str = Header(...)):
    """
    Start a cross-region saga.
    Publishes one sub-booking per region to booking-requests, each tagged with
    saga_id and target_region. The Kafka consumer below collects regional
    outcomes and advances the saga state machine.
    """
    _verify(authorization)

    saga_id = f"saga-{uuid.uuid4().hex[:12]}"
    now = datetime.utcnow().isoformat()

    saga_doc = {
        "saga_id":            saga_id,
        "booking_id":         req.booking_id,
        "driver_id":          req.driver_id,
        "vehicle_id":         req.vehicle_id,
        "origin":             req.origin,
        "destination":        req.destination,
        "departure_time":     req.departure_time,
        "segments_by_region": req.segments_by_region,
        "regions_involved":   req.regions_involved,
        "regional_outcomes":  {},
        "status":             "PENDING",
        "created_at":         now,
    }

    try:
        db = get_db()
        db.sagas.insert_one({**saga_doc, "_id": saga_id})
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {exc}")

    try:
        producer = _kafka_producer()
        for region, segs in req.segments_by_region.items():
            sub_booking = {
                "booking_id":     req.booking_id,
                "saga_id":        saga_id,
                "driver_id":      req.driver_id,
                "vehicle_id":     req.vehicle_id,
                "origin":         segs[0]["from"] if segs else req.origin,
                "destination":    segs[-1]["to"] if segs else req.destination,
                "departure_time": req.departure_time,
                "segments":       segs,
                "target_region":  region,
                "is_sub_booking": True,
            }
            producer.send("booking-requests", sub_booking)
        producer.flush()
    except Exception as exc:
        try:
            get_db().sagas.update_one(
                {"saga_id": saga_id},
                {"$set": {"status": "ABORTED", "abort_reason": str(exc)}},
            )
        except Exception:
            pass
        raise HTTPException(status_code=503, detail=f"Kafka unavailable: {exc}")

    return {"saga_id": saga_id, "status": "PENDING", "regions_involved": req.regions_involved}


@app.get("/sagas/{saga_id}")
def get_saga(saga_id: str, authorization: str = Header(...)):
    """Return current state of a cross-region saga."""
    _verify(authorization)
    try:
        db = get_db()
        saga = db.sagas.find_one({"saga_id": saga_id}, {"_id": 0})
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {exc}")
    if not saga:
        raise HTTPException(status_code=404, detail="Saga not found")
    return saga


def _advance_saga(saga_id: str, region: str, outcome: str, reason: str):
    """
    Called by the Kafka consumer when a regional sub-booking outcome arrives.
    Advances the saga state machine and triggers compensation if needed.
    """
    try:
        db = get_db()
        saga = db.sagas.find_one({"saga_id": saga_id})
        if not saga or saga["status"] not in ("PENDING", "ABORTING"):
            return

        db.sagas.update_one(
            {"saga_id": saga_id},
            {"$set": {f"regional_outcomes.{region}": {"outcome": outcome, "reason": reason}}},
        )
        saga = db.sagas.find_one({"saga_id": saga_id})
        outcomes = saga.get("regional_outcomes", {})
        regions_involved = saga["regions_involved"]

        if not all(r in outcomes for r in regions_involved):
            return  # still waiting

        any_rejected = any(o["outcome"] == "REJECTED" for o in outcomes.values())
        producer = _kafka_producer()

        if any_rejected:
            db.sagas.update_one({"saga_id": saga_id}, {"$set": {"status": "ABORTING"}})
            for r, o in outcomes.items():
                if o["outcome"] == "APPROVED":
                    producer.send("capacity-releases", {
                        "booking_id":    saga["booking_id"],
                        "saga_id":       saga_id,
                        "target_region": r,
                        "driver_id":     saga["driver_id"],
                        "vehicle_id":    saga["vehicle_id"],
                        "segments":      saga["segments_by_region"].get(r, []),
                    })
            producer.flush()
            db.sagas.update_one({"saga_id": saga_id}, {"$set": {"status": "ABORTED"}})
            producer.send("booking-outcomes", {
                "booking_id":        saga["booking_id"],
                "saga_id":           saga_id,
                "driver_id":         saga["driver_id"],
                "outcome":           "REJECTED",
                "reason":            "One or more regions rejected the sub-booking",
                "regional_outcomes": outcomes,
            })
        else:
            db.bookings.insert_one({
                "booking_id":       saga["booking_id"],
                "saga_id":          saga_id,
                "driver_id":        saga["driver_id"],
                "vehicle_id":       saga["vehicle_id"],
                "origin":           saga["origin"],
                "destination":      saga["destination"],
                "departure_time":   saga["departure_time"],
                "regions_involved": regions_involved,
                "status":           "approved",
                "created_at":       datetime.utcnow().isoformat(),
            })
            db.sagas.update_one({"saga_id": saga_id}, {"$set": {"status": "COMMITTED"}})
            producer.send("booking-outcomes", {
                "booking_id":        saga["booking_id"],
                "saga_id":           saga_id,
                "driver_id":         saga["driver_id"],
                "outcome":           "APPROVED",
                "reason":            "All regions approved",
                "regional_outcomes": outcomes,
            })
        producer.flush()

    except Exception as exc:
        print(f"[journey-management] saga advance error for {saga_id}: {exc}")


def _saga_outcome_consumer():
    from kafka import KafkaConsumer
    try:
        consumer = KafkaConsumer(
            "booking-outcomes",
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            value_deserializer=lambda m: json.loads(m.decode("utf-8")),
            group_id="saga-coordinator-group",
            auto_offset_reset="earliest",
        )
        for msg in consumer:
            payload = msg.value
            saga_id = payload.get("saga_id")
            if not saga_id:
                continue
            region = payload.get("region", payload.get("target_region", "unknown"))
            _advance_saga(saga_id, region, payload.get("outcome", "REJECTED"), payload.get("reason", ""))
    except Exception as exc:
        print(f"[journey-management] Kafka consumer error: {exc}")


@app.on_event("startup")
def start_saga_consumer():
    t = threading.Thread(target=_saga_outcome_consumer, daemon=True)
    t.start()


# ── Booking lifecycle endpoints ───────────────────────────────────────────────

@app.get("/journeys")
def list_journeys(authorization: str = Header(...)):
    payload = _verify(authorization)
    driver_id = payload.get("sub")
    try:
        db = get_db()
        bookings = list(db.bookings.find({"driver_id": driver_id}, {"_id": 0}))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {exc}")
    return {"driver_id": driver_id, "bookings": bookings, "region": REGION}


@app.get("/journeys/{booking_id}")
def get_journey(booking_id: str, authorization: str = Header(...)):
    _verify(authorization)
    try:
        db = get_db()
        booking = db.bookings.find_one({"booking_id": booking_id}, {"_id": 0})
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {exc}")
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    return booking


@app.delete("/journeys/{booking_id}", status_code=200)
def cancel_journey(booking_id: str, authorization: str = Header(...)):
    """
    Cancel a booking with compensating transactions.
    For cross-region bookings: publishes one capacity-release per involved
    region (each tagged with target_region) so each regional validation-service
    decrements its own Redis counter.
    """
    payload = _verify(authorization)
    try:
        db = get_db()
        booking = db.bookings.find_one({"booking_id": booking_id}, {"_id": 0})
        if not booking:
            raise HTTPException(status_code=404, detail="Booking not found")
        if booking.get("driver_id") != payload.get("sub"):
            raise HTTPException(status_code=403, detail="Not your booking")
        if booking.get("status") == "cancelled":
            raise HTTPException(status_code=409, detail="Already cancelled")
        db.bookings.update_one(
            {"booking_id": booking_id},
            {"$set": {"status": "cancelled", "cancelled_at": datetime.utcnow().isoformat()}},
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {exc}")

    try:
        producer = _kafka_producer()
        saga_id = booking.get("saga_id")
        regions = booking.get("regions_involved", [REGION])

        if saga_id:
            db = get_db()
            saga = db.sagas.find_one({"saga_id": saga_id}) or {}
            segments_by_region = saga.get("segments_by_region", {})
            for r in regions:
                producer.send("capacity-releases", {
                    "booking_id":    booking_id,
                    "saga_id":       saga_id,
                    "driver_id":     booking["driver_id"],
                    "vehicle_id":    booking["vehicle_id"],
                    "target_region": r,
                    "segments":      segments_by_region.get(r, []),
                    "status":        "cancelled",
                })
        else:
            producer.send("capacity-releases", {**booking, "status": "cancelled"})
        producer.flush()
    except Exception:
        pass  # Non-fatal

    return {"status": "cancelled", "booking_id": booking_id}
